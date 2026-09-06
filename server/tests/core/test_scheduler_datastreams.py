"""Unit tests for Story 8.2 scheduler changes: dispatch_nightly datastream iteration.

Covers:
  - dispatch_nightly uses enabled datastreams as the primary dispatch unit.
  - refetch_days window is respected.
  - Legacy fallback fires for projects with zero enabled datastreams.
  - Module enablement filter (app.project_modules) is applied.
  - datastream_id is passed to enqueue_pull.

AD-2: no module names hardcoded (module_name is data in the mock rows).
"""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from tests.support.dispatch_rows import dispatch_row

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_queue(jobs=None):
    """Return a mock queue module with enqueue_pull."""
    q = MagicMock()
    q.enqueue_pull.return_value = {"job_id": "job_x", "pull_id": "pull_x", "state": "queued"}
    if jobs is not None:
        q.enqueue_pull.side_effect = jobs
    return q


def _make_get_connection(rows_by_query=None):
    """Build a get_connection mock that returns different rows per query.

    rows_by_query: list of row-lists to return in sequence (one per cursor.execute call).
    """
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    if rows_by_query:
        cur.fetchall.side_effect = rows_by_query
    else:
        cur.fetchall.return_value = []

    get_conn = MagicMock(return_value=conn_ctx)
    return get_conn, conn, cur


# ---------------------------------------------------------------------------
# Tests for _dispatch_nightly_datastreams
# ---------------------------------------------------------------------------


class TestDispatchNightlyDatastreams:
    def test_dispatches_one_job_per_enabled_datastream(self):
        """One enabled datastream -> one enqueue_pull call with correct window."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        yesterday = date(2026, 7, 11)
        refetch_days = 3
        expected_from = (yesterday - timedelta(days=refetch_days - 1)).isoformat()
        expected_to = yesterday.isoformat()

        ds_row = dispatch_row(
            ds_id="ds_001",
            project_id="proj_a",
            module_name="google-analytics",
            refetch_days=refetch_days,
            connection_ref_id="conn_x",
            cr_status="active",
            cr_enabled=True,
        )

        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()

        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 1
        assert len(jobs) == 1
        q.enqueue_pull.assert_called_once_with(
            "conn_x",
            expected_from,
            expected_to,
            requested_by="scheduler",
            datastream_id="ds_001",
            # Story 63.1: the run the window belongs to. None here because this
            # fake DB models no execution -- which is itself the contract: a run
            # that cannot be opened never blocks the pull.
            execution_id=None,
            # AI-301: the module the ROW declares, carried explicitly. The gate
            # inside `enqueue_pull` used to re-read it on an RLS-armed connection
            # that could not see the row, and answered `access_denied` -- five days
            # of collection were refused there. Asserting it here is what keeps the
            # dispatcher from silently dropping the argument again.
            module_name="google-analytics",
        )

    def test_skips_datastream_with_no_connection(self):
        """Datastream with connection_ref_id=None is skipped."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        ds_row = dispatch_row(
            ds_id="ds_no_conn",
            project_id="proj_a",
            module_name="ga",
            refetch_days=3,
            connection_ref_id=None,
            cr_status=None,
            cr_enabled=None,
        )
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()
        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 0
        assert jobs == []
        q.enqueue_pull.assert_not_called()

    def test_skips_datastream_with_inactive_connection(self):
        """Datastream whose connection is not active is skipped."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        ds_row = dispatch_row(
            ds_id="ds_revoked",
            project_id="proj_a",
            module_name="ga",
            refetch_days=3,
            connection_ref_id="conn_revoked",
            cr_status="revoked",
            cr_enabled=True,
        )
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()
        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 0
        q.enqueue_pull.assert_not_called()

    def test_refetch_days_controls_window_size(self):
        """refetch_days=7 -> window starts 6 days before yesterday."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        yesterday = date(2026, 7, 11)
        refetch_days = 7
        expected_from = (yesterday - timedelta(days=6)).isoformat()
        expected_to = yesterday.isoformat()

        ds_row = dispatch_row(
            ds_id="ds_7d",
            project_id="proj_a",
            module_name="ga",
            refetch_days=refetch_days,
            connection_ref_id="conn_x",
            cr_status="active",
            cr_enabled=True,
        )
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]

        q = _make_mock_queue()
        _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)

        q.enqueue_pull.assert_called_once_with(
            "conn_x",
            expected_from,
            expected_to,
            requested_by="scheduler",
            datastream_id="ds_7d",
            execution_id=None,
            # AI-301: carried from the row, whatever it spells (AD-2).
            module_name="ga",
        )

    def test_returns_empty_on_db_error(self):
        """DB error -> returns ([], 0) without raising."""
        from core.scheduler import _dispatch_nightly_datastreams

        get_conn = MagicMock(side_effect=Exception("DB down"))
        q = _make_mock_queue()
        jobs, count = _dispatch_nightly_datastreams(date.today(), "scheduler", q, get_conn)
        assert jobs == []
        assert count == 0

    def test_continues_on_enqueue_error(self):
        """Enqueue failure for one datastream does not prevent others from being queued."""
        from core.scheduler import _dispatch_nightly_datastreams

        as_of = date(2026, 7, 12)
        ds_rows = [
            dispatch_row(
                ds_id="ds_fail",
                project_id="proj_a",
                module_name="ga",
                refetch_days=3,
                connection_ref_id="conn_fail",
                cr_status="active",
                cr_enabled=True,
            ),
            dispatch_row(
                ds_id="ds_ok",
                project_id="proj_a",
                module_name="ga",
                refetch_days=3,
                connection_ref_id="conn_ok",
                cr_status="active",
                cr_enabled=True,
            ),
        ]
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_rows[0].keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(r[c] for c in cols) for r in ds_rows]

        q = MagicMock()
        q.enqueue_pull.side_effect = [
            Exception("enqueue failed"),
            {"job_id": "job_ok", "pull_id": "pull_ok", "state": "queued"},
        ]

        jobs, count = _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)
        assert count == 2  # Both were attempted
        assert len(jobs) == 1  # Only the successful one is in the list


# ---------------------------------------------------------------------------
# Tests for dispatch_nightly (full function with fallback logic)
# ---------------------------------------------------------------------------


def _make_db_ctx(fetchall_side_effect=None):
    """Build a mock DB connection context + cursor."""
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    if fetchall_side_effect is not None:
        cur.fetchall.side_effect = fetchall_side_effect
    else:
        cur.fetchall.return_value = []
    cur.description = [("id",), ("provider",), ("project_id",)]
    return conn_ctx, cur


class TestDispatchNightlyFull:
    def test_uses_datastream_path_when_datastreams_exist(self):
        """_dispatch_nightly_datastreams is called by dispatch_nightly."""
        from core.scheduler import dispatch_nightly

        # get_connection (inline import from core.db) patches must target core.db.
        # queue is imported inline (`from core import queue`) so patch core.queue.enqueue_pull.
        conn_ctx, cur = _make_db_ctx(
            fetchall_side_effect=[
                [],  # fallback query: legacy connections (none)
            ]
        )

        mock_ds_jobs = [{"job_id": "job_ds", "pull_id": "pull_ds", "state": "queued"}]
        with patch(
            "core.scheduler._dispatch_nightly_datastreams", return_value=(mock_ds_jobs, 1)
        ) as mock_ds_dispatch:
            with patch("core.db.get_connection", return_value=conn_ctx):
                with patch(
                    "core.queue.enqueue_pull",
                    return_value={"job_id": "j", "pull_id": "p", "state": "queued"},
                ):
                    with patch.dict("os.environ", {"SYNC_ENABLED": "false"}):
                        result = dispatch_nightly(as_of_date=date(2026, 7, 12))

        # _dispatch_nightly_datastreams must have been called
        mock_ds_dispatch.assert_called_once()
        # Result includes the DS job
        assert any(j.get("job_id") == "job_ds" for j in result)

    def test_fallback_fires_for_projects_without_datastreams(self):
        """Legacy connections for projects with zero datastreams are dispatched."""
        from core.scheduler import dispatch_nightly

        as_of = date(2026, 7, 12)

        # get_connection is imported from core.db inside dispatch_nightly.
        conn_ctx, cur = _make_db_ctx(
            fetchall_side_effect=[
                [("conn_legacy", "ga", "proj_legacy")],  # legacy connection
            ]
        )

        mock_q = MagicMock()
        mock_q.enqueue_pull.return_value = {
            "job_id": "job_leg",
            "pull_id": "pull_leg",
            "state": "queued",
        }

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)):
            with patch("core.db.get_connection", return_value=conn_ctx):
                with patch("core.queue.enqueue_pull", side_effect=mock_q.enqueue_pull):
                    with patch.dict("os.environ", {"SYNC_ENABLED": "false"}):
                        dispatch_nightly(as_of_date=as_of, requested_by="scheduler")

        # 2 windows per legacy connection (compute_nightly_work returns 2 windows)
        assert mock_q.enqueue_pull.call_count == 2

    def test_dispatch_nightly_returns_list(self):
        """dispatch_nightly always returns a list (even on empty DB)."""
        from core.scheduler import dispatch_nightly

        conn_ctx, cur = _make_db_ctx(fetchall_side_effect=[[]])

        with patch("core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)):
            with patch("core.db.get_connection", return_value=conn_ctx):
                with patch(
                    "core.queue.enqueue_pull",
                    return_value={"job_id": "j", "pull_id": "p", "state": "queued"},
                ):
                    with patch.dict("os.environ", {"SYNC_ENABLED": "false"}):
                        result = dispatch_nightly(as_of_date=date(2026, 7, 12))

        assert isinstance(result, list)


def test_versioned_drafts_are_outside_the_legacy_scheduler_boundary():
    """Disabled v2 drafts neither dispatch nor suppress the legacy fallback."""
    import inspect

    from core.scheduler import _dispatch_nightly_datastreams, dispatch_nightly

    dispatch_source = inspect.getsource(_dispatch_nightly_datastreams)
    fallback_source = inspect.getsource(dispatch_nightly)
    assert "WHERE ds.enabled = TRUE" in dispatch_source
    assert "SELECT DISTINCT project_id FROM app.datastreams WHERE enabled = TRUE" in fallback_source
    assert "lifecycle_state = 'active'" in dispatch_source
    assert "current_plan_version_id IS NOT NULL" in dispatch_source
    assert "current_mapping_version_id IS NOT NULL" in dispatch_source


# ---------------------------------------------------------------------------
# Story 12.6: hourly recurring dispatch (today-inclusive window).
# ---------------------------------------------------------------------------


class TestDispatchHourlyDatastreams:
    def _hourly_row(self, **over):
        row = dispatch_row(
            ds_id="ds_h1",
            project_id="proj_a",
            module_name="google-analytics",
            refetch_days=3,
            date_window_days=None,
            source_kind="connector_pull",
            connection_ref_id="conn_x",
            cr_status="active",
            cr_enabled=True,
        )
        row.update(over)
        return row

    def test_hourly_window_is_today_inclusive(self):
        from core.scheduler import _dispatch_hourly_datastreams

        as_of = date(2026, 7, 12)  # today
        window = 3
        expected_from = (as_of - timedelta(days=window - 1)).isoformat()
        expected_to = as_of.isoformat()  # today INCLUSIVE (not yesterday)

        row = self._hourly_row(refetch_days=window)
        get_conn, conn, cur = _make_get_connection()
        cols = list(row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(row[c] for c in cols)]
        q = _make_mock_queue()

        jobs, count = _dispatch_hourly_datastreams(as_of, "scheduler", q, get_conn)

        assert count == 1 and len(jobs) == 1
        q.enqueue_pull.assert_called_once_with(
            "conn_x", expected_from, expected_to,
            requested_by="scheduler", datastream_id="ds_h1",
            execution_id=None,
            # AI-301: the hourly path carries the row's module too -- it is the
            # same dispatcher, so a divergence here would be the same refusal.
            module_name="google-analytics",
        )

    def test_hourly_excludes_external_bq(self):
        from core.scheduler import _dispatch_hourly_datastreams

        row = self._hourly_row(ds_id="ds_ext", source_kind="external_bq")
        get_conn, conn, cur = _make_get_connection()
        cols = list(row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(row[c] for c in cols)]
        q = _make_mock_queue()

        jobs, count = _dispatch_hourly_datastreams(date(2026, 7, 12), "scheduler", q, get_conn)

        assert count == 0
        q.enqueue_pull.assert_not_called()

    def test_hourly_skips_inactive_connection(self):
        from core.scheduler import _dispatch_hourly_datastreams

        row = self._hourly_row(cr_status="revoked")
        get_conn, conn, cur = _make_get_connection()
        cols = list(row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(row[c] for c in cols)]
        q = _make_mock_queue()

        jobs, count = _dispatch_hourly_datastreams(date(2026, 7, 12), "scheduler", q, get_conn)

        assert count == 0
        q.enqueue_pull.assert_not_called()


# ---------------------------------------------------------------------------
# Story 57.8 -- a failed pull catches up at the next hour, exactly once.
#
# These assert on the statements the sweep ISSUES, because the sweep is one SQL
# round trip and a MagicMock cursor cannot execute it. What it does when a real
# database runs it is measured in `tests/integration/test_schedule_advance_pg.py`
# -- these guard the shape, that file guards the behaviour, and neither is the
# other's substitute.
# ---------------------------------------------------------------------------


class _RecordingCursor:
    def __init__(self):
        self.statements = []
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


def _recording_connection():
    cur = _RecordingCursor()
    conn = MagicMock()
    conn.cursor.return_value = cur
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=conn)
    ctx.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=ctx), cur


class TestFailedPullCatchUp:
    def _statements(self):
        from core.scheduler import _reschedule_failed_pulls

        get_conn, cur = _recording_connection()
        _reschedule_failed_pulls(get_conn)
        return [sql for sql, _ in cur.statements]

    def test_a_failed_pull_moves_the_next_run_to_the_following_hour(self):
        """`retry_count` was declared in migration 030 and written by NOBODY.

        A single read existed (`datastream_workbench.py`) against a column no
        production path ever incremented, so the Workbench reported a catch-up
        counter that could only ever read 0. This is the code that writes it.
        """
        arming = [s for s in self._statements() if "interval '1 hour'" in s]
        assert len(arming) == 1, "the catch-up is written by exactly one statement"
        assert "next_run_at = NOW() + interval '1 hour'" in arming[0]
        assert "retry_count = ss.retry_count + 1" in arming[0]

    def test_a_queue_ceiling_counts_as_data_that_did_not_arrive(self):
        """A5. `dead_letter` is not a different kind of silence from `failed`.

        A job that exhausted its attempts moved no rows either, and treating it
        as terminal-and-therefore-fine would hide the one outcome an operator
        most needs to see.
        """
        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        assert "'failed'" in arming
        assert "'dead_letter'" in arming

    def test_a_window_somebody_stopped_arms_no_catch_up(self):
        """Story 63.6, and it is the reason the stopped state is its own name.

        This sweep writes `next_run_at = NOW() + interval '1 hour'` on the two
        states it reads. If the state a stop writes were one of them, stopping a
        run would RESTART it within the hour -- the one outcome that gesture may
        never have.

        Read from the registry rather than by naming the state, so the day the
        classification moves this reddens.
        """
        from core import pull_job_states

        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        stopped = set(pull_job_states.TERMINAL_JOB_STATES) - set(
            pull_job_states.ATTEMPTED_JOB_STATES
        )
        assert stopped, "no terminal-but-never-attempted state to check"
        for state in sorted(stopped):
            assert f"'{state}'" not in arming, state

    def test_the_sweep_reads_the_registry_and_not_its_own_list(self):
        """Two sets, generated -- and the difference between them is the safety.

        They were typed here as literals: `('done','failed','dead_letter')` for
        the latest terminal job, `('failed','dead_letter')` for the retry. Six
        copies of the job states existed and no source; this was one of them.
        """
        from core import pull_job_states

        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        for state in pull_job_states.ATTEMPTED_JOB_STATES:
            assert f"'{state}'" in arming, state

    def test_a_stopped_window_does_not_move_next_run_at(self):
        """The whole statement, read: nothing selects the stopped state at all."""
        from core import pull_job_states

        joined = " ".join(self._statements())
        stopped = set(pull_job_states.TERMINAL_JOB_STATES) - set(
            pull_job_states.ATTEMPTED_JOB_STATES
        )
        for state in sorted(stopped):
            assert f"'{state}'" not in joined, state

    def test_the_catch_up_is_one_attempt_and_not_a_loop(self):
        """A4. "The next hour" is an hour, not a retry policy.

        Without the ceiling, the catch-up's own failure would arm another one and
        a broken source would be re-pulled every hour until it recovered --
        spending provider quota all night to answer the same question.
        """
        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        assert "ss.retry_count = 0" in arming, (
            "nothing caps the catch-up: a failing source would re-fire every hour"
        )

    def test_only_the_cadences_that_run_once_a_period_catch_up(self):
        """A3. An hourly Datastream's next hour is already its next run.

        Arming a catch-up there would double the hourly dispatch, and a manual
        Datastream has no clock to move at all.
        """
        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        assert "d.schedule_mode IN ('nightly', 'weekly')" in arming

    def test_an_old_failure_does_not_re_arm_a_schedule_that_already_moved(self):
        """The sweep runs every tick; the failure it reads does not change.

        Without this guard the same terminal job would be re-read hour after
        hour, and the ceiling would be the only thing standing between a stale
        row and a permanent hourly re-dispatch.
        """
        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        assert "latest.completed_at > ss.updated_at" in arming

    def test_the_advance_is_not_allowed_near_the_counter(self):
        """A4's bound is structural, and it stops being so the moment this breaks.

        `_advance_next_run` used to clear `retry_count` whenever the advanced
        value sat on the arrival hour. That condition is trivially true for every
        row whose `arrival_hour_local` is NULL — the majority class — so the
        catch-up's own dispatch re-armed the allowance and the next sweep armed
        another, hourly. Only a successful pull closes it now.
        """
        import inspect

        from core import scheduler

        assert "retry_count =" not in inspect.getsource(scheduler._advance_next_run), (
            "the advance writes retry_count again -- the catch-up re-arms itself"
        )

    def test_a_success_clears_the_counter(self):
        clearing = [
            s for s in self._statements()
            if "retry_count = 0" in s and "interval '1 hour'" not in s
        ]
        assert len(clearing) == 1
        assert "latest.state = 'done'" in clearing[0]
        assert "ss.retry_count > 0" in clearing[0]

    def test_the_sweep_reads_only_the_latest_outcome_per_datastream(self):
        """A pull job history is append-only: every datastream has old failures.

        Reading anything but the LAST terminal job would arm a catch-up for a
        failure that has already been superseded by a success.
        """
        arming = next(s for s in self._statements() if "interval '1 hour'" in s)
        assert "DISTINCT ON (j.datastream_id)" in arming
        assert "ORDER BY j.datastream_id, j.completed_at DESC" in arming

    def test_a_database_error_does_not_take_down_the_tick(self):
        from core.scheduler import _reschedule_failed_pulls

        assert _reschedule_failed_pulls(MagicMock(side_effect=Exception("DB down"))) == 0

    def test_the_sweep_runs_on_every_tick_not_once_a_night(self):
        """A2. The catch-up is an HOUR away: a daily step could never serve it."""
        import inspect

        from core import scheduler

        assert "_reschedule_failed_pulls" in inspect.getsource(scheduler.run_hourly_steps)


# ---------------------------------------------------------------------------
# Story 57.8, A2 -- the frequent tick evaluates the nightly rows that are DUE.
# ---------------------------------------------------------------------------


class TestTheFrequentTickSeesNightlyRows:
    def test_the_hourly_tick_dispatches_a_due_nightly_row(self):
        """An arrival hour of 06:00 was unreachable before this.

        `dispatch-nightly` fires at 02:00 and nothing else looked at a nightly
        row, so a run due at 06:00 was first SEEN at 02:00 the following day --
        and a catch-up an hour after a failure could not exist at all. The file
        already said what it wanted (AD-36, "dumb and FREQUENT"); only the cron
        strangled it.
        """
        from core.scheduler import dispatch_hourly

        nightly_jobs = [{"job_id": "job_nightly", "pull_id": "pull_n", "state": "queued"}]
        with patch(
            "core.scheduler._dispatch_hourly_datastreams", return_value=([], 0)
        ), patch(
            "core.scheduler._dispatch_nightly_datastreams", return_value=(nightly_jobs, 1)
        ) as nightly:
            result = dispatch_hourly(as_of_date=date(2026, 7, 12))

        nightly.assert_called_once()
        assert any(job.get("job_id") == "job_nightly" for job in result)

    def test_the_tick_hands_the_nightly_path_the_callers_original_intent(self):
        """`None` is not `date.today()` here, and the distinction is load-bearing.

        A pinned date means a replay; an unpinned one means each project measures
        its own yesterday (AI-117). Resolving it before the call erases the
        difference and asks a provider for a day one project has not lived.
        """
        from core.scheduler import dispatch_hourly

        with patch(
            "core.scheduler._dispatch_hourly_datastreams", return_value=([], 0)
        ), patch(
            "core.scheduler._dispatch_nightly_datastreams", return_value=([], 0)
        ) as nightly:
            dispatch_hourly()

        assert nightly.call_args.args[0] is None

    def test_a_dispatched_row_is_moved_out_of_the_due_set_before_the_next_tick(self):
        """A2's other half: the double dispatch has to be IMPOSSIBLE, not unlikely.

        Two clocks now read the same rows -- the frequent tick and the 02:00 net.
        What keeps a window from being enqueued twice is that the advance runs
        against the same connection immediately after each enqueue, so the guard
        `next_run_at <= NOW()` no longer selects the row. Both halves are
        asserted here; the round trip itself is executed in
        `tests/integration/test_schedule_advance_pg.py`.
        """
        import inspect

        from core.scheduler import _dispatch_nightly_datastreams

        source = inspect.getsource(_dispatch_nightly_datastreams)
        assert "ss.next_run_at IS NULL OR ss.next_run_at <= NOW()" in source

        ds_row = dispatch_row(
            ds_id="ds_due",
            project_id="proj_a",
            module_name="ga",
            refetch_days=3,
            connection_ref_id="conn_x",
            cr_status="active",
            cr_enabled=True,
        )
        get_conn, conn, cur = _make_get_connection()
        cols = list(ds_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(ds_row[c] for c in cols)]
        q = _make_mock_queue()

        with patch("core.scheduler._advance_next_run") as advance:
            _dispatch_nightly_datastreams(date(2026, 7, 12), "scheduler", q, get_conn)

        assert q.enqueue_pull.call_count == 1
        assert advance.call_count == 1, (
            "the row was enqueued without being advanced -- the next tick would "
            "still see it as due and enqueue the same window again"
        )
        assert advance.call_args.args[1:] == ("ds_due", "proj_a", "nightly")


# ---------------------------------------------------------------------------
# AI-217 -- a `weekly` Datastream has to be SELECTED by a dispatcher.
# ---------------------------------------------------------------------------


def _weekly_row(**overrides):
    row = dispatch_row(
        ds_id="ds_weekly",
        project_id="proj_a",
        module_name="ga",
        refetch_days=3,
        schedule_mode="weekly",
        connection_ref_id="conn_x",
        cr_status="active",
        cr_enabled=True,
    )
    row.update(overrides)
    return row


def _dispatch_rows(rows, as_of=date(2026, 7, 12)):
    """Run the once-a-period dispatcher over *rows* and return (queue, advance)."""
    from core.scheduler import _dispatch_nightly_datastreams

    get_conn, _conn, cur = _make_get_connection()
    cols = list(rows[0].keys())
    cur.description = [(c,) for c in cols]
    cur.fetchall.return_value = [tuple(r[c] for c in cols) for r in rows]
    q = _make_mock_queue()
    with patch("core.scheduler._advance_next_run") as advance:
        _dispatch_nightly_datastreams(as_of, "scheduler", q, get_conn)
    return q, advance


class TestTheWeeklyCadenceIsDispatched:
    """AI-217. Migration 204 made `weekly` legal, and nothing ever selected it.

    `schedule_mcp.CADENCES` accepted it, the CHECK constraint allowed it, the
    Workbench offered it and story 57.8 taught the advance and the catch-up sweep
    to speak it -- feeding a dispatcher that did not exist. An operator could
    choose a weekly cadence, save it, see it displayed, and the collection would
    never fire: `_dispatch_nightly_datastreams` read `schedule_mode = 'nightly'`
    and `_dispatch_hourly_datastreams` read `= 'hourly'`.
    """

    def test_the_due_selection_reads_weekly_as_well_as_nightly(self):
        """The predicate was already right; only the cadence filter was narrow.

        `next_run_at IS NULL OR next_run_at <= NOW()` selects a due weekly row
        exactly as it selects a due nightly one -- the two cadences differ by how
        far the advance moves afterwards, not by how eligibility is decided.
        """
        import inspect

        from core.scheduler import _dispatch_nightly_datastreams

        source = inspect.getsource(_dispatch_nightly_datastreams)
        assert "ds.schedule_mode IN ('nightly', 'weekly')" in source
        assert "ss.next_run_at IS NULL OR ss.next_run_at <= NOW()" in source

    def test_a_due_weekly_row_is_enqueued(self):
        q, _advance = _dispatch_rows([_weekly_row()])

        assert q.enqueue_pull.call_count == 1, (
            "a weekly Datastream whose next run is due was not enqueued -- the "
            "cadence is offered, saved and displayed, and never runs"
        )
        assert q.enqueue_pull.call_args.kwargs["datastream_id"] == "ds_weekly"

    def test_a_dispatched_weekly_row_advances_by_a_week_not_by_a_day(self):
        """The advance is told the row's OWN cadence, not the dispatcher's name.

        `_ADVANCE_STEPS` maps `weekly` to seven days (story 57.8), and that
        mapping is reachable only if the cadence travels from the selected row.
        Passing the literal `"nightly"` would move a weekly row one day and it
        would be re-dispatched every night -- seven times the provider quota its
        cadence asks for, which is the defect 57.8 fixed one layer above.
        """
        _q, advance = _dispatch_rows([_weekly_row()])

        assert advance.call_count == 1
        assert advance.call_args.args[1:] == ("ds_weekly", "proj_a", "weekly")

    def test_a_nightly_row_still_advances_as_nightly(self):
        _q, advance = _dispatch_rows([_weekly_row(ds_id="ds_n", schedule_mode="nightly")])

        assert advance.call_args.args[1:] == ("ds_n", "proj_a", "nightly")

    def test_a_row_that_names_no_cadence_is_treated_as_nightly(self):
        """Defensive only: every real row carries a NOT NULL `schedule_mode`.

        Test mocks omit columns, and a dispatcher that raised on an absent key
        would fail the whole tick rather than one row.
        """
        row = _weekly_row(ds_id="ds_unknown")
        row.pop("schedule_mode")
        _q, advance = _dispatch_rows([row])

        assert advance.call_args.args[1:] == ("ds_unknown", "proj_a", "nightly")

    def test_a_weekly_run_never_fetches_less_than_the_week_it_covers(self):
        """Six days out of seven would be dropped, silently and for ever.

        The retrieval window is chosen independently of the cadence, and its
        legacy fallback is three days. Three days fetched once a week means the
        four oldest days of every week are never asked for -- and nothing reports
        a gap, because each run succeeds. The floor is the interval between two
        runs; anything the operator sets ABOVE it is honoured untouched.
        """
        q, _advance = _dispatch_rows([_weekly_row(refetch_days=3)])

        date_from, date_to = q.enqueue_pull.call_args.args[1:3]
        assert (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days == 6, (
            f"a weekly run fetched {date_from}..{date_to} -- the days between two "
            "weekly runs are dropped and no run ever fails"
        )
        assert date_to == date(2026, 7, 11).isoformat()

    def test_a_deeper_window_chosen_by_a_person_is_not_narrowed_to_the_floor(self):
        q, _advance = _dispatch_rows([_weekly_row(date_window_days=30, refetch_days=3)])

        date_from, date_to = q.enqueue_pull.call_args.args[1:3]
        assert (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days == 29

    def test_the_floor_belongs_to_the_weekly_cadence_alone(self):
        """A nightly row keeps the window it was given, to the day.

        The floor is the gap between two runs of THIS cadence. Applying it to a
        nightly row would multiply every night's fetch by seven.
        """
        q, _advance = _dispatch_rows([_weekly_row(schedule_mode="nightly", refetch_days=3)])

        date_from, date_to = q.enqueue_pull.call_args.args[1:3]
        assert (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days == 2

    def test_the_hourly_dispatcher_does_not_claim_the_weekly_cadence(self):
        """It could not host it: it has no due guard and no advance at all.

        `_dispatch_hourly_datastreams` selects every enabled hourly row on every
        tick -- which is what an hourly cadence means. A weekly row placed there
        would be pulled every hour, and its `next_run_at` would never move.
        """
        import inspect

        from core.scheduler import _dispatch_hourly_datastreams

        source = inspect.getsource(_dispatch_hourly_datastreams)
        assert "ds.schedule_mode = 'hourly'" in source
        assert "weekly" not in source
        assert "next_run_at" not in source


# ---------------------------------------------------------------------------
# Story 12.10: managed-feed sync dispatch step (env-guarded, adapter=None Phase-B).
# ---------------------------------------------------------------------------


class TestManagedFeedSyncStep:
    def test_skipped_when_flag_off(self, monkeypatch):
        from core import scheduler

        monkeypatch.delenv("MANAGED_FEED_SYNC_ENABLED", raising=False)
        called = {"db": False}

        def _fail_get_connection():
            called["db"] = True
            raise AssertionError("must not read DB when flag is off")

        monkeypatch.setattr("core.db.get_connection", _fail_get_connection, raising=False)
        scheduler._run_managed_feed_syncs()  # returns cleanly, no DB read
        assert called["db"] is False

    def test_dispatches_per_enabled_schedule_with_the_live_adapter(self, monkeypatch):
        """One dispatch per enabled schedule, carrying the REAL Sheets reader.

        AI-102. This assertion used to read `sheets_adapter is None` under a
        comment saying `PHASE_B_LIVE_BLOCKED`, and the test name promised the
        same. That was true while the channel could not read a cell -- and story
        47.5 (`2a747544`) wired six lines that made it read one. The expectation
        outlived the block it described, and went red on main rather than
        catching anything.

        The name changes with the assertion on purpose: a test called
        `..._with_none_adapter` that asserts an adapter IS present would be the
        next reader's trap. What it guards now is what the dispatch must carry --
        one call per enabled schedule, for the right Datastream, with a reader
        rather than a placeholder.
        """
        from core import scheduler

        monkeypatch.setenv("MANAGED_FEED_SYNC_ENABLED", "true")
        sched_row = {
            "datastream_id": "ds_sheet",
            "project_id": "proj_a",
            "connection_id": "conn_g",
            "spreadsheet_id": "sheet_1",
            "sheet_range": "A:F",
            "sheet_name": "Budgets",
            "column_mapping": {"date_column": "date"},
            "cadence_mode": "daily",
            "cadence_policy": {"mode": "daily"},
            "quota_profile": {},
            "last_watermark": None,
        }
        get_conn, conn, cur = _make_get_connection()
        cols = list(sched_row.keys())
        cur.description = [(c,) for c in cols]
        cur.fetchall.return_value = [tuple(sched_row[c] for c in cols)]
        monkeypatch.setattr("core.db.get_connection", get_conn, raising=False)

        calls = []

        def _fake_dispatch(**kwargs):
            calls.append(kwargs)
            return {"outcome": "validated_pending_publication"}

        monkeypatch.setattr(
            "core.google_sheets_sync.dispatch_managed_feed_sync", _fake_dispatch, raising=False
        )

        scheduler._run_managed_feed_syncs()

        assert len(calls) == 1
        assert calls[0]["datastream_id"] == "ds_sheet"
        assert callable(calls[0]["sheets_adapter"]), (
            "the managed-feed sync was dispatched without a reader -- the channel "
            "cannot fetch a cell, which is the state 47.5 closed"
        )
        assert calls[0]["actor"] == "scheduler"
