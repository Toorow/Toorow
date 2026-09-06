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
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from core import nango_client as _nango_client
from starlette.testclient import TestClient

from tests.support.statement_router import StatementInventory, UnknownStatement, describe


def _principal(person_id: str):
    """A bound person, as `bind_verified_credential` answers for a verified token."""
    from core.api_auth import ResolvedPrincipal

    return ResolvedPrincipal(
        person_id=person_id,
        issuer="static://toorow",
        subject="tok-valid-123",
        verified_email=None,
        display_name=None,
    )

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

# ---------------------------------------------------------------------------
# AI-317: EVERY STATEMENT `GET /api/organizations/{org_id}/members` ISSUES.
#
# The fake below used to tell the two apart with `"SELECT m.role" in sql` and
# answer the members rows to everything else. Two statements were therefore
# recognised and an unbounded third was answered -- with a `description` copied
# by hand from a projection the fake does not read, which is the copy that goes
# stale first. The route reads exactly these two:
#
#   role    core/project_access.py:32-41   (resolve_org_role, via
#                                           identity_has_org_access)
#   members core/org_members_api.py:165-176
#
# `members` carries `pi.verified_email` since migration 111, and the hand-written
# column list here never did -- see the row below.
# ---------------------------------------------------------------------------
_ORG_MEMBERS = StatementInventory(
    "TestListOrgMembersSeam.FakeMembersCursor",
    role="select m.role from app.organizations o",
    members="select m.id, m.org_id, m.identity",
)


def test_the_members_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: the members fake answers two statements and REFUSES the rest.

    Its old fallthrough answered the members rows -- and the members
    `description` -- to any statement that did not spell `SELECT m.role`. A
    third read on this route would have come back as a members page of the
    right arity, and the assertions below it would have been about a path the
    product never took.
    """
    assert (
        _ORG_MEMBERS.find(
            "SELECT m.role FROM app.organizations o JOIN app.org_members m "
            "ON m.org_id = o.id AND m.identity = %s AND m.status = 'active' "
            "WHERE o.id = %s AND o.status = 'active'"
        )
        == "role"
    )
    assert (
        _ORG_MEMBERS.find(
            "SELECT m.id, m.org_id, m.identity, m.role, m.status, m.invited_by, "
            "m.invited_at, m.joined_at, m.created_at, pi.verified_email "
            "FROM app.org_members m WHERE m.org_id = %s ORDER BY m.created_at ASC"
        )
        == "members"
    )
    with pytest.raises(UnknownStatement) as raised:
        _ORG_MEMBERS.match(
            "SELECT count(*) FROM app.org_members WHERE org_id = %s AND status = 'active'"
        )
    assert "app.org_members where org_id" in str(raised.value)
    assert "members" in str(raised.value)


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
    """GET /api/connections -- the contract this class pins was REWRITTEN, and
    the rewrite is the point.

    These tests used to assert a Nango fallback: DB unreachable -> 200 carrying
    the connection list read straight from Nango, and 502 only when Nango was
    dead too. That path no longer exists, deliberately, and restoring it would
    reopen a hole. `GET /api/connections` is now scoped to ONE project (AD-5):
    it requires `project_id`, resolves strict access before reading anything
    (Story 46.4 removed the runtime flag; the 404 is deliberate so a caller
    without access cannot learn the project exists), and serves rows joined to
    `connection_ref`. A Nango list is org-wide and carries no project scope --
    serving it as a degradation would have handed every caller every provider
    account of the organization exactly when the check that scopes them was
    unavailable. So the endpoint now FAILS CLOSED with 503.

    The tests below therefore assert the opposite of what they used to, plus the
    two guarantees the old shape could not express: that `project_id` is
    mandatory, and that Nango is never consulted for this route at all.
    """

    @staticmethod
    def _fake_db(rows, description):
        """A connection whose cursor answers with `rows`, and nothing else."""

        class FakeCursor:
            def __init__(self):
                self.description = description
                self._rows = rows

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchall(self):
                return self._rows

            def fetchone(self):
                return self._rows[0] if self._rows else None

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

        return _fake_get_connection

    @staticmethod
    @contextmanager
    def _access(allowed: bool):
        """Grant or refuse strict project access without a database."""
        from core.project_access import AccessDecision

        decision = AccessDecision(
            allowed=allowed,
            reason="granted" if allowed else "not_found",
            capability="view" if allowed else None,
            org_id="org_EXAMPLE" if allowed else None,
        )
        with patch(
            "core.project_access.resolve_strict_resource_access", return_value=decision
        ), patch("core.db.set_local_access_context", lambda *a, **k: None):
            yield

    def test_project_id_is_mandatory(self):
        """AD-5: an unscoped call is refused BEFORE any read. This guarantee had
        no test at all while the class still asserted the org-wide fallback."""
        from core.main import build_asgi_app

        nango_mock = AsyncMock(return_value=_FAKE_NANGO_CONN)
        with patch("core.connections_api.nango_client._list_connections_async", nango_mock):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections")

        assert resp.status_code == 400
        assert resp.json()["code"] == "missing_project"
        nango_mock.assert_not_called()

    def test_db_unavailable_fails_closed_and_never_falls_back_to_nango(self):
        """The replaced behaviour, asserted in the negative.

        A Nango list is org-wide. Serving it when the project-scoping read is
        down would disclose every provider account of the organization to a
        caller whose access could not be checked. 503, and Nango untouched.
        """
        from core.main import build_asgi_app

        nango_mock = AsyncMock(return_value=_FAKE_NANGO_CONN)
        with patch(
            "core.connections_api.nango_client._list_connections_async", nango_mock
        ), patch("core.db._psycopg", None):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections?project_id=proj_EXAMPLE")

        assert resp.status_code == 503
        assert resp.json()["code"] == "db_error"
        assert "connections" not in resp.json()
        nango_mock.assert_not_called()

    def test_caller_without_access_gets_a_nondisclosing_404(self):
        """Story 46.4: not 403 -- a refusal must not confirm the project exists."""
        from core.main import build_asgi_app

        fake_db = self._fake_db([], [("id",)])
        with patch("core.db.get_connection", new=fake_db), self._access(False):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections?project_id=proj_EXAMPLE")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    def test_health_field_present_in_get_response(self):
        """GET /api/connections response includes 'health' per AC2 (Story 2.5)."""
        from core.main import build_asgi_app

        columns = [
            "id", "provider", "nango_connection_id", "project_id", "created_at",
            "status", "auth_path", "owner_org_id", "owner_identity", "token_expiry",
            "owner_org_name", "owner_display_name", "owner_email", "viewer_org_id",
            "account_label", "account_state", "health_status", "last_checked_at",
            "last_fetched_at",
        ]
        row = (
            "conn_h1", "ga", "nango-h001", "proj_EXAMPLE",
            datetime(2026, 7, 10, 10, 0, 0, tzinfo=timezone.utc),
            "active", "nango", "org_EXAMPLE", "owner@example.com", None,
            "Example Org", "Owner", "owner@example.com", "org_EXAMPLE",
            "Account A", "active", "ok",
            datetime(2026, 7, 10, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 7, 10, 11, 0, 0, tzinfo=timezone.utc),
        )
        fake_db = self._fake_db([row], [(c,) for c in columns])

        nango_mock = AsyncMock(return_value=_FAKE_NANGO_CONN)
        with patch(
            "core.connections_api.nango_client._list_connections_async", nango_mock
        ), patch("core.db.get_connection", new=fake_db), self._access(True):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections?project_id=proj_EXAMPLE")

        assert resp.status_code == 200, resp.text
        conns = resp.json()["connections"]
        assert len(conns) == 1
        assert "health" in conns[0]
        if conns[0]["health"] is not None:
            assert "status" in conns[0]["health"]
        # review-2-5 F-01, still true and now unconditional: Nango is not on this
        # route's path at all -- not on the happy path, and not as a fallback.
        nango_mock.assert_not_called()

    def test_nango_down_alone_does_not_fail_get(self):
        """review-2-5 F-01: a dead Nango cannot affect GET /api/connections."""
        from core.main import build_asgi_app

        columns = ["id", "provider", "nango_connection_id", "project_id", "created_at"]
        fake_db = self._fake_db([], [(c,) for c in columns])
        nango_mock = AsyncMock(side_effect=Exception("nango unreachable"))
        with patch(
            "core.connections_api.nango_client._list_connections_async", nango_mock
        ), patch("core.db.get_connection", new=fake_db), self._access(True):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/connections?project_id=proj_EXAMPLE")

        assert resp.status_code == 200, resp.text
        nango_mock.assert_not_called()

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

        # Since 67-17 a verified token is BOUND to a person through the database
        # (`bind_verified_credential`); that binding is not what this test
        # measures, so it is answered as the other route tests answer it.
        with patch(
            "core.connections_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            return_value=[],
        ), patch.dict(
            os.environ,
            {"TOOROW_AUTH_MODE": "static", "TOOROW_STATIC_TOKEN": "tok-valid-123"},
        ), patch(
            "core.api_auth.bind_verified_credential",
            return_value=(True, _principal("person_EXAMPLE")),
        ), patch("core.db.get_connection", self._fake_db([], [("id",)])), self._access(True):
            reset_verifier_cache()
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get(
                "/api/connections?project_id=proj_EXAMPLE",
                headers={"Authorization": "Bearer tok-valid-123"},
            )
        reset_verifier_cache()

        # What this test is FOR is the token check: the CONFIGURED token must get
        # past auth. 401 is the regression this test exists to catch.
        assert resp.status_code != 401, resp.text
        assert resp.status_code == 200, resp.text

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

    # AD-5 SCOPING WAS ADDED IN FRONT OF THIS ROUTE, and that is why these two
    # tests went red with a 500 `db_error` reading "a non-anonymous identity is
    # required".
    #
    # `_refresh_connection_health` now calls `_resolve_conn_project_scoped`
    # (`connections_api.py#_resolve_conn_project_scoped`), which installs the access context via
    # `core.db.set_local_access_context` -- and that function refuses the
    # identity "anonymous" outright (`core/db.py:107-108`). These tests run with
    # `TOOROW_AUTH_MODE` unset, so the identity IS "anonymous"; the ValueError
    # was caught by the handler's outer `except Exception` and reported as a
    # database outage. The scoping is deliberate ("Strict, always. The
    # `identity_can_read_project` branch that used to sit here was unreachable
    # after Story 46.4 removed the runtime flag",
    # `connections_api.py#_resolve_conn_project_scoped`).
    #
    # The subject of these two tests is the NANGO CALL and the health upsert, not
    # the scope decision -- which has its own tests. So the scope resolver is
    # patched to a granted verdict, explicitly, and the tests now say out loud
    # that they depend on it instead of silently running unscoped.
    @staticmethod
    def _scope_granted():
        return patch(
            "core.connections_api._resolve_conn_project_scoped",
            return_value=({"project_id": "proj_EXAMPLE", "provider": "ga"}, None),
        )

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
            "core.connections_api.nango_client._poll_connection_health_async",
            new_callable=AsyncMock,
            return_value=_FAKE_HEALTH,
        ), patch("core.db.get_connection", new=_fake_get_connection), self._scope_granted():
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

        with patch("core.db.get_connection", new=_fake_get_connection), self._scope_granted():
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
            "core.connections_api.nango_client._list_connections_async",
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
            "core.connections_api.nango_client._list_connections_async",
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
            "core.connections_api.nango_client._list_connections_async",
            new_callable=AsyncMock,
            return_value=[{"connection_id": "some-other-conn", "provider": "p"}],
        ), patch(
            "core.connections_api._resolve_connection_create_scope",
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

        # ONE ROW OF THE STATEMENT THE PRODUCT ACTUALLY ISSUES. The tenth value
        # is `pi.verified_email` (org_members_api.py:165-176, migration 111) and
        # the fixture never had it: the hand-written column list below stopped at
        # nine, so the projection the fake claimed and the projection the product
        # selects had disagreed since the LEFT JOIN LATERAL landed -- invisibly,
        # because `_org_row_to_dict` zips and a short zip drops in silence.
        #
        # It is NULL here, and that is the legacy row the LATERAL exists for: an
        # identity minted before the 111 is a raw e-mail with no
        # `person_identities` line. That is why `identity` below is an address.
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
            None,                                                        # verified_email
        )

        class FakeMembersCursor:
            """Answers the org-ROLE check and the MEMBERS list, and tells them apart.

            THE ANONYMOUS SHORT-CIRCUIT IS GONE, and that is why this test went
            red with a 500. Its comment used to read: "in disabled-auth mode
            (default), identity_has_org_access short-circuits for 'anonymous'
            BEFORE any DB call. The first (and only) cursor request is the
            members SELECT itself." That short-circuit was a default-open path
            and it was removed on purpose -- `core/project_access.py:20-26`
            now says "never infer access" and always issues the role query,
            raising `ProjectAccessUnavailable` when it cannot run.

            So there are now TWO statements, not one, and a fake that replies to
            everything with the members rows would be answering a question it was
            not asked. `_ORG_MEMBERS` names both; a third statement RAISES
            instead of being served a members page of the right arity (AI-317).
            """

            def __init__(self):
                self.description = None
                self._statement = None

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                # `SELECT m.role`, pas `app.org_members m` : depuis la 111 la
                # requête des MEMBRES lit elle aussi `app.org_members m` (LEFT
                # JOIN LATERAL sur `person_identities`), donc un critère porté
                # par le nom de la table répondrait « rôle » aux deux et la
                # liste reviendrait vide.
                self._statement = _ORG_MEMBERS.match(sql)
                # DERIVED from the statement. The two column lists used to be
                # typed here and had to keep agreeing with a projection this
                # fake never reads; one of them had already stopped.
                self.description = describe(sql)

            def fetchone(self):
                # The caller is an owner of this organization; the members list is
                # what the test is actually about.
                return ("owner",) if self._statement == "role" else None

            def fetchall(self):
                # Only the members statement has rows. The role check reads with
                # `fetchone`, and answering it a page would be inventing a call
                # the product does not make.
                return [_member_row] if self._statement == "members" else []

        class FakeConn:
            def cursor(self):
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
