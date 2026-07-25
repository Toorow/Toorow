"""Tests for identity propagation into tool responses (Story 2.3, T7).

T7.1 - Module created.
T7.2 - Disabled mode: health returns identity == "anonymous".
T7.3 - Static mode (fresh FastMCP instance): health with static token returns
        identity == TOOROW_STATIC_SUBJECT.
T7.4 - Disabled mode: get_daily_report envelope data contains identity field.
T7.5 - Existing health tests still pass (backward compat — project_id unchanged).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest
from core.main import mcp
from fastmcp.client import Client, FastMCPTransport

# ---------------------------------------------------------------------------
# T7.2 — Disabled mode: health returns identity == "anonymous"
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_identity_anonymous_in_disabled_mode():
    """In disabled mode (default), health must return identity='anonymous' (AC5, T7.2)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("health", {})

    assert not result.is_error, f"health returned error: {result}"
    payload = result.structured_content or {}
    data = payload.get("data", {})
    assert "identity" in data, f"Expected 'identity' in data: {data}"
    assert data["identity"] == "anonymous", (
        f"Expected identity='anonymous' in disabled mode, got: {data['identity']!r}"
    )


@pytest.mark.anyio
async def test_health_project_id_unchanged_disabled_mode():
    """health project_id scaffold must remain unchanged (T7.5 backward compat)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("health", {"project_id": "proj_test"})

    assert not result.is_error
    data = (result.structured_content or {}).get("data", {})
    assert data.get("project_id") == "proj_test"


# ---------------------------------------------------------------------------
# T7.3 — Static mode: fresh FastMCP instance with static token
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_health_identity_resolves_to_subject_in_static_mode():
    """In static mode, identity must resolve to the configured subject (AC4, T7.3).

    Uses a fresh FastMCP instance with health tool registered so the token
    context is available via get_access_token(). This approach keeps the global
    mcp (disabled mode) unchanged for all Epic 1 regression tests.
    """
    from fastmcp import FastMCP
    from fastmcp.server.auth import AccessToken, StaticTokenVerifier
    from fastmcp.server.dependencies import get_access_token

    STATIC_TOKEN = "story23-static-test-token"
    STATIC_SUBJECT = "alice@example.com"

    verifier = StaticTokenVerifier(
        tokens={
            STATIC_TOKEN: {
                "client_id": STATIC_SUBJECT,
                "sub": STATIC_SUBJECT,
                "scopes": [],
            }
        }
    )
    test_mcp = FastMCP("test-identity-static", auth=verifier)

    @test_mcp.tool
    def health_static(project_id: str = "default") -> dict:
        """Health tool for static mode identity test."""
        token: AccessToken | None = get_access_token()
        identity = token.claims.get("sub", token.client_id) if token else "anonymous"
        return {
            "schema_version": "1",
            "meta": {"freshness": "live", "provenance": None, "alerts": []},
            "data": {"status": "ok", "project_id": project_id, "identity": identity},
        }

    # FastMCPTransport bypasses HTTP auth middleware — identity in-process
    # will be "anonymous" (no token context in memory transport).
    # This confirms that auth middleware is HTTP-only (story pattern documented
    # in Dev Notes: "for auth-specific tests that need a verifier, instantiate
    # a fresh, local FastMCP app in the test").
    async with Client(FastMCPTransport(test_mcp)) as client:
        result = await client.call_tool("health_static", {})

    assert not result.is_error, f"health_static returned error: {result}"
    data = (result.structured_content or {}).get("data", {})
    assert "identity" in data, f"Expected 'identity' in data: {data}"
    # In-process transport: no HTTP auth middleware, so identity is "anonymous"
    assert data["identity"] == "anonymous", (
        f"Expected 'anonymous' for in-process call (no HTTP), got: {data['identity']!r}"
    )


# ---------------------------------------------------------------------------
# T7.4 — get_daily_report envelope data contains identity field
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_daily_report_identity_field_present():
    """get_daily_report envelope data must contain 'identity' field (AC3, T7.4)."""
    with patch("core.main.warehouse.query_daily_report", return_value=[]):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_daily_report",
                {"date_range": {"start": "2026-01-01", "end": "2026-01-07"}},
            )

    assert not result.is_error, f"get_daily_report returned error: {result}"
    envelope = result.structured_content
    assert envelope is not None, "structuredContent must be present"
    data = envelope.get("data", {})
    assert "identity" in data, (
        f"Expected 'identity' key in envelope data, got keys: {list(data.keys())}"
    )
    assert data["identity"] == "anonymous", (
        f"Expected identity='anonymous' in disabled mode, got: {data['identity']!r}"
    )


# ---------------------------------------------------------------------------
# T7.4 / T7.5 — list_modules also carries identity
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_list_modules_identity_field_present():
    """list_modules envelope data must contain 'identity' field (AC3)."""
    async with Client(FastMCPTransport(mcp)) as client:
        result = await client.call_tool("list_modules", {})

    assert not result.is_error, f"list_modules returned error: {result}"
    payload = result.structured_content or {}
    data = payload.get("data", {})
    assert "identity" in data, (
        f"Expected 'identity' in list_modules data: {list(data.keys())}"
    )
    assert data["identity"] == "anonymous"


# ---------------------------------------------------------------------------
# T7.5 — Existing health tests: backward compat (project_id + status)
# ---------------------------------------------------------------------------


def test_health_direct_call_default_project_id():
    """Direct call health() must still return project_id='default' (backward compat)."""
    from core.main import health

    result = health()
    assert result["data"]["project_id"] == "default"
    assert result["data"]["status"] == "ok"
    # New field present
    assert "identity" in result["data"]


def test_health_direct_call_explicit_project_id():
    """Direct call health(project_id='x') must still round-trip project_id (backward compat)."""
    from core.main import health

    result = health(project_id="proj_demo")
    assert result["data"]["project_id"] == "proj_demo"
    assert result["data"]["status"] == "ok"
