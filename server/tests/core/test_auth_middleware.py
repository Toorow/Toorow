"""Tests for auth middleware rejection and acceptance (Story 2.3, T6).

T6.1 - Module created.
T6.2 - Fresh FastMCP instance with StaticTokenVerifier (isolated from global mcp).
T6.3 - Unauthenticated HTTP call returns 401.
T6.4 - Valid static token accepted over HTTP.

Auth enforcement is an HTTP-layer concern (RFC 6750 Bearer middleware). The
FastMCPTransport (in-process) bypasses HTTP and is used for testing disabled
mode and identity propagation (see test_identity_propagation.py). For auth
rejection we use an ASGI test client against the FastMCP http_app() with its
lifespan properly initialised.
"""

from __future__ import annotations

import pytest
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier
from starlette.testclient import TestClient

TEST_TOKEN = "test-static-token-story23"
TEST_SUBJECT = "test-user-story23"


@pytest.fixture
def auth_mcp():
    """Fresh FastMCP instance with StaticTokenVerifier -- isolated from global mcp."""
    verifier = StaticTokenVerifier(
        tokens={TEST_TOKEN: {"client_id": TEST_SUBJECT, "sub": TEST_SUBJECT, "scopes": []}}
    )
    server = FastMCP("test-auth-story23", auth=verifier)

    @server.tool
    def ping() -> str:
        """Simple tool for auth testing."""
        return "pong"

    return server


def test_unauthenticated_call_rejected(auth_mcp):
    """Unauthenticated HTTP request must return 401 (AC1, T6.3)."""
    app = auth_mcp.http_app(transport="streamable-http")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "ping", "arguments": {}},
            },
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 401, (
        f"Expected 401, got {response.status_code}. Body: {response.text}"
    )
    # RFC 6750: missing auth must have WWW-Authenticate: Bearer header
    www_auth = response.headers.get("www-authenticate", "")
    assert "Bearer" in www_auth, (
        f"Expected WWW-Authenticate: Bearer, got: {www_auth!r}"
    )


def test_valid_static_token_accepted(auth_mcp):
    """Valid Bearer token must not return 401 or 403 (AC2, T6.4)."""
    app = auth_mcp.http_app(transport="streamable-http")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
            headers={
                "content-type": "application/json",
                "authorization": f"Bearer {TEST_TOKEN}",
                "accept": "application/json, text/event-stream",
            },
        )

    # With a valid token the server must not reject with 401 or 403
    assert response.status_code != 401, (
        f"Valid token was rejected (401). Response: {response.text}"
    )
    assert response.status_code != 403, (
        f"Valid token was forbidden (403). Response: {response.text}"
    )
