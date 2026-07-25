"""Regression: CURRENCY_GAP rendered THROUGH list_conflicts (Story 39.3, Epic 39).

39.1 shipped the CURRENCY_GAP decision dormant; 39.3 re-enables emission in
datamodel._detect_conflicts AND teaches conflict_resolutions.list_conflicts to render it
WITH resolutions_by_module (Epic 13's currency-binding lever), NOT via the MEASURE_NULL
`else` branch (which would drop resolutions_by_module the UI needs to bind a currency).

Mocks datamodel.list_target_fields / get_target_field so the test is offline (no DB) yet
runs the real list_conflicts branching.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.conflict_resolutions import list_conflicts


def _field_detail(name, conflicts, used_by):
    return {
        "name": name,
        "display_name": name.title(),
        "data_type": "currency",
        "field_kind": "metric",
        "measure": "sum",
        "status": "approved",
        "used_by": used_by,
        "conflicts": conflicts,
    }


def test_currency_gap_rendered_with_resolutions_by_module():
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A"},
        {"module_name": "mod-b", "datastream_name": "B"},
    ]
    gap = {
        "code": "CURRENCY_GAP",
        "message": "gap",
        "affected_streams": ["A", "B"],
        "severity": "refusal",
        "monetary": True,
        "conflicting_currencies": None,
        "metric": "revenue",
        "resolvable_via": "fx_conversion",
    }
    detail = _field_detail("revenue", [gap], used_by)

    conn = MagicMock()
    with patch(
        "core.datamodel.list_target_fields",
        return_value=[{"name": "revenue"}],
    ), patch(
        "core.datamodel.get_target_field",
        return_value=detail,
    ), patch(
        "core.conflict_resolutions._fetch_resolutions_for_field",
        return_value=[],
    ):
        result = list_conflicts(project_id="proj_a", conn=conn)

    assert len(result) == 1
    item = result[0]
    assert item["conflict"]["code"] == "CURRENCY_GAP"
    # Rendered WITH resolutions_by_module (not the MEASURE_NULL empty-{} else branch):
    # both affected modules must be keys, resolvable via currency binding.
    assert set(item["resolutions_by_module"].keys()) == {"mod-a", "mod-b"}
    assert item["resolutions_by_module"]["mod-a"] is None  # no binding yet


def test_measure_null_still_uses_empty_resolutions():
    """Backward compat: MEASURE_NULL keeps resolutions_by_module == {} (else branch)."""
    used_by = [{"module_name": "ga", "datastream_name": "GA"}]
    measure_null = {"code": "MEASURE_NULL", "message": "m", "affected_streams": ["GA"]}
    detail = _field_detail("sessions", [measure_null], used_by)

    conn = MagicMock()
    with patch(
        "core.datamodel.list_target_fields",
        return_value=[{"name": "sessions"}],
    ), patch(
        "core.datamodel.get_target_field",
        return_value=detail,
    ), patch(
        "core.conflict_resolutions._fetch_resolutions_for_field",
        return_value=[],
    ):
        result = list_conflicts(project_id="proj_a", conn=conn)

    assert result[0]["conflict"]["code"] == "MEASURE_NULL"
    assert result[0]["resolutions_by_module"] == {}
