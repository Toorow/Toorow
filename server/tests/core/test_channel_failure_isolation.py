"""Tests for channel failure isolation (Story 5.2, AC2, T2.4).

Tests:
  - One channel raising an exception does NOT prevent other channels from being called
  - notify_alert calls all channels even when one fails
  - ConsoleChannel.send logs structured JSON

Strategy:
  - Use real ConsoleChannel / fake channels
  - Assert all channels receive .send() call regardless of failures
"""

from __future__ import annotations

import json
import logging
import os
from unittest.mock import MagicMock

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ALERTS_ENABLED", "false")


def make_signal(signal="dead_letter_count", value=1.0, threshold=1.0, severity="high"):
    from core.infra_alerts import AlertSignal

    return AlertSignal(signal=signal, value=value, threshold=threshold, severity=severity)


class TestChannelFailureIsolation:
    def test_second_channel_called_when_first_raises(self):
        """One channel failure does NOT block subsequent channels."""
        from core.infra_alerts import notify_alert

        ch1 = MagicMock()
        ch1.send = MagicMock(side_effect=RuntimeError("channel 1 exploded"))

        ch2 = MagicMock()
        ch2.send = MagicMock()

        signal = make_signal()
        notify_alert(signal, [ch1, ch2])

        ch1.send.assert_called_once_with(signal)
        ch2.send.assert_called_once_with(signal)

    def test_all_channels_called_with_signal(self):
        """All channels receive the alert signal."""
        from core.infra_alerts import notify_alert

        channels = [MagicMock() for _ in range(3)]
        signal = make_signal()
        notify_alert(signal, channels)

        for ch in channels:
            ch.send.assert_called_once_with(signal)

    def test_notify_alert_does_not_raise_when_all_channels_fail(self):
        """notify_alert never raises even if every channel fails."""
        from core.infra_alerts import notify_alert

        failing_ch = MagicMock()
        failing_ch.send = MagicMock(side_effect=Exception("failure"))

        signal = make_signal()
        # Must not raise
        notify_alert(signal, [failing_ch, failing_ch])

    def test_notify_alert_empty_channels_is_noop(self):
        """notify_alert with empty channel list is a safe noop."""
        from core.infra_alerts import notify_alert

        signal = make_signal()
        notify_alert(signal, [])  # should not raise


class TestConsoleChannel:
    def test_console_channel_logs_structured_json(self, caplog):
        """ConsoleChannel logs a structured JSON record to logger.warning."""
        from core.infra_alerts import ConsoleChannel

        channel = ConsoleChannel()
        signal = make_signal(
            signal="dead_letter_count", value=2.0, threshold=1.0, severity="high"
        )

        with caplog.at_level(logging.WARNING, logger="core.infra_alerts"):
            channel.send(signal)

        # Find the log record
        records = [r for r in caplog.records if "infra_alert" in r.message]
        assert len(records) >= 1

        # Parse JSON from the log message
        msg = records[0].message
        # Strip the "infra_alert: " prefix
        json_str = msg.split("infra_alert: ", 1)[-1]
        data = json.loads(json_str)

        assert data["event"] == "infra_alert"
        assert data["signal"] == "dead_letter_count"
        assert data["value"] == 2.0
        assert data["threshold"] == 1.0
        assert data["severity"] == "high"
        assert "timestamp" in data

    def test_console_channel_includes_connector_when_set(self, caplog):
        """ConsoleChannel JSON includes connector field when set."""
        from core.infra_alerts import AlertSignal, ConsoleChannel

        channel = ConsoleChannel()
        signal = AlertSignal(
            signal="dead_letter_count",
            value=1.0,
            threshold=1.0,
            severity="high",
            connector="google-analytics",
        )

        with caplog.at_level(logging.WARNING, logger="core.infra_alerts"):
            channel.send(signal)

        records = [r for r in caplog.records if "infra_alert" in r.message]
        assert len(records) >= 1
        json_str = records[0].message.split("infra_alert: ", 1)[-1]
        data = json.loads(json_str)
        assert data["connector"] == "google-analytics"
