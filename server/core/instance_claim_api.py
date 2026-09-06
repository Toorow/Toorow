"""Revendiquer une instance auto-hebergee, une fois et une seule.

AD-43, 2026-08-13. Quatre routes du parcours d entree auto-heberge : l echange
du jeton d amorcage, la session de revendication, sa confirmation exacte et la
revendication elle-meme. Le plafond d une organisation par personne et le refus
d une seconde revendication sont des REGLES et vivent ailleurs ; ces routes les
appellent.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_canonical_principal(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_canonical_principal as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _first_scope_confirmation_payload(*args, **kwargs):
    """La confirmation exacte de la premiere entree -- chez `entry_api` depuis AD-43.

    Le transfert reste un import a l'APPEL : la revendication auto-hebergee et
    l'entree hebergee posent la MEME question exacte, et c'est `entry_api` qui
    la porte.
    """
    from core.entry_api import _first_scope_confirmation_payload as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

def _invitation_no_store(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _invitation_no_store as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

async def _issue_first_scope_confirmation(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.entry_api import _issue_first_scope_confirmation as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

_INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE = "toorow_instance_bootstrap_exchange"

async def _exchange_instance_bootstrap(request: Request) -> Response:
    """Exchange the fragment-delivered installer bearer before authentication."""
    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    if deployment_mode() != "self_hosted":
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
    from core.self_hosted_instance_claim import (  # noqa: PLC0415
        SelfHostedClaimUnavailable,
        SelfHostedClaimValidationError,
        exchange_bootstrap_capability,
    )

    try:
        body = json.loads(await request.body())
        if not isinstance(body, dict):
            raise ValueError("body must be an object")
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            exchanged = exchange_bootstrap_capability(
                conn,
                deployment_mode="self_hosted",
                bootstrap_bearer=body.get("bootstrap_bearer"),
            )
    except (ValueError, TypeError, json.JSONDecodeError, SelfHostedClaimValidationError):
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Instance claim unavailable."},
                status_code=404,
            )
        )
    except SelfHostedClaimUnavailable:
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Instance claim unavailable."},
                status_code=404,
            )
        )
    except Exception as exc:
        logger.error("admin_api: instance_bootstrap_exchange failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Instance claim unavailable."},
                status_code=500,
            )
        )

    max_age = max(
        1,
        min(900, int((exchanged.expires_at - datetime.now(timezone.utc)).total_seconds())),
    )
    response = _invitation_no_store(JSONResponse({"ready_to_claim": True}))
    response.set_cookie(
        _INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE,
        exchanged.session_bearer,
        max_age=max_age,
        path="/api/instance/claim",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response

async def _get_self_hosted_claim_session(request: Request) -> Response:
    """Resume a valid tokenless claim session without exposing capability state."""
    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    if deployment_mode() != "self_hosted":
        return _invitation_no_store(
            JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)
        )

    exchange_bearer = request.cookies.get(_INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE, "")
    if not exchange_bearer:
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Instance claim unavailable."},
                status_code=404,
            )
        )

    from core.db import get_connection  # noqa: PLC0415
    from core.self_hosted_instance_claim import (  # noqa: PLC0415
        SelfHostedClaimValidationError,
        bootstrap_exchange_session_is_ready,
    )

    try:
        with get_connection() as conn:
            ready = bootstrap_exchange_session_is_ready(
                conn,
                deployment_mode="self_hosted",
                bootstrap_exchange_bearer=exchange_bearer,
            )
    except SelfHostedClaimValidationError:
        ready = False
    except Exception as exc:
        logger.error("admin_api: self_hosted_claim_session failed: %s", type(exc).__name__)
        return _invitation_no_store(
            JSONResponse(
                {"code": "operation_failed", "message": "Instance claim unavailable."},
                status_code=500,
            )
        )

    if not ready:
        return _invitation_no_store(
            JSONResponse(
                {"code": "not_found", "message": "Instance claim unavailable."},
                status_code=404,
            )
        )
    return _invitation_no_store(JSONResponse({"ready_to_claim": True}))

async def _issue_instance_claim_confirmation(request: Request) -> Response:
    from core.entry_confirmations import INSTANCE_CLAIM_COMMAND  # noqa: PLC0415

    return await _issue_first_scope_confirmation(
        request, expected_mode="self_hosted", command_type=INSTANCE_CLAIM_COMMAND
    )

async def _claim_self_hosted_instance(request: Request) -> Response:
    """Claim one unclaimed self-hosted instance and create its first usable scope."""
    from core.deployment_mode import deployment_mode  # noqa: PLC0415

    if deployment_mode() != "self_hosted":
        return JSONResponse({"code": "not_found", "message": "Not found"}, status_code=404)

    authorized, principal = await _check_canonical_principal(request)
    if not authorized or principal is None:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"},
            status_code=401,
        )

    exchange_bearer = request.cookies.get(_INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE, "")
    if not exchange_bearer:
        return JSONResponse(
            {"code": "not_found", "message": "Instance claim unavailable."},
            status_code=404,
        )

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip()
    confirmation_id = (request.headers.get("X-Confirmation-Id") or "").strip()
    confirmation_secret = (request.headers.get("X-Confirmation-Secret") or "").strip()
    if not idempotency_key or not confirmation_id or not confirmation_secret:
        return JSONResponse(
            {
                "code": "missing_confirmation",
                "message": "Idempotency-Key and server confirmation are required.",
            },
            status_code=422,
        )

    from core.entry_confirmations import (  # noqa: PLC0415
        INSTANCE_CLAIM_COMMAND,
        EntryConfirmationRefused,
        EntryConfirmationValidationError,
        bind_entry_confirmation_operation,
        consume_entry_confirmation,
    )
    from core.self_hosted_instance_claim import (  # noqa: PLC0415
        SelfHostedClaimUnavailable,
        SelfHostedClaimValidationError,
        claim_self_hosted_instance,
    )

    try:
        body = json.loads(await request.body())
        payload = _first_scope_confirmation_payload(body)
        from core import tracing  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        workspace_id = (request.headers.get("X-Workspace-Id") or "console")[:256]
        context_reference = f"{INSTANCE_CLAIM_COMMAND}:{workspace_id}:{exchange_bearer}"
        with get_connection() as conn:
            with conn.transaction():
                confirmation = consume_entry_confirmation(
                    conn,
                    confirmation_id=confirmation_id,
                    confirmation_secret=confirmation_secret,
                    actor_person_id=principal.person_id,
                    command_type=INSTANCE_CLAIM_COMMAND,
                    request_payload=payload,
                    idempotency_key=idempotency_key,
                    context_reference=context_reference,
                )
                claimed = claim_self_hosted_instance(
                    conn,
                    deployment_mode="self_hosted",
                    bootstrap_exchange_bearer=exchange_bearer,
                    claimant_person_id=principal.person_id,
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
                    conn, confirmation=confirmation, operation_id=claimed.operation_id
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
        SelfHostedClaimValidationError,
    ):
        return JSONResponse(
            {"code": "invalid_claim", "message": "Instance claim input is invalid."},
            status_code=422,
        )
    except SelfHostedClaimUnavailable:
        return JSONResponse(
            {"code": "not_found", "message": "Instance claim unavailable."},
            status_code=404,
        )
    except Exception as exc:
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        if isinstance(exc, OperationIdempotencyConflict):
            return JSONResponse(
                {"code": "conflict", "message": "Claim conflicts with existing state."},
                status_code=409,
            )
        logger.error("admin_api: self_hosted_claim failed: %s", type(exc).__name__)
        return JSONResponse(
            {"code": "operation_failed", "message": "Instance claim failed."},
            status_code=500,
        )

    response = _invitation_no_store(
        JSONResponse(
            {
                "claim_id": claimed.claim_id,
                "person_id": claimed.person_id,
                "org_id": claimed.org_id,
                "project_id": claimed.project_id,
                "journey_id": claimed.journey_id,
                "operation_id": claimed.operation_id,
                "audit_event_id": claimed.audit_event_id,
                "outbox_event_id": claimed.outbox_event_id,
                "next_url": f"/org/{claimed.org_id}/project/{claimed.project_id}/getting-started",
                "replayed": claimed.replayed,
            },
            status_code=201,
        )
    )
    response.delete_cookie(
        _INSTANCE_BOOTSTRAP_EXCHANGE_COOKIE,
        path="/api/instance/claim",
        secure=True,
        httponly=True,
        samesite="strict",
    )
    return response


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration ; chaque collection est epissee
# a la position que ses routes occupaient. La preuve est un dump avant/apres.

INSTANCE_CLAIM_ROUTES_1 = [
    # Story 21.1 (AC4): organization CRUD + membership. Static routes precede
    # /{org_id} so Starlette matches list/create first; /members after.
    Route(
        "/api/instance/bootstrap/exchange",
        endpoint=_exchange_instance_bootstrap,
        methods=["POST"],
    ),
    Route(
        "/api/instance/claim/session",
        endpoint=_get_self_hosted_claim_session,
        methods=["GET"],
    ),
    Route(
        "/api/instance/claim/confirmation",
        endpoint=_issue_instance_claim_confirmation,
        methods=["POST"],
    ),
    Route("/api/instance/claim", endpoint=_claim_self_hosted_instance, methods=["POST"]),
]
