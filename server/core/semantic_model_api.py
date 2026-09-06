"""The one consequential command family of the Semantic Model (Story 49.3).

Four routes, one lifecycle: create a change set from an exact base, prepare it,
read it, confirm it. Reads stay on the Governance surface
(``governance_surface_api.py``); nothing here returns a collection.

What arrives from the browser is intent, exact base references and an
idempotency key. The diff, the coverage, the impact, the compiled artifacts and
the final state are computed server-side every time, because a client that could
supply them could also supply different ones.

Authorization is resolved before anything else and from the authenticated
identity only: a client-supplied ``org_id`` is never authority, and the
Organization is derived from the authorized Project.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.db import request_connection
from core.project_access import resolve_strict_resource_access
from core.semantic_model import (
    SemanticNotFound,
    SemanticRefused,
    SemanticStale,
    confirm_change_set,
    create_change_set,
    get_change_set,
    prepare_change_set,
)

logger = logging.getLogger(__name__)

#: Reading Governance needs `view`; drafting and publishing meaning needs `edit`.
#: Publishing is not an administrative act, so it is not raised to `manage` —
#: but it is never available to a viewer either.
_WRITE_CAPABILITY = "edit"

MAX_BODY_BYTES = 512 * 1024


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    return response


def _json_error(code: str, message: str, status: int, **extra) -> Response:
    return _no_store(JSONResponse({"code": code, "message": message, **extra}, status_code=status))


def _not_found() -> Response:
    """Existence-hiding, identically to the read surface. A change set of another
    Project and one that never existed are indistinguishable from here."""
    return _json_error("not_found", "Semantic Model object not found", 404)


async def _body(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("request body too large")
    if not raw:
        return {}
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("request body must be an object")
    return payload


async def _authorize(request: Request, capability: str):
    """Returns (project_id, actor, org_id) or a Response to return instead."""
    from core.admin_api import _check_auth  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return _json_error("unauthorized", "Bearer token required", 401)
    project_id = (request.path_params.get("project_id") or "").strip()
    if not project_id:
        return _not_found()
    return project_id, identity or "anonymous", capability


async def _with_project(request: Request, capability: str, handler):

    resolved = await _authorize(request, capability)
    if isinstance(resolved, Response):
        return resolved
    project_id, actor, _ = resolved
    try:
        with request_connection(actor) as conn:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=project_id,
                minimum_capability=capability,
                hold_access=True,
            )
            if not decision.allowed:
                if decision.reason == "insufficient_capability":
                    return _json_error(
                        "denied",
                        "Your access to this Project does not include editing its "
                        "Semantic Model.",
                        403,
                    )
                if decision.reason == "access_unavailable":
                    return _json_error(
                        "semantic_model_access_unavailable",
                        "Semantic Model is unavailable",
                        503,
                    )
                return _not_found()
            # Derived from the AUTHORIZED Project. A client `org_id` is ignored.
            org_id = decision.org_id or ""
            if not org_id:
                return _not_found()
            response = await handler(conn, project_id, actor, org_id)
            # NOTHING ON THIS SURFACE EVER REACHED THE DATABASE. `get_connection`
            # does not commit -- its own docstring says so, and closing rolls the
            # transaction back -- and neither this seam nor `core.semantic_model`
            # called `commit` even once. So create, prepare and confirm all
            # answered success over writes that vanished: the console's "New
            # Semantic View" announced a View it had not created, and a Project
            # could not hold one. Measured 2026-08-12 in production: a change set
            # created with a 201 and an id was absent from the table a second
            # later, and the Project carried zero Semantic Views.
            #
            # Only a successful answer commits: a refusal built its response from
            # a partial write, and committing that would persist the half of it
            # the refusal exists to reject.
            if getattr(response, "status_code", 500) < 400:
                conn.commit()
            return response
    except SemanticNotFound:
        return _not_found()
    except SemanticStale as exc:
        # 409: the request was well formed and authorized, and the world moved.
        return _no_store(JSONResponse(exc.as_dict(), status_code=409))
    except SemanticRefused as exc:
        return _no_store(JSONResponse(exc.as_dict(), status_code=422))
    except ValueError as exc:
        return _json_error("malformed_request", str(exc), 400)
    except Exception as exc:  # fail closed without disclosing existence
        # THE ANSWER STAYS OPAQUE; THE LOG MUST NOT. This logged the exception
        # CLASS alone, so a `CheckViolation` -- the database naming the exact
        # constraint that refused -- reached the operator as "unavailable" and
        # reached the log as the word "CheckViolation". Nothing could be
        # diagnosed from either. The 503 is deliberate and unchanged: it is the
        # response that must disclose nothing, not the record of what happened.
        logger.warning(
            "Semantic Model command unavailable project=%s: %s: %s",
            request.path_params.get("project_id"),
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        return _json_error("semantic_model_unavailable", "Semantic Model is unavailable", 503)


async def _create(request: Request) -> Response:
    payload = await _body(request)

    async def handler(conn, project_id, actor, org_id):
        change_set = create_change_set(
            conn,
            project_id,
            actor=actor,
            object_type=str(payload.get("object_type") or ""),
            object_id=payload.get("object_id"),
            base_version_id=payload.get("base_version_id"),
            intent=payload.get("intent") or {},
            idempotency_key=str(payload.get("idempotency_key") or ""),
        )
        return _no_store(JSONResponse(change_set.as_dict(), status_code=201))

    return await _with_project(request, _WRITE_CAPABILITY, handler)


async def _read(request: Request) -> Response:
    async def handler(conn, project_id, actor, org_id):
        change_set_id = (request.path_params.get("change_set_id") or "").strip()
        return _no_store(
            JSONResponse(get_change_set(conn, project_id, change_set_id).as_dict(), status_code=200)
        )

    # Reading your own change set needs only `view`: it discloses nothing the
    # Governance collection does not already show to the same person.
    return await _with_project(request, "view", handler)


async def _prepare(request: Request) -> Response:
    payload = await _body(request)

    async def handler(conn, project_id, actor, org_id):
        change_set_id = (request.path_params.get("change_set_id") or "").strip()
        prepared = prepare_change_set(
            conn,
            project_id,
            change_set_id,
            actor=actor,
            allow_test_override=payload.get("test_gate_override"),
        )
        return _no_store(JSONResponse(prepared, status_code=200))

    return await _with_project(request, _WRITE_CAPABILITY, handler)


async def _confirm(request: Request) -> Response:
    payload = await _body(request)

    async def handler(conn, project_id, actor, org_id):
        change_set_id = (request.path_params.get("change_set_id") or "").strip()
        token = str(payload.get("confirmation_token") or "")
        if not token:
            return _json_error(
                "missing_confirmation",
                "Confirming a publication requires the single-use token returned by "
                "prepare.",
                400,
            )
        result = confirm_change_set(
            conn,
            project_id,
            change_set_id,
            actor=actor,
            confirmation_token=token,
            org_id=org_id,
            trace_id=request.headers.get("x-trace-id"),
        )
        return _no_store(JSONResponse(result, status_code=200))

    return await _with_project(request, _WRITE_CAPABILITY, handler)


_ROOT = "/api/projects/{project_id}/governance/semantic-model/change-sets"

SEMANTIC_MODEL_ROUTES = [
    Route(f"{_ROOT}/{{change_set_id}}/prepare", _prepare, methods=["POST"],
          name="semantic-change-set-prepare"),
    Route(f"{_ROOT}/{{change_set_id}}/confirm", _confirm, methods=["POST"],
          name="semantic-change-set-confirm"),
    Route(f"{_ROOT}/{{change_set_id}}", _read, methods=["GET"], name="semantic-change-set"),
    Route(_ROOT, _create, methods=["POST"], name="semantic-change-set-create"),
]
