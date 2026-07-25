"""Tests for ALERTS_ENABLED=false guard (Story 5.2, AC3, AC7).

Tests:
  - When ALERTS_ENABLED=false, _run_alert_check() does NOT call evaluate_alerts()
  - When ALERTS_ENABLED=false, no DB queries are made from the alert path
  - _run_alert_check() with ALERTS_ENABLED=true calls evaluate_alerts()
  - _run_alert_check() catches all exceptions (scheduler thread safety)

Strategy:
  - Patch core.infra_alerts.evaluate_alerts and assert call_count
  - Patch core.db.get_connection and assert it is NOT called from alert path
"""

from __future__ import annotations

import os
from unittest.mock import patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ALERTS_ENABLED", "false")


class TestAlertsDisabledGuard:
    def test_run_alert_check_does_not_call_evaluate_when_disabled(self):
        """ALERTS_ENABLED=false -> evaluate_alerts is NEVER called."""
        from core.scheduler import _run_alert_check

        with patch("core.infra_alerts.evaluate_alerts") as mock_eval, \
             patch.dict(os.environ, {"ALERTS_ENABLED": "false"}, clear=False):
            _run_alert_check()

        mock_eval.assert_not_called()

    def test_run_alert_check_no_db_queries_when_disabled(self):
        """ALERTS_ENABLED=false -> core.db.get_connection never called from alert path."""
        from core.scheduler import _run_alert_check

        with patch("core.db.get_connection") as mock_get_conn, \
             patch.dict(os.environ, {"ALERTS_ENABLED": "false"}, clear=False):
            _run_alert_check()

        assert mock_get_conn.call_count == 0

    def test_run_alert_check_calls_evaluate_when_enabled(self):
        """ALERTS_ENABLED=true -> evaluate_alerts IS called."""
        from core.scheduler import _run_alert_check

        mock_breaches = []  # no breaches

        with patch("core.infra_alerts.evaluate_alerts", return_value=mock_breaches) as mock_eval, \
             patch("core.infra_alerts.build_channels", return_value=[]), \
             patch.dict(os.environ, {"ALERTS_ENABLED": "true"}, clear=False):
            _run_alert_check()

        mock_eval.assert_called_once()

    def test_run_alert_check_never_raises(self):
        """_run_alert_check catches all exceptions to protect the scheduler thread."""
        from core.scheduler import _run_alert_check

        with patch(
            "core.infra_alerts.evaluate_alerts",
            side_effect=RuntimeError("catastrophic failure"),
        ), patch.dict(os.environ, {"ALERTS_ENABLED": "true"}, clear=False):
            # Must not raise
            _run_alert_check()

    def test_run_alert_check_notifies_all_breaches(self):
        """When breaches exist, notify_alert is called once per breach."""
        from core.infra_alerts import AlertSignal
        from core.scheduler import _run_alert_check

        breach1 = AlertSignal(
            signal="dead_letter_count", value=2.0, threshold=1.0, severity="high"
        )
        breach2 = AlertSignal(
            signal="mirror_sync_lag_seconds", value=4000.0, threshold=3600.0, severity="medium"
        )

        with patch("core.infra_alerts.evaluate_alerts", return_value=[breach1, breach2]), \
             patch("core.infra_alerts.build_channels", return_value=[]), \
             patch("core.infra_alerts.notify_alert") as mock_notify, \
             patch.dict(os.environ, {"ALERTS_ENABLED": "true"}, clear=False):
            _run_alert_check()

        assert mock_notify.call_count == 2
