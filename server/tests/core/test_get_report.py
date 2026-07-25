"""Tests for the get_report core tool + report renderer (Story 6.1, AC3, T3.9).

The renderer logic lives in core.reports; the thin tool wrapper is core.main.get_report.
Tests mock the warehouse query layer so no live mart is required.
"""

from __future__ import annotations

import json
import os
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import reports as reports_module  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers — a fake LoadedModule registry
# ---------------------------------------------------------------------------


class _FakeModule:
    def __init__(self, name, manifest, reports):
        self.name = name
        self.manifest = manifest
        self.reports = reports


_GA_REPORT = {
    "id": "overview_daily",
    "display_name": "Vue d'ensemble quotidienne",
    "metrics": ["sessions", "conversions"],
    "dimensions": ["date", "device"],
    "layout": {"chart_type": "line", "order_by": "date"},
    "narrative_prompt": "Analyse GA4 les tendances de trafic.",
    "date_window": {"default_days": 30},
}


def _fake_modules():
    return [
        _FakeModule(
            "google-analytics",
            {"widget_ref": "ui://core/daily-report"},
            [_GA_REPORT],
        )
    ]


def _fixture_rows():
    return [
        {
            "date": "2026-07-01",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 1450.0,
            "pull_id": "pull_test_001",
            "loaded_at": "2026-07-01T00:00:00",
        },
        {
            "date": "2026-07-01",
            "connector": "google-analytics",
            "metric": "conversions",
            "breakdown_dimension": "device",
            "breakdown_value": "desktop",
            "value": 32.0,
            "pull_id": "pull_test_001",
            "loaded_at": "2026-07-01T00:00:00",
        },
    ]


# ---------------------------------------------------------------------------
# render_report / envelope shape
# ---------------------------------------------------------------------------


def test_get_report_returns_envelope():
    with patch("core.warehouse.query_report", return_value=_fixture_rows()):
        summary, envelope, widget_uri = reports_module.render_report(
            _fake_modules(),
            "default",
            "google-analytics/overview_daily",
            "2026-07-01",
            "2026-07-01",
        )
    assert envelope["schema_version"] == "1"
    assert set(["freshness", "provenance", "alerts", "trace_id", "as_of"]).issubset(
        envelope["meta"].keys()
    )
    prov = envelope["meta"]["provenance"]
    assert prov["source_system"] == "google-analytics"
    assert prov["source_field"] == "fact_daily_kpi"
    assert "pull_id" in prov
    assert envelope["data"]["report_id"] == "google-analytics/overview_daily"
    assert envelope["data"]["metrics"]["sessions"] == 1450
    assert envelope["data"]["metrics"]["conversions"] == 32
    assert envelope["data"]["rows"]  # full dataset present
    assert widget_uri == "ui://core/daily-report"


def test_get_report_unknown_report_id():
    from core.main import get_report
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        get_report(project_id="default", report_id="google-analytics/does_not_exist")
    err = json.loads(str(exc.value))
    assert err["code"] == "not_found"


def test_get_report_honors_flow_card_template():
    """Story 9.8: a flow.report card_template makes get_report serve the card widget.

    Mocks the report render (module-agnostic) + the merged report doc carrying
    card_template="kpi" + the card path. Verifies the served widget switches to the
    card widget uri (the card path took over).
    """
    from core.main import get_report

    fake_env = {
        "schema_version": "1",
        "meta": {"freshness": "live", "provenance": None, "alerts": []},
        "data": {"report_id": "google-analytics/overview_daily", "date_range":
                 {"start": "2026-07-01", "end": "2026-07-01"}, "metrics": {}},
    }
    card_env = {
        "schema_version": "1",
        "meta": {"freshness": "live", "provenance": None, "alerts": [],
                 "card_selection": {"chosen": "kpi"}, "project_id": "default"},
        "data": {"card_id": "kpi", "card_type": "kpi"},
    }
    with patch("core.db.get_connection", return_value=MagicMock()), \
        patch("core.reports.render_report",
               return_value=("resume", fake_env, "ui://core/daily-report")), \
        patch("core.module_enablement.is_module_enabled", return_value=True), \
        patch("core.flows._base_report_doc", return_value={"card_template": "kpi"}), \
        patch("core.flows._fetch_report_override", return_value=None), \
        patch("core.flows._merge_report",
              return_value={"card_template": "kpi", "metric_definitions": None,
                            "llm_commentary_guidelines": None}), \
        patch("core.cards.get_card",
              return_value=("card resume", card_env, "ui://core/card-kpi")) as m_card:
        result = get_report(project_id="default", report_id="google-analytics/overview_daily")

    # The card path was invoked and its widget uri is bound.
    assert m_card.called
    assert result.meta["ui"]["resourceUri"] == "ui://core/card-kpi"


def test_get_report_unknown_module():
    with pytest.raises(reports_module.ReportNotFound):
        reports_module.render_report(
            _fake_modules(), "default", "no-such-module/x", "", ""
        )


def test_get_report_default_date_window():
    report = _GA_REPORT
    start, end = reports_module.resolve_date_window(
        report, "", "", today=date(2026, 7, 31)
    )
    assert end == "2026-07-31"
    assert start == "2026-07-01"  # 30 days back


def test_get_report_narrative_prompt_in_summary():
    with patch("core.warehouse.query_report", return_value=_fixture_rows()):
        summary, _env, _uri = reports_module.render_report(
            _fake_modules(),
            "default",
            "google-analytics/overview_daily",
            "2026-07-01",
            "2026-07-01",
        )
    assert summary.startswith("Analyse GA4 les tendances de trafic.")


def test_get_report_summary_30_line_cap():
    # 40 rows across many metrics/dims -> rollup lines could be many; but cap holds.
    many_rows = []
    for i in range(60):
        many_rows.append(
            {
                "date": f"2026-07-{(i % 28) + 1:02d}",
                "connector": "google-analytics",
                "metric": "sessions",
                "breakdown_dimension": "device",
                "breakdown_value": f"dev{i}",
                "value": float(i),
                "pull_id": "pull_x",
                "loaded_at": "2026-07-01T00:00:00",
            }
        )
    report = dict(_GA_REPORT)
    summary = reports_module.build_summary(
        report, many_rows, "2026-07-01", "2026-07-28", "google-analytics"
    )
    assert len(summary.splitlines()) <= 30


def test_get_report_non_additive_routes_to_semantic_view():
    """A non-additive metric (average_position) must hit query_semantic_view path.

    We assert via the warehouse query_report routing: the semantic builder is
    invoked for the non-additive metric and NOT the fact builder.
    """
    from core import warehouse

    calls = {"semantic": 0, "fact": 0}

    def _fake_runner(sql, params):
        if "semantic_avg_position" in sql:
            calls["semantic"] += 1
            return [
                {
                    "date": "2026-07-01",
                    "connector": "gsc",
                    "breakdown_dimension": "date",
                    "breakdown_value": "2026-07-01",
                    "value": 4.2,
                    "pull_id": "pull_x",
                }
            ]
        calls["fact"] += 1
        return []

    with (
        patch("core.warehouse._db_mode", return_value="duckdb"),
        patch("core.warehouse._duckdb_mart_prefix", return_value="main_marts."),
        patch("core.warehouse._query_duckdb", side_effect=_fake_runner),
    ):
        rows = warehouse.query_report(
            "default", "gsc", ["average_position"], ["date"], "2026-07-01", "2026-07-01"
        )

    assert calls["semantic"] == 1
    assert calls["fact"] == 0
    assert rows and rows[0]["metric"] == "average_position"
