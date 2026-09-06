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
    """Story 46.4 replaced both `setup-journey` reads with one canonical,
    Project-scoped Getting Started endpoint. The delegated-handoff machinery it
    reuses is unchanged and must still be mounted."""
    from core.admin_api import router
    from core.getting_started_api import getting_started_routes

    paths = {route.path for route in router.routes}
    # Retired by the cutover — no alias, no compatibility route.
    assert "/api/projects/{project_id}/setup-journey" not in paths
    assert "/api/organizations/{org_id}/setup-journey" not in paths
    # The bearer machinery is reused verbatim: issue, exchange, revoke, return.
    assert "/api/setup/handoffs/{handoff_id}/revoke" in paths
    assert "/handoff" in paths
    assert "/api/setup/handoffs/exchange" in paths
    # The two task-scoped operations moved under the Project, which is what makes
    # a handoff resumable at an exact canonical route rather than a bare task id.
    canonical = {route.path for route in getting_started_routes}
    assert canonical == {
        "/api/projects/{project_id}/getting-started",
        # THE EXPLICIT WRITE (amendment of 2026-08-17). The GET used to create the
        # journey and reconcile it; creating one names an operator, and the GET
        # only demands `view` -- so the first person to LOOK became the author.
        # The gesture is a POST now, and it is idempotent.
        "/api/projects/{project_id}/getting-started/journey",
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/handoffs",
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/owner",
    }
    assert canonical <= paths
    assert "/api/setup/tasks/{task_id}/handoffs" not in paths
    assert "/api/setup/tasks/{task_id}/reassign" not in paths


def test_client_completion_payload_is_not_forwarded_to_reconciliation():
    """A client cannot declare a step complete.

    Story 36.6 proved this by inspecting what the removed `_get_setup_journey`
    forwarded. Story 46.4 made it structural: the composer took the Project and the
    actor and nothing else, so no request body could carry `{"completed": true}`.

    The amendment of 2026-08-17 goes one step further. The read takes NO actor at
    all, because it no longer writes anything an actor could be attributed with --
    it derives every step's state from the shared readiness projection. An actor is
    required only by `reconcile_project_journey`, which is a write and says so.
    """
    import inspect

    from core.getting_started import read_getting_started, reconcile_project_journey

    assert list(inspect.signature(read_getting_started).parameters) == ["conn", "project_id"]
    assert list(inspect.signature(reconcile_project_journey).parameters) == [
        "conn",
        "project_id",
        "actor",
    ]


def test_the_organization_scoped_journey_read_is_gone():
    """36.6 exposed an org-wide journey. 46.4 retired it: a journey belongs to a
    Project, and Organization membership alone no longer opens one."""
    from core import admin_api

    assert not hasattr(admin_api, "_get_setup_journey")


def test_prepare_handoff_denial_never_calls_domain(monkeypatch):
    """Denied scope must be refused BEFORE the domain is touched."""
    from core import db, getting_started_api
    from core import setup_responsibilities as setup

    conn = MagicMock()

    @contextmanager
    def get_connection():
        yield conn

    monkeypatch.setattr(db, "get_connection", get_connection)
    monkeypatch.setattr(
        getting_started_api,
        "_authorize",
        AsyncMock(return_value=JSONResponse({"code": "not_found"}, status_code=404)),
    )
    prepare = MagicMock()
    monkeypatch.setattr(setup, "prepare_handoff", prepare)

    response = asyncio.run(
        getting_started_api.post_task_handoff(
            _request(
                "/api/projects/proj-1/getting-started/tasks/task-1/handoffs",
                "POST",
                path_params={"project_id": "proj-1", "task_id": "task-1"},
                body={"expires_in_hours": 48},
            )
        )
    )
    assert response.status_code == 404
    prepare.assert_not_called()
    conn.commit.assert_not_called()


def test_the_real_app_assembles_with_the_canonical_routes_spliced_in():
    """`build_asgi_app()` really constructs, and the routes it mounts are the
    ones asserted above — the canonical Getting Started endpoints live in
    `admin_api.router`, not in an unmounted list.

    Issuing a request is deliberately NOT done here: the canonical handler
    authenticates against Postgres and this suite is not pg-gated. The
    request-level seam coverage is in
    `tests/integration/test_global_scope_api_seams.py`.
    """
    from core.admin_api import router
    from core.getting_started_api import getting_started_routes
    from core.main import build_asgi_app

    assert build_asgi_app() is not None
    mounted = {route.path for route in router.routes}
    assert {route.path for route in getting_started_routes} <= mounted
