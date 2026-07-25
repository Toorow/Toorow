"""Story 36.3 invitation REST/bootstrap seam tests."""

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request


def _request(body: dict, headers=None) -> Request:
    delivered = False

    async def receive():
        nonlocal delivered
        if delivered:
            return {"type": "http.disconnect"}
        delivered = True
        return {
            "type": "http.request",
            "body": json.dumps(body).encode(),
            "more_body": False,
        }

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/organizations/org-1/invitations",
            "headers": headers or [(b"idempotency-key", b"invite-1")],
            "path_params": {"org_id": "org-1"},
        },
        receive,
    )


def test_denied_resource_authority_mints_no_invitation(monkeypatch):
    from core import admin_api, db, invitations, project_access

    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.return_value = ("admin",)
    conn.cursor.return_value = cur

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(db, "set_local_access_context", MagicMock())
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "admin-1")))
    monkeypatch.setattr(admin_api, "_enforce_org_manage", lambda *_a: None)
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_a, **_k: project_access.AccessDecision(False, "grant_required"),
    )
    issue = MagicMock()
    monkeypatch.setattr(invitations, "issue_invitation", issue)
    response = asyncio.run(
        admin_api._issue_invitation(
            _request(
                {
                    "invited_identity": "user@example.com",
                    "role": "member",
                    "project_grants": [{"scope_id": "proj-1", "capability": "view"}],
                }
            )
        )
    )
    assert response.status_code == 404
    issue.assert_not_called()
    conn.commit.assert_not_called()


def test_bootstrap_is_no_store_tokenless_and_has_no_third_party_surface():
    from core.admin_api import _invitation_bootstrap

    request = Request({"type": "http", "method": "GET", "path": "/invite", "headers": []})
    response = asyncio.run(_invitation_bootstrap(request))
    body = response.body.decode()
    assert response.headers["cache-control"].startswith("no-store")
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert "unsafe-inline" not in response.headers["content-security-policy"]
    assert "script-src 'nonce-" in response.headers["content-security-policy"]
    assert "history.replaceState" in body
    assert "/api/invitations/exchange" in body
    assert "analytics" not in body.lower()
    assert "prefetch" not in body.lower()


def test_invitation_routes_are_registered_in_real_asgi_app():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    client = TestClient(build_asgi_app(), raise_server_exceptions=True)
    response = client.get("/invite", headers={"Host": "localhost"})
    assert response.status_code == 200
    assert response.headers["cache-control"].startswith("no-store")
