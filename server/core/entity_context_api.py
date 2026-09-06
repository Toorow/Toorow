"""toorow -- the console's door onto the entity reconciliation context (68.7).

Exports ENTITY_CONTEXT_ROUTES: list[Route] -- a flat list `admin_api.py`
splices into its router at startup. Never imported by `admin_api` at module
level, the pattern of `entity_types_api.py`.

  GET /api/projects/{project_id}/master-data/entity-reconciliation-context

ONE WRITER, TWO DOORS. This route and the MCP tool
`list_entity_reconciliation_context` serve the SAME
`core.object_kind_registry.describe_entity_reconciliation_context` -- the
AD-1 envelope is built there, and this door serializes it verbatim, so the
screen and the model read the same payload (AC3). A door that re-assembled
the sections is how "what may I cross?" starts answering differently
depending on who asks.

ANSWERS THAT ARE NOT THE SAME, kept apart exactly as the neighbour does:

  200 + the envelope   the context, each section `ok` with its counters or
                       `unavailable` with its named reason -- a dependency
                       not yet landed (68.3) is named, never zeroed.
  404                  foreign, denied or nonexistent, indistinguishably. A
                       caller of another org must not learn a type exists.
  503                  the store could not be read. "I could not look" is not
                       "there is nothing" (AC4).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/master-data/entity-reconciliation-context"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _unavailable() -> Response:
    return JSONResponse(
        {
            "code": "entity_context_unavailable",
            "message": (
                "The entity reconciliation context of this Project could not be "
                "read, so what it declares is unknown. This is not a count of zero."
            ),
        },
        status_code=503,
    )


def _guard(project_id: str, identity: str) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may read it.

    A project the guarded org does not own answers 404 and never 403 -- the
    same posture `entity_types_api._guard` applies, for the same reason, and
    an outage of the decision itself fails CLOSED onto that same answer.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability="view",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("entity_context_api: guard failed project=%s: %s", project_id, exc)
        return None, _not_found()

    if not decision.allowed or not decision.org_id:
        return None, _not_found()
    return str(decision.org_id), None


async def _read_context(request: Request) -> Response:
    """GET {base} -- the one discovery read, in the canonical envelope."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.object_kind_registry import (  # noqa: PLC0415
        describe_entity_reconciliation_context,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            envelope = describe_entity_reconciliation_context(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("entity_context_api: read failed project=%s: %s", project_id, exc)
        return _unavailable()

    return JSONResponse(envelope)


ENTITY_CONTEXT_ROUTES: list[Route] = [
    Route(_BASE, _read_context, methods=["GET"]),
]

__all__ = ["ENTITY_CONTEXT_ROUTES"]
