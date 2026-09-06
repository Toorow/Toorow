"""Project-scoped access surface routes (Story 46.4)."""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.operations import OperationIdempotencyConflict
from core.project_access_surface import (
    ProjectAccessConflict,
    ProjectAccessUnavailable,
    ProjectAccessValidationError,
    confirm_grant_change,
    issue_grant_confirmation,
    prepare_access_handoff,
    prepare_grant_change,
    read_project_access,
)

logger = logging.getLogger(__name__)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _error(exc: Exception) -> Response:
    if isinstance(exc, ProjectAccessUnavailable):
        return _no_store(
            JSONResponse({"code": "not_found", "message": "Project not found"}, status_code=404)
        )
    if isinstance(exc, ProjectAccessConflict):
        return _no_store(JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409))
    if isinstance(exc, OperationIdempotencyConflict):
        return _no_store(
            JSONResponse({"code": "idempotency_conflict", "message": str(exc)}, status_code=409)
        )
    if isinstance(exc, (ProjectAccessValidationError, ValueError, TypeError, json.JSONDecodeError)):
        return _no_store(
            JSONResponse({"code": "invalid_request", "message": str(exc)}, status_code=422)
        )
    logger.error("project_access: unmapped_error %s", type(exc).__name__, exc_info=exc)
    return _no_store(
        JSONResponse(
            {"code": "project_access_unavailable", "message": "Project Access is unavailable"},
            status_code=503,
        )
    )


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
            conn,
            identity=actor,
            project_id=project_id,
            minimum_capability=capability,
            hold_access=capability == "manage",
        ):
            return _no_store(
                JSONResponse({"code": "not_found", "message": "Project not found"}, status_code=404)
            )
    return actor


async def _body(request: Request) -> dict:
    value = json.loads(await request.body())
    if not isinstance(value, dict):
        raise ProjectAccessValidationError("JSON body must be an object")
    return value


async def get_project_access(request: Request) -> Response:
    from core.db import get_connection

    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        with get_connection() as conn:
            result = read_project_access(request.path_params["project_id"], conn, actor=actor)
        return _no_store(JSONResponse(result))
    except Exception as exc:
        return _error(exc)


async def post_grant_change(request: Request) -> Response:
    from core.db import get_connection

    actor = await _authorize(request, "manage")
    if isinstance(actor, Response):
        return actor
    try:
        body = await _body(request)
        key = (request.headers.get("Idempotency-Key") or "").strip()
        with get_connection() as conn:
            result = prepare_grant_change(
                conn,
                project_id=request.path_params["project_id"],
                identity=str(body.get("identity") or ""),
                after_capability=body.get("after_capability"),
                actor=actor,
                idempotency_key=key,
            )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=201))
    except Exception as exc:
        return _error(exc)


async def post_grant_confirmation(request: Request) -> Response:
    from core.db import get_connection

    actor = await _authorize(request, "manage")
    if isinstance(actor, Response):
        return actor
    try:
        with get_connection() as conn:
            result = issue_grant_confirmation(
                conn,
                change_id=request.path_params["change_id"],
                actor=actor,
                # The URL's project, the one `_authorize` proved manage on.
                project_id=request.path_params["project_id"],
            )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=201))
    except Exception as exc:
        return _error(exc)


async def confirm_change(request: Request) -> Response:
    from core.db import get_connection

    actor = await _authorize(request, "manage")
    if isinstance(actor, Response):
        return actor
    try:
        body = await _body(request)
        with get_connection() as conn:
            result = confirm_grant_change(
                conn,
                change_id=request.path_params["change_id"],
                actor=actor,
                confirmation_id=str(body.get("confirmation_id") or ""),
                confirmation_secret=str(body.get("confirmation_secret") or ""),
                # The URL's project, the one `_authorize` proved manage on.
                project_id=request.path_params["project_id"],
            )
            conn.commit()
        return _no_store(JSONResponse(result))
    except Exception as exc:
        return _error(exc)


async def post_access_handoff(request: Request) -> Response:
    from core.db import get_connection

    actor = await _authorize(request, "manage")
    if isinstance(actor, Response):
        return actor
    try:
        body = await _body(request)
        key = (request.headers.get("Idempotency-Key") or "").strip()
        with get_connection() as conn:
            result = prepare_access_handoff(
                conn,
                project_id=request.path_params["project_id"],
                identity=body.get("identity"),
                actor=actor,
                resume_ref=str(body.get("resume_ref") or ""),
                expires_in_hours=int(body.get("expires_in_hours", 48)),
                idempotency_key=key,
            )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=201))
    except Exception as exc:
        return _error(exc)


project_access_routes = [
    Route("/api/projects/{project_id}/access", get_project_access, methods=["GET"]),
    Route("/api/projects/{project_id}/access/grant-changes", post_grant_change, methods=["POST"]),
    Route(
        "/api/projects/{project_id}/access/grant-changes/{change_id}/confirmations",
        post_grant_confirmation,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/access/grant-changes/{change_id}/confirm",
        confirm_change,
        methods=["POST"],
    ),
    Route("/api/projects/{project_id}/access/handoffs", post_access_handoff, methods=["POST"]),
]
