"""Story 75-6 -- the presentation-extends REST family, thin over one store.

Provides PRESENTATION_EXTENDS_ROUTES: a flat list `admin_api.py` imports at module
level (`core/admin_api.py:293`) and splices into its router -- the same pattern as
`metric_semantics_api.py:253` and `dimension_lineage_api.py:160`. What this module
imports at module level in return is only its OWN store; every seam that would
make the import graph a cycle -- `core.admin_api`, `core.db`, `core.mcp_scope`,
`core.project_access` -- is imported inside the function that uses it.

Routes:
  GET    /api/presentation-extends           the RESOLVED block plus this scope's own row
  GET    /api/presentation-extends/history   every version of one scope, newest first
  PUT    /api/presentation-extends           set the display of one scope (a new version)
  DELETE /api/presentation-extends           clear one scope (a CLEARED version, not a DELETE)

IT IS THE SIBLING OF THE CLIENT-LABEL DOOR, AND READS LIKE IT.
`core/dimension_lineage_api.py` is the door onto the one presentation value that
already rode the cascade, and every posture here is its posture: reads require org
membership, writes require org-manage, PLATFORM is refused because the definition
is the authority there, and denied / absent / unavailable wear ONE envelope so two
answers side by side cannot enumerate the platform.

WHAT IS NOT FORKED. The authorization QUESTIONS are asked of
`core.project_access.identity_has_org_access` and `identity_can_manage_org` -- the
same two functions the sibling asks, in the same order -- and the non-disclosing
sentence comes from `core.mcp_scope`, imported and never retyped. What is NOT
shared is the 403's sentence, and deliberately: a refusal must name the gesture
that repairs, and the gesture here is dressing a number, not naming a dimension.

NO RULE LIVES HERE. Every verdict -- what a display block may carry, which scope
wins which leaf, whether clearing is a version -- belongs to
`core.presentation_extends`. This file translates HTTP and nothing else.

ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.presentation_extends import (
    OBJECT_TYPES,
    SCOPE_ORG,
    SCOPE_PLATFORM,
    SCOPE_PROJECT,
    WRITABLE_SCOPES,
    PresentationNotFound,
    PresentationRefused,
    application_notes,
    clear_override,
    history,
    org_of_project,
    read_override,
    resolve_display,
    set_override,
)

logger = logging.getLogger(__name__)


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication required."}, status_code=401
    )


def _server_error() -> Response:
    """A failure AFTER the guard passed: the caller is entitled to be here."""
    return JSONResponse({"code": "server_error", "message": "Server error."}, status_code=500)


def _unreachable() -> Response:
    """ONE envelope for "you may not", "it does not exist" and "it could not be checked".

    Two of them side by side are an oracle: a caller reading 403 for a project of a
    neighbouring organization and 404 for an id nobody created has just learned
    which projects exist. The words are the MODEL door's own
    (`core.mcp_scope`), imported so two doors onto one state cannot refuse in two
    vocabularies.
    """
    from core.mcp_scope import (  # noqa: PLC0415
        ORG_NOT_FOUND_CODE,
        ORG_NOT_FOUND_MESSAGE,
    )

    return JSONResponse(
        {"code": ORG_NOT_FOUND_CODE, "message": ORG_NOT_FOUND_MESSAGE}, status_code=404
    )


def _needs_manage() -> Response:
    """The one refusal that is not an existence question, so it may name its gesture.

    Reached only by a PROVEN MEMBER of the organization addressed -- membership is
    asked first, below -- so it discloses nothing, and it is the only refusal a
    person can act on.
    """
    return JSONResponse(
        {
            "code": "forbidden",
            "message": (
                "Choosing how a number is shown is an owner's or an admin's gesture. "
                "Ask one of the owners or admins of this organization to set it."
            ),
        },
        status_code=403,
    )


def _guard(org_id: str | None, project_id: str | None, identity: str, *, manage: bool):
    """`(org_id, None)` when the caller may act here, `(None, Response)` otherwise.

    THE ORDER IS THE WHOLE GUARD, and it is the sibling's order: resolve the org
    from the project when it was not given, verify MEMBERSHIP, verify the project
    belongs to that org, and only then -- for a write -- ask whether the member is
    an owner or an admin. Every other outcome leaves through `_unreachable`. An
    authorization that cannot be evaluated is not an authorization granted.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    guard_org_id = org_id
    try:
        with get_connection() as conn:
            if project_id is not None:
                # The SQL of this door lives in the service (criterion 4 of
                # `module-boundaries.md`): a route module parses, authorizes and
                # calls -- it executes nothing.
                project_org_id = org_of_project(conn, project_id=project_id)
                if not project_org_id:
                    return None, _unreachable()
                if guard_org_id is None:
                    guard_org_id = project_org_id
                elif project_org_id != guard_org_id:
                    return None, _unreachable()
            if guard_org_id is None:
                return None, _unreachable()
            if not identity_has_org_access(guard_org_id, identity, conn):
                return None, _unreachable()
            if manage and not identity_can_manage_org(guard_org_id, identity, conn):
                return None, _needs_manage()
    except Exception as exc:  # noqa: BLE001 -- refuse on an unreachable seam
        logger.error("presentation_extends_api: guard failed: %s", type(exc).__name__)
        return None, _unreachable()
    return guard_org_id, None


def _invalid(code: str, message: str, status: int = 422) -> Response:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _object_params(source) -> tuple[str, str] | Response:
    object_type = (source.get("object_type") or "").strip()
    object_id = (source.get("object_id") or "").strip()
    if object_type not in OBJECT_TYPES:
        return _invalid(
            "invalid_param",
            "object_type must be one of: " + ", ".join(OBJECT_TYPES) + ".",
        )
    if not object_id:
        return _invalid("missing_param", "object_id is required.")
    return object_type, object_id


def _write_scope(source) -> tuple[str, str | None, str | None] | Response:
    """`(scope_level, org_id, project_id)` for a write, or the refusal."""
    scope_level = (source.get("scope_level") or "").strip().upper()
    org_id = (source.get("org_id") or "").strip() or None
    project_id = (source.get("project_id") or "").strip() or None
    if scope_level == SCOPE_PLATFORM:
        return JSONResponse(
            {
                "code": "forbidden",
                "message": (
                    "The platform scope carries the definition and cannot be "
                    "overridden through the API."
                ),
            },
            status_code=403,
        )
    if scope_level not in WRITABLE_SCOPES:
        return _invalid("invalid_param", "scope_level must be ORG or PROJECT.")
    if scope_level == SCOPE_PROJECT and project_id is None:
        return _invalid("missing_param", "project_id is required.")
    if scope_level == SCOPE_ORG and org_id is None:
        return _invalid("missing_param", "org_id is required.")
    return scope_level, org_id, project_id


# ---------------------------------------------------------------------------
# GET /api/presentation-extends
# ---------------------------------------------------------------------------


async def _get_presentation(request: Request) -> Response:
    """GET /api/presentation-extends?object_type=&object_id=[&org_id=][&project_id=]

    THREE answers in one, and they are NOT the same answer: `resolved` is the
    block as the cascade merges it (definition, then organization, then project,
    leaf by leaf, each saying where it came from); `own` is the row stored AT the
    scope addressed -- which may be nothing, or may be a clear; and `application`
    says, section by section, whether a served figure actually carries the value
    and, when it does not, WHY. A screen that showed only the first could not tell
    an inherited value from a chosen one, and a caller reading only the first two
    would believe a colour it stored was on its charts -- it is not, and the
    reason is a ratified arbitration, not an omission.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    params = request.query_params
    parsed = _object_params(params)
    if isinstance(parsed, Response):
        return parsed
    object_type, object_id = parsed

    org_id = (params.get("org_id") or "").strip() or None
    project_id = (params.get("project_id") or "").strip() or None
    guard_org_id, denial = _guard(org_id, project_id, identity, manage=False)
    if denial is not None:
        return denial

    scope_level = (params.get("scope_level") or "").strip().upper() or None
    if scope_level is not None and scope_level not in WRITABLE_SCOPES:
        return _invalid("invalid_param", "scope_level must be ORG or PROJECT.")

    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            resolved = resolve_display(
                conn,
                org_id=guard_org_id,
                project_id=project_id,
                object_type=object_type,
                object_id=object_id,
            )
            own = None
            if scope_level is not None:
                own = read_override(
                    conn,
                    org_id=guard_org_id,
                    project_id=project_id if scope_level == SCOPE_PROJECT else None,
                    scope_level=scope_level,
                    object_type=object_type,
                    object_id=object_id,
                )
    except PresentationRefused as exc:
        return JSONResponse(exc.as_dict(), status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.error("presentation_extends_api: read failed: %s", type(exc).__name__)
        return _server_error()

    return JSONResponse(
        {
            "object_type": object_type,
            "object_id": object_id,
            "resolved": resolved,
            "own": own,
            # The verdict is the STORE's, not this door's: `application_notes` is
            # pure and lives beside the render consumer it describes, so the
            # sentence a caller reads and the branch a figure takes cannot drift.
            "application": application_notes(object_type=object_type, resolved=resolved),
        }
    )


# ---------------------------------------------------------------------------
# GET /api/presentation-extends/history
# ---------------------------------------------------------------------------


async def _get_history(request: Request) -> Response:
    """GET /api/presentation-extends/history?scope_level=&object_type=&object_id=[&...]

    Newest first, and a clear is one of the entries -- which is the whole reason
    clearing is a version and not a DELETE.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    params = request.query_params
    parsed = _object_params(params)
    if isinstance(parsed, Response):
        return parsed
    object_type, object_id = parsed
    scope = _write_scope(params)
    if isinstance(scope, Response):
        return scope
    scope_level, org_id, project_id = scope

    guard_org_id, denial = _guard(org_id, project_id, identity, manage=False)
    if denial is not None:
        return denial

    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            versions = history(
                conn,
                org_id=guard_org_id,
                project_id=project_id if scope_level == SCOPE_PROJECT else None,
                scope_level=scope_level,
                object_type=object_type,
                object_id=object_id,
            )
    except PresentationRefused as exc:
        return JSONResponse(exc.as_dict(), status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.error("presentation_extends_api: history failed: %s", type(exc).__name__)
        return _server_error()

    return JSONResponse({"scope_level": scope_level, "versions": versions})


# ---------------------------------------------------------------------------
# PUT /api/presentation-extends
# ---------------------------------------------------------------------------


async def _put_presentation(request: Request) -> Response:
    """PUT /api/presentation-extends

    body: {"scope_level": "ORG"|"PROJECT", "org_id"?, "project_id"?,
           "object_type": "semantic_concept"|"metric", "object_id": "...",
           "display": {...}, "note"?}

    PUT and not POST: one scope holds ONE presentation for one object, and sending
    it twice must leave one head. What is appended is a VERSION -- the head is
    idempotent, the ledger is not.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    try:
        body = json.loads(await request.body())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _invalid("invalid_json", "Invalid JSON body.", 400)
    if not isinstance(body, dict):
        return _invalid("invalid_json", "Invalid JSON body.", 400)

    parsed = _object_params(body)
    if isinstance(parsed, Response):
        return parsed
    object_type, object_id = parsed
    scope = _write_scope(body)
    if isinstance(scope, Response):
        return scope
    scope_level, org_id, project_id = scope

    guard_org_id, denial = _guard(org_id, project_id, identity, manage=True)
    if denial is not None:
        return denial

    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            written = set_override(
                conn,
                org_id=guard_org_id,
                project_id=project_id if scope_level == SCOPE_PROJECT else None,
                scope_level=scope_level,
                object_type=object_type,
                object_id=object_id,
                display=body.get("display"),
                identity=identity,
                note=(body.get("note") or None),
            )
            conn.commit()
    except PresentationRefused as exc:
        return JSONResponse(exc.as_dict(), status_code=422)
    except PresentationNotFound:
        return _unreachable()
    except Exception as exc:  # noqa: BLE001
        logger.error("presentation_extends_api: set failed: %s", type(exc).__name__)
        return _server_error()

    return JSONResponse(written, status_code=200)


# ---------------------------------------------------------------------------
# DELETE /api/presentation-extends
# ---------------------------------------------------------------------------


async def _delete_presentation(request: Request) -> Response:
    """DELETE /api/presentation-extends?scope_level=&object_type=&object_id=[&...]

    THE VERB IS DELETE AND THE WRITE IS AN APPEND, and that is not a contradiction:
    to the caller the override is gone and the parent is visible again, which is
    what DELETE means on this rail. In the ledger it is a version carrying
    `cleared = true`, so the history still says who stopped it and when.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    params = request.query_params
    parsed = _object_params(params)
    if isinstance(parsed, Response):
        return parsed
    object_type, object_id = parsed
    scope = _write_scope(params)
    if isinstance(scope, Response):
        return scope
    scope_level, org_id, project_id = scope

    guard_org_id, denial = _guard(org_id, project_id, identity, manage=True)
    if denial is not None:
        return denial

    from core.db import request_connection  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            cleared = clear_override(
                conn,
                org_id=guard_org_id,
                project_id=project_id if scope_level == SCOPE_PROJECT else None,
                scope_level=scope_level,
                object_type=object_type,
                object_id=object_id,
                identity=identity,
                note=(params.get("note") or None),
            )
            conn.commit()
    except PresentationRefused as exc:
        return JSONResponse(exc.as_dict(), status_code=422)
    except PresentationNotFound:
        return JSONResponse(
            {
                "code": "not_found",
                "message": "No presentation is stored at that scope for this object.",
            },
            status_code=404,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("presentation_extends_api: clear failed: %s", type(exc).__name__)
        return _server_error()

    return JSONResponse({"cleared": True, **cleared})


# ---------------------------------------------------------------------------
# Route list. The literal segment is declared before the bare collection, same
# rule as every sibling family: `/history` can never be read as a collection.
# ---------------------------------------------------------------------------

PRESENTATION_EXTENDS_ROUTES: list[Route] = [
    Route("/api/presentation-extends/history", _get_history, methods=["GET"]),
    Route("/api/presentation-extends", _get_presentation, methods=["GET"]),
    Route("/api/presentation-extends", _put_presentation, methods=["PUT"]),
    Route("/api/presentation-extends", _delete_presentation, methods=["DELETE"]),
]

#: The orchestrator mounts this name.
ROUTES = PRESENTATION_EXTENDS_ROUTES
