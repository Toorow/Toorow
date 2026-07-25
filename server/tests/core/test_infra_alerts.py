"""Unit tests for server/core/infra_alerts.py (Story 5.2, AC1, AC6).

Tests:
  - AlertSignal dataclass construction
  - _read_dead_letter_count: returns counts grouped by connector; returns {} on DB error
  - _read_mirror_lag: reads _last_sync_result; returns None when None or error key
  - _read_verification_failures: returns count below threshold; 0 on DB error
  - _read_health_poller_staleness: computes seconds; None if no rows; 0 on DB error
  - evaluate_alerts: threshold matrix (AC6) -- 6 required scenarios
  - evaluate_alerts: graceful degradation on DB error (returns [] or partial)

Strategy:
  - All DB calls mocked via contextmanager returning a MagicMock connection.
  - mirror_sync._last_sync_result patched directly.
  - ALERTS_ENABLED not set (evaluator called directly in tests -- AC7 is tested
    in test_alerts_disabled.py).
  - No real Postgres required.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

# Guard background threads
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ALERTS_ENABLED", "false")


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def _make_cursor(rows=None, description=None):
    """Build a MagicMock cursor that returns given rows."""
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=rows[0] if rows else None)
    cur.description = description or []
    return cur


def _make_conn(cursor=None):
    """Build a MagicMock connection yielding the given cursor."""
    cur = cursor or _make_cursor()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn


@contextmanager
def _fake_db(rows=None):
    """Context manager returning a fake DB connection."""
    yield _make_conn(_make_cursor(rows=rows))


# ---------------------------------------------------------------------------
# T1.1 -- AlertSignal dataclass
# ---------------------------------------------------------------------------


class TestAlertSignal:
    def test_alert_signal_construction(self):
        from core.infra_alerts import AlertSignal

        sig = AlertSignal(
            signal="dead_letter_count",
            value=3.0,
            threshold=1.0,
            severity="high",
            connector="google-analytics",
        )
        assert sig.signal == "dead_letter_count"
        assert sig.value == 3.0
        assert sig.threshold == 1.0
        assert sig.severity == "high"
        assert sig.connector == "google-analytics"
        assert sig.timestamp  # auto-filled

    def test_alert_signal_timestamp_is_iso(self):
        from core.infra_alerts import AlertSignal

        sig = AlertSignal(
            signal="mirror_sync_lag_seconds",
            value=4000.0,
            threshold=3600.0,
            severity="medium",
        )
        # Should parse as ISO datetime
        parsed = datetime.fromisoformat(sig.timestamp)
        assert parsed is not None

    def test_alert_signal_connector_defaults_none(self):
        from core.infra_alerts import AlertSignal

        sig = AlertSignal(
            signal="health_poller_staleness_seconds",
            value=8000.0,
            threshold=7200.0,
            severity="medium",
        )
        assert sig.connector is None


# ---------------------------------------------------------------------------
# T1.2 -- _read_dead_letter_count
# ---------------------------------------------------------------------------


class TestReadDeadLetterCount:
    def test_returns_counts_by_connector(self):
        from core.infra_alerts import _read_dead_letter_count

        rows = [("google-analytics", 2), ("meta-ads", 1)]
        conn = _make_conn(_make_cursor(rows=rows))
        result = _read_dead_letter_count(conn)
        assert result == {"google-analytics": 2, "meta-ads": 1}

    def test_returns_empty_when_no_rows(self):
        from core.infra_alerts import _read_dead_letter_count

        conn = _make_conn(_make_cursor(rows=[]))
        result = _read_dead_letter_count(conn)
        assert result == {}

    def test_returns_empty_on_db_error(self):
        from core.infra_alerts import _read_dead_letter_count

        conn = MagicMock()
        conn.cursor = MagicMock(side_effect=RuntimeError("DB error"))
        result = _read_dead_letter_count(conn)
        assert result == {}


# ---------------------------------------------------------------------------
# T1.3 -- _read_mirror_lag
# ---------------------------------------------------------------------------


class TestReadMirrorLag:
    def test_returns_lag_from_last_sync_result(self):
        from core.infra_alerts import _read_mirror_lag

        with patch("core.mirror_sync._last_sync_result", {"lag_seconds": 1234.5}):
            result = _read_mirror_lag()
        assert result == 1234.5

    def test_returns_none_when_never_synced(self):
        from core.infra_alerts import _read_mirror_lag

        with patch("core.mirror_sync._last_sync_result", None):
            result = _read_mirror_lag()
        assert result is None

    def test_returns_none_when_error_in_result(self):
        from core.infra_alerts import _read_mirror_lag

        with patch(
            "core.mirror_sync._last_sync_result", {"error": "timeout", "lag_seconds": 0}
        ):
            result = _read_mirror_lag()
        assert result is None

    def test_returns_none_when_lag_key_missing(self):
        from core.infra_alerts import _read_mirror_lag

        with patch("core.mirror_sync._last_sync_result", {"synced_at": "2026-07-11T00:00:00Z"}):
            result = _read_mirror_lag()
        assert result is None


# ---------------------------------------------------------------------------
# T1.4 -- _read_verification_failures
# ---------------------------------------------------------------------------


class TestReadVerificationFailures:
    def test_returns_count(self):
        from core.infra_alerts import _read_verification_failures

        rows = [(3,)]
        conn = _make_conn(_make_cursor(rows=rows))
        result = _read_verification_failures(conn, threshold=0.5)
        assert result == 3

    def test_returns_zero_when_no_rows(self):
        from core.infra_alerts import _read_verification_failures

        conn = _make_conn(_make_cursor(rows=None))
        result = _read_verification_failures(conn, threshold=0.5)
        assert result == 0

    def test_returns_zero_on_db_error(self):
        from core.infra_alerts import _read_verification_failures

        conn = MagicMock()
        conn.cursor = MagicMock(side_effect=RuntimeError("DB error"))
        result = _read_verification_failures(conn)
        assert result == 0


# ---------------------------------------------------------------------------
# T1.5 -- _read_health_poller_staleness
# ---------------------------------------------------------------------------


class TestReadHealthPollerStaleness:
    def test_returns_staleness_seconds(self):
        from core.infra_alerts import _read_health_poller_staleness

        # last_checked_at = 3 hours ago
        last = datetime.now(tz=timezone.utc) - timedelta(hours=3)
        rows = [(last,)]
        conn = _make_conn(_make_cursor(rows=rows))
        result = _read_health_poller_staleness(conn)
        assert result is not None
        # Should be approximately 3 hours in seconds (allow ±5s tolerance)
        assert abs(result - 3 * 3600) < 5

    def test_returns_none_when_no_rows(self):
        from core.infra_alerts import _read_health_poller_staleness

        rows = [(None,)]
        conn = _make_conn(_make_cursor(rows=rows))
        result = _read_health_poller_staleness(conn)
        assert result is None

    def test_returns_none_on_db_error(self):
        from core.infra_alerts import _read_health_poller_staleness

        conn = MagicMock()
        conn.cursor = MagicMock(side_effect=RuntimeError("DB error"))
        result = _read_health_poller_staleness(conn)
        assert result is None


# ---------------------------------------------------------------------------
# T1.6 -- evaluate_alerts: threshold matrix (AC6)
# ---------------------------------------------------------------------------


class TestEvaluateAlertsThresholdMatrix:
    """AC6: 6 required scenarios."""

    def _mock_db_minimal(self, dead_letter_rows=None, verification_rows=None, staleness_rows=None):
        """Build a patched get_connection that returns a fake conn with controlled cursors."""
        dl_rows = dead_letter_rows if dead_letter_rows is not None else []
        vf_rows = verification_rows if verification_rows is not None else [(0,)]
        st_rows = staleness_rows if staleness_rows is not None else [(None,)]

        call_count = {"n": 0}

        def make_cursor():
            n = call_count["n"]
            call_count["n"] += 1
            if n == 0:
                # dead_letter query
                return _make_cursor(rows=dl_rows)
            elif n == 1:
                # verification failures query
                return _make_cursor(rows=vf_rows)
            else:
                # health staleness query
                return _make_cursor(rows=st_rows)

        conn = MagicMock()
        conn.__enter__ = MagicMock(return_value=conn)
        conn.__exit__ = MagicMock(return_value=False)
        conn.cursor = MagicMock(side_effect=make_cursor)
        conn.commit = MagicMock()

        @contextmanager
        def fake_get_connection():
            yield conn

        return fake_get_connection

    def test_ac6_scenario1_dead_letter_zero_no_alert(self):
        """dead_letter_count=0 -> no alert."""
        from core.infra_alerts import evaluate_alerts

        fake_db = self._mock_db_minimal(dead_letter_rows=[])
        with patch("core.db.get_connection", fake_db), \
             patch("core.mirror_sync._last_sync_result", None), \
             patch.dict(os.environ, {"ALERT_DEAD_LETTER_THRESHOLD": "1"}, clear=False):
            breaches = evaluate_alerts()

        dead_letter_breaches = [b for b in breaches if b.signal == "dead_letter_count"]
        assert len(dead_letter_breaches) == 0

    def test_ac6_scenario2_dead_letter_one_at_threshold_fires(self):
        """dead_letter_count=1 with ALERT_DEAD_LETTER_THRESHOLD=1 -> alert fires."""
        from core.infra_alerts import evaluate_alerts

        fake_db = self._mock_db_minimal(dead_letter_rows=[("google-analytics", 1)])
        with patch("core.db.get_connection", fake_db), \
             patch("core.mirror_sync._last_sync_result", None), \
             patch.dict(os.environ, {"ALERT_DEAD_LETTER_THRESHOLD": "1"}, clear=False):
            breaches = evaluate_alerts()

        dead_letter_breaches = [b for b in breaches if b.signal == "dead_letter_count"]
        assert len(dead_letter_breaches) == 1
        assert dead_letter_breaches[0].value == 1.0
        assert dead_letter_breaches[0].threshold == 1.0

    def test_ac6_scenario3_mirror_lag_above_threshold_fires(self):
        """mirror_sync_lag=3601 with threshold=3600 -> alert fires."""
        from core.infra_alerts import evaluate_alerts

        fake_db = self._mock_db_minimal()
        with patch("core.db.get_connection", fake_db), \
             patch("core.mirror_sync._last_sync_result", {"lag_seconds": 3601.0}), \
             patch.dict(os.environ, {"ALERT_MIRROR_LAG_THRESHOLD_SECONDS": "3600"}, clear=False):
            breaches = evaluate_alerts()

        lag_breaches = [b for b in breaches if b.signal == "mirror_sync_lag_seconds"]
        assert len(lag_breaches) == 1
        assert lag_breaches[0].value == 3601.0

    def test_ac6_scenario4_mirror_lag_below_threshold_no_alert(self):
        """mirror_sync_lag=3599 -> no alert."""
        from core.infra_alerts import evaluate_alerts

        fake_db = self._mock_db_minimal()
        with patch("core.db.get_connection", fake_db), \
             patch("core.mirror_sync._last_sync_result", {"lag_seconds": 3599.0}), \
             patch.dict(os.environ, {"ALERT_MIRROR_LAG_THRESHOLD_SECONDS": "3600"}, clear=False):
            breaches = evaluate_alerts()

        lag_breaches = [b for b in breaches if b.signal == "mirror_sync_lag_seconds"]
        assert len(lag_breaches) == 0

    def test_ac6_scenario5_verification_failure_zero_no_alert(self):
        """verification_failure_count=0 -> no alert."""
        from core.infra_alerts import evaluate_alerts

        fake_db = self._mock_db_minimal(verification_rows=[(0,)])
        with patch("core.db.get_connection", fake_db), \
             patch("core.mirror_sync._last_sync_result", None):
            breaches = evaluate_alerts()

        vf_breaches = [b for b in breaches if b.signal == "verification_failure_count"]
        assert len(vf_breaches) == 0

    def test_ac6_scenario6_health_poller_stale_fires(self):
        """health_poller_staleness=7201 with threshold=7200 -> alert fires."""
        from core.infra_alerts import evaluate_alerts

        # staleness_seconds > 7200: last_checked_at was 7201 seconds ago
        stale_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=7201)
        fake_db = self._mock_db_minimal(staleness_rows=[(stale_ts,)])
        with patch("core.db.get_connection", fake_db), \
             patch("core.mirror_sync._last_sync_result", None), \
             patch.dict(os.environ, {"ALERT_HEALTH_POLLER_STALE_SECONDS": "7200"}, clear=False):
            breaches = evaluate_alerts()

        stale_breaches = [b for b in breaches if b.signal == "health_poller_staleness_seconds"]
        assert len(stale_breaches) == 1
        assert stale_breaches[0].value >= 7200.0

    def test_evaluate_alerts_returns_empty_on_db_failure(self):
        """DB error -> evaluate_alerts returns [] (never raises)."""
        from core.infra_alerts import evaluate_alerts

        def broken_db():
            raise RuntimeError("DB down")

        with patch("core.db.get_connection", broken_db), \
             patch("core.mirror_sync._last_sync_result", None):
            breaches = evaluate_alerts()

        # May return empty or only non-DB breaches (mirror lag)
        # The evaluator must never raise
        assert isinstance(breaches, list)

    def test_evaluate_alerts_never_raises(self):
        """evaluate_alerts must never raise regardless of errors."""
        from core.infra_alerts import evaluate_alerts

        # Patch get_connection to blow up; mirror_sync returns None safely
        with patch("core.db.get_connection", side_effect=Exception("catastrophic")), \
             patch("core.mirror_sync._last_sync_result", None):
            try:
                result = evaluate_alerts()
                assert isinstance(result, list)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"evaluate_alerts raised unexpectedly: {exc}")
