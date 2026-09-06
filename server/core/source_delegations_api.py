"""Faire ouvrir une source par quelqu un d autre, et pouvoir le defaire.

AD-43, 2026-08-13. Quatre routes : preparer une delegation, recevoir son retour,
la revoquer -- et revoquer l autorisation qu elle a produite. La revocation
d une autorisation vit ici et non avec les connexions, parce que c est le seul
geste qui defait ce que ces trois routes ont fait.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.connection_revocation import (
    _apply_credential_revocation,
)

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

# --- les handlers, deplaces TELS QUELS -----------------------------------

async def _prepare_source_delegation(request: Request) -> Response:
    """POST /api/source-delegations -- bind a delegation + mint the owner handoff.

    Body: {task_id, source, provider, owner_org_id, beneficiary_org_id,
           requested_scopes[], redirect_allowlist_ref, expires_in_hours?}
    Enforces auth (AD-14) + edit access on the source_authorization task (AD-5)
    BEFORE binding. Returns the delegation id, state, authorize state, PKCE
    challenge (public) and the minimum-scoped handoff -- never a token/secret.
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
    from core.source_delegation import prepare_source_delegation

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        task_id = str(body.get("task_id") or "")
        with get_connection() as conn:
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=task_id, minimum_capability="edit"
            )
            if denied is not None:
                return denied
            result = prepare_source_delegation(
                conn,
                task_id=task_id,
                source=str(body.get("source") or ""),
                provider=str(body.get("provider") or ""),
                owner_org_id=str(body.get("owner_org_id") or ""),
                beneficiary_org_id=str(body.get("beneficiary_org_id") or ""),
                requested_scopes=body.get("requested_scopes"),
                redirect_allowlist_ref=str(body.get("redirect_allowlist_ref") or ""),
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
                expires_in_hours=int(body.get("expires_in_hours", 48)),
            )
            conn.commit()
    except Exception as exc:
        return _delegation_error(exc)
    payload = {
        "delegation_id": result.delegation_id,
        "state": result.state,
        "expires_at": result.expires_at,
        "operation_id": result.operation_id,
        "audit_event_id": result.audit_event_id,
        "authorize_state": result.authorize_state,
        "pkce_challenge": result.pkce_challenge,
        "pkce_method": result.pkce_method,
        "replayed": result.replayed,
    }
    if result.handoff.delivery_url:
        payload["delivery_handoff"] = {
            "url": result.handoff.delivery_url,
            "single_return": True,
        }
    return _setup_no_store(
        Response(
            json.dumps(payload),
            status_code=201,
            media_type="application/vnd.toorow.source-delegation+json",
        )
    )

async def _source_delegation_callback(request: Request) -> Response:
    """POST /api/source-delegations/{delegation_id}/callback

    The delegated OAuth callback lands here (SameSite=Lax return). Verifies the
    exact redirect allow-list + HMAC state + nonce + PKCE BEFORE any transition,
    then exposes ONLY the exact account and REVALIDATES connection health + exact
    exposure. No token/secret enters this handler, its payload, or its response.

    Body: {state, redirect_uri, credential_id, external_account_id}
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
    from ulid import ULID

    from core import tracing
    from core.db import get_connection
    from core.source_delegation import complete_source_delegation

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise TypeError("body must be an object")
        delegation_id = request.path_params["delegation_id"]
        with get_connection() as conn:
            result = complete_source_delegation(
                conn,
                delegation_id=delegation_id,
                state_param=str(body.get("state") or ""),
                redirect_uri=str(body.get("redirect_uri") or ""),
                credential_id=str(body.get("credential_id") or ""),
                external_account_id=str(body.get("external_account_id") or ""),
                grant_id=f"grant_{ULID()}",
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _delegation_error(exc)
    response = _setup_no_store(
        JSONResponse(
            {
                "delegation_id": result.delegation_id,
                "state": result.state,
                "task_state": result.task_reconciled_state,
                "exposure": result.exposure,
                "operation_id": result.operation_id,
                "audit_event_id": result.audit_event_id,
                "replayed": result.replayed,
            }
        )
    )
    # SameSite=Lax on any delegation cookie the callback may set (Story 36.7 AC).
    response.headers["Set-Cookie-SameSite-Policy"] = "Lax"
    return response

async def _revoke_source_delegation(request: Request) -> Response:
    """POST /api/source-delegations/{delegation_id}/revoke

    Close a pending delegation without touching any prior valid connection or
    exposure. Enforces auth + edit access on the bound task.
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
    from core.source_delegation import revoke_source_delegation

    try:
        delegation_id = request.path_params["delegation_id"]
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM app.source_delegations WHERE delegation_id=%s",
                    (delegation_id,),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="edit"
            )
            if denied is not None:
                return denied
            result = revoke_source_delegation(
                conn,
                delegation_id=delegation_id,
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _delegation_error(exc)
    return _setup_no_store(JSONResponse(result))

async def _revoke_authorization(request: Request) -> Response:
    """POST /api/authorizations/{connection_id}/revoke -- revoke a credential.

    Story 42.9. Revocation belongs to the credential, not to a project: it is
    reached from "My authorizations" and from the organization view, neither of
    which has a project in hand. Same gate as the project-scoped endpoint -- the
    author, or an owner/admin of the owning organization as a backstop.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )
    subject = identity or "anonymous"
    connection_id = request.path_params.get("connection_id", "")
    if not connection_id:
        return JSONResponse(
            {"code": "missing_id", "message": "connection_id is required"}, status_code=400
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_can_manage_org  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT nango_connection_id, provider, owner_org_id,
                           owner_identity, project_id
                    FROM app.connection_ref
                    WHERE id = %s
                    """,
                    (connection_id,),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Authorization not found"},
                    status_code=404,
                )
            nango_connection_id, provider, owner_org_id, owner_identity, project_id = row
            author_revoke = bool(owner_identity) and owner_identity == subject
            org_backstop = identity_can_manage_org(owner_org_id, subject, conn)
            if not (author_revoke or org_backstop):
                # 404, not 403: a credential the caller may not act on and does not
                # own is not theirs to learn about either.
                return JSONResponse(
                    {"code": "not_found", "message": "Authorization not found"},
                    status_code=404,
                )
            revoked_as = "author" if author_revoke else "org_admin_backstop"
    except Exception as exc:
        logger.error("admin_api: revoke_authorization db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": f"Database error: {exc}"}, status_code=500
        )

    nango_deleted, error = _apply_credential_revocation(
        connection_id=connection_id,
        nango_connection_id=nango_connection_id,
        provider=provider,
        subject=subject,
        revoked_as=revoked_as,
        owner_identity=owner_identity,
        project_id=project_id,
    )
    if error is not None:
        return error
    return JSONResponse(
        {"status": "revoked", "nango_deleted": nango_deleted, "revoked_as": revoked_as},
        status_code=200,
    )

# ---------------------------------------------------------------------------
# Story 36.7: delegated source authorization and exact account exposure.
# The operator prepares a delegation; the credential owner authorizes and
# exposes ONLY the exact account. The callback verifies redirect-allowlist +
# state + nonce + PKCE BEFORE any connection/exposure transition (SameSite=Lax).
# No token/secret ever enters a response or an operation payload.
# ---------------------------------------------------------------------------
def _delegation_error(exc: Exception) -> Response:
    from core.operations import OperationIdempotencyConflict
    from core.source_delegation import (
        DelegationConflict,
        DelegationUnavailable,
        DelegationValidationError,
        DelegationVerificationError,
    )

    if isinstance(exc, DelegationVerificationError):
        # Opaque 400 -- no oracle on redirect/state/nonce/PKCE rejection reason.
        status, code, message = (
            400,
            "invalid_delegation",
            "Delegation request invalid or expired. Start the authorization again.",
        )
    elif isinstance(exc, DelegationUnavailable):
        status, code, message = 404, "not_found", "Not found"
    elif isinstance(exc, (DelegationConflict, OperationIdempotencyConflict)):
        status, code, message = 409, "conflict", "Delegation state already changed"
    elif isinstance(exc, (DelegationValidationError, json.JSONDecodeError, TypeError)):
        status, code, message = 422, "invalid_request", str(exc)
    else:
        logger.error("admin_api: source delegation failed: %s", type(exc).__name__)
        status, code, message = 500, "operation_failed", "Delegation unavailable"
    return _setup_no_store(JSONResponse({"code": code, "message": message}, status_code=status))


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

SOURCE_DELEGATIONS_ROUTES_1 = [
    # Story 36.7: delegated source authorization + exact account exposure.
    # Static route precedes the {delegation_id} routes.
    Route(
        "/api/source-delegations",
        endpoint=_prepare_source_delegation,
        methods=["POST"],
    ),
    Route(
        "/api/source-delegations/{delegation_id}/callback",
        endpoint=_source_delegation_callback,
        methods=["POST"],
    ),
    Route(
        "/api/source-delegations/{delegation_id}/revoke",
        endpoint=_revoke_source_delegation,
        methods=["POST"],
    ),
]

SOURCE_DELEGATIONS_ROUTES_2 = [
    Route(
        "/api/authorizations/{connection_id}/revoke",
        endpoint=_revoke_authorization,
        methods=["POST"],
    ),
]
