"""toorow -- Inbound delivery credential REST surface (Story 38.7).

Admin REST surface for the Datastream-scoped delivery credential lifecycle:

  POST /api/connectors/{connector_name}/datastreams/{datastream_id}/credentials
       -- Datastream-role operator; Idempotency-Key required; issues a new
          delivery credential. The full secret is returned EXACTLY ONCE in the
          response body under ``full_secret``. It will not appear in GET/MCP.

  POST .../credentials/{id}/rotate
       -- Datastream-role operator; Idempotency-Key required; rotates the active
          credential. Prior stays ROTATING until overlap_until (or is immediately
          REVOKED if immediate_revoke=true). New full_secret returned ONCE.

  POST .../credentials/{id}/revoke
       -- Datastream-role operator; Idempotency-Key required; immediately revokes.

  GET  /api/connectors/{connector_name}/datastreams/{datastream_id}/credentials
       -- Read. Returns only the safe read-model list; NO secret ever.

GATING (nondisclosing 404-not-403, AD-5):
  * Bearer token required -> 401 when absent/invalid.
  * Write operations (issue/rotate/revoke): caller must have Datastream-edit or
    higher access (``resolve_strict_resource_access`` with minimum_capability
    'edit') under disabled-auth or strict production mode.
  * Every denied write is audited with ACTION_INBOUND_CREDENTIAL_DENIED.
  * The nondisclosing 404 is returned for any authorization failure (no 403).

SHOW-ONCE (AC2, E38-NFR03):
  * ``full_secret`` appears ONLY in the POST /credentials (issue) and
    POST .../rotate response bodies.
  * It NEVER appears in GET responses, MCP tool output, audit payloads, outbox
    payloads, error messages, logs, or query params.

Source-agnostic: connector referenced only by connector_name string (AD-2).
No provider/vendor vocabulary in this module. ASCII-only strings. Lazy imports.
"""

from __future__ import annotations

import json
import logging
import os

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared helpers (mirrors connector_activation_api.py).
# ---------------------------------------------------------------------------

_NOT_FOUND: dict = {"code": "not_found", "message": "Resource not found"}


def _get_environment() -> str:
    """Return the active deployment environment name from env vars."""
    return os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _idempotency_key(request: Request) -> str | None:
    """Extract and return the Idempotency-Key header value, or None."""
    return request.headers.get("Idempotency-Key") or None


def _trace_id(request: Request) -> str | None:
    """Extract a validated X-Trace-Id header or return None."""
    import re  # noqa: PLC0415

    raw = request.headers.get("X-Trace-Id", "").strip()
    if raw and re.fullmatch(r"[0-9a-f]{32}", raw):
        return raw
    return None


# ---------------------------------------------------------------------------
# Access guard -- Datastream-role authority (nondisclosing 404, AD-5).
# ---------------------------------------------------------------------------


def _check_datastream_access(
    datastream_id: str,
    identity: str,
    *,
    minimum_capability: str = "edit",
) -> bool | None:
    """Return True if the identity may perform writes on this Datastream.

    Uses ``resolve_strict_resource_access`` with the given minimum_capability.
    Returns False (not None) on any denial. Returns None on DB error.
    In disabled-auth mode this always grants access (dev compat).
    """
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode == "disabled":
        return True

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
            )
        return decision.allowed
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_credentials_api: access check failed ds=%s: %s",
            datastream_id,
            exc,
        )
        return None  # DB error: fail-closed (caller maps to 404)


# ---------------------------------------------------------------------------
# POST .../credentials   -- issue a new credential.
# ---------------------------------------------------------------------------


async def _post_issue_credential(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/datastreams/{datastream_id}/credentials

    Issues a new delivery credential. The full_secret is returned EXACTLY ONCE
    in the response. It will NOT be retrievable via GET or MCP.

    Body (optional JSON):
      channel          : 'email' | 'webhook' (required)
      overlap_seconds  : int (optional, for future rotation)
      expires_seconds  : int (optional, for expiry)

    Returns 200 with the safe read-model PLUS full_secret (once).
    """
    from core.audit import ACTION_INBOUND_CREDENTIAL_DENIED, write_audit_row  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    datastream_id = (request.path_params.get("datastream_id") or "").strip()

    if not connector_name or not datastream_id:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name and datastream_id are required"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header is required"},
            status_code=422,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    channel = (body.get("channel") or "").strip()
    if not channel:
        return JSONResponse(
            {"code": "missing_param", "message": "channel is required ('email' or 'webhook')"},
            status_code=400,
        )

    trace = _trace_id(request)

    # Datastream-role gate (nondisclosing 404).
    access = _check_datastream_access(datastream_id, identity, minimum_capability="edit")
    if access is None or not access:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_INBOUND_CREDENTIAL_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "datastream_id": datastream_id,
                "operation": "issue",
                "reason": "access_denied",
            },
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            InboundCredentialConflict,
            InboundCredentialDomainNotReady,
            InboundCredentialRateLimited,
            InboundCredentialValidationError,
            issue,
        )
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            result = issue(
                conn,
                datastream_id=datastream_id,
                channel=channel,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
            )
            conn.commit()

    except InboundCredentialValidationError as exc:
        return JSONResponse(
            {"code": "invalid_param", "message": str(exc)},
            status_code=400,
        )
    except InboundCredentialDomainNotReady:
        # Nondisclosing: do not reveal installation state.
        return JSONResponse(
            {
                "code": "domain_not_ready",
                "message": "Connector domain is not verified; contact platform support.",
            },
            status_code=422,
        )
    except InboundCredentialRateLimited:
        return JSONResponse(
            {"code": "rate_limited", "message": "Rate limit exceeded; try again later."},
            status_code=429,
        )
    except InboundCredentialConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "A credential already exists for this channel; rotate or revoke first.",
            },
            status_code=409,
        )
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Idempotency-Key already bound to a different issue request",
            },
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "inbound_credentials_api: issue failed ds=%s ch=%s: %s",
            datastream_id,
            channel,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Credential issuance failed"},
            status_code=500,
        )

    # full_secret is included ONCE here. The caller (HTTP client / operator) must
    # record it immediately; it will not be accessible via GET or MCP.
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# POST .../credentials/{id}/rotate   -- rotate a credential.
# ---------------------------------------------------------------------------


async def _post_rotate_credential(request: Request) -> Response:
    """POST .../credentials/{id}/rotate

    Rotates the active delivery credential. Returns new full_secret ONCE.

    Body (optional JSON):
      overlap_seconds  : int (default 3600 = 1 hour)
      immediate_revoke : bool (default false -- use overlap window)
    """
    from core.audit import ACTION_INBOUND_CREDENTIAL_DENIED, write_audit_row  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    credential_id = (request.path_params.get("credential_id") or "").strip()

    if not connector_name or not datastream_id or not credential_id:
        return JSONResponse(
            {"code": "missing_param", "message": "required path params missing"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header is required"},
            status_code=422,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    overlap_seconds: int = int(body.get("overlap_seconds") or 3600)
    immediate_revoke: bool = bool(body.get("immediate_revoke", False))
    trace = _trace_id(request)

    # Datastream-role gate (nondisclosing 404).
    access = _check_datastream_access(datastream_id, identity, minimum_capability="edit")
    if access is None or not access:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_INBOUND_CREDENTIAL_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "datastream_id": datastream_id,
                "credential_id": credential_id,
                "operation": "rotate",
                "reason": "access_denied",
            },
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415

        # We need the channel for the rotate call; read from the existing credential.
        from core.inbound_credentials import (  # noqa: PLC0415
            InboundCredentialConflict,
            InboundCredentialRateLimited,
            InboundCredentialUnavailable,
            InboundCredentialValidationError,
            get_credential_state,  # noqa: PLC0415
            rotate,
        )
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            cred_state = get_credential_state(
                conn, credential_id=credential_id, datastream_id=datastream_id
            )
            if cred_state is None:
                return JSONResponse(_NOT_FOUND, status_code=404)
            channel = cred_state["channel"]

            result = rotate(
                conn,
                credential_id=credential_id,
                datastream_id=datastream_id,
                channel=channel,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
                overlap_seconds=overlap_seconds,
                immediate_revoke=immediate_revoke,
            )
            conn.commit()

    except InboundCredentialValidationError as exc:
        return JSONResponse(
            {"code": "invalid_param", "message": str(exc)},
            status_code=400,
        )
    except InboundCredentialUnavailable:
        return JSONResponse(_NOT_FOUND, status_code=404)
    except InboundCredentialConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Credential conflict; the credential may have already been rotated.",
            },
            status_code=409,
        )
    except InboundCredentialRateLimited:
        return JSONResponse(
            {"code": "rate_limited", "message": "Rate limit exceeded; try again later."},
            status_code=429,
        )
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Idempotency-Key already bound to a different rotate request",
            },
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "inbound_credentials_api: rotate failed ds=%s cred=%s: %s",
            datastream_id,
            credential_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Credential rotation failed"},
            status_code=500,
        )

    # full_secret returned ONCE (new credential secret).
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# POST .../credentials/{id}/revoke   -- revoke a credential.
# ---------------------------------------------------------------------------


async def _post_revoke_credential(request: Request) -> Response:
    """POST .../credentials/{id}/revoke

    Immediately revokes a delivery credential. No secret in the response.
    """
    from core.audit import ACTION_INBOUND_CREDENTIAL_DENIED, write_audit_row  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    credential_id = (request.path_params.get("credential_id") or "").strip()

    if not connector_name or not datastream_id or not credential_id:
        return JSONResponse(
            {"code": "missing_param", "message": "required path params missing"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {"code": "missing_header", "message": "Idempotency-Key header is required"},
            status_code=422,
        )

    trace = _trace_id(request)

    # Datastream-role gate (nondisclosing 404).
    access = _check_datastream_access(datastream_id, identity, minimum_capability="edit")
    if access is None or not access:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_INBOUND_CREDENTIAL_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "datastream_id": datastream_id,
                "credential_id": credential_id,
                "operation": "revoke",
                "reason": "access_denied",
            },
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            InboundCredentialConflict,
            InboundCredentialUnavailable,
            InboundCredentialValidationError,
            revoke,
        )
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            result = revoke(
                conn,
                credential_id=credential_id,
                datastream_id=datastream_id,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
            )
            conn.commit()

    except InboundCredentialValidationError as exc:
        return JSONResponse(
            {"code": "invalid_param", "message": str(exc)},
            status_code=400,
        )
    except InboundCredentialUnavailable:
        return JSONResponse(_NOT_FOUND, status_code=404)
    except InboundCredentialConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Credential is already in a terminal state (revoked or expired).",
            },
            status_code=409,
        )
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Idempotency-Key already bound to a different revoke request",
            },
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "inbound_credentials_api: revoke failed ds=%s cred=%s: %s",
            datastream_id,
            credential_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Credential revocation failed"},
            status_code=500,
        )

    # Revoke response: safe read-model only. NO secret. channel may be "" if
    # not resolved from the row (the revoke fn doesn't re-query channel).
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# GET .../credentials   -- safe read-model list, NO secret.
# ---------------------------------------------------------------------------


async def _get_list_credentials(request: Request) -> Response:
    """GET /api/connectors/{connector_name}/datastreams/{datastream_id}/credentials

    Returns the safe read-model list for all active/rotating credentials.
    NO secret in the response (E38-NFR03, AC2).

    Query params:
      include_terminal : 'true' to include REVOKED/EXPIRED rows (default false)
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    datastream_id = (request.path_params.get("datastream_id") or "").strip()

    if not connector_name or not datastream_id:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name and datastream_id are required"},
            status_code=400,
        )

    include_terminal = (
        request.query_params.get("include_terminal", "false").lower() == "true"
    )

    # Read access: view capability is sufficient.
    access = _check_datastream_access(datastream_id, identity, minimum_capability="view")
    if access is None or not access:
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.inbound_credentials import list_credentials  # noqa: PLC0415

        with get_connection() as conn:
            credentials = list_credentials(
                conn,
                datastream_id=datastream_id,
                include_terminal=include_terminal,
            )
    except Exception as exc:
        logger.error(
            "inbound_credentials_api: GET list failed ds=%s: %s",
            datastream_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Credential list unavailable"},
            status_code=500,
        )

    return JSONResponse(
        {
            "datastream_id": datastream_id,
            "credentials": credentials,
            "count": len(credentials),
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

INBOUND_CREDENTIAL_ROUTES: list[Route] = [
    # More-specific paths first (rotate, revoke before bare credential collection).
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/credentials/{credential_id}/rotate",
        endpoint=_post_rotate_credential,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/credentials/{credential_id}/revoke",
        endpoint=_post_revoke_credential,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/credentials",
        endpoint=_post_issue_credential,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/datastreams/{datastream_id}/credentials",
        endpoint=_get_list_credentials,
        methods=["GET"],
    ),
]
