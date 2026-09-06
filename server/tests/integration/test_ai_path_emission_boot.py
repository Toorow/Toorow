"""Stories 54.1/65.6 -- live progress does not move the boot invariant.

`assert_data_render_split` (Story 50.6) REFUSES to start the server if any tool
other than `render_analyze_result` advertises a widget resource. This story
advertises no resource and registers no tool: it grafts an emission onto a
middleware that was already mounted. That claim is worth nothing unless the
assembled catalog is actually booted, so this file boots it.

It also pins AC1 at the live end: the mounted middleware chain must carry exactly
ONE middleware for the AI Path concern. A second one would open a second path per
interaction, and the count is the only thing that can say so.
"""

from __future__ import annotations

import pytest
from core.ai_path_recorder import build_middleware
from core.main import mcp
from core.mcp_profiles import assert_data_render_split
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

pytestmark = pytest.mark.anyio


def test_the_assembled_catalog_still_boots() -> None:
    """AC10 -- the boot invariant still lets the server start."""
    bound = assert_data_render_split(mcp)
    assert set(bound) <= {"render_analyze_result"}


def test_the_ai_path_concern_is_carried_by_exactly_one_mounted_middleware() -> None:
    """AC1, against the LIVE chain rather than the source that builds it."""
    mounted = [
        middleware
        for middleware in getattr(mcp, "middleware", [])
        if type(middleware).__name__ == "AiPathMiddleware"
    ]
    assert len(mounted) == 1, [type(m).__name__ for m in getattr(mcp, "middleware", [])]
    # `build_middleware` defines its class inside the function, so identity would
    # never hold; what must hold is that the mounted one comes from THIS module.
    assert type(mounted[0]).__qualname__ == type(build_middleware()).__qualname__
    assert type(mounted[0]).__module__ == "core.ai_path_recorder"


async def test_the_emission_advertises_no_tool_and_the_reading_door_is_the_only_ai_path_tool() -> None:
    """A middleware is not a capability: the emission adds nothing to `tools/list`. The one
    `ai_path` tool there, `get_ai_path`, is the READING door of the evaluation profile
    (2026-09-05, mcp-tool-surface.md), registered by that profile and not by the emission."""
    async with Client(FastMCPTransport(mcp)) as client:
        names = {tool.name for tool in await client.list_tools()}
    assert "execute_analyze_query_spec" in names
    # The emission itself advertises nothing; `get_ai_path` is the separate READING
    # door of the evaluation profile (2026-09-05, mcp-tool-surface.md), not an emission.
    assert not {name for name in names if ("ai_path" in name and name != "get_ai_path") or "progress" in name}
