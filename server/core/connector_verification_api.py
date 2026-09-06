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

from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_CONNECTOR_VERIFICATION_DENIED = declare_action("connector.verification.denied")


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
    """Return True iff *identity* resolves to a TOOROW_SUPER_ADMINS super-admin.

    THE one resolution (audit 12, P1-2). Comparing the raw identity against the
    allow-list was dead in canonical mode: the caller is a ``person_<ULID>`` and
    the allow-list is keyed by email.
    """
    from core.super_admin import identity_is_super_admin  # noqa: PLC0415

    return identity_is_super_admin(identity)


def _idempotency_key(request: Request) -> str | None:
    """Extract and normalize the Idempotency-Key header."""
    value = request.headers.get("Idempotency-Key")
    if value is None:
        return None
    return value.strip() or None


def _audit_denied(identity: str, connector_name: str, method: str, reason: str) -> None:
    """Write a nondisclosing denial audit row (best-effort)."""
    from core.audit import (  # noqa: PLC0415
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
       synthetic_delivery, ttl_seconds, safe_next_action}.
      ``ttl_seconds`` is the re-check interval the evidence row carries; the
      expiry instant is NOT sent -- ``last_run_at + ttl_seconds`` is derivable.
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
        checks = []  # Fail closed as not_configured; no evidence row or state advance.

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
            if read_model.get("last_outcome") == "passed":
                from core.data_identities import (  # noqa: PLC0415
                    snapshot_verified_connector_contract,
                )

                snapshot_verified_connector_contract(
                    conn,
                    environment=environment,
                    connector_id=connector_name,
                    actor=identity,
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
    except ConnectorVerificationUnavailable:
        return JSONResponse(
            {
                "code": "installation_unavailable",
                "message": "Connector verification prerequisites are unavailable",
            },
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
        delivery_runner = lambda: (False, "synthetic_verification_failed")  # noqa: E731

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
    except ConnectorVerificationUnavailable:
        return JSONResponse(
            {
                "code": "installation_unavailable",
                "message": "Connector verification prerequisites are unavailable",
            },
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


#: The authorization paths a connector manifest may declare, and what each one
#: needs THIS DEPLOYMENT to hold. Authorization vocabulary, not provider
#: vocabulary -- the same status mart names hold in `metric_reconciliation`.
_AUTH_PATH_GOOGLE_DIRECT = "google_direct"
_AUTH_PATH_NANGO = "nango"
_AUTH_PATH_NONE = "none"

#: The legacy `auth_type` scalars, and why each one resolves to the Nango path.
#:
#: A scalar names a MECHANISM -- "this provider speaks OAuth2" -- not a path, so
#: it cannot say which platform configuration the deployment must hold. It is
#: resolved rather than refused, and the resolution is derived from a ratified
#: decision instead of guessed: Google products authorize DIRECTLY and every one
#: of them declares `google_direct` explicitly; everything else goes through
#: Nango, key/secret included (Nango Basic extends `nango_client` rather than
#: holding a direct credential).
#:
#: MEASURED BEFORE MAPPING, 2026-08-17: 20 of the 39 shipped manifests carry a
#: legacy scalar, and NOT ONE of them is a Google product. Had a single Google
#: connector been on this list, mapping it here would have sent it to the wrong
#: configuration -- which is why the count was taken before the branch was
#: written, not after it went green.
_LEGACY_SCALARS_ON_NANGO = frozenset({"oauth2", "api_key", "basic"})

#: The one bounded cause a platform check can report. `sanitize_blocking_cause`
#: would collapse anything else to it anyway; naming it here keeps the check and
#: the ledger speaking the same word.
_CAUSE_NOT_CONFIGURED = "dependency_unavailable"


def _declared_auth_path(connector_name: str) -> str | None:
    """The authorization path *connector_name* declares, or ``None`` if unreadable.

    Read from the module's own manifest -- ``auth.auth_path`` when the newer
    object form is present, else the legacy ``auth_type`` scalar. ``None`` means
    the manifest could not be read or declares nothing, which fails closed
    rather than guessing a path.
    """
    import json  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    manifest = Path(__file__).parents[1] / "modules" / connector_name / "manifest.json"
    try:
        declared = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 -- an unreadable manifest is "no declared path"
        return None
    auth = declared.get("auth")
    if isinstance(auth, dict):
        path = auth.get("auth_path") or auth.get("type")
        if isinstance(path, str) and path.strip():
            return path.strip()
    legacy = declared.get("auth_type")
    return legacy.strip() if isinstance(legacy, str) and legacy.strip() else None


def _platform_authorization_is_configured(connector_name: str) -> tuple[bool, str, str]:
    """Can THIS DEPLOYMENT present the authorization path this connector declares?

    THE SCOPE IS THE WHOLE POINT (AI-206). An installation is PLATFORM-scoped --
    `environment` + `connector_name` -- while every authorization in this product
    is a client's OAuth consent, held per PROJECT. So a platform check cannot ask
    "has someone authorized this connector"; nobody can answer that here, and
    minting a platform credential to make the question answerable would
    contradict the ratified Google-stack decision of 2026-08-11.

    What IS platform-scoped is the environment's own readiness: a Google-direct
    connector needs this deployment's OAuth client, a Nango-backed one needs the
    Nango secret, and a connector declaring no authorization needs nothing. The
    path is read from the module's manifest, never assumed.

    WHAT THIS DELIBERATELY DOES NOT PROVE, because a verification that overclaims
    is worse than none: that any client can connect, that a token is valid, or
    that the provider is reachable. Those are per-project facts, answered by the
    OAuth flow and by account discovery on a different surface.

    Returns the core's check contract ``(passed, evidence_class, reason)``. The
    class is always ``auth_check``: migration 268 requires exactly that for a run
    carrying no routing contract, and a module connector never has one.
    """
    # Imported here rather than at module scope: the core module imports this
    # one's route table, and the class name is the only thing needed from it.
    from core.connector_verification import EVIDENCE_CLASS_AUTH  # noqa: PLC0415

    path = _declared_auth_path(connector_name)
    if path == _AUTH_PATH_NONE:
        # A real answer, not an unknown: nothing to configure, so nothing missing.
        return True, EVIDENCE_CLASS_AUTH, ""
    if path == _AUTH_PATH_GOOGLE_DIRECT:
        from core.google_oauth import load_client_config  # noqa: PLC0415

        try:
            load_client_config()
        except Exception:  # noqa: BLE001 -- the config names its own missing vars
            return False, EVIDENCE_CLASS_AUTH, _CAUSE_NOT_CONFIGURED
        return True, EVIDENCE_CLASS_AUTH, ""
    if path == _AUTH_PATH_NANGO or path in _LEGACY_SCALARS_ON_NANGO:
        from core.nango_client import NANGO_SECRET_KEY_ENV  # noqa: PLC0415

        configured = bool(os.environ.get(NANGO_SECRET_KEY_ENV, "").strip())
        return configured, EVIDENCE_CLASS_AUTH, "" if configured else _CAUSE_NOT_CONFIGURED
    # No readable path, or one this deployment does not implement. Fail closed:
    # a connector whose authorization nobody can describe must not read READY.
    return False, EVIDENCE_CLASS_AUTH, _CAUSE_NOT_CONFIGURED


def _build_checks(
    connector_name: str,
    environment: str,
) -> list:
    """Return the list of injectable check callables for this connector.

    Each callable is ``() -> (bool, str, str)`` -- (passed, evidence_class, reason).

    IT RETURNED `[]` UNTIL 2026-08-17, and that is why no installation had ever
    reached READY: the core reads an empty list as `not_configured`, writes no
    evidence and advances no state. The stub said an operator would "wire real
    checks by replacing this function at deploy time", which no deployment
    mechanism in this repository can do -- replacing it means editing it.

    ONE platform question, one check. See
    :func:`_platform_authorization_is_configured` for what it does and does not
    prove.
    """
    return [lambda: _platform_authorization_is_configured(connector_name)]


def _build_delivery_runner(
    connector_name: str,
    environment: str,
):
    """Return a zero-arg callable ``() -> (bool, str)`` for the synthetic delivery.

    The runner exercises only the receipt-adapter authentication seam (auth-only;
    it MUST NOT write any durable receipt, quarantine, or import). The default
    stub returns a failed safe classification so missing wiring cannot produce
    false verification evidence.

    Operators and integration tests replace this function to inject a real
    adapter seam runner (from server/inbound) without touching core (AD-2).
    """
    return lambda: (False, "synthetic_verification_failed")


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
