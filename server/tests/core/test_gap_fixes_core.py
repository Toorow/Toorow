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
            result = _rollup(rows, report)

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
            result = _rollup(rows, report)

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
            result = _rollup(rows, report)

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
# Confidence 3 terms: freshness, provenance, score
# ---------------------------------------------------------------------------

class TestConfidenceThreeTerms:
    """confidence.py must return freshness, provenance, and score terms."""

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

    def test_compute_freshness_no_rows_returns_one(self):
        """No rows -> freshness = 1.0 (best-effort: no penalty for empty result)."""
        from core.confidence import _compute_freshness

        result = _compute_freshness([], "2026-07-10")
        assert result == 1.0

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

    def test_compute_provenance_empty_rows(self):
        """No rows -> provenance = 1.0 (best-effort)."""
        from core.confidence import _compute_provenance

        assert _compute_provenance([]) == 1.0

    def test_compute_confidence_returns_all_three_terms(self, tmp_path):
        """compute_confidence must return completeness, freshness, provenance, score."""
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
        assert "score" in result
        assert result["completeness"] == 0.95
        # freshness: loaded_at=2026-07-10T06:00:00 is within grace window of date_to=2026-07-10
        assert result["freshness"] == 1.0
        # provenance: 1 row, 1 with pull_id -> 1.0
        assert result["provenance"] == 1.0
        # score = completeness * freshness * provenance = 0.95 * 1.0 * 1.0
        assert abs(result["score"] - 0.95) < 1e-6

    def test_compute_confidence_backward_compat_completeness_key_present(self):
        """Existing consumers reading completeness must still find it in the dict."""
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
            result = compute_confidence("proj1", ["gsc"])

        assert result is not None
        # Key that existing consumers read must still exist
        assert "completeness" in result
        assert result["completeness"] == 0.80

    def test_compute_confidence_db_error_returns_none(self):
        """DB error -> compute_confidence returns None (best-effort, never raises)."""
        from core.confidence import compute_confidence

        with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
            result = compute_confidence("proj1", ["gsc"])

        assert result is None

    def test_compute_confidence_score_is_product_of_three_terms(self):
        """score must equal completeness * freshness * provenance."""
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
        expected_score = round(
            result["completeness"] * result["freshness"] * result["provenance"], 4
        )
        assert result["score"] == expected_score
