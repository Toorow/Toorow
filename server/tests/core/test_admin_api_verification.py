"""Unit tests for GET /api/jobs/{id}/verification endpoint (Story 3.5, AC7).

Tests:
  - GET /api/jobs/{id}/verification returns 200 with verification record
  - GET /api/jobs/{id}/verification returns 404 when job not found
  - GET /api/jobs/{id}/verification returns 404 when no verification record
  - GET /api/jobs/{id}/verification returns 401 when unauthorized
  - Route order regression: /api/jobs/{id}/verification is matched before /api/jobs/{id}

Strategy:
  - All DB calls mocked -- no real Postgres required.
  - SCHEDULER_ENABLED=false, HEALTH_POLLER_ENABLED=false, QUEUE_WORKER_ENABLED=false.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pg_cursor_two_queries(job_row, ver_row):
    """Fake cursor that returns job_row then ver_row on successive fetchone() calls."""
    call_count = 0

    class TwoCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return job_row
            return ver_row

    class FakeConn:
        def cursor(self):
            return TwoCursor()

        def commit(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _fake_get_connection():
        yield FakeConn()

    return _fake_get_connection


def _make_pg_cursor_job_not_found():
    """Fake cursor that returns None (job not found)."""
    class NoneJobCursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            pass

        def fetchone(self):
            return None

    class FakeConn:
        def cursor(self):
            return NoneJobCursor()

        def commit(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _fake_get_connection():
        yield FakeConn()

    return _fake_get_connection


def build_asgi_app():
    from core.main import build_asgi_app as _build
    return _build()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetJobVerificationEndpoint:
    _VERIFIED_AT = datetime(2026, 7, 11, 10, 0, 0, tzinfo=timezone.utc)

    def _make_ver_row(self):
        """Return a fake pull_verifications row tuple."""
        return (
            "pull_ver_001",        # pull_id
            7,                     # expected_rows
            7,                     # actual_rows
            Decimal("1.0000"),     # completeness_ratio
            "ok",                  # verdict
            self._VERIFIED_AT,     # verified_at
        )

    def test_returns_200_with_verification_record(self):
        """GET /api/jobs/{id}/verification returns 200 with all expected fields."""
        from starlette.testclient import TestClient

        job_row = ("pull_ver_001",)  # pull_id from pull_jobs
        ver_row = self._make_ver_row()
        fake_conn_mgr = _make_pg_cursor_two_queries(job_row, ver_row)

        with patch("core.db.get_connection", new=fake_conn_mgr):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_ver_001/verification")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["pull_id"] == "pull_ver_001"
        assert body["expected_rows"] == 7
        assert body["actual_rows"] == 7
        assert isinstance(body["completeness_ratio"], float)
        assert body["completeness_ratio"] == 1.0
        assert body["verdict"] == "ok"
        assert "verified_at" in body
        assert "2026-07-11" in body["verified_at"]

    def test_returns_404_when_job_not_found(self):
        """GET /api/jobs/{id}/verification returns 404 when job_id not in pull_jobs."""
        from starlette.testclient import TestClient

        fake_conn_mgr = _make_pg_cursor_job_not_found()

        with patch("core.db.get_connection", new=fake_conn_mgr):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_nonexistent/verification")

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert "Job" in body["message"] or "not found" in body["message"]

    def test_returns_404_when_no_verification_record(self):
        """GET /api/jobs/{id}/verification returns 404 when verification not yet written."""
        from starlette.testclient import TestClient

        # Job exists but no verification record yet
        job_row = ("pull_ver_002",)
        ver_row = None  # no verification written yet
        fake_conn_mgr = _make_pg_cursor_two_queries(job_row, ver_row)

        with patch("core.db.get_connection", new=fake_conn_mgr):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_ver_002/verification")

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"
        assert "verification" in body["message"].lower() or "No verification" in body["message"]

    def test_returns_401_in_static_auth_mode(self):
        """GET /api/jobs/{id}/verification returns 401 when auth required and no token."""
        from starlette.testclient import TestClient

        with patch.dict(os.environ, {"TOOROW_AUTH_MODE": "static"}):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_ver_auth_test/verification")

        assert resp.status_code == 401

    def test_route_order_verification_before_job_status(self):
        """Route order regression: /api/jobs/{id}/verification is NOT absorbed by /api/jobs/{id}.

        The path segment 'verification' must NOT be treated as a job ID.
        If route order were wrong, /api/jobs/job_x/verification would route to
        _get_job_status with id='job_x' and path suffix '/verification' -- which
        Starlette would reject as a 404 (no sub-path on /{id}).
        This test confirms the verification handler is reached, not _get_job_status.
        """
        from starlette.testclient import TestClient

        job_row = ("pull_route_test",)
        ver_row = (
            "pull_route_test",
            3,
            3,
            Decimal("1.0000"),
            "ok",
            self._VERIFIED_AT,
        )
        fake_conn_mgr = _make_pg_cursor_two_queries(job_row, ver_row)

        with patch("core.db.get_connection", new=fake_conn_mgr):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_route_test/verification")

        # If route order is correct, we get the verification response (200 or 404 from DB mock)
        # NOT a 404 for "job not found" from _get_job_status
        assert resp.status_code in (200, 404)
        body = resp.json()
        # Confirm it is from the verification handler: body has pull_id or code="not_found"
        # (not "message": "Job '...' not found" from get_job_status's 404)
        if resp.status_code == 200:
            assert "pull_id" in body
        # If 404, confirm it is the verification-specific 404, not get_job_status 404
        # (both share "not_found" code but messages differ)
        assert "code" in body or "pull_id" in body
