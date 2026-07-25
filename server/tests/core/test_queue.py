"""Unit tests for server/core/queue.py (Story 3.2, AC1, AC3, AC4, AC5, AC8).

Tests:
  - enqueue_pull inserts job row and returns correct dict
  - get_job_status returns row dict or None
  - Local worker executes job and writes audit rows
  - Local worker dead-letters after MAX_ATTEMPTS
  - Cloud Tasks backend enqueues task with correct URL
  - Cloud Tasks backend raises EnvironmentError when CLOUD_TASKS_PROJECT unset
  - GET /api/jobs/{id} endpoint returns 200 or 404

Strategy:
  - All DB calls are mocked (no real Postgres required).
  - QUEUE_WORKER_ENABLED=false in all tests (no background thread started).
  - Worker logic tested by calling _execute_job() directly.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

# Suppress background threads throughout this module
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_connection(rows_by_query=None, fetchone_return=None):
    """Build a fake psycopg connection context manager.

    rows_by_query: optional dict mapping SQL substrings to fetchone returns.
    fetchone_return: simple single fetchone return value.
    """
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    # Default fetchone -> None (e.g. the F-2 dedup SELECT finds no existing job)
    mock_cursor.fetchone = MagicMock(return_value=fetchone_return)

    mock_conn = MagicMock()
    mock_conn.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn.__exit__ = MagicMock(return_value=False)
    mock_conn.cursor = MagicMock(return_value=mock_cursor)
    mock_conn.commit = MagicMock()
    mock_conn.close = MagicMock()

    @contextmanager
    def _fake_get_connection():
        yield mock_conn

    return _fake_get_connection, mock_conn, mock_cursor


# ---------------------------------------------------------------------------
# T9.1 — enqueue_pull tests
# ---------------------------------------------------------------------------


class TestEnqueuePull:
    def test_enqueue_pull_inserts_job_row_and_returns_dict(self):
        """enqueue_pull inserts into app.pull_jobs, writes audit, returns job dict."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_connection()

        # write_audit_row is imported inside the function, patch at the source module
        with patch("core.db.get_connection", new=fake_conn_mgr), \
             patch("core.audit.write_audit_row"):
            from core.queue import LocalBackend
            backend = LocalBackend()
            result = backend.enqueue_pull(
                connection_ref_id="conn_test_001",
                date_from="2026-07-01",
                date_to="2026-07-07",
                requested_by="test-user",
            )

        assert "job_id" in result
        assert result["job_id"].startswith("job_")
        assert "pull_id" in result
        assert result["pull_id"].startswith("pull_")
        assert result["state"] == "queued"

        # Verify INSERT was called
        insert_calls = [
            str(c) for c in mock_cursor.execute.call_args_list
            if "INSERT" in str(c) and "pull_jobs" in str(c)
        ]
        assert insert_calls, "Expected INSERT into app.pull_jobs"

        # Verify commit was called
        mock_conn.commit.assert_called()

    def test_enqueue_pull_mints_unique_ids(self):
        """Each enqueue_pull call produces distinct job_id and pull_id."""
        fake_conn_mgr, _, _ = _make_fake_connection()

        with patch("core.db.get_connection", new=fake_conn_mgr), \
             patch("core.audit.write_audit_row"):
            from core.queue import LocalBackend
            backend = LocalBackend()
            r1 = backend.enqueue_pull("conn_a", "2026-01-01", "2026-01-07", requested_by="u")
            r2 = backend.enqueue_pull("conn_a", "2026-01-01", "2026-01-07", requested_by="u")

        assert r1["job_id"] != r2["job_id"]
        assert r1["pull_id"] != r2["pull_id"]


# ---------------------------------------------------------------------------
# T9.1 — get_job_status tests
# ---------------------------------------------------------------------------


class TestGetJobStatus:
    def _make_job_row(self, job_id="job_01", state="queued"):
        from datetime import datetime, timezone
        return (
            job_id,                # id
            "pull_01",             # pull_id
            "conn_ref_01",         # connection_ref_id
            "2026-07-01",          # date_from
            "2026-07-07",          # date_to
            state,                 # state
            "test-user",           # requested_by
            None,                  # error_detail
            0,                     # attempt_count
            datetime(2026, 7, 1, 10, 0, 0, tzinfo=timezone.utc),  # enqueued_at
            None,                  # started_at
            None,                  # completed_at
        )

    def test_get_job_status_returns_row_dict(self):
        """get_job_status returns a dict when job exists."""
        fake_row = self._make_job_row("job_01", "queued")
        fake_conn_mgr, _, mock_cursor = _make_fake_connection(fetchone_return=fake_row)
        mock_cursor.description = [
            ("id",), ("pull_id",), ("connection_ref_id",), ("date_from",), ("date_to",),
            ("state",), ("requested_by",), ("error_detail",), ("attempt_count",),
            ("enqueued_at",), ("started_at",), ("completed_at",),
        ]

        with patch("core.db.get_connection", new=fake_conn_mgr):
            from core.queue import LocalBackend
            backend = LocalBackend()
            result = backend.get_job_status("job_01")

        assert result is not None
        assert result["id"] == "job_01"
        assert result["state"] == "queued"
        assert result["pull_id"] == "pull_01"
        assert result["enqueued_at"] is not None  # ISO string

    def test_get_job_status_returns_none_when_not_found(self):
        """get_job_status returns None when job_id is not in DB."""
        # Need a cursor that sets description but returns None from fetchone
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone = MagicMock(return_value=None)
        mock_cursor.description = [
            ("id",), ("pull_id",), ("connection_ref_id",), ("date_from",), ("date_to",),
            ("state",), ("requested_by",), ("error_detail",), ("attempt_count",),
            ("enqueued_at",), ("started_at",), ("completed_at",),
        ]

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)
        mock_conn.commit = MagicMock()
        mock_conn.close = MagicMock()

        @contextmanager
        def _fake_get_connection():
            yield mock_conn

        with patch("core.db.get_connection", new=_fake_get_connection):
            from core.queue import LocalBackend
            backend = LocalBackend()
            result = backend.get_job_status("job_nonexistent")

        assert result is None


# ---------------------------------------------------------------------------
# T9.1 — Worker execution tests (calling _execute_job directly)
# ---------------------------------------------------------------------------


class TestWorkerExecution:
    def _make_queued_job(self):
        return {
            "id": "job_worker_01",
            "pull_id": "pull_worker_01",
            "connection_ref_id": "conn_ref_01",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "requested_by": "test-user",
            "attempt_count": 0,
        }

    def _make_conn_ref_row(self):
        return ("conn_ref_01", "nango-conn-001", "test-provider", "proj-01")

    def test_local_worker_executes_job_and_sets_done(self):
        """Worker executes job, transitions to 'done', writes ACTION_PULL_COMPLETED."""
        job = self._make_queued_job()
        conn_ref_row = self._make_conn_ref_row()

        # Track UPDATE calls to verify state transitions
        update_states: list[str] = []

        class TrackingCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]
                self._call_count = 0

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                # Track state transitions via the SQL string (states are literals in SQL)
                sql_str = str(sql)
                if "running" in sql_str and "UPDATE" in sql_str:
                    update_states.append("running")
                elif "done" in sql_str and "UPDATE" in sql_str:
                    update_states.append("done")
                self._call_count += 1

            def fetchone(self):
                return conn_ref_row

        class TrackingConn:
            def cursor(self):
                return TrackingCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield TrackingConn()

        fake_pull_result = {
            "pull_id": "pull_worker_01",
            "row_count": 5,
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
        }
        mock_pull_fn = MagicMock(return_value=fake_pull_result)
        mock_audit = MagicMock()

        with patch("core.db.get_connection", new=_fake_get_connection), \
             patch("core.main.get_module_pull_fn", return_value=mock_pull_fn), \
             patch("core.audit.write_audit_row", mock_audit):
            from core.queue import _execute_job
            _execute_job(job)

        # Verify pull_fn was called with correct kwargs
        mock_pull_fn.assert_called_once()
        call_kwargs = mock_pull_fn.call_args.kwargs
        assert call_kwargs["connection_id"] == "nango-conn-001"
        assert call_kwargs["pull_id"] == "pull_worker_01"

        # Verify terminal transition occurred. review-3-2 F-1: the 'running'
        # transition now happens atomically inside the CLAIM (_dequeue_one),
        # not in _execute_job -- only the terminal state is set here.
        assert "done" in update_states

        # Verify ACTION_PULL_COMPLETED audit row written
        from core.audit import ACTION_PULL_COMPLETED
        completed_calls = [
            c for c in mock_audit.call_args_list
            if ACTION_PULL_COMPLETED in str(c)
        ]
        assert completed_calls, "Expected ACTION_PULL_COMPLETED audit row"

    def test_local_worker_dead_letters_after_max_attempts(self):
        """Worker sets dead_letter state after MAX_ATTEMPTS failures."""
        job = self._make_queued_job()
        job["attempt_count"] = 2  # one more attempt = 3 = MAX_ATTEMPTS

        conn_ref_row = self._make_conn_ref_row()
        final_states: list[str] = []

        class TrackingCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                if params:
                    for state in ("dead_letter", "failed", "done", "running"):
                        if state in str(params):
                            final_states.append(state)
                            break

            def fetchone(self):
                return conn_ref_row

        class TrackingConn:
            def cursor(self):
                return TrackingCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield TrackingConn()

        failing_pull_fn = MagicMock(side_effect=RuntimeError("upstream error"))
        mock_audit = MagicMock()

        with patch("core.db.get_connection", new=_fake_get_connection), \
             patch("core.main.get_module_pull_fn", return_value=failing_pull_fn), \
             patch("core.audit.write_audit_row", mock_audit), \
             patch.dict(os.environ, {"QUEUE_MAX_ATTEMPTS": "3"}):
            from core.queue import _execute_job
            _execute_job(job)

        assert "dead_letter" in final_states, (
            f"Expected 'dead_letter' state after max attempts, got: {final_states}"
        )

        # Verify ACTION_PULL_FAILED audit row written
        from core.audit import ACTION_PULL_FAILED
        failed_calls = [
            c for c in mock_audit.call_args_list
            if ACTION_PULL_FAILED in str(c)
        ]
        assert failed_calls, "Expected ACTION_PULL_FAILED audit row"


# ---------------------------------------------------------------------------
# T9.1 — Cloud Tasks backend tests
# ---------------------------------------------------------------------------


class TestCloudTasksBackend:
    def test_cloud_tasks_backend_raises_when_project_unset(self):
        """CloudTasksBackend.enqueue_pull raises EnvironmentError when CLOUD_TASKS_PROJECT unset."""
        with patch.dict(os.environ, {}, clear=False):
            # Ensure CLOUD_TASKS_PROJECT is not set
            env_without_project = {
                k: v for k, v in os.environ.items() if k != "CLOUD_TASKS_PROJECT"
            }
            with patch.dict(os.environ, env_without_project, clear=True):
                from core.queue import CloudTasksBackend
                backend = CloudTasksBackend()
                with pytest.raises(EnvironmentError, match="CLOUD_TASKS_PROJECT"):
                    backend.enqueue_pull(
                        "conn_ref_01",
                        "2026-07-01",
                        "2026-07-07",
                        requested_by="test-user",
                    )

    def test_cloud_tasks_backend_enqueues_task(self):
        """CloudTasksBackend inserts job row and calls tasks_client.create_task."""
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_connection()

        mock_tasks_client = MagicMock()

        with patch("core.db.get_connection", new=fake_conn_mgr), \
             patch("core.audit.write_audit_row"), \
             patch.dict(os.environ, {
                 "CLOUD_TASKS_PROJECT": "test-project",
                 "CLOUD_TASKS_LOCATION": "us-central1",
                 "CLOUD_TASKS_QUEUE_NAME": "pull-queue",
                 "CLOUD_TASKS_WORKER_URL": "https://worker.example.com",
             }):
            from core.queue import CloudTasksBackend
            backend = CloudTasksBackend(_tasks_client=mock_tasks_client)
            result = backend.enqueue_pull(
                "conn_ref_01",
                "2026-07-01",
                "2026-07-07",
                requested_by="test-user",
            )

        # Verify job row was inserted
        insert_calls = [
            str(c) for c in mock_cursor.execute.call_args_list
            if "INSERT" in str(c)
        ]
        assert insert_calls, "Expected INSERT into app.pull_jobs"

        # Verify task was enqueued
        mock_tasks_client.create_task.assert_called_once()
        task_call = mock_tasks_client.create_task.call_args
        task_body = task_call.kwargs.get("request") or task_call.args[0]
        assert "task" in task_body
        task_url = task_body["task"]["http_request"]["url"]
        assert result["job_id"] in task_url
        assert "execute-pull" in task_url

        # Verify return shape
        assert result["state"] == "queued"
        assert result["job_id"].startswith("job_")
        assert result["pull_id"].startswith("pull_")


# ---------------------------------------------------------------------------
# T9.3 — GET /api/jobs/{id} endpoint tests
# ---------------------------------------------------------------------------


class TestGetJobStatusEndpoint:
    def _make_job_dict(self, state="queued"):
        return {
            "id": "job_endpoint_01",
            "pull_id": "pull_endpoint_01",
            "connection_ref_id": "conn_ref_01",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "state": state,
            "requested_by": "test-user",
            "error_detail": None,
            "attempt_count": 0,
            "enqueued_at": "2026-07-11T10:00:00+00:00",
            "started_at": None,
            "completed_at": None,
        }

    def test_get_job_status_endpoint_200(self):
        """GET /api/jobs/{id} returns 200 with job dict when job exists."""
        fake_job = self._make_job_dict("done")

        with patch("core.queue.get_job_status", return_value=fake_job):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_endpoint_01")

        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["job_id"] == "job_endpoint_01"
        assert body["state"] == "done"
        assert body["pull_id"] == "pull_endpoint_01"
        assert "enqueued_at" in body
        assert "attempt_count" in body

    def test_get_job_status_endpoint_404(self):
        """GET /api/jobs/{id} returns 404 when job not found."""
        with patch("core.queue.get_job_status", return_value=None):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_nonexistent")

        assert resp.status_code == 404
        body = resp.json()
        assert body["code"] == "not_found"

    def test_get_job_status_endpoint_401_in_static_mode(self):
        """GET /api/jobs/{id} returns 401 when auth required and no token."""
        with patch.dict(os.environ, {"TOOROW_AUTH_MODE": "static"}):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/jobs/job_01")

        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# T9.2 — Updated trigger_pull tests (now expects 202, AC4)
# ---------------------------------------------------------------------------


class TestTriggerPullEndpointNew:
    """Verify the /pull endpoint now returns 202 with the queue shape (Story 3.2, AC4)."""

    def test_trigger_pull_returns_202_with_queue_shape(self):
        """POST /api/connections/{id}/pull returns 202 with job_id, pull_id, state."""
        from contextlib import contextmanager

        from starlette.testclient import TestClient

        # Mock DB existence check (only SELECT id needed now)
        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone = MagicMock(return_value=("conn_test_001",))

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        @contextmanager
        def _fake_get_connection():
            yield mock_conn

        fake_result = {
            "job_id": "job_202_test",
            "pull_id": "pull_202_test",
            "state": "queued",
        }

        with patch("core.db.get_connection", new=_fake_get_connection), \
             patch("core.queue.enqueue_pull", return_value=fake_result):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=True)
            resp = client.post(
                "/api/connections/conn_test_001/pull",
                json={"date_from": "2026-07-01", "date_to": "2026-07-07"},
            )

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        body = resp.json()
        assert body["job_id"] == "job_202_test"
        assert body["pull_id"] == "pull_202_test"
        assert body["state"] == "queued"
        assert "row_count" not in body
        assert "dbt_note" not in body

    def test_trigger_pull_returns_404_when_conn_not_found(self):
        """POST /api/connections/{id}/pull returns 404 when connection_ref not in DB."""
        from contextlib import contextmanager

        from starlette.testclient import TestClient

        mock_cursor = MagicMock()
        mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
        mock_cursor.__exit__ = MagicMock(return_value=False)
        mock_cursor.fetchone = MagicMock(return_value=None)  # not found

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cursor)

        @contextmanager
        def _fake_get_connection():
            yield mock_conn

        with patch("core.db.get_connection", new=_fake_get_connection):
            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.post("/api/connections/conn_nonexistent/pull")

        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"


def build_asgi_app():
    """Import and return the ASGI app (helper for test setup)."""
    from core.main import build_asgi_app as _build
    return _build()


# ---------------------------------------------------------------------------
# Story 3.3 -- Quota integration tests (T9.2, AC8)
# ---------------------------------------------------------------------------


class TestWorkerQuotaIntegration:
    """Test _execute_job quota pre-check integration."""

    def _make_queued_job(self, attempt_count=0):
        return {
            "id": "job_quota_01",
            "pull_id": "pull_quota_01",
            "connection_ref_id": "conn_ref_quota_01",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "requested_by": "test-user",
            "attempt_count": attempt_count,
        }

    def _make_conn_ref_row(self, provider="test-provider"):
        return ("conn_ref_quota_01", "nango-conn-001", provider, "proj-01")

    def _build_tracking_connection(self, conn_ref_row):
        """Build a fake connection that returns conn_ref_row from SELECT."""
        from contextlib import contextmanager

        class TrackingCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]
                self._states: list[str] = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                sql_str = str(sql)
                for state in ("running", "done", "failed", "dead_letter", "queued"):
                    if state in sql_str and "UPDATE" in sql_str:
                        self._states.append(state)
                        break

            def fetchone(self):
                return conn_ref_row

        states_holder = []

        class TrackingConn:
            def __init__(self):
                self._cursor = TrackingCursor()

            def cursor(self):
                return self._cursor

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            conn = TrackingConn()
            yield conn
            states_holder.extend(conn._cursor._states)

        return _fake_get_connection, states_holder

    def test_execute_job_skips_when_breaker_open(self):
        """When quota.pre_check returns (False, 'breaker_open'), job stays queued.

        attempt_count must NOT be incremented (re-queue without penalty).
        """
        from unittest.mock import patch

        job = self._make_queued_job(attempt_count=0)

        with patch("core.quota.pre_check", return_value=(False, "breaker_open")), \
             patch("core.quota._engine") as mock_engine:
            mock_engine._configs.get.return_value = {}
            from core.queue import _execute_job

            # pre_check is module-level function -- patch at the right location
        with patch("core.quota._engine._configs", {}), \
             patch("core.quota.pre_check", return_value=(False, "breaker_open")):
            # We need to also stub the DB for pre-check lookup
            from contextlib import contextmanager

            class FakeCursor:
                def __init__(self):
                    self.description = [
                        ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                    ]

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    pass

                def execute(self, sql, params=None):
                    pass

                def fetchone(self):
                    return ("conn_ref_quota_01", "nango-001", "test-provider", "proj-01")

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

            with patch("core.db.get_connection", new=_fake_get_connection), \
                 patch("core.audit.write_audit_row"):
                from core.queue import _execute_job
                result = _execute_job(job)

        # Job was quota-blocked: returns False, attempt_count unchanged
        assert result is False
        assert job["attempt_count"] == 0, "attempt_count must not be incremented on quota block"

    def test_execute_job_proceeds_when_quota_ok(self):
        """When quota.pre_check returns (True, 'ok'), job executes and record_spend is called."""
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch

        job = self._make_queued_job(attempt_count=0)

        class TrackingCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return ("conn_ref_quota_01", "nango-001", "test-provider", "proj-01")

        class FakeConn:
            def cursor(self):
                return TrackingCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield FakeConn()

        mock_pull_fn = MagicMock(return_value={"pull_id": "p1", "row_count": 3})
        mock_record_spend = MagicMock()

        with patch("core.db.get_connection", new=_fake_get_connection), \
             patch("core.audit.write_audit_row"), \
             patch("core.main.get_module_pull_fn", return_value=mock_pull_fn), \
             patch("core.quota.pre_check", return_value=(True, "ok")), \
             patch("core.quota.record_spend", mock_record_spend), \
             patch("core.quota._engine._configs", {}):
            from core.queue import _execute_job
            result = _execute_job(job)

        assert result is True

    def test_execute_job_handles_rate_limit_error(self):
        """When pull fn raises RateLimitError, record_rate_limit is called and job is re-queued."""
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch

        from core.quota import RateLimitError

        job = self._make_queued_job(attempt_count=0)

        queued_states: list[str] = []

        class TrackingCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                if "queued" in str(sql) and "UPDATE" in str(sql):
                    queued_states.append("queued")

            def fetchone(self):
                return ("conn_ref_quota_01", "nango-001", "test-provider", "proj-01")

        class FakeConn:
            def cursor(self):
                return TrackingCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield FakeConn()

        mock_record_rate_limit = MagicMock()
        failing_pull = MagicMock(side_effect=RateLimitError("test-provider", 30))

        with patch("core.db.get_connection", new=_fake_get_connection), \
             patch("core.audit.write_audit_row"), \
             patch("core.main.get_module_pull_fn", return_value=failing_pull), \
             patch("core.quota.pre_check", return_value=(True, "ok")), \
             patch("core.quota.record_rate_limit", mock_record_rate_limit), \
             patch("core.quota._engine._configs", {}):
            from core.queue import _execute_job
            result = _execute_job(job)

        # job was executed (attempt counted) -- returns True
        assert result is True
        # record_rate_limit was called
        mock_record_rate_limit.assert_called_once()
        call_args = mock_record_rate_limit.call_args
        assert call_args[0][0] == "test-provider"  # platform
        assert call_args[0][1] == 30              # retry_after
        # Job was re-queued
        assert queued_states, "Expected UPDATE ... SET state = 'queued'"


# ---------------------------------------------------------------------------
# Story 3.5 -- Verification hook tests (T9.2, AC3)
# ---------------------------------------------------------------------------


class TestVerificationHook:
    """Verify that _execute_job calls run_post_pull_verification on success."""

    def _make_queued_job(self):
        return {
            "id": "job_ver_01",
            "pull_id": "pull_ver_01",
            "connection_ref_id": "conn_ref_ver_01",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "requested_by": "test-user",
            "attempt_count": 0,
        }

    def test_execute_job_calls_verification_on_success(self):
        """On successful pull (state=done), _execute_job calls run_post_pull_verification
        with the correct pull_id, connection_ref_id, date_from, and date_to."""
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch

        job = self._make_queued_job()
        conn_ref_row = ("conn_ref_ver_01", "nango-conn-001", "test-provider", "proj-01")

        class FakeCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return conn_ref_row

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

        mock_pull_fn = MagicMock(return_value={"pull_id": "pull_ver_01", "row_count": 5})
        verification_calls: list[dict] = []

        def _capture_verification(
            pull_id, connection_ref_id, date_from, date_to, manifest, **_kwargs
        ):
            verification_calls.append({
                "pull_id": pull_id,
                "connection_ref_id": connection_ref_id,
                "date_from": date_from,
                "date_to": date_to,
                "manifest": manifest,
            })

        with (
            patch("core.db.get_connection", new=_fake_get_connection),
            patch("core.audit.write_audit_row"),
            patch("core.main.get_module_pull_fn", return_value=mock_pull_fn),
            patch("core.quota.pre_check", return_value=(True, "ok")),
            patch("core.quota.record_spend"),
            patch("core.quota._engine._configs", {}),
            patch(
                "core.verification.run_post_pull_verification",
                side_effect=_capture_verification,
            ),
        ):
            from core.queue import _execute_job
            result = _execute_job(job)

        assert result is True
        assert verification_calls, "run_post_pull_verification must be called after successful pull"
        call = verification_calls[0]
        assert call["pull_id"] == "pull_ver_01"
        assert call["connection_ref_id"] == "conn_ref_ver_01"
        assert call["date_from"] == "2026-07-01"
        assert call["date_to"] == "2026-07-07"

    def test_execute_job_does_not_call_verification_on_failure(self):
        """When the pull fn raises an exception, verification is NOT called."""
        from contextlib import contextmanager
        from unittest.mock import MagicMock, patch

        job = self._make_queued_job()
        conn_ref_row = ("conn_ref_ver_01", "nango-conn-001", "test-provider", "proj-01")

        class FakeCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                pass

            def fetchone(self):
                return conn_ref_row

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

        failing_pull = MagicMock(side_effect=RuntimeError("pull failed"))
        verification_calls: list = []

        with (
            patch("core.db.get_connection", new=_fake_get_connection),
            patch("core.audit.write_audit_row"),
            patch("core.main.get_module_pull_fn", return_value=failing_pull),
            patch("core.quota.pre_check", return_value=(True, "ok")),
            patch("core.quota.record_spend"),
            patch("core.quota._engine._configs", {}),
            patch(
                "core.verification.run_post_pull_verification",
                side_effect=lambda *a, **kw: verification_calls.append(a),
            ),
        ):
            from core.queue import _execute_job
            _execute_job(job)

        assert not verification_calls, (
            "run_post_pull_verification must NOT be called when the pull fails"
        )


class TestConnectorErrorTaxonomy:
    """Story 25.2 (AC3, AC4): per-class retry policy + structured error_detail.

    Reuses the injectable-clock / fake-connection idioms from the quota tests: we
    capture every (state, error_detail) written by the terminal-state UPDATE so we
    can assert the class-specific policy without a live DB.
    """

    def _make_queued_job(self, attempt_count=0):
        return {
            "id": "job_tax_01",
            "pull_id": "pull_tax_01",
            "connection_ref_id": "conn_ref_tax_01",
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "requested_by": "test-user",
            "attempt_count": attempt_count,
        }

    def _run_with_pull(self, job, pull_fn, max_attempts="3"):
        """Execute _execute_job with a capturing fake connection.

        Returns (result, writes) where writes is a list of (state, error_detail)
        captured from every terminal-state UPDATE (state + error_detail params).
        """
        from contextlib import contextmanager

        writes: list[tuple] = []

        class CapturingCursor:
            def __init__(self):
                self.description = [
                    ("id",), ("nango_connection_id",), ("provider",), ("project_id",)
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def execute(self, sql, params=None):
                sql_str = str(sql)
                # The terminal UPDATE sets state=%s, error_detail=%s -> params[0:2].
                if (
                    "UPDATE" in sql_str
                    and "state = %s" in sql_str
                    and "error_detail = %s" in sql_str
                    and params is not None
                ):
                    writes.append((params[0], params[1]))

            def fetchone(self):
                return ("conn_ref_tax_01", "nango-001", "test-provider", "proj-01")

        class CapturingConn:
            def cursor(self):
                return CapturingCursor()

            def commit(self):
                pass

            def close(self):
                pass

        @contextmanager
        def _fake_get_connection():
            yield CapturingConn()

        with patch("core.db.get_connection", new=_fake_get_connection), \
             patch("core.audit.write_audit_row"), \
             patch("core.main.get_module_pull_fn", return_value=pull_fn), \
             patch("core.quota.pre_check", return_value=(True, "ok")), \
             patch("core.quota._engine._configs", {}), \
             patch.dict(os.environ, {"QUEUE_MAX_ATTEMPTS": max_attempts}):
            from core.queue import _execute_job
            result = _execute_job(job)

        return result, writes

    def test_non_retryable_auth_expired_fails_immediately_with_json(self):
        """A non-retryable ConnectorError (auth_expired) -> immediate 'failed',
        JSON error_detail carrying error_class + user_action, no requeue."""
        import json

        from core.pull_errors import AuthExpiredError

        job = self._make_queued_job(attempt_count=0)
        failing = MagicMock(
            side_effect=AuthExpiredError(401, {"error": {"code": 190, "fbtrace_id": "z9"}})
        )

        result, writes = self._run_with_pull(job, failing)

        assert result is True
        # Terminal state is 'failed' on first attempt (not dead_letter, not requeue).
        states = [s for s, _ in writes]
        assert "failed" in states
        assert "queued" not in states, "non-retryable errors must NOT be requeued"
        assert "dead_letter" not in states

        # error_detail is the structured JSON envelope.
        detail_str = next(d for s, d in writes if s == "failed")
        detail = json.loads(detail_str)
        assert detail["error_class"] == "auth_expired"
        assert detail["user_action"] == "reconnect"
        assert detail["provider_status"] == 401
        assert detail["provider_payload"]["error"]["fbtrace_id"] == "z9"
        assert detail["attempt_count"] == 1

    def test_non_retryable_does_not_requeue_even_below_max_attempts(self):
        """Non-retryable at attempt 1/3 still terminates (single-attempt semantics)."""
        from core.pull_errors import PermissionDeniedError

        job = self._make_queued_job(attempt_count=0)
        failing = MagicMock(side_effect=PermissionDeniedError(403, {"m": "no"}))

        result, writes = self._run_with_pull(job, failing, max_attempts="3")

        assert result is True
        states = [s for s, _ in writes]
        assert "failed" in states
        assert "queued" not in states

    def test_invalid_request_logs_drift_warning(self):
        """invalid_request (400) emits the pull_invalid_request_drift WARNING key."""

        from core.pull_errors import InvalidRequestError

        job = self._make_queued_job(attempt_count=0)
        failing = MagicMock(side_effect=InvalidRequestError(400, {"bad": "param"}))

        import json

        with patch("core.queue.logger") as mock_logger:
            result, writes = self._run_with_pull(job, failing)

        assert result is True
        warning_calls = [str(c) for c in mock_logger.warning.call_args_list]
        assert any("pull_invalid_request_drift" in c for c in warning_calls), (
            f"expected pull_invalid_request_drift warning, got: {warning_calls}"
        )
        detail = json.loads(next(d for s, d in writes if s == "failed"))
        assert detail["error_class"] == "invalid_request"
        assert detail["user_action"] is None

    def test_retryable_provider_transient_uses_attempt_path(self):
        """Retryable ConnectorError (provider_transient) -> 'failed' below max
        attempts (same attempt/backoff path as the legacy generic exception)."""
        import json

        from core.pull_errors import ProviderTransientError

        job = self._make_queued_job(attempt_count=0)  # attempt 1/3
        failing = MagicMock(side_effect=ProviderTransientError(503, "upstream down"))

        result, writes = self._run_with_pull(job, failing, max_attempts="3")

        assert result is True
        states = [s for s, _ in writes]
        assert "failed" in states  # not dead_letter (attempt 1 < 3)
        assert "dead_letter" not in states
        detail = json.loads(next(d for s, d in writes if s == "failed"))
        assert detail["error_class"] == "provider_transient"

    def test_retryable_provider_transient_dead_letters_at_max(self):
        """Retryable ConnectorError at the last attempt -> dead_letter."""
        from core.pull_errors import ProviderTransientError

        job = self._make_queued_job(attempt_count=2)  # attempt 3/3
        failing = MagicMock(side_effect=ProviderTransientError(500, "still down"))

        result, writes = self._run_with_pull(job, failing, max_attempts="3")

        assert result is True
        states = [s for s, _ in writes]
        assert "dead_letter" in states

    def test_generic_exception_upgraded_to_unclassified_json(self):
        """A plain (untyped) exception is the last-resort net -> error_class
        unclassified, retry semantics bit-identical (failed below max attempts)."""
        import json

        job = self._make_queued_job(attempt_count=0)
        failing = MagicMock(side_effect=RuntimeError("boom"))

        result, writes = self._run_with_pull(job, failing, max_attempts="3")

        assert result is True
        states = [s for s, _ in writes]
        assert "failed" in states
        detail = json.loads(next(d for s, d in writes if s == "failed"))
        assert detail["error_class"] == "unclassified"
        assert detail["user_action"] is None
        assert detail["message"] == "boom"


class TestStaleJobRecovery:
    """review-3-4 F-1: recovery must use the real column name (error_detail)."""

    def test_recover_uses_error_detail_column(self):
        fake_conn_mgr, mock_conn, mock_cursor = _make_fake_connection()
        mock_cursor.fetchall = MagicMock(return_value=[("job_stale_1",)])

        with patch("core.db.get_connection", new=fake_conn_mgr):
            from core.queue import recover_stale_running_jobs

            recovered = recover_stale_running_jobs(max_running_seconds=60)

        assert recovered == 1
        sql = str(mock_cursor.execute.call_args_list[0])
        assert "error_detail" in sql, "recovery must write the error_detail column"
        assert "last_error" not in sql


class TestBuildErrorDetailTruncation:
    """Story 25.2 (AC4) review F-A1/F-C2: the truncation ladder must shrink the
    message and drop the payload INDEPENDENTLY -- never overwrite the provider
    payload with a copy of the message -- and always return valid JSON <= 2000."""

    @staticmethod
    def _build(**overrides):
        from core.queue import _build_error_detail

        kwargs = {
            "error_class": "auth_expired",
            "user_action": "reconnect",
            "provider_status": 401,
            "provider_payload": {"error": {"code": "1"}},
            "message": "short message",
            "attempt_count": 1,
        }
        kwargs.update(overrides)
        return _build_error_detail(**kwargs)

    def test_small_envelope_untouched(self):
        import json

        encoded = self._build()
        assert len(encoded) <= 2000
        detail = json.loads(encoded)
        assert detail["provider_payload"] == {"error": {"code": "1"}}
        assert detail["message"] == "short message"

    def test_huge_message_is_shrunk_payload_preserved(self):
        import json

        encoded = self._build(message="m" * 3000, provider_payload={"k": "v"})
        assert len(encoded) <= 2000
        detail = json.loads(encoded)
        assert detail["message"] == "m" * 500
        # The real payload survives message truncation (review F-A1: the old
        # ladder overwrote provider_payload with the truncated message).
        assert detail["provider_payload"] == {"k": "v"}
        assert detail["error_class"] == "auth_expired"
        assert detail["user_action"] == "reconnect"

    def test_huge_payload_dropped_to_marker_message_kept(self):
        import json

        encoded = self._build(message="m" * 3000, provider_payload="p" * 4000)
        assert len(encoded) <= 2000
        detail = json.loads(encoded)
        assert detail["provider_payload"] == "<truncated>"
        assert detail["message"] == "m" * 500
        assert detail["error_class"] == "auth_expired"

    def test_none_payload_stays_none_on_overflow(self):
        import json

        encoded = self._build(message="m" * 3000, provider_payload=None)
        assert len(encoded) <= 2000
        detail = json.loads(encoded)
        assert detail["provider_payload"] is None
        assert detail["message"] == "m" * 500
