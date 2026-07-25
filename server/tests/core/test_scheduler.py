"""Unit tests for server/core/scheduler.py (Story 3.4, AC1, AC2, AC3).

Tests:
  - compute_nightly_work produces exactly 2 windows with correct dates
  - dispatch_nightly enqueues 2 windows per connection
  - dispatch_nightly with 0 connections returns empty list
  - start_nightly_scheduler disabled by default (HG-5)
  - start_nightly_scheduler starts a daemon thread named nightly-scheduler
  - POST /internal/scheduler/dispatch-nightly returns 404 for local backend
  - POST /internal/scheduler/dispatch-nightly returns 200 for cloud_tasks (mock)

Strategy:
  - All DB calls mocked -- no real Postgres required.
  - SCHEDULER_ENABLED=false in all tests (no daemon thread started accidentally).
  - Thread count verified for enabled/disabled cases.
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# Ensure background threads don't start during module import
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_CONN_COLS = ["id", "provider", "project_id"]


def _col_desc(cols: list[str]):
    """Build fake column descriptors where desc[i][0] is the column name."""
    return [
        type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})()
        for c in cols
    ]


def _make_fake_db(rows: list[tuple]):
    """Return a fake get_connection() context manager yielding rows."""
    mock_cursor = MagicMock()
    mock_cursor.__enter__ = MagicMock(return_value=mock_cursor)
    mock_cursor.__exit__ = MagicMock(return_value=False)
    mock_cursor.fetchall = MagicMock(return_value=rows)
    mock_cursor.description = _col_desc(_CONN_COLS)

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
# T9.1a -- compute_nightly_work
# ---------------------------------------------------------------------------


class TestComputeNightlyWork:
    def test_compute_nightly_work_produces_two_windows(self):
        """as_of_date=2026-07-11 -> re-pull 2026-07-03..2026-07-09, fresh 2026-07-10."""
        from core.scheduler import compute_nightly_work

        as_of = date(2026, 7, 11)
        windows = compute_nightly_work("conn_test", as_of)

        assert len(windows) == 2, f"Expected 2 windows, got {len(windows)}"

        repull, fresh = windows
        assert repull["date_from"] == "2026-07-03"
        assert repull["date_to"] == "2026-07-09"
        assert fresh["date_from"] == "2026-07-10"
        assert fresh["date_to"] == "2026-07-10"

    def test_compute_nightly_work_windows_are_dicts(self):
        """Each window dict must have date_from and date_to keys."""
        from core.scheduler import compute_nightly_work

        windows = compute_nightly_work("conn_x", date(2026, 1, 1))
        for w in windows:
            assert "date_from" in w
            assert "date_to" in w

    def test_compute_nightly_work_repull_never_overlaps_fresh(self):
        """Re-pull window must end before the fresh window starts."""
        from core.scheduler import compute_nightly_work

        windows = compute_nightly_work("conn_x", date(2026, 7, 11))
        repull, fresh = windows
        # repull_to < fresh_from (no overlap with yesterday)
        assert repull["date_to"] < fresh["date_from"]


# ---------------------------------------------------------------------------
# T9.1b -- dispatch_nightly
# ---------------------------------------------------------------------------


class TestDispatchNightly:
    """Tests for the legacy fallback path in dispatch_nightly.

    Story 8.2: dispatch_nightly now calls _dispatch_nightly_datastreams first.
    All tests here patch _dispatch_nightly_datastreams to ([], 0) so the legacy
    fallback path (per-connection 2-window dispatch) runs under test.
    """

    def test_dispatch_nightly_enqueues_per_connection_per_window(self):
        """2 connections -> enqueue_pull called 4 times (2 x 2 windows)."""
        from core.scheduler import dispatch_nightly

        conn_rows = [
            ("conn_001", "google-analytics", "proj_A"),
            ("conn_002", "google-analytics", "proj_B"),
        ]
        fake_db, _, mock_cursor = _make_fake_db(conn_rows)
        mock_cursor.description = _col_desc(_CONN_COLS)
        mock_cursor.fetchall.return_value = conn_rows

        mock_job = {"job_id": "job_test", "pull_id": "pull_test", "state": "queued"}

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=fake_db), \
             patch("core.queue.enqueue_pull", return_value=mock_job) as mock_enqueue:
            result = dispatch_nightly(as_of_date=date(2026, 7, 11))

        assert mock_enqueue.call_count == 4, (
            f"Expected 4 calls (2 conns x 2 windows), got {mock_enqueue.call_count}"
        )
        assert len(result) == 4

    def test_dispatch_nightly_empty_connections(self):
        """0 connections -> empty list without error."""
        from core.scheduler import dispatch_nightly

        fake_db, _, mock_cursor = _make_fake_db([])
        mock_cursor.description = _col_desc(_CONN_COLS)
        mock_cursor.fetchall.return_value = []

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=fake_db), \
             patch("core.queue.enqueue_pull") as mock_enqueue:
            result = dispatch_nightly(as_of_date=date(2026, 7, 11))

        assert result == []
        mock_enqueue.assert_not_called()

    def test_dispatch_nightly_uses_today_when_as_of_date_is_none(self):
        """dispatch_nightly defaults as_of_date to date.today() when not given."""
        from core.scheduler import dispatch_nightly

        fake_db, _, mock_cursor = _make_fake_db([])
        mock_cursor.description = _col_desc(_CONN_COLS)
        mock_cursor.fetchall.return_value = []

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=fake_db):
            result = dispatch_nightly()

        assert result == []

    def test_dispatch_nightly_returns_jobs_from_enqueue(self):
        """Jobs returned by enqueue_pull are collected and returned."""
        from core.scheduler import dispatch_nightly

        conn_rows = [("conn_single", "google-analytics", "proj_A")]
        fake_db, _, mock_cursor = _make_fake_db(conn_rows)
        mock_cursor.description = _col_desc(_CONN_COLS)
        mock_cursor.fetchall.return_value = conn_rows

        job_a = {"job_id": "job_A", "pull_id": "pull_A", "state": "queued"}
        job_b = {"job_id": "job_B", "pull_id": "pull_B", "state": "queued"}
        enqueue_results = [job_a, job_b]
        call_idx = [0]

        def _mock_enqueue(*args, **kwargs):
            idx = call_idx[0]
            call_idx[0] += 1
            return enqueue_results[idx]

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=fake_db), \
             patch("core.queue.enqueue_pull", side_effect=_mock_enqueue):
            result = dispatch_nightly(as_of_date=date(2026, 7, 11))

        assert len(result) == 2
        assert result[0]["job_id"] == "job_A"
        assert result[1]["job_id"] == "job_B"

    def test_dispatch_nightly_redispatch_is_safe_deduplicated(self):
        """Re-dispatching an already-queued window is safe -- deduplicated=True.

        enqueue_pull() returns deduplicated=True when the same connection+window
        is already pending (review-3-2 F-2 idempotency). dispatch_nightly passes
        it through unchanged.
        """
        from core.scheduler import dispatch_nightly

        conn_rows = [("conn_dup", "google-analytics", "proj_A")]
        fake_db, _, mock_cursor = _make_fake_db(conn_rows)
        mock_cursor.description = _col_desc(_CONN_COLS)
        mock_cursor.fetchall.return_value = conn_rows

        dedup_job = {
            "job_id": "job_existing",
            "pull_id": "pull_existing",
            "state": "queued",
            "deduplicated": True,
        }

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)), \
             patch("core.db.get_connection", new=fake_db), \
             patch("core.queue.enqueue_pull", return_value=dedup_job) as mock_enqueue:
            result = dispatch_nightly(as_of_date=date(2026, 7, 11))

        # Still calls enqueue_pull for both windows (idempotency inside the fn)
        assert mock_enqueue.call_count == 2
        assert all(r.get("deduplicated") is True for r in result)


# ---------------------------------------------------------------------------
# T9.1c -- start_nightly_scheduler thread tests
# ---------------------------------------------------------------------------


class TestStartNightlyScheduler:
    def test_start_nightly_scheduler_disabled_by_default(self):
        """SCHEDULER_ENABLED=false -> no nightly-scheduler thread starts."""
        from core.scheduler import start_nightly_scheduler

        before = {t.name for t in threading.enumerate()}

        with patch.dict(os.environ, {"SCHEDULER_ENABLED": "false"}):
            start_nightly_scheduler()

        after = {t.name for t in threading.enumerate()}
        assert "nightly-scheduler" not in after - before

    def test_start_nightly_scheduler_disabled_when_env_unset(self):
        """SCHEDULER_ENABLED not set -> no thread starts."""
        from core.scheduler import start_nightly_scheduler

        env = {k: v for k, v in os.environ.items() if k != "SCHEDULER_ENABLED"}
        before = {t.name for t in threading.enumerate()}

        with patch.dict(os.environ, env, clear=True):
            start_nightly_scheduler()

        after = {t.name for t in threading.enumerate()}
        assert "nightly-scheduler" not in after - before

    def test_start_nightly_scheduler_enabled_starts_daemon_thread(self):
        """SCHEDULER_ENABLED=true -> daemon thread named nightly-scheduler starts."""
        stop_event = threading.Event()

        def _fake_loop(_check_interval_seconds=60):
            stop_event.wait(timeout=5)

        # Skip if a thread already exists from a previous test in the same process
        if any(t.name == "nightly-scheduler" for t in threading.enumerate()):
            pytest.skip("nightly-scheduler thread already exists -- skip start test")

        with patch("core.scheduler._scheduler_loop", side_effect=_fake_loop), \
             patch.dict(os.environ, {"SCHEDULER_ENABLED": "true"}):
            from core.scheduler import start_nightly_scheduler
            start_nightly_scheduler()

        found = [t for t in threading.enumerate() if t.name == "nightly-scheduler"]
        stop_event.set()
        assert found, "Expected a daemon thread named 'nightly-scheduler'"
        assert found[0].daemon is True


# ---------------------------------------------------------------------------
# T9.1d -- /internal/scheduler/dispatch-nightly endpoint
# ---------------------------------------------------------------------------


class TestInternalDispatchEndpoint:
    def test_internal_dispatch_endpoint_returns_404_for_local_backend(self):
        """QUEUE_BACKEND=local -> POST /internal/... returns 404."""
        with patch.dict(os.environ, {
            "QUEUE_BACKEND": "local",
            "SCHEDULER_ENABLED": "false",
            "HEALTH_POLLER_ENABLED": "false",
            "QUEUE_WORKER_ENABLED": "false",
        }):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/internal/scheduler/dispatch-nightly")

        assert response.status_code == 404

    def test_internal_dispatch_endpoint_returns_200_for_cloud_tasks_backend(self):
        """QUEUE_BACKEND=cloud_tasks -> POST /internal/... calls dispatch_nightly, 200."""
        mock_jobs = [{"job_id": "job_1", "pull_id": "pull_1", "state": "queued"}]

        with patch.dict(os.environ, {
            "QUEUE_BACKEND": "cloud_tasks",
            "SCHEDULER_ENABLED": "false",
            "HEALTH_POLLER_ENABLED": "false",
            "QUEUE_WORKER_ENABLED": "false",
        }), patch("core.scheduler.dispatch_nightly", return_value=mock_jobs):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/internal/scheduler/dispatch-nightly")

        assert response.status_code == 200
        data = response.json()
        assert "jobs" in data
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["job_id"] == "job_1"


# ---------------------------------------------------------------------------
# AI-46 -- date_window_days wired into _dispatch_nightly_datastreams
# ---------------------------------------------------------------------------

# Column names returned by the datastreams SELECT in _dispatch_nightly_datastreams.
_DS_COLS = [
    "ds_id", "project_id", "module_name", "refetch_days", "date_window_days",
    "connection_ref_id", "cr_status", "cr_enabled",
]

_AS_OF = date(2026, 7, 14)  # yields yesterday = 2026-07-13


def _make_ds_fake_db(ds_rows: list[tuple]):
    """Return a get_connection() context-manager for _dispatch_nightly_datastreams tests.

    The function under test issues a single SELECT (datastreams) in ONE get_connection()
    call.  We use a plain object to avoid type(MagicMock).description= polluting other
    tests (MagicMock uses a shared metaclass whose class attributes are global).
    """
    rows_ref = list(ds_rows)
    cols = _col_desc(_DS_COLS)

    class _Cursor:
        def __init__(self):
            self.description = cols

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            pass

        def fetchall(self):
            return rows_ref

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def cursor(self):
            return _Cursor()

        def commit(self):
            pass

    @contextmanager
    def _fake_get_connection():
        yield _Conn()

    return _fake_get_connection


def _ds_row(
    *,
    ds_id: str = "ds_001",
    project_id: str = "proj_A",
    module_name: str = "google-analytics",
    refetch_days: int | None = 3,
    date_window_days: int | None = None,
    connection_ref_id: str = "conn_001",
    cr_status: str = "active",
    cr_enabled: bool = True,
) -> tuple:
    """Build a fake datastream row tuple matching _DS_COLS order."""
    return (
        ds_id, project_id, module_name, refetch_days, date_window_days,
        connection_ref_id, cr_status, cr_enabled,
    )


class TestDateWindowDaysDispatch:
    """AI-46: _dispatch_nightly_datastreams uses date_window_days as pull window length.

    Precedence (highest to lowest):
        1. date_window_days (non-NULL, > 0) -- per-stream override
        2. refetch_days     (non-NULL, > 0) -- per-stream fallback
        3. global default 3                 -- defensive last resort
    """

    def _run_dispatch(self, ds_rows, as_of=_AS_OF):
        """Run _dispatch_nightly_datastreams with mocked DB and a capturing queue."""
        from core.scheduler import _dispatch_nightly_datastreams

        enqueued_calls: list[dict] = []

        class _FakeQueue:
            @staticmethod
            def enqueue_pull(conn_id, date_from, date_to, *, requested_by, datastream_id):
                enqueued_calls.append({
                    "conn_id": conn_id,
                    "date_from": date_from,
                    "date_to": date_to,
                    "ds_id": datastream_id,
                })
                return {"job_id": "j", "pull_id": "p", "state": "queued"}

        fake_db = _make_ds_fake_db(ds_rows)
        jobs, count = _dispatch_nightly_datastreams(
            as_of, "scheduler", _FakeQueue, fake_db
        )
        return enqueued_calls, jobs, count

    def test_date_window_days_set_uses_that_window(self):
        """date_window_days=14 -> pull window is 14 days ending yesterday.

        yesterday = 2026-07-13, window_days=14:
            date_from = 2026-07-13 - 13 days = 2026-06-30
            date_to   = 2026-07-13
        """
        row = _ds_row(date_window_days=14, refetch_days=3)
        calls, jobs, count = self._run_dispatch([row])

        assert count == 1
        assert len(calls) == 1
        assert calls[0]["date_from"] == "2026-06-30"
        assert calls[0]["date_to"] == "2026-07-13"

    def test_date_window_days_null_falls_back_to_refetch_days(self):
        """date_window_days=NULL, refetch_days=7 -> pull window is 7 days.

        yesterday = 2026-07-13, window_days=7:
            date_from = 2026-07-13 - 6 days = 2026-07-07
            date_to   = 2026-07-13
        """
        row = _ds_row(date_window_days=None, refetch_days=7)
        calls, jobs, count = self._run_dispatch([row])

        assert count == 1
        assert len(calls) == 1
        assert calls[0]["date_from"] == "2026-07-07"
        assert calls[0]["date_to"] == "2026-07-13"

    def test_both_null_falls_back_to_global_default_3(self):
        """date_window_days=NULL, refetch_days=NULL -> pull window is 3 days (global default).

        yesterday = 2026-07-13, window_days=3:
            date_from = 2026-07-13 - 2 days = 2026-07-11
            date_to   = 2026-07-13
        """
        row = _ds_row(date_window_days=None, refetch_days=None)
        calls, jobs, count = self._run_dispatch([row])

        assert count == 1
        assert len(calls) == 1
        assert calls[0]["date_from"] == "2026-07-11"
        assert calls[0]["date_to"] == "2026-07-13"

    def test_date_window_days_takes_precedence_over_refetch_days(self):
        """When both are set, date_window_days wins.

        date_window_days=30, refetch_days=3  -> window = 30 days INCLUSIVE ending
        yesterday (same convention as refetch_days: date_from = yesterday - (N-1)).
        yesterday = 2026-07-13, date_from = 2026-06-14.
        """
        row = _ds_row(date_window_days=30, refetch_days=3)
        calls, jobs, count = self._run_dispatch([row])

        assert count == 1
        assert len(calls) == 1
        assert calls[0]["date_from"] == "2026-06-14"
        assert calls[0]["date_to"] == "2026-07-13"

    def test_inactive_connection_skipped_regardless_of_window(self):
        """Connection inactive -> skipped; no enqueue even with date_window_days set."""
        row = _ds_row(date_window_days=14, cr_status="inactive")
        calls, jobs, count = self._run_dispatch([row])

        assert count == 0
        assert calls == []

    def test_no_connection_ref_skipped(self):
        """connection_ref_id=None -> datastream skipped (not yet linked to a connection)."""
        row = _ds_row(date_window_days=14, connection_ref_id=None)
        calls, jobs, count = self._run_dispatch([row])

        assert count == 0
        assert calls == []
