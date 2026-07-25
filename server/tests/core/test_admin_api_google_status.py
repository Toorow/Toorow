"""Tests for Story 18.4 -- GET /api/google/oauth/status/{id} and
POST /api/google/oauth/revoke/{id}.

Coverage:
  - _derive_google_health: unit tests for health derivation logic.
  - GET /api/google/oauth/status/{id}: 200 (connected/not-connected), 401, 403,
    404, cross-project isolation (AD-5).
  - POST /api/google/oauth/revoke/{id}: 200 (revoke ok), 200 (best-effort-failed),
    200 (idempotent on already-clear), 401, 403 (cross-project AD-5), 404.
  - performed_by == real identity, never 'system' (review-18-1 F-6).
  - Seam ASGI via build_asgi_app (AI-56): assertions on values, not just HTTP 200.

Strategy:
  - All DB + google_token_store + httpx calls mocked -- no real Postgres.
  - SCHEDULER_ENABLED=false, HEALTH_POLLER_ENABLED=false.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Unit tests: _derive_google_health
# ---------------------------------------------------------------------------


class TestDeriveGoogleHealth:
    """Unit tests for the health derivation helper (no DB required)."""

    def _derive(self, token_expiry, auth_path):
        from core.admin_api import _derive_google_health

        return _derive_google_health(token_expiry, auth_path)

    def test_not_connected_when_nango_auth_path(self):
        """auth_path='nango' -> 'not_connected' regardless of expiry."""
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        assert self._derive(future, "nango") == "not_connected"

    def test_not_connected_when_empty_auth_path(self):
        assert self._derive(None, "") == "not_connected"

    def test_unknown_when_google_direct_and_no_expiry(self):
        """google_direct + no expiry -> 'unknown' (honest state, AD-9)."""
        assert self._derive(None, "google_direct") == "unknown"

    def test_ok_when_expiry_is_far_future(self):
        """Expiry > 5 min from now -> 'ok'."""
        future = datetime.now(tz=timezone.utc) + timedelta(hours=2)
        assert self._derive(future, "google_direct") == "ok"

    def test_stale_when_expiry_is_past(self):
        """Expired token -> 'stale' (refresh required)."""
        past = datetime.now(tz=timezone.utc) - timedelta(minutes=1)
        assert self._derive(past, "google_direct") == "stale"

    def test_stale_when_expiry_within_5_min(self):
        """Expiry <= 5 min -> 'stale' (imminent refresh)."""
        soon = datetime.now(tz=timezone.utc) + timedelta(minutes=2)
        assert self._derive(soon, "google_direct") == "stale"

    def test_ok_boundary_just_over_5_min(self):
        """Exactly 5 min 1 sec from now -> 'ok'."""
        near_future = datetime.now(tz=timezone.utc) + timedelta(seconds=301)
        assert self._derive(near_future, "google_direct") == "ok"

    def test_naive_datetime_treated_as_utc(self):
        """Naive datetime (from Postgres without tzinfo) is normalised to UTC."""
        # A naive far-future datetime should be treated as UTC -> ok
        naive_future = datetime.now() + timedelta(hours=2)
        assert naive_future.tzinfo is None
        result = self._derive(naive_future, "google_direct")
        assert result == "ok"


# ---------------------------------------------------------------------------
# Helpers: mock DB / app
# ---------------------------------------------------------------------------

# Horloge DYNAMIQUE (jamais figee: une date en dur devient fausse des minuit --
# bug attrape en verification centrale le 2026-07-19).
_NOW = datetime.now(tz=timezone.utc)
_FAR_FUTURE = _NOW + timedelta(hours=6)
_PAST = _NOW - timedelta(hours=1)

_CONN_COLS = ["project_id", "auth_path", "token_expiry", "granted_scopes"]


def _make_cursor_mock(fetchone_row=None):
    """Return a mock cursor whose fetchone returns the given row."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=fetchone_row)
    return cur


def _make_conn_mock(fetchone_row=None):
    """Return a mock psycopg connection that returns fetchone_row."""
    cur = _make_cursor_mock(fetchone_row)
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    return conn


@contextmanager
def _fake_db(fetchone_row=None):
    """Context manager that yields a mock connection with a fixed fetchone result."""
    conn = _make_conn_mock(fetchone_row)

    @contextmanager
    def _get():
        yield conn

    return _get()


def _build_client():
    from core.main import build_asgi_app
    from starlette.testclient import TestClient

    return TestClient(build_asgi_app(), raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers: project_access mock
# ---------------------------------------------------------------------------

def _patch_project_access(allowed: bool):
    """Patch identity_has_project_access to return *allowed*."""
    return patch(
        "core.admin_api.identity_has_project_access",  # the name used in admin_api.py
        return_value=allowed,
    )


# Actually core.project_access is imported inside the function; patch the
# module where it's referenced (inside admin_api).

def _patch_pa(allowed: bool):
    return patch("core.project_access.identity_has_project_access", return_value=allowed)


# ---------------------------------------------------------------------------
# GET /api/google/oauth/status/{connection_ref_id}
# ---------------------------------------------------------------------------


class TestGoogleStatus:
    """GET /api/google/oauth/status/{id} endpoint tests (AI-56 seam ASGI)."""

    def _row_connected(self, health_expiry=None):
        """Row for a google_direct connection with an optional expiry."""
        return (
            "proj_alpha",       # project_id
            "google_direct",    # auth_path
            health_expiry,      # token_expiry
            [
                "https://www.googleapis.com/auth/analytics.readonly",
                "https://www.googleapis.com/auth/webmasters.readonly",
            ],                  # granted_scopes
        )

    def _row_nango(self):
        return ("proj_alpha", "nango", None, None)

    def _patch_db(self, row):
        """Patch core.db.get_connection to return a mock yielding *row*."""
        cur = _make_cursor_mock(row)
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)

        @contextmanager
        def _get():
            yield conn

        return patch("core.db.get_connection", new=_get)

    def test_returns_401_when_no_auth(self):
        """Unauthenticated request -> 401 (auth mode test = disabled: on patche _check_auth)."""
        with patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))
        ):
            client = _build_client()
            resp = client.get("/api/google/oauth/status/conn_test001")
        assert resp.status_code == 401

    def test_returns_404_when_connection_not_found(self):
        """Unknown connection_ref_id -> 404."""
        with self._patch_db(None), patch(
            "core.project_access.identity_has_project_access", return_value=True
        ):
            client = _build_client()
            resp = client.get(
                "/api/google/oauth/status/conn_unknown",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 404
        data = resp.json()
        assert data["code"] == "not_found"

    def test_returns_403_cross_project(self):
        """Caller without access to the connection's project -> 403 + audit."""
        row = self._row_connected(_FAR_FUTURE)
        with self._patch_db(row), patch(
            "core.project_access.identity_has_project_access", return_value=False
        ), patch("core.admin_api.write_audit_row") as audit_mock:
            client = _build_client()
            resp = client.get(
                "/api/google/oauth/status/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 403
        data = resp.json()
        assert data["code"] == "forbidden"
        # Audit must have been called for cross-scope attempt.
        assert audit_mock.called

    def test_returns_status_connected_ok(self):
        """google_direct + far-future expiry -> health='ok', scopes present (AI-56)."""
        row = self._row_connected(_FAR_FUTURE)
        with self._patch_db(row), patch(
            "core.project_access.identity_has_project_access", return_value=True
        ):
            client = _build_client()
            resp = client.get(
                "/api/google/oauth/status/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        # AI-56: assert on values, not just HTTP 200.
        assert data["auth_path"] == "google_direct"
        assert data["health"] == "ok"
        assert data["token_expiry"] is not None
        # Scopes list contains labeled entries.
        scopes = data["granted_scopes"]
        assert len(scopes) == 2
        scope_uris = [s["scope"] for s in scopes]
        assert "https://www.googleapis.com/auth/analytics.readonly" in scope_uris
        # Labels must be French (UX-DR10).
        ga4_entry = next(
            s for s in scopes
            if s["scope"] == "https://www.googleapis.com/auth/analytics.readonly"
        )
        assert "Google Analytics" in ga4_entry["label"]
        # NFR3: no blob, no token in response.
        assert "blob" not in data
        assert "access_token" not in data
        assert "refresh_token" not in data

    def test_returns_status_not_connected(self):
        """auth_path='nango' -> health='not_connected', scopes empty."""
        row = self._row_nango()
        with self._patch_db(row), patch(
            "core.project_access.identity_has_project_access", return_value=True
        ):
            client = _build_client()
            resp = client.get(
                "/api/google/oauth/status/conn_nango_001",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_path"] == "nango"
        assert data["health"] == "not_connected"
        assert data["granted_scopes"] == []

    def test_returns_status_stale_when_expired(self):
        """google_direct + past expiry -> health='stale'."""
        row = self._row_connected(_PAST)
        with self._patch_db(row), patch(
            "core.project_access.identity_has_project_access", return_value=True
        ):
            client = _build_client()
            resp = client.get(
                "/api/google/oauth/status/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        assert resp.json()["health"] == "stale"

    def test_scope_label_unknown_scope_returns_raw_uri(self):
        """An unrecognised scope URI is returned as-is (no crash)."""
        row = (
            "proj_alpha",
            "google_direct",
            _FAR_FUTURE,
            ["https://www.googleapis.com/auth/unknown.scope"],
        )
        with self._patch_db(row), patch(
            "core.project_access.identity_has_project_access", return_value=True
        ):
            client = _build_client()
            resp = client.get(
                "/api/google/oauth/status/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 200
        data = resp.json()
        entry = data["granted_scopes"][0]
        # Unknown scope: label == scope URI.
        assert entry["label"] == entry["scope"]


# ---------------------------------------------------------------------------
# POST /api/google/oauth/revoke/{connection_ref_id}
# ---------------------------------------------------------------------------


class TestGoogleRevoke:
    """POST /api/google/oauth/revoke/{id} endpoint tests."""

    def _row_connected(self):
        return ("proj_alpha", "google_direct", _FAR_FUTURE, ["analytics.readonly"])

    def _row_nango(self):
        return ("proj_alpha", "nango", None, None)

    def _patch_db(self, row):
        cur = _make_cursor_mock(row)
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)

        @contextmanager
        def _get():
            yield conn

        return patch("core.db.get_connection", new=_get)

    def test_returns_401_when_no_auth(self):
        """Unauthenticated request -> 401 (auth mode test = disabled: on patche _check_auth)."""
        with patch(
            "core.admin_api._check_auth", new=AsyncMock(return_value=(False, None))
        ):
            client = _build_client()
            resp = client.post("/api/google/oauth/revoke/conn_test001")
        assert resp.status_code == 401

    def test_returns_404_when_not_found(self):
        """Unknown connection_ref_id -> 404."""
        with self._patch_db(None), patch(
            "core.project_access.identity_has_project_access", return_value=True
        ):
            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_unknown",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_returns_403_cross_project(self):
        """Caller without project access -> 403 + audit (AD-5)."""
        row = self._row_connected()
        with self._patch_db(row), patch(
            "core.project_access.identity_has_project_access", return_value=False
        ), patch("core.admin_api.write_audit_row") as audit_mock:
            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )
        assert resp.status_code == 403
        assert resp.json()["code"] == "forbidden"
        assert audit_mock.called

    def test_revoke_success_google_ok(self):
        """Happy path: Google revoke ok, local clear ok -> revoked=true (AI-56)."""
        row = self._row_connected()
        from core.google_token_store import GoogleToken

        mock_token = GoogleToken(
            access_token="fake_access",
            refresh_token="fake_refresh",
            token_expiry=_FAR_FUTURE,
            granted_scopes=[],
        )
        mock_response = MagicMock()
        mock_response.status_code = 200

        with (
            self._patch_db(row),
            patch("core.project_access.identity_has_project_access", return_value=True),
            # Patch at the source module (local import inside _google_revoke).
            patch(
                "core.google_token_store.load_google_token",
                return_value=mock_token,
            ),
            patch(
                "core.google_token_store.clear_google_token",
            ) as mock_clear,
            patch(
                "httpx.AsyncClient",
            ) as mock_httpx_cls,
        ):
            # Mock the async context manager for httpx.AsyncClient
            mock_httpx_inst = AsyncMock()
            mock_httpx_inst.post = AsyncMock(return_value=mock_response)
            mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_httpx_inst)
            mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )

        assert resp.status_code == 200
        data = resp.json()
        # AI-56: assertions on values.
        assert data["revoked"] is True
        assert data["connection_ref_id"] == "conn_alpha_001"
        assert data["google_revoke"] == "ok"
        # review-18-1 F-6: performed_by must NOT be 'system'.
        clear_call_kwargs = mock_clear.call_args
        performed_by_arg = (
            clear_call_kwargs.kwargs.get("performed_by")
            or (clear_call_kwargs.args[1] if len(clear_call_kwargs.args) > 1 else None)
        )
        assert performed_by_arg != "system", (
            "F-6: performed_by must be the real identity, never 'system'"
        )
        assert performed_by_arg is not None
        # review-18-5 fix-1 HIGH: token must be in FORM BODY, never in the URL
        # (query string leaks to logs/proxies -- security issue).
        post_call = mock_httpx_inst.post.call_args
        # keyword arg `data` must carry the token
        call_data = post_call.kwargs.get("data") or {}
        assert "token" in call_data, (
            "fix-1: token must be in form body (data=), never in query string (params=)"
        )
        assert call_data["token"] == "fake_access"
        # params kwarg must NOT carry the token
        call_params = post_call.kwargs.get("params") or {}
        assert "token" not in call_params, (
            "fix-1: token must NOT be in query string (params=) -- leaks to logs"
        )

    def test_revoke_best_effort_google_fails(self):
        """Google revoke endpoint fails -> best_effort_failed, local clear proceeds."""
        row = self._row_connected()
        from core.google_token_store import GoogleToken

        mock_token = GoogleToken(
            access_token="fake_access",
            refresh_token="fake_refresh",
            token_expiry=_FAR_FUTURE,
            granted_scopes=[],
        )
        mock_response = MagicMock()
        mock_response.status_code = 400  # Google refuses

        with (
            self._patch_db(row),
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch("core.google_token_store.load_google_token", return_value=mock_token),
            patch("core.google_token_store.clear_google_token"),
            patch("httpx.AsyncClient") as mock_httpx_cls,
        ):
            mock_httpx_inst = AsyncMock()
            mock_httpx_inst.post = AsyncMock(return_value=mock_response)
            mock_httpx_cls.return_value.__aenter__ = AsyncMock(return_value=mock_httpx_inst)
            mock_httpx_cls.return_value.__aexit__ = AsyncMock(return_value=False)

            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["revoked"] is True
        # google_revoke should indicate the failure (best-effort).
        assert data["google_revoke"] == "best_effort_failed"

    def test_revoke_idempotent_already_clear(self):
        """auth_path='nango' (no token) -> revoke skips Google call, still ok."""
        row = self._row_nango()
        with (
            self._patch_db(row),
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch("core.google_token_store.clear_google_token"),
        ):
            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_nango_001",
                headers={"Authorization": "Bearer test-secret"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["revoked"] is True
        # Google was NOT contacted (auth_path != google_direct).
        assert data["google_revoke"] == "skipped"

    def test_revoke_skipped_already_clear_when_no_blob(self):
        """review-18-5 fix-3: blob absent → skipped_already_clear (token never issued)."""
        from core.google_token_store import GoogleTokenStoreError

        row = self._row_connected()
        with (
            self._patch_db(row),
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch(
                "core.google_token_store.load_google_token",
                side_effect=GoogleTokenStoreError("no encrypted token blob (token redacted)"),
            ),
            patch("core.google_token_store.clear_google_token"),
        ):
            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["revoked"] is True
        # Blob absent → safe skip (token was never issued or already purged).
        assert data["google_revoke"] == "skipped_already_clear", (
            "fix-3: absent blob must yield 'skipped_already_clear', not 'skipped'"
        )

    def test_revoke_skipped_decrypt_failed_when_blob_corrupt(self):
        """review-18-5 fix-3: blob present but undecryptable → skipped_decrypt_failed
        (token may still be ACTIVE at Google — requires warning)."""
        from core.google_token_store import GoogleTokenStoreError

        row = self._row_connected()
        with (
            self._patch_db(row),
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch(
                "core.google_token_store.load_google_token",
                side_effect=GoogleTokenStoreError(
                    "cannot decrypt token blob: wrong key or tampered (token redacted)"
                ),
            ),
            patch("core.google_token_store.clear_google_token"),
        ):
            client = _build_client()
            resp = client.post(
                "/api/google/oauth/revoke/conn_alpha_001",
                headers={"Authorization": "Bearer test-secret"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["revoked"] is True
        # Blob present but unreadable → distinct status (token may be active at Google).
        assert data["google_revoke"] == "skipped_decrypt_failed", (
            "fix-3: corrupted/undecryptable blob must yield 'skipped_decrypt_failed' "
            "(token may still be ACTIVE at Google -- not safe to call 'skipped')"
        )

    def test_revoke_performed_by_is_real_identity(self):
        """F-6 (review-18-1): performed_by must be the authenticated identity."""
        row = self._row_nango()  # simple path: no token to revoke from Google
        captured: list[dict] = []

        def _capture_clear(connection_ref_id, performed_by="system", **kwargs):
            captured.append({"performed_by": performed_by})

        with (
            self._patch_db(row),
            patch("core.project_access.identity_has_project_access", return_value=True),
            patch("core.google_token_store.clear_google_token", side_effect=_capture_clear),
        ):
            client = _build_client()
            client.post(
                "/api/google/oauth/revoke/conn_nango_001",
                headers={"Authorization": "Bearer test-secret"},
            )

        assert len(captured) == 1, "clear_google_token must have been called"
        pb = captured[0]["performed_by"]
        assert pb != "system", (
            "F-6: performed_by on a human-triggered path must never be 'system'"
        )


# ---------------------------------------------------------------------------
# @pg_available live tests (AI-37): run only when PLATFORM_DB_URL is set.
# These prove the endpoints work against a real Postgres schema (029+).
# ---------------------------------------------------------------------------

_PLATFORM_DB_URL = os.getenv("PLATFORM_DB_URL", "")
_pg_available = pytest.mark.skipif(
    not _PLATFORM_DB_URL,
    reason="PLATFORM_DB_URL not set -- skipping live Postgres tests",
)


@_pg_available
class TestGoogleStatusLivePostgres:
    """Live Postgres tests for status/revoke (AI-37 pattern).

    These run only when PLATFORM_DB_URL is set (CI bring-up).
    They seed a connection_ref row, call the endpoint, and verify the response
    against the real schema (migration 029: auth_path, token_expiry, etc.).
    """

    def test_status_returns_nango_for_standard_row(self):
        """A standard Nango connection row -> health='not_connected' from live DB."""
        import psycopg

        conn_ref_id = "conn_18_4_live_test_001"
        db_url = _PLATFORM_DB_URL

        # Seed a minimal connection_ref row (Nango path, no Google columns).
        with psycopg.connect(db_url) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.connection_ref (id, provider, nango_connection_id, project_id)
                    VALUES (%s, 'gsc', %s, 'default')
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (conn_ref_id, conn_ref_id),
                )
            conn.commit()

        try:
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            client = TestClient(build_asgi_app(), raise_server_exceptions=False)
            resp = client.get(
                f"/api/google/oauth/status/{conn_ref_id}",
                headers={"Authorization": "Bearer test-secret"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["auth_path"] in ("nango", None, "")
            assert data["health"] == "not_connected"
            assert data["granted_scopes"] == []
        finally:
            # Cleanup.
            with psycopg.connect(db_url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "DELETE FROM app.connection_ref WHERE id = %s",
                        (conn_ref_id,),
                    )
                conn.commit()
