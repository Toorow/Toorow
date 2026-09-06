"""AI-101: a verdict, read per Datastream -- on a live Postgres.

WHAT ONLY A DATABASE CAN PROVE HERE. The whole subject of
`core/verification_streaks.py` is ONE SQL statement: the gaps-and-islands
predicate that keeps the CURRENT run of `empty` verdicts and drops every older
one. A mocked cursor agrees with any predicate -- it returns the rows the test
already decided on -- so mocking this would assert nothing about the only thing
that can be wrong.

Five facts, each of which the query gets right or wrong on its own:

1. **A stream that went quiet and STAYED quiet is reported**, with the run length
   and the date the run started -- not the date of the oldest empty verdict it
   ever had.
2. **A stream that recovered is ABSENT.** Empty in June, `ok` since: the LEFT
   JOIN on the last non-empty verdict is what drops it, and an off-by-one on
   that comparison would report every stream that was ever briefly empty.
3. **`partial` breaks a streak.** The verdict enum is `ok | partial | empty`
   (migration 007); a stream landing few rows is still producing, and folding
   `partial` into emptiness would name the wrong sources.
4. **A stream that NEVER produced is distinguishable** from one that broke:
   `last_non_empty_at IS NULL` survives the LEFT JOIN, which is also the row the
   predicate must not drop.
5. **A verdict whose job carries no `datastream_id` is COUNTED, not silently
   omitted.** `pull_jobs.datastream_id` is nullable (migration 023), so an empty
   `streams` list can mean "nothing is quiet" or "nothing is attributable", and
   those two must never arrive as the same silence.

The reading is GLOBAL by design -- it answers "which streams are quiet", not
"which streams of project X". So every assertion here is a DELTA against a
baseline taken before the fixture writes, never an absolute count: the
disposable base carries other sessions' rows and an absolute assertion would be
green or red for reasons that have nothing to do with this query.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from tests.conftest import purge_fixture_project

TEST_ORG_ID = "org_test_fixture"

#: A fixed clock. The query orders and compares on `verified_at`, so the fixture
#: must own those instants exactly rather than inherit "now".
T0 = datetime(2026, 7, 1, 6, 0, tzinfo=timezone.utc)

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the streak query needs a live Postgres",
)

pytestmark = [_skip_without_dsn, pytest.mark.pg_owner]


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _at(days: int) -> datetime:
    return T0 + timedelta(days=days)


@pytest.fixture
def streaks_fixture(live_postgres):
    """One project, four Datastreams, one unattributed pull -- and a baseline.

    Stream layout (verdicts oldest -> newest):

      quiet      : ok(d0), empty(d1), empty(d2), empty(d3)   -> streak 3 from d1
      recovered  : empty(d1), empty(d2), ok(d3)              -> absent
      partial    : empty(d1), partial(d2), empty(d3)         -> streak 1 from d3
      never      : empty(d1), empty(d2)                      -> streak 2, no ok ever

    Plus one `empty` verification on a pull job with `datastream_id` NULL, which
    belongs to no stream and must show up in the blind-spot count instead.
    """
    conn = live_postgres

    project_id = _id("proj_")
    connection_id = _id("cref_")
    streams = {
        "quiet": _id("ds_"),
        "recovered": _id("ds_"),
        "partial": _id("ds_"),
        "never": _id("ds_"),
    }
    pull_ids: list[str] = []

    # The baseline: what the base already says before this fixture writes a row.
    from core.verification_streaks import read_empty_streaks  # noqa: PLC0415

    baseline = read_empty_streaks(conn, limit=10_000)

    def _seed_pull(datastream_id: str | None, verdict: str, when: datetime) -> None:
        job_id = _id("job_")
        pull_id = _id("pull_")
        pull_ids.append(pull_id)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.pull_jobs
                    (id, pull_id, connection_ref_id, datastream_id, date_from, date_to,
                     state, requested_by)
                VALUES (%s, %s, %s, %s, DATE '2026-06-01', DATE '2026-06-30',
                        'done', 'ai-101-test')
                """,
                (job_id, pull_id, connection_id, datastream_id),
            )
            cur.execute(
                """
                INSERT INTO app.pull_verifications
                    (id, pull_id, connection_ref_id, expected_rows, actual_rows,
                     completeness_ratio, verdict, verified_at)
                VALUES (%s, %s, %s, 100, %s, %s, %s, %s)
                """,
                (
                    _id("ver_"),
                    pull_id,
                    connection_id,
                    0 if verdict == "empty" else 100,
                    0.0 if verdict == "empty" else (0.2 if verdict == "partial" else 1.0),
                    verdict,
                    when,
                ),
            )

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'ai-101-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "  (id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
            "VALUES (%s, 'ga4', %s, %s, %s, 'owner@example.com')",
            (connection_id, connection_id, project_id, TEST_ORG_ID),
        )
        for label, stream_id in streams.items():
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, module_name, connection_ref_id,
                     enabled, schedule_mode, created_by, org_id)
                VALUES (%s, %s, %s, 'ga4', %s, TRUE, 'nightly', 'test', %s)
                """,
                (stream_id, project_id, f"AI-101 {label}", connection_id, TEST_ORG_ID),
            )

    _seed_pull(streams["quiet"], "ok", _at(0))
    _seed_pull(streams["quiet"], "empty", _at(1))
    _seed_pull(streams["quiet"], "empty", _at(2))
    _seed_pull(streams["quiet"], "empty", _at(3))

    _seed_pull(streams["recovered"], "empty", _at(1))
    _seed_pull(streams["recovered"], "empty", _at(2))
    _seed_pull(streams["recovered"], "ok", _at(3))

    _seed_pull(streams["partial"], "empty", _at(1))
    _seed_pull(streams["partial"], "partial", _at(2))
    _seed_pull(streams["partial"], "empty", _at(3))

    _seed_pull(streams["never"], "empty", _at(1))
    _seed_pull(streams["never"], "empty", _at(2))

    # The blind spot: a verdict that belongs to no stream.
    _seed_pull(None, "empty", _at(2))

    conn.commit()

    yield conn, project_id, streams, baseline

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.pull_verifications WHERE pull_id = ANY(%s)", (pull_ids,)
        )
        cur.execute("DELETE FROM app.pull_jobs WHERE pull_id = ANY(%s)", (pull_ids,))
        cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
        cur.execute(
            "DELETE FROM app.connection_ref WHERE id = %s", (connection_id,)
        )
    # A hand-written DELETE list loses the race with the next migration -- AI-291
    # measured it on `project_capabilities`, seeded for every project.
    purge_fixture_project(conn, project_id)
    conn.commit()


def _by_id(reading, streams: dict[str, str]) -> dict[str, object]:
    """The fixture's streams only, keyed by label -- the base carries others."""
    index = {stream.datastream_id: stream for stream in reading.streams}
    return {
        label: index[stream_id]
        for label, stream_id in streams.items()
        if stream_id in index
    }


class TestCurrentStreak:
    def test_quiet_stream_reports_run_length_and_the_date_it_went_quiet(
        self, streaks_fixture
    ):
        """Fact 1: three empties after an ok -> streak 3, dated from the first."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, _baseline = streaks_fixture
        found = _by_id(read_empty_streaks(conn, limit=10_000), streams)

        quiet = found["quiet"]
        assert quiet.empty_streak == 3
        assert quiet.first_empty_at == _at(1)
        assert quiet.last_empty_at == _at(3)
        assert quiet.last_non_empty_at == _at(0)
        assert quiet.never_produced is False
        assert quiet.module_name == "ga4"
        assert quiet.enabled is True

    def test_recovered_stream_is_absent(self, streaks_fixture):
        """Fact 2: empty, empty, then ok -- the stream is not quiet at all."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, _baseline = streaks_fixture
        found = _by_id(read_empty_streaks(conn, limit=10_000), streams)

        assert "recovered" not in found

    def test_partial_breaks_the_streak(self, streaks_fixture):
        """Fact 3: a stream landing few rows is producing -- the run restarts."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, _baseline = streaks_fixture
        found = _by_id(read_empty_streaks(conn, limit=10_000), streams)

        partial = found["partial"]
        assert partial.empty_streak == 1
        assert partial.first_empty_at == _at(3)
        assert partial.last_non_empty_at == _at(2)

    def test_a_stream_that_never_produced_is_distinguishable(self, streaks_fixture):
        """Fact 4: no non-empty verdict ever is not the same as 'it broke'."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, _baseline = streaks_fixture
        found = _by_id(read_empty_streaks(conn, limit=10_000), streams)

        never = found["never"]
        assert never.empty_streak == 2
        assert never.first_empty_at == _at(1)
        assert never.last_non_empty_at is None
        assert never.never_produced is True

    def test_min_streak_filters_without_deciding_anything(self, streaks_fixture):
        """`min_streak` is a caller's filter, not a policy this module applies."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, _baseline = streaks_fixture
        found = _by_id(read_empty_streaks(conn, min_streak=2, limit=10_000), streams)

        assert set(found) == {"quiet", "never"}  # partial is at 1, recovered absent


class TestBlindSpot:
    def test_unattributed_empty_pull_is_counted_not_dropped(self, streaks_fixture):
        """Fact 5: a verdict with no `datastream_id` reaches the reader as a count."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, baseline = streaks_fixture
        reading = read_empty_streaks(conn, limit=10_000)

        delta = reading.unattributed_empty_pulls - baseline.unattributed_empty_pulls
        assert delta == 1
        # ... and it went to NONE of the streams: of the nine empty verdicts the
        # fixture attached to a stream, six are in a current run (3 + 1 + 2) --
        # the other three sit behind a later ok or partial.
        found = _by_id(reading, streams)
        assert sum(stream.empty_streak for stream in found.values()) == 6

    def test_describe_names_every_stream_and_the_blind_spot(self, streaks_fixture):
        """The sentence an alert carries says which stream and since when."""
        from core.verification_streaks import read_empty_streaks

        conn, _project_id, streams, _baseline = streaks_fixture
        sentence = read_empty_streaks(conn, limit=10_000).describe()

        assert streams["quiet"] in sentence
        assert "2026-07-02" in sentence  # _at(1), the day it went quiet
        assert "not attributed" in sentence
        assert sentence.isascii()  # AI-03
