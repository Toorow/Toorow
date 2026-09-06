"""The typed setup client, pinned offline (Story 57.1).

There is no BigQuery test account and there will not be one for this story, so a
double stands where `google.cloud.bigquery.Client` stands. What these tests prove
is OUR half of the contract: that we estimate before we read, that we never offer
a folder or an array as a column, that we refuse an identifier that is not one,
and that a failed estimate is `None` rather than a fabricated zero. What the real
API answers is the provider's half, and a double supposes it.

The pattern is copied from `test_selector.py` rather than reinvented: the real
class is importable, so the monkeypatch lands on the real symbol.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_MODULES = Path(__file__).resolve().parents[3] / "modules"
if str(_MODULES) not in sys.path:
    sys.path.insert(0, str(_MODULES))

from bigquery.connector import BigQuerySetupReader  # noqa: E402

from tests.modules.bigquery.test_selector import BILLING_SCHEMA, _Field  # noqa: E402


class _FakeQueryJob:
    """A planned job. `result()` would BE the scan, so it raises if called."""

    def __init__(self, total_bytes_processed):
        self.total_bytes_processed = total_bytes_processed

    def result(self):  # pragma: no cover -- calling it is the defect
        raise AssertionError("A dry-run estimate must never fetch a result")


class _FakeTable:
    def __init__(self, schema):
        self.schema = schema
        self.location = "EU"
        self.num_rows = 1_200
        self.num_bytes = 4_096


class _FakeClient:
    """Enough of bigquery.Client for one described table and one estimate."""

    project = "toorow"

    def __init__(self, *, schema=None, total_bytes=8_192, query_raises=False):
        self._schema = BILLING_SCHEMA if schema is None else schema
        self._total_bytes = total_bytes
        self._query_raises = query_raises
        self.job_configs: list[object] = []
        self.statements: list[str] = []

    def get_table(self, table_ref):
        return _FakeTable(self._schema)

    def query(self, sql, job_config=None):
        self.statements.append(sql)
        self.job_configs.append(job_config)
        if self._query_raises:
            raise RuntimeError("dry run refused")
        return _FakeQueryJob(self._total_bytes)


@pytest.fixture()
def fake_bigquery(monkeypatch):
    from google.cloud import bigquery as real

    client = _FakeClient()
    monkeypatch.setattr(real, "Client", lambda *a, **k: client)
    return client


def test_a_struct_and_its_leaf_are_both_described(fake_bigquery):
    """A NESTED FIELD IS DESCRIBED, and the description says it is a group.

    The first version of this client filtered emission on `roles`, so no RECORD
    and no REPEATED field ever came out. Every dimension of a GCP billing export
    is nested, so an operator saw a table missing half its columns with nothing
    saying why. `service` is now listed as the `RECORD` it is, beside the
    `service.description` leaf that CAN be selected.
    """
    metadata = BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")
    by_name = {field["name"]: field for field in metadata["fields"]}
    assert "service.description" in by_name
    assert by_name["service"]["type"] == "RECORD"
    assert by_name["service"]["mode"] == "NULLABLE"


def test_a_repeated_record_reads_as_a_repeated_record(fake_bigquery):
    """`credits` is a REPEATED RECORD and says so; its children are not walked.

    This is the line the plan asked for in place of an example value: the mode
    tells an operator more about the column than one of its rows would, and it
    takes nothing out of the customer's warehouse.
    """
    metadata = BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")
    by_name = {field["name"]: field for field in metadata["fields"]}
    assert by_name["credits"]["type"] == "RECORD"
    assert by_name["credits"]["mode"] == "REPEATED"
    # No UNNEST exists in this product, so nothing UNDER an array is read.
    assert "credits.amount" not in by_name
    # Described, and refused as a column. Counted so the screen can mark them.
    assert metadata["unselectable_field_count"] == 2


def test_a_container_field_is_never_offered_as_a_column(fake_bigquery):
    """The other half of the same decision, checked against the shared rule."""
    import sys
    from pathlib import Path

    server_root = Path(__file__).resolve().parents[3]
    if str(server_root) not in sys.path:
        sys.path.insert(0, str(server_root))
    from core.datastream_setup_observations import is_container_field

    metadata = BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")
    refused = {field["name"] for field in metadata["fields"] if is_container_field(field)}
    assert refused == {"service", "credits"}


def test_every_emitted_field_carries_its_mode_and_no_value(fake_bigquery):
    metadata = BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")
    date_field = next(f for f in metadata["fields"] if f["name"] == "usage_start_time")
    assert date_field["type"] == "TIMESTAMP"
    assert date_field["mode"] == "NULLABLE"
    assert set(metadata["fields"][0]) == {
        "name",
        "field_id",
        "type",
        "nullable",
        "mode",
        "description",
    }


def test_one_date_candidate_is_a_watermark_and_two_are_a_question(fake_bigquery, monkeypatch):
    """A silent pick between two date columns is a decision nobody made."""
    assert (
        BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")["watermark"]
        == "usage_start_time"
    )

    from google.cloud import bigquery as real

    two_dates = _FakeClient(
        schema=[_Field("usage_start_time", "TIMESTAMP"), _Field("export_date", "DATE")]
    )
    monkeypatch.setattr(real, "Client", lambda *a, **k: two_dates)
    assert BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")["watermark"] is None


def test_freshness_is_never_invented(fake_bigquery):
    """No read has happened, so there is no freshness -- not `0`, not `now`."""
    assert BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")["freshness"] is None


def test_the_estimate_is_a_dry_run_and_never_executes(fake_bigquery):
    """ESTIMATING IS NOT SCANNING, and this is the assertion that bites.

    Remove `dry_run=True` and BigQuery runs the statement: the operator pays to
    learn what it costs. `use_query_cache=False` matters as much -- a cached plan
    reports bytes nobody would really scan.
    """
    estimated = BigQuerySetupReader().estimate_scan_bytes("toorow.billing.export_v1")
    assert estimated == 8_192
    assert len(fake_bigquery.job_configs) == 1
    config = fake_bigquery.job_configs[0]
    assert config.dry_run is True
    assert config.use_query_cache is False
    # The statement priced is the one a read would run, not a cheaper stand-in.
    assert fake_bigquery.statements[0].startswith("SELECT * FROM `toorow.billing.export_v1`")


def test_a_failed_estimate_is_none_and_never_zero(monkeypatch):
    """`0` would read as "this read is free", which nobody measured."""
    from google.cloud import bigquery as real

    monkeypatch.setattr(real, "Client", lambda *a, **k: _FakeClient(query_raises=True))
    assert BigQuerySetupReader().estimate_scan_bytes("toorow.billing.export_v1") is None


def test_a_dataset_is_not_a_readable_object(monkeypatch):
    """Two parts is a dataset. It is refused BEFORE any client is built.

    A dataset offered where an object is expected gives a wizard that never finds
    a field, and the refusal has to happen here as well as in the adapter -- one
    guard is a guard, two guards are the class.
    """
    calls: list[str] = []

    from google.cloud import bigquery as real

    monkeypatch.setattr(real, "Client", lambda *a, **k: calls.append("built"))
    reader = BigQuerySetupReader()
    with pytest.raises(ValueError):
        reader.get_table_metadata("toorow.billing")
    with pytest.raises(ValueError):
        reader.estimate_scan_bytes("toorow.billing")
    assert calls == []


def test_a_schema_deeper_than_the_catalog_says_so(monkeypatch):
    """A folder whose contents were never walked is stated, not dropped."""
    from google.cloud import bigquery as real

    deep = _FakeClient(
        schema=[
            _Field("cost", "FLOAT"),
            _Field("labels", "RECORD", fields=[]),
        ]
    )
    monkeypatch.setattr(real, "Client", lambda *a, **k: deep)
    metadata = BigQuerySetupReader().get_table_metadata("toorow.billing.export_v1")
    assert metadata["schema_depth_truncated"] is True
