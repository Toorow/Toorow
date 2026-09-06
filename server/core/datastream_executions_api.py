"""An execution: open it, advance it, reconcile it, read it -- and what it published.

AD-40, second extraction (2026-08-12). Six handlers over one lifecycle. The
state machine itself is `core.execution_states`; these doors parse the request,
check the role and call it. `/observe` sits here rather than with the object
because what it opens IS an execution -- the read-only observation of an
external BigQuery table (story 12.7).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _require_datastream_role(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _create_datastream_execution(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions (Member) -- create a candidate execution.

    Body: {project_id, plan_version_id, mapping_version_id, projection_plan,
           idempotency_key}. The projection_plan must be an executable 12.4 plan.
    201 on success; 409 idempotency_conflict / concurrent_execution_active; 422
    on validation failure.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    plan_version_id = str(body.get("plan_version_id") or "").strip()
    mapping_version_id = str(body.get("mapping_version_id") or "").strip()
    projection_plan = body.get("projection_plan")
    idempotency_key = str(
        body.get("idempotency_key") or request.headers.get("Idempotency-Key") or ""
    ).strip()
    if not plan_version_id or not mapping_version_id or not isinstance(projection_plan, dict):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": "plan_version_id, mapping_version_id et projection_plan sont requis",
            },
            422,
        )

    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ConcurrentExecutionActive,
            IdempotencyConflict,
            InvalidReference,
            PublicationError,
            create_execution,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                record = create_execution(
                    ds_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    projection_plan,
                    identity or "anonymous",
                    idempotency_key,
                    conn,
                )
                conn.commit()
            except IdempotencyConflict:
                conn.rollback()
                return JSONResponse(
                    {"code": "idempotency_conflict", "message": "Cle idempotente reutilisee"},
                    409,
                )
            except ConcurrentExecutionActive as exc:
                conn.rollback()
                return JSONResponse(
                    {
                        "code": "concurrent_execution_active",
                        "message": "An execution is already active",
                        "blocking_execution_id": exc.blocking_execution_id,
                    },
                    409,
                )
            except InvalidReference as exc:
                # Non-existent plan/mapping reference -> opaque, non-disclosing 422.
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Reference invalide"}, 422)
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Requete invalide"}, 422)
    except Exception as exc:
        logger.error("admin_api: create_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Publication indisponible"}, 503)
    return JSONResponse(record, 201)

async def _advance_datastream_execution_state(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions/{exec_id}/state (Member).

    Body: {project_id, new_state, content_hash?, row_count?, error_code?,
           error_detail?}. The publishing/published transitions are INTERNAL to
    commit_publication and rejected here (publication goes through the governed
    confirmation flow, not a direct route).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    new_state = str(body.get("new_state") or "").strip()
    # publishing/published are internal-only (commit_publication owns the pointer).
    if new_state in ("publishing", "published"):
        return JSONResponse(
            {
                "code": "invalid_state_transition",
                "message": "La publication passe par le flux gouverne (transition interne)",
            },
            422,
        )

    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            InvalidStateTransition,
            advance_state,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                record = advance_state(
                    exec_id,
                    None,
                    new_state,
                    identity or "anonymous",
                    conn,
                    project_id=project_id,
                    content_hash=body.get("content_hash"),
                    row_count=body.get("row_count"),
                    error_code=body.get("error_code"),
                    error_detail=body.get("error_detail"),
                )
                conn.commit()
            except ExecutionNotFound:
                conn.rollback()
                return JSONResponse({"code": "not_found", "message": "Execution introuvable"}, 404)
            except InvalidStateTransition as exc:
                conn.rollback()
                return JSONResponse(
                    {"code": exc.code, "message": "Transition d'etat invalide"}, 422
                )
    except Exception as exc:
        logger.error("admin_api: advance_datastream_execution_state_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Publication indisponible"}, 503)
    return JSONResponse(record, 200)

async def _reconcile_datastream_execution(request: Request) -> Response:
    """POST /api/datastreams/{id}/executions/{exec_id}/reconcile (Owner)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            reconcile_execution,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "owner", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = reconcile_execution(exec_id, project_id, conn)
            except ExecutionNotFound:
                return JSONResponse({"code": "not_found", "message": "Execution introuvable"}, 404)
    except Exception as exc:
        logger.error("admin_api: reconcile_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Reconciliation indisponible"}, 503)
    return JSONResponse(result, 200)

async def _get_datastream_execution(request: Request) -> Response:
    """GET /api/datastreams/{id}/executions/{exec_id}?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    exec_id = request.path_params.get("exec_id", "")
    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ExecutionNotFound,
            get_execution,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                record = get_execution(exec_id, project_id, conn)
            except ExecutionNotFound:
                return JSONResponse({"code": "not_found", "message": "Execution introuvable"}, 404)
    except Exception as exc:
        logger.error("admin_api: get_datastream_execution_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Execution indisponible"}, 503)
    return JSONResponse(record, 200)

async def _list_datastream_publications(request: Request) -> Response:
    """GET /api/datastreams/{id}/publications?project_id=<id> (Viewer)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    try:
        from core.datastream_publication import get_publication_log  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            rows = get_publication_log(ds_id, project_id, conn)
    except Exception as exc:
        logger.error("admin_api: list_datastream_publications_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Journal indisponible"}, 503)
    return JSONResponse({"publications": rows})

async def _observe_datastream(request: Request) -> Response:
    """POST /api/datastreams/{id}/observe (Member) -- Story 12.7.

    Body: {project_id, plan_version_id, mapping_version_id, projection_plan,
           probe_result, project_region?, expected_content_hash?, idempotency_key}.
    On a fresh `ok` verdict: 201 {verdict:"ok", execution, virtual_pull_commit,
    observation}. On a blocking verdict / unchanged_noop: 200 {verdict,
    observation, repair?} (the published pointer is left untouched). Concurrent /
    idempotency conflicts -> 409; invalid reference / publication error -> 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)
    project_id = str(body.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_field", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    plan_version_id = str(body.get("plan_version_id") or "").strip()
    mapping_version_id = str(body.get("mapping_version_id") or "").strip()
    projection_plan = body.get("projection_plan")
    probe_result = body.get("probe_result")
    idempotency_key = str(
        body.get("idempotency_key") or request.headers.get("Idempotency-Key") or ""
    ).strip()
    if (
        not plan_version_id
        or not mapping_version_id
        or not isinstance(projection_plan, dict)
        or not isinstance(probe_result, dict)
    ):
        return JSONResponse(
            {
                "code": "missing_field",
                "message": (
                    "plan_version_id, mapping_version_id, projection_plan et "
                    "probe_result sont requis"
                ),
            },
            422,
        )

    try:
        from core.datastream_publication import (  # noqa: PLC0415
            ConcurrentExecutionActive,
            IdempotencyConflict,
            InvalidReference,
            PublicationError,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.external_bq_registration import (  # noqa: PLC0415
            BLOCKING_VERDICTS,
            ObservationInputError,
            observe_and_register,
        )

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            external_object = _load_external_object(conn, ds_id, project_id, plan_version_id)
            if external_object is None:
                # Non-disclosing: unknown plan version / not an external_bq source.
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            try:
                result = observe_and_register(
                    datastream_id=ds_id,
                    project_id=project_id,
                    external_object=external_object,
                    plan_version_id=plan_version_id,
                    mapping_version_id=mapping_version_id,
                    projection_plan=projection_plan,
                    probe_result=probe_result,
                    actor=identity or "anonymous",
                    idempotency_key=idempotency_key,
                    conn=conn,
                    project_region=body.get("project_region"),
                    expected_content_hash=body.get("expected_content_hash"),
                )
                conn.commit()
            except IdempotencyConflict:
                conn.rollback()
                return JSONResponse(
                    {"code": "idempotency_conflict", "message": "Cle idempotente reutilisee"},
                    409,
                )
            except ConcurrentExecutionActive as exc:
                conn.rollback()
                return JSONResponse(
                    {
                        "code": "concurrent_execution_active",
                        "message": "An execution is already active",
                        "blocking_execution_id": exc.blocking_execution_id,
                    },
                    409,
                )
            except InvalidReference as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Reference invalide"}, 422)
            except ObservationInputError:
                conn.rollback()
                return JSONResponse(
                    {"code": "invalid_observation", "message": "Observation invalide"}, 422
                )
            except PublicationError as exc:
                conn.rollback()
                return JSONResponse({"code": exc.code, "message": "Requete invalide"}, 422)
    except Exception as exc:
        logger.error("admin_api: observe_datastream_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Observation indisponible"}, 503)

    verdict = result.get("verdict")
    # Fresh, changed, ok content minted a virtual pull commit + a 12.5 candidate.
    if verdict == "ok" and result.get("execution") is not None:
        return JSONResponse(result, 201)
    # A blocking verdict or an unchanged no-op leaves the published pointer intact:
    # 200 with the observation (+ repair on a blocking verdict), no execution.
    response: dict = {"verdict": verdict, "observation": result.get("observation")}
    observation = result.get("observation")
    if isinstance(observation, dict) and observation.get("repair"):
        response["repair"] = observation["repair"]
    elif verdict in BLOCKING_VERDICTS and "repair" in result:
        response["repair"] = result["repair"]
    return JSONResponse(response, 200)

def _load_external_object(conn, ds_id: str, project_id: str, plan_version_id: str):
    """Read source.external_object from a pinned plan version (project-scoped).

    Returns the external_object dict, or None when the plan version is absent in
    this (datastream, project) scope or is not an external_bq source. Mirrors the
    inline scoped SELECT the publish handler uses -- no new module seam invented.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT normalized_payload
            FROM app.datastream_plan_versions
            WHERE id = %s AND datastream_id = %s AND project_id = %s
            """,
            (plan_version_id, ds_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return None
    payload = row[0]
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except (TypeError, ValueError):
            return None
    if not isinstance(payload, dict):
        return None
    source = payload.get("source")
    if not isinstance(source, dict):
        return None
    external_object = source.get("external_object")
    return external_object if isinstance(external_object, dict) else None


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : meme
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

DATASTREAM_EXECUTION_ROUTES = [
    # Story 12.5: atomic candidate publication. Static/sub-path routes precede
    # the /{id} catch-alls; more-specific /executions/{exec_id}/<verb> routes
    # precede /executions/{exec_id}.
    Route(
        "/api/datastreams/{id}/executions",
        endpoint=_create_datastream_execution,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/executions/{exec_id}/state",
        endpoint=_advance_datastream_execution_state,
        methods=["POST"],
    ),

    Route(
        "/api/datastreams/{id}/executions/{exec_id}/reconcile",
        endpoint=_reconcile_datastream_execution,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/executions/{exec_id}",
        endpoint=_get_datastream_execution,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/publications",
        endpoint=_list_datastream_publications,
        methods=["GET"],
    ),
    # Story 12.7: read-only external BigQuery observation (Member). Static
    # /observe sub-path precedes the /{id} catch-alls.
    Route(
        "/api/datastreams/{id}/observe",
        endpoint=_observe_datastream,
        methods=["POST"],
    ),
]
