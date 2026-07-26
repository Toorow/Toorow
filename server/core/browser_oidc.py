"""Browser authentication through an OIDC Authorization Code + PKCE BFF.

The browser only receives encrypted, HttpOnly transaction/session cookies.
Provider tokens and the PKCE verifier stay on the server side.
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
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from cryptography.fernet import Fernet, InvalidToken
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from core.deployment_mode import deployment_mode

logger = logging.getLogger(__name__)

_TRANSACTION_COOKIE = "toorow_oidc_transaction"
_SESSION_COOKIE = "toorow_browser_session"
_TRANSACTION_TTL_SECONDS = 600
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DISCOVERY_TTL_SECONDS = 3600
_ALLOWED_ID_TOKEN_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})
_discovery_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_verifier_cache: dict[tuple[str, str, str, str], object] = {}


class BrowserOIDCConfigurationError(ValueError):
    """The operator's browser OIDC contract is incomplete or unsafe."""


@dataclass(frozen=True, slots=True)
class OIDCSettings:
    issuer: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    session_secret: str
    scopes: str
    id_token_algorithm: str
    session_ttl_seconds: int
    secure_cookies: bool
    provider_name: str


@dataclass(frozen=True, slots=True)
class BrowserSession:
    subject: str
    issuer: str
    claims: dict[str, Any]
    expires_at: int


def reset_browser_oidc_caches() -> None:
    """Clear process caches (primarily for tests changing environment)."""
    _discovery_cache.clear()
    _verifier_cache.clear()


def _required_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise BrowserOIDCConfigurationError(f"{name} is required")
    return value


def _is_loopback_http(url: str) -> bool:
    parsed = urlsplit(url)
    return parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}


def _validate_https_url(name: str, value: str, *, allow_loopback_http: bool = False) -> str:
    parsed = urlsplit(value)
    allowed = parsed.scheme == "https" or (allow_loopback_http and _is_loopback_http(value))
    if not allowed or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        raise BrowserOIDCConfigurationError(f"{name} must be an absolute HTTPS URL")
    return value


def _load_oidc_settings() -> OIDCSettings:
    issuer = _validate_https_url("TOOROW_OIDC_ISSUER", _required_env("TOOROW_OIDC_ISSUER"))
    if urlsplit(issuer).query:
        raise BrowserOIDCConfigurationError("TOOROW_OIDC_ISSUER must not contain a query")

    redirect_uri = _validate_https_url(
        "TOOROW_OIDC_REDIRECT_URI",
        _required_env("TOOROW_OIDC_REDIRECT_URI"),
        allow_loopback_http=True,
    )
    redirect = urlsplit(redirect_uri)
    if redirect.path != "/api/auth/oidc/callback" or redirect.query:
        raise BrowserOIDCConfigurationError(
            "TOOROW_OIDC_REDIRECT_URI must end exactly at /api/auth/oidc/callback"
        )

    session_secret = _required_env("TOOROW_OIDC_SESSION_SECRET")
    if len(session_secret.encode("utf-8")) < 32:
        raise BrowserOIDCConfigurationError(
            "TOOROW_OIDC_SESSION_SECRET must contain at least 32 bytes"
        )

    scopes = os.environ.get("TOOROW_OIDC_SCOPES", "openid email profile").strip()
    if "openid" not in scopes.split():
        raise BrowserOIDCConfigurationError("TOOROW_OIDC_SCOPES must include openid")

    algorithm = os.environ.get("TOOROW_OIDC_ID_TOKEN_ALG", "RS256").strip().upper()
    if algorithm not in _ALLOWED_ID_TOKEN_ALGORITHMS:
        raise BrowserOIDCConfigurationError(
            "TOOROW_OIDC_ID_TOKEN_ALG must be an asymmetric RS* or ES* algorithm"
        )

    raw_ttl = os.environ.get("TOOROW_OIDC_SESSION_TTL_SECONDS", "28800").strip()
    try:
        session_ttl = int(raw_ttl)
    except ValueError as exc:
        raise BrowserOIDCConfigurationError(
            "TOOROW_OIDC_SESSION_TTL_SECONDS must be an integer"
        ) from exc
    if not 300 <= session_ttl <= 86400:
        raise BrowserOIDCConfigurationError(
            "TOOROW_OIDC_SESSION_TTL_SECONDS must be between 300 and 86400"
        )

    raw_secure = os.environ.get("TOOROW_OIDC_COOKIE_SECURE", "1").strip().lower()
    if raw_secure not in {"0", "1", "false", "true"}:
        raise BrowserOIDCConfigurationError("TOOROW_OIDC_COOKIE_SECURE must be 0 or 1")
    secure_cookies = raw_secure in {"1", "true"}
    if not secure_cookies and not _is_loopback_http(redirect_uri):
        raise BrowserOIDCConfigurationError(
            "insecure OIDC cookies are allowed only with a loopback HTTP redirect URI"
        )

    provider_name = os.environ.get("TOOROW_OIDC_PROVIDER_NAME", "Identity provider").strip()
    if not provider_name or len(provider_name) > 80:
        raise BrowserOIDCConfigurationError(
            "TOOROW_OIDC_PROVIDER_NAME must contain between 1 and 80 characters"
        )

    client_id = _required_env("TOOROW_OIDC_CLIENT_ID")

    return OIDCSettings(
        issuer=issuer,
        client_id=client_id,
        client_secret=os.environ.get("TOOROW_OIDC_CLIENT_SECRET", "").strip() or None,
        redirect_uri=redirect_uri,
        session_secret=session_secret,
        scopes=scopes,
        id_token_algorithm=algorithm,
        session_ttl_seconds=session_ttl,
        secure_cookies=secure_cookies,
        provider_name=provider_name,
    )


def _browser_mode() -> tuple[str, str | None, OIDCSettings | None]:
    api_mode = os.environ.get("TOOROW_AUTH_MODE", "disabled").strip().lower()
    if api_mode == "disabled":
        return "disabled", None, None
    if api_mode == "static":
        return "static", None, None
    if api_mode != "oauth":
        return "misconfigured", "unsupported_api_auth_mode", None

    configured = os.environ.get("TOOROW_BROWSER_AUTH_MODE", "").strip().lower()
    if configured == "google_gis":
        if deployment_mode() != "hosted":
            return "misconfigured", "google_gis_is_hosted_only", None
        return "google_gis", None, None
    if configured != "oidc":
        return "misconfigured", "browser_auth_mode_required", None
    try:
        return "oidc", None, _load_oidc_settings()
    except BrowserOIDCConfigurationError:
        logger.exception("browser_oidc_misconfigured")
        return "misconfigured", "oidc_configuration_invalid", None


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return response


async def browser_auth_config(_request: Request) -> Response:
    mode, reason, settings = _browser_mode()
    body: dict[str, Any] = {"mode": mode}
    if reason:
        body["reason"] = reason
    if settings is not None:
        body["provider_name"] = settings.provider_name
    return _no_store(JSONResponse(body))


def _fernet(settings: OIDCSettings) -> Fernet:
    digest = hashlib.sha256(settings.session_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _seal(settings: OIDCSettings, payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return _fernet(settings).encrypt(raw).decode("ascii")


def _open(settings: OIDCSettings, token: str, *, ttl: int) -> dict[str, Any] | None:
    try:
        raw = _fernet(settings).decrypt(token.encode("ascii"), ttl=ttl)
        payload = json.loads(raw)
    except (InvalidToken, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _safe_return_path(value: str | None) -> str:
    if (
        not value
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or not value.startswith("/")
        or value.startswith("//")
    ):
        return "/"
    parsed = urlsplit(value)
    if parsed.scheme or parsed.netloc or parsed.fragment:
        return "/"
    return urlunsplit(("", "", parsed.path, parsed.query, ""))


def _discovery_url(issuer: str) -> str:
    return issuer.rstrip("/") + "/.well-known/openid-configuration"


async def _discover(settings: OIDCSettings) -> dict[str, Any]:
    cached = _discovery_cache.get(settings.issuer)
    if cached and cached[0] > time.time():
        return cached[1]

    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.get(
            _discovery_url(settings.issuer),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        metadata = response.json()
    if not isinstance(metadata, dict) or metadata.get("issuer") != settings.issuer:
        raise BrowserOIDCConfigurationError("OIDC discovery issuer mismatch")

    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        value = metadata.get(key)
        if not isinstance(value, str):
            raise BrowserOIDCConfigurationError(f"OIDC discovery is missing {key}")
        _validate_https_url(f"OIDC discovery {key}", value)

    response_types = metadata.get("response_types_supported", [])
    if "code" not in response_types:
        raise BrowserOIDCConfigurationError("OIDC provider does not advertise code flow")
    methods = metadata.get("code_challenge_methods_supported", [])
    if "S256" not in methods:
        raise BrowserOIDCConfigurationError("OIDC provider does not advertise PKCE S256")
    algorithms = metadata.get("id_token_signing_alg_values_supported", [])
    if settings.id_token_algorithm not in algorithms:
        raise BrowserOIDCConfigurationError(
            "OIDC provider does not advertise the configured ID-token algorithm"
        )

    _discovery_cache[settings.issuer] = (
        time.time() + _DISCOVERY_TTL_SECONDS,
        metadata,
    )
    return metadata


def _authorization_url(endpoint: str, parameters: dict[str, str]) -> str:
    parsed = urlsplit(endpoint)
    reserved = set(parameters)
    existing = [(key, value) for key, value in parse_qsl(parsed.query) if key not in reserved]
    query = urlencode([*existing, *parameters.items()])
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, query, parsed.fragment))


async def oidc_login(request: Request) -> Response:
    mode, _reason, settings = _browser_mode()
    if mode != "oidc" or settings is None:
        return _no_store(JSONResponse({"code": "browser_auth_unavailable"}, status_code=503))
    try:
        metadata = await _discover(settings)
    except (BrowserOIDCConfigurationError, httpx.HTTPError, ValueError):
        logger.exception("browser_oidc_discovery_failed")
        return _no_store(JSONResponse({"code": "oidc_discovery_failed"}, status_code=503))

    state = secrets.token_urlsafe(32)
    nonce = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    now = int(time.time())
    transaction = _seal(
        settings,
        {
            "exp": now + _TRANSACTION_TTL_SECONDS,
            "issuer": settings.issuer,
            "nonce": nonce,
            "return_to": _safe_return_path(request.query_params.get("return_to")),
            "state": state,
            "verifier": verifier,
        },
    )
    location = _authorization_url(
        str(metadata["authorization_endpoint"]),
        {
            "response_type": "code",
            "client_id": settings.client_id,
            "redirect_uri": settings.redirect_uri,
            "scope": settings.scopes,
            "state": state,
            "nonce": nonce,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        },
    )
    response = RedirectResponse(location, status_code=302)
    response.set_cookie(
        _TRANSACTION_COOKIE,
        transaction,
        max_age=_TRANSACTION_TTL_SECONDS,
        path="/api/auth",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="lax",
    )
    return _no_store(response)


async def _exchange_code(
    settings: OIDCSettings,
    metadata: dict[str, Any],
    *,
    code: str,
    verifier: str,
) -> dict[str, Any]:
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.redirect_uri,
        "client_id": settings.client_id,
        "code_verifier": verifier,
    }
    auth: httpx.BasicAuth | None = None
    methods = metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
    if settings.client_secret:
        if "client_secret_basic" in methods:
            auth = httpx.BasicAuth(settings.client_id, settings.client_secret)
            data.pop("client_id")
        elif "client_secret_post" in methods:
            data["client_secret"] = settings.client_secret
        else:
            raise BrowserOIDCConfigurationError(
                "OIDC provider offers no supported client-secret authentication method"
            )
    elif "none" not in methods:
        raise BrowserOIDCConfigurationError(
            "OIDC public client requires token_endpoint_auth_methods_supported=none"
        )
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        response = await client.post(
            str(metadata["token_endpoint"]),
            data=data,
            auth=auth,
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError("OIDC token response must be a JSON object")
    return payload


async def _verify_id_token(
    settings: OIDCSettings,
    metadata: dict[str, Any],
    id_token: str,
    *,
    expected_nonce: str,
) -> dict[str, Any] | None:
    from fastmcp.server.auth import JWTVerifier  # noqa: PLC0415

    key = (
        settings.issuer,
        settings.client_id,
        str(metadata["jwks_uri"]),
        settings.id_token_algorithm,
    )
    verifier = _verifier_cache.get(key)
    if verifier is None:
        verifier = JWTVerifier(
            jwks_uri=str(metadata["jwks_uri"]),
            issuer=settings.issuer,
            audience=settings.client_id,
            algorithm=settings.id_token_algorithm,
        )
        _verifier_cache[key] = verifier
    access = await verifier.verify_token(id_token)  # type: ignore[attr-defined]
    if access is None or not isinstance(access.claims, dict):
        return None
    claims = access.claims
    now = int(time.time())
    exp = claims.get("exp")
    issued_at = claims.get("iat")
    subject = claims.get("sub")
    nonce = claims.get("nonce")
    if (
        isinstance(exp, bool)
        or not isinstance(exp, (int, float))
        or exp <= now
        or isinstance(issued_at, bool)
        or not isinstance(issued_at, (int, float))
        or issued_at > now + 60
        or not isinstance(subject, str)
        or not subject
        or not isinstance(nonce, str)
        or not hmac.compare_digest(nonce, expected_nonce)
    ):
        return None
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings.client_id not in audiences:
        return None
    authorized_party = claims.get("azp")
    if (
        len(audiences) > 1 or authorized_party is not None
    ) and authorized_party != settings.client_id:
        return None
    email_verified = claims.get("email_verified")
    if email_verified is not None and not isinstance(email_verified, bool):
        return None

    return claims


def _callback_error(settings: OIDCSettings, code: str, status_code: int = 400) -> Response:
    response = JSONResponse({"code": code}, status_code=status_code)
    response.delete_cookie(_TRANSACTION_COOKIE, path="/api/auth")
    return _no_store(response)


async def oidc_callback(request: Request) -> Response:
    mode, _reason, settings = _browser_mode()
    if mode != "oidc" or settings is None:
        return _no_store(JSONResponse({"code": "browser_auth_unavailable"}, status_code=503))
    transaction_cookie = request.cookies.get(_TRANSACTION_COOKIE, "")
    transaction = _open(
        settings,
        transaction_cookie,
        ttl=_TRANSACTION_TTL_SECONDS,
    )
    if transaction is None:
        return _callback_error(settings, "oidc_transaction_invalid")
    if (
        transaction.get("issuer") != settings.issuer
        or not isinstance(transaction.get("exp"), int)
        or transaction["exp"] < int(time.time())
    ):
        return _callback_error(settings, "oidc_transaction_invalid")
    returned_state = request.query_params.get("state", "")
    expected_state = transaction.get("state")
    if (
        not isinstance(expected_state, str)
        or not returned_state
        or not hmac.compare_digest(returned_state, expected_state)
    ):
        return _callback_error(settings, "oidc_state_invalid")
    returned_issuer = request.query_params.get("iss")
    if returned_issuer is not None and returned_issuer != settings.issuer:
        return _callback_error(settings, "oidc_issuer_invalid")
    if request.query_params.get("error"):
        return _callback_error(settings, "oidc_authorization_failed")
    code = request.query_params.get("code", "")
    verifier = transaction.get("verifier")
    nonce = transaction.get("nonce")
    if not code or not isinstance(verifier, str) or not isinstance(nonce, str):
        return _callback_error(settings, "oidc_callback_invalid")

    try:
        metadata = await _discover(settings)
        token_response = await _exchange_code(
            settings,
            metadata,
            code=code,
            verifier=verifier,
        )
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise ValueError("OIDC token response has no id_token")
        claims = await _verify_id_token(
            settings,
            metadata,
            id_token,
            expected_nonce=nonce,
        )
    except (BrowserOIDCConfigurationError, httpx.HTTPError, ValueError):
        logger.exception("browser_oidc_callback_exchange_failed")
        return _callback_error(settings, "oidc_token_exchange_failed", 502)
    if claims is None:
        return _callback_error(settings, "oidc_id_token_invalid", 401)

    now = int(time.time())
    expires_at = min(int(claims["exp"]), now + settings.session_ttl_seconds)
    session_claims = {
        key: claims[key]
        for key in (
            "email",
            "email_verified",
            "family_name",
            "given_name",
            "name",
            "picture",
        )
        if key in claims
    }
    session_ticket = _seal(
        settings,
        {
            "exp": expires_at,
            "iss": settings.issuer,
            "sub": claims["sub"],
            "claims": session_claims,
        },
    )
    response = RedirectResponse(
        _safe_return_path(transaction.get("return_to")),
        status_code=303,
    )
    response.delete_cookie(_TRANSACTION_COOKIE, path="/api/auth")
    response.set_cookie(
        _SESSION_COOKIE,
        session_ticket,
        max_age=max(0, expires_at - now),
        path="/",
        secure=settings.secure_cookies,
        httponly=True,
        samesite="strict",
    )
    return _no_store(response)


def _session_origin_allowed(request: Request, settings: OIDCSettings) -> bool:
    """Require an exact public Origin before a cookie authorizes a mutation."""
    if request.method.upper() not in _UNSAFE_METHODS:
        return True
    redirect = urlsplit(settings.redirect_uri)
    expected = f"{redirect.scheme}://{redirect.netloc}"
    origin = request.headers.get("origin", "")
    return bool(origin) and hmac.compare_digest(origin, expected)


def get_browser_session(request: Request) -> BrowserSession | None:
    """Return a valid browser session only in the explicit OIDC mode."""
    mode, _reason, settings = _browser_mode()
    if mode != "oidc" or settings is None:
        return None
    if not _session_origin_allowed(request, settings):
        return None
    ticket = request.cookies.get(_SESSION_COOKIE, "")
    payload = _open(settings, ticket, ttl=settings.session_ttl_seconds)
    if payload is None:
        return None
    subject = payload.get("sub")
    issuer = payload.get("iss")
    expires_at = payload.get("exp")
    claims = payload.get("claims")
    if (
        not isinstance(subject, str)
        or not subject
        or issuer != settings.issuer
        or not isinstance(expires_at, int)
        or expires_at <= int(time.time())
        or not isinstance(claims, dict)
    ):
        return None
    return BrowserSession(
        subject=subject,
        issuer=issuer,
        claims={**claims, "iss": issuer, "sub": subject, "exp": expires_at},
        expires_at=expires_at,
    )


async def browser_session(request: Request) -> Response:
    session = get_browser_session(request)
    if session is None:
        return _no_store(JSONResponse({"authenticated": False}, status_code=401))
    claims = session.claims
    return _no_store(
        JSONResponse(
            {
                "authenticated": True,
                "subject": session.subject,
                "email": claims.get("email"),
                "email_verified": claims.get("email_verified") is True,
                "display_name": claims.get("name"),
                "picture": claims.get("picture"),
                "expires_at": session.expires_at,
            }
        )
    )


async def browser_logout(request: Request) -> Response:
    mode, _reason, settings = _browser_mode()
    if mode == "oidc" and settings is not None and not _session_origin_allowed(request, settings):
        return _no_store(JSONResponse({"code": "csrf_origin_invalid"}, status_code=403))
    response = Response(status_code=204)
    response.delete_cookie(_SESSION_COOKIE, path="/")
    return _no_store(response)


BROWSER_AUTH_ROUTES = [
    Route("/api/auth/browser-config", endpoint=browser_auth_config, methods=["GET"]),
    Route("/api/auth/oidc/login", endpoint=oidc_login, methods=["GET"]),
    Route("/api/auth/oidc/callback", endpoint=oidc_callback, methods=["GET"]),
    Route("/api/auth/session", endpoint=browser_session, methods=["GET"]),
    Route("/api/auth/logout", endpoint=browser_logout, methods=["POST"]),
]
