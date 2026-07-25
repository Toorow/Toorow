"""toorow -- Connector activation REST surface (Story 38.5).

Admin REST surface for the org-scoped connector activation state:

  POST /api/connectors/{connector_name}/activation
       -- org-owner only; Idempotency-Key header required; creates/replays the
          ACTIVE row; fails closed if the installation is not READY (nondisclosing
          422); 409 on conflicting Idempotency-Key payload.

  POST /api/connectors/{connector_name}/activation/deactivate
       -- org-owner only; Idempotency-Key header required; flips to DEACTIVATED
          (preserves row -- never deletes).

  GET  /api/connectors/{connector_name}/activation?org_id=...
       -- org-owner or org-member read; org_id is required; callers from a
          different org receive nondisclosing 404 (AC4).

GATING (nondisclosing 404-not-403, AD-5):
  * Bearer token required -> 401 when absent/invalid.
  * POST/deactivate: caller MUST be an org-owner (``identity_can_manage_org``);
    a non-owner gets 404 (nondisclosing).
  * GET: caller must have org access; a caller from a different org gets 404.
  * Every denied write is audited with ACTION_CONNECTOR_ACTIVATION_DENIED.

READY GATE (AC1):
  * Activation is refused if the connector installation is not READY.
  * The refusal is nondisclosing (maps ConnectorNotReady -> 422 with a generic
    message -- the actual installation state is not disclosed).

HEALTH LAYERING (AC2, Task 4):
  * ``get_connector_activation_state`` is a helper read function that returns the
    org-scoped activation state ALONGSIDE (not merged with) the installation state.
    The health read path calls this function; the two layers stay distinct.

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
# Shared helpers (mirrors connector_installation_api.py).
# ---------------------------------------------------------------------------

_NOT_FOUND = {"code": "not_found", "message": "Resource not found"}


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
# Health layering helper (AC2, Task 4).
# ---------------------------------------------------------------------------


def get_connector_activation_state(
    conn,
    *,
    org_id: str,
    connector_name: str,
    environment: str | None = None,
) -> dict | None:
    """Return the org-scoped activation read-model ALONGSIDE (not merged with)
    the installation state.

    This helper is intended for the connector-health read path (Task 4): it
    returns the activation layer independently so callers can surface both the
    platform installation state (38.2) and the org activation state (38.5)
    as distinct fields in the health response.

    Returns None when no activation row exists for this org + connector + env.
    NEVER returns secrets or cross-tenant detail. Org-scoped (AC4).
    """
    from core.connector_activation import get_activation  # noqa: PLC0415

    env = (environment or _get_environment()).strip() or _get_environment()
    return get_activation(
        conn,
        org_id=org_id,
        connector_name=connector_name,
        environment=env,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/activation
# ---------------------------------------------------------------------------


async def _post_connector_activation(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/activation

    Org-owner only. Idempotency-Key required. Activates a READY connector for
    an organisation. Body: {org_id (required), activated_by?}.

    Not-READY -> 422 (nondisclosing; installation state not disclosed).
    Non-owner -> 404. Missing Idempotency-Key -> 422.
    Conflicting payload on same key -> 409.
    """
    from core.audit import (  # noqa: PLC0415
        ACTION_CONNECTOR_ACTIVATION_DENIED,
        write_audit_row,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {
                "code": "missing_header",
                "message": "Idempotency-Key header is required",
            },
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

    org_id = (body.get("org_id") or "").strip()
    if not org_id:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id is required in body"},
            status_code=400,
        )
    activated_by = (body.get("activated_by") or identity or "").strip()
    environment = _get_environment()
    trace = _trace_id(request)

    # Org-owner gate (nondisclosing 404 if not owner).
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_can_manage_org  # noqa: PLC0415

        with get_connection() as conn:
            can_manage = identity_can_manage_org(org_id, identity, conn)
    except Exception as exc:
        logger.error(
            "connector_activation_api: org-owner check failed org=%s: %s", org_id, exc
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    if not can_manage:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CONNECTOR_ACTIVATION_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "connector_name": connector_name,
                "org_id": org_id,
                "reason": "not_org_owner",
            },
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.connector_activation import activate  # noqa: PLC0415
        from core.connector_installation_api import ConnectorNotReady  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            read_model = activate(
                conn,
                org_id=org_id,
                connector_name=connector_name,
                environment=environment,
                activated_by=activated_by,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
            )
            conn.commit()
    except ConnectorNotReady:
        # Nondisclosing: do not reveal the installation state or why it is not ready.
        return JSONResponse(
            {
                "code": "connector_unavailable",
                "message": "Connector setup is not complete; contact platform support.",
            },
            status_code=422,
        )
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": (
                    "Idempotency-Key already bound to a different activation request"
                ),
            },
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "connector_activation_api: POST error cn=%s org=%s: %s",
            connector_name,
            org_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Activation failed"},
            status_code=500,
        )

    return JSONResponse(
        {"connector_name": connector_name, "environment": environment, **read_model},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/activation/deactivate
# ---------------------------------------------------------------------------


async def _post_connector_deactivation(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/activation/deactivate

    Org-owner only. Idempotency-Key required. Flips the activation state to
    DEACTIVATED. NEVER deletes the row or any retained evidence (AC3).
    Body: {org_id (required)}.
    """
    from core.audit import (  # noqa: PLC0415
        ACTION_CONNECTOR_ACTIVATION_DENIED,
        write_audit_row,
    )

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    idempotency_key = _idempotency_key(request)
    if not idempotency_key:
        return JSONResponse(
            {
                "code": "missing_header",
                "message": "Idempotency-Key header is required",
            },
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

    org_id = (body.get("org_id") or "").strip()
    if not org_id:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id is required in body"},
            status_code=400,
        )
    environment = _get_environment()
    trace = _trace_id(request)

    # Org-owner gate (nondisclosing 404).
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_can_manage_org  # noqa: PLC0415

        with get_connection() as conn:
            can_manage = identity_can_manage_org(org_id, identity, conn)
    except Exception as exc:
        logger.error(
            "connector_activation_api: org-owner check failed org=%s: %s", org_id, exc
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    if not can_manage:
        write_audit_row(
            identity=identity or "anonymous",
            action=ACTION_CONNECTOR_ACTIVATION_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "connector_name": connector_name,
                "org_id": org_id,
                "reason": "not_org_owner",
            },
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.connector_activation import (  # noqa: PLC0415
            ConnectorActivationUnavailable,
            deactivate,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            read_model = deactivate(
                conn,
                org_id=org_id,
                connector_name=connector_name,
                environment=environment,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace,
            )
            conn.commit()
    except ConnectorActivationUnavailable:
        return JSONResponse(_NOT_FOUND, status_code=404)
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": (
                    "Idempotency-Key already bound to a different deactivation request"
                ),
            },
            status_code=409,
        )
    except Exception as exc:
        logger.error(
            "connector_activation_api: deactivate error cn=%s org=%s: %s",
            connector_name,
            org_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Deactivation failed"},
            status_code=500,
        )

    return JSONResponse(
        {"connector_name": connector_name, "environment": environment, **read_model},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# GET /api/connectors/{connector_name}/activation?org_id=...
# ---------------------------------------------------------------------------


async def _get_connector_activation(request: Request) -> Response:
    """GET /api/connectors/{connector_name}/activation?org_id=...

    Org-scoped read. ``org_id`` query-param required. The caller must have org
    access (any role). A caller from a different org, or with no access, receives
    a nondisclosing 404 (AC4). State and timestamps are returned; no secrets.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    connector_name = (request.path_params.get("connector_name") or "").strip()
    if not connector_name:
        return JSONResponse(
            {"code": "missing_param", "message": "connector_name is required"},
            status_code=400,
        )

    org_id = (request.query_params.get("org_id") or "").strip()
    if not org_id:
        return JSONResponse(
            {"code": "missing_param", "message": "org_id query parameter is required"},
            status_code=400,
        )

    environment = _get_environment()

    # Org access gate (nondisclosing 404 for non-members of enrolled orgs).
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.project_access import identity_has_org_access  # noqa: PLC0415

        with get_connection() as conn:
            has_access = identity_has_org_access(org_id, identity, conn)
    except Exception as exc:
        logger.error(
            "connector_activation_api: org access check failed org=%s: %s", org_id, exc
        )
        return JSONResponse(_NOT_FOUND, status_code=404)

    if not has_access:
        return JSONResponse(_NOT_FOUND, status_code=404)

    try:
        from core.connector_activation import get_activation  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            read_model = get_activation(
                conn,
                org_id=org_id,
                connector_name=connector_name,
                environment=environment,
            )
    except Exception as exc:
        logger.error(
            "connector_activation_api: GET error cn=%s org=%s: %s",
            connector_name,
            org_id,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Activation state unavailable"},
            status_code=500,
        )

    if read_model is None:
        return JSONResponse(_NOT_FOUND, status_code=404)

    return JSONResponse(
        {"connector_name": connector_name, "environment": environment, **read_model},
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

CONNECTOR_ACTIVATION_ROUTES: list[Route] = [
    # More-specific path first: /activation/deactivate before /activation.
    Route(
        "/api/connectors/{connector_name}/activation/deactivate",
        endpoint=_post_connector_deactivation,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/activation",
        endpoint=_post_connector_activation,
        methods=["POST"],
    ),
    Route(
        "/api/connectors/{connector_name}/activation",
        endpoint=_get_connector_activation,
        methods=["GET"],
    ),
]
