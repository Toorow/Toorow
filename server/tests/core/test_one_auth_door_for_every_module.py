"""ONE auth door for every HTTP module -- the review of 6744599b (2026-08-30).

6744599b closed the class only for `api_auth._authenticate_canonical_api_request`;
`admin_api._check_canonical_principal` -- the gate of entry_api, instance_claim_api,
project_settings_api and the invitation routes -- still opened `get_connection()`
FIRST and looked for a credential second (REJECT finding F1). And the refactor
read the browser session three times per request, each read a revocation
connection, so a cookie request cost 4 connections where it cost 2 (F2). Both
doors now walk `api_auth.resolve_request_principal`, which reads the session
ONCE and opens exactly one connection to bind.
"""
from __future__ import annotations

import asyncio
import contextlib
import secrets
import time
from unittest.mock import MagicMock

from starlette.requests import Request


def _request(headers=()) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/entry-state",
            "headers": list(headers),
            "query_string": b"",
        }
    )


def _refuse_connection(*args, **kwargs):
    raise AssertionError("get_connection was opened for a request carrying no credential")


def test_second_door_refuses_anonymous_without_a_connection(monkeypatch):
    from core import admin_api, db

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "test-token-abc")
    monkeypatch.setattr(db, "get_connection", _refuse_connection)

    ok, principal = asyncio.run(admin_api._check_canonical_principal(_request()))

    assert (ok, principal) == (False, None)


def test_second_door_refuses_a_wrong_token_without_a_connection(monkeypatch):
    from core import admin_api, api_auth, db

    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_STATIC_TOKEN", "tok-valid-123")
    api_auth.reset_verifier_cache()
    monkeypatch.setattr(db, "get_connection", _refuse_connection)

    ok, principal = asyncio.run(
        admin_api._check_canonical_principal(
            _request([(b"authorization", b"Bearer an-arbitrary-token")])
        )
    )

    assert (ok, principal) == (False, None)





def _configure_oidc(monkeypatch) -> None:
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv("TOOROW_BROWSER_AUTH_MODE", "oidc")
    monkeypatch.setenv("TOOROW_DEPLOYMENT_MODE", "self_hosted")
    monkeypatch.setenv("TOOROW_OIDC_ISSUER", "https://issuer.example")
    monkeypatch.setenv("TOOROW_OIDC_CLIENT_ID", "toorow-browser")
    monkeypatch.setenv("TOOROW_OIDC_REDIRECT_URI", "http://localhost/api/auth/oidc/callback")
    monkeypatch.setenv("TOOROW_OIDC_SESSION_SECRET", "s" * 32)
    monkeypatch.setenv("TOOROW_OIDC_COOKIE_SECURE", "0")
    # DEFAULT ON in production (core/session_revocation.py:98).
    monkeypatch.setenv("TOOROW_SESSION_REVOCATION_ENABLED", "1")


def _ticket() -> str:
    from core import browser_oidc

    _mode, _reason, settings = browser_oidc._browser_mode()
    assert settings is not None
    now = int(time.time())
    return browser_oidc._seal(
        settings,
        {
            "exp": now + 3600,
            "iat": now,
            "sid": secrets.token_urlsafe(24),
            "iss": settings.issuer,
            "sub": "oidc-subject",
            "claims": {"email": "person@example.com", "email_verified": True},
        },
    )


def _cookie_request(cookie: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/protected",
            "headers": [(b"cookie", cookie.encode())],
        }
    )


def _install_counting_db(monkeypatch, counter: list[int]):
    from core import db

    conn = MagicMock()

    @contextlib.contextmanager
    def fake_get_connection():
        counter[0] += 1
        yield conn

    monkeypatch.setattr(db, "get_connection", fake_get_connection)
    return conn


def test_cookie_request_opens_one_connection_for_revocation_plus_one_to_bind(monkeypatch):
    from core import api_auth, session_revocation

    _configure_oidc(monkeypatch)
    counter = [0]
    _install_counting_db(monkeypatch, counter)
    monkeypatch.setattr(session_revocation, "is_session_revoked", lambda *a, **k: False)
    monkeypatch.setattr(
        api_auth,
        "_resolve_browser_canonical_principal",
        lambda session, conn: (
            True,
            api_auth.ResolvedPrincipal(
                person_id="person_EXAMPLE",
                issuer=session.issuer,
                subject=session.subject,
                verified_email="person@example.com",
                display_name=None,
            ),
        ),
    )

    cookie = f"toorow_browser_session={_ticket()}"
    ok, person = asyncio.run(api_auth._authenticate_canonical_api_request(_cookie_request(cookie)))

    assert (ok, person) == (True, "person_EXAMPLE")
    # 1 revocation connection (get_browser_session) + 1 binding connection.
    assert counter[0] == 2, f"opened {counter[0]} connections for one cookie request"


def test_the_invitation_identity_reuses_the_door_s_answer(monkeypatch):
    """AI-323 (2026-08-30): POST /api/organizations and the super-admin check ask
    `_check_invitation_identity` AFTER `_check_auth` -- that used to read the
    session (and its revocation store) a third time. The door's principal now
    travels with the request, so the second question costs no connection."""
    from core import admin_api, api_auth, session_revocation

    _configure_oidc(monkeypatch)
    counter = [0]
    _install_counting_db(monkeypatch, counter)
    monkeypatch.setattr(session_revocation, "is_session_revoked", lambda *a, **k: False)
    monkeypatch.setattr(
        api_auth,
        "_resolve_browser_canonical_principal",
        lambda session, conn: (
            True,
            api_auth.ResolvedPrincipal(
                person_id="person_EXAMPLE",
                issuer=session.issuer,
                subject=session.subject,
                verified_email="person@example.com",
                display_name=None,
            ),
        ),
    )

    request = _cookie_request(f"toorow_browser_session={_ticket()}")
    ok, person = asyncio.run(admin_api._check_auth(request))
    assert (ok, person) == (True, "person_EXAMPLE")
    assert counter[0] == 2

    found, email = asyncio.run(admin_api._check_invitation_identity(request))
    assert (found, email) == (True, "person@example.com")
    assert counter[0] == 2, f"the invitation identity re-read the session: {counter[0]}"
