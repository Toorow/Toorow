"""Integration smoke test for get_daily_report (Story 1.5, T7;
                                                Story 2.5, AC5, AC6, AC8, T8.3).

Uses FastMCPTransport (in-process, no network) to call the live ``mcp`` instance.
BigQuery client is mocked; DuckDB mode is used with a realistic 90-day fixture.

Covers:
  T7.2 — 90-day mock fixture (810 rows)
  T7.3 — summary <=30 lines, no raw JSON, structuredContent shape, _meta.ui.resourceUri
  T7.4 — tool named exactly "get_daily_report" (no module prefix)
  T7.5 — health tool still responds (Story 1.1 regression)
  T7.6 — list_modules tool still responds (Story 1.3 regression)
  Story 2.5 AC8 — auth_expired alert in envelope when revoked connection
  Story 2.5 AC5 — stale_since populated when stale connection
  Story 2.5 no-op — Epic 1 backward compat when no connection_ref exists
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from core.main import mcp
from fastmcp.client import Client, FastMCPTransport

# Story 2.5: disable health poller thread during all test imports.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fixture_rows(days: int = 90) -> list[dict]:
    """Generate a realistic ~810-row fixture (90d × 3 metrics × 3 devices)."""
    metrics = ["sessions", "active_users", "conversions"]
    devices = ["desktop", "mobile", "tablet"]
    countries = ["France", "Germany", "Spain"]

    start = date(2026, 1, 1)
    rows = []
    for d in range(days):
        current_date = (start + timedelta(days=d)).isoformat()
        for metric in metrics:
            for device in devices:
                for country in countries:
                    rows.append(
                        {
                            "date": current_date,
                            "connector": "my-connector",
                            "metric": metric,
                            "breakdown_dimension": "device_category",
                            "breakdown_value": device,
                            "value": 100.0,
                            "pull_id": "pull_01KX6J2VS4QN621V9V1MSJ1SG3",
                            "loaded_at": "2026-04-01T00:00:00",
                        }
                    )
    return rows


# ---------------------------------------------------------------------------
# T7.3 — Core assertions on get_daily_report result shape
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_daily_report_summary_under_30_lines():
    """Summary text must be ≤30 lines (NFR1 P1 gate, AC2)."""
    fixture_rows = _make_fixture_rows(90)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-03-31"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error, f"Tool returned error: {result}"

    # Text channel must be ≤30 lines
    text_content = result.content[0].text if result.content else ""
    lines = text_content.splitlines()
    assert len(lines) <= 30, (
        f"Summary exceeded 30 lines: {len(lines)}\n---\n{text_content}"
    )


@pytest.mark.anyio
async def test_get_daily_report_summary_no_raw_json():
    """Summary must not contain raw JSON rows (AD-1 token-burn split)."""
    fixture_rows = _make_fixture_rows(30)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "date_range": {"start": "2026-01-01", "end": "2026-01-30"},
                },
            )

    text_content = result.content[0].text if result.content else ""
    assert "breakdown_dimension" not in text_content
    assert '"date":' not in text_content


@pytest.mark.anyio
async def test_get_daily_report_structured_content_schema():
    """structuredContent must match the canonical AD-1 envelope (AC3)."""
    fixture_rows = _make_fixture_rows(7)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert result.structured_content is not None, "structuredContent must be present"
    envelope = result.structured_content

    assert envelope["schema_version"] == "1", "schema_version must be '1'"
    assert "meta" in envelope
    assert "data" in envelope

    meta = envelope["meta"]
    assert "freshness" in meta
    assert "provenance" in meta
    assert "alerts" in meta
    assert isinstance(meta["alerts"], list), "meta.alerts must be a list"

    data = envelope["data"]
    assert "rows" in data, "data.rows must be present"
    assert len(data["rows"]) == len(fixture_rows), (
        f"data.rows length {len(data['rows'])} != fixture {len(fixture_rows)}"
    )


@pytest.mark.anyio
async def test_get_daily_report_meta_ui_resource_uri():
    """Tool result must carry _meta.ui.resourceUri = 'ui://core/daily-report' (Story 1.6 AC8)."""
    with patch("core.main.warehouse.query_daily_report", return_value=[]):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"date_range": {"start": "2026-01-01", "end": "2026-01-31"}},
            )

    # FastMCP surfaces _meta via result._meta (or the raw MCP result meta field)
    # The ToolResult.meta field is exposed on the client result as result.meta
    assert hasattr(result, "meta") or hasattr(result, "_meta"), (
        "Result must have a meta field"
    )
    meta = getattr(result, "meta", None) or getattr(result, "_meta", None)
    # review-1-5 F-03: unconditional — a regression in _meta propagation must
    # fail loudly, not skip the assertion.
    assert meta is not None, "_meta must propagate to the client result (AC4)"
    assert meta.get("ui", {}).get("resourceUri") == "ui://core/daily-report", (
        f"Expected ui://core/daily-report, got: {meta}"
    )


# ---------------------------------------------------------------------------
# T7.4 — Tool appears in tool list with no module prefix
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_daily_report_tool_name_exact():
    """Tool must be named exactly 'get_daily_report' (not namespaced) in core app."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    assert "get_daily_report" in tool_names, (
        f"get_daily_report not found in tools: {tool_names}"
    )
    # Must not appear with any module-name prefix
    assert "google-analytics_get_daily_report" not in tool_names
    assert "google-analytics/get_daily_report" not in tool_names


# ---------------------------------------------------------------------------
# T7.5 — health tool regression (Story 1.1)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_health_tool_regression():
    """health tool must still respond after Story 1.5 changes (Story 1.1 regression)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("health", {})

    assert not result.is_error, f"health returned error: {result}"
    payload = result.structured_content or {}
    data = payload.get("data", {})
    assert data.get("status") == "ok"


# ---------------------------------------------------------------------------
# T7.6 — list_modules tool regression (Story 1.3)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_tool_regression():
    """list_modules tool must still respond after Story 1.5 changes (Story 1.3 regression)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    assert not result.is_error, f"list_modules returned error: {result}"
    payload = result.structured_content or {}
    data = payload.get("data", payload)
    assert "modules" in data, f"Expected 'modules' in list_modules response: {data}"


# ---------------------------------------------------------------------------
# T7.3 extended — invalid date_range returns isError from client perspective
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_get_daily_report_invalid_date_range_via_client():
    """Invalid date_range must surface as isError to MCP client (T1.3).

    FastMCP raises ToolError when isError=True. Catching it confirms the
    error propagates correctly through the MCP protocol.
    """
    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc_info:
        async with Client(FastMCPTransport(mcp)) as client:
            await client.call_tool(
                "get_daily_report",
                {"date_range": {"start": "bad-date", "end": "also-bad"}},
            )

    assert "invalid_date_range" in str(exc_info.value)


# ---------------------------------------------------------------------------
# Story 2.5: health-aware envelope enrichment (AC5, AC6, AC8, T8.3)
# ---------------------------------------------------------------------------

# Helpers to build fake DB scenarios for _enrich_envelope_with_health

_FAKE_LAST_FETCHED = datetime(2026, 7, 9, 12, 0, 0, tzinfo=timezone.utc)


def _make_health_db(health_row: tuple | None):
    """Return a fake get_connection yielding a connection with one health row.

    health_row: (status, last_fetched_at, conn_created_at) or None if no connection_ref.
    """

    class FakeCursor:
        def __init__(self):
            self._row = health_row

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return self._row

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _fake_get_connection():
        yield FakeConn()

    return _fake_get_connection


@pytest.mark.anyio
async def test_get_daily_report_revoked_connection_adds_auth_expired_alert():
    """Story 2.5 AC8: revoked connection -> meta.alerts contains auth_expired (T8.3)."""
    fixture_rows = _make_fixture_rows(7)

    # Fake DB returns a revoked health row for project_id="default"
    revoked_health_db = _make_health_db(
        ("revoked", None, datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc))
    )

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=revoked_health_db
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error, f"Tool returned error: {result}"
    envelope = result.structured_content
    assert envelope is not None
    meta = envelope.get("meta", {})
    alerts = meta.get("alerts", [])

    # AC8(b): meta.alerts must contain auth_expired
    auth_expired_alerts = [a for a in alerts if a.get("code") == "auth_expired"]
    assert len(auth_expired_alerts) >= 1, (
        f"Expected auth_expired alert in meta.alerts, got: {alerts}"
    )
    assert auth_expired_alerts[0]["severity"] == "error"


@pytest.mark.anyio
async def test_get_daily_report_revoked_connection_includes_summary_line():
    """Story 2.5 AC8(a): revoked connection -> text summary mentions token expired."""
    fixture_rows = _make_fixture_rows(7)

    revoked_health_db = _make_health_db(
        ("revoked", None, datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc))
    )

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=revoked_health_db
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                },
            )

    text = result.content[0].text if result.content else ""
    assert "expire" in text.lower() or "Token" in text, (
        f"Expected token expiry mention in summary, got: {text!r}"
    )


@pytest.mark.anyio
async def test_get_daily_report_stale_connection_sets_stale_since():
    """Story 2.5 AC5: stale connection -> meta.freshness.stale_since is populated."""
    fixture_rows = _make_fixture_rows(7)

    stale_health_db = _make_health_db(
        ("stale", _FAKE_LAST_FETCHED, datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc))
    )

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=stale_health_db
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                },
            )

    assert not result.is_error
    envelope = result.structured_content
    meta = envelope.get("meta", {})
    freshness = meta.get("freshness", {})
    assert freshness.get("stale_since") is not None, (
        f"Expected stale_since to be set for stale connection, got freshness: {freshness}"
    )


@pytest.mark.anyio
async def test_get_daily_report_ok_connection_no_alert_no_stale():
    """Story 2.5 AC5: ok + recent last_fetched_at -> no auth_expired, no stale_since."""
    fixture_rows = _make_fixture_rows(7)

    # Recent last_fetched_at (1 hour ago, well within 26h cadence)
    recent_fetched = datetime.now(tz=timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    ok_health_db = _make_health_db(
        ("ok", recent_fetched, datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc))
    )

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=ok_health_db
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                },
            )

    assert not result.is_error
    envelope = result.structured_content
    meta = envelope.get("meta", {})
    alerts = meta.get("alerts", [])
    auth_expired = [a for a in alerts if a.get("code") == "auth_expired"]
    assert len(auth_expired) == 0, f"Unexpected auth_expired alert for ok connection: {alerts}"


@pytest.mark.anyio
async def test_get_daily_report_no_connection_ref_is_no_op():
    """Story 2.5 AC backward compat: no connection_ref -> no health enrichment (Epic 1 tests)."""
    fixture_rows = _make_fixture_rows(7)

    # DB returns None (no connection_ref for this project)
    no_connection_db = _make_health_db(None)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=no_connection_db
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                },
            )

    assert not result.is_error
    envelope = result.structured_content
    meta = envelope.get("meta", {})
    alerts = meta.get("alerts", [])
    # No auth_expired alert when no connection_ref
    auth_expired = [a for a in alerts if a.get("code") == "auth_expired"]
    assert len(auth_expired) == 0, f"Unexpected alert when no connection_ref: {alerts}"


@pytest.mark.anyio
async def test_get_daily_report_db_unreachable_is_no_op():
    """Story 2.5 backward compat: DB unreachable -> no crash, envelope unmodified."""
    fixture_rows = _make_fixture_rows(7)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db._psycopg", None  # simulate psycopg not installed
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                },
            )

    assert not result.is_error
    # Must have the standard envelope shape
    envelope = result.structured_content
    assert envelope is not None
    assert "meta" in envelope
    assert "data" in envelope


# ---------------------------------------------------------------------------
# AI-50: metric_definitions injected into get_daily_report envelope
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_daily_report_carries_metric_definitions_when_pack_declares_them():
    """AI-50: when _fetch_r6_adhoc returns metric_definitions they appear in envelope.data."""
    fixture_rows = _make_fixture_rows(7)
    fake_defs = {"sessions": {"direction": "up_good", "unit": "users"}}

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), \
         patch("core.cards._fetch_r6_adhoc", return_value=(fake_defs, None)):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error, f"Tool returned error: {result}"
    envelope = result.structured_content
    assert envelope is not None
    data = envelope.get("data", {})
    assert "metric_definitions" in data, (
        f"Expected metric_definitions in envelope.data, got keys: {list(data.keys())}"
    )
    assert data["metric_definitions"]["sessions"]["direction"] == "up_good"


@pytest.mark.anyio
async def test_get_daily_report_carries_llm_guidelines_when_pack_declares_them():
    """AI-50: when _fetch_r6_adhoc returns guidelines they appear in envelope.data."""
    fixture_rows = _make_fixture_rows(7)
    fake_guidelines = "Mettez en avant la tendance hebdomadaire."

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), \
         patch("core.cards._fetch_r6_adhoc", return_value=(None, fake_guidelines)):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error, f"Tool returned error: {result}"
    data = result.structured_content.get("data", {})
    assert "llm_commentary_guidelines" in data, (
        f"Expected llm_commentary_guidelines in envelope.data, got keys: {list(data.keys())}"
    )
    assert data["llm_commentary_guidelines"] == fake_guidelines


@pytest.mark.anyio
async def test_get_daily_report_omits_metric_definitions_when_pack_absent():
    """AI-50 degrade: when _fetch_r6_adhoc returns (None, None) no keys are injected."""
    fixture_rows = _make_fixture_rows(7)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), \
         patch("core.cards._fetch_r6_adhoc", return_value=(None, None)):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error, f"Tool returned error: {result}"
    data = result.structured_content.get("data", {})
    assert "metric_definitions" not in data, (
        "metric_definitions must be absent when pack returns None"
    )
    assert "llm_commentary_guidelines" not in data, (
        "llm_commentary_guidelines must be absent when pack returns None"
    )


@pytest.mark.anyio
async def test_get_daily_report_degrades_cleanly_when_fetch_r6_raises():
    """AI-50 degrade: if _fetch_r6_adhoc raises, the envelope is still returned normally."""
    fixture_rows = _make_fixture_rows(7)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), \
         patch("core.cards._fetch_r6_adhoc", side_effect=RuntimeError("cards import failed")):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error, "Tool must not error when _fetch_r6_adhoc raises"
    envelope = result.structured_content
    assert envelope is not None
    assert "meta" in envelope
    assert "data" in envelope
    # metric_definitions is absent (best-effort omit)
    data = envelope.get("data", {})
    assert "metric_definitions" not in data


@pytest.mark.anyio
async def test_get_daily_report_existing_tests_unaffected_additive_key():
    """AI-50 regression: existing envelope shape is additive -- existing keys still present."""
    fixture_rows = _make_fixture_rows(7)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), \
         patch("core.cards._fetch_r6_adhoc", return_value=(None, None)):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    assert not result.is_error
    envelope = result.structured_content
    # Core schema keys remain unchanged (additive policy, AI-31)
    assert envelope["schema_version"] == "1"
    assert "freshness" in envelope["meta"]
    assert "provenance" in envelope["meta"]
    assert "alerts" in envelope["meta"]
    assert "rows" in envelope["data"]
