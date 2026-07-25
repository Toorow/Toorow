"""toorow -- Media plan REST route handlers (Story 22.1, FR38 / CAP-26).

Exports MEDIAPLAN_ROUTES: list[Route] -- spliced into admin_api.router at startup.

AD-5 scoping (pattern: core.context_api): writes require Member+ of the project,
reads require Viewer+; cross-project denials are audited and return 404 (existence
is never disclosed). Business errors from core.mediaplan_store map to 4xx; 5xx
bodies are opaque (never str(exc) -- lesson 12.3).

Routes:
  POST   /api/projects/{project_id}/mediaplans            -> create plan
  GET    /api/projects/{project_id}/mediaplans            -> list plans
  GET    /api/mediaplans/{plan_id}                        -> plan detail (active version + lines)
  POST   /api/mediaplans/{plan_id}/versions               -> create candidate version (lines)
  GET    /api/mediaplans/{plan_id}/versions               -> list versions
  POST   /api/mediaplans/versions/{version_id}/publish    -> publish a candidate version
  GET    /api/mediaplans/{plan_id}/diff?from=..&to=..     -> diff two versions
  PUT    /api/mediaplans/{plan_id}/lines/{line_key}/mappings -> set a line's mappings (22.3)
  GET    /api/mediaplans/{plan_id}/mappings               -> list mappings by line (22.3)
  GET    /api/mediaplans/{plan_id}/unmapped-actuals       -> unmapped perimeter spend (22.3)
  GET    /api/mediaplans/{plan_id}/pacing                  -> plan-vs-actual pacing (22.4)
  PUT    /api/mediaplans/{plan_id}/import-contracts/{sheet_name} -> upsert import contract (22.2)
  GET    /api/mediaplans/{plan_id}/import-contracts         -> list import contracts (22.2)
  POST   /api/mediaplans/{plan_id}/import                   -> import xlsx into a candidate (22.2)
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import ACTION_CROSS_SCOPE_ATTEMPT, write_audit_row

logger = logging.getLogger(__name__)

# DoS guard (F-5): cap the uploaded xlsx. 15 Mo of decoded bytes; base64 inflates
# the payload ~4/3, so ~20 Mo of text -- guarded BEFORE decoding to avoid
# materialising a huge byte buffer from a crafted body.
MAX_IMPORT_FILE_BYTES = 15 * 1024 * 1024
MAX_IMPORT_FILE_BASE64 = (MAX_IMPORT_FILE_BYTES * 4) // 3 + 4


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _check_project_role(
    conn: Any, project_id: str, identity: str, minimum_role: str
) -> bool:
    """Return whether ``identity`` meets ``minimum_role`` on ``project_id``."""
    from core.project_access import identity_has_project_role  # noqa: PLC0415

    try:
        return identity_has_project_role(project_id, identity, minimum_role, conn)
    except Exception:
        return False


def _audit_cross_scope(
    identity: str, resource_id: str, attempted_project_id: str | None = None
) -> None:
    write_audit_row(
        identity=identity,
        action=ACTION_CROSS_SCOPE_ATTEMPT,
        provider_account="platform",
        connection_ref="",
        metadata={"resource_id": resource_id, "attempted_project_id": attempted_project_id},
    )


def _unauthorized() -> JSONResponse:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentification requise"}, status_code=401
    )


def _not_found(message: str = "Ressource introuvable") -> JSONResponse:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _is_uuid(value: str) -> bool:
    """Guard: malformed ids must 404 like unknown ones (never a DB-cast 500)."""
    try:
        UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _plan_project_id(conn: Any, plan_id: str) -> str | None:
    """Resolve a plan's project_id (None if the plan does not exist)."""
    with conn.cursor() as cur:
        cur.execute("SELECT project_id FROM app.media_plans WHERE id = %s", (plan_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _version_plan(conn: Any, version_id: str) -> tuple[str, str] | None:
    """Resolve (plan_id, project_id) for a version id (None if missing)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.plan_id, p.project_id
            FROM app.media_plan_versions v
            JOIN app.media_plans p ON p.id = v.plan_id
            WHERE v.id = %s
            """,
            (version_id,),
        )
        row = cur.fetchone()
    return (row[0], row[1]) if row else None


def _store_error_response(exc: Exception) -> JSONResponse | None:
    """Map a typed store error to a 4xx JSONResponse, or None if not one."""
    from core.mediaplan_store import (  # noqa: PLC0415
        MediaPlanNotFoundError,
        MediaPlanStateError,
        MediaPlanValidationError,
    )

    if isinstance(exc, MediaPlanNotFoundError):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=404)
    if isinstance(exc, MediaPlanStateError):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=409)
    if isinstance(exc, MediaPlanValidationError):
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)
    return None


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


async def _create_plan(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = request.path_params.get("project_id", "")
    try:
        body = await request.json()
    except Exception:
        body = {}

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import create_plan  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, "mediaplan_create", project_id)
                return _not_found("Projet introuvable")

            plan = create_plan(
                conn,
                project_id=project_id,
                name=body.get("name", ""),
                currency=body.get("currency", "EUR"),
                created_by=identity,
            )
            conn.commit()
            return JSONResponse(plan, status_code=201)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: create_plan error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la création du plan"},
            status_code=500,
        )


async def _list_plans(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    project_id = request.path_params.get("project_id", "")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import list_plans  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, "mediaplans_list", project_id)
                return _not_found("Projet introuvable")

            plans = list_plans(conn, project_id=project_id)
            return JSONResponse({"plans": plans})
    except Exception as exc:
        logger.warning("mediaplan_api: list_plans error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des plans"},
            status_code=500,
        )


async def _get_plan(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import get_plan  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            plan = get_plan(conn, plan_id=plan_id)
            if plan is None:
                return _not_found("Plan introuvable")
            return JSONResponse(plan)
    except Exception as exc:
        logger.warning("mediaplan_api: get_plan error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération du plan"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


async def _create_version(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")
    try:
        body = await request.json()
    except Exception:
        body = {}

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import create_version_with_lines  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            version = create_version_with_lines(
                conn,
                plan_id=plan_id,
                lines=body.get("lines", []),
                source_note=body.get("source_note"),
                created_by=identity,
            )
            conn.commit()
            return JSONResponse(version, status_code=201)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: create_version error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la création de la version"},
            status_code=500,
        )


async def _list_versions(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import list_versions  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            versions = list_versions(conn, plan_id=plan_id)
            return JSONResponse({"versions": versions})
    except Exception as exc:
        logger.warning("mediaplan_api: list_versions error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des versions"},
            status_code=500,
        )


async def _publish_version(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    version_id = request.path_params.get("version_id", "")
    if not _is_uuid(version_id):
        return _not_found("Version introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import publish_version  # noqa: PLC0415

    try:
        with get_connection() as conn:
            resolved = _version_plan(conn, version_id)
            if resolved is None:
                return _not_found("Version introuvable")
            _plan_id, project_id = resolved
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, version_id, project_id)
                return _not_found("Version introuvable")

            version = publish_version(conn, version_id=version_id, published_by=identity)
            conn.commit()
            return JSONResponse(version)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: publish_version error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la publication de la version"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Diff
# ---------------------------------------------------------------------------


async def _diff_versions(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    version_from = (request.query_params.get("from") or "").strip()
    version_to = (request.query_params.get("to") or "").strip()

    if not version_from or not version_to:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "Les paramètres « from » et « to » sont requis.",
            },
            status_code=422,
        )
    if not _is_uuid(plan_id) or not _is_uuid(version_from) or not _is_uuid(version_to):
        return _not_found("Plan ou version introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_store import diff_versions  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            # Both versions must belong to this plan (no cross-plan diff).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*) FROM app.media_plan_versions
                    WHERE plan_id = %s AND id IN (%s, %s)
                    """,
                    (plan_id, version_from, version_to),
                )
                count = int(cur.fetchone()[0])
            if count != len({version_from, version_to}):
                return _not_found("Version introuvable pour ce plan")

            diff = diff_versions(conn, version_a=version_from, version_b=version_to)
            return JSONResponse(diff)
    except Exception as exc:
        logger.warning("mediaplan_api: diff_versions error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors du calcul du diff"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Mappings (Story 22.3, FR38 / CAP-26)
# ---------------------------------------------------------------------------


async def _set_line_mappings(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    line_key = request.path_params.get("line_key", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")
    if not line_key:
        return _not_found("Ligne introuvable")
    try:
        body = await request.json()
    except Exception:
        body = {}

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import set_line_mappings  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            result = set_line_mappings(
                conn,
                plan_id=plan_id,
                line_key=line_key,
                entries=body.get("mappings", []),
                actor=identity,
            )
            conn.commit()
            return JSONResponse(result)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: set_line_mappings error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'enregistrement des mappings"},
            status_code=500,
        )


async def _list_mappings(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import list_mappings  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            return JSONResponse(list_mappings(conn, plan_id=plan_id))
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: list_mappings error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des mappings"},
            status_code=500,
        )


async def _list_unmapped_actuals(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_mapping import list_unmapped_actuals  # noqa: PLC0415
    from core.warehouse import query_campaign_spend  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            result = list_unmapped_actuals(
                conn, plan_id=plan_id, campaign_spend_fn=query_campaign_spend
            )
            return JSONResponse(result)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        # WarehouseUnavailable (and any other read failure): opaque 5xx, never a
        # fake empty perimeter (AD-9 / story rule "JAMAIS filtré silencieusement").
        logger.warning("mediaplan_api: list_unmapped_actuals error: %s", exc)
        return JSONResponse(
            {
                "code": "warehouse_unavailable",
                "message": "Dépenses réelles indisponibles pour le moment",
            },
            status_code=503,
        )


# ---------------------------------------------------------------------------
# Pacing (Story 22.4, FR38 / CAP-26) -- read-only plan-vs-actual.
# ---------------------------------------------------------------------------


async def _get_pacing(request: Request) -> Response:
    """GET the plan-vs-actual pacing for a plan (Viewer+).

    Returns the by-line, by-channel and by-plan pacing (each row already carrying its
    provenance: plan_version_id + actual_pull_id_min/max/count). Every ratio is
    NULL-honest (pace NULL when allocated-to-date=0; actual NULL for plan-only lines) --
    the mart never fabricates a 0 (AD-9). A 503 (opaque) is returned when the warehouse
    is unavailable -- never a fake empty pacing (story rule "JAMAIS filtré
    silencieusement").
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.warehouse import query_plan_vs_actual  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

        # The pacing read is a pure warehouse read (no Postgres write) -- run it
        # OUTSIDE the Postgres connection scope. project_id is resolved + authorised
        # above (AD-5); the mart is also project-scoped in SQL.
        result = query_plan_vs_actual(project_id=project_id, plan_id=plan_id)
        return JSONResponse(
            {
                "plan_id": plan_id,
                "project_id": project_id,
                "lines": result.get("lines", []),
                "channels": result.get("channels", []),
                # Epic review E3-F-1: the mart returns 0..1 plan-level rows as a
                # list; the route contract is ONE object or null (the console
                # types PacingPlan | null -- a leaked list rendered silently
                # empty). Unwrap here: the route owns its contract.
                "plan": (result.get("plan") or [None])[0],
            }
        )
    except Exception as exc:
        # WarehouseUnavailable (and any other read failure): opaque 503, never a fake
        # empty pacing (AD-9 / story rule).
        logger.warning("mediaplan_api: get_pacing error: %s", exc)
        return JSONResponse(
            {
                "code": "warehouse_unavailable",
                "message": "Pacing indisponible pour le moment",
            },
            status_code=503,
        )


# ---------------------------------------------------------------------------
# Import contracts & Excel import (Story 22.2, FR38 / CAP-26)
# ---------------------------------------------------------------------------


async def _set_import_contract(request: Request) -> Response:
    """PUT a reusable per-sheet import contract (Member+). Upsert on (plan, sheet)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    sheet_name = request.path_params.get("sheet_name", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")
    if not sheet_name.strip():
        return JSONResponse(
            {"code": "invalid_param", "message": "Le nom d'onglet est requis."},
            status_code=422,
        )
    try:
        body = await request.json()
    except Exception:
        body = {}

    from core.audit import (  # noqa: PLC0415
        ACTION_MEDIA_PLAN_IMPORT_CONTRACT_SET,
        insert_audit_row,
    )
    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_import import validate_contract  # noqa: PLC0415

    try:
        contract = validate_contract(body.get("contract", body))
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        return JSONResponse(
            {"code": "invalid_param", "message": "Contrat d'import invalide."},
            status_code=422,
        )

    try:
        import json  # noqa: PLC0415

        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.media_plan_import_contracts
                        (plan_id, sheet_name, contract, created_by, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, now(), now())
                    ON CONFLICT (plan_id, sheet_name) DO UPDATE
                        SET contract = EXCLUDED.contract, updated_at = now()
                    RETURNING id, plan_id, sheet_name, contract, created_by,
                              created_at, updated_at
                    """,
                    (plan_id, sheet_name.strip(), json.dumps(contract), identity),
                )
                row = cur.fetchone()
            insert_audit_row(
                conn,
                identity=identity,
                action=ACTION_MEDIA_PLAN_IMPORT_CONTRACT_SET,
                provider_account="platform",
                connection_ref="",
                metadata={"plan_id": plan_id, "sheet_name": sheet_name.strip()},
            )
            conn.commit()
            return JSONResponse(
                {
                    "id": str(row[0]),
                    "plan_id": str(row[1]),
                    "sheet_name": row[2],
                    "contract": row[3],
                    "created_by": row[4],
                    "created_at": row[5].isoformat(),
                    "updated_at": row[6].isoformat(),
                },
                status_code=200,
            )
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: set_import_contract error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'enregistrement du contrat"},
            status_code=500,
        )


async def _list_import_contracts(request: Request) -> Response:
    """GET the reusable import contracts of a plan (Viewer+)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_import import load_contracts  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "viewer"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            contracts = load_contracts(conn, plan_id=plan_id)
            return JSONResponse({"contracts": contracts})
    except Exception as exc:
        logger.warning("mediaplan_api: list_import_contracts error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la récupération des contrats"},
            status_code=500,
        )


async def _import_workbook(request: Request) -> Response:
    """POST an xlsx to import into a CANDIDATE version (Member+).

    Upload format: JSON body {"file_base64": "<base64 of the .xlsx bytes>"}. The
    repo has no existing multipart/upload helper in server/core, so a base64 JSON
    body is used (documented). The import is NEVER destructive: publication stays
    an explicit second step (POST /mediaplans/versions/{id}/publish).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()

    plan_id = request.path_params.get("plan_id", "")
    if not _is_uuid(plan_id):
        return _not_found("Plan introuvable")
    try:
        body = await request.json()
    except Exception:
        body = {}

    import base64  # noqa: PLC0415

    raw = body.get("file_base64")
    if not isinstance(raw, str) or not raw.strip():
        return JSONResponse(
            {"code": "invalid_param", "message": "Le champ « file_base64 » est requis."},
            status_code=422,
        )
    # F-5: cap the base64 length BEFORE decoding (never materialise a bomb buffer).
    if len(raw) > MAX_IMPORT_FILE_BASE64:
        return JSONResponse(
            {"code": "invalid_param", "message": "Fichier trop volumineux (max 15 Mo)."},
            status_code=422,
        )
    try:
        file_bytes = base64.b64decode(raw, validate=True)
    except Exception:
        return JSONResponse(
            {"code": "invalid_param", "message": "« file_base64 » n'est pas du base64 valide."},
            status_code=422,
        )
    # F-5: re-check the decoded size (base64 padding/whitespace could sneak past).
    if len(file_bytes) > MAX_IMPORT_FILE_BYTES:
        return JSONResponse(
            {"code": "invalid_param", "message": "Fichier trop volumineux (max 15 Mo)."},
            status_code=422,
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.mediaplan_import import import_workbook  # noqa: PLC0415

    try:
        with get_connection() as conn:
            project_id = _plan_project_id(conn, plan_id)
            if project_id is None:
                return _not_found("Plan introuvable")
            if not _check_project_role(conn, project_id, identity, "member"):
                _audit_cross_scope(identity, plan_id, project_id)
                return _not_found("Plan introuvable")

            report = import_workbook(
                conn, plan_id=plan_id, file_bytes=file_bytes, actor=identity
            )
            conn.commit()
            return JSONResponse(report, status_code=201)
    except Exception as exc:
        mapped = _store_error_response(exc)
        if mapped is not None:
            return mapped
        logger.warning("mediaplan_api: import_workbook error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'import du fichier"},
            status_code=500,
        )


# NOTE: static path segments precede parametrised ones so Starlette matches the
# static route first (e.g. /mediaplans/versions/{id}/publish before nothing that
# would shadow it). The plan-scoped and version-scoped roots are disjoint.
# The {line_key:path} converter allows a line_key that contains '/' (e.g.
# 'digital/meta'); the static '/mappings' suffix keeps the PUT route unambiguous.
MEDIAPLAN_ROUTES: list[Route] = [
    Route(
        "/api/projects/{project_id}/mediaplans", endpoint=_create_plan, methods=["POST"]
    ),
    Route("/api/projects/{project_id}/mediaplans", endpoint=_list_plans, methods=["GET"]),
    Route(
        "/api/mediaplans/versions/{version_id}/publish",
        endpoint=_publish_version,
        methods=["POST"],
    ),
    Route("/api/mediaplans/{plan_id}/versions", endpoint=_create_version, methods=["POST"]),
    Route("/api/mediaplans/{plan_id}/versions", endpoint=_list_versions, methods=["GET"]),
    Route("/api/mediaplans/{plan_id}/diff", endpoint=_diff_versions, methods=["GET"]),
    # Story 22.3: mapping surface (line_key may contain '/', hence :path).
    Route(
        "/api/mediaplans/{plan_id}/lines/{line_key:path}/mappings",
        endpoint=_set_line_mappings,
        methods=["PUT"],
    ),
    Route("/api/mediaplans/{plan_id}/mappings", endpoint=_list_mappings, methods=["GET"]),
    Route(
        "/api/mediaplans/{plan_id}/unmapped-actuals",
        endpoint=_list_unmapped_actuals,
        methods=["GET"],
    ),
    Route("/api/mediaplans/{plan_id}/pacing", endpoint=_get_pacing, methods=["GET"]),
    # Story 22.2: Excel import contracts + import. Static suffixes keep these
    # disjoint from the {plan_id} catch-all below. sheet_name may contain spaces
    # or '/', hence :path.
    Route(
        "/api/mediaplans/{plan_id}/import-contracts/{sheet_name:path}",
        endpoint=_set_import_contract,
        methods=["PUT"],
    ),
    Route(
        "/api/mediaplans/{plan_id}/import-contracts",
        endpoint=_list_import_contracts,
        methods=["GET"],
    ),
    Route("/api/mediaplans/{plan_id}/import", endpoint=_import_workbook, methods=["POST"]),
    Route("/api/mediaplans/{plan_id}", endpoint=_get_plan, methods=["GET"]),
]
