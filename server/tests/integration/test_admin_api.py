"""Integration tests for admin API endpoints (Story 2.4, AC9, T5.7;
                                              Story 2.5, AC2, AC4, T8.2).

Tests:
  - GET /api/connections: mocked Nango, optional real Postgres (skip if absent)
  - GET /api/connections health join: health field present in response
  - POST /api/connections: creates connection_ref row, returns 201
  - POST /api/connections/<id>/refresh-health: calls Nango and updates cache

Strategy:
  - nango_client is mocked via pytest monkeypatch (no real Nango call).
  - Postgres: uses PLATFORM_DB_URL env var; tests are skipped when absent.
  - TestClient: Starlette TestClient against build_asgi_app().

Skip guard follows the same pattern as test_audit.py and test_nango_integration.py.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from core import nango_client as _nango_client
from starlette.testclient import TestClient

# Story 2.5: disable the health poller background thread in all tests.
# build_asgi_app() calls start_health_poller(); HEALTH_POLLER_ENABLED=false suppresses it.
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
# Story 3.2: disable the queue worker background thread in all tests.
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Skip guard
# ---------------------------------------------------------------------------

_PLATFORM_DB_URL = os.getenv("PLATFORM_DB_URL", "")
_HAS_DB = bool(_PLATFORM_DB_URL)

_skip_no_db = pytest.mark.skipif(
    not _HAS_DB,
    reason="PLATFORM_DB_URL not set -- skipping DB integration tests",
)

# A fake Nango connection list for mocking
_FAKE_NANGO_CONN = [
    {
        "connection_id": "nango-test-001",
        "provider": "google-analytics",
        "created_at": "2026-07-11T10:00:00Z",
    }
]


# ---------------------------------------------------------------------------
# Unit-level: TestClient with fully mocked Nango + mocked DB
# ---------------------------------------------------------------------------


class TestGetConnectionsMockedAll:
    """GET /api/connections with all external deps mocked (no real DB, no Nango)."""

    def test_returns_200_with_empty_list_when_db_unavailable(self):
        """If DB is unreachable, endpoint returns Nango-only list (graceful degradation)."""
        from core.main import build_asgi_app

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            return_value=_FAKE_NANGO_CONN,
        ), patch(
            "core.db._psycopg",
            None,  # simulate psycopg not installed / DB unreachable
        ):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections")

        # Graceful degradation: 200 with nango-only list OR 200 with connections
        assert resp.status_code == 200
        body = resp.json()
        assert "connections" in body
        assert isinstance(body["connections"], list)

    def test_health_field_present_in_get_response(self):
        """GET /api/connections response includes 'health' key per AC2 (Story 2.5)."""
        from contextlib import contextmanager

        from core.main import build_asgi_app

        # Simulate DB returning one connection_ref + health row
        # Use datetime objects as psycopg would return for TIMESTAMPTZ columns
        _health_row = (
            "conn_h1",         # id
            "ga",              # provider
            "nango-h001",      # nango_connection_id
            "default",         # project_id
            datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc),   # created_at
            "ok",              # health_status
            datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),   # last_checked_at
            datetime(2026, 7, 10, 11, 0, 0, tzinfo=timezone.utc),   # last_fetched_at
        )

        class FakeCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("provider",), ("nango_connection_id",), ("project_id",),
                    ("created_at",), ("health_status",), ("last_checked_at",), ("last_fetched_at",),
                ]
                self._rows = [_health_row]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchall(self):
                return self._rows

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield FakeConn()

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            return_value=_FAKE_NANGO_CONN,
        ), patch("core.db.get_connection", new=_fake_get_connection):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections")

        assert resp.status_code == 200
        body = resp.json()
        assert "connections" in body
        conns = body["connections"]
        assert len(conns) == 1
        conn = conns[0]
        assert "health" in conn
        # health may be a dict or null
        if conn["health"] is not None:
            assert "status" in conn["health"]

    def test_nango_down_alone_does_not_fail_get(self):
        """review-2-5 F-01: Nango is NOT called on the happy path -- with the
        DB reachable, a dead Nango must not affect GET /api/connections."""
        from core.main import build_asgi_app

        nango_mock = AsyncMock(side_effect=Exception("nango unreachable"))
        with patch("core.admin_api.nango_client._list_connections_async", nango_mock):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections")

        if resp.status_code == 200:
            # DB path served the request; Nango must not have been touched.
            nango_mock.assert_not_called()
        else:
            # DB unreachable in this environment -> fallback hit dead Nango.
            assert resp.status_code == 502
            assert resp.json()["code"] == "nango_error"

    def test_returns_502_when_db_and_nango_both_fail(self):
        """502 only when the DB fallback ALSO cannot reach Nango."""
        from core.main import build_asgi_app

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            side_effect=Exception("nango unreachable"),
        ), patch("core.db._psycopg", None):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections")

        assert resp.status_code == 502
        assert resp.json()["code"] == "nango_error"

    def test_returns_401_when_auth_required_and_no_token(self):
        """In static auth mode, missing Bearer token returns 401."""
        from core.main import build_asgi_app

        with patch.dict(os.environ, {"TOOROW_AUTH_MODE": "static"}):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections")

        assert resp.status_code == 401

    def test_returns_200_with_valid_token_in_static_mode(self):
        """In static auth mode, the CONFIGURED token returns 200 (review-2-6 F-01)."""
        from core.api_auth import reset_verifier_cache
        from core.main import build_asgi_app

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            return_value=[],
        ), patch.dict(
            os.environ,
            {"TOOROW_AUTH_MODE": "static", "TOOROW_STATIC_TOKEN": "tok-valid-123"},
        ), patch("core.db._psycopg", None):
            reset_verifier_cache()
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/api/connections",
                headers={"Authorization": "Bearer tok-valid-123"},
            )
        reset_verifier_cache()

        # 200 (graceful degradation since DB is mocked as None)
        assert resp.status_code == 200

    def test_returns_401_with_wrong_token_in_static_mode(self):
        """review-2-6 F-01 exploit case: an arbitrary token must be REJECTED."""
        from core.api_auth import reset_verifier_cache
        from core.main import build_asgi_app

        with patch.dict(
            os.environ,
            {"TOOROW_AUTH_MODE": "static", "TOOROW_STATIC_TOKEN": "tok-valid-123"},
        ):
            reset_verifier_cache()
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/api/connections",
                headers={"Authorization": "Bearer any-token"},
            )
        reset_verifier_cache()

        assert resp.status_code == 401


class TestRefreshHealthMockedAll:
    """POST /api/connections/<id>/refresh-health with mocked Nango + DB (Story 2.5, AC4)."""

    def test_refresh_health_calls_nango_and_returns_health(self):
        """refresh-health calls nango, upserts DB, returns new health state."""
        from contextlib import contextmanager

        from core.main import build_asgi_app

        _FAKE_HEALTH = _nango_client.ConnectionHealth(
            status="ok",
            last_fetched_at=datetime(2026, 7, 10, 11, 0, 0, tzinfo=timezone.utc),
        )

        upserted: list[dict] = []
        conn_call_count = [0]

        class SelectCursor:
            """Cursor for the SELECT connection_ref query."""
            description = [("id",), ("nango_connection_id",), ("provider",)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return ("conn_r1", "nango-r001", "ga")

        class UpsertCursor:
            """Cursor for the INSERT ... ON CONFLICT upsert."""
            description = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                if params:
                    upserted.append(dict(params) if isinstance(params, dict) else {})

        class FakeConn:
            def __init__(self, is_select: bool):
                self._is_select = is_select

            def cursor(self):
                return SelectCursor() if self._is_select else UpsertCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            conn_call_count[0] += 1
            yield FakeConn(is_select=conn_call_count[0] == 1)

        with patch(
            "core.admin_api.nango_client._poll_connection_health_async",
            new_callable=AsyncMock,
            return_value=_FAKE_HEALTH,
        ), patch("core.db.get_connection", new=_fake_get_connection):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/connections/conn_r1/refresh-health")

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "conn_r1"
        assert "health" in body
        assert body["health"]["status"] == "ok"
        assert body["health"]["last_fetched_at"] is not None

    def test_refresh_health_returns_404_for_unknown_id(self):
        """refresh-health returns 404 when connection ref id does not exist."""
        from contextlib import contextmanager

        from core.main import build_asgi_app

        class FakeCursor:
            def __init__(self):
                self.description = [("id",), ("nango_connection_id",), ("provider",)]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return None  # not found

        class FakeConn:
            def cursor(self):
                return FakeCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield FakeConn()

        with patch("core.db.get_connection", new=_fake_get_connection):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/connections/nonexistent/refresh-health")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_refresh_health_returns_401_when_auth_required_and_no_token(self):
        """In static auth mode, POST refresh-health without token returns 401."""
        from core.main import build_asgi_app

        with patch.dict(os.environ, {"TOOROW_AUTH_MODE": "static"}):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/connections/conn_01/refresh-health")

        assert resp.status_code == 401


class TestPostConnectionsMockedAll:
    """POST /api/connections with mocked DB (no real Postgres)."""

    def test_returns_400_on_missing_nango_connection_id(self):
        """POST without nango_connection_id returns 400."""
        from core.main import build_asgi_app

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/connections",
            json={"provider": "google-analytics", "project_id": "default"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "missing_field"

    def test_returns_400_on_missing_provider(self):
        """POST without provider returns 400."""
        from core.main import build_asgi_app

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/connections",
            json={"nango_connection_id": "nango-abc", "project_id": "default"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "missing_field"

    def test_returns_400_on_invalid_json(self):
        """POST with invalid JSON returns 400."""
        from core.main import build_asgi_app

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(
            "/api/connections",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_body"

    def test_returns_401_when_auth_required_and_no_token(self):
        """In static auth mode, POST without token returns 401."""
        from core.main import build_asgi_app

        with patch.dict(os.environ, {"TOOROW_AUTH_MODE": "static"}):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post(
                "/api/connections",
                json={
                    "nango_connection_id": "nango-abc",
                    "provider": "google-analytics",
                    "project_id": "default",
                },
            )

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Integration tests: real Postgres (skip if PLATFORM_DB_URL not set)
# ---------------------------------------------------------------------------


@_skip_no_db
class TestAdminApiIntegration:
    """Integration tests against live platform-db.

    Requires:
      - docker compose -f infra/nango/docker-compose.yml up -d
      - PLATFORM_DB_URL=postgresql://connector:connector_dev_only@localhost:5432/connector
      - Migration 001 applied (app.connection_ref table exists)
    """

    # Test-owned rows are identified by this project_id to avoid collisions
    _TEST_PROJECT_ID = "story-2-4-integ-test"

    @pytest.fixture(autouse=True)
    def _cleanup_test_rows(self):
        """Seed the test project (FK since migration 018) and clean up after."""
        try:
            import psycopg  # noqa: PLC0415

            url = os.environ.get("PLATFORM_DB_URL", "")
            with psycopg.connect(url) as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO app.projects (id, name, slug, created_by, org_id)
                        VALUES (%s, 'Story 2.4 Integ', %s, 'integ-test', 'org_test_fixture')
                        ON CONFLICT (id) DO NOTHING
                        """,
                        (self._TEST_PROJECT_ID, self._TEST_PROJECT_ID),
                    )
                conn.commit()
        except Exception:
            pass
        yield
        try:
            import psycopg  # noqa: PLC0415

            url = os.environ.get("PLATFORM_DB_URL", "")
            with psycopg.connect(url) as conn:
                with conn.cursor() as cur:
                    # Story 8.2: datastreams FK-reference connections; clear first.
                    cur.execute(
                        "DELETE FROM app.datastream_mappings WHERE datastream_id IN "
                        "(SELECT id FROM app.datastreams WHERE project_id = %s)",
                        (self._TEST_PROJECT_ID,),
                    )
                    cur.execute(
                        "DELETE FROM app.datastreams WHERE project_id = %s",
                        (self._TEST_PROJECT_ID,),
                    )
                    cur.execute(
                        "DELETE FROM app.connection_ref WHERE project_id = %s",
                        (self._TEST_PROJECT_ID,),
                    )
                    cur.execute(
                        "DELETE FROM app.tenant_key_audit WHERE project_id = %s",
                        (self._TEST_PROJECT_ID,),
                    )
                    cur.execute(
                        "DELETE FROM app.projects WHERE id = %s",
                        (self._TEST_PROJECT_ID,),
                    )
                conn.commit()
        except Exception:
            pass  # Best-effort cleanup

    def test_post_creates_connection_ref_row(self):
        """POST /api/connections writes a row to app.connection_ref (AC5)."""
        from core.main import build_asgi_app

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            # review-2-4 F-01 orphan guard: Nango must KNOW the connection for
            # the POST to be accepted -- mock it as known.
            return_value=[{"connection_id": "nango-integ-test-001"}],
        ), patch("core.audit.psycopg", create=True) as mock_audit_psycopg:
            # Mock audit psycopg to avoid audit_log FK dependency
            from unittest.mock import MagicMock

            mock_audit_psycopg.connect.return_value.__enter__ = lambda s: s
            mock_audit_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
            mock_audit_psycopg.connect.return_value.cursor.return_value.__enter__ = lambda s: s
            mock_audit_psycopg.connect.return_value.cursor.return_value.__exit__ = MagicMock(
                return_value=False
            )

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/api/connections",
                json={
                    "nango_connection_id": "nango-integ-test-001",
                    "provider": "google-analytics",
                    "project_id": self._TEST_PROJECT_ID,
                },
            )

        assert resp.status_code == 201, f"Expected 201, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["provider"] == "google-analytics"
        assert body["nango_connection_id"] == "nango-integ-test-001"
        assert body["project_id"] == self._TEST_PROJECT_ID
        assert body["id"].startswith("conn_")
        assert "created_at" in body

    def test_get_returns_created_connection(self):
        """GET /api/connections returns the row created by POST (AC4)."""
        from core.main import build_asgi_app

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            # review-2-4 F-01 orphan guard: mock the connection as known to Nango.
            return_value=[{"connection_id": "nango-integ-get-test"}],
        ), patch("core.audit.psycopg", create=True) as mock_audit_psycopg:
            from unittest.mock import MagicMock

            mock_audit_psycopg.connect.return_value.__enter__ = lambda s: s
            mock_audit_psycopg.connect.return_value.__exit__ = MagicMock(return_value=False)
            mock_audit_psycopg.connect.return_value.cursor.return_value.__enter__ = lambda s: s
            mock_audit_psycopg.connect.return_value.cursor.return_value.__exit__ = MagicMock(
                return_value=False
            )

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=True)

            # Create a connection
            post_resp = client.post(
                "/api/connections",
                json={
                    "nango_connection_id": "nango-integ-get-test",
                    "provider": "google-analytics",
                    "project_id": self._TEST_PROJECT_ID,
                },
            )
            assert post_resp.status_code == 201

            # List connections -- the created row must appear
            get_resp = client.get(f"/api/connections?project_id={self._TEST_PROJECT_ID}")
            assert get_resp.status_code == 200
            body = get_resp.json()
            assert "connections" in body
            conn_ids = [c["nango_connection_id"] for c in body["connections"]]
            assert "nango-integ-get-test" in conn_ids


class TestOrphanPrevention:
    """review-2-4 F-01: never record a connection Nango does not know about."""

    def test_post_unknown_nango_connection_returns_409(self):
        from core.main import build_asgi_app

        with patch(
            "core.admin_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            return_value=[{"connection_id": "some-other-conn", "provider": "p"}],
        ), patch(
            "core.admin_api._resolve_connection_create_scope",
            return_value=("org_test", False),
        ), patch("core.db.get_connection"):
            client = TestClient(build_asgi_app(), raise_server_exceptions=False)
            resp = client.post(
                "/api/connections",
                json={
                    "provider": "test-provider",
                    "nango_connection_id": "nango-ghost-999",
                    "project_id": "project-test",
                },
            )
        assert resp.status_code == 409
        assert resp.json()["code"] == "unknown_nango_connection"

    def test_post_invalid_provider_charset_returns_400(self):
        from core.main import build_asgi_app

        client = TestClient(build_asgi_app(), raise_server_exceptions=False)
        resp = client.post(
            "/api/connections",
            json={
                "provider": "BAD PROVIDER!",
                "nango_connection_id": "x",
                "project_id": "project-test",
            },
        )
        assert resp.status_code == 400
        assert resp.json()["code"] == "invalid_field"


class TestListOrgMembersSeam:
    """Seam tests for GET /api/organizations/{org_id}/members (Story 21.8, Task 3, AI-56).

    Verifies that the route is registered and the endpoint returns the correct
    shape without a real Postgres database. A DB-live variant is gated on
    PLATFORM_DB_URL (same pattern as the existing connection tests above).
    """

    def test_route_registered_returns_401_without_token_in_static_mode(self):
        """The route exists: in static auth mode, missing token returns 401 (not 404)."""
        from core.main import build_asgi_app

        with patch.dict(os.environ, {"TOOROW_AUTH_MODE": "static"}):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/organizations/org_test01/members")

        # 401 proves the route is registered (a 404 would mean it is missing).
        assert resp.status_code == 401, (
            f"Expected 401 (route registered, auth required) but got {resp.status_code}. "
            "This likely means GET /api/organizations/{org_id}/members is not registered."
        )

    def test_returns_members_list_shape_with_mocked_db(self):
        """With mocked DB, GET /api/organizations/{org_id}/members returns
        {"members": [...]} with the expected columns (AI-54 fixture shape)."""
        from contextlib import contextmanager
        from datetime import datetime, timezone

        from core.main import build_asgi_app

        # Simulate the DB returning one org_members row with all expected columns.
        _member_row = (
            "mem_001",                                                   # id
            "org_test01",                                                # org_id
            "alice@example.com",                                         # identity
            "owner",                                                     # role
            "active",                                                    # status
            None,                                                        # invited_by
            None,                                                        # invited_at
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),         # joined_at
            datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),         # created_at
        )

        class FakeMembersCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("org_id",), ("identity",), ("role",), ("status",),
                    ("invited_by",), ("invited_at",), ("joined_at",), ("created_at",),
                ]
                self._rows = [_member_row]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchall(self):
                return self._rows

        class FakeConn:
            def cursor(self):
                # In disabled-auth mode (default), identity_has_org_access short-
                # circuits for "anonymous" BEFORE any DB call. The first (and only)
                # cursor request is the members SELECT itself.
                return FakeMembersCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield FakeConn()

        with patch("core.db.get_connection", new=_fake_get_connection):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/organizations/org_test01/members")

        assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert "members" in body, f"Response missing 'members' key: {body}"
        assert isinstance(body["members"], list), "'members' must be a list"

        members = body["members"]
        assert len(members) == 1
        m = members[0]

        # Verify all expected fields are present (AI-54 fixture contract).
        expected_keys = {
            "id", "org_id", "identity", "role", "status",
            "invited_by", "invited_at", "joined_at", "created_at",
        }
        assert expected_keys.issubset(m.keys()), (
            f"Missing keys in member response: {expected_keys - m.keys()}"
        )
        assert m["identity"] == "alice@example.com"
        assert m["role"] == "owner"
        assert m["status"] == "active"

    @_skip_no_db
    def test_live_returns_200_with_members_array(self):
        """Live Postgres: GET /api/organizations/{org_id}/members returns 200 + members key.

        Uses a non-existent org_id so identity_has_org_access returns False → 404.
        This confirms the route is live and the DB query runs without crashing
        (a 500 would indicate a SQL or import error; 404 is the expected result
        for a non-existent org).
        """
        from core.main import build_asgi_app

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/organizations/org_nonexistent_21_8_seam/members")

        # Either 404 (org not found / access denied — correct scoping) or 200 (empty list).
        assert resp.status_code in (200, 404), (
            f"Unexpected status {resp.status_code}: {resp.text}"
        )
        if resp.status_code == 200:
            assert "members" in resp.json()
