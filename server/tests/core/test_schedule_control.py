"""WHEN a Datastream runs is a product setting, editable from Postgres. AI-119.

What these tests protect is a single sentence: the moment lives in
`app.datastream_schedule_state.next_run_at`, and every door — the console's REST
seam, the MCP tools, the dispatcher — reads and writes THAT row. Before this,
`next_run_at` was computed at activation and read by nobody, while the actual
moment came from `SCHEDULER_NIGHTLY_HOUR`: one hour, one timezone, every project.
"""

from __future__ import annotations

from contextlib import contextmanager

import pytest
from core import schedule_mcp


class _Cur:
    def __init__(self, rows=None, rowcount=1):
        self._rows = list(rows or [])
        self.rowcount = rowcount
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.statements.append((" ".join(sql.split()), params))

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None


class _Conn:
    def __init__(self, cur):
        self._cur = cur
        self.committed = False

    def cursor(self):
        return self._cur

    def commit(self):
        self.committed = True


def _row(
    mode="nightly", enabled=True, window=7, refetch=3, offset=1, lifecycle="active",
    next_run=None, watermark=None, last_run=None, arrival_hour=None, retry_count=0,
    timezone_name=None, archived_at=None,
):
    """One `read_schedule` row, named rather than positional.

    The column list grew with story 57.8 (the arrival hour, the catch-up counter
    and the zone the hour is read in). A positional literal per test meant every
    addition rewrote every test, which is how a fixture stops describing what the
    test is about.
    """
    return (
        mode, enabled, window, refetch, offset, lifecycle,
        next_run, watermark, last_run, arrival_hour, retry_count, timezone_name,
        archived_at,
    )


def test_read_reports_one_effective_window_not_two_knobs():
    """`refetch_days` is a legacy ALIAS, not a second setting (AI-118).

    Reporting both as if they were independent would present an inherited
    fallback as a feature, and an operator would set one while the dispatcher
    obeyed the other.
    """
    cur = _Cur([_row(window=7, refetch=3)])
    result = schedule_mcp.read_schedule(_Conn(cur), project_id="proj_X", datastream_id="ds_X")
    assert result["window_days"] == 7
    assert result["window_source"] == "date_window_days"

    cur = _Cur([_row(window=None, refetch=3)])
    result = schedule_mcp.read_schedule(_Conn(cur), project_id="proj_X", datastream_id="ds_X")
    assert result["window_days"] == 3
    assert "legacy" in result["window_source"]


def test_read_joins_the_current_plan_version():
    """The table is keyed by plan_version_id: a datastream has one row per version.

    Reading without that join would answer with a schedule belonging to a version
    the dispatcher no longer looks at.
    """
    cur = _Cur([_row(refetch=None)])
    schedule_mcp.read_schedule(_Conn(cur), project_id="proj_X", datastream_id="ds_X")
    sql = cur.statements[0][0]
    assert "s.plan_version_id = d.current_plan_version_id" in sql


def test_weekly_is_accepted_as_cadence():
    """`weekly` is an accepted cadence mode (running once a week)."""
    cur = _Cur([(1,), _row(mode="weekly")])
    res = schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X", cadence="weekly"
    )
    assert res["cadence"] == "weekly"


def test_an_absurd_window_is_refused():
    cur = _Cur([(1,)])
    with pytest.raises(ValueError):
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", window_days=0
        )


def test_moving_the_run_of_a_never_activated_datastream_says_so():
    """UPDATE, never INSERT: the row belongs to a plan version activation creates.

    Inventing one would attach a schedule to no version -- one the dispatcher can
    never see -- and report success for a run that will never happen.
    """
    cur = _Cur([(1,)], rowcount=0)
    with pytest.raises(ValueError) as exc:
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X",
            next_run_at="2026-08-02T02:00:00Z",
        )
    assert "not been activated" in str(exc.value)


def test_only_what_is_given_is_written():
    """Moving the next run must not restate — and overwrite — the cadence."""
    cur = _Cur([(1,), _row(refetch=None)], rowcount=1)
    schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X",
        next_run_at="2026-08-02T02:00:00Z",
    )
    updates = [s for s, _ in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert updates == [], "the cadence was rewritten by a call that never mentioned it"


def test_the_dispatcher_honours_next_run_at():
    """The guard that makes the ledger authoritative, and keeps NULL compatible.

    Read from the source rather than asserted about behaviour: the whole defect
    was that this column was joined and never consulted.
    """
    from pathlib import Path

    source = Path(schedule_mcp.__file__).with_name("scheduler.py").read_text(encoding="utf-8")
    assert "ss.next_run_at IS NULL OR ss.next_run_at <= NOW()" in source
    assert "def _advance_next_run(" in source


def test_the_advance_is_scoped_to_the_current_plan_version():
    """Advancing every version's row would move schedules nothing dispatches."""
    from pathlib import Path

    source = Path(schedule_mcp.__file__).with_name("scheduler.py").read_text(encoding="utf-8")
    advance = source[source.index("def _advance_next_run("):]
    advance = advance[: advance.index("\ndef ")]
    assert "ss.plan_version_id = d.current_plan_version_id" in advance


def test_the_last_run_is_a_clock_reading_not_a_data_boundary():
    """`last_committed_watermark` answers a different question and keeps its name.

    Reporting a data boundary as "last run" would have someone read "it ran on
    2026-07-28" from a value that never meant that.
    """
    cur = _Cur([_row(refetch=None, watermark="2026-07-28")])
    result = schedule_mcp.read_schedule(_Conn(cur), project_id="p", datastream_id="d")
    assert result["last_run_at"] is None
    assert result["never_ran"] is True
    assert result["last_committed_watermark"] == "2026-07-28"
    assert "max(j.completed_at)" in cur.statements[0][0]


# ---------------------------------------------------------------------------
# Story 57.8 -- the arrival hour, and what happens when a pull fails.
# ---------------------------------------------------------------------------


def test_the_read_names_the_arrival_hour_its_origin_and_the_zone_it_is_read_in():
    """An hour without its zone is not a moment.

    The hour is local to the PROJECT, not to the reader's browser, so the read
    carries the zone that resolves it and says where that zone came from -- the
    same treatment `window_source` already gives the effective window.
    """
    cur = _Cur([_row(arrival_hour=6, timezone_name="America/New_York")])
    result = schedule_mcp.read_schedule(_Conn(cur), project_id="p", datastream_id="d")
    assert result["arrival_hour_local"] == 6
    assert result["arrival_hour_source"] == "datastream"
    assert result["timezone"] == "America/New_York"
    assert result["timezone_source"] == "project_preference"


def test_an_unset_arrival_hour_reads_as_absent_never_as_midnight():
    """`0` is a legal arrival hour. Reporting it for "nobody chose" erases both.

    The console has to be able to say "no arrival hour set"; it cannot do that
    from a payload that has already substituted a number for the absence.
    """
    cur = _Cur([_row(arrival_hour=None)])
    result = schedule_mcp.read_schedule(_Conn(cur), project_id="p", datastream_id="d")
    assert result["arrival_hour_local"] is None
    assert result["arrival_hour_source"] == "unset"


def test_the_read_says_what_happens_when_a_pull_fails():
    """A5 + A4: one extra attempt at the next hour, and the counter is shown.

    A cadence that already runs every hour has nothing to catch up to, and a
    manual Datastream has no clock at all -- both say so rather than promising a
    retry that will not happen.
    """
    cur = _Cur([_row(mode="nightly", retry_count=1)])
    nightly = schedule_mcp.read_schedule(_Conn(cur), project_id="p", datastream_id="d")
    assert nightly["on_failure"] == "retry_at_next_hour"
    assert nightly["retry_count"] == 1

    cur = _Cur([_row(mode="hourly")])
    hourly = schedule_mcp.read_schedule(_Conn(cur), project_id="p", datastream_id="d")
    assert hourly["on_failure"] == "next_hourly_run"

    cur = _Cur([_row(mode="manual")])
    manual = schedule_mcp.read_schedule(_Conn(cur), project_id="p", datastream_id="d")
    assert manual["on_failure"] == "nothing_is_retried"


def test_an_arrival_hour_outside_the_local_day_is_refused():
    for refused in (24, -1):
        cur = _Cur([(1,), (1,)])
        with pytest.raises(ValueError, match="between 0 and 23"):
            schedule_mcp.set_schedule(
                _Conn(cur), project_id="proj_X", datastream_id="ds_X", arrival_hour=refused
            )


def test_writing_the_arrival_hour_alone_rewrites_nothing_else():
    """Same guard as `test_only_what_is_given_is_written`, one setting further.

    A caller that moves the hour must not have to restate the cadence and the
    window, and must not silently overwrite them with whatever it last read.
    """
    cur = _Cur([(1,), (1,), _row(arrival_hour=6)], rowcount=1)
    result = schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X", arrival_hour=6
    )
    updates = [s for s, _ in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert len(updates) == 1
    assert "arrival_hour_local = %s" in updates[0]
    assert "schedule_mode" not in updates[0]
    assert "date_window_days" not in updates[0]
    assert result["arrival_hour_local"] == 6


def test_an_hour_cannot_be_written_on_a_datastream_that_was_never_activated():
    """The refusal `set_schedule` already makes for `next_run_at`, reused verbatim.

    An arrival hour is only ever read by the advance, and the advance only looks
    at the schedule state row activation creates. Accepting the write would
    report success for an hour nothing will ever honour.
    """
    cur = _Cur([(1,), None])
    with pytest.raises(ValueError) as exc:
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", arrival_hour=6
        )
    assert "not been activated" in str(exc.value)


def test_nothing_is_written_before_the_activation_check_refuses():
    """The refusal comes FIRST, so a rejected call leaves the row untouched.

    Checking after the UPDATE would depend on the caller's transaction being
    rolled back -- and `set_schedule` is handed a connection it does not own.
    """
    cur = _Cur([(1,), None])
    with pytest.raises(ValueError):
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X",
            arrival_hour=6, cadence="weekly",
        )
    assert [s for s, _ in cur.statements if s.startswith("UPDATE")] == []


# ---------------------------------------------------------------------------
# AI-117 -- a project's day is measured in the project's timezone.
# ---------------------------------------------------------------------------


def test_the_project_timezone_wins_over_the_deployment_constant():
    """`SCHEDULER_TIMEZONE` was a deployment constant standing in for a preference.

    Its own comment said so -- "yesterday is the PROJECT's day, not the server's"
    -- while `app.project_preferences.reporting_timezone` already carried the
    answer.
    """
    from core import scheduler

    cur = _Cur([("America/New_York",)])
    assert scheduler.project_timezone(_Conn(cur), "proj_X") == "America/New_York"


def test_a_project_without_a_preference_keeps_the_previous_behaviour():
    import os

    from core import scheduler

    previous = os.environ.get("SCHEDULER_TIMEZONE")
    os.environ["SCHEDULER_TIMEZONE"] = "Europe/Paris"
    try:
        cur = _Cur([None])
        assert scheduler.project_timezone(_Conn(cur), "proj_X") == "Europe/Paris"
    finally:
        if previous is None:
            os.environ.pop("SCHEDULER_TIMEZONE", None)
        else:
            os.environ["SCHEDULER_TIMEZONE"] = previous


def test_two_timezones_do_not_share_a_yesterday():
    """The defect in one assertion: a single value for every project.

    Between 18:00 and midnight in New York the two calendars disagree, so a
    Paris-decided "yesterday" asks a provider for a day the New York project has
    not finished living.
    """
    from core import scheduler

    paris = scheduler.project_yesterday("Europe/Paris")
    auckland = scheduler.project_yesterday("Pacific/Auckland")
    assert (auckland - paris).days in (0, 1), (
        "the two yesterdays are neither equal nor one day apart -- the computation "
        "is not reading a calendar"
    )


def test_an_unknown_timezone_falls_back_instead_of_stopping_a_dispatch():
    from datetime import date, timedelta

    from core import scheduler

    assert scheduler.project_yesterday("Not/AZone") == date.today() - timedelta(days=1)


def test_the_advance_preserves_the_local_hour_across_dst():
    """The SHAPE of the fix, pinned here; the BEHAVIOUR is proven against a database.

    This asserts on the source, which proves a string is present and nothing about
    what it does -- so it is not the guarantee. `tests/integration/
    test_schedule_advance_pg.py` executes the advance across a real DST boundary
    and measures 04:00 where naive arithmetic gives 03:00. What this one adds is
    cheap and worth keeping: it goes red without a database, the day someone
    removes the round trip.
    """
    from pathlib import Path

    from core import schedule_mcp

    source = Path(schedule_mcp.__file__).with_name("scheduler.py").read_text(encoding="utf-8")
    advance = source[source.index("def _advance_next_run("):]
    advance = advance[: advance.index("\ndef ")]
    assert "AT TIME ZONE" in advance
    assert "project_timezone(conn, project_id)" in advance


def test_an_arrival_hour_can_be_erased_and_the_absence_is_written():
    """`null` is an ERASURE, and it has to be told apart from "not mentioned".

    Both arrive at this function as `None` in Python, so an optional argument
    defaulting to `None` can express one or the other but never both. The console
    could therefore announce "arrival hour goes from 06:00 to not set", omit the
    key, and report success while the server kept 06:00.
    """
    cur = _Cur([(1,), (1,), _row(arrival_hour=None)], rowcount=1)
    result = schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X", arrival_hour=None
    )
    updates = [(s, p) for s, p in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert len(updates) == 1, "clearing the arrival hour wrote nothing at all"
    assert "arrival_hour_local = %s" in updates[0][0]
    assert updates[0][1][0] is None
    assert result["arrival_hour_local"] is None
    assert result["arrival_hour_source"] == "unset"


def test_a_call_that_never_mentions_the_arrival_hour_leaves_it_alone():
    """The other half of the same distinction, and the reason for the sentinel."""
    cur = _Cur([(1,), _row(arrival_hour=6)], rowcount=1)
    schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X", cadence="nightly"
    )
    updates = [s for s, _ in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert len(updates) == 1
    assert "arrival_hour_local" not in updates[0], (
        "a call about the cadence erased the arrival hour"
    )


# ---------------------------------------------------------------------------
# Lot D1 (issue #68) -- whether the schedule is armed at all.
#
# `enabled` was read by this module and put on the payload from the first day,
# and no door wrote it and no screen drew it. A person could set a frequency, an
# arrival hour, a window, an offset and a next run on a Datastream that will
# never run, and nothing said so. `enabled = false` on the live base.
# ---------------------------------------------------------------------------


def test_the_read_says_whether_the_schedule_runs_at_all():
    """The word is DERIVED from the pair the dispatcher reads, and only there.

    `_dispatch_nightly_datastreams` selects `d.enabled = TRUE AND
    d.lifecycle_state = 'active'`, so two columns decide it. Recombining them per
    door -- console, seam, model -- is three chances to disagree with the
    dispatcher.
    """
    running = schedule_mcp.read_schedule(
        _Conn(_Cur([_row(enabled=True, lifecycle="active")])), project_id="p", datastream_id="d"
    )
    assert running["run_state"] == "running"

    paused = schedule_mcp.read_schedule(
        _Conn(_Cur([_row(enabled=False, lifecycle="active")])), project_id="p", datastream_id="d"
    )
    assert paused["run_state"] == "paused"


def test_a_draft_is_not_reported_as_a_paused_datastream():
    """Two states that look alike on screen and take opposite gestures.

    One is repaired by starting it; the other only by publishing it. Flattening
    them would send a person to a control that cannot help.
    """
    draft = schedule_mcp.read_schedule(
        _Conn(_Cur([_row(enabled=False, lifecycle="draft")])), project_id="p", datastream_id="d"
    )
    assert draft["run_state"] == "not_activated"
    assert draft["lifecycle_state"] == "draft"


def test_an_archived_datastream_is_not_reported_as_merely_stopped():
    """Soft archive sets `enabled = FALSE` and leaves `lifecycle_state` active.

    Read from `enabled` alone, an archived Datastream is indistinguishable from
    one somebody paused this morning -- and starting it would restart a flow
    that was retired.
    """
    archived = schedule_mcp.read_schedule(
        _Conn(_Cur([_row(enabled=False, lifecycle="active", archived_at="2026-08-01T00:00:00Z")])),
        project_id="p", datastream_id="d",
    )
    assert archived["run_state"] == "archived"
    assert archived["archived"] is True


@contextmanager
def _allowance(guard=lambda *_a, **_k: None):
    """Replace the trial allowance guard for the length of one test.

    The real one resolves the org through the connection it is handed, and this
    file's fake cursor answers scripted rows -- so leaving it in place makes the
    guard eat the row the assertion is about, which is what it did.
    """
    import core.trial_enforcement as trial

    original = trial.check_datastream_limit
    trial.check_datastream_limit = guard
    try:
        yield
    finally:
        trial.check_datastream_limit = original


def test_starting_a_datastream_writes_the_column_the_dispatcher_reads():
    cur = _Cur([(1,), (False, "active", None), _row(enabled=True)], rowcount=1)
    with _allowance():
        result = schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled=True
        )
    updates = [(s, p) for s, p in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert len(updates) == 1
    assert "enabled = %s" in updates[0][0]
    assert updates[0][1][0] is True
    assert result["run_state"] == "running"


def test_stopping_a_datastream_touches_nothing_else():
    """The same guard the arrival hour has: a stop must not rewrite the cadence."""
    cur = _Cur([(1,), (True, "active", None), _row(enabled=False)], rowcount=1)
    schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled=False
    )
    updates = [s for s, _ in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert len(updates) == 1
    assert "schedule_mode" not in updates[0]
    assert "date_window_days" not in updates[0]
    assert "arrival_hour_local" not in updates[0]


def test_this_door_cannot_activate_a_draft():
    """`publish_activate_mutation` sets `lifecycle_state` and `enabled` TOGETHER.

    Setting `enabled` alone here would be a second activation authority, and a
    dishonest one: the dispatcher would still skip the row on
    `d.lifecycle_state = 'active'`, so the screen would report an armed clock
    that does not exist.
    """
    cur = _Cur([(1,), (False, "draft", None)])
    with pytest.raises(ValueError) as exc:
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled=True
        )
    assert "never been activated" in str(exc.value)
    assert [s for s, _ in cur.statements if s.startswith("UPDATE")] == []


def test_an_archived_datastream_cannot_be_restarted_from_the_schedule():
    cur = _Cur([(1,), (False, "active", "2026-08-01T00:00:00Z")])
    with pytest.raises(ValueError) as exc:
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled=True
        )
    assert "archived" in str(exc.value)
    assert [s for s, _ in cur.statements if s.startswith("UPDATE")] == []


def test_a_call_that_never_mentions_enabled_does_not_even_ask():
    """The lifecycle read is asked only when the caller mentions starting.

    A call about the cadence keeps the shape -- and the cost -- it had before
    this setting existed.
    """
    cur = _Cur([(1,), _row()], rowcount=1)
    schedule_mcp.set_schedule(
        _Conn(cur), project_id="proj_X", datastream_id="ds_X", cadence="nightly"
    )
    assert not any("lifecycle_state, archived_at" in s for s, _ in cur.statements)
    updates = [s for s, _ in cur.statements if s.startswith("UPDATE app.datastreams SET")]
    assert "enabled" not in updates[0]


def test_a_string_is_not_accepted_where_a_decision_is_expected():
    """`bool("false")` is `True`, and it would arm a Datastream asked to stop."""
    cur = _Cur([(1,)])
    with pytest.raises(ValueError, match="true .* or false"):
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled="false"
        )


def test_starting_a_paused_datastream_spends_the_same_trial_allowance_as_creating_one():
    """The allowance counts `enabled = TRUE` rows, so arming is the same act.

    Without this guard an org at its cap creates three, stops one, arms the
    fourth -- and the rule epic 34 ratified is bypassed by the schedule screen.
    """
    calls = []

    def _guard(project_id, conn, *, identity="system"):
        calls.append((project_id, identity))

    with _allowance(_guard):
        cur = _Cur([(1,), (False, "active", None), _row(enabled=True)], rowcount=1)
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X",
            enabled=True, identity="person_EXAMPLE",
        )
        assert calls == [("proj_X", "person_EXAMPLE")]

        # And re-arming a Datastream that is ALREADY running is not a new one:
        # the count includes it, so the guard would refuse a no-op.
        calls.clear()
        cur = _Cur([(1,), (True, "active", None), _row(enabled=True)], rowcount=1)
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled=True
        )
        assert calls == []


def test_stopping_never_asks_the_allowance():
    """Stopping frees a slot; asking permission to free one would be absurd."""

    def _guard(*_args, **_kwargs):
        raise AssertionError("the allowance was consulted to STOP a Datastream")

    with _allowance(_guard):
        cur = _Cur([(1,), (True, "active", None), _row(enabled=False)], rowcount=1)
        schedule_mcp.set_schedule(
            _Conn(cur), project_id="proj_X", datastream_id="ds_X", enabled=False
        )
