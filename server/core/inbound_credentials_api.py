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

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_INBOUND_CREDENTIAL_DENIED = declare_action("inbound.credential.denied")


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
    """Extract a bounded Idempotency-Key or return None."""
    value = (request.headers.get("Idempotency-Key") or "").strip()
    return value if value and len(value) <= 255 else None


def _json_object(body_bytes: bytes) -> dict:
    if not body_bytes.strip():
        return {}
    value = json.loads(body_bytes)
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def _optional_positive_int(body: dict, name: str, *, maximum: int) -> int | None:
    value = body.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


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
    conn=None,
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

        if conn is not None:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
                hold_access=True,
            )
            return decision.allowed
        with get_connection() as owned_conn:
            decision = resolve_strict_resource_access(
                identity,
                owned_conn,
                datastream_id=datastream_id,
                minimum_capability=minimum_capability,
                hold_access=True,
            )
        return decision.allowed
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "inbound_credentials_api: access check failed ds=%s: %s",
            datastream_id,
            exc,
        )
        return None  # DB error: fail-closed (caller maps to 404)


def _audit_credential_404(
    *,
    identity: str,
    datastream_id: str,
    operation: str,
    reason: str,
    credential_id: str | None = None,
) -> None:
    """Leave evidence for every intentionally nondisclosing credential 404."""
    from core.audit import write_audit_row  # noqa: PLC0415

    metadata = {"datastream_id": datastream_id, "operation": operation, "reason": reason}
    if credential_id:
        metadata["credential_id"] = credential_id
    write_audit_row(
        identity=identity or "anonymous",
        action=ACTION_INBOUND_CREDENTIAL_DENIED,
        provider_account="",
        connection_ref="",
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# POST .../credentials   -- issue a new credential.
# ---------------------------------------------------------------------------


async def _post_issue_credential(request: Request) -> Response:
    """Issue one credential; reveal its secret only for the winning operation."""
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
            {
                "code": "missing_header",
                "message": "A non-empty Idempotency-Key up to 255 characters is required",
            },
            status_code=422,
        )
    try:
        body = _json_object(await request.body())
        channel = body.get("channel")
        if not isinstance(channel, str) or not channel.strip():
            raise ValueError("channel is required ('email' or 'webhook')")
        channel = channel.strip()
        expires_seconds = _optional_positive_int(
            body, "expires_seconds", maximum=365 * 24 * 60 * 60
        )
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, status_code=400)
    trace = _trace_id(request)
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            InboundCredentialConflict,
            InboundCredentialDomainNotReady,
            InboundCredentialRateLimited,
            InboundCredentialUnavailable,
            InboundCredentialValidationError,
            datastream_matches_connector,
            issue,
        )
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with request_connection(identity) as conn:
            access = _check_datastream_access(
                datastream_id, identity, minimum_capability="edit", conn=conn
            )
            if not access:
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    operation="issue",
                    reason="access_denied",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            if not datastream_matches_connector(
                conn, datastream_id=datastream_id, connector_name=connector_name
            ):
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    operation="issue",
                    reason="scope_mismatch",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            result = issue(
                conn,
                datastream_id=datastream_id,
                channel=channel,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
                expires_seconds=expires_seconds,
            )
            conn.commit()
    except InboundCredentialValidationError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=400)
    except InboundCredentialUnavailable:
        _audit_credential_404(
            identity=identity,
            datastream_id=datastream_id,
            operation="issue",
            reason="resource_unavailable",
        )
        return JSONResponse(_NOT_FOUND, status_code=404)
    except InboundCredentialDomainNotReady:
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
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# POST .../credentials/{id}/rotate   -- rotate a credential.
# ---------------------------------------------------------------------------


async def _post_rotate_credential(request: Request) -> Response:
    """Rotate one credential with strict JSON types and same-transaction guards."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
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
            {
                "code": "missing_header",
                "message": "A non-empty Idempotency-Key up to 255 characters is required",
            },
            status_code=422,
        )
    try:
        body = _json_object(await request.body())
        parsed_overlap = _optional_positive_int(body, "overlap_seconds", maximum=7 * 24 * 60 * 60)
        overlap_seconds = parsed_overlap if parsed_overlap is not None else 3600
        immediate_revoke = body.get("immediate_revoke", False)
        if not isinstance(immediate_revoke, bool):
            raise ValueError("immediate_revoke must be a boolean")
    except (json.JSONDecodeError, ValueError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, status_code=400)
    trace = _trace_id(request)
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            InboundCredentialConflict,
            InboundCredentialDomainNotReady,
            InboundCredentialRateLimited,
            InboundCredentialUnavailable,
            InboundCredentialValidationError,
            datastream_matches_connector,
            get_credential_state,
            rotate,
        )
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with request_connection(identity) as conn:
            access = _check_datastream_access(
                datastream_id, identity, minimum_capability="edit", conn=conn
            )
            if not access:
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    credential_id=credential_id,
                    operation="rotate",
                    reason="access_denied",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            if not datastream_matches_connector(
                conn, datastream_id=datastream_id, connector_name=connector_name
            ):
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    credential_id=credential_id,
                    operation="rotate",
                    reason="scope_mismatch",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            current = get_credential_state(
                conn, credential_id=credential_id, datastream_id=datastream_id
            )
            if current is None:
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    credential_id=credential_id,
                    operation="rotate",
                    reason="credential_absent",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            result = rotate(
                conn,
                credential_id=credential_id,
                datastream_id=datastream_id,
                channel=current["channel"],
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
                overlap_seconds=overlap_seconds,
                immediate_revoke=immediate_revoke,
            )
            conn.commit()
    except InboundCredentialValidationError as exc:
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=400)
    except InboundCredentialUnavailable:
        _audit_credential_404(
            identity=identity,
            datastream_id=datastream_id,
            credential_id=credential_id,
            operation="rotate",
            reason="resource_unavailable",
        )
        return JSONResponse(_NOT_FOUND, status_code=404)
    except InboundCredentialDomainNotReady:
        return JSONResponse(
            {
                "code": "domain_not_ready",
                "message": "Connector domain is not verified; contact platform support.",
            },
            status_code=422,
        )
    except InboundCredentialConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": "Credential conflict; it may already have been rotated.",
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
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# POST .../credentials/{id}/revoke   -- revoke a credential.
# ---------------------------------------------------------------------------


async def _post_revoke_credential(request: Request) -> Response:
    """Revoke one credential with same-transaction access and scope checks."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
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
            {
                "code": "missing_header",
                "message": "A non-empty Idempotency-Key up to 255 characters is required",
            },
            status_code=422,
        )
    trace = _trace_id(request)
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            InboundCredentialConflict,
            InboundCredentialUnavailable,
            InboundCredentialValidationError,
            datastream_matches_connector,
            revoke,
        )
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with request_connection(identity) as conn:
            access = _check_datastream_access(
                datastream_id, identity, minimum_capability="edit", conn=conn
            )
            if not access:
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    credential_id=credential_id,
                    operation="revoke",
                    reason="access_denied",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            if not datastream_matches_connector(
                conn, datastream_id=datastream_id, connector_name=connector_name
            ):
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    credential_id=credential_id,
                    operation="revoke",
                    reason="scope_mismatch",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
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
        return JSONResponse({"code": "invalid_param", "message": str(exc)}, status_code=400)
    except InboundCredentialUnavailable:
        _audit_credential_404(
            identity=identity,
            datastream_id=datastream_id,
            credential_id=credential_id,
            operation="revoke",
            reason="resource_unavailable",
        )
        return JSONResponse(_NOT_FOUND, status_code=404)
    except InboundCredentialConflict:
        return JSONResponse(
            {"code": "conflict", "message": "Credential is already terminal."},
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
    return JSONResponse(result, status_code=200)


# ---------------------------------------------------------------------------
# GET .../credentials   -- safe read-model list, NO secret.
# ---------------------------------------------------------------------------


async def _get_list_credentials(request: Request) -> Response:
    """Return a secret-free list after same-connection view/scope checks."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"}, status_code=401
        )
    connector_name = (request.path_params.get("connector_name") or "").strip()
    datastream_id = (request.path_params.get("datastream_id") or "").strip()
    if not connector_name or not datastream_id:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name and datastream_id are required"},
            status_code=400,
        )
    raw_include_terminal = request.query_params.get("include_terminal", "false").lower()
    if raw_include_terminal not in {"true", "false"}:
        return JSONResponse(
            {"code": "invalid_param", "message": "include_terminal must be true or false"},
            status_code=400,
        )
    include_terminal = raw_include_terminal == "true"
    try:
        from core.db import request_connection  # noqa: PLC0415
        from core.inbound_credentials import (  # noqa: PLC0415
            datastream_matches_connector,
            list_credentials,
        )

        with request_connection(identity) as conn:
            access = _check_datastream_access(
                datastream_id, identity, minimum_capability="view", conn=conn
            )
            if not access:
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    operation="list",
                    reason="access_denied",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            if not datastream_matches_connector(
                conn, datastream_id=datastream_id, connector_name=connector_name
            ):
                _audit_credential_404(
                    identity=identity,
                    datastream_id=datastream_id,
                    operation="list",
                    reason="scope_mismatch",
                )
                return JSONResponse(_NOT_FOUND, status_code=404)
            credentials = list_credentials(
                conn, datastream_id=datastream_id, include_terminal=include_terminal
            )
    except Exception as exc:
        logger.error("inbound_credentials_api: GET list failed ds=%s: %s", datastream_id, exc)
        return JSONResponse(
            {"code": "server_error", "message": "Credential list unavailable"},
            status_code=500,
        )
    return JSONResponse(
        {"datastream_id": datastream_id, "credentials": credentials, "count": len(credentials)},
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
