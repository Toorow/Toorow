"""toorow -- the door of the declared entity type (story 68.1).

Exports ENTITY_TYPE_ROUTES: list[Route] -- a flat list `admin_api.py` splices
into its router at startup. Never imported by `admin_api` at module level, the
pattern of `mdm_common_keys_api.py`.

  GET  /api/projects/{project_id}/master-data/entity-types
  POST /api/projects/{project_id}/master-data/entity-types

ONE WRITER, TWO DOORS. The POST and the MCP tool `declare_entity_type` call
the same `core.object_kind_registry.declare_entity_type`; this module owns no
validation and no storage. A second door that re-implements the declaration is
how a replay and a real duplicate become indistinguishable in one of them.

ANSWERS THAT ARE NOT THE SAME, kept apart exactly as the common-key door does:

  200 + `empty_reason`   nobody has declared a type yet. A fact about the
                         vocabulary, not about the address.
  200 + `replayed`       the SAME declaration was sent again: the existing
                         type is returned, nothing was written. A fresh
                         declaration answers 201 -- the two are distinguishable.
  404                    foreign, denied or nonexistent, indistinguishably. A
                         caller of another org must not learn a type exists.
  409 `entity_type_exists`
                         the kind is already declared HERE with a different
                         key or label. A conflict with the world, not a
                         malformed request -- the answer names the kind and
                         the registry that holds it.
  422                    the declaration itself is malformed, with its code.
  503                    the store could not be read. "I could not look" is not
                         "there is nothing".
"""

from __future__ import annotations

import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/master-data/entity-types"

#: The empty answer's reason, in the vocabulary of the person reading it: it
#: names the gesture, not a table and not a deployment state.
_EMPTY_REASON = {
    "code": "no_entity_type_declared",
    "message": (
        "No entity type has been declared yet. An entity type names a business "
        "object this Project reconciles -- declared here with its canonical "
        "key, before any source feeds it."
    ),
}


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _refused(exc) -> Response:
    return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)


def _unavailable(what: str) -> Response:
    return JSONResponse(
        {
            "code": "entity_types_unavailable",
            "message": (
                f"The {what} could not be read, so what this Project declares is "
                "unknown. This is not a count of zero."
            ),
        },
        status_code=503,
    )


def _guard(
    project_id: str, identity: str, minimum_capability: str = "view"
) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may read it.

    A project the guarded org does not own answers 404 and never 403 -- the
    same posture `mdm_common_keys_api._guard` applies, for the same reason.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability=minimum_capability,
                hold_access=minimum_capability == "edit",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("entity_types_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    if not decision.allowed or not decision.org_id:
        return None, _not_found()
    return str(decision.org_id), None


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


def _jsonable(value: Any) -> Any:
    """Timestamps to ISO strings; the registry row carries datetimes."""
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


async def _list_types(request: Request) -> Response:
    """GET {base} -- every declared entity type, with its feeding state."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.object_kind_registry import list_entity_types  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            types = list_entity_types(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("entity_types_api: list failed project=%s: %s", project_id, exc)
        return _unavailable("entity types of this Project")

    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "entity_types": types,
            "empty_reason": _EMPTY_REASON if not types else None,
        }
    )


async def _declare_type(request: Request) -> Response:
    """POST {base} -- declare an entity type, or return the replayed one."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.master_data import MasterDataError, MasterDataNotFound  # noqa: PLC0415
    from core.object_kind_registry import (  # noqa: PLC0415
        EntityTypeExists,
        declare_entity_type,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            declared = declare_entity_type(
                conn,
                org_id=org_id,
                project_id=project_id,
                object_kind=body.get("object_kind"),
                canonical_key=body.get("canonical_key"),
                display_name=body.get("display_name"),
                actor=identity,
            )
            conn.commit()
    except EntityTypeExists as exc:
        # A CONFLICT, not a malformed request: the caller sent something valid
        # and the world says no. 409 is what a screen turns into "open the
        # existing declaration".
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
    except MasterDataNotFound:
        return _not_found()
    except MasterDataError as exc:
        return _refused(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("entity_types_api: declare failed project=%s: %s", project_id, exc)
        return _unavailable("entity type store")

    entity_type = _jsonable(declared["registry"])
    entity_type["display_name"] = entity_type.get("label")
    status = 200 if declared["replayed"] else 201
    return JSONResponse(
        {
            "project_id": project_id,
            "entity_type": entity_type,
            "replayed": declared["replayed"],
        },
        status_code=status,
    )


ENTITY_TYPE_ROUTES: list[Route] = [
    Route(_BASE, _list_types, methods=["GET"]),
    Route(_BASE, _declare_type, methods=["POST"]),
]

__all__ = ["ENTITY_TYPE_ROUTES"]
