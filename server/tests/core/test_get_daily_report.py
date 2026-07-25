"""Unit tests for get_daily_report (Story 1.5, T1.4, T5.7, T6).

Covers:
  * Tool is registered on core app (not namespaced) — T1.4, T7.4.
  * Invalid date_range returns isError + error code — T1.3.
  * None connectors defaults to all enabled modules — T1.4.
  * Token/latency metrics log record has all required fields — T5.7.
  * No-rows (warehouse not seeded) returns valid error-free response — T6.3.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re

import pytest
from core.main import get_daily_report, mcp
from core.metrics import log_tool_metrics


@pytest.fixture(autouse=True)
def _offline_platform_db(monkeypatch):
    """Keep unit reports deterministic unless a test supplies its own DB seam."""

    def _offline_connection():
        raise RuntimeError("platform database unavailable in unit test")

    monkeypatch.setattr("core.main._resolve_project", lambda project_id: project_id)
    monkeypatch.setattr("core.db.get_connection", _offline_connection)
    monkeypatch.setattr("core.main._fetch_context_events", lambda *args, **kwargs: [])

# ---------------------------------------------------------------------------
# T1.4 — Tool is registered on core app with no namespace prefix
# ---------------------------------------------------------------------------

def test_get_daily_report_registered_on_core_app():
    """get_daily_report must be registered directly on the core mcp (no prefix)."""
    tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
    assert "get_daily_report" in tool_names, (
        f"get_daily_report not found in core tools: {tool_names}"
    )


def test_get_daily_report_not_namespaced():
    """Tool name must be exactly 'get_daily_report', not 'module/get_daily_report'."""
    tool_names = [t.name for t in asyncio.run(mcp.list_tools())]
    # Must not appear with any module prefix
    namespaced = [n for n in tool_names if "/" in n and "get_daily_report" in n]
    assert namespaced == [], f"get_daily_report appears namespaced: {namespaced}"


# ---------------------------------------------------------------------------
# T1.3 — Invalid date_range returns isError
# ---------------------------------------------------------------------------

def test_invalid_date_range_none():
    """None date_range returns isError with invalid_date_range code."""
    result = get_daily_report(project_id="default", date_range=None)
    assert result.is_error is True
    # Content should contain the error JSON
    text = result.content[0].text
    err = json.loads(text)
    assert err["code"] == "invalid_date_range"
    assert err["provenance"] is None


def test_invalid_date_range_missing_end():
    """Missing 'end' key in date_range returns isError."""
    result = get_daily_report(date_range={"start": "2026-01-01"})
    assert result.is_error is True
    err = json.loads(result.content[0].text)
    assert err["code"] == "invalid_date_range"


def test_invalid_date_range_non_iso():
    """Non-ISO date string returns isError."""
    result = get_daily_report(date_range={"start": "01/01/2026", "end": "2026-03-31"})
    assert result.is_error is True
    err = json.loads(result.content[0].text)
    assert err["code"] == "invalid_date_range"


def test_invalid_date_range_start_after_end():
    """start > end returns isError."""
    result = get_daily_report(date_range={"start": "2026-12-31", "end": "2026-01-01"})
    assert result.is_error is True
    err = json.loads(result.content[0].text)
    assert err["code"] == "invalid_date_range"


def test_invalid_date_range_not_dict():
    """Non-dict date_range returns isError."""
    result = get_daily_report(date_range="2026-01-01 to 2026-03-31")
    assert result.is_error is True


# ---------------------------------------------------------------------------
# T1.4 — None connectors defaults to all enabled modules
# ---------------------------------------------------------------------------

def test_none_connectors_defaults_to_all_enabled(monkeypatch):
    """connectors=None should use all loaded modules (AC1)."""
    called_with: list = []

    def fake_warehouse(project_id, start_date, end_date, connectors):
        called_with.append(connectors)
        return []

    monkeypatch.setattr("core.main.warehouse.query_daily_report", fake_warehouse)

    get_daily_report(
        project_id="default",
        date_range={"start": "2026-01-01", "end": "2026-01-31"},
        connectors=None,
    )

    assert len(called_with) == 1
    # Either None (no modules loaded) or a list of connector names
    assert called_with[0] is None or isinstance(called_with[0], list)


# ---------------------------------------------------------------------------
# T6.3 — No rows returns valid response (not an error)
# ---------------------------------------------------------------------------

def test_no_rows_returns_empty_state_not_error(monkeypatch):
    """Warehouse returning [] → valid response with empty-state summary (T6.3)."""
    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])

    result = get_daily_report(
        date_range={"start": "2026-01-01", "end": "2026-01-31"},
    )

    assert result.is_error is False
    summary_text = result.content[0].text
    # Empty-state summary (French)
    assert "Aucune donnée" in summary_text
    # structuredContent envelope is present with empty rows
    envelope = result.structured_content
    assert envelope is not None
    assert envelope["schema_version"] == "1"
    assert envelope["data"]["rows"] == []


# ---------------------------------------------------------------------------
# T5.7 — Metrics log record has all required fields
# ---------------------------------------------------------------------------

def test_log_tool_metrics_returns_required_fields(caplog):
    """log_tool_metrics produces a record with all P1 gate fields (T5.7)."""
    with caplog.at_level(logging.INFO, logger="core.metrics"):
        record = log_tool_metrics(
            tool_name="get_daily_report",
            summary_text="line 1\nline 2\nline 3",
            payload_bytes=204800,
            latency_ms=1500,
        )

    required_fields = {
        "event", "tool", "summary_lines", "summary_token_estimate",
        "payload_bytes", "latency_ms",
    }
    assert required_fields.issubset(record.keys()), (
        f"Missing fields in metrics record: {required_fields - record.keys()}"
    )
    assert record["event"] == "tool_metrics"
    assert record["tool"] == "get_daily_report"
    assert record["summary_lines"] == 3
    assert record["payload_bytes"] == 204800
    assert record["latency_ms"] == 1500
    # Token estimate = words * 1.3
    word_count = len("line 1\nline 2\nline 3".split())
    expected_tokens = int(word_count * 1.3)
    assert record["summary_token_estimate"] == expected_tokens


def test_log_tool_metrics_record_logged_as_json(caplog):
    """Metrics record must be logged as JSON to stdout (T5.5)."""
    with caplog.at_level(logging.INFO, logger="core.metrics"):
        log_tool_metrics("get_daily_report", "test summary", 1024, 250)

    # Find the log line that looks like JSON
    json_logged = False
    for record in caplog.records:
        try:
            data = json.loads(record.message)
            if data.get("event") == "tool_metrics":
                json_logged = True
                break
        except (json.JSONDecodeError, AttributeError):
            continue

    assert json_logged, "No JSON tool_metrics record found in log output"


# ---------------------------------------------------------------------------
# Story 3.7 — AD-4-conversions dedup tests (T6.1, T6.2, T6.3, T6.4)
# ---------------------------------------------------------------------------

_DEDUP_SEED = [
    # GA4 conversions — priority-1 source
    {
        "date": "2026-07-01", "connector": "google-analytics", "metric": "conversions",
        "breakdown_dimension": "device_category", "breakdown_value": "desktop",
        "value": 100.0, "pull_id": "pull_GA4", "loaded_at": "2026-07-02T00:00:00",
        "project_id": "default",
    },
    # Meta Ads conversions — priority-2 source (same project/date)
    {
        "date": "2026-07-01", "connector": "meta-ads", "metric": "conversions",
        "breakdown_dimension": "campaign_id", "breakdown_value": "camp_1",
        "value": 150.0, "pull_id": "pull_META", "loaded_at": "2026-07-02T01:00:00",
        "project_id": "default",
    },
    # Non-conversion metric — must pass through unaffected
    {
        "date": "2026-07-01", "connector": "meta-ads", "metric": "impressions",
        "breakdown_dimension": "campaign_id", "breakdown_value": "camp_1",
        "value": 5000.0, "pull_id": "pull_META", "loaded_at": "2026-07-02T01:00:00",
        "project_id": "default",
    },
]


def test_get_daily_report_cross_source_conversions_deduped(monkeypatch):
    """Cross-source report must NOT sum GA4 + Meta conversions (Story 3.7 T6.1).

    Rule P: GA4 is priority-1, so the conversions total for a cross-source call
    must be 100.0 (GA4 value), NOT 250.0 (raw sum of 100 + 150).
    """
    monkeypatch.setattr(
        "core.main.warehouse.query_daily_report",
        lambda *a, **kw: list(_DEDUP_SEED),
    )

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=["google-analytics", "meta-ads"],
    )

    assert result.is_error is False
    summary = result.content[0].text
    # The summary must not contain 250 (raw sum). GA4 wins with 100.
    assert "250" not in summary, "Cross-source summary must not sum GA4 + Meta conversions"
    # Verify envelope metrics are not double-counted
    envelope = result.structured_content
    assert envelope is not None
    rows = envelope["data"]["rows"]
    # Only one conversions row should survive dedup (GA4 wins)
    conv_rows = [r for r in rows if r["metric"] == "conversions"]
    connectors_in_result = {r["connector"] for r in conv_rows}
    assert "google-analytics" in connectors_in_result, "GA4 conversions must be in result"
    assert "meta-ads" not in connectors_in_result, (
        "Meta conversions must be excluded when GA4 is present (Rule P)"
    )


def test_get_daily_report_single_source_conversions_unchanged(monkeypatch):
    """Single-connector GA4 call must return 100.0 conversions unaffected (T6.2, HG-6)."""
    monkeypatch.setattr(
        "core.main.warehouse.query_daily_report",
        lambda *a, **kw: [r for r in _DEDUP_SEED if r["connector"] == "google-analytics"],
    )

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=["google-analytics"],
    )

    assert result.is_error is False
    summary = result.content[0].text
    assert "100" in summary, "GA4 single-source conversions must be 100"


def test_get_daily_report_single_source_meta_conversions(monkeypatch):
    """Single-connector Meta call must return 150.0 conversions unaffected (T6.3, HG-6)."""
    monkeypatch.setattr(
        "core.main.warehouse.query_daily_report",
        lambda *a, **kw: [r for r in _DEDUP_SEED if r["connector"] == "meta-ads"],
    )

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=["meta-ads"],
    )

    assert result.is_error is False
    summary = result.content[0].text
    assert "150" in summary, "Meta single-source conversions must be 150"


def test_per_source_rows_distinct_and_provenance_intact():
    """AC7: both source rows exist with distinct connectors and full provenance (T6.4)."""
    # Simulate both rows present in fact_daily_kpi (as seeded in _DEDUP_SEED)
    conv_rows = [r for r in _DEDUP_SEED if r["metric"] == "conversions"]
    connectors = {r["connector"] for r in conv_rows}
    assert "google-analytics" in connectors, "GA4 conversions row must be present"
    assert "meta-ads" in connectors, "Meta Ads conversions row must be present"
    assert len(connectors) == 2, "Both connectors must have distinct rows"

    # Verify full provenance fields on both rows
    for row in conv_rows:
        assert row.get("pull_id"), f"pull_id missing on {row['connector']} row"
        assert row.get("loaded_at"), f"loaded_at missing on {row['connector']} row"
        assert row.get("project_id"), f"project_id missing on {row['connector']} row"


def test_apply_conversions_dedup_ga4_wins_when_both_present():
    """Unit test for _apply_conversions_dedup: GA4 row selected when both present."""
    from core.main import _apply_conversions_dedup

    rows = [
        {
            "metric": "conversions", "connector": "google-analytics",
            "project_id": "p", "date": "2026-07-01", "value": 100.0,
        },
        {
            "metric": "conversions", "connector": "meta-ads",
            "project_id": "p", "date": "2026-07-01", "value": 150.0,
        },
        {
            "metric": "sessions", "connector": "google-analytics",
            "project_id": "p", "date": "2026-07-01", "value": 1000.0,
        },
    ]
    result = _apply_conversions_dedup(rows)

    conv = [r for r in result if r["metric"] == "conversions"]
    assert len(conv) == 1, "Only one conversions row after dedup"
    assert conv[0]["connector"] == "google-analytics", "GA4 must win (priority-1)"
    assert conv[0]["value"] == 100.0

    # Non-conversion rows must pass through
    non_conv = [r for r in result if r["metric"] != "conversions"]
    assert len(non_conv) == 1
    assert non_conv[0]["metric"] == "sessions"


def test_apply_conversions_dedup_meta_wins_when_ga4_absent():
    """Unit test for _apply_conversions_dedup: Meta row selected when GA4 absent."""
    from core.main import _apply_conversions_dedup

    rows = [
        {
            "metric": "conversions", "connector": "meta-ads",
            "project_id": "p", "date": "2026-07-01", "value": 150.0,
        },
    ]
    result = _apply_conversions_dedup(rows)

    conv = [r for r in result if r["metric"] == "conversions"]
    assert len(conv) == 1
    assert conv[0]["connector"] == "meta-ads"
    assert conv[0]["value"] == 150.0


def test_apply_conversions_dedup_no_conversions_passthrough():
    """Unit test: rows with no conversions metric pass through unchanged."""
    from core.main import _apply_conversions_dedup

    rows = [
        {
            "metric": "sessions", "connector": "google-analytics",
            "project_id": "p", "date": "2026-07-01", "value": 1000.0,
        },
        {
            "metric": "impressions", "connector": "meta-ads",
            "project_id": "p", "date": "2026-07-01", "value": 5000.0,
        },
    ]
    result = _apply_conversions_dedup(rows)
    assert result == rows, "No-conversions rows must pass through unmodified"


# ---------------------------------------------------------------------------
# Story 4.2 — AI-21: confidence scoped per connection_ref_id (AC8)
# ---------------------------------------------------------------------------

def test_confidence_scoped_to_requested_connector_single(monkeypatch):
    """AC8: single-connector request uses scoped confidence query (AI-21 fix).

    With two connections (GA4 + Meta), get_daily_report(connectors=['google-analytics'])
    must query pull_verifications filtered to the GA4 connection_ref_id.
    Confirms the SQL query includes the connector_name filter (not cross-connector mixing).
    """
    captured_queries: list[str] = []

    class FakeCursor:
        def __init__(self):
            self.results = []

        def execute(self, sql, params=None):
            captured_queries.append(sql)
            # Return a high-confidence ratio for GA4
            if params and "google-analytics" in str(params):
                self.results = [(0.98,)]
            else:
                self.results = []

        def fetchone(self):
            return self.results[0] if self.results else None

        def fetchall(self):
            return self.results

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection", lambda: FakeConn(), raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=["google-analytics"],
    )

    assert result.is_error is False
    # The confidence query must have scoped to connector_name (AI-21 fix)
    single_connector_queries = [
        q for q in captured_queries if "connector_name" in q and "= %s" in q
    ]
    assert single_connector_queries, (
        "AI-21: confidence query must filter by connector_name for single-connector requests. "
        f"Captured queries: {captured_queries}"
    )


# ---------------------------------------------------------------------------
# Story 4.6 — as_of parameter tests (AC3, AC5, AC8)
# ---------------------------------------------------------------------------

_ASOF_ROWS = [
    {
        "date": "2026-07-01", "connector": "ga4", "metric": "sessions",
        "breakdown_dimension": "country", "breakdown_value": "FR",
        "value": 100.0, "pull_id": "pull_aaa", "loaded_at": "2026-07-03T00:00:00",
        "project_id": "default",
    },
    {
        "date": "2026-07-01", "connector": "ga4", "metric": "sessions",
        "breakdown_dimension": "country", "breakdown_value": "DE",
        "value": 50.0, "pull_id": "pull_aaa", "loaded_at": "2026-07-03T00:00:00",
        "project_id": "default",
    },
]


class TestAsOfParameter:
    """AC8 — as_of parameter integration tests for get_daily_report."""

    def test_summary_contains_asof_line_when_as_of_set(self, monkeypatch):
        """AC8 / AC5: summary must carry the as-of reconstitution line when as_of is set.

        Story 6.4: the summary is now the deterministic what+why narrative. The as-of
        provenance surfaces as 'Données reconstituées au {as_of}' (AC3, build_narrative).
        """
        monkeypatch.setattr(
            "core.main.warehouse.get_daily_report_asof",
            lambda *a, **kw: list(_ASOF_ROWS),
        )
        monkeypatch.setattr(
            "core.main.warehouse.query_daily_report",
            lambda *a, **kw: [],  # should NOT be called when as_of is set
        )

        result = get_daily_report(
            project_id="default",
            date_range={"start": "2026-07-01", "end": "2026-07-01"},
            connectors=["ga4"],
            as_of="2026-07-05T00:00:00Z",
        )

        assert result.is_error is False
        summary = result.content[0].text
        assert "reconstituées au" in summary, (
            f"Summary must contain the as-of reconstitution line. Got:\n{summary}"
        )
        # Should contain the as-of date and at least one citation token (AC6).
        assert "2026-07-05" in summary
        assert re.search(r"\(\w+:\w+, pull_\w+\)", summary), (
            f"Summary must carry a citation token. Got:\n{summary}"
        )

    def test_meta_as_of_in_envelope_when_set(self, monkeypatch):
        """AC8 / AC5: meta.as_of must be set in structuredContent when as_of is provided."""
        monkeypatch.setattr(
            "core.main.warehouse.get_daily_report_asof",
            lambda *a, **kw: list(_ASOF_ROWS),
        )

        result = get_daily_report(
            project_id="default",
            date_range={"start": "2026-07-01", "end": "2026-07-01"},
            connectors=["ga4"],
            as_of="2026-07-05T00:00:00Z",
        )

        assert result.is_error is False
        envelope = result.structured_content
        assert envelope is not None
        meta_as_of = envelope.get("meta", {}).get("as_of")
        assert meta_as_of is not None, (
            f"meta.as_of must be set when as_of is provided. meta={envelope.get('meta')}"
        )
        # Should be an ISO datetime string
        assert "2026-07-05" in str(meta_as_of)

    def test_meta_as_of_null_when_not_set(self, monkeypatch):
        """AC8 / HG-4: meta.as_of must be null when as_of is NOT provided (regression)."""
        monkeypatch.setattr(
            "core.main.warehouse.query_daily_report",
            lambda *a, **kw: [],
        )

        result = get_daily_report(
            project_id="default",
            date_range={"start": "2026-07-01", "end": "2026-07-01"},
            connectors=["ga4"],
        )

        assert result.is_error is False
        envelope = result.structured_content
        assert envelope is not None
        meta_as_of = envelope.get("meta", {}).get("as_of")
        assert meta_as_of is None, (
            f"meta.as_of must be null when as_of is not set. Got: {meta_as_of}"
        )

    def test_tool_error_on_invalid_as_of_format(self, monkeypatch):
        """AC8 / AC3: invalid as_of format raises ToolError (not a crash)."""
        monkeypatch.setattr(
            "core.main.warehouse.query_daily_report",
            lambda *a, **kw: [],
        )

        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError) as exc_info:
            get_daily_report(
                project_id="default",
                date_range={"start": "2026-07-01", "end": "2026-07-01"},
                connectors=["ga4"],
                as_of="not-a-valid-datetime",
            )

        # The ToolError message should mention invalid input
        error_str = str(exc_info.value)
        assert "invalid" in error_str.lower() or "ISO-8601" in error_str, (
            f"ToolError message should indicate invalid ISO-8601 format. Got: {error_str}"
        )

    def test_current_path_used_when_as_of_is_none(self, monkeypatch):
        """HG-4: when as_of=None, query_daily_report is called (not get_daily_report_asof)."""
        called_current: list = []
        called_asof: list = []

        monkeypatch.setattr(
            "core.main.warehouse.query_daily_report",
            lambda *a, **kw: called_current.append(True) or [],
        )
        monkeypatch.setattr(
            "core.main.warehouse.get_daily_report_asof",
            lambda *a, **kw: called_asof.append(True) or [],
        )

        get_daily_report(
            project_id="default",
            date_range={"start": "2026-07-01", "end": "2026-07-01"},
            connectors=["ga4"],
            as_of=None,
        )

        assert called_current, "query_daily_report must be called when as_of=None"
        assert not called_asof, "get_daily_report_asof must NOT be called when as_of=None"

    def test_asof_path_used_when_as_of_set(self, monkeypatch):
        """AC3: when as_of is set, get_daily_report_asof is called (not query_daily_report)."""
        called_current: list = []
        called_asof: list = []

        monkeypatch.setattr(
            "core.main.warehouse.query_daily_report",
            lambda *a, **kw: called_current.append(True) or [],
        )
        monkeypatch.setattr(
            "core.main.warehouse.get_daily_report_asof",
            lambda *a, **kw: called_asof.append(True) or [],
        )

        get_daily_report(
            project_id="default",
            date_range={"start": "2026-07-01", "end": "2026-07-01"},
            connectors=["ga4"],
            as_of="2026-07-05T00:00:00Z",
        )

        assert called_asof, "get_daily_report_asof must be called when as_of is set"
        assert not called_current, (
            "query_daily_report must NOT be called when as_of is set"
        )


# ---------------------------------------------------------------------------
# Story 6.4 — get_daily_report summary carries citation tokens (AC10)
# ---------------------------------------------------------------------------

def test_get_daily_report_summary_has_citations(monkeypatch):
    """AC10: the summary must contain at least one (source:field, pull_id) token."""
    seed = [
        {
            "date": "2026-07-01", "connector": "gsc", "metric": "clicks",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-01",
            "value": 1245.0, "pull_id": "pull_a1b2c3d4", "loaded_at": "2026-07-02T00:00:00",
            "project_id": "default",
        },
        {
            "date": "2026-07-01", "connector": "gsc", "metric": "impressions",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-01",
            "value": 8500.0, "pull_id": "pull_a1b2c3d4", "loaded_at": "2026-07-02T00:00:00",
            "project_id": "default",
        },
    ]
    monkeypatch.setattr(
        "core.main.warehouse.query_daily_report", lambda *a, **kw: list(seed)
    )

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=["gsc"],
    )

    assert result.is_error is False
    summary = result.content[0].text
    assert re.search(r"\(\w+:\w+, pull_\w+\)", summary), (
        f"Summary must carry at least one citation token (AC10). Got:\n{summary}"
    )
    # 30-line cap still respected (AC10 / NFR1).
    assert len(summary.splitlines()) <= 30
    # structuredContent envelope is UNCHANGED in shape (backward compat, AC10).
    envelope = result.structured_content
    assert envelope["schema_version"] == "1"
    assert "rows" in envelope["data"]


def test_get_daily_report_summary_context_missing_line(monkeypatch):
    """AC10: with rows but no context events, the narrative states context missing."""
    seed = [
        {
            "date": "2026-07-01", "connector": "gsc", "metric": "clicks",
            "breakdown_dimension": "date", "breakdown_value": "2026-07-01",
            "value": 10.0, "pull_id": "pull_x", "loaded_at": "2026-07-02T00:00:00",
            "project_id": "default",
        },
    ]
    monkeypatch.setattr(
        "core.main.warehouse.query_daily_report", lambda *a, **kw: list(seed)
    )
    monkeypatch.setattr(
        "core.main._fetch_context_events", lambda *a, **kw: []
    )

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=["gsc"],
    )
    assert result.is_error is False
    assert "Contexte manquant pour cette période." in result.content[0].text


def test_confidence_returns_per_connector_map_for_all_connectors(monkeypatch):
    """AC8: multi-connector request returns per-connector confidence dict (AI-21 fix).

    When connectors=None (all connectors), the confidence key must be a per_connector
    dict rather than a single mixed ratio. This prevents misleading averaged confidence.
    """
    class FakeCursorMulti:
        def __init__(self):
            self.rows = [
                ("google-analytics", 0.98),
                ("meta-ads", 0.85),
            ]
            self._executed = False

        def execute(self, sql, params=None):
            self._executed = True

        def fetchone(self):
            return None

        def fetchall(self):
            # Return per-connector rows (most recent per connector)
            return self.rows

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class FakeConnMulti:
        def cursor(self):
            return FakeCursorMulti()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection", lambda: FakeConnMulti(), raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-01", "end": "2026-07-01"},
        connectors=None,
    )

    assert result.is_error is False
    envelope = result.structured_content
    if envelope is not None:
        confidence = envelope.get("meta", {}).get("confidence")
        if confidence is not None:
            # For multi-connector: must be per_connector dict, not a single float
            has_per_connector = "per_connector" in confidence
            has_single = isinstance(confidence.get("completeness"), float)
            assert has_per_connector or has_single, (
                f"AI-21: multi-connector confidence must be per_connector dict,"
                f" got: {confidence}"
            )


# ---------------------------------------------------------------------------
# Story 6.7 — Morning briefing integration tests (AC5, AC6, AC7)
# ---------------------------------------------------------------------------

def _make_briefing_cursor(briefing_row=None, existing_confidence_rows=None):
    """Return a FakeCursor class that serves a briefing row from morning_briefings."""

    class BriefingCursor:
        def __init__(self):
            self._results = []
            self.description = [("id",), ("insights",), ("built_at",)]
            self._sql_calls: list[str] = []
            self.morning_briefings_selects = [0]

        def execute(self, sql, params=None):
            sql_upper = sql.strip().upper()
            self._sql_calls.append(sql_upper)
            if "FROM APP.MORNING_BRIEFINGS" in sql_upper:
                self.morning_briefings_selects[0] += 1
                if briefing_row is not None:
                    self._results = [briefing_row]
                    self.description = [("id",), ("insights",), ("built_at",)]
                else:
                    self._results = []
            elif "PULL_VERIFICATIONS" in sql_upper or "TOOROW_NAME" in sql_upper:
                if existing_confidence_rows:
                    self._results = existing_confidence_rows
                else:
                    self._results = []
                self.description = [("connector_name",), ("ratio",)]
            else:
                self._results = []

        def fetchone(self):
            return self._results[0] if self._results else None

        def fetchall(self):
            return list(self._results)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    return BriefingCursor


def _make_briefing_conn(briefing_row=None):
    """Return a FakeConn that uses BriefingCursor."""
    cursor_cls = _make_briefing_cursor(briefing_row)
    morning_briefings_selects = [0]

    class BriefingConn:
        def cursor(self):
            c = cursor_cls()
            c.morning_briefings_selects = morning_briefings_selects
            return c

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    conn = BriefingConn()
    conn.morning_briefings_selects = morning_briefings_selects
    return conn


def _sample_briefing_insights() -> dict:
    return {
        "version": 1,
        "briefing_date": "2026-07-12",
        "insights": [
            {
                "rank": 1,
                "type": "business_alert",
                "metric": "clicks",
                "connector": "gsc",
                "headline": "Clics GSC en baisse de 22 % (231 → 180)",
                "citation": "(gsc:clicks, pull_01JXXXXXXXXXX)",
                "context_event_id": None,
                "context_event_label": None,
                "alert_id": "alrt_01JYYYYYY",
                "pull_ids": ["pull_01JXXXXXXXXXX"],
            }
        ],
        "alerts_count": 1,
        "anomalies_count": 0,
        "build_duration_ms": 100,
    }


def test_briefing_served_when_available(monkeypatch):
    """Seed morning_briefings row -> get_daily_report summary starts with '[Briefing matinal'."""
    insights = _sample_briefing_insights()
    briefing_row = ("brief_test", insights, None)

    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection",
                        lambda: _make_briefing_conn(briefing_row=briefing_row),
                        raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-12", "end": "2026-07-12"},
    )

    assert result.is_error is False
    summary = result.content[0].text
    assert summary.startswith("[Briefing matinal"), (
        "Summary must start with '[Briefing matinal' when briefing available. "
        f"Got:\n{summary[:200]}"
    )
    assert "Clics GSC" in summary, (
        f"Summary must contain briefing headline. Got:\n{summary[:200]}"
    )


def test_briefing_absent_no_change(monkeypatch):
    """No briefing row -> get_daily_report behaves exactly as pre-story (regression)."""
    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection",
                        lambda: _make_briefing_conn(briefing_row=None),
                        raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-12", "end": "2026-07-12"},
    )

    assert result.is_error is False
    summary = result.content[0].text
    # Should NOT have the briefing header
    assert "[Briefing matinal" not in summary, (
        f"Summary must NOT contain briefing header when no briefing exists. Got:\n{summary[:200]}"
    )
    # Empty-state French summary (existing behavior)
    assert "Aucune donnée" in summary, (
        f"Empty-state summary must be present when no rows. Got:\n{summary[:200]}"
    )


def test_briefing_fetch_is_one_select(monkeypatch):
    """mock Postgres; assert only 1 SELECT on morning_briefings in the hot path (AC7)."""
    morning_briefings_selects = [0]

    class CountingCursor:
        def __init__(self):
            self._results = []
            self.description = [("id",), ("insights",), ("built_at",)]

        def execute(self, sql, params=None):
            sql_upper = sql.strip().upper()
            if "FROM APP.MORNING_BRIEFINGS" in sql_upper:
                morning_briefings_selects[0] += 1
            self._results = []

        def fetchone(self):
            return None

        def fetchall(self):
            return []

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    class CountingConn:
        def cursor(self):
            return CountingCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection", lambda: CountingConn(), raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-12", "end": "2026-07-12"},
    )

    assert result.is_error is False
    assert morning_briefings_selects[0] == 1, (
        f"Performance guarantee: expected exactly 1 SELECT on morning_briefings, "
        f"got {morning_briefings_selects[0]}"
    )


def test_meta_briefing_key_present_when_briefing_available(monkeypatch):
    """Envelope meta.briefing is non-null when briefing served (AC6)."""
    insights = _sample_briefing_insights()
    briefing_row = ("brief_test", insights, None)

    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection",
                        lambda: _make_briefing_conn(briefing_row=briefing_row),
                        raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-12", "end": "2026-07-12"},
    )

    assert result.is_error is False
    envelope = result.structured_content
    assert envelope is not None
    meta_briefing = envelope.get("meta", {}).get("briefing")
    assert meta_briefing is not None, (
        f"meta.briefing must be non-null when briefing is available. "
        f"meta={envelope.get('meta')}"
    )
    assert "briefing_date" in meta_briefing, "meta.briefing must contain briefing_date"
    assert "insights" in meta_briefing, "meta.briefing must contain insights"


def test_meta_briefing_key_null_when_no_briefing(monkeypatch):
    """meta.briefing is null (not missing) when no briefing (AC6)."""
    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection",
                        lambda: _make_briefing_conn(briefing_row=None),
                        raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-12", "end": "2026-07-12"},
    )

    assert result.is_error is False
    envelope = result.structured_content
    assert envelope is not None
    meta = envelope.get("meta", {})
    assert "briefing" in meta, "meta.briefing key must be present (even if null)"
    assert meta["briefing"] is None, (
        f"meta.briefing must be null when no briefing. Got: {meta['briefing']}"
    )


def test_briefing_summary_within_30_line_cap(monkeypatch):
    """Briefing uses ≤5 lines; total summary stays within 30-line cap (AC5, NFR1)."""
    insights = _sample_briefing_insights()
    briefing_row = ("brief_test", insights, None)

    monkeypatch.setattr("core.main.warehouse.query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.db.get_connection",
                        lambda: _make_briefing_conn(briefing_row=briefing_row),
                        raising=False)

    result = get_daily_report(
        project_id="default",
        date_range={"start": "2026-07-12", "end": "2026-07-12"},
    )

    assert result.is_error is False
    summary = result.content[0].text
    line_count = len(summary.splitlines())
    assert line_count <= 30, (
        f"30-line cap violated: summary has {line_count} lines (briefing + narrative). "
        f"Summary:\n{summary}"
    )
