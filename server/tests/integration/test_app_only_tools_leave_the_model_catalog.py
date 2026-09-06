"""Only the reviewed app-only host-routing seam reaches the MCP wire.

All five app tools stay registered and callable by stable name. Exactly two
must also survive ``Client.list_tools()`` so a host can discover their routing
metadata; generated model Skills still exclude the complete app-only set.
"""

from __future__ import annotations

from collections import Counter

import pytest
from core.main import mcp
from core.mcp_profiles import (
    DEFAULT_PROFILE,
    HOST_ROUTED_APP_ONLY_TOOLS,
    is_app_only_tool,
    model_visible_tools,
)
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

pytestmark = pytest.mark.anyio

#: The current five are spelled out on purpose. The registry-driven assertions
#: also catch a sixth app-only tool added without updating this contract.
_ALL_APP_ONLY = frozenset(
    {
        "app_read_result_manifest",
        "app_read_result_slice",
        "app_record_evidence_inspection",
        "submit_analyze_feedback",
        "submit_feedback",
    }
)


async def _wire_catalog() -> list[object]:
    async with Client(FastMCPTransport(mcp)) as client:
        return list(await client.list_tools())


async def _assembled() -> list[object]:
    return list(await mcp._list_tools())


async def test_the_five_are_registered_and_callable_by_the_app():
    """The app view keeps them registered and callable.

    A widget calls `app_read_result_slice` by a name it holds as a constant
    (`ui/cards/shell/src/viz/entries/resultSliceDelivery.ts`), never by a name it
    discovered. If this assertion ever fails, the App surface is broken.
    """
    assembled = await _assembled()
    names = [tool.name for tool in assembled]
    for name in sorted(_ALL_APP_ONLY):
        assert names.count(name) == 1, f"{name} must be registered exactly once"
        assert await mcp._get_tool(name) is not None, f"{name} must stay callable"


async def test_only_the_named_host_routing_allowlist_reaches_the_wire_catalog():
    """Raw-list proof: no dict comprehension may hide duplicate declarations."""
    wire = await _wire_catalog()
    wire_names = [tool.name for tool in wire]
    app_only_names = [tool.name for tool in wire if is_app_only_tool(tool)]

    assert HOST_ROUTED_APP_ONLY_TOOLS == frozenset(
        {"app_read_result_slice", "submit_feedback"}
    )
    assert Counter(app_only_names) == Counter(HOST_ROUTED_APP_ONLY_TOOLS)
    for name in HOST_ROUTED_APP_ONLY_TOOLS:
        assert wire_names.count(name) == 1, f"{name} must reach tools/list exactly once"
    for name in _ALL_APP_ONLY - HOST_ROUTED_APP_ONLY_TOOLS:
        assert wire_names.count(name) == 0
        assert await mcp._get_tool(name) is not None, f"{name} stays callable by name"
    # NOT asserted here: `app_only_tool_names()`. It is process-global registration
    # state that a sibling suite's fixture clears, and the wire declaration read
    # above is the source of truth anyway -- `test_mcp_data_render_split.py` owns
    # the registry-side assertion.


async def test_the_wire_catalog_applies_only_profile_and_presence_filters():
    """App visibility is metadata; profile and confirmation remain server gates."""
    assembled = await _assembled()
    wire = await _wire_catalog()
    app_only = {tool.name for tool in assembled if is_app_only_tool(tool)}
    assert app_only == _ALL_APP_ONLY, sorted(app_only)
    expected = model_visible_tools(
        assembled,
        allowed=frozenset({DEFAULT_PROFILE}),
        interactive=False,
    )
    assert Counter(t.name for t in expected) == Counter(t.name for t in wire)


async def test_an_analytical_tool_is_untouched_by_the_filter():
    """The data tool is an analytical capability and must stay in the catalog."""
    wire = await _wire_catalog()
    names = [tool.name for tool in wire]
    assert "analyze_result" in names
    assert "execute_analyze_query_spec" in names


def test_the_filter_keeps_only_the_named_app_visibility_metadata_on_the_wire():
    """Unit-level: the same call the middleware and the report script both make."""

    class _Tool:
        def __init__(self, name, meta):
            self.name = name
            self.meta = meta
            self.tags = set()

    insights = {"profile": "insights", "effect": "read"}
    plain = _Tool("get_something", dict(insights))
    routed = _Tool(
        "app_read_result_slice", {**insights, "ui": {"visibility": ["app"]}}
    )
    hidden = _Tool(
        "app_read_result_manifest", {**insights, "ui": {"visibility": ["app"]}}
    )
    both = _Tool(
        "hybrid", {**insights, "ui": {"visibility": ["app", "model"]}}
    )
    kept = model_visible_tools(
        [plain, routed, hidden, both],
        allowed=frozenset({DEFAULT_PROFILE}),
        interactive=False,
    )
    # `["app", "model"]` is NOT app-only: the declaration says both channels.
    assert [t.name for t in kept] == ["get_something", "app_read_result_slice", "hybrid"]
