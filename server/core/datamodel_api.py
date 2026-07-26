"""toorow -- Data model REST route handlers (Story 8.5, Epic 8 + Story 13.1, Epic 13).

Provides DATAMODEL_ROUTES: list[Route] — a flat list that the orchestrator
splices into admin_api.router at startup. This module is NEVER imported by
admin_api.py at module level (no circular import risk).

Routes:
  GET    /api/datamodel/fields              ?project_id=&kind=&usage=
  GET    /api/datamodel/fields/{name}       field detail + used-by + conflicts
  POST   /api/datamodel/fields              create user field
  PATCH  /api/datamodel/fields/{name}       update display_name/measure/description
  POST   /api/datamodel/fields/{name}/approve  approve field (draft -> approved) [13.1]
  DELETE /api/datamodel/fields/{name}       soft-delete field (guard: used_by=0) [13.1;
                                              soft delete + versioned since 44.7]
  GET    /api/datamodel/fields/{name}/history  full version timeline [44.8]
  PUT    /api/datamodel/mappings            upsert mapping (body: datastream_id,
                                              source_field, target_field|null)
  GET    /api/datamodel/mappings            list by ?datastream_id= OR, since 44.10,
                                              by ?target_field=[&project_id=] ("Fed by")

Auth: same _check_auth from core.admin_api (Bearer token via core.api_auth).
AD-5: project scoping on datastream-bound queries.
AD-8: admin console communicates through this REST layer only.
French error messages throughout (Epic 8 Part B + 13.1).

Story 44.7: PATCH and DELETE thread the caller's REAL identity (from
_check_auth) into core.datamodel.update_target_field / delete_target_field so
every app.target_fields_versions row and every 'target_field.*' audit row
carries who actually made the change, never a literal "system".

Story 44.8: GET .../history calls core.datamodel.list_field_versions(name,
conn) (the store fn already existed from 44.7), after checking
target_field_exists(name, conn) for a genuine 404 on unknown names (finding
#5). History outlives visibility: it is servable even for a soft-deleted
field (status='deleted' row still exists) -- no status filter anywhere in
this path. Restore has NO dedicated endpoint -- the UI PATCHes this same
/fields/{name} route with the snapshot's patchable fields plus a
`restored_from` hint; _patch_field normalises + verifies that hint (a plain
int, or a dict with an int version_number, verified to exist in
app.target_fields_versions -- 422 otherwise) BEFORE calling
update_target_field, which then always records change_kind='restored'
(finding #4 -- validation moved to the API layer, the store no longer
trusts the client's value verbatim).

AC3 (permission-gated Restore) is intentionally NOT implemented here: see
the note under Story 44.8 in the epic file. No org-permission model exists
in this API today; Restore is available to any authenticated operator, and
failures surface verbatim via the PATCH response (no fabricated
403/"forbidden reason" state).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import json
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


# ---------------------------------------------------------------------------
# GET /api/datamodel/fields
# ---------------------------------------------------------------------------


async def _list_fields(request: Request) -> Response:
    """GET /api/datamodel/fields -- list target fields with used_by_count.

    Query params:
        project_id  (optional) -- scope used_by_count to one project (AD-5)
        kind        (optional) -- 'metric' | 'dimension'
        usage       (optional) -- 'used' | 'unmapped'
        module      (optional) -- AI-49: restrict to fields mapped by datastreams of this
                                  module_name (JOIN datastream_mappings -> datastreams).
                                  Unknown module -> empty list (200, not an error).

    Response (200):
        [{"name", "display_name", "data_type", "field_kind", "measure",
          "description", "created_by", "is_default", "created_at", "used_by_count"}]

    Error responses:
        401 -- unauthorized
        422 -- invalid kind/usage filter value
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    kind = (request.query_params.get("kind") or "").strip() or None
    usage = (request.query_params.get("usage") or "").strip() or None
    # AI-49: ?module= filter (optional; unknown module -> empty 200, not an error).
    module = (request.query_params.get("module") or "").strip() or None

    if kind is not None and kind not in ("metric", "dimension"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "kind doit etre 'metric' ou 'dimension'",
            },
            status_code=422,
        )
    if usage is not None and usage not in ("used", "unmapped"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "usage doit etre 'used' ou 'unmapped'",
            },
            status_code=422,
        )

    try:
        from core.datamodel import list_target_fields  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            fields = list_target_fields(
                conn, project_id=project_id, kind=kind, usage=usage, module=module
            )
    except Exception as exc:
        logger.error("datamodel_api: list_fields_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
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
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
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
            field = get_target_field(name, conn)
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
    """POST /api/datamodel/fields -- create a user-defined target field.

    Request body (JSON):
        {"name": str, "display_name": str, "data_type": str,
         "field_kind": str, "measure": str|null, "description": str|null}

    Response (201):
        {name, display_name, data_type, field_kind, measure, description,
         created_by, is_default, created_at}

    Error responses:
        400 -- missing/invalid JSON body
        401 -- unauthorized
        409 -- name already exists (UniqueViolation)
        422 -- validation error (snake_case, enum values, etc.)
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=400,
        )

    try:
        from core.datamodel import DuplicateFieldError, create_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            created = create_target_field(body, created_by=identity or "anonymous", conn=conn)
    except DuplicateFieldError as exc:
        # Live duplicate name (44.7 revival path re-raises this instead of the
        # raw UniqueViolation) -- keep the documented 409 contract.
        return JSONResponse({"code": "conflict", "message": str(exc)}, status_code=409)
    except ValueError as exc:
        return JSONResponse({"code": "validation_error", "message": str(exc)}, status_code=422)
    except Exception as exc:
        err_str = str(exc)
        if "UniqueViolation" in type(exc).__name__ or "unique" in err_str.lower():
            return JSONResponse(
                {
                    "code": "conflict",
                    "message": f"Un champ nomme '{body.get('name')}' existe deja",
                },
                status_code=409,
            )
        logger.error("datamodel_api: create_field_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse(created, status_code=201)


# ---------------------------------------------------------------------------
# PATCH /api/datamodel/fields/{name}
# ---------------------------------------------------------------------------


async def _patch_field(request: Request) -> Response:
    """PATCH /api/datamodel/fields/{name} -- update mutable attributes.

    Patchable: display_name, measure, description.
    Immutable: name, data_type, field_kind (returns 422 if attempted).

    Request body (JSON, all optional):
        {"display_name": str, "measure": str|null, "description": str|null,
         "restored_from": int}  -- restored_from is the 44.8 restore hint,
        see below.

    Story 44.8 finding #4: `restored_from` is client-supplied and would
    otherwise be injected verbatim into the append-only history jsonb
    unvalidated. It is normalised and verified HERE, before the store is
    ever called:
      - accepted shapes: a plain int, OR a dict carrying an int
        `version_number` key (some UI snapshots pass the whole version
        object back) -- anything else is a 422 with a French message;
      - the resulting int is verified to name a real
        (name, version_number) row in app.target_fields_versions via a
        SELECT -- 422 if it does not exist;
      - only the validated int is ever passed through to
        core.datamodel.update_target_field. The store copies that int
        as-is into the diff metadata; it does not re-validate it.

    Response (200): updated field row.

    Error responses:
        400 -- invalid JSON body
        401 -- unauthorized
        404 -- field not found
        422 -- immutable field change attempted, invalid/unknown
                restored_from, or other validation error
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name est requis"},
            status_code=400,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=400,
        )

    # Story 44.8 finding #4: normalise + verify restored_from BEFORE it ever
    # reaches the store.
    if "restored_from" in body:
        raw_restored_from = body["restored_from"]
        version_number: int | None = None
        if isinstance(raw_restored_from, bool):
            version_number = None  # bool is a subclass of int -- reject explicitly
        elif isinstance(raw_restored_from, int):
            version_number = raw_restored_from
        elif isinstance(raw_restored_from, dict):
            candidate = raw_restored_from.get("version_number")
            if isinstance(candidate, int) and not isinstance(candidate, bool):
                version_number = candidate

        if version_number is None:
            return JSONResponse(
                {
                    "code": "validation_error",
                    "message": (
                        "restored_from doit etre un entier (numero de version) valide"
                    ),
                },
                status_code=422,
            )

        body["restored_from"] = version_number

    # ONE connection for both the restored_from existence check and the
    # update (44.8 re-review): two get_connection() calls doubled pooler
    # churn and left a TOCTOU window between verify and write.
    try:
        from core.datamodel import update_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if "restored_from" in body:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT 1 FROM app.target_fields_versions
                        WHERE name = %s AND version_number = %s
                        """,
                        (name, body["restored_from"]),
                    )
                    if cur.fetchone() is None:
                        return JSONResponse(
                            {
                                "code": "validation_error",
                                "message": (
                                    f"Version {body['restored_from']} introuvable "
                                    f"pour le champ '{name}'"
                                ),
                            },
                            status_code=422,
                        )
            updated = update_target_field(name, body, conn, identity=identity or "anonymous")
    except ValueError as exc:
        return JSONResponse({"code": "validation_error", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.error("datamodel_api: patch_field_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    if updated is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Champ '{name}' introuvable"},
            status_code=404,
        )

    return JSONResponse(updated)


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
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
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
    """PUT /api/datamodel/mappings -- upsert a source->target field mapping.

    Request body (JSON):
        {"datastream_id": str, "source_field": str, "target_field": str|null}

    target_field=null removes the mapping (sets it to unmapped).

    Response (200):
        {"datastream_id", "source_field", "target_field", "is_key_column", "created_at"}

    Error responses:
        400 -- missing/invalid JSON body or missing required fields
        401 -- unauthorized
        422 -- validation error (FK violation = target_field does not exist)
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes)
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=400,
        )

    datastream_id = (body.get("datastream_id") or "").strip()
    source_field = (body.get("source_field") or "").strip()
    # target_field can be explicitly None / null (unmapping)
    target_field = body.get("target_field") or None
    if isinstance(target_field, str):
        target_field = target_field.strip() or None

    if not datastream_id:
        return JSONResponse(
            {"code": "missing_field", "message": "datastream_id est requis"},
            status_code=400,
        )
    if not source_field:
        return JSONResponse(
            {"code": "missing_field", "message": "source_field est requis"},
            status_code=400,
        )

    try:
        from core.datamodel import upsert_mapping  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = upsert_mapping(
                datastream_id=datastream_id,
                source_field=source_field,
                target_field_name=target_field,
                identity=identity or "anonymous",
                conn=conn,
            )
    except ValueError as exc:
        return JSONResponse({"code": "validation_error", "message": str(exc)}, status_code=422)
    except Exception as exc:
        err_str = str(exc)
        # FK violation: target_field does not exist in target_fields
        if "ForeignKeyViolation" in type(exc).__name__ or "foreign key" in err_str.lower():
            return JSONResponse(
                {
                    "code": "invalid_target",
                    "message": (
                        f"Le champ cible '{target_field}' n'existe pas dans le dictionnaire"
                    ),
                },
                status_code=422,
            )
        logger.error("datamodel_api: upsert_mapping_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    return JSONResponse(result)


async def _list_mappings(request: Request) -> Response:
    """GET /api/datamodel/mappings -- list mappings by datastream OR by target field.

    Story 8.x (review-epic-8): the datastream-detail Mapping tab needs to READ the
    current source->target mappings; only PUT existed, so GET returned 405.

    Two mutually exclusive lookups, exactly one of which is required:
      * ``?datastream_id=`` -- one stream's mappings (the original shape).
        Response: {"mappings": [{"source_field", "target_field", "is_key_column"}]}
      * ``?target_field=&project_id=`` -- Story 44.10's "Fed by" read: which
        datastreams feed this dictionary field. Response rows additionally carry
        datastream_id / datastream_name / module_name / project_id / enabled, so
        the knowledge-graph drawer can name the SOURCE, not just the column.
        ``project_id`` is REQUIRED on this branch (44.10 re-review): the
        platform-wide answer (every project's streams) must never be reachable
        by accident -- the only consumer (the graph drawer) always scopes.
    """
    authorized, _identity = await _check_auth(request)
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
                    "message": "project_id est requis avec target_field",
                },
                status_code=400,
            )
        try:
            from core.datamodel import list_mappings_for_target_field  # noqa: PLC0415
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
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

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT source_field, target_field, is_key_column
                    FROM app.datastream_mappings
                    WHERE datastream_id = %s
                    ORDER BY source_field
                    """,
                    (datastream_id,),
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
    """POST /api/datamodel/fields/{name}/approve -- approve a target field.

    Transitions the field from 'draft' to 'approved'. Idempotent: approving
    an already-approved field updates the approval timestamp and appends a new
    audit row. Immutable fields (name/data_type/field_kind) are never touched.

    Response (200): updated field row (includes status='approved', approved_at,
                    approved_by).

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

    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name est requis"},
            status_code=400,
        )

    try:
        from core.datamodel import approve_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = approve_target_field(name, identity=identity or "anonymous", conn=conn)
    except Exception as exc:
        logger.error("datamodel_api: approve_field_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    if result is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Champ '{name}' introuvable"},
            status_code=404,
        )

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# DELETE /api/datamodel/fields/{name}  [Story 13.1]
# ---------------------------------------------------------------------------


async def _delete_field(request: Request) -> Response:
    """DELETE /api/datamodel/fields/{name} -- delete a target field.

    Guard: refuses deletion (409) when the field is referenced by at least one
    datastream mapping (used_by_count > 0). Error message is in French.

    Response (204): no content on success.

    Error responses:
        401 -- unauthorized
        404 -- field not found
        409 -- field has active mappings (FR message included)
        500 -- DB error
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    name = request.path_params.get("name", "").strip()
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name est requis"},
            status_code=400,
        )

    try:
        from core.datamodel import FieldInUseError, delete_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            delete_target_field(name, identity or "anonymous", conn)
    except FieldInUseError as exc:
        return JSONResponse(
            {"code": "field_in_use", "message": str(exc)},
            status_code=409,
        )
    except ValueError as exc:
        # Field not found
        return JSONResponse(
            {"code": "not_found", "message": str(exc)},
            status_code=404,
        )
    except Exception as exc:
        logger.error("datamodel_api: delete_field_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de donnees : {exc}"},
            status_code=500,
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Exported route list (orchestrator wires into admin_api.router)
# ---------------------------------------------------------------------------

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
    # GET/PUT /api/datamodel/mappings — list + upsert source->target bindings
    Route("/api/datamodel/mappings", endpoint=_list_mappings, methods=["GET"]),
    Route("/api/datamodel/mappings", endpoint=_upsert_mapping, methods=["PUT"]),
]
