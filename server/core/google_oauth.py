"""toorow -- Google server-side OAuth 2.0 flow (Story 18.2, AD-21).

Builds the single multi-scope Google consent URL and exchanges the returned
authorization ``code`` for (access_token, refresh_token, expiry, scopes). The
persisted, encrypted token lands in ``app.connection_ref`` via the Story 18.1
store (``core.google_token_store.store_google_token``) -- this module NEVER
writes the blob itself (AD-8 single writer); it only produces the payload and
hands it to that store.

SECURITY INVARIANTS (the 18.5 review is adversarial on these -- any leak of a
token OR the authorization ``code`` = CRITICAL):
  * The authorization ``code`` and every token value NEVER appear in a log line,
    an exception message, a redirect URL, or an OTel trace. Google token-endpoint
    errors are REDACTED to the HTTP status + Google's short ``error`` code only
    (never the response body, which can echo the code).
  * The anti-CSRF ``state`` is an HMAC-signed, short-expiry token that BINDS the
    admin identity + project_id + target connection_ref_id. A tampered, expired,
    unknown, or mismatched state is rejected with NO oracle detail (the caller
    surfaces a generic French 4xx; this module raises a typed error whose message
    is safe to log but not to echo verbatim to an attacker).
  * No client_id / client_secret / redirect_uri is ever hard-coded -- all come
    from the environment (``GOOGLE_OAUTH_CLIENT_ID`` / ``_SECRET`` / ``_REDIRECT_URI``).

State design (see the story Dev Agent Record for the full rationale):
  * STATELESS HMAC token, NOT a server-stored nonce. Chosen so the flow needs no
    extra table and no cross-replica session store in Phase A: the state carries
    ``{project_id, connection_ref_id, identity, nonce, exp}`` and an HMAC-SHA256
    tag over that payload keyed by ``GOOGLE_OAUTH_STATE_SECRET``. Integrity +
    authenticity are proven by the tag (an attacker cannot forge or mutate it
    without the key); freshness by ``exp`` (short window, default 600s). The
    random ``nonce`` makes each state unique.
  * "Single use" is approximated by the short expiry window (Phase A). A true
    replay-cache (consumed-state table) is the Phase B hardening -- documented in
    the story BLOCKED section. Within the window the state is bound to one exact
    (project, connection, identity) triple, so a replay cannot cross tenants.

AI-53 -- scopes/endpoints VERIFIED against Google's official OAuth docs on
2026-07-18 (still "to confirm" against the real prod client, HG/AI-08):
  * authorize : https://accounts.google.com/o/oauth2/v2/auth
  * token     : https://oauth2.googleapis.com/token
  * revoke    : https://oauth2.googleapis.com/revoke   (used by 18.4, not here)
  * scopes    : analytics.readonly (GA4 Data API), webmasters.readonly (GSC),
                adwords (Google Ads), spreadsheets.readonly (Sheets).
  These exact names are pinned in ``GOOGLE_STACK_SCOPES`` below.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Google OAuth constants (AI-53 -- verified 2026-07-18; prod client HG/AI-08).
# ---------------------------------------------------------------------------

GOOGLE_AUTHORIZE_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"

# The full Google stack consent, granted in ONE screen (Story 18.2 vision).
# EXACT scope names pinned per AI-53. GA4 Data API + GSC + Ads + Sheets (read).
GOOGLE_STACK_SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/dfareporting",
    "https://www.googleapis.com/auth/display-video",
    "https://www.googleapis.com/auth/doubleclicksearch",
)

# openid/email are NOT requested: this flow authorizes DATA access, not identity
# (identity is the admin Bearer token -- AD-14). Documented non-goal in the epic.

_DEFAULT_TIMEOUT = httpx.Timeout(15.0, connect=5.0)

# Anti-CSRF state lifetime. Short by design (freshness stands in for a replay
# cache in Phase A -- see module docstring + story BLOCKED).
_STATE_TTL_SECONDS = 600

_STATE_SECRET_ENV = "GOOGLE_OAUTH_STATE_SECRET"


class GoogleOAuthError(Exception):
    """OAuth-flow failure with all token/code material redacted.

    Never carries the authorization ``code``, an access/refresh token, a client
    secret, or the raw Google response body. Its message is safe to log; callers
    map it to a generic French 4xx WITHOUT echoing it to the end user verbatim
    (no CSRF/oracle detail leaks to an attacker).
    """


class GoogleOAuthConfigError(GoogleOAuthError):
    """The OAuth client env config is missing/incomplete (client_id/secret/redirect).

    Also raised when GOOGLE_OAUTH_STATE_SECRET is absent in a production auth mode
    (TOOROW_AUTH_MODE in {'oauth', 'static'}) -- the dev fallback is not safe
    for production (F-1).
    """


class GoogleOAuthStateError(GoogleOAuthError):
    """The anti-CSRF ``state`` is absent, tampered, expired, or unknown.

    A single opaque type for every state-rejection reason: the caller returns one
    generic 4xx so an attacker cannot distinguish "expired" from "forged" from
    "mismatch" (no timing/oracle signal).
    """


# ---------------------------------------------------------------------------
# Env-driven client config (NEVER hard-coded -- AD-21 / NFR3).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GoogleOAuthClientConfig:
    """Resolved OAuth client config. ``client_secret`` is never logged/repr'd."""

    client_id: str
    client_secret: str
    redirect_uri: str

    def __repr__(self) -> str:  # secret-safe repr
        return (
            "GoogleOAuthClientConfig("
            f"client_id=<{len(self.client_id)} chars>, "
            "client_secret=<redacted>, "
            f"redirect_uri={self.redirect_uri!r})"
        )

    __str__ = __repr__


def load_client_config() -> GoogleOAuthClientConfig:
    """Read the OAuth client config from the environment.

    Raises:
        GoogleOAuthConfigError: if client_id / client_secret / redirect_uri is
            missing. The message names WHICH var is missing but never a value.
    """
    client_id = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "").strip()
    redirect_uri = os.environ.get("GOOGLE_OAUTH_REDIRECT_URI", "").strip()

    missing = [
        name
        for name, val in (
            ("GOOGLE_OAUTH_CLIENT_ID", client_id),
            ("GOOGLE_OAUTH_CLIENT_SECRET", client_secret),
            ("GOOGLE_OAUTH_REDIRECT_URI", redirect_uri),
        )
        if not val
    ]
    if missing:
        raise GoogleOAuthConfigError(
            "configuration OAuth Google incomplete (variables manquantes): "
            + ", ".join(missing)
        )
    return GoogleOAuthClientConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
    )


# ---------------------------------------------------------------------------
# Anti-CSRF state (HMAC-signed, short expiry, identity/project/connection bound).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OAuthState:
    """The decoded, verified payload carried by a valid ``state``."""

    project_id: str
    connection_ref_id: str
    identity: str
    nonce: str
    exp: int  # unix seconds


def _state_secret() -> bytes:
    """Return the HMAC key for state signing.

    F-1 (HIGH): en mode de production (TOOROW_AUTH_MODE in {'oauth','static'}),
    GOOGLE_OAUTH_STATE_SECRET DOIT etre positionne. L'absence de cette variable en
    prod est une erreur de configuration critique : lever GoogleOAuthConfigError
    plutot que derive silencieusement d'une cle moins sure.

    En mode dev/disabled, le fallback derive d'API_SECRET_KEY (avec WARNING) ou
    une cle ephemere process-locale est accepte (WARNING).
    """
    secret = os.environ.get(_STATE_SECRET_ENV, "").strip()
    if secret:
        return secret.encode("utf-8")

    # F-1: hard-fail en mode de production -- le fallback derive/ephemere n'est
    # pas acceptable en prod (risque de cle predictible ou ephemere cross-restart).
    auth_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if auth_mode in ("oauth", "static"):
        raise GoogleOAuthConfigError(
            f"configuration OAuth Google incomplete : la variable {_STATE_SECRET_ENV} "
            "est obligatoire en mode de production (TOOROW_AUTH_MODE=oauth/static). "
            "Positionnez cette variable dans Secret Manager avant de demarrer le serveur."
        )

    api_secret = os.environ.get("API_SECRET_KEY", "").strip()
    if api_secret:
        logger.warning(
            "google_oauth: %s unset -- deriving state key from API_SECRET_KEY "
            "(dev fallback; set %s in production)",
            _STATE_SECRET_ENV,
            _STATE_SECRET_ENV,
        )
        return hashlib.sha256(b"google-oauth-state|" + api_secret.encode("utf-8")).digest()
    # Last-resort dev fallback -- unusable across restarts (ephemeral), which is
    # exactly what we want: no silent weak static key in prod.
    logger.warning(
        "google_oauth: neither %s nor API_SECRET_KEY set -- using an ephemeral "
        "process-local state key (dev only)",
        _STATE_SECRET_ENV,
    )
    return _EPHEMERAL_DEV_SECRET


# Generated once per process; makes the no-config dev path non-static without
# ever shipping a hard-coded key.
_EPHEMERAL_DEV_SECRET = secrets.token_bytes(32)


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def mint_state(
    project_id: str,
    connection_ref_id: str,
    identity: str,
    ttl_seconds: int = _STATE_TTL_SECONDS,
    now: int | None = None,
) -> str:
    """Mint an HMAC-signed anti-CSRF state binding project/connection/identity.

    The returned token is ``<b64url(payload)>.<b64url(hmac)>``. It is opaque to
    Google (round-tripped as the ``state`` query param) and only THIS server can
    verify it (the HMAC key never leaves the server).

    Args:
        project_id:        target project (scopes the token store write).
        connection_ref_id: the ``conn_`` row that will receive the token.
        identity:          the authenticated admin subject (AD-14) -- audited.
        ttl_seconds:       validity window (short by design).
        now:               injectable clock for tests (unix seconds).
    """
    issued = int(time.time()) if now is None else now
    payload = {
        "project_id": project_id,
        "connection_ref_id": connection_ref_id,
        "identity": identity,
        "nonce": secrets.token_urlsafe(16),
        "exp": issued + int(ttl_seconds),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    tag = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).digest()
    return f"{_b64url_encode(payload_bytes)}.{_b64url_encode(tag)}"


def verify_state(state: str, now: int | None = None) -> OAuthState:
    """Verify an anti-CSRF state and return its bound payload.

    Rejects (all as ``GoogleOAuthStateError``, no distinguishing detail):
      * absent / malformed token,
      * bad HMAC tag (forged or tampered),
      * expired token (past ``exp``).

    Args:
        state: the ``state`` query param returned by Google.
        now:   injectable clock for tests (unix seconds).
    """
    if not state or "." not in state:
        raise GoogleOAuthStateError("etat OAuth invalide")
    try:
        payload_b64, tag_b64 = state.split(".", 1)
        payload_bytes = _b64url_decode(payload_b64)
        presented_tag = _b64url_decode(tag_b64)
    except Exception:
        # Do NOT echo the offending state back (no oracle).
        raise GoogleOAuthStateError("etat OAuth invalide") from None

    expected_tag = hmac.new(_state_secret(), payload_bytes, hashlib.sha256).digest()
    # Constant-time compare -- no timing oracle on the tag.
    if not hmac.compare_digest(presented_tag, expected_tag):
        raise GoogleOAuthStateError("etat OAuth invalide")

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        raise GoogleOAuthStateError("etat OAuth invalide") from None

    exp = int(payload.get("exp", 0))
    current = int(time.time()) if now is None else now
    if current >= exp:
        raise GoogleOAuthStateError("etat OAuth expire")

    project_id = str(payload.get("project_id", ""))
    connection_ref_id = str(payload.get("connection_ref_id", ""))
    identity = str(payload.get("identity", ""))

    # F-7: rejeter les champs obligatoires vides apres decodage.
    # Un state minte artificiellement avec un champ vide est invalide : un attaquant
    # pourrait exploiter une valeur vide pour contourner des verifications aval.
    if not project_id or not connection_ref_id or not identity:
        raise GoogleOAuthStateError("etat OAuth invalide")

    return OAuthState(
        project_id=project_id,
        connection_ref_id=connection_ref_id,
        identity=identity,
        nonce=str(payload.get("nonce", "")),
        exp=exp,
    )


# ---------------------------------------------------------------------------
# Authorization URL construction.
# ---------------------------------------------------------------------------


def build_authorize_url(
    project_id: str,
    connection_ref_id: str,
    identity: str,
    scopes: tuple[str, ...] | list[str] = GOOGLE_STACK_SCOPES,
) -> str:
    """Build the single multi-scope Google consent URL.

    Uses Authorization Code + ``access_type=offline`` + ``prompt=consent`` (to
    force a refresh_token every time -- see the refresh-token handling in
    ``exchange_code``) + ``include_granted_scopes=true`` (incremental auth).

    Args:
        project_id / connection_ref_id / identity: bound into the ``state``.
        scopes: the stack scopes (default: the full pinned set).

    Returns:
        The absolute authorize URL to redirect the admin's browser to.

    Raises:
        GoogleOAuthConfigError: if the client env config is missing.
    """
    cfg = load_client_config()
    state = mint_state(project_id, connection_ref_id, identity)
    params = {
        "client_id": cfg.client_id,
        "redirect_uri": cfg.redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    url = f"{GOOGLE_AUTHORIZE_ENDPOINT}?{urlencode(params)}"
    # Log the flow start with ids only -- never the client_secret (absent here
    # anyway) and never the state's HMAC.
    logger.info(
        "google_oauth: authorize_url_built project=%s connection=%s scopes=%d",
        project_id,
        connection_ref_id,
        len(scopes),
    )
    return url


# ---------------------------------------------------------------------------
# Code -> token exchange.
# ---------------------------------------------------------------------------


@dataclass
class GoogleTokenResponse:
    """The parsed token-endpoint result. Secret fields never appear in repr."""

    access_token: str
    refresh_token: str
    token_expiry: datetime | None
    granted_scopes: list[str]
    token_type: str = "Bearer"

    def __repr__(self) -> str:  # secret-safe repr
        return (
            "GoogleTokenResponse("
            f"access_token=<{len(self.access_token)} chars, redacted>, "
            f"refresh_token=<{len(self.refresh_token)} chars, redacted>, "
            f"token_expiry={self.token_expiry!r}, "
            f"granted_scopes={self.granted_scopes!r}, "
            f"token_type={self.token_type!r})"
        )

    __str__ = __repr__


def _redact_google_error(resp: httpx.Response) -> str:
    """Extract Google's short ``error`` code WITHOUT echoing the body.

    Google's error body can echo request params (including the code); we only
    surface the HTTP status + the short machine ``error`` field (e.g.
    ``invalid_grant``). Never the ``error_description`` (may be verbose) and
    never the raw body.
    """
    code = "unknown"
    try:
        data = resp.json()
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            code = data["error"]
    except Exception:
        code = "unparseable"
    return f"HTTP {resp.status_code} ({code})"


async def _exchange_code_async(code: str, cfg: GoogleOAuthClientConfig) -> dict:
    """POST the code to Google's token endpoint. Returns the parsed JSON dict.

    Raises:
        GoogleOAuthError: on transport error or non-2xx (message redacted).
    """
    form = {
        "code": code,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
        "redirect_uri": cfg.redirect_uri,
        "grant_type": "authorization_code",
    }
    try:
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
            resp = await client.post(GOOGLE_TOKEN_ENDPOINT, data=form)
    except httpx.HTTPError as exc:
        # Transport failure -- type name only, never the form (carries the code).
        raise GoogleOAuthError(
            f"echange de code impossible (redacted): {type(exc).__name__}"
        ) from None

    if resp.status_code >= 400:
        # Redacted: status + Google's short error code only, NEVER the body.
        raise GoogleOAuthError(
            f"echec de l'echange de code cote Google (redacted): {_redact_google_error(resp)}"
        )
    try:
        data = resp.json()
    except Exception:
        raise GoogleOAuthError(
            "reponse Google inattendue (redacted): corps non-JSON"
        ) from None
    if not isinstance(data, dict):
        raise GoogleOAuthError("reponse Google inattendue (redacted)")
    return data


def _parse_token_response(data: dict) -> GoogleTokenResponse:
    """Turn the token-endpoint JSON into a typed response.

    Enforces the refresh_token presence contract: if Google omits it (consent
    already granted WITHOUT ``prompt=consent``), we refuse to persist a partial
    connection and raise an explicit French re-consent error rather than store an
    access-only token that cannot be refreshed (AD-9 -- honest state).
    """
    access_token = data.get("access_token") or ""
    refresh_token = data.get("refresh_token") or ""
    if not access_token:
        raise GoogleOAuthError("reponse Google sans access_token (redacted)")
    if not refresh_token:
        # prompt=consent should force this; if it is still missing the user must
        # re-consent (or revoke prior grant at myaccount.google.com). Honest,
        # actionable French message -- never a masked "connected".
        raise GoogleOAuthError(
            "Google n'a pas renvoye de refresh_token : le consentement doit etre "
            "reaccorde. Reconnectez le compte Google (un nouvel ecran de "
            "consentement est requis)."
        )

    expiry: datetime | None = None
    expires_in = data.get("expires_in")
    if isinstance(expires_in, (int, float)) and expires_in > 0:
        expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=int(expires_in))

    scope_str = data.get("scope") or ""
    granted_scopes = [s for s in scope_str.split(" ") if s]

    return GoogleTokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_expiry=expiry,
        granted_scopes=granted_scopes,
        token_type=str(data.get("token_type") or "Bearer"),
    )


async def exchange_code(code: str) -> GoogleTokenResponse:
    """Exchange an authorization ``code`` for tokens (async).

    The ``code`` and the resulting tokens are NEVER logged, put in an exception,
    or returned in a redirect URL. On any Google error the message is redacted to
    status + short error code.

    Raises:
        GoogleOAuthConfigError: missing client config.
        GoogleOAuthError: transport/HTTP/parse failure or missing refresh_token
            (message redacted).
    """
    if not code:
        raise GoogleOAuthError("code d'autorisation absent (redacted)")
    cfg = load_client_config()
    data = await _exchange_code_async(code, cfg)
    parsed = _parse_token_response(data)
    logger.info(
        "google_oauth: code_exchanged scopes=%d expiry=%s",
        len(parsed.granted_scopes),
        parsed.token_expiry.isoformat() if parsed.token_expiry else "none",
    )
    return parsed
