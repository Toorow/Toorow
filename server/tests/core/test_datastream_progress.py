"""The progress reader, on a simulated cursor -- story 63.2.

Four questions a fake cursor answers honestly, and one it answers better than a
live database because it can be made to lie: does the SELECT really carry BOTH
`datastream_id` and `project_id`? `_require_datastream_role` does not check that
a Datastream belongs to the project it is asked about -- the id it receives only
reaches the audit metadata -- so the scope of this route lives in its SQL and
nowhere else.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from core import execution_states, run_origins
from core.datastream_progress_api import (
    IDLE_LAST_RUN_FAILED,
    IDLE_LAST_RUN_SUCCEEDED,
    IDLE_NEVER_RAN,
    PROGRESS_FIELDS,
    PROGRESS_SCHEMA,
    DatastreamNotFound,
    compose_payload,
    read_active_progress,
    read_idle_reason,
)

ROOT = Path(__file__).resolve().parents[3]
MODULE = ROOT / "server" / "core" / "datastream_progress_api.py"
SURFACE = (
    ROOT / "docs" / "product-architecture" / "datastream-workbench-and-wizard.md"
)

_COLUMNS = (
    "datastream_id",
    "execution_id",
    "state",
    "step",
    "day_in_progress",
    "days_done",
    "days_total",
    "rows_written",
    "started_at",
    "progress_updated_at",
    "plan_version_id",
    "mapping_version_id",
    "windows_total",
    "windows_done",
    "window_from",
    "window_to",
    "window_started_at",
    "window_completed_at",
    "history_seconds_per_day",
    "measured_at",
    # Story 63.7: the plan the origin is derived FROM. It travels on the row the
    # poll already reads and is popped before the payload is built -- what
    # reaches the wire is one resolved field, never the plan.
    "projection_plan_ref",
)

#: Story 63.6 added the three that say WHAT WAS KEPT. After a stop the run is
#: terminal, so `PROGRESS_SQL` no longer returns it and this is the only payload
#: that still carries the days and the rows the run collected.
_IDLE_COLUMNS = (
    "execution_id", "state", "ended_at", "error_code",
    "days_done", "days_total", "rows_written",
)


class _Cursor:
    """Records the statement and parameters it was given, returns one row."""

    def __init__(self, row, columns=_COLUMNS):
        self._row = row
        self.statements: list[str] = []
        self.parameters: list[tuple] = []
        self.description = [(name,) for name in columns]

    def execute(self, sql, params=None):
        self.statements.append(sql)
        self.parameters.append(params)

    def fetchone(self):
        return self._row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Connection:
    def __init__(self, row, columns=_COLUMNS):
        self.cursor_object = _Cursor(row, columns)

    def cursor(self):
        return self.cursor_object


def _row(**overrides):
    values = {
        "datastream_id": "ds_progress",
        "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B",
        "state": "loading",
        "step": "Collect",
        "day_in_progress": date(2026, 7, 12),
        "days_done": 12,
        "days_total": 30,
        "rows_written": 4218,
        "started_at": datetime(2026, 8, 5, 2, 15, tzinfo=timezone.utc),
        "progress_updated_at": datetime(2026, 8, 5, 2, 41, tzinfo=timezone.utc),
        "plan_version_id": "dsp_progress",
        "mapping_version_id": "dmap_progress",
        # 24 monthly windows: one is in flight, one has landed.
        "windows_total": 24,
        "windows_done": 1,
        "window_from": date(2026, 7, 1),
        "window_to": date(2026, 7, 31),
        # The per-window chronometer: the window in flight has started and, by
        # definition, has not completed.
        "window_started_at": datetime(2026, 8, 5, 2, 41, tzinfo=timezone.utc),
        "window_completed_at": None,
        # Story 63.4: one seconds-per-day rate per finished run of this stream.
        "history_seconds_per_day": [30.0, 60.0, 90.0],
        "measured_at": datetime(2026, 8, 5, 2, 51, tzinfo=timezone.utc),
        "projection_plan_ref": {
            "executable": True,
            "kind": "recurring_collection",
            "origin": run_origins.SCHEDULER_NIGHTLY,
        },
    }
    values.update(overrides)
    return tuple(values[name] for name in _COLUMNS)


def _idle_row(**overrides):
    values = {
        "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9C",
        "state": "collected",
        "ended_at": datetime(2026, 8, 5, 3, 2, tzinfo=timezone.utc),
        "error_code": None,
        "days_done": None,
        "days_total": None,
        "rows_written": None,
    }
    values.update(overrides)
    return tuple(values[name] for name in _IDLE_COLUMNS)


# ---------------------------------------------------------------------------
# What it reads.
# ---------------------------------------------------------------------------


def test_an_active_run_is_returned_in_full() -> None:
    conn = _Connection(_row())
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    assert progress == {
        "execution_id": "dse_01J8ZC4Q0N7R2K3W5X6Y7Z8A9B",
        "state": "loading",
        # WHY A RUN THAT HAS NOT STARTED IS NOT STARTING. `None` here and on
        # every healthy run: the reader answers only for a materialization job
        # that FAILED, and the route asks only about a run with no `started_at`.
        # A key that appeared for a queued job and for a dead one would be read
        # as one fact.
        "materialization": None,
        # Story 63.7: WHY this run is running, resolved against the registry.
        "origin": run_origins.SCHEDULER_NIGHTLY,
        "step": "Collect",
        "day_in_progress": date(2026, 7, 12),
        "days_done": 12,
        "days_total": 30,
        "windows_done": 1,
        "windows_total": 24,
        "window_in_progress": {
            "date_from": date(2026, 7, 1),
            "date_to": date(2026, 7, 31),
            "days": 31,
            "started_at": datetime(2026, 8, 5, 2, 41, tzinfo=timezone.utc),
            "completed_at": None,
        },
        "rows_written": 4218,
        "started_at": datetime(2026, 8, 5, 2, 15, tzinfo=timezone.utc),
        "progress_updated_at": datetime(2026, 8, 5, 2, 41, tzinfo=timezone.utc),
        "plan_version_id": "dsp_progress",
        "mapping_version_id": "dmap_progress",
        # 18 days still owed, a median of 60 s per day, and a window that has
        # been running ten of the eighteen minutes it should cost. The band
        # (30..90 s per day) and its spread travel with the number.
        "estimate": {
            "armed": True,
            "observations": 3,
            "minimum_observations": 3,
            "precision": "point",
            "seconds_remaining": 480,
            "seconds_remaining_low": 0,
            "seconds_remaining_high": 1020,
            "spread_ratio": 3.0,
            "behind_by_seconds": 0,
            "measured_at": datetime(2026, 8, 5, 2, 51, tzinfo=timezone.utc),
            "reason": None,
            "sentence": "About 8 minutes left.",
        },
    }
    # The version identities are on the row already: a screen can say WHICH run
    # the load it is watching belongs to without one new column.
    assert set(PROGRESS_FIELDS) == set(progress)


def test_the_payload_can_explain_a_day_count_that_is_standing_still() -> None:
    """`days_done` moves at a WINDOW boundary, so it advances in steps of up to 31.

    A counter frozen for twenty minutes and a run that is stuck are the same
    picture unless the payload names the window in flight AND its size. 63.3
    cannot invent that on the screen.
    """
    conn = _Connection(_row(days_done=0, days_total=730, windows_done=0, windows_total=24))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    assert progress is not None
    assert (progress["days_done"], progress["days_total"]) == (0, 730)
    assert (progress["windows_done"], progress["windows_total"]) == (0, 24)
    assert progress["window_in_progress"]["days"] == 31
    assert progress["window_in_progress"]["date_from"] == date(2026, 7, 1)


def test_a_run_between_two_windows_names_no_window_in_flight() -> None:
    conn = _Connection(_row(window_from=None, window_to=None))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    assert progress is not None
    assert progress["window_in_progress"] is None


def test_a_datastream_with_no_active_run_reads_as_none() -> None:
    """The answer that stops 63.3's poll -- and the same one for a flux that never ran."""
    conn = _Connection(_row(execution_id=None, state=None, step=None, days_done=None))
    assert read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress") is None


def test_an_unwritten_column_stays_none_and_is_never_a_zero() -> None:
    """A run that has landed nothing has no row count. `0` would be a claim it never made."""
    conn = _Connection(
        _row(
            days_done=None,
            days_total=None,
            rows_written=None,
            day_in_progress=None,
            # No pull job bound to this run: it declared NO window. Not zero.
            windows_total=0,
            windows_done=0,
        )
    )
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    assert progress is not None
    for field in (
        "days_done",
        "days_total",
        "rows_written",
        "day_in_progress",
        "windows_total",
        "windows_done",
    ):
        assert progress[field] is None, field
        assert progress[field] != 0


def test_a_datastream_of_another_project_is_not_found_not_an_empty_payload() -> None:
    """Non-disclosing: the same refusal as one that does not exist at all."""
    conn = _Connection(None)
    with pytest.raises(DatastreamNotFound):
        read_active_progress(conn, project_id="proj_other", datastream_id="ds_progress")


# ---------------------------------------------------------------------------
# What the statement itself says.
# ---------------------------------------------------------------------------


def test_the_select_filters_on_datastream_id_AND_project_id() -> None:
    """The guard does not prove the pair; this SELECT is the only thing that does."""
    conn = _Connection(_row())
    read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    statement = conn.cursor_object.statements[0]
    assert "d.id = %s" in statement
    assert "d.project_id = %s" in statement
    assert "run.project_id = d.project_id" in statement
    assert conn.cursor_object.parameters[0] == ("ds_progress", "proj_a")


def test_the_poll_is_one_statement_and_the_run_itself_is_never_sorted() -> None:
    """`uq_datastream_executions_active` allows at most one row: nothing to sort.

    Every ordering in the statement is inside a pull-job LATERAL and every one of
    them is BOUNDED by a LIMIT of its own -- the window in flight, the rows the
    rate is read from, and the runs it is grouped into. The executions table is
    never sorted: it grows by one row per Datastream per night, and this is the
    one statement of the product executed on a timer.
    """
    from core.datastream_progress_estimate import HISTORY_RUNS, HISTORY_WINDOWS

    conn = _Connection(_row())
    read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    assert len(conn.cursor_object.statements) == 1
    statement = conn.cursor_object.statements[0]
    assert [" ".join(line.split()) for line in re.findall(r"ORDER BY .+", statement)] == [
        "ORDER BY j.date_from ASC, j.id ASC",
        # The ROW read, bounded before the aggregate -- migration 219.
        "ORDER BY j.completed_at DESC",
        "ORDER BY max(recent.completed_at) DESC",
    ]
    assert statement.upper().count("LIMIT") == 3
    assert f"LIMIT {HISTORY_WINDOWS}" in statement
    assert f"LIMIT {HISTORY_RUNS}" in statement
    assert not re.search(r"ORDER BY[^\n]*\brun\.", statement), (
        "the executions table is being sorted on the one route polled in a loop"
    )


def test_the_state_lists_are_generated_from_their_registries() -> None:
    from core import execution_progress

    conn = _Connection(_row())
    read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    quoted = set(re.findall(r"'([a-z_]+)'", conn.cursor_object.statements[0]))
    execution = quoted & set(execution_states.BY_NAME)
    # `failed` is BOTH an execution state and a pull-job state; the job list is
    # what the pull-job clauses carry, so the two sets are compared together.
    assert quoted == set(execution_states.ACTIVE_STATES) | set(
        execution_progress.TERMINAL_JOB_STATES
    ) | {execution_progress.JOB_DONE}
    # Only stored states may reach SQL: a name the column cannot hold would be a
    # clause that reads like a rule and filters nothing.
    assert execution <= set(execution_states.STORED_STATES)


def test_the_idle_read_asks_the_registry_which_states_are_terminal() -> None:
    conn = _Connection(_idle_row(), _IDLE_COLUMNS)
    read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")

    statement = conn.cursor_object.statements[0]
    assert "e.datastream_id = %s" in statement
    assert "e.project_id = %s" in statement
    assert set(re.findall(r"'([a-z_]+)'", statement)) == set(
        execution_states.TERMINAL_STATES
    )
    assert conn.cursor_object.parameters[0] == ("ds_progress", "proj_a")


def test_no_execution_state_is_typed_in_this_module() -> None:
    """Story 63.1's rule, applied to its first new reader.

    Six copies of the state list existed before `core.execution_states`, and
    adding one state broke four surfaces in four ways. This module generates its
    list; the guard is that no state name appears as a literal in its source.
    """
    source = MODULE.read_text(encoding="utf-8")
    # The docstrings and comments may name a state; the CODE may not.
    code = "\n".join(
        line for line in source.splitlines() if not line.lstrip().startswith("#")
    )
    typed = [
        name
        for name in execution_states.BY_NAME
        if f'"{name}"' in code or f"'{name}'" in code
    ]
    assert not typed, f"these state names are typed instead of generated: {typed}"


# ---------------------------------------------------------------------------
# How much longer -- story 63.4.
#
# The estimate is taken on the FINISHED RUNS of this Datastream, not on the days
# already done of the run in flight: the largest window count any path in this
# repository produces is three, so a run offers at most two finished windows
# before it ends and usually offers none. It is computed on the SERVER because
# the MCP reads the same payload as the screen.
# ---------------------------------------------------------------------------


def _estimate(**overrides):
    conn = _Connection(_row(**overrides))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")
    assert progress is not None
    return progress["estimate"]


def test_the_estimate_is_the_median_of_this_datastreams_finished_runs() -> None:
    """59 days owed, 60 s per day, and 10 of the window's 31 minutes already spent."""
    estimate = _estimate(
        days_done=31,
        days_total=90,
        windows_done=1,
        windows_total=3,
        window_from=date(2026, 8, 1),
        window_to=date(2026, 8, 31),
        # A MEDIAN, not a mean -- and a sample tight enough (factor 2) for one
        # number to be an honest claim about it.
        history_seconds_per_day=[30.0, 60.0, 60.0, 60.0],
    )

    # 31 days in flight at 60 s = 1860 s expected, 600 s already spent -> 1260 s
    # left on this window, plus 28 further days at 60 s.
    assert estimate["armed"] is True
    assert estimate["precision"] == "point"
    assert estimate["seconds_remaining"] == 1260 + 28 * 60
    assert estimate["behind_by_seconds"] == 0
    assert estimate["sentence"] == "About 49 minutes left."
    assert (estimate["observations"], estimate["minimum_observations"]) == (4, 3)
    # The band is published beside the point, always -- the fastest run measured
    # (30 s/day) and the slowest (60 s/day) are what the point sits between.
    assert estimate["seconds_remaining_low"] == max(0, 30 * 31 - 600) + 28 * 30
    assert estimate["seconds_remaining_high"] == 1260 + 28 * 60
    assert estimate["spread_ratio"] == 2.0


def test_a_run_that_has_outrun_the_sample_stops_estimating_and_says_by_how_much() -> None:
    """The defect this replaces, and it was PINNED by a test: `seconds_remaining: 0`.

    `max(0, expected - elapsed)` clamps to zero the instant a run passes what the
    sample predicted, so a window stalled for 48 hours answered "Less than a
    minute left." forever -- a `0` standing in for an absence, on the exact
    screen an operator opens when something is wrong. Once the window has been
    running longer than the SLOWEST run ever measured for this Datastream,
    nothing in the sample describes it, so nothing is multiplied by it.
    """
    from core.datastream_progress_estimate import ESTIMATE_LONGER_THAN_MEASURED

    estimate = _estimate(
        days_done=0,
        days_total=31,
        windows_done=0,
        windows_total=1,
        window_from=date(2026, 8, 1),
        window_to=date(2026, 8, 31),
        history_seconds_per_day=[60.0, 60.0, 60.0],
        # 31 days should cost 1860 s at the slowest rate ever measured; this
        # window has been running 3600 s.
        window_started_at=datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc),
        measured_at=datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc),
    )

    assert estimate["armed"] is False
    assert estimate["reason"] == ESTIMATE_LONGER_THAN_MEASURED
    assert estimate["seconds_remaining"] is None
    assert estimate["precision"] is None
    assert estimate["behind_by_seconds"] == 3600 - 1860
    assert estimate["sentence"] == (
        "This window has been running 29 minutes longer than anything measured "
        "for this Datastream — no estimate."
    )


def test_a_stalled_window_never_counts_down_to_zero() -> None:
    """48 hours on a window: the answer gets WORSE, it does not reach zero."""
    for hours in (2, 12, 48):
        estimate = _estimate(
            days_done=0,
            days_total=31,
            windows_done=0,
            windows_total=1,
            window_from=date(2026, 8, 1),
            window_to=date(2026, 8, 31),
            history_seconds_per_day=[60.0, 60.0, 60.0],
            window_started_at=datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc),
            measured_at=datetime(2026, 8, 5 + hours // 24, hours % 24, 0,
                                 tzinfo=timezone.utc),
        )
        assert estimate["seconds_remaining"] is None, hours
        assert estimate["armed"] is False, hours
        assert estimate["behind_by_seconds"] == hours * 3600 - 1860, hours


def test_a_median_over_a_scattered_sample_is_published_as_a_range() -> None:
    """10, 120 and 1200 seconds per day is a factor of 120 between two runs.

    "About 20 minutes left." said off that sample carries a confidence the
    measurement does not have. The dispersion is published beside every answer,
    and past `MAX_SPREAD_RATIO` the sentence degrades to the band it really is.
    """
    from core.datastream_progress_estimate import MAX_SPREAD_RATIO

    estimate = _estimate(
        days_done=0,
        days_total=10,
        windows_done=0,
        windows_total=1,
        window_from=date(2026, 8, 1),
        window_to=date(2026, 8, 10),
        window_started_at=None,
        history_seconds_per_day=[10.0, 120.0, 1200.0],
    )

    assert estimate["armed"] is True
    assert estimate["precision"] == "range"
    assert estimate["spread_ratio"] == 120.0
    assert estimate["spread_ratio"] > MAX_SPREAD_RATIO
    # No single number to mistake for a measurement.
    assert estimate["seconds_remaining"] is None
    assert estimate["seconds_remaining_low"] == 100
    assert estimate["seconds_remaining_high"] == 12000
    assert estimate["sentence"] == (
        "Between 2 minutes and 3 hours 20 minutes left."
    )


def test_the_dispersion_is_published_even_when_the_sample_is_tight() -> None:
    """`fetch_detector_readiness` discloses its bound with the answer, not instead of it."""
    estimate = _estimate(history_seconds_per_day=[50.0, 60.0, 70.0])

    assert estimate["armed"] is True
    assert estimate["precision"] == "point"
    assert estimate["spread_ratio"] == 1.4
    assert estimate["seconds_remaining_low"] < estimate["seconds_remaining"]
    assert estimate["seconds_remaining"] < estimate["seconds_remaining_high"]


def test_below_three_finished_runs_it_says_nothing_and_discloses_the_bound() -> None:
    """The `fetch_detector_readiness` shape: a silence that carries its count.

    A median over one observation is an invented number. Which is why the
    minimum is disclosed rather than the line simply being absent.
    """
    from core.datastream_progress_estimate import (
        ESTIMATE_NOT_ENOUGH_HISTORY,
        MINIMUM_FINISHED_RUNS,
    )

    estimate = _estimate(history_seconds_per_day=[60.0, 60.0])

    assert estimate["armed"] is False
    assert estimate["reason"] == ESTIMATE_NOT_ENOUGH_HISTORY
    assert estimate["seconds_remaining"] is None
    assert estimate["observations"] == 2
    assert estimate["minimum_observations"] == MINIMUM_FINISHED_RUNS
    assert "2 of 3" in estimate["sentence"]


def test_a_datastream_that_has_never_finished_a_run_is_a_sentence_not_a_blank() -> None:
    estimate = _estimate(history_seconds_per_day=None)

    assert estimate["armed"] is False
    assert estimate["observations"] == 0
    assert estimate["sentence"].strip() != ""


def test_a_run_that_has_declared_nothing_yet_is_not_estimated() -> None:
    """`days_total` is NULL until story 63.1 writes it. Nothing to multiply."""
    from core.datastream_progress_estimate import ESTIMATE_RUN_NOT_MEASURED_YET

    for missing in ({"days_total": None}, {"days_done": None}):
        estimate = _estimate(**missing)
        assert estimate["reason"] == ESTIMATE_RUN_NOT_MEASURED_YET, missing
        assert estimate["seconds_remaining"] is None


def test_a_history_that_is_not_a_rate_is_refused_rather_than_divided_by() -> None:
    """Four runs, none of which describes elapsed work: a THIRD silence.

    Folding this into "not enough history" would tell an operator that a
    Datastream which has run twenty times has never run.
    """
    from core.datastream_progress_estimate import ESTIMATE_INCONSISTENT_RATE

    estimate = _estimate(history_seconds_per_day=[0.0, -5.0, 0.0, 0.0])

    assert estimate["armed"] is False
    assert estimate["reason"] == ESTIMATE_INCONSISTENT_RATE
    assert estimate["observations"] == 4
    assert estimate["seconds_remaining"] is None


def test_every_silence_carries_a_phrase_and_never_a_zero() -> None:
    """`0` and `—` are both refused: neither is a measurement that was made."""
    from core.datastream_progress_estimate import ESTIMATE_REASONS

    stalled = {
        "window_started_at": datetime(2026, 8, 5, 0, 0, tzinfo=timezone.utc),
        "measured_at": datetime(2026, 8, 6, 0, 0, tzinfo=timezone.utc),
        "history_seconds_per_day": [60.0, 60.0, 60.0],
    }
    seen = set()
    for overrides in (
        {"days_total": None},
        {"history_seconds_per_day": [60.0]},
        {"history_seconds_per_day": [0.0, 0.0, 0.0]},
        stalled,
    ):
        estimate = _estimate(**overrides)
        seen.add(estimate["reason"])
        assert estimate["reason"] in ESTIMATE_REASONS
        assert estimate["armed"] is False
        assert estimate["seconds_remaining"] is None
        assert estimate["seconds_remaining_low"] is None
        assert estimate["seconds_remaining_high"] is None
        assert estimate["precision"] is None
        assert len(estimate["sentence"]) > 20
    # Four distinct silences, never one for all four.
    assert seen == set(ESTIMATE_REASONS)
    assert len(ESTIMATE_REASONS) == 4


def test_the_estimate_divides_by_DAYS_and_never_by_windows() -> None:
    """A 31-day window does not cost what a one-day window costs.

    The two runs below have the SAME number of windows in flight and thirty-one
    times the work; an estimate divided by windows would give them the same
    answer.
    """
    from core.datastream_progress_estimate import estimate_time_left

    common = {
        "days_done": 0,
        "window_started_at": None,
        "samples": [60.0, 60.0, 60.0],
        "measured_at": datetime(2026, 8, 5, 3, 0, tzinfo=timezone.utc),
    }
    wide = estimate_time_left(days_total=31, window_days=31, **common)
    narrow = estimate_time_left(days_total=1, window_days=1, **common)

    assert wide["seconds_remaining"] == 31 * 60
    assert narrow["seconds_remaining"] == 60
    assert wide["seconds_remaining"] == 31 * narrow["seconds_remaining"]


def test_both_ends_of_every_subtraction_come_from_the_server() -> None:
    """A browser epoch compared with a server timestamp manufactures time.

    Only `measured_at` -- `now()`, taken by the statement that read the run --
    moves the answer. Nothing in this arithmetic can be supplied by a caller's
    clock.
    """
    from core.datastream_progress_estimate import estimate_time_left

    fixed = {
        "days_done": 0,
        "days_total": 31,
        "window_days": 31,
        "window_started_at": datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc),
        "samples": [60.0, 60.0, 60.0],
    }
    early = estimate_time_left(measured_at=datetime(2026, 8, 5, 2, 0, tzinfo=timezone.utc), **fixed)
    later = estimate_time_left(
        measured_at=datetime(2026, 8, 5, 2, 10, tzinfo=timezone.utc), **fixed
    )

    assert early["seconds_remaining"] == 31 * 60
    assert later["seconds_remaining"] == 31 * 60 - 600
    assert early["measured_at"] != later["measured_at"]
    # A naive instant beside an aware one is refused, not silently coerced.
    naive = estimate_time_left(
        **{**fixed, "window_started_at": datetime(2026, 8, 5, 2, 0)},
        measured_at=datetime(2026, 8, 5, 2, 10, tzinfo=timezone.utc),
    )
    assert naive["seconds_remaining"] == 31 * 60


def test_the_history_lateral_is_scoped_to_this_stream_and_excludes_the_run() -> None:
    """It reads FINISHED runs of THIS Datastream, and pays no statement for them.

    `uq_datastream_executions_active` allows at most one non-terminal execution
    per Datastream, so `execution_id <> run.id` is what "finished" means here.
    """
    conn = _Connection(_row())
    read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    statement = conn.cursor_object.statements[0]
    assert len(conn.cursor_object.statements) == 1
    assert "j.datastream_id = d.id" in statement
    assert "j.execution_id <> run.id" in statement
    assert "j.date_to - j.date_from + 1" in statement
    assert "now() AS measured_at" in statement


def test_the_duration_is_spelled_in_english_at_the_precision_it_is_worth() -> None:
    """One formatter, on the server, so the console and the MCP say one thing."""
    from core.datastream_progress_estimate import format_duration_english

    assert format_duration_english(0) == "less than a minute"
    assert format_duration_english(59) == "less than a minute"
    assert format_duration_english(60) == "1 minute"
    assert format_duration_english(49 * 60) == "49 minutes"
    assert format_duration_english(3600) == "1 hour"
    assert format_duration_english(3600 + 30 * 60) == "1 hour 30 minutes"
    assert format_duration_english(2 * 86400) == "2 days"
    assert format_duration_english(86400 + 3600) == "1 day 1 hour"
    assert format_duration_english(None) == "an unknown time"
    assert format_duration_english(-1) == "an unknown time"


# ---------------------------------------------------------------------------
# Why the poll stops -- three sentences, one test each.
# ---------------------------------------------------------------------------


def test_a_flux_that_never_ran_says_so() -> None:
    conn = _Connection(None, _IDLE_COLUMNS)
    idle = read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")

    from core.datastream_progress_api import IDLE_FIELDS

    assert idle == {**{field: None for field in IDLE_FIELDS}, "reason": IDLE_NEVER_RAN}
    # Never a `0` for a flux that never ran: absent is not zero.
    assert idle["days_done"] is None
    assert idle["rows_written"] is None


def test_a_run_that_finished_well_is_not_the_same_sentence_as_one_that_broke() -> None:
    conn = _Connection(_idle_row(), _IDLE_COLUMNS)
    idle = read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")

    assert idle["reason"] == IDLE_LAST_RUN_SUCCEEDED
    assert idle["state"] == "collected"
    assert idle["error_code"] is None


def test_a_run_that_ended_badly_carries_its_state_and_its_error_code() -> None:
    """`cancelled` is never flattened into `failed`: the exact state travels.

    Story 63.6 went one step further -- a stopped run gets its OWN reason -- so
    this test now covers the adverse states that are NOT a deliberate stop, and
    the one that is has a test of its own below.
    """
    from core.datastream_publication import STATE_CANCELLED

    states = [
        name for name in execution_states.ADVERSE_STATES
        if name in set(execution_states.TERMINAL_STATES) and name != STATE_CANCELLED
    ]
    assert states, "the sweep has stopped covering anything"
    for state in states:
        conn = _Connection(
            _idle_row(state=state, error_code="collection_window_failed"), _IDLE_COLUMNS
        )
        idle = read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")

        assert idle["reason"] == IDLE_LAST_RUN_FAILED, state
        assert idle["state"] == state
        assert idle["error_code"] == "collection_window_failed"


def test_a_run_somebody_stopped_is_not_reported_as_a_failure() -> None:
    """Story 63.6. Its own sentence, on every reader -- the MCP included.

    Under `last_run_failed` a deliberate stop would fire the failure phrasing on
    all three surfaces of 63.5 and send an operator hunting for an outage that
    never happened.
    """
    from core.datastream_progress_api import IDLE_LAST_RUN_STOPPED
    from core.datastream_publication import STATE_CANCELLED

    conn = _Connection(
        _idle_row(state=STATE_CANCELLED, error_code="collection_run_stopped"),
        _IDLE_COLUMNS,
    )
    idle = read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")

    assert idle["reason"] == IDLE_LAST_RUN_STOPPED
    assert idle["reason"] != IDLE_LAST_RUN_FAILED
    assert idle["state"] == STATE_CANCELLED


def test_a_stopped_run_carries_what_it_kept_because_nothing_else_does() -> None:
    """The whole promise of the gesture: the days already collected stay.

    Once the run is terminal `PROGRESS_SQL` stops joining it, so without these
    columns "show what was kept" is on NO payload and the screen has nothing
    true to say.
    """
    from core.datastream_publication import STATE_CANCELLED

    conn = _Connection(
        _idle_row(state=STATE_CANCELLED, days_done=31, days_total=730,
                  rows_written=18_420),
        _IDLE_COLUMNS,
    )
    idle = read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")

    assert idle["days_done"] == 31
    assert idle["days_total"] == 730
    assert idle["rows_written"] == 18_420


def test_the_idle_statement_reads_what_was_kept_in_the_same_row() -> None:
    """Two more columns on a row already being read, never a second statement.

    This branch is the one that ENDS the poll -- once per run, not once per tick
    -- and `tests/integration/test_datastream_progress_route_cost.py` holds the
    repeating tick at two statements regardless.
    """
    from core.datastream_progress_api import IDLE_SQL

    for column in ("days_done", "days_total", "rows_written"):
        assert f"e.{column}" in IDLE_SQL, column
    assert IDLE_SQL.count("SELECT") == 1
    assert IDLE_SQL.count("FROM") == 1


def test_every_reason_the_registry_can_produce_is_one_of_the_three() -> None:
    """A state added tomorrow cannot invent a fourth sentence in silence."""
    from core.datastream_progress_api import IDLE_REASONS

    for state in execution_states.TERMINAL_STATES:
        conn = _Connection(_idle_row(state=state), _IDLE_COLUMNS)
        idle = read_idle_reason(conn, project_id="proj_a", datastream_id="ds_progress")
        assert idle["reason"] in IDLE_REASONS, state


# ---------------------------------------------------------------------------
# The envelope.
# ---------------------------------------------------------------------------


def test_the_envelope_names_its_flux_and_why_nothing_runs() -> None:
    """A bare `null` cannot be routed: 63.3 has three polls in flight at once."""
    idle = {
        "reason": IDLE_LAST_RUN_FAILED,
        "execution_id": "dse_1",
        "state": "failed",
        "ended_at": None,
        "error_code": "collection_window_failed",
    }
    assert compose_payload(
        project_id="proj_a", datastream_id="ds_1", progress=None, idle=idle
    ) == {
        "schema": PROGRESS_SCHEMA,
        "project_id": "proj_a",
        "datastream_id": "ds_1",
        "progress": None,
        "idle": idle,
    }


def test_a_running_flux_carries_no_idle_reason() -> None:
    """Exactly one of the two is set: a run in flight has not stopped on anything."""
    payload = compose_payload(
        project_id="proj_a",
        datastream_id="ds_1",
        progress={"execution_id": "dse_1"},
        idle={"reason": IDLE_NEVER_RAN},
    )
    assert payload["idle"] is None
    assert payload["progress"] == {"execution_id": "dse_1"}


# ---------------------------------------------------------------------------
# The ratified surface says the same thing as the code.
# ---------------------------------------------------------------------------


def test_the_ratified_surface_carries_this_contract() -> None:
    """A behaviour whose document was not touched is an unfinished story.

    Before this change the ratified surface held no address for the live state
    of a run at all; every string asserted here is new with it.
    """
    from core.datastream_progress_api import IDLE_REASONS, PROGRESS_ROUTE_PATH

    text = SURFACE.read_text(encoding="utf-8")
    assert PROGRESS_ROUTE_PATH in text
    assert PROGRESS_SCHEMA in text
    for field in PROGRESS_FIELDS:
        assert field in text, field
    for reason in IDLE_REASONS:
        assert reason in text, reason
    # The sentences the surface may not lose: what stops the poll, what an
    # unwritten number is, and what a flux of another project is answered.
    assert "stops the poll" in text
    assert "`null`, never `0`" in text
    assert "404 not_found" in text


def test_the_surface_no_longer_asks_the_screen_for_a_unit_nobody_writes() -> None:
    """Corrected 2026-08-06, and the correction is guarded.

    The ratified text said "the screen says windows" while no `windows_done` /
    `windows_total` column exists anywhere -- a document asking a surface for a
    unit the server does not render. The screen counts DAYS, derived from the
    windows that finished; `windows_done` / `windows_total` are what the progress
    ROUTE derives from the run's pull jobs, and the migration is untouched.
    """
    text = SURFACE.read_text(encoding="utf-8")
    migration = (
        ROOT / "infra" / "nango" / "migrations"
        / "218_a_running_collection_writes_where_it_is.sql"
    ).read_text(encoding="utf-8")

    assert "the screen says windows" not in text
    assert "the unit the screen COUNTS is the day" in text
    # And it says WHY the count can stand still, before it does.
    assert "advances in STEPS" in text
    for column in ("windows_done", "windows_total"):
        assert f"ADD COLUMN IF NOT EXISTS {column}" not in migration


# ---------------------------------------------------------------------------
# Story 63.7 -- WHY this treatment is running, and the silence of the ones
# that read no provider window.
# ---------------------------------------------------------------------------


def _update_row(origin, **overrides):
    """A run that is NOT a collection: one activation job, and no pull job.

    `candidate_materialization` binds no `app.pull_jobs` row to the execution,
    so the three LATERALs of `PROGRESS_SQL` come back empty at once -- that is
    what the `None`s below are, and none of them is a failure to measure.
    """
    values = {
        "windows_total": None,
        "windows_done": None,
        "window_from": None,
        "window_to": None,
        "window_started_at": None,
        "window_completed_at": None,
        "history_seconds_per_day": None,
        "days_done": None,
        "days_total": None,
        "rows_written": None,
        "day_in_progress": None,
        # And `step` stays NULL. A run that collects nothing has not reached a
        # collection step, and writing one would invent a phase the server never
        # measured -- which is the thing this epic has refused since 63.4.
        "step": None,
        "projection_plan_ref": {"executable": True, "origin": origin},
    }
    values.update(overrides)
    return _row(**values)


def test_an_update_carries_its_origin_and_stays_silent_about_windows() -> None:
    """The four paths that do not collect are on this payload too.

    Before story 63.7 a mapping change arrived here indistinguishable from a
    nightly collection with every counter empty: the route's `run.state IN
    (ACTIVE_STATES)` join carries no filter of nature, so the candidate was
    already being returned -- with nothing on it that said what it was.
    """
    conn = _Connection(_update_row(run_origins.MAPPING_CHANGE))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")

    assert progress is not None
    assert progress["origin"] == run_origins.MAPPING_CHANGE
    assert run_origins.reads_provider_windows(progress["origin"]) is False
    # Silent, not zero. A `0` here is a measurement nobody made, and a fraction
    # over a run with no windows would be a claim the database cannot support.
    for field in ("windows_total", "windows_done", "window_in_progress",
                  "days_done", "days_total", "rows_written", "step"):
        assert progress[field] is None, field
    # The estimate declines rather than counting down from nothing.
    assert progress["estimate"]["armed"] is False
    assert progress["estimate"]["seconds_remaining"] is None
    assert progress["estimate"]["sentence"]


def test_an_origin_no_build_knows_travels_through_unreplaced() -> None:
    """A console older than its server shows what it was sent.

    The route resolves nothing and substitutes nothing: folding an unrecognised
    origin onto a neighbouring one would put a reason on a screen that no path
    ever wrote.
    """
    invented = "an_origin_no_build_knows"
    assert invented not in run_origins.ORIGIN_KEYS

    conn = _Connection(_update_row(invented))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")
    assert progress["origin"] == invented
    assert run_origins.label_for(invented) is None


def test_a_run_minted_before_this_story_says_it_has_no_origin() -> None:
    """`None`, and never a guessed one: nothing recorded which path created it."""
    conn = _Connection(_row(projection_plan_ref={"executable": True}))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")
    assert progress["origin"] is None

    # The same for a run whose plan column was never written at all.
    conn = _Connection(_row(projection_plan_ref=None))
    assert read_active_progress(
        conn, project_id="proj_a", datastream_id="ds_progress"
    )["origin"] is None


def test_the_plan_itself_never_reaches_the_wire() -> None:
    """One derived field leaves; the projection plan stays on the server.

    It carries the compiled projection, the recovery scope and the retained
    execution reference -- none of it a screen's business, and this is the one
    route of the product that is called in a loop.
    """
    conn = _Connection(_update_row(run_origins.PLAN_CHANGE))
    progress = read_active_progress(conn, project_id="proj_a", datastream_id="ds_progress")
    assert "projection_plan_ref" not in progress
    assert set(progress) == set(PROGRESS_FIELDS)


def test_the_registry_decides_which_origins_may_show_a_fraction() -> None:
    """A progress bar belongs to a run that declared windows, and to no other.

    Read from the registry rather than from a list here, so an origin added
    tomorrow is classified once, in one file, instead of being forgotten by the
    screen that would then paint it at 0 %.
    """
    for origin in run_origins.RUN_ORIGINS:
        if origin.reads_provider_windows:
            continue
        conn = _Connection(_update_row(origin.key))
        progress = read_active_progress(
            conn, project_id="proj_a", datastream_id="ds_progress"
        )
        assert progress["windows_total"] is None, origin.key
        assert progress["estimate"]["armed"] is False, origin.key
