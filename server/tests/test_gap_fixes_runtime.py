"""Runtime robustness tests for G-12 gap fixes (review-global-gaps.md).

Covers:
  G-12 -- RateLimitError dead-letters after max_attempts (queue.py)
  G-12 -- RateLimitError requeues before max_attempts (queue.py)
  Dedup -- enqueue_pull ON CONFLICT fast-path returns existing job
  Scheduler -- advisory lock skips nightly steps when lock not acquired
  Scheduler -- advisory lock releases in finally even when steps fail
  Visibility -- QUEUE_RUNNING_VISIBILITY_SECONDS defaults to 5400
  Quota -- concurrent record_spend leaves consistent balance
  Airbyte -- trigger_sync echoes pull_id and includes it in payload

All tests are mock-based (no live DB, no network).
"""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

# Suppress background threads for entire module
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(fetchone=None, fetchall=None, description=None):
    """Build a minimal mock psycopg connection + cursor."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=fetchone)
    cur.fetchall = MagicMock(return_value=fetchall or [])
    cur.description = description or []

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()

    @contextmanager
    def _get():
        yield conn

    return _get, conn, cur


# ---------------------------------------------------------------------------
# G-12: RateLimitError dead-letters after max_attempts
# ---------------------------------------------------------------------------


def test_rate_limit_dead_letters_when_max_attempts_reached():
    """When attempt_count >= max_attempts, RateLimitError -> dead_letter."""
    from core.queue import _execute_job
    from core.quota import RateLimitError

    get_conn, mock_conn, mock_cur = _make_conn()

    job = {
        "id": "job_AAAA",
        "pull_id": "pull_BBBB",
        "connection_ref_id": "conn_001",
        "date_from": "2026-01-01",
        "date_to": "2026-01-07",
        "requested_by": "tester",
        "attempt_count": 2,  # next attempt = 3 = max_attempts (default 3)
        "trace_id": None,
    }

    def _boom_rate_limit(connection_id, date_from, date_to, project_id, pull_id):
        raise RateLimitError("test_platform", retry_after=60)

    fake_ref = {
        "id": "conn_001",
        "nango_connection_id": "nango_001",
        "provider": "test_platform",
        "project_id": "proj_01",
    }

    with (
        patch("core.db.get_connection", new=get_conn),
        patch("core.quota.pre_check", return_value=(True, "ok")),
        patch("core.quota.get_read_cost", return_value=1),
        patch("core.quota.record_rate_limit"),
        patch("core.queue._resolve_connection_ref", return_value=fake_ref),
        patch("core.main.get_module_pull_fn", return_value=_boom_rate_limit),
        patch("core.queue._get_manifest_for_provider", return_value={}),
        patch("core.tracing.worker_span") as mock_span,
        patch("core.tracing.traceparent_from_trace_id", return_value=None),
        patch("core.audit.write_audit_row"),
        patch.dict(os.environ, {"QUEUE_MAX_ATTEMPTS": "3"}),
    ):
        mock_span.return_value.__enter__ = MagicMock(return_value=MagicMock(set=MagicMock()))
        mock_span.return_value.__exit__ = MagicMock(return_value=False)
        _execute_job(job)

    # Find the UPDATE that sets state
    update_calls = [
        c for c in mock_cur.execute.call_args_list
        if "UPDATE" in str(c) and "pull_jobs" in str(c) and "dead_letter" in str(c)
    ]
    assert update_calls, (
        "Expected an UPDATE setting state='dead_letter' after rate_limit_exhausted"
    )


def test_rate_limit_requeues_before_max_attempts():
    """When attempt_count < max_attempts, RateLimitError -> requeue (state='queued')."""
    from core.queue import _execute_job
    from core.quota import RateLimitError

    get_conn, mock_conn, mock_cur = _make_conn()

    job = {
        "id": "job_CCCC",
        "pull_id": "pull_DDDD",
        "connection_ref_id": "conn_002",
        "date_from": "2026-01-01",
        "date_to": "2026-01-07",
        "requested_by": "tester",
        "attempt_count": 0,  # next attempt = 1 < max 3
        "trace_id": None,
    }

    def _boom_rate_limit(connection_id, date_from, date_to, project_id, pull_id):
        raise RateLimitError("test_platform", retry_after=30)

    fake_ref = {
        "id": "conn_002",
        "nango_connection_id": "nango_002",
        "provider": "test_platform",
        "project_id": "proj_02",
    }

    with (
        patch("core.db.get_connection", new=get_conn),
        patch("core.quota.pre_check", return_value=(True, "ok")),
        patch("core.quota.get_read_cost", return_value=1),
        patch("core.quota.record_rate_limit"),
        patch("core.queue._resolve_connection_ref", return_value=fake_ref),
        patch("core.main.get_module_pull_fn", return_value=_boom_rate_limit),
        patch("core.queue._get_manifest_for_provider", return_value={}),
        patch("core.tracing.worker_span") as mock_span,
        patch("core.tracing.traceparent_from_trace_id", return_value=None),
        patch("core.audit.write_audit_row"),
        patch.dict(os.environ, {"QUEUE_MAX_ATTEMPTS": "3"}),
    ):
        mock_span.return_value.__enter__ = MagicMock(return_value=MagicMock(set=MagicMock()))
        mock_span.return_value.__exit__ = MagicMock(return_value=False)
        _execute_job(job)

    # Should have a requeue UPDATE (state='queued') but NOT dead_letter
    update_calls_str = [str(c) for c in mock_cur.execute.call_args_list]
    queued_updates = [s for s in update_calls_str if "queued" in s and "pull_jobs" in s]
    dead_updates = [s for s in update_calls_str if "dead_letter" in s and "pull_jobs" in s]
    assert queued_updates, "Expected a requeue UPDATE (state='queued')"
    assert not dead_updates, "Expected NO dead_letter UPDATE before max_attempts"


# ---------------------------------------------------------------------------
# Dedup: ON CONFLICT fast-path (enqueue race returns existing job)
# ---------------------------------------------------------------------------


def test_enqueue_pull_conflict_returns_existing_job():
    """When INSERT returns no id (ON CONFLICT DO NOTHING), returns existing active job."""
    # Simulate: fast-path SELECT finds nothing, INSERT returns None (conflict),
    # follow-up SELECT finds the winner row.
    call_count = [0]

    @contextmanager
    def _get_conn():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)

        def _fetchone_side_effect():
            call_count[0] += 1
            if call_count[0] == 1:
                # Fast-path dedup SELECT: no existing job
                return None
            elif call_count[0] == 2:
                # INSERT ... RETURNING id: conflict -> no row returned
                return None
            else:
                # Follow-up SELECT for the winning row
                return ("job_WINNER", "pull_WINNER", "queued")

        cur.fetchone = MagicMock(side_effect=_fetchone_side_effect)
        cur.description = [("id",), ("pull_id",), ("state",)]

        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)
        conn.commit = MagicMock()
        yield conn

    with (
        patch("core.db.get_connection", new=_get_conn),
        patch("core.audit.write_audit_row"),
        patch("core.queue._capture_trace_id", return_value=None),
    ):
        from core.queue import LocalBackend
        backend = LocalBackend()
        result = backend.enqueue_pull(
            connection_ref_id="conn_race_001",
            date_from="2026-01-01",
            date_to="2026-01-07",
            requested_by="tester",
        )

    assert result["job_id"] == "job_WINNER"
    assert result["pull_id"] == "pull_WINNER"
    assert result["state"] == "queued"
    assert result.get("deduplicated") is True


# ---------------------------------------------------------------------------
# Scheduler: advisory lock prevents double-fire
# ---------------------------------------------------------------------------


def test_run_nightly_steps_skips_when_lock_not_acquired(caplog):
    """run_nightly_steps returns early (and logs) when advisory lock is False."""
    import logging

    with (
        patch("core.scheduler._try_advisory_lock", return_value=False),
        patch("core.scheduler.dispatch_nightly") as mock_dispatch,
    ):
        from core.scheduler import run_nightly_steps
        with caplog.at_level(logging.INFO, logger="core.scheduler"):
            run_nightly_steps(date.today())

    mock_dispatch.assert_not_called()
    skip_records = [r for r in caplog.records if "nightly_skipped" in r.getMessage()]
    assert skip_records, "Expected 'nightly_skipped' log when advisory lock not acquired"


def test_run_nightly_steps_releases_lock_even_when_step_fails():
    """Advisory lock is released in finally even when a step raises."""
    release_calls = []

    def _fake_release():
        release_calls.append(True)

    with (
        patch("core.scheduler._try_advisory_lock", return_value=True),
        patch("core.scheduler._release_advisory_lock", side_effect=_fake_release),
        patch("core.scheduler.dispatch_nightly", side_effect=RuntimeError("boom")),
        patch("core.scheduler._run_alert_check"),
        patch("core.scheduler._run_business_alert_check"),
        patch("core.scheduler._run_anomaly_alert_check"),
        patch("core.scheduler._run_due_notebooks"),
        patch("core.scheduler._run_due_briefings"),
        patch("core.scheduler._insert_meta_alert"),
        patch("core.scheduler._write_scheduler_step_degraded_alert"),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="TESTULID"))}),
    ):
        from core.scheduler import run_nightly_steps
        run_nightly_steps(date.today())

    assert release_calls, "Expected _release_advisory_lock to be called in finally"


def test_run_nightly_steps_proceeds_when_lock_unavailable(caplog):
    """When advisory lock returns None (Postgres down), steps still run."""

    step_ran = []

    def _fake_dispatch(**kwargs):
        step_ran.append("dispatch")

    with (
        patch("core.scheduler._try_advisory_lock", return_value=None),
        patch("core.scheduler._release_advisory_lock"),
        patch("core.scheduler.dispatch_nightly", side_effect=_fake_dispatch),
        patch("core.scheduler._run_alert_check"),
        patch("core.scheduler._run_business_alert_check"),
        patch("core.scheduler._run_anomaly_alert_check"),
        patch("core.scheduler._run_due_notebooks"),
        patch("core.scheduler._run_due_briefings"),
        patch.dict("sys.modules", {"ulid": MagicMock(ULID=MagicMock(return_value="TESTULID"))}),
        patch.dict(os.environ, {"ALERT_TIMEOUT_SECONDS": "9999"}),
    ):
        from core.scheduler import run_nightly_steps
        run_nightly_steps(date.today())

    assert "dispatch" in step_ran, "Steps must run even when advisory lock is unavailable"


# ---------------------------------------------------------------------------
# Visibility: QUEUE_RUNNING_VISIBILITY_SECONDS default raised to 5400
# ---------------------------------------------------------------------------


def test_running_visibility_default_is_5400(monkeypatch):
    """recover_stale_running_jobs reads 5400 when QUEUE_RUNNING_VISIBILITY_SECONDS is unset."""
    monkeypatch.delenv("QUEUE_RUNNING_VISIBILITY_SECONDS", raising=False)

    recovered_with: list[int] = []

    @contextmanager
    def _fake_conn():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall = MagicMock(return_value=[])

        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)
        conn.commit = MagicMock()

        # Capture the second parameter (max_running_seconds) from the UPDATE call
        def _cap_execute(sql, params=None):
            if params and len(params) >= 2 and "make_interval" in (sql or ""):
                recovered_with.append(params[1])

        cur.execute = MagicMock(side_effect=_cap_execute)
        yield conn

    with patch("core.db.get_connection", new=_fake_conn):
        from core.queue import recover_stale_running_jobs
        recover_stale_running_jobs()

    assert recovered_with, "Expected the UPDATE to have been called"
    assert recovered_with[0] == 5400, (
        f"Expected default visibility of 5400s, got {recovered_with[0]}"
    )


# ---------------------------------------------------------------------------
# Quota: concurrent record_spend leaves consistent balance
# ---------------------------------------------------------------------------


def test_quota_concurrent_record_spend_consistent():
    """Concurrent record_spend calls leave correct total balance."""
    from core.quota import QuotaEngine

    t = [0.0]

    def clock():
        return t[0]

    engine = QuotaEngine()
    engine.register_platform(
        "concurrent_test",
        window_seconds=3600,
        budget_points=1000,
        read_cost=1,
        write_cost=1,
    )
    # Anchor the window start
    engine._buckets["concurrent_test"]._window_start = 0.0
    engine._buckets["concurrent_test"]._balance = 1000

    def _spend_10():
        for _ in range(10):
            engine.record_spend("concurrent_test", 1, clock)

    threads = [threading.Thread(target=_spend_10) for _ in range(10)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    # 10 threads x 10 spends = 100 total spend; balance must be 900
    balance = engine._buckets["concurrent_test"]._balance
    assert balance == 900, f"Expected balance=900 after 100 concurrent spends, got {balance}"


def test_quota_pre_check_thread_safe_no_exception():
    """Concurrent pre_check calls do not raise."""
    from core.quota import QuotaEngine

    engine = QuotaEngine()
    engine.register_platform(
        "thread_check",
        window_seconds=3600,
        budget_points=10000,
        read_cost=1,
        write_cost=1,
    )

    errors: list[Exception] = []

    def _check():
        try:
            for _ in range(20):
                engine.pre_check("thread_check", 1)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_check) for _ in range(5)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert not errors, f"Unexpected exceptions during concurrent pre_check: {errors}"


# ---------------------------------------------------------------------------
# Mirror sync: unknown table name is ignored with a warning
# ---------------------------------------------------------------------------


def test_mirror_sync_ignores_unknown_table(caplog):
    """sync_tables silently drops unknown table names and warns."""
    import logging

    with caplog.at_level(logging.WARNING, logger="core.mirror_sync"):
        from core import mirror_sync
        # Provide an unknown table name that is not in _ALLOWED_TABLES
        result = mirror_sync.sync_tables(["totally_unknown_table_xyz"])

    # No rows synced for the unknown table
    assert "totally_unknown_table_xyz" not in result.get("synced", {}), (
        "Unknown table should not appear in synced dict"
    )
    warn_records = [
        r for r in caplog.records if "unknown_table_ignored" in r.getMessage()
    ]
    assert warn_records, "Expected WARNING log for unknown table name"


def test_mirror_sync_known_table_not_ignored(tmp_path):
    """A known table name passes through validation and is synced."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock, patch

    db_path = str(tmp_path / "known_table_test.duckdb")
    col_names = ["id", "project_id"]
    mock_cur = MagicMock()
    mock_cur.__enter__ = MagicMock(return_value=mock_cur)
    mock_cur.__exit__ = MagicMock(return_value=False)
    mock_cur.fetchall.return_value = []
    mock_cur.description = [(c,) for c in col_names]

    @contextmanager
    def _pg_conn():
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = mock_cur
        yield conn

    with (
        patch("core.db.get_connection", side_effect=_pg_conn),
        patch.dict(os.environ, {"TOOROW_DUCKDB_PATH": db_path}),
    ):
        from core import mirror_sync
        result = mirror_sync.sync_tables(["context_events"])

    assert "context_events" in result.get("synced", {}), (
        "Known table 'context_events' should appear in synced dict"
    )
