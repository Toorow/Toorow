"""Story 36.6 REST route and authorization seam tests."""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock

from starlette.requests import Request
from starlette.responses import JSONResponse


def _request(path: str, method: str, *, path_params=None, body=None, headers=None):
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

    raw_headers = [(b"idempotency-key", b"setup-1")]
    raw_headers.extend((k.lower().encode(), v.encode()) for k, v in (headers or {}).items())
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "path_params": path_params or {},
            "headers": raw_headers,
        },
        receive,
    )


def test_setup_routes_registered_and_reachable():
    from core.admin_api import router

    paths = {route.path for route in router.routes}
    assert "/api/projects/{project_id}/setup-journey" in paths
    assert "/api/organizations/{org_id}/setup-journey" in paths
    assert "/api/setup/tasks/{task_id}/handoffs" in paths
    assert "/api/setup/tasks/{task_id}/reassign" in paths
    assert "/api/setup/handoffs/{handoff_id}/revoke" in paths
    assert "/handoff" in paths
    assert "/api/setup/handoffs/exchange" in paths


def test_client_completion_payload_is_not_forwarded_to_reconciliation(monkeypatch):
    from core import admin_api, db, project_access
    from core import setup_responsibilities as setup

    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "camille")))
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    decision = MagicMock(allowed=True, org_id="org-1")
    monkeypatch.setattr(
        project_access, "resolve_strict_resource_access", lambda *_a, **_k: decision
    )
    reconcile = MagicMock(return_value={"journey_id": "journey-1", "tasks": []})
    monkeypatch.setattr(setup, "get_reconciled_journey", reconcile)

    response = asyncio.run(
        admin_api._get_setup_journey(
            _request(
                "/api/projects/proj-1/setup-journey",
                "GET",
                path_params={"project_id": "proj-1"},
                body={"completed": True},
            )
        )
    )
    assert response.status_code == 200
    assert reconcile.call_args.kwargs.keys() == {"project_id", "server_evidence"}
    assert response.headers["cache-control"].startswith("no-store")


def test_org_setup_journey_requires_membership_and_uses_org_evidence(monkeypatch):
    from core import admin_api, db, project_access
    from core import setup_responsibilities as setup

    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "camille")))
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(project_access, "identity_has_org_access", lambda *_a: True)
    reconcile = MagicMock(return_value={"journey_id": "journey-org", "tasks": []})
    monkeypatch.setattr(setup, "get_reconciled_journey", reconcile)

    response = asyncio.run(
        admin_api._get_setup_journey(
            _request(
                "/api/organizations/org-1/setup-journey",
                "GET",
                path_params={"org_id": "org-1"},
            )
        )
    )

    assert response.status_code == 200
    assert reconcile.call_args.kwargs == {
        "org_id": "org-1",
        "server_evidence": {"organization_access": {"org-1": True}},
    }

def test_prepare_handoff_denial_never_calls_domain(monkeypatch):
    from core import admin_api, db, project_access
    from core import setup_responsibilities as setup

    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(admin_api, "_check_auth", AsyncMock(return_value=(True, "outsider")))
    monkeypatch.setattr(project_access, "epic36_production_access_enabled", lambda: True)
    monkeypatch.setattr(
        admin_api,
        "_authorize_setup_task",
        lambda *_a, **_k: JSONResponse({"code": "not_found"}, status_code=404),
    )
    prepare = MagicMock()
    monkeypatch.setattr(setup, "prepare_handoff", prepare)

    response = asyncio.run(
        admin_api._prepare_setup_handoff(
            _request(
                "/api/setup/tasks/task-1/handoffs",
                "POST",
                path_params={"task_id": "task-1"},
                body={"expires_in_hours": 48},
            )
        )
    )
    assert response.status_code == 404
    prepare.assert_not_called()
    conn.commit.assert_not_called()


def test_setup_route_is_reachable_through_real_asgi_app():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    response = TestClient(build_asgi_app()).get(
        "/api/projects/proj-1/setup-journey", headers={"Host": "localhost"}
    )
    assert response.status_code == 404
    assert response.status_code != 405
