"""Story 38.13 offline contract tests for governed managed-file dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest
from core.csv_excel_import import (
    ColumnSpec,
    CsvExcelImportError,
    ParseResult,
    _apply_governed_mapping,
)
from core.managed_file_dispatch import fingerprint_bundle

#: Repository root, anchored on this file rather than on the working directory.
#: Resolving these artefacts against the cwd made the same tree return two
#: different verdicts -- green from the repository root, FileNotFoundError from
#: `server/` -- so the red count depended on where pytest was launched (AI-136).
ROOT = Path(__file__).resolve().parents[3]


def _parsed(*rows: dict[str, str]) -> ParseResult:
    return ParseResult(
        rows=list(rows),
        rejected=[],
        columns=[
            ColumnSpec("day", 0, "text"),
            ColumnSpec("spend", 1, "text"),
            ColumnSpec("clicks", 2, "text"),
            ColumnSpec("active", 3, "text"),
        ],
        encoding="utf-8",
        delimiter=",",
        sheet_name=None,
        detected_row_count=len(rows),
        content_hash="a" * 64,
        source_format="csv",
    )


def _mapping() -> dict:
    types = {"day": "date", "spend": "decimal", "clicks": "integer", "active": "boolean"}
    return {
        "fields": [
            {
                "field_id": source,
                "physical_type": physical,
                "binding": {
                    "status": "confirmed",
                    "canonical_target": f"canonical_{source}",
                },
            }
            for source, physical in types.items()
        ]
    }


def _projection(*, grain: str = "day") -> dict:
    return {
        "executable": True,
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "projection_contract_version": "1",
        "full_grain_relation": {
            "grain_columns": [{"field_id": grain, "type_class": "date"}],
            "grain_key": "grain_key",
            "source_fields": ["active", "clicks", "spend"],
            "provenance": {},
            "scope_columns": ["connector", "project_id"],
        },
        "additive_measures": [
            {"field_id": "clicks"},
            {"field_id": "spend"},
        ],
        "governed_dimension_projection": None,
    }


def test_mapping_builds_typed_universal_candidate_before_landing():
    candidate = _apply_governed_mapping(
        _parsed(
            {"day": "2026-08-01", "spend": "12.50", "clicks": "4", "active": "true"},
            {"day": "bad-date", "spend": "2", "clicks": "1", "active": "false"},
        ),
        mapping_payload=_mapping(),
        projection_plan=_projection(),
        plan_version_id="dsp_1",
        mapping_version_id="dmap_1",
    )

    assert candidate.envelope_version == "universal-datastream-candidate-v1"
    assert [column.detected_type for column in candidate.columns] == [
        "date", "boolean", "integer", "decimal", "text"
    ]
    assert candidate.rows[0]["canonical_day"].isoformat() == "2026-08-01"
    assert candidate.rows[0]["canonical_spend"] == 12.5
    assert candidate.rows[0]["canonical_clicks"] == 4
    assert candidate.rows[0]["canonical_active"] is True
    assert len(candidate.rows[0]["grain_key"]) == 64
    assert candidate.metadata["full_grain_relation_applied"] is True
    assert candidate.rejected[0].rule == "dispatch_type_mismatch"


def test_unconfirmed_mapping_never_reaches_a_candidate():
    mapping = _mapping()
    mapping["fields"][0]["binding"]["status"] = "suggested"
    with pytest.raises(CsvExcelImportError) as raised:
        _apply_governed_mapping(
            _parsed({"day": "2026-08-01", "spend": "1", "clicks": "1", "active": "true"}),
            mapping_payload=mapping,
            projection_plan={"executable": True},
            plan_version_id="dsp_1",
            mapping_version_id="dmap_1",
        )
    assert raised.value.code == "dispatch_mapping_unconfirmed"


def test_bundle_identity_is_canonical_and_version_sensitive():
    left = {"plan_version_id": "dsp_1", "mapping_version_id": "dmap_1"}
    reordered = {"mapping_version_id": "dmap_1", "plan_version_id": "dsp_1"}
    changed = {"plan_version_id": "dsp_1", "mapping_version_id": "dmap_2"}
    assert fingerprint_bundle(left) == fingerprint_bundle(reordered)
    assert fingerprint_bundle(left) != fingerprint_bundle(changed)


def test_migration_enforces_scope_immutability_and_forward_state_machine():
    sql = ROOT.joinpath("infra/nango/migrations/194_managed_file_dispatch.sql").read_text(
        encoding="utf-8"
    )
    assert "FOREIGN KEY (ledger_id, datastream_id, project_id)" in sql
    assert "FOREIGN KEY (execution_id, datastream_id, project_id)" in sql
    assert "bundle_fingerprint IS DISTINCT FROM OLD.bundle_fingerprint" in sql
    assert "invalid managed-file dispatch transition" in sql
    assert "candidate evidence is incomplete" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql

def test_unknown_physical_type_is_never_inferred_from_source():
    mapping = _mapping()
    mapping["fields"][1]["physical_type"] = "money-ish"
    with pytest.raises(CsvExcelImportError) as raised:
        _apply_governed_mapping(
            _parsed(
                {
                    "day": "2026-08-01",
                    "spend": "1.0",
                    "clicks": "1",
                    "active": "true",
                }
            ),
            mapping_payload=mapping,
            projection_plan={"executable": True},
            plan_version_id="dsp_1",
            mapping_version_id="dmap_1",
        )
    assert raised.value.code == "dispatch_type_unsupported"


def test_invalid_boolean_and_non_finite_numbers_are_rejected():
    candidate = _apply_governed_mapping(
        _parsed(
            {
                "day": "2026-08-01",
                "spend": "NaN",
                "clicks": "1",
                "active": "sometimes",
            }
        ),
        mapping_payload=_mapping(),
        projection_plan=_projection(),
        plan_version_id="dsp_1",
        mapping_version_id="dmap_1",
    )
    assert candidate.rows == []
    assert candidate.rejected[0].rule == "dispatch_type_mismatch"


def test_all_excluded_mapping_fails_before_an_empty_candidate_exists():
    mapping = _mapping()
    for field in mapping["fields"]:
        field["binding"] = {"status": "excluded"}
    with pytest.raises(CsvExcelImportError) as raised:
        _apply_governed_mapping(
            _parsed(
                {
                    "day": "2026-08-01",
                    "spend": "1",
                    "clicks": "1",
                    "active": "true",
                }
            ),
            mapping_payload=mapping,
            projection_plan={"executable": True},
            plan_version_id="dsp_1",
            mapping_version_id="dmap_1",
        )
    assert raised.value.code == "dispatch_projection_empty"


def test_projection_grain_must_exist_in_the_governed_mapping():
    with pytest.raises(CsvExcelImportError) as raised:
        _apply_governed_mapping(
            _parsed(
                {
                    "day": "2026-08-01",
                    "spend": "1",
                    "clicks": "1",
                    "active": "true",
                }
            ),
            mapping_payload=_mapping(),
            projection_plan=_projection(grain="missing_dimension"),
            plan_version_id="dsp_1",
            mapping_version_id="dmap_1",
        )
    assert raised.value.code == "dispatch_projection_field_mismatch"


def test_candidate_evidence_fingerprints_typed_rows_not_transport_bytes():
    from core.raw_landing import (
        candidate_rows_fingerprint,
        candidate_schema_fingerprint,
    )

    columns = [("day", "DATE"), ("spend", "FLOAT"), ("execution_id", "STRING")]
    left = [{"day": "2026-08-01", "spend": 1.0, "execution_id": "dse_1"}]
    right = [{"day": "2026-08-01", "spend": 2.0, "execution_id": "dse_1"}]
    assert candidate_rows_fingerprint(left, columns=columns) != (
        candidate_rows_fingerprint(right, columns=columns)
    )
    assert candidate_schema_fingerprint(columns) != candidate_schema_fingerprint(
        [("day", "DATE"), ("spend", "STRING"), ("execution_id", "STRING")]
    )



def test_cross_store_publication_orders_promotion_before_pointer_and_outbox():
    # AI-287: the three calls left `run_import` for two named steps of the same
    # module. This guard used to slice the FILE from `dq_evidence = {` and compare
    # offsets -- so the day the code moved it raised `substring not found`, which
    # is the class `module-boundaries.md` criterion 13 names. It now resolves each
    # step BY FUNCTION, so it follows the code instead of the file layout, and it
    # proves strictly more than before: that the pointer swap cannot even reach
    # the merge, not merely that it is written after it.
    import ast

    source = ROOT.joinpath("server/core/import_runner.py").read_text(encoding="utf-8")
    bodies = {
        node.name: ast.get_source_segment(source, node)
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef)
    }

    promotion_step = bodies["_promote_candidate_or_reconcile"]
    assert promotion_step.index("begin_managed_file_promotion(") < promotion_step.index(
        "promotion = promote_candidate("
    )
    assert "commit_publication(" not in promotion_step

    commit_step = bodies["_commit_publication_or_reconcile"]
    assert "publication = commit_publication(" in commit_step
    assert "promote_candidate(" not in commit_step

    orchestration = bodies["_publish_governed_candidate"]
    assert orchestration.index("_promote_candidate_or_reconcile(") < orchestration.index(
        "_commit_publication_or_reconcile("
    )

    publication = ROOT.joinpath("server/core/datastream_publication.py").read_text(
        encoding="utf-8"
    )
    begin_body = publication[
        publication.index("def begin_managed_file_promotion(") :
        publication.index("def commit_publication(")
    ]
    # The execution enters `publishing` here -- but AI-223 took the private
    # `SET state = 'publishing'` away from this function: it asks the ONE state
    # machine for the transition, which is what stamps `state_changed_at` and
    # refuses an execution that is no longer `ready`. What this test is about is
    # unchanged: the DURABLE CLAIM is made here, and the pointer swap is not.
    assert "advance_state(" in begin_body
    assert "STATE_READY,\n                    STATE_PUBLISHING," in begin_body
    assert "SET state = 'promoting'" in begin_body
    assert "current_published_execution_id = %s" not in begin_body
    assert "app.datastream_outbox" not in begin_body


def test_dispatch_persists_positive_dq_and_freezes_candidate_evidence():
    migration = ROOT.joinpath(
        "infra/nango/migrations/194_managed_file_dispatch.sql"
    ).read_text(encoding="utf-8")
    assert "dq_evidence JSONB" in migration
    assert "positive DQ evidence is immutable" in migration
    assert "candidate evidence is immutable" in migration
    assert "dq_evidence->>'status' <> 'passed'" in migration


def test_bigquery_promotion_is_one_deterministic_merge_then_independent_proof():
    source = ROOT.joinpath("server/core/raw_landing.py").read_text(encoding="utf-8")
    promotion = source[source.index("def promote_candidate(") :]
    assert "MERGE {tick}{target_ref}{tick}" in promotion
    assert "job_id=merge_job_id" in promotion
    assert "client.get_job(merge_job_id)" in promotion
    assert "observed_fingerprint = _canonical_rows_fingerprint" in promotion
    assert promotion.index("observed_fingerprint =") < promotion.index(
        "client.delete_table(source_ref"
    )


def test_bundle_is_resolved_after_scan_and_written_with_accepted(monkeypatch):
    import core.inbound_raw_imports as raw_imports
    import core.inbound_scan as inbound_scan
    from core.inbound_scan import ScanVerdict

    events = []
    accepted_bundle = {"schema": "managed-file-dispatch-bundle-v1"}

    def mark(conn, **kwargs):
        events.append(("state", kwargs["state"], kwargs.get("dispatch_bundle")))
        return {}

    def scan(data, **kwargs):
        events.append(("scan", len(data)))
        return ScanVerdict(
            accepted=True,
            reason=None,
            detected_type="text/csv",
            declared_type="text/csv",
            size_bytes=len(data),
        )

    def resolve():
        events.append(("resolve",))
        return accepted_bundle

    monkeypatch.setattr(raw_imports, "mark_raw_import_state", mark)
    monkeypatch.setattr(inbound_scan, "scan_bytes", scan)
    verdict = inbound_scan.scan_raw_import(
        object(),
        raw_import_id="inbraw_1",
        datastream_id="ds_1",
        data=b"a,b\n1,2\n",
        dispatch_bundle_resolver=resolve,
    )

    assert verdict.accepted is True
    assert events == [
        ("state", "SCANNING", None),
        ("scan", 8),
        ("resolve",),
        ("state", "ACCEPTED", accepted_bundle),
    ]


def test_candidate_evidence_fingerprint_is_invariant_to_value_representation():
    """AI-321 (2026-08-29, production): same rows, same schema, two content
    fingerprints -- the parser kept Decimal("110.00") / "5", BigQuery returned
    110.0 / 5. A fingerprint is a claim about the DATA, so each value is put in
    the shape its declared kind fixes before hashing."""
    from decimal import Decimal

    from core.raw_landing import candidate_rows_fingerprint

    columns = [("day", "DATE"), ("spend", "FLOAT"), ("clicks", "INTEGER"), ("ok", "BOOLEAN")]
    parsed = [{"day": "2026-08-01", "spend": Decimal("110.00"), "clicks": "5", "ok": "true"}]
    warehouse = [{"day": "2026-08-01", "spend": 110.0, "clicks": 5, "ok": True}]
    assert candidate_rows_fingerprint(parsed, columns=columns) == candidate_rows_fingerprint(
        warehouse, columns=columns
    )
    # Still a fingerprint of the data: a different number is a different hash,
    # and a null is not an empty string.
    other = [{"day": "2026-08-01", "spend": 111.0, "clicks": 5, "ok": True}]
    assert candidate_rows_fingerprint(parsed, columns=columns) != candidate_rows_fingerprint(
        other, columns=columns
    )
    null = [{"day": "2026-08-01", "spend": None, "clicks": 5, "ok": True}]
    empty = [{"day": "2026-08-01", "spend": "", "clicks": 5, "ok": True}]
    assert candidate_rows_fingerprint(null, columns=columns) != candidate_rows_fingerprint(
        empty, columns=columns
    )
