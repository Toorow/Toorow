"""Unit tests for EmailChannel (Story 5.2, AC2, AC5).

Tests:
  - EmailChannel.send calls smtplib.SMTP with configured host/port
  - Subject prefix is [toorow]
  - Recipient matches ALERT_EMAIL_TO
  - Body contains signal name and threshold value
  - SMTP login called when credentials provided
  - SMTP login NOT called when no credentials
  - EmailChannel raises on SMTP failure (notify_alert catches it)

Strategy:
  - Mock smtplib.SMTP via unittest.mock.patch
  - No real SMTP server required
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")
os.environ.setdefault("ALERTS_ENABLED", "false")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_signal(
    signal="dead_letter_count",
    value=3.0,
    threshold=1.0,
    severity="high",
    connector="google-analytics",
):
    from core.infra_alerts import AlertSignal

    return AlertSignal(
        signal=signal,
        value=value,
        threshold=threshold,
        severity=severity,
        connector=connector,
    )


# ---------------------------------------------------------------------------
# EmailChannel tests (AC5)
# ---------------------------------------------------------------------------


class TestEmailChannel:
    def test_send_calls_smtp_with_host_and_port(self):
        """smtplib.SMTP is called with configured host and port."""
        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="localhost",
            smtp_port=1025,
            email_to="alert@example.com",
            email_from="toorow@example.com",
        )
        signal = make_signal()

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp_instance) as mock_smtp_cls:
            channel.send(signal)

        mock_smtp_cls.assert_called_once_with("localhost", 1025)

    def test_send_uses_correct_subject_prefix(self):
        """Subject starts with [toorow]."""
        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="localhost",
            smtp_port=1025,
            email_to="alert@example.com",
            email_from="toorow@example.com",
        )
        signal = make_signal(signal="dead_letter_count")

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            channel.send(signal)

        # Extract the message sent via sendmail (subject line is always plain text)
        assert mock_smtp_instance.sendmail.called
        _from, _to, raw_msg = mock_smtp_instance.sendmail.call_args[0]
        # Subject header is always plain ASCII in the raw message
        assert "Subject: [toorow] Infra alert: dead_letter_count" in raw_msg

    def test_send_uses_correct_recipient(self):
        """sendmail is called with ALERT_EMAIL_TO as recipient."""
        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="localhost",
            smtp_port=1025,
            email_to="recipient@example.com",
            email_from="sender@example.com",
        )
        signal = make_signal()

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            channel.send(signal)

        _from, recipients, _msg = mock_smtp_instance.sendmail.call_args[0]
        assert "recipient@example.com" in recipients

    def test_send_body_contains_signal_name_and_threshold(self):
        """Email body contains the signal name and threshold value."""
        from email import message_from_string

        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="localhost",
            smtp_port=1025,
            email_to="alert@example.com",
            email_from="toorow@example.com",
        )
        signal = make_signal(signal="mirror_sync_lag_seconds", value=4000.0, threshold=3600.0)

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            channel.send(signal)

        _from, _to, raw_msg = mock_smtp_instance.sendmail.call_args[0]
        # The subject line is always plain text
        assert "mirror_sync_lag_seconds" in raw_msg  # subject contains signal

        # Body may be base64-encoded (UTF-8 email); decode to check content
        msg = message_from_string(raw_msg)
        payload = msg.get_payload(decode=True)
        if payload is not None:
            body_text = payload.decode("utf-8")
        else:
            body_text = msg.get_payload()

        assert "mirror_sync_lag_seconds" in body_text
        assert "3600" in body_text  # threshold value in body

    def test_send_calls_login_when_credentials_provided(self):
        """smtp.login() is called when smtp_user and smtp_password are set."""
        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="localhost",
            smtp_port=587,
            smtp_user="user@example.com",
            smtp_password="secret",
            email_to="alert@example.com",
            email_from="toorow@example.com",
        )
        signal = make_signal()

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            channel.send(signal)

        mock_smtp_instance.login.assert_called_once_with("user@example.com", "secret")

    def test_send_does_not_call_login_when_no_credentials(self):
        """smtp.login() is NOT called when no credentials are provided."""
        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="localhost",
            smtp_port=1025,
            smtp_user=None,
            smtp_password=None,
            email_to="alert@example.com",
            email_from="toorow@example.com",
        )
        signal = make_signal()

        mock_smtp_instance = MagicMock()
        mock_smtp_instance.__enter__ = MagicMock(return_value=mock_smtp_instance)
        mock_smtp_instance.__exit__ = MagicMock(return_value=False)

        with patch("smtplib.SMTP", return_value=mock_smtp_instance):
            channel.send(signal)

        mock_smtp_instance.login.assert_not_called()

    def test_send_raises_on_smtp_failure(self):
        """EmailChannel.send raises when SMTP fails (notify_alert catches it)."""
        from core.infra_alerts import EmailChannel

        channel = EmailChannel(
            smtp_host="unreachable-host",
            smtp_port=9999,
            email_to="alert@example.com",
            email_from="toorow@example.com",
        )
        signal = make_signal()

        with patch("smtplib.SMTP", side_effect=ConnectionRefusedError("refused")):
            with pytest.raises(ConnectionRefusedError):
                channel.send(signal)


# ---------------------------------------------------------------------------
# build_channels tests
# ---------------------------------------------------------------------------


class TestBuildChannels:
    def test_always_includes_console_channel(self):
        """build_channels always returns at least one ConsoleChannel."""
        from core.infra_alerts import ConsoleChannel, build_channels

        with patch.dict(
            os.environ,
            {"ALERT_EMAIL_ENABLED": "false", "SMTP_HOST": ""},
            clear=False,
        ):
            channels = build_channels()

        assert any(isinstance(c, ConsoleChannel) for c in channels)

    def test_adds_email_channel_when_enabled_and_smtp_host_set(self):
        """EmailChannel added when ALERT_EMAIL_ENABLED=true and SMTP_HOST is set."""
        from core.infra_alerts import ConsoleChannel, EmailChannel, build_channels

        with patch.dict(
            os.environ,
            {
                "ALERT_EMAIL_ENABLED": "true",
                "SMTP_HOST": "localhost",
                "SMTP_PORT": "1025",
                "ALERT_EMAIL_TO": "alert@example.com",
            },
            clear=False,
        ):
            channels = build_channels()

        assert any(isinstance(c, ConsoleChannel) for c in channels)
        assert any(isinstance(c, EmailChannel) for c in channels)

    def test_no_email_channel_when_smtp_host_empty(self):
        """EmailChannel NOT added when SMTP_HOST is empty (even if ALERT_EMAIL_ENABLED=true)."""
        from core.infra_alerts import ConsoleChannel, EmailChannel, build_channels

        with patch.dict(
            os.environ,
            {"ALERT_EMAIL_ENABLED": "true", "SMTP_HOST": ""},
            clear=False,
        ):
            channels = build_channels()

        assert any(isinstance(c, ConsoleChannel) for c in channels)
        assert not any(isinstance(c, EmailChannel) for c in channels)
