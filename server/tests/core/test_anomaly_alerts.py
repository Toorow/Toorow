"""Unit tests for server/core/anomaly_alerts.py (Story 5.4, AC10).

Tests per AC10:
  - test_anomaly_detected_above_threshold: mart row at z=4.0 -> firing written.
  - test_no_anomaly_below_threshold: z=2.9 in mart -> no firing (below threshold).
  - test_context_event_cited_in_message: anomaly date has context_event -> label cited.
  - test_context_missing_when_no_event: no context event -> "contexte missing".
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

# Imported BEFORE any `patch("ulid.ULID")` below: `core.audit` binds `ULID` at
# module level, and a first import made INSIDE a patched block would freeze the
# mock into it for the rest of the session -- every later audit row of the run
# then carried `audit_TESTULID01` and the second one violated the primary key
# (measured 2026-09-01 on the pg-gated record fixture of
# `test_mirror_fetch_context_events.py`).
from core import audit as _audit_bound_to_the_real_ulid  # noqa: E402, F401

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
        result = MagicMock()
        if "information_schema" in sql:
            # The catalogue probes (table present, columns present): this fixture
            # models a SYNCED mirror, so the walk runs there -- AI-344 sends it to
            # the record only when the table is absent.
            result.fetchall = MagicMock(return_value=[(1,)])
            return result
        call_count[0] += 1
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


# ---------------------------------------------------------------------------
# AI-344 (2026-09-01) -- the candidate-cause walk is the SAME defect as
# `fetch_context_events`: on a deployment that keeps no DuckDB mirror it read
# nothing and said "Contexte manquant." The walk now runs on the record when the
# warehouse carries no `mirror.context_events`, and when neither store can serve
# the anomaly keeps its verdict and says the walk did not run.
# ---------------------------------------------------------------------------

_RECORD_COLS = ("id", "project_id", "event_date", "type", "label", "platform", "value",
                "source", "metric")


def _warehouse_without_the_mirror():
    """A warehouse connection whose catalogue has no `mirror.context_events`."""
    duck = MagicMock()

    def _execute(sql, params=None):
        res = MagicMock()
        res.fetchall = MagicMock(return_value=[])
        return res

    duck.execute = MagicMock(side_effect=_execute)
    return duck


def _record_with(rows):
    """A Postgres connection whose only read answers *rows* in the record's shape."""
    from datetime import date as _date  # noqa: PLC0415

    cur = _make_cursor(rows=rows, description=[(c,) for c in _RECORD_COLS])
    cur.fetchall = MagicMock(return_value=rows)
    conn = _make_pg_conn(cur)
    conn.record_date = _date
    return conn


class TestCandidateCauseWalkReadsTheRecord:
    def test_no_mirror_reads_the_record_with_both_discriminants(self):
        from datetime import date as _date

        from core.briefing import CONTEXT_BASIS_METRIC, CONTEXT_BASIS_PLATFORM

        rows = [
            ("evt_a", "proj1", _date(2026, 7, 12), "campaign_launch", "Campaign launch",
             "google-analytics", None, "manual", None),
            ("evt_b", "proj1", _date(2026, 7, 12), "release", "Meta creative swap",
             "meta-ads", None, "manual", None),
            ("evt_c", "proj1", _date(2026, 7, 12), "business", "Cost cap raised",
             None, None, "manual", "cost"),
        ]
        pg = _record_with(rows)
        labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
            "proj1", _date(2026, 7, 12), _warehouse_without_the_mirror(),
            connector="google-analytics", metric="sessions",
            record_connection=lambda: pg,
        )
        # Another platform is out; another metric is out; "none" stays.
        assert labels == ["Campaign launch"]
        assert CONTEXT_BASIS_PLATFORM in pairing["basis"]
        assert CONTEXT_BASIS_METRIC in pairing["basis"]
        assert "unavailable" not in pairing
        sql = pg.cursor().execute.call_args[0][0]
        assert "app.context_events" in sql and "retired_at IS NULL" in sql

    def test_no_mirror_and_no_record_says_unavailable_not_no_cause(self):
        from datetime import date as _date

        labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
            "proj1", _date(2026, 7, 12), _warehouse_without_the_mirror(),
            connector="google-analytics",
        )
        assert labels == []
        assert set(pairing["unavailable"]) == {"reason", "repair"}
        assert pairing["unscoped_dimensions"]

        anomaly = {
            "metric": "sessions", "zscore": 4.0, "window_date": "2026-07-12",
            "context_events": labels, "context_pairing": pairing,
        }
        line = anomaly_alerts.format_anomaly_line(anomaly)
        assert "Contexte manquant" not in line
        assert "Contexte non lu" in line
        widget = anomaly_alerts._build_widget_alert(anomaly)
        assert widget["context_missing"] is False
        assert widget["context_unavailable"] == pairing["unavailable"]
        assert "Contexte manquant" not in widget["message"]

    def test_a_record_that_cannot_be_reached_is_unavailable_with_the_repair(self):
        from datetime import date as _date

        def _refused():
            raise ConnectionRefusedError("connection refused")

        labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
            "proj1", _date(2026, 7, 12), _warehouse_without_the_mirror(),
            record_connection=_refused,
        )
        assert labels == []
        assert "could not be reached" in pairing["unavailable"]["reason"]
        assert "PLATFORM_DB_URL" in pairing["unavailable"]["repair"]

    def test_the_verdict_still_fires_when_the_walk_reads_the_record(self):
        """End to end: no mirror table in the warehouse, rows in the record --
        the firing carries the labels, the record connection is opened ONCE for
        the scan and closed with it."""
        from datetime import date as _date

        anomaly_row = ("proj1", "google-analytics", "sessions", 12000.0, 3000.0, 4.0)
        duck = _make_duck_conn(anomaly_rows=[anomaly_row])

        def _duck_execute(sql, params=None):
            res = MagicMock()
            if "information_schema" in sql:
                res.fetchall = MagicMock(return_value=[])  # no mirror here
            else:
                res.fetchall = MagicMock(return_value=[anomaly_row])
            return res

        duck.execute = MagicMock(side_effect=_duck_execute)
        pg = _record_with([
            ("evt_a", "proj1", _date(2026, 7, 12), "campaign_launch", "Campaign launch",
             None, None, "manual", None),
        ])
        opened: list = []

        def _open():
            opened.append(1)
            return _pg_conn_ctx(pg)

        with (
            patch.dict(
                os.environ,
                {"ANOMALY_ALERTS_ENABLED": "true", "TOOROW_DUCKDB_PATH": "/fake.duckdb"},
            ),
            patch("duckdb.connect", return_value=duck),
            patch("core.db.get_connection", side_effect=_open),
        ):
            result = anomaly_alerts.evaluate_anomalies(evaluation_date=_date(2026, 7, 12))

        assert len(result) == 1
        assert result[0]["context_events"] == ["Campaign launch"]
        assert "unavailable" not in result[0]["context_pairing"]
        # One connection for the walk, one for the firing write -- never one per row.
        assert len(opened) == 2
