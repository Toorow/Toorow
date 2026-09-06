"""toorow -- Data model REST route handlers (Story 8.5, Epic 8 + Story 13.1, Epic 13).

Provides DATAMODEL_ROUTES: list[Route] — a flat list that the orchestrator
splices into admin_api.router at startup. This module is NEVER imported by
admin_api.py at module level (no circular import risk).

Routes (READS ONLY since 2026-08-25 -- story 49.3 AC1, see below):
  GET    /api/datamodel/fields              ?project_id=&kind=&usage=
  GET    /api/datamodel/fields/{name}       field detail + used-by + conflicts
  GET    /api/datamodel/fields/{name}/history  full version timeline [44.8]
  GET    /api/datamodel/mappings            list by ?datastream_id= OR, since 44.10,
                                              by ?target_field=[&project_id=] ("Fed by")
  POST   /api/datamodel/fields                 REFUSED, 409 legacy_store_is_read_only
  PATCH  /api/datamodel/fields/{name}          REFUSED, 409 legacy_store_is_read_only
  POST   /api/datamodel/fields/{name}/approve  REFUSED, 409 legacy_store_is_read_only
  DELETE /api/datamodel/fields/{name}          REFUSED, 409 legacy_store_is_read_only
  PUT    /api/datamodel/mappings               REFUSED, 409 legacy_store_is_read_only

Auth: same _check_auth from core.admin_api (Bearer token via core.api_auth).
AD-5: project scoping on datastream-bound queries.
AD-8: admin console communicates through this REST layer only.
French error messages throughout (Epic 8 Part B + 13.1).

Story 44.8: GET .../history calls core.datamodel.list_field_versions(name,
conn), after checking target_field_exists(name, conn) for a genuine 404 on
unknown names (finding #5). History outlives visibility: it is servable even
for a soft-deleted field (status='deleted' row still exists) -- no status
filter anywhere in this path. That timeline is now a pure READ of what the
dictionary already recorded; the Restore that used to append to it went with
the PATCH door in the 49.3 cutover (see the block below), because going back
to an earlier definition is publishing a Concept version again.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth helper — import from admin_api (safe: admin_api never imports us)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _project_access_allowed(
    conn,
    *,
    identity: str,
    project_id: str,
    minimum_capability: str,
) -> bool:
    """Delegate to the shared strict seam, including explicit local compatibility."""
    from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415

    try:
        return _strict_project_capability_allowed(
            conn,
            identity=identity,
            project_id=project_id,
            minimum_capability=minimum_capability,
        )
    except Exception as exc:
        logger.warning(
            "datamodel_api: project access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return False


def _datastream_in_project(conn, *, datastream_id: str, project_id: str) -> bool:
    """Delegate the pair proof to the one seam that owns it (AI-219).

    This module used to read the owner project and compare it in Python. The
    answer was right and the shape was wrong: two statements where one does, and
    a local copy of a rule that has to hold for every reader of a Datastream id.
    """
    from core.admin_api import require_datastream_in_project  # noqa: PLC0415

    return require_datastream_in_project(
        conn, datastream_id=datastream_id, project_id=project_id
    )


def _project_not_found() -> JSONResponse:
    return JSONResponse(
        {"code": "not_found", "message": "Project not found"}, status_code=404
    )


def _required_project_id(request: Request) -> str | None:
    return (request.query_params.get("project_id") or "").strip() or None


# ---------------------------------------------------------------------------
# THIS SURFACE STOPPED TAKING WRITES ON 2026-08-25 (story 49.3, AC1).
#
# WHAT WENT, AND WHY. `app.target_fields` is the legacy field dictionary. The
# Semantic Model is the authority `governance.md` names -- "The Semantic Model
# owns canonical metrics, dimensions, relationships, aggregation behavior" --
# and a field is declared, published and retired there, as a Concept, through a
# change set. Two stores took a declaration and only one of them governed
# anything, which is the same defect story 60.2 measured on additivity.
#
# MEASURED BEFORE REMOVING THEM, and this is why no caller breaks: the five
# write doors had exactly two callers in the whole repository --
# `ui/admin/src/datamodel/FieldDetailDrawer.tsx` and
# `ui/admin/src/datamodel/NewFieldDialog.tsx` -- and both were orphans, imported
# by their own tests and by nothing else since their host `FieldsTable.tsx` was
# deleted. They go in this commit with the doors they called.
#
# THE READS STAY, and that is not an oversight. `GET /api/datamodel/fields*` and
# `GET /api/datamodel/mappings` have four real console readers, one of which IS
# the governed `mapping-coverage` lens of Governance
# (`ui/admin/src/shell/pages/ProjectMapping.tsx`); retiring them with the writes
# would blind that lens. The dictionary keeps its 15 delivered rows (migration
# 023) and stays readable.
#
# THE STORE IS NOT ORPHANED EITHER. `app.target_fields` is still written by
# `conflict_resolutions_api` (a MEASURE_NULL resolution PATCHes `measure`
# through `core.datamodel.update_target_field`), and `app.datastream_mappings`
# is still written by `flows._apply_mappings` -- the Datastream Workbench
# mapping tab, under a version ledger and a publication review. Only the two
# ungoverned doors are closed.
#
# THEY REFUSE RATHER THAN DISAPPEAR, exactly like `notebooks_api` did in the
# 67.23 cutover: unmounting them would answer 404 to a write, which tells the
# caller its object does not exist and sends it looking for it. A refusal names
# the gesture that works instead.
# ---------------------------------------------------------------------------

_LEGACY_FIELD_WRITE_REFUSED = {
    "code": "legacy_store_is_read_only",
    "message": (
        "This field dictionary no longer takes writes. A measure or a dimension "
        "is declared, published and retired in Governance, on the Project's "
        "Semantic Model, where every screen reads its definition from."
    ),
}

_LEGACY_MAPPING_WRITE_REFUSED = {
    "code": "legacy_store_is_read_only",
    "message": (
        "This mapping surface no longer takes writes. A source column is bound "
        "to a canonical field on the Datastream's Mapping tab, which proposes a "
        "mapping version and publishes it under review."
    ),
}


def _refuse_legacy_field_write() -> Response:
    """The same refusal for the four field doors -- one code, one sentence."""
    return JSONResponse(_LEGACY_FIELD_WRITE_REFUSED, status_code=409)


def _refuse_legacy_mapping_write() -> Response:
    """The mapping door names a different gesture, under the same code."""
    return JSONResponse(_LEGACY_MAPPING_WRITE_REFUSED, status_code=409)


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields
# ---------------------------------------------------------------------------


async def _list_fields(request: Request) -> Response:
    """GET /api/datamodel/fields -- list fields for one authorized project."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=422,
        )
    kind = (request.query_params.get("kind") or "").strip() or None
    usage = (request.query_params.get("usage") or "").strip() or None
    module = (request.query_params.get("module") or "").strip() or None

    if kind is not None and kind not in ("metric", "dimension"):
        return JSONResponse(
            {"code": "invalid_param", "message": "kind must be 'metric' or 'dimension'"},
            status_code=422,
        )
    if usage is not None and usage not in ("used", "unmapped"):
        return JSONResponse(
            {"code": "invalid_param", "message": "usage must be 'used' or 'unmapped'"},
            status_code=422,
        )

    try:
        from core.admin_api import _strict_project_capability_allowed  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            allowed = _strict_project_capability_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            )
    except Exception as exc:
        logger.warning(
            "datamodel_api: field-list access unavailable project=%s: %s",
            project_id,
            type(exc).__name__,
        )
        return JSONResponse(
            {"code": "not_found", "message": "Project not found"}, status_code=404
        )
    if not allowed:
        return JSONResponse(
            {"code": "not_found", "message": "Project not found"}, status_code=404
        )

    try:
        from core.datamodel import list_target_fields  # noqa: PLC0415

        with get_connection() as conn:
            fields = list_target_fields(
                conn, project_id=project_id, kind=kind, usage=usage, module=module
            )
    except Exception as exc:
        logger.error("datamodel_api: list_fields_error: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    return JSONResponse(fields)

# ---------------------------------------------------------------------------
# GET /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


async def _get_field(request: Request) -> Response:
    """GET /api/datamodel/fields/{name} -- full field detail.

    Response (200):
        {"name", "display_name", "data_type", "field_kind", "measure",
         "description", "created_by", "is_default", "created_at",
         "used_by_count", "used_by": [...], "conflicts": [...]}

    Each used_by item:
        {"datastream_id", "datastream_name", "module_name", "project_id",
         "enabled", "source_field", "last_loaded_at", "last_verdict"}

    Each conflict item:
        {"code", "message", "affected_streams": [...]}

    Error responses:
        401 -- unauthorized
        404 -- field not found
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = _required_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name est requis"},
            status_code=400,
        )

    try:
        from core.datamodel import get_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _project_access_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _project_not_found()
            field = get_target_field(name, conn, project_id=project_id)
    except Exception as exc:
        logger.error("datamodel_api: get_field_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    if field is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Champ '{name}' introuvable"},
            status_code=404,
        )

    return JSONResponse(field)


# ---------------------------------------------------------------------------
# POST /api/datamodel/fields
# ---------------------------------------------------------------------------


async def _create_field(request: Request) -> Response:
    """POST /api/datamodel/fields -- REFUSED since 2026-08-25 (story 49.3 AC1).

    Declaring a measure or a dimension is a Semantic Model act: the Concept
    workbench in Governance writes `app.semantic_concepts` through a change set
    that is prepared, confirmed and versioned. This door wrote `app.target_fields`
    directly, with no version a reader could pin and no review anyone saw.

    Its only caller, `ui/admin/src/datamodel/NewFieldDialog.tsx`, went with it.
    """
    return _refuse_legacy_field_write()


# ---------------------------------------------------------------------------
# PATCH /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


async def _patch_field(request: Request) -> Response:
    """PATCH /api/datamodel/fields/{name} -- REFUSED since 2026-08-25 (49.3 AC1).

    This carried both the attribute edit and the 44.8 Restore (`restored_from`).
    Both are Semantic Model acts now: a Concept is edited by publishing a new
    version, and going back to an earlier one is publishing that one again --
    an append, never a rewrite, which is what `app.target_fields_versions` could
    only imitate.

    The store function stays: `conflict_resolutions_api` still calls
    `core.datamodel.update_target_field` to resolve a MEASURE_NULL conflict, and
    that path is governed by the conflict it answers.
    """
    return _refuse_legacy_field_write()


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields/{name}/history  [Story 44.8]
# ---------------------------------------------------------------------------


async def _get_field_history(request: Request) -> Response:
    """GET /api/datamodel/fields/{name}/history -- full version timeline.

    History outlives visibility: this is servable even when the field's
    current status is 'deleted' (the target_fields row still exists; only
    list_target_fields/get_target_field hide it) -- NO status filter is
    applied on the existence check below, deliberately, so deleted fields
    still serve history. This is API-only for now: the drawer has no
    affordance to OPEN on a deleted field yet (Story 44.8 finding #6), so
    this route is only reachable directly today; no "show deleted" UI is
    being built this round.

    Story 44.8 finding #5: an UNKNOWN name (never existed, in any status)
    is a genuine 404, distinguished from "exists but has no history yet"
    (which returns 200 with an empty list).

    Response (200):
        {"versions": [{"version_number", "change_kind", "changed_by",
                       "changed_at", "diff",
                       "snapshot": {"display_name", "measure", "description",
                                    "status"}}]}
        ordered DESC (newest first), capped at 200 rows (core.datamodel's
        _FIELD_VERSIONS_LIMIT).

    Error responses:
        401 -- unauthorized
        404 -- unknown field name (never existed)
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = _required_project_id(request)
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name est requis"},
            status_code=400,
        )

    try:
        from core.datamodel import list_field_versions, target_field_exists  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _project_access_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ):
                return _project_not_found()
            if not target_field_exists(name, conn):
                return JSONResponse(
                    {"code": "not_found", "message": f"Champ '{name}' introuvable"},
                    status_code=404,
                )
            rows = list_field_versions(name, conn)
    except Exception as exc:
        logger.error("datamodel_api: get_field_history_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    versions = [
        {
            "version_number": row.get("version_number"),
            "change_kind": row.get("change_kind"),
            "changed_by": row.get("changed_by"),
            "changed_at": row.get("changed_at"),
            "diff": row.get("diff"),
            "snapshot": {
                "display_name": row.get("display_name"),
                "measure": row.get("measure"),
                "description": row.get("description"),
                "status": row.get("status"),
            },
        }
        for row in rows
    ]

    return JSONResponse({"versions": versions})


# ---------------------------------------------------------------------------
# PUT /api/datamodel/mappings
# ---------------------------------------------------------------------------


async def _upsert_mapping(request: Request) -> Response:
    """PUT /api/datamodel/mappings -- REFUSED since 2026-08-25 (story 49.3 AC1).

    A binding between a source column and a canonical field is decided on the
    Datastream's Mapping tab, which proposes a mapping VERSION and publishes it
    under review (`core.flows._apply_mappings`, the ledger the Workbench draws).
    This door wrote one row of `app.datastream_mappings` with no version and no
    review, so a binding could change under a published Datastream with nothing
    recording that it had.

    Its only caller, `ui/admin/src/datamodel/FieldDetailDrawer.tsx`, went with it.
    """
    return _refuse_legacy_mapping_write()


async def _list_mappings(request: Request) -> Response:
    """GET /api/datamodel/mappings -- list mappings by datastream OR by target field.

    Story 8.x (review-epic-8): the datastream-detail Mapping tab needs to READ the
    current source->target mappings; only PUT existed, so GET returned 405.

    Two mutually exclusive lookups, exactly one of which is required:
      * ``?datastream_id=&project_id=`` -- one stream's mappings.
        Response: {"mappings": [{"source_field", "target_field", "is_key_column"}]}
        ``project_id`` is REQUIRED (AI-219). Until 2026-08-06 this branch
        authenticated the identity and then read ``WHERE datastream_id = %s``
        with nothing else: ANY authenticated caller could name ANY stream id and
        receive its field mapping. Its PUT sibling, twenty lines up, had proven
        the pair since 43.x -- the read had simply never been asked to.
      * ``?target_field=&project_id=`` -- Story 44.10's "Fed by" read: which
        datastreams feed this dictionary field. Response rows additionally carry
        datastream_id / datastream_name / module_name / project_id / enabled, so
        the knowledge-graph drawer can name the SOURCE, not just the column.
        ``project_id`` is REQUIRED on this branch (44.10 re-review): the
        platform-wide answer (every project's streams) must never be reachable
        by accident -- the only consumer (the graph drawer) always scopes.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    datastream_id = (request.query_params.get("datastream_id") or "").strip()
    target_field = (request.query_params.get("target_field") or "").strip()

    if datastream_id and target_field:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "datastream_id et target_field s'excluent mutuellement",
            },
            status_code=400,
        )
    if not datastream_id and not target_field:
        return JSONResponse(
            {"code": "missing_field", "message": "datastream_id ou target_field est requis"},
            status_code=400,
        )

    if target_field:
        project_id = (request.query_params.get("project_id") or "").strip() or None
        if project_id is None:
            # 44.10 re-review: no accidental platform-wide cross-project answer.
            return JSONResponse(
                {
                    "code": "missing_field",
                    "message": "project_id is required with target_field",
                },
                status_code=400,
            )
        try:
            from core.datamodel import list_mappings_for_target_field  # noqa: PLC0415
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                if not _project_access_allowed(
                    conn,
                    identity=identity,
                    project_id=project_id,
                    minimum_capability="view",
                ):
                    return _project_not_found()
                mappings = list_mappings_for_target_field(
                    conn, target_field=target_field, project_id=project_id
                )
        except Exception as exc:
            logger.error("datamodel_api: list_mappings_by_field_error: %s", exc)
            return JSONResponse(
                {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
                status_code=500,
            )
        return JSONResponse({"mappings": mappings})

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if project_id is None:
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "project_id is required with datastream_id",
            },
            status_code=400,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _project_access_allowed(
                conn,
                identity=identity,
                project_id=project_id,
                minimum_capability="view",
            ) or not _datastream_in_project(
                conn, datastream_id=datastream_id, project_id=project_id
            ):
                # ONE envelope for "you may not" and for "it is not here": a
                # caller comparing two answers must not learn that the stream
                # exists in somebody else's project.
                return _project_not_found()
            with conn.cursor() as cur:
                # `app.datastream_mappings` carries no `project_id` of its own,
                # so the pair is carried by the join, not by a second column.
                cur.execute(
                    """
                    SELECT m.source_field, m.target_field, m.is_key_column
                    FROM app.datastream_mappings m
                    JOIN app.datastreams d ON d.id = m.datastream_id
                    WHERE m.datastream_id = %s AND d.project_id = %s
                    ORDER BY m.source_field
                    """,
                    (datastream_id, project_id),
                )
                mappings = [
                    {
                        "source_field": r[0],
                        "target_field": r[1],
                        "is_key_column": bool(r[2]),
                    }
                    for r in cur.fetchall()
                ]
    except Exception as exc:
        logger.error("datamodel_api: list_mappings_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse({"mappings": mappings})


# ---------------------------------------------------------------------------
# POST /api/datamodel/fields/{name}/approve  [Story 13.1]
# ---------------------------------------------------------------------------


async def _approve_field(request: Request) -> Response:
    """POST /api/datamodel/fields/{name}/approve -- REFUSED (story 49.3 AC1).

    Approving is a publication, and publication belongs to the change set: the
    Semantic Model gate verifies the change, records the verdict and moves the
    concept pointer. A boolean flipped on a dictionary row recorded none of that.
    """
    return _refuse_legacy_field_write()


# ---------------------------------------------------------------------------
# DELETE /api/datamodel/fields/{name}  [Story 13.1]
# ---------------------------------------------------------------------------


async def _delete_field(request: Request) -> Response:
    """DELETE /api/datamodel/fields/{name} -- REFUSED (story 49.3 AC1).

    Retiring a measure or a dimension is `lifecycle_status = 'archived'` on its
    Concept, published like any other change. The soft delete here left the row
    physically present and every reader had to remember to filter it out.
    """
    return _refuse_legacy_field_write()


# ---------------------------------------------------------------------------
# Exported route list (orchestrator wires into admin_api.router)
# ---------------------------------------------------------------------------

#: THE FIVE WRITE VERBS ARE STILL DECLARED, and each one now points at a refusal.
#: They stay because unmounting a write answers 405 or 404 -- "this address takes
#: no such verb", or "your object is not here" -- and both send the caller looking
#: instead of telling it where to go. Their handlers carry the reason; the router
#: only has to keep the address reachable. `screens/routes.json` is unchanged by
#: the cutover for exactly this reason: not one path left the server.
DATAMODEL_ROUTES: list[Route] = [
    # IMPORTANT: the static /api/datamodel/fields route (list + create) must
    # precede the parametrized /{name} routes so Starlette matches list/create first.
    Route("/api/datamodel/fields", endpoint=_list_fields, methods=["GET"]),
    Route("/api/datamodel/fields", endpoint=_create_field, methods=["POST"]),
    # Story 13.1: approve sub-resource must be declared BEFORE the plain /{name} routes
    # so Starlette's router matches /fields/{name}/approve before /{name}.
    Route("/api/datamodel/fields/{name}/approve", endpoint=_approve_field, methods=["POST"]),
    # Story 44.8: history sub-resource must also precede the plain /{name} route.
    Route("/api/datamodel/fields/{name}/history", endpoint=_get_field_history, methods=["GET"]),
    Route("/api/datamodel/fields/{name}", endpoint=_get_field, methods=["GET"]),
    Route("/api/datamodel/fields/{name}", endpoint=_patch_field, methods=["PATCH"]),
    Route("/api/datamodel/fields/{name}", endpoint=_delete_field, methods=["DELETE"]),
    # GET/PUT /api/datamodel/mappings — list + the retired upsert of source->target
    Route("/api/datamodel/mappings", endpoint=_list_mappings, methods=["GET"]),
    Route("/api/datamodel/mappings", endpoint=_upsert_mapping, methods=["PUT"]),
]
