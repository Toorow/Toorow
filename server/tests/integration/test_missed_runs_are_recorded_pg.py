"""A scheduled run that does not happen leaves a record. `Incomplete if` n.2.

WHY THIS FILE EXISTS. `missed_run_count` has been a column since migration 030
(`030_versioned_datastream_intents.sql:92`) and a number on the Workbench
Overview since the Datastream surface shipped. Until this change it had **no
writer at all** -- every occurrence in `server/` was a read, a comment, or a test
saying so. The consequence was not a missing feature but an inverted signal: the
screen showed `0`, and `0` is what a healthy stream shows too.

It was measured, twice, on real deployments:

  * `execution-substrate.md:53` -- a nightly thread that is not alive at its
    minute does nothing "*and nothing records that nothing happened --
    `missed_run_count` stays at 0, which reads like health*";
  * AI-301, 2026-08-12 to 2026-08-17
    (`datastream-workbench-and-wizard.md:2602`) -- EVERY window of the whole
    platform was refused `access_denied` at the enqueue for five days while
    `next_run_at` advanced each night, `missed_run_count` stayed at `0`, and
    nothing was logged above DEBUG.

AI-301 closed half of it: a refusal no longer advances the clock. This closes the
other half: a refusal, and a tick that never fired, now MOVE THE COUNTER. The
rule the two writers share is that **every due occurrence moves exactly one
counter, never neither** -- `next_run_at` when it became a job, or
`missed_run_count` when it did not.

WHAT IT PROVES, in order of value:

  1. the two writers exist and are correct on a real Postgres -- the arithmetic
     of `_advance_next_run` (silence: the ticks a single advance steps over) and
     the observation of `_record_missed_run` (a refusal the dispatcher watched
     itself make);
  2. THE READ<->WRITE LOOP CLOSES: what the dispatcher writes is what
     `datastream_workbench.read_tab` serves to the Overview screen, and what
     flips its `late_reasons` off healthy. A counter written where no reader
     looks would be the same defect wearing the other shoe;
  3. the healthy cases stay at zero -- a punctual tick and a never-armed row
     both add nothing. Without this a "fix" that increments unconditionally
     would look just as green and would make every stream permanently late.

Skipped without TEST_POSTGRES_DSN:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import json
import os

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")

_ORG = "org_missed_test"
_PROJECT = "proj_missed_test"
_DATASTREAM = "ds_missed_test"
_PLAN = "dspv_missed_current"
_MAPPING = "dsmv_missed_current"
_CONNECTION = "conn_missed_test"


def _connection():
    return psycopg.connect(_DSN)


@pytest.fixture()
def conn():
    """The same arming chain `test_schedule_advance_pg.py` documents.

    Every NOT NULL and every CHECK below was measured against this schema rather
    than guessed; the comments on the sibling file explain each one.
    """
    connection = _connection()
    try:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, 'missed', %s, 'test') ON CONFLICT (id) DO NOTHING",
                (_ORG, _ORG),
            )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, 'missed', %s, 'test', %s) ON CONFLICT (id) DO NOTHING",
                (_PROJECT, _PROJECT, _ORG),
            )
            cur.execute(
                "INSERT INTO app.datastreams "
                "  (id, project_id, name, org_id, schedule_mode, source_kind, module_name) "
                "VALUES (%s, %s, 'missed', %s, 'nightly', 'connector_pull', 'gsc') "
                "ON CONFLICT (id) DO NOTHING",
                (_DATASTREAM, _PROJECT, _ORG),
            )
            cur.execute(
                """
                INSERT INTO app.datastream_plan_versions
                    (id, datastream_id, project_id, version_number, contract_version,
                     source_kind, writer_kind, destination_policy, normalized_payload,
                     content_hash, idempotency_key_hash, created_by)
                VALUES (%s, %s, %s, 1, 1, 'connector_pull', 'toorow', 'managed_raw',
                        %s::jsonb, repeat('f', 64), repeat('9', 64), 'test')
                ON CONFLICT (id) DO NOTHING
                """,
                (_PLAN, _DATASTREAM, _PROJECT, json.dumps({})),
            )
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_versions
                    (id, datastream_id, project_id, version_number, mapping_contract_version,
                     source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                     toorow_extension_version, executable, mapping_payload, ossie_projection,
                     idempotency_key_hash, created_by)
                VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('d', 64), '0.1.1',
                        '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('7', 64), 'test')
                ON CONFLICT (id) DO NOTHING
                """,
                (_MAPPING, _DATASTREAM, _PROJECT, _PLAN),
            )
            cur.execute(
                "UPDATE app.datastreams SET current_plan_version_id = %s, "
                "current_mapping_version_id = %s WHERE id = %s",
                (_PLAN, _MAPPING, _DATASTREAM),
            )
            cur.execute(
                "INSERT INTO app.connection_ref "
                "  (id, provider, nango_connection_id, project_id, owner_org_id, "
                "   owner_identity) "
                "VALUES (%s, 'gsc', %s, %s, %s, 'test') ON CONFLICT (id) DO NOTHING",
                (_CONNECTION, _CONNECTION, _PROJECT, _ORG),
            )
            # The Workbench reads through `app.project_flux`, not through
            # `app.datastreams` directly: the base record INNER JOINs it
            # (`datastream_workbench.py:613`), so a datastream absent from the
            # membership table is `WorkbenchNotFound` however well armed it is.
            # Measured, not guessed -- the loop test below failed exactly here.
            cur.execute(
                "INSERT INTO app.project_flux (project_id, flux_id, org_id) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (_PROJECT, _DATASTREAM, _ORG),
            )
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM app.datastream_schedule_state WHERE project_id = %s",
                (_PROJECT,),
            )
            cur.execute(
                "DELETE FROM app.project_preferences WHERE project_id = %s", (_PROJECT,)
            )
            cur.execute(
                "UPDATE app.datastreams SET arrival_hour_local = NULL, "
                "schedule_mode = 'nightly', lifecycle_state = 'active' WHERE id = %s",
                (_DATASTREAM,),
            )
        connection.commit()
        connection.close()


def _arm(cur, overdue_by: str | None) -> None:
    """Put the clock `overdue_by` in the past. `None` means never armed.

    The interval is INTERPOLATED, not bound: `NOW() - interval '3 days'` is an
    expression, and a bound parameter is a value -- Postgres refuses the string.
    Every caller below passes a literal written in this file.
    """
    moment = "NULL" if overdue_by is None else f"NOW() - interval '{overdue_by}'"
    cur.execute(
        f"""
        INSERT INTO app.datastream_schedule_state
            (plan_version_id, datastream_id, project_id, next_run_at)
        VALUES (%s, %s, %s, {moment})
        ON CONFLICT (plan_version_id) DO UPDATE
            SET next_run_at = EXCLUDED.next_run_at, missed_run_count = 0,
                updated_at = NOW()
        """,  # noqa: S608 -- a literal chosen in this file, never input
        (_PLAN, _DATASTREAM, _PROJECT),
    )


def _missed(cur) -> int:
    cur.execute(
        "SELECT missed_run_count FROM app.datastream_schedule_state "
        "WHERE plan_version_id = %s",
        (_PLAN,),
    )
    return int(cur.fetchone()[0])


# ---------------------------------------------------------------------------
# 1. SILENCE -- the ticks a single advance steps over.
# ---------------------------------------------------------------------------


def test_an_advance_that_steps_over_three_nights_counts_three(conn):
    """The container was down for three days. That is three uncollected nights.

    `_advance_next_run` jumps straight to the next due occurrence rather than
    walking forward one period per tick -- its own docstring says the
    intermediate windows "are not fetched, they are dropped". This is the last
    moment anything can see them: after the UPDATE the clock reads as if it had
    always been on time.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _arm(cur, "3 days")
    conn.commit()

    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")

    with conn.cursor() as cur:
        assert _missed(cur) == 3, (
            "three nightly occurrences were stepped over and the counter did not "
            "move -- this is exactly the silence Incomplete if n.2 names"
        )


def test_a_punctual_tick_counts_nothing(conn):
    """A nightly row five minutes late has missed nothing.

    The guard against the easy wrong fix: a writer that increments on every
    advance would be just as green on the test above and would mark every
    healthy stream late for ever.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _arm(cur, "5 minutes")
    conn.commit()

    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")

    with conn.cursor() as cur:
        assert _missed(cur) == 0, (
            "a tick that fired five minutes after its moment was counted as a "
            "missed run -- lateness is not absence"
        )


def test_a_row_that_was_never_armed_counts_nothing(conn):
    """A NULL `next_run_at` has no occurrence to have missed.

    This is the case `test_recurring_retrieval_arming_pg.py` already pins from
    the other side (`assert missed == 0` after a real dispatch): it explicitly
    nulls the clock first, so a naive "how overdue are we" writer would have
    turned that green assertion red for the wrong reason.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        _arm(cur, None)
    conn.commit()

    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "nightly")

    with conn.cursor() as cur:
        assert _missed(cur) == 0, (
            "a datastream whose clock was never set was charged with a missed run"
        )


def test_a_weekly_row_counts_weeks_not_days(conn):
    """The step is the row's OWN cadence (AI-217), on this counter as on the clock.

    Ten days overdue on a weekly cadence is ONE missed occurrence, not ten. A
    counter that divided by a hard-coded day would report seven phantom misses
    per real one on every weekly stream.
    """
    from core.scheduler import _advance_next_run

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET schedule_mode = 'weekly' WHERE id = %s",
            (_DATASTREAM,),
        )
        _arm(cur, "10 days")
    conn.commit()

    _advance_next_run(_connection, _DATASTREAM, _PROJECT, "weekly")

    with conn.cursor() as cur:
        assert _missed(cur) == 1, (
            "ten days overdue on a weekly cadence is one missed week; the counter "
            "divided by the wrong step"
        )


# ---------------------------------------------------------------------------
# 2. OBSERVATION -- a refusal the dispatcher watched itself make.
# ---------------------------------------------------------------------------


def test_a_refused_window_is_recorded_as_missed(conn):
    """AI-301's other half: the refusal stopped the clock, and moved nothing.

    `_record_missed_run` is the counterpart of `_advance_next_run`, and this is
    the case it was written for -- five days of platform-wide `access_denied`
    that left every ledger reading healthy.
    """
    from core.scheduler import _record_missed_run

    with conn.cursor() as cur:
        _arm(cur, "1 hour")
    conn.commit()

    _record_missed_run(_connection, _DATASTREAM, _PROJECT, reason="access_denied")

    with conn.cursor() as cur:
        assert _missed(cur) == 1, (
            "a window refused at the enqueue left no mark -- the exact AI-301 defect"
        )


def test_the_recorder_never_raises_on_an_unknown_datastream(conn):
    """A bookkeeping failure must not take down a loop with streams left to walk.

    The dispatcher calls this inside its per-datastream iteration; an exception
    here would cost every datastream after this one in the same tick.
    """
    from core.scheduler import _record_missed_run

    _record_missed_run(
        _connection, "ds_does_not_exist", _PROJECT, reason="access_denied"
    )


def test_the_recorder_only_touches_the_current_plan_version(conn):
    """The table is keyed by plan version; a datastream carries a row per version.

    The same scoping the advance and the dispatch SELECT already use. Charging a
    superseded version's row would move a number no screen reads while leaving
    the one it does read at zero.
    """
    from core.scheduler import _record_missed_run

    stale_plan = "dspv_missed_stale"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 2, 1, 'connector_pull', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('e', 64), repeat('8', 64), 'test')
            ON CONFLICT (id) DO NOTHING
            """,
            (stale_plan, _DATASTREAM, _PROJECT),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_schedule_state
                (plan_version_id, datastream_id, project_id, next_run_at)
            VALUES (%s, %s, %s, NOW() - interval '1 hour')
            ON CONFLICT (plan_version_id) DO UPDATE SET missed_run_count = 0
            """,
            (stale_plan, _DATASTREAM, _PROJECT),
        )
        _arm(cur, "1 hour")
    conn.commit()

    _record_missed_run(_connection, _DATASTREAM, _PROJECT, reason="access_denied")

    with conn.cursor() as cur:
        assert _missed(cur) == 1
        cur.execute(
            "SELECT missed_run_count FROM app.datastream_schedule_state "
            "WHERE plan_version_id = %s",
            (stale_plan,),
        )
        assert int(cur.fetchone()[0]) == 0, (
            "a superseded plan version's schedule row was charged with the miss"
        )
    # Only the schedule row is cleaned up: `app.datastream_plan_versions` carries
    # an immutability trigger that refuses DELETE outright ("datastream plan
    # versions are immutable"), which is the correct schema and one more thing a
    # mock would have let through. The fixture's `ON CONFLICT DO NOTHING` makes
    # the leftover version harmless on a rerun.
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.datastream_schedule_state WHERE plan_version_id = %s",
            (stale_plan,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# 3. THE LOOP -- the screen reads what the dispatcher wrote.
# ---------------------------------------------------------------------------


def test_the_workbench_serves_the_miss_the_dispatcher_recorded(conn):
    """THE POINT OF THE WHOLE FILE: writer and reader meet on the same row.

    `read_tab(tab="overview")` is what the Workbench Overview calls, and
    `missed_run_count` is the number it prints under "Missed runs". A counter
    written where no reader looks would be the same defect as a reader with no
    writer -- this asserts the two are the same row, through the real read path
    rather than a second hand-written SELECT.
    """
    from core.datastream_workbench import read_tab
    from core.scheduler import _record_missed_run

    with conn.cursor() as cur:
        _arm(cur, "1 hour")
    conn.commit()

    _record_missed_run(_connection, _DATASTREAM, _PROJECT, reason="access_denied")

    payload = read_tab(
        conn, project_id=_PROJECT, datastream_id=_DATASTREAM, tab="overview"
    )
    # The tab answers `{schema, tab, project_id, datastream_id, evidence}` and the
    # schedule lives under `evidence` -- asserted through the real envelope rather
    # than a flattened guess, because the envelope is what the screen receives.
    schedule = (payload.get("evidence") or {}).get("schedule") or {}
    assert schedule.get("missed_run_count") == 1, (
        "the Overview tab does not serve the miss the dispatcher recorded: "
        f"{schedule!r}"
    )
