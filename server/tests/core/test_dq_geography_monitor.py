"""Story 37.9: unmapped geography must reach the DQ supervision surfaces.

Without this monitor the ``country_value_unmapped`` evidence lived only in the
report envelope's alert list, so an agent asked whether the data is trustworthy
saw no geographic gap at all.
"""

from __future__ import annotations

from datetime import date

from core import dq_monitors
from core.geographic_reporting import GeographicPosture, Market

LOCAL = "local_markets"


class _FakeConn:
    def cursor(self):  # pragma: no cover - never reached in these tests
        raise AssertionError("posture fetch is stubbed in these tests")


def _local_posture() -> GeographicPosture:
    return GeographicPosture(
        mode=LOCAL,
        markets=(Market(id="hexagone", label="Hexagone", country_codes=("FR",)),),
    )


def _patch_posture(monkeypatch, posture) -> None:
    import core.geographic_reporting as geo

    monkeypatch.setattr(geo, "fetch_project_geographic_posture", lambda *_a, **_k: posture)


def _patch_warehouse(monkeypatch, rows) -> None:
    from core import warehouse

    monkeypatch.setattr(warehouse, "query_breakdown_values", lambda *_a, **_k: rows)


def _capture_firings(monkeypatch) -> list[dict]:
    from core import infra_alerts

    written: list[dict] = []
    monkeypatch.setattr(
        infra_alerts, "write_infra_firing", lambda **kwargs: written.append(kwargs)
    )
    return written


def _silence_suggestions(monkeypatch) -> list:
    from core import geographic_conformance as gc

    calls: list = []

    def _fake(project_id, items, **kwargs):
        calls.append((project_id, items))
        return {"proposed": len(items), "skipped": 0, "unchanged": 0, "unresolved": 0}

    monkeypatch.setattr(gc, "register_unmapped_country_values", _fake)
    return calls


def test_global_project_is_not_evaluated(monkeypatch) -> None:
    """A Global project makes no geographic promise -- nothing to be a gap against."""

    _patch_posture(monkeypatch, GeographicPosture())
    written = _capture_firings(monkeypatch)

    assert dq_monitors._check_geography("prj_1", _FakeConn(), date(2026, 7, 25)) is False
    assert written == []


def test_fully_resolved_geography_fires_nothing(monkeypatch) -> None:
    _patch_posture(monkeypatch, _local_posture())
    _patch_warehouse(
        monkeypatch, [{"connector": "acme", "breakdown_value": "FR", "row_count": 12}]
    )
    written = _capture_firings(monkeypatch)

    assert dq_monitors._check_geography("prj_1", _FakeConn(), date(2026, 7, 25)) is False
    assert written == []


def test_unmapped_value_fires_a_dq_geography_alert(monkeypatch) -> None:
    _patch_posture(monkeypatch, _local_posture())
    _patch_warehouse(
        monkeypatch,
        [
            {"connector": "acme", "breakdown_value": "FR", "row_count": 12},
            {"connector": "acme", "breakdown_value": "Frnce", "row_count": 4},
        ],
    )
    written = _capture_firings(monkeypatch)
    suggested = _silence_suggestions(monkeypatch)

    assert dq_monitors._check_geography("prj_1", _FakeConn(), date(2026, 7, 25)) is True
    assert len(written) == 1
    firing = written[0]
    # The 'dq_' prefix is what Epic 13's monitors and get_data_quality_report filter on.
    assert firing["alert_type"] == "dq_geography"
    assert firing["project_id"] == "prj_1"
    meta = firing["metadata"]
    assert meta["distinct_unmapped_values"] == 1
    assert meta["unmapped_row_count"] == 4
    assert meta["values"][0]["source_value"] == "Frnce"
    # The same pass raises the governed repair suggestion.
    assert suggested and suggested[0][0] == "prj_1"


def test_a_confirmed_client_mapping_closes_the_gap(monkeypatch) -> None:
    """Once repaired, the monitor stops reporting the value -- no fact was rewritten."""

    from core import geographic_conformance as gc

    _patch_posture(monkeypatch, _local_posture())
    _patch_warehouse(
        monkeypatch, [{"connector": "acme", "breakdown_value": "Frnce", "row_count": 4}]
    )
    monkeypatch.setattr(
        gc, "load_project_country_conformance", lambda _p: {("acme", "Frnce"): "FR"}
    )
    written = _capture_firings(monkeypatch)

    assert dq_monitors._check_geography("prj_1", _FakeConn(), date(2026, 7, 25)) is False
    assert written == []


def test_empty_warehouse_is_silent_not_green(monkeypatch) -> None:
    _patch_posture(monkeypatch, _local_posture())
    _patch_warehouse(monkeypatch, [])
    written = _capture_firings(monkeypatch)

    assert dq_monitors._check_geography("prj_1", _FakeConn(), date(2026, 7, 25)) is False
    assert written == []


def test_summary_reports_geography_issues(monkeypatch) -> None:
    """run_dq_monitors carries the geography count into its summary."""

    monkeypatch.setenv("DQ_MONITORS_ENABLED", "false")
    summary = dq_monitors.run_dq_monitors(project_id="prj_1")
    assert "geography_issues" in summary
    assert summary["geography_issues"] == 0
