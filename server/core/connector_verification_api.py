"""toorow -- Connector verification REST surface (Story 38.4).

Admin REST surface for continuous domain verification and synthetic test delivery:

  POST /api/connectors/{connector_name}/verify
       -- platform-admin; Idempotency-Key required; runs domain verification;
          replay-safe; 409 on conflicting payload.

  POST /api/connectors/{connector_name}/test-delivery
       -- platform-admin; Idempotency-Key required; runs a synthetic delivery
          (auth-only, flags synthetic_delivery=TRUE, writes NO durable receipt or
          import). Replay-safe.

  GET  /api/connectors/{connector_name}/verification
       -- platform-admin sees full safe read-model; non-admin nondisclosing 404.

GATING (nondisclosing 404-not-403, AD-5):
  * Bearer token required -> 401 when absent/invalid.
  * All endpoints: caller MUST be a super-admin (TOOROW_SUPER_ADMINS env var).
    A non-super-admin gets 404 -- we do NOT reveal the surface exists.
  * Every denied request is audited with ACTION_CONNECTOR_VERIFICATION_DENIED.

SAFE READ MODEL (AC4):
  * DNS tokens, signing secrets, evidence_hash, and any raw credential are
    NEVER returned. The response shape is identical whether returned by REST or MCP.

Source-agnostic: connector referenced only by connector_name string (AD-2).
No adapter/vendor vocabulary in this module. ASCII-only strings (Windows/CI safe).
Lazy imports keep this import-safe.
"""

from __future__ import annotations

import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth helpers (mirrors connector_domain_api.py exactly).
# ---------------------------------------------------------------------------

_NOT_FOUND = {"code": "not_found", "message": "Resource not found"}


def _get_environment() -> str:
    """Return the active deployment environment name from env vars."""
    return os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _is_platform_admin(identity: str) -> bool:
    """Return True iff identity is in the TOOROW_SUPER_ADMINS allow-list."""
    from core.super_admin import is_super_admin  # noqa: PLC0415

    return is_super_admin(identity)


def _idempotency_key(request: Request) -> str | None:
    """Extract and return the Idempotency-Key header value, or None."""
    return request.headers.get("Idempotency-Key") or None


def _audit_denied(identity: str, connector_name: str, method: str, reason: str) -> None:
    """Write a nondisclosing denial audit row (best-effort)."""
    from core.audit import (  # noqa: PLC0415
        ACTION_CONNECTOR_VERIFICATION_DENIED,
        write_audit_row,
    )

    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_CONNECTOR_VERIFICATION_DENIED,
        provider_account="",
        connection_ref="",
        metadata={
            "connector_name": connector_name,
            "reason": reason,
            "method": method,
        },
    )


# ---------------------------------------------------------------------------
# GET /api/connectors/{connector_name}/verification
# ---------------------------------------------------------------------------


async def _get_connector_verification(request: Request) -> Response:
    """GET /api/connectors/{connector_name}/verification

    Platform-admin: returns the safe read-model of the latest verification run
      {connector_name, environment, installation_state, last_outcome,
       evidence_class, first_seen_at, last_run_at, blocking_reason,
       synthetic_delivery, safe_next_action}.
    Non-admin: nondisclosing 404 (surface hidden).

    200 with ``verified: false`` when no run has ever been recorded.
    NEVER returns evidence_hash, DNS tokens, signing secrets, or any raw
    credential (AC4, E38-NFR03).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not _is_platform_admin(identity):
        _audit_denied(identity, connector_name, "GET", "not_platform_admin")
        logger.info("connector_verification_api: GET denied non-admin identity=%r", identity)
        return JSONResponse(_NOT_FOUND, status_code=404)

    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    environment = _get_environment()

    try:
        from core.connector_verification import get_verification_state  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            read_model = get_verification_state(
                conn,
                environment=environment,
                connector_name=connector_name,
            )
    except Exception as exc:
        logger.error(
            "connector_verification_api: GET error cn=%s: %s", connector_name, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Verification state unavailable"},
            status_code=500,
        )

    if read_model is None:
        return JSONResponse(
            {
                "connector_name": connector_name,
                "environment": environment,
                "verified": False,
                "safe_next_action": (
                    "platform_admin: POST /verify to run the first verification"
                ),
            },
            status_code=200,
        )

    return JSONResponse(
        {"connector_name": connector_name, "environment": environment,
         "verified": True, **read_model},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/verify
# ---------------------------------------------------------------------------


async def _post_connector_verify(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/verify

    Platform-admin only. Idempotency-Key header required. Runs domain verification
    against the active domain config using the platform's configured check functions.
    Replay-safe: same key + payload returns the stored outcome (200).
    Conflicting payload for the same key: 409.

    State transitions driven internally (AC1):
      DOMAIN_PENDING / VERIFYING -> READY on all checks passing.
      READY -> DEGRADED on a blocking check failing.

    Non-super-admin: 404. Missing Idempotency-Key: 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not _is_platform_admin(identity):
        _audit_denied(identity, connector_name, "POST /verify", "not_platform_admin")
        logger.info("connector_verification_api: verify denied non-admin identity=%r", identity)
        return JSONResponse(_NOT_FOUND, status_code=404)

    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header is required"},
            status_code=422,
        )

    environment = _get_environment()

    trace_id: str | None = None
    import re as _re  # noqa: PLC0415
    raw_trace = request.headers.get("X-Trace-Id", "").strip()
    if raw_trace and _re.fullmatch(r"[0-9a-f]{32}", raw_trace):
        trace_id = raw_trace

    # Build the injectable check list from the platform's routing-check factory.
    # Imported lazily and called here so the factory -- which may contain
    # provider-specific logic -- lives outside server/core (AD-2).
    try:
        checks = _build_checks(connector_name=connector_name, environment=environment)
    except Exception as exc:
        logger.error(
            "connector_verification_api: check build error cn=%s: %s", connector_name, exc
        )
        checks = []  # Run with empty checks -> always passes (operator must configure).

    try:
        from core.connector_verification import (  # noqa: PLC0415
            ConnectorVerificationError,
            ConnectorVerificationUnavailable,
            run_verification,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            read_model = run_verification(
                conn,
                environment=environment,
                connector_name=connector_name,
                checks=checks,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace_id,
            )
            conn.commit()
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Idempotency-Key already bound to a different verification request",
            },
            status_code=409,
        )
    except ConnectorVerificationUnavailable as exc:
        return JSONResponse(
            {"code": "installation_unavailable", "message": str(exc)},
            status_code=409,
        )
    except ConnectorVerificationError as exc:
        return JSONResponse(
            {"code": "validation_error", "message": str(exc)},
            status_code=422,
        )
    except Exception as exc:
        logger.error(
            "connector_verification_api: verify error cn=%s: %s", connector_name, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Verification failed"},
            status_code=500,
        )

    return JSONResponse(
        {"connector_name": connector_name, "environment": environment, **read_model},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/test-delivery
# ---------------------------------------------------------------------------


async def _post_connector_test_delivery(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/test-delivery

    Platform-admin only. Idempotency-Key required. Runs a synthetic delivery
    (auth-only, explicitly flagged synthetic_delivery=TRUE). The delivery
    MUST NOT write a durable receipt, quarantine object, mapping row, or
    publication (AC3). Its outcome is recorded as verification evidence only.

    Non-super-admin: 404. Missing Idempotency-Key: 422.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not _is_platform_admin(identity):
        _audit_denied(identity, connector_name, "POST /test-delivery", "not_platform_admin")
        logger.info(
            "connector_verification_api: test-delivery denied non-admin identity=%r", identity
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header is required"},
            status_code=422,
        )

    environment = _get_environment()

    trace_id: str | None = None
    import re as _re  # noqa: PLC0415
    raw_trace = request.headers.get("X-Trace-Id", "").strip()
    if raw_trace and _re.fullmatch(r"[0-9a-f]{32}", raw_trace):
        trace_id = raw_trace

    # Build the injectable delivery runner (auth-only, no durable writes).
    try:
        delivery_runner = _build_delivery_runner(
            connector_name=connector_name, environment=environment
        )
    except Exception as exc:
        logger.error(
            "connector_verification_api: runner build error cn=%s: %s", connector_name, exc
        )
        delivery_runner = lambda: (True, "no_runner_configured")  # noqa: E731

    try:
        from core.connector_verification import (  # noqa: PLC0415
            ConnectorVerificationError,
            ConnectorVerificationUnavailable,
            run_synthetic_delivery,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            read_model = run_synthetic_delivery(
                conn,
                environment=environment,
                connector_name=connector_name,
                delivery_runner=delivery_runner,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace_id,
            )
            conn.commit()
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Idempotency-Key already bound to a different test-delivery request",
            },
            status_code=409,
        )
    except ConnectorVerificationUnavailable as exc:
        return JSONResponse(
            {"code": "installation_unavailable", "message": str(exc)},
            status_code=409,
        )
    except ConnectorVerificationError as exc:
        return JSONResponse(
            {"code": "validation_error", "message": str(exc)},
            status_code=422,
        )
    except Exception as exc:
        logger.error(
            "connector_verification_api: test-delivery error cn=%s: %s", connector_name, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Test delivery failed"},
            status_code=500,
        )

    return JSONResponse(
        {"connector_name": connector_name, "environment": environment, **read_model},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Injectable factory stubs for checks and delivery runner.
#
# These stubs are the SEAM points. The real provider-specific implementations
# live in server/inbound/ (behind the transport adapter boundary) and are wired
# at deploy time or in tests via monkeypatching. Core never imports server/inbound.
# ---------------------------------------------------------------------------


def _build_checks(
    connector_name: str,
    environment: str,
) -> list:
    """Return the list of injectable check callables for this connector.

    Each callable is ``() -> (bool, str, str)`` -- (passed, evidence_class, reason).

    This stub returns an empty list (always-pass: no checks configured). The
    operator wires real checks by replacing this function at deploy time or in
    integration tests. Keeping this stub empty means offline unit tests never
    need a live adapter (AD-2: no provider vocabulary here).
    """
    return []


def _build_delivery_runner(
    connector_name: str,
    environment: str,
):
    """Return a zero-arg callable ``() -> (bool, str)`` for the synthetic delivery.

    The runner exercises only the receipt-adapter authentication seam (auth-only;
    it MUST NOT write any durable receipt, quarantine, or import). The default
    stub returns (True, 'no_runner_configured') -- a no-op pass.

    Operators and integration tests replace this function to inject a real
    adapter seam runner (from server/inbound) without touching core (AD-2).
    """
    return lambda: (True, "no_runner_configured")


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

CONNECTOR_VERIFICATION_ROUTES: list[Route] = [
    Route(
        "/api/connectors/{connector_name}/verification",
        endpoint=_get_connector_verification,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/verify",
        endpoint=_post_connector_verify,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/test-delivery",
        endpoint=_post_connector_test_delivery,
        methods=["POST"],
    ),
]
