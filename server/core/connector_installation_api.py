"""toorow -- Connector installation state REST surface (Story 38.2).

Admin REST surface for the connector installation state machine:

  GET  /api/connectors/{connector_name}/installation
       -- platform-admin sees full evidence; tenant roles see nondisclosing
          status + safe next action only.

  POST /api/connectors/{connector_name}/installation
       -- platform-admin only; Idempotency-Key header; initial apply advances
          NOT_INSTALLED -> DOMAIN_PENDING; replay-safe; 409 on conflicting payload.

GATING (nondisclosing 404-not-403, AD-5):
  * Bearer token required -> 401 when absent/invalid.
  * POST: caller MUST be a super-admin (TOOROW_SUPER_ADMINS env var).
    A non-super-admin gets 404 -- we do NOT reveal the surface exists.
  * GET: any authenticated caller; non-super-admin receives the restricted
    projection (visible/unavailable + safe_next_action, no blocking detail).
  * Every denied POST is audited with ACTION_CONNECTOR_INSTALL_DENIED.

CATALOG + ACTIVATION GATE (AC3, AC4):
  * Non-READY connector is `unavailable` with a nondisclosing setup status.
  * READY connector is `selectable`.
  * `refuse_activation_unless_ready` is a guard helper for the tenant
    activation write path (38.5 will call this).

Source-agnostic: connector is referenced only by its `connector_name` string
(AD-2). No provider/vendor vocabulary in this module.
ASCII-only strings (Windows/CI safe). Lazy imports keep this import-safe.
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
ACTION_CONNECTOR_INSTALL_DENIED = declare_action("connector.install.denied")


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

_NOT_FOUND = {"code": "not_found", "message": "Resource not found"}


def _get_environment() -> str:
    """Return the active deployment environment name from env vars."""
    return os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"


def _is_module_connector(connector_name: str) -> bool:
    """Does this connector ship as a loaded module, rather than as a transport?

    The one question that decides whether an installation owes a domain routing
    step (AI-206), and it is now answered in ONE place --
    `core.connector_family` -- because `run_verification` needs the same answer
    for the same row later in the lifecycle. It used to hold its own copy here,
    and read a transient state there; see that module for what the divergence
    cost.
    """
    from core.connector_family import is_module_connector  # noqa: PLC0415

    return is_module_connector(connector_name)


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.api_auth import authenticate_api_request  # noqa: PLC0415

    return await authenticate_api_request(request)


def _is_platform_admin(identity: str) -> bool:
    """Return True iff *identity* resolves to a TOOROW_SUPER_ADMINS super-admin.

    THE one resolution (audit 12, P1-2). Comparing the raw identity against the
    allow-list was dead in canonical mode: the caller is a ``person_<ULID>`` and
    the allow-list is keyed by email.
    """
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    return identity_is_super_admin(identity)


def _idempotency_key(request: Request) -> str | None:
    """Extract and return the Idempotency-Key header value, or None."""
    value = request.headers.get("Idempotency-Key", "").strip()
    return value or None


# ---------------------------------------------------------------------------
# GET /api/connectors/{connector_name}/installation
# ---------------------------------------------------------------------------


async def _get_connector_installation(request: Request) -> Response:
    """GET /api/connectors/{connector_name}/installation

    Platform-admin: returns the full safe read-model
      {state, safe_next_action, responsible_actor, blocking_cause,
       last_verified_at, catalog_availability}.
    Tenant role (non-super-admin): returns restricted projection
      {catalog_availability, safe_next_action} -- nondisclosing.

    404 when the installation row does not exist in this environment (treats
    the connector as NOT_INSTALLED / unavailable -- existence not disclosed to
    non-admin callers).
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

    environment = _get_environment()
    is_admin = _is_platform_admin(identity)

    try:
        from core.connector_installation import get_installation_state  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            read_model = get_installation_state(
                conn,
                environment=environment,
                connector_name=connector_name,
            )
    except Exception as exc:
        logger.error(
            "connector_installation_api: get_installation error cn=%s: %s",
            connector_name,
            exc,
        )
        return JSONResponse(
            {"code": "server_error", "message": "Installation state unavailable"},
            status_code=500,
        )

    # Not found -> nondisclosing response regardless of caller role.
    if read_model is None:
        if is_admin:
            # Admin can see the connector does not exist in this environment.
            return JSONResponse(
                {
                    "connector_name": connector_name,
                    "environment": environment,
                    "state": "NOT_INSTALLED",
                    "catalog_availability": "unavailable",
                    "catalog_status": "setup_pending",
                    "safe_next_action": (
                        "platform_admin: apply installation to advance to DOMAIN_PENDING"
                    ),
                    "responsible_actor": "platform_admin",
                    "blocking_cause": None,
                    "last_verified_at": None,
                },
                status_code=200,
            )
        # Non-admin: nondisclosing -- looks the same as any unavailable connector.
        return JSONResponse(
            {
                "connector_name": connector_name,
                "catalog_availability": "unavailable",
                "catalog_status": "setup_pending",
                "safe_next_action": "contact platform support",
            },
            status_code=200,
        )

    state = read_model["state"]
    catalog_availability = "selectable" if state == "READY" else "unavailable"
    catalog_status = "ready" if state == "READY" else "setup_pending"

    if is_admin:
        return JSONResponse(
            {
                "connector_name": connector_name,
                "environment": environment,
                "state": state,
                "catalog_availability": catalog_availability,
                "catalog_status": catalog_status,
                "safe_next_action": read_model["safe_next_action"],
                "responsible_actor": read_model["responsible_actor"],
                "blocking_cause": read_model["blocking_cause"],
                "last_verified_at": read_model["last_verified_at"],
            },
            status_code=200,
        )

    # Non-admin: restricted projection (nondisclosing AC3).
    return JSONResponse(
        {
            "connector_name": connector_name,
            "catalog_availability": catalog_availability,
            "catalog_status": catalog_status,
            "safe_next_action": "contact platform support",
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# POST /api/connectors/{connector_name}/installation
# ---------------------------------------------------------------------------


async def _post_connector_installation(request: Request) -> Response:
    """POST /api/connectors/{connector_name}/installation

    Platform-admin only. Idempotency-Key header required. Initial apply advances
    NOT_INSTALLED -> DOMAIN_PENDING (never straight to READY). Replay-safe: same
    key returns existing state (200). Conflicting payload for the same key: 409.

    Body (optional JSON):
      {responsible_actor?, blocking_cause?}

    Non-super-admin: 404 (surface hidden). Missing Idempotency-Key: 422.
    """
    from core.audit import (  # noqa: PLC0415
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
            action=ACTION_CONNECTOR_INSTALL_DENIED,
            provider_account="",
            connection_ref="",
            metadata={
                "connector_name": request.path_params.get("connector_name", ""),
                "reason": "not_platform_admin",
            },
        )
        logger.info(
            "connector_installation_api: POST denied non-admin identity=%r", identity
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
    if len(idempotency_key) > 255:
        return JSONResponse(
            {"code": "invalid_header", "message": "Idempotency-Key is too long"},
            status_code=422,
        )

    try:
        body_bytes = await request.body()
        body = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception:  # parser details are not part of the public error contract
        return JSONResponse(
            {"code": "invalid_body", "message": "Body must contain valid JSON"},
            status_code=400,
        )
    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "Body must be a JSON object"},
            status_code=400,
        )

    responsible_actor = body.get("responsible_actor")
    blocking_cause = body.get("blocking_cause")
    if any(
        value is not None and not isinstance(value, str)
        for value in (responsible_actor, blocking_cause)
    ):
        return JSONResponse(
            {
                "code": "invalid_body",
                "message": "responsible_actor and blocking_cause must be strings",
            },
            status_code=422,
        )
    responsible_actor = responsible_actor.strip() or None if responsible_actor else None
    blocking_cause = blocking_cause.strip() or None if blocking_cause else None
    environment = _get_environment()

    trace_id: str | None = None
    raw_trace = request.headers.get("X-Trace-Id", "").strip()
    import re  # noqa: PLC0415
    if raw_trace and re.fullmatch(r"[0-9a-f]{32}", raw_trace):
        trace_id = raw_trace

    try:
        from core.connector_installation import (  # noqa: PLC0415
            ConnectorInstallationValidationError,
            apply_installation,
        )
        from core.db import get_connection  # noqa: PLC0415
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        with get_connection() as conn:
            read_model = apply_installation(
                conn,
                environment=environment,
                connector_name=connector_name,
                responsible_actor=responsible_actor,
                blocking_cause=blocking_cause,
                actor=identity,
                idempotency_key=idempotency_key,
                host_context={},
                trace_id=trace_id,
                # WHO KNOWS THE FAMILY (AI-206). A domain config carries `domain`,
                # `provider_adapter`, `webhook_endpoint_version` and DNS evidence
                # -- an inbound transport contract. A connector that ships as a
                # MODULE has none of that: measured 2026-08-16, not one of the 39
                # manifests declares a domain, a receipt adapter or a transport,
                # so every one of them was parked in DOMAIN_PENDING on a gesture
                # that does not exist for it.
                #
                # The decision is made here rather than in the state machine
                # because this layer is what knows the loaded modules, and
                # `core.connector_installation` must stay free of module
                # vocabulary (AD-2). `managed_feed` -- the inbound family's one
                # member -- is not a module, so it keeps the domain step.
                requires_domain=not _is_module_connector(connector_name),
            )
            conn.commit()
    except OperationIdempotencyConflict:
        return JSONResponse(
            {
                "code": "conflict",
                "message": (
                    "Idempotency-Key already bound to a different installation request"
                ),
            },
            status_code=409,
        )
    except ConnectorInstallationValidationError as exc:
        return JSONResponse(
            {
                "code": "invalid_body",
                "message": str(exc),
            },
            status_code=422,
        )
    except Exception as exc:
        logger.error(
            "connector_installation_api: POST error cn=%s: %s", connector_name, exc
        )
        return JSONResponse(
            {"code": "server_error", "message": "Installation apply failed"},
            status_code=500,
        )

    return JSONResponse(
        {
            "connector_name": connector_name,
            "environment": environment,
            **read_model,
        },
        status_code=200,
    )


# ---------------------------------------------------------------------------
# Catalog + activation gate helpers (AC3, AC4, Task 4).
# ---------------------------------------------------------------------------


def get_catalog_availability(conn, *, connector_name: str) -> dict[str, str]:
    """Return the catalog availability record for one connector.

    Used by the catalog read path (source_capabilities.py / datastreams.py) to
    gate whether a connector is selectable. Non-READY connectors are
    `unavailable` with `catalog_status='setup_pending'`; READY connectors are
    `selectable`. The state itself is NOT returned to non-admin callers --
    only the nondisclosing availability flag.
    """
    from core.connector_installation import get_installation_state  # noqa: PLC0415

    environment = _get_environment()
    read_model = get_installation_state(
        conn, environment=environment, connector_name=connector_name
    )
    if read_model is None or read_model["state"] != "READY":
        return {
            "connector_name": connector_name,
            "catalog_availability": "unavailable",
            "catalog_status": "setup_pending",
        }
    return {
        "connector_name": connector_name,
        "catalog_availability": "selectable",
        "catalog_status": "ready",
    }


def refuse_activation_unless_ready(
    conn,
    *,
    connector_name: str,
    environment: str | None = None,
) -> None:
    """Lock and require READY until the caller's write transaction completes.

    FOR SHARE closes the check-then-write race: a concurrent installation
    transition waits until activation or credential issuance commits.
    """
    env = (environment or _get_environment()).strip() or _get_environment()
    name = connector_name.strip() if isinstance(connector_name, str) else ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.connector_installations "
            "WHERE environment = %s AND connector_name = %s FOR SHARE",
            (env, name),
        )
        row = cur.fetchone()
    if row is None or str(row[0]) != "READY":
        raise ConnectorNotReady(
            f"Connector {name!r} is not available for tenant activation"
        )


class ConnectorNotReady(RuntimeError):
    """Raised when tenant activation is attempted for a non-READY connector.

    The message is nondisclosing: it does not reveal why the connector is not
    READY, only that setup is required. (AC4)
    """


# ---------------------------------------------------------------------------
# Route table
# ---------------------------------------------------------------------------

CONNECTOR_INSTALLATION_ROUTES: list[Route] = [
    # More-specific path first (Starlette convention).
    Route(
        "/api/connectors/{connector_name}/installation",
        endpoint=_get_connector_installation,
        methods=["GET"],
    ),
    Route(
        "/api/connectors/{connector_name}/installation",
        endpoint=_post_connector_installation,
        methods=["POST"],
    ),
]
