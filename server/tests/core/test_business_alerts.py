"""Unit tests for server/core/business_alerts.py (Story 5.3, AC4, AC9).

Tests per AC9:
  - test_threshold_breach_fires_row: mock DuckDB query returning observed > threshold
    -> assert alert_firings INSERT called with correct pull_ids.
  - test_no_breach_no_firing: observed < threshold -> no INSERT.
  - test_operator_whitelist_lt_gte_lte: each operator evaluates correctly.
  - test_semantic_metric_cpa_uses_view: CPA metric routes to semantic view query.
  - test_business_alerts_disabled_guard: BUSINESS_ALERTS_ENABLED=false -> returns [].
  - test_graceful_degradation_db_error: DB raises exception -> returns [].
  - test_pull_ids_in_firing: firing row carries correct pull_ids from fact rows.

Strategy:
  - DB calls mocked via MagicMock connection.
  - DuckDB queries patched at the helper function level.
  - No real Postgres or DuckDB required.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# Guard background threads
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("BUSINESS_ALERTS_ENABLED", "false")

from core import business_alerts  # noqa: E402

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_cursor(rows=None, description=None):
    """Build a MagicMock cursor."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=(rows[0] if rows else None))
    cur.description = description or []
    return cur


def _make_conn(cursor=None):
    """Build a MagicMock psycopg connection."""
    cur = cursor or _make_cursor()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn


@contextmanager
def _conn_ctx(conn):
    yield conn


# ---------------------------------------------------------------------------
# T9.1 tests
# ---------------------------------------------------------------------------


class TestBusinessAlertsDisabledGuard:
    """test_business_alerts_disabled_guard (AC9 item 5)."""

    def test_business_alerts_disabled_guard(self):
        """BUSINESS_ALERTS_ENABLED=false -> returns [] immediately, no DB calls."""
        with patch.dict(os.environ, {"BUSINESS_ALERTS_ENABLED": "false"}):
            result = business_alerts.evaluate_business_alerts()
        assert result == []

    def test_business_alerts_enabled_with_no_definitions(self):
        """BUSINESS_ALERTS_ENABLED=true but no definitions -> returns []."""
        # Cursor returns empty list for SELECT on alert_definitions
        cur = _make_cursor(
            rows=[],
            description=[
                ("id",), ("project_id",), ("metric",), ("operator",),
                ("threshold",), ("connector",),
            ],
        )
        conn = _make_conn(cur)
        with (
            patch.dict(
                os.environ,
                {"BUSINESS_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/tmp/test.duckdb"},
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
        ):
            result = business_alerts.evaluate_business_alerts(
                evaluation_date=date(2026, 7, 10)
            )
        assert result == []


class TestThresholdBreachFiresRow:
    """test_threshold_breach_fires_row (AC9 item 1)."""

    def test_threshold_breach_fires_row(self):
        """observed > threshold -> alert_firings INSERT executed with correct pull_ids."""
        eval_date = date(2026, 7, 10)
        defn_row = ("alrt_TEST01", "proj1", "cost", ">", 100.0, None)
        defn_description = [
            ("id",), ("project_id",), ("metric",), ("operator",),
            ("threshold",), ("connector",),
        ]

        # Two cursors: one for SELECT definitions, one for INSERT firing
        select_cur = _make_cursor(rows=[defn_row], description=defn_description)
        insert_cur = _make_cursor()

        call_count = [0]
        def side_effect_cursor():
            call_count[0] += 1
            if call_count[0] == 1:
                return select_cur
            return insert_cur

        conn = _make_conn()
        conn.cursor.side_effect = side_effect_cursor

        with (
            patch.dict(
                os.environ,
                {"BUSINESS_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch.object(
                business_alerts,
                "_query_additive_metric",
                return_value=(150.0, ["pull_A", "pull_B"]),
            ),
        ):
            result = business_alerts.evaluate_business_alerts(evaluation_date=eval_date)

        # One firing should have been returned
        assert len(result) == 1
        firing = result[0]
        assert firing["code"] == "business_threshold"
        assert firing["metric"] == "cost"
        assert firing["observed_value"] == 150.0
        assert firing["threshold"] == 100.0
        assert firing["pull_ids"] == ["pull_A", "pull_B"]
        assert firing["operator"] == ">"
        assert firing["firing_id"].startswith("fire_")
        assert firing["definition_id"] == "alrt_TEST01"

        # INSERT must have been called
        assert insert_cur.execute.called
        call_args = insert_cur.execute.call_args[0]
        sql = call_args[0]
        assert "INSERT INTO app.alert_firings" in sql


class TestNoBreachNoFiring:
    """test_no_breach_no_firing (AC9 item 2)."""

    def test_no_breach_no_firing(self):
        """observed < threshold -> no INSERT, returns []."""
        eval_date = date(2026, 7, 10)
        defn_row = ("alrt_TEST02", "proj1", "cost", ">", 200.0, None)
        defn_description = [
            ("id",), ("project_id",), ("metric",), ("operator",),
            ("threshold",), ("connector",),
        ]
        cur = _make_cursor(rows=[defn_row], description=defn_description)
        conn = _make_conn(cur)

        with (
            patch.dict(
                os.environ,
                {"BUSINESS_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch.object(
                business_alerts,
                "_query_additive_metric",
                return_value=(50.0, ["pull_X"]),  # 50 < 200 -> no breach
            ),
        ):
            result = business_alerts.evaluate_business_alerts(evaluation_date=eval_date)

        assert result == []


class TestOperatorWhitelistLtGteLte:
    """test_operator_whitelist_lt_gte_lte (AC9 item 3)."""

    @pytest.mark.parametrize(
        "operator, observed, threshold, should_fire",
        [
            # < operator
            ("<", 1.0, 2.0, True),   # 1 < 2 -> breach
            ("<", 3.0, 2.0, False),  # 3 < 2 -> no breach
            # >= operator
            (">=", 5.0, 5.0, True),  # 5 >= 5 -> breach
            (">=", 4.0, 5.0, False), # 4 >= 5 -> no breach
            # <= operator
            ("<=", 2.0, 3.0, True),  # 2 <= 3 -> breach
            ("<=", 4.0, 3.0, False), # 4 <= 3 -> no breach
            # > operator (also tested here for completeness)
            (">", 10.0, 5.0, True),  # 10 > 5 -> breach
            (">", 3.0, 5.0, False),  # 3 > 5 -> no breach
        ],
    )
    def test_operator_evaluation(
        self, operator: str, observed: float, threshold: float, should_fire: bool
    ):
        """Each operator evaluates correctly."""
        eval_date = date(2026, 7, 10)
        defn_row = ("alrt_OP_TEST", "proj1", "cost", operator, threshold, None)
        defn_description = [
            ("id",), ("project_id",), ("metric",), ("operator",),
            ("threshold",), ("connector",),
        ]

        select_cur = _make_cursor(rows=[defn_row], description=defn_description)
        insert_cur = _make_cursor()

        call_count = [0]
        def side_effect_cursor():
            call_count[0] += 1
            return select_cur if call_count[0] == 1 else insert_cur

        conn = _make_conn()
        conn.cursor.side_effect = side_effect_cursor

        with (
            patch.dict(
                os.environ,
                {"BUSINESS_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch.object(
                business_alerts,
                "_query_additive_metric",
                return_value=(observed, ["pull_Z"]),
            ),
        ):
            result = business_alerts.evaluate_business_alerts(evaluation_date=eval_date)

        if should_fire:
            assert len(result) == 1, f"Expected breach for {observed} {operator} {threshold}"
        else:
            assert len(result) == 0, f"Expected no breach for {observed} {operator} {threshold}"


class TestSemanticMetricCpaUsesView:
    """test_semantic_metric_cpa_uses_view (AC9 item 4)."""

    def test_semantic_metric_cpa_uses_view(self):
        """CPA metric routes to semantic view query, not raw fact_daily_kpi."""
        eval_date = date(2026, 7, 10)
        defn_row = ("alrt_CPA01", "proj1", "cpa", ">", 1.5, None)
        defn_description = [
            ("id",), ("project_id",), ("metric",), ("operator",),
            ("threshold",), ("connector",),
        ]

        select_cur = _make_cursor(rows=[defn_row], description=defn_description)
        insert_cur = _make_cursor()

        call_count = [0]
        def side_effect_cursor():
            call_count[0] += 1
            return select_cur if call_count[0] == 1 else insert_cur

        conn = _make_conn()
        conn.cursor.side_effect = side_effect_cursor

        with (
            patch.dict(
                os.environ,
                {
                    "BUSINESS_ALERTS_ENABLED": "true",
                    "TOOROW_DUCKDB_PATH": "/fake.duckdb",
                    "ALERT_SEMANTIC_METRICS": "cpa,roas,ctr",
                },
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch.object(
                business_alerts,
                "_query_semantic_metric",
                return_value=(2.45, ["pull_CPA"]),
            ) as mock_semantic,
            patch.object(
                business_alerts,
                "_query_additive_metric",
                return_value=(0.0, []),
            ) as mock_additive,
        ):
            result = business_alerts.evaluate_business_alerts(evaluation_date=eval_date)

        # Semantic query should have been called, NOT additive
        mock_semantic.assert_called_once()
        mock_additive.assert_not_called()

        # CPA 2.45 > 1.5 -> breach should fire
        assert len(result) == 1
        assert result[0]["metric"] == "cpa"
        assert result[0]["observed_value"] == 2.45


class TestGracefulDegradationDbError:
    """test_graceful_degradation_db_error (AC9 item 6)."""

    def test_graceful_degradation_db_error(self):
        """DB raises exception -> function returns [] without propagating."""
        with (
            patch.dict(
                os.environ,
                {"BUSINESS_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("core.db.get_connection", side_effect=RuntimeError("DB is down")),
        ):
            result = business_alerts.evaluate_business_alerts()

        # Must return [] gracefully, never raise
        assert result == []


class TestPullIdsInFiring:
    """test_pull_ids_in_firing (AC9 item 7)."""

    def test_pull_ids_in_firing(self):
        """Firing row carries correct pull_ids array from contributing fact rows."""
        eval_date = date(2026, 7, 10)
        expected_pull_ids = ["pull_001", "pull_002", "pull_003"]
        defn_row = ("alrt_PIDS", "proj1", "clicks", ">", 100.0, None)
        defn_description = [
            ("id",), ("project_id",), ("metric",), ("operator",),
            ("threshold",), ("connector",),
        ]

        select_cur = _make_cursor(rows=[defn_row], description=defn_description)
        insert_cur = _make_cursor()

        call_count = [0]
        def side_effect_cursor():
            call_count[0] += 1
            return select_cur if call_count[0] == 1 else insert_cur

        conn = _make_conn()
        conn.cursor.side_effect = side_effect_cursor

        with (
            patch.dict(
                os.environ,
                {"BUSINESS_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch.object(
                business_alerts,
                "_query_additive_metric",
                return_value=(500.0, expected_pull_ids),
            ),
        ):
            result = business_alerts.evaluate_business_alerts(evaluation_date=eval_date)

        assert len(result) == 1
        assert result[0]["pull_ids"] == expected_pull_ids

        # Verify the INSERT carries the pull_ids
        insert_args = insert_cur.execute.call_args[0]
        insert_params = insert_args[1]
        # pull_ids is the 6th param: (firing_id, def_id, fired_at, obs, thr, pull_ids, date)
        assert expected_pull_ids in insert_params


class TestFormatAlertLine:
    """Test the format_alert_line helper."""

    def test_format_alert_line_basic(self):
        """format_alert_line returns expected French string."""
        firing = {
            "metric": "cpa",
            "observed_value": 2.45,
            "threshold": 2.00,
            "operator": ">",
        }
        line = business_alerts.format_alert_line(firing)
        assert "CPA" in line
        assert "2.45" in line
        assert "2.00" in line
        assert ">" in line
        assert line.startswith("⚠️")

    def test_format_alert_line_len(self):
        """format_alert_line output is reasonably short (≤120 chars)."""
        firing = {
            "metric": "clicks",
            "observed_value": 12345.67,
            "threshold": 10000.00,
            "operator": ">=",
        }
        line = business_alerts.format_alert_line(firing)
        assert len(line) <= 120
