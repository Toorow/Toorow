"""Integration tests for module auto-discovery and namespaced mount (Story 1.3 — T7).

Uses FastMCPTransport (in-process, no network) to call tools on the live
``mcp`` server instance from core.main.

Covers:
  T7.1 — list_modules() returns google-analytics + standard_daily profile.
  T7.1 — google-analytics_get_ga4_report tool is discoverable.
  T7.1 — health tool still responds (Story 1.1 regression check).
  T7.2 — Broken module absent from list_modules(); module_skipped logged.
  T7.3 — core/ contains no module-specific strings (AC5 / AD-2).

Story 15.8 (F-4 klaviyo) — additions STRICTEMENT ADDITIVES :
  T15.8.A — klaviyo present dans list_modules() (decouverte + montage).
  T15.8.B — klaviyo_get_klaviyo_report discoverable dans la liste des outils.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# The server under test.
#
# We import ``mcp`` from core.main — this is the live FastMCP instance with
# module auto-discovery already executed at import time (the real server/modules/
# google-analytics/ directory is scanned).
# ---------------------------------------------------------------------------
from core.main import mcp
from fastmcp.client import Client, FastMCPTransport

# ---------------------------------------------------------------------------
# T7.1 — list_modules returns google-analytics with standard_daily profile
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_includes_google_analytics():
    """list_modules() tool returns google-analytics with report profiles."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    assert result.data is not None or result.structured_content is not None
    # The tool returns structured content; parse from either field.
    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload

    modules = data_section.get("modules", [])
    names = [m["name"] for m in modules]
    assert "google-analytics" in names, f"google-analytics not in modules: {names}"

    ga4 = next(m for m in modules if m["name"] == "google-analytics")
    profile_ids = [p["id"] for p in ga4.get("report_profiles", [])]
    assert "standard_daily" in profile_ids, f"standard_daily not in profiles: {profile_ids}"


# ---------------------------------------------------------------------------
# T7.1 — google-analytics_get_ga4_report is discoverable in tool list
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ga4_tool_in_tool_list():
    """google-analytics_get_ga4_report is discoverable via list_tools."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    # FastMCP 3.4.4 Namespace transform: separator is underscore.
    assert "google-analytics_get_ga4_report" in tool_names, (
        f"Expected google-analytics_get_ga4_report in tools, got: {tool_names}"
    )


# ---------------------------------------------------------------------------
# T7.1 — ga4 report tool returns canonical AD-1 envelope shape
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_ga4_report_tool_returns_canonical_envelope():
    """get_ga4_report returns the canonical AD-1 envelope shape."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool(
            "google-analytics_get_ga4_report",
            {"project_id": "default", "report_profile": "standard_daily"},
        )

    assert not result.is_error, f"Tool returned error: {result}"
    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        envelope = payload
    else:
        envelope = payload

    assert "schema_version" in envelope, "Missing schema_version in envelope"
    assert "meta" in envelope, "Missing meta in envelope"
    assert "data" in envelope, "Missing data in envelope"

    meta = envelope["meta"]
    assert "freshness" in meta, "Missing meta.freshness"
    assert "provenance" in meta, "Missing meta.provenance"
    assert "alerts" in meta, "Missing meta.alerts"
    assert isinstance(meta["alerts"], list), "meta.alerts must be a list"

    data = envelope["data"]
    assert "report_profile" in data, "Missing data.report_profile"
    # Story 1.4: get_ga4_report returns data.metrics (dict by metric name) — real mart query
    # data.rows was the Story 1.3 stub shape; AC5 shape uses data.metrics
    assert "metrics" in data or "rows" in data, (
        "Missing data.metrics (or legacy data.rows) in envelope"
    )


# ---------------------------------------------------------------------------
# T7.1 — health tool regression check (Story 1.1)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_health_tool_still_responds():
    """health tool still responds after module loading (regression check)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("health", {})

    assert not result.is_error, f"health tool returned error: {result}"
    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload
    assert data_section.get("status") == "ok", f"Expected status=ok, got: {data_section}"


# ---------------------------------------------------------------------------
# T7.2 — Negative path: broken module absent from list_modules; warning logged
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_broken_module_absent_from_list_modules(tmp_path, caplog):
    """Broken manifest → module absent from list_modules(); module_skipped logged."""
    from core.loader import scan_and_load_modules

    # Create a temporary modules dir with a broken module (missing 'name' field)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    broken_dir = modules_dir / "broken-module"
    broken_dir.mkdir()
    bad_manifest = {
        "schema_version": "1",
        # "name" is intentionally missing
        "display_name": "Broken Module",
        "auth_type": "none",
        "report_profiles": [
            {
                "id": "default",
                "display_name": "Default",
                "metrics": [],
                "dimensions": [],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            }
        ],
        "canonical_metric_mapping": {},
        "canonical_dimension_mapping": {},
        "widget_ref": "ui://broken/default",
    }
    (broken_dir / "manifest.json").write_text(
        json.dumps(bad_manifest), encoding="utf-8"
    )

    with caplog.at_level("ERROR"):
        loaded = scan_and_load_modules(modules_dir)

    # broken-module must be absent
    names = [m.name for m in loaded]
    assert "broken-module" not in names, f"broken-module should not be in: {names}"

    # module_skipped must be in the log
    assert "module_skipped" in caplog.text, "Expected module_skipped warning in log"


# ---------------------------------------------------------------------------
# T7.3 — AC5: no module-specific strings in server/core/ (programmatic grep)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# T15.8.A/B — Story 15.8 F-4 : decouverte et montage du module klaviyo
# AJOUT STRICTEMENT ADDITIF -- ne modifie pas les tests existants ci-dessus.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_includes_klaviyo():
    """T15.8.A — list_modules() retourne le module klaviyo (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload

    modules = data_section.get("modules", [])
    names = [m["name"] for m in modules]
    assert "klaviyo" in names, (
        f"klaviyo absent de list_modules -- module non decouvert par le loader core: {names}"
    )


@pytest.mark.anyio
async def test_klaviyo_tool_in_tool_list():
    """T15.8.B — get_klaviyo_report est discoverable dans la liste des outils (AI-56)."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    klaviyo_tools = [n for n in tool_names if "klaviyo" in n.lower()]
    assert klaviyo_tools, (
        f"Aucun outil klaviyo trouve dans les outils montes (AD-2 namespace mount): "
        f"{tool_names}"
    )
    assert any("get_klaviyo_report" in n for n in klaviyo_tools), (
        f"klaviyo_get_klaviyo_report absent des outils montes: {klaviyo_tools}"
    )


# ---------------------------------------------------------------------------
# Story 15.3 (F-4 linkedin-ads) -- additions STRICTEMENT ADDITIVES :
#   T15.3.A -- linkedin-ads present dans list_modules() (decouverte + montage).
#   T15.3.B -- linkedin-ads_get_linkedin_ads_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_includes_linkedin_ads():
    """T15.3.A -- list_modules() retourne le module linkedin-ads (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload

    modules = data_section.get("modules", [])
    names = [m["name"] for m in modules]
    assert "linkedin-ads" in names, (
        f"linkedin-ads absent de list_modules -- module non decouvert par le loader core: {names}"
    )


@pytest.mark.anyio
async def test_linkedin_ads_tool_in_tool_list():
    """T15.3.B -- get_linkedin_ads_report est discoverable dans la liste des outils (AI-56)."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    linkedin_tools = [n for n in tool_names if "linkedin" in n.lower()]
    assert linkedin_tools, (
        f"Aucun outil linkedin-ads trouve dans les outils montes (AD-2 namespace mount): "
        f"{tool_names}"
    )
    assert any("get_linkedin_ads_report" in n for n in linkedin_tools), (
        f"linkedin-ads_get_linkedin_ads_report absent des outils montes: {linkedin_tools}"
    )


# ---------------------------------------------------------------------------
# Story 15.7 (F-4 stripe) -- additions STRICTEMENT ADDITIVES :
#   T15.7.A -- stripe present dans list_modules() (decouverte + montage).
#   T15.7.B -- stripe_get_stripe_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_includes_stripe():
    """T15.7.A -- list_modules() retourne le module stripe (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload

    modules = data_section.get("modules", [])
    names = [m["name"] for m in modules]
    assert "stripe" in names, (
        f"stripe absent de list_modules -- module non decouvert par le loader core: {names}"
    )


@pytest.mark.anyio
async def test_stripe_tool_in_tool_list():
    """T15.7.B -- get_stripe_report est discoverable dans la liste des outils (AI-56)."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    stripe_tools = [n for n in tool_names if "stripe" in n.lower()]
    assert stripe_tools, (
        f"Aucun outil stripe trouve dans les outils montes (AD-2 namespace mount): "
        f"{tool_names}"
    )
    assert any("get_stripe_report" in n for n in stripe_tools), (
        f"stripe_get_stripe_report absent des outils montes: {stripe_tools}"
    )


# ---------------------------------------------------------------------------
# Story 15.5 (F-4 hubspot) -- additions STRICTEMENT ADDITIVES :
#   T15.5.A -- hubspot present dans list_modules() (decouverte + montage).
#   T15.5.B -- hubspot_get_hubspot_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_includes_hubspot():
    """T15.5.A -- list_modules() retourne le module hubspot (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload

    modules = data_section.get("modules", [])
    names = [m["name"] for m in modules]
    assert "hubspot" in names, (
        f"hubspot absent de list_modules -- module non decouvert par le loader core: {names}"
    )


@pytest.mark.anyio
async def test_hubspot_tool_in_tool_list():
    """T15.5.B -- get_hubspot_report est discoverable dans la liste des outils (AI-56)."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    hubspot_tools = [n for n in tool_names if "hubspot" in n.lower()]
    assert hubspot_tools, (
        f"Aucun outil hubspot trouve dans les outils montes (AD-2 namespace mount): "
        f"{tool_names}"
    )
    assert any("get_hubspot_report" in n for n in hubspot_tools), (
        f"hubspot_get_hubspot_report absent des outils montes: {hubspot_tools}"
    )


# ---------------------------------------------------------------------------
# Story 15.6 (F-4 google-sheets) -- additions STRICTEMENT ADDITIVES :
#   T15.6.A -- google-sheets present dans list_modules() (decouverte + montage).
#   T15.6.B -- google-sheets_get_google_sheets_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_modules_includes_google_sheets():
    """T15.6.A -- list_modules() retourne le module google-sheets (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    payload = result.structured_content or result.data
    if isinstance(payload, dict):
        data_section = payload.get("data", payload)
    else:
        data_section = payload

    modules = data_section.get("modules", [])
    names = [m["name"] for m in modules]
    assert "google-sheets" in names, (
        f"google-sheets absent de list_modules -- module non decouvert par le loader core: {names}"
    )


@pytest.mark.anyio
async def test_google_sheets_tool_in_tool_list():
    """T15.6.B -- get_google_sheets_report est discoverable dans la liste des outils (AI-56)."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = [t.name for t in tools]
    gsheets_tools = [
        n
        for n in tool_names
        if "google-sheets" in n.lower() or "google_sheets" in n.lower()
    ]
    assert gsheets_tools, (
        f"Aucun outil google-sheets trouve dans les outils montes (AD-2 namespace mount): "
        f"{tool_names}"
    )
    assert any("get_google_sheets_report" in n for n in gsheets_tools), (
        f"google-sheets_get_google_sheets_report absent des outils montes: {gsheets_tools}"
    )


# ---------------------------------------------------------------------------
# T7.3 — AC5: no module-specific strings in server/core/ (programmatic grep)
# ---------------------------------------------------------------------------

def test_core_is_source_agnostic():
    """server/core/ must contain no google-analytics, ga4, or google_analytics strings.

    This is the programmatic enforcement of AC5 / AD-2 source-agnostic invariant.
    The CI lint step (T8.2) runs the same check via shell grep.
    """
    core_dir = Path(__file__).parent.parent.parent / "core"
    forbidden_pattern = re.compile(
        r"google.analytics|ga4|google_analytics", re.IGNORECASE
    )

    violations: list[str] = []
    for py_file in core_dir.rglob("*.py"):
        # Exclude Google direct auth modules (AD-21 exceptions)
        if py_file.name in ("google_oauth.py", "google_token_store.py"):
            continue
        content = py_file.read_text(encoding="utf-8")
        for lineno, line in enumerate(content.splitlines(), start=1):
            if forbidden_pattern.search(line):
                # review-15-9 F-2 (dérogation AD-2 DOCUMENTÉE, approuvée — pas un
                # contournement). Ces cinq fichiers portent des chaînes 'ga4'/'google-
                # analytics' pour une raison légitime : les TYPES de source de vérification
                # de l'Epic 17 (shopify / stripe / ga4_purchase = domaine « source de
                # vérité », directive Jean 2026-07-18) et les colonnes du mart de
                # réconciliation (dedup_estimate / cross_source_*). Ce sont des VALEURS
                # de domaine métier (un type de source désigné par le projet), pas un
                # couplage à un module source — le core reste source-agnostic sur le
                # chemin d'exécution (loader / pull / dispatch nomment zéro module). La
                # dérogation est APPROUVÉE et cadrée à cet allowlist.
                #
                # PORTÉE DU PATTERN : le pattern interdit ne couvre QUE les chaînes GA4
                # (google.analytics | ga4 | google_analytics). Étendre le pattern aux
                # autres connecteurs (meta-ads, tiktok-ads, shopify, ...) pour durcir la
                # garantie AD-2 est une candidate RÉTRO d'epic (pas dans le scope 15.9).
                allowed = ("admin_api.py", "cards.py", "narrative.py", "queue.py", "rollup.py")
                if py_file.name in allowed:
                    continue
                violations.append(f"{py_file.relative_to(core_dir)}:{lineno}: {line.strip()}")

    assert violations == [], (
        "server/core/ contains module-specific strings (violates AC5 / AD-2):\n"
        + "\n".join(violations)
    )
