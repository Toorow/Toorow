"""AI-306 -- the nightly's own failure alarm, proven against a live table.

WHAT THE EXISTING TEST COULD NOT SEE. `test_scheduler_failure_isolation.py::
test_meta_alert_insert_writes_row` hands `_insert_meta_alert` a fake cursor and
asserts the SQL string contains "meta_alert". A fake cursor accepts any statement,
so it passed for as long as it existed while the real INSERT was refused by
production on EVERY nightly -- the row named a Project id (`'default'`) that the
multi-project layer had left behind, and the foreign key said so:

    2026-08-18T00:32:25Z  scheduler: meta_alert_insert_failed step=dbt_per_project:
    insert or update on table "alert_firings" violates foreign key constraint
    "fk_alert_firings_project"

The failure was then swallowed by the `except` that exists so a broken alarm
cannot break the nightly -- which is right, and is also why nothing ever
surfaced. An alarm that cannot ring is indistinguishable from a night with
nothing to report, and that is the thing this alarm exists to prevent.

So the proof has to be a REAL table with the REAL constraint. Two facts only a
database can state: that a platform-scope firing carries `project_id IS NULL`,
and that the FK tolerates it. Read on 2026-08-23, production carries 676
alert_firings rows and NOT ONE of them is a meta_alert.

The insert commits on a connection of its own, so `live_postgres`' rollback
cannot undo it: the row is deleted by id in a `finally`.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date

import pytest


@contextmanager
def _second_connection():
    """The connection `_insert_meta_alert` opens, pinned to THIS database."""
    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def written_firing(live_postgres):
    """One meta alert written by the real function, then removed by id."""
    from unittest.mock import patch  # noqa: PLC0415

    from core import scheduler  # noqa: PLC0415

    before = _ids(live_postgres)
    with patch("core.db.get_connection", _second_connection):
        scheduler._insert_meta_alert("dbt_per_project", "no such file or directory: 'dbt'")
    live_postgres.commit()
    fresh = _ids(live_postgres) - before
    try:
        yield fresh
    finally:
        if fresh:
            with live_postgres.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.alert_firings WHERE id = ANY(%s)", (list(fresh),)
                )
            live_postgres.commit()


def _ids(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM app.alert_firings WHERE type = 'meta_alert'")
        return {row[0] for row in cur.fetchall()}


def test_the_row_lands_instead_of_being_refused_by_the_foreign_key(
    live_postgres, written_firing
):
    """One row, committed. The FK refused every one of these until migration 291."""
    assert len(written_firing) == 1, (
        "the meta alert did not reach the table -- `_insert_meta_alert` swallows "
        "its own exception, so an absent row IS the failure mode"
    )


def test_the_constraint_that_refused_it_is_live_on_this_table(live_postgres):
    """The FK is REAL here, so the row above landing means something.

    WHY THIS TEST EXISTS, and it is the correction of a first draft of this file.
    Restoring the defect -- writing `'default'` instead of NULL -- left the test
    above GREEN, because the disposable base carries a Project called `default`
    while production does not. A proof that passes on the copy and would have
    failed on the original is not a proof; it is the shape of a green suite that
    proves nothing.

    So the constraint is exercised for what it is: a project_id naming no Project
    is refused, and NULL is the one scope that needs no Project at all. Between
    this test and the NULL assertion below, the defect has nowhere left to hide on
    either shape of database.
    """
    import psycopg  # noqa: PLC0415

    with live_postgres.cursor() as cur:
        cur.execute("SAVEPOINT fk_probe")
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute(
                """
                INSERT INTO app.alert_firings
                    (id, definition_id, type, project_id, metric, fired_at,
                     observed_value, threshold, pull_ids, window_date, severity, message)
                VALUES ('fire_ai306_probe', NULL, 'meta_alert', 'proj_that_does_not_exist',
                        'scheduler_health', now(), 0, 0, '{}', CURRENT_DATE, 'error', 'probe')
                """
            )
        cur.execute("ROLLBACK TO SAVEPOINT fk_probe")


def test_the_platform_scope_is_NULL_and_not_a_project_that_does_not_exist(
    live_postgres, written_firing
):
    """NULL is the scope. `'default'` was a Project id, and it named nothing."""
    (firing_id,) = written_firing
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT project_id, definition_id, type, metric, severity, window_date, message "
            "FROM app.alert_firings WHERE id = %s",
            (firing_id,),
        )
        project_id, definition_id, kind, metric, severity, window_date, message = cur.fetchone()

    assert project_id is None
    assert definition_id is None, "no definition declares the scheduler's own health"
    assert (kind, metric, severity) == ("meta_alert", "scheduler_health", "error")
    assert window_date == date.today()
    assert "dbt_per_project" in message and "dbt" in message


def test_the_reader_a_human_opens_can_see_a_firing_that_belongs_to_no_project(
    live_postgres, written_firing
):
    """A row nothing can read is the same as no row.

    The scope that made the write fail is the scope that makes the READ hard: a
    project-scoped reader filters on `project_id = %s` and a NULL never matches
    it. This asks the platform-wide question instead, which is the one a person
    watching the nightly asks.
    """
    (firing_id,) = written_firing
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.alert_firings "
            "WHERE type = 'meta_alert' AND project_id IS NULL AND window_date = %s",
            (date.today(),),
        )
        assert cur.fetchone()[0] >= 1

        cur.execute(
            "SELECT count(*) FROM app.alert_firings WHERE id = %s AND project_id = 'default'",
            (firing_id,),
        )
        assert cur.fetchone()[0] == 0, (
            "'default' is the value that could not be inserted -- it must not come back"
        )
