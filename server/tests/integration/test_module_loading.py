"""Integration tests for module auto-discovery and namespaced mount (Story 1.3 — T7).

Uses FastMCPTransport (in-process, no network) to call tools on the live
``mcp`` server instance from core.main.

Covers:
  T7.1 — list_connectors() returns google-analytics + standard_daily profile.
  T7.1 — health tool still responds (Story 1.1 regression check).
  T7.2 — Broken module absent from list_connectors(); module_skipped logged.
  T7.3 — core/ contains no module-specific strings (AC5 / AD-2).

Story 15.8 / 15.3 / 15.5 / 15.6 / 15.7 (F-4) : chaque connecteur reste present
dans `list_connectors()` — c'est la preuve de DECOUVERTE, et elle tient.

AD-42 (2026-08-12) a retire l'autre moitie. Six cas asseraient
« <connecteur>_get_<connecteur>_report est discoverable » : c'etait la mesure du
defaut, pas de la capacite. Le montage sous espace de noms publiait un outil par
connecteur dans un catalogue borne par rien. Un seul cas les remplace,
`test_no_connector_reaches_the_tool_list`, derive du registre de modules charge
— donc vrai pour le quarantieme connecteur sans qu'on y pense.
"""

from __future__ import annotations

import ast
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
from core.model_channel import APP_PAYLOAD_META_KEY, WITHHELD_MARKER
from fastmcp.client import Client, FastMCPTransport

# ---------------------------------------------------------------------------
# Reading the connector list -- TWO deliberate product changes are folded in here.
#
# 1. The tool is `list_connectors`, not `list_modules`. Connector is the
#    canonical noun (docs/product-architecture/glossary.md; Module/Extension
#    were retired, Tool reserved for MCP), and the payload key moved from
#    `modules` to `connectors` with it. These tests kept the retired name in
#    their own names, which is the part that actually misleads a reader.
#
# 2. Since Story 50.6 the list does not ride `structuredContent` at all.
#    `core.model_channel.partition_envelope` routes any payload over
#    MODEL_CHANNEL_MAX_BYTES (4 KiB) to the APP channel and leaves a stated
#    descriptor -- {"withheld": "moved_to_app_channel", "bytes": N} -- in its
#    place. Nothing is discarded. Reading the app channel is therefore a
#    STRONGER assertion than the old direct read: it proves the channel split
#    preserved the whole list rather than trimming it.
# ---------------------------------------------------------------------------

def _connector_names(result) -> list[str]:
    """Names of every connector the tool returned, whichever channel carries it."""
    return [c["name"] for c in _connectors(result)]


def _connectors(result) -> list[dict]:
    payload = result.structured_content or result.data
    data_section = payload.get("data", payload) if isinstance(payload, dict) else payload
    listed = data_section.get("connectors")

    if isinstance(listed, dict) and listed.get("withheld") == WITHHELD_MARKER:
        app_payload = (result.meta or {}).get(APP_PAYLOAD_META_KEY) or {}
        moved = app_payload.get("connectors")
        assert isinstance(moved, list), (
            "list_connectors withheld its payload but the app channel does not "
            f"carry it: _meta[{APP_PAYLOAD_META_KEY!r}] = {app_payload!r}"
        )
        # The descriptor must not lie about what was moved (AD-1: a trimmed list
        # that still reads as complete is the failure mode the budget prevents).
        assert data_section.get("count") == len(moved), (
            f"descriptor says count={data_section.get('count')} but the app "
            f"channel carries {len(moved)} connectors"
        )
        return moved

    assert isinstance(listed, list), f"unexpected list_connectors payload: {data_section!r}"
    return listed


# ---------------------------------------------------------------------------
# T7.1 — list_modules returns google-analytics with standard_daily profile
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_connectors_includes_google_analytics():
    """list_connectors() tool returns google-analytics with report profiles."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    assert result.data is not None or result.structured_content is not None
    connectors = _connectors(result)
    names = [c["name"] for c in connectors]
    assert "google-analytics" in names, f"google-analytics not in modules: {names}"

    ga4 = next(c for c in connectors if c["name"] == "google-analytics")
    profile_ids = [p["id"] for p in ga4.get("report_profiles", [])]
    assert "standard_daily" in profile_ids, f"standard_daily not in profiles: {profile_ids}"


# ---------------------------------------------------------------------------
# AD-42 — NO connector reaches the tool list, and that is the assertion now.
#
# Six tests in this file asserted the opposite, one per connector: "<connector>_
# get_<connector>_report is discoverable via list_tools". They were right about
# the mechanism (FastMCP's namespace transform) and they encoded the defect: the
# catalog grew one entry per connector, and the connector catalogue is bounded by
# nothing. On 2026-08-12 they were 39 of the 92 tools a default host received.
#
# Six per-connector cases are also the wrong shape for this property. What must
# hold is a property of the WHOLE catalog against the WHOLE module registry, so
# the fortieth connector is covered without a seventh test being remembered.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_no_connector_reaches_the_tool_list():
    """No tool name is derived from a connector name — measured, not listed."""
    from core.main import _loaded_modules

    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    connectors = sorted(loaded.name for loaded in _loaded_modules)
    assert connectors, "no connector loaded — this test would prove nothing"

    offenders = sorted(
        tool.name
        for tool in tools
        if any(
            tool.name.startswith(f"{connector}_") or f"_{connector}_" in tool.name
            for connector in connectors
        )
    )
    assert offenders == [], (
        f"these MCP tools carry a provider's name: {offenders}. What a project "
        "collects is exposed through its Datastreams (list_datastreams / "
        "get_datastream_report), never one tool per connector — AD-42."
    )


# ---------------------------------------------------------------------------
# AD-42 — the connectors are still DISCOVERED; only their tools are gone.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_the_replacement_pair_answers_what_the_project_collects():
    """Removing 39 tools is only half: something must answer in their place."""
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()

    tool_names = {tool.name for tool in tools}
    assert "list_datastreams" in tool_names
    assert "get_datastream_report" in tool_names


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
# T7.2 — Negative path: broken module absent from list_connectors; warning logged
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_broken_module_absent_from_list_connectors(tmp_path, caplog):
    """Broken manifest → module absent from list_connectors(); module_skipped logged."""
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
async def test_list_connectors_includes_klaviyo():
    """T15.8.A — list_connectors() retourne le module klaviyo (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    names = _connector_names(result)
    assert "klaviyo" in names, (
        f"klaviyo absent de list_connectors -- module non decouvert par le loader core: {names}"
    )


# T15.8.B retired by AD-42: see `test_no_connector_reaches_the_tool_list`, which
# asserts the property for every loaded connector instead of this one.


# ---------------------------------------------------------------------------
# Story 15.3 (F-4 linkedin-ads) -- additions STRICTEMENT ADDITIVES :
#   T15.3.A -- linkedin-ads present dans list_connectors() (decouverte + montage).
#   T15.3.B -- linkedin-ads_get_linkedin_ads_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_connectors_includes_linkedin_ads():
    """T15.3.A -- list_connectors() retourne le module linkedin-ads (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    names = _connector_names(result)
    assert "linkedin-ads" in names, (
        f"linkedin-ads absent de list_connectors -- "
        f"connector non decouvert par le loader core: {names}"
    )


# T15.3.B retired by AD-42 -- see `test_no_connector_reaches_the_tool_list`.


# ---------------------------------------------------------------------------
# Story 15.7 (F-4 stripe) -- additions STRICTEMENT ADDITIVES :
#   T15.7.A -- stripe present dans list_connectors() (decouverte + montage).
#   T15.7.B -- stripe_get_stripe_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_connectors_includes_stripe():
    """T15.7.A -- list_connectors() retourne le module stripe (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    names = _connector_names(result)
    assert "stripe" in names, (
        f"stripe absent de list_connectors -- module non decouvert par le loader core: {names}"
    )


# T15.7.B retired by AD-42 -- see `test_no_connector_reaches_the_tool_list`.


# ---------------------------------------------------------------------------
# Story 15.5 (F-4 hubspot) -- additions STRICTEMENT ADDITIVES :
#   T15.5.A -- hubspot present dans list_connectors() (decouverte + montage).
#   T15.5.B -- hubspot_get_hubspot_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_connectors_includes_hubspot():
    """T15.5.A -- list_connectors() retourne le module hubspot (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    names = _connector_names(result)
    assert "hubspot" in names, (
        f"hubspot absent de list_connectors -- module non decouvert par le loader core: {names}"
    )


# T15.5.B retired by AD-42 -- see `test_no_connector_reaches_the_tool_list`.


# ---------------------------------------------------------------------------
# Story 15.6 (F-4 google-sheets) -- additions STRICTEMENT ADDITIVES :
#   T15.6.A -- google-sheets present dans list_connectors() (decouverte + montage).
#   T15.6.B -- google-sheets_get_google_sheets_report discoverable.
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_list_connectors_includes_google_sheets():
    """T15.6.A -- list_connectors() retourne le module google-sheets (decouverte + montage)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_connectors", {})

    names = _connector_names(result)
    assert "google-sheets" in names, (
        f"google-sheets absent de list_connectors -- "
        f"connector non decouvert par le loader core: {names}"
    )


# T15.6.B retired by AD-42 -- see `test_no_connector_reaches_the_tool_list`.


# ---------------------------------------------------------------------------
# T7.3 — AC5: no module-specific strings in server/core/ (programmatic grep)
# ---------------------------------------------------------------------------

def _declares_ad21_carrier(source: str) -> bool:
    """True when the module's own docstring cites AD-21.

    AD-21 (`_bmad-output/specs/spec-toorow/SPEC.md:153`, ratified 2026-07-18) is
    a bounded exception to AD-3: ONE server-side consent screen carries every
    Google scope. The scope->connector map that implements it cannot be written
    without naming Google connectors -- naming them there is the decision, not a
    breach of it. Requiring the citation in the docstring keeps the exception
    self-declaring: a new carrier says AD-21 in its own file or it is reported.
    """
    try:
        return "AD-21" in (ast.get_docstring(ast.parse(source)) or "")
    except SyntaxError:  # pragma: no cover - a core file that will not parse
        return False


def _prose_line_numbers(source: str) -> set[int]:
    """Line numbers carrying only a comment, or belonging to a docstring.

    Neither can couple `core` to a source module, so neither is an AD-2 breach.
    Docstrings are found via `ast` (module, class and function alike) rather than
    by guessing at quotes, so a string literal that is REAL code -- the values of
    `GOOGLE_SCOPE_CONNECTORS`, say -- is never mistaken for prose.
    """
    prose: set[int] = set()
    for lineno, raw in enumerate(source.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            prose.add(lineno)
    try:
        tree = ast.parse(source)
    except SyntaxError:  # pragma: no cover - a core file that will not parse
        return prose
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            if isinstance(first.value.value, str):
                prose.update(range(first.lineno, (first.end_lineno or first.lineno) + 1))
    return prose


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
        content = py_file.read_text(encoding="utf-8")
        # A module that DECLARES itself an AD-21 carrier in its own docstring is
        # exempt. This used to be the hardcoded pair ("google_oauth.py",
        # "google_token_store.py") -- both of which do cite AD-21 in their
        # docstring, so nothing is newly excused; the criterion simply moved from
        # a list this test maintains to a statement the module makes about
        # itself. `connection_tools.py` (the scope->connector map, extracted
        # after that list was written) cites AD-21 on its line 7 and is covered
        # by the same rule instead of by a sixth hardcoded filename.
        if _declares_ad21_carrier(content):
            continue
        lines = content.splitlines()
        prose_lines = _prose_line_numbers(content)
        for lineno, line in enumerate(lines, start=1):
            if forbidden_pattern.search(line):
                # AD-2 binds the EXECUTION PATH -- "le core reste source-agnostic
                # sur le chemin d'execution (loader / pull / dispatch nomment zero
                # module)", as this test's own derogation note says below. A
                # comment or a docstring cannot couple anything to a source; a
                # pasted DuckDB error message and a docstring naming an example
                # connector were being reported as boundary breaches. Prose is
                # excluded so the guard reports couplings and only couplings.
                if lineno in prose_lines:
                    continue

                # AD-21 is a RATIFIED, bounded exception to AD-3 (SPEC.md:153,
                # directive Jean 2026-07-18): ONE server-side consent screen
                # carries every Google scope. Naming the Google connectors is not
                # a leak in that machinery -- it IS the machinery, and the
                # scope->connector map cannot be written source-agnostically.
                #
                # Anchored on the CITATION, not on the filename. The filename
                # allowlist below exempts five files WHOLE; this rule exempts one
                # declaration and forces whoever writes the next one to put the
                # ratified decision number next to it. That is why
                # `connection_tools.GOOGLE_SCOPE_CONNECTORS` and
                # `source_delegation.GOOGLE_DIRECT_PROVIDERS` -- both extracted
                # after the filename allowlist was written, both already citing
                # AD-21 in their own comments -- pass without widening the list.
                if any("AD-21" in prior for prior in lines[max(0, lineno - 16) : lineno]):
                    continue

                # review-15-9 F-2 (dérogation AD-2 DOCUMENTÉE, approuvée — pas un
                # contournement). Ces cinq fichiers portent des chaînes 'ga4'/'google-
                # analytics' pour une raison légitime : les TYPES de source de verification
                # de l'Epic 17 (shopify / stripe / ga4_purchase = domaine « source de
                # vérité », directive Jean 2026-07-18) et les colonnes du mart de
                # reconciliation (dedup_estimate / cross_source_*). Ce sont des VALEURS
                # de domaine métier (un type de source désigné par le projet), pas un
                # couplage à un module source — le core reste source-agnostic sur le
                # chemin d'exécution (loader / pull / dispatch nomment zéro module). La
                # dérogation est APPROUVÉE et cadrée à cet allowlist.
                #
                # PORTÉE DU PATTERN : le pattern interdit ne couvre QUE les chaînes GA4
                # (google.analytics | ga4 | google_analytics). Étendre le pattern aux
                # autres connecteurs (meta-ads, tiktok-ads, shopify, ...) pour durcir la
                # garantie AD-2 est une candidate RÉTRO d'epic (pas dans le scope 15.9).
                allowed = (
                    "admin_api.py",
                    "projects_api.py",
                    "cards.py",
                    "narrative.py",
                    "queue.py",
                    "rollup.py",
                )
                if py_file.name in allowed:
                    continue
                violations.append(f"{py_file.relative_to(core_dir)}:{lineno}: {line.strip()}")

    assert violations == [], (
        "server/core/ contains module-specific strings (violates AC5 / AD-2):\n"
        + "\n".join(violations)
    )
