"""Live-Postgres contract for a stopped window -- story 63.6, migration 220.

WHAT A FAKE CURSOR CANNOT CATCH, and each of these was a real risk:

  * whether the CHECK constraint accepts the state at all. A stop whose write is
    refused by the database would raise inside a request handler AFTER the run
    had already been closed -- a run marked stopped whose windows are still
    queued, which is the one inconsistency this story must not produce;
  * whether the SAME window can be enqueued again afterwards.
    `uq_pull_jobs_active` is a PARTIAL unique index on `(connection_ref_id,
    date_from, date_to)`; if the stopped state stayed inside its predicate, the
    days a person gave back could never be collected again;
  * whether the RGPD erasure still passes over a stopped window. Nothing new was
    created, and this is how that claim stops being an assumption.

`pg_owner`: the fixture applies migration 220, which is DDL.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from core import pull_job_states

from tests.migration_ledger import apply_migrations_absent_from_the_ledger

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
STOP_MIGRATION = MIGRATIONS / "220_a_window_that_never_started_can_be_refused.sql"
DEDUP_MIGRATION = MIGRATIONS / "022_pull_jobs_dedup_index.sql"

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def requires_postgres(fn):
    """The DSN gate AND the `pg_owner` marker, on the live tests only.

    Deliberately not a module-level `pytestmark`: the migration-text assertions
    need no database and no ownership, and marking the whole file would have them
    SKIP on a plain application role -- a file reporting "all skipped" while some
    of its tests could have run measures the connection, not the code.
    """
    return _skip_without_dsn(pytest.mark.pg_owner(fn))


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Migration text (runs without Postgres).
# ---------------------------------------------------------------------------


def test_the_migration_widens_the_check_and_creates_nothing() -> None:
    sql = STOP_MIGRATION.read_text(encoding="utf-8")
    assert "pull_jobs_state_check" in sql
    # A state of a row that already exists: no new append-only table owing an
    # RGPD hatch, no new trigger, no new edge for `org_purge`.
    assert "CREATE TABLE" not in sql
    assert "CREATE TRIGGER" not in sql


def test_the_migration_leaves_the_active_index_alone() -> None:
    """It is the index that lets the same days be collected again.

    Re-declaring `uq_pull_jobs_active` here with the stopped state inside its
    predicate would make a refused window keep holding the days it gave back.
    """
    sql = STOP_MIGRATION.read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX" not in sql
    assert "DROP INDEX" not in sql


def test_the_check_declares_exactly_what_the_registry_declares() -> None:
    """Read from the SQL, compared to Python -- both, in the same change.

    The SQL read is the LAST migration that declares `pull_jobs_state_check`,
    never migration 220: 297 widened the same named constraint with `prevented`,
    and a guard anchored on the file that happened to be current when it was
    written measures its own frozen copy. Same derivation as
    `tests/conformance/test_pull_job_state_registry.py::_latest_state_check`.
    """
    import re

    pattern = re.compile(
        r"pull_jobs_state_check\s+CHECK\s*\(\s*state\s+IN\s*\((?P<body>[^)]*)\)",
        re.IGNORECASE,
    )
    body = ""
    for path in sorted(MIGRATIONS.glob("*.sql")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            body = match.group("body")
    assert body, "no CHECK on pull_jobs.state found in the migrations"
    assert set(re.findall(r"'([a-z_]+)'", body)) == set(pull_job_states.JOB_STATES)


# ---------------------------------------------------------------------------
# Live Postgres.
# ---------------------------------------------------------------------------


@pytest.fixture
def stop_db(live_postgres):
    """Apply migration 220, and seed one connection to enqueue against.

    Migration 022 is NOT re-applied here, deliberately: it drops and re-adds the
    same named constraint with the state list it knew in its own day, so running
    it after 220 would silently NARROW the CHECK back and every test below would
    then be measuring a schema this build does not ship. `uq_pull_jobs_active`
    comes from the same file and is already in place on any cluster the
    migrations have been applied to.

    THE SAME TRAP CAUGHT 220 ITSELF (AI-367): this fixture used to run its text
    raw with `cur.execute`, and migration 297 -- which widened the very same
    named constraint with `prevented` -- was silently rolled back on every run.
    The narrowing outlived the test: a shared database keeps it. It now replays
    only what the ledger does not carry, like every other integration fixture.
    """
    conn = live_postgres
    apply_migrations_absent_from_the_ledger(conn, (STOP_MIGRATION,))

    org_id = _id("org_")
    project_id = _id("proj_")
    conn_ref = _id("cref_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'owner@example.com')",
            (org_id, org_id, org_id),
        )
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, %s, %s, 'story-63.6-test', %s)
            """,
            (project_id, project_id, project_id, org_id),
        )
        cur.execute(
            """
            INSERT INTO app.connection_ref
                (id, provider, nango_connection_id, project_id, status, enabled,
                 owner_org_id, owner_identity)
            VALUES (%s, 'generic', %s, %s, 'active', TRUE, %s, 'owner@example.com')
            """,
            (conn_ref, conn_ref, project_id, org_id),
        )
    conn.commit()
    ids = {"org_id": org_id, "project_id": project_id, "connection_ref_id": conn_ref}
    yield conn, ids

    # Cleaned by the product's own erasure rather than by a hand-written list of
    # tables: creating an org arms side tables by trigger (`mdm_business_domains`
    # among them), and an enumeration written here would rot the day the next one
    # is added. `purge_org_tree` erases the tree and leaves the org row, which is
    # exactly the contract the endpoint relies on.
    from tests.conftest import purge_fixture_org  # noqa: PLC0415

    conn.rollback()
    purge_fixture_org(conn, org_id)
    conn.commit()


def _enqueue(conn, ids, date_from: str, date_to: str, state: str) -> str:
    job_id = _id("job_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, date_from, date_to, state, requested_by)
            VALUES (%s, %s, %s, %s, %s, %s, 'test')
            """,
            (job_id, _id("pull_"), ids["connection_ref_id"], date_from, date_to, state),
        )
    return job_id


@requires_postgres
def test_the_check_accepts_every_registered_state_and_nothing_else(stop_db) -> None:
    conn, ids = stop_db
    job_id = _enqueue(conn, ids, "2026-07-01", "2026-07-31", pull_job_states.QUEUED)
    conn.commit()
    for state in pull_job_states.JOB_STATES:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.pull_jobs SET state = %s WHERE id = %s", (state, job_id)
            )
    conn.commit()
    with pytest.raises(Exception) as excinfo:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.pull_jobs SET state = %s WHERE id = %s",
                ("stopping", job_id),
            )
    assert "pull_jobs_state_check" in str(excinfo.value)
    conn.rollback()


@requires_postgres
def test_the_same_window_is_enqueueable_again_after_it_was_refused(stop_db) -> None:
    """The point of the whole gesture: the days are given back, not lost.

    While the window is `queued`, `uq_pull_jobs_active` refuses a second job for
    the same `(connection_ref_id, date_from, date_to)`. Once it is refused, the
    row leaves the index predicate and the same days can be collected again.
    """
    conn, ids = stop_db
    _enqueue(conn, ids, "2026-07-01", "2026-07-31", pull_job_states.QUEUED)
    conn.commit()

    with pytest.raises(Exception) as excinfo:
        _enqueue(conn, ids, "2026-07-01", "2026-07-31", pull_job_states.QUEUED)
    assert "uq_pull_jobs_active" in str(excinfo.value)
    conn.rollback()

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.pull_jobs SET state = %s WHERE connection_ref_id = %s",
            (pull_job_states.CANCELLED, ids["connection_ref_id"]),
        )
    conn.commit()

    re_queued = _enqueue(conn, ids, "2026-07-01", "2026-07-31", pull_job_states.QUEUED)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute("SELECT state FROM app.pull_jobs WHERE id = %s", (re_queued,))
        assert cur.fetchone()[0] == pull_job_states.QUEUED


@requires_postgres
def test_a_running_window_still_holds_its_days(stop_db) -> None:
    """The stop does not touch it, and neither does the index.

    A window in flight keeps the days it is collecting: enqueueing them again
    while it runs would pull the same range twice.
    """
    conn, ids = stop_db
    _enqueue(conn, ids, "2026-06-01", "2026-06-30", pull_job_states.RUNNING)
    conn.commit()
    with pytest.raises(Exception) as excinfo:
        _enqueue(conn, ids, "2026-06-01", "2026-06-30", pull_job_states.QUEUED)
    assert "uq_pull_jobs_active" in str(excinfo.value)
    conn.rollback()


@requires_postgres
def test_the_rgpd_erasure_still_passes_over_a_refused_window(stop_db) -> None:
    """Nothing new was created -- and this is where that stops being an assumption."""
    from core.org_purge import purge_org_tree

    conn, ids = stop_db
    _enqueue(conn, ids, "2026-07-01", "2026-07-31", pull_job_states.CANCELLED)
    conn.commit()

    report = purge_org_tree(conn, ids["org_id"])
    assert report["statements"] > 0
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.pull_jobs WHERE connection_ref_id = %s",
            (ids["connection_ref_id"],),
        )
        assert cur.fetchone()[0] == 0
    conn.rollback()
