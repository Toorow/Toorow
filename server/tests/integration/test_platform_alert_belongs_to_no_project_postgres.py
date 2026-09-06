"""Live-Postgres proof that a platform alert can be written and can be read.

AI-306, measured in PRODUCTION and nowhere else. Cloud Run, 2026-08-18T00:32:25Z:

    WARNING core.scheduler scheduler: meta_alert_insert_failed step=dbt_per_project:
    insert or update on table "alert_firings" violates foreign key constraint
    "fk_alert_firings_project"

`_insert_meta_alert` wrote `project_id = 'default'` -- a Project id from before
the multi-project layer, seeded by migration 018 and gone from production since.
The foreign key refused every platform alert, and the caller's `except` turned
each one into a log line. Every nightly step that failed said so to nobody.

Two things this file exists to hold, and neither can be held by a mock:

  * the WRITE reaches the table. A fake cursor accepts any SQL, which is exactly
    why `test_scheduler_failure_isolation.test_meta_alert_insert_writes_row` was
    green throughout: it asserts the statement's TEXT, and the statement was
    fine. What was wrong was a value the database refused.
  * the READ finds it. A row nobody can read is the same silence one layer
    further on -- the trap named when this was still an open question: platform
    scope must not become invisible scope. `fetch_recent_meta_alerts` is asked
    from a Project that has nothing to do with the scheduler, because that is
    the design: one shared scheduler, every Project sees its health.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from unittest.mock import patch

import pytest

FIXTURE_AUTHOR = "owner@example.com"


@pytest.fixture()
def platform_scope(live_postgres, test_org):
    """A Project that is NOT the scheduler's, plus a clean slate of meta-alerts."""
    conn = live_postgres
    project_id = f"proj_ma_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (project_id, test_org, "Platform alert scope", project_id.lower(), FIXTURE_AUTHOR),
        )
    conn.commit()
    written: list[str] = []
    try:
        yield conn, project_id, written
    finally:
        from tests.conftest import purge_fixture_project  # noqa: PLC0415

        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.alert_firings WHERE message LIKE %s", ("%kaboom%",))
        conn.commit()
        purge_fixture_project(conn, project_id)


@contextlib.contextmanager
def _second_connection():
    """The scheduler opens its own connection; pin it to the database under test."""
    import psycopg  # noqa: PLC0415

    conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=5)
    try:
        yield conn
    finally:
        conn.close()


def _raise_a_meta_alert(step: str = "dbt_per_project") -> None:
    from core.scheduler import _insert_meta_alert  # noqa: PLC0415

    with patch("core.db.get_connection", _second_connection):
        _insert_meta_alert(step, "kaboom")


def test_a_failed_nightly_step_leaves_a_row_instead_of_a_log_line(platform_scope):
    conn, _project_id, _written = platform_scope

    _raise_a_meta_alert()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, metric, severity, message FROM app.alert_firings "
            "WHERE type = 'meta_alert' AND message LIKE %s",
            ("%kaboom%",),
        )
        rows = cur.fetchall()

    assert len(rows) == 1, "the meta-alert was refused by the database again"
    project_id, metric, severity, message = rows[0]
    assert project_id is None, "platform scope is NULL, never a sentinel id"
    assert (metric, severity) == ("scheduler_health", "error")
    assert "dbt_per_project" in message and "kaboom" in message


def test_every_project_reads_the_health_of_the_one_shared_scheduler(platform_scope):
    """Platform scope must not become invisible scope.

    The Project asked here has nothing to do with the scheduler. That is the
    point: one process serves every Project, so its failure is everybody's to
    see -- and a NULL that no reader unions would be the same silence one layer
    further on.
    """
    from core.business_alerts import fetch_recent_meta_alerts  # noqa: PLC0415

    conn, project_id, _written = platform_scope

    _raise_a_meta_alert()

    alerts = fetch_recent_meta_alerts(project_id, conn, hours=24)

    mine = [a for a in alerts if "kaboom" in a["message"]]
    assert len(mine) == 1, "a platform alert nobody can read is the same silence"
    assert mine[0]["code"] == "meta_alert"
    assert mine[0]["metric"] == "scheduler_health"


def test_the_column_no_longer_defaults_to_a_project_that_does_not_exist(live_postgres):
    """Migration 291 removed the trap, not only the symptom.

    `DEFAULT 'default'` (migration 013) meant any INSERT omitting `project_id`
    aimed at a row production does not have: it failed at the database, inside
    somebody's `except`, rather than at the writer. With no default an omission
    lands as NULL -- platform scope, honest and readable.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = 'app'
              AND table_name = 'alert_firings'
              AND column_name = 'project_id'
            """
        )
        row = cur.fetchone()

    assert row is not None, "app.alert_firings.project_id is gone"
    is_nullable, column_default = row
    assert is_nullable == "YES"
    assert column_default is None, f"the sentinel default is back: {column_default!r}"


def test_the_foreign_key_still_guards_a_named_project(live_postgres):
    """Nothing was loosened: NULL is unconstrained by an FK, a wrong id is not.

    The absent id is a random one, deliberately NOT `'default'`. On this
    database `'default'` EXISTS -- migration 018 seeds it and the chain gives it
    an org before migration 100 makes `org_id` NOT NULL -- while production has
    no such row. That difference is the whole reason the defect was invisible
    here for as long as it lasted: the value the FK refused in production is a
    value this database accepts.
    """
    import psycopg  # noqa: PLC0415
    from ulid import ULID  # noqa: PLC0415

    absent = f"proj_absent_{uuid.uuid4().hex[:12]}"
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.alert_firings
                    (id, definition_id, type, project_id, metric, fired_at,
                     observed_value, threshold, pull_ids, window_date, severity, message)
                VALUES (%s, NULL, 'meta_alert', %s, 'scheduler_health', NOW(),
                        0, 0, '{}', CURRENT_DATE, 'error', 'kaboom absent project')
                """,
                (f"fire_{ULID()}", absent),
            )
    live_postgres.rollback()
