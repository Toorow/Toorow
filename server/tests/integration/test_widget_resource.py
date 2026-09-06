"""Integration test — daily-report widget FastMCP resource (Story 1.6, T10.2 / AC8).

Verifies that:
  1. The core registers a resource at ``ui://core/daily-report``.
  2. Reading it returns an HTML string.
  3. When the widget dist exists, the served HTML is the built bundle
     (contains a <div id="root"> mount point); otherwise a graceful
     "not built" placeholder is served (no crash).
  4. get_daily_report's _meta.ui.resourceUri points to the SAME URI (consistency).

Uses FastMCPTransport (in-process, no network) against the live ``mcp`` instance.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from core.main import DAILY_REPORT_WIDGET_URI, mcp
from fastmcp.client import Client, FastMCPTransport

_REPO_ROOT = Path(__file__).parent.parent.parent.parent
# Story 50.6 -- track the RESOLVER, not a connector. This pointed at
# `ui/widgets/google-analytics/dist/index.html`, which Story 50.5 deliberately
# stopped serving when it deleted the connector scan that let whichever
# connector loaded first decide how the standard report looked. The test then
# passed or failed depending on whether that one connector happened to be built
# locally -- an environment coin-flip, not a contract. Asking `core.main` which
# path it actually resolves makes the branch below follow the code.
from core.main import _WIDGET_PATH as _WIDGET_DIST  # noqa: E402


@pytest.mark.anyio
async def test_daily_report_widget_resource_registered():
    """The ui://core/daily-report resource is registered and lists in the catalog."""
    async with Client(FastMCPTransport(mcp)) as client:
        resources = await client.list_resources()
    uris = {str(r.uri) for r in resources}
    assert DAILY_REPORT_WIDGET_URI in uris, (
        f"Expected {DAILY_REPORT_WIDGET_URI} in resource catalog, got: {uris}"
    )


@pytest.mark.anyio
async def test_daily_report_widget_resource_serves_html():
    """Reading the resource returns HTML — the built bundle if present, else a placeholder."""
    async with Client(FastMCPTransport(mcp)) as client:
        contents = await client.read_resource(DAILY_REPORT_WIDGET_URI)

    assert contents, "read_resource returned no content"
    text = getattr(contents[0], "text", None)
    assert isinstance(text, str) and text, "resource content must be a non-empty HTML string"
    assert "<html" in text.lower() or "<!doctype" in text.lower(), (
        "resource content must be HTML"
    )

    if _WIDGET_DIST.exists():
        # Built bundle — the widget mounts into #root and is self-contained.
        assert 'id="root"' in text, "built widget HTML must contain the #root mount point"
    else:
        # Graceful placeholder when not built yet (server must not crash).
        assert "not built" in text.lower()


@pytest.mark.anyio
async def test_a_data_tool_no_longer_advertises_the_widget_resource():
    """Story 50.6 -- INVERTED, deliberately, so the removal leaves a trace.

    Until Story 50.6 this asserted `get_daily_report._meta.ui.resourceUri ==
    DAILY_REPORT_WIDGET_URI` (Story 1.6 AC8). `visualization-and-rendering.md`
    ("Tool split") retires that: a DATA tool does not attach a widget resource,
    only the render tool advertises one, and
    `core.mcp_profiles.assert_data_render_split` aborts boot on a violation.

    The assertion is inverted rather than deleted so a future reader meets the
    decision instead of an absence, and cannot silently restore the binding.
    The RESOURCE registration above stays: a resource no data tool advertises is
    inert, and deleting it would destroy the inventory of what Story 50.5
    replaces.
    """
    with patch("core.main.warehouse.query_daily_report", return_value=[]):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"date_range": {"start": "2026-01-01", "end": "2026-01-31"}},
            )
    meta = getattr(result, "meta", None) or getattr(result, "_meta", None)
    assert (meta or {}).get("ui") is None, (
        f"a data tool must not advertise a widget resource; got {meta!r} "
        f"(the retired binding was {DAILY_REPORT_WIDGET_URI})"
    )
