"""AI-101 -- a verdict becomes readable per Datastream, against a live database.

The question `execution-substrate.md` asked was not answerable: the only reaction
to an empty pull was a PLATFORM-WIDE count raised with `connector=None`, so
neither "stay scheduled" nor "suspend after N" could be implemented -- one could
not name its target, the other could not measure the noise it accepted.

These tests drive `core.verification_streaks` over real rows, because the whole
point is a JOIN nobody had made: `pull_verifications.pull_id` -> `pull_jobs`,
which carries `datastream_id`.

Four facts, and each is one the sentence a person reads depends on:
  * consecutive empties are counted, and dated from the OLDEST of the run;
  * a good pull ENDS a run -- an older empty streak must not be resurrected;
  * a Datastream that was never verified is ABSENT, never a zero;
  * the run of a stream that has ALWAYS been empty has no boundary, and is
    counted whole.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_OWNER_DSN"),
    reason="TEST_POSTGRES_OWNER_DSN not set -- live Postgres test skipped",
)

_NOW = datetime(2026, 8, 17, 12, 0, tzinfo=timezone.utc)


def _conn():
    import psycopg  # noqa: PLC0415

    return psycopg.connect(os.environ["TEST_POSTGRES_OWNER_DSN"], connect_timeout=5)


def _uid(prefix: str) -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"{prefix}_{ULID()}"


def _seed(cur, *, org_id, project_id, conn_ref, datastream_id, verdicts):
    """Land one Datastream and one verification per (verdict, age-in-days).

    EVERY REQUIRED COLUMN IS NAMED, read from `information_schema` rather than
    discovered one `NotNullViolation` at a time. A fixture that omits one fails
    on the schema instead of on the behaviour it exists to check -- which is what
    `test_project_readiness_sql` does today, and why its red says nothing about
    readiness.
    """
    cur.execute(
        "INSERT INTO app.organizations (id, name, slug, created_by)"
        " VALUES (%s, %s, %s, %s)",
        (org_id, "AI-101 probe", org_id.replace("_", "-").lower(), "person_TEST"),
    )
    cur.execute(
        "INSERT INTO app.projects (id, org_id, name, slug, created_by)"
        " VALUES (%s, %s, %s, %s, %s)",
        (project_id, org_id, "AI-101 probe",
         project_id.replace("_", "-").lower(), "person_TEST"),
    )
    cur.execute(
        # `auth_path` is NOT decoration here: it defaults to `nango`, and
        # `ck_connection_ref_nango_id_required` then demands a nango connection
        # id this fixture has no business inventing. The ratified path for a
        # Google product is direct, so the row says so and the CHECK is satisfied
        # by being HONEST rather than by carrying a fake id.
        "INSERT INTO app.connection_ref"
        " (id, project_id, owner_org_id, owner_identity, provider, auth_path)"
        " VALUES (%s, %s, %s, %s, %s, 'google_direct')",
        (conn_ref, project_id, org_id, "person_TEST", "google-analytics"),
    )
    cur.execute(
        "INSERT INTO app.datastreams (id, project_id, org_id, name, module_name)"
        " VALUES (%s, %s, %s, %s, %s)",
        (datastream_id, project_id, org_id, "AI-101 probe", "google-analytics"),
    )
    for verdict, age_days in verdicts:
        pull_id = _uid("pull")
        # ONE WINDOW PER PULL, and not because a constraint forced it: two pulls
        # of the SAME window on the same stream are the same unit of work, which
        # `uq_pull_jobs_active` says out loud. A fixture that reused one window
        # would be describing a history that cannot happen.
        window = (_NOW - timedelta(days=age_days)).date().isoformat()
        cur.execute(
            "INSERT INTO app.pull_jobs (id, pull_id, connection_ref_id, date_from,"
            " date_to, requested_by, datastream_id)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (_uid("job"), pull_id, conn_ref, window, window,
             "person_TEST", datastream_id),
        )
        cur.execute(
            "INSERT INTO app.pull_verifications (id, pull_id, connection_ref_id,"
            " expected_rows, actual_rows, completeness_ratio, verdict, verified_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
            (
                _uid("ver"), pull_id, conn_ref, 10,
                0 if verdict == "empty" else 10,
                0 if verdict == "empty" else 1,
                verdict,
                _NOW - timedelta(days=age_days),
            ),
        )


def _streaks_for(verdicts):
    """Seed one Datastream with *verdicts* and return its streak, or None."""
    from core.verification_streaks import read_empty_streaks  # noqa: PLC0415

    org_id, project_id = _uid("org"), _uid("proj")
    conn_ref, datastream_id = _uid("conn"), _uid("ds")
    conn = _conn()
    try:
        with conn.cursor() as cur:
            _seed(cur, org_id=org_id, project_id=project_id, conn_ref=conn_ref,
                  datastream_id=datastream_id, verdicts=verdicts)
        conn.commit()
        reading = read_empty_streaks(conn, min_streak=1, limit=500)
        found = [s for s in reading.streams if s.datastream_id == datastream_id]
    finally:
        conn.close()
    return (found[0] if found else None), datastream_id


def test_consecutive_empties_are_counted_and_dated_from_the_oldest():
    streak, _ = _streaks_for([("empty", 0), ("empty", 1), ("empty", 2)])
    assert streak is not None
    assert streak.empty_streak == 3
    assert streak.first_empty_at.date().isoformat() == "2026-08-15"
    assert streak.last_empty_at.date().isoformat() == "2026-08-17"


def test_a_good_pull_ends_the_run_and_an_older_streak_is_not_resurrected():
    """The two most recent are empty; an `ok` before them, then older empties.

    Counting all five would date the silence a week too early and tell a person
    the source has been quiet since a day on which it delivered.
    """
    streak, _ = _streaks_for(
        [("empty", 0), ("empty", 1), ("ok", 2), ("empty", 3), ("empty", 4)]
    )
    assert streak is not None
    assert streak.empty_streak == 2
    assert streak.first_empty_at.date().isoformat() == "2026-08-16"


def test_a_stream_that_has_always_been_empty_is_counted_whole():
    """No boundary exists, so every verdict it carries belongs to the run."""
    streak, _ = _streaks_for([("empty", 0), ("empty", 5), ("empty", 9)])
    assert streak is not None
    assert streak.empty_streak == 3
    assert streak.first_empty_at.date().isoformat() == "2026-08-08"


def test_a_stream_whose_last_pull_delivered_has_no_streak():
    streak, _ = _streaks_for([("ok", 0), ("empty", 1), ("empty", 2)])
    assert streak is None


def test_a_never_verified_stream_is_absent_not_a_zero():
    """"Never verified" and "verified and empty" are two facts, never one."""
    streak, _ = _streaks_for([])
    assert streak is None


def test_the_sentence_names_the_stream_the_count_and_the_date():
    streak, datastream_id = _streaks_for([("empty", 0), ("empty", 1)])
    assert streak is not None
    sentence = streak.describe()
    assert datastream_id in sentence
    assert "2" in sentence
    assert "2026-08-16" in sentence
    # It does NOT guess a cause: an empty pull is often a true fact about a quiet
    # account, and naming a culprit would invent the half nothing can see.
    for guess in ("broken", "failed", "error", "misconfigured"):
        assert guess not in sentence.lower()


def test_a_stream_that_produced_before_is_told_apart_from_one_that_never_did():
    """`never_produced` is the difference between broken and never wired.

    A stream that delivered and then went quiet BROKE; one that has only ever
    verified empty may never have worked at all. Reporting them with one sentence
    would send a reader looking for a regression that never happened.
    """
    broke, _ = _streaks_for([("empty", 0), ("empty", 1), ("ok", 5)])
    never, _ = _streaks_for([("empty", 0), ("empty", 1)])
    assert broke is not None and never is not None
    assert broke.never_produced is False
    assert never.never_produced is True
    assert "was producing before" in broke.describe()
    assert "never non-empty" in never.describe()
