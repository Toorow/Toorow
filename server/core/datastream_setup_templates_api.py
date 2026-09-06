"""Story 57.7 -- the three doors of an organization's saved setup templates.

THE ONLY NEW ROUTES OF THIS STORY, and they are only the SAVING half. Applying a
template is `PATCH /api/projects/{project_id}/datastream-setup-drafts/{draft_id}`
with the payload the template holds -- a route that already exists and already
accepts a whole `operator_input`. Adding an "apply" endpoint would put the same
rule in two places.

Same authorization seam, same idempotency discipline and same error mapping as
`datastream_preconfiguration_api`: this is the same surface, and an operator must
not meet two dialects of refusal inside one wizard.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.datastream_setup_templates import (
    TemplateConflict,
    TemplateNotFound,
    TemplateValidationError,
    list_templates,
    retire_template,
    save_template,
)

logger = logging.getLogger(__name__)


def _response(payload: Any, status: int = 200) -> Response:
    response = JSONResponse(payload, status_code=status)
    response.headers["Cache-Control"] = "no-store"
    return response


def _not_found() -> Response:
    return _response({"code": "not_found", "message": "Resource not found"}, 404)


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise TemplateValidationError("JSON body must be an object")
    return value


async def _authorize(request: Request, capability: str) -> str | Response:
    """The project capability check, and NEVER a 404 for a failure to evaluate.

    Copied in shape from `datastream_preconfiguration_api._authorize` because the
    lesson it carries is this surface's: a database fault answered as `404` is
    indistinguishable from a legitimate refusal, and an operator watches an
    object they own disappear with nothing in the log.
    """
    from core.admin_api import _check_auth, _strict_project_capability_allowed  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _response({"code": "unauthorized", "message": "Bearer token required"}, 401)
    actor = identity or "anonymous"
    try:
        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=request.path_params["project_id"],
                minimum_capability=capability,
                hold_access=capability == "edit",
            )
    except Exception:
        logger.exception(
            "datastream_setup_templates: capability_check_failed project=%s capability=%s",
            request.path_params.get("project_id"),
            capability,
        )
        return _response(
            {"code": "setup_templates_unavailable", "message": "Setup templates are unavailable"},
            503,
        )
    return actor if allowed else _not_found()


def _key(request: Request) -> str | Response:
    value = (request.headers.get("Idempotency-Key") or "").strip()
    if not value:
        return _response(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"}, 422
        )
    if len(value) > 200:
        return _response(
            {"code": "invalid_idempotency_key", "message": "Idempotency-Key is too long"}, 422
        )
    return value


def _error(exc: Exception) -> Response:
    if isinstance(exc, TemplateNotFound):
        return _not_found()
    if isinstance(exc, TemplateConflict):
        return _response({"code": exc.code, "message": str(exc)}, 409)
    if isinstance(exc, (TemplateValidationError, json.JSONDecodeError, TypeError, ValueError)):
        code = getattr(exc, "code", "invalid_request")
        return _response({"code": code, "message": str(exc)}, 422)
    # The catch-all LOGS. A 503 that leaves no trace is a defect nobody can
    # diagnose -- the same silence `datastream_preconfiguration_api` was
    # corrected for on 2026-08-04.
    logger.error("datastream_setup_templates: unmapped_error %s", type(exc).__name__, exc_info=exc)
    return _response(
        {"code": "setup_templates_unavailable", "message": "Setup templates are unavailable"}, 503
    )


async def _save(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    key = _key(request)
    if isinstance(key, Response):
        return key
    try:
        body = await _body(request)
        datastream_id = str(body.get("datastream_id") or "").strip()
        if not datastream_id:
            raise TemplateValidationError("datastream_id is required")
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = save_template(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=datastream_id,
                label=str(body.get("label") or ""),
                actor=actor,
                idempotency_key=key,
            )
            conn.commit()
        return _response(result, 200 if result.get("idempotent_replay") else 201)
    except Exception as exc:
        return _error(exc)


async def _list(request: Request) -> Response:
    actor = await _authorize(request, "view")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            return _response(list_templates(conn, project_id=request.path_params["project_id"]))
    except Exception as exc:
        return _error(exc)


async def _retire(request: Request) -> Response:
    actor = await _authorize(request, "edit")
    if isinstance(actor, Response):
        return actor
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = retire_template(
                conn,
                project_id=request.path_params["project_id"],
                template_ref=request.path_params["template_ref"],
                actor=actor,
            )
            conn.commit()
        return _response(result)
    except Exception as exc:
        return _error(exc)


datastream_setup_templates_routes = [
    Route(
        "/api/projects/{project_id}/datastream-setup-templates",
        endpoint=_list,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-templates",
        endpoint=_save,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastream-setup-templates/{template_ref}",
        endpoint=_retire,
        methods=["DELETE"],
    ),
]
