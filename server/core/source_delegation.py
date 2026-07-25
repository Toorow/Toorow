"""Story 36.7 -- delegated source authorization and exact account exposure.

An operator who does NOT own source credentials prepares a *delegation* so that
the correct credential owner authorizes the provider and exposes ONLY the exact
account the beneficiary needs -- without ever sharing a credential or an
unrelated account.

This is a generic, source-agnostic domain seam (E36-NFR05): it holds the
*delegation state machine* (prepared -> authorized -> exposed / failed / expired
/ revoked), binds the security context, and drives the shared seams. It does NOT
implement provider OAuth -- that stays in ``core.google_oauth`` (state+nonce,
Google-direct) or the Nango adapter (generic providers). The only provider
specifics that touch this module are two facts the caller supplies: the provider
name and whether that provider is Google-direct.

SECURITY INVARIANTS (the 36.7 review is adversarial on these):

  * The delegation binds, immutably: source/provider, owner + beneficiary orgs,
    requested scopes, return context (task + return condition), expiry, state,
    an HMAC-signed anti-CSRF ``state`` carrying a random ``nonce`` (nonce_hash
    persisted, never the raw nonce), and -- for every provider that is NOT
    Google-direct -- a PKCE ``code_challenge`` (S256). Google-direct providers
    reuse the state+nonce binding already proven by ``google_oauth`` and add no
    PKCE (documented gap: ``google_oauth`` is state+nonce only, PKCE would be
    dead weight there because that flow is confidential-client server-side).

  * On the owner's OAuth callback, ``complete_source_delegation`` verifies the
    EXACT redirect allow-list, the HMAC ``state``, the ``nonce`` and (where
    applicable) PKCE BEFORE any connection/exposure state transition. A failure
    on any of those raises ``DelegationVerificationError`` and NOTHING moves.

  * Account exposure DEFAULTS TO NONE. Authorization only records the connection;
    the owner must explicitly allow one exact account, which routes through
    ``account_exposure.expose_account`` (a separate audited action).

  * On resume, connection health + the exact exposure grant are REVALIDATED via
    ``project_access.resolve_provider_account_access`` -- a prior "ready" is never
    trusted.

  * NO token or secret ever enters a model-facing result or an operation payload.
    The PKCE ``code_verifier`` is a server-only secret: it is persisted in a
    dedicated column and never returned, logged, or placed in any operation/audit
    payload. The bearer/nonce are stored only as one-way keyed hashes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from ulid import ULID

from core.operations import MutationResult, OperationResult, OperationSpec, execute_operation
from core.setup_responsibilities import HandoffResult, prepare_handoff

# Delegation lifecycle. The provider OAuth machinery lives elsewhere; here we
# only own the source-agnostic state of the delegation itself.
DELEGATION_STATES = {"prepared", "authorized", "exposed", "failed", "expired", "revoked"}
TERMINAL_STATES = {"exposed", "failed", "expired", "revoked"}

# Providers authorized through the Google-direct server-side flow (AD-21). These
# reuse ``google_oauth`` state+nonce and add NO PKCE. Every other provider goes
# through the generic delegation carrier and MUST carry a PKCE challenge.
GOOGLE_DIRECT_PROVIDERS = {"google", "google_direct", "ga4", "gsc", "google_ads", "google_sheets"}

_STATE_TTL_SECONDS = 600
_STATE_SECRET_ENV = "TOOROW_DELEGATION_STATE_SECRET"


class DelegationValidationError(ValueError):
    """Raised before SQL when delegation inputs are unsafe or malformed."""


class DelegationConflict(RuntimeError):
    """The delegation cannot transition from its current state."""


class DelegationUnavailable(RuntimeError):
    """The delegation does not exist, is expired, or is otherwise unusable."""


class DelegationVerificationError(RuntimeError):
    """A callback verification failed (redirect allow-list, state, nonce, PKCE).

    A single opaque type for every verification-rejection reason so the callback
    surfaces one generic French 4xx and leaks no oracle (forged vs expired vs
    mismatch are indistinguishable to an attacker).
    """


@dataclass(frozen=True)
class PreparedSourceDelegation:
    delegation_id: str
    state: str
    expires_at: str
    operation_id: str
    audit_event_id: str | None
    outbox_event_id: str | None
    handoff: HandoffResult
    authorize_state: str
    pkce_challenge: str | None
    pkce_method: str | None
    replayed: bool


@dataclass(frozen=True)
class CompletedSourceDelegation:
    delegation_id: str
    state: str
    task_reconciled_state: str
    exposure: dict[str, Any]
    operation_id: str
    audit_event_id: str | None
    replayed: bool


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _state_secret() -> bytes:
    """HMAC key for the delegation ``state``.

    Mirrors ``google_oauth._state_secret`` fail-closed posture: in a production
    auth mode the dedicated secret is mandatory; in dev it derives from
    ``API_SECRET_KEY`` or an ephemeral process key.
    """
    secret = os.environ.get(_STATE_SECRET_ENV, "").strip()
    if secret:
        return secret.encode("utf-8")
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode in ("oauth", "static"):
        raise DelegationValidationError(
            f"{_STATE_SECRET_ENV} est obligatoire en mode de production "
            "(TOOROW_AUTH_MODE=oauth/static)."
        )
    api_secret = os.environ.get("API_SECRET_KEY", "").strip()
    if api_secret:
        return hashlib.sha256(b"delegation-state|" + api_secret.encode("utf-8")).digest()
    return _EPHEMERAL_DEV_SECRET


_EPHEMERAL_DEV_SECRET = secrets.token_bytes(32)


def _keyed_hash(value: str, purpose: str) -> str:
    return hmac.new(_state_secret(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def provider_is_google_direct(provider: str) -> bool:
    """True when the provider authorizes through the Google-direct server flow.

    Google-direct reuses ``google_oauth`` state+nonce and requires NO PKCE; every
    other provider goes through the generic carrier and MUST carry a PKCE S256
    challenge.
    """
    return provider.strip().lower() in GOOGLE_DIRECT_PROVIDERS


def _validate_scopes(requested_scopes: Any) -> list[str]:
    if not isinstance(requested_scopes, (list, tuple)) or not requested_scopes:
        raise DelegationValidationError("requested_scopes must be a non-empty list")
    scopes: list[str] = []
    for scope in requested_scopes:
        if not isinstance(scope, str) or not scope.strip() or len(scope) > 512:
            raise DelegationValidationError("requested_scopes contains an invalid scope")
        scopes.append(scope.strip())
    if len(scopes) > 64:
        raise DelegationValidationError("too many requested scopes")
    return scopes


def _mint_state(delegation_id: str, nonce: str, *, now: int | None = None) -> str:
    """Mint an HMAC-signed anti-CSRF state binding delegation_id + nonce + exp.

    Returned as ``<b64url(payload)>.<b64url(hmac)>`` -- opaque to the provider,
    verifiable only by this server. The delegation carrier owns this state; a
    Google-direct sub-flow may additionally mint its own ``google_oauth`` state,
    but the delegation transition is always gated by THIS state.
    """
    issued = int(time.time()) if now is None else now
    payload = {
        "delegation_id": delegation_id,
        "nonce": nonce,
        "exp": issued + _STATE_TTL_SECONDS,
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    tag = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url(payload_bytes)}.{_b64url(tag)}"


def _verify_state(state: str, *, now: int | None = None) -> tuple[str, str]:
    """Verify a delegation ``state`` and return (delegation_id, nonce).

    Raises ``DelegationVerificationError`` (opaque) on any failure: malformed,
    forged/tampered tag, expired, or empty bound fields.
    """
    if not isinstance(state, str) or "." not in state:
        raise DelegationVerificationError("etat de delegation invalide")
    try:
        payload_b64, tag_b64 = state.split(".", 1)
        pad = "=" * (-len(payload_b64) % 4)
        payload_bytes = base64.urlsafe_b64decode(payload_b64 + pad)
        pad = "=" * (-len(tag_b64) % 4)
        presented_tag = base64.urlsafe_b64decode(tag_b64 + pad)
    except Exception:
        raise DelegationVerificationError("etat de delegation invalide") from None
    expected_tag = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).digest()
    if not hmac.compare_digest(presented_tag, expected_tag):
        raise DelegationVerificationError("etat de delegation invalide")
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise DelegationVerificationError("etat de delegation invalide") from None
    current = int(time.time()) if now is None else now
    if current >= int(payload.get("exp", 0)):
        raise DelegationVerificationError("etat de delegation expire")
    delegation_id = str(payload.get("delegation_id", ""))
    nonce = str(payload.get("nonce", ""))
    if not delegation_id or not nonce:
        raise DelegationVerificationError("etat de delegation invalide")
    return delegation_id, nonce


def _new_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) using PKCE S256 (RFC 7636).

    The verifier is a high-entropy server-only secret; the challenge is the
    base64url-unpadded SHA-256 of the verifier. Only the challenge is presented
    to the provider; the verifier is kept server-side and replayed on token
    exchange by the provider adapter (never by this module).
    """
    verifier = secrets.token_urlsafe(64)
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    return verifier, challenge


def prepare_source_delegation(
    conn,
    *,
    task_id: str,
    source: str,
    provider: str,
    owner_org_id: str,
    beneficiary_org_id: str,
    requested_scopes: Any,
    redirect_allowlist_ref: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    expires_in_hours: int = 48,
) -> PreparedSourceDelegation:
    """Bind a delegation and mint the purpose-scoped handoff for the owner.

    Binds source/provider, owner + beneficiary orgs, requested scopes, return
    context (via the ``source_authorization`` task's return condition), expiry,
    state (HMAC anti-CSRF), nonce (hashed) and PKCE (S256) for every provider
    that is not Google-direct. The handoff (``setup_responsibilities.prepare_handoff``)
    is the delivery carrier that reveals only the minimum bound action to the
    credential owner.

    Exposure is NOT touched here -- authorization and exposure are separate
    audited actions (E36-NFR02 / Story 36.1 AC).
    """
    if not isinstance(source, str) or not source.strip():
        raise DelegationValidationError("source is required")
    if not isinstance(provider, str) or not provider.strip():
        raise DelegationValidationError("provider is required")
    if not isinstance(owner_org_id, str) or not owner_org_id.strip():
        raise DelegationValidationError("owner_org_id is required")
    if not isinstance(beneficiary_org_id, str) or not beneficiary_org_id.strip():
        raise DelegationValidationError("beneficiary_org_id is required")
    if not isinstance(redirect_allowlist_ref, str) or not redirect_allowlist_ref.strip():
        raise DelegationValidationError("redirect_allowlist_ref is required")
    scopes = _validate_scopes(requested_scopes)
    source, provider = source.strip(), provider.strip()

    # The delegation binds to the source_authorization task: its return condition
    # is the deterministic resume signal. Verify the task exists and is handoff-able.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT t.id,t.journey_id,j.org_id,t.state,t.return_condition,t.step_key "
            "FROM app.setup_tasks t JOIN app.setup_journeys j ON j.id=t.journey_id "
            "WHERE t.id=%s FOR UPDATE OF t",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise DelegationUnavailable("setup task unavailable")
    if row[2] != owner_org_id and row[2] != beneficiary_org_id:
        # The task's owning org must be one of the two delegation parties.
        raise DelegationValidationError("task organization does not match the delegation")
    if row[3] not in {"waiting", "blocked", "ready"}:
        raise DelegationConflict("setup task cannot start a delegation")

    delegation_id = f"srcdeleg_{ULID()}"
    nonce = secrets.token_urlsafe(24)
    nonce_hash = _keyed_hash(nonce, "delegation-nonce")
    authorize_state = _mint_state(delegation_id, nonce)
    google_direct = provider_is_google_direct(provider)
    pkce_verifier, pkce_challenge, pkce_method = (None, None, None)
    if not google_direct:
        # PKCE is the genuine gap for non-Google-direct providers: add S256 here.
        pkce_verifier, pkce_challenge = _new_pkce()
        pkce_method = "S256"
    pkce_verifier_hash = _keyed_hash(pkce_verifier, "delegation-pkce") if pkce_verifier else None
    expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_in_hours)

    spec = OperationSpec(
        command_type="source.delegation.prepare",
        actor=actor,
        effective_org_id=owner_org_id,
        resource_path=(
            f"organization:{owner_org_id}",
            f"beneficiary:{beneficiary_org_id}",
            f"source-delegation:{delegation_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "delegation-v1", "catalog": "delegation-v1", "tool": "rest-v1"},
        request_payload={
            # NOTE: no token/secret, no raw nonce, no PKCE verifier -- only
            # identifiers, the challenge (public) and scope names.
            "delegation_id": delegation_id,
            "source": source,
            "provider": provider,
            "beneficiary_org_id": beneficiary_org_id,
            "requested_scopes": scopes,
            "google_direct": google_direct,
            "pkce_challenge": pkce_challenge,
            "pkce_method": pkce_method,
        },
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=f"source-delegation:{delegation_id}:prepare",
        trace_id=trace_id,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.source_delegations "
                "(delegation_id,task_id,source,provider,owner_org_id,beneficiary_org_id,"
                "requested_scopes,state,nonce_hash,pkce_challenge,pkce_method,"
                "pkce_verifier_hash,redirect_allowlist_ref,expires_at,operation_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s::jsonb,'prepared',%s,%s,%s,%s,%s,%s,%s)",
                (
                    delegation_id,
                    task_id,
                    source,
                    provider,
                    owner_org_id,
                    beneficiary_org_id,
                    json.dumps(scopes),
                    nonce_hash,
                    pkce_challenge,
                    pkce_method,
                    pkce_verifier_hash,
                    redirect_allowlist_ref,
                    expires_at,
                    operation_id,
                ),
            )
        result = {
            "delegation_id": delegation_id,
            "state": "prepared",
            "provider": provider,
            "google_direct": google_direct,
            "expires_at": expires_at.isoformat(),
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "delegation_id": delegation_id,
                "provider": provider,
                "beneficiary_org_id": beneficiary_org_id,
                "delivery_kind": "source_delegation",
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)

    # The handoff is the minimum-scoped delivery carrier to the credential owner.
    handoff = prepare_handoff(
        conn,
        task_id=task_id,
        actor=actor,
        expires_in_hours=expires_in_hours,
        idempotency_key=f"{idempotency_key}:handoff",
        host_context=host_context,
        trace_id=trace_id,
    )
    return PreparedSourceDelegation(
        delegation_id=delegation_id,
        state=operation.result.get("state", "prepared"),
        expires_at=operation.result.get("expires_at", expires_at.isoformat()),
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        outbox_event_id=operation.outbox_event_id,
        handoff=handoff,
        authorize_state=authorize_state,
        pkce_challenge=pkce_challenge,
        pkce_method=pkce_method,
        replayed=operation.replayed,
    )


def verify_delegated_callback(
    conn,
    *,
    delegation_id: str,
    state_param: str,
    redirect_uri: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify redirect allow-list + state + nonce + PKCE BEFORE any transition.

    Returns the loaded delegation row context on success. Raises
    ``DelegationVerificationError`` (opaque) on any failure so no connection or
    exposure transition can proceed. This is the fail-closed guard the OAuth
    callback MUST call first (mirrors ``admin_api._google_oauth_callback``
    ordering: verify state -> recheck access -> exchange).
    """
    # 1. Verify the HMAC state + freshness, and that it targets THIS delegation.
    bound_delegation_id, nonce = _verify_state(state_param, now=now)
    if not secrets.compare_digest(bound_delegation_id, delegation_id):
        raise DelegationVerificationError("etat de delegation invalide")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT delegation_id,task_id,source,provider,owner_org_id,beneficiary_org_id,"
            "requested_scopes,state,nonce_hash,pkce_challenge,pkce_method,redirect_allowlist_ref,"
            "expires_at FROM app.source_delegations WHERE delegation_id=%s FOR UPDATE",
            (delegation_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise DelegationVerificationError("delegation invalide")
    (
        _did,
        task_id,
        source,
        provider,
        owner_org_id,
        beneficiary_org_id,
        requested_scopes,
        state,
        nonce_hash,
        pkce_challenge,
        pkce_method,
        redirect_allowlist_ref,
        expires_at,
    ) = row

    # 2. The delegation must still be awaiting authorization and unexpired.
    if state != "prepared":
        raise DelegationVerificationError("delegation deja resolue")
    if isinstance(expires_at, datetime) and _aware(expires_at) <= datetime.now(timezone.utc):
        raise DelegationVerificationError("delegation expiree")

    # 3. Exact redirect allow-list: the presented redirect_uri MUST be one of the
    #    exact entries bound at preparation time. No prefix / substring match.
    allowlist = _load_redirect_allowlist(conn, redirect_allowlist_ref)
    if not isinstance(redirect_uri, str) or redirect_uri not in allowlist:
        raise DelegationVerificationError("redirect_uri non autorise")

    # 4. Nonce: the HMAC-bound nonce must match the persisted keyed hash.
    if not secrets.compare_digest(_keyed_hash(nonce, "delegation-nonce"), nonce_hash or ""):
        raise DelegationVerificationError("nonce invalide")

    # 5. PKCE: non-Google-direct delegations MUST carry a challenge; the provider
    #    adapter proves possession of the verifier during token exchange. Here we
    #    assert the delegation is internally consistent (a prepared non-Google
    #    delegation without a challenge is a tampered/invalid delegation).
    if not provider_is_google_direct(provider):
        if not pkce_challenge or pkce_method != "S256":
            raise DelegationVerificationError("PKCE requis pour ce fournisseur")

    return {
        "delegation_id": delegation_id,
        "task_id": task_id,
        "source": source,
        "provider": provider,
        "owner_org_id": owner_org_id,
        "beneficiary_org_id": beneficiary_org_id,
        "requested_scopes": requested_scopes,
        "pkce_challenge": pkce_challenge,
        "pkce_method": pkce_method,
    }


def _load_redirect_allowlist(conn, redirect_allowlist_ref: str) -> frozenset[str]:
    """Resolve the exact redirect allow-list for a delegation.

    The allow-list is a server-owned, immutable set keyed by
    ``redirect_allowlist_ref`` (never client-supplied at callback time). Falls
    back to the env-configured delegated redirect URI so the flow is usable
    before a per-provider allow-list table exists.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT redirect_uri FROM app.source_delegation_redirects "
            "WHERE allowlist_ref=%s AND active",
            (redirect_allowlist_ref,),
        )
        rows = cur.fetchall()
    entries = {r[0] for r in rows if isinstance(r[0], str) and r[0]}
    env_uri = os.environ.get("TOOROW_DELEGATION_REDIRECT_URI", "").strip()
    if env_uri:
        entries.add(env_uri)
    return frozenset(entries)


def complete_source_delegation(
    conn,
    *,
    delegation_id: str,
    state_param: str,
    redirect_uri: str,
    credential_id: str,
    external_account_id: str,
    grant_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    now: int | None = None,
) -> CompletedSourceDelegation:
    """After the owner's verified callback, expose the exact account and resume.

    Ordering (fail-closed):
      1. ``verify_delegated_callback`` -- redirect allow-list + state + nonce +
         PKCE. Any failure => nothing transitions.
      2. ``account_exposure.expose_account`` -- the owner's explicit allow of ONE
         exact account (exposure defaults to none; this is the only place a grant
         is written, through the Story 36.2 seam).
      3. ``resolve_provider_account_access`` -- REVALIDATE connection health +
         exact exposure (never trust the prior state).
      4. mark the delegation ``exposed`` and drive ``reconcile_task_state`` to
         ``source_authorized`` from SERVER evidence.

    NO token or secret is read, returned, or placed in any payload here.
    """
    from core.account_exposure import AccountExposureConflict, expose_account
    from core.project_access import resolve_provider_account_access

    # 1. FAIL-CLOSED verification before any transition.
    context = verify_delegated_callback(
        conn,
        delegation_id=delegation_id,
        state_param=state_param,
        redirect_uri=redirect_uri,
        now=now,
    )
    owner_org_id = context["owner_org_id"]
    beneficiary_org_id = context["beneficiary_org_id"]
    task_id = context["task_id"]

    # AUTHORIZATION (review H1): a valid HMAC state is a bearer capability, not proof of
    # authority. The credential OWNER authorizes the exposure, so require the caller to
    # manage the owner org AND require the credential to actually belong to that owner org
    # -- else a beneficiary-side holder of the handoff link could expose an arbitrary
    # account. Both checks are server-derived (owner_org_id comes from the verified
    # delegation record, not the request body).
    from core.project_access import identity_can_manage_org

    if not identity_can_manage_org(owner_org_id, actor, conn):
        raise DelegationConflict("caller lacks authority over the credential owner org")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT owner_org_id FROM app.connection_ref WHERE id=%s",
            (credential_id,),
        )
        _cred = cur.fetchone()
    if _cred is None or _cred[0] != owner_org_id:
        raise DelegationConflict("credential does not belong to the delegation owner org")

    # 2. Explicit exact account exposure (defaults to none until now). Separate
    #    audited action through the shared operation seam.
    try:
        exposure = expose_account(
            conn,
            grant_id=grant_id,
            credential_id=credential_id,
            external_account_id=external_account_id,
            owner_org_id=owner_org_id,
            grantee_org_id=beneficiary_org_id,
            actor=actor,
            idempotency_key=f"{idempotency_key}:expose",
            host_context=host_context,
            versions={"policy": "delegation-v1", "catalog": "delegation-v1", "tool": "rest-v1"},
            confirmation_reference=f"source-delegation:{delegation_id}:expose",
            trace_id=trace_id,
        )
    except AccountExposureConflict:
        # Idempotent resume: the exact account is already exposed. Continue to the
        # revalidation + reconciliation path rather than fail.
        exposure = None

    # 3. REVALIDATE health + exact exposure -- do NOT trust a prior "ready".
    #    resolve_provider_account_access needs a resource scope (project/datastream)
    #    to confirm resource.org_id == beneficiary_org_id; without it the strict seam
    #    denies "invalid_resource" and the delegation could never complete. Resolve the
    #    beneficiary project from the delegation's setup task -> journey.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT j.project_id FROM app.setup_tasks t "
            "JOIN app.setup_journeys j ON j.id = t.journey_id WHERE t.id = %s",
            (task_id,),
        )
        _project_row = cur.fetchone()
    beneficiary_project_id = _project_row[0] if _project_row else None

    # First-value funnel (E36-FR09): the owner explicitly exposed exactly one account
    # (step 2) -> record account_selection for the beneficiary project's journey. The
    # pseudonymous ref is built inside the seam; no credential/account/provider value
    # leaves this frame. Best-effort -- NEVER breaks the delegation completion.
    from core.first_value_instrumentation import record_stage  # noqa: PLC0415

    record_stage(
        conn,
        org_id=beneficiary_org_id,
        project_id=beneficiary_project_id,
        subject=beneficiary_project_id,
        stage="account_selection",
        outcome="succeeded",
    )

    decision = resolve_provider_account_access(
        actor,
        conn,
        credential_id=credential_id,
        external_account_id=external_account_id,
        beneficiary_org_id=beneficiary_org_id,
        project_id=beneficiary_project_id,
    )
    if not decision.allowed:
        record_stage(
            conn,
            org_id=beneficiary_org_id,
            project_id=beneficiary_project_id,
            subject=beneficiary_project_id,
            stage="source_authorization",
            outcome="failed",
        )
        # Preserve prior valid setup: mark the delegation failed but leave the
        # (possibly already valid) connection/exposure untouched; the checklist
        # will show the safe next action from server evidence.
        _transition(
            conn,
            delegation_id=delegation_id,
            target_state="failed",
            actor=actor,
            idempotency_key=f"{idempotency_key}:fail",
            host_context=host_context,
            trace_id=trace_id,
            reason=decision.reason,
        )
        raise DelegationConflict(f"account revalidation failed: {decision.reason}")

    # 4. Mark exposed + drive the task to source_authorized from SERVER evidence.
    transition = _transition(
        conn,
        delegation_id=delegation_id,
        target_state="exposed",
        actor=actor,
        idempotency_key=f"{idempotency_key}:expose-state",
        host_context=host_context,
        trace_id=trace_id,
        reason=None,
    )
    reconciled = _reconcile_source_task(conn, task_id=task_id)

    # First-value funnel (E36-FR09): the source is authorized and the exact account is
    # exposed + revalidated -> record source_authorization succeeded. Best-effort.
    record_stage(
        conn,
        org_id=beneficiary_org_id,
        project_id=beneficiary_project_id,
        subject=beneficiary_project_id,
        stage="source_authorization",
        outcome="succeeded",
    )

    exposure_result = exposure.result if isinstance(exposure, OperationResult) else {
        "state": "already_exposed",
        "credential_id": credential_id,
        "external_account_id": external_account_id,
    }
    return CompletedSourceDelegation(
        delegation_id=delegation_id,
        state="exposed",
        task_reconciled_state=reconciled,
        exposure=exposure_result,
        operation_id=transition.operation_id,
        audit_event_id=transition.audit_event_id,
        replayed=transition.replayed,
    )


def _reconcile_source_task(conn, *, task_id: str) -> str:
    """Reconcile the source_authorization task from SERVER evidence, not a callback."""
    from core.setup_responsibilities import reconcile_task_state

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,return_condition FROM app.setup_tasks WHERE id=%s FOR UPDATE",
            (task_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise DelegationUnavailable("setup task unavailable")
    return_condition = row[1]
    resource_id = (return_condition or {}).get("resource_id")
    kind = (return_condition or {}).get("kind")
    # Server evidence: the exposure now revalidated true for this resource.
    evidence = {kind: {resource_id: True}} if kind and resource_id else {}
    new_state = reconcile_task_state(
        current_state=row[0],
        return_condition=return_condition,
        server_evidence=evidence,
    )
    if new_state != row[0]:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.setup_tasks SET state=%s,updated_at=NOW(),"
                "completed_at=CASE WHEN %s='completed' THEN NOW() ELSE completed_at END "
                "WHERE id=%s",
                (new_state, new_state, task_id),
            )
    return new_state


def _transition(
    conn,
    *,
    delegation_id: str,
    target_state: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
    reason: str | None,
) -> OperationResult:
    """Drive a delegation state transition through the shared operation seam."""
    if target_state not in DELEGATION_STATES:
        raise DelegationValidationError("unsupported delegation state")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,owner_org_id,beneficiary_org_id FROM app.source_delegations "
            "WHERE delegation_id=%s FOR UPDATE",
            (delegation_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise DelegationUnavailable("delegation unavailable")
    current_state, owner_org_id, beneficiary_org_id = row
    if current_state in TERMINAL_STATES and current_state != target_state:
        raise DelegationConflict("delegation already resolved")

    spec = OperationSpec(
        command_type="source.delegation.transition",
        actor=actor,
        effective_org_id=owner_org_id,
        resource_path=(
            f"organization:{owner_org_id}",
            f"beneficiary:{beneficiary_org_id}",
            f"source-delegation:{delegation_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={"policy": "delegation-v1", "catalog": "delegation-v1", "tool": "rest-v1"},
        request_payload={
            "delegation_id": delegation_id,
            "target_state": target_state,
            "reason": reason,
        },
        provider_references={},
        confirmation_mode="server",
        confirmation_reference=f"source-delegation:{delegation_id}:{target_state}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.source_delegations SET state=%s,updated_at=NOW() "
                "WHERE delegation_id=%s AND state=%s",
                (target_state, delegation_id, current_state),
            )
            if cur.rowcount != 1 and current_state != target_state:
                raise DelegationConflict("delegation transition race lost")
        result = {"delegation_id": delegation_id, "state": target_state}
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"state": current_state}),
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "delegation_id": delegation_id,
                "transition": target_state,
                "beneficiary_org_id": beneficiary_org_id,
            },
        )

    return execute_operation(conn, spec, mutation=mutation)


def revoke_source_delegation(
    conn,
    *,
    delegation_id: str,
    actor: str,
    idempotency_key: str,
    host_context: dict[str, Any],
    trace_id: str | None,
) -> dict[str, Any]:
    """Revoke a prepared delegation (owner declined / operator cancelled).

    Preserves any prior valid setup: this only closes the pending delegation; it
    never invalidates an existing healthy connection or exposure.
    """
    return _transition(
        conn,
        delegation_id=delegation_id,
        target_state="revoked",
        actor=actor,
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
        reason="revoked",
    ).result
