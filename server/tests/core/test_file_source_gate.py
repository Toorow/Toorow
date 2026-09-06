"""toorow -- tests for the file-source required-field validation gate (Story 22.15).

OFFLINE: evaluate_required_field_gate is pure (reuses the 12.3 profile_fields
confidence + ambiguity scoring). confirm_mapping_version's execute_operation
call is asserted with a patched operations seam.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from core.file_source_gate import (
    GateNotPassed,
    confirm_mapping_version,
    evaluate_required_field_gate,
)


def _template(required):
    return {"contract": {"kind": "catalog", "class": "planned", "required_fields": required}}


_SAMPLE = [
    {"vendor": "Google", "spend": "100", "day": "2026-03-01"},
    {"vendor": "Meta", "spend": "200", "day": "2026-03-02"},
    {"vendor": "TikTok", "spend": "300", "day": "2026-03-03"},
]


# ---------------------------------------------------------------------------
# evaluate_required_field_gate
# ---------------------------------------------------------------------------


def test_gate_passes_when_required_covered_and_confident():
    template = _template(["mdm_cost", "mdm_date"])
    mapping = {"vendor": "mdm_channel", "spend": "mdm_cost", "day": "mdm_date"}
    gate = evaluate_required_field_gate(template, mapping, _SAMPLE)
    assert gate["passed"] is True
    assert gate["missing_required"] == []
    assert gate["flagged"] == []


def test_gate_flags_missing_required_field():
    template = _template(["mdm_cost", "mdm_date", "mdm_impressions"])
    mapping = {"vendor": "mdm_channel", "spend": "mdm_cost", "day": "mdm_date"}
    gate = evaluate_required_field_gate(template, mapping, _SAMPLE)
    assert gate["passed"] is False
    assert gate["missing_required"] == ["mdm_impressions"]  # surfaced, not landed


def test_gate_flags_low_confidence_required_binding():
    # A required field mapped to a column that is EMPTY across the sample -> weak
    # evidence -> low confidence -> blocking -> flagged (offending field surfaced).
    template = _template(["mdm_cost"])
    mapping = {"mystery": "mdm_cost", "day": "mdm_date"}
    # 'mystery' is absent from every sample row -> no evidence -> low confidence.
    sample = [{"day": "2026-03-01"} for _ in range(3)]
    gate = evaluate_required_field_gate(template, mapping, sample)
    assert gate["passed"] is False
    flagged_targets = {f["canonical_target"] for f in gate["flagged"]}
    assert "mdm_cost" in flagged_targets
    offending = next(f for f in gate["flagged"] if f["canonical_target"] == "mdm_cost")
    assert offending["field_id"] == "mystery"
    assert offending["blocking_reason"] is not None


# ---------------------------------------------------------------------------
# confirm_mapping_version (AD-7)
# ---------------------------------------------------------------------------


def test_mapping_confirmation_refuses_a_failing_gate():
    """Fail-closed, sur la fonction qui TOURNE.

    Cette assertion vivait sur `record_gate_confirmation`, que plus aucun chemin
    de production n'appelait (AI-92) : l'invariant etait prouve sur l'ancetre et
    pas sur son successeur. Portee ici avant que l'ancetre ne soit retire -- sinon
    supprimer du code mort aurait emporte une preuve avec lui.
    """
    failing = {"passed": False, "missing_required": ["mdm_cost"], "flagged": []}
    with pytest.raises(GateNotPassed):
        confirm_mapping_version(
            MagicMock(), template_id="fst_1", project_id="p", org_id="o",
            datastream_id="ds_1", actor="u", gate_result=failing,
            mapping_payload={}, evidence={"sample_content_hash": "b" * 64},
            idempotency_key="k", content_hash="a" * 64,
            pinned_plan_version_id="dpv_1",
        )


def test_mapping_confirmation_is_atomic_and_never_advances_the_pointer():
    passing = {"passed": True, "missing_required": [], "flagged": [], "ambiguities": []}
    conn = MagicMock()
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.return_value = ("fst_1",)
    conn.cursor.return_value = cursor
    saved = {
        "id": "dmap_pending",
        "plan_version_id": "dpv_1",
        "executable": True,
        "blocking_count": 0,
    }

    def execute(_conn, spec, *, mutation):
        result = mutation(_conn, "op_1")
        return SimpleNamespace(
            result=result.result,
            operation_id="op_1",
            replayed=False,
        )

    from core.file_source_gate import confirm_mapping_version

    evidence = {
        "template_id": "fst_1",
        "template_content_hash": "a" * 64,
        "sample_content_hash": "b" * 64,
        "sample_filename": "sample.csv",
        "actor": "u",
        "confirmed_at": "2026-08-01T00:00:00+00:00",
        "resolutions": [],
        "accepted_warnings": [],
        "warning_reason": None,
    }
    with patch(
        "core.datastream_field_mapping.save_field_mapping", return_value=saved
    ) as save, patch("core.operations.execute_operation", side_effect=execute):
        out = confirm_mapping_version(
            conn,
            template_id="fst_1",
            project_id="p",
            org_id="o",
            datastream_id="ds_1",
            actor="u",
            gate_result=passing,
            mapping_payload={"fields": []},
            evidence=evidence,
            idempotency_key="idem",
            content_hash="a" * 64,
            pinned_plan_version_id="dpv_1",
        )

    assert out["mapping_version_id"] == "dmap_pending"
    assert out["active_pointer_advanced"] is False
    assert save.call_args.kwargs["advance_pointer"] is False
    assert save.call_args.kwargs["commit"] is False
    assert "file_source_template_confirmations" in cursor.execute.call_args_list[0].args[0]
