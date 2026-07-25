"""Tests for the Story 1.1 platform skeleton.

Covers:
  * the ``health`` tool returns the canonical AD-1 envelope with status "ok";
  * the AD-14 identity scaffold (``project_id`` parameter) round-trips;
  * the AD-1 envelope shape (schema_version + meta{freshness,provenance,alerts} + data);
  * the canonical error shape helper;
  * the Host-header 421 ASGI middleware (T3.2): allowed passes, disallowed → 421.

These use only the local functions/objects in ``core.main`` so they run without
a live network server. A live end-to-end smoke test is documented in
CONTRIBUTING.md (T8.2).
"""

from __future__ import annotations

import asyncio

from core.main import (
    HostHeaderValidationMiddleware,
    _envelope,
    _error,
    health,
    mcp,
)


def _unwrap(result):
    """FastMCP's @tool may return the raw dict (sync call) — normalize it."""
    # When called directly as a Python function, `health` returns the envelope dict.
    return result


# --------------------------------------------------------------------------- #
# health tool                                                                  #
# --------------------------------------------------------------------------- #
def test_health_returns_ok_envelope():
    result = _unwrap(health())
    assert result["schema_version"] == "1"
    assert result["data"]["status"] == "ok"


def test_health_identity_scaffold_default():
    # AD-14: project_id parameter flows through from P0.
    result = _unwrap(health())
    assert result["data"]["project_id"] == "default"


def test_health_identity_scaffold_explicit():
    result = _unwrap(health(project_id="proj_demo"))
    assert result["data"]["project_id"] == "proj_demo"


def test_health_envelope_meta_shape():
    result = _unwrap(health())
    meta = result["meta"]
    assert set(meta.keys()) == {"freshness", "provenance", "alerts"}
    assert meta["freshness"] == "live"
    assert isinstance(meta["alerts"], list)
    assert meta["provenance"]["source_system"] == "connector-core"


# --------------------------------------------------------------------------- #
# canonical envelope + error helpers                                           #
# --------------------------------------------------------------------------- #
def test_envelope_shape():
    env = _envelope({"x": 1}, provenance={"p": 1}, freshness="live", alerts=["a"])
    assert env["schema_version"] == "1"
    assert env["data"] == {"x": 1}
    assert env["meta"]["alerts"] == ["a"]


def test_error_shape():
    err = _error("auth_expired", "reconnect required")
    assert err == {"code": "auth_expired", "message": "reconnect required", "provenance": None}


# --------------------------------------------------------------------------- #
# FastMCP app is mount-ready and named "connector"                             #
# --------------------------------------------------------------------------- #
def test_mcp_app_name():
    assert mcp.name == "connector"


# --------------------------------------------------------------------------- #
# Host-header 421 middleware (T3.2)                                            #
# --------------------------------------------------------------------------- #
def _run_asgi(app, host_header: bytes | None):
    """Drive one HTTP request through an ASGI app; return (status, body)."""
    headers = []
    if host_header is not None:
        headers.append((b"host", host_header))
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
    }
    sent = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(msg):
        sent.append(msg)

    asyncio.run(app(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return status, body


class _OkApp:
    """Minimal downstream ASGI app returning 200."""

    async def __call__(self, scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})


def test_host_guard_rejects_disallowed(monkeypatch):
    monkeypatch.setenv("HOST_HEADER_VALIDATION", "strict")
    monkeypatch.setenv("ALLOWED_HOST", "connector.example.com")
    mw = HostHeaderValidationMiddleware(_OkApp())
    status, body = _run_asgi(mw, b"evil.attacker.com")
    assert status == 421
    assert b"misdirected_request" in body


def test_host_guard_allows_matching(monkeypatch):
    monkeypatch.setenv("HOST_HEADER_VALIDATION", "strict")
    monkeypatch.setenv("ALLOWED_HOST", "connector.example.com")
    mw = HostHeaderValidationMiddleware(_OkApp())
    status, body = _run_asgi(mw, b"connector.example.com")
    assert status == 200
    assert body == b"ok"


def test_host_guard_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HOST_HEADER_VALIDATION", raising=False)
    monkeypatch.delenv("ALLOWED_HOST", raising=False)
    mw = HostHeaderValidationMiddleware(_OkApp())
    status, _ = _run_asgi(mw, b"anything.com")
    assert status == 200
