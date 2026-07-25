"""Integration seam tests for Epic 9 cards (review-epic-9-integration F-9/F-10/F-11/F-12).

AI-45 lesson: the card system was tested ONLY via direct core.cards function calls; the
middleware / MCP-protocol / ASGI layers were never exercised. This file closes that gap:

  * MCP tool call ``get_card`` through the FastMCP tool layer for template="keywords" AND
    template="connectors" (context-card exemption path), asserting the AD-1 TRIPLE:
    summary (TextContent) + structuredContent envelope + _meta.ui.resourceUri (F-9/F-10).
  * MCP ``list_card_templates`` through the tool layer (F-12).
  * REST ``GET /api/cards`` (ad-hoc metrics) AND ``GET /api/cards?template=connectors``
    (context exemption) AND ``GET /api/cards/templates?project_id=`` through the REAL
    ``build_asgi_app()`` ASGI stack (F-9/F-10 REST + admin catalog usability).
  * All 6 card widget resource URIs fetchable through the MCP resource layer, each a
    non-empty HTML string (F-11).

MCP tool/resource seams use FastMCPTransport(mcp) (in-process, the established convention
in test_widget_resource.py / test_module_loading.py). REST seams use build_asgi_app() +
TestClient (the G-14 convention in test_cards_api.py). Warehouse / DB are mocked so no live
mart or Postgres is required; identity resolves to "anonymous" over the in-process transport.
"""

from __future__ import annotations

import os

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402
from core.cards import (  # noqa: E402
    ATTRIBUTION_CARD_WIDGET_URI,
    CONNECTORS_CARD_WIDGET_URI,
    CONVERSIONS_CARD_WIDGET_URI,
    DEDUP_CARD_WIDGET_URI,
    JOURNEY_CARD_WIDGET_URI,
    KEYWORDS_CARD_WIDGET_URI,
    KPI_CARD_WIDGET_URI,
    USERTYPES_CARD_WIDGET_URI,
)
from core.main import DAILY_REPORT_WIDGET_URI, mcp  # noqa: E402
from fastmcp.apps import UI_MIME_TYPE  # noqa: E402
from fastmcp.client import Client, FastMCPTransport  # noqa: E402

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _keyword_rows():
    """Rows satisfying the keywords card (clicks+impressions metrics, page dimension)."""
    rows = []
    for d in ("2026-07-05", "2026-07-06"):
        rows += [
            {"date": d, "connector": "gsc", "metric": "clicks",
             "breakdown_dimension": "page", "breakdown_value": "/a", "value": 50.0,
             "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
            {"date": d, "connector": "gsc", "metric": "impressions",
             "breakdown_dimension": "page", "breakdown_value": "/a", "value": 1200.0,
             "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
            {"date": d, "connector": "gsc", "metric": "clicks",
             "breakdown_dimension": "query", "breakdown_value": "chaussures", "value": 40.0,
             "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
            {"date": d, "connector": "gsc", "metric": "impressions",
             "breakdown_dimension": "query", "breakdown_value": "chaussures", "value": 900.0,
             "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
        ]
    return rows


def _cannibalisation_rows():
    """fact_daily_kpi rows for the keywords card (Story 10.5, AI-54 fixture honesty).

    This is EXACTLY what warehouse.query_daily_report can produce from fact_daily_kpi:
    ADDITIVE metrics only (clicks, impressions) at the marginal 'page' grain AND the
    'query>page' composite grain. It carries NO average_position -- fact_daily_kpi never
    does (AD-4 non-additive; test_composite_additive_only.sql forbids it). The composite
    positions the detector also needs come from a SEPARATE source -- see
    _cannibalisation_position_rows / the query_composite_positions mock -- proving the
    real F-1 wiring (query_daily_report cannot manufacture average_position query>page).

    3-day contiguous range (AI-45 multi-day rule). The query 'chaussures de sport' is split
    across two pages on the JOINT query>page composite grain (breakdown_dimension='query>page',
    breakdown_value='<query>>​<page>').

      /sport/   : 550 impr   (55% share)  @ pos 4.5 (from the composite view)
      /running/ : 450 impr   (45% share)  @ pos 8.5 (from the composite view) -> gap 4.0 -> FLAGGED
    """
    rows = []
    for d in ("2026-07-04", "2026-07-05", "2026-07-06"):
        # Marginal page rows -- AI-54 fixture honesty: they RECONCILE with the query>page
        # composite (page total == sum over queries), exactly as the real mart would produce.
        for page, clk, impr in (("/sport/", 44.0, 550.0), ("/running/", 18.0, 450.0)):
            rows += [
                {"date": d, "connector": "gsc", "metric": "clicks",
                 "breakdown_dimension": "page", "breakdown_value": page, "value": clk,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
                {"date": d, "connector": "gsc", "metric": "impressions",
                 "breakdown_dimension": "page", "breakdown_value": page, "value": impr,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
            ]
        # JOINT query>page composite ADDITIVE rows (impressions only -- fact can produce these).
        for page, impr in (("/sport/", 550.0), ("/running/", 450.0)):
            val = f"chaussures de sport>{page}"
            rows += [
                {"date": d, "connector": "gsc", "metric": "impressions",
                 "breakdown_dimension": "query>page", "breakdown_value": val, "value": impr,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"},
            ]
    return rows


def _cannibalisation_position_rows():
    """Composite average_position query>page rows -- from semantic_avg_position_composite.

    This is what warehouse.query_composite_positions returns (NEVER query_daily_report):
    the impression-weighted position per (query, page) cell. Mocking this source SEPARATELY
    from query_daily_report is the whole point of the AI-54 correction -- it proves the F-1
    wiring is what surfaces these rows. Remove the wiring and the detector sees no positions
    -> the `qualifying` filter (avg_position is not None) empties -> flagged=[] -> the test
    below fails (no /sport/ + /running/ rows), which is exactly the guard we want.
    """
    rows = []
    for d in ("2026-07-04", "2026-07-05", "2026-07-06"):
        for page, pos in (("/sport/", 4.5), ("/running/", 8.5)):
            val = f"chaussures de sport>{page}"
            rows.append(
                {"date": d, "connector": "gsc", "metric": "average_position",
                 "breakdown_dimension": "query>page", "breakdown_value": val, "value": pos,
                 "pull_id": "pull_1", "loaded_at": f"{d}T00:00:00"}
            )
    return rows


def _sessions_rows():
    return [
        {"date": "2026-07-05", "connector": "google-analytics", "metric": "sessions",
         "breakdown_dimension": "device", "breakdown_value": "desktop", "value": 100.0,
         "pull_id": "pull_1", "loaded_at": "2026-07-05T00:00:00"},
        {"date": "2026-07-06", "connector": "google-analytics", "metric": "sessions",
         "breakdown_dimension": "device", "breakdown_value": "desktop", "value": 120.0,
         "pull_id": "pull_1", "loaded_at": "2026-07-06T00:00:00"},
    ]


def _meta_from(result):
    """AD-1 _meta.ui.resourceUri accessor (works across fastmcp result shapes)."""
    return getattr(result, "meta", None) or getattr(result, "_meta", None)


# ---------------------------------------------------------------------------
# F-9 / F-10 — get_card MCP tool path through the FastMCP tool layer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_card_keywords_through_mcp_tool_layer_ad1_triple():
    """F-9: get_card(template="keywords") through the MCP tool layer returns the AD-1 triple.

    Exercises identity resolution + scope check + context/alerts fetch middleware that the
    direct core.cards.get_card tests never touch.
    """
    with patch("core.main.warehouse.query_daily_report", return_value=_keyword_rows()):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_card",
                {
                    "project_id": "default",
                    "template": "keywords",
                    "metrics": ["clicks", "impressions"],
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-06",
                },
            )

    assert not result.is_error, f"get_card MCP tool errored: {result}"
    # (1) summary via TextContent
    text_blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    assert text_blocks and text_blocks[0].text.strip(), "missing non-empty TextContent summary"
    # (2) structuredContent envelope
    envelope = result.structured_content or result.data
    assert envelope["data"]["card_id"] == "keywords"
    assert envelope["meta"]["card_selection"]["chosen"] == "keywords"
    # (3) _meta.ui.resourceUri
    meta = _meta_from(result)
    assert meta is not None
    assert meta.get("ui", {}).get("resourceUri") == KEYWORDS_CARD_WIDGET_URI


@pytest.mark.anyio
async def test_get_card_connectors_through_mcp_tool_layer_context_exemption():
    """F-10: get_card(template="connectors") through the MCP tool layer -- the context-card
    exemption path (no metrics / report_ref) wires through, returns the AD-1 triple.

    DB is down inside the resolver -> designed empty envelope (still a valid triple).
    """
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_card",
                {"project_id": "default", "template": "connectors"},
            )

    assert not result.is_error, f"connectors get_card MCP tool errored: {result}"
    text_blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    assert text_blocks and text_blocks[0].text.strip()
    envelope = result.structured_content or result.data
    assert envelope["data"]["card_type"] == "connectors"
    meta = _meta_from(result)
    assert meta.get("ui", {}).get("resourceUri") == CONNECTORS_CARD_WIDGET_URI


# ---------------------------------------------------------------------------
# F-12 — list_card_templates MCP tool path through the tool layer
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_card_templates_through_mcp_tool_layer():
    """F-12: list_card_templates through the MCP tool layer returns the AD-1 catalog."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_card_templates", {})

    assert not result.is_error, f"list_card_templates MCP tool errored: {result}"
    text_blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    assert text_blocks and text_blocks[0].text.strip()
    envelope = result.structured_content or result.data
    ids = {t["id"] for t in envelope["data"]["templates"]}
    # every Epic 9 card + Story 16.3 attribution + Story 17.3 dedup discoverable via tool layer
    assert {
        "kpi", "keywords", "conversions", "usertypes", "journey", "connectors",
        "attribution", "dedup",
    } <= ids


# ---------------------------------------------------------------------------
# F-11 — all 6 card widget resources fetchable through the MCP resource layer
# ---------------------------------------------------------------------------

_ALL_CARD_WIDGET_URIS = [
    KPI_CARD_WIDGET_URI,
    KEYWORDS_CARD_WIDGET_URI,
    CONVERSIONS_CARD_WIDGET_URI,
    USERTYPES_CARD_WIDGET_URI,
    JOURNEY_CARD_WIDGET_URI,
    CONNECTORS_CARD_WIDGET_URI,
    ATTRIBUTION_CARD_WIDGET_URI,
    # Story 17.3: dedup card widget URI
    DEDUP_CARD_WIDGET_URI,
]


@pytest.mark.anyio
async def test_all_card_widget_uris_registered_in_resource_catalog():
    """F-11: all 6 card widget URIs are registered as MCP resources."""
    async with Client(FastMCPTransport(mcp)) as client:
        resources = await client.list_resources()
    uris = {str(r.uri) for r in resources}
    for uri in _ALL_CARD_WIDGET_URIS:
        assert uri in uris, f"{uri} not in MCP resource catalog: {uris}"


@pytest.mark.anyio
async def test_all_card_widget_resources_serve_non_empty_html():
    """F-11: each card widget resource returns a non-empty HTML string (built bundle or a
    graceful 'not built' placeholder -- never blank, never a crash)."""
    async with Client(FastMCPTransport(mcp)) as client:
        for uri in _ALL_CARD_WIDGET_URIS:
            contents = await client.read_resource(uri)
            assert contents, f"{uri} returned no content"
            text = getattr(contents[0], "text", None)
            assert isinstance(text, str) and text.strip(), f"{uri} content not a non-empty string"
            lowered = text.lower()
            assert "<html" in lowered or "<!doctype" in lowered, f"{uri} content is not HTML"


# ---------------------------------------------------------------------------
# Story 9.10 — FastMCP Apps conventions alignment (AC 1, 2, 3) through the
# in-process MCP layer (AI-45/AI-56 seam discipline).
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_all_ui_resources_report_mcp_app_mime_profile():
    """9.10 AC1: every ui://core/* resource lists with the MCP Apps profile mime.

    The registrations pass NO explicit mime_type, so FastMCP auto-serves
    fastmcp.apps.UI_MIME_TYPE ("text/html;profile=mcp-app") -- the profile hosts
    key off to render the resource as an app iframe. Constant imported, never a
    string literal (no duplication drift).
    """
    async with Client(FastMCPTransport(mcp)) as client:
        resources = await client.list_resources()

    ui_resources = {
        str(r.uri): r.mimeType for r in resources if str(r.uri).startswith("ui://core/")
    }
    # All 8 core-registered widget resources must be present (7 Epic 9 + 1 Epic 16)...
    expected = {DAILY_REPORT_WIDGET_URI} | set(_ALL_CARD_WIDGET_URIS)
    assert expected <= set(ui_resources), (
        f"missing ui://core/* resources: {expected - set(ui_resources)}"
    )
    # ...and every ui://core/* resource must carry the profile mime.
    for uri, mime in ui_resources.items():
        assert mime == UI_MIME_TYPE, f"{uri} mimeType={mime!r}, expected {UI_MIME_TYPE!r}"


@pytest.mark.anyio
async def test_submit_feedback_wire_meta_declares_app_only_visibility():
    """9.10 AC2: submit_feedback's wire meta carries {"ui": {"visibility": ["app"]}}.

    Declared via @mcp.tool(app=AppConfig(visibility=["app"])): a pure UI affordance
    (FeedbackBar / CardFeedbackBar callServerTool) that conforming hosts keep out of
    the model's tool list. Wire format checked through tool.to_mcp_tool().meta.
    """
    tool = await mcp.get_tool("submit_feedback")
    assert tool is not None, "submit_feedback must stay registered (and callable)"
    meta = tool.to_mcp_tool().meta
    assert meta is not None
    assert meta.get("ui", {}).get("visibility") == ["app"], f"wire meta.ui: {meta.get('ui')}"


@pytest.mark.anyio
async def test_submit_feedback_visibility_travels_the_mcp_wire():
    """9.10 AC2 (wire seam): tools/list carries submit_feedback's app-only visibility.

    The object-model assertion above reads tool.to_mcp_tool().meta directly; this
    complementary test proves the SAME contract survives the REAL MCP layer the way
    AC1 does -- through Client(FastMCPTransport(mcp)).list_tools() (AI-45/AI-56:
    seam through the MCP layer, assertion of VALUE). A conforming host reads exactly
    this wire meta to keep the pure-UI affordance out of the model's tool list.
    """
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    by_name = {t.name: t for t in tools}
    assert "submit_feedback" in by_name, "submit_feedback must appear in tools/list"
    meta = by_name["submit_feedback"].meta
    assert meta is not None, "submit_feedback wire meta must be present"
    assert meta.get("ui", {}).get("visibility") == ["app"], (
        f"wire tools/list meta.ui: {meta.get('ui')}"
    )


@pytest.mark.anyio
async def test_get_daily_report_declares_static_widget_binding():
    """9.10 AC3: get_daily_report carries the declaration-level MCP Apps binding.

    Its widget is static, so AppConfig(resource_uri=DAILY_REPORT_WIDGET_URI) rides
    the tool declaration (tools/list meta.ui.resourceUri). The result-level
    _meta.ui binding is asserted separately (test_widget_resource.py) and KEPT --
    it is the only channel for get_report/get_card's dynamic template selection.
    """
    tool = await mcp.get_tool("get_daily_report")
    assert tool is not None
    meta = tool.to_mcp_tool().meta
    assert meta is not None
    assert meta.get("ui", {}).get("resourceUri") == DAILY_REPORT_WIDGET_URI


# ---------------------------------------------------------------------------
# Story 16.3 — Attribution card seam (AI-56)
# ---------------------------------------------------------------------------


def _attribution_seam_rows():
    """Multi-day, multi-partition seed rows for the attribution seam test (AI-45/AI-56).

    Three marginal partitions of the SAME conversions. The seam test proves:
    (a) the card resolves through the MCP tool layer end-to-end;
    (b) the last-click bar returns session_source_medium values, NOT campaign values;
    (c) the breakdown_dim_filter prevents double-count (kpi value = 1x partition).
    """
    rows = []
    for d in ("2026-07-05", "2026-07-06", "2026-07-07"):
        # last-click channel (session_source_medium)
        for channel, conv in (("cpc / google", 100.0), ("organic / google", 60.0)):
            rows.append({
                "date": d, "connector": "google-analytics", "metric": "conversions",
                "breakdown_dimension": "session_source_medium", "breakdown_value": channel,
                "value": conv, "pull_id": "pull_attr_seam", "loaded_at": f"{d}T00:00:00",
            })
        # last-click campaign (session_campaign) — same total, different axis
        for campaign, conv in (("summer_sale", 110.0), ("retargeting", 50.0)):
            rows.append({
                "date": d, "connector": "google-analytics", "metric": "conversions",
                "breakdown_dimension": "session_campaign", "breakdown_value": campaign,
                "value": conv, "pull_id": "pull_attr_seam", "loaded_at": f"{d}T00:00:00",
            })
        # first-click channel (first_user_source_medium)
        for channel, conv in (("organic / google", 115.0), ("direct / (none)", 45.0)):
            rows.append({
                "date": d, "connector": "google-analytics", "metric": "conversions",
                "breakdown_dimension": "first_user_source_medium", "breakdown_value": channel,
                "value": conv, "pull_id": "pull_attr_seam", "loaded_at": f"{d}T00:00:00",
            })
    return rows


@pytest.mark.anyio
async def test_get_card_attribution_through_mcp_tool_layer_seam():
    """AI-56 / Story 16.3: get_card(template='attribution') through the FastMCP tool layer.

    Proves the AD-1 triple (summary + envelope + _meta.ui.resourceUri), the
    breakdown_dim_filter prevents double-count (kpi value = session_source_medium ONLY),
    and the last-click bar contains channel values (not campaign names).
    """
    with patch("core.main.warehouse.query_daily_report", return_value=_attribution_seam_rows()):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_card",
                {
                    "project_id": "default",
                    "template": "attribution",
                    "metrics": ["conversions"],
                    "date_from": "2026-07-05",
                    "date_to": "2026-07-07",
                },
            )

    assert not result.is_error, f"get_card attribution MCP tool errored: {result}"

    # (1) summary TextContent
    text_blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    assert text_blocks and text_blocks[0].text.strip(), "missing non-empty TextContent summary"

    # (2) structuredContent envelope
    envelope = result.structured_content or result.data
    assert envelope["data"]["card_id"] == "attribution"
    assert envelope["meta"]["card_selection"]["chosen"] == "attribution"

    # Composition checks
    composition = envelope["data"]["composition"]
    assert len(composition) == 5

    # kpi_row: conversions from session_source_medium ONLY (100+60)/day * 3 days = 480.
    # Must NOT be 960 (2x) or 1440 (3x).
    kpi_block = next(b for b in composition if b["type"] == "kpi_row")
    kpi_metrics = {m["metric"]: m["value"] for m in kpi_block["data"]["metrics"]}
    kpi_conv = kpi_metrics.get("conversions") or 0
    assert 400 <= kpi_conv <= 500, (
        f"Attribution kpi_row double-count: expected ~480, got {kpi_conv}. "
        "The breakdown_dim_filter is not applied."
    )

    # last-click bar: must contain channel values (cpc/organic), NOT campaign names
    last_bar = composition[1]
    assert last_bar["type"] == "bar"
    bar_labels = {b["label"] for b in last_bar["data"].get("bars", [])}
    assert "cpc / google" in bar_labels or "organic / google" in bar_labels, (
        f"Expected channel labels in last-click bar, got: {bar_labels}"
    )
    assert "summer_sale" not in bar_labels, "Campaign label leaked into channel bar"
    assert "retargeting" not in bar_labels, "Campaign label leaked into channel bar"

    # (3) _meta.ui.resourceUri
    meta = _meta_from(result)
    assert meta is not None
    assert meta.get("ui", {}).get("resourceUri") == ATTRIBUTION_CARD_WIDGET_URI


# ---------------------------------------------------------------------------
# F-9 / F-10 (REST) — GET /api/cards through the REAL build_asgi_app() ASGI stack
# ---------------------------------------------------------------------------


def _splice_card_routes_into_admin_router():
    """Append CARDS_ROUTES to the shared admin router (idempotent), as the orchestrator
    does at startup and the existing G-14 test does."""
    from core.admin_api import router as admin_router
    from core.cards_api import CARDS_ROUTES

    existing = {(r.path, tuple(sorted(r.methods or []))) for r in admin_router.routes}
    for route in CARDS_ROUTES:
        key = (route.path, tuple(sorted(route.methods or [])))
        if key not in existing:
            admin_router.routes.append(route)


def test_get_card_rest_ad_hoc_wires_through_build_asgi_app():
    """F-9 (REST): GET /api/cards?metrics=... resolves through the full ASGI stack."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    with patch("core.warehouse.query_daily_report", return_value=_sessions_rows()), patch(
        "core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))
    ), patch("core.project_access.identity_has_project_access", return_value=True):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&metrics=sessions",
                headers={"Host": "localhost"},
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["widget_uri"] == KPI_CARD_WIDGET_URI
    assert body["envelope"]["data"]["card_id"] == "kpi"
    assert body["summary"].strip()


def test_get_card_connectors_rest_context_exemption_wires_through_build_asgi_app():
    """F-10 (REST): GET /api/cards?template=connectors (context exemption, no metrics)
    wires through the full ASGI stack. DB down -> designed empty (still 200)."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    with patch("core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))), \
        patch("core.project_access.identity_has_project_access", return_value=True), \
        patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&template=connectors",
                headers={"Host": "localhost"},
            )
    assert resp.status_code == 200
    assert resp.json()["widget_uri"] == CONNECTORS_CARD_WIDGET_URI


def test_get_card_templates_rest_usability_wires_through_build_asgi_app():
    """F-10 (REST): GET /api/cards/templates?project_id= (admin catalog + usability, auth
    required) wires through the full ASGI stack."""
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    available = [("sessions",), ("active_users",), ("device_category",)]

    class _Cur:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return available

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cur()

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=_Conn())
    ctx.__exit__ = MagicMock(return_value=False)

    with patch("core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))), \
        patch("core.db.get_connection", return_value=ctx), \
        patch("core.project_access.identity_has_project_access", return_value=True):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards/templates?project_id=projA",
                headers={"Host": "localhost"},
            )
    assert resp.status_code == 200
    by_id = {t["id"]: t for t in resp.json()["templates"]}
    # usability computed through the full stack; context card always usable.
    assert by_id["connectors"]["usable"] is True
    assert "usable" in by_id["kpi"]


# ---------------------------------------------------------------------------
# Story 10.5 — cannibalisation block through the REAL build_asgi_app() ASGI stack
# (AI-45 multi-day / AI-56 value-assertion seam).
# ---------------------------------------------------------------------------


def test_cannibalisation_block_through_asgi_seam():
    """Story 10.5 (AI-45/AI-56): the cannibalisation block resolves through the full ASGI
    stack from a 3-day query>page composite seed, and the flagged query yields >= 1 row.

    Asserts on derived VALUES (block present in the composition, flagged query row count
    >= 1), not just HTTP 200 -- the whole point of the AI-56 seam discipline.
    """
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    _splice_card_routes_into_admin_router()
    app = build_asgi_app()

    # AI-54: the TWO real sources are mocked SEPARATELY. query_daily_report returns only
    # what fact_daily_kpi can produce (additive rows, no average_position); the composite
    # positions come from query_composite_positions (semantic_avg_position_composite). This
    # is what proves the F-1 wiring -- if get_card stopped splicing query_composite_positions
    # onto all_rows, no average_position query>page rows would reach the detector and the
    # value assertions below (flagged /sport/ + /running/) would fail.
    with patch("core.warehouse.query_daily_report", return_value=_cannibalisation_rows()), \
        patch("core.warehouse.query_composite_positions",
              return_value=_cannibalisation_position_rows()), \
        patch("core.cards_api._check_auth", new=AsyncMock(return_value=(True, "test@test"))), \
        patch("core.project_access.identity_has_project_access", return_value=True):
        with TestClient(app, raise_server_exceptions=True) as c:
            resp = c.get(
                "/api/cards?project_id=default&template=keywords"
                "&metrics=clicks,impressions&date_from=2026-07-01&date_to=2026-07-06",
                headers={"Host": "localhost"},
            )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["envelope"]["data"]["card_id"] == "keywords"

    composition = body["envelope"]["data"]["composition"]
    cannib_blocks = [b for b in composition if b.get("title") == "Cannibalisation"]
    assert cannib_blocks, "cannibalisation block missing from the composition"
    data = cannib_blocks[0]["data"]
    assert data["columns"] == ["Requête", "Page", "Part (%)", "Position moy."]
    # VALUE assertion: the flagged query yields >= 1 row (both competing pages, in fact).
    flagged_rows = [r for r in data["rows"] if r["Requête"] == "chaussures de sport"]
    assert len(flagged_rows) >= 1
    pages = {r["Page"] for r in flagged_rows}
    assert pages == {"/sport/", "/running/"}


# ---------------------------------------------------------------------------
# Story 17.3 — Déduplication card seam (AI-56)
# ---------------------------------------------------------------------------


def _dedup_seam_rows():
    """Multi-day, multi-channel dedup_estimate rows for the seam test (AI-45/AI-56).

    3 days x 3 channels (meta-ads 80/day, google-ads 60/day, linkedin-ads 20/day).
    GA4 verified = 100/day. claimed_total = 160/day. rate = 1.6/day.
    Window: claimed=480, verified=300, rate=480/300=1.6 (identical to epic AC).
    """
    rows = []
    dates = ["2026-07-05", "2026-07-06", "2026-07-07"]
    channels = [
        ("meta-ads", 80.0, 50.0),
        ("google-ads", 60.0, 37.5),
        ("linkedin-ads", 20.0, 12.5),
    ]
    for d in dates:
        for ch, claimed, dedup in channels:
            rows.append(
                {
                    "date": d,
                    "channel_connector": ch,
                    "claimed_conversions": claimed,
                    "verified_total": 100.0,
                    "claimed_total": 160.0,
                    "duplication_rate": 1.6,
                    "deduplicated_contribution": dedup,
                    "verification_source_type": "ga4",
                    "verification_source_id": "ga4_stream_01",
                    "lead_event_name": "generate_lead",
                    "estimate_label": "estimation",
                    "pull_id": "pull_seam_17_3",
                }
            )
    return rows


@pytest.mark.anyio
async def test_get_card_dedup_through_mcp_tool_layer_seam():
    """AI-56 / Story 17.3: get_card(template='dedup') through the FastMCP tool layer.

    Proves the AD-1 triple (summary + envelope + _meta.ui.resourceUri), the
    duplication_rate = 1.6 (epic-17 AC), and the 'estimation' label is present (AD-9).
    """
    with patch("core.warehouse.query_dedup_estimate", return_value=_dedup_seam_rows()):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_card",
                {
                    "project_id": "default",
                    "template": "dedup",
                    "date_from": "2026-07-05",
                    "date_to": "2026-07-07",
                },
            )

    assert not result.is_error, f"get_card dedup MCP tool errored: {result}"

    # (1) summary TextContent
    text_blocks = [c for c in (result.content or []) if getattr(c, "text", None)]
    assert text_blocks and text_blocks[0].text.strip(), "missing non-empty TextContent summary"

    # (2) structuredContent envelope
    envelope = result.structured_content or result.data
    assert envelope["data"]["card_id"] == "dedup"
    assert envelope["meta"]["card_selection"]["chosen"] == "dedup"

    # Composition: 4 blocks (kpi_row + bar + table + comment)
    composition = envelope["data"]["composition"]
    assert len(composition) == 4

    # kpi_row: rate = 480/300 = 1.6
    kpi_block = next(b for b in composition if b["type"] == "kpi_row")
    rate_val = kpi_block["data"]["metrics"][0]["value"]
    assert abs(rate_val - 1.6) < 0.05, (
        f"Expected duplication_rate ~1.6, got {rate_val}"
    )

    # AD-9: 'estimation' label in kpi_row AND in summary text
    assert kpi_block["data"]["metrics"][0].get("estimate_label") == "Estimation"
    summary_text = text_blocks[0].text
    assert "estimation" in summary_text.lower(), (
        "AD-9: 'estimation' must appear in the summary text channel"
    )

    # Table: 3 channels, Meta has part_pct = 50%
    table_block = next(b for b in composition if b["type"] == "table")
    rows_data = table_block["data"]["rows"]
    assert len(rows_data) == 3
    meta_row = next(r for r in rows_data if r["_dim"] == "meta-ads")
    assert abs(meta_row["part_pct"] - 50.0) < 0.5

    # (3) _meta.ui.resourceUri
    meta = _meta_from(result)
    assert meta is not None
    assert meta.get("ui", {}).get("resourceUri") == DEDUP_CARD_WIDGET_URI
