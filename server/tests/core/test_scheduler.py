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
    """The Cloud Scheduler trigger, under the AD-36 contract (story 56.5).

    TWO ASSERTIONS CHANGED HERE, AND BOTH WERE DELIBERATE CHANGES OF INTENT --
    not tests bent to fit new code:

      * the endpoint used to answer 404 unless QUEUE_BACKEND=cloud_tasks. That
        coupled two independent things -- who carries the CLOCK, and how a job is
        DELIVERED -- and made the endpoint untestable in the very mode a
        deployment starts in. Cloud Scheduler is the right trigger either way.
      * it used to require the shared secret AND a user Bearer token. Cloud
        Scheduler carries no Bearer token: it is the platform calling itself. The
        conjunction would have answered 401 to every scheduled run.
    """

    def test_the_clock_endpoint_is_reachable_whatever_the_queue_backend(self):
        """The 404 gate is gone: the clock does not depend on the delivery mode.

        AI-127 CHANGED WHAT THIS TEST HAD TO DO, without changing what it means.
        It used to call unauthorized and read the status directly, because the
        only 404 the route could produce was the QUEUE_BACKEND gate. Now an
        unauthorized caller ALSO gets 404 -- so calling that way would leave the
        assertion unable to tell "the backend hid the route" from "you were
        refused", and it would pass for the wrong reason the day the gate came
        back. The caller is therefore authorized the way the platform actually
        is, by the shared secret, and the 404 recovers its single meaning.
        """
        with patch.dict(os.environ, {
            "QUEUE_BACKEND": "local",
            "INTERNAL_ENDPOINTS_REQUIRE_HEADER": "s3cret",
            "SCHEDULER_ENABLED": "false",
            "HEALTH_POLLER_ENABLED": "false",
            "QUEUE_WORKER_ENABLED": "false",
        }), patch("core.scheduler.dispatch_nightly", return_value=[]):
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/internal/scheduler/dispatch-nightly",
                headers={"X-Internal-Auth": "s3cret"},
            )

        assert response.status_code != 404, "the local backend no longer hides the clock"
        assert response.status_code == 200

    def test_the_platform_calls_it_with_the_shared_secret_and_no_user_token(self):
        with patch.dict(os.environ, {
            "QUEUE_BACKEND": "cloud_tasks",
            "INTERNAL_ENDPOINTS_REQUIRE_HEADER": "s3cret",
            "SCHEDULER_ENABLED": "false",
            "HEALTH_POLLER_ENABLED": "false",
            "QUEUE_WORKER_ENABLED": "false",
        }), patch("core.scheduler.run_nightly_steps") as run_nightly:
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post(
                "/internal/scheduler/dispatch-nightly",
                headers={"X-Internal-Auth": "s3cret"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["ran"] == "nightly_steps"
        run_nightly.assert_called_once()

    def test_push_mode_without_a_secret_answers_503_so_the_run_comes_back(self):
        """A misconfiguration must not read like a broken scheduler.

        Without the secret no task and no scheduled call can ever authenticate.
        503 makes the caller retry once an operator sets it; 401 would look like
        a permissions bug and the run would be lost.
        """
        env = {
            "QUEUE_BACKEND": "cloud_tasks",
            "SCHEDULER_ENABLED": "false",
            "HEALTH_POLLER_ENABLED": "false",
            "QUEUE_WORKER_ENABLED": "false",
        }
        with patch.dict(os.environ, env):
            os.environ.pop("INTERNAL_ENDPOINTS_REQUIRE_HEADER", None)
            from core.main import build_asgi_app
            from starlette.testclient import TestClient

            app = build_asgi_app()
            client = TestClient(app, raise_server_exceptions=False)
            response = client.post("/internal/scheduler/dispatch-nightly")

        assert response.status_code == 503


# ---------------------------------------------------------------------------
# AI-46 -- date_window_days wired into _dispatch_nightly_datastreams
# ---------------------------------------------------------------------------

# Column names returned by the datastreams SELECT in _dispatch_nightly_datastreams.
# The gate columns are here because the SELECT really projects them: since
# 2026-08-12 the dispatcher asks `datastream_dispatch.gate_refusal` the same
# question the manual door asks, and that guard answers `row_incomplete` for a
# column nobody showed it rather than waving the row through.
_DS_COLS = [
    "ds_id", "project_id", "module_name", "refetch_days", "date_window_days",
    "connection_ref_id", "cr_status", "cr_enabled",
    # `archived_at` joins the projection 2026-08-18: `gate_refusal` reads the
    # column the soft archive really writes, and answers `row_incomplete` for a
    # key the double did not project rather than letting it pass.
    "enabled", "lifecycle_state", "archived_at", "source_kind",
    "current_plan_version_id", "current_mapping_version_id",
    "module_enabled", "project_status",
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
    enabled: bool = True,
    lifecycle_state: str = "active",
    archived_at: str | None = None,
    source_kind: str = "connector_pull",
    current_plan_version_id: str = "dsp_001",
    current_mapping_version_id: str = "dsm_001",
    module_enabled: bool | None = True,
    project_status: str = "active",
) -> tuple:
    """Build a fake datastream row tuple matching _DS_COLS order."""
    return (
        ds_id, project_id, module_name, refetch_days, date_window_days,
        connection_ref_id, cr_status, cr_enabled,
        enabled, lifecycle_state, archived_at, source_kind,
        current_plan_version_id, current_mapping_version_id,
        module_enabled, project_status,
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
            # Story 63.1: `execution_id` is part of the real signature (the run
            # this window belongs to). A double that omitted it made the dispatch
            # swallow a TypeError and enqueue NOTHING -- the AI-97 divergence
            # again, one layer up.
            #
            # AND IT HAPPENED A THIRD TIME, 2026-08-17. AI-301 added `module_name`
            # to the real `enqueue_pull` -- the module the caller already resolved,
            # because the gate inside used to re-read it on an RLS-armed connection
            # that could not see the row -- and this double kept the old shape. Four
            # tests went red saying `enqueued_calls == []`, which reads as "the
            # dispatch decided not to enqueue" and is not what happened: it raised
            # TypeError and swallowed it.
            #
            # `**kwargs` is deliberate now. A double that enumerates the keywords of
            # a signature it does not own re-breaks on every legitimate addition,
            # and each time the failure lies about its cause. What this test is
            # about is the WINDOW, so it captures the window and lets the rest pass
            # through -- and `test_the_fake_queue_matches_the_real_signature` below
            # holds the conformance the enumeration was pretending to hold.
            def enqueue_pull(
                conn_id, date_from, date_to, *, requested_by, datastream_id,
                execution_id=None, **kwargs,
            ):
                enqueued_calls.append({
                    "conn_id": conn_id,
                    "date_from": date_from,
                    "date_to": date_to,
                    "ds_id": datastream_id,
                    "execution_id": execution_id,
                    **kwargs,
                })
                return {"job_id": "j", "pull_id": "p", "state": "queued"}

        # Exposed so the conformance test below can introspect the very double the
        # dispatch was handed, rather than a copy of it that could drift apart.
        type(self)._captured_double = _FakeQueue.enqueue_pull

        fake_db = _make_ds_fake_db(ds_rows)
        jobs, count = _dispatch_nightly_datastreams(
            as_of, "scheduler", _FakeQueue, fake_db
        )
        return enqueued_calls, jobs, count

    def test_the_fake_queue_accepts_every_keyword_the_dispatch_sends(self):
        """The conformance the enumerated signature was pretending to hold.

        THREE TIMES NOW, the same failure: the real `enqueue_pull` gains a
        keyword, this double does not, the dispatch raises TypeError and swallows
        it, and the tests report `enqueued_calls == []` -- which reads as a
        DECISION not to enqueue and is nothing of the sort. AI-97 the first time,
        Story 63.1's `execution_id` the second, AI-301's `module_name` the third.

        So the double stops enumerating and this test binds it to the subject
        instead: every keyword-or-positional parameter of the real module-level
        `enqueue_pull` must be acceptable here. It fails on the addition, at the
        line that explains it, instead of four windows away.
        """
        import inspect

        from core import queue as real_queue

        real = inspect.signature(real_queue.enqueue_pull)
        calls, _jobs, count = self._run_dispatch(
            [_ds_row(date_window_days=7, refetch_days=3)]
        )

        # The dispatch enqueued: had the double refused a keyword, the TypeError
        # would have been swallowed and this list would be empty.
        assert count == 1
        assert len(calls) == 1, (
            "the dispatch enqueued nothing. Before reading this as a decision, "
            "check the double's signature against `core.queue.enqueue_pull`: a "
            "refused keyword raises TypeError inside the dispatch, which swallows "
            "it and reports an empty list -- three times so far."
        )

        # And the double must be able to ACCEPT every keyword the real function
        # declares, not merely the ones today's caller happens to send.
        every_keyword = {
            name: None
            for name, p in real.parameters.items()
            if p.kind is inspect.Parameter.KEYWORD_ONLY
        }
        every_keyword.setdefault("requested_by", "x")
        every_keyword.setdefault("datastream_id", "ds")
        double = type(self)._captured_double
        try:
            inspect.signature(double).bind("c", "2026-01-01", "2026-01-02", **every_keyword)
        except TypeError as exc:
            raise AssertionError(
                f"the capturing double cannot accept the real signature: {exc}. "
                "Add **kwargs rather than enumerating -- an enumeration re-breaks "
                "on every legitimate addition and lies about why."
            ) from None

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
