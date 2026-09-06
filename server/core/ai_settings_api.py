"""The AI settings doors: one per scope, and both say where a value came from.

Story 75-4. Six routes, three per scope family, and the same three verbs on
each: read the resolution, state this scope's override, give it back to the
parent.

WHY THE READ RETURNS THREE THINGS AND NOT ONE. `resolved` is what an agent
obeys; `sources` is which scope decided each field; `own` and `inherited` are
the raw overrides of this scope and of its parent. A panel that only received
the resolution could show a value but never the sentence beside it -- "Set
here" / "Inherited from organization" / "Platform default" -- and the whole
point of this object is that inheritance is VISIBLE.

THE DELETE IS A CLEARING, NOT A DELETION. `DELETE .../ai-settings` appends a
`cleared` version (`ai_settings.clear_settings`) and returns it. Nothing in this
object is ever removed; the verb is the HTTP one because "give this scope back
to its parent" is what a reader means by deleting an override.

AUTHORIZATION. Project routes reuse the Analyze seam verbatim -- `viewer` reads,
`member` writes, the org resolved once, the connection ARMED with the Epic-36
floor (`request_connection`). Org routes use the org seam: a member reads, a
manager writes. Neither door lets a caller name an organization it does not hold.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.ai_settings import (
    SCOPE_ORG,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    AiSettingsRefused,
    clear_settings,
    history,
    platform_defaults,
    project_org_id,
    read_scope,
    resolve,
    set_settings,
)

logger = logging.getLogger("uvicorn.error")

_NOT_FOUND = {"code": "not_found", "message": "Not found"}


def _refused(exc: AiSettingsRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _invalid_body(exc: Exception) -> Response:
    return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)


# ---------------------------------------------------------------------------
# The Project family.
# ---------------------------------------------------------------------------


async def _authorize_project(request: Request, role: str):
    """(identity, org_id, project_id) or the Response that refuses."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            return denied
        # The SQL lives in the service module: a door issues none of its own
        # (`module-boundaries.md` criterion 4, `scripts/api_sql_census.py --gate`).
        org_id = project_org_id(conn, project_id)
    if org_id is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), org_id, str(project_id)


def _project_payload(conn, *, org_id: str, project_id: str) -> dict:
    resolved = resolve(conn, org_id=org_id, project_id=project_id)
    return {
        "scope": SCOPE_PROJECT,
        "scope_id": project_id,
        "org_id": org_id,
        "resolved": resolved["values"],
        "sources": resolved["sources"],
        "own": read_scope(conn, scope=SCOPE_PROJECT, scope_id=project_id, org_id=org_id),
        "inherited": read_scope(conn, scope=SCOPE_ORG, scope_id=org_id, org_id=org_id),
        "platform_defaults": platform_defaults(),
        "history": history(conn, scope=SCOPE_PROJECT, scope_id=project_id, org_id=org_id),
    }


async def _get_project_ai_settings(request: Request) -> Response:
    """GET /api/projects/{project_id}/ai-settings -- resolved, sourced, and both overrides."""
    from core.db import request_connection  # noqa: PLC0415

    auth = await _authorize_project(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    with request_connection(identity) as conn:
        return JSONResponse(_project_payload(conn, org_id=org_id, project_id=project_id))


async def _put_project_ai_settings(request: Request) -> Response:
    """PUT /api/projects/{project_id}/ai-settings -- state this project's override."""
    from core.db import request_connection  # noqa: PLC0415

    auth = await _authorize_project(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _invalid_body(exc)
    try:
        with request_connection(identity) as conn:
            set_settings(
                conn, scope=SCOPE_PROJECT, scope_id=project_id, org_id=org_id,
                payload=body, actor=identity, note=body.get("note"),
            )
            payload = _project_payload(conn, org_id=org_id, project_id=project_id)
            conn.commit()
    except AiSettingsRefused as exc:
        return _refused(exc)
    return JSONResponse(payload)


async def _delete_project_ai_settings(request: Request) -> Response:
    """DELETE /api/projects/{project_id}/ai-settings -- give it back to the organization."""
    from core.db import request_connection  # noqa: PLC0415

    auth = await _authorize_project(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id, project_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _invalid_body(exc)
    try:
        with request_connection(identity) as conn:
            clear_settings(
                conn, scope=SCOPE_PROJECT, scope_id=project_id, org_id=org_id,
                actor=identity, note=body.get("note"),
            )
            payload = _project_payload(conn, org_id=org_id, project_id=project_id)
            conn.commit()
    except AiSettingsRefused as exc:
        return _refused(exc)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# The Organization family. Same three verbs, one scope up.
# ---------------------------------------------------------------------------


async def _authorize_org(request: Request, *, manage: bool):
    """(identity, org_id) or the Response that refuses.

    A non-member gets 404 rather than 403: existence is never disclosed, which is
    the posture every other organization route takes (7.4).
    """
    from core.admin_api import _check_auth  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    org_id = request.path_params["org_id"]
    subject = identity or "anonymous"
    with get_connection() as conn:
        if not identity_has_org_access(org_id, subject, conn):
            return JSONResponse(
                {"code": "not_found", "message": "organization not found"}, 404
            )
        if manage and not identity_can_manage_org(org_id, subject, conn):
            return JSONResponse(
                {
                    "code": "forbidden",
                    "message": "Changing the organization's AI settings needs an "
                               "owner or an admin of this organization.",
                },
                403,
            )
    return str(identity), str(org_id)


def _org_payload(conn, *, org_id: str) -> dict:
    resolved = resolve(conn, org_id=org_id, project_id=None)
    return {
        "scope": SCOPE_ORG,
        "scope_id": org_id,
        "org_id": org_id,
        "resolved": resolved["values"],
        "sources": resolved["sources"],
        "own": read_scope(conn, scope=SCOPE_ORG, scope_id=org_id, org_id=org_id),
        "inherited": read_scope(conn, scope=SCOPE_PLATFORM, scope_id=None),
        "platform_defaults": platform_defaults(),
        "history": history(conn, scope=SCOPE_ORG, scope_id=org_id, org_id=org_id),
    }


async def _get_org_ai_settings(request: Request) -> Response:
    """GET /api/organizations/{org_id}/ai-settings."""
    from core.db import request_connection  # noqa: PLC0415

    auth = await _authorize_org(request, manage=False)
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    with request_connection(identity) as conn:
        return JSONResponse(_org_payload(conn, org_id=org_id))


async def _put_org_ai_settings(request: Request) -> Response:
    """PUT /api/organizations/{org_id}/ai-settings."""
    from core.db import request_connection  # noqa: PLC0415

    auth = await _authorize_org(request, manage=True)
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _invalid_body(exc)
    try:
        with request_connection(identity) as conn:
            set_settings(
                conn, scope=SCOPE_ORG, scope_id=org_id, org_id=org_id,
                payload=body, actor=identity, note=body.get("note"),
            )
            payload = _org_payload(conn, org_id=org_id)
            conn.commit()
    except AiSettingsRefused as exc:
        return _refused(exc)
    return JSONResponse(payload)


async def _delete_org_ai_settings(request: Request) -> Response:
    """DELETE /api/organizations/{org_id}/ai-settings -- back to the platform values."""
    from core.db import request_connection  # noqa: PLC0415

    auth = await _authorize_org(request, manage=True)
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return _invalid_body(exc)
    try:
        with request_connection(identity) as conn:
            clear_settings(
                conn, scope=SCOPE_ORG, scope_id=org_id, org_id=org_id,
                actor=identity, note=body.get("note"),
            )
            payload = _org_payload(conn, org_id=org_id)
            conn.commit()
    except AiSettingsRefused as exc:
        return _refused(exc)
    return JSONResponse(payload)


#: `ai-settings` is a LITERAL last segment under two address families whose other
#: members are literal too, so it captures nothing and nothing captures it.
AI_SETTINGS_ROUTES = [
    Route("/api/projects/{project_id}/ai-settings", _get_project_ai_settings, methods=["GET"]),
    Route("/api/projects/{project_id}/ai-settings", _put_project_ai_settings, methods=["PUT"]),
    Route(
        "/api/projects/{project_id}/ai-settings",
        _delete_project_ai_settings,
        methods=["DELETE"],
    ),
    Route("/api/organizations/{org_id}/ai-settings", _get_org_ai_settings, methods=["GET"]),
    Route("/api/organizations/{org_id}/ai-settings", _put_org_ai_settings, methods=["PUT"]),
    Route(
        "/api/organizations/{org_id}/ai-settings",
        _delete_org_ai_settings,
        methods=["DELETE"],
    ),
]
