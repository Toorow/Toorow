"""Tests pour server/core/google_oauth.py + les routes admin (Story 18.2, AD-21).

Familles de tests :

  (a) UNIT -- URL d'autorisation :
      - scopes/params EXACTS (Authorization Code, access_type=offline,
        prompt=consent, include_granted_scopes) ;
      - AUCUN client_secret dans l'URL ;
      - config manquante -> GoogleOAuthConfigError.

  (b) UNIT -- state anti-CSRF :
      - round-trip mint -> verify (project/connection/identity/nonce) ;
      - expiration (exp depasse -> GoogleOAuthStateError) ;
      - mismatch/altération (tag falsifié, payload modifié -> rejet) ;
      - erreur unique opaque (pas d'oracle expired vs forged).

  (c) ROUTES via respx -- token endpoint Google mocké :
      - succès (store appelé, audit d'émission, redirection succès) ;
      - erreur Google redigée (pas de code/token) ;
      - refresh_token absent -> message re-consentement français.

  (d) caplog durci (_full_log_dump) -- zéro secret loggé sur succès ET échec.

  (e) live_postgres (@pg_available) -- callback complet contre la vraie DB.

  (f) seam AI-56 build_asgi_app -- les 2 routes existent avec assertions de valeurs.

Règles : NFR3 (zéro token/code en clair), AI-53 (scopes/endpoints), AI-56 (seam
avec valeurs), AI-03 (ASCII-only), messages FR.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from unittest.mock import MagicMock, patch

import pytest
import respx
from httpx import Response

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Placeholders manifestement factices -- JAMAIS de vrai token/code ici.
_FAKE_CODE = "code_test_not_a_secret"
_FAKE_ACCESS = "tok_test_not_a_secret_access"
_FAKE_REFRESH = "tok_test_not_a_secret_refresh"
_FAKE_CLIENT_SECRET = "clientsecret_test_not_a_secret"

_AUTH = ("core.admin_api._check_auth", (True, "admin@example.com"))


@pytest.fixture()
def anyio_backend():
    """Pin the anyio backend to asyncio (trio is not a project dependency).

    Matches the existing async-test convention in the suite; keeps @pytest.mark.anyio
    from also parametrizing a [trio] variant that would error on the missing dep.
    """
    return "asyncio"


@pytest.fixture()
def oauth_env(monkeypatch):
    """Configure un client OAuth de test + un secret de state deterministe."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id-123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI", "https://console.example.com/api/google/oauth/callback"
    )
    monkeypatch.setenv("GOOGLE_OAUTH_STATE_SECRET", "state-secret-deterministic-test-key")
    yield


# ---------------------------------------------------------------------------
# (a) URL d'autorisation
# ---------------------------------------------------------------------------


def test_authorize_url_has_exact_params_and_scopes(oauth_env):
    from urllib.parse import parse_qs, urlparse

    from core.google_oauth import (
        GOOGLE_AUTHORIZE_ENDPOINT,
        GOOGLE_STACK_SCOPES,
        build_authorize_url,
    )

    url = build_authorize_url("proj_1", "conn_1", "admin@example.com")
    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == GOOGLE_AUTHORIZE_ENDPOINT

    q = parse_qs(parsed.query)
    assert q["response_type"] == ["code"]
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    assert q["include_granted_scopes"] == ["true"]
    assert q["client_id"] == ["client-id-123.apps.googleusercontent.com"]
    # Les 4 scopes exacts du stack, dans un seul ecran (AI-53).
    scope_val = q["scope"][0]
    for scope in GOOGLE_STACK_SCOPES:
        assert scope in scope_val
    assert "analytics.readonly" in scope_val
    assert "webmasters.readonly" in scope_val
    assert "adwords" in scope_val
    assert "spreadsheets.readonly" in scope_val
    # Un state est present.
    assert q["state"][0]


def test_authorize_url_never_contains_client_secret(oauth_env):
    from core.google_oauth import build_authorize_url

    url = build_authorize_url("proj_1", "conn_1", "admin@example.com")
    assert _FAKE_CLIENT_SECRET not in url
    assert "client_secret" not in url


def test_authorize_url_missing_config_raises(monkeypatch):
    from core.google_oauth import GoogleOAuthConfigError, build_authorize_url

    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    with pytest.raises(GoogleOAuthConfigError) as exc_info:
        build_authorize_url("proj_1", "conn_1", "admin@example.com")
    # Le message nomme les variables manquantes mais aucune valeur.
    assert "GOOGLE_OAUTH_CLIENT_ID" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (b) State anti-CSRF
# ---------------------------------------------------------------------------


def test_state_round_trip(oauth_env):
    from core.google_oauth import mint_state, verify_state

    state = mint_state("proj_xyz", "conn_abc", "admin@example.com")
    decoded = verify_state(state)
    assert decoded.project_id == "proj_xyz"
    assert decoded.connection_ref_id == "conn_abc"
    assert decoded.identity == "admin@example.com"
    assert decoded.nonce  # non vide


def test_state_expired_rejected(oauth_env):
    from core.google_oauth import GoogleOAuthStateError, mint_state, verify_state

    # Emis a t=1000 avec ttl=10 -> expire a 1010. On verifie a t=2000.
    state = mint_state("proj_1", "conn_1", "admin@example.com", ttl_seconds=10, now=1000)
    with pytest.raises(GoogleOAuthStateError):
        verify_state(state, now=2000)


def test_state_not_yet_expired_ok(oauth_env):
    from core.google_oauth import mint_state, verify_state

    state = mint_state("proj_1", "conn_1", "admin@example.com", ttl_seconds=600, now=1000)
    decoded = verify_state(state, now=1100)
    assert decoded.project_id == "proj_1"


def test_state_tampered_tag_rejected(oauth_env):
    from core.google_oauth import GoogleOAuthStateError, mint_state, verify_state

    state = mint_state("proj_1", "conn_1", "admin@example.com")
    payload_b64, tag_b64 = state.split(".", 1)
    # Falsifier le tag (dernier caractere).
    forged_tag = tag_b64[:-1] + ("A" if tag_b64[-1] != "A" else "B")
    with pytest.raises(GoogleOAuthStateError):
        verify_state(f"{payload_b64}.{forged_tag}")


def test_state_tampered_payload_rejected(oauth_env):
    """Modifier le payload (autre projet) sans re-signer -> rejet (le tag ne matche plus)."""
    import base64

    from core.google_oauth import GoogleOAuthStateError, mint_state, verify_state

    state = mint_state("proj_victim", "conn_1", "admin@example.com")
    _payload_b64, tag_b64 = state.split(".", 1)
    # Construire un payload attaquant pour un autre projet.
    evil = json.dumps(
        {
            "project_id": "proj_attacker",
            "connection_ref_id": "conn_1",
            "identity": "admin@example.com",
            "nonce": "x",
            "exp": int(time.time()) + 600,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    evil_b64 = base64.urlsafe_b64encode(evil).rstrip(b"=").decode()
    with pytest.raises(GoogleOAuthStateError):
        verify_state(f"{evil_b64}.{tag_b64}")


def test_state_malformed_rejected(oauth_env):
    from core.google_oauth import GoogleOAuthStateError, verify_state

    for bad in ["", "no-dot", "only.", ".only", "@@@.@@@"]:
        with pytest.raises(GoogleOAuthStateError):
            verify_state(bad)


def test_state_mismatch_and_expired_same_error_type(oauth_env):
    """Anti-oracle : expiration et falsification levent le MEME type (pas de signal)."""
    from core.google_oauth import GoogleOAuthStateError, mint_state, verify_state

    expired = mint_state("p", "c", "i", ttl_seconds=1, now=1000)
    forged = mint_state("p", "c", "i")
    fp, ft = forged.split(".", 1)
    forged = f"{fp}.{ft[:-2]}AA"

    e1 = e2 = None
    try:
        verify_state(expired, now=5000)
    except GoogleOAuthStateError as exc:
        e1 = exc
    try:
        verify_state(forged)
    except GoogleOAuthStateError as exc:
        e2 = exc
    assert type(e1) is type(e2) is GoogleOAuthStateError


# ---------------------------------------------------------------------------
# (b-bis) Parse token response : refresh_token absent
# ---------------------------------------------------------------------------


def test_parse_token_missing_refresh_forces_reconsent(oauth_env):
    from core.google_oauth import GoogleOAuthError, _parse_token_response

    with pytest.raises(GoogleOAuthError) as exc_info:
        _parse_token_response({"access_token": _FAKE_ACCESS, "scope": "a b", "expires_in": 3600})
    msg = str(exc_info.value)
    assert "refresh_token" in msg
    assert "consentement" in msg.lower() or "reconnect" in msg.lower()


def test_parse_token_missing_access_rejected(oauth_env):
    from core.google_oauth import GoogleOAuthError, _parse_token_response

    with pytest.raises(GoogleOAuthError):
        _parse_token_response({"refresh_token": _FAKE_REFRESH})


def test_parse_token_success(oauth_env):
    from core.google_oauth import _parse_token_response

    parsed = _parse_token_response(
        {
            "access_token": _FAKE_ACCESS,
            "refresh_token": _FAKE_REFRESH,
            "scope": "https://www.googleapis.com/auth/analytics.readonly x",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
    )
    assert parsed.access_token == _FAKE_ACCESS
    assert parsed.refresh_token == _FAKE_REFRESH
    assert parsed.token_expiry is not None
    assert "https://www.googleapis.com/auth/analytics.readonly" in parsed.granted_scopes


def test_token_response_repr_hides_secrets(oauth_env):
    from core.google_oauth import GoogleTokenResponse

    resp = GoogleTokenResponse(
        access_token=_FAKE_ACCESS,
        refresh_token=_FAKE_REFRESH,
        token_expiry=None,
        granted_scopes=["adwords"],
    )
    for rendered in (repr(resp), str(resp), f"{resp!r}"):
        assert _FAKE_ACCESS not in rendered
        assert _FAKE_REFRESH not in rendered
        assert "redacted" in rendered.lower()


# ---------------------------------------------------------------------------
# (c) exchange_code via respx -- token endpoint mocké
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.anyio
async def test_exchange_code_success(oauth_env):
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, exchange_code

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
                "token_type": "Bearer",
            },
        )
    )
    token = await exchange_code(_FAKE_CODE)
    assert token.access_token == _FAKE_ACCESS
    assert token.refresh_token == _FAKE_REFRESH
    assert "https://www.googleapis.com/auth/adwords" in token.granted_scopes


@respx.mock
@pytest.mark.anyio
async def test_exchange_code_google_error_redacted(oauth_env):
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, GoogleOAuthError, exchange_code

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            400,
            json={
                "error": "invalid_grant",
                # Google peut renvoyer le code dans error_description -> ne doit PAS fuir.
                "error_description": f"Bad Request for code={_FAKE_CODE}",
            },
        )
    )
    with pytest.raises(GoogleOAuthError) as exc_info:
        await exchange_code(_FAKE_CODE)
    msg = str(exc_info.value)
    assert _FAKE_CODE not in msg, "le code d'autorisation fuit dans l'erreur !"
    # On surface le status + le code d'erreur court, jamais la description.
    assert "invalid_grant" in msg
    assert "Bad Request for code" not in msg


@respx.mock
@pytest.mark.anyio
async def test_exchange_code_missing_refresh_reconsent(oauth_env):
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, GoogleOAuthError, exchange_code

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={"access_token": _FAKE_ACCESS, "expires_in": 3599, "scope": "adwords"},
        )
    )
    with pytest.raises(GoogleOAuthError) as exc_info:
        await exchange_code(_FAKE_CODE)
    assert "refresh_token" in str(exc_info.value)


# ---------------------------------------------------------------------------
# (d) caplog durci -- zero secret loggé (succès ET échec)
# ---------------------------------------------------------------------------


def _full_log_dump(caplog) -> str:
    """review-18-1 F-3: agrege msg + args + exc_text + exc_info de chaque record."""
    parts: list[str] = []
    for r in caplog.records:
        parts.append(r.getMessage())
        parts.append(repr(getattr(r, "msg", "")))
        parts.append(repr(getattr(r, "args", "")))
        if r.exc_text:
            parts.append(r.exc_text)
        if r.exc_info:
            parts.append(repr(r.exc_info))
    return "\n".join(parts)


@respx.mock
@pytest.mark.anyio
async def test_no_secret_in_logs_on_success(oauth_env, caplog):
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, build_authorize_url, exchange_code

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "adwords",
            },
        )
    )
    with caplog.at_level(logging.DEBUG, logger="core.google_oauth"):
        build_authorize_url("proj_1", "conn_1", "admin@example.com")
        await exchange_code(_FAKE_CODE)
    dump = _full_log_dump(caplog)
    assert _FAKE_ACCESS not in dump
    assert _FAKE_REFRESH not in dump
    assert _FAKE_CODE not in dump
    assert _FAKE_CLIENT_SECRET not in dump


@respx.mock
@pytest.mark.anyio
async def test_no_secret_in_logs_on_failure(oauth_env, caplog):
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, GoogleOAuthError, exchange_code

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            400, json={"error": "invalid_grant", "error_description": f"code={_FAKE_CODE}"}
        )
    )
    with caplog.at_level(logging.DEBUG, logger="core.google_oauth"):
        with pytest.raises(GoogleOAuthError):
            await exchange_code(_FAKE_CODE)
    dump = _full_log_dump(caplog)
    assert _FAKE_CODE not in dump
    assert _FAKE_ACCESS not in dump


# ---------------------------------------------------------------------------
# (c-routes) Routes admin -- authorize + callback (mocks)
# ---------------------------------------------------------------------------


def _get_request(query: dict) -> MagicMock:
    req = MagicMock()
    req.query_params = query
    return req
def _authorize_db():
    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    cursor.fetchone.return_value = ("org_1", "org_1", "google-analytics")
    connection = MagicMock()
    connection.__enter__ = MagicMock(return_value=connection)
    connection.__exit__ = MagicMock(return_value=False)
    connection.cursor.return_value = cursor
    return patch("core.db.get_connection", return_value=connection)


@pytest.mark.anyio
async def test_route_authorize_requires_auth():
    from core.admin_api import _google_oauth_authorize

    with patch(_AUTH[0], return_value=(False, "")):
        resp = await _google_oauth_authorize(
            _get_request({"project_id": "p", "connection_ref_id": "c"})
        )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_route_authorize_missing_params(oauth_env):
    from core.admin_api import _google_oauth_authorize

    with patch(_AUTH[0], return_value=_AUTH[1]):
        resp = await _google_oauth_authorize(_get_request({"project_id": "p"}))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert "connection_ref_id" in body["message"]


@pytest.mark.anyio
async def test_route_authorize_success_returns_url(oauth_env):
    from core.admin_api import _google_oauth_authorize

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.project_access.identity_has_project_access", return_value=True),
        _authorize_db(),
        patch("core.project_access.identity_can_manage_org", return_value=True),
    ):
        resp = await _google_oauth_authorize(
            _get_request({"project_id": "proj_1", "connection_ref_id": "conn_1"})
        )
    assert resp.status_code == 200
    body = json.loads(resp.body)
    assert body["authorize_url"].startswith("https://accounts.google.com/o/oauth2/v2/auth")
    assert _FAKE_CLIENT_SECRET not in body["authorize_url"]


@pytest.mark.anyio
async def test_route_authorize_denies_cross_project(oauth_env):
    from core.admin_api import _google_oauth_authorize

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.project_access.identity_has_project_access", return_value=False),
        _authorize_db(),
        patch("core.project_access.identity_can_manage_org", return_value=True),
        patch("core.admin_api.write_audit_row") as audit,
    ):
        resp = await _google_oauth_authorize(
            _get_request({"project_id": "proj_other", "connection_ref_id": "conn_1"})
        )
    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["code"] == "forbidden"
    assert audit.called


@pytest.mark.anyio
async def test_route_authorize_not_configured(monkeypatch):
    """Sans client OAuth (Phase B / AI-08) -> 503 message FR honnete."""
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("GOOGLE_OAUTH_REDIRECT_URI", raising=False)
    from core.admin_api import _google_oauth_authorize

    with (
        patch(_AUTH[0], return_value=_AUTH[1]),
        patch("core.project_access.identity_has_project_access", return_value=True),
        _authorize_db(),
        patch("core.project_access.identity_can_manage_org", return_value=True),
    ):
        resp = await _google_oauth_authorize(
            _get_request({"project_id": "proj_1", "connection_ref_id": "conn_1"})
        )
    assert resp.status_code == 503
    body = json.loads(resp.body)
    assert body["code"] == "oauth_not_configured"


@pytest.mark.anyio
async def test_route_callback_bad_state_generic_400(oauth_env):
    from core.admin_api import _google_oauth_callback

    resp = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": "forged.tag"}))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["code"] == "invalid_state"
    # Pas d'oracle : message generique, pas de detail.
    assert _FAKE_CODE not in json.dumps(body)


@respx.mock
@pytest.mark.anyio
async def test_route_callback_success_stores_and_audits(oauth_env):
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
            },
        )
    )
    state = mint_state("proj_1", "conn_1", "admin@example.com")

    with (
        patch("core.google_token_store.store_google_token") as store,
        patch("core.admin_api.write_audit_row") as audit,
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))

    # Redirection succes (302), avec un flag coarse et AUCUN token/code.
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert "google_oauth=success" in location
    assert _FAKE_ACCESS not in location
    assert _FAKE_REFRESH not in location
    assert _FAKE_CODE not in location

    # Store appele avec expected_project_id (defense 18.1 F-1).
    assert store.called
    _args, kwargs = store.call_args
    assert kwargs.get("expected_project_id") == "proj_1"

    # Audit d'emission ecrit (AD-14) avec l'identite reelle du state.
    assert audit.called
    audit_kwargs = audit.call_args.kwargs
    assert audit_kwargs["identity"] == "admin@example.com"
    assert audit_kwargs["action"] == "connection.created"
    # JAMAIS le token dans les metadata d'audit.
    assert _FAKE_ACCESS not in json.dumps(audit_kwargs.get("metadata"))
    assert _FAKE_REFRESH not in json.dumps(audit_kwargs.get("metadata"))


@respx.mock
@pytest.mark.anyio
async def test_route_callback_google_error_redacted(oauth_env):
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(400, json={"error": "invalid_grant", "error_description": _FAKE_CODE})
    )
    state = mint_state("proj_1", "conn_1", "admin@example.com")
    with (
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["code"] == "exchange_failed"
    # Le message UI ne contient ni le code ni le detail Google.
    assert _FAKE_CODE not in json.dumps(body)
    assert "invalid_grant" not in json.dumps(body)


@respx.mock
@pytest.mark.anyio
async def test_route_callback_missing_refresh_reconsent(oauth_env):
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200, json={"access_token": _FAKE_ACCESS, "expires_in": 3599, "scope": "adwords"}
        )
    )
    state = mint_state("proj_1", "conn_1", "admin@example.com")
    with (
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))
    # refresh_token absent -> exchange leve -> 502 exchange_failed (message re-consent generique).
    assert resp.status_code == 502
    body = json.loads(resp.body)
    assert body["code"] == "exchange_failed"


@pytest.mark.anyio
async def test_route_callback_user_declined(oauth_env):
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import mint_state

    state = mint_state("proj_1", "conn_1", "admin@example.com")
    with patch("core.admin_api.write_audit_row"):
        resp = await _google_oauth_callback(
            _get_request({"error": "access_denied", "state": state})
        )
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["code"] == "consent_declined"


# ---------------------------------------------------------------------------
# (f) seam AI-56 build_asgi_app -- les 2 routes sont montees, avec valeurs
# ---------------------------------------------------------------------------


def test_seam_oauth_routes_registered():
    """AI-56: les 2 routes OAuth sont montees dans le router admin (assertions valeurs)."""
    from core.admin_api import router

    paths = {}
    for r in router.routes:
        path = getattr(r, "path", None)
        if path and "google/oauth" in path:
            paths[path] = getattr(r, "methods", set())

    assert "/api/google/oauth/authorize" in paths
    assert "/api/google/oauth/callback" in paths
    assert "GET" in paths["/api/google/oauth/authorize"]
    assert "GET" in paths["/api/google/oauth/callback"]


# ---------------------------------------------------------------------------
# (e) live_postgres -- callback complet contre la vraie DB (AI-37)
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="TEST_POSTGRES_DSN not set/reachable -- skip live PG"
)


def _seed_connection(dsn: str, project_id: str, conn_id: str) -> None:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
                VALUES (%s, %s, %s, 'active', 'test', 'org_test_fixture')
                ON CONFLICT (id) DO NOTHING
                """,
                (project_id, f"oauth-{project_id}", f"slug-{uuid.uuid4().hex[:8]}"),
            )
            cur.execute(
                """
                INSERT INTO app.connection_ref
                    (id, provider, nango_connection_id, project_id, auth_path, owner_org_id,
                        owner_identity)
                VALUES (%s, 'google-analytics', %s, %s, 'nango', 'org_test_fixture',
                    'tester@example.com')
                """,
                (conn_id, f"nango_{conn_id}", project_id),
            )
        conn.commit()


def _cleanup(dsn: str, project_id: str, conn_id: str) -> None:
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # app.audit_log est append-only (FR12, trigger anti-DELETE) ET porte
            # une FK vers connection_ref : une connexion auditee ne peut pas etre
            # hard-delete (1re passe live Supabase -- en prod ce sera du
            # soft-delete). Cleanup BEST-EFFORT : les entites de test auditees
            # restent sur la base dev, tolere et documente.
            for sql, arg in (
                ("DELETE FROM app.connection_ref WHERE id = %s", conn_id),
                ("DELETE FROM app.projects WHERE id = %s", project_id),
            ):
                try:
                    cur.execute("SAVEPOINT cleanup_step")
                    cur.execute(sql, (arg,))
                    cur.execute("RELEASE SAVEPOINT cleanup_step")
                except Exception:  # noqa: BLE001 -- best-effort, FK audit_log
                    cur.execute("ROLLBACK TO SAVEPOINT cleanup_step")
        conn.commit()


@pg_available
@respx.mock
@pytest.mark.anyio
async def test_live_callback_persists_encrypted_token(monkeypatch, oauth_env):
    """Callback complet : le token chiffre est persiste dans la vraie DB (auth_path
    google_direct, blob opaque) et une ligne d'audit d'emission est ecrite."""
    dsn = os.environ["TEST_POSTGRES_DSN"]
    monkeypatch.setenv("PLATFORM_DB_URL", dsn)
    monkeypatch.setenv("TENANT_KEY_BACKEND", "local")
    monkeypatch.setenv(
        "TENANT_KEY_DIR", os.path.join(os.environ.get("TMP", "/tmp"), f"k_{uuid.uuid4().hex[:8]}")
    )
    import psycopg  # noqa: PLC0415
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
            },
        )
    )

    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    try:
        _seed_connection(dsn, pid, cid)
        state = mint_state(pid, cid, "admin@example.com")
        resp = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))
        assert resp.status_code == 302

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT auth_path, encrypted_token_blob, granted_scopes "
                    "FROM app.connection_ref WHERE id = %s",
                    (cid,),
                )
                auth_path, blob, scopes = cur.fetchone()
                cur.execute(
                    "SELECT action, identity FROM app.audit_log WHERE connection_ref = %s",
                    (cid,),
                )
                audit_rows = cur.fetchall()
        assert auth_path == "google_direct"
        assert blob is not None
        raw = bytes(blob)
        assert _FAKE_ACCESS.encode() not in raw
        assert _FAKE_REFRESH.encode() not in raw
        assert "https://www.googleapis.com/auth/adwords" in list(scopes or [])
        assert any(
            action == "connection.created" and identity == "admin@example.com"
            for action, identity in audit_rows
        ), "ligne d'audit d'emission manquante"
    finally:
        _cleanup(dsn, pid, cid)


# ---------------------------------------------------------------------------
# (g) GREP anti-fuite -- aucun token/code realiste en clair dans les fichiers livres
# ---------------------------------------------------------------------------


def test_no_realistic_token_plaintext_in_story_files():
    import re
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[3]
    story_files = [
        repo_root / "server" / "core" / "google_oauth.py",
        repo_root / "server" / "tests" / "core" / "test_google_oauth.py",
    ]
    forbidden = [
        re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),
        re.compile(r"1//[A-Za-z0-9_\-]{20,}"),
        re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ]
    for path in story_files:
        assert path.exists(), f"fichier de story manquant : {path}"
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            assert pat.search(text) is None, (
                f"token realiste en clair detecte dans {path.name} (motif {pat.pattern!r})"
            )


# ---------------------------------------------------------------------------
# Review securite 18.2 (Opus) -- fixes F-1, F-2, F-3, F-4, F-5, F-7
# ---------------------------------------------------------------------------


# --- F-1 HIGH : hard-fail de la cle de state en prod ---


def test_state_secret_hard_fails_in_oauth_mode(monkeypatch):
    """F-1: TOOROW_AUTH_MODE=oauth sans GOOGLE_OAUTH_STATE_SECRET -> GoogleOAuthConfigError."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.delenv("GOOGLE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("API_SECRET_KEY", raising=False)

    # Recharger le module pour eviter la cache du secret ephemere.
    import importlib

    import core.google_oauth as mod

    importlib.reload(mod)

    from core.google_oauth import GoogleOAuthConfigError

    with pytest.raises(GoogleOAuthConfigError) as exc_info:
        mod._state_secret()
    assert "GOOGLE_OAUTH_STATE_SECRET" in str(exc_info.value)
    err_msg = str(exc_info.value).lower()
    assert "obligatoire" in err_msg or "production" in err_msg


def test_state_secret_hard_fails_in_static_mode(monkeypatch):
    """F-1: TOOROW_AUTH_MODE=static sans GOOGLE_OAUTH_STATE_SECRET -> GoogleOAuthConfigError."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.delenv("GOOGLE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.delenv("API_SECRET_KEY", raising=False)

    import importlib

    import core.google_oauth as mod

    importlib.reload(mod)

    from core.google_oauth import GoogleOAuthConfigError

    with pytest.raises(GoogleOAuthConfigError):
        mod._state_secret()


def test_state_secret_fallback_warning_in_disabled_mode(monkeypatch, caplog):
    """F-1: mode disabled sans secret -> fallback WARNING (pas de raise), comportement actuel."""
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")
    monkeypatch.delenv("GOOGLE_OAUTH_STATE_SECRET", raising=False)
    monkeypatch.setenv("API_SECRET_KEY", "api-secret-test-key-not-real")

    import importlib

    import core.google_oauth as mod

    importlib.reload(mod)

    with caplog.at_level(logging.WARNING, logger="core.google_oauth"):
        result = mod._state_secret()
    assert isinstance(result, bytes)
    assert len(result) == 32  # sha256 digest
    assert any(
        "dev fallback" in r.getMessage().lower() or "deriving" in r.getMessage().lower()
        for r in caplog.records
    )


def test_state_secret_set_works_in_any_mode(monkeypatch):
    """F-1: GOOGLE_OAUTH_STATE_SECRET positionne -> utilise directement (tous modes)."""
    for mode in ("oauth", "static", "disabled"):
        monkeypatch.setenv("TOOROW_AUTH_MODE", mode)
        monkeypatch.setenv("GOOGLE_OAUTH_STATE_SECRET", "tok_test_not_a_secret_state_key")

        import importlib

        import core.google_oauth as mod

        importlib.reload(mod)

        result = mod._state_secret()
        assert result == b"tok_test_not_a_secret_state_key"


# --- F-7 LOW : verify_state rejette les champs vides ---


def test_state_empty_project_id_rejected(oauth_env):
    """F-7: state minte avec project_id='' -> GoogleOAuthStateError."""
    import hashlib as _hashlib
    import hmac as _hmac
    import json as _json
    import time as _time

    from core.google_oauth import GoogleOAuthStateError, _b64url_encode, _state_secret

    payload = {
        "project_id": "",  # vide
        "connection_ref_id": "conn_abc",
        "identity": "admin@example.com",
        "nonce": "test_nonce",
        "exp": int(_time.time()) + 600,
    }
    payload_bytes = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    tag = _hmac.new(_state_secret(), payload_bytes, _hashlib.sha256).digest()
    state = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(tag)}"

    from core.google_oauth import verify_state

    with pytest.raises(GoogleOAuthStateError):
        verify_state(state)


def test_state_empty_connection_ref_id_rejected(oauth_env):
    """F-7: state minte avec connection_ref_id='' -> GoogleOAuthStateError."""
    import hashlib as _hashlib
    import hmac as _hmac
    import json as _json
    import time as _time

    from core.google_oauth import GoogleOAuthStateError, _b64url_encode, _state_secret, verify_state

    payload = {
        "project_id": "proj_1",
        "connection_ref_id": "",  # vide
        "identity": "admin@example.com",
        "nonce": "test_nonce",
        "exp": int(_time.time()) + 600,
    }
    payload_bytes = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    tag = _hmac.new(_state_secret(), payload_bytes, _hashlib.sha256).digest()
    state = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(tag)}"

    with pytest.raises(GoogleOAuthStateError):
        verify_state(state)


def test_state_empty_identity_rejected(oauth_env):
    """F-7: state minte avec identity='' -> GoogleOAuthStateError."""
    import hashlib as _hashlib
    import hmac as _hmac
    import json as _json
    import time as _time

    from core.google_oauth import GoogleOAuthStateError, _b64url_encode, _state_secret, verify_state

    payload = {
        "project_id": "proj_1",
        "connection_ref_id": "conn_abc",
        "identity": "",  # vide
        "nonce": "test_nonce",
        "exp": int(_time.time()) + 600,
    }
    payload_bytes = _json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    tag = _hmac.new(_state_secret(), payload_bytes, _hashlib.sha256).digest()
    state = f"{_b64url_encode(payload_bytes)}.{_b64url_encode(tag)}"

    with pytest.raises(GoogleOAuthStateError):
        verify_state(state)


# --- F-5 LOW : valider ADMIN_CONSOLE_OAUTH_RETURN ---


def test_oauth_console_redirect_valid_path(monkeypatch):
    """F-5: valeur commencant par '/' -> utilisee telle quelle."""
    monkeypatch.setenv("ADMIN_CONSOLE_OAUTH_RETURN", "/mon/chemin/console")
    from core.admin_api import _oauth_console_redirect

    url = _oauth_console_redirect("success", "conn_123")
    assert url.startswith("/mon/chemin/console")
    assert "google_oauth=success" in url
    assert "conn_123" in url


def test_oauth_console_redirect_external_url_falls_back(monkeypatch):
    """F-5: valeur HTTP externe -> fallback /console/connections + warning."""
    monkeypatch.setenv("ADMIN_CONSOLE_OAUTH_RETURN", "http://evil.example.com/steal")
    from core.admin_api import _oauth_console_redirect

    url = _oauth_console_redirect("success", "conn_123")
    assert url.startswith("/console/connections")
    assert "evil.example.com" not in url
    assert "google_oauth=success" in url


def test_oauth_console_redirect_empty_uses_default(monkeypatch):
    """F-5: valeur absente -> defaut /console/connections."""
    monkeypatch.delenv("ADMIN_CONSOLE_OAUTH_RETURN", raising=False)
    from core.admin_api import _oauth_console_redirect

    url = _oauth_console_redirect("error")
    assert url.startswith("/console/connections")
    assert "google_oauth=error" in url


# --- F-3 MEDIUM : re-verification AD-5 au callback ---


@respx.mock
@pytest.mark.anyio
async def test_callback_revoked_access_denied_before_exchange(oauth_env):
    """F-3: acces revoque entre authorize et callback -> 403 + code non echange."""
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    # Mocker le token endpoint pour verifier qu'il n'est PAS appele.
    token_route = respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
            },
        )
    )

    state = mint_state("proj_revoked", "conn_1", "admin@example.com")

    with (
        patch("core.project_access.identity_has_project_access", return_value=False),
        patch("core.db.get_connection"),
        patch("core.admin_api.write_audit_row") as audit,
    ):
        resp = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))

    assert resp.status_code == 403
    body = json.loads(resp.body)
    assert body["code"] == "forbidden"
    # Message francais present.
    msg_low = body["message"].lower()
    assert "acces" in msg_low or "refuse" in msg_low or "membre" in msg_low
    # L'echange du code N'a pas eu lieu (defense en profondeur).
    assert not token_route.called, "le token endpoint a ete appele alors que l'acces est revoque !"
    # Audit d'acces refuse ecrit.
    assert audit.called
    audit_kwargs = audit.call_args.kwargs
    # La valeur REELLE de la constante (core/audit.py ACTION_CROSS_SCOPE_ATTEMPT).
    from core.audit import ACTION_CROSS_SCOPE_ATTEMPT

    assert audit_kwargs["action"] == ACTION_CROSS_SCOPE_ATTEMPT


# --- F-4 MEDIUM : corps non-JSON (preuve que _redact_google_error tient) ---


@respx.mock
@pytest.mark.anyio
async def test_exchange_code_non_json_body_redacted(oauth_env):
    """F-4: Google renvoie un body HTML (400) -> le message d'erreur redirige ne contient
    ni le body HTML ni le code d'autorisation (preuve que _redact_google_error tient)."""
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, GoogleOAuthError, exchange_code

    html_body = f"<html>error code=tok_test_not_a_secret_code body details {_FAKE_CODE}</html>"
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(return_value=Response(400, text=html_body))
    with pytest.raises(GoogleOAuthError) as exc_info:
        await exchange_code(_FAKE_CODE)
    msg = str(exc_info.value)
    # Le body HTML ne doit pas fuir dans le message d'erreur.
    assert html_body not in msg
    assert _FAKE_CODE not in msg
    assert "<html>" not in msg
    # On surface "unparseable" car le body n'est pas JSON.
    assert "unparseable" in msg or "400" in msg


# --- F-2 HIGH : caracterisation du rejeu intra-fenetre ---


@respx.mock
@pytest.mark.anyio
async def test_replay_same_state_google_invalid_grant(oauth_env):
    """F-2a: rejeu du meme state valide, Google rejette le 2e code (invalid_grant).
    Le 2e echange -> 502 exchange_failed, SANS audit supplementaire ni store.
    """
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    state = mint_state("proj_1", "conn_1", "admin@example.com")

    # Premier echange: succes.
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
            },
        )
    )
    with (
        patch("core.google_token_store.store_google_token"),
        patch("core.admin_api.write_audit_row"),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp1 = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))
    assert resp1.status_code == 302

    # Deuxieme echange du meme state: Google renvoie invalid_grant (code deja consomme).
    # Comportement ATTENDU en Phase A: le state HMAC est encore valide (pas de replay-cache),
    # mais Google rejette le code -> 502 exchange_failed, pas de 2e audit, pas de 2e store.
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(400, json={"error": "invalid_grant"})
    )
    with (
        patch("core.google_token_store.store_google_token") as store2,
        patch("core.admin_api.write_audit_row") as audit2,
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp2 = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))

    assert resp2.status_code == 502
    body2 = json.loads(resp2.body)
    assert body2["code"] == "exchange_failed"
    # Aucun store ni audit supplementaire sur le rejeu echoue.
    assert not store2.called, "store appele sur un rejeu invalide !"
    assert not audit2.called, "audit ecrit sur un rejeu invalide (2e ligne indesirable) !"


@respx.mock
@pytest.mark.anyio
async def test_replay_same_state_google_accepts_second_code_behavior_documented(oauth_env):
    """F-2b: BLOCKED Phase B -- cas theorique : 2 codes differents presentes avec le meme state.
    En Phase A (sans replay-cache), si Google accepte les 2, le 2e store est idempotent
    et une 2e ligne d'audit est ecrite. Ce test DOCUMENTE le comportement actuel et
    ANNOTE l'ecart par rapport a l'ideal (dedup d'audit / table anti-rejeu = Phase B).

    NOTE: Ce scenario est quasi-impossible en production (Google invalide le code des
    qu'il est utilise -- F-2a), mais documente le comportement du serveur si cela arrivait.
    Le vrai fix (table anti-rejeu) est BLOCKED Phase B.
    """
    from core.admin_api import _google_oauth_callback
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT, mint_state

    state = mint_state("proj_1", "conn_1", "admin@example.com")

    # Simuler 2 echanges successifs "reussis" (2 codes differents avec le meme state).
    _FAKE_CODE_2 = "code_test_second_not_a_secret"

    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
            },
        )
    )

    with (
        patch("core.google_token_store.store_google_token") as store1,
        patch("core.admin_api.write_audit_row") as audit1,
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp1 = await _google_oauth_callback(_get_request({"code": _FAKE_CODE, "state": state}))
    assert resp1.status_code == 302
    assert store1.called
    assert audit1.called

    # 2e appel avec le meme state valide (toujours dans la fenetre) mais un code different.
    # COMPORTEMENT ACTUEL Phase A : store idempotent (upsert), 2e ligne d'audit.
    # BLOCKED Phase B : la table anti-rejeu devrait rejeter le 2e state ici.
    respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
        return_value=Response(
            200,
            json={
                "access_token": _FAKE_ACCESS,
                "refresh_token": _FAKE_REFRESH,
                "expires_in": 3599,
                "scope": "https://www.googleapis.com/auth/adwords",
            },
        )
    )
    with (
        patch("core.google_token_store.store_google_token") as store2,
        patch("core.admin_api.write_audit_row") as audit2,
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.db.get_connection"),
    ):
        resp2 = await _google_oauth_callback(_get_request({"code": _FAKE_CODE_2, "state": state}))

    # BLOCKED Phase B: en Phase A sans replay-cache, le 2e appel reussit.
    # Ce comportement sera corrige quand la table anti-rejeu sera implementee.
    # Pour l'instant on documente : store idempotent + 2e ligne d'audit.
    assert resp2.status_code == 302  # BLOCKED Phase B: devrait etre 400 avec replay-cache
    assert store2.called  # idempotent upsert -- acceptable en Phase A
    assert audit2.called  # BLOCKED Phase B: la 2e ligne d'audit est indesirable
