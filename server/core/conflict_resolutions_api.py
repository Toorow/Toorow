"""toorow -- MDM Conflict resolutions REST API (Story 13.2, Epic 13).

Routes (spliced into admin_api.router at startup):

  GET    /api/mdm/conflicts                ?project_id=
      -> list all fields with active CURRENCY_CONFLICT or MEASURE_NULL,
         with their existing fx resolutions.

  GET    /api/mdm/conflicts/resolutions    ?project_id=
      -> list all fx_conflict_resolutions (optionally project-scoped).

  POST   /api/mdm/conflicts/resolutions
      Body: {project_id, target_field, source_module, resolved_source_currency, note?}
      -> upsert fx resolution. AD-6: NO currency conversion. dbt consumes the row.

  DELETE /api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}
      -> delete fx resolution (204).

  PATCH  /api/mdm/conflicts/measure/{field_name}
      Body: {measure: str}  (e.g. "sum", "average", ...)
      -> resolve MEASURE_NULL by patching target_fields.measure via existing API.
         Reuses update_target_field (DRY, AI-53).

Auth: Bearer token via core.api_auth (same as admin_api).
AD-5: project_id scoping -- returns 404 (non-disclosant) for cross-project access.
AD-6: AUCUNE conversion FX dans ce module. Résolution = donnée persistée lue par dbt.
AD-15: admin console writes via REST only.

French error messages throughout.
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
# Auth helper
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


# ---------------------------------------------------------------------------
# AD-5 project access guard
# ---------------------------------------------------------------------------


async def _guard_project_access(project_id: str, identity: str, conn) -> bool:
    """Return True if identity has access to project_id; False otherwise.

    On False the caller returns 404 (non-disclosant -- same as unknown project).
    """
    if not project_id:
        return True  # global / unscoped calls allowed

    try:
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        return identity_has_project_access(project_id, identity, conn)
    except Exception as exc:
        logger.warning(
            "conflict_resolutions_api: project_access_check_failed project=%s: %s",
            project_id, exc,
        )
        return False


# ---------------------------------------------------------------------------
# GET /api/mdm/conflicts
# ---------------------------------------------------------------------------


async def _list_conflicts(request: Request) -> Response:
    """GET /api/mdm/conflicts?project_id= -- list all active MDM conflicts.

    Each item in the response:
        {
            "field": {name, display_name, data_type, field_kind, measure, status, used_by},
            "conflict": {code, message, affected_streams},
            "resolutions_by_module": {module_name: resolution_dict | null}
        }

    CURRENCY_CONFLICT items include resolutions_by_module with existing FX resolutions.
    MEASURE_NULL items include resolutions_by_module = {} (resolution = PATCH /measure endpoint).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None

    # AD-5 (HIGH-1): project_id is mandatory on read endpoints to prevent cross-project
    # data leakage. Without it, list_conflicts(project_id=None) would return data from
    # ALL projects to any bearer token holder.
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=422,
        )

    try:
        from core.conflict_resolutions import list_conflicts  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Guard called unconditionally (project_id is now always present).
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
            conflicts = list_conflicts(project_id=project_id, conn=conn)
    except Exception as exc:
        logger.error("conflict_resolutions_api: list_conflicts_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    return JSONResponse(conflicts)


# ---------------------------------------------------------------------------
# GET /api/mdm/conflicts/resolutions
# ---------------------------------------------------------------------------


async def _list_resolutions(request: Request) -> Response:
    """GET /api/mdm/conflicts/resolutions?project_id= -- list fx resolutions."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None

    # AD-5 (HIGH-1): project_id is mandatory on read endpoints to prevent cross-project
    # data leakage. Without it, list_fx_resolutions(project_id=None) performs a full
    # SELECT on app.fx_conflict_resolutions with no WHERE clause, exposing every
    # project's resolutions to any bearer token holder.
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=422,
        )

    try:
        from core.conflict_resolutions import list_fx_resolutions  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Guard called unconditionally (project_id is now always present).
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
            resolutions = list_fx_resolutions(project_id=project_id, conn=conn)
    except Exception as exc:
        logger.error("conflict_resolutions_api: list_resolutions_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    return JSONResponse(resolutions)


# ---------------------------------------------------------------------------
# POST /api/mdm/conflicts/resolutions
# ---------------------------------------------------------------------------


async def _create_resolution(request: Request) -> Response:
    """POST /api/mdm/conflicts/resolutions -- upsert fx conflict resolution.

    Body (JSON):
        {
            "project_id": str,
            "target_field": str,
            "source_module": str,
            "resolved_source_currency": str,  // ISO 4217 code
            "note": str | null                // optional justification
        }

    AD-6: AUCUNE conversion FX ici. La ligne persistée est consommée par dbt staging.
    NON RÉTROACTIF: s'applique à la prochaine publication/reprocess uniquement.

    Response (200): the upserted resolution row.
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

    project_id = (body.get("project_id") or "").strip()
    target_field = (body.get("target_field") or "").strip()
    source_module = (body.get("source_module") or "").strip()
    resolved_source_currency = (body.get("resolved_source_currency") or "").strip()
    note = body.get("note") or None

    if not project_id:
        return JSONResponse(
            {"code": "missing_field", "message": "project_id est requis"},
            status_code=400,
        )
    if not target_field:
        return JSONResponse(
            {"code": "missing_field", "message": "target_field est requis"},
            status_code=400,
        )
    if not source_module:
        return JSONResponse(
            {"code": "missing_field", "message": "source_module est requis"},
            status_code=400,
        )

    try:
        from core.conflict_resolutions import upsert_fx_resolution  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
            result = upsert_fx_resolution(
                project_id=project_id,
                target_field=target_field,
                source_module=source_module,
                resolved_source_currency=resolved_source_currency,
                decided_by=identity or "anonymous",
                note=note,
                conn=conn,
            )
    except ValueError as exc:
        return JSONResponse({"code": "validation_error", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.error("conflict_resolutions_api: create_resolution_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    return JSONResponse(result)


# ---------------------------------------------------------------------------
# DELETE /api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}
# ---------------------------------------------------------------------------


async def _delete_resolution(request: Request) -> Response:
    """DELETE /api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}.

    Response (204): no content on success.
    Response (404): resolution or project not found.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.path_params.get("project_id") or "").strip()
    target_field = (request.path_params.get("target_field") or "").strip()
    source_module = (request.path_params.get("source_module") or "").strip()

    if not project_id or not target_field or not source_module:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "project_id, target_field et source_module sont requis",
            },
            status_code=400,
        )

    try:
        from core.conflict_resolutions import delete_fx_resolution  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
            delete_fx_resolution(
                project_id=project_id,
                target_field=target_field,
                source_module=source_module,
                conn=conn,
            )
    except ValueError as exc:
        return JSONResponse({"code": "not_found", "message": str(exc)}, status_code=404)
    except Exception as exc:
        logger.error("conflict_resolutions_api: delete_resolution_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    return Response(status_code=204)


# ---------------------------------------------------------------------------
# PATCH /api/mdm/conflicts/measure/{field_name}  (MEASURE_NULL resolution)
# ---------------------------------------------------------------------------


async def _resolve_measure(request: Request) -> Response:
    """PATCH /api/mdm/conflicts/measure/{field_name} -- declare measure for MEASURE_NULL.

    Reuses update_target_field (AI-53 -- pas de duplication).
    Body (JSON): {"measure": str}  valid values: sum/average/min/max/count

    Response (200): updated target_field row.

    SCOPE: app.target_fields is a PLATFORM-GLOBAL table (no project_id column).
    The `measure` attribute on a canonical field is intentionally shared across all
    projects (a field like `cost` has one semantic aggregation for the whole platform).
    Consequently _guard_project_access is NOT applicable here -- there is no project
    to scope against. This is consistent with the governance decision in Story 13.1
    (target_fields global, cf AD-5 exception for platform-level writes).

    Auth: Bearer token is still required (authenticated write, not anonymous). Any
    authenticated platform user can declare a measure; this is intentional because
    MEASURE_NULL conflicts are platform-level data-quality issues, not project data.
    If per-project measure scoping is ever needed, a project_id column must first be
    added to app.target_fields (out of scope for 13.x).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    field_name = (request.path_params.get("field_name") or "").strip()
    if not field_name:
        return JSONResponse(
            {"code": "missing_param", "message": "field_name est requis"},
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

    measure = body.get("measure")
    if measure is None:
        return JSONResponse(
            {"code": "missing_field", "message": "measure est requis dans le corps"},
            status_code=400,
        )

    try:
        from core.datamodel import update_target_field  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            result = update_target_field(field_name, {"measure": measure}, conn)
    except ValueError as exc:
        return JSONResponse({"code": "validation_error", "message": str(exc)}, status_code=422)
    except Exception as exc:
        logger.error("conflict_resolutions_api: resolve_measure_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    if result is None:
        return JSONResponse(
            {"code": "not_found", "message": f"Champ '{field_name}' introuvable"},
            status_code=404,
        )

    logger.info(
        "conflict_resolutions_api: measure_resolved field=%s measure=%s by=%s",
        field_name, measure, identity,
    )
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# CONFLICT_RESOLUTION_ROUTES — spliced into admin_api at startup
# ---------------------------------------------------------------------------

CONFLICT_RESOLUTION_ROUTES: list[Route] = [
    Route("/api/mdm/conflicts", _list_conflicts, methods=["GET"]),
    Route("/api/mdm/conflicts/resolutions", _list_resolutions, methods=["GET"]),
    Route("/api/mdm/conflicts/resolutions", _create_resolution, methods=["POST"]),
    Route(
        "/api/mdm/conflicts/resolutions/{project_id}/{target_field}/{source_module}",
        _delete_resolution,
        methods=["DELETE"],
    ),
    Route(
        "/api/mdm/conflicts/measure/{field_name}",
        _resolve_measure,
        methods=["PATCH"],
    ),
]
