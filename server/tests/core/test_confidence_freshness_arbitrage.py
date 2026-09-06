"""One report, one answer to "how fresh is this" (story 53.3, second pass).

Two functions answered that question in the same envelope, two lines apart in
`core/main.py`, and they answered it in OPPOSITE directions:

  * `core.health_enrichment._worst_health` -- the WORST connection decides;
  * `core.confidence._compute_freshness` -- `max(loaded_ats)`, the NEWEST load
    decides.

So one payload could carry `meta.freshness.stale_since` set AND
`meta.confidence.freshness = 1.0`. A reader who trusts the first is told the data
is frozen; a reader who trusts the second is told it is perfect. Nothing in the
suite noticed, because nothing asserted them together.

`docs/product-architecture/overview.md:61-63` arbitrates, and against the newest:
*"`Complete through` is the latest interval complete across every required active
input under policy, never the newest timestamp from one isolated source."* These
tests pin that arbitration and the two silent defaults it travelled with.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.confidence import _compute_freshness, _stalest_contributing_load


def _row(connector: str, loaded_at: str) -> dict:
    return {"connector": connector, "loaded_at": loaded_at, "pull_id": "p", "date": "2026-07-10"}


class TestTheStalestSourceDecides:
    def test_one_frozen_source_is_not_masked_by_a_fresh_one(self):
        """The defect, stated as a report: google-ads loaded today, meta-ads 30 days ago.

        Under `max(loaded_ats)` this scored 1.0 -- a figure adding a month-old
        source presented as perfectly fresh.
        """
        rows = [
            _row("google-ads", "2026-07-10T06:00:00+00:00"),
            _row("meta-ads", "2026-06-10T06:00:00+00:00"),
        ]
        assert _compute_freshness(rows, "2026-07-10") == 0.0

    def test_a_source_is_counted_at_its_own_newest_load(self):
        """Three loads from one source: it is as fresh as its last one, not its first.

        The naive repair -- `min` over every row -- would score this 0.0 and
        punish a source for having reported more than once.
        """
        rows = [
            _row("google-ads", "2026-06-01T06:00:00+00:00"),
            _row("google-ads", "2026-07-01T06:00:00+00:00"),
            _row("google-ads", "2026-07-10T06:00:00+00:00"),
        ]
        assert _compute_freshness(rows, "2026-07-10") == 1.0

    def test_the_stalest_load_is_the_one_returned(self):
        rows = [
            _row("a", "2026-07-10T06:00:00+00:00"),
            _row("b", "2026-07-08T06:00:00+00:00"),
            _row("b", "2026-07-01T06:00:00+00:00"),
        ]
        assert _stalest_contributing_load(rows) == datetime(
            2026, 7, 8, 6, 0, tzinfo=timezone.utc
        )

    def test_it_agrees_with_the_envelope_builder_on_the_same_rows(self):
        """`core.envelope` already published this rule as `complete_through`.

        Two modules deriving freshness from the same rows must not disagree; that
        disagreement is the whole subject of this file.
        """
        from core.envelope import derive_meta_from_rows

        rows = [
            _row("google-ads", "2026-07-10T06:00:00+00:00"),
            _row("meta-ads", "2026-06-10T06:00:00+00:00"),
        ]
        meta_freshness, _ = derive_meta_from_rows(rows, ["google-ads", "meta-ads"])
        stalest = _stalest_contributing_load(rows)

        assert stalest is not None
        assert meta_freshness["complete_through"].startswith(stalest.date().isoformat())


class TestTheHealthVerdictAndTheTermCannotDisagree:
    def test_stale_since_caps_the_freshness_term(self):
        """A connection that fetched nothing since T cannot have loaded rows after T.

        Rows claim a load on the report's end date; connection health says the
        source has been frozen for a month. The report used to carry both, at
        face value, in one envelope.
        """
        rows = [_row("google-ads", "2026-07-10T06:00:00+00:00")]

        assert _compute_freshness(rows, "2026-07-10") == 1.0
        assert (
            _compute_freshness(rows, "2026-07-10", stale_since="2026-06-01T00:00:00+00:00")
            == 0.0
        )

    def test_a_health_verdict_newer_than_the_rows_does_not_flatter_them(self):
        """The cap only ever makes the term worse. The stalest input decides."""
        rows = [_row("google-ads", "2026-06-01T06:00:00+00:00")]
        capped = _compute_freshness(
            rows, "2026-07-10", stale_since="2026-07-10T06:00:00+00:00"
        )
        assert capped == _compute_freshness(rows, "2026-07-10") == 0.0

    def test_an_unreadable_stale_since_is_ignored_not_treated_as_now(self):
        rows = [_row("google-ads", "2026-07-10T06:00:00+00:00")]
        assert _compute_freshness(rows, "2026-07-10", stale_since="never") == 1.0


class TestAWindowThatCannotBeReadIsNotToday:
    """`date_to` used to fall back to `datetime.now()`, and now is always fresh.

    A report whose window could not be read therefore scored MAXIMUM freshness --
    a number where the fact was unknown, which `overview.md:38` forbids: missing
    evidence is `Unknown` or `Unavailable`, never Healthy.
    """

    def test_an_unparsable_end_date_is_unknown_not_perfect(self):
        rows = [
            {
                "connector": "google-ads",
                "loaded_at": datetime.now(tz=timezone.utc).isoformat(),
                "pull_id": "p",
            }
        ]
        assert _compute_freshness(rows, "not-a-date") is None

    def test_an_absent_end_date_is_unknown_not_perfect(self):
        rows = [
            {
                "connector": "google-ads",
                "loaded_at": datetime.now(tz=timezone.utc).isoformat(),
                "pull_id": "p",
            }
        ]
        assert _compute_freshness(rows, None) is None

    def test_the_old_default_scored_an_unreadable_window_perfect(self):
        """The regression, measured: the fallback made the answer 1.0 every time.

        `datetime.now()` is by construction close to a recent load, so ANY report
        with recent rows and an unreadable window scored maximum freshness. This
        pins the two answers apart -- what the rejected code returned, and what an
        unknown window is worth.
        """
        an_hour_ago = (datetime.now(tz=timezone.utc) - timedelta(hours=1)).isoformat()
        rows = [{"connector": "google-ads", "loaded_at": an_hour_ago, "pull_id": "p"}]

        today = datetime.now(tz=timezone.utc).date().isoformat()
        assert _compute_freshness(rows, today) == 1.0, "the rejected fallback's answer"
        assert _compute_freshness(rows, "not-a-date") is None, "the honest answer"

    def test_a_load_after_the_window_ended_is_not_penalised(self):
        """Data loaded after the period closed is fresh data about that period.

        Pinned because the sign convention is the reason the test above cannot be
        written as "stale against an old window": age is how far the load is
        BEHIND the window end, and a negative age is not staleness.
        """
        rows = [_row("google-ads", "2026-07-20T06:00:00+00:00")]
        assert _compute_freshness(rows, "2026-07-10") == 1.0
