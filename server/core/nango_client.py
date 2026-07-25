"""toorow -- Nango API client wrapper (Story 2.2, AC5).

Source-agnostic: no provider names appear here (AD-2).
All provider-specific configuration lives in Nango's integration config.

# AD-3 invariant: tokens are NEVER stored, logged, or cached here.
#   get_fresh_token() obtains a token from Nango on demand and returns it
#   directly to the caller. No file writes, no module-level cache, no DEBUG logs
#   that include token values.
#
# Provider-aware routing (Story 18.3, review-18-5):
#   get_fresh_token() first resolves the connection via token_service to check
#   auth_path. Connections with auth_path='google_direct' are served by
#   token_service.get_fresh_google_token (direct OAuth refresh, 18.2/18.3)
#   instead of the Nango endpoint. All other connections (auth_path='nango' or
#   unresolved) remain on the standard Nango path (backward compat).

# AD-2 invariant: this module is source-agnostic.
#   No reference to any specific provider name (CI-enforced).

HTTP library: httpx (async, preferred over requests for future async FastMCP
handlers). Sync wrappers are loop-safe (thread fallback); async callers
should use the *_async variants directly.
Decision recorded per Story 2.2 Dev Notes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Environment variable names
# ---------------------------------------------------------------------------

NANGO_BASE_URL_ENV = "NANGO_BASE_URL"
NANGO_SECRET_KEY_ENV = "NANGO_SECRET_KEY"

# Timeout for all Nango API calls (seconds)
_DEFAULT_TIMEOUT = 10.0

# A connection is considered stale if last_fetched_at is older than this (seconds)
_STALE_THRESHOLD_SECONDS = 24 * 3600


# ---------------------------------------------------------------------------
# Helpers: env-var reading (same pattern as warehouse.py — read at call time)
# ---------------------------------------------------------------------------


def _nango_base_url() -> str:
    return os.environ.get(NANGO_BASE_URL_ENV, "http://localhost:3003")


def _nango_secret_key() -> str:
    val = os.environ.get(NANGO_SECRET_KEY_ENV, "")
    if not val:
        raise EnvironmentError(f"{NANGO_SECRET_KEY_ENV} must be set to call Nango APIs")
    return val


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_nango_secret_key()}"}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


class NangoTokenError(Exception):
    """Raised when Nango cannot provide a valid access token.

    Distinct from generic HTTP errors so callers can handle token failures
    specifically (e.g. mark connection as revoked, surface auth UI to user).
    """


@dataclass
class BasicCredentials:
    """Basic-auth credentials + per-connection config resolved from Nango.

    For self-hosted providers that authenticate with a STATIC key/secret pair
    over HTTP Basic auth (no OAuth broker, no token to refresh) AND whose base
    URL differs per connection (e.g. a self-hosted store domain). The username /
    password come from Nango's Basic auth credential type; ``connection_config``
    carries the per-connection fields (e.g. the base URL) the connector needs to
    build its requests.

    AD-2: this type names no provider. AD-3: ``password`` is a secret — callers
    use it immediately and never log or persist it (this module never logs it).

    username:
        Basic-auth username (e.g. a consumer key).
    password:
        Basic-auth password (e.g. a consumer secret). SECRET — do not log.
    connection_config:
        Nango ``connection_config`` dict (per-connection settings such as the
        base URL). Empty dict when the connection declares none.
    """

    username: str
    password: str
    connection_config: dict


@dataclass
class ConnectionHealth:
    """Health status of a Nango connection.

    status:
        "ok"      -- credentials present and fresh
        "stale"   -- credentials present but last_fetched_at > 24 hours ago
        "revoked" -- connection does not exist in Nango (404) or credentials absent
    last_fetched_at:
        UTC datetime of the last successful token fetch, or None if unknown.
    """

    status: Literal["ok", "stale", "revoked"]
    last_fetched_at: datetime | None


# ---------------------------------------------------------------------------
# Async core implementations (httpx)
# ---------------------------------------------------------------------------


def _error_code(resp: "httpx.Response") -> str:
    """Extract Nango's error.code from a JSON error body ("" if absent)."""
    try:
        return (resp.json().get("error") or {}).get("code", "")
    except Exception:
        return ""


async def _list_connections_async(provider: str | None = None) -> list[dict]:
    """Async impl: GET /connection, optionally filtered by provider.

    Verified against nango-server hosted-0.70.9: the list endpoint accepts NO
    provider filter parameter (any is rejected with 400 invalid_query_params),
    so filtering happens client-side on the provider_config_key field.

    Returns a list of dicts with at minimum: connection_id, provider, created_at.
    """
    base_url = _nango_base_url()

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{base_url}/connection",
            headers=_auth_headers(),
        )

    if resp.status_code >= 500:
        logger.error(
            '{"event": "nango_api_error", "status": %d, "path": "/connection"}',
            resp.status_code,
        )
        resp.raise_for_status()

    resp.raise_for_status()
    payload = resp.json()

    # Nango API returns { connections: [...] } or a list directly depending on version
    if isinstance(payload, dict):
        connections = payload.get("connections", [])
    else:
        connections = payload

    # Normalise to the minimal contract: connection_id, provider, created_at
    result: list[dict] = []
    for conn in connections:
        result.append(
            {
                "connection_id": conn.get("connection_id") or conn.get("id", ""),
                "provider": conn.get("provider_config_key") or conn.get("provider", ""),
                "created_at": conn.get("created_at", ""),
                # Preserve all original fields for callers that need them
                **conn,
            }
        )

    if provider is not None:
        # Client-side filter (no server-side param exists in 0.70.9).
        result = [c for c in result if c["provider"] == provider]

    return result


async def _resolve_provider_async(connection_id: str) -> str | None:
    """Look up a connection's provider_config_key from the list endpoint.

    nango-server hosted-0.70.9 REQUIRES provider_config_key as a query param on
    GET /connection/{id}; when the caller doesn't know it we resolve it here.
    Returns None when the connection does not exist.
    """
    for conn in await _list_connections_async():
        if conn.get("connection_id") == connection_id:
            return conn.get("provider") or None
    return None


async def _get_fresh_token_async(connection_id: str, provider: str | None = None) -> str:
    """Async impl: GET /connection/{connection_id}?force_refresh=true.

    Returns the access_token string.
    Raises NangoTokenError on 401/403, missing credentials, or missing access_token.
    AD-3: the token is returned directly -- never logged or cached here.
    """
    base_url = _nango_base_url()

    if provider is None:
        provider = await _resolve_provider_async(connection_id)
        if provider is None:
            raise NangoTokenError(
                f"Connection '{connection_id}' not found in Nango (no provider match)"
            )

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{base_url}/connection/{connection_id}",
            headers=_auth_headers(),
            params={"force_refresh": "true", "provider_config_key": provider},
        )

    if resp.status_code in (401, 403):
        raise NangoTokenError(
            f"Nango refused token for connection '{connection_id}': HTTP {resp.status_code}"
        )

    if resp.status_code == 404 or (
        resp.status_code == 400
        and _error_code(resp) in ("unknown_provider_config", "unknown_connection")
    ):
        raise NangoTokenError(
            f"Connection '{connection_id}' not found in Nango (HTTP {resp.status_code})"
        )

    resp.raise_for_status()

    payload = resp.json()
    credentials = payload.get("credentials") or {}
    access_token = credentials.get("access_token")

    if not access_token:
        raise NangoTokenError(
            f"Nango response for '{connection_id}' has no credentials.access_token"
        )

    # AD-3: return directly -- do not log the token value
    return access_token


async def _get_basic_credentials_async(
    connection_id: str, provider: str | None = None
) -> BasicCredentials:
    """Async impl: GET /connection/{connection_id} -> Basic-auth credentials.

    Reads Nango's Basic auth credential type: ``credentials.username`` /
    ``credentials.password`` (Nango's canonical Basic-auth field names) plus the
    per-connection ``connection_config``. No ``force_refresh`` is sent — a static
    key/secret pair has nothing to refresh, unlike an OAuth token.

    Raises NangoTokenError on 401/403, a missing connection, or absent
    username/password. AD-3: the password is returned directly, never logged.
    """
    base_url = _nango_base_url()

    if provider is None:
        provider = await _resolve_provider_async(connection_id)
        if provider is None:
            raise NangoTokenError(
                f"Connection '{connection_id}' not found in Nango (no provider match)"
            )

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{base_url}/connection/{connection_id}",
            headers=_auth_headers(),
            params={"provider_config_key": provider},
        )

    if resp.status_code in (401, 403):
        raise NangoTokenError(
            f"Nango refused credentials for connection '{connection_id}': HTTP {resp.status_code}"
        )

    if resp.status_code == 404 or (
        resp.status_code == 400
        and _error_code(resp) in ("unknown_provider_config", "unknown_connection")
    ):
        raise NangoTokenError(
            f"Connection '{connection_id}' not found in Nango (HTTP {resp.status_code})"
        )

    resp.raise_for_status()

    payload = resp.json()
    credentials = payload.get("credentials") or {}
    username = credentials.get("username")
    password = credentials.get("password")

    if not username or not password:
        raise NangoTokenError(
            f"Nango response for '{connection_id}' has no Basic-auth credentials.username/password"
        )

    connection_config = payload.get("connection_config") or {}

    # AD-3: return directly -- do not log the password value.
    return BasicCredentials(
        username=username,
        password=password,
        connection_config=connection_config,
    )


async def _poll_connection_health_async(
    connection_id: str, provider: str | None = None
) -> ConnectionHealth:
    """Async impl: determine connection health by calling Nango API.

    Maps:
        404                       -> revoked (connection gone)
        credentials missing/empty -> revoked
        last_fetched_at > 24h     -> stale
        otherwise                 -> ok
    """
    base_url = _nango_base_url()

    if provider is None:
        provider = await _resolve_provider_async(connection_id)
        if provider is None:
            return ConnectionHealth(status="revoked", last_fetched_at=None)

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.get(
            f"{base_url}/connection/{connection_id}",
            headers=_auth_headers(),
            params={"provider_config_key": provider},
        )

    if resp.status_code == 404 or (
        resp.status_code == 400
        and _error_code(resp) in ("unknown_provider_config", "unknown_connection")
    ):
        return ConnectionHealth(status="revoked", last_fetched_at=None)

    if resp.status_code in (401, 403):
        # Cannot verify health without auth -- treat as revoked
        logger.warning(
            '{"event": "nango_health_auth_error", "status": %d, "connection_id": "[REDACTED]"}',
            resp.status_code,
        )
        return ConnectionHealth(status="revoked", last_fetched_at=None)

    resp.raise_for_status()

    payload = resp.json()
    credentials = payload.get("credentials") or {}

    # Accept OAuth2 (access_token) OR OAuth 1.0a (oauth_token/oauth_token_secret).
    # Nango returns OAuth1 credentials with field names oauth_token/oauth_token_secret,
    # NOT access_token, so checking only access_token falsely revokes every OAuth1
    # connection (H-1).
    _has_oauth2 = bool(credentials.get("access_token"))
    _has_oauth1 = bool(credentials.get("oauth_token") or credentials.get("oauth_token_secret"))
    if not credentials or not (_has_oauth2 or _has_oauth1):
        return ConnectionHealth(status="revoked", last_fetched_at=None)

    # Parse last_fetched_at from Nango response
    last_fetched_raw = credentials.get("last_fetched_at") or payload.get("last_fetched_at")
    last_fetched_at: datetime | None = None

    if last_fetched_raw:
        try:
            # ISO 8601 with optional trailing Z
            last_fetched_at = datetime.fromisoformat(last_fetched_raw.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            last_fetched_at = None

    if last_fetched_at is not None:
        now = datetime.now(tz=timezone.utc)
        # Ensure both are timezone-aware for comparison
        if last_fetched_at.tzinfo is None:
            last_fetched_at = last_fetched_at.replace(tzinfo=timezone.utc)
        age_seconds = (now - last_fetched_at).total_seconds()
        if age_seconds > _STALE_THRESHOLD_SECONDS:
            return ConnectionHealth(status="stale", last_fetched_at=last_fetched_at)

    return ConnectionHealth(status="ok", last_fetched_at=last_fetched_at)


# ---------------------------------------------------------------------------
# Public sync API — loop-safe wrappers (review-2-2 F: asyncio.run() raises
# RuntimeError inside a running event loop, e.g. when called from an async
# FastMCP tool handler). _run_coro executes in a fresh thread-local loop when
# one is already running; async callers should prefer the *_async variants.


def _run_coro(coro):
    """Run *coro* to completion whether or not an event loop is running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)  # no running loop: normal sync path
    # Called from async context: run the coroutine on a dedicated thread.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


# ---------------------------------------------------------------------------


def list_connections(provider: str | None = None) -> list[dict]:
    """List all connections in Nango, optionally filtered by provider.

    Args:
        provider: If given, filters by Nango providerConfigKey (e.g. the
                  integration key configured in Nango). Pass None for all.

    Returns:
        List of dicts, each containing at minimum:
            connection_id (str), provider (str), created_at (str)

    Raises:
        EnvironmentError: if NANGO_SECRET_KEY is not set
        httpx.HTTPStatusError: on 4xx/5xx from Nango (except handled cases)
    """
    return _run_coro(_list_connections_async(provider))


def get_fresh_token(connection_id: str, provider: str | None = None) -> str:
    """Get a valid (freshly refreshed) access token for the given connection.

    The CENTRAL token entry point every connector calls (AD-2: the signature is
    UNCHANGED). It routes provider-aware on the connection's ``auth_path`` -- a
    generic column, NOT a hard-coded provider name, so this module stays
    source-agnostic (AD-2 / Story 18.3):

      * ``auth_path='google_direct'`` -> the Google-direct token service
        (``core.token_service``): decrypt the stored token (18.1), refresh it via
        the ``refresh_token`` grant (18.2) when expired, return the fresh access
        token. A revoked refresh_token surfaces as ``auth_expired`` so the shell
        renders a reconnect affordance (AD-15).
      * otherwise (or no row / DB pre-029 / DB unreachable) -> Nango with
        force_refresh=true (the unchanged behaviour for every non-Google provider).

    The token is returned directly -- it is the caller's responsibility to use it
    and not persist it (AD-3).

    Args:
        connection_id: The connection identifier connectors pass. For Nango rows
            this is the ``nango_connection_id`` used against the Nango API; the
            same value is the routing lookup key in ``app.connection_ref``.

    Returns:
        Access token string (do not log or persist -- AD-3).

    Raises:
        NangoTokenError: on 401, 403, 404, or missing credentials.access_token
        token_service.GoogleAuthExpired: google_direct refresh_token revoked
            (``auth_expired`` -- reconnect required).
        EnvironmentError: if NANGO_SECRET_KEY is not set (Nango path only)
    """
    # Story 18.3: provider-aware routing decided on auth_path (source-agnostic).
    from core import token_service  # noqa: PLC0415 -- local import: avoid cycle

    resolved = token_service.resolve_connection_by_nango_id(connection_id)
    if resolved is not None and resolved.auth_path == token_service.AUTH_PATH_GOOGLE_DIRECT:
        # Scheduled pulls are a SYSTEM context (not tenant-exposed): identity='system'.
        # A future tenant-exposed caller MUST gate identity_has_project_access first
        # and pass the real subject (see token_service docstring).
        return token_service.get_fresh_google_token(resolved, identity="system")

    # Unchanged Nango path for every non-google_direct connection.
    return _run_coro(_get_fresh_token_async(connection_id, provider))


def get_basic_credentials(connection_id: str, provider: str | None = None) -> BasicCredentials:
    """Resolve HTTP Basic-auth credentials + connection config for a connection.

    The entry point for self-hosted connectors that authenticate with a STATIC
    key/secret pair (HTTP Basic auth, no OAuth broker) and carry a per-connection
    base URL — the complement to ``get_fresh_token`` (which is OAuth-shaped and
    only returns an access token). Reusable by ANY such provider; this module
    names none (AD-2).

    Unlike ``get_fresh_token`` there is no ``auth_path`` routing: a static
    key/secret pair is served straight from Nango (no google_direct equivalent).

    Args:
        connection_id: The Nango connection identifier the connector passes.
        provider: Nango providerConfigKey; resolved from the connection when None.

    Returns:
        BasicCredentials(username, password, connection_config). The password is
        a secret — use it immediately, never persist or log it (AD-3).

    Raises:
        NangoTokenError: on 401, 403, 404, or missing username/password.
        EnvironmentError: if NANGO_SECRET_KEY is not set.
    """
    return _run_coro(_get_basic_credentials_async(connection_id, provider))


async def _oauth1_proxy_request_async(
    connection_id: str,
    provider: str,
    method: str,
    path: str,
    *,
    base_url_override: str | None = None,
    **kwargs,
) -> httpx.Response:
    """Delegate an OAuth 1.0a-signed provider request to the Nango broker."""
    headers = {
        **_auth_headers(),
        "Connection-Id": connection_id,
        "Provider-Config-Key": provider,
    }
    if base_url_override:
        headers["Nango-Proxy-Base-Url-Override"] = base_url_override.rstrip("/")
    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        return await client.request(
            method,
            f"{_nango_base_url()}/proxy/{path.lstrip('/')}",
            headers=headers,
            **kwargs,
        )


def oauth1_proxy_request(
    connection_id: str,
    provider: str,
    method: str,
    path: str,
    *,
    base_url_override: str | None = None,
    **kwargs,
) -> httpx.Response:
    """Execute a broker-signed OAuth 1.0a request without exposing token secrets."""
    return _run_coro(
        _oauth1_proxy_request_async(
            connection_id,
            provider,
            method,
            path,
            base_url_override=base_url_override,
            **kwargs,
        )
    )


def poll_connection_health(connection_id: str, provider: str | None = None) -> ConnectionHealth:
    """Determine the health of a connection -- provider-aware (Story 18.3).

    Routes on ``auth_path`` exactly like ``get_fresh_token``:
      * ``auth_path='google_direct'`` -> health read from the LOCAL ``token_expiry``
        (no Nango polling): ok / stale (refresh due) / revoked (no token) -- same
        vocabulary (green/amber/red + ``auth_expired``) the poller already writes.
      * otherwise -> Nango polling (unchanged for every non-Google provider).

    Returns:
        ConnectionHealth with status one of "ok", "stale", "revoked".

    Raises:
        EnvironmentError: if NANGO_SECRET_KEY is not set (Nango path only)
        httpx.HTTPStatusError: on unexpected 5xx errors (Nango path only)
    """
    from core import token_service  # noqa: PLC0415 -- local import: avoid cycle

    resolved = token_service.resolve_connection_by_nango_id(connection_id)
    if resolved is not None and resolved.auth_path == token_service.AUTH_PATH_GOOGLE_DIRECT:
        return token_service.google_direct_health(resolved)

    return _run_coro(_poll_connection_health_async(connection_id, provider))


async def _delete_connection_async(connection_id: str, provider_config_key: str) -> bool:
    """Async impl: DELETE /connection/{connection_id}?provider_config_key=...

    Calls the Nango OSS API to delete (revoke) a connection.
    - 200/204 -> True (deleted)
    - 404     -> False (already deleted; not an error)
    - 5xx     -> raises httpx.HTTPStatusError (caller catches and continues)

    AD-3: no token material is logged here.

    Args:
        connection_id:       Nango connection_id (nango_connection_id in our schema).
        provider_config_key: Nango provider config key (integration key / 'provider'
                             column in app.connection_ref).

    Returns:
        True if the connection was deleted, False if it was already gone (404).
    """
    base_url = _nango_base_url()

    async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT) as client:
        resp = await client.delete(
            f"{base_url}/connection/{connection_id}",
            headers=_auth_headers(),
            params={"provider_config_key": provider_config_key},
        )

    # 404 = already deleted; treat as success (idempotent revocation)
    if resp.status_code == 404 or (
        resp.status_code == 400
        and _error_code(resp) in ("unknown_provider_config", "unknown_connection")
    ):
        logger.info(
            '{"event": "nango_delete_connection_not_found", "path": "/connection/[REDACTED]"}'
        )
        return False

    if resp.status_code in (200, 204):
        logger.info('{"event": "nango_delete_connection_ok"}')
        return True

    # 5xx or unexpected 4xx -- raise so caller can log warning and continue
    resp.raise_for_status()
    return True  # unreachable; raise_for_status already raised


def delete_connection(connection_id: str, provider_config_key: str) -> bool:
    """Delete (revoke) a Nango connection via the Nango OSS API.

    Story 7.3 (AC4): called during per-connection revocation to purge the
    Nango-side token.  The raw OAuth token lives in Nango's own encrypted store;
    calling this endpoint makes the token irrecoverable from Nango's side.

    Returns:
        True  -- connection deleted by this call.
        False -- connection was already gone (404); treated as success.

    Raises:
        httpx.HTTPStatusError: on 5xx or unexpected 4xx from Nango.
        EnvironmentError:      if NANGO_SECRET_KEY is not set.

    Caller responsibility: catch exceptions and log warning; do NOT block the
    revocation flow on Nango errors (best-effort, same pattern as _delete_project).
    """
    return _run_coro(_delete_connection_async(connection_id, provider_config_key))


def revoke_connection(provider_config_key: str, connection_id: str) -> bool:
    """Alias for delete_connection (Story 7.1 AC4 compatibility shim).

    _delete_project calls nango_client.revoke_connection(provider, nango_conn_id)
    with positional args in (provider, connection_id) order.  This thin wrapper
    normalises the call to delete_connection.
    """
    return delete_connection(connection_id, provider_config_key)
