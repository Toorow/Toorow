"""Two Datastreams are not one pull -- AI-302, migration 275.

WHAT WAS MEASURED, on production 2026-08-17. Nine active Datastreams share one
Google authorization and one 30-day window, and each reads a DIFFERENT
`report_profile_id`. One nightly tick dispatched all nine and `app.pull_jobs`
received exactly ONE row (`audience by age and gender`, 344 rows). The other
eight deduplicated into it because `uq_pull_jobs_active` keyed on
`(connection_ref_id, date_from, date_to)` and not on the Datastream -- so eight
report profiles were never pulled. And because a deduplicated answer is a REAL
job (`state='running'`), `dispatch_windows` called `on_enqueued` for each, so all
eight advanced `next_run_at` to the next night. Work skipped, clock moved.

WHY NOBODY HAD SEEN IT. Every pull this platform completed before 2026-08-17 was
requested BY HAND (`app.pull_jobs.requested_by` is a person on every earlier
row). A human clicks one Datastream, waits, clicks the next -- each job is `done`
before the following is enqueued, and a terminal row is outside the partial
index, so dedup never fired. The first fleet dispatch is what exposed it, and the
clock had never run one.

WHAT THESE TESTS PIN. The index is the authority -- `enqueue_pull`'s fast-path
SELECT, its `ON CONFLICT` target and its post-conflict re-read must all agree
with it, and a disagreement between `ON CONFLICT` and the index does not
deduplicate: it raises. So these exercise the REAL index on a REAL database
rather than a mocked queue, which is exactly what the existing arming harness
(`test_recurring_retrieval_arming_pg.py`) cannot do with its fake queue.

NOT COVERED: the worker's behaviour once a job is claimed, and whether one pull
of one profile lands the rows of another. That is the connector's contract, not
this key's.
"""

from __future__ import annotations

import os
import uuid

import psycopg
import pytest

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN is required: this proves a real unique index",
)

WINDOW = ("2026-08-01", "2026-08-30")


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:22]}"


@pytest.fixture()
def conn():
    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as connection:
        yield connection


@pytest.fixture()
def scope(conn):
    """One real authorization and two real Datastreams, BORROWED not created.

    `app.pull_jobs` carries foreign keys to both, so the rows must exist -- but
    this file is about a unique index, not about how a Datastream is built.
    Creating one here would drag in its activation triggers and tell us nothing
    about the key. Nothing is written to the borrowed rows, and every job this
    file inserts is rolled back.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM app.connection_ref LIMIT 1")
        ref = cur.fetchone()
        cur.execute("SELECT id FROM app.datastreams LIMIT 2")
        streams = [row[0] for row in cur.fetchall()]
    if ref is None or len(streams) < 2:
        pytest.skip("this base carries no connection and two Datastreams to borrow")
    yield {"connection_ref_id": ref[0], "datastreams": streams}
    conn.rollback()


def _insert(cur, *, connection_ref_id, datastream_id, state="queued"):
    """The INSERT `enqueue_pull` performs, reduced to the columns of the key."""
    job_id = _id("job_")
    cur.execute(
        """
        INSERT INTO app.pull_jobs
            (id, pull_id, connection_ref_id, date_from, date_to, state,
             requested_by, datastream_id)
        VALUES (%s, %s, %s, %s, %s, %s, 'scheduler', %s)
        ON CONFLICT (connection_ref_id, COALESCE(datastream_id, ''), date_from, date_to)
            WHERE state IN ('queued', 'running')
        DO NOTHING
        RETURNING id
        """,
        (job_id, _id("pull_"), connection_ref_id, *WINDOW, state, datastream_id),
    )
    row = cur.fetchone()
    return row[0] if row else None


@requires_postgres
def test_two_datastreams_on_one_authorization_each_get_their_pull(conn, scope):
    """The nine-into-one collapse, in its smallest form.

    Same connection, same window, two Datastreams. Before migration 275 the
    second returned nothing and its report profile was never pulled.
    """
    with conn.cursor() as cur:
        first = _insert(
            cur,
            connection_ref_id=scope["connection_ref_id"],
            datastream_id=scope["datastreams"].pop(),
        )
        second = _insert(
            cur,
            connection_ref_id=scope["connection_ref_id"],
            datastream_id=scope["datastreams"].pop(),
        )

    assert first is not None, "the first pull must be written"
    assert second is not None, (
        "the second Datastream deduplicated into the first: its report profile "
        "would never be pulled, and its clock would advance anyway"
    )
    assert first != second


@requires_postgres
def test_the_same_datastream_asked_twice_is_still_one_spend(conn, scope):
    """What the rule was written for, and it is kept.

    `un double-clic est une dépense, pas deux` -- the same Datastream, same
    window, twice, is ONE pull. Widening the key must not have widened this.
    """
    with conn.cursor() as cur:
        datastream_id = scope["datastreams"][0]
        first = _insert(
            cur, connection_ref_id=scope["connection_ref_id"], datastream_id=datastream_id
        )
        second = _insert(
            cur, connection_ref_id=scope["connection_ref_id"], datastream_id=datastream_id
        )

    assert first is not None
    assert second is None, "the same Datastream asked twice must collide"


@requires_postgres
def test_the_legacy_connection_level_pull_still_deduplicates(conn, scope):
    """COALESCE, and why it is not cosmetic.

    `datastream_id` is nullable -- the legacy per-connection dispatch writes
    none. In a unique index NULLs never collide, so keying on the bare column
    would silently stop deduplicating that path: a double-click there would
    become two provider pulls.
    """
    with conn.cursor() as cur:
        first = _insert(cur, connection_ref_id=scope["connection_ref_id"], datastream_id=None)
        second = _insert(cur, connection_ref_id=scope["connection_ref_id"], datastream_id=None)

    assert first is not None
    assert second is None, "two connection-level pulls of one window must collide"


@requires_postgres
def test_a_datastream_pull_and_a_connection_pull_are_not_the_same_row(conn, scope):
    """A NULL Datastream is its own bucket, not a wildcard matching every one."""
    with conn.cursor() as cur:
        legacy = _insert(cur, connection_ref_id=scope["connection_ref_id"], datastream_id=None)
        owned = _insert(
            cur,
            connection_ref_id=scope["connection_ref_id"],
            datastream_id=scope["datastreams"].pop(),
        )

    assert legacy is not None and owned is not None


@requires_postgres
def test_a_terminal_job_never_blocks_the_next_window(conn, scope):
    """The predicate is still partial: `done` and `failed` are outside it.

    This is what made the defect invisible for months -- a human waiting for one
    pull to finish before asking the next never met the index at all.
    """
    with conn.cursor() as cur:
        datastream_id = scope["datastreams"][0]
        done = _insert(
            cur,
            connection_ref_id=scope["connection_ref_id"],
            datastream_id=datastream_id,
            state="done",
        )
        again = _insert(
            cur, connection_ref_id=scope["connection_ref_id"], datastream_id=datastream_id
        )

    assert done is not None and again is not None


@requires_postgres
def test_the_index_is_the_one_the_code_targets(conn):
    """`ON CONFLICT` must mirror the index EXPRESSION, or the INSERT raises.

    Read from the catalogue rather than asserted from memory: this is the pair
    that a future migration is most likely to split.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname='app' "
            "AND indexname='uq_pull_jobs_active'"
        )
        row = cur.fetchone()

    assert row is not None, "the dedup index must exist"
    definition = " ".join(row[0].split())
    assert "COALESCE(datastream_id" in definition, definition
    assert "connection_ref_id" in definition and "date_from" in definition
    assert "'queued'" in definition and "'running'" in definition
