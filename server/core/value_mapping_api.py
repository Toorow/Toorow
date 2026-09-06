"""toorow -- REST surface of the client value mapping tables (Story 60.1).

Exports VALUE_MAPPING_ROUTES: list[Route] -- a flat list admin_api.py splices into
its router at startup. Never imported by admin_api at module level (no circular
import), the pattern of `dimension_lineage_api.py`.

Routes (all under one Project address, because that is where the surface lives):

  GET    /api/projects/{project_id}/value-mapping-tables
  POST   /api/projects/{project_id}/value-mapping-tables
  GET    /api/projects/{project_id}/value-mapping-tables/{table_id}
  PATCH  /api/projects/{project_id}/value-mapping-tables/{table_id}
  DELETE /api/projects/{project_id}/value-mapping-tables/{table_id}
  POST   /api/projects/{project_id}/value-mapping-tables/{table_id}/entries
  PATCH  /api/projects/{project_id}/value-mapping-tables/{table_id}/entries/{entry_id}
  DELETE /api/projects/{project_id}/value-mapping-tables/{table_id}/entries/{entry_id}
  POST   /api/projects/{project_id}/value-mapping-tables/{table_id}/import
  GET    /api/projects/{project_id}/value-mapping-tables/{table_id}/assignments
  POST   /api/projects/{project_id}/value-mapping-tables/{table_id}/assignments
  DELETE /api/projects/{project_id}/value-mapping-tables/{table_id}/assignments

FOUR REFUSALS THIS MODULE EXISTS FOR:

  * `PLATFORM` -> **403** `platform_scope_forbidden`. ORG and PROJECT only; seeds
    are the platform authority (`dimension_lineage_api.py:14-16`).
  * a `project_id` the guarded org does not own -> **404**, never 403:
    existence-hiding, lesson F-3 of 27.2 (`dimension_lineage_api.py:17-19`).
  * `PATCH` / `DELETE` without `acknowledge_impact` while the table is applied
    somewhere -> **409** `value_mapping_impact_not_acknowledged`, carrying the
    impact that was READ so the surface shows the same evidence the guard used
    (`master_data_commands.py:62-74,107-113`).
  * an impact read that FAILS -> **503** `value_mapping_impact_unavailable` with
    `impact_state: "unknown"` and NO assignment list of any kind. An empty list
    would say "nothing depends on this", which was not observed
    (`master_data.py:745-749`).

Every message a person reads is in English, like the rest of this repository.
The 409 template of `master_data_commands.py:62-74,107-113` is copied for its
SHAPE -- the code, the status and the acknowledgement -- never for its language.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/value-mapping-tables"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found(message: str = "Project not found.") -> Response:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _server_error() -> Response:
    return JSONResponse({"code": "server_error", "message": "Server error."}, status_code=500)


def _impact_unavailable(detail: str) -> Response:
    """The impact could not be read. Fail closed, and say `unknown` -- not zero."""
    return JSONResponse(
        {
            "code": "value_mapping_impact_unavailable",
            "message": (
                "The number of affected Datastreams could not be read. No list is "
                "shown and no change is applied: \"I could not check\" is not "
                "\"nothing depends on this\"."
            ),
            "impact_state": "unknown",
            "datastream_count": None,
            "detail": detail,
        },
        status_code=503,
    )


def _guard(project_id: str, identity: str, *, manage: bool) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may touch it.

    Same shape and the same reasons as `dimension_lineage_api._guard_org_and_project`
    (:14-19): reads require org membership, writes require org-manage, and a
    project that cannot be resolved is 404 rather than a fall-through to an
    unguarded read. It returns the org id because every store call below is
    org-scoped, which the shared guard does not expose.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                row = cur.fetchone()
            if not row or not row[0]:
                return None, _not_found()
            org_id = str(row[0])
            allowed = (
                identity_can_manage_org(org_id, identity, conn)
                if manage
                else identity_has_org_access(org_id, identity, conn)
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("value_mapping_api: guard failed project=%s: %s", project_id, exc)
        return None, _server_error()

    if not allowed:
        if manage:
            return None, JSONResponse(
                {"code": "forbidden", "message": "Insufficient rights."}, status_code=403
            )
        # A reader who is not a member must not learn the Project exists.
        return None, _not_found()
    return org_id, None


async def _body(request: Request) -> tuple[dict | None, Response | None]:
    try:
        parsed = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JSONResponse(
            {"code": "invalid_json", "message": "Invalid JSON body."}, status_code=400
        )
    if not isinstance(parsed, dict):
        return None, JSONResponse(
            {"code": "invalid_json", "message": "Invalid JSON body."}, status_code=400
        )
    return parsed, None


def _refusal(exc: Exception) -> Response | None:
    """Map a store refusal to its status code. Returns None when it is not one."""
    from core import value_mapping_tables as store  # noqa: PLC0415

    if isinstance(exc, store.ImpactNotAcknowledged):
        return JSONResponse(
            {
                "code": "value_mapping_impact_not_acknowledged",
                "message": str(exc),
                "impact": exc.impact,
            },
            status_code=409,
        )
    if isinstance(exc, store.ValueMappingUnavailable):
        return _impact_unavailable(str(exc))
    if isinstance(exc, store.ValueMappingNotFound):
        return _not_found("Value mapping table not found.")
    if isinstance(exc, store.ValueMappingConflict):
        return JSONResponse(
            {"code": "value_mapping_conflict", "message": str(exc)}, status_code=409
        )
    if isinstance(exc, store.InvalidValueMappingScope):
        return JSONResponse(
            {"code": "invalid_scope", "message": "Invalid scope."}, status_code=422
        )
    return None


def _scope_refusal(scope_level: str) -> Response | None:
    if scope_level == "PLATFORM":
        return JSONResponse(
            {
                "code": "platform_scope_forbidden",
                "message": (
                    "The PLATFORM scope cannot be modified through the API: seeds "
                    "are its authority."
                ),
            },
            status_code=403,
        )
    from core.value_mapping_tables import VALID_SCOPES  # noqa: PLC0415

    if scope_level not in VALID_SCOPES:
        return JSONResponse(
            {"code": "invalid_scope", "message": "scope_level must be ORG or PROJECT."},
            status_code=422,
        )
    return None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


async def _list_tables(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import list_tables  # noqa: PLC0415

    try:
        with get_connection() as conn:
            tables = list_tables(conn, org_id=org_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("value_mapping_api: list failed project=%s: %s", project_id, exc)
        return _server_error()
    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "tables": tables,
            # Stated rather than derived from the length of the list, so a screen
            # never has to infer "did the read happen" from "is the list empty".
            "impact_state": "known",
        }
    )


async def _create_table(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refused = await _body(request)
    if refused is not None:
        return refused

    scope_level = (body.get("scope_level") or "PROJECT").strip().upper()
    scope_refused = _scope_refusal(scope_level)
    if scope_refused is not None:
        return scope_refused

    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import create_table  # noqa: PLC0415

    name = (body.get("name") or "").strip()
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name is required."}, status_code=422
        )
    try:
        with get_connection() as conn:
            table = create_table(
                conn,
                org_id=org_id,
                project_id=project_id,
                scope_level=scope_level,
                name=name,
                description=(body.get("description") or None),
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: create failed project=%s: %s", project_id, exc)
        return _server_error()
    return JSONResponse(table, status_code=201)


# ---------------------------------------------------------------------------
# One table
# ---------------------------------------------------------------------------


async def _get_one(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    table_id = request.path_params["table_id"]
    org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import (  # noqa: PLC0415
        assess_table_impact,
        get_table,
        list_entries,
    )

    try:
        with get_connection() as conn:
            table = get_table(conn, table_id=table_id, org_id=org_id)
            entries = list_entries(conn, table_id=table["id"])
            impact = assess_table_impact(conn, table_id=table["id"])
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: read failed table=%s: %s", table_id, exc)
        return _server_error()
    return JSONResponse({"table": table, "entries": entries, **impact.as_dict()})


async def _patch_one(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    table_id = request.path_params["table_id"]
    body, refused = await _body(request)
    if refused is not None:
        return refused
    if "scope_level" in body:
        scope_refused = _scope_refusal((body.get("scope_level") or "").strip().upper())
        if scope_refused is not None:
            return scope_refused

    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import update_table  # noqa: PLC0415

    try:
        with get_connection() as conn:
            table = update_table(
                conn,
                table_id=table_id,
                org_id=org_id,
                name=body.get("name"),
                description=body.get("description"),
                identity=identity,
                acknowledge_impact=bool(body.get("acknowledge_impact")),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: patch failed table=%s: %s", table_id, exc)
        return _server_error()
    return JSONResponse(table)


async def _delete_one(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    table_id = request.path_params["table_id"]
    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import delete_table  # noqa: PLC0415

    acknowledged = (request.query_params.get("acknowledge_impact") or "").lower() in {
        "1",
        "true",
        "yes",
    }
    try:
        with get_connection() as conn:
            result = delete_table(
                conn,
                table_id=table_id,
                org_id=org_id,
                identity=identity,
                acknowledge_impact=acknowledged,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: delete failed table=%s: %s", table_id, exc)
        return _server_error()
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Entries
# ---------------------------------------------------------------------------


async def _add_entry(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    table_id = request.path_params["table_id"]
    body, refused = await _body(request)
    if refused is not None:
        return refused
    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import add_entry  # noqa: PLC0415

    source_value = (body.get("source_value") or "").strip()
    canonical_value = (body.get("canonical_value") or "").strip()
    if not source_value or not canonical_value:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "source_value and canonical_value are required.",
            },
            status_code=422,
        )
    try:
        with get_connection() as conn:
            entry = add_entry(
                conn,
                table_id=table_id,
                org_id=org_id,
                source_value=source_value,
                canonical_value=canonical_value,
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: entry failed table=%s: %s", table_id, exc)
        return _server_error()
    return JSONResponse(entry, status_code=201)


async def _patch_entry(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refused = await _body(request)
    if refused is not None:
        return refused
    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import update_entry  # noqa: PLC0415

    canonical_value = (body.get("canonical_value") or "").strip()
    if not canonical_value:
        return JSONResponse(
            {"code": "missing_param", "message": "canonical_value is required."}, status_code=422
        )
    try:
        with get_connection() as conn:
            entry = update_entry(
                conn,
                table_id=request.path_params["table_id"],
                org_id=org_id,
                entry_id=request.path_params["entry_id"],
                canonical_value=canonical_value,
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: entry patch failed: %s", exc)
        return _server_error()
    return JSONResponse(entry)


async def _delete_entry(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    org_id, refused = _guard(request.path_params["project_id"], identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import delete_entry  # noqa: PLC0415

    try:
        with get_connection() as conn:
            result = delete_entry(
                conn,
                table_id=request.path_params["table_id"],
                org_id=org_id,
                entry_id=request.path_params["entry_id"],
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: entry delete failed: %s", exc)
        return _server_error()
    return JSONResponse(result)


async def _import(request: Request) -> Response:
    """POST .../{table_id}/import -- a two-column file, rejections NAMED.

    The body carries the text (`{"text": "..."}`). Every refused line comes back
    with its number and reason and the counts are stated: an import that dropped
    a malformed line quietly would present a partial vocabulary as a complete one.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refused = await _body(request)
    if refused is not None:
        return refused
    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import import_pairs  # noqa: PLC0415

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"code": "missing_param", "message": "text is required."}, status_code=422
        )
    try:
        with get_connection() as conn:
            result = import_pairs(
                conn,
                table_id=request.path_params["table_id"],
                org_id=org_id,
                text=text,
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: import failed: %s", exc)
        return _server_error()
    return JSONResponse(result)


async def _import_preview(request: Request) -> Response:
    """POST .../{table_id}/import/preview -- what the import WOULD do. No write.

    S4 of `unresolved-values.md` asks for a preview before the write, and
    `_import` above cannot be it: it writes the accepted half first and names the
    rejections after. Honest, but too late for somebody who mistyped a column.

    IT TAKES `manage=True` LIKE THE WRITE IT PREVIEWS, not the read guard. The
    preview discloses which source values the table already holds -- that is the
    table's content, and a person allowed only to read the project has no reason
    to enumerate it through a door named "preview".
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refused = await _body(request)
    if refused is not None:
        return refused
    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import preview_import  # noqa: PLC0415

    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"code": "missing_param", "message": "text is required."}, status_code=422
        )
    try:
        with get_connection() as conn:
            result = preview_import(
                conn,
                table_id=request.path_params["table_id"],
                org_id=org_id,
                text=text,
            )
            # NO COMMIT, and no rollback needed either -- nothing was written.
            # The absence of `conn.commit()` here is the difference between this
            # route and the one above it, and it is the whole route.
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: import preview failed: %s", exc)
        return _server_error()
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Assignments
# ---------------------------------------------------------------------------


async def _list_assignments(request: Request) -> Response:
    """GET .../{table_id}/assignments -- who this table is applied to.

    RAISES rather than answering an empty list when the store cannot be read
    (`master_data.py:745-749`); the caller then sees `impact_state: "unknown"`
    and never a count of zero.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    org_id, refused = _guard(request.path_params["project_id"], identity, manage=False)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import assess_table_impact, get_table  # noqa: PLC0415

    try:
        with get_connection() as conn:
            table = get_table(conn, table_id=request.path_params["table_id"], org_id=org_id)
            impact = assess_table_impact(conn, table_id=table["id"])
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: assignments failed: %s", exc)
        return _server_error()
    return JSONResponse({"table_id": table["id"], **impact.as_dict()})


async def _add_assignment(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    body, refused = await _body(request)
    if refused is not None:
        return refused
    org_id, refused = _guard(request.path_params["project_id"], identity, manage=True)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import create_assignment  # noqa: PLC0415

    datastream_id = (body.get("datastream_id") or "").strip()
    source_field = (body.get("source_field") or "").strip()
    if not datastream_id or not source_field:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "datastream_id and source_field are required.",
            },
            status_code=422,
        )
    try:
        with get_connection() as conn:
            assignment = create_assignment(
                conn,
                table_id=request.path_params["table_id"],
                org_id=org_id,
                datastream_id=datastream_id,
                source_field=source_field,
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: assignment failed: %s", exc)
        return _server_error()
    return JSONResponse(assignment, status_code=201)


async def _remove_assignment(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    org_id, refused = _guard(request.path_params["project_id"], identity, manage=True)
    if refused is not None:
        return refused

    assignment_id = (request.query_params.get("assignment_id") or "").strip()
    if not assignment_id:
        return JSONResponse(
            {"code": "missing_param", "message": "assignment_id is required."}, status_code=422
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.value_mapping_tables import delete_assignment  # noqa: PLC0415

    try:
        with get_connection() as conn:
            result = delete_assignment(
                conn,
                table_id=request.path_params["table_id"],
                org_id=org_id,
                assignment_id=assignment_id,
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("value_mapping_api: assignment delete failed: %s", exc)
        return _server_error()
    return JSONResponse(result)


VALUE_MAPPING_ROUTES: list[Route] = [
    Route(_BASE, _list_tables, methods=["GET"]),
    Route(_BASE, _create_table, methods=["POST"]),
    Route(f"{_BASE}/{{table_id}}", _get_one, methods=["GET"]),
    Route(f"{_BASE}/{{table_id}}", _patch_one, methods=["PATCH"]),
    Route(f"{_BASE}/{{table_id}}", _delete_one, methods=["DELETE"]),
    Route(f"{_BASE}/{{table_id}}/entries", _add_entry, methods=["POST"]),
    Route(f"{_BASE}/{{table_id}}/entries/{{entry_id}}", _patch_entry, methods=["PATCH"]),
    Route(f"{_BASE}/{{table_id}}/entries/{{entry_id}}", _delete_entry, methods=["DELETE"]),
    Route(f"{_BASE}/{{table_id}}/import/preview", _import_preview, methods=["POST"]),
    Route(f"{_BASE}/{{table_id}}/import", _import, methods=["POST"]),
    Route(f"{_BASE}/{{table_id}}/assignments", _list_assignments, methods=["GET"]),
    Route(f"{_BASE}/{{table_id}}/assignments", _add_assignment, methods=["POST"]),
    Route(f"{_BASE}/{{table_id}}/assignments", _remove_assignment, methods=["DELETE"]),
]

__all__ = ["VALUE_MAPPING_ROUTES"]
