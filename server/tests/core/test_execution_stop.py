"""Stopping a run refuses what has not started -- story 63.6, epic 63.

WHAT THIS FILE PINS, AND WHY EACH LINE OF IT EXISTS.

A run that cannot be stopped is a run somebody watches burn provider quota. But
the product cannot interrupt a provider call already in flight: `_execute_job`
is synchronous and consults nothing between two pages, and the only mechanism
that takes a `running` job back is the stale sweep at 5400 s. So the gesture has
an honest scope -- the windows still in `queued` -- and the tests below refuse
every way of pretending otherwise.

The four refusals are named, and each is a different sentence: a run that already
ended, a run that is not a collection at all, a run in another project, and a
caller without the role. One code for all four would make the console say
"something went wrong" to four different situations, three of which a person can
act on.

NO POSTGRES HERE. The refusals, the ordering and the arithmetic are decidable
against a recording connection; whether the CHECK constraint accepts the state
and whether the same window can be re-queued afterwards are questions only a
real cluster answers, and they live in
`tests/integration/test_execution_stop_constraints.py`.
"""

from __future__ import annotations

import inspect

import pytest
from core import execution_progress, pull_job_states
from core.execution_progress import (
    STOP_FORBIDDEN,
    STOP_NOT_A_COLLECTION_RUN,
    STOP_NOT_FOUND,
    STOP_NOT_RUNNING,
    StopRefused,
    stop_collection_run,
)

# ---------------------------------------------------------------------------
# A connection that answers a scripted set of reads and records every write.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, owner):
        self._owner = owner
        self.description = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(str(sql).split())
        self._owner.statements.append((text, params))
        columns, rows = self._owner.answer(text)
        self.description = [(name,) for name in columns] if columns else None
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    """Scripted reads, recorded writes. The run's own row is mutable."""

    def __init__(self, *, state="loading", kind="recurring_collection",
                 days_done=0, rows_written=None, queued=(), running=None,
                 known=True):
        self.statements: list[tuple[str, object]] = []
        self.commits = 0
        self.state = state
        self.kind = kind
        self.days_done = days_done
        self.rows_written = rows_written
        self.queued = list(queued)
        self.running = running
        self.known = known
        self.cancelled: list[dict] = []
        #: `state_changed_at`, which the state machine moves when it closes the
        #: run -- so a second call reads the instant of the stop, not the
        #: instant the run last changed before it.
        self.stopped_at = "2026-08-06T10:00:00+00:00"

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # -- the script -------------------------------------------------------
    def answer(self, text: str):
        if "SELECT state, state_changed_at" in text:
            return ["state", "stopped_at"], [(self.state, self.stopped_at)]
        if "FROM app.datastream_executions" in text and "SELECT" in text:
            if not self.known:
                return ["state"], []
            columns = [
                "state", "plan_kind", "days_done", "rows_written",
                "stopped_at", "datastream_id",
            ]
            return columns, [(
                self.state, self.kind, self.days_done, self.rows_written,
                self.stopped_at, "ds_sample",
            )]
        if "UPDATE app.pull_jobs" in text:
            refused = [
                {"id": f"job_{index}", "date_from": window[0], "date_to": window[1]}
                for index, window in enumerate(self.queued)
            ]
            self.cancelled = refused
            self.queued = []
            return ["id", "date_from", "date_to"], [
                (row["id"], row["date_from"], row["date_to"]) for row in refused
            ]
        if "UPDATE app.datastream_executions" in text:
            return None, []
        if "FROM app.pull_jobs" in text and "count(" in text.lower():
            return ["windows_refused"], [(len(self.cancelled),)]
        if "FROM app.pull_jobs" in text:
            if self.running is None:
                return ["date_from", "date_to"], []
            return ["date_from", "date_to"], [self.running]
        return None, []


def _stop(conn, **kwargs):
    return stop_collection_run(
        conn,
        execution_id="dse_sample",
        project_id="proj_EXAMPLE",
        actor="owner@example.com",
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _no_real_state_machine(monkeypatch):
    """`advance_state` is the publication module's; here it only has to be CALLED.

    Its own machine is proven by `test_datastream_publication.py`; what this file
    proves is that the stop goes THROUGH it rather than writing a state itself.
    """
    calls: list[dict] = []

    def _advance(execution_id, expected, new_state, actor, conn, **kwargs):
        calls.append({
            "execution_id": execution_id,
            "expected": expected,
            "new_state": new_state,
            "actor": actor,
            **kwargs,
        })
        # Recorded on the same tape as the SQL, so the ORDER of the two writes
        # is observable -- which is the whole safety of this gesture.
        conn.statements.append(("<advance_state>", new_state))
        conn.state = new_state
        conn.stopped_at = "2026-08-06T10:04:00+00:00"
        return {"id": execution_id, "state": new_state}

    monkeypatch.setattr(
        "core.datastream_publication.advance_state", _advance, raising=True
    )
    return calls


# ---------------------------------------------------------------------------
# The four refusals, each with its own code.
# ---------------------------------------------------------------------------


def test_the_four_refusals_are_four_distinct_codes() -> None:
    """A console cannot offer three different repairs behind one message."""
    assert len({STOP_NOT_FOUND, STOP_NOT_RUNNING, STOP_NOT_A_COLLECTION_RUN,
                STOP_FORBIDDEN}) == 4
    assert set(execution_progress.STOP_REFUSALS) == {
        STOP_NOT_FOUND, STOP_NOT_RUNNING, STOP_NOT_A_COLLECTION_RUN, STOP_FORBIDDEN
    }


def test_a_run_of_another_project_is_not_found_and_never_confirmed() -> None:
    """The envelope of an ABSENT run -- a refusal that confirms it exists leaks it."""
    conn = _Conn(known=False)
    with pytest.raises(StopRefused) as excinfo:
        _stop(conn)
    assert excinfo.value.code == STOP_NOT_FOUND
    assert not [s for s, _ in conn.statements if s.startswith("UPDATE")]


def test_a_run_that_already_ended_is_refused_by_name() -> None:
    conn = _Conn(state="collected")
    with pytest.raises(StopRefused) as excinfo:
        _stop(conn)
    assert excinfo.value.code == STOP_NOT_RUNNING
    assert not [s for s, _ in conn.statements if s.startswith("UPDATE")]


def test_a_publication_candidate_is_not_a_collection_run() -> None:
    """Story 63.6's scope clause, and it is not pedantry.

    `managed_file_dispatch` reads `{"failed", "cancelled"}` off an execution and
    marks the dispatch FAILED. Stopping a publication candidate through this door
    would report a dispatch failure nobody caused.
    """
    conn = _Conn(kind="publication")
    with pytest.raises(StopRefused) as excinfo:
        _stop(conn)
    assert excinfo.value.code == STOP_NOT_A_COLLECTION_RUN
    assert not [s for s, _ in conn.statements if s.startswith("UPDATE")]


def test_the_plan_kind_it_accepts_is_the_one_the_collection_paths_write() -> None:
    """Named from the constant, so a rename cannot leave this test agreeing."""
    conn = _Conn(kind=execution_progress.COLLECTION_PLAN_KIND)
    assert _stop(conn)["state"] == "cancelled"


# ---------------------------------------------------------------------------
# What it does, and the order it does it in.
# ---------------------------------------------------------------------------


def test_it_refuses_the_queued_windows_and_leaves_the_running_one_alone(
    _no_real_state_machine,
) -> None:
    conn = _Conn(
        queued=[("2026-07-01", "2026-07-31"), ("2026-08-01", "2026-08-31")],
        running=("2026-06-01", "2026-06-30"),
    )
    answer = _stop(conn)

    assert answer["windows_refused"] == 2
    assert answer["window_in_flight"] == {
        "date_from": "2026-06-01", "date_to": "2026-06-30"
    }
    # The write names ONE state to refuse from, and it is not `running`.
    write = next(s for s, _ in conn.statements if "UPDATE app.pull_jobs" in s)
    assert f"state = '{pull_job_states.QUEUED}'" in write.split("WHERE", 1)[1]
    assert f"'{pull_job_states.RUNNING}'" not in write.split("WHERE", 1)[1]


def test_the_run_is_closed_before_its_windows_are_refused(
    _no_real_state_machine,
) -> None:
    """The order is the whole safety of the gesture.

    A window landing between the two writes calls `record_and_close`, which
    re-derives the run's progress. With the run already terminal, that write is
    refused and the closure is a no-op; the other way round, the same landing
    would find a `loading` run with cancelled windows and could reopen the
    question of what this run is.
    """
    conn = _Conn(queued=[("2026-07-01", "2026-07-31")])
    _stop(conn)
    order = [s for s, _ in conn.statements]
    closed = order.index("<advance_state>")
    refused = next(i for i, s in enumerate(order) if "UPDATE app.pull_jobs" in s)
    assert closed < refused


def test_the_run_is_closed_through_the_state_machine_and_not_by_hand(
    _no_real_state_machine,
) -> None:
    calls = _no_real_state_machine
    conn = _Conn()
    _stop(conn)
    assert len(calls) == 1
    assert calls[0]["new_state"] == "cancelled"
    assert calls[0]["expected"] == "loading"
    assert calls[0]["actor"] == "owner@example.com"
    assert calls[0]["project_id"] == "proj_EXAMPLE"
    # A stopped run says WHY it ended, or it reads as an unexplained cancellation.
    assert calls[0]["error_code"] == execution_progress.ERROR_RUN_STOPPED


def test_nothing_arms_a_catch_up(_no_real_state_machine) -> None:
    """`_reschedule_failed_pulls` would restart the run within the hour.

    Held against the REGISTRY rather than against a name: the day the stop state
    is reclassified as a failure, this reddens.
    """
    conn = _Conn(queued=[("2026-07-01", "2026-07-31")])
    _stop(conn)
    written = next(s for s, _ in conn.statements if "UPDATE app.pull_jobs" in s)
    for state in sorted(pull_job_states.ATTEMPTED_JOB_STATES):
        assert f"state = '{state}'" not in written.split("WHERE", 1)[0]
    assert "next_run_at" not in " ".join(s for s, _ in conn.statements)


def test_it_never_undoes_a_window_that_already_landed(_no_real_state_machine) -> None:
    """The raw zone is append-only: there is nothing to undo, and none is tried."""
    conn = _Conn(queued=[("2026-07-01", "2026-07-31")])
    _stop(conn)
    joined = " ".join(s for s, _ in conn.statements)
    assert "DELETE" not in joined
    assert f"state = '{pull_job_states.DONE}'" not in joined


# ---------------------------------------------------------------------------
# What it reports -- and the `0` it never reports.
# ---------------------------------------------------------------------------


def test_a_run_that_measured_nothing_reports_null_and_never_zero(
    _no_real_state_machine,
) -> None:
    """`open_collection_run` writes `days_done = 0` before any window lands.

    Reporting that 0 as "0 days were collected and kept" would be a measurement
    the run never made -- the same rule 63.1 applies to `rows_written`.
    """
    conn = _Conn(days_done=0, rows_written=None,
                 queued=[("2026-07-01", "2026-07-31")])
    answer = _stop(conn)
    assert answer["days_kept"] is None
    assert answer["rows_kept"] is None


def test_what_was_kept_is_reported_exactly_as_the_run_measured_it(
    _no_real_state_machine,
) -> None:
    conn = _Conn(days_done=31, rows_written=18_420)
    answer = _stop(conn)
    assert answer["days_kept"] == 31
    assert answer["rows_kept"] == 18_420
    # A window that landed zero rows measured zero: that IS a measurement.
    zero = _Conn(days_done=31, rows_written=0)
    assert _stop(zero)["rows_kept"] == 0


def test_the_answer_carries_every_field_the_console_and_the_mcp_read(
    _no_real_state_machine,
) -> None:
    conn = _Conn(running=("2026-06-01", "2026-06-30"))
    answer = _stop(conn)
    assert set(answer) == set(execution_progress.STOP_FIELDS)
    assert answer["execution_id"] == "dse_sample"
    assert answer["state"] == "cancelled"
    assert answer["stopped_at"] is not None


def test_no_window_in_flight_is_null_and_never_an_empty_object(
    _no_real_state_machine,
) -> None:
    conn = _Conn(running=None)
    assert _stop(conn)["window_in_flight"] is None


# ---------------------------------------------------------------------------
# Asked twice.
# ---------------------------------------------------------------------------


def test_a_second_call_answers_the_same_thing_and_writes_nothing(
    _no_real_state_machine,
) -> None:
    """A double click, a retried MCP call, a page reloaded mid-request.

    The second call must not re-run the state machine (`cancelled` is terminal
    and `advance_state` would raise) and must not re-count what it refused: the
    count comes from the jobs that ARE stopped, not from the rows one UPDATE
    happened to touch.
    """
    conn = _Conn(queued=[("2026-07-01", "2026-07-31")], days_done=31,
                 rows_written=500)
    first = _stop(conn)
    calls_after_first = len(_no_real_state_machine)
    writes_after_first = len([s for s, _ in conn.statements if s.startswith("UPDATE")])

    second = _stop(conn)

    assert second == first
    assert len(_no_real_state_machine) == calls_after_first
    assert len([s for s, _ in conn.statements if s.startswith("UPDATE")]) == (
        writes_after_first
    )


# ---------------------------------------------------------------------------
# The transaction, and who owns it.
# ---------------------------------------------------------------------------


def test_it_does_not_commit_and_says_so(_no_real_state_machine) -> None:
    """The caller owns the transaction, as every neighbour in this module does.

    Committing here would leave a stopped run and its unrefused windows in two
    different transactions the moment the caller failed after the call.
    """
    conn = _Conn(queued=[("2026-07-01", "2026-07-31")])
    _stop(conn)
    assert conn.commits == 0
    assert "conn.commit()" not in inspect.getsource(stop_collection_run)
