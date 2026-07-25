"""Story 12.1 real REST/MCP seams for the scoped source-capability catalog."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from starlette.testclient import TestClient

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("TOOROW_AUTH_MODE", "disabled")

from core.main import build_asgi_app, mcp  # noqa: E402

_DEFAULT_ROW = object()


class _Cursor:
    def __init__(
        self,
        *,
        provider: str = "google-sheets",
        enabled: bool = True,
        connection_row=_DEFAULT_ROW,
        connection_project_id: str = "project-a",
    ):
        self.provider = provider
        self.enabled = enabled
        self.connection_row = connection_row
        self.connection_project_id = connection_project_id
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=()):
        sql = " ".join(str(query).split())
        if "FROM app.connection_ref" in sql:
            assert len(_params) == 2
            connection_ref_id, project_id = _params
            assert connection_ref_id == "connection-a"
            if project_id != self.connection_project_id:
                self.row = None
            else:
                self.row = (
                    (self.provider, "active", True)
                    if self.connection_row is _DEFAULT_ROW
                    else self.connection_row
                )
        elif "FROM app.project_modules" in sql:
            self.row = (self.enabled,)
        else:
            raise AssertionError(f"Unexpected SQL in capability seam: {sql}")

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(
        self,
        *,
        provider: str = "google-sheets",
        enabled: bool = True,
        connection_row=_DEFAULT_ROW,
        connection_project_id: str = "project-a",
    ):
        self.provider = provider
        self.enabled = enabled
        self.connection_row = connection_row
        self.connection_project_id = connection_project_id

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def cursor(self):
        return _Cursor(
            provider=self.provider,
            enabled=self.enabled,
            connection_row=self.connection_row,
            connection_project_id=self.connection_project_id,
        )


def _connection_factory():
    return _Connection()


def _rest_get(*, auth=(True, "operator@example.com"), connection=None, access=True):
    app = build_asgi_app()
    factory = connection or _connection_factory
    with (
        patch("core.admin_api._check_auth", new=AsyncMock(return_value=auth)),
        patch("core.db.get_connection", side_effect=factory),
        patch("core.project_access.identity_has_project_access", return_value=access),
        TestClient(app, raise_server_exceptions=True) as client,
    ):
        return client.get(
            "/api/source-capabilities",
            params={"project_id": "project-a", "connection_ref_id": "connection-a"},
        )


def test_source_capabilities_rest_wires_through_real_asgi_app():
    response = _rest_get()

    assert response.status_code == 200
    payload = response.json()
    assert payload["project_id"] == "project-a"
    assert payload["connection_ref_id"] == "connection-a"
    assert payload["module"]["name"] == "google-sheets"
    assert payload["reports"][0]["id"] == "sheet_daily"
    assert "source_capabilities" not in payload
    assert "_ai_53_notes" not in repr(payload)
    assert "secret" not in repr(payload).lower()


def test_source_capabilities_rest_requires_authentication():
    response = _rest_get(auth=(False, "anonymous"))
    assert response.status_code == 401


@pytest.mark.parametrize(
    "connection",
    [
        lambda: _Connection(connection_row=None),
        lambda: _Connection(connection_project_id="project-b"),
        lambda: _Connection(enabled=False),
        lambda: _Connection(provider="not-a-loaded-module"),
        lambda: _Connection(connection_row=("google-sheets", "revoked", True)),
    ],
)
def test_source_capabilities_rest_hides_unknown_disabled_or_mismatched_scope(connection):
    response = _rest_get(connection=connection)
    assert response.status_code == 404
    assert response.json() == {"error": "source_capabilities_not_found"}
    assert "google-sheets" not in response.text


def test_source_capabilities_rest_hides_denied_project_access():
    response = _rest_get(access=False)
    assert response.status_code == 404
    assert response.json() == {"error": "source_capabilities_not_found"}

def test_source_capabilities_rest_returns_503_when_scope_cannot_be_proven():
    def _db_down():
        raise RuntimeError("db down")

    response = _rest_get(connection=_db_down)
    assert response.status_code == 503
    assert response.json() == {"error": "source_capabilities_unavailable"}


@pytest.mark.anyio
async def test_source_capabilities_mcp_uses_public_transport_and_dual_channel():
    with (
        patch("core.db.get_connection", side_effect=_connection_factory),
        patch("core.project_access.identity_has_project_access", return_value=True),
    ):
        async with Client(FastMCPTransport(mcp)) as client:
            result = await client.call_tool(
                "get_source_capabilities",
                {"project_id": "project-a", "connection_ref_id": "connection-a"},
            )

    assert result.is_error is False
    assert result.structured_content["module"]["name"] == "google-sheets"
    assert result.structured_content["reports"][0]["id"] == "sheet_daily"
    assert result.content[0].text == "Google Sheets: 1 report, 0 selectable."
    assert "source_capabilities" not in result.content[0].text
