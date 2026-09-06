"""The claim semantics, against a real PostgreSQL — not a mock. AI-106 / story 56.

WHY THIS FILE EXISTS. Epic 56 shipped 33 tests and every one of them mocked the
database. That is exactly where a mock lies: `FOR UPDATE SKIP LOCKED` has no
behaviour without a real lock, `ON CONFLICT` on a partial unique index has no
behaviour without the index, and a state transition committed by one connection
is invisible to another only if there really are two connections. The defect
found by re-reading the SQL by hand — a contended claim reported as a terminal
job — is the kind these tests catch and mocks cannot.

Skipped without TEST_POSTGRES_DSN. Stand one up in one command:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import os

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")


_ORG = "org_claim_sem_test"
_PROJECT = "proj_claim_sem_test"
_CONNECTION = "conn_claim_sem_test"


@pytest.fixture()
def conn():
    """A connection with the FK chain a pull job actually needs.

    THE FIRST RUN OF THIS FILE TAUGHT IT. The seed helper carried the comment
    "`connection_ref_id` is free text at this layer" -- and a real database
    answered ForeignKeyViolation: `pull_jobs.connection_ref_id` references
    `app.connection_ref`, which references `app.projects`, which references
    `app.organizations`. A mock accepts any string; only the schema says which
    strings exist. That is the whole reason this file is not a unit test.
    """
    connection = psycopg.connect(_DSN)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, 'claim semantics', %s, 'test') ON CONFLICT (id) DO NOTHING",
                (_ORG, _ORG),
            )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, 'claim semantics', %s, 'test', %s) "
                "ON CONFLICT (id) DO NOTHING",
                (_PROJECT, _PROJECT, _ORG),
            )
            cur.execute(
                # `owner_org_id` is NOT NULL: a credential belongs to an
                # organization, not only to a project. Another thing the schema
                # says and a mock never would.
                # `ck_connection_ref_nango_id_required`: an auth_path of 'nango'
                # (the column default) demands a nango_connection_id. Declaring
                # the direct path instead is what a Google credential really is,
                # and it is the shape the queue's own fallback was written for.
                "INSERT INTO app.connection_ref "
                "  (id, provider, project_id, owner_org_id, owner_identity, auth_path) "
                "VALUES (%s, 'gsc', %s, %s, 'test', 'google_direct') "
                "ON CONFLICT (id) DO NOTHING",
                (_CONNECTION, _PROJECT, _ORG),
            )
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cur:
            # ON DELETE CASCADE from connection_ref takes the jobs with it, so the
            # fixture cleans up even after a test that failed mid-way.
            cur.execute("DELETE FROM app.connection_ref WHERE id = %s", (_CONNECTION,))
            # The org and the project STAY. Removing them is not the fixture's
            # business and it cannot be done from here: deleting an organization
            # fires a trigger onto `org_plan_history`, a table the application
            # role is deliberately not allowed to write -- the same
            # least-privilege that makes this database worth testing against.
            # Both inserts are ON CONFLICT DO NOTHING, so reusing them across
            # runs is the intended behaviour rather than a leak.
        connection.commit()
        connection.close()


def _seed_job(cur, job_id: str, *, state: str = "queued", window: str = "2026-07-01") -> None:
    """Insert one pull job against the fixture's real connection_ref."""
    cur.execute(
        """
        INSERT INTO app.pull_jobs
            (id, pull_id, connection_ref_id, date_from, date_to, state, requested_by)
        VALUES (%s, %s, %s, %s, %s, %s, 'test')
        """,
        (job_id, f"pull_{job_id}", _CONNECTION, window, window, state),
    )


def test_a_claim_moves_the_job_and_the_second_claim_finds_nothing(conn):
    """The transition is committed by the claim, not left to the caller."""
    from core.queue import claim_job_by_id

    with conn.cursor() as cur:
        _seed_job(cur, "job_claim_1")
    conn.commit()

    first = claim_job_by_id(conn, "job_claim_1")
    assert first is not None
    assert first["id"] == "job_claim_1"

    second = claim_job_by_id(conn, "job_claim_1")
    assert second is None, "the same job was claimed twice"

    with conn.cursor() as cur:
        cur.execute("SELECT state, started_at FROM app.pull_jobs WHERE id = %s", ("job_claim_1",))
        state, started_at = cur.fetchone()
    assert state == "running"
    assert started_at is not None, "a claim that does not stamp started_at cannot be recovered"

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.pull_jobs WHERE id = %s", ("job_claim_1",))
    conn.commit()


def test_a_terminal_job_is_never_reclaimed(conn):
    """At-least-once delivery must not become at-least-once PULLING.

    The append-only raw zone would HIDE the duplicate (supersede by pull_id)
    rather than reveal it, so the refusal has to happen at the claim.
    """
    from core.queue import claim_job_by_id

    with conn.cursor() as cur:
        _seed_job(cur, "job_done_1", state="done", window="2026-07-02")
    conn.commit()

    assert claim_job_by_id(conn, "job_done_1") is None

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.pull_jobs WHERE id = %s", ("job_done_1",))
    conn.commit()


def test_two_deliveries_race_and_exactly_one_wins(conn):
    """`SKIP LOCKED` with two REAL connections -- the case a mock cannot express.

    Cloud Tasks is at-least-once, so the same job_id genuinely arrives twice. The
    loser must see None (and answer 409 `claim_contended`), never execute.
    """
    from core.queue import claim_job_by_id

    with conn.cursor() as cur:
        _seed_job(cur, "job_race_1", window="2026-07-03")
    conn.commit()

    other = psycopg.connect(_DSN)
    try:
        # Hold the row in a second transaction, uncommitted, exactly as a
        # concurrent delivery would.
        with other.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.pull_jobs WHERE id = %s FOR UPDATE",
                ("job_race_1",),
            )
        loser = claim_job_by_id(conn, "job_race_1")
        assert loser is None, "both deliveries claimed the same job"

        # And the loser must not read it as finished: the row is still 'queued'
        # because the winner has not committed. This is the exact state that used
        # to be reported as `already_terminal`.
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM app.pull_jobs WHERE id = %s", ("job_race_1",))
            assert cur.fetchone()[0] == "queued"
    finally:
        other.rollback()
        other.close()

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.pull_jobs WHERE id = %s", ("job_race_1",))
    conn.commit()


def test_the_partial_unique_index_refuses_a_second_live_window(conn):
    """What `ON CONFLICT` in CloudTasksBackend.enqueue_pull actually rides on.

    The backend carried no ON CONFLICT at all until story 56.1: a double click
    raised UniqueViolation and, worse, would have dispatched a SECOND task for a
    job already in flight. This proves the index it now relies on exists and
    covers exactly the live states.
    """
    with conn.cursor() as cur:
        _seed_job(cur, "job_uq_1", window="2026-07-04")
        conn.commit()

        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                """
                INSERT INTO app.pull_jobs
                    (id, pull_id, connection_ref_id, date_from, date_to, state, requested_by)
                VALUES ('job_uq_2', 'pull_uq_2', %s,
                        '2026-07-04', '2026-07-04', 'queued', 'test')
                """,
                (_CONNECTION,),
            )
    conn.rollback()

    # A TERMINAL job does not block a fresh one: the index is partial, so
    # re-pulling the same window tomorrow is allowed -- which is what makes the
    # rewrite pattern (seven days, every night) possible at all.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.pull_jobs SET state = 'done' WHERE id = %s", ("job_uq_1",)
        )
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, date_from, date_to, state, requested_by)
            VALUES ('job_uq_3', 'pull_uq_3', %s,
                    '2026-07-04', '2026-07-04', 'queued', 'test')
            """,
            (_CONNECTION,),
        )
        cur.execute("DELETE FROM app.pull_jobs WHERE id IN ('job_uq_1', 'job_uq_3')")
    conn.commit()


def test_recover_stale_running_jobs_does_not_touch_a_queued_row(conn):
    """The gap the reconciliation sweep exists for (story 56.4).

    A row committed as 'queued' whose task was never created is invisible to the
    stale-claim recovery: that mechanism re-queues jobs stuck in 'running', which
    is the OPPOSITE failure. Proving it here is what justifies the sweep rather
    than reusing what already existed.
    """
    from core.queue import recover_stale_running_jobs

    with conn.cursor() as cur:
        _seed_job(cur, "job_orphan_1", window="2026-07-05")
    conn.commit()

    recover_stale_running_jobs(max_running_seconds=0)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, attempt_count FROM app.pull_jobs WHERE id = %s", ("job_orphan_1",)
        )
        state, attempts = cur.fetchone()
    assert state == "queued"
    assert attempts == 0, "the stale-RUNNING recovery spent an attempt on a QUEUED row"

    with conn.cursor() as cur:
        cur.execute("DELETE FROM app.pull_jobs WHERE id = %s", ("job_orphan_1",))
    conn.commit()
