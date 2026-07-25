"""AI-56 seam test for the Epic 35 daily-insight MCP tools (Story 35.2).

Asserts the four tools are registered on the core FastMCP app, and exercises the
main.py wrappers (readiness + publish) end-to-end with mocked identity/availability/DB,
so the wiring (identity/AD-5 -> resolve availability -> delegate -> ToolResult) is proven
in this story rather than at epic-review time.

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from core.main import (
    get_card_capabilities,
    get_daily_insight_readiness,
    mcp,
    publish_daily_insights,
)

_TOOLS = {
    "get_daily_insight_readiness",
    "get_card_capabilities",
    "preview_daily_insight",
    "publish_daily_insights",
}


def _make_conn(fetchone_return=None):
    conn = MagicMock()
    conn.commit = MagicMock()
    conn.rollback = MagicMock()
    cur = MagicMock()
    cur.fetchone = MagicMock(return_value=fetchone_return)
    cur.fetchall = MagicMock(return_value=[])
    cur.description = []
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_daily_insight_tools_registered_on_core_app():
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    missing = _TOOLS - names
    assert not missing, f"unregistered daily-insight tools: {missing}"


def test_daily_insight_tools_not_namespaced():
    names = [t.name for t in asyncio.run(mcp.list_tools())]
    namespaced = [n for n in names if "/" in n and any(t in n for t in _TOOLS)]
    assert namespaced == []


# ---------------------------------------------------------------------------
# Wrapper seams (mocked identity / availability / DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_scope_and_inputs(monkeypatch):
    # _daily_insight_scope now returns (identity, project_id, access_ok) and accepts strict=.
    monkeypatch.setattr(
        "core.main._daily_insight_scope", lambda pid, **kw: ("user_1", pid, True)
    )
    monkeypatch.setattr(
        "core.main._resolve_daily_insight_inputs",
        lambda pid, d0, d1: ({"conversions", "cost"}, set(), "2026-07-21", []),
    )


def test_readiness_wrapper_ready(_stub_scope_and_inputs):
    res = get_daily_insight_readiness("proj_a", "2026-07-21")
    assert res.structured_content["status"] == "ready"


def test_readiness_wrapper_blocked_when_stale(monkeypatch):
    monkeypatch.setattr(
        "core.main._daily_insight_scope", lambda pid, **kw: ("user_1", pid, True)
    )
    monkeypatch.setattr(
        "core.main._resolve_daily_insight_inputs",
        lambda pid, d0, d1: (set(), set(), "2026-07-19", []),
    )
    res = get_daily_insight_readiness("proj_a", "2026-07-21")
    assert res.structured_content["status"] == "blocked"


def test_capabilities_wrapper(_stub_scope_and_inputs):
    res = get_card_capabilities("proj_a")
    sc = res.structured_content
    assert sc["contractVersion"] == "1"
    assert "conversions" in {e["id"] for e in sc["catalog"]}


def test_publish_wrapper_persists_via_store(monkeypatch, _stub_scope_and_inputs):
    conn = _make_conn(fetchone_return=None)  # no existing run; record_run SELECT -> None
    monkeypatch.setattr("core.db.get_connection", lambda: _ctx(conn))

    payload = {
        "schemaVersion": "1",
        "slot": 0,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": "conversions"},
    }
    res = publish_daily_insights("proj_a", "2026-07-21", items=[payload], status="published")
    sc = res.structured_content
    assert sc["ok"] is True and sc["publishedSlots"] == [0]
    assert sc["runId"].startswith("dir_")
    conn.commit.assert_called_once()


def test_publish_wrapper_rejects_bad_item(monkeypatch, _stub_scope_and_inputs):
    from fastmcp.exceptions import ToolError

    conn = _make_conn(fetchone_return=None)
    monkeypatch.setattr("core.db.get_connection", lambda: _ctx(conn))
    bad = {
        "schemaVersion": "1",
        "slot": 0,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": "does_not_exist"},
    }
    with pytest.raises(ToolError):
        publish_daily_insights("proj_a", "2026-07-21", items=[bad], status="published")
    conn.commit.assert_not_called()  # all-or-nothing


class _ctx:
    """Minimal context-manager wrapper so get_connection() works as `with ... as conn`."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False
