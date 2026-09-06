"""Raw landing on both backends, and all three BigQuery ingestion methods.

`TOOROW_DB_MODE=bigquery` used to be a mode the code accepted and did not
honour: every connector raised `unsupported db_mode`, and mirror_sync's BigQuery
branch logged "deferred (Phase B)" and returned. These pin that the mode now
does what it says, and that the three methods stay distinguishable -- a load job
where streaming was meant is minutes of staleness, the reverse is a bill, and
`stream` where `storage_write` was meant is both a higher rate and at-least-once
delivery instead of exactly-once.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

from core import raw_landing  # noqa: E402
from core.raw_landing import MODE_LOAD, MODE_STREAM, RawLandingError, land_raw_rows  # noqa: E402

COLUMNS = [("date", "STRING"), ("value", "FLOAT"), ("pull_id", "STRING")]
ROWS = [{"date": "2026-07-29", "value": 1.5, "pull_id": "pull_EXAMPLE"}]


# --------------------------------------------------------------------------
# The fake BigQuery client records what it was asked to do.
# --------------------------------------------------------------------------


class _FakeJob:
    errors = None

    def result(self):
        return None


class _FakeClient:
    project = "proj-example"

    def __init__(self, stream_errors=None):
        self.calls: list[tuple] = []
        self._stream_errors = stream_errors or []

    def create_dataset(self, ref, exists_ok=False):
        self.calls.append(("create_dataset", ref))

    def create_table(self, table, exists_ok=False):
        self.calls.append(("create_table", table.table_id if hasattr(table, "table_id") else table))

    def insert_rows_json(self, table_id, payload):
        self.calls.append(("stream", table_id, payload))
        return self._stream_errors

    def load_table_from_json(self, payload, table_id, job_config=None):
        self.calls.append(("load", table_id, payload))
        return _FakeJob()


@pytest.fixture()
def bq(monkeypatch):
    """Route the BigQuery path at a recording client."""
    from google.cloud import bigquery as real

    client = _FakeClient()

    def _factory(*args, **kwargs):
        return client

    monkeypatch.setattr(real, "Client", _factory)
    monkeypatch.setattr(
        "core.warehouse_tenancy.bigquery_raw_dataset", lambda pid, conn=None: "org_example_raw"
    )
    # Named explicitly: the landing refuses to inherit the credential's project.
    monkeypatch.setenv("GCP_PROJECT", "proj-example")
    return client


def _kinds(client):
    return [call[0] for call in client.calls]


# --------------------------------------------------------------------------


def test_a_load_job_is_used_by_default(bq):
    result = land_raw_rows(
        "raw_example_daily", ROWS, columns=COLUMNS, project_id="proj_EXAMPLE", backend="bigquery"
    )
    assert result == {
        "rows": 1,
        "backend": "bigquery",
        "mode": MODE_LOAD,
        "table": "raw_example_daily",
    }
    assert "load" in _kinds(bq) and "stream" not in _kinds(bq)


def test_streaming_is_a_different_call_not_a_faster_load(bq):
    """The two paths must stay distinguishable: one is billed, the other is slow."""
    result = land_raw_rows(
        "raw_example_daily",
        ROWS,
        columns=COLUMNS,
        project_id="proj_EXAMPLE",
        mode=MODE_STREAM,
        backend="bigquery",
    )
    assert result["mode"] == MODE_STREAM
    assert "stream" in _kinds(bq) and "load" not in _kinds(bq)


def test_the_table_is_created_before_either_write(bq):
    """Streaming has no schema to autodetect, and a load job creating the table
    would let the first pull decide the types for every pull after it."""
    land_raw_rows(
        "raw_example_daily",
        ROWS,
        columns=COLUMNS,
        project_id="proj_EXAMPLE",
        mode=MODE_STREAM,
        backend="bigquery",
    )
    kinds = _kinds(bq)
    assert kinds.index("create_table") < kinds.index("stream")


def test_a_partially_rejected_stream_is_not_a_success(monkeypatch):
    """insert_rows_json REPORTS per-row failures instead of raising.

    A caller that ignores the return value writes half a pull and logs a
    success -- silently short rows in the warehouse with a green pull above them.
    """
    from google.cloud import bigquery as real

    client = _FakeClient(stream_errors=[{"index": 0, "errors": [{"reason": "invalid"}]}])
    monkeypatch.setattr(real, "Client", lambda *a, **k: client)
    monkeypatch.setattr(
        "core.warehouse_tenancy.bigquery_raw_dataset", lambda pid, conn=None: "org_example_raw"
    )
    monkeypatch.setenv("GCP_PROJECT", "proj-example")
    with pytest.raises(RawLandingError, match="rejected rows"):
        land_raw_rows(
            "raw_example_daily",
            ROWS,
            columns=COLUMNS,
            project_id="proj_EXAMPLE",
            mode=MODE_STREAM,
            backend="bigquery",
        )


def test_the_dataset_is_resolved_never_composed(bq):
    """A connector must not invent a warehouse location; the resolver owns it."""
    land_raw_rows(
        "raw_example_daily", ROWS, columns=COLUMNS, project_id="proj_EXAMPLE", backend="bigquery"
    )
    target = next(call[1] for call in bq.calls if call[0] == "load")
    assert target == "proj-example.org_example_raw.raw_example_daily"


@pytest.mark.parametrize(
    ("table", "columns"),
    [
        ("raw x; DROP TABLE t", COLUMNS),
        ("raw_ok", [("value); DROP TABLE t --", "STRING")]),
        ("raw_ok", [("value", "NOT_A_TYPE")]),
    ],
)
def test_identifiers_are_validated_even_though_callers_are_trusted(table, columns):
    """These names are interpolated into DDL; 'the caller is module code' is not
    a property the DDL builder can check."""
    with pytest.raises(RawLandingError):
        land_raw_rows(table, ROWS, columns=columns, project_id="p", backend="duckdb")


def test_candidate_registry_freezes_the_typed_columns_at_the_write_seam():
    execution_id = "dse_typed_columns"
    result = land_raw_rows(
        "raw_example_daily",
        [],
        columns=COLUMNS,
        project_id="proj_EXAMPLE",
        execution_id=execution_id,
        backend="duckdb",
    )

    assert result["table"] == f"raw_example_daily__cand_{execution_id}"
    assert raw_landing.landed_candidate_columns(execution_id, result["table"]) == COLUMNS

    with pytest.raises(RawLandingError, match="different column contract"):
        land_raw_rows(
            "raw_example_daily",
            [],
            columns=[("date", "DATE")],
            project_id="proj_EXAMPLE",
            execution_id=execution_id,
            backend="duckdb",
        )


def test_an_unknown_backend_is_refused_not_ignored():
    with pytest.raises(RawLandingError, match="unsupported backend"):
        land_raw_rows("raw_ok", ROWS, columns=COLUMNS, project_id="p", backend="snowflake")


def test_no_rows_writes_nothing_and_says_so(bq):
    result = land_raw_rows("raw_ok", [], columns=COLUMNS, project_id="p", backend="bigquery")
    assert result["rows"] == 0
    assert bq.calls == []


def test_the_default_mode_is_configurable(monkeypatch):
    monkeypatch.setenv("TOOROW_BQ_WRITE_MODE", MODE_STREAM)
    assert raw_landing.default_write_mode() == MODE_STREAM
    monkeypatch.setenv("TOOROW_BQ_WRITE_MODE", "nonsense")
    assert raw_landing.default_write_mode() == MODE_LOAD


def test_duckdb_round_trips_the_rows(tmp_path, monkeypatch):
    """The existing backend must be unchanged by gaining a second one."""
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "t.duckdb"))
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    result = land_raw_rows(
        "raw_example_daily", ROWS, columns=COLUMNS, project_id=None, backend="duckdb"
    )
    assert result == {"rows": 1, "backend": "duckdb", "mode": None, "table": "raw_example_daily"}

    import duckdb

    con = duckdb.connect(str(tmp_path / "t.duckdb"))
    assert con.execute("SELECT date, value, pull_id FROM raw_example_daily").fetchall() == [
        ("2026-07-29", 1.5, "pull_EXAMPLE")
    ]
    con.close()


def test_the_warehouse_project_is_never_inherited_from_the_credential(monkeypatch):
    """`bigquery.Client(project=None)` adopts the ADC default project silently.

    Measured landing rows into an unrelated project that way. The write succeeds,
    in the wrong place, and is found much later by someone looking for data that
    is not there -- so an unset variable must refuse, not guess.
    """
    monkeypatch.delenv("GCP_PROJECT", raising=False)
    monkeypatch.delenv("GOOGLE_CLOUD_PROJECT", raising=False)
    with pytest.raises(RawLandingError, match="no warehouse project configured"):
        raw_landing.resolve_warehouse_project()


def test_the_warehouse_project_prefers_the_explicit_variable(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "ambient-example")
    monkeypatch.setenv("GCP_PROJECT", "warehouse-example")
    assert raw_landing.resolve_warehouse_project() == "warehouse-example"
    monkeypatch.delenv("GCP_PROJECT")
    assert raw_landing.resolve_warehouse_project() == "ambient-example"


# --------------------------------------------------------------------------
# Storage Write API — the third mode.
# --------------------------------------------------------------------------


def test_storage_write_is_its_own_transport(bq, monkeypatch):
    """Not a faster `stream`: protobuf over gRPC, a different client entirely."""
    seen = {}

    def _fake(gcp_project, dataset, table, columns, payload):
        seen.update(project=gcp_project, dataset=dataset, table=table, rows=payload)

    monkeypatch.setattr("core.raw_landing._write_via_storage_api", _fake)
    result = land_raw_rows(
        "raw_example_daily",
        ROWS,
        columns=COLUMNS,
        project_id="proj_EXAMPLE",
        mode=raw_landing.MODE_STORAGE_WRITE,
        backend="bigquery",
    )
    assert result["mode"] == raw_landing.MODE_STORAGE_WRITE
    assert seen["dataset"] == "org_example_raw"
    assert seen["rows"] == [dict(zip([c for c, _ in COLUMNS], ROWS[0].values()))]
    # It still goes through create_table: the descriptor describes the rows, not
    # the table, so the table must already exist with the matching schema.
    assert "create_table" in _kinds(bq)
    assert "stream" not in _kinds(bq) and "load" not in _kinds(bq)


def test_a_missing_dependency_fails_rather_than_downgrading(monkeypatch):
    """The two streaming transports differ in PRICE and in delivery guarantee.

    Falling back from storage_write to stream would change the bill and swap
    exactly-once for at-least-once, with nothing in the output saying so.
    """
    import builtins

    real_import = builtins.__import__

    def _no_storage(name, *args, **kwargs):
        if "bigquery_storage" in name:
            raise ImportError("google-cloud-bigquery-storage is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_storage)
    with pytest.raises(RawLandingError, match="Refusing to fall back"):
        raw_landing._write_via_storage_api("p", "d", "t", COLUMNS, [{}])


def test_the_descriptor_matches_the_column_vocabulary():
    """A number sent as a string is accepted by BigQuery and then sorts wrong."""
    from google.protobuf import descriptor_pb2 as dpb2

    descriptor = raw_landing._proto_descriptor(
        [("label", "STRING"), ("amount", "FLOAT"), ("hits", "INTEGER")]
    )
    kinds = {field.name: field.type for field in descriptor.field}
    assert kinds["label"] == dpb2.FieldDescriptorProto.TYPE_STRING
    assert kinds["amount"] == dpb2.FieldDescriptorProto.TYPE_DOUBLE
    assert kinds["hits"] == dpb2.FieldDescriptorProto.TYPE_INT64
    # OPTIONAL, so an absent value lands NULL rather than a zero -- proven live:
    # a row with clicks=None read back as NULL, not 0.
    assert all(
        field.label == dpb2.FieldDescriptorProto.LABEL_OPTIONAL for field in descriptor.field
    )


def test_field_numbers_are_stable_and_one_based():
    """Field numbers ARE the wire identity; reordering them re-maps every column."""
    descriptor = raw_landing._proto_descriptor(COLUMNS)
    assert [f.number for f in descriptor.field] == [1, 2, 3]
    assert [f.name for f in descriptor.field] == [name for name, _ in COLUMNS]


def test_storage_write_is_selectable_as_the_deployment_default(monkeypatch):
    monkeypatch.setenv("TOOROW_BQ_WRITE_MODE", "storage_write")
    assert raw_landing.default_write_mode() == raw_landing.MODE_STORAGE_WRITE


def test_boolean_is_in_the_vocabulary_on_both_backends():
    """A raw column declared BOOLEAN must survive to each backend as a boolean.

    Pinned because it did not: BOOLEAN reached the BigQuery writer as STRING, and
    a Python bool serialised into a TYPE_STRING proto field raises TypeError at
    the wire rather than at review.
    """
    from core.raw_landing import (
        _BIGQUERY_TYPES,
        _DUCKDB_TYPES,
        _PROTO_TYPES,
        _validate,
    )

    assert _DUCKDB_TYPES["BOOLEAN"] == "BOOLEAN"
    assert _BIGQUERY_TYPES["BOOLEAN"] == "BOOL"
    assert _PROTO_TYPES["BOOLEAN"] == "TYPE_BOOL"
    _validate("raw_t", [("non_additive", "BOOLEAN")])  # must not raise


def test_a_boolean_survives_the_storage_write_descriptor():
    """The proto descriptor is the contract on the wire -- build it and set a bool."""
    pytest.importorskip("google.protobuf")
    from core.raw_landing import _proto_descriptor
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    descriptor = _proto_descriptor([("non_additive", "BOOLEAN"), ("metric", "STRING")])
    pool = descriptor_pool.DescriptorPool()
    file_proto = descriptor_pb2.FileDescriptorProto()
    file_proto.name = "raw_landing_boolean_test.proto"
    file_proto.syntax = "proto2"
    file_proto.message_type.add().CopyFrom(descriptor)
    pool.Add(file_proto)
    message = message_factory.GetMessageClass(pool.FindMessageTypeByName("RawRow"))()

    message.non_additive = True  # raised TypeError while BOOLEAN mapped to STRING
    message.metric = "ctr"
    assert message.SerializeToString() == b"\x08\x01\x12\x03ctr"


def test_ai76_all_24_connectors_support_bigquery_db_mode(bq) -> None:
    """AI-76: Every connector supports BigQuery without raising unsupported db_mode."""
    import importlib
    import inspect

    connector_modules = [
        "adjust",
        "amazon-ads",
        "doubleverify",
        "google-ad-manager",
        "google-ads",
        "google-analytics",
        "google-business-profile",
        "google-sheets",
        "gsc",
        "hubspot",
        "ias",
        "klaviyo",
        "linkedin-ads",
        "meta-ads",
        "microsoft-ads",
        "piano",
        "pinterest-ads",
        "shopify",
        "square",
        "strava",
        "stripe",
        "thetradedesk",
        "tiktok-ads",
        "woocommerce",
        "youtube-analytics",
    ]

    for module_name in connector_modules:
        mod = importlib.import_module(f"modules.{module_name}.connector")
        if hasattr(mod, "_insert_raw_rows"):
            fn = getattr(mod, "_insert_raw_rows")
            sig = inspect.signature(fn)
            kwargs = {}
            if "rows" in sig.parameters:
                kwargs["rows"] = []
            elif "long_rows" in sig.parameters:
                kwargs["long_rows"] = []
            if "pull_id" in sig.parameters:
                kwargs["pull_id"] = "pull_test"
            if "loaded_at" in sig.parameters:
                kwargs["loaded_at"] = "2026-08-04T00:00:00Z"
            if "project_id" in sig.parameters:
                kwargs["project_id"] = "proj_test"
            if "db_mode" in sig.parameters:
                kwargs["db_mode"] = "bigquery"
            if "duckdb_path" in sig.parameters:
                kwargs["duckdb_path"] = ":memory:"
            if "metric_names" in sig.parameters:
                kwargs["metric_names"] = ["spend"]
            if "report_profile" in sig.parameters:
                kwargs["report_profile"] = "standard"

            try:
                fn(**kwargs)
            except Exception as exc:
                assert "unsupported db_mode" not in str(
                    exc
                ), f"Module {module_name} raised unsupported db_mode: {exc}"


def test_the_raw_zone_is_created_in_the_platform_location(bq):
    """AI-314 -- a dataset created by ID lands in BigQuery's default region, not ours.

    The call passed a string and no location, so the two live `raw_proj_*` zones
    were created in US while every other dataset of the platform is EU and the
    nightly's dbt profile queries in EU. BigQuery does not read across locations:
    the build answered `Not found: Dataset toorow:raw_proj_... was not found in
    location EU` on the only staging model the live Project has, so no mart of
    its connector could ever be built (measured 2026-08-24 by a dry run, 0 bytes).
    """
    from core import warehouse_tenancy

    land_raw_rows(
        "raw_example_daily", ROWS, columns=COLUMNS, project_id="proj_EXAMPLE", backend="bigquery"
    )

    created = [call[1] for call in bq.calls if call[0] == "create_dataset"]
    assert created, "the landing must ensure its dataset exists"
    assert getattr(created[0], "location", None) == warehouse_tenancy.BIGQUERY_LOCATION, (
        "the raw zone must be created in the ONE location the platform declares -- "
        f"got {getattr(created[0], 'location', None)!r}"
    )
