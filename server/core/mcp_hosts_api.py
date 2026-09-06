"""Installer toorow dans un hote MCP : le catalogue, le prevol, la remise, la liaison.

AD-43, 2026-08-13. Quatre routes qui composent un parcours unique et ordonne --
lire ce que l hote sait faire, preparer, remettre les instructions, lier la
connexion. Chacune refuse par son propre `_setup_gate_response`, qui reste au
joint parce que c est de l autorisation.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
def _authorize_setup_task(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _authorize_setup_task as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _setup_gate_response(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_gate_response as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_host_context(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_host_context as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_no_store(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_no_store as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _enforce_org_manage(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _enforce_org_manage as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _get_host_catalog(request: Request) -> Response:
    """GET /api/mcp-hosts/catalog -- the maintained host capability catalog.

    Returns an OPAQUE, capability-keyed MAPPING of hosts (E36-NFR03: no brand-first
    ordering). Authenticated + gated only; the catalog is source-agnostic reference
    data with no org-specific content, so no per-resource access check is required.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    from core.host_preflight import host_catalog

    return _setup_no_store(JSONResponse({"hosts": host_catalog()}))

async def _prepare_host_preflight(request: Request) -> Response:
    """POST /api/mcp-hosts/preflight -- record a dated, capability-negotiated preflight.

    Body: {host_key, task_id, org_id, project_id?, expires_in_hours?}
    Enforces auth (AD-14) + manage access on the host_connection task (AD-5) BEFORE
    recording. Returns the dated capabilities/plan/role + UI-support decision.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
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
    from core.host_preflight import preflight_host

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        task_id = str(body.get("task_id") or "")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=task_id, minimum_capability="manage"
            )
            if denied is not None:
                return denied
            result = preflight_host(
                conn,
                host_key=str(body.get("host_key") or ""),
                task_id=task_id,
                org_id=str(body.get("org_id") or ""),
                project_id=body.get("project_id"),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
                expires_in_hours=int(body.get("expires_in_hours", 72)),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(
        Response(
            json.dumps(result),
            status_code=201,
            media_type="application/vnd.toorow.host-preflight+json",
        )
    )

async def _prepare_host_install_handoff(request: Request) -> Response:
    """POST /api/mcp-hosts/preflight/{preflight_id}/handoff

    Hand the install to the host administrator via a minimal purpose-scoped handoff
    (no Toorow data). Enforces auth + manage access on the bound host_connection task.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
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
    from core.host_preflight import prepare_host_install_handoff

    try:
        preflight_id = request.path_params["preflight_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT task_id FROM app.host_preflights WHERE id=%s", (preflight_id,))
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="manage"
            )
            if denied is not None:
                return denied
            result = prepare_host_install_handoff(
                conn,
                preflight_id=preflight_id,
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
                expires_in_hours=int(
                    (json.loads(await request.body() or "{}") or {}).get("expires_in_hours", 72)
                ),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(JSONResponse(result))

async def _bind_host_connection(request: Request) -> Response:
    """POST /api/mcp-hosts/preflight/{preflight_id}/bind

    On a verified install/authorization, reconcile the callback state from SERVER
    evidence and write the Story 36.11 capability-context binding (endpoint/org/
    policy/catalog version). High-risk profiles bind ONLY with a verifiable 64-hex
    workspace_evidence_hash; otherwise only Insights binds (fail closed). No token
    or secret enters this handler, its payload or its response.

    Body: {endpoint_binding, enabled_profiles[], workspace_evidence_hash?, host?,
           workspace_id?, workspace_type?, client_id?, policy_version}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
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
    from core.host_preflight import bind_host_connection

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        preflight_id = request.path_params["preflight_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT task_id FROM app.host_preflights WHERE id=%s", (preflight_id,))
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="manage"
            )
            if denied is not None:
                return denied
            profiles = body.get("enabled_profiles")
            result = bind_host_connection(
                conn,
                preflight_id=preflight_id,
                endpoint_binding=str(body.get("endpoint_binding") or ""),
                enabled_profiles=profiles if isinstance(profiles, list) else ["insights"],
                workspace_evidence_hash=body.get("workspace_evidence_hash"),
                interactive_presence_evidence_hash=body.get(
                    "interactive_presence_evidence_hash"
                ),
                host=body.get("host"),
                workspace_id=body.get("workspace_id"),
                workspace_type=body.get("workspace_type"),
                client_id=body.get("client_id"),
                policy_version=str(body.get("policy_version") or ""),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(JSONResponse(result))

async def _list_capability_contexts(request: Request) -> Response:
    """GET /api/mcp-hosts/contexts?org_id=... -- the capability contexts of one org.

    67-16. The bind of a host wrote a row nothing could ever read back, so an
    operator could not answer "which hosts hold capabilities on this
    organization, and since when". Revoked contexts are RETURNED, carrying their
    cut: a lifecycle list that hides what was revoked cannot evidence a
    revocation. `manage` on the org, like the bind that created the row.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
    org_id = (request.query_params.get("org_id") or "").strip()
    if not org_id:
        return _setup_no_store(
            JSONResponse(
                {"code": "invalid_request", "message": "org_id is required"}, status_code=422
            )
        )
    from core.db import get_connection
    from core.mcp_attestation import list_capability_contexts

    try:
        with get_connection() as conn:
            denied = _enforce_org_manage(org_id, identity, conn, "list_mcp_capability_contexts")
            if denied is not None:
                return denied
            contexts = list_capability_contexts(conn, org_id=org_id)
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(JSONResponse({"contexts": contexts}))


async def _revoke_capability_context(request: Request) -> Response:
    """POST /api/mcp-hosts/contexts/{context_id}/revoke -- cut a bound host context.

    67-16. The capabilities go at the NEXT MCP call, not at token expiry:
    `mcp_profiles._capability_context` re-attests the row on every call and a
    revoked row stops attesting, so the host drops to Insights while its session
    keeps running. Same answer, same reason, as `render_shares.resolve_session`
    and `session_revocation`.

    Body: {reason?}. The row is never deleted -- the cut is a state, not an
    erasure, or the list above could not evidence it.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    if denied := _setup_gate_response():
        return denied
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
    from core.mcp_attestation import read_capability_context, revoke_capability_context

    try:
        raw = await request.body()
        body = json.loads(raw) if raw else {}
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        context_id = request.path_params["context_id"]
        with get_connection() as conn:
            existing = read_capability_context(conn, context_id=context_id)
            denied = _enforce_org_manage(
                existing["org_id"], identity, conn, "revoke_mcp_capability_context"
            )
            if denied is not None:
                return denied
            result = revoke_capability_context(
                conn,
                context_id=context_id,
                reason=str(body.get("reason") or ""),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _host_preflight_error(exc)
    return _setup_no_store(JSONResponse(result))


# ---------------------------------------------------------------------------
# Story 36.14: capability-driven host preflight, install handoff and callback bind.
# Project-scoped, fail-closed via the Epic 36 gate + resolve_strict_resource_access
# (manage, since the bind may enable a high-risk-capable capability context; the
# preflight binds to the host_connection setup task). NO brand-first ordering: the
# catalog is returned as an opaque capability-keyed mapping (E36-NFR03). NO token or
# Toorow data enters any handler, payload or response.
# ---------------------------------------------------------------------------
def _host_preflight_error(exc: Exception) -> Response:
    from core.host_preflight import (
        HostPreflightConflict,
        HostPreflightUnavailable,
        HostPreflightValidationError,
    )
    from core.mcp_attestation import CapabilityContextUnavailable
    from core.operations import OperationIdempotencyConflict

    if isinstance(exc, (HostPreflightUnavailable, CapabilityContextUnavailable)):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, (HostPreflightConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "Host preflight state already changed"
    elif isinstance(exc, (HostPreflightValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: host preflight failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Host preflight unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

MCP_HOSTS_ROUTES_1 = [
    # Story 36.14: capability-driven host preflight, install handoff + bind.
    # Static/catalog route precedes the {preflight_id} routes.
    Route(
        "/api/mcp-hosts/catalog",
        endpoint=_get_host_catalog,
        methods=["GET"],
    ),
    Route(
        "/api/mcp-hosts/preflight",
        endpoint=_prepare_host_preflight,
        methods=["POST"],
    ),
    Route(
        "/api/mcp-hosts/preflight/{preflight_id}/handoff",
        endpoint=_prepare_host_install_handoff,
        methods=["POST"],
    ),
    Route(
        "/api/mcp-hosts/preflight/{preflight_id}/bind",
        endpoint=_bind_host_connection,
        methods=["POST"],
    ),
    # 67-16: the exit the lifecycle never had. Static segment before the
    # {context_id} route, exactly as /catalog precedes {preflight_id} above.
    Route(
        "/api/mcp-hosts/contexts",
        endpoint=_list_capability_contexts,
        methods=["GET"],
    ),
    Route(
        "/api/mcp-hosts/contexts/{context_id}/revoke",
        endpoint=_revoke_capability_context,
        methods=["POST"],
    ),
]
