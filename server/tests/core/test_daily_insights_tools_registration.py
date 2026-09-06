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

#: The three reads a default host may DISCOVER. `publish_daily_insights` is
#: registered like the others and deliberately absent from this set: AD-43 made it
#: declare what it is -- `governance` / `confirmed_write` -- and the capability
#: middleware hides a consequential write from a host that has proven no
#: interactive presence. Registered and not discoverable are two different facts,
#: and this file used to be unable to tell them apart because `mcp.list_tools()`
#: runs the middleware.
_DISCOVERABLE_BY_DEFAULT = _TOOLS - {"publish_daily_insights"}

#: The warehouse rows the resolver now hands back with the availability it built
#: from them. `publish` derives the insight's confidence from the rows carrying
#: the members it CITED (`core.insight_confidence`), so a double that returns no
#: rows would publish an honestly `unmeasurable` insight and prove nothing about
#: the derivation. These back `metric:conversions` over the fixture's period.
_ROWS = [
    {
        "date": "2026-07-21",
        "loaded_at": "2026-07-21T05:00:00+00:00",
        "connector": "google-ads",
        "pull_id": "pull_EXAMPLE",
        "metric": "conversions",
    }
]


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
    """All four are REGISTERED -- read off the declarations, not off discovery."""
    from core import mcp_profiles

    declared = {decl.name for decl in mcp_profiles.registered_declarations()}
    missing = _TOOLS - declared
    assert not missing, f"unregistered daily-insight tools: {missing}"


def test_only_the_reads_reach_a_default_host():
    """Publishing is an assertion nobody asked for; it does not ride the default catalog."""
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert _DISCOVERABLE_BY_DEFAULT <= names
    assert "publish_daily_insights" not in names, (
        "a confirmed write reached the default Insights catalog -- AD-43"
    )


def test_daily_insight_tools_not_namespaced():
    names = [t.name for t in asyncio.run(mcp.list_tools())]
    namespaced = [n for n in names if "/" in n and any(t in n for t in _TOOLS)]
    assert namespaced == []


# ---------------------------------------------------------------------------
# Wrapper seams (mocked identity / availability / DB)
# ---------------------------------------------------------------------------


@pytest.fixture
def _stub_scope_and_inputs(monkeypatch):
    # LES DEUX AIDES SONT A LEUR MODULE, PAS A L'ENTRYPOINT (decoupage de
    # `main.py`, 2026-08-11). `_daily_insight_scope` et
    # `_resolve_daily_insight_inputs` sont privees a `core.daily_insight_mcp` et
    # ne sont appelees que par ses quatre outils : les patcher sur `core.main`
    # rebindait un nom re-exporte que plus personne ne lit.
    # `_project_topic_catalog` reste sur `core.main` -- elle appartient a
    # `core.cards_mcp` et les outils insight la lisent bien par cette adresse.
    # _daily_insight_scope now returns (identity, project_id, access_ok) and accepts strict=.
    monkeypatch.setattr(
        "core.daily_insight_mcp._daily_insight_scope", lambda pid, **kw: ("user_1", pid, True)
    )
    monkeypatch.setattr(
        "core.daily_insight_mcp._resolve_daily_insight_inputs",
        lambda pid, d0, d1, *, identity: ({"conversions", "cost"}, set(), "2026-07-21", [], _ROWS),
    )
    # STORY 53.4 -- ce stub etait absent, et ces tests passaient DE CE FAIT.
    # `_project_topic_catalog("proj_a")` ne resout pas ce projet et rendait donc
    # les gabarits par defaut de la PLATEFORME avec un code de raison que le
    # chemin de publication jetait : la publication se validait contre un
    # catalogue qui n'etait celui d'aucun projet. La publication ferme
    # desormais sur ce cas, donc le test doit dire lequel des deux mondes il
    # exerce -- ici : projet resolu, rien de configure, catalogue = les defauts.
    from core import answerable_topics as _topics

    monkeypatch.setattr(
        "core.main._project_topic_catalog", lambda pid: (_topics.default_catalog(), None)
    )


def test_readiness_wrapper_ready(_stub_scope_and_inputs):
    res = get_daily_insight_readiness("proj_a", "2026-07-21")
    assert res.structured_content["status"] == "ready"


def test_readiness_wrapper_blocked_when_stale(monkeypatch):
    monkeypatch.setattr(
        "core.daily_insight_mcp._daily_insight_scope", lambda pid, **kw: ("user_1", pid, True)
    )
    monkeypatch.setattr(
        "core.daily_insight_mcp._resolve_daily_insight_inputs",
        lambda pid, d0, d1, *, identity: (set(), set(), "2026-07-19", [], []),
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
    # THE ACQUISITION COMMITS ITS OWN ACCESS CONTEXT -- 2026-08-21. The publish
    # path acquires through `core.db.request_connection`, and
    # `install_access_context` commits the two `set_config` statements before the
    # surface writes anything. Counting that commit would make
    # `commit.assert_called_once()` assert the shape of `core/db.py` rather than
    # "the run is committed exactly once", which is the property these tests own.
    # The acquisition itself is measured against a real Postgres in
    # `tests/core/test_mcp_tools_request_connection_rls_pg.py`.
    monkeypatch.setattr("core.db.install_access_context", lambda conn, identity: None)

    payload = {
        "schemaVersion": "1",
        "slot": 0,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": "conversions"},
        # Story 53.4 : la publication cite une metrique que le serveur a mesuree
        # sur la fenetre (`_stub_scope_and_inputs` rend `conversions`). Elle
        # citait le gabarit de sa propre carte -- une citation circulaire.
        "evidenceRefs": ["metric:conversions"],
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
    # THE ACQUISITION COMMITS ITS OWN ACCESS CONTEXT -- 2026-08-21. The publish
    # path acquires through `core.db.request_connection`, and
    # `install_access_context` commits the two `set_config` statements before the
    # surface writes anything. Counting that commit would make
    # `commit.assert_called_once()` assert the shape of `core/db.py` rather than
    # "the run is committed exactly once", which is the property these tests own.
    # The acquisition itself is measured against a real Postgres in
    # `tests/core/test_mcp_tools_request_connection_rls_pg.py`.
    monkeypatch.setattr("core.db.install_access_context", lambda conn, identity: None)
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


def test_publish_wrapper_records_blocked_when_the_day_was_not_ready(monkeypatch):
    """The WIRE, not the guard: this wrapper must resolve readiness and hand it down.

    `_resolve_no_insight_status` refuses a `no_insight` whose readiness is unresolved, so
    a wrapper that forgets to pass it fails closed rather than silently -- but it fails on
    every quiet day, which is not a thing to discover in production. This pins the
    argument at its only production call site. The DQ blocker is what makes the point:
    `dq_blocking` was resolved here and thrown away.
    """
    monkeypatch.setattr(
        "core.daily_insight_mcp._daily_insight_scope", lambda pid, **kw: ("user_1", pid, True)
    )
    monkeypatch.setattr(
        "core.daily_insight_mcp._resolve_daily_insight_inputs",
        lambda pid, d0, d1, *, identity: (
            {"conversions"}, set(), "2026-07-21", ["gap in cost"], _ROWS,
        ),
    )
    conn = _make_conn(fetchone_return=None)
    monkeypatch.setattr("core.db.get_connection", lambda: _ctx(conn))
    # THE ACQUISITION COMMITS ITS OWN ACCESS CONTEXT -- 2026-08-21. The publish
    # path acquires through `core.db.request_connection`, and
    # `install_access_context` commits the two `set_config` statements before the
    # surface writes anything. Counting that commit would make
    # `commit.assert_called_once()` assert the shape of `core/db.py` rather than
    # "the run is committed exactly once", which is the property these tests own.
    # The acquisition itself is measured against a real Postgres in
    # `tests/core/test_mcp_tools_request_connection_rls_pg.py`.
    monkeypatch.setattr("core.db.install_access_context", lambda conn, identity: None)

    res = publish_daily_insights("proj_a", "2026-07-21", items=[], status="no_insight")
    sc = res.structured_content
    assert sc["ok"] is True
    assert sc["status"] == "blocked"
    assert sc["statusCorrectedFrom"] == "no_insight"


class _ctx:
    """Minimal context-manager wrapper so get_connection() works as `with ... as conn`."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


def test_the_publish_wrapper_hands_down_a_confidence_it_measured(
    monkeypatch, _stub_scope_and_inputs
):
    """THE WIRE. `core.insight_confidence` derives; only this call site feeds it.

    A derivation nothing calls is the same object as a declared confidence with a
    longer docstring. What is pinned here is that the production wrapper passes a
    `confidence_fn`, and that the function it passes reads the rows the resolver
    returned -- not an empty list, which would publish `unmeasurable` forever and
    look exactly like a surface with no data yet.
    """
    seen: dict = {}

    def _capture(**kwargs):
        seen.update(kwargs)
        return {"ok": True, "runId": "dir_EXAMPLE", "status": "published", "publishedSlots": [0]}

    monkeypatch.setattr("core.daily_insights_tools.publish", _capture)
    conn = _make_conn(fetchone_return=None)
    monkeypatch.setattr("core.db.get_connection", lambda: _ctx(conn))
    monkeypatch.setattr("core.db.install_access_context", lambda conn, identity: None)

    payload = {
        "schemaVersion": "1",
        "slot": 0,
        "insight": {"title": "t", "summary": "s", "whyItMatters": "w", "confidence": "high"},
        "period": {"dateFrom": "2026-07-21", "dateTo": "2026-07-21"},
        "card": {"mode": "template", "template": "conversions"},
        "evidenceRefs": ["metric:conversions"],
    }
    publish_daily_insights("proj_a", "2026-07-21", items=[payload], status="published")

    confidence_fn = seen.get("confidence_fn")
    assert confidence_fn is not None, "the publication path measures no confidence"
    block = confidence_fn(payload)
    # `_ROWS` backs `metric:conversions` over this period, so the reading is a
    # MEASUREMENT. Were the rows not threaded, this would read `unmeasurable`.
    assert block["reading"] == "high"
    assert block["citedMembers"] == ["metric:conversions"]
