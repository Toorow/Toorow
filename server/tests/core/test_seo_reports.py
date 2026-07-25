"""Tests for Story 6.3 — SEO Expert Pack (AC9).

Tests cover:
  - position_movements WoW delta computation (post-processor)
  - organic_crossview multi-connector merge (GSC + GA4)
  - post_deploy_regressions context event join
  - report schema extension with connectors field
  - regression guard: existing get_report unaffected

All tests use mock warehouse layers or in-process DuckDB fixtures so no live
mart is required (mirrors test_get_report.py pattern).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import reports as reports_module  # noqa: E402, I001
from core.reports import (  # noqa: E402
    POST_PROCESSORS,
    _compute_post_deploy_regressions,
    _compute_wow_position_delta,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeModule:
    def __init__(self, name, manifest, module_reports):
        self.name = name
        self.manifest = manifest
        self.reports = module_reports


_GSC_POSITION_MOVEMENTS_REPORT = {
    "id": "position_movements",
    "display_name": "Mouvements de position & visibilite",
    "metrics": ["average_position", "impressions", "clicks"],
    "dimensions": ["date", "page"],
    "layout": {"chart_type": "line", "order_by": "date", "top_n": 50},
    "narrative_prompt": "Analyse SEO: mouvements de position hebdomadaires.",
    "date_window": {"default_days": 90, "min_days": 14, "max_days": 365},
}

_GSC_ORGANIC_CROSSVIEW_REPORT = {
    "id": "organic_crossview",
    "display_name": "Vue organique croisee GA4 x GSC",
    "metrics": ["clicks", "impressions", "average_position", "sessions"],
    "dimensions": ["date"],
    "connectors": ["gsc", "google-analytics"],
    "layout": {"chart_type": "small_multiples", "order_by": "date"},
    "narrative_prompt": "Vue croisee SEO+Analytics.",
    "date_window": {"default_days": 30, "min_days": 7, "max_days": 90},
}

_GSC_POST_DEPLOY_REPORT = {
    "id": "post_deploy_regressions",
    "display_name": "Regressions post-deploiement",
    "metrics": ["average_position", "clicks", "impressions"],
    "dimensions": ["date", "page"],
    "layout": {"chart_type": "line", "order_by": "date", "top_n": 20},
    "narrative_prompt": "Analyse SEO post-deploiement.",
    "date_window": {"default_days": 60, "min_days": 21, "max_days": 180},
}

_GA_OVERVIEW_REPORT = {
    "id": "overview_daily",
    "display_name": "Vue d'ensemble quotidienne",
    "metrics": ["sessions", "conversions"],
    "dimensions": ["date", "device"],
    "layout": {"chart_type": "line", "order_by": "date"},
    "narrative_prompt": "Analyse GA4 les tendances de trafic.",
    "date_window": {"default_days": 30},
}


def _fake_gsc_modules():
    return [
        _FakeModule(
            "gsc",
            {"widget_ref": "ui://core/daily-report"},
            [
                _GSC_POSITION_MOVEMENTS_REPORT,
                _GSC_ORGANIC_CROSSVIEW_REPORT,
                _GSC_POST_DEPLOY_REPORT,
            ],
        )
    ]


def _fake_ga_modules():
    return [
        _FakeModule(
            "google-analytics",
            {"widget_ref": "ui://core/daily-report"},
            [_GA_OVERVIEW_REPORT],
        )
    ]


def _make_avg_pos_rows(page: str, dates_and_positions: list[tuple[str, float]]) -> list[dict]:
    """Build fact-shaped average_position rows for a given page."""
    rows = []
    for d, pos in dates_and_positions:
        rows.append({
            "date": d,
            "connector": "gsc",
            "metric": "average_position",
            "breakdown_dimension": "page",
            "breakdown_value": page,
            "value": pos,
            "pull_id": "pull_test",
            "loaded_at": None,
        })
    return rows


# ---------------------------------------------------------------------------
# AC9 Test 1: WoW delta computed correctly
# ---------------------------------------------------------------------------

def test_position_movements_wow_delta():
    """Seed 14d of page-level position rows; assert delta_position and movement flags."""
    # last_7d: Jul 8-14, prev_7d: Jul 1-7
    # /blog/: last_7d avg=9.0, prev_7d avg=5.0 → delta=+4 → alert (rank worsened)
    # /docs/: last_7d avg=3.0, prev_7d avg=7.0 → delta=-4 → opportunity (rank improved)
    blog_dates_pos = [
        ("2026-07-01", 5.0), ("2026-07-02", 5.0), ("2026-07-03", 5.0),
        ("2026-07-04", 5.0), ("2026-07-05", 5.0), ("2026-07-06", 5.0), ("2026-07-07", 5.0),
        ("2026-07-08", 9.0), ("2026-07-09", 9.0), ("2026-07-10", 9.0),
        ("2026-07-11", 9.0), ("2026-07-12", 9.0), ("2026-07-13", 9.0), ("2026-07-14", 9.0),
    ]
    docs_dates_pos = [
        ("2026-07-01", 7.0), ("2026-07-02", 7.0), ("2026-07-03", 7.0),
        ("2026-07-04", 7.0), ("2026-07-05", 7.0), ("2026-07-06", 7.0), ("2026-07-07", 7.0),
        ("2026-07-08", 3.0), ("2026-07-09", 3.0), ("2026-07-10", 3.0),
        ("2026-07-11", 3.0), ("2026-07-12", 3.0), ("2026-07-13", 3.0), ("2026-07-14", 3.0),
    ]
    rows = (
        _make_avg_pos_rows("https://example.com/blog/", blog_dates_pos)
        + _make_avg_pos_rows("https://example.com/docs/", docs_dates_pos)
    )
    result = _compute_wow_position_delta(rows, "2026-07-01", "2026-07-14")

    # Find blog rows in last_7d range that have the delta
    blog_delta_rows = [
        r for r in result
        if r.get("breakdown_value") == "https://example.com/blog/"
        and r.get("metric") == "average_position"
        and "delta_position" in r
    ]
    docs_delta_rows = [
        r for r in result
        if r.get("breakdown_value") == "https://example.com/docs/"
        and r.get("metric") == "average_position"
        and "delta_position" in r
    ]

    assert blog_delta_rows, "Blog rows should have delta_position"
    assert docs_delta_rows, "Docs rows should have delta_position"

    blog_delta = blog_delta_rows[0]["delta_position"]
    docs_delta = docs_delta_rows[0]["delta_position"]

    # Blog: position got worse (higher number) → delta positive → alert
    assert blog_delta >= 2.0, f"Expected blog delta >= 2, got {blog_delta}"
    assert blog_delta_rows[0]["movement"] == "alert"

    # Docs: position improved (lower number) → delta negative → opportunity
    assert docs_delta <= -2.0, f"Expected docs delta <= -2, got {docs_delta}"
    assert docs_delta_rows[0]["movement"] == "opportunity"


def test_position_movements_no_delta_when_insufficient_data():
    """Single week of data → no delta computed, rows returned without delta_position."""
    # Only last 7 days — no prev_7d data available
    rows = _make_avg_pos_rows(
        "https://example.com/blog/",
        [(f"2026-07-{8+i:02d}", 7.0) for i in range(7)],
    )
    result = _compute_wow_position_delta(rows, "2026-07-08", "2026-07-14")

    delta_rows = [r for r in result if "delta_position" in r]
    assert not delta_rows, "Should have no delta_position when only one week of data"


# ---------------------------------------------------------------------------
# AC9 Test 3: organic_crossview merges GSC and GA4
# ---------------------------------------------------------------------------

def test_organic_crossview_merges_gsc_and_ga4():
    """Seed gsc + ga4 rows for same dates; assert merged result contains both.

    Cross-view E2E proof: ga4 sessions + gsc clicks joined on same date.
    On 2026-07-01: gsc clicks=42, ga4 sessions=320 (both present in result rows).
    """
    gsc_rows = [
        {
            "date": "2026-07-01",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-07-01",
            "value": 42.0,
            "pull_id": "pull_gsc_001",
            "loaded_at": "2026-07-01T00:00:00",
        },
        {
            "date": "2026-07-01",
            "connector": "gsc",
            "metric": "impressions",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-07-01",
            "value": 850.0,
            "pull_id": "pull_gsc_001",
            "loaded_at": "2026-07-01T00:00:00",
        },
    ]
    ga4_rows = [
        {
            "date": "2026-07-01",
            "connector": "google-analytics",
            "metric": "sessions",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-07-01",
            "value": 320.0,
            "pull_id": "pull_ga4_001",
            "loaded_at": "2026-07-01T00:00:00",
        },
    ]
    combined_rows = gsc_rows + ga4_rows

    with patch("core.warehouse.query_report", return_value=combined_rows):
        summary, envelope, _ = reports_module.render_report(
            _fake_gsc_modules(),
            "default",
            "gsc/organic_crossview",
            "2026-07-01",
            "2026-07-01",
        )

    all_rows = envelope["data"]["rows"]
    metrics_present = {r["metric"] for r in all_rows}
    connectors_present = {r["connector"] for r in all_rows}

    assert "clicks" in metrics_present, f"clicks missing from result: {metrics_present}"
    assert "sessions" in metrics_present, f"sessions missing from result: {metrics_present}"
    assert "gsc" in connectors_present
    assert "google-analytics" in connectors_present

    # Cross-view E2E proof: same date, gsc clicks + ga4 sessions both present.
    date_rows = [r for r in all_rows if r["date"] == "2026-07-01"]
    gsc_click_rows = [
        r for r in date_rows
        if r["connector"] == "gsc" and r["metric"] == "clicks"
    ]
    ga4_session_rows = [
        r for r in date_rows
        if r["connector"] == "google-analytics" and r["metric"] == "sessions"
    ]
    assert gsc_click_rows, "Expected gsc/clicks rows for 2026-07-01"
    assert ga4_session_rows, "Expected ga4/sessions rows for 2026-07-01"
    assert gsc_click_rows[0]["value"] == 42.0
    assert ga4_session_rows[0]["value"] == 320.0


# ---------------------------------------------------------------------------
# AC9 Test 4: organic_crossview missing sessions — no error, note in summary
# ---------------------------------------------------------------------------

def test_organic_crossview_missing_ga4_absent_not_error():
    """Second connector data absent (no sessions rows) -> note in summary, no error."""
    gsc_only_rows = [
        {
            "date": "2026-07-01",
            "connector": "gsc",
            "metric": "clicks",
            "breakdown_dimension": "date",
            "breakdown_value": "2026-07-01",
            "value": 42.0,
            "pull_id": "pull_gsc_001",
            "loaded_at": "2026-07-01T00:00:00",
        },
    ]

    with patch("core.warehouse.query_report", return_value=gsc_only_rows):
        summary, envelope, _ = reports_module.render_report(
            _fake_gsc_modules(),
            "default",
            "gsc/organic_crossview",
            "2026-07-01",
            "2026-07-01",
        )

    all_rows = envelope["data"]["rows"]
    metrics_present = {r["metric"] for r in all_rows}
    assert "sessions" not in metrics_present
    # Scope guard: absent connector data triggers a note in the summary (AC3, AD-2).
    assert "non disponibles" in summary or "Données partielles" in summary


# ---------------------------------------------------------------------------
# AC9 Test 5: post_deploy_regressions with events
# ---------------------------------------------------------------------------

def test_post_deploy_regressions_with_events(tmp_path):
    """Seed 14d pre + 14d post around a deployment event; assert regression delta."""
    import duckdb  # noqa: PLC0415

    # Build a DuckDB file with mirror.context_events
    db_path = tmp_path / "test.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    con.execute(
        """CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date VARCHAR,
            type VARCHAR, label VARCHAR, description VARCHAR, created_by VARCHAR
        )"""
    )
    con.execute(
        "INSERT INTO mirror.context_events VALUES (?,?,?,?,?,?,?)",
        ["evt_001", "default", "2026-07-10", "deployment", "Deploy v1", "test deploy", "test"],
    )
    con.close()

    # Build rows: 9 days pre (Jul 1-9) + 14 days post (Jul 11-24) around event Jul 10
    page = "https://example.com/blog/"
    rows = []
    for i in range(9):  # Jul 1-9 (pre)
        d = f"2026-07-{i+1:02d}"
        rows.append({
            "date": d, "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "page", "breakdown_value": page,
            "value": 5.0, "pull_id": "pull_t", "loaded_at": None,
        })
        rows.append({
            "date": d, "connector": "gsc", "metric": "clicks",
            "breakdown_dimension": "page", "breakdown_value": page,
            "value": 40.0, "pull_id": "pull_t", "loaded_at": None,
        })
    for i in range(14):  # Jul 11-24 (post)
        d = f"2026-07-{i+11:02d}"
        rows.append({
            "date": d, "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "page", "breakdown_value": page,
            "value": 9.0,  # rank worsened after deploy
            "pull_id": "pull_t", "loaded_at": None,
        })
        rows.append({
            "date": d, "connector": "gsc", "metric": "clicks",
            "breakdown_dimension": "page", "breakdown_value": page,
            "value": 20.0,  # clicks dropped after deploy
            "pull_id": "pull_t", "loaded_at": None,
        })

    with patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": str(db_path)}):
        result = _compute_post_deploy_regressions(
            rows, "2026-07-01", "2026-07-24", project_id="default"
        )

    # Verify regression deltas are present
    annotated = [r for r in result if "context_event_id" in r]
    assert annotated, "Expected rows annotated with context_event_id"
    sample = annotated[0]
    assert sample["context_event_id"] == "evt_001"
    assert sample["context_event_label"] == "Deploy v1"
    assert sample["regression_delta_position"] is not None
    # Position worsened (went from 5 -> 9), delta should be positive
    assert sample["regression_delta_position"] > 0, (
        f"Expected positive delta (rank worsened), got {sample['regression_delta_position']}"
    )
    # Clicks dropped, delta should be negative
    if sample["regression_delta_clicks"] is not None:
        assert sample["regression_delta_clicks"] < 0, (
            f"Expected negative click delta, got {sample['regression_delta_clicks']}"
        )


# ---------------------------------------------------------------------------
# AC9 Test 6: post_deploy_regressions no events
# ---------------------------------------------------------------------------

def test_post_deploy_regressions_no_events(tmp_path):
    """No deployment events -> data.context_events = [], narrative carries note."""
    import duckdb  # noqa: PLC0415

    # Empty mirror — no events
    db_path = tmp_path / "empty.duckdb"
    con = duckdb.connect(str(db_path))
    con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    con.execute(
        """CREATE TABLE mirror.context_events (
            id VARCHAR, project_id VARCHAR, event_date VARCHAR,
            type VARCHAR, label VARCHAR, description VARCHAR, created_by VARCHAR
        )"""
    )
    con.close()

    rows = [
        {
            "date": "2026-07-01", "connector": "gsc", "metric": "average_position",
            "breakdown_dimension": "page", "breakdown_value": "https://example.com/",
            "value": 3.0, "pull_id": "pull_t", "loaded_at": None,
        }
    ]

    with (
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": str(db_path)}),
        patch("core.warehouse.query_report", return_value=rows),
    ):
        summary, envelope, _ = reports_module.render_report(
            _fake_gsc_modules(),
            "default",
            "gsc/post_deploy_regressions",
            "2026-07-01",
            "2026-07-31",
        )

    assert "context_events" in envelope["data"]
    assert envelope["data"]["context_events"] == []
    assert "Contexte manquant" in summary


# ---------------------------------------------------------------------------
# AC9 Test 7: report schema extended with connectors field
# ---------------------------------------------------------------------------

def test_report_schema_extended_with_connectors_field():
    """organic_crossview.json validates against the updated report.schema.json."""
    import jsonschema  # noqa: PLC0415

    schema_path = (
        Path(__file__).parent.parent.parent / "core" / "schemas" / "report.schema.json"
    )
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft7Validator(schema)

    report_path = (
        Path(__file__).parent.parent.parent
        / "modules" / "gsc" / "reports" / "organic_crossview.json"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    errors = list(validator.iter_errors(report))
    assert not errors, f"organic_crossview.json schema errors: {errors}"
    assert "connectors" in report
    assert "gsc" in report["connectors"]
    assert "google-analytics" in report["connectors"]


# ---------------------------------------------------------------------------
# AC9 Test 8: existing get_report unaffected (regression guard)
# ---------------------------------------------------------------------------

def test_existing_get_report_unaffected():
    """Call render_report on GA4/overview_daily → identical behavior to pre-story."""
    ga_rows = [
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
    with patch("core.warehouse.query_report", return_value=ga_rows):
        summary, envelope, widget_uri = reports_module.render_report(
            _fake_ga_modules(),
            "default",
            "google-analytics/overview_daily",
            "2026-07-01",
            "2026-07-01",
        )

    assert envelope["schema_version"] == "1"
    assert envelope["data"]["report_id"] == "google-analytics/overview_daily"
    assert envelope["data"]["metrics"]["sessions"] == 1450
    assert envelope["data"]["metrics"]["conversions"] == 32
    assert summary.startswith("Analyse GA4 les tendances de trafic.")
    assert widget_uri == "ui://core/daily-report"
    # No post-processor should have fired
    assert "delta_position" not in str(envelope["data"]["rows"])


# ---------------------------------------------------------------------------
# Bonus: verify POST_PROCESSORS registration
# ---------------------------------------------------------------------------

def test_post_processors_registered():
    """Both post-processors should be registered in POST_PROCESSORS map."""
    assert "position_movements" in POST_PROCESSORS
    assert "post_deploy_regressions" in POST_PROCESSORS
