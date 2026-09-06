"""Tests for AI-117 and AI-118: Datastream Schedule Management & Lookback Window Clarity.

Verifies:
1. `read_schedule` and `set_schedule` support 'nightly', 'weekly', 'hourly', and 'manual'.
2. `window_days` is preserved independently of the cadence.
3. `set_schedule` updates `schedule_mode`, `date_window_days`, and `next_run_at`.
"""

import pytest
from core.schedule_mcp import CADENCES, set_schedule

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
    flatten,
)


def test_ai118_cadences_tuple_includes_weekly() -> None:
    """AI-118: CADENCES tuple in schedule_mcp includes weekly, nightly, hourly, manual."""
    assert "weekly" in CADENCES
    assert "nightly" in CADENCES
    assert "hourly" in CADENCES
    assert "manual" in CADENCES


#: AI-317 -- EVERY STATEMENT THE TESTED PATH ISSUES ON THIS FAKE, NAMED. A
#: `set_schedule` call that only touches the Datastream row emits exactly three:
#: the existence check (`core/schedule_mcp.py:292`), the `UPDATE`
#: (`core/schedule_mcp.py:370`) and the full read `set_schedule` returns
#: (`read_schedule`, `core/schedule_mcp.py:153`).
#:
#: The three statements `set_schedule` issues on paths NO test in this file
#: walks are deliberately ABSENT: the run-state read (`:303`), the schedule-state
#: existence check behind `arrival_hour` (`:337`) and the `next_run_at` UPDATE
#: (`:382`). The old fake carried a branch for the last of them, answering
#: `rowcount = 1` to a statement nothing here ever sent. Absent, they now raise
#: and name themselves the day a test does walk them -- which is the only moment
#: their row shape can be modelled from something other than a guess.
INVENTORY = StatementInventory(
    "_FakeCursor",
    schedule_read="select d.schedule_mode",
    datastream_exists="select 1 from app.datastreams",
    datastream_update="update app.datastreams set",
)

#: The projection of `read_schedule`'s SELECT, which `describe()` refuses to
#: derive -- it carries two scalar sub-selects, and a fake that guessed at a
#: sub-select's column list would be inventing the query. Named ONCE, and the
#: row is assembled in this order, so the tuple and the description cannot come
#: to disagree about what column 9 is.
_SCHEDULE_READ_COLUMNS = (
    "schedule_mode",
    "enabled",
    "date_window_days",
    "refetch_days",
    "window_offset_days",
    "lifecycle_state",
    "next_run_at",
    "last_committed_watermark",
    "last_run_at",
    "arrival_hour_local",
    "retry_count",
    "reporting_timezone",
    # Lot D1: `archived_at`. A soft-archived Datastream keeps
    # `lifecycle_state = 'active'`, so the read needs this column to tell
    # "retired" from "paused this morning".
    "archived_at",
)


def _assigned_columns(sql: str) -> list[str]:
    """The columns an `UPDATE ... SET` binds to a parameter, in the order it binds them.

    Read off the statement rather than restated, because the product builds this
    SET list one clause at a time and its order IS the order of `params`. The
    old fake sniffed the parameter tuple instead -- `params[0] in CADENCES` for
    the cadence, "any int between 1 and 90" for the offset -- so a call setting
    a 7-day window and a 7-day offset could not be told apart from itself.
    """

    body = sql.split(" set ", 1)[1].split(" where ", 1)[0]
    return [
        clause.split("=", 1)[0].strip()
        for clause in body.split(",")
        if clause.strip().endswith("= %s")
    ]


class _FakeCursor:
    """A cursor that answers the three statements it was taught, and refuses the rest.

    AI-317: the previous version dispatched on four fragments and simply fell off
    the end of its `if/elif` for anything else -- keeping `_last_result` from the
    PREVIOUS statement, so an unrecognized read answered the row of whatever ran
    before it. Now an untaught statement raises (`INVENTORY.match`), and every
    statement resets the result, so "no row" is only ever said on purpose.
    """

    def __init__(self):
        # ONE state, keyed by the column names of the read -- the UPDATE writes
        # into the same dictionary the SELECT is projected from, which is what
        # makes "set it, then read it back" mean anything here.
        self._columns = {
            "schedule_mode": "nightly",
            "enabled": True,
            "date_window_days": 7,
            "refetch_days": 3,
            "window_offset_days": 1,
            "lifecycle_state": "active",
            "next_run_at": None,
            "last_committed_watermark": None,
            "last_run_at": None,
            # Story 57.8 widened the read: the arrival hour, the catch-up
            # counter and the project's reporting timezone travel with the
            # schedule now.
            "arrival_hour_local": None,
            "retry_count": 0,
            "reporting_timezone": None,
            "archived_at": None,
        }
        self._result = None
        self.description = None
        self.rowcount = 1

    def execute(self, query, params=()):
        statement = INVENTORY.match(query)
        self._result = None
        self.description = None
        if statement == "schedule_read":
            self.description = [(name,) for name in _SCHEDULE_READ_COLUMNS]
            self._result = tuple(self._columns[name] for name in _SCHEDULE_READ_COLUMNS)
        elif statement == "datastream_exists":
            self.description = describe(query)
            self._result = (1,)
        elif statement == "datastream_update":
            flat = flatten(query)
            for column, value in zip(_assigned_columns(flat), params):
                if column not in self._columns:
                    raise UnknownStatement(
                        f"_FakeCursor was never taught the column {column!r} this "
                        f"UPDATE writes: {flat}"
                    )
                self._columns[column] = value
            self.rowcount = 1

    def fetchone(self):
        return self._result

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class _FakeConnection:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317: a question this fixture was never asked can no longer be answered.

    "No row" is what an empty relation says, not what an unwritten query says.
    While the two sounded alike, a statement moved in `schedule_mcp.py` left
    these tests green over a path they had stopped walking.

    The refusal has to carry both halves of the repair: the statement ("which
    query moved?") and the inventory ("what did it used to look like?").
    """
    untaught = (
        "SELECT next_run_at FROM app.datastream_schedule_state "
        "WHERE datastream_id = %s"
    )

    with pytest.raises(UnknownStatement) as raised:
        _FakeCursor().execute(untaught)

    message = str(raised.value)
    assert "app.datastream_schedule_state" in message, message
    assert "schedule_read" in message, message


def test_ai117_set_schedule_weekly_cadence_and_window() -> None:
    """AI-117 & AI-118: set_schedule accepts weekly cadence and updates schedule cleanly."""
    conn = _FakeConnection()
    res = set_schedule(
        conn,
        project_id="proj_1",
        datastream_id="ds_1",
        cadence="weekly",
        window_days=7,
    )
    assert res["cadence"] == "weekly"
    assert res["window_days"] == 7
    assert res["runs_when"] == "at the platform weekly tick (next_run_at unset)"


def test_ai145_set_schedule_window_offset_days() -> None:
    """AI-145: set_schedule accepts window_offset_days and returns it in read_schedule."""
    conn = _FakeConnection()
    res = set_schedule(
        conn,
        project_id="proj_1",
        datastream_id="ds_1",
        window_offset_days=3,
    )
    assert res["window_offset_days"] == 3
