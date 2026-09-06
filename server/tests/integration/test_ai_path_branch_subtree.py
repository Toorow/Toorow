"""Story 55.2 -- the branch subtree reached the artifact, and acquired no capability.

WHY THIS FILE EXISTS, and why the assertions are on the BUILT bundle. Story 55.1
learned it the expensive way: a component exported from `viz/index.ts` but never
reached from the MCP App entry is tree-shaken out, and the widget ships without
the drawing while every TypeScript test stays green. The subtree is reached
through `renderers/aiPath.tsx`, which the entry imports -- so the only assertion
that sees what the host actually loads is one that reads `dist/viz/mcp-app.html`.

The second half is the boundary: a drill-down needs data, data needs a reader, and
a reader is a tool. This story adds NO tool -- the branches arrive on the Story
54.1 notification channel and the recording leaves through an HTTP route that
already existed. The catalog is booted through the real validator to prove it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from core.mcp_profiles import RENDER_TOOL_NAME, assert_data_render_split
from core.visualization_families import get_family
from core.visualization_runtime_resource import runtime_bundle_path
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

pytestmark = pytest.mark.anyio

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SUBTREE_TSX = (
    _REPO_ROOT / "ui" / "cards" / "shell" / "src" / "viz" / "renderers" / "aiPathBranches.tsx"
)
_FAMILY_TSX = (
    _REPO_ROOT / "ui" / "cards" / "shell" / "src" / "viz" / "renderers" / "aiPath.tsx"
)


# ---------------------------------------------------------------------------
# The declared capability.
# ---------------------------------------------------------------------------


def test_the_family_declares_the_subtree_and_its_recording():
    """A capability nobody can read from the server is one no evaluator can check."""
    family = get_family("ai_path")
    assert family is not None
    assert "branch_subtree" in family.capabilities
    assert "inspection_recorded" in family.capabilities
    # The third state stays declared: Story 55.1 put it there and 55.2 draws it.
    assert "unreached_node" in family.capabilities


def test_the_subtree_did_not_move_the_table_fallback_contract():
    """55.1 pins `AI_PATH_FALLBACK_COLUMNS` against this tuple. 55.2 adds a
    subtree, not a column -- a fifth well would have silently reddened that pin."""
    family = get_family("ai_path")
    assert family.table_fallback_wells == ("dimension", "label", "detail", "color")


# ---------------------------------------------------------------------------
# The boundary: no tool, no second resource.
# ---------------------------------------------------------------------------


async def test_the_subtree_added_no_reader_tool_to_the_catalog():
    """The drawing still acquires no reader. It is handed its data; it fetches none."""
    from core.main import mcp  # noqa: PLC0415

    async with Client(FastMCPTransport(mcp)) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert names, "the catalog must not be empty, or this proves nothing"
    for invented in (
        "ai_path_branches",
        "get_ai_path_branches",
        "app_read_ai_path_branches",
        "record_evidence_inspection",
    ):
        assert invented not in names, (
            f"{invented!r} appeared in the catalog. The subtree is handed its "
            "branches; a tool that fetches them would be a second read path."
        )


async def test_the_one_writer_that_was_added_is_app_only_and_reads():
    """`app_record_evidence_inspection` is now in the catalog, and that is decided.

    This test used to assert its ABSENCE, with the reason: "a new MCP tool here
    would be a decision to take at mcp_profiles.py". The decision was taken
    (migration 177): the app-only slot has required `effect="read"` /
    `confirmation_mode="none"` since Story 50.6, and `read` is honest because
    AD-24 effects classify DOMAIN state while AD-28 separately requires every read
    to leave an append-only audit row -- an inspection IS that row.

    So the guard is not dropped, it is made stronger: absence proved nobody had
    decided; this proves the tool exists under exactly the declaration that was
    decided, and it fails if somebody makes it model-visible or lets it mutate.
    """
    from core.main import mcp  # noqa: PLC0415
    from core.mcp_profiles import app_only_tool_names, registered_declarations  # noqa: PLC0415

    async with Client(FastMCPTransport(mcp)) as client:
        tools = list(await client.list_tools())
    wire_names = [tool.name for tool in tools]

    assert "app_record_evidence_inspection" in app_only_tool_names()

    decl = next(
        d for d in registered_declarations() if d.name == "app_record_evidence_inspection"
    )
    assert (decl.profile, decl.effect, decl.confirmation_mode) == (
        "insights",
        "read",
        "none",
    )

    # Only the named host-routing allowlist reaches discovery. This writer keeps
    # the same declaration and remains callable by stable name without broadening
    # the model-facing wire surface.
    assert wire_names.count("app_read_result_slice") == 1
    assert wire_names.count("app_record_evidence_inspection") == 0
    wire_sibling = next(tool for tool in tools if tool.name == "app_read_result_slice")
    sibling = await mcp._get_tool("app_read_result_slice")
    writer = await mcp._get_tool("app_record_evidence_inspection")
    assert sibling is not None and writer is not None
    assert writer.meta == sibling.meta, (
        "the app-only writer must carry the same declaration as the app-only readers"
    )
    assert wire_sibling.meta["ui"] == writer.meta["ui"]
    analyze = next(tool for tool in tools if tool.name == "analyze_result")
    assert analyze.meta.get("ui") is None, (
        "a model-callable tool must not acquire app visibility"
    )


def test_the_subtree_added_no_second_widget_binding():
    from core.main import mcp  # noqa: PLC0415

    bound = assert_data_render_split(mcp)
    assert set(bound) <= {RENDER_TOOL_NAME}
    assert len(bound) <= 1


def test_neither_the_subtree_nor_the_family_names_a_transport():
    """The drawing never acquires a reader. Recording is a callback handed in."""
    for source_file in (_SUBTREE_TSX, _FAMILY_TSX):
        source = source_file.read_text(encoding="utf-8")
        for forbidden in ("callServerTool", "ui://", "fetch(", "XMLHttpRequest", "apiFetch"):
            assert forbidden not in source, f"{source_file.name}: {forbidden}"


# ---------------------------------------------------------------------------
# The artifact the host actually loads.
# ---------------------------------------------------------------------------


def test_the_built_widget_carries_the_subtree_and_its_honest_states():
    path = runtime_bundle_path()
    if not path.exists():
        pytest.skip(
            "runtime bundle not built; run `node scripts/build-viz.mjs` in "
            "ui/cards/shell. A skip is not a pass: this is the only assertion "
            "that sees the artifact the host loads."
        )
    bundle = path.read_text(encoding="utf-8")
    # The subtree itself.
    assert "ai-path-branch-toggle" in bundle
    # AC3: the sentence that keeps a partial examination from reading as an
    # exhaustive one. It must survive minification, so it is asserted by its
    # literal rather than by a symbol name.
    assert "were not enumerated or judged" in bundle
    # AC1/AC3: the state that is NOT a zero.
    assert "branch count unknown" in bundle
    # And 55.1's markers are still there -- the subtree did not displace them.
    assert "ai-path-timeline" in bundle
    assert "No AI path" in bundle
