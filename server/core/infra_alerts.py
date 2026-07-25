"""toorow -- Infra alert evaluator (Story 5.2, AC1, AC2, AC3).

Reads pipeline health signals from Postgres and in-process state, compares
against env-driven thresholds, and dispatches notifications through pluggable
channels (console + optional SMTP email).

Design principles:
  - Never raises: all reader functions catch exceptions and return safe defaults.
  - Best-effort: DB unreachable or mirror never synced -> no alerts for those signals.
  - Synchronous: matches existing psycopg v3 sync + smtplib patterns.
  - No new dependencies: stdlib smtplib, existing psycopg / core.mirror_sync.

Environment variables (all optional, env-driven thresholds):
  ALERT_DEAD_LETTER_THRESHOLD       default 1     -- count >= threshold -> alert
  ALERT_MIRROR_LAG_THRESHOLD_SECONDS default 3600  -- lag_seconds >= threshold -> alert
  ALERT_VERIFICATION_THRESHOLD      default 0.5   -- completeness_ratio < threshold -> fail
  ALERT_HEALTH_POLLER_STALE_SECONDS default 7200  -- staleness_seconds >= threshold -> alert
  ALERTS_ENABLED                    default false  -- master switch
  ALERT_EMAIL_ENABLED               default false  -- enable SMTP email channel
  ALERT_EMAIL_TO                                   -- recipient address
  ALERT_EMAIL_FROM                  default toorow@example.com
  ALERT_LINK_BASE_URL               default http://localhost:5173
  SMTP_HOST                                        -- required for email channel
  SMTP_PORT                         default 587
  SMTP_USER                                        -- optional SMTP auth
  SMTP_PASSWORD                                    -- optional SMTP auth

AD-2: no module-specific strings.
HG-5: ALERTS_ENABLED defaults to false -- evaluator never called in CI/tests.
"""

from __future__ import annotations

import json
import logging
import os
import smtplib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.mime.text import MIMEText
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_ALERT_LINK_BASE_URL_DEFAULT = "http://localhost:5173"


def _validated_link_base_url(raw: str) -> str:
    """Return raw if it is an http/https URL with a non-empty netloc; else fall back.

    Parses with urllib.parse. Never raises. Logs a WARNING on invalid input.
    """
    try:
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return raw
    except Exception:
        pass
    logger.warning(
        "infra_alerts: ALERT_LINK_BASE_URL '%s' is invalid (must be http/https with host) "
        "-- falling back to default '%s'",
        raw,
        _ALERT_LINK_BASE_URL_DEFAULT,
    )
    return _ALERT_LINK_BASE_URL_DEFAULT


# ---------------------------------------------------------------------------
# T1.1 -- AlertSignal dataclass
# ---------------------------------------------------------------------------


@dataclass
class AlertSignal:
    """Represents a single breached alert signal."""

    signal: str
    value: float
    threshold: float
    severity: str
    connector: str | None = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(tz=timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# T1.2 -- _read_dead_letter_count
# ---------------------------------------------------------------------------


def _read_dead_letter_count(conn, hours: int = 24) -> dict[str, int]:
    """Query pull_jobs for dead_letter rows in the last N hours, grouped by connection.

    Returns {connector_name_or_id: count}. Returns {} on any error.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT r.provider, COUNT(*) AS cnt
                FROM app.pull_jobs j
                JOIN app.connection_ref r ON r.id = j.connection_ref_id
                WHERE j.state = 'dead_letter'
                  AND j.enqueued_at >= NOW() - INTERVAL '%s hours'
                GROUP BY r.provider
                """,
                (hours,),
            )
            rows = cur.fetchall()
        return {row[0]: int(row[1]) for row in rows} if rows else {}
    except Exception as exc:
        logger.warning("infra_alerts: dead_letter_count_error: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# T1.3 -- _read_mirror_lag
# ---------------------------------------------------------------------------


def _read_mirror_lag() -> float | None:
    """Read lag_seconds from mirror_sync._last_sync_result.

    Returns None if mirror has never synced or lag is unavailable.
    """
    try:
        from core import mirror_sync  # noqa: PLC0415

        result = mirror_sync._last_sync_result
        if result is None or "error" in result:
            return None
        lag = result.get("lag_seconds")
        return float(lag) if lag is not None else None
    except Exception as exc:
        logger.warning("infra_alerts: mirror_lag_read_error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# T1.4 -- _read_verification_failures
# ---------------------------------------------------------------------------


def _read_verification_failures(
    conn, hours: int = 24, threshold: float = 0.5
) -> int:
    """Count pull_verifications rows with completeness_ratio < threshold in last N hours.

    Returns 0 on any error.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*)
                FROM app.pull_verifications
                WHERE completeness_ratio < %s
                  AND verified_at >= NOW() - INTERVAL '%s hours'
                """,
                (threshold, hours),
            )
            row = cur.fetchone()
        return int(row[0]) if row else 0
    except Exception as exc:
        logger.warning("infra_alerts: verification_failures_error: %s", exc)
        return 0


# ---------------------------------------------------------------------------
# T1.5 -- _read_health_poller_staleness
# ---------------------------------------------------------------------------


def _read_health_poller_staleness(conn) -> float | None:
    """Query connection_health for MAX(last_checked_at), return staleness seconds.

    Returns None if no rows exist in connection_health.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT MAX(last_checked_at) FROM app.connection_health"
            )
            row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        last_checked = row[0]
        # Ensure timezone-aware comparison
        now = datetime.now(tz=timezone.utc)
        if last_checked.tzinfo is None:
            last_checked = last_checked.replace(tzinfo=timezone.utc)
        staleness = (now - last_checked).total_seconds()
        return max(0.0, staleness)
    except Exception as exc:
        logger.warning("infra_alerts: health_poller_staleness_error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# T1.6 -- evaluate_alerts
# ---------------------------------------------------------------------------


def evaluate_alerts(project_id: str | None = None) -> list[AlertSignal]:
    """Evaluate all alert signals and return list of breached AlertSignals.

    Returns empty list if all clear. Never raises.

    Environment-driven thresholds (see module docstring for env var names).
    The project_id parameter is reserved for future per-project scoping.
    """
    dead_letter_threshold = int(
        os.environ.get("ALERT_DEAD_LETTER_THRESHOLD", "1")
    )
    mirror_lag_threshold = float(
        os.environ.get("ALERT_MIRROR_LAG_THRESHOLD_SECONDS", "3600")
    )
    verification_threshold = float(
        os.environ.get("ALERT_VERIFICATION_THRESHOLD", "0.5")
    )
    health_poller_stale_threshold = float(
        os.environ.get("ALERT_HEALTH_POLLER_STALE_SECONDS", "7200")
    )

    breaches: list[AlertSignal] = []

    # --- DB-based signals ---
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Dead-letter counts (per connector)
            dead_letter_counts = _read_dead_letter_count(conn)
            for connector_name, count in dead_letter_counts.items():
                if count >= dead_letter_threshold:
                    breaches.append(
                        AlertSignal(
                            signal="dead_letter_count",
                            value=float(count),
                            threshold=float(dead_letter_threshold),
                            severity="high",
                            connector=connector_name,
                        )
                    )

            # Verification failures
            fail_count = _read_verification_failures(
                conn, threshold=verification_threshold
            )
            if fail_count > 0:
                breaches.append(
                    AlertSignal(
                        signal="verification_failure_count",
                        value=float(fail_count),
                        threshold=float(verification_threshold),
                        severity="high",
                        connector=None,
                    )
                )

            # Health poller staleness
            staleness = _read_health_poller_staleness(conn)
            if staleness is not None and staleness >= health_poller_stale_threshold:
                breaches.append(
                    AlertSignal(
                        signal="health_poller_staleness_seconds",
                        value=staleness,
                        threshold=health_poller_stale_threshold,
                        severity="medium",
                        connector=None,
                    )
                )

    except Exception as exc:
        logger.warning("infra_alerts: db_connection_error: %s", exc)

    # --- In-process signals (no DB) ---
    lag = _read_mirror_lag()
    if lag is not None and lag >= mirror_lag_threshold:
        breaches.append(
            AlertSignal(
                signal="mirror_sync_lag_seconds",
                value=lag,
                threshold=mirror_lag_threshold,
                severity="medium",
                connector=None,
            )
        )

    if breaches:
        logger.info(
            "infra_alerts: evaluate_alerts: %d breach(es) detected", len(breaches)
        )
    else:
        logger.debug("infra_alerts: evaluate_alerts: all clear")

    return breaches


# ---------------------------------------------------------------------------
# AI-32 / AI-41 -- write_infra_firing: generic best-effort firing writer
# ---------------------------------------------------------------------------


def write_infra_firing(
    alert_type: str,
    project_id: str = "default",
    metric: str = "infra",
    severity: str = "error",
    message: str = "",
    metadata: dict | None = None,
) -> None:
    """Insert one row into app.alert_firings with the given *alert_type* as the type column.

    Used by AI-32 (scheduler_step_degraded) and AI-41 (nango_revoke_failed) to surface
    infra-level events through the existing alert delivery pipeline.

    Design:
      - Never raises: best-effort, logs at debug on failure.
      - *metadata* is serialised to JSON and stored in the message field appended
        after the human-readable *message* so the data is accessible without a
        schema change (alert_firings has no separate metadata column at P3-dev).
      - Uses 'default' project_id for system-level events with no project scope.
    """
    import json as _json  # noqa: PLC0415

    try:
        from ulid import ULID  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        firing_id = f"fire_{ULID()}"
        window_date = date.today()
        full_message = message
        if metadata:
            full_message = f"{message} | meta={_json.dumps(metadata, default=str)}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.alert_firings
                        (id, definition_id, type, project_id, metric, fired_at,
                         observed_value, threshold, pull_ids, window_date, severity, message)
                    VALUES (%s, NULL, %s, %s, %s, %s, 0, 0, '{}', %s, %s, %s)
                    """,
                    (
                        firing_id,
                        alert_type,
                        project_id,
                        metric,
                        datetime.now(tz=timezone.utc),
                        window_date,
                        severity,
                        full_message,
                    ),
                )
            conn.commit()
        logger.info(
            "infra_alerts: write_infra_firing: type=%s project=%s", alert_type, project_id
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "infra_alerts: write_infra_firing_failed: type=%s: %s", alert_type, exc
        )


# ---------------------------------------------------------------------------
# T2 -- Notification channel abstraction
# ---------------------------------------------------------------------------


class NotificationChannel(ABC):
    """Abstract base for alert notification channels."""

    @abstractmethod
    def send(self, alert: AlertSignal) -> None:
        """Send alert through this channel. May raise on failure."""
        ...


# ---------------------------------------------------------------------------
# T2.2 -- ConsoleChannel
# ---------------------------------------------------------------------------


class ConsoleChannel(NotificationChannel):
    """Logs structured JSON alert to logger.warning (always enabled)."""

    def send(self, alert: AlertSignal) -> None:
        record = {
            "event": "infra_alert",
            "severity": alert.severity,
            "signal": alert.signal,
            "value": alert.value,
            "threshold": alert.threshold,
            "connector": alert.connector,
            "timestamp": alert.timestamp,
        }
        logger.warning("infra_alert: %s", json.dumps(record))


# ---------------------------------------------------------------------------
# T2.3 -- EmailChannel
# ---------------------------------------------------------------------------


class EmailChannel(NotificationChannel):
    """SMTP email notification channel (sync, matching existing psycopg pattern).

    Reads configuration from env vars at construction time.
    """

    def __init__(
        self,
        smtp_host: str,
        smtp_port: int = 587,
        smtp_user: str | None = None,
        smtp_password: str | None = None,
        email_to: str = "",
        email_from: str = "toorow@example.com",
        link_base_url: str = "http://localhost:5173",
    ) -> None:
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.smtp_user = smtp_user
        self.smtp_password = smtp_password
        self.email_to = email_to
        self.email_from = email_from
        self.link_base_url = link_base_url

    def send(self, alert: AlertSignal) -> None:
        """Send SMTP email for this alert. Raises on failure."""
        subject = f"[toorow] Infra alert: {alert.signal}"
        link = f"{self.link_base_url}/{alert.signal}"
        connector_line = f"\nConnecteur : {alert.connector}" if alert.connector else ""
        body = (
            f"Alerte infrastructure détectée.\n\n"
            f"Signal    : {alert.signal}{connector_line}\n"
            f"Valeur    : {alert.value}\n"
            f"Seuil     : {alert.threshold}\n"
            f"Sévérité  : {alert.severity}\n"
            f"Horodatage: {alert.timestamp}\n\n"
            f"Lien      : {link}\n"
        )

        msg = MIMEText(body, "plain", "utf-8")
        msg["Subject"] = subject
        msg["From"] = self.email_from
        msg["To"] = self.email_to

        with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
            if self.smtp_user and self.smtp_password:
                smtp.login(self.smtp_user, self.smtp_password)
            smtp.sendmail(self.email_from, [self.email_to], msg.as_string())


# ---------------------------------------------------------------------------
# T2.4 -- notify_alert
# ---------------------------------------------------------------------------


def notify_alert(
    alert: AlertSignal, channels: list[NotificationChannel]
) -> None:
    """Dispatch alert to all channels. One channel failure does not block others."""
    for channel in channels:
        try:
            channel.send(alert)
        except Exception as exc:
            logger.warning(
                "infra_alerts: channel_error: channel=%s signal=%s error=%s",
                type(channel).__name__,
                alert.signal,
                exc,
            )


# ---------------------------------------------------------------------------
# T2.5 -- build_channels
# ---------------------------------------------------------------------------


def build_channels() -> list[NotificationChannel]:
    """Build enabled notification channels from environment variables.

    Always includes ConsoleChannel. Adds EmailChannel when
    ALERT_EMAIL_ENABLED=true and SMTP_HOST is set.
    """
    channels: list[NotificationChannel] = [ConsoleChannel()]

    email_enabled = os.environ.get("ALERT_EMAIL_ENABLED", "false").lower() == "true"
    smtp_host = os.environ.get("SMTP_HOST", "").strip()

    if email_enabled and smtp_host:
        channels.append(
            EmailChannel(
                smtp_host=smtp_host,
                smtp_port=int(os.environ.get("SMTP_PORT", "587")),
                smtp_user=os.environ.get("SMTP_USER") or None,
                smtp_password=os.environ.get("SMTP_PASSWORD") or None,
                email_to=os.environ.get("ALERT_EMAIL_TO", ""),
                email_from=os.environ.get(
                    "ALERT_EMAIL_FROM", "toorow@example.com"
                ),
                link_base_url=_validated_link_base_url(
                    os.environ.get("ALERT_LINK_BASE_URL", _ALERT_LINK_BASE_URL_DEFAULT)
                ),
            )
        )
        logger.info(
            "infra_alerts: email_channel_enabled: host=%s to=%s",
            smtp_host,
            os.environ.get("ALERT_EMAIL_TO", ""),
        )

    return channels
