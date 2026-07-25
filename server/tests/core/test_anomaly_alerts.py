"""Unit tests for server/core/anomaly_alerts.py (Story 5.4, AC10).

Tests per AC10:
  - test_anomaly_detected_above_threshold: mart row at z=4.0 -> firing written.
  - test_no_anomaly_below_threshold: z=2.9 in mart -> no firing (below threshold).
  - test_context_event_cited_in_message: anomaly date has context_event -> label cited.
  - test_context_missing_when_no_event: no context event -> "contexte manquant".
  - test_causal_language_absent: prohibited words not in any generated message (AD-9).
  - test_ranked_by_zscore_magnitude: multiple anomalies returned sorted by |z| DESC.
  - test_anomaly_type_in_firing: firing row has type='anomaly' in INSERT SQL.
  - test_anomaly_alerts_disabled_guard: ANOMALY_ALERTS_ENABLED=false -> returns [].
  - test_zero_stddev_no_anomaly: stddev=0 -> no row in anomalies_daily -> no firing.

Strategy:
  - DuckDB calls patched via MagicMock connection (same pattern as test_business_alerts.py).
  - Postgres calls mocked via MagicMock connection.
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
os.environ.setdefault("ANOMALY_ALERTS_ENABLED", "false")

from core import anomaly_alerts  # noqa: E402

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


def _make_pg_conn(cursor=None):
    """Build a MagicMock psycopg connection."""
    cur = cursor or _make_cursor()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn


@contextmanager
def _pg_conn_ctx(conn):
    yield conn


def _make_duck_conn(anomaly_rows=None, context_rows=None):
    """Build a MagicMock DuckDB connection with configurable results."""
    duck_conn = MagicMock()
    duck_conn.__enter__ = MagicMock(return_value=duck_conn)
    duck_conn.__exit__ = MagicMock(return_value=False)

    # Track call count to distinguish anomaly query vs context query
    call_count = [0]

    def execute_side_effect(sql, params=None):
        call_count[0] += 1
        result = MagicMock()
        if call_count[0] == 1:
            # First call: anomalies_daily query
            result.fetchall = MagicMock(return_value=anomaly_rows or [])
        else:
            # Subsequent calls: context_events query per anomaly
            result.fetchall = MagicMock(return_value=context_rows or [])
        return result

    duck_conn.execute = MagicMock(side_effect=execute_side_effect)
    return duck_conn


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAnomalyAlertsDisabledGuard:
    """test_anomaly_alerts_disabled_guard (AC10 item 8)."""

    def test_anomaly_alerts_disabled_guard(self):
        """ANOMALY_ALERTS_ENABLED=false -> returns [] immediately, no DB calls."""
        with patch.dict(os.environ, {"ANOMALY_ALERTS_ENABLED": "false"}):
            result = anomaly_alerts.evaluate_anomalies()
        assert result == []


class TestAnomalyDetectedAboveThreshold:
    """test_anomaly_detected_above_threshold (AC10 item 1)."""

    def test_anomaly_detected_above_threshold(self):
        """Mart row at z=4.0 -> firing written to alert_firings."""
        eval_date = date(2026, 7, 10)
        # anomalies_daily row: (project_id, connector, metric, observed, expected, zscore)
        anomaly_row = ("proj1", "google-analytics", "sessions", 12000.0, 3000.0, 4.0)
        duck_conn = _make_duck_conn(anomaly_rows=[anomaly_row], context_rows=[])

        insert_cur = _make_cursor()
        pg_conn = _make_pg_conn(cursor=insert_cur)

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck_conn),
            patch("core.db.get_connection", return_value=_pg_conn_ctx(pg_conn)),
            patch("ulid.ULID", return_value="TESTULID01"),
        ):
            result = anomaly_alerts.evaluate_anomalies(evaluation_date=eval_date)

        assert len(result) == 1
        firing = result[0]
        assert firing["code"] == "anomaly"
        assert firing["metric"] == "sessions"
        assert firing["observed_value"] == 12000.0
        assert firing["expected_value"] == 3000.0
        assert firing["zscore"] == 4.0
        assert firing["pull_ids"] == []
        assert firing["severity"] == "warning"  # 4.0 < 5.0 -> warning
        assert firing["firing_id"].startswith("fire_")

        # Verify INSERT was called with type='anomaly'
        assert insert_cur.execute.called
        call_args = insert_cur.execute.call_args[0]
        sql = call_args[0]
        assert "INSERT INTO app.alert_firings" in sql
        assert "'anomaly'" in sql


class TestNoAnomalyBelowThreshold:
    """test_no_anomaly_below_threshold (AC10 item 2).

    When anomalies_daily has no rows (threshold already enforced in dbt),
    evaluate_anomalies returns [].
    """

    def test_no_anomaly_below_threshold(self):
        """No rows in anomalies_daily -> no firing."""
        eval_date = date(2026, 7, 10)
        # Empty anomalies_daily (z=2.9 already filtered by dbt WHERE |z| >= 3.0)
        duck_conn = _make_duck_conn(anomaly_rows=[], context_rows=[])

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck_conn),
        ):
            result = anomaly_alerts.evaluate_anomalies(evaluation_date=eval_date)

        assert result == []


class TestContextEventCitedInMessage:
    """test_context_event_cited_in_message (AC10 item 3)."""

    def test_context_event_cited_in_message(self):
        """Anomaly date has context_event in mirror -> label cited in message."""
        anomaly_dict = {
            "metric": "sessions",
            "zscore": 4.1,
            "window_date": "2026-07-15",
            "context_events": ["Lancement campagne", "Deploiement v2.1"],
        }
        line = anomaly_alerts.format_anomaly_line(anomaly_dict)
        assert "Lancement campagne" in line
        assert "Deploiement v2.1" in line
        assert "Contexte :" in line
        assert "manquant" not in line

    def test_context_event_cited_in_widget_alert(self):
        """Widget alert has context_events list and context_missing=False."""
        anomaly_dict = {
            "metric": "sessions",
            "zscore": 4.1,
            "observed_value": 12500,
            "expected_value": 3050.2,
            "window_date": "2026-07-15",
            "context_events": ["Lancement campagne"],
        }
        widget = anomaly_alerts._build_widget_alert(anomaly_dict)
        assert widget["code"] == "anomaly"
        assert widget["context_events"] == ["Lancement campagne"]
        assert widget["context_missing"] is False
        assert "Contexte :" in widget["message"]


class TestContextMissingWhenNoEvent:
    """test_context_missing_when_no_event (AC10 item 4)."""

    def test_context_missing_when_no_event(self):
        """No context_event for date -> message says 'contexte manquant'."""
        anomaly_dict = {
            "metric": "sessions",
            "zscore": 4.1,
            "window_date": "2026-07-15",
            "context_events": [],
        }
        line = anomaly_alerts.format_anomaly_line(anomaly_dict)
        assert "manquant" in line.lower()
        assert "Contexte manquant." in line

    def test_context_missing_in_widget_alert(self):
        """Widget alert has context_missing=True when context_events is empty."""
        anomaly_dict = {
            "metric": "sessions",
            "zscore": 3.5,
            "observed_value": 5000,
            "expected_value": 3000,
            "context_events": [],
        }
        widget = anomaly_alerts._build_widget_alert(anomaly_dict)
        assert widget["context_missing"] is True
        assert widget["context_events"] == []
        assert "manquant" in widget["message"].lower()


class TestCausalLanguageAbsent:
    """test_causal_language_absent (AC10 item 5, AD-9 hard rule)."""

    CAUSAL_PATTERNS = [
        "causé par",
        "caused by",
        "due to",
        "because of",
        "as a result of",
        "en raison de",
    ]

    def _check_no_causal_language(self, text: str) -> None:
        """Assert none of the prohibited causal patterns appear in text."""
        text_lower = text.lower()
        for pattern in self.CAUSAL_PATTERNS:
            assert pattern.lower() not in text_lower, (
                f"Prohibited causal language found: '{pattern}' in message: {text!r}"
            )

    def test_format_anomaly_line_no_causal_language(self):
        """format_anomaly_line never produces causal language."""
        # With context events
        line_with_ctx = anomaly_alerts.format_anomaly_line({
            "metric": "sessions",
            "zscore": 4.1,
            "window_date": "2026-07-15",
            "context_events": ["Campaign launch"],
        })
        self._check_no_causal_language(line_with_ctx)

        # Without context events
        line_no_ctx = anomaly_alerts.format_anomaly_line({
            "metric": "clicks",
            "zscore": -3.5,
            "window_date": "2026-07-15",
            "context_events": [],
        })
        self._check_no_causal_language(line_no_ctx)

    def test_build_widget_alert_no_causal_language(self):
        """_build_widget_alert message never produces causal language."""
        widget_with_ctx = anomaly_alerts._build_widget_alert({
            "metric": "sessions",
            "zscore": 4.1,
            "observed_value": 12000,
            "expected_value": 3000,
            "context_events": ["Campaign launch"],
        })
        self._check_no_causal_language(widget_with_ctx["message"])

        widget_no_ctx = anomaly_alerts._build_widget_alert({
            "metric": "cost",
            "zscore": 5.2,
            "observed_value": 50000,
            "expected_value": 10000,
            "context_events": [],
        })
        self._check_no_causal_language(widget_no_ctx["message"])


class TestRankedByZscoreMagnitude:
    """test_ranked_by_zscore_magnitude (AC10 item 6)."""

    def test_ranked_by_zscore_magnitude(self):
        """Multiple anomalies returned sorted by |zscore| DESC."""
        eval_date = date(2026, 7, 10)
        # Two anomaly rows: sessions z=4.0, clicks z=6.5 (sorted by |z| DESC)
        anomaly_rows = [
            ("proj1", "google-analytics", "clicks", 50000.0, 8000.0, 6.5),
            ("proj1", "google-analytics", "sessions", 12000.0, 3000.0, 4.0),
        ]
        duck_conn = _make_duck_conn(anomaly_rows=anomaly_rows, context_rows=[])

        insert_cur = _make_cursor()
        pg_conn = _make_pg_conn(cursor=insert_cur)

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck_conn),
            patch("core.db.get_connection", return_value=_pg_conn_ctx(pg_conn)),
            patch("ulid.ULID", return_value="TESTULID01"),
        ):
            result = anomaly_alerts.evaluate_anomalies(evaluation_date=eval_date)

        assert len(result) == 2
        # The DuckDB query returns ORDER BY ABS(zscore) DESC — clicks (6.5) comes first
        assert result[0]["metric"] == "clicks"
        assert result[0]["zscore"] == 6.5
        assert result[1]["metric"] == "sessions"
        assert result[1]["zscore"] == 4.0


class TestAnomalyTypeInFiring:
    """test_anomaly_type_in_firing (AC10 item 7)."""

    def test_anomaly_type_in_firing(self):
        """Firing row has type='anomaly' in the INSERT SQL."""
        eval_date = date(2026, 7, 10)
        anomaly_row = ("proj1", "google-analytics", "sessions", 12000.0, 3000.0, 3.5)
        duck_conn = _make_duck_conn(anomaly_rows=[anomaly_row], context_rows=[])

        insert_cur = _make_cursor()
        pg_conn = _make_pg_conn(cursor=insert_cur)

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck_conn),
            patch("core.db.get_connection", return_value=_pg_conn_ctx(pg_conn)),
            patch("ulid.ULID", return_value="TESTULID01"),
        ):
            result = anomaly_alerts.evaluate_anomalies(evaluation_date=eval_date)

        assert len(result) == 1
        assert result[0]["code"] == "anomaly"

        # Verify INSERT SQL contains type='anomaly'
        call_args = insert_cur.execute.call_args[0]
        sql = call_args[0]
        assert "'anomaly'" in sql


class TestZeroStddevNoAnomaly:
    """test_zero_stddev_no_anomaly (AC10 item 9).

    When rolling_stddev is 0 or NULL, the dbt model excludes the row
    (CASE WHEN stddev = 0 THEN NULL ELSE ... END -> NULL excluded by WHERE).
    So anomalies_daily has 0 rows for those days -> no firing.
    """

    def test_zero_stddev_no_anomaly(self):
        """stddev=0 means no row in anomalies_daily -> no firing."""
        eval_date = date(2026, 7, 10)
        # Empty anomalies_daily (stddev=0 filtered by dbt WHERE zscore IS NOT NULL)
        duck_conn = _make_duck_conn(anomaly_rows=[], context_rows=[])

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck_conn),
        ):
            result = anomaly_alerts.evaluate_anomalies(evaluation_date=eval_date)

        # No anomaly -> no firing, no DB insert
        assert result == []


class TestSeverityAssignment:
    """Test severity assignment: warning for 3<=|z|<5, error for |z|>=5."""

    @pytest.mark.parametrize(
        "zscore, expected_severity",
        [
            (3.0, "warning"),
            (3.5, "warning"),
            (4.9, "warning"),
            (5.0, "error"),
            (6.0, "error"),
            (-3.5, "warning"),
            (-5.1, "error"),
        ],
    )
    def test_severity(self, zscore: float, expected_severity: str):
        """_severity returns correct severity for given z-score."""
        assert anomaly_alerts._severity(zscore) == expected_severity


class TestFormatAnomalyLine:
    """Test format_anomaly_line output format."""

    def test_format_anomaly_line_with_context(self):
        """format_anomaly_line includes context labels."""
        line = anomaly_alerts.format_anomaly_line({
            "metric": "sessions",
            "zscore": 4.1,
            "window_date": "2026-07-15",
            "context_events": ["Lancement campagne"],
        })
        assert line.startswith("⚠️")
        assert "sessions" in line
        assert "z=+4.1" in line
        assert "2026-07-15" in line
        assert "Lancement campagne" in line

    def test_format_anomaly_line_negative_zscore(self):
        """format_anomaly_line shows negative z-score sign correctly."""
        line = anomaly_alerts.format_anomaly_line({
            "metric": "clicks",
            "zscore": -3.5,
            "window_date": "2026-07-15",
            "context_events": [],
        })
        assert "z=-3.5" in line
        assert "Contexte manquant." in line

    def test_format_anomaly_line_no_context(self):
        """format_anomaly_line says 'Contexte manquant.' when no events."""
        line = anomaly_alerts.format_anomaly_line({
            "metric": "cost",
            "zscore": 5.2,
            "window_date": "2026-07-10",
            "context_events": [],
        })
        assert "Contexte manquant." in line


class TestGracefulDegradation:
    """Test graceful degradation when DB is unavailable."""

    def test_duckdb_error_returns_empty(self):
        """DuckDB connection error -> returns []."""
        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", side_effect=RuntimeError("DuckDB unavailable")),
        ):
            result = anomaly_alerts.evaluate_anomalies()

        assert result == []

    def test_postgres_error_returns_empty(self):
        """Postgres connection error -> returns []."""
        anomaly_row = ("proj1", "google-analytics", "sessions", 12000.0, 3000.0, 4.0)
        duck_conn = _make_duck_conn(anomaly_rows=[anomaly_row], context_rows=[])

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck_conn),
            patch("core.db.get_connection", side_effect=RuntimeError("PG unavailable")),
        ):
            result = anomaly_alerts.evaluate_anomalies()

        assert result == []
