"""MCP-layer seam tests for the measured pre-query gate (Story 11.6, AD-18, AI-56).

Exercises the gate THROUGH the FastMCP tool layer (Client(FastMCPTransport(mcp)))
-- the same seam convention as the other agent-surface stories -- proving:

  * AD-18: adherence is MEASURED, never enforced. A non-adherent data query still
    returns a normal, successful envelope (the gate never blocks).
  * Adherence is recorded in BOTH call orders: context-then-data (adherent=true)
    and data-only / data-then-context (adherent=false), surfaced on meta.gate.
  * AD-1: the ~1-line search_context pointer is added when definitions are served
    (AI-50) and context was NOT consulted, and the <=30-line summary cap holds.
  * The data-tool descriptions carry the "consult context first" rule (NFR9: the
    gate lives in surfaces WE control, no Claude-only hack).

The adherence Postgres FALLBACK is patched out (no live DB needed); the trace_id
is stubbed so the "same session" key is deterministic across the two calls in a
test. AI-13: N/A -- no external API is touched (Langfuse is best-effort).
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from contextlib import contextmanager  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from core import adherence  # noqa: E402
from core.context_search import ContextHit  # noqa: E402
from core.main import mcp  # noqa: E402
from fastmcp.client import Client, FastMCPTransport  # noqa: E402


def _one_hit():
    """A single genuine, NON-EMPTY search_context result.

    [F-5] The adherent path must be proven by a real non-empty retrieval: the mark
    fires AFTER search_context returns >=1 hit ([F-1]). Patching the search to yield
    this hit -- rather than letting the db-less path return [] -- is what makes
    context-then-data truly adherent (the old test only "passed" because the mark
    fired BEFORE retrieval, the F-1 bug).
    """
    return [
        ContextHit(
            id="top_conv",
            kind="topic",
            title="Conversions",
            snippet="Definition des conversions.",
            score=3.0,
            tier="title",
            project_id="p",
            matched=True,
        )
    ]

_DATE_RANGE = {"start": "2026-01-01", "end": "2026-01-07"}


def _rows():
    return [
        {
            "date": "2026-01-01",
            "connector": "my-connector",
            "metric": "conversions",
            "breakdown_dimension": "device_category",
            "breakdown_value": "mobile",
            "value": 10.0,
            "pull_id": "pull_x",
            "loaded_at": "2026-02-01T00:00:00",
        }
    ]


def _text(result):
    blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    return blocks[0].text if blocks else ""


class _NoRowsCursor:
    """Minimal cursor that returns no rows -- lets data-tool DB reads (briefing,
    alerts, module enablement) degrade cleanly on the adherent path."""

    description: list = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _DummyConn:
    """Benign connection for the adherent path: the patched
    ``core.context_search.search_context`` ignores it, and any incidental data-tool
    read gets an empty cursor (so those paths degrade, not crash)."""

    def cursor(self):
        return _NoRowsCursor()

    def commit(self):
        pass

    def rollback(self):
        pass


@contextmanager
def _seam(trace_id: str, *, defs=None, context_hits=None):
    """Patch the warehouse + resolver + adherence sinks + trace id for one session.

    ``defs`` (metric_definitions) is injected via the get_daily_report R6 fetch so
    the pointer path can be exercised without a live report override.

    ``context_hits`` ([F-5]) drives the adherent path: when None, search_context
    hits the db-less resilience path and returns [] (NON-adherent -- the mark does
    NOT fire, [F-1]). When a list is supplied, ``core.context_search.search_context``
    is patched to return it and get_connection yields a benign conn, so a GENUINE
    non-empty retrieval fires the mark -- proving true context-then-data adherence
    rather than the old mark-before-retrieval bug.
    """
    adherence.reset_for_tests()

    if context_hits is None:
        # Empty context corpus so search_context returns cleanly without a DB.
        @contextmanager
        def _conn_cm():
            raise RuntimeError("db-less seam")  # forces the tools' resilience path
            yield  # pragma: no cover
    else:
        @contextmanager
        def _conn_cm():
            yield _DummyConn()

    patches = [
        patch("core.main.warehouse.query_daily_report", return_value=_rows()),
        patch("core.main._resolve_project", side_effect=lambda p: p or "default"),
        patch("core.main.tracing.current_trace_id_hex", return_value=trace_id),
        # No live Postgres: the adherence fallback + context search degrade cleanly.
        patch("core.adherence._record_postgres_fallback", return_value=None),
        patch("core.db.get_connection", _conn_cm),
        patch(
            "core.cards._fetch_r6_adhoc",
            return_value=(defs, None),
        ),
    ]
    if context_hits is not None:
        # [F-5] Patch the REAL retrieval to return one genuine ContextHit so the mark
        # fires AFTER a non-empty return (F-1), not before it.
        patches.append(
            patch("core.context_search.search_context", return_value=context_hits)
        )
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()
        adherence.reset_for_tests()


# ---------------------------------------------------------------------------
# Both call orders (adherent true/false), asserted via meta.gate.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_context_then_data_is_adherent():
    # [F-5] A GENUINE non-empty search_context return marks the session -- the mark
    # fires AFTER retrieval (F-1). (The old test patched the DB to raise, so the
    # search returned [] yet was still marked; it only passed because of the F-1 bug.)
    with _seam("a" * 32, context_hits=_one_hit()):
        async with Client(FastMCPTransport(mcp)) as client:
            search = await client.call_tool(
                "search_context", {"query": "conversions", "project_id": "p"}
            )
            # Prove the retrieval was truly non-empty (not the db-less empty path).
            search_env = search.structured_content or search.data
            assert search_env["data"]["count"] >= 1
            result = await client.call_tool(
                "get_daily_report",
                {"project_id": "p", "date_range": _DATE_RANGE, "connectors": ["my-connector"]},
            )
    assert not result.is_error
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is True
    assert env["meta"]["gate"]["context_tool"] == "search_context"


@pytest.mark.anyio
async def test_context_then_data_not_adherent_when_search_returns_empty():
    """[F-1] A search that retrieves NOTHING must NOT mark the session adherent.

    The db-less resilience path (get_connection raises) yields [] hits, so the mark
    does NOT fire and the subsequent data query is non-adherent -- proving the mark
    landed AFTER a non-empty retrieval, not before it.
    """
    with _seam("a1" * 16):  # context_hits=None -> search returns [] (db-less path)
        async with Client(FastMCPTransport(mcp)) as client:
            search = await client.call_tool(
                "search_context", {"query": "conversions", "project_id": "p"}
            )
            search_env = search.structured_content or search.data
            assert search_env["data"]["count"] == 0  # empty retrieval
            result = await client.call_tool(
                "get_daily_report",
                {"project_id": "p", "date_range": _DATE_RANGE, "connectors": ["my-connector"]},
            )
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is False


@pytest.mark.anyio
async def test_data_only_is_not_adherent_but_still_succeeds():
    """AD-18: a non-adherent query is NOT blocked -- normal successful envelope."""
    with _seam("b" * 32):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"project_id": "p", "date_range": _DATE_RANGE, "connectors": ["my-connector"]},
            )
    assert not result.is_error  # never blocked
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is False
    # The envelope is still a normal, complete data envelope.
    assert env["data"]["rows"]
    assert env["schema_version"] == "1"


@pytest.mark.anyio
async def test_data_then_context_is_not_adherent_for_that_query():
    with _seam("c" * 32):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"project_id": "p", "date_range": _DATE_RANGE, "connectors": ["my-connector"]},
            )
            # A context call AFTER the data query does not change the recorded verdict.
            await client.call_tool("get_procedure", {"name": "whatever", "project_id": "p"})
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is False


# ---------------------------------------------------------------------------
# AD-1: the one-line pointer + <=30-line cap when definitions are served.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pointer_added_when_defs_served_and_not_adherent():
    defs = {"conversions": {"definition": "Conversions attribuees", "direction": "up_good"}}
    with _seam("d" * 32, defs=defs):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"project_id": "p", "date_range": _DATE_RANGE, "connectors": ["my-connector"]},
            )
    summary = _text(result)
    lines = summary.splitlines()
    assert len(lines) <= 30, f"summary broke the 30-line cap: {len(lines)}"
    assert "search_context" in summary  # the pointer is present
    # It is a POINTER, not a dump: the definition text is NOT inlined.
    assert "Conversions attribuees" not in summary


@pytest.mark.anyio
async def test_no_pointer_when_adherent_even_with_defs():
    defs = {"conversions": {"definition": "Conversions attribuees"}}
    # [F-5] Real non-empty retrieval so the session is genuinely adherent.
    with _seam("e" * 32, defs=defs, context_hits=_one_hit()):
        async with Client(FastMCPTransport(mcp)) as client:
            await client.call_tool("search_context", {"query": "conversions", "project_id": "p"})
            result = await client.call_tool(
                "get_daily_report",
                {"project_id": "p", "date_range": _DATE_RANGE, "connectors": ["my-connector"]},
            )
    summary = _text(result)
    assert "search_context" not in summary  # no nag once context was consulted
    assert len(summary.splitlines()) <= 30


# ---------------------------------------------------------------------------
# [F-6] Call-order gate seams for get_report AND get_card (adherent + non-adherent).
# Both data tools stamp meta.gate.adherent per session, exactly like get_daily_report.
# The report render / card render is mocked (module-agnostic) so the tests focus on
# the gate wiring; core.context_search.search_context is patched for the adherent leg.
# ---------------------------------------------------------------------------

_REPORT_ID = "google-analytics/overview_daily"


def _report_env():
    return {
        "schema_version": "1",
        "meta": {"freshness": "live", "provenance": None, "alerts": []},
        "data": {
            "report_id": _REPORT_ID,
            "date_range": {"start": "2026-01-01", "end": "2026-01-07"},
            "metrics": {},
        },
    }


def _card_env():
    return {
        "schema_version": "1",
        "meta": {"freshness": "live", "provenance": None, "alerts": [],
                 "card_selection": {"chosen": "kpi"}, "project_id": "p"},
        "data": {"card_id": "kpi", "card_type": "kpi"},
    }


@contextmanager
def _report_seam(trace_id: str, *, context_hits=None):
    """Patch get_report's render + R6 + module enablement, share the adherence seam."""
    with _seam(trace_id, context_hits=context_hits):
        report_patches = [
            patch("core.module_enablement.is_module_enabled", return_value=True),
            patch("core.reports.render_report",
                  return_value=("report resume", _report_env(), "ui://core/daily-report")),
            patch("core.flows._base_report_doc", return_value={}),
            patch("core.flows._fetch_report_override", return_value=None),
            patch("core.flows._merge_report",
                  return_value={"card_template": "", "metric_definitions": None,
                                "llm_commentary_guidelines": None}),
        ]
        for p in report_patches:
            p.start()
        try:
            yield
        finally:
            for p in report_patches:
                p.stop()


@contextmanager
def _card_seam(trace_id: str, *, context_hits=None):
    """Patch get_card's card render + project access, share the adherence seam."""
    with _seam(trace_id, context_hits=context_hits):
        card_patches = [
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch("core.cards.get_card",
                  return_value=("card resume", _card_env(), "ui://core/card-kpi")),
        ]
        for p in card_patches:
            p.start()
        try:
            yield
        finally:
            for p in card_patches:
                p.stop()


@pytest.mark.anyio
async def test_get_report_context_then_data_is_adherent():
    with _report_seam("11" * 16, context_hits=_one_hit()):
        async with Client(FastMCPTransport(mcp)) as client:
            await client.call_tool("search_context", {"query": "conversions", "project_id": "p"})
            result = await client.call_tool(
                "get_report", {"project_id": "p", "report_id": _REPORT_ID}
            )
    assert not result.is_error
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is True
    assert env["meta"]["gate"]["context_tool"] == "search_context"


@pytest.mark.anyio
async def test_get_report_data_only_is_not_adherent():
    with _report_seam("22" * 16):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_report", {"project_id": "p", "report_id": _REPORT_ID}
            )
    assert not result.is_error  # AD-18: never blocked
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is False


@pytest.mark.anyio
async def test_get_card_context_then_data_is_adherent():
    with _card_seam("33" * 16, context_hits=_one_hit()):
        async with Client(FastMCPTransport(mcp)) as client:
            await client.call_tool("search_context", {"query": "conversions", "project_id": "p"})
            result = await client.call_tool(
                "get_card", {"project_id": "p", "template": "kpi"}
            )
    assert not result.is_error
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is True
    assert env["meta"]["gate"]["context_tool"] == "search_context"


@pytest.mark.anyio
async def test_get_card_data_only_is_not_adherent():
    with _card_seam("44" * 16):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_card", {"project_id": "p", "template": "kpi"}
            )
    assert not result.is_error  # AD-18: never blocked
    env = result.structured_content or result.data
    assert env["meta"]["gate"]["adherent"] is False


# ---------------------------------------------------------------------------
# NFR9: the gate rule lives in the tool DESCRIPTIONS we control.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_data_tool_descriptions_state_consult_context_first():
    async with Client(FastMCPTransport(mcp)) as client:
        tools = {t.name: t for t in await client.list_tools()}
    for name in ("get_daily_report", "get_report", "get_card"):
        desc = (tools[name].description or "")
        assert "search_context" in desc, f"{name} description missing the AD-18 rule"
        assert "AD-18" in desc, f"{name} description missing the AD-18 tag"
