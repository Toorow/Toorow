"""First value: the first report a Datastream can answer, and the journeys to it.

AD-43, 2026-08-12. Nine routes that share `/api/projects/…` with the Project
object and have nothing else in common with it: recommending a first report,
saving its draft, previewing it, executing the recent-first pull, rendering and
reproducing it, and reading where a project stands on its way to a first answer.

A change here would never touch the Project CRUD, which is the test AD-42 gives
for "these are two subjects".
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
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._check_auth` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _setup_gate_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._setup_gate_response` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _setup_gate_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_host_context(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._setup_host_context` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _setup_host_context as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_no_store(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._setup_no_store` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _setup_no_store as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _recommend_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_draft import recommend_first_report
    from core.main import get_loaded_modules
    from core.project_access import (
        resolve_provider_account_access,
        resolve_strict_resource_access,
    )
    from core.source_capabilities import (
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="edit", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            # Resolve the datastream's connection + one exposed eligible account.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT current_plan_version_id FROM app.datastreams "
                    "WHERE id=%s AND project_id=%s",
                    (datastream_id, project_id),
                )
                if cur.fetchone() is None:
                    return JSONResponse(
                        {"code": "not_found", "message": "Not found"}, status_code=404
                    )
                cur.execute(
                    """
                    -- Le credential DU datastream, via d.connection_ref_id : c'est
                    -- lui qui sert a aller chercher ses donnees. La jointure
                    -- precedente prenait  n'importe quel credential du meme projet
                    -- puis LIMIT 1 -- des qu'un projet en a plusieurs, elle pouvait
                    -- rendre celui d'un autre fournisseur. Elle serait de surcroit
                    -- devenue vide le jour ou connection_ref.project_id est retire.
                    SELECT cr.id, COALESCE(ca.external_account_id, s.account_id)
                    FROM app.datastreams d
                    JOIN app.connection_ref cr ON cr.id = d.connection_ref_id
                    -- The account the DATASTREAM names (migration 211), falling
                    -- back to the authorization-wide selection for rows created
                    -- before the column existed. Reading the credential's scope
                    -- first meant a second Datastream on a second property was
                    -- described by the first property's account.
                    LEFT JOIN app.credential_accounts ca
                      ON ca.credential_id = cr.id
                     AND ca.source_account_id = d.source_account_id
                    LEFT JOIN LATERAL (
                        SELECT sc.account_id
                        FROM app.connection_account_scope sc
                        WHERE sc.connection_ref_id = cr.id AND sc.account_id IS NOT NULL
                        ORDER BY (sc.state = 'ready') DESC, sc.verified_at DESC NULLS LAST
                        LIMIT 1
                    ) s ON d.source_account_id IS NULL
                    WHERE d.id = %s AND d.project_id = %s AND cr.status = 'active'
                    LIMIT 1
                    """,
                    (datastream_id, project_id),
                )
                conn_row = cur.fetchone()
            if conn_row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            connection_ref_id, exposed_account_id = conn_row[0], conn_row[1]
            account = None
            if exposed_account_id:
                acc = resolve_provider_account_access(
                    identity,
                    conn,
                    credential_id=connection_ref_id,
                    external_account_id=exposed_account_id,
                    beneficiary_org_id=decision.org_id or "",
                    project_id=project_id,
                )
                if acc.allowed:
                    account = {
                        "credential_id": connection_ref_id,
                        "external_account_id": exposed_account_id,
                    }
            try:
                capabilities = get_scoped_source_capabilities(
                    project_id=project_id,
                    connection_ref_id=connection_ref_id,
                    identity=identity,
                    loaded_modules=get_loaded_modules(),
                    conn=conn,
                    external_account_id=exposed_account_id,
                )
            except SourceCapabilitiesNotFound:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            except SourceCapabilitiesUnavailable:
                return JSONResponse(
                    {"code": "operation_failed", "message": "Source unavailable"}, status_code=503
                )
            recommendation = recommend_first_report(
                conn,
                project_id=project_id,
                capabilities=capabilities,
                account=account,
                actor=identity,
                datastream_id=datastream_id,
            )
    except Exception as exc:
        return _first_report_error(exc)
    payload = recommendation.as_dict()
    payload["connection_ref_id"] = connection_ref_id
    return _setup_no_store(JSONResponse(payload))

async def _save_first_report_draft(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core import tracing
    from core.db import get_connection
    from core.first_report_draft import save_first_report_draft
    from core.main import get_loaded_modules
    from core.project_access import resolve_strict_resource_access
    from core.source_capabilities import (
        SourceCapabilitiesNotFound,
        SourceCapabilitiesUnavailable,
        get_scoped_source_capabilities,
    )

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="edit", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            connection_ref_id = str(body.get("connection_ref_id") or "")
            try:
                capabilities = get_scoped_source_capabilities(
                    project_id=project_id,
                    connection_ref_id=connection_ref_id,
                    identity=identity,
                    loaded_modules=get_loaded_modules(),
                    conn=conn,
                )
            except SourceCapabilitiesNotFound:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            except SourceCapabilitiesUnavailable:
                return JSONResponse(
                    {"code": "operation_failed", "message": "Source unavailable"}, status_code=503
                )
            saved = save_first_report_draft(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                connection_ref_id=connection_ref_id,
                report_id=str(body.get("report_id") or ""),
                metrics=list(body.get("metrics") or []),
                dimensions=list(body.get("dimensions") or []),
                grain=list(body.get("grain") or []),
                timezone=str(body.get("timezone") or "UTC"),
                currency=str(body.get("currency") or "unknown"),
                interval=body.get("interval") or {},
                capabilities=capabilities,
                actor=identity,
                idempotency_key=key,
                effective_org_id=decision.org_id or "",
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
    except Exception as exc:
        return _first_report_error(exc)
    return _setup_no_store(
        JSONResponse(
            {
                "draft_id": saved.draft_id,
                "plan_version_id": saved.plan_version_id,
                "mapping_version_id": saved.mapping_version_id,
                "state": saved.state,
                "operation_id": saved.operation_id,
                "audit_event_id": saved.audit_event_id,
                "replayed": saved.replayed,
                "recommendation": saved.recommendation,
            },
            status_code=201,
        )
    )

async def _preview_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    from core.db import get_connection
    from core.first_report_draft import preview_first_report
    from core.project_access import resolve_strict_resource_access

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            preview = preview_first_report(
                conn,
                project_id=project_id,
                plan_version_id=str(body.get("plan_version_id") or ""),
                mapping_version_id=str(body.get("mapping_version_id") or ""),
                actor=identity,
                draft_id=body.get("draft_id"),
                row_count=body.get("row_count"),
                content_hash=body.get("content_hash"),
            )
    except Exception as exc:
        return _first_report_error(exc)
    return _setup_no_store(
        JSONResponse(
            {
                "draft_id": preview.draft_id,
                "plan_version_id": preview.plan_version_id,
                "mapping_version_id": preview.mapping_version_id,
                "executable": preview.executable,
                "row_count": preview.row_count,
                "content_hash": preview.content_hash,
                "dq_issues": preview.dq_issues,
                "evidence": preview.evidence,
                "can_publish": preview.can_publish,
            }
        )
    )

async def _execute_recent_first(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    key = (request.headers.get("Idempotency-Key") or "").strip()
    if not key:
        return _setup_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required"},
                status_code=422,
            )
        )
    from core.db import get_connection
    from core.project_access import resolve_strict_resource_access
    from core.recent_first_publication import execute_recent_first

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="edit", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            result = execute_recent_first(
                conn,
                project_id=project_id,
                actor=identity,
                idempotency_key=key,
                draft_id=body.get("draft_id"),
                datastream_id=datastream_id if not body.get("draft_id") else None,
                row_count=body.get("row_count"),
                content_hash=body.get("content_hash"),
                verification_verdict=body.get("verification_verdict"),
                pull_id=body.get("pull_id"),
                validated_content_hash=body.get("validated_content_hash"),
                force_empty_publish=bool(body.get("force_empty_publish", False)),
                approved=bool(body.get("approved", False)),
            )
    except Exception as exc:
        return _recent_first_error(exc)
    status = 201 if result.published else 200
    return _setup_no_store(JSONResponse(result.as_dict(), status_code=status))

async def _get_recent_first_state(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.project_access import resolve_strict_resource_access
    from core.recent_first_publication import get_recent_first_state

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            state = get_recent_first_state(conn, project_id=project_id, datastream_id=datastream_id)
    except Exception as exc:
        return _recent_first_error(exc)
    return _setup_no_store(JSONResponse(state))

# ---------------------------------------------------------------------------
# Story 36.10: expose first-pull progress + AUTHORITATIVE report readiness.
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (view+; existence-hides on denial). Returns ONE versioned first_report_readiness
# object whose overall / host_cta are SERVER-derived (the UI never re-infers) and
# whose degraded state is honest (never labelled fully ready). Pure read/compose
# surface (E36-NFR05) -- mutates nothing.
# ---------------------------------------------------------------------------
async def _get_first_report_readiness(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_readiness import compute_first_report_readiness
    from core.project_access import resolve_strict_resource_access

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            readiness = compute_first_report_readiness(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                actor=identity,
            )
    except Exception as exc:
        logger.error("admin_api: first-report readiness failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Readiness unavailable"},
                status_code=500,
            )
        )
    return _setup_no_store(JSONResponse(readiness.as_dict()))

# ---------------------------------------------------------------------------
# Story 36.19: tenant-facing first-value funnel read (E36-FR09).
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (view+). The funnel domain (`tenant_journeys`) INDEPENDENTLY re-resolves strict
# resource access for `identity` and existence-hides (empty) on denial; the endpoint
# ALSO gates up-front so an unauthorized caller learns nothing about the project.
# Returns ONLY allowlisted stage/outcome enums + wait-state OWNER TYPES for the
# authorized project's own journeys -- never a pseudonymised cross-tenant cohort, never
# a raw email/org/provider (AD-32). Cross-tenant cohort stays STAFF-only tooling
# (`first_value_funnel.cross_tenant_cohort`) and is deliberately NOT exposed here.
# ---------------------------------------------------------------------------
async def _get_first_value_journeys(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    project_id = request.path_params.get("project_id")
    from core.db import get_connection
    from core.first_value_funnel import tenant_journeys
    from core.project_access import resolve_strict_resource_access

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                # Existence-hiding: an unauthorized caller learns nothing (404).
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            journeys = tenant_journeys(conn, identity=identity, project_id=project_id)
    except Exception as exc:
        logger.error("admin_api: first-value journeys failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "First-value journeys unavailable"},
                status_code=500,
            )
        )
    payload = {
        "project_id": project_id,
        "journeys": [
            {
                "journey_ref": view.journey_ref_hash,
                "stages": view.stages,
                "wait_state_owners": view.wait_state_owners,
            }
            for view in journeys
        ],
    }
    return _setup_no_store(JSONResponse(payload))

# ---------------------------------------------------------------------------
# Story 36.15: render / validate / reproduce the first report (E36-FR05).
# READ-ONLY starter request. Project-scoped, fail-closed via the Epic 36 gate +
# resolve_strict_resource_access (view+; existence-hides on denial). Runs only
# when readiness is ready/degraded-within-policy; returns BOUNDED evidence + an
# optional authenticated deep-link -- NEVER the full dataset (E36-NFR06). The
# reproduce endpoint INDEPENDENTLY re-evaluates access for the SECOND identity and
# existence-hides on denial (no report/first-user leak).
# ---------------------------------------------------------------------------
async def _render_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_render import (
        FirstReportRenderUnavailable,
        render_first_report,
    )
    from core.project_access import resolve_strict_resource_access

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, minimum_capability="view", project_id=project_id
            )
            if not decision.allowed:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            rendered = render_first_report(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                actor=identity,
            )
    except FirstReportRenderUnavailable:
        return _setup_no_store(
            JSONResponse(
                {"code": "not_renderable", "message": "Report is not yet renderable"},
                status_code=409,
            )
        )
    except Exception as exc:
        logger.error("admin_api: first-report render failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Render unavailable"},
                status_code=500,
            )
        )
    return _setup_no_store(JSONResponse(rendered.as_dict()))

async def _reproduce_first_report(request: Request) -> Response:
    denied, identity, project_id, _ = await _first_report_scope(request)
    if denied is not None:
        return denied
    datastream_id = request.path_params.get("datastream_id")
    from core.db import get_connection
    from core.first_report_render import (
        FirstReportRenderUnavailable,
        FirstReportReproductionDenied,
        reproduce_first_report,
    )

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        workspace_ref = body.get("workspace_ref")
        with get_connection() as conn:
            # The SECOND user's access is re-evaluated INDEPENDENTLY inside the
            # domain (resolve_strict_resource_access for `identity`); denial ->
            # existence-hiding not_found. No first-user readiness object leaks.
            rendered = reproduce_first_report(
                conn,
                datastream_id=datastream_id,
                project_id=project_id,
                second_actor=identity,
                workspace_ref=workspace_ref,
            )
    except FirstReportReproductionDenied:
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    except FirstReportRenderUnavailable:
        return _setup_no_store(
            JSONResponse(
                {"code": "not_renderable", "message": "Report is not yet renderable"},
                status_code=409,
            )
        )
    except (json.JSONDecodeError, TypeError):
        return _setup_no_store(
            JSONResponse({"code": "invalid_request", "message": "Invalid body"}, status_code=422)
        )
    except Exception as exc:
        logger.error("admin_api: first-report reproduce failed: %s", type(exc).__name__)
        return _setup_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Reproduce unavailable"},
                status_code=500,
            )
        )
    payload = rendered.as_dict()
    payload["reproduced"] = True
    return _setup_no_store(JSONResponse(payload))

# ---------------------------------------------------------------------------
# Story 36.8: recommend and save a bounded first-report draft, then preview it.
# All three routes are project-scoped and fail-closed via the Epic 36 gate +
# resolve_strict_resource_access. The preview NEVER publishes or advances a
# current pointer (the domain enforces can_publish=False).
# ---------------------------------------------------------------------------
def _first_report_error(exc: Exception) -> Response:
    from core.first_report_draft import (
        FirstReportDraftConflict,
        FirstReportDraftUnavailable,
        FirstReportDraftValidationError,
    )
    from core.operations import OperationIdempotencyConflict

    if isinstance(exc, FirstReportDraftUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, (FirstReportDraftConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "First report draft already saved"
    elif isinstance(exc, (FirstReportDraftValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: first report draft failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "First report draft unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))

async def _first_report_scope(request: Request):
    """Shared fail-closed gate: (denied_response | None, identity, project_id, decision)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return (
            JSONResponse(
                {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
            ),
            identity,
            None,
            None,
        )
    if denied := _setup_gate_response():
        return denied, identity, None, None
    return None, identity, request.path_params.get("project_id"), None

# ---------------------------------------------------------------------------
# Story 36.9: execute and publish the recent-first candidate SAFELY.
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (edit+ to execute; view+ to read state). ONE idempotent candidate; a bad
# candidate NEVER replaces the publication (last-known-good preserved); a ready
# valid candidate publishes atomically. Recent/historical coverage stay separate.
# ---------------------------------------------------------------------------
def _recent_first_error(exc: Exception) -> Response:
    from core.recent_first_publication import (
        RecentFirstConflict,
        RecentFirstUnavailable,
        RecentFirstValidationError,
    )

    if isinstance(exc, RecentFirstUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, RecentFirstConflict):
        status, code, message = 409, "conflict", "Recent-first candidate already running"
    elif isinstance(exc, (RecentFirstValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: recent-first publish failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Recent-first publish unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est epissee
# par `admin_api` a la position que ses routes occupaient : memes chemins,
# memes methodes, meme ordre. La preuve est un dump avant/apres, pas une
# lecture de diff.

FIRST_VALUE_ROUTES_1 = [
    # Story 36.8: bounded first-report draft -- recommend / save / preview.
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/recommend",
        endpoint=_recommend_first_report,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/draft",
        endpoint=_save_first_report_draft,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/preview",
        endpoint=_preview_first_report,
        methods=["POST"],
    ),
    # Story 36.9: execute + publish the recent-first candidate safely; read the
    # separate recent/historical coverage state for the 36.10 readiness object.
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/execute",
        endpoint=_execute_recent_first,
        methods=["POST"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/recent-state",
        endpoint=_get_recent_first_state,
        methods=["GET"],
    ),
    # Story 36.10: one versioned first-report readiness object (progress +
    # authoritative readiness). Server-derived overall / host_cta.
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/readiness",
        endpoint=_get_first_report_readiness,
        methods=["GET"],
    ),
    # Story 36.19: tenant-facing first-value funnel journeys (E36-FR09). Project-
    # scoped, fail-closed (view+); returns ONLY allowlisted enums + wait-state owner
    # types for the authorized project's own journeys. Cross-tenant cohort stays
    # staff tooling and is NOT exposed here.
    Route(
        "/api/projects/{project_id}/first-value/journeys",
        endpoint=_get_first_value_journeys,
        methods=["GET"],
    ),
    # Story 36.15: render / validate the first report (read-only starter
    # request) and reproduce it for a SECOND independently-authorized user.
    # Bounded evidence + optional deep-link -- never the full dataset.
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/render",
        endpoint=_render_first_report,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{datastream_id}/first-report/reproduce",
        endpoint=_reproduce_first_report,
        methods=["POST"],
    ),
]
