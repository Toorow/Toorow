"""toorow -- Connector domain config REST surface (Story 38.3).

Admin REST surface for the connector domain and adapter-route configuration:

  GET  /api/connectors/{connector_name}/domain
       -- platform-admin sees full evidence; non-admin gets nondisclosing 404.

  POST /api/connectors/{connector_name}/domain
       -- platform-admin only; Idempotency-Key header required; versioned and
          replay-safe; 409 on conflicting payload.

GATING (nondisclosing 404-not-403, AD-5):
  * Bearer token required -> 401 when absent/invalid.
  * All writes: caller MUST be a super-admin (TOOROW_SUPER_ADMINS env var).
    A non-super-admin gets 404 -- we do NOT reveal the surface exists.
  * GET: platform-admin only (nondisclosing 404 for non-admin).
  * Every denied POST is audited with ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED.

SAFE READ MODEL (AC6):
  * signing_secret_ref, dns_evidence_hash, and any raw secret are NEVER returned.
  * The response shape is identical whether returned by REST or MCP.

Source-agnostic: connector referenced only by connector_name string (AD-2).
No adapter/vendor vocabulary in this module. ASCII-only strings (Windows/CI safe).
Lazy imports keep this import-safe.
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
# Auth helpers (mirrors connector_installation_api.py exactly).
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


# ---------------------------------------------------------------------------
# GET /api/connectors/{connector_name}/domain
# ---------------------------------------------------------------------------


async def _get_connector_domain(request: Request) -> Response:
    """GET /api/connectors/{connector_name}/domain

    Platform-admin: returns the full safe read-model
      {domain, provider_adapter, webhook_endpoint_version,
       dns_evidence_class, config_version, safe_next_action, configured_at}.
    Non-admin: nondisclosing 404 (surface hidden).

    404 when no domain config exists in this environment.
    NEVER returns signing_secret_ref, dns_evidence_hash, or any raw secret.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    # Non-admin: nondisclosing 404 (existence not disclosed, AD-5).
    if not _is_platform_admin(identity):
        from core.audit import (  # noqa: PLC0415
            ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED,
            write_audit_row,
        )

        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "connector_name": request.path_params.get("connector_name", ""),
                "reason": "not_platform_admin",
                "method": "GET",
            },
        )
        logger.info(
            "connector_domain_api: GET denied non-admin identity=%r", identity
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    environment = _get_environment()

    try:
        from core.connector_domain import get_domain_config  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            read_model = get_domain_config(
                conn,
                environment=environment,
                connector_name=connector_name,
            )
    except Exception as exc:
        logger.error(
            "connector_domain_api: GET error cn=%s: %s",
            connector_name,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Domain config unavailable"},
            status_code=500,
        )

    if read_model is None:
        return JSONResponse(
            {
                "connector_name": connector_name,
                "environment": environment,
                "configured": False,
                "safe_next_action": (
                    "platform_admin: POST domain config to configure receiving domain"
                ),
            },
            status_code=200,
        )

    return JSONResponse(
        {
            "connector_name": connector_name,
            "environment": environment,
            "configured": True,
            **read_model,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/domain
# ---------------------------------------------------------------------------


async def _post_connector_domain(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/domain

    Platform-admin only. Idempotency-Key header required. Versioned configure:
    supersedes the prior active config and inserts a new one with config_version+1.
    Replay-safe: same key + payload returns the existing read-model (200).
    Conflicting payload for the same key: 409.

    Body (required JSON):
      {domain, provider_adapter, webhook_endpoint_version?,
       signing_secret_ref?, dns_evidence_class?}

    Non-super-admin: 404 (surface hidden, AD-5). Missing Idempotency-Key: 422.
    Invalid/duplicate domain: 409.
    """
    from core.audit import (  # noqa: PLC0415
        ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED,
        write_audit_row,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    # Platform-admin gate: deny-by-default 404 (not 403).
    if not _is_platform_admin(identity):
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "connector_name": request.path_params.get("connector_name", ""),
                "reason": "not_platform_admin",
                "method": "POST",
            },
        )
        logger.info(
            "connector_domain_api: POST denied non-admin identity=%r", identity
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    connector_name = (request.path_params.get("connector_name") or "").strip()
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

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Invalid JSON body: {exc}"},
            status_code=400,
        )

    domain = (body.get("domain") or "").strip()
    provider_adapter = (body.get("provider_adapter") or "").strip()
    webhook_endpoint_version = (body.get("webhook_endpoint_version") or "v1").strip() or "v1"
    signing_secret_ref = body.get("signing_secret_ref") or None
    dns_evidence_class = body.get("dns_evidence_class") or None
    environment = _get_environment()

    if not domain:
        return JSONResponse(
            {"code": "missing_field", "message": "domain is required in request body"},
            status_code=422,
        )
    if not provider_adapter:
        return JSONResponse(
            {"code": "missing_field", "message": "provider_adapter is required in request body"},
            status_code=422,
        )

    trace_id: str | None = None
    import re as _re  # noqa: PLC0415
    raw_trace = request.headers.get("X-Trace-Id", "").strip()
    if raw_trace and _re.fullmatch(r"[0-9a-f]{32}", raw_trace):
        trace_id = raw_trace

    try:
        from core.connector_domain import (  # noqa: PLC0415
            ConnectorDomainConflict,
            ConnectorDomainUnavailable,
            ConnectorDomainValidationError,
            configure_domain,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            read_model = configure_domain(
                conn,
                environment=environment,
                connector_name=connector_name,
                domain=domain,
                provider_adapter=provider_adapter,
                webhook_endpoint_version=webhook_endpoint_version,
                signing_secret_ref=signing_secret_ref,
                dns_evidence_class=dns_evidence_class,
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
                "message": (
                    "Idempotency-Key already bound to a different domain config request"
                ),
            },
            status_code=409,
        )
    except ConnectorDomainConflict as exc:
        return JSONResponse(
            {"code": "domain_conflict", "message": str(exc)},
            status_code=409,
        )
    except ConnectorDomainValidationError as exc:
        return JSONResponse(
            {"code": "validation_error", "message": str(exc)},
            status_code=422,
        )
    except ConnectorDomainUnavailable as exc:
        return JSONResponse(
            {"code": "installation_unavailable", "message": str(exc)},
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "connector_domain_api: POST error cn=%s: %s", connector_name, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Domain configure failed"},
            status_code=500,
        )

    return JSONResponse(
        {
            "connector_name": connector_name,
            "environment": environment,
            "configured": True,
            **read_model,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

CONNECTOR_DOMAIN_ROUTES: list[Route] = [
    Route(
        "/api/connectors/{connector_name}/domain",
        endpoint=_get_connector_domain,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/domain",
        endpoint=_post_connector_domain,
        methods=["POST"],
    ),
]
