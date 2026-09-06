"""Integration smoke test for get_daily_report (Story 1.5, T7;
                                                Story 2.5, AC5, AC6, AC8, T8.3).

Uses FastMCPTransport (in-process, no network) to call the live ``mcp`` instance.
BigQuery client is mocked; DuckDB mode is used with a realistic 90-day fixture.

Covers:
  T7.2 — 90-day mock fixture (810 rows)
  T7.3 — summary <=30 lines, no raw JSON, structuredContent shape, _meta.ui.resourceUri
  T7.4 — tool named exactly "get_daily_report" (no module prefix)
  T7.5 — health tool still responds (Story 1.1 regression)
  T7.6 — list_connectors tool still responds (Story 1.3 regression)
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
from core import confidence as confidence_module
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
    # Story 50.6 -- UPDATED, not weakened. `data.rows` used to be the full dataset
    # in the MODEL-visible channel; `visualization-and-rendering.md` requires the
    # opposite. What stays here is a stated descriptor carrying the EXACT row
    # count, so the model-visible payload can never read as a complete dataset
    # that is really a truncated one; the dataset itself travels whole in `_meta`.
    assert "rows" in data, "data.rows must state what was routed"
    descriptor = data["rows"]
    assert descriptor["withheld"] == "moved_to_app_channel", descriptor
    assert descriptor["row_count"] == len(fixture_rows), (
        f"stated row_count {descriptor['row_count']} != fixture {len(fixture_rows)}"
    )

    # And the dataset really is there, whole, on the app channel -- the two halves
    # of one envelope, which is what makes this a split rather than a loss.
    app_meta = getattr(result, "meta", None) or getattr(result, "_meta", None)
    assert app_meta is not None, "_meta must carry the routed dataset"
    routed = app_meta["toorow.app_payload"]["rows"]
    assert len(routed) == len(fixture_rows), (
        f"routed rows {len(routed)} != fixture {len(fixture_rows)}"
    )


@pytest.mark.anyio
async def test_get_daily_report_meta_carries_app_data_and_no_widget():
    """Story 50.6 -- INVERTED from "must carry _meta.ui.resourceUri" (Story 1.6 AC8).

    A data tool no longer advertises a widget (`visualization-and-rendering.md`,
    "Tool split"). `_meta` is still present and still propagates -- that half of
    the original test is exactly what Story 50.6 depends on -- but it now carries
    the APP DATA the model-channel split routed out of `structuredContent`, not a
    resource advertisement.
    """
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
    assert meta.get("ui") is None, (
        f"a data tool must not advertise a widget resource; got: {meta}"
    )
    # The channel is alive and carries the routed dataset under ONE namespaced key.
    assert "toorow.app_payload" in meta, f"app payload missing from _meta: {meta}"


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
# T7.6 — list_connectors tool regression (Story 1.3)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_connectors_tool_regression():
    """list_connectors tool must still respond after Story 1.5 changes (Story 1.3 regression)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    assert not result.is_error, f"list_connectors returned error: {result}"
    payload = result.structured_content or {}
    data = payload.get("data", payload)
    # The key is `connectors`: Connector is the canonical noun and Module was
    # retired (docs/product-architecture/glossary.md). And since Story 50.6 the
    # list itself rides the APP channel, leaving a stated descriptor behind --
    # so a regression check that only looked for a key name would pass on an
    # empty answer. Assert BOTH: the descriptor is honest about how much moved,
    # and the app channel really carries that many connectors.
    listed = data.get("connectors")
    assert listed is not None, f"Expected 'connectors' in list_connectors response: {data}"

    from core.model_channel import APP_PAYLOAD_META_KEY, WITHHELD_MARKER

    if isinstance(listed, dict) and listed.get("withheld") == WITHHELD_MARKER:
        listed = ((result.meta or {}).get(APP_PAYLOAD_META_KEY) or {}).get("connectors")
    assert isinstance(listed, list) and listed, (
        f"list_connectors answered with no connectors at all: {data}"
    )
    assert data.get("count") == len(listed)


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
    """Return a fake get_connection that answers the HEALTH query, and only it.

    health_row: (status, last_fetched_at, conn_created_at) or None if no connection_ref.

    Two things this fake got wrong, both fixed here, both worth stating because
    they are the reason five tests in this file were red.

    1. It only implemented ``fetchone()``. Story 53.3 / CAV-04 (`2d9c5391`)
       deliberately changed `core.health_enrichment` from reading ONE row --
       the organization's oldest credential, which may have contributed nothing
       to the figures -- to reading EVERY contributing connection and letting the
       worst one decide. That read is `fetchall()`. A fake without it made the
       enrichment swallow an AttributeError and no-op, so the alert the test
       asserted never appeared.

    2. It answered EVERY query with the health row. `get_daily_report` also
       resolves org branding through `core.db.get_connection`, and that query
       selects five columns; handed a three-column health row it raised "not
       enough values to unpack (expected 5, got 3)" straight out of the tool.
       (`core.branding.resolve_org_branding` should never have been able to do
       that -- its contract is "never raised" -- and it no longer can. But a fake
       that answers questions it was not asked is a polluted instrument: it
       cannot tell a product defect from its own noise.) So this one dispatches
       on the SQL and returns nothing for anything that is not the health read.

    3. IT IGNORED THE `WHERE` CLAUSE, and that made every test below vacuous.
       Measured on the second pass of story 53.3: replacing the scope predicate
       with `WHERE (%s IS NOT NULL OR TRUE)` -- which makes the report read EVERY
       organization's connections -- left the file at its exact baseline, and so
       did deleting `AND r.provider = ANY(%s)`, the very filter the story was
       written to add. A fake that answers on `"connection_health" in statement`
       says only "some health query was typed".

       A double cannot evaluate SQL, so it cannot assert the ANSWER. It can
       assert the QUESTION, and that is what it now does: the health read must
       carry the governed project chain and both window bounds, or the fake
       raises. Scope is a property of the statement, which is exactly the kind of
       property a double is entitled to check. The DATA semantics -- project A
       does not inherit project B's revoked credential -- are proven against a
       real database in `test_health_scope_pg.py`, because only a database can
       prove them.
    """

    #: Every fragment the health read must contain to be scoped. Each one is a
    #: separate mutation that used to cost nothing.
    required_scope_fragments = (
        "app.pull_jobs",        # the governed chain, not `connection_ref.owner_org_id`
        "app.datastreams",      # ... through the Datastream, which carries the project
        "ds.project_id = %s",   # THIS project
        "pj.date_from <= %s",   # THIS window (start)
        "pj.date_to >= %s",     # THIS window (end)
    )

    class FakeCursor:
        def __init__(self):
            self._rows: list[tuple] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            statement = sql if isinstance(sql, str) else str(sql)
            is_health_read = "connection_health" in statement
            if is_health_read:
                missing = [f for f in required_scope_fragments if f not in statement]
                assert not missing, (
                    "the connection-health read lost its scope: "
                    f"{missing} absent from the statement. A health verdict that "
                    "is not bound to this project and this window describes "
                    "someone else's connection.\n" + statement
                )
            self._rows = [health_row] if (is_health_read and health_row is not None) else []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

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
# Story 53.3, second pass: the health read is bound to a project and a window
#
# These assert the SQL and its parameters, not the answer. The answer needs a
# database and is proven in `tests/integration/test_health_scope_pg.py`. What is
# provable here -- and what nothing proved before -- is that the scope clauses
# are still in the statement and that the values bound to them are the report's
# own project and dates.
# ---------------------------------------------------------------------------


def _capturing_db(health_row: tuple | None):
    """A fake connection that records every statement and its parameters."""
    captured: list[tuple[str, tuple | None]] = []

    class FakeCursor:
        def __init__(self):
            self._rows: list[tuple] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            statement = sql if isinstance(sql, str) else str(sql)
            captured.append((statement, params))
            is_health_read = "connection_health" in statement
            self._rows = [health_row] if (is_health_read and health_row is not None) else []

        def fetchone(self):
            return self._rows[0] if self._rows else None

        def fetchall(self):
            return list(self._rows)

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

    return _fake_get_connection, captured


def _health_reads(captured):
    return [(sql, params) for sql, params in captured if "connection_health" in sql]


@pytest.mark.anyio
async def test_health_read_binds_this_project_and_this_window():
    """The parameters carry the report's project and its two dates -- not the org.

    The rejected version filtered on
    `r.owner_org_id = (SELECT org_id FROM app.projects WHERE id = %s)`, so the
    only project-shaped value in the query was used to find the ORGANIZATION, and
    no date was bound at all. Both facts are visible in the parameter tuple.
    """
    fixture_rows = _make_fixture_rows(7)
    db, captured = _capturing_db(("ok", _FAKE_LAST_FETCHED, None))

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=db
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
    reads = _health_reads(captured)
    assert reads, "no connection-health read was issued at all"
    for statement, params in reads:
        assert "owner_org_id" not in statement, (
            "the health read is org-scoped again -- one project's badge will "
            f"describe another's credential:\n{statement}"
        )
        assert params is not None
        assert params[0] == "default", f"project not bound: {params!r}"
        assert "2026-01-07" in params, f"window end not bound: {params!r}"
        assert "2026-01-01" in params, f"window start not bound: {params!r}"


@pytest.mark.anyio
async def test_health_read_keeps_the_contributing_provider_filter():
    """`AND r.provider = ANY(%s)` is the story's own addition and nothing tested it.

    Deleting it left the suite at its baseline. It is what stops a revoked
    credential for a connector that contributed NOTHING from putting an
    `auth_expired` banner on a report built from another one.
    """
    fixture_rows = _make_fixture_rows(7)
    db, captured = _capturing_db(("ok", _FAKE_LAST_FETCHED, None))

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=db
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            await client.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
                    "connectors": ["my-connector"],
                },
            )

    reads = _health_reads(captured)
    assert reads, "no connection-health read was issued at all"
    statement, params = reads[0]
    assert "r.provider = ANY(%s)" in statement, (
        f"the contributing-provider filter is gone:\n{statement}"
    )
    assert ["my-connector"] in params, (
        f"the contributors from meta.provenance were not bound: {params!r}"
    )


@pytest.mark.anyio
async def test_one_envelope_carries_one_answer_about_freshness():
    """`stale_since` and `meta.confidence.freshness` are the same question, twice.

    They are computed two lines apart in `core.main` and used to disagree: the
    WORST connection decided the first, `max(loaded_at)` -- the NEWEST load -- the
    second. So a report could say "this source has been frozen since June" and
    "freshness: 1.0" in one payload, and nothing in the suite compared them.

    This asserts the hand-off itself, at the seam. The arbitration it enforces is
    unit-tested in `tests/core/test_confidence_freshness_arbitrage.py`; what only
    this test can prove is that `get_daily_report` actually passes the verdict on.
    """
    fixture_rows = _make_fixture_rows(7)
    frozen_since = datetime(2026, 1, 2, 6, 0, 0, tzinfo=timezone.utc)
    stale_db = _make_health_db(("stale", frozen_since, None))

    captured: dict = {}
    real_compute = confidence_module.compute_confidence

    def _spy(*args, **kwargs):
        captured.update(kwargs)
        return real_compute(*args, **kwargs)

    with patch("core.main.warehouse.query_daily_report", return_value=fixture_rows), patch(
        "core.db.get_connection", new=stale_db
    ), patch.object(confidence_module, "compute_confidence", new=_spy):
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
    stale_since = envelope["meta"]["freshness"].get("stale_since")
    assert stale_since is not None, "the fixture no longer produces a stale verdict"

    assert captured.get("stale_since") == stale_since, (
        "the health verdict on this envelope never reached the confidence term: "
        f"{captured.get('stale_since')!r} != {stale_since!r}"
    )
    assert captured.get("date_to") == "2026-01-07"
    assert captured.get("date_from") == "2026-01-01"


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
