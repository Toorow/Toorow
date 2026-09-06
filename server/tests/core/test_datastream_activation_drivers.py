"""Story 47.5 Task 1: the mounted activation drivers do real work or refuse."""

from __future__ import annotations

import pytest
from core.datastream_activation import build_safe_preview
from inbound.adapters import datastream_activation_drivers as drivers

_CSV = b"date,country,revenue\n2026-07-01,FR,120.5\n2026-07-02,FR,90\n"

_PINS = {
    "draft_revision_id": "dsdr_1",
    "proposal_id": "dspr_1",
    "observation_id": "dso_1",
    "connector_contract_version_id": "ccv_1",
    "project_configuration_version_id": "pcv_1",
    "capability_version_ids": ["cap_1"],
    "mapping_hash": "a" * 64,
    "processing_hash": "b" * 64,
}


def _context(**metadata):
    return {
        "kind": "setup_preview",
        "project_id": "proj_EXAMPLE",
        "draft_id": "dsd_1",
        "mode": "managed_feed",
        "channel": "file_upload",
        "pins": dict(_PINS),
        "proposal": {"confirmed_intent_bundle": {}},
        "observation": {
            "observed_at": "2026-07-30T00:00:00+00:00",
            "safe_metadata": {"staged_asset_ref": "dsa_1", **metadata},
        },
    }


@pytest.fixture
def staged(monkeypatch):
    def _stage(payload: bytes, filename: str = "plan.csv"):
        monkeypatch.setattr(
            drivers, "_staged_asset", lambda *_args, **_kwargs: (filename, payload)
        )

    return _stage


def test_file_preview_reads_the_real_staged_asset(staged):
    staged(_CSV)

    evidence = drivers.managed_feed_preview(_context())

    assert evidence["adapter_verified"] is True
    assert evidence["placeholder"] is False
    assert [field["field_id"] for field in evidence["schema"]] == ["date", "country", "revenue"]
    assert len(evidence["rows"]) == 2
    assert evidence["rows"][0]["country"] == "FR"
    assert evidence["detected_format"] == "csv"
    assert evidence["write_mode"] == "replace"
    assert evidence["coverage"] == {"schema": "available", "values": "available"}
    assert evidence["exceptions"] == []


def test_file_preview_evidence_survives_the_safe_preview_contract(staged):
    staged(_CSV)

    preview = build_safe_preview(
        mode="managed_feed",
        pins=dict(_PINS),
        adapter_evidence=drivers.managed_feed_preview(_context()),
        classifications={"date": "none", "country": "none", "revenue": "confidential"},
    )

    assert preview["status"] == "ready_for_review"
    assert len(preview["sample"]) == 2
    # Masking is the caller's: an unclassified or sensitive column never leaves clear.
    assert {row["revenue"] for row in preview["sample"]} == {"[MASKED]"}
    assert {row["country"] for row in preview["sample"]} == {"FR"}
    assert preview["evidence_hash"] == build_safe_preview(
        mode="managed_feed",
        pins=dict(_PINS),
        adapter_evidence=drivers.managed_feed_preview(_context()),
        classifications={"date": "none", "country": "none", "revenue": "confidential"},
    )["evidence_hash"]


def test_append_capable_feed_reports_append(staged):
    staged(_CSV)

    evidence = drivers.managed_feed_preview(_context(append_supported=True))

    assert evidence["write_mode"] == "append"


def test_unparseable_file_blocks_instead_of_passing(staged):
    staged(b"", "plan.csv")

    evidence = drivers.managed_feed_preview(_context())

    assert evidence["dq"]["blocking"], "an empty staged file cannot reach ready_for_review"
    assert evidence["schema"] == []
    preview = build_safe_preview(
        mode="managed_feed",
        pins=dict(_PINS),
        adapter_evidence=evidence,
        classifications={},
    )
    assert preview["status"] == "blocked"


def test_channel_without_an_observation_refuses(staged):
    context = _context()
    context["observation"]["safe_metadata"].pop("staged_asset_ref")

    with pytest.raises(Exception, match="no driver"):
        drivers.managed_feed_preview(context)


def test_all_supported_runtime_modes_are_mounted():
    from inbound import datastream_activation_worker as worker

    worker._RUNTIME_DRIVERS.clear()
    drivers.install_runtime_activation_drivers()

    assert set(worker._RUNTIME_DRIVERS) == {
        (kind, mode)
        for kind in ("setup_preview", "candidate_materialization")
        for mode in ("connector_pull", "external_bq", "managed_feed")
    }


def test_connector_pull_candidate_materializes_locally_isolated_by_execution(monkeypatch):
    """The candidate now runs the REAL pull inside the execution's isolation.

    Story 47.5 C-8 used to route this at `TOOROW_DATASTREAM_ACTIVATION_URL`, a
    service no repository implemented and no deployment configured. That was the
    warehouse problem relocated, not solved: a candidate could not be isolated
    because every staging model supersedes on `pull_id`, and putting the same
    impossibility behind an unbuilt service turned a findable contract into a
    missing env var. The isolation landed in the warehouse instead, so there is
    no remote boundary left to configure.
    """
    seen = {}

    def fake_pull(**kwargs):
        from core.raw_landing import active_candidate_execution

        # The whole point: the pull is real, and it is ALREADY isolated when it
        # runs -- not tagged afterwards.
        seen["isolated_as"] = active_candidate_execution()
        seen["pull_id"] = kwargs.get("pull_id")
        return {"row_count": 7}

    monkeypatch.setattr("core.main.get_module_pull_fn", lambda *a, **k: fake_pull)

    result = drivers.connector_pull_candidate(
        {"execution_id": "dse_1", "module": "example", "project_id": "proj_EXAMPLE"}
    )

    assert seen["isolated_as"] == "dse_1"
    assert seen["pull_id"] == "pull_dse_1", "AD-7: a candidate's rows carry a real pull_id"
    assert result["adapter_verified"] is True
    assert result["isolation"] == {"kind": "relation_per_execution", "published": False}
    assert result["row_count"] == 7

    from core.raw_landing import active_candidate_execution

    assert active_candidate_execution() is None, "the scope must not outlive the pull"


def test_connector_pull_candidate_carries_the_columns_observed_at_landing(monkeypatch):
    monkeypatch.setattr(
        "core.main.get_module_pull_fn", lambda *a, **k: lambda **kw: {"row_count": 2}
    )
    monkeypatch.setattr(
        "core.raw_landing.landed_candidate_relations",
        lambda execution_id: [f"raw_example__cand_{execution_id}"],
    )
    monkeypatch.setattr(
        "core.raw_landing.landed_candidate_columns",
        lambda execution_id, relation: [("date", "DATE"), ("pull_id", "STRING")],
    )

    result = drivers.connector_pull_candidate(
        {"execution_id": "dse_typed", "module": "example", "project_id": "proj_EXAMPLE"}
    )

    assert result["artifact_ref"] == "raw_example__cand_dse_typed"
    assert result["candidate_columns"] == [
        {"name": "date", "type": "DATE"},
        {"name": "pull_id", "type": "STRING"},
    ]


def test_worker_keeps_landing_evidence_until_completion_then_releases_it(monkeypatch):
    """The bounded registry outlives the pull reader, not the terminal job."""
    from core import raw_landing
    from inbound import datastream_activation_worker as worker

    execution_id = "dse_registry_cycle"
    relation = "raw_cycle_candidate"
    context = {
        "mode": "connector_pull",
        "channel": None,
        "org_id": "org_1",
        "project_id": "proj_1",
        "datastream_id": "ds_1",
        "execution_id": execution_id,
        "plan_version_id": "plan_1",
        "mapping_version_id": "map_1",
    }

    def adapter(_context):
        raw_landing.record_candidate_landing(
            execution_id, relation, [("date", "DATE")]
        )
        return {"stage_evidence": [], "phase_evidence": []}

    def complete(_conn, **_kwargs):
        assert raw_landing.landed_candidate_relations(execution_id) == [relation]
        assert raw_landing.landed_candidate_columns(execution_id, relation) == [
            ("date", "DATE")
        ]

    monkeypatch.setattr(worker, "_candidate_context", lambda *_args: context)
    monkeypatch.setattr(worker, "_adapter", lambda *_args: adapter)
    monkeypatch.setattr(worker, "complete_candidate_from_adapter", complete)

    worker._execute_candidate(None, {"requested_by": "pytest"}, {})

    assert raw_landing.landed_candidate_relations(execution_id) == []
    assert raw_landing.landed_candidate_columns(execution_id, relation) == []
    # Reusing the execution key after its terminal cycle cannot collide with
    # the prior contract, which proves both registries were released.
    raw_landing.record_candidate_landing(
        execution_id, relation, [("date", "STRING")]
    )
    assert raw_landing.landed_candidate_columns(execution_id, relation) == [
        ("date", "STRING")
    ]
    raw_landing.clear_candidate_landing(execution_id)


def test_worker_releases_landing_evidence_when_completion_fails(monkeypatch):
    from core import raw_landing
    from inbound import datastream_activation_worker as worker

    execution_id = "dse_registry_failure"
    relation = "raw_failure_candidate"
    context = {
        "mode": "connector_pull",
        "channel": None,
        "org_id": "org_1",
        "project_id": "proj_1",
        "datastream_id": "ds_1",
        "execution_id": execution_id,
        "plan_version_id": "plan_1",
        "mapping_version_id": "map_1",
    }

    def adapter(_context):
        raw_landing.record_candidate_landing(
            execution_id, relation, [("date", "DATE")]
        )
        return {"stage_evidence": [], "phase_evidence": []}

    monkeypatch.setattr(worker, "_candidate_context", lambda *_args: context)
    monkeypatch.setattr(worker, "_adapter", lambda *_args: adapter)
    monkeypatch.setattr(
        worker,
        "complete_candidate_from_adapter",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("terminal")),
    )

    with pytest.raises(RuntimeError, match="terminal"):
        worker._execute_candidate(None, {"requested_by": "pytest"}, {})

    assert raw_landing.landed_candidate_relations(execution_id) == []
    assert raw_landing.landed_candidate_columns(execution_id, relation) == []


def test_a_candidate_without_an_execution_is_refused():
    """The isolation IS the execution; there is nothing to isolate into without one."""
    with pytest.raises(Exception, match="requires an execution_id"):
        drivers.connector_pull_candidate({"module": "example"})


def test_the_candidate_result_satisfies_the_completion_contract(monkeypatch):
    """The seam the two sides never met on.

    `datastream_activation_worker._execute_candidate` hands the driver's result
    straight to `complete_candidate_from_adapter`, which refuses anything whose
    `artifact_ref` does not carry the execution and whose `content_hash`,
    `validated_content_hash` and `artifact_hash` are not sha256. Until this test
    existed, the driver was verified alone and the completion was verified
    against a hand-built dict, so a driver that produced none of that provenance
    still read as green on both sides while the worker refused it in production.
    """
    monkeypatch.setattr(
        "core.main.get_module_pull_fn", lambda *a, **k: lambda **kw: {"row_count": 3}
    )

    result = drivers.connector_pull_candidate(
        {"execution_id": "dse_seam", "module": "example", "project_id": "proj_EXAMPLE"}
    )

    artifact_ref = str(result.get("artifact_ref") or "")
    assert "dse_seam" in artifact_ref, (
        "complete_candidate_from_adapter refuses a candidate whose artifact_ref "
        "does not carry its execution"
    )
    for key in ("content_hash", "validated_content_hash", "artifact_hash"):
        value = str(result.get(key) or "")
        assert len(value) == 64, f"{key} must be a sha256 the completion contract accepts"
    assert isinstance(result.get("row_count"), int)

    # And the contract itself, not a restatement of it. The call reaches the
    # database and fails there because this test has no execution row -- what
    # matters is that it gets PAST the provenance guard, which it could not
    # before. Asserting the shape alone would drift the day the guard changes.
    from unittest.mock import MagicMock  # noqa: PLC0415

    from core.datastream_activation import (  # noqa: PLC0415
        ActivationValidationError,
        complete_candidate_from_adapter,
    )

    try:
        complete_candidate_from_adapter(
            MagicMock(),
            project_id="proj_EXAMPLE",
            datastream_id="ds_1",
            execution_id="dse_seam",
            actor="pytest",
            adapter_result=result,
        )
    except ActivationValidationError as exc:  # pragma: no cover - the assertion is the point
        assert "provenance" not in str(exc), f"the provenance guard still refuses it: {exc}"
    except Exception:
        pass  # anything past the guard (database, state) is out of this test's scope


def test_external_bq_candidate_verifies_in_place_and_writes_nothing(monkeypatch):
    """The ratified contract calls this a virtual pull, not a materialization.

    `datastream-workbench-and-wizard.md` says the external object "remains
    external and read-only", that toorow must "never imply [it] owns or writes
    the source", and that "a verification is a virtual pull". This driver had
    been routed to the unbuilt remote service on the opposite assumption.
    """
    monkeypatch.setattr(
        "modules.bigquery.connector.describe_table",
        lambda table_ref: {
            "table_ref": table_ref,
            "location": "EU",
            "num_rows": 4200,
            "num_bytes": 99,
            "schema": [{"name": "day", "type": "DATE"}, {"name": "clicks", "type": "INTEGER"}],
        },
    )

    result = drivers.external_bq_candidate(
        {"execution_id": "dse_bq", "table_ref": "proj.dataset.table"}
    )

    assert result["adapter_verified"] is True
    assert result["isolation"] == {"kind": "external_read_only", "published": False}
    assert result["row_count"] == 4200, "the object's own count, not rows toorow wrote"
    assert "dse_bq" in result["artifact_ref"]
    for key in ("content_hash", "validated_content_hash", "artifact_hash"):
        assert len(result[key]) == 64
    assert [c["name"] for c in result["schema"]] == ["day", "clicks"]


def test_external_bq_candidate_refuses_without_the_exact_object():
    with pytest.raises(Exception, match="exact table or view"):
        drivers.external_bq_candidate({"execution_id": "dse_bq"})


def test_external_bq_preview_samples_without_landing(monkeypatch):
    """A preview reads; it must not land. The connector's own dry run is the proof."""
    seen = {}

    def fake_pull(**kwargs):
        seen.update(kwargs)
        return {
            "dry_run": True,
            "row_count": 2,
            "rows": [{"day": "2026-07-01", "clicks": 5}, {"day": "2026-07-02", "clicks": 6}],
            "schema": ["clicks", "day"],
            "bytes_estimated": 4096,
            "bytes_processed": 128,
            "table_ref": kwargs.get("table_ref"),
        }

    monkeypatch.setattr("modules.bigquery.connector.pull", fake_pull)

    result = drivers.external_bq_preview(
        {
            "project_id": "proj_EXAMPLE",
            "table_ref": "proj.dataset.table",
            "date_column": "day",
            "interval": {"from": "2026-07-01", "to_exclusive": "2026-07-03"},
        }
    )

    assert seen["dry_run"] is True, "a preview that lands is not a preview"
    assert result["write_mode"] == "external_read_only", (
        "toorow never replaces or appends to an object it does not own"
    )
    assert len(result["rows"]) == 2
    assert result["scan_estimate"] == {"bytes_estimated": 4096, "bytes_processed": 128}
    assert result["watermark"]["date_column"] == "day"


def test_external_bq_preview_refuses_without_its_bounded_window():
    with pytest.raises(Exception, match="bounded window"):
        drivers.external_bq_preview({"table_ref": "proj.dataset.table"})


def _fake_connection():
    """A `get_connection` context manager whose commit is observable and harmless."""
    from contextlib import contextmanager  # noqa: PLC0415
    from unittest.mock import MagicMock  # noqa: PLC0415

    @contextmanager
    def get_connection():
        yield MagicMock()

    return get_connection


def test_managed_feed_candidate_imports_inside_the_execution_isolation(monkeypatch):
    """The import runs for real, and it is already isolated when it runs."""
    seen = {}

    def fake_run_import(data, **kwargs):
        from core.raw_landing import active_candidate_execution

        seen["isolated_as"] = active_candidate_execution()
        seen["bytes"] = data
        seen["plan_version_id"] = kwargs.get("plan_version_id")
        return {"row_count": 9, "landed_row_count": 9, "rejected_count": 1}

    monkeypatch.setattr("core.csv_excel_import.run_import", fake_run_import)
    monkeypatch.setattr(
        drivers, "_staged_asset",
        lambda project_id, draft_id, ref: ("feed.csv", b"day,clicks\n2026-07-01,5\n"),
    )
    monkeypatch.setattr("core.db.get_connection", _fake_connection())

    result = drivers.managed_feed_candidate(
        {
            "channel": "file_upload",
            "execution_id": "dse_feed",
            "project_id": "proj_EXAMPLE",
            "datastream_id": "ds_1",
            "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_1",
            "plan_intent": {"source": {"managed_feed": {"source_ref": "asset_1"}}},
        }
    )

    assert seen["isolated_as"] == "dse_feed", "the import must be isolated BEFORE it runs"
    assert seen["plan_version_id"] == "dsp_1"
    assert result["row_count"] == 9
    assert result["isolation"] == {"kind": "relation_per_execution", "published": False}
    assert "dse_feed" in result["artifact_ref"]
    for key in ("content_hash", "validated_content_hash", "artifact_hash"):
        assert len(result[key]) == 64

    from core.raw_landing import active_candidate_execution

    assert active_candidate_execution() is None, "the scope must not outlive the import"


def test_managed_feed_candidate_reports_what_landed_not_what_parsed(monkeypatch):
    """A partial write must not read as a full candidate."""
    monkeypatch.setattr(
        "core.csv_excel_import.run_import",
        lambda data, **kw: {"row_count": 100, "landed_row_count": 0, "rejected_count": 0},
    )
    monkeypatch.setattr(
        drivers, "_staged_asset", lambda project_id, draft_id, ref: ("feed.csv", b"")
    )
    monkeypatch.setattr("core.db.get_connection", _fake_connection())

    result = drivers.managed_feed_candidate(
        {
            "channel": "file_upload",
            "execution_id": "dse_empty",
            "project_id": "proj_EXAMPLE",
            "datastream_id": "ds_1",
            "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_1",
            "plan_intent": {"source": {"managed_feed": {"source_ref": "asset_1"}}},
        }
    )

    assert result["row_count"] == 0, "parse accepted 100, nothing landed: the candidate is empty"
    assert result["coverage"]["values"] == "empty_file"


def _managed_feed_context(execution_id: str) -> dict:
    return {
        "channel": "file_upload",
        "execution_id": execution_id,
        "project_id": "proj_EXAMPLE",
        "datastream_id": "ds_01KYMBJX3B8YSJZA9TGTXZN5GF",
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "plan_intent": {"source": {"managed_feed": {"source_ref": "asset_1"}}},
    }


def test_managed_feed_candidate_publishes_the_relation_the_reader_accepts(monkeypatch, tmp_path):
    """The file family gets the SAME repair the connector family already had.

    This is the seam, not a shape assertion: the driver's `artifact_ref` is
    copied verbatim into `datastream_output_versions.relation_ref`
    (`datastream_activation.py:968`) and that column is what
    `query_execution` resolves for link 9. The driver used to publish
    `execution/<id>/candidate/relation` unconditionally -- a string
    `_safe_identifier` can never accept -- so EVERY published file Datastream
    was unreadable while both ends passed their own tests. Here the import
    lands for real through the shared seam and the published reference is
    checked against the reader's own guard.
    """
    from core.query_execution import _IDENTIFIER
    from core.raw_landing import candidate_table, land_raw_rows

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "landing.duckdb"))

    execution_id = "dse_01KYMBJX3B8YSJZA9TGTXZN5GF"
    table = "managed_feed_ds_01KYMBJX3B8YSJZA9TGTXZN5GF"

    def fake_run_import(data, **kwargs):
        landed = land_raw_rows(
            table,
            [{"date": "2026-07-01", "clicks": 5}],
            columns=[("date", "STRING"), ("clicks", "INTEGER")],
            project_id="proj_EXAMPLE",
        )
        return {
            "row_count": 1,
            "landed_row_count": 1,
            "rejected_count": 0,
            "landing": {"table": landed["table"]},
        }

    monkeypatch.setattr("core.csv_excel_import.run_import", fake_run_import)
    monkeypatch.setattr(
        drivers, "_staged_asset",
        lambda project_id, draft_id, ref: ("feed.csv", b"date,clicks\n2026-07-01,5\n"),
    )
    monkeypatch.setattr("core.db.get_connection", _fake_connection())

    result = drivers.managed_feed_candidate(_managed_feed_context(execution_id))

    assert result["artifact_ref"] == candidate_table(table, execution_id)
    assert _IDENTIFIER.match(result["artifact_ref"]), (
        "query_execution._safe_identifier would refuse this reference, so the "
        "published Datastream would be silently unreadable"
    )
    assert "/" not in result["artifact_ref"]
    assert execution_id in result["artifact_ref"], (
        "the activation guard refuses a reference that does not carry its execution"
    )


def test_managed_feed_candidate_republishes_the_relation_a_replay_resumed_from(monkeypatch):
    """A deterministic replay writes nothing, so the ledger's relation IS the observation."""
    execution_id = "dse_01REPLAYED0000000000000000"
    replayed = f"managed_feed_ds_X__cand_{execution_id}"

    monkeypatch.setattr(
        "core.csv_excel_import.run_import",
        lambda data, **kw: {
            "row_count": 4,
            "landed_row_count": 4,
            "rejected_count": 0,
            "landing": {"table": replayed},
        },
    )
    monkeypatch.setattr(
        drivers, "_staged_asset", lambda project_id, draft_id, ref: ("feed.csv", b"x")
    )
    monkeypatch.setattr("core.db.get_connection", _fake_connection())

    result = drivers.managed_feed_candidate(_managed_feed_context(execution_id))

    assert result["artifact_ref"] == replayed


def _reads_as(relation_ref: str) -> dict:
    """What `query_execution` ANSWERS for a published Output carrying *relation_ref*.

    The two ends of this defect were each tested alone and never met: the driver
    was asserted on the SHAPE of its reference, the reader on references the
    driver never produced. This walks the driver's own string through the reader
    that consumes it -- `datastream_output_versions.relation_ref` is copied
    verbatim from `artifact_ref` (`datastream_activation.py:1069`).
    """
    from core.query_execution import resolve_physical_plan

    spec = {
        "measures": [{"id": "sc_clicks"}],
        "dimensions": [{"id": "sc_date"}],
        "filters": [],
        "sort": [],
        "row_limit": 100,
        "time": {},
    }
    script = [
        ("semantic_view_version_bindings",
         [("sc_clicks", "ds_1", "dmap_1", "active", "clicks"),
          ("sc_date", "ds_1", "dmap_1", "active", "date")]),
        ("datastream_output_versions", (relation_ref, "dse_1", "dplog_1", None, "dsov_1", "ds_1")),
        ("datastream_mapping_versions",
         ({"fields": [{"field_id": "clicks", "binding": {"canonical_target": "clicks"}},
                      {"field_id": "date", "binding": {"canonical_target": "date"}}]},)),
    ]

    class _Cur:
        def __init__(self):
            self._last = ""

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params=None):
            self._last = sql

        def fetchone(self):
            for pattern, value in script:
                if pattern in self._last:
                    return value
            return None

        def fetchall(self):
            for pattern, value in script:
                if pattern in self._last:
                    return value
            return []

    class _Conn:
        def cursor(self):
            return _Cur()

    return resolve_physical_plan(
        _Conn(), project_id="proj_EXAMPLE", semantic_view_version_id="sv_ver", spec=spec
    )


def _no_relation_ref_from_a_managed_feed_that_landed_nothing(monkeypatch) -> str:
    monkeypatch.setattr(
        "core.csv_excel_import.run_import",
        lambda data, **kw: {"row_count": 0, "landed_row_count": 0, "rejected_count": 0},
    )
    monkeypatch.setattr(
        drivers, "_staged_asset", lambda project_id, draft_id, ref: ("feed.csv", b"")
    )
    monkeypatch.setattr("core.db.get_connection", _fake_connection())
    result = drivers.managed_feed_candidate(
        _managed_feed_context("dse_01LANDEDNOTHING000000000000")
    )
    return result["artifact_ref"]


def _no_relation_ref_from_a_pull_that_landed_nothing(monkeypatch) -> str:
    monkeypatch.setattr(
        "core.main.get_module_pull_fn", lambda *a, **k: lambda **kw: {"row_count": 0}
    )
    result = drivers.connector_pull_candidate(
        {
            "execution_id": "dse_01PULLEDNOTHING000000000000",
            "module": "example",
            "project_id": "proj_EXAMPLE",
        }
    )
    return result["artifact_ref"]


def test_both_drivers_that_landed_nothing_publish_the_one_shared_reference(monkeypatch):
    """AI-308: the class has TWO sites, and they mint from ONE place.

    Connector pull and managed feed each composed this string from their own copy
    of the same comment. A tracker line naming one of them could be closed while
    the other kept publishing. They now call `raw_landing.unlanded_candidate_ref`,
    so a third driver inherits the convention instead of inventing a third one.
    """
    from core.raw_landing import unlanded_candidate_ref

    feed_ref = _no_relation_ref_from_a_managed_feed_that_landed_nothing(monkeypatch)
    pull_ref = _no_relation_ref_from_a_pull_that_landed_nothing(monkeypatch)

    assert feed_ref == unlanded_candidate_ref("dse_01LANDEDNOTHING000000000000")
    assert pull_ref == unlanded_candidate_ref("dse_01PULLEDNOTHING000000000000")
    # Provenance still names the act: the activation guard asks exactly this.
    assert "dse_01LANDEDNOTHING000000000000" in feed_ref
    assert "dse_01PULLEDNOTHING000000000000" in pull_ref


@pytest.mark.parametrize(
    "produce_ref",
    [
        _no_relation_ref_from_a_managed_feed_that_landed_nothing,
        _no_relation_ref_from_a_pull_that_landed_nothing,
    ],
    ids=["managed_feed", "connector_pull"],
)
def test_a_window_that_landed_nothing_is_ANSWERED_not_raised(produce_ref, monkeypatch):
    """What the PERSON gets, not what the reference looks like.

    The test this replaces asserted only that the reference is not a SQL
    identifier -- which was true while the reader turned that same fact into an
    uncaught `ExecutionUnavailable`, an accepted attempt with no Result, and a
    500. It measured the defect and called it the target.
    """
    plan = _reads_as(produce_ref(monkeypatch))

    # An OUTCOME, with the link a repairer needs -- never a plan that will explode.
    assert "relation" not in plan
    assert plan["missing_link"] == "relation_ref"

    sentence = plan["unavailable_reason"]
    # It names the gesture.
    assert "Re-run it" in sentence
    # And nothing else: no reference, no execution, no column, no word of the base.
    assert "/" not in sentence
    assert "dse_" not in sentence and "candidate" not in sentence
    for jargon in ("relation", "artifact_ref", "identifier", "SQL", "DQ", "null", "exception"):
        assert jargon not in sentence, f"the sentence says `{jargon}` to a person"


def test_managed_feed_candidate_refuses_an_input_its_plan_never_pinned():
    """Materializing 'the latest arrival' would import bytes nobody reviewed."""
    with pytest.raises(Exception, match="its plan version pinned"):
        drivers.managed_feed_candidate(
            {
                "channel": "file_upload",
                "execution_id": "dse_1",
                "project_id": "proj_EXAMPLE",
                "plan_intent": {"source": {"managed_feed": {}}},
            }
        )


def test_remote_driver_fails_closed_and_names_the_real_blocker(monkeypatch):
    """The refusal must name the warehouse gap, not a missing setting.

    Story 47.5 C-8: the message used to read "The Datastream activation service is
    not configured", which sends the next reader hunting an env var. The blocker is
    upstream of configuration -- no staging model has a notion of an execution.

    This used to exercise `external_bq_candidate`. That driver is local now: the
    ratified contract calls an external candidate a virtual pull, so it never
    needed the remote boundary. The assertion moved to `managed_feed_candidate`,
    which is still routed there, so the test keeps measuring a driver that
    genuinely is remote rather than passing on a stale example.
    """
    monkeypatch.delenv("TOOROW_DATASTREAM_ACTIVATION_URL", raising=False)
    with pytest.raises(Exception, match="isolated by execution") as failure:
        drivers.managed_feed_candidate({"execution_id": "dse_1"})
    message = str(failure.value)
    assert "pull_id" in message
    assert "not a missing setting" in message


def test_a_persisted_preview_is_looked_up_under_the_key_it_was_stored_with():
    """Writer and reader hashed the same string two different ways.

    `persist_preview` stores the row under `_hash(idempotency_key)`, and `_hash`
    canonicalises first -- for a plain string that means JSON quotes, so it
    hashes `"dsdr_01..."` INCLUDING them. `_read_preview_job` recomputed
    `hashlib.sha256(str(correlation_id))`, without them. The two can never match.

    So a preview job that RAN, succeeded and wrote its row could never be read
    back: the reader found nothing and raised "Completed preview job has no
    durable evidence" -- a sentence that accuses the worker of losing the
    evidence it had in fact just written.

    MEASURED live 2026-08-07, job dsaj_01KZENNSWWBFJ58X938C3X93B5, state `done`,
    correlation `dsdr_01KZENNPGA5H5R10NRM0PGENB5`:
        row in app.datastream_setup_previews -> idempotency_key_hash ea49313243...
        what the reader looked for            ->                     14bf758d16...

    One definition, used by both sides, so they cannot drift again.
    """
    from core.datastream_activation import preview_idempotency_hash
    from core.datastream_preconfiguration_api import _preview_idempotency_hash

    correlation_id = "dsdr_01EXAMPLE0000000000000000"
    assert _preview_idempotency_hash(correlation_id) == preview_idempotency_hash(correlation_id)
