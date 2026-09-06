"""Canonical Project-scoped Overview REST surface (Story 46.2)."""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.project_overview import compose_project_overview

logger = logging.getLogger(__name__)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> Response:
    return _no_store(
        JSONResponse(
            {"code": "not_found", "message": "Project not found"},
            status_code=404,
        )
    )


async def _get_project_overview(request: Request) -> Response:
    from core.admin_api import _check_auth, _strict_project_capability_allowed
    from core.db import get_connection

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"},
                status_code=401,
            )
        )
    actor = identity or "anonymous"
    project_id = request.path_params["project_id"]
    try:
        with get_connection() as conn:
            if not _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _not_found()
            can_edit = _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=project_id,
                minimum_capability="edit",
            )
            envelope = compose_project_overview(
                project_id,
                conn,
                actor=actor,
                can_edit=can_edit,
            )
        return _no_store(JSONResponse(envelope, status_code=200))
    except Exception as exc:  # fail closed; required identity/posture failures are fatal.
        logger.warning("project overview unavailable project=%s: %s", project_id, exc)
        return _no_store(
            JSONResponse(
                {
                    "code": "project_overview_unavailable",
                    "message": "Project Overview is unavailable",
                },
                status_code=503,
            )
        )


project_overview_routes = [
    Route(
        "/api/projects/{project_id}/overview",
        endpoint=_get_project_overview,
        methods=["GET"],
    )
]
