"""The SECOND queue and the reconciliation sweep, against a real PostgreSQL.

Stories 56.3 and 56.4. The pull queue got its integration proof first; these two
seams were still mocked, and they are where the mock is least trustworthy:

  * the activation queue's claim accepts a state the pull queue's refuses
    (`failed`, under the attempt ceiling), and that difference IS the reason its
    endpoint answers 429 where the pull target answers 200. A mock returning
    whatever it was told proves neither;
  * the sweep selects on `enqueued_at` against a grace period -- a time window,
    which a mock has no clock for;
  * and the activation payload passes through `app.safe_preconfiguration_evidence`,
    a CHECK that refuses any object carrying a credential-shaped key. That
    guarantee lives in the schema and nowhere else, so only a database can be
    asked whether it holds.

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

_ORG = "org_activation_sem_test"
_PROJECT = "proj_activation_sem_test"
_DRAFT = "dsd_01ARZ3NDEKTSV4RRFFQ69G5FAV"
_HASH = "a" * 64


def _job_id(suffix: str) -> str:
    """A ULID-shaped id: the table's CHECK refuses anything else."""
    base = "01ARZ3NDEKTSV4RRFFQ69G5F"
    return f"dsaj_{base}{suffix}"[:31]


@pytest.fixture()
def conn():
    connection = psycopg.connect(_DSN)
    try:
        with connection.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, 'activation semantics', %s, 'test') "
                "ON CONFLICT (id) DO NOTHING",
                (_ORG, _ORG),
            )
            cur.execute(
                "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
                "VALUES (%s, 'activation semantics', %s, 'test', %s) "
                "ON CONFLICT (id) DO NOTHING",
                (_PROJECT, _PROJECT, _ORG),
            )
            # A setup_preview job REQUIRES a draft: the composite FK is on
            # (draft_id, project_id), and the table's CHECK forbids the pair
            # (draft_id NULL, kind='setup_preview').
            cur.execute(
                "INSERT INTO app.datastream_setup_drafts "
                "  (id, project_id, created_by, idempotency_key_hash) "
                "VALUES (%s, %s, 'test', %s) ON CONFLICT (id) DO NOTHING",
                (_DRAFT, _PROJECT, _HASH),
            )
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        with connection.cursor() as cur:
            cur.execute(
                "DELETE FROM app.datastream_activation_jobs WHERE project_id = %s", (_PROJECT,)
            )
        connection.commit()
        connection.close()


def _seed(cur, job_id: str, *, state: str = "queued", attempts: int = 0,
          age_seconds: int = 0, payload: dict | None = None) -> None:
    cur.execute(
        """
        INSERT INTO app.datastream_activation_jobs
            (id, kind, project_id, draft_id, correlation_id, payload, state,
             attempt_count, requested_by, enqueued_at)
        VALUES (%s, 'setup_preview', %s, %s, %s, %s::jsonb, %s, %s, 'test',
                now() - (%s * interval '1 second'))
        """,
        (job_id, _PROJECT, _DRAFT, job_id, json.dumps(payload or {"step": "preview"}),
         state, attempts, age_seconds),
    )


def test_a_claim_moves_the_job_and_spends_one_attempt(conn):
    from core.queue import claim_activation_job_by_id

    job_id = _job_id("A1")
    with conn.cursor() as cur:
        _seed(cur, job_id)
    conn.commit()

    claimed = claim_activation_job_by_id(conn, job_id)
    assert claimed is not None
    assert claimed["attempt_count"] == 1, "the claim must spend the attempt it is about to use"
    assert claim_activation_job_by_id(conn, job_id) is None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, started_at FROM app.datastream_activation_jobs WHERE id = %s",
            (job_id,),
        )
        state, started_at = cur.fetchone()
    assert state == "running"
    assert started_at is not None


def test_a_failed_job_under_the_ceiling_is_claimable_again(conn):
    """THE difference from the pull queue, and the reason for the 429.

    This queue retries IN PLACE. A pull job that failed is terminal for its task;
    an activation job that failed is work that must come back -- so its endpoint
    answers 429 where the pull target answers 200.
    """
    from core.queue import claim_activation_job_by_id

    job_id = _job_id("B2")
    with conn.cursor() as cur:
        _seed(cur, job_id, state="failed", attempts=1)
    conn.commit()

    claimed = claim_activation_job_by_id(conn, job_id)
    assert claimed is not None, "a failed job with attempts left was refused"
    assert claimed["attempt_count"] == 2


def test_a_job_at_the_attempt_ceiling_is_refused(conn):
    """Otherwise a redelivered task would re-run a job forever.

    The endpoint reports this as `claim_contended_or_exhausted`, not as
    `already_terminal`: the row is still `failed`, and calling that "finished"
    would put a state under a code saying the opposite.
    """
    from core.queue import _max_attempts, claim_activation_job_by_id

    job_id = _job_id("C3")
    with conn.cursor() as cur:
        _seed(cur, job_id, state="failed", attempts=_max_attempts())
    conn.commit()

    assert claim_activation_job_by_id(conn, job_id) is None


def test_the_queue_refuses_to_carry_a_secret(conn):
    """`app.safe_preconfiguration_evidence` is a privacy guard in the SCHEMA.

    The activation payload travels from the wizard to a worker and back; the
    CHECK refuses any object carrying a credential-shaped key, whatever the
    application layer believes it is sending. Nothing in Python enforces this,
    so nothing but a database can be asked whether it still holds.
    """
    with conn.cursor() as cur:
        with pytest.raises(psycopg.errors.CheckViolation):
            _seed(cur, _job_id("D4"), payload={"refresh_token": "leaked"})
    conn.rollback()

    # And the same payload without the forbidden key is accepted, so the guard is
    # refusing the SECRET rather than the shape.
    with conn.cursor() as cur:
        _seed(cur, _job_id("D5"), payload={"step": "preview", "rows": 42})
    conn.commit()


def test_the_sweep_re_dispatches_what_is_stale_and_executes_nothing(conn):
    """Story 56.4, against a real clock and a real table.

    Two properties in one run: the grace period is honoured (a fresh row is left
    alone), and nothing is EXECUTED -- the moment this sweep runs a job itself it
    has become a second worker.
    """
    from unittest.mock import patch

    from core import queue

    stale_id, fresh_id = _job_id("E6"), _job_id("E7")
    with conn.cursor() as cur:
        _seed(cur, stale_id, age_seconds=3600)
        _seed(cur, fresh_id, age_seconds=0)
    conn.commit()

    dispatched: list[str] = []
    executed: list[dict] = []
    previous = os.environ.get("QUEUE_BACKEND")
    os.environ["QUEUE_BACKEND"] = "cloud_tasks"
    try:
        with (
            patch.object(queue, "dispatch_activation_task",
                         side_effect=lambda job_id, **_: dispatched.append(job_id) or True),
            patch.object(queue, "dispatch_pull_task", return_value=False),
            patch.object(queue, "execute_claimed_activation_job",
                         side_effect=lambda job: executed.append(job)),
            patch.object(queue, "execute_claimed_job",
                         side_effect=lambda job: executed.append(job)),
        ):
            result = queue.reconcile_pending_tasks(grace_seconds=300)
    finally:
        if previous is None:
            os.environ.pop("QUEUE_BACKEND", None)
        else:
            os.environ["QUEUE_BACKEND"] = previous

    assert stale_id in dispatched, "the stale job was not re-dispatched"
    assert fresh_id not in dispatched, "the grace period was ignored"
    assert executed == [], "the sweep executed work -- it has become a second worker"
    assert result["activation_redispatched"] >= 1
