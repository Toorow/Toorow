"""Bounded recovery, and where a governed dataset is allowed to land.

AD-40, second extraction (2026-08-12). Two subjects that share one seam: both
answer "may this write happen, and against which version", and both refuse with
a named code rather than a stack trace. `prepare`/`confirm` are the AD-27
two-phase contract over synchronize / reload / reprocess; `replace/preflight`,
`append/availability` and `destination-policy` are the governed destination
questions the Workbench asks before it offers either verb.

`rollback` is NOT here: it is owned by the Project-scoped exact-confirmation
route and stays with that family.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _dataset_recovery_error_response(*args, **kwargs):
    """Le plan d'ERREURS de la recuperation, atteint a l'APPEL (AI-286).

    Ses voisins de ce bloc sont des joints d'authentification et de portee ; ce
    n'est pas le cas de celui-ci, et son commentaire disait le contraire. Il
    traduit un `DatasetRecoveryError` en code HTTP stable -- rien a voir avec
    l'authentification.

    Il reste chez `admin_api` pour la meme raison que les autres : UN seul
    endroit ecrit la correspondance, et les deux surfaces qui la lisent y
    arrivent par le meme joint. Le deplacer ici en ferait la copie du module de
    sujet, avec l'orphelin retire d'`admin_api` qui la lirait encore."""
    from core.admin_api import _dataset_recovery_error_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _require_datastream_role(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _resolve_datastream_route_scope(*args, **kwargs):
    """Le joint d'authentification/portee d'`admin_api`, atteint a l'APPEL."""
    from core.admin_api import _resolve_datastream_route_scope as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _prepare_bounded_recovery(request: Request) -> Response:
    """POST /api/datastreams/{id}/bounded/prepare (Member) -- Story 12.11.

    Body: {project_id, kind, reason?, date_from?, date_to_exclusive?, partition?,
    chosen_mapping_version_id?}. Assembles the AD-27 immutable
    proposal (NO durable operation, NO dispatch). 200 with the proposal
    {preparation_id, kind, target, target_versions, interval, impact, quota, ...}.
    BoundedRecoveryError codes -> 422/409/404.
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
    kind = str(body.get("kind") or "").strip()
    if not kind:
        return JSONResponse({"code": "missing_field", "message": "kind est requis"}, 422)
    try:
        from core.bounded_recovery import (  # noqa: PLC0415
            BoundedRecoveryError,
            prepare_bounded_recovery,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            data_project_id = _resolve_datastream_route_scope(conn, ds_id, project_id)
            if data_project_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            org_id = _load_datastream_org_id(conn, ds_id, data_project_id)
            if org_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            try:
                result = prepare_bounded_recovery(
                    conn,
                    org_id=org_id,
                    datastream_id=ds_id,
                    kind=kind,
                    actor=identity or "anonymous",
                    reason=body.get("reason"),
                    date_from=body.get("date_from"),
                    date_to_exclusive=body.get("date_to_exclusive"),
                    partition=body.get("partition"),
                    chosen_mapping_version_id=body.get("chosen_mapping_version_id"),
                )
                conn.commit()
            except BoundedRecoveryError as exc:
                conn.rollback()
                mapped = _bounded_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: prepare_bounded_recovery_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Preparation indisponible"}, 503)
    return JSONResponse(result, 200)

async def _confirm_bounded_recovery(request: Request) -> Response:
    """POST /api/datastreams/{id}/bounded/confirm (Member) -- Story 12.11.

    Body: {project_id, preparation_id}. Re-validates every
    precondition against the live target and routes EXACTLY ONE durable operation
    (never commit_publication / pointer mutation). 200 with {preparation_id,
    operation_id, outcome, replayed, result}. BoundedRecoveryError codes ->
    422/409/404.
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
    preparation_id = str(body.get("preparation_id") or "").strip()
    if not preparation_id:
        return JSONResponse({"code": "missing_field", "message": "preparation_id est requis"}, 422)

    try:
        from core.bounded_recovery import (  # noqa: PLC0415
            BoundedRecoveryError,
            confirm_bounded_recovery,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "member", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            # Scope guard: the preparation must belong to this datastream's org. A
            # cross-project confirm would otherwise leak an out-of-scope proposal.
            data_project_id = _resolve_datastream_route_scope(conn, ds_id, project_id)
            if data_project_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            org_id = _load_datastream_org_id(conn, ds_id, data_project_id)
            if org_id is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"}, 404
                )
            try:
                from core import tracing  # noqa: PLC0415

                server_trace_id = tracing.current_trace_id_hex() or os.urandom(16).hex()
                result = confirm_bounded_recovery(
                    conn,
                    preparation_id=preparation_id,
                    expected_org_id=str(org_id),
                    expected_project_id=data_project_id,
                    expected_datastream_id=ds_id,
                    actor=identity or "anonymous",
                    trace_id=server_trace_id,
                )
                conn.commit()
            except BoundedRecoveryError as exc:
                conn.rollback()
                mapped = _bounded_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: confirm_bounded_recovery_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Confirmation indisponible"}, 503)
    return JSONResponse(result, 200)

async def _preflight_replace_dataset(request: Request) -> Response:
    """POST /api/datastreams/{id}/replace/preflight (Member) -- Story 12.12.

    Body: {project_id, candidate_row_count, force_empty_publish?}. Validates the
    12.12 concurrency + empty pre-checks BEFORE an execution is minted (the caller
    then composes the 12.5 create_execution -> run_dq_gates -> commit_publication).
    200 {ok: True, action: dataset.replace}. concurrent_mutation_active -> 409;
    empty_replacement_blocked -> 422.
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
    candidate_row_count = body.get("candidate_row_count")
    if not isinstance(candidate_row_count, int):
        return JSONResponse(
            {"code": "missing_field", "message": "candidate_row_count (entier) est requis"}, 422
        )
    force_empty_publish = bool(body.get("force_empty_publish", False))
    try:
        from core.dataset_recovery import (  # noqa: PLC0415
            DatasetRecoveryError,
            preflight_replace,
        )
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Member floor; an empty replace additionally needs Owner (owner-on-force).
            minimum_role = "owner" if force_empty_publish else "member"
            role_error = _require_datastream_role(
                project_id, identity, minimum_role, conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                result = preflight_replace(
                    conn,
                    datastream_id=ds_id,
                    project_id=project_id,
                    candidate_row_count=candidate_row_count,
                    force_empty_publish=force_empty_publish,
                )
            except DatasetRecoveryError as exc:
                mapped = _dataset_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: preflight_replace_dataset_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Preflight indisponible"}, 503)
    return JSONResponse(result, 200)

async def _append_availability_dataset(request: Request) -> Response:
    """GET /api/datastreams/{id}/append/availability?project_id=<id> (Viewer).

    Story 12.12. Reports whether Append is available (needs a stable-key contract +
    compatible schema); otherwise presents Replace as the safe fallback. Optional
    candidate_schema_hash / target_schema_hash query params feed the compatibility
    check. 200 {available, fallback_action, reason, ...}.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Token requis"}, 401)
    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse({"code": "missing_param", "message": "project_id est requis"}, 400)
    ds_id = request.path_params.get("id", "")
    candidate_schema_hash = (
        request.query_params.get("candidate_schema_hash") or ""
    ).strip() or None
    target_schema_hash = (request.query_params.get("target_schema_hash") or "").strip() or None
    try:
        from core.dataset_recovery import resolve_append_availability  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            availability = resolve_append_availability(
                conn,
                datastream_id=ds_id,
                project_id=project_id,
                candidate_schema_hash=candidate_schema_hash,
                target_schema_hash=target_schema_hash,
            )
    except Exception as exc:
        logger.error("admin_api: append_availability_dataset_error: %s", exc)
        return JSONResponse({"code": "unavailable", "message": "Disponibilite indisponible"}, 503)
    return JSONResponse(availability, 200)

async def _dataset_destination_policy(request: Request) -> Response:
    """POST /api/datastreams/{id}/destination-policy (Owner) -- Story 12.12.

    Body: {project_id, operation}. Enforces the Owner floor via
    dataset_recovery.enforce_owner_floor for destination-policy operations
    (ownership / access / retention / irreversible deletion). This is the RBAC
    enforcement seam; the actual policy MUTATION is the caller's follow-up (this
    route proves the Owner floor holds). 200 {ok: True, operation} when authorized;
    owner_floor_required -> 403; access_unavailable -> 503.
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
    operation = str(body.get("operation") or "").strip()
    try:
        from core.dataset_recovery import (  # noqa: PLC0415
            OWNER_FLOOR_OPERATIONS,
            DatasetRecoveryError,
            enforce_owner_floor,
        )
        from core.db import get_connection  # noqa: PLC0415

        if operation not in OWNER_FLOOR_OPERATIONS:
            return JSONResponse(
                {
                    "code": "invalid_operation",
                    "message": "operation must be a destination policy operation",
                    "allowed": sorted(OWNER_FLOOR_OPERATIONS),
                },
                422,
            )
        with get_connection() as conn:
            # A cross-project / unknown datastream is a non-disclosing 404 BEFORE the
            # owner check (mirror the role-gate scoping used everywhere else).
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            try:
                enforce_owner_floor(
                    conn,
                    operation=operation,
                    identity=identity or "anonymous",
                    project_id=project_id,
                    datastream_id=ds_id,
                )
            except DatasetRecoveryError as exc:
                mapped = _dataset_recovery_error_response(exc)
                if mapped is not None:
                    return mapped
                raise
    except Exception as exc:
        logger.error("admin_api: dataset_destination_policy_error: %s", exc)
        return JSONResponse(
            {"code": "unavailable", "message": "Verification de politique indisponible"}, 503
        )
    return JSONResponse({"ok": True, "operation": operation}, 200)

def _bounded_recovery_error_response(exc) -> Response | None:
    """Map a BoundedRecoveryError to its stable ``.code`` -> HTTP, or None.

    Lock conflicts (concurrent active execution) -> 409; not_found / wrong_verb ->
    404; every other precondition breach (forbidden_interval, retention_unavailable,
    incompatible_schema, stale_versions, policy_changed, quota_violation,
    missing_exposure, stale_preparation, invalid_kind) -> 422.
    """
    from core.bounded_recovery import BoundedRecoveryError  # noqa: PLC0415

    if not isinstance(exc, BoundedRecoveryError):
        return None
    code = exc.code
    if code == "outcome_unknown":
        return JSONResponse(
            {"code": code, "outcome": "outcome_unknown", "message": exc.message}, 409
        )
    if code == "lock_conflict":
        return JSONResponse({"code": code, "message": exc.message}, 409)
    if code in ("not_found", "wrong_verb"):
        return JSONResponse({"code": "not_found", "message": "Flux de donnees introuvable"}, 404)
    return JSONResponse({"code": code, "message": exc.message}, 422)

def _load_datastream_org_id(conn, ds_id: str, project_id: str) -> str | None:
    """Read app.datastreams.org_id for a (datastream, project) scope, or None.

    Mirrors the inline scoped SELECT convention (_load_external_object). Returns None
    for an out-of-scope / unknown datastream so the caller returns a non-disclosing
    404. bounded_recovery needs org_id (the AD-27 proposal + operation are org-scoped).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT org_id FROM app.datastreams WHERE id = %s AND project_id = %s",
            (ds_id, project_id),
        )
        row = cur.fetchone()
    if row is None or row[0] is None:
        return None
    return row[0]


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : meme
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

DATASTREAM_RECOVERY_ROUTES = [
    # Story 12.11: bounded sync / reload / reprocess (prepare + confirm).
    Route(
        "/api/datastreams/{id}/bounded/prepare",
        endpoint=_prepare_bounded_recovery,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/bounded/confirm",
        endpoint=_confirm_bounded_recovery,
        methods=["POST"],
    ),
    # Governed replace/append remain available; rollback is owned by the
    # Project-scoped Workbench exact-confirmation route.
    Route(
        "/api/datastreams/{id}/replace/preflight",
        endpoint=_preflight_replace_dataset,
        methods=["POST"],
    ),
    Route(
        "/api/datastreams/{id}/append/availability",
        endpoint=_append_availability_dataset,
        methods=["GET"],
    ),
    Route(
        "/api/datastreams/{id}/destination-policy",
        endpoint=_dataset_destination_policy,
        methods=["POST"],
    ),
]
