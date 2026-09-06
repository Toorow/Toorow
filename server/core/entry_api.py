"""Entrer sans avoir encore de scope : la remise, l echange, la confirmation.

AD-43, sixieme etape, 2026-08-13. Six routes qui repondent AVANT qu une personne
ait un projet : l etat d entree du deploiement, la consommation d une entree
hebergee acceptee, et les trois portes d une remise de mise en route.

Elles partagent ce qui les rend delicates, pas leur adresse : une page d amorce
sans referrer, un jeton echange contre un scope, et une identite qui peut etre
liee ou volontairement ouverte. Une modification du contrat d arrivee -- cookie,
politique de referrer, liaison d identite -- les touche toutes les six et rien
d autre. C est le test de sujet d AD-43, pas une famille d URL.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.instance_claim_api import (
    # Le nom du cookie d'echange est encore lu par le reste d'admin_api ; son
    # proprietaire est le sujet, et ce module l'importe deja pour ses routes.
    _INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE,
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

async def _check_canonical_principal(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_canonical_principal as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

async def _check_invitation_identity(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_invitation_identity as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _invitation_no_store(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _invitation_no_store as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _setup_error(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _setup_error as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

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

# Story 36.6: setup responsibility checklist and minimum handoff seams.
_SETUP_HANDOFF_COOKIE = "toorow_setup_handoff"

async def _get_entry_state(request: Request) -> Response:
    """Return the only valid first-entry transition for this deployment."""
    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    mode = deployment_mode()
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if mode == "hosted" and auth_mode == "disabled":
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM app.organizations
                            WHERE status = 'active'
                              AND id NOT IN ('org_default', 'org_integ-test-project')
                        )
                        """
                    )
                    row = cur.fetchone()
            state = "scoped" if row and row[0] else "local_entry_ready"
        except Exception as exc:
            logger.error("admin_api: local_entry_state failed: %s", type(exc).__name__)
            return _invitation_no_store(
                JSONResponse(
                    {
                        "code": "entry_state_unavailable",
                        "message": "Entry state unavailable.",
                    },
                    status_code=500,
                )
            )
        return _invitation_no_store(JSONResponse({"deployment_mode": mode, "state": state}))

    authorized, principal = await _check_canonical_principal(request)
    if not authorized or principal is None:
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"},
                status_code=401,
            )
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                if mode == "self_hosted":
                    cur.execute(
                        """
                        SELECT
                          EXISTS (SELECT 1 FROM app.instance_claims),
                          EXISTS (
                            SELECT 1
                            FROM app.instance_claims claim
                            WHERE EXISTS (
                              SELECT 1
                              FROM app.instance_members member
                              WHERE member.person_id = %s
                                AND member.status = 'active'
                            )
                            OR EXISTS (
                              SELECT 1
                              FROM app.org_members member
                              WHERE member.org_id = claim.org_id
                                AND member.identity = %s
                                AND member.status = 'active'
                            )
                          )
                        """,
                        (principal.person_id, principal.person_id),
                    )
                    claimed, has_membership = cur.fetchone()
                    state = (
                        "setup_required"
                        if not claimed
                        else "scoped"
                        if has_membership
                        else "invitation_required"
                    )
                else:
                    cur.execute(
                        """
                        SELECT
                          EXISTS (
                            SELECT 1 FROM app.org_members
                            WHERE identity = %s AND status = 'active'
                          ),
                          EXISTS (
                            SELECT 1
                            FROM app.invitations invitation
                            JOIN app.invitation_exchange_sessions exchange
                              ON exchange.invitation_id = invitation.id
                            LEFT JOIN app.hosted_entry_scope_consumptions consumption
                              ON consumption.invitation_id = invitation.id
                            WHERE invitation.org_id IS NULL
                              AND invitation.state = 'accepted'
                              AND invitation.accepted_at IS NOT NULL
                              AND exchange.person_id = %s
                              AND exchange.consumed_at IS NOT NULL
                              AND exchange.accepted_operation_id IS NOT NULL
                              AND consumption.id IS NULL
                          )
                        """,
                        (principal.person_id, principal.person_id),
                    )
                    has_membership, has_entry = cur.fetchone()
                    state = (
                        "scoped"
                        if has_membership
                        else "hosted_entry_ready"
                        if has_entry
                        else "invitation_required"
                    )
    except Exception as exc:
        logger.error("admin_api: entry_state failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "entry_state_unavailable", "message": "Entry state unavailable."},
                status_code=500,
            )
        )

    return _invitation_no_store(JSONResponse({"deployment_mode": mode, "state": state}))

async def _create_hosted_entry_scope(request: Request) -> Response:
    """Consume one accepted hosted ENTRY into the first usable tenant scope."""
    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    if deployment_mode() != "hosted":
        return _invitation_no_store(
            JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        )

    authorized, principal = await _check_canonical_principal(request)
    if not authorized or principal is None:
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"},
                status_code=401,
            )
        )

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    confirmation_id = (request.headers.get("X-Confirmation-Id") or "").strip()
    confirmation_secret = (request.headers.get("X-Confirmation-Secret") or "").strip()
    if not idempotency_key or not confirmation_id or not confirmation_secret:
        return _invitation_no_store(
            JSONResponse(
                {
                    "code": "missing_confirmation",
                    "message": "Idempotency-Key and server confirmation are required.",
                },
                status_code=422,
            )
        )

    from core.entry_confirmations import (  # noqa: PLC0415
        HOSTED_ENTRY_COMMAND,
        EntryConfirmationRefused,
        EntryConfirmationValidationError,
        bind_entry_confirmation_operation,
        consume_entry_confirmation,
    )
    from core.hosted_entry_scope import (  # noqa: PLC0415
        HostedEntryScopeUnavailable,
        HostedEntryScopeValidationError,
        create_hosted_entry_scope,
    )

    try:
        body = json.loads(await request.body())
        payload = _first_scope_confirmation_payload(body)
        from core import tracing  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        workspace_id = (request.headers.get("X-Workspace-Id") or "console")[:256]
        with get_connection() as conn:
            with conn.transaction():
                confirmation = consume_entry_confirmation(
                    conn,
                    confirmation_id=confirmation_id,
                    confirmation_secret=confirmation_secret,
                    actor_person_id=principal.person_id,
                    command_type=HOSTED_ENTRY_COMMAND,
                    request_payload=payload,
                    idempotency_key=idempotency_key,
                    context_reference=f"{HOSTED_ENTRY_COMMAND}:{workspace_id}",
                )
                created = create_hosted_entry_scope(
                    conn,
                    deployment_mode="hosted",
                    person_id=principal.person_id,
                    organization_name=payload["organization_name"],
                    organization_slug=payload["organization_slug"],
                    project_name=payload["project_name"],
                    project_slug=payload["project_slug"],
                    # `.get`, not `[...]`: since the sign-up screens stopped
                    # fabricating a currency and a timezone (AD-9 -- a missing
                    # currency is a surfaced gap, never a default), neither key
                    # is present on an ordinary payload. Required-key access
                    # turned that into a 500 on organization creation.
                    currency=payload.get("currency"),
                    timezone_name=payload.get("timezone"),
                    timezone_suggestion=payload.get("timezone_suggestion"),
                    idempotency_key=idempotency_key,
                    confirmation=confirmation,
                    host_context={"host": "rest", "workspace_id": workspace_id},
                    versions={
                        "policy": os.environ.get("TOOROW_POLICY_VERSION", "v1"),
                        "tool": "rest-v1",
                    },
                    trace_id=tracing.current_trace_id_hex(),
                )
                bind_entry_confirmation_operation(
                    conn, confirmation=confirmation, operation_id=created.operation_id
                )
    except EntryConfirmationRefused as exc:
        return _invitation_no_store(
            JSONResponse(
                {"code": exc.code, "message": "Confirmation is invalid or no longer usable."},
                status_code=409,
            )
        )
    except (
        ValueError,
        TypeError,
        json.JSONDecodeError,
        EntryConfirmationValidationError,
        HostedEntryScopeValidationError,
    ):
        return _invitation_no_store(
            JSONResponse(
                {"code": "invalid_entry_scope", "message": "Entry scope input is invalid."},
                status_code=422,
            )
        )
    except HostedEntryScopeUnavailable:
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Entry scope unavailable."},
                status_code=404,
            )
        )
    except Exception as exc:
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        if (
            isinstance(exc, OperationIdempotencyConflict)
            or getattr(exc, "sqlstate", None) == "23505"
        ):
            return _invitation_no_store(
                JSONResponse(
                    {"code": "conflict", "message": "Entry scope conflicts with existing state."},
                    status_code=409,
                )
            )
        logger.error("admin_api: hosted_entry_scope failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Entry scope creation failed."},
                status_code=500,
            )
        )

    return _invitation_no_store(
        JSONResponse(
            {
                "id": created.org_id,
                "name": payload["organization_name"].strip(),
                "slug": payload["organization_slug"],
                "status": "active",
                "org_id": created.org_id,
                "project_id": created.project_id,
                "journey_id": created.journey_id,
                "operation_id": created.operation_id,
                "audit_event_id": created.audit_event_id,
                "outbox_event_id": created.outbox_event_id,
                "next_url": created.next_url,
                "replayed": created.replayed,
            },
            status_code=201,
        )
    )

async def _issue_hosted_entry_confirmation(request: Request) -> Response:
    from core.entry_confirmations import HOSTED_ENTRY_COMMAND  # noqa: PLC0415

    return await _issue_first_scope_confirmation(
        request, expected_mode="hosted", command_type=HOSTED_ENTRY_COMMAND
    )

async def _setup_handoff_bootstrap(_request: Request) -> Response:
    import secrets as _secrets

    nonce = _secrets.token_urlsafe(18)
    html = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><title>Action de mise en route</title></head>
<body><main><h1>Action de mise en route</h1><p>Vérification du périmètre.</p></main>
<script nonce="__NONCE__">'use strict';const raw=location.hash.startsWith('#handoff=')
?location.hash.slice(9):'';history.replaceState(null,'',location.pathname);
if(raw){fetch('/api/setup/handoffs/exchange',{method:'POST',credentials:'same-origin',
headers:{'Content-Type':'application/json'},body:JSON.stringify({bearer:raw})});}</script>
</body></html>""".replace("__NONCE__", nonce)
    return Response(
        html,
        media_type="text/html",
        headers={
            "Cache-Control": "no-store, max-age=0",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": (
                f"default-src 'none'; script-src 'nonce-{nonce}'; connect-src 'self'; "
                "style-src 'none'; img-src 'none'; frame-ancestors 'none'; base-uri 'none'"
            ),
        },
    )

async def _exchange_setup_handoff(request: Request) -> Response:
    from core.db import get_connection
    from core.setup_responsibilities import exchange_handoff

    # Best-effort identity: a bound handoff requires the authenticated matching
    # identity; an unbound (anonymous, external-actor) handoff stays open.
    authorized, identity = await _check_invitation_identity(request)
    presented_identity = identity if authorized and identity != "anonymous" else None
    try:
        body = json.loads(await request.body())
        bearer = body.get("bearer") if isinstance(body, dict) else None
        with get_connection() as conn:
            result = exchange_handoff(conn, bearer=bearer, presented_identity=presented_identity)
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    response = _setup_no_store(
        JSONResponse(
            {
                "handoff_id": result.handoff_id,
                "task_id": result.task_id,
                "purpose": result.purpose,
                "actor_type": result.actor_type,
                "safe_scope": result.safe_scope,
                "return_path": result.return_path,
            }
        )
    )
    response.set_cookie(
        _SETUP_HANDOFF_COOKIE,
        result.session_value,
        max_age=result.max_age_seconds,
        path="/api/setup/handoffs",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response

async def _revoke_setup_handoff(request: Request) -> Response:
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
    from core.setup_responsibilities import revoke_handoff

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT task_id FROM app.setup_handoffs WHERE id=%s",
                    (request.path_params["handoff_id"],),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
            denied = _authorize_setup_task(
                conn, identity=identity, task_id=row[0], minimum_capability="manage"
            )
            if denied is not None:
                return denied
            result = revoke_handoff(
                conn,
                handoff_id=request.path_params["handoff_id"],
                actor=identity,
                idempotency_key=key,
                host_context=_setup_host_context(request),
                trace_id=tracing.current_trace_id_hex(),
            )
            conn.commit()
    except Exception as exc:
        return _setup_error(exc)
    return _setup_no_store(JSONResponse(result))

def _first_scope_confirmation_payload(body: dict) -> dict:
    """Normalize the exact browser-reviewed payload used by both entry commands."""
    if not isinstance(body, dict):
        raise ValueError("body must be an object")
    return {
        "organization_name": body.get("organization_name"),
        "organization_slug": body.get("organization_slug"),
        "project_name": body.get("project_name"),
        "project_slug": body.get("project_slug"),
        # AI-77/AI-81: this used to inject "EUR" and "Europe/Paris" when the
        # operator sent neither. The payload is what the human confirms and what
        # the confirmation hash certifies -- injecting a constant made them
        # certify a value they never saw, which then reached the row labelled a
        # suggestion. An absent choice stays absent; core.project_provenance
        # turns it into a named platform fallback at write time.
        "currency": body.get("currency"),
        "timezone": body.get("timezone"),
    }

async def _issue_first_scope_confirmation(
    request: Request, *, expected_mode: str, command_type: str
) -> Response:
    """Mint one short-lived console-only confirmation for an exact payload."""
    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    if deployment_mode() != expected_mode:
        return _invitation_no_store(
            JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        )

    authorized, principal = await _check_canonical_principal(request)
    if not authorized or principal is None:
        return _invitation_no_store(
            JSONResponse(
                {"code": "unauthorized", "message": "Authentication required"},
                status_code=401,
            )
        )

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idempotency_key:
        return _invitation_no_store(
            JSONResponse(
                {"code": "missing_idempotency_key", "message": "Idempotency-Key is required."},
                status_code=422,
            )
        )
    workspace_id = (request.headers.get("X-Workspace-Id") or "console")[:256]
    context_reference = f"{command_type}:{workspace_id}"
    if expected_mode == "self_hosted":
        exchange_bearer = request.cookies.get(_INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE, "")
        if not exchange_bearer:
            return _invitation_no_store(
                JSONResponse(
                    {"code": "not_found", "message": "Instance claim unavailable."},
                    status_code=404,
                )
            )
        context_reference = f"{context_reference}:{exchange_bearer}"

    from core.entry_confirmations import (  # noqa: PLC0415
        EntryConfirmationValidationError,
        issue_entry_confirmation,
    )

    try:
        body = json.loads(await request.body())
        payload = _first_scope_confirmation_payload(body)
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            issued = issue_entry_confirmation(
                conn,
                actor_person_id=principal.person_id,
                command_type=command_type,
                request_payload=payload,
                idempotency_key=idempotency_key,
                context_reference=context_reference,
            )
            # `get_connection` closes without committing, and psycopg rolls back
            # on close: without this the caller receives a 201 and a raw secret
            # for a row that never landed, and its confirm answers
            # `confirmation_invalid` -- a lost write wearing a refusal's clothes.
            conn.commit()
    except (ValueError, TypeError, json.JSONDecodeError, EntryConfirmationValidationError):
        return _invitation_no_store(
            JSONResponse(
                {
                    "code": "invalid_confirmation_request",
                    "message": "Confirmation input is invalid.",
                },
                status_code=422,
            )
        )
    except Exception as exc:
        logger.error("admin_api: entry confirmation issue failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "confirmation_unavailable", "message": "Confirmation is unavailable."},
                status_code=500,
            )
        )

    return _invitation_no_store(
        JSONResponse(
            {
                "confirmation_id": issued.confirmation_id,
                "confirmation_secret": issued.confirmation_secret,
                "command_type": issued.command_type,
                "payload_hash": issued.payload_hash,
                "expires_at": issued.expires_at.isoformat(),
            },
            status_code=201,
        )
    )


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

ENTRY_ROUTES_1 = [
    Route("/api/entry-state", endpoint=_get_entry_state, methods=["GET"]),
    Route(
        "/api/entry/scope/confirmation",
        endpoint=_issue_hosted_entry_confirmation,
        methods=["POST"],
    ),
    Route("/api/entry/scope", endpoint=_create_hosted_entry_scope, methods=["POST"]),
]

ENTRY_ROUTES_2 = [
    Route(
        "/api/setup/handoffs/{handoff_id}/revoke",
        endpoint=_revoke_setup_handoff,
        methods=["POST"],
    ),
    Route("/handoff", endpoint=_setup_handoff_bootstrap, methods=["GET"]),
    Route(
        "/api/setup/handoffs/exchange",
        endpoint=_exchange_setup_handoff,
        methods=["POST"],
    ),
]
