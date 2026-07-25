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
  DELETE /api/datamodel/fields/{name}       delete field (guard: used_by=0)      [13.1]
  PUT    /api/datamodel/mappings            upsert mapping (body: datastream_id,
                                              source_field, target_field|null)

Auth: same _check_auth from core.admin_api (Bearer token via core.api_auth).
AD-5: project scoping on datastream-bound queries.
AD-8: admin console communicates through this REST layer only.
French error messages throughout (Epic 8 Part B + 13.1).

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
        from core.datamodel import create_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            created = create_target_field(body, created_by=identity or "anonymous", conn=conn)
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
        {"display_name": str, "measure": str|null, "description": str|null}

    Response (200): updated field row.

    Error responses:
        400 -- invalid JSON body
        401 -- unauthorized
        404 -- field not found
        422 -- immutable field change attempted or validation error
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
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=400,
        )

    try:
        from core.datamodel import update_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            updated = update_target_field(name, body, conn)
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
    """GET /api/datamodel/mappings?datastream_id=&project_id= -- list a stream's mappings.

    Story 8.x (review-epic-8): the datastream-detail Mapping tab needs to READ the
    current source->target mappings; only PUT existed, so GET returned 405.

    Response (200): {"mappings": [{"source_field", "target_field", "is_key_column"}]}
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    datastream_id = (request.query_params.get("datastream_id") or "").strip()
    if not datastream_id:
        return JSONResponse(
            {"code": "missing_field", "message": "datastream_id est requis"},
            status_code=400,
        )

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
        from core.datamodel import FieldInUseError, delete_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            delete_target_field(name, conn)
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
    Route("/api/datamodel/fields/{name}", endpoint=_get_field, methods=["GET"]),
    Route("/api/datamodel/fields/{name}", endpoint=_patch_field, methods=["PATCH"]),
    Route("/api/datamodel/fields/{name}", endpoint=_delete_field, methods=["DELETE"]),
    # GET/PUT /api/datamodel/mappings — list + upsert source->target bindings
    Route("/api/datamodel/mappings", endpoint=_list_mappings, methods=["GET"]),
    Route("/api/datamodel/mappings", endpoint=_upsert_mapping, methods=["PUT"]),
]
