"""Canonical Getting Started API routes (Story 46.4)."""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.getting_started import read_getting_started, reconcile_project_journey

logger = logging.getLogger(__name__)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


async def _authorize(request: Request, capability: str):
    from core.admin_api import _check_auth, _strict_project_capability_allowed
    from core.db import get_connection

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            )
        )
    actor = identity or ""
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        if not _strict_project_capability_allowed(
            conn, identity=actor, project_id=project_id, minimum_capability=capability
        ):
            return _no_store(
                JSONResponse({"code": "not_found", "message": "Project not found"}, status_code=404)
            )
    return actor


async def get_getting_started(request: Request) -> Response:
    """READ ONLY. No bootstrap, no reconciliation, NO COMMIT.

    This handler used to create the journey and reconcile its steps, and the
    capability it demands is `view` -- so the first person to open the page became
    the journey's `operator_identity`. The bootstrap moved to Project creation
    (where it already lived) and to the explicit POST below; the reconciliation
    moved to `reconcile_project_journey`. What is left here reads and derives.
    """
    from core.db import get_connection

    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        with get_connection() as conn:
            result = read_getting_started(
                conn, project_id=request.path_params["project_id"]
            )
        return _no_store(JSONResponse(result))
    except Exception:
        # The response stays deliberately vague to the caller -- it says nothing
        # about the project it could not read. The LOG must not: this handler
        # swallowed a CheckViolation while the screen showed "Getting Started is
        # unavailable" and nothing anywhere named the row that broke.
        logger.exception(
            "getting_started_read_failed project_id=%s",
            request.path_params["project_id"],
        )
        return _no_store(
            JSONResponse(
                {
                    "code": "getting_started_unavailable",
                    "message": "Getting Started is unavailable",
                },
                status_code=503,
            )
        )


async def post_journey_materialization(request: Request) -> Response:
    """Create this Project's setup journey, and journal readiness against it.

    THE EXPLICIT WRITE the GET used to perform implicitly. Idempotent: a Project
    that already has a journey gets its reconciliation and nothing else, so the
    route may be called any number of times. `edit`, not `view` -- creating a
    journey names an operator, and naming one is an authoring act.
    """
    from core.db import get_connection

    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    try:
        with get_connection() as conn:
            result = reconcile_project_journey(
                conn, project_id=request.path_params["project_id"], actor=actor
            )
            conn.commit()
        return _no_store(JSONResponse(result))
    except Exception:
        logger.exception(
            "getting_started_materialization_failed project_id=%s",
            request.path_params["project_id"],
        )
        return _no_store(
            JSONResponse(
                {
                    "code": "journey_not_started",
                    "message": "The setup journey was not started",
                },
                status_code=422,
            )
        )


async def post_task_handoff(request: Request) -> Response:
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import prepare_handoff

    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = (request.headers.get("Idempotency-Key") or "").strip()
    try:
        body = json.loads(await request.body())
        task_id = request.path_params["task_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM app.setup_tasks t JOIN app.setup_journeys j "
                    "ON j.id=t.journey_id WHERE t.id=%s AND j.project_id=%s",
                    (task_id, request.path_params["project_id"]),
                )
                if not cur.fetchone():
                    return _no_store(
                        JSONResponse(
                            {"code": "not_found", "message": "Task not found"}, status_code=404
                        )
                    )
            # THE JOURNAL CATCHES UP ON A WRITE, NEVER ON A READ. This is an
            # authorized `edit` gesture on the surface, so it is the right moment
            # to persist the readiness derivation the GET only displays.
            reconcile_project_journey(
                conn, project_id=request.path_params["project_id"], actor=actor
            )
            result = prepare_handoff(
                conn,
                task_id=task_id,
                actor=actor,
                expires_in_hours=int(body.get("expires_in_hours", 48)),
                idempotency_key=key,
                host_context={"scheme": request.url.scheme, "host": request.url.hostname or ""},
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
        payload = {
            "handoff_id": result.handoff_id,
            "state": result.state,
            "expires_at": result.expires_at,
            "replayed": result.replayed,
        }
        if result.delivery_url:
            payload["delivery_handoff"] = {"url": result.delivery_url, "single_return": True}
        return _no_store(JSONResponse(payload, status_code=201))
    except Exception:
        # Same discipline as the GET above: the caller learns nothing about the
        # cause, the log learns everything. A handler that discards the
        # exception is how the CheckViolation behind this screen stayed unnamed.
        logger.exception(
            "getting_started_handoff_failed project_id=%s task_id=%s",
            request.path_params["project_id"],
            request.path_params["task_id"],
        )
        return _no_store(
            JSONResponse(
                {"code": "handoff_unavailable", "message": "The handoff was not created"},
                status_code=422,
            )
        )


async def patch_task_owner(request: Request) -> Response:
    from core import tracing
    from core.db import get_connection
    from core.setup_responsibilities import reassign_task

    actor = await _authorize(request, "manage")
    if isinstance(actor, Response):
        return actor
    key = (request.headers.get("Idempotency-Key") or "").strip()
    try:
        body = json.loads(await request.body())
        task_id = request.path_params["task_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT 1 FROM app.setup_tasks t JOIN app.setup_journeys j "
                    "ON j.id=t.journey_id WHERE t.id=%s AND j.project_id=%s",
                    (task_id, request.path_params["project_id"]),
                )
                if not cur.fetchone():
                    return _no_store(
                        JSONResponse(
                            {"code": "not_found", "message": "Task not found"}, status_code=404
                        )
                    )
            result = reassign_task(
                conn,
                task_id=task_id,
                actor=actor,
                actor_type=str(body.get("actor_type") or ""),
                assigned_identity=body.get("assigned_identity"),
                idempotency_key=key,
                host_context={"scheme": request.url.scheme, "host": request.url.hostname or ""},
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
        return _no_store(JSONResponse(result))
    except Exception:
        logger.exception(
            "getting_started_owner_change_failed project_id=%s task_id=%s",
            request.path_params["project_id"],
            request.path_params["task_id"],
        )
        return _no_store(
            JSONResponse(
                {"code": "owner_change_unavailable", "message": "The task owner was not changed"},
                status_code=422,
            )
        )


getting_started_routes = [
    Route("/api/projects/{project_id}/getting-started", get_getting_started, methods=["GET"]),
    Route(
        "/api/projects/{project_id}/getting-started/journey",
        post_journey_materialization,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/handoffs",
        post_task_handoff,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/owner",
        patch_task_owner,
        methods=["PATCH"],
    ),
]
