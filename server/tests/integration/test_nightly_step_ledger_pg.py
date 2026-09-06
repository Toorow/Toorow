"""A nightly STEP that does not run leaves a record. `Incomplete if` 2, locus 3.

WHY THIS FILE EXISTS. Two of the three loci of *"a scheduled run that does not
happen leaves no record that it did not happen"* were already closed: a stepped-
over Datastream occurrence is charged (`test_missed_runs_are_recorded_pg.py`),
and a platform clock that did not fire is observed
(`app.platform_clocks.observed_last_attempt_status`). The third was inside the
run. `scheduler._run_isolated_step` deliberately never re-raises, so a step that
silently never ran -- an exception before its call site, an early return, a name
added to the sequence whose call site is dead, a container killed mid-step --
left NOTHING behind: no row, no firing, and a log stream that is only ever
evidence of what did happen.

WHAT IT PROVES, in order of value:

  1. THE THREE STATES ARE DISTINCT ON A REAL POSTGRES, and each is produced by
     the code path it claims: a green step closes `succeeded`; a step that
     RAISES closes `failed` and names the exception CLASS; a step that kills the
     process -- a `BaseException` `_run_isolated_step` does not catch -- leaves
     the row OPEN, which is the record that it did not finish. There is
     deliberately no `finally` in `_run_isolated_step` that would close it
     politely on the way out, and this file is what stops one being added.
  2. THE STEP THAT NEVER BEGAN IS A ROW. The whole declared sequence is written
     at dispatch, so a call site that never executes leaves `started_at IS NULL`
     rather than an absence nobody can query.
  3. THE READ<->WRITE LOOP CLOSES: what the scheduler writes is what
     `platform_clocks.list_nightly_step_runs` serves to the Platform Clocks
     screen, with the states derived rather than re-invented. A row written
     where no reader looks is the same defect wearing the other shoe.
  4. THE GUARD HOLDS: the row cannot be deleted, a closed row cannot be
     rewritten, and a message cannot be smuggled into `error_class`.

Skipped without TEST_POSTGRES_DSN:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import date

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")

#: THE NIGHT THIS FILE WRITES, and it must be the one the reader will serve.
#:
#: It was a literal `date(2026, 8, 31)`. The table is append-only by design (no
#: DELETE, no scrub, deliberately), so the disposable database accumulates every
#: night any other suite or a local scheduler pass ever wrote -- and the moment
#: one of them wrote a LATER `as_of_date`, section 3 below asked
#: `list_nightly_step_runs(nights=1)` for a night that was no longer the last
#: one and got somebody else's run back. Measured on 2026-09-01: two full runs
#: of `nrun_01M1E...` stamped `2026-09-01` sat ahead of the fixed literal, and
#: the three reader tests raised `StopIteration` on their own `run_id`.
#:
#: `date.today()` is not a convenience here: it is exactly what
#: `scheduler.run_nightly_steps` is handed in production, so the rows this file
#: writes are shaped like the rows the reader is built for.
_NIGHT = date.today()


def _connection():
    return psycopg.connect(_DSN)


@pytest.fixture()
def conn():
    connection = _connection()
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


@pytest.fixture()
def ledger_db(monkeypatch):
    """Point the scheduler's ledger at the DISPOSABLE database, never at a DSN
    resolved from the environment. `core.db.get_connection` reads its URL from
    configuration, and a test that let it do so would be one mis-set variable
    away from writing a test artefact into production."""

    @contextlib.contextmanager
    def _get_connection():
        connection = _connection()
        try:
            yield connection
        finally:
            connection.close()

    monkeypatch.setattr("core.db.get_connection", _get_connection)
    return _get_connection


def _rows(conn, run_id: str) -> dict[str, dict]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT step_name, step_ordinal, started_at, ended_at, outcome, error_class
              FROM app.nightly_step_runs
             WHERE run_id = %s
             ORDER BY step_ordinal
            """,
            (run_id,),
        )
        columns = [c.name for c in cur.description]
        return {
            row[0]: dict(zip(columns, row, strict=True)) for row in cur.fetchall()
        }


def _run_id(_label: str) -> str:
    """A FRESH id per call, never a deterministic one.

    The table is append-only by design: its DELETE guard refuses, so
    `conftest`'s pg scrub cannot clean it and a fixed id would make the SECOND
    run of this file collide on the primary key. That is not a wart to work
    around -- it is the guarantee under test, met the way production meets it:
    every night mints its own `nrun_`.
    """
    return f"nrun_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# 1. The whole declared sequence is written before the first step runs
# ---------------------------------------------------------------------------


def test_dispatch_declares_every_step_as_an_open_row(conn, ledger_db):
    from core import scheduler

    run_id = _run_id("A1")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    assert ledger.declare() is True

    rows = _rows(conn, run_id)
    assert set(rows) == set(scheduler.NIGHTLY_STEPS)
    assert len(rows) == len(scheduler.NIGHTLY_STEPS)
    # Every one of them is the SILENCE state until something starts it.
    assert all(row["started_at"] is None for row in rows.values())
    assert all(row["ended_at"] is None for row in rows.values())
    assert all(row["outcome"] is None for row in rows.values())
    # The order is the sequence's, not the table's insertion order.
    assert [rows[name]["step_ordinal"] for name in scheduler.NIGHTLY_STEPS] == list(
        range(len(scheduler.NIGHTLY_STEPS))
    )


def test_declaring_twice_adds_nothing(conn, ledger_db):
    """A retried dispatch must not double the night, and must not reset it."""
    from core import scheduler

    run_id = _run_id("A2")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    ledger.declare()
    scheduler._run_isolated_step("alert_check", lambda: None, _ledger=ledger)
    ledger.declare()

    rows = _rows(conn, run_id)
    assert len(rows) == len(scheduler.NIGHTLY_STEPS)
    assert rows["alert_check"]["outcome"] == "succeeded"


# ---------------------------------------------------------------------------
# 2. The three outcomes of a step, each from the code path that produces it
# ---------------------------------------------------------------------------


def test_a_green_night_leaves_n_closed_rows(conn, ledger_db):
    from core import scheduler

    run_id = _run_id("B1")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    ledger.declare()
    for step in scheduler.NIGHTLY_STEPS:
        scheduler._run_isolated_step(step, lambda: None, _ledger=ledger)

    rows = _rows(conn, run_id)
    assert len(rows) == len(scheduler.NIGHTLY_STEPS)
    assert {row["outcome"] for row in rows.values()} == {"succeeded"}
    assert all(row["error_class"] is None for row in rows.values())
    assert all(row["ended_at"] >= row["started_at"] for row in rows.values())


def test_a_step_that_raises_leaves_a_failed_row_naming_the_class(conn, ledger_db, monkeypatch):
    from core import scheduler

    # `_run_isolated_step` also writes a meta-alert on failure; that path is not
    # what this file measures, and it opens its own connection.
    monkeypatch.setattr(scheduler, "_insert_meta_alert", lambda *a, **k: None)

    run_id = _run_id("B2")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    ledger.declare()

    def boom():
        raise ZeroDivisionError("a message that must never reach the table")

    scheduler._run_isolated_step("dq_monitors", boom, _ledger=ledger)

    row = _rows(conn, run_id)["dq_monitors"]
    assert row["outcome"] == "failed"
    assert row["error_class"] == "ZeroDivisionError"
    assert row["started_at"] is not None and row["ended_at"] is not None
    # The isolation contract still holds: the raise did not escape.
    assert _rows(conn, run_id)["alert_check"]["outcome"] is None


def test_a_crash_shaped_abort_leaves_the_open_row(conn, ledger_db):
    """The step began, the process died, and the OPEN ROW is the record.

    A `BaseException` is what a SIGTERM handler, an OOM kill and a
    `KeyboardInterrupt` all look like from inside the step: `_run_isolated_step`
    catches `Exception` and this passes straight through it. What must survive is
    a row with `started_at` set and `ended_at` NULL -- which is only true because
    there is no `finally` closing the row on the way out. Adding one would make
    a killed step indistinguishable from a step that reported nothing, and this
    test is what refuses it.
    """
    from core import scheduler

    run_id = _run_id("B3")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    ledger.declare()

    def killed():
        raise KeyboardInterrupt("the container went away")

    with pytest.raises(KeyboardInterrupt):
        scheduler._run_isolated_step("rebuild_cache", killed, _ledger=ledger)

    row = _rows(conn, run_id)["rebuild_cache"]
    assert row["started_at"] is not None
    assert row["ended_at"] is None
    assert row["outcome"] is None


def test_a_step_whose_call_site_never_runs_stays_never_started(conn, ledger_db):
    """The whole point: a dead call site is a row, not an absence."""
    from core import scheduler

    run_id = _run_id("B4")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    ledger.declare()
    for step in scheduler.NIGHTLY_STEPS:
        if step == "run_due_briefings":
            continue  # the dead call site
        scheduler._run_isolated_step(step, lambda: None, _ledger=ledger)

    rows = _rows(conn, run_id)
    assert rows["run_due_briefings"]["started_at"] is None
    assert rows["run_due_briefings"]["outcome"] is None
    assert len([r for r in rows.values() if r["outcome"] == "succeeded"]) == (
        len(scheduler.NIGHTLY_STEPS) - 1
    )


# ---------------------------------------------------------------------------
# 3. The read<->write loop: what is written is what the screen is served
# ---------------------------------------------------------------------------


def test_the_reader_serves_every_state_the_writer_can_produce(conn, ledger_db, monkeypatch):
    from core import platform_clocks, scheduler

    monkeypatch.setattr(scheduler, "_insert_meta_alert", lambda *a, **k: None)

    run_id = _run_id("C1")
    ledger = scheduler._NightlyStepLedger(run_id, _NIGHT)
    ledger.declare()

    scheduler._run_isolated_step("dispatch_nightly", lambda: None, _ledger=ledger)

    def boom():
        raise RuntimeError("x")

    scheduler._run_isolated_step("alert_check", boom, _ledger=ledger)
    ledger.started("dq_monitors")  # started and never closed

    state = platform_clocks.list_nightly_step_runs(conn, nights=1)
    assert state["has_run"] is True
    assert state["declared_steps"] == list(scheduler.NIGHTLY_STEPS)

    night = next(run for run in state["runs"] if run["run_id"] == run_id)
    by_name = {step["step_name"]: step for step in night["steps"]}
    assert by_name["dispatch_nightly"]["state"] == "succeeded"
    assert by_name["dispatch_nightly"]["duration_ms"] is not None
    assert by_name["alert_check"]["state"] == "failed"
    assert by_name["alert_check"]["error_class"] == "RuntimeError"
    assert by_name["dq_monitors"]["state"] == platform_clocks.UNFINISHED
    assert by_name["dq_monitors"]["duration_ms"] is None
    assert by_name["run_due_briefings"]["state"] == platform_clocks.NEVER_STARTED
    # The summary a person acts on: everything that is not a closed success.
    assert "dispatch_nightly" not in night["unresolved"]
    assert {"alert_check", "dq_monitors", "run_due_briefings"} <= set(night["unresolved"])
    # The steps come back in the declared order, never the table's.
    assert [s["step_name"] for s in night["steps"]] == list(scheduler.NIGHTLY_STEPS)


def test_a_declared_step_with_no_row_reads_as_unrecorded_not_as_missing(conn, ledger_db):
    """A dispatch-time write that did not land must not shorten the list.

    A short list of green steps is exactly the silence that reads like health.
    """
    from core import platform_clocks, scheduler

    run_id = _run_id("C2")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.nightly_step_runs
                (run_id, as_of_date, step_name, step_ordinal)
            VALUES (%s, %s, 'dispatch_nightly', 0)
            """,
            (run_id, _NIGHT),
        )
    conn.commit()

    state = platform_clocks.list_nightly_step_runs(conn, nights=1)
    night = next(run for run in state["runs"] if run["run_id"] == run_id)
    by_name = {step["step_name"]: step for step in night["steps"]}
    assert len(night["steps"]) == len(scheduler.NIGHTLY_STEPS)
    assert by_name["dispatch_nightly"]["state"] == platform_clocks.NEVER_STARTED
    assert by_name["alert_check"]["state"] == platform_clocks.UNRECORDED
    assert by_name["alert_check"]["declared"] is True


def test_a_step_the_sequence_no_longer_declares_is_still_reported(conn, ledger_db):
    """It ran. Dropping it from the read would rewrite what happened."""
    from core import platform_clocks

    run_id = _run_id("C3")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.nightly_step_runs
                (run_id, as_of_date, step_name, step_ordinal)
            VALUES (%s, %s, 'a_retired_step', 99)
            """,
            (run_id, _NIGHT),
        )
    conn.commit()

    state = platform_clocks.list_nightly_step_runs(conn, nights=1)
    night = next(run for run in state["runs"] if run["run_id"] == run_id)
    retired = next(s for s in night["steps"] if s["step_name"] == "a_retired_step")
    assert retired["declared"] is False
    assert retired["state"] == platform_clocks.NEVER_STARTED


def test_one_night_dispatched_twice_serves_both_runs(conn, ledger_db):
    """`nights=1` means ONE NIGHT, not one run, and a night can hold several.

    The screen this feeds carries `dispatch-nightly` with its own *Run now*, so a
    second run of the same night is an ordinary gesture, not an anomaly. The
    reader bounded `run_id` instead of `as_of_date`, so the earlier run of the
    night silently left the answer -- and a person who pressed *Run now* to
    retry a failed step could no longer see the failure they were retrying.
    """
    from core import platform_clocks

    first, second = _run_id("D1a"), _run_id("D1b")
    with conn.cursor() as cur:
        for run_id in (first, second):
            cur.execute(
                """
                INSERT INTO app.nightly_step_runs
                    (run_id, as_of_date, step_name, step_ordinal)
                VALUES (%s, %s, 'dispatch_nightly', 0)
                """,
                (run_id, _NIGHT),
            )
    conn.commit()

    state = platform_clocks.list_nightly_step_runs(conn, nights=1)
    assert {first, second} <= {run["run_id"] for run in state["runs"]}


# ---------------------------------------------------------------------------
# 4. The guard
#
# The trigger raises with `ERRCODE = '23000'`, which psycopg maps to
# `IntegrityConstraintViolation` -- NOT to `RaiseException` (that is the mapping
# for an un-coded `RAISE`). Asserting the wrong class here would make every
# refusal below read as an unexpected error rather than as the guard working.
# ---------------------------------------------------------------------------


def _declared_row(conn, run_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.nightly_step_runs
                (run_id, as_of_date, step_name, step_ordinal)
            VALUES (%s, %s, 'alert_check', 0)
            """,
            (run_id, _NIGHT),
        )
    conn.commit()


def test_a_recorded_step_can_never_be_deleted(conn):
    run_id = _run_id("D1")
    _declared_row(conn, run_id)
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as caught:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.nightly_step_runs WHERE run_id = %s", (run_id,))
    assert "never deleted" in str(caught.value)
    conn.rollback()


def test_a_row_cannot_be_born_judged(conn):
    run_id = _run_id("D2")
    with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.nightly_step_runs
                    (run_id, as_of_date, step_name, step_ordinal,
                     started_at, ended_at, outcome)
                VALUES (%s, %s, 'alert_check', 0, NOW(), NOW(), 'succeeded')
                """,
                (run_id, _NIGHT),
            )
    assert "declared before it is judged" in str(caught.value)
    conn.rollback()


def test_a_closed_row_is_frozen(conn):
    run_id = _run_id("D3")
    _declared_row(conn, run_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.nightly_step_runs SET started_at = NOW() WHERE run_id = %s",
            (run_id,),
        )
        cur.execute(
            "UPDATE app.nightly_step_runs SET ended_at = NOW(), outcome = 'failed', "
            "error_class = 'RuntimeError' WHERE run_id = %s",
            (run_id,),
        )
    conn.commit()

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.nightly_step_runs SET outcome = 'succeeded', "
                "error_class = NULL WHERE run_id = %s",
                (run_id,),
            )
    assert "frozen" in str(caught.value)
    conn.rollback()


def test_a_message_cannot_be_smuggled_into_the_error_class(conn):
    """The payload guard. A class name is an identifier; a message is not."""
    run_id = _run_id("D4")
    _declared_row(conn, run_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.nightly_step_runs SET started_at = NOW() WHERE run_id = %s",
            (run_id,),
        )
    conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.nightly_step_runs SET ended_at = NOW(), outcome = 'failed', "
                "error_class = %s WHERE run_id = %s",
                ("ConnectorError: token AKIA1234 for owner@example.com refused", run_id),
            )
    assert "error_class_is_a_class" in str(caught.value)
    conn.rollback()


def test_a_failure_must_name_its_class(conn):
    run_id = _run_id("D5")
    _declared_row(conn, run_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.nightly_step_runs SET started_at = NOW() WHERE run_id = %s",
            (run_id,),
        )
    conn.commit()

    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.nightly_step_runs SET ended_at = NOW(), outcome = 'failed' "
                "WHERE run_id = %s",
                (run_id,),
            )
    assert "failure_names_its_class" in str(caught.value)
    conn.rollback()


def test_a_row_cannot_end_without_having_begun(conn):
    run_id = _run_id("D6")
    _declared_row(conn, run_id)
    with pytest.raises(psycopg.errors.CheckViolation) as caught:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.nightly_step_runs SET ended_at = NOW(), outcome = 'succeeded' "
                "WHERE run_id = %s",
                (run_id,),
            )
    assert "ended_implies_started" in str(caught.value)
    conn.rollback()
