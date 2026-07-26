from __future__ import annotations

import asyncio
import hashlib
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.requests import Request
from starlette.routing import Router
from starlette.testclient import TestClient


def _configure_oidc(monkeypatch) -> None:
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_BROWSER_AUTH_MODE", "oidc")
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("TOOROW_OIDC_CLIENT_ID", "toorow-browser")
    monkeypatch.setenv(
        "TOOROW_OIDC_REDIRECT_URI",
        "http://localhost/api/auth/oidc/callback",
    )
    monkeypatch.setenv("TOOROW_OIDC_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("TOOROW_OIDC_COOKIE_SECURE", "0")
    monkeypatch.setenv("TOOROW_OIDC_PROVIDER_NAME", "Example SSO")


def _metadata() -> dict[str, object]:
    return {
        "issuer": "https://issuer.example",
        "authorization_endpoint": "https://issuer.example/authorize",
        "token_endpoint": "https://issuer.example/token",
        "jwks_uri": "https://issuer.example/jwks",
        "response_types_supported": ["code"],
        "code_challenge_methods_supported": ["S256"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "token_endpoint_auth_methods_supported": ["none"],
    }


def _request(
    *,
    method: str = "GET",
    cookie: str = "",
    origin: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if cookie:
        headers.append((b"cookie", cookie.encode()))
    if origin:
        headers.append((b"origin", origin.encode()))
    return Request(
        {
            "type": "http",
            "method": method,
            "path": "/api/protected",
            "headers": headers,
        }
    )


def test_browser_config_is_fail_closed_without_explicit_mode(monkeypatch):
    from core import browser_oidc

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.delenv("TOOROW_BROWSER_AUTH_MODE", raising=False)

    response = asyncio.run(browser_oidc.browser_auth_config(_request()))

    assert response.status_code == 200
    assert b'"mode":"misconfigured"' in response.body
    assert b"browser_auth_mode_required" in response.body
    assert response.headers["cache-control"] == "no-store"


def test_google_gis_is_refused_in_self_hosted_mode(monkeypatch):
    from core import browser_oidc

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_BROWSER_AUTH_MODE", "google_gis")
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")

    mode, reason, _settings = browser_oidc._browser_mode()

    assert mode == "misconfigured"
    assert reason == "google_gis_is_hosted_only"


def test_login_binds_state_nonce_and_pkce_to_encrypted_cookie(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)

    async def discover(_settings):
        return _metadata()

    monkeypatch.setattr(browser_oidc, "_discover", discover)
    app = Router(routes=browser_oidc.BROWSER_AUTH_ROUTES)

    with TestClient(app, base_url="http://localhost") as client:
        response = client.get(
            "/api/auth/oidc/login?return_to=https://evil.example/steal",
            follow_redirects=False,
        )

    assert response.status_code == 302
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["toorow-browser"]
    assert query["code_challenge_method"] == ["S256"]
    assert len(query["state"][0]) >= 43
    assert len(query["nonce"][0]) >= 43
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    transaction = browser_oidc._open(
        browser_oidc._load_oidc_settings(),
        response.cookies["toorow_oidc_transaction"],
        ttl=600,
    )
    assert transaction is not None
    assert transaction["return_to"] == "/"
    challenge = hashlib.sha256(transaction["verifier"].encode("ascii")).digest()
    import base64

    assert (
        base64.urlsafe_b64encode(challenge).rstrip(b"=").decode("ascii")
        == query["code_challenge"][0]
    )


def test_callback_creates_http_only_session_without_exposing_provider_tokens(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)
    now = int(time.time())

    async def discover(_settings):
        return _metadata()

    async def exchange(_settings, _metadata_value, *, code, verifier):
        assert code == "authorization-code"
        assert len(verifier) == 43
        return {
            "access_token": "must-never-reach-the-browser",
            "refresh_token": "must-never-reach-the-browser",
            "id_token": "signed-id-token",
        }

    async def verify(_settings, _metadata_value, id_token, *, expected_nonce):
        assert id_token == "signed-id-token"
        assert expected_nonce
        return {
            "iss": "https://issuer.example",
            "sub": "oidc-subject",
            "aud": "toorow-browser",
            "iat": now,
            "exp": now + 3600,
            "nonce": expected_nonce,
            "email": "person@example.com",
            "email_verified": True,
            "name": "Person Example",
        }

    monkeypatch.setattr(browser_oidc, "_discover", discover)
    monkeypatch.setattr(browser_oidc, "_exchange_code", exchange)
    monkeypatch.setattr(browser_oidc, "_verify_id_token", verify)
    app = Router(routes=browser_oidc.BROWSER_AUTH_ROUTES)

    with TestClient(app, base_url="http://localhost") as client:
        login = client.get("/api/auth/oidc/login?return_to=/p/one", follow_redirects=False)
        state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
        callback = client.get(
            f"/api/auth/oidc/callback?code=authorization-code&state={state}"
            "&iss=https%3A%2F%2Fissuer.example",
            follow_redirects=False,
        )
        session = client.get("/api/auth/session")

    assert callback.status_code == 303
    assert callback.headers["location"] == "/p/one"
    set_cookie = callback.headers["set-cookie"]
    assert "toorow_browser_session=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=strict" in set_cookie
    assert "must-never-reach-the-browser" not in set_cookie
    assert session.status_code == 200
    assert session.json()["subject"] == "oidc-subject"
    assert "access_token" not in session.json()


def test_callback_rejects_state_mismatch_before_token_exchange(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)

    async def discover(_settings):
        return _metadata()

    exchanged = False

    async def exchange(*_args, **_kwargs):
        nonlocal exchanged
        exchanged = True
        return {}

    monkeypatch.setattr(browser_oidc, "_discover", discover)
    monkeypatch.setattr(browser_oidc, "_exchange_code", exchange)
    app = Router(routes=browser_oidc.BROWSER_AUTH_ROUTES)

    with TestClient(app, base_url="http://localhost") as client:
        client.get("/api/auth/oidc/login", follow_redirects=False)
        response = client.get(
            "/api/auth/oidc/callback?code=code&state=foreign-state",
            follow_redirects=False,
        )

    assert response.status_code == 400
    assert response.json()["code"] == "oidc_state_invalid"
    assert exchanged is False


def test_id_token_audience_is_always_the_oidc_client_id(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)
    settings = browser_oidc._load_oidc_settings()
    now = int(time.time())
    key = (
        settings.issuer,
        settings.client_id,
        "https://issuer.example/jwks",
        settings.id_token_algorithm,
    )

    class FakeVerifier:
        async def verify_token(self, _token):
            return SimpleNamespace(
                claims={
                    "iss": settings.issuer,
                    "sub": "subject",
                    "aud": "different-api-audience",
                    "iat": now,
                    "exp": now + 60,
                    "nonce": "nonce",
                }
            )

    browser_oidc._verifier_cache[key] = FakeVerifier()
    claims = asyncio.run(
        browser_oidc._verify_id_token(
            settings,
            _metadata(),
            "token",
            expected_nonce="nonce",
        )
    )

    assert claims is None


def test_cookie_authenticated_mutations_require_exact_origin(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)
    settings = browser_oidc._load_oidc_settings()
    ticket = browser_oidc._seal(
        settings,
        {
            "iss": settings.issuer,
            "sub": "subject",
            "exp": int(time.time()) + 600,
            "claims": {},
        },
    )
    cookie = f"toorow_browser_session={ticket}"

    assert browser_oidc.get_browser_session(_request(method="POST", cookie=cookie)) is None
    assert (
        browser_oidc.get_browser_session(
            _request(
                method="PATCH",
                cookie=cookie,
                origin="https://attacker.example",
            )
        )
        is None
    )
    accepted = browser_oidc.get_browser_session(
        _request(
            method="DELETE",
            cookie=cookie,
            origin="http://localhost",
        )
    )
    assert accepted is not None
    assert accepted.subject == "subject"


def test_logout_rejects_missing_or_foreign_origin(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)
    assert asyncio.run(browser_oidc.browser_logout(_request(method="POST"))).status_code == 403
    assert (
        asyncio.run(
            browser_oidc.browser_logout(_request(method="POST", origin="https://attacker.example"))
        ).status_code
        == 403
    )
    assert (
        asyncio.run(
            browser_oidc.browser_logout(_request(method="POST", origin="http://localhost"))
        ).status_code
        == 204
    )


def test_shared_api_auth_returns_canonical_person_for_all_modules(monkeypatch):
    from core import api_auth, db

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_CANONICAL_IDENTITY_ENABLED", "1")
    principal = api_auth.ResolvedPrincipal(
        person_id="person_canonical",
        issuer="https://issuer.example",
        subject="oidc-subject",
        verified_email="person@example.com",
        display_name="Person Example",
    )
    resolver = AsyncMock(return_value=(True, principal))
    monkeypatch.setattr(api_auth, "authenticate_canonical_principal", resolver)
    conn = MagicMock()
    connection_context = MagicMock()
    connection_context.__enter__.return_value = conn
    connection_context.__exit__.return_value = False
    monkeypatch.setattr(db, "get_connection", lambda: connection_context)

    result = asyncio.run(api_auth.authenticate_api_request(_request()))

    assert result == (True, "person_canonical")
    resolver.assert_awaited_once()
    conn.commit.assert_called_once_with()


def test_public_oidc_client_must_be_explicitly_advertised(monkeypatch):
    from core import browser_oidc

    _configure_oidc(monkeypatch)
    settings = browser_oidc._load_oidc_settings()
    metadata = {
        **_metadata(),
        "token_endpoint_auth_methods_supported": ["client_secret_basic"],
    }

    with pytest.raises(
        browser_oidc.BrowserOIDCConfigurationError,
        match="public client",
    ):
        asyncio.run(browser_oidc._exchange_code(settings, metadata, code="code", verifier="v" * 43))
