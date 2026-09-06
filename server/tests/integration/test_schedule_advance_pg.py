"""The schedule advance, executed rather than read. AI-117 / AI-119.

WHY THIS FILE REPLACES TWO ASSERTIONS. The unit tests for this behaviour assert
on the SOURCE -- `assert "AT TIME ZONE" in advance` -- which proves the string is
present and nothing about what it does. Two claims deserve better than that:

  * a project's `reporting_timezone` decides the advance, not a deployment
    constant;
  * the advance PRESERVES THE LOCAL HOUR across a daylight-saving change. This is
    the whole reason for the `AT TIME ZONE` round trip: `+ interval '1 day'` on a
    UTC session is 24 hours, and 24 hours across a DST boundary moves 02:00 to
    01:00 or 03:00 -- where it stays. Only PostgreSQL, with a real timezone
    database, can be asked whether the round trip fixes it.

And one structural claim: the advance touches ONLY the row of the datastream's
CURRENT plan version, because the table is keyed by plan version and carries one
row per version.

Skipped without TEST_POSTGRES_DSN:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import json
import os
from datetime import date

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")

_ORG = "org_advance_test"
_PROJECT = "proj_advance_test"
_DATASTREAM = "ds_advance_test"
_PLAN_CURRENT = "dspv_advance_current"
_PLAN_OLD = "dspv_advance_old"
#: `_dispatch_nightly_datastreams` joins the CURRENT mapping version and refuses
#: a row without one (AI-217): the dispatch suite below needs a real one, and a
#: mapping version is refused without the plan version it projects.
_MAPPING_CURRENT = "dsmv_advance_current"
#: `app.pull_jobs.connection_ref_id` is NOT NULL and references
#: `app.connection_ref`: the catch-up sweep reads a pull outcome, so this suite
#: needs a real job row, which needs a real connection to hang off.
_CONNECTION = "conn_advance_test"
_HASH = "b" * 64
#: Europe/Paris leaves summer time on 2025-10-26 at 03:00 local. The instant has
#: to be in the PAST -- the advance only moves what is due -- and LATE ENOUGH in
#: the day that adding a fixed 24 hours lands after the change. Measured against
#: this database rather than reasoned about: 02:00 the day before does NOT drift
#: (the switch happens later that morning), 04:00 does. Choosing the first would
#: have produced a green test that proves nothing.
_BEFORE_DST = "2025-10-25 04:00:00+02"


def _connection():
    return psycopg.connect(_DSN)


@pytest.fixture()
def conn():
    connection = _connection()
    try:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, 'advance', %s, 'test') ON CONFLICT (id) DO NOTHING",
                (_ORG, _ORG),
            )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, 'advance', %s, 'test', %s) ON CONFLICT (id) DO NOTHING",
                (_PROJECT, _PROJECT, _ORG),
            )
            # `source_kind` is NOT optional in practice, whatever its nullability
            # says: a BEFORE INSERT trigger derives `external_dispatch_excluded`
            # from it (`NEW.source_kind = 'external_bq'`), and NULL propagates
            # into a NOT NULL column. The column's own DEFAULT never applies,
            # because the trigger assigns before the default would.
            cur.execute(
                "INSERT INTO app.datastreams "
                # And `ck_datastreams_source_kind` binds the two together:
                # 'connector_pull' REQUIRES a module_name. The schema refuses a
                # datastream that pulls from no connector -- which is the right
                # refusal, and one more thing a mock would have accepted.
                "  (id, project_id, name, org_id, schedule_mode, source_kind, module_name) "
                "VALUES (%s, %s, 'advance', %s, 'nightly', 'connector_pull', 'gsc') "
                "ON CONFLICT (id) DO NOTHING",
                (_DATASTREAM, _PROJECT, _ORG),
            )
            # Each plan version needs its OWN idempotency hash:
            # `uq_datastream_plan_idempotency` refuses two versions claiming the
            # same key -- which is the point of an idempotency key, and a rule a
            # mock has no way to express.
            for plan_id, number, key_hash in (
                (_PLAN_OLD, 1, "b" * 64), (_PLAN_CURRENT, 2, "c" * 64)
            ):
                cur.execute(
                    """
                    INSERT INTO app.datastream_plan_versions
                        (id, datastream_id, project_id, version_number, contract_version,
                         source_kind, writer_kind, destination_policy, normalized_payload,
                         content_hash, idempotency_key_hash, created_by)
                    VALUES (%s, %s, %s, %s, 1, 'connector_pull', 'toorow', 'managed_raw',
                            %s::jsonb, %s, %s, 'test')
                    ON CONFLICT (id) DO NOTHING
                    """,
                    (plan_id, _DATASTREAM, _PROJECT, number, json.dumps({}),
                     _HASH, key_hash),
                )
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_versions
                    (id, datastream_id, project_id, version_number, mapping_contract_version,
                     source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                     toorow_extension_version, executable, mapping_payload, ossie_projection,
                     idempotency_key_hash, created_by)
                VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('d', 64), '0.1.1',
                        '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('e', 64), 'test')
                ON CONFLICT (id) DO NOTHING
                """,
                (_MAPPING_CURRENT, _DATASTREAM, _PROJECT, _PLAN_CURRENT),
            )
            cur.execute(
                "UPDATE app.datastreams SET current_plan_version_id = %s, "
                "current_mapping_version_id = %s WHERE id = %s",
                (_PLAN_CURRENT, _MAPPING_CURRENT, _DATASTREAM),
            )
            # `owner_org_id` and `owner_identity` are both NOT NULL: an
            # authorization belongs to an organization AND to the person who
            # granted it. Measured against this schema, not guessed -- an insert
            # without either is refused.
            cur.execute(
                "INSERT INTO app.connection_ref "
                "  (id, provider, nango_connection_id, project_id, owner_org_id, "
                "   owner_identity) "
                "VALUES (%s, 'gsc', %s, %s, %s, 'test') ON CONFLICT (id) DO NOTHING",
                (_CONNECTION, _CONNECTION, _PROJECT, _ORG),
            )
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cur:
            cur.execute("DELETE FROM app.pull_jobs WHERE datastream_id = %s", (_DATASTREAM,))
            cur.execute(
                "DELETE FROM app.datastream_schedule_state WHERE project_id = %s", (_PROJECT,)
            )
            cur.execute("DELETE FROM app.project_preferences WHERE project_id = %s", (_PROJECT,))
            # The cadence and the window are reset with the arrival hour: the
            # datastream row survives the suite (`ON CONFLICT DO NOTHING`), so a
            # test that sets `weekly` would otherwise hand the next test a
            # cadence it never asked for.
            cur.execute(
                "UPDATE app.datastreams SET arrival_hour_local = NULL, "
                "schedule_mode = 'nightly', lifecycle_state = 'active', "
                "date_window_days = 30, connection_ref_id = NULL WHERE id = %s",
                (_DATASTREAM,),
            )
        connection.commit()
        connection.close()


def _set_schedule_row(cur, plan_id: str, next_run_at: str, *, retry_count: int = 0) -> None:
    cur.execute(
        """
        INSERT INTO app.datastream_schedule_state
            (plan_version_id, datastream_id, project_id, next_run_at, retry_count)
        VALUES (%s, %s, %s, %s::timestamptz, %s)
        ON CONFLICT (plan_version_id) DO UPDATE
            SET next_run_at = EXCLUDED.next_run_at, retry_count = EXCLUDED.retry_count,
                updated_at = NOW()
        """,
        (plan_id, _DATASTREAM, _PROJECT, next_run_at, retry_count),
    )


def _set_arrival_hour(cur, hour: int | None) -> None:
    """Story 57.8: the hour the advance now reads instead of inheriting.

    Set explicitly in every test below rather than left to the previous value's
    time of day. That is the whole change A1 makes: the hour is a VALUE, so a
    test that means "this row runs at 04:00" has to say 04:00 somewhere.
    """
    cur.execute(
        "UPDATE app.datastreams SET arrival_hour_local = %s WHERE id = %s",
        (hour, _DATASTREAM),
    )


def _set_project_timezone(cur, timezone_name: str) -> None:
    cur.execute(
        "INSERT INTO app.project_preferences (project_id, reporting_timezone) "
        "VALUES (%s, %s) "
        "ON CONFLICT (project_id) DO UPDATE SET reporting_timezone = EXCLUDED.reporting_timezone",
        (_PROJECT, timezone_name),
    )


def _local_hour(cur, plan_id: str, timezone_name: str) -> str:
    cur.execute(
        "SELECT to_char(next_run_at AT TIME ZONE %s, 'YYYY-MM-DD HH24:MI') "
        "FROM app.datastream_schedule_state WHERE plan_version_id = %s",
        (timezone_name, plan_id),
    )
    return cur.fetchone()[0]


def test_the_project_preference_is_what_the_advance_reads(conn):
    from core.scheduler import project_timezone

    with conn.cursor() as cur:
        _set_project_timezone(cur, "America/New_York")
    conn.commit()

    assert project_timezone(conn, _PROJECT) == "America/New_York"


def test_the_advance_keeps_the_local_hour_across_a_dst_change(conn):
    """02:00 stays 02:00 the night the clocks change.

    Adding 24 hours would land on 01:00 -- and every night after that, because
    the drift is never corrected. A schedule a person chose would silently walk
    away from the hour they chose.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 4)
        _set_schedule_row(cur, _PLAN_CURRENT, _BEFORE_DST)
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2025-10-25 04:30:00+02"
    )

    with conn.cursor() as cur:
        advanced = _local_hour(cur, _PLAN_CURRENT, "Europe/Paris")
    assert advanced == "2025-10-26 04:00", (
        f"the advance landed on {advanced}: the local hour drifted across the DST "
        "boundary, which is exactly what the AT TIME ZONE round trip exists to stop"
    )


def test_a_naive_24_hour_addition_would_have_drifted(conn):
    """The control that gives the test above its meaning.

    Without it, a green result could mean the DST boundary was never crossed.
    This asserts that the naive arithmetic really does move the local hour, so
    the previous test is measuring a difference rather than a coincidence.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SET TIME ZONE 'UTC'"
        )
        cur.execute(
            "SELECT to_char((%s::timestamptz + interval '1 day') AT TIME ZONE "
            "'Europe/Paris', 'YYYY-MM-DD HH24:MI')",
            (_BEFORE_DST,),
        )
        naive = cur.fetchone()[0]
    conn.rollback()
    assert naive == "2025-10-26 03:00", f"expected the naive drift, measured {naive}"


def test_the_advance_touches_only_the_current_plan_version(conn):
    """The table is keyed by plan version: a datastream carries one row per version.

    Advancing them all would move schedules belonging to versions the dispatcher
    no longer looks at.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 2)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-07-01 02:00:00+02")
        _set_schedule_row(cur, _PLAN_OLD, "2026-07-01 02:00:00+02")
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2026-07-01 02:00:30+02"
    )

    with conn.cursor() as cur:
        current = _local_hour(cur, _PLAN_CURRENT, "Europe/Paris")
        old = _local_hour(cur, _PLAN_OLD, "Europe/Paris")
    assert current == "2026-07-02 02:00"
    assert old == "2026-07-01 02:00", "a superseded plan version's schedule was moved"


def test_a_future_run_is_not_brought_forward(conn):
    """The advance only moves what is DUE.

    Its WHERE clause requires `next_run_at <= now()`. Without that, a sweep every
    ten minutes would keep pushing a schedule that has not fired yet further into
    the future -- a datastream that never runs, and nothing saying why.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 2)
        cur.execute(
            """
            INSERT INTO app.datastream_schedule_state
                (plan_version_id, datastream_id, project_id, next_run_at)
            VALUES (%s, %s, %s, now() + interval '3 days')
            ON CONFLICT (plan_version_id) DO UPDATE SET next_run_at = EXCLUDED.next_run_at
            """,
            (_PLAN_CURRENT, _DATASTREAM, _PROJECT),
        )
        cur.execute(
            "SELECT next_run_at FROM app.datastream_schedule_state WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
        before = cur.fetchone()[0]
    conn.commit()

    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT next_run_at FROM app.datastream_schedule_state WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
        after = cur.fetchone()[0]
    assert after == before, "a run that was not due yet was pushed further away"


# ---------------------------------------------------------------------------
# Story 57.8 -- the arrival hour is a VALUE, and it survives a catch-up.
# ---------------------------------------------------------------------------


def _retry_count(cur) -> int:
    cur.execute(
        "SELECT retry_count FROM app.datastream_schedule_state WHERE plan_version_id = %s",
        (_PLAN_CURRENT,),
    )
    return cur.fetchone()[0]


def test_the_arrival_hour_survives_a_week_of_advances(conn):
    """Seven advances, one hour: 06:00 every day, never 06:00 plus drift.

    The advance used to add a period to the previous value. That is stable while
    nothing else writes the row -- and stops being stable the first time
    something does, which is what the next test measures.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-01 06:00:00+02")
    conn.commit()

    seen = []
    for _ in range(7):
        _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")
        with conn.cursor() as cur:
            seen.append(_local_hour(cur, _PLAN_CURRENT, "Europe/Paris"))
            # Put the row back in the past so the next advance sees it as due;
            # the DATE moves, the hour is what is being measured.
            cur.execute(
                "UPDATE app.datastream_schedule_state "
                "SET next_run_at = next_run_at - interval '7 days' "
                "WHERE plan_version_id = %s",
                (_PLAN_CURRENT,),
            )
        conn.commit()

    assert [value[-5:] for value in seen] == ["06:00"] * 7, seen


def test_a_catch_up_hands_the_schedule_back_to_the_next_arrival_hour(conn):
    """The measurement that decided A1, run rather than reasoned about.

    A catch-up writes an off-hour instant into `next_run_at`. When the advance
    added a period to the PREVIOUS value, the next advance inherited that
    off-hour instant and every following day ran an hour late -- for ever, from
    one failure. Reading the hour from `arrival_hour_local` instead, the
    catch-up's own advance lands back on 06:00.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        # 07:13 on 2026-06-01: what a catch-up armed at 06:13 leaves behind.
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-01 07:13:00+02", retry_count=1)
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2026-06-01 07:13:30+02"
    )

    with conn.cursor() as cur:
        advanced = _local_hour(cur, _PLAN_CURRENT, "Europe/Paris")
        after_catch_up = _retry_count(cur)
    assert advanced == "2026-06-02 06:00", (
        f"the advance landed on {advanced}: the catch-up's off-hour instant became "
        "the new anchor, which is the drift arbitrage A1 exists to stop"
    )
    assert after_catch_up == 1, (
        "the catch-up's own dispatch re-armed the allowance -- one extra attempt "
        "would have become an hourly loop (A4)"
    )


def test_a_catch_up_late_in_the_evening_does_not_skip_tomorrow(conn):
    """23:40 + one hour is tomorrow, and tomorrow's arrival hour is still due.

    Anchoring on "the day of the previous value, plus one" would land on the day
    AFTER tomorrow here, silently skipping a whole pull -- the one edge a naive
    recomputation gets wrong.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-02 00:40:00+02", retry_count=1)
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2026-06-02 00:40:30+02"
    )

    with conn.cursor() as cur:
        advanced = _local_hour(cur, _PLAN_CURRENT, "Europe/Paris")
    assert advanced == "2026-06-02 06:00", f"a whole day was skipped: {advanced}"


def test_the_advance_never_touches_the_allowance(conn):
    """A4's bound is the counter, and the advance is not allowed near it.

    This row is dispatched AT its arrival hour, which is the case an earlier
    version treated as "a fresh period, re-arm". The assertion is inverted on
    purpose: clearing the counter anywhere but on a success re-opens the hourly
    loop, because the condition that version used
    (`arrival_local = previous_local`) is trivially true for every row whose
    arrival hour is NULL.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-02 06:00:00+02", retry_count=1)
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2026-06-02 06:00:30+02"
    )

    with conn.cursor() as cur:
        assert _local_hour(cur, _PLAN_CURRENT, "Europe/Paris") == "2026-06-03 06:00"
        assert _retry_count(cur) == 1, (
            "the advance cleared the allowance -- only a successful pull may"
        )


def test_a_row_far_behind_jumps_to_its_next_due_run_in_one_advance(conn):
    """Converging one period per tick was not a catch-up. Measured, not assumed.

    The intermediate windows are never fetched: every one of those dispatches
    computes the SAME window, this project's yesterday. And the queue's dedup
    index does not absorb them — it is PARTIAL
    (`022_pull_jobs_dedup_index.sql`: `WHERE state IN ('queued','running')`), so
    once a window has finished the identical window is enqueued again. A row ten
    days behind meant eleven advances, and with the frequent tick reading nightly
    rows that is eleven pulls of one window in eleven hours.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-01 06:00:00+02")
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2026-06-11 09:00:00+02"
    )

    with conn.cursor() as cur:
        advanced = _local_hour(cur, _PLAN_CURRENT, "Europe/Paris")
        cur.execute(
            "SELECT next_run_at > %s::timestamptz FROM app.datastream_schedule_state "
            "WHERE plan_version_id = %s",
            ("2026-06-11 09:00:00+02", _PLAN_CURRENT),
        )
        in_the_future = cur.fetchone()[0]
    assert advanced == "2026-06-12 06:00", (
        f"one advance landed on {advanced}: ten days behind still means ten more "
        "ticks, each enqueueing the same window again"
    )
    assert in_the_future is True, "the advance left the row due, so the next tick re-dispatches it"


def test_a_row_that_names_no_arrival_hour_keeps_the_behaviour_it_had(conn):
    """NULL is not 0. A row nobody gave an hour advances exactly as before.

    Migration 217 backfills every anchored row, so this is the shape of a row
    activated after it -- and the guarantee that adding the column moved nothing
    that was already running.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, None)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-01 03:17:00+02")
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "nightly", now_override="2026-06-01 03:17:30+02"
    )

    with conn.cursor() as cur:
        assert _local_hour(cur, _PLAN_CURRENT, "Europe/Paris") == "2026-06-02 03:17"


def test_a_weekly_row_advances_by_a_week_not_by_a_day(conn):
    """`weekly` became a legal cadence with migration 204.

    `_advance_next_run` mapped everything that was not `hourly` to one day, so a
    weekly row would have been re-dispatched every night -- seven times the
    provider quota its cadence asks for.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-01 06:00:00+02")
    conn.commit()

    _advance_next_run(
        _connection, _DATASTREAM, _PROJECT, "weekly", now_override="2026-06-01 06:00:30+02"
    )

    with conn.cursor() as cur:
        assert _local_hour(cur, _PLAN_CURRENT, "Europe/Paris") == "2026-06-08 06:00"


def test_the_frequent_tick_cannot_dispatch_the_same_due_row_twice(conn):
    """A2's guarantee, executed: two clocks, one dispatch.

    `dispatch_hourly` now evaluates due nightly rows and `dispatch-nightly`
    remains as the net, so the same row is read by two schedules. What makes the
    second read harmless is that the advance moves the row out of the due set
    before anyone can read it again. The predicate below is the dispatcher's own
    (`_dispatch_nightly_datastreams`), asserted to select the row before the
    advance and nothing after it.

    WHAT THIS DOES NOT CLAIM, measured while writing it: a row overdue by MORE
    than one period stays due after one advance, because the advance moves by one
    period. It converges one period per tick, every dispatch computes the same
    window, and `enqueue_pull`'s dedup index is what covers that interval. The
    guarantee here is the one the two clocks actually need -- a row that became
    due since the last tick is not readable twice.
    """
    from core.scheduler import _advance_next_run

    due = (
        "SELECT count(*) FROM app.datastream_schedule_state ss "
        "JOIN app.datastreams d ON d.id = ss.datastream_id "
        " AND d.project_id = ss.project_id "
        " AND ss.plan_version_id = d.current_plan_version_id "
        "WHERE d.id = %s AND (ss.next_run_at IS NULL OR ss.next_run_at <= NOW())"
    )

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        # The arrival hour is the hour that is running right now, and the row is
        # due as of the top of it: exactly the state a tick finds.
        cur.execute("SELECT EXTRACT(HOUR FROM (NOW() AT TIME ZONE 'Europe/Paris'))::int")
        _set_arrival_hour(cur, cur.fetchone()[0])
        cur.execute(
            """
            INSERT INTO app.datastream_schedule_state
                (plan_version_id, datastream_id, project_id, next_run_at)
            VALUES (%s, %s, %s, date_trunc('hour', NOW()))
            ON CONFLICT (plan_version_id) DO UPDATE
                SET next_run_at = EXCLUDED.next_run_at, retry_count = 0
            """,
            (_PLAN_CURRENT, _DATASTREAM, _PROJECT),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(due, (_DATASTREAM,))
        assert cur.fetchone()[0] == 1, "the fixture is not due -- the test proves nothing"

    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")

    with conn.cursor() as cur:
        cur.execute(due, (_DATASTREAM,))
        assert cur.fetchone()[0] == 0, (
            "the row is still due after being dispatched: a second tick would "
            "enqueue the same window again"
        )


class _RecordingQueue:
    """A queue that records instead of enqueueing. AI-217.

    The dispatcher is run against the real database, but a pull job is a
    provider call waiting to happen: what this suite measures is WHICH rows the
    dispatch selects and what window it computes for them, and writing rows into
    `app.pull_jobs` would prove neither.
    """

    def __init__(self):
        self.calls = []

    def enqueue_pull(self, connection_ref_id, date_from, date_to, **kwargs):
        self.calls.append((connection_ref_id, date_from, date_to, kwargs))
        return {"job_id": f"job_{len(self.calls)}", "state": "queued"}


def _make_weekly_and_due(cur, *, window_days: int = 30) -> None:
    # The arrival hour is the hour running RIGHT NOW, so the advance always adds
    # a full week. Any fixed hour makes the test depend on the wall clock: an
    # arrival hour still ahead of now today is re-anchored to today (the advance
    # lands in the future in ONE step), and the same test would prove seven days
    # in the afternoon and one morning in the small hours.
    cur.execute("SELECT EXTRACT(HOUR FROM (NOW() AT TIME ZONE 'Europe/Paris'))::int")
    arrival_hour = cur.fetchone()[0]
    # The connection is what the dispatch enqueues AGAINST: a datastream that
    # names none is skipped before any window is computed, and the suite above
    # never needed the link because the advance does not read it.
    cur.execute(
        "UPDATE app.datastreams SET schedule_mode = 'weekly', enabled = TRUE, "
        "lifecycle_state = 'active', arrival_hour_local = %s, date_window_days = %s, "
        "connection_ref_id = %s WHERE id = %s",
        (arrival_hour, window_days, _CONNECTION, _DATASTREAM),
    )
    cur.execute(
        "UPDATE app.connection_ref SET status = 'active', enabled = TRUE WHERE id = %s",
        (_CONNECTION,),
    )
    _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-01 06:00:00+02")


def test_a_due_weekly_row_is_dispatched_and_is_not_dispatched_again(conn):
    """AI-217, the whole defect in one round trip.

    Migration 204 made `weekly` a legal cadence, `schedule_mcp.CADENCES` accepted
    it, the Workbench offered it and story 57.8 taught the advance and the
    catch-up sweep to speak it -- and NO dispatcher selected it. A person could
    choose a weekly cadence, save it, see it displayed, and the collection would
    never fire.

    The second dispatch is the other half: the frequent tick reads this row every
    hour, so the advance must move it out of the due set the moment it is
    enqueued. Run against the real ledger rather than a mock, because both halves
    are decided by one SQL predicate the mock would have answered for.
    """
    from core.scheduler import _dispatch_nightly_datastreams

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _make_weekly_and_due(cur)
    conn.commit()

    queue = _RecordingQueue()
    _dispatch_nightly_datastreams(None, "scheduler", queue, _connection)
    assert len(queue.calls) == 1, (
        "a due weekly Datastream was not dispatched -- the cadence is offered, "
        "saved and displayed, and the collection never fires"
    )
    assert queue.calls[0][3]["datastream_id"] == _DATASTREAM

    _dispatch_nightly_datastreams(None, "scheduler", queue, _connection)
    assert len(queue.calls) == 1, (
        "the same weekly row was dispatched twice -- the advance did not move it "
        "out of the due set and the next tick spends the quota again"
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT next_run_at > NOW() + interval '6 days' "
            "  FROM app.datastream_schedule_state WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
        assert cur.fetchone()[0] is True, (
            "the dispatched weekly row advanced by less than a week -- it will be "
            "re-dispatched tomorrow, at seven times its cadence"
        )


def test_a_weekly_run_fetches_the_week_it_covers(conn):
    """A window narrower than the gap between two runs drops the days in between.

    `date_window_days` is chosen independently of the cadence and the seam
    accepts `1`. One day fetched once a week means six days out of seven are
    never asked for -- and nothing reports a gap, because every run succeeds.
    """
    from core.scheduler import _dispatch_nightly_datastreams

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _make_weekly_and_due(cur, window_days=1)
    conn.commit()

    queue = _RecordingQueue()
    _dispatch_nightly_datastreams(None, "scheduler", queue, _connection)

    assert len(queue.calls) == 1
    _conn_id, date_from, date_to, _kwargs = queue.calls[0]
    span = (date.fromisoformat(date_to) - date.fromisoformat(date_from)).days
    assert span == 6, (
        f"a weekly run fetched {date_from}..{date_to} -- the six days between two "
        "runs are dropped and every run still reports success"
    )


def test_a_failed_pull_arms_exactly_one_catch_up(conn):
    """`retry_count` gets its first production writer. Story 57.8.

    Migration 030 declared the column in 2026 and one read existed against it;
    no code ever incremented it. The sweep is run twice here on purpose: the
    second call must find nothing to do, or "the next hour" would be an hourly
    loop against a source that is down.
    """
    from core.scheduler import _reschedule_failed_pulls

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        cur.execute(
            "UPDATE app.datastreams SET enabled = TRUE, lifecycle_state = 'active', "
            "schedule_mode = 'nightly' WHERE id = %s",
            (_DATASTREAM,),
        )
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-02 06:00:00+02")
        # The dispatch happens BEFORE the pull finishes: `_advance_next_run`
        # stamps `updated_at` at enqueue, and the job completes minutes later.
        # Leaving both at the transaction's `NOW()` would make them equal and
        # the sweep's freshness guard would read the failure as already handled.
        cur.execute(
            "UPDATE app.datastream_schedule_state SET updated_at = NOW() - interval "
            "'10 minutes' WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, datastream_id, date_from, date_to,
                 state, requested_by, completed_at)
            VALUES (%s, %s, %s, %s, '2026-06-01', '2026-06-01', 'failed', 'test', NOW())
            ON CONFLICT (id) DO UPDATE SET state = 'failed', completed_at = NOW()
            """,
            ("job_advance_failed", "pull_advance_failed", _CONNECTION, _DATASTREAM),
        )
    conn.commit()

    assert _reschedule_failed_pulls(_connection) == 1
    with conn.cursor() as cur:
        assert _retry_count(cur) == 1
        cur.execute(
            "SELECT next_run_at <= NOW() + interval '61 minutes' "
            "   AND next_run_at > NOW() + interval '59 minutes' "
            "  FROM app.datastream_schedule_state WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
        assert cur.fetchone()[0] is True, "the catch-up did not land an hour from now"

    assert _reschedule_failed_pulls(_connection) == 0, (
        "the same failure armed a second catch-up -- one extra attempt (A4) has "
        "become an hourly loop"
    )


def _activate_for_sweep(cur, *, arrival_hour: int | None, next_run_at: str) -> None:
    cur.execute(
        "UPDATE app.datastreams SET enabled = TRUE, lifecycle_state = 'active', "
        "schedule_mode = 'nightly', arrival_hour_local = %s WHERE id = %s",
        (arrival_hour, _DATASTREAM),
    )
    _set_schedule_row(cur, _PLAN_CURRENT, next_run_at)
    # A dispatch stamps `updated_at` at enqueue and the job completes minutes
    # later. Leaving both at one transaction's `NOW()` would make them equal and
    # the sweep's freshness guard would read the failure as already handled.
    cur.execute(
        "UPDATE app.datastream_schedule_state SET updated_at = NOW() - interval "
        "'10 minutes' WHERE plan_version_id = %s",
        (_PLAN_CURRENT,),
    )


def _record_failed_pull(cur, job_id: str) -> None:
    cur.execute(
        """
        INSERT INTO app.pull_jobs
            (id, pull_id, connection_ref_id, datastream_id, date_from, date_to,
             state, requested_by, completed_at)
        VALUES (%s, %s, %s, %s, '2026-06-01', '2026-06-01', 'failed', 'test', NOW())
        ON CONFLICT (id) DO UPDATE SET state = 'failed', completed_at = NOW()
        """,
        (job_id, f"pull_{job_id}", _CONNECTION, _DATASTREAM),
    )


def test_a_line_without_an_arrival_hour_is_bounded_like_every_other(conn):
    """A4 must hold for the MAJORITY class, not only for backfilled rows.

    Migration 217 backfills the rows that were already anchored; every row
    activated without an arrival hour still carries NULL, and that is the common
    case. The allowance must therefore be bounded by something structural — the
    counter itself — and never by a comparison of two local hours, which is
    trivially true for a row that names no hour and would re-arm the catch-up
    every single tick, for ever, against a source that is down.

    The sequence below is one full catch-up cycle: the scheduled pull fails, the
    catch-up is armed, the catch-up is dispatched, and the catch-up fails too.
    The second sweep must arm NOTHING.
    """
    from core.scheduler import _advance_next_run, _reschedule_failed_pulls

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _activate_for_sweep(cur, arrival_hour=None, next_run_at="2026-06-02 00:00:00+02")
        _record_failed_pull(cur, "job_null_hour_first")
    conn.commit()

    assert _reschedule_failed_pulls(_connection) == 1
    with conn.cursor() as cur:
        assert _retry_count(cur) == 1

    # An hour passes and the catch-up becomes due. Modelled by bringing the
    # instant the sweep wrote back to now: without this the advance selects
    # nothing at all (`next_run_at <= NOW()`) and the test would pass while
    # exercising none of the path it claims to.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_schedule_state SET next_run_at = NOW() "
            "WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
    conn.commit()

    # The tick dispatches the catch-up, and the advance runs with it.
    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")
    with conn.cursor() as cur:
        assert _retry_count(cur) == 1, (
            "the catch-up's own dispatch cleared the allowance -- the next failure "
            "will arm another catch-up, and the next one after that, hourly"
        )
        # The catch-up fails a few minutes after it was dispatched.
        cur.execute(
            "UPDATE app.datastream_schedule_state SET updated_at = updated_at - "
            "interval '5 minutes' WHERE plan_version_id = %s",
            (_PLAN_CURRENT,),
        )
        _record_failed_pull(cur, "job_null_hour_second")
    conn.commit()

    assert _reschedule_failed_pulls(_connection) == 0, (
        "a second catch-up was armed for a row with no arrival hour -- one extra "
        "attempt (A4) is an hourly loop for every row migration 217 did not backfill"
    )


def test_a_success_clears_the_catch_up_counter(conn):
    from core.scheduler import _reschedule_failed_pulls

    with conn.cursor() as cur:
        _set_project_timezone(cur, "Europe/Paris")
        _set_arrival_hour(cur, 6)
        cur.execute(
            "UPDATE app.datastreams SET enabled = TRUE, lifecycle_state = 'active', "
            "schedule_mode = 'nightly' WHERE id = %s",
            (_DATASTREAM,),
        )
        _set_schedule_row(cur, _PLAN_CURRENT, "2026-06-02 06:00:00+02", retry_count=1)
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, datastream_id, date_from, date_to,
                 state, requested_by, completed_at)
            VALUES (%s, %s, %s, %s, '2026-06-01', '2026-06-01', 'done', 'test', NOW())
            ON CONFLICT (id) DO UPDATE SET state = 'done', completed_at = NOW()
            """,
            ("job_advance_done", "pull_advance_done", _CONNECTION, _DATASTREAM),
        )
    conn.commit()

    _reschedule_failed_pulls(_connection)

    with conn.cursor() as cur:
        assert _retry_count(cur) == 0
