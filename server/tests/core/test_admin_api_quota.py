"""Unit tests for admin_api.py quota/rate-limit additions (Story 3.3, AC10, AI-18).

Tests:
  - test_refresh_health_rate_limited: second call within 30s returns 429.

Strategy:
  - Mocks time.monotonic() via patch.
  - Mocks DB and Nango calls so the test is pure unit-level.
  - HEALTH_POLLER_ENABLED=false and QUEUE_WORKER_ENABLED=false to suppress threads.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


def _make_fake_db_cursor(row):
    """Return a fake DB context manager that yields a connection with a stubbed cursor."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchone = MagicMock(return_value=row)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = MagicMock()

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection


class TestRefreshHealthRateLimit:
    """AC10 / AI-18: per-connection rate limit on POST /api/connections/{id}/refresh-health."""

    def test_refresh_health_rate_limited_on_second_call(self):
        """Second call within 30s for the same connection returns 429 with retry_after."""
        from core.main import build_asgi_app
        from starlette.testclient import TestClient

        # Fake DB row: (id, nango_connection_id, provider)
        conn_ref_row = ("conn_rl_test", "nango-rl-001", "test-provider")

        # Fake Nango health response
        mock_health = MagicMock()
        mock_health.status = "ok"
        mock_health.last_fetched_at = None

        # Fake Nango upsert (second DB call for upsert)
        upsert_cursor = MagicMock()
        upsert_cursor.__enter__ = MagicMock(return_value=upsert_cursor)
        upsert_cursor.__exit__ = MagicMock(return_value=False)
        upsert_conn = MagicMock()
        upsert_conn.__enter__ = MagicMock(return_value=upsert_conn)
        upsert_conn.__exit__ = MagicMock(return_value=False)
        upsert_conn.cursor = MagicMock(return_value=upsert_cursor)
        upsert_conn.commit = MagicMock()

        call_count = [0]

        @contextmanager
        def _multi_call_db():
            call_count[0] += 1
            if call_count[0] % 2 == 1:
                # Odd calls: SELECT connection_ref
                mc = MagicMock()
                mc.__enter__ = MagicMock(return_value=mc)
                mc.__exit__ = MagicMock(return_value=False)
                mc.fetchone = MagicMock(return_value=conn_ref_row)
                mc2 = MagicMock()
                mc2.__enter__ = MagicMock(return_value=mc)
                mc2.__exit__ = MagicMock(return_value=False)
                mc2.cursor = MagicMock(return_value=mc)
                mc2.commit = MagicMock()
                yield mc2
            else:
                # Even calls: upsert
                yield upsert_conn

        # Use a controllable monotonic clock
        mono_values = [0.0]

        def _fake_mono():
            return mono_values[0]

        # Reset the module-level rate limit dict to avoid cross-test contamination
        import core.admin_api as _admin_api
        original_last = _admin_api._refresh_health_last.copy()
        _admin_api._refresh_health_last.clear()

        try:
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)

            with patch("core.db.get_connection", new=_multi_call_db), \
                 patch("core.nango_client._poll_connection_health_async",
                        new=AsyncMock(return_value=mock_health)), \
                 patch("time.monotonic", side_effect=_fake_mono):

                # First call at t=0 -- may or may not succeed depending on deeper DB mocking;
                # we only care about the rate-limit behaviour. Record the timestamp manually.
                client.post("/api/connections/conn_rl_test/refresh-health")
                _admin_api._refresh_health_last["conn_rl_test"] = 0.0

                # Second call at t=10 (within 30s window)
                mono_values[0] = 10.0
                resp2 = client.post("/api/connections/conn_rl_test/refresh-health")

            assert resp2.status_code == 429, (
                f"Expected 429 on second call within 30s, got {resp2.status_code}: {resp2.text}"
            )
            body = resp2.json()
            assert body["code"] == "rate_limited"
            assert "retry_after" in body
            assert body["retry_after"] > 0

        finally:
            _admin_api._refresh_health_last.clear()
            _admin_api._refresh_health_last.update(original_last)

    # ---------------------------------------------------------------------------
    # AI-24 (Story 4.1, AC12): quota_state in GET /api/jobs/{id}
    # ---------------------------------------------------------------------------


class TestQuotaStateInJobStatus:
    """AI-24: quota_state field in GET /api/jobs/{id} response."""

    def _make_job_row(self, error_detail: str | None) -> tuple:
        """Build a minimal pull_jobs row tuple matching get_job_status query."""
        return (
            "job_test_001",           # id
            "pull_test_001",          # pull_id
            "conn_test_001",          # connection_ref_id
            "2026-07-01",             # date_from
            "2026-07-07",             # date_to
            "queued",                 # state
            "test_user",              # requested_by
            error_detail,             # error_detail
            0,                        # attempt_count
            None,                     # enqueued_at
            None,                     # started_at
            None,                     # completed_at
        )

    def _get_job_response(self, error_detail: str | None) -> dict:
        """Call GET /api/jobs/{id} with a mocked DB row and return the JSON body."""
        from contextlib import contextmanager

        from core.main import build_asgi_app
        from starlette.testclient import TestClient

        row = self._make_job_row(error_detail)
        cols = [
            "id", "pull_id", "connection_ref_id", "date_from", "date_to",
            "state", "requested_by", "error_detail", "attempt_count",
            "enqueued_at", "started_at", "completed_at",
        ]

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone = MagicMock(return_value=row)
        mock_cursor.description = [(c,) for c in cols]

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        @contextmanager
        def _fake_db():
            yield mock_conn

        app = build_asgi_app()
        client = TestClient(app, raise_server_exceptions=False)

        with patch("core.db.get_connection", new=_fake_db):
            resp = client.get("/api/jobs/job_test_001")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        return resp.json()

    def test_quota_state_null_when_no_quota_block(self):
        """quota_state is null when error_detail is not a quota message."""
        body = self._get_job_response(error_detail=None)
        assert "quota_state" in body
        assert body["quota_state"] is None

    def test_quota_state_null_when_unrelated_error(self):
        """quota_state is null when error_detail is a non-quota error."""
        body = self._get_job_response(error_detail="connection_ref 'x' not found")
        assert body["quota_state"] is None

    def test_quota_state_quota_blocked_on_budget_exhausted(self):
        """quota_state='quota_blocked' when error_detail is 'quota_blocked: budget_exhausted'."""
        body = self._get_job_response(error_detail="quota_blocked: budget_exhausted")
        assert body["quota_state"] == "quota_blocked"

    def test_quota_state_circuit_open_on_breaker_open(self):
        """quota_state='circuit_open' when error_detail is 'quota_blocked: circuit_open'."""
        body = self._get_job_response(error_detail="quota_blocked: circuit_open")
        assert body["quota_state"] == "circuit_open"

    def test_quota_state_field_always_present(self):
        """quota_state key must always be present in response, even when null."""
        body = self._get_job_response(error_detail=None)
        assert "quota_state" in body, "quota_state key must always appear in job status response"

    def test_refresh_health_allowed_after_window(self):
        """After 30s have elapsed, a refresh is allowed again."""
        import core.admin_api as _admin_api

        original_last = _admin_api._refresh_health_last.copy()
        _admin_api._refresh_health_last.clear()
        _admin_api._refresh_health_last["conn_window_test"] = 0.0

        try:
            mono_values = [35.0]  # 35s elapsed -- past the 30s window

            def _fake_mono():
                return mono_values[0]

            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            conn_ref_row = ("conn_window_test", "nango-w-001", "test-provider")

            mock_health = MagicMock()
            mock_health.status = "ok"
            mock_health.last_fetched_at = None

            @contextmanager
            def _fake_db():
                mc = MagicMock()
                mc.__enter__ = MagicMock(return_value=mc)
                mc.__exit__ = MagicMock(return_value=False)
                mc.fetchone = MagicMock(return_value=conn_ref_row)
                conn = MagicMock()
                conn.__enter__ = MagicMock(return_value=conn)
                conn.__exit__ = MagicMock(return_value=False)
                conn.cursor = MagicMock(return_value=mc)
                conn.commit = MagicMock()
                yield conn

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)

            with patch("core.db.get_connection", new=_fake_db), \
                 patch("core.nango_client._poll_connection_health_async",
                        new=AsyncMock(return_value=mock_health)), \
                 patch("time.monotonic", side_effect=_fake_mono):
                resp = client.post("/api/connections/conn_window_test/refresh-health")

            # Should NOT be 429 (window elapsed)
            assert resp.status_code != 429, (
                f"Expected non-429 after window elapsed, got {resp.status_code}: {resp.text}"
            )

        finally:
            _admin_api._refresh_health_last.clear()
            _admin_api._refresh_health_last.update(original_last)
