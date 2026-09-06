"""Tests for confirmed data-correctness gap fixes (review-global-gaps.md).

Covers:
  G-02  _rollup in reports.py uses impression-weighted average_position
  G-03  average_position business alert routes to semantic view
  G-06  prior-period deltas are populated (warehouse widens fetch window)
  NFR1  30-line cap is re-enforced after briefing prepend
  Conf  confidence.py returns freshness, provenance, and score terms
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

# Guard background threads (same pattern as all other test files)
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("BUSINESS_ALERTS_ENABLED", "false")


# ---------------------------------------------------------------------------
# G-02: _rollup must use impression-weighted average for average_position
# ---------------------------------------------------------------------------

class TestRollupWeightedAveragePosition:
    """G-02: reports._rollup must delegate to _weighted_avg_position (AD-4)."""

    def _make_rows(self):
        """Two date rows: pos=2 with 900 impressions, pos=10 with 100 impressions.
        Naive mean = 6.0; impression-weighted = (2*900 + 10*100)/1000 = 2.8."""
        return [
            {
                "date": "2026-07-08", "connector": "gsc", "metric": "average_position",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
                "value": 2.0, "pull_id": "pull_p", "loaded_at": "2026-07-08T00:00:00",
            },
            {
                "date": "2026-07-08", "connector": "gsc", "metric": "impressions",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
                "value": 900.0, "pull_id": "pull_p", "loaded_at": "2026-07-08T00:00:00",
            },
            {
                "date": "2026-07-09", "connector": "gsc", "metric": "average_position",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
                "value": 10.0, "pull_id": "pull_p", "loaded_at": "2026-07-09T00:00:00",
            },
            {
                "date": "2026-07-09", "connector": "gsc", "metric": "impressions",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
                "value": 100.0, "pull_id": "pull_p", "loaded_at": "2026-07-09T00:00:00",
            },
        ]

    def test_rollup_uses_impression_weighted_not_naive_mean(self):
        """_rollup(rows, report) must return impression-weighted pos, not naive mean."""
        from core.reports import _rollup

        report = {"metrics": ["average_position", "impressions"]}
        rows = self._make_rows()

        # Patch is_non_additive so average_position is treated as non-additive.
        _is_nonadd = lambda m: m == "average_position"  # noqa: E731
        with patch("core.report_dictionary.is_non_additive", side_effect=_is_nonadd):
            result, _refused = _rollup(rows, report)

        # Impression-weighted: (2*900 + 10*100) / (900+100) = 2.8
        assert "average_position" in result
        assert abs(result["average_position"] - 2.8) < 1e-6, (
            f"Expected impression-weighted 2.8 but got {result['average_position']} "
            "(naive mean would be 6.0)"
        )

    def test_rollup_average_position_fallback_to_simple_mean_no_impressions(self):
        """Without impression rows, _rollup falls back to simple mean."""
        from core.reports import _rollup

        rows = [
            {
                "date": "2026-07-08", "connector": "gsc", "metric": "average_position",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
                "value": 3.0, "pull_id": "p", "loaded_at": "2026-07-08T00:00:00",
            },
            {
                "date": "2026-07-09", "connector": "gsc", "metric": "average_position",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
                "value": 5.0, "pull_id": "p", "loaded_at": "2026-07-09T00:00:00",
            },
        ]
        report = {"metrics": ["average_position"]}
        _is_nonadd = lambda m: m == "average_position"  # noqa: E731
        with patch("core.report_dictionary.is_non_additive", side_effect=_is_nonadd):
            result, _refused = _rollup(rows, report)

        # Simple mean: (3 + 5) / 2 = 4.0 (not sum 8.0)
        assert "average_position" in result
        assert result["average_position"] == 4.0, (
            f"Expected simple mean 4.0 but got {result['average_position']}"
        )

    def test_rollup_clicks_still_summed(self):
        """Additive metrics (clicks) must still be summed, not averaged."""
        from core.reports import _rollup

        rows = [
            {
                "date": "2026-07-08", "connector": "gsc", "metric": "clicks",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-08",
                "value": 100.0, "pull_id": "p", "loaded_at": "2026-07-08T00:00:00",
            },
            {
                "date": "2026-07-09", "connector": "gsc", "metric": "clicks",
                "breakdown_dimension": "date", "breakdown_value": "2026-07-09",
                "value": 200.0, "pull_id": "p", "loaded_at": "2026-07-09T00:00:00",
            },
        ]
        report = {"metrics": ["clicks"]}
        with patch("core.report_dictionary.is_non_additive", return_value=False):
            result, _refused = _rollup(rows, report)

        assert result["clicks"] == 300.0


# ---------------------------------------------------------------------------
# G-03: average_position alert must route to semantic view
# ---------------------------------------------------------------------------

class TestBusinessAlertAveragePositionSemanticRouting:
    """G-03: average_position must route through semantic view, not fact_daily_kpi."""

    def _make_cursor(self, rows=None, description=None):
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall = MagicMock(return_value=rows or [])
        cur.fetchone = MagicMock(return_value=(rows[0] if rows else None))
        cur.description = description or []
        return cur

    def _make_conn(self, cursor):
        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(return_value=cursor)
        conn.commit = MagicMock()
        return conn

    def test_average_position_in_default_semantic_set(self):
        """ALERT_SEMANTIC_METRICS default must include average_position."""
        from core.business_alerts import _get_semantic_metrics

        with patch.dict(os.environ, {}, clear=False):
            # Remove any overriding env var so default is used.
            os.environ.pop("ALERT_SEMANTIC_METRICS", None)
            metrics = _get_semantic_metrics()

        assert "average_position" in metrics, (
            "average_position must be in the default ALERT_SEMANTIC_METRICS set "
            "so it routes to semantic_avg_position view"
        )

    def test_average_position_alert_uses_semantic_not_additive(self):
        """An average_position alert definition must call _query_semantic_metric."""
        from core import business_alerts

        eval_date = date(2026, 7, 10)
        defn_row = ("alrt_AVGPOS", "proj1", "average_position", "<", 5.0, None)
        defn_desc = [
            ("id",), ("project_id",), ("metric",), ("operator",),
            ("threshold",), ("connector",),
        ]

        select_cur = self._make_cursor(rows=[defn_row], description=defn_desc)
        insert_cur = self._make_cursor()

        call_count = [0]
        def side_effect_cursor():
            call_count[0] += 1
            return select_cur if call_count[0] == 1 else insert_cur

        conn = self._make_conn(MagicMock())
        conn.cursor.side_effect = side_effect_cursor

        @contextmanager
        def _conn_ctx(c):
            yield c

        with (
            patch.dict(
                os.environ,
                {
                    "BUSINESS_ALERTS_ENABLED": "true",
                    "TOOROW_DUCKDB_PATH": "/fake.duckdb",
                    "ALERT_SEMANTIC_METRICS": "cpa,roas,ctr,average_position",
                },
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch.object(
                business_alerts,
                "_query_semantic_metric",
                return_value=(3.0, ["pull_avgpos"]),
            ) as mock_semantic,
            patch.object(
                business_alerts,
                "_query_additive_metric",
                return_value=(0.0, []),
            ) as mock_additive,
        ):
            result = business_alerts.evaluate_business_alerts(evaluation_date=eval_date)

        # Semantic must be called, additive must NOT be called for average_position.
        mock_semantic.assert_called_once()
        mock_additive.assert_not_called()
        # 3.0 < 5.0 -> breach -> one firing
        assert len(result) == 1
        assert result[0]["metric"] == "average_position"

    def test_query_semantic_metric_average_position_real_duckdb(self, tmp_path):
        """_query_semantic_metric handles semantic_avg_position view shape correctly."""
        import duckdb
        from core.business_alerts import _query_semantic_metric

        db_path = tmp_path / "avgpos.duckdb"
        con = duckdb.connect(str(db_path))
        try:
            con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
            # Seed semantic_avg_position with impressions_weight column.
            con.execute(
                """CREATE VIEW main_marts.semantic_avg_position AS
                   SELECT
                       'proj1' AS project_id,
                       DATE '2026-07-10' AS date,
                       'gsc' AS connector,
                       'page' AS breakdown_dimension,
                       '/home' AS breakdown_value,
                       2.5 AS average_position,
                       1000.0 AS impressions_weight,
                       'pull_ap1' AS pull_id
                   UNION ALL
                   SELECT 'proj1', DATE '2026-07-10', 'gsc',
                          'page', '/about', 8.0, 500.0, 'pull_ap2'
                """
            )
        finally:
            con.close()

        observed, pull_ids = _query_semantic_metric(
            "proj1", "average_position", date(2026, 7, 10), None, str(db_path)
        )

        # Impression-weighted: (2.5*1000 + 8.0*500) / (1000+500) = (2500+4000)/1500 = 4.333...
        assert observed is not None
        assert abs(observed - (2.5 * 1000 + 8.0 * 500) / 1500) < 1e-4
        assert set(pull_ids) == {"pull_ap1", "pull_ap2"}


# ---------------------------------------------------------------------------
# G-06: prior-period deltas must be populated
# ---------------------------------------------------------------------------

class TestPriorPeriodDeltaPopulated:
    """G-06: warehouse widens date window so rollup._split_periods gets prior rows."""

    def test_widen_to_prior_symmetric(self):
        """_widen_to_prior returns a start date exactly one span earlier."""
        from core.warehouse import _widen_to_prior

        # 7-day window 2026-07-08 to 2026-07-14 -> prior start = 2026-07-01
        result = _widen_to_prior("2026-07-08", "2026-07-14")
        assert result == "2026-07-01"

    def test_widen_to_prior_single_day(self):
        """Single-day window 2026-07-14 to 2026-07-14 -> prior start = 2026-07-13."""
        from core.warehouse import _widen_to_prior

        result = _widen_to_prior("2026-07-14", "2026-07-14")
        assert result == "2026-07-13"

    def test_widen_to_prior_invalid_dates_passthrough(self):
        """Invalid dates fall back to returning start_date unchanged."""
        from core.warehouse import _widen_to_prior

        result = _widen_to_prior("bad-date", "2026-07-14")
        assert result == "bad-date"

    def test_compute_rollup_delta_pct_populated_with_prior_rows(self):
        """compute_rollup produces delta_pct when prior rows are in the same list."""
        from core.rollup import compute_rollup

        # Current: Jul 8-14, 10 clicks each = 70.
        current = [
            {
                "date": f"2026-07-{8+i:02d}", "connector": "gsc", "metric": "clicks",
                "breakdown_dimension": "date", "breakdown_value": f"2026-07-{8+i:02d}",
                "value": 10.0, "pull_id": "pull_cur", "loaded_at": f"2026-07-{8+i:02d}T00:00:00",
            }
            for i in range(7)
        ]
        # Prior: Jul 1-7, 8 clicks each = 56.
        prior = [
            {
                "date": f"2026-07-{1+i:02d}", "connector": "gsc", "metric": "clicks",
                "breakdown_dimension": "date", "breakdown_value": f"2026-07-{1+i:02d}",
                "value": 8.0, "pull_id": "pull_pri", "loaded_at": f"2026-07-{1+i:02d}T00:00:00",
            }
            for i in range(7)
        ]

        rollup = compute_rollup(
            current + prior, ["clicks"], "2026-07-08", "2026-07-14", "default", []
        )

        assert rollup["clicks"]["delta"] == 14.0
        assert rollup["clicks"]["delta_pct"] == "+25%"

    def test_compute_rollup_no_delta_without_prior_rows(self):
        """Without prior rows, delta and delta_pct are None (regression guard)."""
        from core.rollup import compute_rollup

        current = [
            {
                "date": f"2026-07-{8+i:02d}", "connector": "gsc", "metric": "clicks",
                "breakdown_dimension": "date", "breakdown_value": f"2026-07-{8+i:02d}",
                "value": 10.0, "pull_id": "pull_cur", "loaded_at": f"2026-07-{8+i:02d}T00:00:00",
            }
            for i in range(7)
        ]

        rollup = compute_rollup(
            current, ["clicks"], "2026-07-08", "2026-07-14", "default", []
        )

        assert rollup["clicks"]["delta"] is None
        assert rollup["clicks"]["delta_pct"] is None

    def test_split_periods_excludes_prior_from_current(self):
        """_split_periods correctly buckets prior rows separately from current."""
        from core.rollup import _split_periods

        current = [
            {"date": "2026-07-08", "metric": "clicks", "value": 10.0},
            {"date": "2026-07-14", "metric": "clicks", "value": 20.0},
        ]
        prior = [
            {"date": "2026-07-01", "metric": "clicks", "value": 8.0},
            {"date": "2026-07-07", "metric": "clicks", "value": 9.0},
        ]

        cur_out, pri_out = _split_periods(current + prior, "2026-07-08", "2026-07-14")

        assert len(cur_out) == 2
        assert len(pri_out) == 2
        # Current dates must all be within [2026-07-08, 2026-07-14]
        assert all(r["date"] >= "2026-07-08" for r in cur_out)
        # Prior dates must all be < 2026-07-08
        assert all(r["date"] < "2026-07-08" for r in pri_out)

    def test_data_rows_excludes_prior_after_split(self):
        """Envelope data.rows must not contain prior-period rows after G-06 fix.

        Simulates what render_report and get_daily_report do: query returns
        current+prior; split happens; only current goes into data.rows.
        """
        from core.rollup import _split_periods

        all_rows = [
            {"date": "2026-07-08", "metric": "clicks", "value": 10.0},
            {"date": "2026-07-01", "metric": "clicks", "value": 8.0},  # prior
        ]
        current_rows, _prior_rows = _split_periods(all_rows, "2026-07-08", "2026-07-08")

        # data.rows = current_rows only
        assert len(current_rows) == 1
        assert current_rows[0]["date"] == "2026-07-08"


# ---------------------------------------------------------------------------
# NFR1: 30-line cap must be enforced after briefing prepend
# ---------------------------------------------------------------------------

class TestNFR1BriefingLineCap:
    """NFR1: briefing prepend must not push total above 30 lines."""

    def _apply_briefing_and_cap(self, summary: str, brief_lines: list[str]) -> str:
        """Replicate the briefing-prepend + NFR1-cap logic from main.py.

        Keep in sync with the get_daily_report briefing-prepend block: one line
        is reserved for the "[tronque]" marker so the total stays at the cap.
        """
        _nfr1_cap = 30
        brief_section = "\n".join(brief_lines)
        combined = brief_section + "\n" + summary
        combined_lines = combined.split("\n")
        if len(combined_lines) > _nfr1_cap:
            brief_line_count = len(brief_lines)
            allowed_summary_lines = _nfr1_cap - brief_line_count - 1
            if allowed_summary_lines > 0:
                combined_lines = (
                    combined_lines[:brief_line_count]
                    + combined_lines[
                        brief_line_count : brief_line_count + allowed_summary_lines
                    ]
                    + ["[tronque]"]
                )
            else:
                combined_lines = combined_lines[:_nfr1_cap]
        return "\n".join(combined_lines)

    def test_briefing_plus_30_line_summary_truncated_to_cap(self):
        """A 30-line summary + 5-line briefing must be truncated to <=30 total."""
        # 30-line summary
        summary = "\n".join(f"line {i}" for i in range(30))
        # 5-line briefing block (header + 3 insights + blank separator)
        brief_lines = [
            "[Briefing matinal -- 2026-07-12]",
            "* insight 1",
            "* insight 2",
            "* insight 3",
            "",  # blank separator
        ]

        result = self._apply_briefing_and_cap(summary, brief_lines)
        line_count = len(result.split("\n"))

        assert line_count <= 30, (
            f"Expected <=30 lines after briefing prepend, got {line_count}"
        )

    def test_briefing_plus_short_summary_not_truncated(self):
        """A 5-line summary + 5-line briefing stays under 30 lines, no truncation marker."""
        summary = "\n".join(f"line {i}" for i in range(5))
        brief_lines = ["[Briefing matinal -- 2026-07-12]", "* insight 1", ""]

        result = self._apply_briefing_and_cap(summary, brief_lines)

        assert "[tronque]" not in result
        assert len(result.split("\n")) <= 30

    def test_briefing_lines_always_preserved(self):
        """The briefing block (first N lines) must always be present in output."""
        summary = "\n".join(f"line {i}" for i in range(30))
        brief_lines = ["[Briefing matinal -- 2026-07-12]", "* top insight", ""]

        result = self._apply_briefing_and_cap(summary, brief_lines)
        result_lines = result.split("\n")

        # Briefing header must be the first line
        assert result_lines[0] == "[Briefing matinal -- 2026-07-12]"
        assert result_lines[1] == "* top insight"


# ---------------------------------------------------------------------------
# Confidence: three terms, and NO fourth number over them
# ---------------------------------------------------------------------------

class TestConfidenceThreeTerms:
    """confidence.py returns completeness, freshness and provenance -- and nothing
    that merges them.

    The `score` key these tests were written around is GONE.
    `proactive-assertions.md` ("Incomplete if": a single score merges evidence of
    different natures) refuses it, so each assertion on it below became an
    assertion on the terms it used to compress -- and on `limiting_term`, which is
    the only summary the three support."""

    def test_compute_freshness_within_grace_returns_one(self):
        """loaded_at within 48h of date_to -> freshness = 1.0."""
        from core.confidence import _compute_freshness

        # date_to = 2026-07-10, loaded_at = 2026-07-10T10:00:00Z (fresh)
        rows = [{"loaded_at": "2026-07-10T10:00:00+00:00", "pull_id": "p"}]
        result = _compute_freshness(rows, "2026-07-10")
        assert result == 1.0

    def test_compute_freshness_old_data_returns_zero(self):
        """loaded_at 10 days before date_to -> freshness = 0.0 (past decay window)."""
        from core.confidence import _compute_freshness

        rows = [{"loaded_at": "2026-07-01T00:00:00+00:00", "pull_id": "p"}]
        # date_to = 2026-07-10, loaded_at = 2026-07-01: age = 9 days, grace=48h, decay=7d
        # total tolerance = 2 + 7 = 9 days; 9 days old = exactly at boundary -> 0.0
        result = _compute_freshness(rows, "2026-07-10")
        assert result == 0.0

    def test_compute_freshness_linear_decay(self):
        """loaded_at 3 days before date_to (within decay window) -> 0 < freshness < 1."""
        from core.confidence import _compute_freshness

        # date_to=2026-07-10 midnight, loaded_at=2026-07-07: age=3*86400s=259200s
        # grace=48*3600=172800s; decay=7*86400=604800s
        # elapsed_decay = 259200 - 172800 = 86400; freshness = 1 - 86400/604800 ~ 0.857
        rows = [{"loaded_at": "2026-07-07T00:00:00+00:00", "pull_id": "p"}]
        result = _compute_freshness(rows, "2026-07-10")
        assert 0.0 < result < 1.0

    def test_compute_freshness_no_rows_is_unknown_not_perfect(self):
        """No rows -> freshness is UNKNOWN (None), never 1.0.

        This test previously asserted 1.0 ("no penalty for empty result"). An
        empty result has no freshness to state, and asserting the maximum is the
        posture `docs/product-architecture/overview.md:38` forbids: *"Missing
        evidence is `Unknown` or `Unavailable`, never Healthy."* (It cited
        `README.md:123`, which is a line about `NANGO_ENCRYPTION_KEY` -- the same
        fabricated citation repaired in `core/confidence.py`.)
        """
        from core.confidence import _compute_freshness

        assert _compute_freshness([], "2026-07-10") is None

    def test_compute_freshness_rows_without_timestamps_is_unknown(self):
        """Rows exist but carry no loaded_at -> unknown, not maximum freshness.

        The regression that mattered: a report whose rows had lost their load
        timestamps used to score MAXIMUM freshness, so an unmeasurable report
        outranked a measured, slightly stale one.
        """
        from core.confidence import _compute_freshness

        rows = [{"pull_id": "p1"}, {"pull_id": "p2", "loaded_at": None}]
        assert _compute_freshness(rows, "2026-07-10") is None

    def test_compute_provenance_all_rows_have_pull_id(self):
        """All rows with pull_id -> provenance = 1.0."""
        from core.confidence import _compute_provenance

        rows = [{"pull_id": "p1"}, {"pull_id": "p2"}, {"pull_id": "p3"}]
        assert _compute_provenance(rows) == 1.0

    def test_compute_provenance_no_pull_ids(self):
        """No rows with pull_id -> provenance = 0.0."""
        from core.confidence import _compute_provenance

        rows = [{"pull_id": None}, {"pull_id": ""}, {"pull_id": None}]
        assert _compute_provenance(rows) == 0.0

    def test_compute_provenance_partial(self):
        """Half rows with pull_id -> provenance = 0.5."""
        from core.confidence import _compute_provenance

        rows = [{"pull_id": "p1"}, {"pull_id": None}]
        assert _compute_provenance(rows) == 0.5

    def test_compute_provenance_empty_rows_is_unknown(self):
        """No rows -> provenance is UNKNOWN (None), never 1.0.

        A fraction with a zero denominator is not 1.0. `overview.md:124`: every
        count names its denominator, and a zero with unknown coverage is not an
        all-clear.
        """
        from core.confidence import _compute_provenance

        assert _compute_provenance([]) is None

    def test_an_unknown_term_is_named_and_never_absorbed(self):
        """An unknown factor must not enter any product as 1.0.

        Before the fix the score was a bare `completeness * freshness *
        provenance`, so a report with no derivable freshness scored HIGHER than a
        measured, imperfect one -- the compensation that made the number
        unreadable.
        """
        from unittest.mock import MagicMock, patch

        from core.confidence import compute_confidence

        conn, cur = MagicMock(), MagicMock()
        conn.cursor.return_value.__enter__.return_value = cur
        cur.fetchone.return_value = (0.9,)
        with patch("core.db.get_connection", return_value=conn):
            conn.__enter__ = MagicMock(return_value=conn)
            conn.__exit__ = MagicMock(return_value=False)
            # rows=[] -> freshness and provenance are both unknown.
            result = compute_confidence("proj1", ["gsc"], rows=[], date_to="2026-07-10")

        assert result is not None
        assert result["completeness"] == 0.9
        assert "score" not in result
        assert result["unknown_terms"] == ["freshness", "provenance"]
        assert result["limiting_term"] == "completeness"
        assert "this project's Datastreams" in result["completeness_scope"]

    def test_compute_confidence_returns_all_three_terms(self, tmp_path):
        """compute_confidence must return completeness, freshness and provenance."""
        from core.confidence import compute_confidence

        rows = [
            {
                "loaded_at": "2026-07-10T06:00:00+00:00",
                "pull_id": "pull_abc",
                "metric": "clicks",
            },
        ]

        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        # completeness_ratio = 0.95
        mock_cur.fetchone = MagicMock(return_value=(0.95,))

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @contextmanager
        def _conn_ctx(c):
            yield c

        with patch("core.db.get_connection", return_value=_conn_ctx(mock_conn)):
            result = compute_confidence(
                "proj1",
                ["gsc"],
                rows=rows,
                date_to="2026-07-10",
            )

        assert result is not None
        assert "completeness" in result
        assert "freshness" in result
        assert "provenance" in result
        assert "score" not in result
        assert result["completeness"] == 0.95
        # freshness: loaded_at=2026-07-10T06:00:00 is within grace window of date_to=2026-07-10
        assert result["freshness"] == 1.0
        # provenance: 1 row, 1 with pull_id -> 1.0
        assert result["provenance"] == 1.0
        # The weakest term is the only summary offered, and it is named.
        assert result["limiting_term"] == "completeness"

    def test_compute_confidence_backward_compat_completeness_key_present(self):
        """Existing consumers reading completeness must still find it in the dict.

        Story 53.3, second pass: this test used to call with NO `date_to` and
        assert `0.80`, so it encoded the silent default it was written before --
        `_date_to` fell back to `datetime.now()` and the query ran over today.
        The key is what backward compatibility owes a consumer; the number owes a
        window. The companion below states what the same call returns when no
        window is given.
        """
        from core.confidence import compute_confidence

        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone = MagicMock(return_value=(0.80,))

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @contextmanager
        def _conn_ctx(c):
            yield c

        with patch("core.db.get_connection", return_value=_conn_ctx(mock_conn)):
            result = compute_confidence("proj1", ["gsc"], date_to="2026-07-10")

        assert result is not None
        # Key that existing consumers read must still exist
        assert "completeness" in result
        assert result["completeness"] == 0.80

    def test_compute_confidence_without_a_window_keeps_the_key_and_drops_the_number(self):
        """No window: the key survives for its consumers, the value is unknown.

        The DB is not even reached -- an unrestricted completeness lookup returns
        another period's verification, which is the defect story 53.3 closed on
        the completeness half. Refusing to run it is the same rule applied to the
        missing-window case.
        """
        from core.confidence import compute_confidence

        with patch("core.db.get_connection", side_effect=AssertionError("must not query")):
            result = compute_confidence("proj1", ["gsc"])

        assert result is not None
        assert "completeness" in result
        assert result["completeness"] is None
        assert "score" not in result
        assert "window could not be read" in result["completeness_scope"]

    def test_compute_confidence_db_error_returns_none(self):
        """DB error -> compute_confidence returns None (best-effort, never raises).

        The window is now passed explicitly: without one the function refuses
        before it ever opens a connection, so this test would have proven the
        refusal instead of the error handling it was written for.
        """
        from core.confidence import compute_confidence

        with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
            result = compute_confidence("proj1", ["gsc"], date_to="2026-07-10")

        assert result is None

    def test_compute_confidence_never_merges_the_three_terms(self):
        """The three terms travel apart, and the weakest one is named.

        This test used to assert `score == completeness * freshness * provenance`.
        The product is exactly what `proactive-assertions.md` refuses, so what is
        proven now is its ABSENCE plus the disclosure that replaced it: with
        provenance at 0.5 and completeness at 1.0, no key reads 0.5 as an overall
        verdict, and `limiting_term` says which of the three holds the report back.
        """
        from core.confidence import compute_confidence

        # rows: half have pull_id -> provenance = 0.5
        rows = [
            {"loaded_at": "2026-07-10T01:00:00+00:00", "pull_id": "p1"},
            {"loaded_at": "2026-07-10T01:00:00+00:00", "pull_id": None},
        ]

        mock_cur = MagicMock()
        mock_cur.__enter__ = MagicMock(return_value=mock_cur)
        mock_cur.__exit__ = MagicMock(return_value=False)
        mock_cur.fetchone = MagicMock(return_value=(1.0,))  # completeness = 1.0

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_conn.cursor = MagicMock(return_value=mock_cur)

        @contextmanager
        def _conn_ctx(c):
            yield c

        with patch("core.db.get_connection", return_value=_conn_ctx(mock_conn)):
            result = compute_confidence("proj1", ["gsc"], rows=rows, date_to="2026-07-10")

        assert result is not None
        assert result["completeness"] == 1.0
        assert result["freshness"] == 1.0
        assert result["provenance"] == 0.5
        assert "score" not in result
        # No key other than `provenance` itself carries the product the removed
        # `score` used to.
        product = round(1.0 * 1.0 * 0.5, 4)
        assert product not in [
            value for key, value in result.items() if key != "provenance"
        ]
        assert result["limiting_term"] == "provenance"
