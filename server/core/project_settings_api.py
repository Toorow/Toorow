"""Thin REST handlers for the Project Settings control plane (Story 46.3)."""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.project_settings import (
    ProjectSettingsBlocked,
    ProjectSettingsConflict,
    ProjectSettingsStale,
    ProjectSettingsValidationError,
    confirm_change_set,
    create_change_set,
    issue_change_confirmation,
    prepare_change_set,
    read_change_set_impact,
    read_project_settings,
)

logger = logging.getLogger(__name__)


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Project not found"}, status_code=404)


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


async def _body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ProjectSettingsValidationError("JSON body must be an object")
    return value


def _error(exc: Exception) -> Response:
    if isinstance(exc, ProjectSettingsStale):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
    if isinstance(exc, ProjectSettingsBlocked):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)
    if isinstance(exc, ProjectSettingsConflict):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
    if isinstance(exc, ProjectSettingsValidationError):
        status = 404 if str(exc) in {"Project not found", "Change Set not found"} else 422
        code = "not_found" if status == 404 else exc.code
        return JSONResponse({"code": code, "message": str(exc)}, status_code=status)
    from core.entry_confirmations import EntryConfirmationRefused, EntryConfirmationValidationError

    if isinstance(exc, EntryConfirmationRefused):
        return JSONResponse({"code": exc.code, "message": "Confirmation refused"}, status_code=409)
    if isinstance(exc, EntryConfirmationValidationError):
        return JSONResponse({"code": "invalid_confirmation", "message": str(exc)}, status_code=422)
    logger.error("project_settings: unmapped_error %s", type(exc).__name__, exc_info=exc)
    return JSONResponse(
        {"code": "project_settings_unavailable", "message": "Project Settings is unavailable"},
        status_code=503,
    )


async def _authorize(request: Request, minimum_capability: str) -> tuple[str, Any] | Response:
    from core.admin_api import _check_auth, _strict_project_capability_allowed
    from core.db import get_connection

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    actor = identity or "anonymous"
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        if not _strict_project_capability_allowed(
            conn,
            identity=actor,
            project_id=project_id,
            minimum_capability=minimum_capability,
            hold_access=minimum_capability == "manage",
        ):
            return _not_found()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
                (project_id,),
            )
            row = cur.fetchone()
        if row is None:
            return _not_found()
    return actor, row[0]


async def _get_settings(request: Request) -> Response:
    from core.admin_api import _check_auth, _strict_project_capability_allowed
    from core.db import get_connection

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    actor = identity or "anonymous"
    project_id = request.path_params["project_id"]
    try:
        with get_connection() as conn:
            if not _strict_project_capability_allowed(
                conn, identity=actor, project_id=project_id, minimum_capability="view"
            ):
                return _not_found()
            can_edit = _strict_project_capability_allowed(
                conn, identity=actor, project_id=project_id, minimum_capability="edit"
            )
            # `manage`, not `edit`: turning external sharing on is an
            # administrative posture change, the same rank as revoking a live
            # link. The screen reads this rather than inferring authority.
            can_manage = _strict_project_capability_allowed(
                conn,
                identity=actor,
                project_id=project_id,
                minimum_capability="manage",
                hold_access=True,
            )
            return _no_store(
                JSONResponse(
                    read_project_settings(
                        conn,
                        project_id=project_id,
                        can_edit=can_edit,
                        can_manage=can_manage,
                    ),
                    status_code=200,
                )
            )
    except Exception as exc:  # fail closed without leaking scope
        return _no_store(_error(exc))


async def _patch_profile(request: Request) -> Response:
    scope = await _authorize(request, "edit")
    if isinstance(scope, Response):
        return scope
    _actor, _org_id = scope
    project_id = request.path_params["project_id"]
    try:
        body = await _body(request)
        if not body or not set(body).issubset({"name", "description"}):
            raise ProjectSettingsValidationError("Only name and description are profile fields")
        assignments = []
        params: list[Any] = []
        if "name" in body:
            name = body["name"]
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
                raise ProjectSettingsValidationError("name must contain 1..100 characters")
            assignments.append("name = %s")
            params.append(name.strip())
        if "description" in body:
            description = body["description"]
            if description is not None and not isinstance(description, str):
                raise ProjectSettingsValidationError("description must be a string or null")
            value = description.strip() if isinstance(description, str) else None
            if value is not None and len(value) > 1000:
                raise ProjectSettingsValidationError("description must be at most 1000 characters")
            assignments.append("description = %s")
            params.append(value or None)
        from core.db import get_connection

        with get_connection() as conn:
            params.append(project_id)
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.projects SET "
                    + ", ".join(assignments)
                    + ", updated_at = NOW() WHERE id = %s RETURNING id, name, description",
                    params,
                )
                row = cur.fetchone()
            if row is None:
                return _not_found()
            conn.commit()
        return JSONResponse({"id": row[0], "name": row[1], "description": row[2]}, status_code=200)
    except Exception as exc:
        return _error(exc)


async def _put_external_sharing(request: Request) -> Response:
    """Allow or forbid anything leaving this Project's platform boundary.

    `manage`, and NOT the Change Set ceremony. A Change Set exists to review the
    per-Datastream impact of a capability version before it activates; this
    posture compiles into no Datastream and has no impact to review. Routing it
    through that flow would make an empty impact report the price of a switch,
    and `project-settings.md` already forbids a settings surface that becomes a
    generic editor of everything.

    `proactive-assertions.md` decision 2 owns the default -- `forbidden` -- and
    the fact that it exists at all.
    """
    scope = await _authorize(request, "manage")
    if isinstance(scope, Response):
        return scope
    actor, org_id = scope
    project_id = request.path_params["project_id"]
    try:
        from core.project_external_sharing import (  # noqa: PLC0415
            ExternalSharingValidationError,
            set_external_sharing,
        )

        body = await _body(request)
        state = body.get("external_sharing")
        if not isinstance(state, str):
            raise ExternalSharingValidationError(
                "external sharing is either allowed or forbidden"
            )
        idempotency_key = (
            request.headers.get("idempotency-key")
            or f"external-sharing:{project_id}:{state}"
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            posture = set_external_sharing(
                conn,
                org_id=org_id,
                project_id=project_id,
                state=state,
                actor=actor,
                idempotency_key=idempotency_key,
            )
            conn.commit()
        return _no_store(JSONResponse(posture, status_code=200))
    except Exception as exc:
        from core.project_external_sharing import (  # noqa: PLC0415
            ExternalSharingValidationError,
        )

        if isinstance(exc, ExternalSharingValidationError):
            return JSONResponse(
                {"code": exc.code, "message": str(exc)}, status_code=422
            )
        return _error(exc)


async def _create_change_set(request: Request) -> Response:
    scope = await _authorize(request, "edit")
    if isinstance(scope, Response):
        return scope
    actor, _org_id = scope
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            status_code=422,
        )
    try:
        body = await _body(request)
        # Intent only. Owner references, coverage, blockers, exceptions and hashes
        # are compiled by the server during prepare; accepting them here would
        # make the authority boundary a shape check rather than a boundary.
        if not body or not set(body).issubset({"intent"}):
            raise ProjectSettingsValidationError("Unsupported Change Set fields")
        from core.db import get_connection

        with get_connection() as conn:
            result = create_change_set(
                conn,
                project_id=request.path_params["project_id"],
                intent=body.get("intent"),
                actor=actor,
                idempotency_key=key,
            )
            conn.commit()
        return JSONResponse(result, status_code=200 if result["idempotent_replay"] else 201)
    except Exception as exc:
        return _error(exc)


async def _prepare_change_set(request: Request) -> Response:
    scope = await _authorize(request, "edit")
    if isinstance(scope, Response):
        return scope
    try:
        body = await _body(request)
        if body:
            raise ProjectSettingsValidationError("Prepare body must be empty")
        from core.db import get_connection
        from core.main import get_loaded_modules

        with get_connection() as conn:
            result = prepare_change_set(
                conn,
                project_id=request.path_params["project_id"],
                change_set_id=request.path_params["change_set_id"],
                actor=scope[0],
                loaded_modules=get_loaded_modules(),
            )
            conn.commit()
        return _no_store(JSONResponse(result, status_code=200))
    except Exception as exc:
        return _no_store(_error(exc))


async def _get_change_set_impact(request: Request) -> Response:
    """Read the frozen impact of one prepared Change Set. Never recompiles."""
    scope = await _authorize(request, "view")
    if isinstance(scope, Response):
        return scope
    try:
        from core.db import get_connection

        with get_connection() as conn:
            result = read_change_set_impact(
                conn,
                project_id=request.path_params["project_id"],
                change_set_id=request.path_params["change_set_id"],
            )
        return _no_store(JSONResponse(result, status_code=200))
    except Exception as exc:
        return _no_store(_error(exc))


async def _get_capability_datastreams(request: Request) -> Response:
    """The Project/capability projection: exact per-Datastream rows and coverage."""
    scope = await _authorize(request, "view")
    if isinstance(scope, Response):
        return scope
    try:
        from core.capability_proposals import read_project_capability
        from core.db import get_connection

        with get_connection() as conn:
            result = read_project_capability(
                conn,
                project_id=request.path_params["project_id"],
                capability_key=request.path_params["capability_key"],
            )
        return _no_store(JSONResponse(result, status_code=200))
    except Exception as exc:
        return _no_store(_error(exc))


async def _get_datastream_capabilities(request: Request) -> Response:
    """The Datastream/capability projection consumed by the Workbench."""
    scope = await _authorize(request, "view")
    if isinstance(scope, Response):
        return scope
    try:
        from core.capability_proposals import read_datastream_capabilities
        from core.db import get_connection

        with get_connection() as conn:
            result = read_datastream_capabilities(
                conn,
                project_id=request.path_params["project_id"],
                datastream_id=request.path_params["datastream_id"],
            )
        return _no_store(JSONResponse(result, status_code=200))
    except Exception as exc:
        return _no_store(_error(exc))


async def _issue_confirmation(request: Request) -> Response:
    scope = await _authorize(request, "manage")
    if isinstance(scope, Response):
        return scope
    _actor, org_id = scope
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            status_code=422,
        )
    from core.admin_api import _check_canonical_principal

    authorized, principal = await _check_canonical_principal(request)
    if not authorized or principal is None:
        return JSONResponse(
            {"code": "unauthorized", "message": "Canonical identity required"}, status_code=401
        )
    try:
        from core.db import get_connection

        with get_connection() as conn:
            issued = issue_change_confirmation(
                conn,
                project_id=request.path_params["project_id"],
                org_id=org_id,
                change_set_id=request.path_params["change_set_id"],
                actor_person_id=principal.person_id,
                idempotency_key=key,
            )
            # `get_connection` closes without committing, and psycopg rolls back
            # on close: without this the caller receives a 201 and a raw secret
            # for a row that never landed, and its confirm answers
            # `confirmation_invalid` -- a lost write wearing a refusal's clothes.
            conn.commit()
        return _no_store(
            JSONResponse(
                {
                    "confirmation_id": issued.confirmation_id,
                    "confirmation_secret": issued.confirmation_secret,
                    "expires_at": issued.expires_at.isoformat(),
                    "prepared_payload_hash": issued.prepared_payload_hash,
                },
                status_code=201,
            )
        )
    except Exception as exc:
        return _no_store(_error(exc))


async def _confirm_change_set(request: Request) -> Response:
    scope = await _authorize(request, "manage")
    if isinstance(scope, Response):
        return scope
    _actor, org_id = scope
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return JSONResponse(
            {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
            status_code=422,
        )
    from core.admin_api import _check_canonical_principal

    authorized, principal = await _check_canonical_principal(request)
    if not authorized or principal is None:
        return JSONResponse(
            {"code": "unauthorized", "message": "Canonical identity required"}, status_code=401
        )
    try:
        body = await _body(request)
        if not set(body).issubset(
            {"confirmation_id", "confirmation_secret", "prepared_payload_hash"}
        ):
            raise ProjectSettingsValidationError("Unsupported confirmation fields")
        confirmation_id = str(body.get("confirmation_id") or "").strip()
        secret = str(body.get("confirmation_secret") or "").strip()
        prepared_payload_hash = str(body.get("prepared_payload_hash") or "").strip()
        if not confirmation_id or not secret or len(prepared_payload_hash) != 64:
            raise ProjectSettingsValidationError(
                "confirmation_id, confirmation_secret and prepared_payload_hash are required"
            )
        from core.db import get_connection
        from core.main import get_loaded_modules
        from core.tracing import current_trace_id_hex

        with get_connection() as conn:
            try:
                result = confirm_change_set(
                    conn,
                    project_id=request.path_params["project_id"],
                    org_id=org_id,
                    change_set_id=request.path_params["change_set_id"],
                    actor_person_id=principal.person_id,
                    confirmation_id=confirmation_id,
                    confirmation_secret=secret,
                    prepared_payload_hash=prepared_payload_hash,
                    idempotency_key=key,
                    host_context={"host": "toorow-console", "workspace_id": "console"},
                    trace_id=current_trace_id_hex(),
                    loaded_modules=get_loaded_modules(),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        # THE PUSH FOLLOWS THE COMMIT (AI-321, 2026-08-29): a Country fan-out
        # mints one candidate per Datastream inside the confirmation, and none
        # of them was ever pushed from this door.
        from core.queue import dispatch_after_commit  # noqa: PLC0415

        fan_out = result.get("country_plan_fan_out") or {}
        dispatch_after_commit(
            *[c.get("candidate_job_id") for c in (fan_out.get("candidates") or [])]
        )
        return _no_store(JSONResponse(result, status_code=200))
    except Exception as exc:
        return _no_store(_error(exc))


project_settings_routes = [
    Route("/api/projects/{project_id}/settings", endpoint=_get_settings, methods=["GET"]),
    Route(
        "/api/projects/{project_id}/settings/profile", endpoint=_patch_profile, methods=["PATCH"]
    ),
    Route(
        "/api/projects/{project_id}/settings/external-sharing",
        endpoint=_put_external_sharing,
        methods=["PUT"],
    ),
    Route(
        "/api/projects/{project_id}/settings/change-sets",
        endpoint=_create_change_set,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/prepare",
        endpoint=_prepare_change_set,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/impact",
        endpoint=_get_change_set_impact,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/confirmations",
        endpoint=_issue_confirmation,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/settings/change-sets/{change_set_id}/confirm",
        endpoint=_confirm_change_set,
        methods=["POST"],
    ),
    # Two narrow Data read projections. They exist because neither the Settings
    # read model nor the Workbench read model can answer "this capability across
    # every Datastream" or "every capability for this Datastream" on its own.
    Route(
        "/api/projects/{project_id}/capabilities/{capability_key}/datastreams",
        endpoint=_get_capability_datastreams,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/capabilities",
        endpoint=_get_datastream_capabilities,
        methods=["GET"],
    ),
]
