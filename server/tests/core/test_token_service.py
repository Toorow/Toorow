"""Tests pour server/core/token_service.py + le routage provider-aware de
``nango_client.get_fresh_token`` (Story 18.3, AD-21 / AD-2).

Familles de tests :

  (a) ROUTAGE (sans DB, mocks) :
      - une connexion ``auth_path='google_direct'`` est servie par le service
        Google direct (source resolue asseree -- AI-56, pas seulement absence
        d'erreur) ;
      - une connexion ``auth_path='nango'`` (ou aucune ligne / DB indisponible)
        reste servie par Nango (non-regression : le chemin async Nango est appele).

  (b) REFRESH :
      - token encore valide -> AUCUN refresh, l'access token stocke est rendu ;
      - token expire -> refresh via grant refresh_token, re-chiffre + persiste,
        audit ecrit, access token frais rendu ;
      - Google ``invalid_grant`` -> ``GoogleAuthExpired`` (code ``auth_expired``) ;
      - ZERO fuite : aucun token/refresh dans les logs (caplog durci
        ``_full_log_dump``) ni dans les messages d'erreur.

  (c) EXPIRY / HEALTH :
      - ``_is_expired_or_near`` (None, expire, proche, valide) ;
      - ``google_direct_health`` -> ok / stale / revoked (vocabulaire Nango).

  (d) NON-REGRESSION Nango : ``get_fresh_token`` sur une connexion non-google_direct
      appelle bien le chemin Nango existant (respx).

  (e) SEAM AI-56 : ``build_asgi_app`` importe le module et le routage est cable
      (assertions sur les valeurs de routage).

  (f) LIVE POSTGRES (@pg_available, AI-37) : chemin complet load->refresh->store
      contre la vraie DB (blob opaque + audit).

  (g) GREP anti-fuite : aucun token realiste en clair dans les fichiers livres.

Regles : NFR3 (zero token en clair), AD-2 (routage sur auth_path, pas de nom de
provider), AI-56 (seam avec valeurs), AI-37 (live pg), AI-03 (ASCII-only).
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# Placeholders manifestement factices -- JAMAIS de vrai token ici.
_FAKE_ACCESS = "tok_test_not_a_secret_access"
_FAKE_ACCESS_2 = "tok_test_not_a_secret_access_refreshed"
_FAKE_REFRESH = "tok_test_not_a_secret_refresh"
_FAKE_CLIENT_SECRET = "clientsecret_test_not_a_secret"


def _resolved(auth_path="google_direct", expiry=None, has_token_blob=True):
    from core.token_service import ResolvedConnection

    return ResolvedConnection(
        connection_ref_id="conn_test_123",
        project_id="proj_test",
        auth_path=auth_path,
        token_expiry=expiry,
        has_token_blob=has_token_blob,
    )


def _google_token(access=_FAKE_ACCESS, refresh=_FAKE_REFRESH, expiry=None, scopes=None):
    from core.google_token_store import GoogleToken

    return GoogleToken(
        access_token=access,
        refresh_token=refresh,
        token_expiry=expiry,
        granted_scopes=scopes or ["analytics.readonly"],
        metadata={},
    )


@pytest.fixture()
def oauth_env(monkeypatch):
    """Client OAuth de test (pour le grant refresh_token)."""
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "client-id-123.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", _FAKE_CLIENT_SECRET)
    monkeypatch.setenv(
        "GOOGLE_OAUTH_REDIRECT_URI",
        "https://console.example.com/api/google/oauth/callback",
    )
    yield


def _full_log_dump(caplog) -> str:
    """review-18-1 F-3: aggrege TOUTES les surfaces de fuite de chaque record."""
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


# ---------------------------------------------------------------------------
# (a) ROUTAGE -- google_direct vs nango (mocks, source resolue asseree AI-56)
# ---------------------------------------------------------------------------


def test_routing_google_direct_calls_google_service():
    """auth_path='google_direct' -> get_fresh_google_token (source resolue asseree)."""
    from core import nango_client

    with patch(
        "core.token_service.resolve_connection_by_nango_id",
        return_value=_resolved(auth_path="google_direct"),
    ), patch(
        "core.token_service.get_fresh_google_token", return_value=_FAKE_ACCESS
    ) as mock_google, patch(
        "core.nango_client._run_coro"
    ) as mock_nango:
        tok = nango_client.get_fresh_token("nango_conn_abc", provider="google-analytics")

    assert tok == _FAKE_ACCESS
    # AI-56 : la source resolue est bien le service Google direct, pas Nango.
    mock_google.assert_called_once()
    _, kwargs = mock_google.call_args
    assert kwargs.get("identity") == "system"  # contexte systeme pour les pulls
    mock_nango.assert_not_called()


def test_routing_nango_when_auth_path_nango():
    """auth_path='nango' -> chemin Nango inchange (non-regression)."""
    from core import nango_client

    with patch(
        "core.token_service.resolve_connection_by_nango_id",
        return_value=_resolved(auth_path="nango"),
    ), patch(
        "core.token_service.get_fresh_google_token"
    ) as mock_google, patch(
        "core.nango_client._run_coro", return_value=_FAKE_ACCESS
    ) as mock_nango:
        tok = nango_client.get_fresh_token("nango_conn_abc", provider="meta-ads")

    assert tok == _FAKE_ACCESS
    mock_google.assert_not_called()
    mock_nango.assert_called_once()  # AI-56 : la source resolue est Nango.


def test_routing_no_row_defaults_to_nango():
    """Aucune ligne connection_ref (ou DB pre-029) -> Nango (backward compat)."""
    from core import nango_client

    with patch(
        "core.token_service.resolve_connection_by_nango_id", return_value=None
    ), patch(
        "core.token_service.get_fresh_google_token"
    ) as mock_google, patch(
        "core.nango_client._run_coro", return_value=_FAKE_ACCESS
    ) as mock_nango:
        tok = nango_client.get_fresh_token("unknown_conn", provider="shopify")

    assert tok == _FAKE_ACCESS
    mock_google.assert_not_called()
    mock_nango.assert_called_once()


# ---------------------------------------------------------------------------
# (b) REFRESH -- valide / expire / invalid_grant / zero fuite
# ---------------------------------------------------------------------------


def test_valid_token_is_returned_without_refresh():
    """Un token encore valide (expiry lointaine) est rendu tel quel, sans refresh."""
    from core import token_service

    future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
    with patch(
        "core.google_token_store.load_google_token",
        return_value=_google_token(expiry=future),
    ), patch("core.google_token_store.store_google_token") as mock_store, patch(
        "core.token_service._refresh_google_access_token"
    ) as mock_refresh:
        tok = token_service.get_fresh_google_token(_resolved(expiry=future))

    assert tok == _FAKE_ACCESS
    mock_refresh.assert_not_called()
    mock_store.assert_not_called()  # pas de re-persistance si pas de refresh


def test_expired_token_triggers_refresh_and_persist(oauth_env):
    """Un token expire -> refresh via grant refresh_token, re-persiste + audit."""
    import respx
    from core import token_service
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT
    from httpx import Response

    past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)

    with respx.mock:
        route = respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
            return_value=Response(
                200,
                json={
                    "access_token": _FAKE_ACCESS_2,
                    "expires_in": 3600,
                    "scope": "https://www.googleapis.com/auth/analytics.readonly",
                    "token_type": "Bearer",
                },
            )
        )
        with patch(
            "core.google_token_store.load_google_token",
            return_value=_google_token(expiry=past),
        ), patch(
            "core.google_token_store.store_google_token"
        ) as mock_store, patch(
            "core.token_service._audit_refresh"
        ) as mock_audit:
            tok = token_service.get_fresh_google_token(_resolved(expiry=past))

    assert route.called
    assert tok == _FAKE_ACCESS_2  # l'access token FRAIS est rendu
    # Le refresh grant a bien ete envoye.
    sent = route.calls.last.request
    assert b"grant_type=refresh_token" in sent.content
    # Re-persistance chiffree avec le nouvel access token + refresh existant conserve.
    mock_store.assert_called_once()
    args, kwargs = mock_store.call_args
    assert args[0] == "conn_test_123"
    assert args[1]["access_token"] == _FAKE_ACCESS_2
    assert args[1]["refresh_token"] == _FAKE_REFRESH  # Google a omis -> on conserve
    assert kwargs.get("expected_project_id") == "proj_test"
    mock_audit.assert_called_once()  # audit de refresh (AD-14)


def test_invalid_grant_raises_auth_expired(oauth_env):
    """Google invalid_grant (refresh_token revoque) -> GoogleAuthExpired (auth_expired)."""
    import respx
    from core import token_service
    from core.constants import AUTH_EXPIRED_CODE
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT
    from httpx import Response

    past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)

    with respx.mock:
        respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
            return_value=Response(400, json={"error": "invalid_grant"})
        )
        with patch(
            "core.google_token_store.load_google_token",
            return_value=_google_token(expiry=past),
        ), patch("core.google_token_store.store_google_token") as mock_store:
            with pytest.raises(token_service.GoogleAuthExpired) as exc_info:
                token_service.get_fresh_google_token(_resolved(expiry=past))

    assert exc_info.value.code == AUTH_EXPIRED_CODE
    mock_store.assert_not_called()  # rien persiste sur echec d'auth


def test_empty_refresh_token_raises_auth_expired():
    """Un refresh_token vide stocke ne peut jamais rafraichir -> auth_expired."""
    from core import token_service

    past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    with patch(
        "core.google_token_store.load_google_token",
        return_value=_google_token(refresh="", expiry=past),
    ):
        with pytest.raises(token_service.GoogleAuthExpired):
            token_service.get_fresh_google_token(_resolved(expiry=past))


def test_no_token_leak_in_logs_on_refresh(oauth_env, caplog):
    """Aucun token (access/refresh) n'apparait dans les logs pendant un refresh."""
    import respx
    from core import token_service
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT
    from httpx import Response

    past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)

    with caplog.at_level(logging.DEBUG):
        with respx.mock:
            respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
                return_value=Response(
                    200, json={"access_token": _FAKE_ACCESS_2, "expires_in": 3600}
                )
            )
            with patch(
                "core.google_token_store.load_google_token",
                return_value=_google_token(expiry=past),
            ), patch("core.google_token_store.store_google_token"), patch(
                "core.token_service._audit_refresh"
            ):
                token_service.get_fresh_google_token(_resolved(expiry=past))

    joined = _full_log_dump(caplog)
    assert _FAKE_ACCESS not in joined
    assert _FAKE_ACCESS_2 not in joined
    assert _FAKE_REFRESH not in joined


def test_refresh_reduced_scopes_persists_intersection(oauth_env):
    """review-18-5 MEDIUM fix-2 (AD-9): when Google returns non-empty but REDUCED scopes
    on refresh, only the intersection is persisted -- never preserve scopes the new
    token no longer carries."""
    import respx
    from core import token_service
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT
    from httpx import Response

    past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    stored_scopes = [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ]
    # Google refresh returns only analytics scope -- webmasters has been revoked.
    refreshed_scopes_str = "https://www.googleapis.com/auth/analytics.readonly"

    persisted_scopes_captured: list[list[str] | None] = []

    def _capture_store(_conn_id, _payload, _expiry, scopes, **kwargs):
        persisted_scopes_captured.append(list(scopes) if scopes else None)

    with respx.mock:
        respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
            return_value=Response(
                200,
                json={
                    "access_token": _FAKE_ACCESS_2,
                    "expires_in": 3600,
                    "scope": refreshed_scopes_str,  # reduced scopes
                    "token_type": "Bearer",
                },
            )
        )
        with patch(
            "core.google_token_store.load_google_token",
            return_value=_google_token(expiry=past, scopes=stored_scopes),
        ), patch(
            "core.google_token_store.store_google_token",
            side_effect=_capture_store,
        ), patch("core.token_service._audit_refresh"):
            token_service.get_fresh_google_token(_resolved(expiry=past))

    assert len(persisted_scopes_captured) == 1
    persisted = persisted_scopes_captured[0]
    assert persisted is not None
    # webmasters scope must NOT be persisted (Google removed it from the refresh response)
    assert "https://www.googleapis.com/auth/webmasters.readonly" not in persisted, (
        "fix-2 AD-9: a scope not in the refreshed token must be dropped from the stored "
        "scopes, never preserved"
    )
    # analytics scope must still be present (it's in both stored and refreshed)
    assert "https://www.googleapis.com/auth/analytics.readonly" in persisted


def test_refresh_empty_scopes_preserves_stored_scopes(oauth_env):
    """review-18-5 fix-2: when Google OMITS scopes on refresh (empty list), the stored
    scopes are preserved unchanged (Google means 'same as before')."""
    import respx
    from core import token_service
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT
    from httpx import Response

    past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
    stored_scopes = [
        "https://www.googleapis.com/auth/analytics.readonly",
        "https://www.googleapis.com/auth/webmasters.readonly",
    ]

    persisted_scopes_captured: list[list[str] | None] = []

    def _capture_store(_conn_id, _payload, _expiry, scopes, **kwargs):
        persisted_scopes_captured.append(list(scopes) if scopes else None)

    with respx.mock:
        # No 'scope' field in response -- Google says "same as before"
        respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
            return_value=Response(
                200,
                json={
                    "access_token": _FAKE_ACCESS_2,
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    # no 'scope' key
                },
            )
        )
        with patch(
            "core.google_token_store.load_google_token",
            return_value=_google_token(expiry=past, scopes=stored_scopes),
        ), patch(
            "core.google_token_store.store_google_token",
            side_effect=_capture_store,
        ), patch("core.token_service._audit_refresh"):
            token_service.get_fresh_google_token(_resolved(expiry=past))

    assert len(persisted_scopes_captured) == 1
    persisted = persisted_scopes_captured[0]
    # When Google omits scope, stored scopes must be preserved unchanged.
    assert persisted == stored_scopes, (
        "fix-2: when Google omits scope on refresh, stored scopes must be preserved"
    )


def test_refresh_transport_error_is_redacted(oauth_env):
    """Une erreur transport httpx est redigee (type only, jamais le refresh_token)."""
    import httpx
    from core import token_service
    from core.google_oauth import GoogleOAuthError

    def _boom(*a, **k):
        raise httpx.ConnectError(f"boom {_FAKE_REFRESH}")  # secret dans la cause

    with patch("httpx.post", _boom):
        with pytest.raises(GoogleOAuthError) as exc_info:
            token_service._refresh_google_access_token(_FAKE_REFRESH)

    assert _FAKE_REFRESH not in str(exc_info.value)
    assert "redacted" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# (c) EXPIRY / HEALTH
# ---------------------------------------------------------------------------


def test_is_expired_or_near():
    from core.token_service import _is_expired_or_near

    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
    assert _is_expired_or_near(None, now=now) is True  # expiry inconnue -> refresh
    assert _is_expired_or_near(now - timedelta(minutes=5), now=now) is True  # expire
    assert _is_expired_or_near(now + timedelta(seconds=60), now=now) is True  # dans le skew
    assert _is_expired_or_near(now + timedelta(hours=1), now=now) is False  # valide


def test_google_direct_health_ok_stale_revoked():
    from core.nango_client import ConnectionHealth
    from core.token_service import google_direct_health

    now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)

    # ok : expiry lointaine
    h_ok = google_direct_health(_resolved(expiry=now + timedelta(hours=1)), now=now)
    assert isinstance(h_ok, ConnectionHealth)
    assert h_ok.status == "ok"

    # stale : dans le skew (refresh du), blob present
    h_stale = google_direct_health(_resolved(expiry=now + timedelta(seconds=60)), now=now)
    assert h_stale.status == "stale"

    # stale : blob present mais expiry inconnue (refresh du, pas revoked)
    h_unknown = google_direct_health(_resolved(expiry=None, has_token_blob=True), now=now)
    assert h_unknown.status == "stale"

    # revoked : pas de blob local (purge / jamais emis)
    h_rev = google_direct_health(_resolved(expiry=None, has_token_blob=False), now=now)
    assert h_rev.status == "revoked"


def test_poll_connection_health_routes_google_direct():
    """poll_connection_health route aussi sur auth_path (health provider-aware)."""
    from core import nango_client

    now = datetime.now(tz=timezone.utc)
    with patch(
        "core.token_service.resolve_connection_by_nango_id",
        return_value=_resolved(expiry=now + timedelta(hours=1)),
    ), patch("core.nango_client._run_coro") as mock_nango:
        health = nango_client.poll_connection_health("nango_conn_abc", provider="gsc")

    assert health.status == "ok"
    mock_nango.assert_not_called()  # pas de polling Nango pour google_direct


def test_poll_connection_health_nango_unchanged():
    """poll_connection_health sur une connexion nango garde le polling Nango."""
    from core import nango_client
    from core.nango_client import ConnectionHealth

    with patch(
        "core.token_service.resolve_connection_by_nango_id",
        return_value=_resolved(auth_path="nango"),
    ), patch(
        "core.nango_client._run_coro",
        return_value=ConnectionHealth(status="ok", last_fetched_at=None),
    ) as mock_nango:
        health = nango_client.poll_connection_health("nango_conn_abc", provider="meta-ads")

    assert health.status == "ok"
    mock_nango.assert_called_once()


# ---------------------------------------------------------------------------
# (d) NON-REGRESSION Nango -- le chemin async Nango reste intact
# ---------------------------------------------------------------------------


def test_nango_path_unchanged_end_to_end():
    """get_fresh_token sur une connexion nango appelle le vrai chemin async Nango."""
    import respx
    from core import nango_client
    from httpx import Response

    base = "http://nango-test.local"
    with patch.dict(
        os.environ, {"NANGO_BASE_URL": base, "NANGO_SECRET_KEY": "test-secret"}
    ):
        with respx.mock:
            # La resolution renvoie une ligne nango -> chemin Nango.
            respx.get(f"{base}/connection/nango_conn_xyz").mock(
                return_value=Response(
                    200, json={"credentials": {"access_token": _FAKE_ACCESS}}
                )
            )
            with patch(
                "core.token_service.resolve_connection_by_nango_id",
                return_value=_resolved(auth_path="nango"),
            ):
                tok = nango_client.get_fresh_token("nango_conn_xyz", provider="meta-ads")

    assert tok == _FAKE_ACCESS  # le token vient bien de Nango


# ---------------------------------------------------------------------------
# (e) SEAM AI-56 -- build_asgi_app importe le module + routage cable
# ---------------------------------------------------------------------------


def test_seam_token_service_importable_and_routing_constants():
    """AI-56 : le module se charge et expose les constantes de routage (valeurs)."""
    from core import token_service

    assert token_service.AUTH_PATH_GOOGLE_DIRECT == "google_direct"
    assert token_service.AUTH_PATH_NANGO == "nango"
    # get_fresh_token importe token_service sans cycle (le seam charge le core).
    from core.nango_client import get_fresh_token  # noqa: F401


# ---------------------------------------------------------------------------
# (f) LIVE POSTGRES (@pg_available, AI-37) -- chemin complet contre la vraie DB
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


def _pg_env(monkeypatch, tmp_key_dir):
    dsn = os.environ["TEST_POSTGRES_DSN"]
    monkeypatch.setenv("PLATFORM_DB_URL", dsn)
    monkeypatch.setenv("TENANT_KEY_BACKEND", "local")
    monkeypatch.setenv("TENANT_KEY_DIR", tmp_key_dir)


def _seed(dsn, project_id, conn_id, nango_id, auth_path):
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.projects (id, name, slug, status, created_by)
                VALUES (%s, %s, %s, 'active', 'test')
                ON CONFLICT (id) DO NOTHING
                """,
                (project_id, f"ts-{project_id}", f"slug-{uuid.uuid4().hex[:8]}"),
            )
            cur.execute(
                """
                INSERT INTO app.connection_ref
                    (id, provider, nango_connection_id, project_id, auth_path)
                VALUES (%s, 'google-analytics', %s, %s, %s)
                """,
                (conn_id, nango_id, project_id, auth_path),
            )
        conn.commit()


def _cleanup(dsn, project_id, conn_id):
    import psycopg  # noqa: PLC0415

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            # app.audit_log append-only (FR12) + FK vers connection_ref : une
            # connexion auditee n'est pas hard-deletable (prod = soft-delete).
            # Cleanup BEST-EFFORT : les entites de test auditees restent.
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
def test_live_resolve_by_nango_id(monkeypatch, tmp_path):
    """resolve_connection_by_nango_id lit auth_path/id/project sur la vraie DB."""
    _pg_env(monkeypatch, str(tmp_path / "keys"))
    from core.token_service import resolve_connection_by_nango_id

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    nid = f"nango_{uuid.uuid4().hex[:12]}"
    try:
        _seed(dsn, pid, cid, nid, "google_direct")
        resolved = resolve_connection_by_nango_id(nid)
        assert resolved is not None
        assert resolved.connection_ref_id == cid
        assert resolved.project_id == pid
        assert resolved.auth_path == "google_direct"
    finally:
        _cleanup(dsn, pid, cid)


@pg_available
def test_live_full_refresh_path(monkeypatch, tmp_path, oauth_env):
    """Chemin complet : store token expire -> get_fresh_token refresh -> re-persiste."""
    import respx
    from httpx import Response

    _pg_env(monkeypatch, str(tmp_path / "keys"))
    from core import nango_client
    from core.google_oauth import GOOGLE_TOKEN_ENDPOINT
    from core.google_token_store import store_google_token

    dsn = os.environ["TEST_POSTGRES_DSN"]
    pid = f"proj_{uuid.uuid4().hex[:12]}"
    cid = f"conn_{uuid.uuid4().hex[:12]}"
    nid = f"nango_{uuid.uuid4().hex[:12]}"
    try:
        _seed(dsn, pid, cid, nid, "nango")
        past = datetime.now(tz=timezone.utc) - timedelta(minutes=10)
        # Persiste un token expire (met aussi auth_path='google_direct').
        store_google_token(
            cid,
            {"access_token": _FAKE_ACCESS, "refresh_token": _FAKE_REFRESH},
            past,
            ["analytics.readonly"],
        )
        with respx.mock:
            respx.post(GOOGLE_TOKEN_ENDPOINT).mock(
                return_value=Response(
                    200, json={"access_token": _FAKE_ACCESS_2, "expires_in": 3600}
                )
            )
            # get_fresh_token doit router google_direct, refresh, et rendre le token frais.
            tok = nango_client.get_fresh_token(nid, provider="google-analytics")
        assert tok == _FAKE_ACCESS_2

        # Le blob au repos reste opaque + un audit de refresh est ecrit.
        import psycopg  # noqa: PLC0415

        with psycopg.connect(dsn) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT encrypted_token_blob FROM app.connection_ref WHERE id = %s",
                    (cid,),
                )
                (blob,) = cur.fetchone()
                assert blob is not None
                raw = bytes(blob)
                assert _FAKE_ACCESS_2.encode() not in raw  # opaque
                assert _FAKE_REFRESH.encode() not in raw
                cur.execute(
                    "SELECT action FROM app.audit_log WHERE connection_ref = %s", (cid,)
                )
                actions = [r[0] for r in cur.fetchall()]
                assert "connection.created" in actions  # audit de refresh (emission)
    finally:
        _cleanup(dsn, pid, cid)


# ---------------------------------------------------------------------------
# (g) GREP anti-fuite -- aucun token realiste en clair dans les fichiers livres
# ---------------------------------------------------------------------------


def test_no_realistic_token_plaintext_in_delivered_files():
    """Aucun fichier livre par 18.3 ne contient de token realiste en clair."""
    import re

    repo_root = Path(__file__).resolve().parents[3]
    delivered = [
        repo_root / "server" / "core" / "token_service.py",
        repo_root / "server" / "core" / "nango_client.py",
        repo_root / "server" / "tests" / "core" / "test_token_service.py",
        repo_root / "_bmad-output" / "implementation-artifacts"
        / "18-3-provider-aware-token-service.md",
    ]
    forbidden = [
        re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"),
        re.compile(r"1//[A-Za-z0-9_\-]{20,}"),
        re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),
        re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    ]
    for path in delivered:
        assert path.exists(), f"fichier de story manquant : {path}"
        text = path.read_text(encoding="utf-8")
        for pat in forbidden:
            assert pat.search(text) is None, (
                f"token realiste en clair detecte dans {path.name} (motif {pat.pattern!r})"
            )
        if "tok_test" in text:
            assert "not_a_secret" in text, f"placeholder ambigu dans {path.name}"
