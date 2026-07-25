"""toorow -- Provider-aware token service (Story 18.3, AD-21 / AD-2).

The Google-direct half of the ``get_fresh_token`` facade. ``nango_client.get_fresh_token``
routes on the connection's ``auth_path`` (a generic column -- NOT a hard-coded provider
name, so the core stays source-agnostic per AD-2): rows with ``auth_path='google_direct'``
are served HERE; every other row keeps the unchanged Nango path.

WHAT THIS MODULE DOES (AC2 / AC3 of the story):
  * Resolve the connection_ref row from the identifier a connector passes
    (``nango_connection_id`` -- the same value connectors have always passed to
    ``get_fresh_token``; the signature is UNCHANGED, AD-2). From it we read the
    ``conn_`` id, ``project_id`` and local ``token_expiry``.
  * Load + decrypt the token blob via the 18.1 store (the ONLY sanctioned reader).
  * If the access token is expired or within the refresh skew, mint a fresh one via
    the Google OAuth ``refresh_token`` grant (18.2 endpoint, same zero-leak rules),
    re-encrypt and persist it (updating ``token_expiry``), and write a refresh audit
    row (AD-14 -- the emission/refresh audit that 18.1 delegated to us, BLOCKED-18.3).
  * Return the fresh access token string DIRECTLY to the connector -- never logged,
    never persisted by the connector (AD-3 stays true on the module side).
  * A revoked/invalid ``refresh_token`` (Google ``invalid_grant``) surfaces as the
    reserved ``auth_expired`` code so the shell renders a reconnect affordance (AD-15).

IDENTITY / AD-5 (BLOCKED-18.3 from the 18.1 F-1 review):
  The 18.1 store documents that ``identity_has_project_access`` enforcement is the
  CALLER's duty on any tenant-exposed path. Scheduled pulls are a SYSTEM context, not
  a tenant-exposed one: the queue worker resolved the project itself and runs under a
  ``system`` identity -- there is no inbound tenant identity to check here. So this
  service performs the load/refresh under ``performed_by='system'`` and passes
  ``expected_project_id`` (defense in depth: the store rejects any cross-project id
  confusion). ANY FUTURE tenant-exposed entry point (e.g. an interactive
  "test this connection" button in the console) MUST call
  ``get_fresh_google_token(..., identity=<real subject>)`` AND gate it with
  ``identity_has_project_access`` BEFORE reaching this service. That guard is wired as
  an explicit, documented parameter below so the future path cannot forget it.

SECURITY INVARIANTS (18.5 is adversarial -- any plaintext token leak = CRITICAL):
  * The decrypted/refreshed token is returned as the function value only. It is never
    logged, never put in an exception message, never in a fixture.
  * Every Google refresh error is redacted to the HTTP status + Google's short error
    code (never the response body, which can echo secrets) -- reusing 18.2's
    ``_redact_google_error``.
  * ``auth_expired`` (the reserved ``constants.AUTH_EXPIRED_CODE``) is raised as a
    typed ``GoogleAuthExpired`` on a revoked/invalid refresh_token, NOT a masked
    "connected" (AD-9 honest state).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from core.constants import AUTH_EXPIRED_CODE

logger = logging.getLogger(__name__)

# Refresh the access token when it is within this window of expiring (or already
# expired). Google access tokens live ~3600s; a generous skew avoids a race where a
# token that passed the check expires mid-request.
_REFRESH_SKEW_SECONDS = 300

# The auth_path value that routes a connection to THIS service (generic column value,
# not a provider name -- AD-2 keeps the core source-agnostic).
AUTH_PATH_GOOGLE_DIRECT = "google_direct"
AUTH_PATH_NANGO = "nango"


class GoogleAuthExpired(Exception):
    """The Google refresh_token is invalid/revoked -- an honest re-consent is required.

    Carries the reserved ``auth_expired`` code (``constants.AUTH_EXPIRED_CODE``) so the
    shell renders a reconnect affordance (AD-15). NEVER carries any token material.
    """

    code = AUTH_EXPIRED_CODE

    def __init__(self, message: str = "Google refresh token invalide -- reconnexion requise"):
        super().__init__(message)


@dataclass(frozen=True)
class ResolvedConnection:
    """The minimal connection_ref facts the router needs. No secret here.

    ``has_token_blob`` is a boolean presence flag only -- NOT the blob itself (which
    stays sealed in the store). It lets health distinguish a purged connection (no
    blob -> revoked) from a stored token whose expiry is unknown (blob -> stale).
    """

    connection_ref_id: str  # the conn_ id (store key)
    project_id: str
    auth_path: str
    token_expiry: datetime | None
    has_token_blob: bool = False


# ---------------------------------------------------------------------------
# Routing lookup (by the identifier connectors pass -- nango_connection_id).
# ---------------------------------------------------------------------------


def resolve_connection_by_nango_id(nango_connection_id: str) -> ResolvedConnection | None:
    """Resolve a connection_ref row from the identifier connectors pass.

    Connectors call ``get_fresh_token(connection_id, provider)`` with the
    ``nango_connection_id`` (see queue.py: pull_fn(connection_id=nango_connection_id)).
    We look the row up by that column to read its ``auth_path`` (the routing key),
    ``id`` (the store key) and ``project_id`` (the tenant-key scope).

    Returns None when there is no matching row OR the DB is unreachable -- the caller
    then falls back to the unchanged Nango path (backward compat / non-regression).
    """
    try:
        from core.db import get_connection  # local import: avoid import cycle

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, project_id, auth_path, token_expiry,
                           (encrypted_token_blob IS NOT NULL) AS has_blob
                      FROM app.connection_ref
                     WHERE nango_connection_id = %s
                     LIMIT 1
                    """,
                    (nango_connection_id,),
                )
                row = cur.fetchone()
    except Exception:
        # DB unreachable / column absent (pre-029) -- fall back to Nango silently.
        logger.debug(
            "token_service: connection_ref lookup unavailable -- defaulting to nango"
        )
        return None

    if row is None:
        return None
    return ResolvedConnection(
        connection_ref_id=row[0],
        project_id=row[1],
        auth_path=row[2] or AUTH_PATH_NANGO,
        token_expiry=row[3],
        has_token_blob=bool(row[4]),
    )


def _is_expired_or_near(expiry: datetime | None, now: datetime | None = None) -> bool:
    """True when the access token is missing an expiry, expired, or within the skew."""
    if expiry is None:
        # Unknown expiry -- refresh to be safe (honest: never hand out a token we
        # cannot vouch for).
        return True
    now = now or datetime.now(tz=timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return now >= (expiry - timedelta(seconds=_REFRESH_SKEW_SECONDS))


# ---------------------------------------------------------------------------
# The google_direct token path.
# ---------------------------------------------------------------------------


def get_fresh_google_token(
    resolved: ResolvedConnection,
    identity: str = "system",
    _now: datetime | None = None,
) -> str:
    """Return a fresh Google access token for a ``google_direct`` connection.

    Loads + decrypts the stored token (18.1). If it is expired or within the refresh
    skew, mints a new access token via the ``refresh_token`` grant (18.2), re-encrypts
    and persists it, and writes a refresh audit row (AD-14). Returns the access token
    string directly -- NEVER logged or persisted by the connector (AD-3).

    Args:
        resolved: the routing facts from ``resolve_connection_by_nango_id``.
        identity: the audit subject. Defaults to ``'system'`` for scheduled pulls (a
            system context, not tenant-exposed). A tenant-exposed caller MUST pass the
            real human subject AND have gated the call with ``identity_has_project_access``
            beforehand (AD-5 -- see module docstring).

    Raises:
        GoogleAuthExpired: the refresh_token is revoked/invalid (``auth_expired``).
        core.google_token_store.GoogleTokenStoreError: load/store failure (redacted).
        core.google_oauth.GoogleOAuthError: transport/config failure (redacted).
    """
    from core.google_token_store import GoogleToken, load_google_token, store_google_token

    # AD-5 defense in depth: pass expected_project_id so the store hard-fails on any
    # cross-project id confusion (the identity check itself is the caller's duty).
    token: GoogleToken = load_google_token(
        resolved.connection_ref_id,
        expected_project_id=resolved.project_id,
    )

    if not _is_expired_or_near(token.token_expiry, now=_now):
        # Still valid -- hand back the stored access token as-is (no refresh needed).
        return token.access_token

    # Expired / near expiry -- refresh via the Google refresh_token grant.
    refreshed = _refresh_google_access_token(token.refresh_token)

    # Google may or may not return a new refresh_token on a refresh grant; keep the
    # existing one when absent (the original long-lived refresh_token stays valid).
    new_refresh = refreshed.refresh_token or token.refresh_token

    # Scope reconciliation (review-18-5 AD-9 / MEDIUM):
    # If Google returns a NON-EMPTY scope list on refresh, we must NEVER preserve
    # scopes that the new token no longer carries -- that would be dishonest (AD-9).
    # Strategy: use the INTERSECTION of the stored scopes and the refreshed scopes
    # when the refresh response includes scopes; otherwise keep the stored scopes
    # unchanged (refresh grants typically omit scope, meaning "same as before").
    refreshed_scopes = refreshed.granted_scopes  # list[str], may be []
    stored_scopes = token.granted_scopes or []
    if refreshed_scopes:
        # Google returned scopes -- trust only the intersection (AD-9).
        persisted_scopes: list[str] | None = [
            s for s in stored_scopes if s in refreshed_scopes
        ] or refreshed_scopes  # if nothing in common, use refreshed (honest)
        if persisted_scopes != stored_scopes:
            logger.info(
                "token_service: scope_reduced_on_refresh "
                "stored=%d refreshed=%d intersection=%d (AD-9: persisting intersection)",
                len(stored_scopes), len(refreshed_scopes), len(persisted_scopes),
            )
    else:
        # No scope list in refresh response -> Google means "unchanged"; keep stored.
        persisted_scopes = stored_scopes or None

    store_google_token(
        resolved.connection_ref_id,
        {
            "access_token": refreshed.access_token,
            "refresh_token": new_refresh,
            "metadata": token.metadata or {},
        },
        refreshed.token_expiry,
        persisted_scopes,
        expected_project_id=resolved.project_id,
    )

    # AD-14 refresh audit (the emission/refresh audit 18.1 delegated to 18.3). The
    # token is NEVER in the metadata -- only ids, scopes count and expiry.
    _audit_refresh(resolved, identity, refreshed.token_expiry)

    # AD-3: return directly -- never logged here.
    return refreshed.access_token


def _refresh_google_access_token(refresh_token: str):
    """Exchange a ``refresh_token`` for a fresh access token (Google token endpoint).

    Reuses the 18.2 OAuth module (same endpoint, same zero-leak redaction) but with the
    ``refresh_token`` grant type. A revoked/invalid refresh_token yields Google
    ``invalid_grant`` -> we raise ``GoogleAuthExpired`` (reserved ``auth_expired`` code)
    so the shell renders a reconnect affordance rather than a masked error (AD-9/AD-15).

    Returns a ``GoogleTokenResponse`` (18.2). The refresh_token field may be empty on a
    refresh grant (Google usually omits it) -- the caller keeps the existing one.
    """
    import httpx

    from core.google_oauth import (
        GOOGLE_TOKEN_ENDPOINT,
        GoogleOAuthError,
        GoogleTokenResponse,
        _redact_google_error,
        load_client_config,
    )

    if not refresh_token:
        # An empty stored refresh_token can never mint a token -- honest re-consent.
        raise GoogleAuthExpired()

    cfg = load_client_config()
    form = {
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    try:
        resp = httpx.post(GOOGLE_TOKEN_ENDPOINT, data=form, timeout=15.0)
    except httpx.HTTPError as exc:
        # Transport failure -- type name only, never the form (carries the refresh_token).
        raise GoogleOAuthError(
            f"rafraichissement du token impossible (redacted): {type(exc).__name__}"
        ) from None

    if resp.status_code >= 400:
        redacted = _redact_google_error(resp)
        # invalid_grant on a refresh grant = the refresh_token is revoked/expired ->
        # the user must re-consent (honest auth_expired, not a masked failure).
        if "invalid_grant" in redacted:
            raise GoogleAuthExpired()
        raise GoogleOAuthError(
            f"echec du rafraichissement du token cote Google (redacted): {redacted}"
        )

    try:
        data = resp.json()
    except Exception:
        raise GoogleOAuthError(
            "reponse Google inattendue au rafraichissement (redacted): corps non-JSON"
        ) from None
    if not isinstance(data, dict):
        raise GoogleOAuthError("reponse Google inattendue au rafraichissement (redacted)")

    access_token = data.get("access_token") or ""
    if not access_token:
        # No access token AND no error status is anomalous -- treat as re-consent.
        raise GoogleAuthExpired()

    expiry: datetime | None = None
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=int(expires_in))

    scope_str = data.get("scope") or ""
    granted_scopes = [s for s in scope_str.split(" ") if s]

    logger.info(
        "token_service: google_token_refreshed scopes=%d expiry=%s",
        len(granted_scopes),
        expiry.isoformat() if expiry else "none",
    )

    return GoogleTokenResponse(
        access_token=access_token,
        # Google typically omits refresh_token on a refresh grant; empty => caller keeps
        # the existing one.
        refresh_token=data.get("refresh_token") or "",
        token_expiry=expiry,
        granted_scopes=granted_scopes,
        token_type=str(data.get("token_type") or "Bearer"),
    )


def _audit_refresh(
    resolved: ResolvedConnection, identity: str, new_expiry: datetime | None
) -> None:
    """Write the AD-14 refresh audit row (best-effort -- never blocks the pull).

    The token is NEVER in the metadata. ``ACTION_CONNECTION_CREATED`` is reused as the
    emission/refresh action (18.1 delegated emission/refresh audit to 18.2/18.3).
    """
    try:
        from core.audit import ACTION_CONNECTION_CREATED, write_audit_row

        write_audit_row(
            identity=identity,
            action=ACTION_CONNECTION_CREATED,
            provider_account="google_direct",
            connection_ref=resolved.connection_ref_id,
            metadata={
                "event": "google_token_refreshed",
                "project_id": resolved.project_id,
                "auth_path": resolved.auth_path,
                "token_expiry": new_expiry.isoformat() if new_expiry else None,
            },
        )
    except Exception as exc:  # pragma: no cover -- audit never blocks the refresh
        logger.warning("token_service: refresh_audit_failed: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# Provider-aware health for google_direct connections (AC on health).
# ---------------------------------------------------------------------------


def google_direct_health(resolved: ResolvedConnection, now: datetime | None = None):
    """Compute health for a ``google_direct`` connection from the LOCAL expiry.

    No Nango polling: the health reads on ``token_expiry`` (and the presence of a
    stored blob). Returns a ``nango_client.ConnectionHealth`` so it slots into the
    existing poller/enrichment vocabulary (ok / stale / revoked -> the shell's
    green/amber/red + ``auth_expired``).

    Mapping (same vocabulary as Nango):
      * no stored token blob (purged / never issued) -> ``revoked`` (auth_expired).
      * blob present but expired past the refresh skew (or expiry unknown) -> ``stale``
        (a refresh is due; the poller stays green until a refresh actually fails).
      * otherwise -> ``ok``.

    A revoked refresh_token only becomes observable at refresh time (Google does not
    expose a cheap "is this refresh_token alive" probe); the poller reflects ``stale``
    while a refresh is due, and a failed refresh raises ``auth_expired`` at pull time.
    """
    from core.nango_client import ConnectionHealth

    now = now or datetime.now(tz=timezone.utc)

    # A purged connection has no blob -> reconnect required (revoked). Presence is a
    # boolean flag only (the blob itself never leaves the store).
    if not resolved.has_token_blob:
        return ConnectionHealth(status="revoked", last_fetched_at=None)

    if resolved.token_expiry is None:
        # Blob present but expiry unknown -> a refresh is due (amber), not revoked.
        return ConnectionHealth(status="stale", last_fetched_at=None)

    expiry = resolved.token_expiry
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)

    if now >= (expiry - timedelta(seconds=_REFRESH_SKEW_SECONDS)):
        # Access token is due for refresh -- amber (a refresh will run at next pull).
        return ConnectionHealth(status="stale", last_fetched_at=None)

    return ConnectionHealth(status="ok", last_fetched_at=None)
