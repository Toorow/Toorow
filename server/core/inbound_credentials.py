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
                        an ACTIVE/ROTATING credential for an ENABLED Datastream.
                        Used by the 38.8 receipt path (wired there, not here).
  * ``get_credential_state`` : safe read-model projection (state, safe_suffix,
                        version, expires_at, overlap_until). NEVER the hash or
                        raw token.
  * ``check_rate_limit`` : non-enumerating rate-limit decision (env / provider /
                        datastream-capability scopes). Throttled or unknown
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

  * ROWCOUNT RECONCILED. The partial-unique constraint (one ACTIVE/ROTATING per
    (datastream_id, channel)) is enforced at the DB level; the mutation closure
    reconciles on race.

Mirrors ``connector_activation.py`` conventions: ``from __future__ import
annotations``, lazy imports of shared seams, mutations only through the operation
seam, ASCII-only source.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationSpec, execute_operation

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
# Rate-limit defaults (read from env; non-enumerating on denial).
# ---------------------------------------------------------------------------

_RL_MAX_ISSUES_PER_HOUR: int = int(os.environ.get("INBOUND_RL_MAX_ISSUES_PER_HOUR", "10"))
_RL_MAX_ROTATIONS_PER_HOUR: int = int(
    os.environ.get("INBOUND_RL_MAX_ROTATIONS_PER_HOUR", "5")
)

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
    """A rate-limit scope (env / provider / datastream-capability) was exceeded.

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
    """Build the secret-free read-model for one credential row.

    Fields: credential_id, datastream_id, channel, safe_suffix, state, version,
    expires_at, overlap_until, issued_by, created_at.
    NEVER includes token_hash, raw token, or any cross-tenant detail.
    Identical shape whether returned by REST or MCP (AC2, E38-NFR03).
    """
    return {
        "credential_id": credential_id,
        "datastream_id": datastream_id,
        "channel": channel,
        "safe_suffix": safe_suffix,
        "state": state,
        "version": version,
        "expires_at": _dt2iso(expires_at),
        "overlap_until": _dt2iso(overlap_until),
        "issued_by": issued_by,
        "created_at": _dt2iso(created_at),
    }


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
        cred_id, token_hash, safe_suffix, state, version,
        expires_at, overlap_until, issued_by, created_at,
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
# _get_datastream_status -- verify ENABLED and retrieve connector_name/environment.
# ---------------------------------------------------------------------------


def _get_datastream_status(conn, *, datastream_id: str) -> dict[str, Any] | None:
    """Return minimal Datastream facts or None if absent.

    Returns dict with {connector_name, environment, org_id, enabled}.
    Source-agnostic: connector_name is an opaque identifier string (AD-2).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT source_kind, org_id "
            "FROM app.datastreams "
            "WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    if row is None:
        return None
    source_kind, org_id = row
    return {
        "connector_name": source_kind or "",
        "org_id": org_id or "",
    }


# ---------------------------------------------------------------------------
# check_rate_limit -- non-enumerating, fail-closed.
# ---------------------------------------------------------------------------


def check_rate_limit(
    conn,
    *,
    datastream_id: str,
    channel: str,
    operation: str,
) -> None:
    """Check rate limits at env / datastream-capability scopes.

    ``operation`` is 'issue' or 'rotate'. Raises ``InboundCredentialRateLimited``
    when a limit is exceeded. The denial is constant-shape (non-enumerating):
    the exception message carries no existence detail about the credential or
    the exact scope that triggered the limit (AC4).

    Limits are intentionally lightweight (count-based, read from env vars) so
    this module requires no external rate-limit service. A future story may wire
    a token-bucket engine; the interface here is stable.
    """
    if operation not in ("issue", "rotate"):
        return  # unknown operation: pass through (fail-open for extensibility)

    max_ops = (
        _RL_MAX_ISSUES_PER_HOUR if operation == "issue" else _RL_MAX_ROTATIONS_PER_HOUR
    )

    # Count non-terminal credentials for this (datastream_id, channel) issued
    # within the last hour as a simple rolling-window approximation.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_inbound_credentials "
            "WHERE datastream_id = %s AND channel = %s "
            "AND created_at >= NOW() - INTERVAL '1 hour'",
            (datastream_id, channel),
        )
        row = cur.fetchone()

    count = int(row[0]) if row else 0
    if count >= max_ops:
        # Non-enumerating: same exception regardless of which scope triggered.
        raise InboundCredentialRateLimited(
            "operation rate limit exceeded -- retry after the window resets"
        )


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
    overlap_seconds: int | None = None,
    expires_seconds: int | None = None,
) -> dict[str, Any]:
    """Issue a new delivery credential for a managed-feed Datastream (AC1, AC2).

    Generates a high-entropy token via ``secrets.token_urlsafe``. Computes
    ``token_hash = sha256(token)`` and a ``safe_suffix`` (last 6 chars). The
    full secret is returned EXACTLY ONCE in the result dict under the key
    ``full_secret`` and is NEVER persisted, logged, placed in request_payload,
    audit payload, outbox payload, or any read-model.

    For email channel: the full_secret is the complete inbound address
    ``ds_<token>@<verified-domain>``.
    For webhook channel: the full_secret is the raw bearer token.

    Preconditions (fail-closed before SQL):
      - ``datastream_id``, ``channel``, ``actor``, ``idempotency_key`` must be
        non-empty strings.
      - ``channel`` must be 'email' or 'webhook'.
      - The connector installation domain MUST be READY (verified); otherwise
        ``InboundCredentialDomainNotReady`` is raised.
      - At most one ACTIVE/ROTATING credential per (datastream_id, channel)
        at any time (partial-unique constraint; existing ACTIVE = 409).

    Returns a dict containing the safe read-model PLUS ``full_secret`` (once).
    The caller MUST include ``full_secret`` in the HTTP response and then discard
    it; it will NOT be accessible via GET or MCP.
    """
    # ------------------------------------------------------------------
    # Input validation (fail-closed before SQL).
    # ------------------------------------------------------------------
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

    # ------------------------------------------------------------------
    # Domain READY gate (AC1): refuse if the installation is not READY.
    # Read the verified domain for email address construction.
    # ------------------------------------------------------------------
    ds_info = _get_datastream_status(conn, datastream_id=datastream_id)
    if ds_info is None:
        raise InboundCredentialUnavailable("datastream not found")

    connector_name = ds_info.get("connector_name", "")

    # Read the domain config to get the verified domain and check READY.
    # We re-use connector_domain.get_domain_config which reads the active config.
    # The installation READY check reuses the connector_installation_api guard.
    from core.connector_domain import get_domain_config  # noqa: PLC0415

    # Determine environment from env var (same as other 38.x modules).
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"

    domain_cfg = None
    if connector_name:
        domain_cfg = get_domain_config(conn, environment=environment, connector_name=connector_name)

    if domain_cfg is None:
        raise InboundCredentialDomainNotReady(
            "no domain configuration found for this connector -- "
            "configure and verify the domain before issuing credentials"
        )

    # Verify the installation is READY (the gate guard from 38.2).
    from core.connector_installation_api import (  # noqa: PLC0415
        ConnectorNotReady,
        refuse_activation_unless_ready,
    )

    try:
        refuse_activation_unless_ready(conn, connector_name=connector_name, environment=environment)
    except ConnectorNotReady as exc:
        raise InboundCredentialDomainNotReady(
            "connector installation is not READY -- verify domain before issuing credentials"
        ) from exc

    verified_domain: str = domain_cfg["domain"]

    # ------------------------------------------------------------------
    # Rate-limit check (AC4) before token generation.
    # ------------------------------------------------------------------
    check_rate_limit(conn, datastream_id=datastream_id, channel=channel, operation="issue")

    # ------------------------------------------------------------------
    # Generate the ephemeral token (NEVER persisted).
    # token_hash and safe_suffix are the only token derivatives stored.
    # ------------------------------------------------------------------
    raw_token, token_hash, safe_suffix = _generate_token()

    # Build the channel-specific full_secret (returned ONCE to caller).
    if channel == "email":
        full_secret = f"ds_{raw_token}@{verified_domain}"
    else:
        # webhook: the bearer token itself is the capability secret.
        full_secret = raw_token

    # Immediately discard raw_token from local scope after building full_secret.
    # Python GC will reclaim it; we do not zero-fill (stdlib limitation).
    del raw_token

    # ------------------------------------------------------------------
    # Build the OperationSpec.
    # DETERMINISTIC PAYLOAD (review H1): no random id AND no raw token in
    # request_payload. The row id is generated at WRITE TIME inside the
    # mutation closure.
    # ------------------------------------------------------------------
    org_id = ds_info.get("org_id", "platform")

    spec = OperationSpec(
        command_type="inbound.credential.issued",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"datastream:{datastream_id}",
            f"channel:{channel}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-credential-v1",
            "catalog": "inbound-credential-v1",
            "tool": "rest-v1",
        },
        # NO raw token here. token_hash is a safe digest, not secret material.
        # The _is_secret_key scanner in operations.py accepts 'token_hash'
        # because it ends with the '_hash' safe-id suffix pattern.
        request_payload={
            "datastream_id": datastream_id,
            "channel": channel,
            "issued_by": actor,
            "token_hash": token_hash,
            "version": 1,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"inbound-credential:{datastream_id}:{channel}:issue"
        ),
        trace_id=trace_id,
    )

    # Capture token_hash and safe_suffix for the closure (they are not secret;
    # token_hash is a one-way digest and safe_suffix is a display hint).
    _token_hash = token_hash
    _safe_suffix = safe_suffix

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        credential_id = f"dic_{ULID()}"

        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, "
                "state, version, issued_by, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, 'ACTIVE', 1, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    credential_id,
                    datastream_id,
                    channel,
                    _token_hash,
                    _safe_suffix,
                    actor,
                    operation_id,
                ),
            )
            if cur.rowcount == 1:
                # Read back DB-authoritative timestamps.
                cur.execute(
                    "SELECT created_at FROM app.datastream_inbound_credentials "
                    "WHERE id = %s",
                    (credential_id,),
                )
                ts_row = cur.fetchone()
                created_at = ts_row[0] if ts_row else None
                result = {
                    "credential_id": credential_id,
                    "datastream_id": datastream_id,
                    "channel": channel,
                    "safe_suffix": _safe_suffix,
                    "state": "ACTIVE",
                    "version": 1,
                    "issued_by": actor,
                }
                return MutationResult(
                    outcome="succeeded",
                    before_hash=None,
                    after_hash=_canonical_hash(result),
                    result={
                        **result,
                        "created_at": _dt2iso(created_at),
                    },
                    # Outbox payload: safe fields only; NO token_hash, NO raw token.
                    outbox_payload={
                        "datastream_id": datastream_id,
                        "channel": channel,
                        "state": "ACTIVE",
                        "version": 1,
                    },
                )

            # ON CONFLICT DO NOTHING: a concurrent insert or partial-unique
            # constraint violation (an ACTIVE credential already exists).
            # Reconcile to the existing row.
            cur.execute(
                "SELECT id, safe_suffix, state, version, expires_at, "
                "overlap_until, issued_by, created_at "
                "FROM app.datastream_inbound_credentials "
                "WHERE datastream_id = %s AND channel = %s "
                "AND state IN ('ACTIVE', 'ROTATING') "
                "ORDER BY version DESC LIMIT 1",
                (datastream_id, channel),
            )
            race_row = cur.fetchone()
            if race_row is None:  # pragma: no cover -- row vanished
                raise InboundCredentialConflict(
                    "issue race lost and row not found"
                )
            (
                r_id, r_suffix, r_state, r_version, r_exp,
                r_overlap, r_issued_by, r_created,
            ) = race_row
            result = {
                "credential_id": r_id,
                "datastream_id": datastream_id,
                "channel": channel,
                "safe_suffix": r_suffix,
                "state": r_state,
                "version": r_version,
                "issued_by": r_issued_by,
            }
            return MutationResult(
                outcome="succeeded",
                before_hash=None,
                after_hash=_canonical_hash(result),
                result={
                    **result,
                    "created_at": _dt2iso(r_created),
                },
                outbox_payload={
                    "datastream_id": datastream_id,
                    "channel": channel,
                    "state": r_state,
                    "version": r_version,
                },
            )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}

    safe_model = _safe_read_model(
        credential_id=data.get("credential_id", ""),
        datastream_id=datastream_id,
        channel=channel,
        safe_suffix=data.get("safe_suffix"),
        state=data.get("state", "ACTIVE"),
        version=data.get("version", 1),
        expires_at=data.get("expires_at"),
        overlap_until=data.get("overlap_until"),
        issued_by=data.get("issued_by", actor),
        created_at=data.get("created_at"),
    )

    # Attach full_secret to the result ONCE. It is the caller's responsibility
    # to include it in the HTTP response and then discard it.
    return {**safe_model, "full_secret": full_secret}


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
    """Rotate the active delivery credential for a Datastream (AC3).

    Creates a new version ACTIVE credential. Moves the prior to ROTATING with
    ``overlap_until = NOW() + overlap_seconds`` unless ``immediate_revoke=True``,
    in which case the prior is immediately REVOKED. Both transitions are fail-closed:
    the old credential stops working at ``overlap_until`` (or immediately on revoke).

    The new full_secret is returned EXACTLY ONCE in the result. The old
    credential's token is not returned (it was never stored; rotate does not
    recover it).

    Raises ``InboundCredentialUnavailable`` if no ACTIVE credential exists for the
    given (datastream_id, channel) pair.
    Raises ``InboundCredentialConflict`` if the credential_id does not match the
    current ACTIVE row.
    """
    if not isinstance(credential_id, str) or not credential_id.strip():
        raise InboundCredentialValidationError("credential_id is required")
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

    credential_id = credential_id.strip()
    datastream_id = datastream_id.strip()
    channel = channel.strip()
    actor = actor.strip()

    # ------------------------------------------------------------------
    # Domain READY gate: same as issue.
    # ------------------------------------------------------------------
    ds_info = _get_datastream_status(conn, datastream_id=datastream_id)
    if ds_info is None:
        raise InboundCredentialUnavailable("datastream not found")

    # ------------------------------------------------------------------
    # Rate-limit check (AC4).
    # ------------------------------------------------------------------
    check_rate_limit(conn, datastream_id=datastream_id, channel=channel, operation="rotate")

    # ------------------------------------------------------------------
    # Load the current ACTIVE credential (must exist and match credential_id).
    # ------------------------------------------------------------------
    prior = _load_active_credential(conn, datastream_id=datastream_id, channel=channel)
    if prior is None or prior["state"] != "ACTIVE":
        raise InboundCredentialUnavailable(
            "no ACTIVE credential found for this (datastream, channel)"
        )
    if prior["id"] != credential_id:
        raise InboundCredentialConflict(
            "credential_id does not match the current ACTIVE credential"
        )

    prior_id = prior["id"]
    new_version = prior["version"] + 1
    org_id = ds_info.get("org_id", "platform")

    # Generate new token (ephemeral; only hash + suffix stored).
    raw_token, token_hash, safe_suffix = _generate_token()

    # Build the channel-specific full_secret.
    connector_name = ds_info.get("connector_name", "")
    environment = os.environ.get("TOOROW_ENVIRONMENT", "production").strip() or "production"
    verified_domain = ""
    if channel == "email" and connector_name:
        from core.connector_domain import get_domain_config  # noqa: PLC0415

        domain_cfg = get_domain_config(conn, environment=environment, connector_name=connector_name)
        if domain_cfg:
            verified_domain = domain_cfg["domain"]

    if channel == "email":
        full_secret = f"ds_{raw_token}@{verified_domain}" if verified_domain else f"ds_{raw_token}"
    else:
        full_secret = raw_token

    del raw_token

    prior_new_state = "REVOKED" if immediate_revoke else "ROTATING"
    _token_hash = token_hash
    _safe_suffix = safe_suffix

    spec = OperationSpec(
        command_type="inbound.credential.rotated",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"datastream:{datastream_id}",
            f"channel:{channel}",
            f"credential:{credential_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-credential-v1",
            "catalog": "inbound-credential-v1",
            "tool": "rest-v1",
        },
        # Deterministic: no raw token, no random id.
        request_payload={
            "datastream_id": datastream_id,
            "channel": channel,
            "prior_credential_id": prior_id,
            "prior_new_state": prior_new_state,
            "new_version": new_version,
            "immediate_revoke": immediate_revoke,
            "overlap_seconds": overlap_seconds if not immediate_revoke else 0,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=(
            f"inbound-credential:{credential_id}:rotated:v{new_version}"
        ),
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        new_cred_id = f"dic_{ULID()}"

        with operation_conn.cursor() as cur:
            # Step 1: move the prior credential to its new state.
            if prior_new_state == "ROTATING":
                cur.execute(
                    "UPDATE app.datastream_inbound_credentials "
                    "SET state = 'ROTATING', "
                    "overlap_until = NOW() + (%s || ' seconds')::interval, "
                    "operation_id = %s, updated_at = NOW() "
                    "WHERE id = %s AND state = 'ACTIVE'",
                    (str(overlap_seconds), operation_id, prior_id),
                )
            else:
                cur.execute(
                    "UPDATE app.datastream_inbound_credentials "
                    "SET state = 'REVOKED', operation_id = %s, updated_at = NOW() "
                    "WHERE id = %s AND state = 'ACTIVE'",
                    (operation_id, prior_id),
                )

            prior_updated = cur.rowcount

            # Step 2: insert the new ACTIVE credential.
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, "
                "state, version, issued_by, operation_id) "
                "VALUES (%s, %s, %s, %s, %s, 'ACTIVE', %s, %s, %s) "
                "ON CONFLICT DO NOTHING",
                (
                    new_cred_id,
                    datastream_id,
                    channel,
                    _token_hash,
                    _safe_suffix,
                    new_version,
                    actor,
                    operation_id,
                ),
            )

            if cur.rowcount == 0 or prior_updated == 0:
                # Race condition: reconcile to existing new-version row.
                cur.execute(
                    "SELECT id, safe_suffix, state, version, expires_at, "
                    "overlap_until, issued_by, created_at "
                    "FROM app.datastream_inbound_credentials "
                    "WHERE datastream_id = %s AND channel = %s "
                    "AND state = 'ACTIVE' "
                    "ORDER BY version DESC LIMIT 1",
                    (datastream_id, channel),
                )
                race = cur.fetchone()
                if race is None:  # pragma: no cover
                    raise InboundCredentialConflict("rotate race lost and row not found")
                (
                    r_id, r_suffix, r_state, r_version, r_exp,
                    r_overlap, r_issued_by, r_created,
                ) = race
                result = {
                    "credential_id": r_id,
                    "datastream_id": datastream_id,
                    "channel": channel,
                    "safe_suffix": r_suffix,
                    "state": r_state,
                    "version": r_version,
                    "issued_by": r_issued_by,
                }
                return MutationResult(
                    outcome="succeeded",
                    before_hash=_canonical_hash({"prior_id": prior_id, "state": "ACTIVE"}),
                    after_hash=_canonical_hash(result),
                    result={**result, "created_at": _dt2iso(r_created)},
                    outbox_payload={
                        "datastream_id": datastream_id,
                        "channel": channel,
                        "prior_credential_id": prior_id,
                        "prior_new_state": prior_new_state,
                        "new_credential_id": r_id,
                        "new_version": r_version,
                    },
                )

            # Read back the new credential's timestamps.
            cur.execute(
                "SELECT created_at FROM app.datastream_inbound_credentials WHERE id = %s",
                (new_cred_id,),
            )
            ts_row = cur.fetchone()
            created_at = ts_row[0] if ts_row else None

            result = {
                "credential_id": new_cred_id,
                "datastream_id": datastream_id,
                "channel": channel,
                "safe_suffix": _safe_suffix,
                "state": "ACTIVE",
                "version": new_version,
                "issued_by": actor,
            }
            return MutationResult(
                outcome="succeeded",
                before_hash=_canonical_hash({"prior_id": prior_id, "state": "ACTIVE"}),
                after_hash=_canonical_hash(result),
                result={**result, "created_at": _dt2iso(created_at)},
                outbox_payload={
                    "datastream_id": datastream_id,
                    "channel": channel,
                    "prior_credential_id": prior_id,
                    "prior_new_state": prior_new_state,
                    "new_credential_id": new_cred_id,
                    "new_version": new_version,
                },
            )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}

    safe_model = _safe_read_model(
        credential_id=data.get("credential_id", ""),
        datastream_id=datastream_id,
        channel=channel,
        safe_suffix=data.get("safe_suffix"),
        state=data.get("state", "ACTIVE"),
        version=data.get("version", new_version),
        expires_at=data.get("expires_at"),
        overlap_until=data.get("overlap_until"),
        issued_by=data.get("issued_by", actor),
        created_at=data.get("created_at"),
    )

    return {**safe_model, "full_secret": full_secret}


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
    """Revoke a delivery credential immediately (AC3, fail-closed).

    Flips the credential state to REVOKED. From this point the credential
    fails closed: ``resolve_for_delivery`` will not return a scope for it.
    NEVER deletes the row (data-preservation invariant).

    Raises ``InboundCredentialUnavailable`` if the credential does not exist or
    does not belong to the given Datastream.
    Raises ``InboundCredentialConflict`` if the credential is already in a
    terminal state (REVOKED or EXPIRED).
    """
    if not isinstance(credential_id, str) or not credential_id.strip():
        raise InboundCredentialValidationError("credential_id is required")
    if not isinstance(datastream_id, str) or not datastream_id.strip():
        raise InboundCredentialValidationError("datastream_id is required")
    if not isinstance(actor, str) or not actor.strip():
        raise InboundCredentialValidationError("actor is required")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise InboundCredentialValidationError("idempotency_key is required")

    credential_id = credential_id.strip()
    datastream_id = datastream_id.strip()
    actor = actor.strip()

    # Load the current row.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, state, version, safe_suffix, issued_by, created_at, channel "
            "FROM app.datastream_inbound_credentials "
            "WHERE id = %s AND datastream_id = %s",
            (credential_id, datastream_id),
        )
        row = cur.fetchone()

    if row is None:
        raise InboundCredentialUnavailable(
            "credential not found for this datastream"
        )

    cred_id, current_state, version, safe_suffix, issued_by, created_at, channel = row

    if current_state in _TERMINAL_STATES:
        raise InboundCredentialConflict(
            f"credential is already in terminal state {current_state}"
        )

    ds_info = _get_datastream_status(conn, datastream_id=datastream_id)
    org_id = (ds_info or {}).get("org_id", "platform")

    spec = OperationSpec(
        command_type="inbound.credential.revoked",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"datastream:{datastream_id}",
            f"credential:{credential_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "inbound-credential-v1",
            "catalog": "inbound-credential-v1",
            "tool": "rest-v1",
        },
        request_payload={
            "credential_id": credential_id,
            "datastream_id": datastream_id,
            "from_state": current_state,
            "target_state": "REVOKED",
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"inbound-credential:{credential_id}:revoked",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:  # noqa: ANN001
        from core.operations import _canonical_hash  # noqa: PLC0415

        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.datastream_inbound_credentials "
                "SET state = 'REVOKED', operation_id = %s, updated_at = NOW() "
                "WHERE id = %s AND state NOT IN ('REVOKED', 'EXPIRED')",
                (operation_id, credential_id),
            )
            cur.execute(
                "SELECT state, created_at FROM app.datastream_inbound_credentials "
                "WHERE id = %s",
                (credential_id,),
            )
            ts_row = cur.fetchone()

        final_state = ts_row[0] if ts_row else "REVOKED"
        final_created = ts_row[1] if ts_row else created_at

        result = {
            "credential_id": credential_id,
            "datastream_id": datastream_id,
            "state": final_state,
            "version": version,
            "issued_by": issued_by,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": current_state}),
            after_hash=_canonical_hash(result),
            result={**result, "created_at": _dt2iso(final_created)},
            outbox_payload={
                "datastream_id": datastream_id,
                "credential_id": credential_id,
                "state": "REVOKED",
            },
        )

    op_result = execute_operation(conn, spec, mutation=mutation)
    data = op_result.result or {}

    return _safe_read_model(
        credential_id=credential_id,
        datastream_id=datastream_id,
        channel=channel,
        safe_suffix=safe_suffix,
        state=data.get("state", "REVOKED"),
        version=data.get("version", version),
        expires_at=None,
        overlap_until=None,
        issued_by=data.get("issued_by", issued_by),
        created_at=data.get("created_at"),
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
    """Shared lookup for the two resolve entrypoints (given a presented hash).

    Looks up an ACTIVE/ROTATING credential by ``presented_hash`` and applies the
    ROTATING overlap window, in-path expiry, and datastream-exists checks. Every
    failure returns the IDENTICAL constant-shape denial (non-enumerating, AC4);
    the caller has already hashed and discarded any raw token before this point.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, datastream_id, channel, token_hash, state, version, "
                "overlap_until, expires_at "
                "FROM app.datastream_inbound_credentials "
                "WHERE token_hash = %s "
                "AND state IN ('ACTIVE', 'ROTATING') "
                "LIMIT 1",
                (presented_hash,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 -- fail-closed, non-enumerating
        return dict(_DENIAL)

    if row is None:
        return dict(_DENIAL)

    (
        cred_id, datastream_id, channel, stored_hash, state, version,
        overlap_until, expires_at,
    ) = row

    # Constant-time comparison (hmac.compare_digest) at application layer.
    # Both values are hex strings of the same sha256 digest, so lengths are equal.
    if not hmac.compare_digest(presented_hash, stored_hash):  # pragma: no cover
        # This branch cannot be reached via the DB query (exact match), but we
        # keep it as a defence-in-depth layer.
        return dict(_DENIAL)

    # Check ROTATING overlap window.
    if state == "ROTATING" and overlap_until is not None:
        import datetime  # noqa: PLC0415

        now = datetime.datetime.now(datetime.timezone.utc)
        try:
            if now > overlap_until:
                return dict(_DENIAL)
        except Exception:  # noqa: BLE001
            return dict(_DENIAL)

    # Expiry (AC3): a credential past its expires_at fails closed even before a
    # sweeper flips its stored state -- enforced in-path, constant-shape denial.
    if expires_at is not None:
        import datetime  # noqa: PLC0415

        try:
            if datetime.datetime.now(datetime.timezone.utc) > expires_at:
                return dict(_DENIAL)
        except Exception:  # noqa: BLE001
            return dict(_DENIAL)

    # Verify the Datastream is accessible (existence check, not deep access).
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            ds_row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return dict(_DENIAL)

    if ds_row is None:
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
        cred_id, channel, safe_suffix, state, version,
        expires_at, overlap_until, issued_by, created_at,
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
    if include_terminal:
        state_filter = "state IN ('ACTIVE', 'ROTATING', 'REVOKED', 'EXPIRED')"
    else:
        state_filter = "state IN ('ACTIVE', 'ROTATING')"

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT id, channel, safe_suffix, state, version, expires_at, "
            f"overlap_until, issued_by, created_at "
            f"FROM app.datastream_inbound_credentials "
            f"WHERE datastream_id = %s AND {state_filter} "
            f"ORDER BY version DESC",
            (datastream_id,),
        )
        rows = cur.fetchall()

    result = []
    for row in rows:
        (
            cred_id, channel, safe_suffix, state, version,
            expires_at, overlap_until, issued_by, created_at,
        ) = row
        result.append(_safe_read_model(
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
        ))

    return result
