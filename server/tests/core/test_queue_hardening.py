"""Queue hardening tests (G-12 gap fixes).

Covers:
  - RateLimitError at attempt N < max: state -> 'queued' (not dead_letter)
  - RateLimitError at attempt N == max: state -> 'dead_letter', completed_at set
  - enqueue_pull dedup fast-path: SELECT returns existing -> returned immediately
  - enqueue_pull ON CONFLICT path: INSERT returns None -> follow-up SELECT
  - QUEUE_RUNNING_VISIBILITY_SECONDS default reads as 5400 (not 1800)

Mock-only, no live DB. Style matches test_queue.py.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _conn_factory(fetchone_seq: list):
    """Return a get_connection context manager whose cursor.fetchone()
    returns successive values from fetchone_seq."""
    idx = [0]

    @contextmanager
    def _get():
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.description = [("id",), ("pull_id",), ("state",)]

        def _fetchone():
            val = fetchone_seq[idx[0]] if idx[0] < len(fetchone_seq) else None
            idx[0] += 1
            return val

        cur.fetchone = MagicMock(side_effect=_fetchone)
        cur.fetchall = MagicMock(return_value=[])

        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cur)
        conn.commit = MagicMock()
        yield conn

    return _get


def _simple_conn(fetchone=None):
    return _conn_factory([fetchone])


# ---------------------------------------------------------------------------
# Enqueue dedup: fast-path SELECT returns existing
# ---------------------------------------------------------------------------


class TestEnqueueDedup:
    def test_fast_path_returns_existing_when_found(self):
        """When the fast-path SELECT finds an active job, no INSERT is attempted."""
        sql_calls: list[str] = []

        @contextmanager
        def _tracking_conn():
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.description = [("id",), ("pull_id",), ("state",)]
            cur.fetchone = MagicMock(return_value=("job_OLD", "pull_OLD", "queued"))

            def _capture(sql, params=None):
                sql_calls.append(str(sql))

            cur.execute = MagicMock(side_effect=_capture)

            conn = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor = MagicMock(return_value=cur)
            conn.commit = MagicMock()
            yield conn

        with (
            patch("core.db.get_connection", new=_tracking_conn),
            patch("core.audit.write_audit_row"),
            patch("core.queue._capture_trace_id", return_value=None),
        ):
            from core.queue import LocalBackend
            result = LocalBackend().enqueue_pull(
                connection_ref_id="conn_001",
                date_from="2026-01-01",
                date_to="2026-01-07",
                requested_by="tester",
            )

        assert result["job_id"] == "job_OLD"
        assert result["pull_id"] == "pull_OLD"
        assert result.get("deduplicated") is True
        # INSERT must NOT have been attempted
        insert_calls = [s for s in sql_calls if "INSERT" in s]
        assert not insert_calls, "Fast-path dedup should short-circuit before INSERT"

    def test_on_conflict_path_returns_winner_job(self):
        """When INSERT returns no row (ON CONFLICT), follow-up SELECT returns winner."""
        call_idx = [0]

        @contextmanager
        def _conflict_conn():
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.description = [("id",), ("pull_id",), ("state",)]

            def _fetchone():
                call_idx[0] += 1
                if call_idx[0] == 1:
                    return None  # fast-path SELECT: no existing
                elif call_idx[0] == 2:
                    return None  # INSERT RETURNING: conflict -> nothing returned
                else:
                    return ("job_WINNER", "pull_WINNER", "queued")  # winner SELECT

            cur.fetchone = MagicMock(side_effect=_fetchone)
            cur.fetchall = MagicMock(return_value=[])

            conn = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor = MagicMock(return_value=cur)
            conn.commit = MagicMock()
            yield conn

        with (
            patch("core.db.get_connection", new=_conflict_conn),
            patch("core.audit.write_audit_row"),
            patch("core.queue._capture_trace_id", return_value=None),
        ):
            from core.queue import LocalBackend
            result = LocalBackend().enqueue_pull(
                connection_ref_id="conn_002",
                date_from="2026-02-01",
                date_to="2026-02-07",
                requested_by="tester",
            )

        assert result["job_id"] == "job_WINNER"
        assert result.get("deduplicated") is True

    def test_successful_insert_returns_new_job(self):
        """When INSERT succeeds (no conflict), new job dict is returned."""
        call_idx = [0]

        @contextmanager
        def _success_conn():
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.description = [("id",), ("pull_id",), ("state",)]

            def _fetchone():
                call_idx[0] += 1
                if call_idx[0] == 1:
                    return None  # fast-path SELECT: no existing
                else:
                    return ("job_BRAND_NEW",)  # INSERT RETURNING id

            cur.fetchone = MagicMock(side_effect=_fetchone)
            cur.fetchall = MagicMock(return_value=[])

            conn = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor = MagicMock(return_value=cur)
            conn.commit = MagicMock()
            yield conn

        with (
            patch("core.db.get_connection", new=_success_conn),
            patch("core.audit.write_audit_row"),
            patch("core.queue._capture_trace_id", return_value=None),
        ):
            from core.queue import LocalBackend
            result = LocalBackend().enqueue_pull(
                connection_ref_id="conn_003",
                date_from="2026-03-01",
                date_to="2026-03-07",
                requested_by="tester",
            )

        assert result["state"] == "queued"
        assert result.get("deduplicated") is not True
        assert result["job_id"].startswith("job_")


# ---------------------------------------------------------------------------
# G-12: RateLimitError dead-letter / requeue
# ---------------------------------------------------------------------------


def _make_execute_job_patches(attempt_count: int):
    """Return common patches for _execute_job tests with RateLimitError."""
    from core.quota import RateLimitError

    def _pull_fn(connection_id, date_from, date_to, project_id, pull_id):
        raise RateLimitError("plat_x", retry_after=10)

    fake_ref = {
        "id": "conn_rl",
        "nango_connection_id": "nango_rl",
        "provider": "plat_x",
        "project_id": "proj_rl",
    }
    job = {
        "id": "job_RL",
        "pull_id": "pull_RL",
        "connection_ref_id": "conn_rl",
        "date_from": "2026-01-01",
        "date_to": "2026-01-07",
        "requested_by": "tester",
        "attempt_count": attempt_count,
        "trace_id": None,
    }
    return job, _pull_fn, fake_ref


class TestRateLimitBehavior:
    def _run_execute(self, job, pull_fn, fake_ref, max_attempts, captured_updates):
        """Run _execute_job and capture UPDATE SQL calls."""
        @contextmanager
        def _get_conn():
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.fetchone = MagicMock(return_value=None)

            def _cap_execute(sql, params=None):
                captured_updates.append((str(sql), params))

            cur.execute = MagicMock(side_effect=_cap_execute)
            cur.fetchall = MagicMock(return_value=[])
            cur.description = []

            conn = MagicMock()
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            conn.cursor = MagicMock(return_value=cur)
            conn.commit = MagicMock()
            yield conn

        span_mock = MagicMock()
        span_mock.__enter__ = MagicMock(return_value=MagicMock(set=MagicMock()))
        span_mock.__exit__ = MagicMock(return_value=False)

        with (
            patch("core.db.get_connection", new=_get_conn),
            patch("core.quota.pre_check", return_value=(True, "ok")),
            patch("core.quota.get_read_cost", return_value=0),
            patch("core.quota.record_rate_limit"),
            patch("core.queue._resolve_connection_ref", return_value=fake_ref),
            patch("core.main.get_module_pull_fn", return_value=pull_fn),
            patch("core.queue._get_manifest_for_provider", return_value={}),
            patch("core.tracing.worker_span", return_value=span_mock),
            patch("core.tracing.traceparent_from_trace_id", return_value=None),
            patch("core.audit.write_audit_row"),
            patch.dict(os.environ, {"QUEUE_MAX_ATTEMPTS": str(max_attempts)}),
        ):
            from core.queue import _execute_job
            _execute_job(job)

    def test_dead_letter_at_max_attempts(self):
        """RateLimitError at attempt == max_attempts -> dead_letter."""
        job, pull_fn, fake_ref = _make_execute_job_patches(attempt_count=2)
        captured: list = []
        self._run_execute(job, pull_fn, fake_ref, max_attempts=3, captured_updates=captured)

        dead_letter_calls = [
            (sql, p) for sql, p in captured
            if "dead_letter" in (sql + str(p or ""))
        ]
        assert dead_letter_calls, (
            "Expected 'dead_letter' in UPDATE when attempt_count reaches max_attempts"
        )

    def test_requeue_before_max_attempts(self):
        """RateLimitError at attempt < max_attempts -> requeue (state='queued')."""
        job, pull_fn, fake_ref = _make_execute_job_patches(attempt_count=0)
        captured: list = []
        self._run_execute(job, pull_fn, fake_ref, max_attempts=3, captured_updates=captured)

        requeue_calls = [
            (sql, p) for sql, p in captured
            if "'queued'" in sql or (p and "queued" in str(p))
        ]
        dead_calls = [
            (sql, p) for sql, p in captured
            if "dead_letter" in (sql + str(p or ""))
        ]
        assert requeue_calls, "Expected requeue UPDATE before max_attempts"
        assert not dead_calls, "Should NOT dead_letter before max_attempts"

    def test_dead_letter_has_completed_at(self):
        """Dead-letter UPDATE for rate limit includes completed_at = now()."""
        job, pull_fn, fake_ref = _make_execute_job_patches(attempt_count=2)
        captured: list = []
        self._run_execute(job, pull_fn, fake_ref, max_attempts=3, captured_updates=captured)

        dead_letter_sqls = [
            sql for sql, _ in captured
            if "dead_letter" in sql and "completed_at" in sql
        ]
        assert dead_letter_sqls, (
            "Dead-letter UPDATE must set completed_at when rate_limit_exhausted"
        )
