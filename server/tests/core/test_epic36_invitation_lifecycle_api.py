"""Story 36.5 invitation lifecycle REST seam tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request
from starlette.responses import JSONResponse


def _request(path: str, *, method: str, body: dict | None = None, path_params=None) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": json.dumps(body or {}).encode(),
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "path_params": path_params or {},
            "headers": [(b"idempotency-key", b"lifecycle-1")],
        },
        receive,
    )


def test_lifecycle_routes_are_registered():
    from core.admin_api import router

    paths = {route.path for route in router.routes}
    assert "/api/organizations/{org_id}/invitations" in paths
    assert "/api/organizations/{org_id}/invitations/{invitation_id}/revoke" in paths
    assert "/api/organizations/{org_id}/invitations/{invitation_id}/resend" in paths


def test_list_route_returns_only_safe_projection(monkeypatch):
    from core import admin_api, db, invitations, project_access

    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "admin-1")))
    monkeypatch.setattr(admin_api, "_enforce_org_manage", lambda *_a: None)
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        invitations,
        "list_safe_invitations",
        lambda *_a, **_k: [
            {
                "invitation_id": "invite-1",
                "state": "pending",
                "explicit_grants": [],
                "explicit_none": True,
            }
        ],
    )
    response = asyncio.run(
        admin_api._list_invitations(
            _request(
                "/api/organizations/org-1/invitations",
                method="GET",
                path_params={"org_id": "org-1"},
            )
        )
    )
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body["items"][0]["explicit_none"] is True
    assert "bearer" not in response.body.decode()
    assert response.headers["cache-control"].startswith("no-store")


def test_resend_authority_denial_calls_no_domain_mutation(monkeypatch):
    from core import admin_api, db, invitations, project_access

    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "admin-1")))
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        admin_api,
        "_authorize_invitation_binding",
        lambda *_a, **_k: JSONResponse({"code": "not_found"}, status_code=404),
    )
    resend = MagicMock()
    monkeypatch.setattr(invitations, "resend_invitation", resend)
    response = asyncio.run(
        admin_api._resend_invitation(
            _request(
                "/api/organizations/org-1/invitations/invite-1/resend",
                method="POST",
                body={"expires_in_hours": 48},
                path_params={"org_id": "org-1", "invitation_id": "invite-1"},
            )
        )
    )
    assert response.status_code == 404
    resend.assert_not_called()
    conn.commit.assert_not_called()


def test_lifecycle_list_route_is_reachable_through_real_asgi_app():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    client = TestClient(build_asgi_app(), raise_server_exceptions=True)
    response = client.get(
        "/api/organizations/org-1/invitations",
        headers={"Host": "localhost"},
    )
    assert response.status_code == 404
    assert response.status_code != 405
