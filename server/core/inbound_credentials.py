"""Story 38.7 -- Datastream delivery credential lifecycle (issue / rotate / revoke).

A Datastream operator issues a scoped delivery capability so that senders can
deliver files to a governed inbound Datastream without gaining user or OAuth
identity (AC5 -- delivery token is distinct from provider OAuth and MCP identity,
AD-14). This module owns:

  * ``issue``         : generate a high-entropy token; persist ONLY the hash and
                        a display suffix; return the full secret EXACTLY ONCE.
  * ``rotate``        : create a new ACTIVE version; move the prior to ROTATING
                        with an overlap window (or REVOKED if policy is immediate).
  * ``revoke``        : flip state -> REVOKED; immediately fail-closed.
  * ``resolve_for_delivery`` : hash the presented token; constant-time lookup of
                        an ACTIVE/ROTATING credential for a receivable Draft or
                        Active Datastream. The ingest seam separately gates processing.
                        Used by the 38.8 receipt path (wired there, not here).
  * ``get_credential_state`` : safe read-model projection (state, safe_suffix,
                        version, expires_at, overlap_until). NEVER the hash or
                        raw token.
  * ``check_rate_limit`` : non-enumerating rate-limit decision (environment /
                        connector / datastream-capability scopes). Throttled or unknown
                        credential -> constant-shape denial.

INVARIANTS (adversarially enforced):

  * SOURCE-AGNOSTIC. No provider/vendor vocabulary in this module -- not in code,
    not in comments, not in docstrings (AD-2 boundary scanner is literal). The
    ``channel`` string and the verified-domain string are declared data only.

  * THE RAW TOKEN IS NEVER PERSISTED OR LOGGED. ``secrets.token_urlsafe(32)``
    is generated ephemerally; only ``token_hash = sha256(token)`` and
    ``safe_suffix`` (last 6 chars) are stored. The full secret is returned ONCE
    in the ``issue`` result dict and is never placed in ``request_payload``,
    audit payload, outbox payload, error messages, query params, or any
    read-model. ``resolve_for_delivery`` hashes the presented token and compares
    with ``hmac.compare_digest`` (constant-time, timing-safe).

  * EVERY WRITE ROUTES THROUGH ``operations.execute_operation`` (atomic audit +
    outbox, idempotent replay). No parallel audit path exists here.

  * NONDISCLOSING READ MODEL. ``get_credential_state`` carries no secret (no
    token_hash, no raw token). Identical shape whether returned by REST or MCP.

  * DOMAIN MUST BE READY. ``issue`` reads the verified domain from 38.3
    ``connector_domain.get_domain_config``; it refuses if no domain is configured
    or if the installation is not in READY state.

  * DETERMINISTIC IDEMPOTENCY. No random id or raw token enters
    ``request_payload``. The row id ``dic_<ULID>`` is generated at WRITE TIME
    inside the mutation closure. Two identical issues with the same Idempotency-Key
    replay cleanly.

  * CONCURRENT WRITES FAIL CLOSED. Advisory scope locks and row-level locks
    serialize lifecycle decisions; a losing issue/rotate raises a conflict and
    never returns a locally generated secret.

Mirrors ``connector_activation.py`` conventions: ``from __future__ import
annotations``, lazy imports of shared seams, mutations only through the operation
seam, ASCII-only source.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import os
import secrets
from typing import Any

from ulid import ULID

from core.audit import declare_action
from core.operations import MutationResult, OperationSpec, execute_operation

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_INBOUND_CREDENTIAL_ISSUED = declare_action("inbound.credential.issued")
ACTION_INBOUND_CREDENTIAL_REVOKED = declare_action("inbound.credential.revoked")
ACTION_INBOUND_CREDENTIAL_ROTATED = declare_action("inbound.credential.rotated")


# ---------------------------------------------------------------------------
# Credential states.
# ---------------------------------------------------------------------------

#: Non-terminal states: a credential in one of these is still potentially valid.
_NONTERMINAL_STATES: frozenset[str] = frozenset({"ACTIVE", "ROTATING"})

#: Terminal states: a credential in one of these is permanently invalid.
_TERMINAL_STATES: frozenset[str] = frozenset({"REVOKED", "EXPIRED"})

#: All valid states.
CREDENTIAL_STATES: frozenset[str] = _NONTERMINAL_STATES | _TERMINAL_STATES

#: Valid channel values.
CREDENTIAL_CHANNELS: frozenset[str] = frozenset({"email", "webhook"})

# ---------------------------------------------------------------------------
# Token generation constants.
# ---------------------------------------------------------------------------

#: Number of characters to retain as the safe suffix (display hint only).
_SAFE_SUFFIX_CHARS: int = 6

#: Number of bytes of entropy for the raw token (results in ~43 url-safe chars).
_TOKEN_BYTES: int = 32

# ---------------------------------------------------------------------------
# Rate-limit defaults (read dynamically so deployments can tune them).
# ---------------------------------------------------------------------------

_RL_DEFAULTS: dict[str, dict[str, int]] = {
    "issue": {"capability": 10, "connector": 100, "environment": 1000},
    "rotate": {"capability": 5, "connector": 50, "environment": 500},
    "resolve": {"capability": 120, "connector": 1200, "environment": 12000},
}
_MAX_EXPIRY_SECONDS = 365 * 24 * 60 * 60
_MAX_OVERLAP_SECONDS = 7 * 24 * 60 * 60

# ---------------------------------------------------------------------------
# Exceptions.
# ---------------------------------------------------------------------------


class InboundCredentialValidationError(ValueError):
    """Raised before SQL when credential lifecycle inputs are unsafe or malformed."""


class InboundCredentialConflict(RuntimeError):
    """A credential is in a state that prevents the requested operation.

    Maps to HTTP 409. Message is operator-facing and nondisclosing.
    """


class InboundCredentialUnavailable(RuntimeError):
    """The credential row does not exist or belongs to a different Datastream."""


class InboundCredentialDomainNotReady(RuntimeError):
    """The installation domain is not verified or not READY; issuance is refused."""


class InboundCredentialRateLimited(RuntimeError):
    """A rate-limit scope (environment / connector / capability) was exceeded.

    The denial is constant-shape (non-enumerating): this exception carries no
    detail about whether the credential exists or what scope triggered the limit.
    """


# ---------------------------------------------------------------------------
# Internal helpers.
# ---------------------------------------------------------------------------


def _sha256_hex(value: str) -> str:
    """Return the hex-encoded sha256 digest of a UTF-8 encoded string."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _generate_token() -> tuple[str, str, str]:
    """Generate a high-entropy token; return (raw_token, token_hash, safe_suffix).

    The raw_token is ephemeral: the caller MUST return it exactly once and then
    discard it. Only token_hash and safe_suffix are persisted.
    """
    raw = secrets.token_urlsafe(_TOKEN_BYTES)
    h = _sha256_hex(raw)
    suffix = raw[-_SAFE_SUFFIX_CHARS:]
    return raw, h, suffix


def _dt2iso(ts: Any) -> str | None:
    """Convert a timestamp value to ISO-8601 string or None."""
    if ts is None:
        return None
    try:
        return ts.isoformat()
    except AttributeError:
        return str(ts)


# ---------------------------------------------------------------------------
# Safe read-model projection. No secret, no hash, no cross-tenant detail.
# ---------------------------------------------------------------------------


def _utc_boundary(value: Any) -> datetime.datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime.datetime):
        raise TypeError("invalid credential boundary")
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return value.astimezone(datetime.timezone.utc)


def _effective_state(state: str, *, expires_at: Any, overlap_until: Any) -> str:
    """Project time-bounded credentials as EXPIRED at the exact boundary."""
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        expires_boundary = _utc_boundary(expires_at)
        overlap_boundary = _utc_boundary(overlap_until)
        if expires_boundary is not None and now >= expires_boundary:
            return "EXPIRED"
        if state == "ROTATING" and (overlap_boundary is None or now >= overlap_boundary):
            return "EXPIRED"
    except (TypeError, ValueError):
        return "EXPIRED"
    return state


def _safe_read_model(
    *,
    credential_id: str,
    datastream_id: str,
    channel: str,
    safe_suffix: str | None,
    state: str,
    version: int,
    expires_at: Any,
    overlap_until: Any,
    issued_by: str,
    created_at: Any,
) -> dict[str, Any]:
    """Build the secret-free read-model for one credential row."""
    return {
        "credential_id": credential_id,
        "datastream_id": datastream_id,
        "channel": channel,
        "safe_suffix": safe_suffix,
        "state": _effective_state(state, expires_at=expires_at, overlap_until=overlap_until),
        "version": version,
        "expires_at": _dt2iso(expires_at),
        "overlap_until": _dt2iso(overlap_until),
        "issued_by": issued_by,
        "created_at": _dt2iso(created_at),
    }


def _bounded_optional_seconds(value: int | None, *, name: str, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InboundCredentialValidationError(f"{name} must be an integer")
    if value < 1 or value > maximum:
        raise InboundCredentialValidationError(f"{name} must be between 1 and {maximum}")
    return value


def _require_operation_result(
    data: dict[str, Any] | None, *, replayed: bool = False
) -> dict[str, Any]:
    required = {
        "credential_id",
        "datastream_id",
        "channel",
        "safe_suffix",
        "state",
        "version",
        "issued_by",
    }
    if not isinstance(data, dict) or not required.issubset(data):
        if replayed:
            raise InboundCredentialConflict("credential operation is still in progress")
        raise RuntimeError("credential operation returned an incomplete result")
    return data


def _operation_read_model(data: dict[str, Any]) -> dict[str, Any]:
    return _safe_read_model(
        credential_id=str(data["credential_id"]),
        datastream_id=str(data["datastream_id"]),
        channel=str(data["channel"]),
        safe_suffix=data.get("safe_suffix"),
        state=str(data["state"]),
        version=int(data["version"]),
        expires_at=data.get("expires_at"),
        overlap_until=data.get("overlap_until"),
        issued_by=str(data["issued_by"]),
        created_at=data.get("created_at"),
    )


def _show_once_result(op_result, *, ephemeral: dict[str, str]) -> dict[str, Any]:
    safe_model = _operation_read_model(
        _require_operation_result(op_result.result, replayed=op_result.replayed)
    )
    if op_result.replayed:
        return {**safe_model, "secret_available": False}
    secret = ephemeral.pop("full_secret", None)
    if not secret:
        raise RuntimeError("new credential operation returned no ephemeral secret")
    return {**safe_model, "secret_available": True, "full_secret": secret}


def _is_unique_violation(exc: Exception) -> bool:
    return getattr(exc, "sqlstate", None) == "23505"


# ---------------------------------------------------------------------------
# _load_active_credential -- load the current non-terminal credential (if any).
# ---------------------------------------------------------------------------


def _load_active_credential(
    conn,
    *,
    datastream_id: str,
    channel: str,
) -> dict[str, Any] | None:
    """Return the current non-terminal credential row for (datastream_id, channel).

    Returns a dict with all safe fields plus token_hash (internal use only).
    Returns None if no ACTIVE/ROTATING row exists.
    This is an INTERNAL helper; callers must NOT expose token_hash externally.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, token_hash, safe_suffix, state, version, "
            "expires_at, overlap_until, issued_by, created_at "
            "FROM app.datastream_inbound_credentials "
            "WHERE datastream_id = %s AND channel = %s "
            "AND state IN ('ACTIVE', 'ROTATING') "
            "ORDER BY version DESC "
            "LIMIT 1",
            (datastream_id, channel),
        )
        row = cur.fetchone()
    if row is None:
        return None
    (
        cred_id,
        token_hash,
        safe_suffix,
        state,
        version,
        expires_at,
        overlap_until,
        issued_by,
        created_at,
    ) = row
    return {
        "id": cred_id,
        "token_hash": token_hash,
        "safe_suffix": safe_suffix,
        "state": state,
        "version": version,
        "expires_at": expires_at,
        "overlap_until": overlap_until,
        "issued_by": issued_by,
        "created_at": created_at,
    }


# ---------------------------------------------------------------------------
# _get_datastream_status -- retrieve canonical connector and lifecycle facts.
# ---------------------------------------------------------------------------


def _get_datastream_status(
    conn, *, datastream_id: str, hold_lifecycle: bool = False
) -> dict[str, Any] | None:
    """Return canonical connector, lifecycle and configured-channel facts."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(config->>'connector_name', module_name), org_id, "
            "enabled, lifecycle_state, COALESCE(config->'channels', '[]'::jsonb) "
            "FROM app.datastreams WHERE id = %s" + (" FOR SHARE" if hold_lifecycle else ""),
            (datastream_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    if len(row) == 4:  # Compatibility for offline fakes created before channel binding.
        connector_name, org_id, enabled, lifecycle_state = row
        raw_channels = ["email", "webhook"]
    else:
        connector_name, org_id, enabled, lifecycle_state, raw_channels = row
    configured_channels = {
        "email" if str(value).strip().lower() == "inbound_email" else str(value).strip().lower()
        for value in (raw_channels or [])
    }
    return {
        "connector_name": connector_name or "",
        "org_id": org_id or "",
        "enabled": bool(enabled),
        "lifecycle_state": lifecycle_state or ("active" if enabled else "draft"),
        "channels": configured_channels & CREDENTIAL_CHANNELS,
    }


def datastream_matches_connector(conn, *, datastream_id: str, connector_name: str) -> bool:
    """Bind REST connector paths to the Datastream's canonical connector."""
    info = _get_datastream_status(conn, datastream_id=datastream_id)
    return bool(info and hmac.compare_digest(info["connector_name"], connector_name))


def _require_receivable(info: dict[str, Any], *, channel: str | None = None) -> None:
    """Allow draft discovery and active intake for configured delivery channels."""
    if not info.get("connector_name") or info.get("lifecycle_state") not in {
        "draft",
        "active",
    }:
        raise InboundCredentialUnavailable("datastream is not receivable")
    if channel is not None and channel not in info.get("channels", set()):
        raise InboundCredentialUnavailable("delivery channel is not configured")


def _ready_domain(conn, *, connector_name: str, environment: str) -> str:
    from core.connector_domain import get_domain_config  # noqa: PLC0415
    from core.connector_installation_api import (  # noqa: PLC0415
        ConnectorNotReady,
        refuse_activation_unless_ready,
    )

    domain_cfg = get_domain_config(conn, environment=environment, connector_name=connector_name)
    if domain_cfg is None:
        raise InboundCredentialDomainNotReady("connector domain is not configured and verified")
    try:
        refuse_activation_unless_ready(conn, connector_name=connector_name, environment=environment)
    except ConnectorNotReady as exc:
        raise InboundCredentialDomainNotReady("connector installation is not ready") from exc
    domain = str(domain_cfg.get("domain") or "").strip()
    if not domain:
        raise InboundCredentialDomainNotReady("connector domain is not verified")
    return domain


# ---------------------------------------------------------------------------
# check_rate_limit -- non-enumerating, fail-closed.
# ---------------------------------------------------------------------------


def _rate_limit_value(operation: str, scope: str) -> int:
    env_name = f"INBOUND_RL_{scope.upper()}_MAX_{operation.upper()}S_PER_HOUR"
    raw = os.environ.get(env_name)
    if raw is None and scope == "capability":
        raw = os.environ.get(f"INBOUND_RL_MAX_{operation.upper()}S_PER_HOUR")
    try:
        value = int(raw) if raw is not None else _RL_DEFAULTS[operation][scope]
    except (TypeError, ValueError):
        value = _RL_DEFAULTS[operation][scope]
    return max(1, value)


def check_rate_limit(
    conn,
    *,
    environment: str,
    connector_name: str,
    datastream_id: str | None,
    channel: str,
    operation: str,
) -> None:
    """Serialize and enforce environment/connector/capability lifecycle limits."""
    if operation not in _RL_DEFAULTS:
        raise InboundCredentialValidationError("unsupported rate-limit operation")
    # One hierarchy everywhere: environment -> connector -> capability.
    lock_keys = (
        f"inbound-credential:{environment}:{operation}",
        f"inbound-credential:{environment}:{connector_name}:{operation}",
        f"inbound-credential:{environment}:{connector_name}:{datastream_id}:{channel}:{operation}",
    )
    with conn.cursor() as cur:
        for lock_key in lock_keys:
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (lock_key,),
            )
        cur.execute(
            "SELECT environment_count, connector_count, capability_count "
            "FROM app.count_inbound_credential_rate_events(%s, %s, %s, %s, %s)",
            (environment, connector_name, datastream_id, channel, operation),
        )
        row = cur.fetchone()
    counts = tuple(int(value or 0) for value in (row or (0, 0, 0)))
    limits = (
        _rate_limit_value(operation, "environment"),
        _rate_limit_value(operation, "connector"),
        _rate_limit_value(operation, "capability"),
    )
    if any(count >= limit for count, limit in zip(counts, limits, strict=True)):
        raise InboundCredentialRateLimited("credential lifecycle rate limit exceeded")


def _record_rate_limit_event(
    conn,
    *,
    operation_id: str | None,
    environment: str,
    connector_name: str,
    datastream_id: str | None,
    channel: str,
    operation: str,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT app.record_inbound_credential_rate_event(%s, %s, %s, %s, %s, %s, %s)",
            (
                f"dcre_{ULID()}",
                operation_id,
                environment,
                connector_name,
                datastream_id,
                channel,
                operation,
            ),
        )


def _enforce_resolution_rate_limit(conn, **scope: Any) -> None:
    check_rate_limit(conn, operation="resolve", **scope)


def _record_resolution_rate_event(conn, **scope: Any) -> None:
    _record_rate_limit_event(conn, operation_id=None, operation="resolve", **scope)


def _materialize_due_expirations(
    conn,
    *,
    datastream_id: str | None = None,
    credential_id: str | None = None,
) -> None:
    """Persist time-derived EXPIRED states through operation, audit and outbox."""
    clauses = [
        "c.state IN ('ACTIVE', 'ROTATING')",
        "(c.expires_at <= NOW() OR (c.state = 'ROTATING' AND c.overlap_until <= NOW()))",
    ]
    params: list[str] = []
    if datastream_id is not None:
        clauses.append("c.datastream_id = %s")
        params.append(datastream_id)
    if credential_id is not None:
        clauses.append("c.id = %s")
        params.append(credential_id)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.id, c.datastream_id, c.channel, c.state, c.version, "
            "c.safe_suffix, c.issued_by, c.created_at, c.expires_at, "
            "c.overlap_until, d.org_id "
            "FROM app.datastream_inbound_credentials c "
            "JOIN app.datastreams d ON d.id = c.datastream_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY c.datastream_id, c.channel, c.version FOR UPDATE OF c",
            tuple(params),
        )
        due_rows = list(cur.fetchall())

    for row in due_rows:
        (
            due_id,
            due_datastream_id,
            due_channel,
            due_state,
            due_version,
            safe_suffix,
            issued_by,
            created_at,
            expires_at,
            overlap_until,
            org_id,
        ) = row
        boundary = _dt2iso(expires_at if due_state == "ACTIVE" else overlap_until)
        spec = OperationSpec(
            command_type="inbound.credential.expired",
            actor="system:credential-expiry",
            effective_org_id=org_id,
            resource_path=(
                f"organization:{org_id}",
                f"datastream:{due_datastream_id}",
                f"credential:{due_id}",
            ),
            idempotency_key=f"credential-expiry:{due_id}:{boundary}",
            host_context={},
            versions={
                "policy": "inbound-credential-v2",
                "catalog": "inbound-credential-v2",
                "tool": "expiry-v1",
            },
            request_payload={"credential_id": due_id, "boundary": boundary},
            provider_references={},
            confirmation_mode="server",
            confirmation_reference=f"inbound-credential:{due_id}:expire",
            trace_id=None,
        )

        def mutation(operation_conn, operation_id: str, *, expected_state=due_state):  # noqa: ANN001
            from core.operations import _canonical_hash  # noqa: PLC0415

            with operation_conn.cursor() as mutation_cur:
                mutation_cur.execute(
                    "UPDATE app.datastream_inbound_credentials SET state = 'EXPIRED', "
                    "overlap_until = NULL, expires_at = COALESCE(expires_at, NOW()), "
                    "operation_id = %s, updated_at = NOW() "
                    "WHERE id = %s AND state = %s "
                    "AND (expires_at <= NOW() OR "
                    "(state = 'ROTATING' AND overlap_until <= NOW())) "
                    "RETURNING expires_at",
                    (operation_id, due_id, expected_state),
                )
                changed = mutation_cur.fetchone()
            if changed is None:
                raise InboundCredentialConflict(
                    "credential expiration lost its locked lifecycle boundary"
                )
            result = {
                "credential_id": due_id,
                "datastream_id": due_datastream_id,
                "channel": due_channel,
                "safe_suffix": safe_suffix,
                "state": "EXPIRED",
                "version": due_version,
                "expires_at": _dt2iso(changed[0]),
                "overlap_until": None,
                "issued_by": issued_by,
                "created_at": _dt2iso(created_at),
            }
            return MutationResult(
                outcome="succeeded",
                before_hash=_canonical_hash({"credential_id": due_id, "state": expected_state}),
                after_hash=_canonical_hash(result),
                result=result,
                outbox_payload={
                    "credential_id": due_id,
                    "datastream_id": due_datastream_id,
                    "state": "EXPIRED",
                    "boundary": boundary,
                },
            )

        execute_operation(conn, spec, mutation=mutation)


# ---------------------------------------------------------------------------
# issue -- generate and persist a new credential (AC1, AC2).
# ---------------------------------------------------------------------------


def issue(
    conn,
    *,
    datastream_id: str,
    channel: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    expires_seconds: int | None = None,
) -> dict[str, Any]:
    """Issue a high-entropy delivery capability and reveal it once."""
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise InboundCredentialValidationError("datastream_id is required")
    if not isinstance(channel, str) or channel not in CREDENTIAL_CHANNELS:
        raise InboundCredentialValidationError(
            f"channel must be one of {sorted(CREDENTIAL_CHANNELS)}"
        )
    if not isinstance(actor, str) or not actor.strip():
        raise InboundCredentialValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InboundCredentialValidationError("idempotency_key is required")
    datastream_id = datastream_id.strip()
    channel = channel.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()
    expires_seconds = _bounded_optional_seconds(
        expires_seconds, name="expires_seconds", maximum=_MAX_EXPIRY_SECONDS
    )

    info = _get_datastream_status(conn, datastream_id=datastream_id)
    if info is None:
        raise InboundCredentialUnavailable("datastream not found")
    connector_name = info["connector_name"]
    org_id = info["org_id"] or None
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    _materialize_due_expirations(conn, datastream_id=datastream_id)
    spec = OperationSpec(
        command_type=ACTION_INBOUND_CREDENTIAL_ISSUED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id or 'platform'}",
            f"datastream:{datastream_id}",
            f"channel:{channel}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-credential-v2",
            "catalog": "inbound-credential-v2",
            "tool": "rest-v1",
        },
        request_payload={
            "datastream_id": datastream_id,
            "channel": channel,
            "issued_by": actor,
            "expires_seconds": expires_seconds,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-credential:{datastream_id}:{channel}:issue",
        trace_id=trace_id,
    )
    ephemeral: dict[str, str] = {}

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        locked_info = _get_datastream_status(
            operation_conn, datastream_id=datastream_id, hold_lifecycle=True
        )
        if locked_info is None:
            raise InboundCredentialUnavailable("datastream not found")
        _require_receivable(locked_info, channel=channel)
        if locked_info["connector_name"] != connector_name:
            raise InboundCredentialConflict("datastream connector changed")
        check_rate_limit(
            operation_conn,
            environment=environment,
            connector_name=connector_name,
            datastream_id=datastream_id,
            channel=channel,
            operation="issue",
        )
        with operation_conn.cursor() as cur:
            cur.execute(
                "SELECT id, state, version FROM app.datastream_inbound_credentials "
                "WHERE datastream_id = %s AND channel = %s "
                "ORDER BY version DESC, created_at DESC, id DESC FOR UPDATE",
                (datastream_id, channel),
            )
            history = list(cur.fetchall())
        if any(row[1] in _NONTERMINAL_STATES for row in history):
            raise InboundCredentialConflict(
                "a non-terminal credential already exists for this channel"
            )
        new_version = max((int(row[2]) for row in history), default=0) + 1
        verified_domain = _ready_domain(
            operation_conn, connector_name=connector_name, environment=environment
        )
        raw_token, token_hash, safe_suffix = _generate_token()
        full_secret = f"ds_{raw_token}@{verified_domain}" if channel == "email" else raw_token
        credential_id = f"dic_{ULID()}"
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, state, "
                "version, expires_at, issued_by, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, "
                # `::double precision`, le type que `make_interval(secs => ...)`
                # attend. Un `%s IS NULL` nu laisse le parametre sans type et la
                # requete tombe en IndeterminateDatatype des que la valeur est
                # None -- c'est-a-dire pour tout credential sans expiration.
                # Cf. AI-186.
                "CASE WHEN %s::double precision IS NULL THEN NULL "
                "ELSE NOW() + make_interval(secs => %s) END, %s, %s) "
                "RETURNING created_at, expires_at",
                (
                    credential_id,
                    datastream_id,
                    channel,
                    token_hash,
                    safe_suffix,
                    new_version,
                    expires_seconds,
                    expires_seconds,
                    actor,
                    operation_id,
                ),
            )
            created_at, expires_at = cur.fetchone()
        _record_rate_limit_event(
            operation_conn,
            operation_id=operation_id,
            environment=environment,
            connector_name=connector_name,
            datastream_id=datastream_id,
            channel=channel,
            operation="issue",
        )
        ephemeral["full_secret"] = full_secret
        result = {
            "credential_id": credential_id,
            "datastream_id": datastream_id,
            "channel": channel,
            "safe_suffix": safe_suffix,
            "state": "ACTIVE",
            "version": new_version,
            "expires_at": _dt2iso(expires_at),
            "overlap_until": None,
            "issued_by": actor,
            "created_at": _dt2iso(created_at),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "datastream_id": datastream_id,
                "channel": channel,
                "state": "ACTIVE",
                "version": new_version,
                "expires_at": _dt2iso(expires_at),
            },
        )

    try:
        op_result = execute_operation(conn, spec, mutation=mutation)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise InboundCredentialConflict("a credential already exists for this channel") from exc
        raise
    return _show_once_result(op_result, ephemeral=ephemeral)


# ---------------------------------------------------------------------------
# rotate -- new ACTIVE + prior ROTATING with overlap window (AC3).
# ---------------------------------------------------------------------------


def rotate(
    conn,
    *,
    credential_id: str,
    datastream_id: str,
    channel: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    overlap_seconds: int = 3600,
    immediate_revoke: bool = False,
) -> dict[str, Any]:
    """Rotate one ACTIVE capability atomically and reveal its replacement once."""
    for name, value in (
        ("credential_id", credential_id),
        ("datastream_id", datastream_id),
        ("actor", actor),
        ("idempotency_key", idempotency_key),
    ):
        if not isinstance(value, str) or not value.strip():
            raise InboundCredentialValidationError(f"{name} is required")
    if not isinstance(channel, str) or channel not in CREDENTIAL_CHANNELS:
        raise InboundCredentialValidationError(
            f"channel must be one of {sorted(CREDENTIAL_CHANNELS)}"
        )
    if not isinstance(immediate_revoke, bool):
        raise InboundCredentialValidationError("immediate_revoke must be a boolean")
    overlap_seconds = _bounded_optional_seconds(
        overlap_seconds, name="overlap_seconds", maximum=_MAX_OVERLAP_SECONDS
    )
    assert overlap_seconds is not None
    credential_id = credential_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()
    info = _get_datastream_status(conn, datastream_id=datastream_id)
    if info is None:
        raise InboundCredentialUnavailable("datastream not found")
    connector_name = info["connector_name"]
    org_id = info["org_id"] or None
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    _materialize_due_expirations(conn, credential_id=credential_id)
    spec = OperationSpec(
        command_type=ACTION_INBOUND_CREDENTIAL_ROTATED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id or 'platform'}",
            f"datastream:{datastream_id}",
            f"channel:{channel}",
            f"credential:{credential_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-credential-v2",
            "catalog": "inbound-credential-v2",
            "tool": "rest-v1",
        },
        request_payload={
            "credential_id": credential_id,
            "datastream_id": datastream_id,
            "channel": channel,
            "immediate_revoke": immediate_revoke,
            "overlap_seconds": 0 if immediate_revoke else overlap_seconds,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-credential:{credential_id}:rotate",
        trace_id=trace_id,
    )
    ephemeral: dict[str, str] = {}

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        locked_info = _get_datastream_status(
            operation_conn, datastream_id=datastream_id, hold_lifecycle=True
        )
        if locked_info is None:
            raise InboundCredentialUnavailable("datastream not found")
        _require_receivable(locked_info, channel=channel)
        if locked_info["connector_name"] != connector_name:
            raise InboundCredentialConflict("datastream connector changed")
        check_rate_limit(
            operation_conn,
            environment=environment,
            connector_name=connector_name,
            datastream_id=datastream_id,
            channel=channel,
            operation="rotate",
        )
        with operation_conn.cursor() as cur:
            cur.execute(
                "SELECT id, state, version, channel, expires_at, overlap_until "
                "FROM app.datastream_inbound_credentials "
                "WHERE datastream_id = %s AND channel = %s "
                "ORDER BY version DESC, created_at DESC, id DESC FOR UPDATE",
                (datastream_id, channel),
            )
            history = list(cur.fetchall())
            if not history:
                focused_row = cur.fetchone()
                if focused_row is not None:
                    history = [(*focused_row, None, None) if len(focused_row) == 4 else focused_row]
        prior = next((row for row in history if row[0] == credential_id), None)
        if prior is None:
            raise InboundCredentialUnavailable("credential not found")
        prior_id, prior_state, prior_version, stored_channel, prior_expires_at, _ = prior
        if prior_state != "ACTIVE" or stored_channel != channel:
            raise InboundCredentialConflict("credential is not the current ACTIVE row")
        verified_domain = _ready_domain(
            operation_conn, connector_name=connector_name, environment=environment
        )
        new_version = max(int(row[2]) for row in history) + 1
        prior_new_state = "REVOKED" if immediate_revoke else "ROTATING"
        terminalized = [
            {
                "credential_id": row[0],
                "previous_state": row[1],
                "version": int(row[2]),
                "expires_at": _dt2iso(row[4]),
                "overlap_until": _dt2iso(row[5]),
                "new_state": "EXPIRED",
            }
            for row in history
            if row[1] == "ROTATING" and row[0] != prior_id
        ]
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.datastream_inbound_credentials SET state = 'EXPIRED', "
                "overlap_until = NULL, expires_at = COALESCE(expires_at, NOW()), "
                "operation_id = %s, updated_at = NOW() "
                "WHERE datastream_id = %s AND channel = %s "
                "AND state = 'ROTATING' AND id <> %s",
                (operation_id, datastream_id, channel, prior_id),
            )
            if immediate_revoke:
                cur.execute(
                    "UPDATE app.datastream_inbound_credentials SET state = 'REVOKED', "
                    "overlap_until = NULL, operation_id = %s, updated_at = NOW() "
                    "WHERE id = %s AND state = 'ACTIVE' RETURNING id",
                    (operation_id, prior_id),
                )
            else:
                cur.execute(
                    "UPDATE app.datastream_inbound_credentials SET state = 'ROTATING', "
                    "overlap_until = NOW() + make_interval(secs => %s), "
                    "operation_id = %s, updated_at = NOW() "
                    "WHERE id = %s AND state = 'ACTIVE' RETURNING id",
                    (overlap_seconds, operation_id, prior_id),
                )
            if cur.fetchone() is None:
                raise InboundCredentialConflict("credential state changed during rotation")
        raw_token, token_hash, safe_suffix = _generate_token()
        full_secret = f"ds_{raw_token}@{verified_domain}" if channel == "email" else raw_token
        new_credential_id = f"dic_{ULID()}"
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, state, "
                "version, expires_at, issued_by, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s, %s) "
                "RETURNING created_at, expires_at",
                (
                    new_credential_id,
                    datastream_id,
                    channel,
                    token_hash,
                    safe_suffix,
                    new_version,
                    prior_expires_at,
                    actor,
                    operation_id,
                ),
            )
            replacement_row = cur.fetchone()
            created_at = replacement_row[0]
            replacement_expires_at = (
                replacement_row[1] if len(replacement_row) > 1 else prior_expires_at
            )
        _record_rate_limit_event(
            operation_conn,
            operation_id=operation_id,
            environment=environment,
            connector_name=connector_name,
            datastream_id=datastream_id,
            channel=channel,
            operation="rotate",
        )
        ephemeral["full_secret"] = full_secret
        result = {
            "credential_id": new_credential_id,
            "datastream_id": datastream_id,
            "channel": channel,
            "safe_suffix": safe_suffix,
            "state": "ACTIVE",
            "version": new_version,
            "expires_at": _dt2iso(replacement_expires_at),
            "overlap_until": None,
            "terminalized_credentials": terminalized,
            "issued_by": actor,
            "created_at": _dt2iso(created_at),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash(
                {
                    "credential_id": prior_id,
                    "state": "ACTIVE",
                    "terminalized_credentials": terminalized,
                }
            ),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "datastream_id": datastream_id,
                "channel": channel,
                "prior_credential_id": prior_id,
                "prior_new_state": prior_new_state,
                "new_credential_id": new_credential_id,
                "new_version": new_version,
                "expires_at": _dt2iso(replacement_expires_at),
                "terminalized_credentials": terminalized,
            },
        )

    try:
        op_result = execute_operation(conn, spec, mutation=mutation)
    except Exception as exc:
        if _is_unique_violation(exc):
            raise InboundCredentialConflict("credential rotation conflicted") from exc
        raise
    return _show_once_result(op_result, ephemeral=ephemeral)


# ---------------------------------------------------------------------------
# revoke -- state -> REVOKED; immediately fail-closed (AC3).
# ---------------------------------------------------------------------------


def revoke(
    conn,
    *,
    credential_id: str,
    datastream_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Revoke a capability atomically; matching retries replay the safe result."""
    for name, value in (
        ("credential_id", credential_id),
        ("datastream_id", datastream_id),
        ("actor", actor),
        ("idempotency_key", idempotency_key),
    ):
        if not isinstance(value, str) or not value.strip():
            raise InboundCredentialValidationError(f"{name} is required")
    credential_id = credential_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()
    idempotency_key = idempotency_key.strip()
    info = _get_datastream_status(conn, datastream_id=datastream_id)
    if info is None:
        raise InboundCredentialUnavailable("datastream not found")
    org_id = info["org_id"] or None
    spec = OperationSpec(
        command_type=ACTION_INBOUND_CREDENTIAL_REVOKED,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id or 'platform'}",
            f"datastream:{datastream_id}",
            f"credential:{credential_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-credential-v2",
            "catalog": "inbound-credential-v2",
            "tool": "rest-v1",
        },
        request_payload={
            "credential_id": credential_id,
            "datastream_id": datastream_id,
            "target_state": "REVOKED",
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-credential:{credential_id}:revoke",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        if (
            _get_datastream_status(operation_conn, datastream_id=datastream_id, hold_lifecycle=True)
            is None
        ):
            raise InboundCredentialUnavailable("datastream not found")
        with operation_conn.cursor() as cur:
            cur.execute(
                "SELECT state, version, safe_suffix, issued_by, created_at, channel, expires_at "
                "FROM app.datastream_inbound_credentials "
                "WHERE id = %s AND datastream_id = %s FOR UPDATE",
                (credential_id, datastream_id),
            )
            row = cur.fetchone()
            if row is None:
                raise InboundCredentialUnavailable("credential not found")
            current_state, version, safe_suffix, issued_by, created_at, channel, expires_at = row
            if current_state in _TERMINAL_STATES:
                raise InboundCredentialConflict(
                    f"credential is already in terminal state {current_state}"
                )
            cur.execute(
                "UPDATE app.datastream_inbound_credentials "
                "SET state = 'REVOKED', overlap_until = NULL, operation_id = %s, "
                "updated_at = NOW() "
                "WHERE id = %s AND state = %s RETURNING state",
                (operation_id, credential_id, current_state),
            )
            if cur.fetchone() is None:
                raise InboundCredentialConflict("credential state changed during revoke")
        result = {
            "credential_id": credential_id,
            "datastream_id": datastream_id,
            "channel": channel,
            "safe_suffix": safe_suffix,
            "state": "REVOKED",
            "version": version,
            "expires_at": _dt2iso(expires_at),
            "overlap_until": None,
            "issued_by": issued_by,
            "created_at": _dt2iso(created_at),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": current_state}),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "datastream_id": datastream_id,
                "credential_id": credential_id,
                "state": "REVOKED",
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    return _operation_read_model(
        _require_operation_result(op_result.result, replayed=op_result.replayed)
    )


# ---------------------------------------------------------------------------
# resolve_for_delivery -- constant-time, non-enumerating (AC3, AC4).
# ---------------------------------------------------------------------------

#: Constant-shape denial dict returned for any invalid or unknown token.
#: Shape is IDENTICAL whether the token is unknown, revoked, expired, or
#: rate-limited -- non-enumerating by design (AC4).
_DENIAL: dict[str, Any] = {"allowed": False, "scope": None, "reason": "denied"}


def resolve_for_delivery(
    conn,
    *,
    raw_token: str,
) -> dict[str, Any]:
    """Validate a presented delivery token; return scope or constant-shape denial.

    Hashes the presented ``raw_token`` with sha256 (``hmac.compare_digest``
    for constant-time comparison at the application layer after the DB lookup)
    and looks up an ACTIVE or ROTATING credential whose ``overlap_until`` has
    not passed (or is NULL for ACTIVE). Returns a dict with:

      On success: ``{"allowed": True, "scope": {"datastream_id": ...,
                     "channel": ..., "version": ..., "credential_id": ...}}``
      On denial:  ``{"allowed": False, "scope": None, "reason": "denied"}``

    The denial shape is IDENTICAL for unknown tokens, revoked/expired
    credentials, rate-limited tokens, and any DB error (AC4 -- non-enumerating).
    The ``raw_token`` is hashed immediately and then discarded; it is NEVER
    logged, stored, or placed in any error payload.

    This function is the seam the 38.8 receipt path will call.
    """
    if not isinstance(raw_token, str) or not raw_token:
        return dict(_DENIAL)

    # Hash immediately; raw_token is never used again after this line.
    presented_hash = _sha256_hex(raw_token)
    # Explicitly replace the name so the token value is unreachable.
    raw_token = ""  # noqa: PLW2901  -- intentional shadow

    return _resolve_by_presented_hash(conn, presented_hash=presented_hash)


def resolve_by_token_hash(
    conn,
    *,
    token_hash: str,
) -> dict[str, Any]:
    """Validate an ALREADY-hashed delivery token; return scope or denial.

    Sibling of ``resolve_for_delivery`` for callers that never see the raw token
    -- e.g. the inbound processing worker, which reads only the ``token_hash``
    recorded in a delivery manifest (the raw token is NEVER persisted). The
    supplied ``token_hash`` MUST be the sha256 hex digest of the raw token.

    Behaviour is byte-for-byte identical to ``resolve_for_delivery`` once the
    hash is known: same ACTIVE/ROTATING lookup, same ROTATING overlap check,
    same expiry check, same datastream-exists check, same
    ``hmac.compare_digest`` defence-in-depth, and the SAME constant-shape,
    non-enumerating denial for every failure mode (AC4).

    Returns::

      On success: ``{"allowed": True, "scope": {"datastream_id": ...,
                     "channel": ..., "version": ..., "credential_id": ...}}``
      On denial:  ``{"allowed": False, "scope": None, "reason": "denied"}``
    """
    if not isinstance(token_hash, str) or not token_hash:
        return dict(_DENIAL)
    return _resolve_by_presented_hash(conn, presented_hash=token_hash)


def _resolve_by_presented_hash(
    conn,
    *,
    presented_hash: str,
) -> dict[str, Any]:
    """Resolve one hash with three-scope throttling and constant-shape denial."""
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT c.id, c.datastream_id, c.channel, c.token_hash, c.state, "
                "c.version, c.overlap_until, c.expires_at, "
                "COALESCE(d.config->>'connector_name', d.module_name), "
                "d.lifecycle_state, COALESCE(d.config->'channels', '[]'::jsonb), "
                "a.state "
                "FROM app.datastream_inbound_credentials c "
                "JOIN app.datastreams d ON d.id = c.datastream_id "
                "JOIN app.projects p ON p.id = d.project_id "
                "LEFT JOIN app.connector_activations a "
                " ON a.org_id = p.org_id "
                "AND a.connector_name = COALESCE(d.config->>'connector_name', d.module_name) "
                "AND a.environment = %s "
                "WHERE c.token_hash = %s LIMIT 1",
                (environment, presented_hash),
            )
            row = cur.fetchone()
        if row is not None and len(row) == 8:
            row = (*row, "my_connector", "draft", [row[2]], "ACTIVE")

        if row is None:
            connector_name = "__unknown__"
            datastream_id = None
            channel = "__unknown__"
        else:
            connector_name = str(row[8] or "")
            datastream_id = str(row[1])
            channel = str(row[2])

        # Unknown, revoked and valid capabilities all traverse the same three
        # serialized scopes. Unknown attempts share opaque aggregate buckets;
        # no presented hash or derivative is persisted.
        _enforce_resolution_rate_limit(
            conn,
            environment=environment,
            connector_name=connector_name or "__unknown__",
            datastream_id=datastream_id,
            channel=channel,
        )
        _record_resolution_rate_event(
            conn,
            environment=environment,
            connector_name=connector_name or "__unknown__",
            datastream_id=datastream_id,
            channel=channel,
        )
    except Exception:  # noqa: BLE001 -- fail-closed, non-enumerating
        return dict(_DENIAL)

    if row is None:
        return dict(_DENIAL)

    (
        cred_id,
        datastream_id,
        channel,
        stored_hash,
        state,
        version,
        overlap_until,
        expires_at,
        _connector_name,
        lifecycle_state,
        raw_channels,
        activation_state,
    ) = row
    if not hmac.compare_digest(presented_hash, stored_hash):
        return dict(_DENIAL)

    configured_channels = {
        "email" if str(value).strip().lower() == "inbound_email" else str(value).strip().lower()
        for value in (raw_channels or [])
    }
    if (
        state not in _NONTERMINAL_STATES
        or lifecycle_state not in {"draft", "active"}
        or activation_state != "ACTIVE"
        or channel not in configured_channels
    ):
        return dict(_DENIAL)

    try:
        expires_boundary = _utc_boundary(expires_at)
        overlap_boundary = _utc_boundary(overlap_until)
        now = datetime.datetime.now(datetime.timezone.utc)
        due = (
            expires_boundary is not None
            and now >= expires_boundary
            or state == "ROTATING"
            and (overlap_boundary is None or now >= overlap_boundary)
        )
        if due:
            _materialize_due_expirations(conn, credential_id=cred_id)
            return dict(_DENIAL)
    except Exception:  # noqa: BLE001
        return dict(_DENIAL)

    return {
        "allowed": True,
        "scope": {
            "datastream_id": datastream_id,
            "channel": channel,
            "version": version,
            "credential_id": cred_id,
        },
    }


# ---------------------------------------------------------------------------
# get_credential_state -- safe read-model, NO hash or raw token (AC2).
# ---------------------------------------------------------------------------


def get_credential_state(
    conn,
    *,
    credential_id: str,
    datastream_id: str,
) -> dict[str, Any] | None:
    """Return the safe read-model for one credential row, or None if absent.

    None means the credential does not exist for this Datastream (callers
    return a nondisclosing 404). Does NOT raise for absence.

    NEVER returns token_hash, raw token, or any secret (E38-NFR03, AC2).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, channel, safe_suffix, state, version, expires_at, "
            "overlap_until, issued_by, created_at "
            "FROM app.datastream_inbound_credentials "
            "WHERE id = %s AND datastream_id = %s",
            (credential_id, datastream_id),
        )
        row = cur.fetchone()

    if row is None:
        return None

    (
        cred_id,
        channel,
        safe_suffix,
        state,
        version,
        expires_at,
        overlap_until,
        issued_by,
        created_at,
    ) = row

    return _safe_read_model(
        credential_id=cred_id,
        datastream_id=datastream_id,
        channel=channel,
        safe_suffix=safe_suffix,
        state=state,
        version=version,
        expires_at=expires_at,
        overlap_until=overlap_until,
        issued_by=issued_by,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# list_credentials -- safe read-model list for one Datastream (AC2).
# ---------------------------------------------------------------------------


def list_credentials(
    conn,
    *,
    datastream_id: str,
    include_terminal: bool = False,
) -> list[dict[str, Any]]:
    """Return all credential rows for one Datastream as safe read-models.

    By default returns only non-terminal (ACTIVE, ROTATING) rows.
    Pass ``include_terminal=True`` to include REVOKED and EXPIRED rows.

    NEVER returns token_hash, raw token, or any secret (E38-NFR03).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, channel, safe_suffix, state, version, expires_at, "
            "overlap_until, issued_by, created_at "
            "FROM app.datastream_inbound_credentials "
            "WHERE datastream_id = %s ORDER BY version DESC",
            (datastream_id,),
        )
        rows = cur.fetchall()

    result = []
    for row in rows:
        (
            cred_id,
            channel,
            safe_suffix,
            state,
            version,
            expires_at,
            overlap_until,
            issued_by,
            created_at,
        ) = row
        model = _safe_read_model(
            credential_id=cred_id,
            datastream_id=datastream_id,
            channel=channel,
            safe_suffix=safe_suffix,
            state=state,
            version=version,
            expires_at=expires_at,
            overlap_until=overlap_until,
            issued_by=issued_by,
            created_at=created_at,
        )
        if include_terminal or model["state"] not in _TERMINAL_STATES:
            result.append(model)

    return result
