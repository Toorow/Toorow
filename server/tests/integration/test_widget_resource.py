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
_WIDGET_DIST = _REPO_ROOT / "ui" / "widgets" / "google-analytics" / "dist" / "index.html"


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
async def test_meta_resource_uri_matches_widget_resource():
    """get_daily_report._meta.ui.resourceUri must equal the registered widget URI (AC8)."""
    with patch("core.main.warehouse.query_daily_report", return_value=[]):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"date_range": {"start": "2026-01-01", "end": "2026-01-31"}},
            )
    meta = getattr(result, "meta", None) or getattr(result, "_meta", None)
    assert meta is not None
    assert meta.get("ui", {}).get("resourceUri") == DAILY_REPORT_WIDGET_URI
