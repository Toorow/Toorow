"""Story 55.1 AC4 -- the widget carries the family, and the catalog did not move.

WHAT THIS FILE IS FOR. Adding a visual family is exactly the kind of change that
grows a second tool or a second widget binding without anyone deciding to: the
family needs data, the data needs a reader, and a reader is a tool. The catalog
invariant Story 50.6 installed (`mcp_profiles.RENDER_TOOL_NAME`,
`assert_data_render_split`) makes that a decision taken AT `mcp_profiles.py`
rather than acquired here -- so this file boots the assembled catalog through the
real validator and pins the binding count against the surviving binding, not
against a hand-written list of names.

The bundle half is checked against the BUILT artifact rather than against the
source graph. A family exported from `viz/index.ts` but never referenced by the
MCP App entry is tree-shaken out, and the widget would ship without the drawing
while every TypeScript test stayed green.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from core.main import mcp
from core.mcp_profiles import RENDER_TOOL_NAME, assert_data_render_split
from core.visualization_runtime_resource import (
    VISUALIZATION_RUNTIME_URI,
    runtime_bundle_path,
)
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

pytestmark = pytest.mark.anyio

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AI_PATH_TSX = _REPO_ROOT / "ui" / "cards" / "shell" / "src" / "viz" / "renderers" / "aiPath.tsx"
_MCP_APP_ENTRY = _REPO_ROOT / "ui" / "cards" / "shell" / "src" / "viz" / "entries" / "mcpApp.tsx"


# ---------------------------------------------------------------------------
# The catalog invariant: booted, not restated.
# ---------------------------------------------------------------------------


def test_the_assembled_catalog_still_passes_the_data_render_split():
    """It raises `CatalogValidationError` on a violation, so calling it IS the test."""
    bound = assert_data_render_split(mcp)
    assert set(bound) <= {RENDER_TOOL_NAME}


def test_the_family_added_no_second_widget_binding():
    bound = assert_data_render_split(mcp)
    # One binding when the runtime bundle is built, zero when it is not. Both are
    # legitimate; TWO never is, and that is the number this pins.
    assert bound in ({}, {RENDER_TOOL_NAME: VISUALIZATION_RUNTIME_URI})


async def test_the_family_added_no_tool_to_the_catalog():
    """A visual family is a drawing, not a capability. It reads nothing of its own."""
    async with Client(FastMCPTransport(mcp)) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names, "the catalog must not be empty, or this proves nothing"
    # `get_ai_path` IS the decision taken at mcp_profiles.py (2026-09-05,
    # mcp-tool-surface.md): the READING door of the evaluation profile. The family
    # (a drawing) still adds nothing of its own to the catalog.
    for invented in ("ai_path", "app_read_ai_path", "render_ai_path"):
        assert invented not in names, (
            f"{invented!r} appeared in the catalog. A reader for the walk is a "
            "decision to take at mcp_profiles.py, not one to acquire from a story "
            "about a drawing."
        )
    assert "get_ai_path" in names, "the reading door of the evaluation profile is missing from the catalog"


async def test_exactly_one_resource_uri_is_advertised_by_any_tool():
    async with Client(FastMCPTransport(mcp)) as client:
        tools = await client.list_tools()
    uris = {
        ((tool.meta or {}).get("ui") or {}).get("resourceUri")
        for tool in tools
        if ((tool.meta or {}).get("ui") or {}).get("resourceUri")
    }
    assert uris <= {VISUALIZATION_RUNTIME_URI}


# ---------------------------------------------------------------------------
# The bundle half.
# ---------------------------------------------------------------------------


def test_the_mcp_app_entry_references_the_family_or_it_is_tree_shaken_away():
    """The family must be REACHABLE from the entry -- not literally in it.

    The guarantee is the bundler's: an export nobody imports is dropped, and the
    widget then ships without the drawing. That is unchanged. What changed is the
    shape of the reach: the entry imports `aiPathCapability`, which imports
    `./renderers/aiPath`. A check that demanded the literal path in ONE file
    failed on an indirection that keeps the module perfectly reachable -- a false
    alarm about a real rule, which is how a gate stops being believed.

    So the reach is followed, one local hop, and the assertion still fails the day
    nothing imports the family at all.
    """
    import re

    source = _MCP_APP_ENTRY.read_text(encoding="utf-8")
    reachable = [source]
    for specifier in re.findall(r'from\s+"(\.[^"]+)"', source):
        for suffix in (".tsx", ".ts", "/index.tsx", "/index.ts"):
            candidate = (_MCP_APP_ENTRY.parent / (specifier + suffix)).resolve()
            if candidate.exists():
                reachable.append(candidate.read_text(encoding="utf-8"))
                break

    assert any("renderers/aiPath" in text for text in reachable), (
        "nothing the MCP App entry imports references the family; an export nobody "
        "imports is dropped by the bundler and the widget ships without the drawing"
    )
    assert any("AiPathFamily" in text for text in reachable)


def test_the_built_widget_resource_carries_the_family():
    path = runtime_bundle_path()
    if not path.exists():
        pytest.skip(
            "runtime bundle not built; run `pnpm --filter @toorow/card-shell build`. "
            "A skip is not a pass: this assertion is the only one that sees the "
            "artifact the host actually loads."
        )
    bundle = path.read_text(encoding="utf-8")
    # The literal `analyze-and-test.md:113` requires, and a marker only this
    # family emits. Both, so a coincidental substring cannot carry the test.
    assert "No AI path" in bundle
    assert "ai-path-timeline" in bundle


def test_the_family_source_never_names_a_second_tool_or_resource():
    source = _AI_PATH_TSX.read_text(encoding="utf-8")
    for forbidden in ("callServerTool", "ui://", "fetch(", "XMLHttpRequest"):
        assert forbidden not in source, forbidden


def test_the_family_did_not_add_a_module_that_names_the_widget_uri():
    """Two owners -- the resource module, and the module that documents it. A
    THIRD would mean this story acquired a second binding site by hand.

    The second owner MOVED on 2026-08-14 and the count did not: `c6208933` took
    the eleven `ui://core/*` resources out of the entrypoint, so `main.py`'s boot
    comment is now `widget_resources.py`'s. Both of its occurrences are prose --
    one says the resource is "registered by `core.main`", the other is help text
    -- so neither is a registration. What this guard refuses is a THIRD place
    that names the URI, and there are still two.
    """
    core = _REPO_ROOT / "server" / "core"
    literal = "ui://core/visualization-runtime"
    owners = sorted(
        p.name for p in core.glob("*.py") if literal in p.read_text(encoding="utf-8")
    )
    assert owners == ["visualization_runtime_resource.py", "widget_resources.py"], owners


def test_the_environment_override_is_honoured_so_a_deployment_can_build_elsewhere():
    """Guards the skip above from becoming permanent on a machine that builds out of tree."""
    previous = os.environ.get("TOOROW_VISUALIZATION_RUNTIME_DIST")
    os.environ["TOOROW_VISUALIZATION_RUNTIME_DIST"] = "/tmp/example/mcp-app.html"
    try:
        assert runtime_bundle_path() == Path("/tmp/example/mcp-app.html")
    finally:
        if previous is None:
            del os.environ["TOOROW_VISUALIZATION_RUNTIME_DIST"]
        else:
            os.environ["TOOROW_VISUALIZATION_RUNTIME_DIST"] = previous
