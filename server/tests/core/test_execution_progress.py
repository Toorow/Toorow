"""A run writes where it is ONCE PER WINDOW -- story 63.1.

The plan asked for "un run simule sur 30 jours ecrit 30 fois et pas 18 420". The
measurement in the story showed that number is not producible: a contiguous
30-day refetch is ONE window and therefore ONE provider call, and the arbitrage
settled the unit as the WINDOW. So what is pinned here is the number the code can
actually produce -- one write per window that `refetch.compute_refetch_windows`
really returns -- and never one per landed row or one per page.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone

import pytest
from core import pull_job_states, refetch
from core.execution_progress import (
    COLLECTION_PLAN_KIND,
    PROGRESS_STEPS,
    STEP_COLLECT,
    CollectionProgress,
    declared_days_total,
    derive_progress,
    record_window_progress,
    window_span_days,
)

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

# ---------------------------------------------------------------------------
# A fake connection that counts statements. Nothing here needs a database, and
# the point of the test is precisely HOW MANY writes happen.
# ---------------------------------------------------------------------------

# THE TWO STATEMENTS `record_window_progress` ISSUES ON THE PATHS TESTED HERE,
# named. Before AI-317 the chain ended in an `else` that answered no rows AND
# `description = None` -- psycopg's signal for "this statement returned no
# result set". The two step-span writes (`close_open_step_spans`,
# `open_step_span`) are deliberately NOT here: they fire only on a REAL step
# transition, which no path in this file produces, so a test that ever drives
# one must be told rather than silently answered.
_PROGRESS = StatementInventory(
    "_Cursor (record_window_progress)",
    run_jobs="from app.pull_jobs",
    progress_update="update app.datastream_executions as e",
)


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
        normalized = " ".join(sql.split())
        self._owner.statements.append((normalized, params))
        statement = _PROGRESS.match(normalized)
        # `description` is DERIVED from the projection, never restated: the
        # RETURNING of the progress write gained `prior.step AS previous_step`
        # in story 58.10 and the hand-written column tuple here never learned
        # it, so `record_window_progress` read its own payload one name short.
        self.description = describe(normalized)
        match statement:
            case "run_jobs":
                self._rows = [
                    (j["id"], j["date_from"], j["date_to"], j["state"], j.get("row_count"))
                    for j in self._owner.jobs
                ]
            case "progress_update":
                self._owner.updates.append(params)
                # Story 63.6: the write carries `AND NOT (state = ANY(%s))`, so
                # a run that is over matches no row. `refuses_update` is how
                # this double says "the predicate excluded it" -- the same empty
                # result the project-scope mismatch already produced.
                if self._owner.refuses_update:
                    self._rows = []
                    return
                # RETURNING prior.step AS previous_step, e.step, e.day_in_progress,
                # e.days_done, e.days_total, e.rows_written, e.started_at,
                # e.progress_updated_at. `previous_step` is the row as it was
                # BEFORE this statement; on a run this double has never written,
                # that column is NULL -- no path has yet declared where the run
                # was -- which is why no step span is opened here.
                self._rows = [
                    (
                        self._owner.step,
                        params[0],
                        params[1],
                        params[2],
                        None,          # days_total: never assigned by this write
                        params[3],
                        None,          # started_at: declared by open_collection_run
                        None,          # progress_updated_at: NOW(), no clock here
                    )
                ]
                self._owner.step = params[0]
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _PROGRESS.unknown(normalized)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _Conn:
    def __init__(self, jobs):
        self.jobs = jobs
        self.statements: list[tuple] = []
        self.updates: list[tuple] = []
        self.refuses_update = False
        #: `app.datastream_executions.step` as this double holds it -- NULL
        #: until a write declares one, exactly like a freshly minted run.
        self.step = None

    def cursor(self):
        return _Cursor(self)


def test_the_fake_refuses_a_statement_it_was_never_taught():
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The old `else` answered no rows and `description = None`, which is what
    psycopg reserves for a statement with no result set. `read_run_jobs` reads
    `cur.description` unconditionally, so a moved SELECT would have died inside
    the product with a `TypeError` naming `execution_progress.py`.
    """
    cursor = _Cursor(_Conn([]))
    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT state, project_id FROM app.datastream_executions WHERE id = %s"
        )
    message = str(raised.value)
    assert "select state, project_id from app.datastream_executions" in message
    assert "progress_update" in message


# ---------------------------------------------------------------------------
# The unit is the window, and a day count comes from bounds.
# ---------------------------------------------------------------------------


def test_a_window_is_measured_by_its_bounds_never_by_a_loop():
    assert window_span_days("2026-07-01", "2026-07-31") == 31
    assert window_span_days("2026-07-01", "2026-07-01") == 1
    assert window_span_days(date(2026, 7, 1), date(2026, 7, 3)) == 3


def test_unreadable_bounds_measure_nothing_rather_than_zero():
    for bad in (("", "2026-07-01"), ("2026-07-05", "2026-07-01"), (None, None), (7, 8)):
        assert window_span_days(*bad) is None, bad


def test_a_run_that_declared_no_window_has_no_total_and_not_a_zero():
    """ui-acceptance-contract.md: a 0 shown for an absence is the costliest defect."""
    assert declared_days_total([]) is None
    assert declared_days_total(None) is None
    assert declared_days_total([{"date_from": "x", "date_to": "y"}]) is None
    assert declared_days_total([{"date_from": "2026-07-01", "date_to": "2026-07-02"}]) == 2


# ---------------------------------------------------------------------------
# One write per window that the windowing code REALLY produces.
# ---------------------------------------------------------------------------


def _windows_for_two_years():
    """The 24 windows a two-year catch-up really produces (31-day tiling)."""
    return refetch.compute_refetch_windows(
        date(2026, 7, 1), {"monthly_days": 730}, cadence="monthly"
    )


def test_a_two_year_catch_up_is_twenty_four_windows_not_seven_hundred_and_thirty():
    windows = _windows_for_two_years()
    assert len(windows) == 24
    assert declared_days_total(windows) == 730


def test_one_progress_write_per_finished_window_never_one_per_row():
    """24 windows landing 18_420 rows each write 24 times, not 24 * 18_420."""
    windows = _windows_for_two_years()
    jobs = [
        {
            "id": f"job_{i}",
            "date_from": w["date_from"],
            "date_to": w["date_to"],
            "state": "queued",
            "row_count": None,
        }
        for i, w in enumerate(windows)
    ]
    conn = _Conn(jobs)

    for job in jobs:
        # The worker closes the window, then writes ONCE.
        job["state"] = "done"
        job["row_count"] = 18_420
        record_window_progress(conn, execution_id="dse_x", project_id="proj_EXAMPLE")

    assert len(conn.updates) == len(windows) == 24
    update_statements = [
        s for s, _ in conn.statements if s.startswith("UPDATE app.datastream_executions")
    ]
    assert len(update_statements) == 24


def test_the_counters_are_derived_from_the_bounds_of_the_finished_windows():
    windows = _windows_for_two_years()
    jobs = [
        {
            "id": f"job_{i}",
            "date_from": w["date_from"],
            "date_to": w["date_to"],
            "state": "done" if i < 3 else "queued",
            "row_count": 100 if i < 3 else None,
        }
        for i, w in enumerate(windows)
    ]
    progress = derive_progress(jobs)

    assert progress.days_done == sum(
        window_span_days(w["date_from"], w["date_to"]) for w in windows[:3]
    )
    assert progress.rows_written == 300
    assert progress.windows_total == 24
    assert progress.windows_finished == 3
    # The day in progress is the first day of the window now being collected.
    assert progress.day_in_progress == date.fromisoformat(windows[3]["date_from"])
    assert progress.all_terminal is False


def test_a_run_that_has_finished_nothing_has_no_row_count_and_not_a_zero():
    progress = derive_progress(
        [{"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-31",
          "state": "running", "row_count": None}]
    )
    assert progress.days_done == 0
    assert progress.rows_written is None
    assert progress.day_in_progress == date(2026, 7, 1)


# ---------------------------------------------------------------------------
# Replay: the UPDATE is monotone, so nothing goes backwards and nothing doubles.
# ---------------------------------------------------------------------------


def test_the_update_is_monotone_so_a_replayed_window_cannot_rewind_a_counter():
    jobs = [
        {"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-31",
         "state": "done", "row_count": 500},
    ]
    conn = _Conn(jobs)
    record_window_progress(conn, execution_id="dse_x", project_id="proj_EXAMPLE")

    statement = next(
        s for s, _ in conn.statements if s.startswith("UPDATE app.datastream_executions")
    )
    # Qualified since story 58.10: the statement joins the row as it was BEFORE
    # the update (`prior`) to learn which step is being LEFT, so every column of
    # the target carries its alias.
    assert "days_done = GREATEST(COALESCE(e.days_done, 0), %s)" in statement
    assert "GREATEST(COALESCE(e.rows_written, 0), %s::bigint)" in statement


def test_replaying_a_finished_window_proposes_the_same_value_twice():
    """Derivation, not accumulation: the second pass proposes what the first did."""
    jobs = [
        {"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-10",
         "state": "done", "row_count": 500},
    ]
    conn = _Conn(jobs)
    record_window_progress(conn, execution_id="dse_x")
    record_window_progress(conn, execution_id="dse_x")

    # params: (step, day_in_progress, days_done, rows_written, rows_written, ...)
    assert conn.updates[0][2] == conn.updates[1][2] == 10
    assert conn.updates[0][3] == conn.updates[1][3] == 500


def test_a_run_with_no_declared_window_leaves_the_total_null():
    conn = _Conn([])
    record_window_progress(conn, execution_id="dse_x")
    statement = next(
        s for s, _ in conn.statements if s.startswith("UPDATE app.datastream_executions")
    )
    # The per-window write never ASSIGNS days_total: only the dispatch that knows
    # the windows declares one, and NULL is what "none declared" looks like. It
    # is read back (RETURNING) but never written here.
    set_clause = statement.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assert "days_total" not in set_clause
    assert "days_total" in statement.split("RETURNING", 1)[1]


# ---------------------------------------------------------------------------
# The vocabulary is the product's own.
# ---------------------------------------------------------------------------


def test_the_step_vocabulary_is_the_ratified_one():
    """datastream-workbench-and-wizard.md: Collect / Map / Check / Publish."""
    assert PROGRESS_STEPS == ("Collect", "Map", "Check", "Publish")
    assert STEP_COLLECT == "Collect"


def test_a_step_outside_the_vocabulary_is_refused_loudly():
    conn = _Conn([])
    for word in ("Fetch", "Enrich", "Load", "collect"):
        with pytest.raises(ValueError):
            record_window_progress(conn, execution_id="dse_x", step=word)
    assert conn.updates == []


def test_a_collection_run_declares_its_kind():
    assert COLLECTION_PLAN_KIND == "recurring_collection"


# ---------------------------------------------------------------------------
# All terminal: what lets a run be closed instead of blocking the datastream.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("states", "expected"),
    [
        ([], True),                              # a run that took no window of its own
        (["done"], True),
        (["done", "failed"], True),
        (["done", "dead_letter"], True),
        (["done", "queued"], False),
        (["running"], False),
    ],
)
def test_a_run_is_over_when_no_window_of_it_can_move_again(states, expected):
    jobs = [
        {"id": f"job_{i}", "date_from": "2026-07-01", "date_to": "2026-07-01",
         "state": s, "row_count": 0}
        for i, s in enumerate(states)
    ]
    assert derive_progress(jobs).all_terminal is expected


# ---------------------------------------------------------------------------
# The execution payload serialises by TYPE, never by a list of column names.
# ---------------------------------------------------------------------------
#
# The three progress columns this story adds are date-like, and the payload
# builder used to isoformat() three hardcoded names. `admin_api` renders with the
# standard JSONResponse, whose TypeError fires inside render() -- outside the
# handler's try/except -- so an unserialised column is a bare 500 that logs
# nothing. The test below therefore names NO column: it adds an invented one, so
# it fails for the next date column too rather than for this story's.


class _DescribedCursor:
    def __init__(self, columns):
        self.description = [(name,) for name in columns]


def test_any_date_like_column_of_an_execution_row_is_serialised():
    from core.datastream_publication import _row_to_execution

    columns = (
        "id",
        "state",
        "row_count",
        # A column no code names anywhere: the point is that the TYPE decides.
        "a_column_added_after_this_test_was_written",
        "another_one_holding_a_plain_date",
        "and_one_holding_a_time",
    )
    row = (
        "dse_x",
        "loading",
        7,
        datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc),
        date(2026, 8, 5),
        time(2, 0),
    )
    record = _row_to_execution(_DescribedCursor(columns), row)

    for name, value in record.items():
        assert not isinstance(value, (datetime, date, time)), name
    assert record["a_column_added_after_this_test_was_written"].startswith("2026-08-05T")
    assert record["another_one_holding_a_plain_date"] == "2026-08-05"
    assert record["and_one_holding_a_time"] == "02:00:00"
    # Non-temporal values are untouched.
    assert record["row_count"] == 7
    assert record["state"] == "loading"


def test_a_failed_window_is_counted_as_failed_not_as_done():
    progress = derive_progress(
        [
            {"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-02",
             "state": "done", "row_count": 10},
            {"id": "job_2", "date_from": "2026-07-03", "date_to": "2026-07-04",
             "state": "dead_letter", "row_count": None},
        ]
    )
    assert isinstance(progress, CollectionProgress)
    assert progress.days_done == 2          # only the window that landed
    assert progress.rows_written == 10      # kept: what was collected before the failure
    assert progress.windows_failed == 1
    assert progress.all_terminal is True


# ---------------------------------------------------------------------------
# A window that ended WITHOUT being attempted -- story 63.6.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    sorted(
        set(pull_job_states.TERMINAL_JOB_STATES)
        - set(pull_job_states.ATTEMPTED_JOB_STATES)
    ),
)
def test_a_window_that_ended_without_being_attempted_is_terminal(state):
    """The class, not the instance -- and it is why a run could hang forever.

    Before story 63.6 this module named three states and read EVERYTHING else as
    a window still in flight. `superseded` (migration 022) already fell in that
    hole: one deduplicated window left `all_terminal` False, the run stayed
    `loading`, and `uq_datastream_executions_active` answered 409 to every later
    publish and to every following night's dispatch, forever.

    Parametrised over the registry, so the next state added is covered without
    anyone remembering this paragraph.
    """
    progress = derive_progress(
        [
            {"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-02",
             "state": "done", "row_count": 10},
            {"id": "job_2", "date_from": "2026-07-03", "date_to": "2026-07-04",
             "state": state, "row_count": None},
        ]
    )
    assert progress.all_terminal is True
    assert progress.windows_stopped == 1
    # And it is NOT a failure: `close_collection_run_if_complete` would mark the
    # whole run failed, and story 57.8's sweep would retry it within the hour.
    assert progress.windows_failed == 0
    # What landed before the stop is kept. The raw zone is append-only.
    assert progress.days_done == 2
    assert progress.rows_written == 10


def test_a_window_landing_after_the_stop_does_not_leave_the_run_open():
    """The window in flight finishes -- and finishing it must close nothing open.

    The run is already terminal by then; what this pins is that the DERIVATION
    agrees, so no later reader re-opens the question of whether this run is over.
    """
    jobs = [
        {"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-02",
         "state": "running", "row_count": None},
        {"id": "job_2", "date_from": "2026-07-03", "date_to": "2026-07-04",
         "state": pull_job_states.CANCELLED, "row_count": None},
    ]
    assert derive_progress(jobs).all_terminal is False
    jobs[0]["state"] = "done"
    jobs[0]["row_count"] = 700
    landed = derive_progress(jobs)
    assert landed.all_terminal is True
    assert landed.rows_written == 700


def test_the_progress_of_a_run_that_is_over_is_frozen():
    """Arbitrage 4: a number that changes after the stop is a number reported wrong.

    The write filters the run's own state, so a window landing at 10:07 on a run
    stopped at 10:04 cannot move `days_done`, `rows_written` or
    `progress_updated_at`. The statement is read rather than executed: what is
    being pinned is that the predicate is THERE and that it comes from the
    registry, not that psycopg applies it.
    """
    from core.execution_states import TERMINAL_STATES

    conn = _Conn(
        [{"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-02",
          "state": "done", "row_count": 10}]
    )
    record_window_progress(conn, execution_id="dse_sample", project_id="proj_EXAMPLE")
    statement, params = next(
        entry for entry in conn.statements
        if entry[0].startswith("UPDATE app.datastream_executions")
    )
    assert "NOT (e.state = ANY(%s))" in statement
    assert list(TERMINAL_STATES) in [p for p in params if isinstance(p, list)]


def test_a_terminal_run_writes_nothing_and_says_so():
    """No row updated -> None, the same answer a project-scope mismatch gives.

    A caller that received a payload here would report a progress write that the
    database refused.
    """
    conn = _Conn(
        [{"id": "job_1", "date_from": "2026-07-01", "date_to": "2026-07-02",
          "state": "done", "row_count": 10}]
    )
    conn.refuses_update = True
    assert record_window_progress(conn, execution_id="dse_sample") is None
