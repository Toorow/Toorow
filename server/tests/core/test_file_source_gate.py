"""toorow -- tests for the file-source required-field validation gate (Story 22.15).

OFFLINE: evaluate_required_field_gate is pure (reuses the 12.3 profile_fields
confidence + ambiguity scoring). record_gate_confirmation's execute_operation
call is asserted with a patched operations seam.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from core.file_source_gate import (
    GateNotPassed,
    evaluate_required_field_gate,
    record_gate_confirmation,
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
# record_gate_confirmation (AD-7)
# ---------------------------------------------------------------------------


def test_confirmation_refuses_a_failing_gate():
    failing = {"passed": False, "missing_required": ["mdm_cost"], "flagged": []}
    with pytest.raises(GateNotPassed):
        record_gate_confirmation(
            MagicMock(), template_id="fst_1", project_id="p", org_id="o",
            actor="u", gate_result=failing, idempotency_key="k", content_hash="a" * 64,
        )


def test_confirmation_records_through_execute_operation_when_passed():
    passing = {"passed": True, "missing_required": [], "flagged": [], "ambiguities": []}
    fake_op = MagicMock(operation_id="op_1", replayed=False)
    with patch("core.operations.execute_operation", return_value=fake_op) as mock_exec:
        out = record_gate_confirmation(
            MagicMock(), template_id="fst_1", project_id="p", org_id="o",
            actor="u", gate_result=passing, idempotency_key="k", content_hash="a" * 64,
        )
    assert out == {"confirmed": True, "operation_id": "op_1", "replayed": False}
    assert mock_exec.call_count == 1
    spec = mock_exec.call_args.args[1]
    assert spec.command_type == "file_source.template.gate_confirmed"
    assert spec.confirmation_mode == "human"  # AD-7: human-in-the-loop
