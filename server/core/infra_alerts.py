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
  (ALERT_VERIFICATION_THRESHOLD is RETIRED -- AI-101 replaced the platform-wide
   completeness count with a per-Datastream empty streak; see core/verification_streaks.py)
  ALERT_HEALTH_POLLER_STALE_SECONDS default 7200  -- staleness_seconds >= threshold -> alert
  ALERTS_ENABLED                    default true   -- off-switch, not an arming
  ALERT_EMAIL_ENABLED               default false  -- enable SMTP email channel
  ALERT_EMAIL_TO                                   -- recipient address
  ALERT_EMAIL_FROM                  default toorow@example.com
  ALERT_LINK_BASE_URL               default http://localhost:5173
  SMTP_HOST                                        -- required for email channel
  SMTP_PORT                         default 587
  SMTP_USER                                        -- optional SMTP auth
  SMTP_PASSWORD                                    -- optional SMTP auth

AD-2: no module-specific strings.
HG-5 is carried by SCHEDULER_ENABLED (default false), which is what keeps the
evaluator out of CI: the scheduler thread never starts there.
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
from typing import Mapping, Sequence
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

#: The shortest run of empty verifications worth a signal. TWO, not one: a single
#: empty pull is an ordinary fact about a quiet day, and firing on it would make
#: the alert the noise this change exists to remove. The second one in a row is
#: the first that says something new.
_EMPTY_STREAK_MINIMUM = 2

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
    #: The sentence a person reads, when the alert has one (story 59.6). Three of
    #: the four `evaluate_alerts` signals have none -- they are a measurement
    #: against a threshold and nothing more. A PERSISTED firing does:
    #: `alert_firings.message` is what the monitor wrote, and dropping it on the
    #: way to a destination would send a recipient a metric with no sentence.
    #:
    #: AI-101: `verification_failure_count` now carries one, and it is still a
    #: measurement -- WHICH Datastreams are verifying empty and since when, read
    #: from `verification_streaks`. The count alone could not distinguish a
    #: one-off empty pull from a source dead for a month, so a reader had no way
    #: to tell whether the alert had become the noise it warned about.
    message: str | None = None


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

            # Verification failures -- ONE SIGNAL PER DATASTREAM (AI-101).
            #
            # It was a PLATFORM-WIDE count over 24 h raised with
            # `connector=None`: it named no Datastream, so a one-off empty pull
            # and a source dead for a month produced the same line, and the
            # question `execution-substrate.md` asked -- does an empty stream
            # stay scheduled? -- could not even be evaluated. Fourteen modules
            # read `app.pull_verifications` and not one read it per stream.
            #
            # A streak carries the stream, the run length and the date it went
            # quiet, so a reader can act on it and a destination can deduplicate
            # on `streak.key` instead of re-firing the same silence daily. The
            # cadence itself is UNCHANGED: nothing here unschedules anything --
            # see the decision recorded in `execution-substrate.md`.
            from core.verification_streaks import read_empty_streaks  # noqa: PLC0415

            reading = read_empty_streaks(conn, min_streak=_EMPTY_STREAK_MINIMUM)
            for streak in reading.streams:
                breaches.append(
                    AlertSignal(
                        signal="datastream_empty_streak",
                        value=float(streak.empty_streak),
                        threshold=float(_EMPTY_STREAK_MINIMUM),
                        severity="high",
                        connector=streak.module_name,
                        message=streak.describe(),
                    )
                )
            # THE BLIND SPOT TRAVELS WITH THE READING, never silently. A verdict on
            # a job carrying no `datastream_id` belongs to no stream, so an empty
            # `streams` list can mean "nothing is quiet" OR "nothing is
            # attributable" -- and those must not be reported by the same silence.
            if reading.unattributed_empty_pulls:
                breaches.append(
                    AlertSignal(
                        signal="unattributed_empty_pulls",
                        value=float(reading.unattributed_empty_pulls),
                        threshold=1.0,
                        severity="medium",
                        connector=None,
                        message=(
                            f"{reading.unattributed_empty_pulls} empty pull(s) carry "
                            "no datastream_id and are attributed to no stream"
                        ),
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


def _firing_window_date(
    window_date: date | str | None, metadata: Mapping | None
) -> date:
    """The day the finding SPEAKS of, in order of decreasing honesty.

    1. what the caller named;
    2. `metadata["window_date"]`, which every DQ monitor has always filled and
       which reached the row only concatenated into `message`;
    3. today -- for the infra events (AI-32, AI-41) that are genuinely about now.

    An unparseable value falls through rather than raising: this writer is
    best-effort and a firing lost to a malformed date would be an alert deleted by
    its own metadata.
    """
    candidates: list = [window_date]
    if isinstance(metadata, Mapping):
        candidates.append(metadata.get("window_date"))
    for candidate in candidates:
        if isinstance(candidate, datetime):
            return candidate.date()
        if isinstance(candidate, date):
            return candidate
        if isinstance(candidate, str) and candidate.strip():
            try:
                return date.fromisoformat(candidate.strip()[:10])
            except ValueError:
                continue
    return date.today()


def _firing_datastream_id(metadata: Mapping | None) -> str | None:
    """WHICH Datastream this finding is about, or None for an infra event.

    Read from `metadata`, where every DQ monitor has always put it, for the same
    reason `_firing_window_date` reads the date there: the six checks that never
    learned a new argument stop being anonymous tonight, without a call site
    moving. A value that is not a plain identifier is refused rather than
    coerced -- a dedup key built from a stray dict would silently stop
    deduplicating.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("datastream_id")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return None


def _firing_finding_key(metadata: Mapping | None) -> str | None:
    """WHAT this finding is about, when the Datastream and the day do not say it.

    Read from `metadata` for the same measured reason `_firing_datastream_id` is:
    the eight checks that never learned a new argument keep their exact identity
    without a call site moving.

    Migration 229 dedups a DQ firing on `(project_id, type, datastream_id,
    window_date)` -- "one finding, one day, one Datastream, one alert" -- which is
    right for a monitor whose finding IS the Datastream (its volume, its lateness,
    its schema). The ninth monitor's finding is `(datastream, dimension, reason)`,
    so two real findings of one Datastream on one night collapsed into one row and
    the second was lost to `ON CONFLICT DO NOTHING`, in silence. Migration 326
    widens the index by `COALESCE(finding_key, '')`, so a writer that states
    nothing keeps migration 229's identity exactly.
    """
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("finding_key")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()[:200]
    return None


#: The two readings a person can ask of `app.alert_firings`, and the two words
#: the console sends to ask them. There is no `status` column on that table:
#: `acknowledged_at` alone carries the notion, which is exactly why it kept
#: being respelled at each call site.
FIRING_STATUSES: tuple[str, ...] = ("open", "acknowledged")


def firing_status_predicate(status: str, *, alias: str | None = None) -> str:
    """The `WHERE` fragment for an OPEN or an ACKNOWLEDGED firing -- spelled once.

    `app.alert_firings` is written here, so the sentence that says whether one of
    its rows is still open belongs here too. Until 2026-08-21 it did not exist:
    `acknowledged_at IS NULL` was typed three times in `dq_api` and once in
    `project_readiness`, and `dq_api` renders the result to a person as "open
    issues" -- the same word `dq_governance.open_issue_predicate` answers for
    `app.dq_issues`. Two tables, two lifecycles, and one word: the guard AI-235
    put on the anomaly predicate said nothing about this one because it looked
    for the anomaly's spelling, not for the notion.

    A firing and an anomaly are NOT the same object and this predicate is not a
    second reading of that one -- an alert is a delivery, an anomaly is a
    finding. What they share is the word the screen prints, and a word rendered
    to a person must resolve to exactly one sentence per object.

    `alias` prefixes the column (``alias="af"`` reads ``af.acknowledged_at``) so
    a JOIN stays unambiguous without respelling anything.
    """

    if status not in FIRING_STATUSES:
        raise ValueError(
            f"a firing is {' or '.join(FIRING_STATUSES)}; {status!r} is neither, "
            "and inventing a third reading here would be a second vocabulary"
        )
    col = f"{alias}." if alias else ""
    return f"{col}acknowledged_at IS {'NOT NULL' if status == 'acknowledged' else 'NULL'}"


#: The bare-column reading of "this firing is still open", DERIVED from the
#: builder above like every other predicate noun in this package.
OPEN_FIRING_PREDICATE = firing_status_predicate("open")


def write_infra_firing(
    alert_type: str,
    project_id: str | None = None,
    metric: str = "infra",
    severity: str = "error",
    message: str = "",
    metadata: dict | None = None,
    observed_value: float | None = None,
    threshold: float | None = None,
    pull_ids: Sequence[str] | None = None,
    window_date: date | str | None = None,
) -> None:
    """Insert one row into app.alert_firings with the given *alert_type* as the type column.

    Used by AI-32 (scheduler_step_degraded) and AI-41 (nango_revoke_failed) to surface
    infra-level events through the existing alert delivery pipeline.

    Design:
      - Never raises: best-effort, logs at debug on failure.
      - *metadata* is serialised to JSON and stored in the message field appended
        after the human-readable *message* so the data is accessible without a
        schema change (alert_firings has no separate metadata column at P3-dev).
      - A system-level event with no Project scope carries `project_id=None`,
        which is NULL in the row: PLATFORM SCOPE (migration 291). It used to
        default to the literal `'default'`, a Project id from before the
        multi-project layer; production has no such row, so the foreign key
        refused every firing that took this default and the caller's `except`
        turned each one into a log line (AI-306, measured on Cloud Run
        2026-08-18T00:32:25Z). A caller that HAS a Project still passes it --
        `nango_revoke_failed` names the Project being archived.

    STORY 59.4, ARBITRAGE 5 -- THE FOUR COLUMNS THIS FUNCTION USED TO INVENT.
    `observed_value`, `threshold` and `pull_ids` were written as `0`, `0` and
    `'{}'` in the SQL literal, and `window_date` as `date.today()`. The last one
    was not an approximation but a falsehood: a finding about the 5th, written on
    the 8th, said the 8th. Every DQ monitor already carried the real date in
    `metadata["window_date"]`, where only a human reading a concatenated message
    could find it.

    The signature is shared by all seven checks, so the repair lands here rather
    than in one of them. `window_date` FALLS BACK to `metadata["window_date"]`
    before `date.today()`: that is what makes it a class repair -- the six checks
    that never learned to pass the argument stop lying tonight, without a call site
    moving. The 2423 rows already written do not move; nothing here rewrites them.
    """
    import json as _json  # noqa: PLC0415

    try:
        from ulid import ULID  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        firing_id = f"fire_{ULID()}"
        window_day = _firing_window_date(window_date, metadata)
        datastream_id = _firing_datastream_id(metadata)
        finding = _firing_finding_key(metadata)
        full_message = message
        if metadata:
            full_message = f"{message} | meta={_json.dumps(metadata, default=str)}"
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.alert_firings
                        (id, definition_id, type, project_id, metric, fired_at,
                         observed_value, threshold, pull_ids, window_date, severity,
                         message, datastream_id, finding_key)
                    VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    -- Migration 229: one finding, one day, one Datastream, one row.
                    -- `run-dq-monitors` ticks every fifteen minutes
                    -- (`infra/gcp/provision_ad36_substrate.sh:113`), and past the
                    -- due hour a Datastream still missing yesterday is still
                    -- missing it at every tick, so the same fact would be restated
                    -- about sixty times a day. DO NOTHING rather than a refusal:
                    -- re-stating a known fact is not an error, it is the normal
                    -- shape of a frequent tick, and it must not abort the
                    -- transaction. Untargeted on purpose, so a kind that gains a
                    -- dedup index later needs no change here.
                    --
                    -- WHAT THE 2421 EXISTING ROWS ARE NOT: they are not that tick.
                    -- Measured under role `postgres` on `date_trunc('minute',
                    -- fired_at)` -- thirteen distinct minutes, all on 2026-08-05,
                    -- NONE on a quarter hour, one `window_date`. Irregular bursts
                    -- on one afternoon are a harness driving the endpoint by hand
                    -- on a disposable base. The dedup above is justified by the
                    -- tick's cadence, never by those rows.
                    ON CONFLICT DO NOTHING
                    RETURNING id
                    """,
                    (
                        firing_id,
                        alert_type,
                        project_id,
                        metric,
                        datetime.now(tz=timezone.utc),
                        float(observed_value or 0),
                        float(threshold or 0),
                        # TEXT[] NOT NULL DEFAULT '{}' (`011_create_alert_tables.sql:38`):
                        # the provenance of the measurement, empty when there is none.
                        [str(pull) for pull in (pull_ids or []) if pull],
                        window_day,
                        severity,
                        full_message,
                        datastream_id,
                        # Migration 326: NULL for every writer whose finding is
                        # the Datastream itself, which is the eight of nine that
                        # pass no `finding_key` in their metadata.
                        finding,
                    ),
                )
                written = cur.fetchone() is not None
            conn.commit()
        if written:
            logger.info(
                "infra_alerts: write_infra_firing: type=%s project=%s ds=%s day=%s",
                alert_type,
                project_id,
                datastream_id or "-",
                window_day,
            )
        else:
            # Not a failure, and it must be visible: a tick that observed a known
            # fact did its job. Counting these is how the frequency of a finding
            # stays readable once the rows stop multiplying.
            logger.info(
                "infra_alerts: firing_already_stated: type=%s project=%s ds=%s day=%s",
                alert_type,
                project_id,
                datastream_id or "-",
                window_day,
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
        """Send SMTP email for this alert. Raises on failure.

        STORY 59.6 -- THE BODY IS ENGLISH. Four of its eight labels were French
        ("Connecteur", "Valeur", "Seuil", "Horodatage") on a product whose
        console, glossary and monitor labels are entirely English. It is the
        ONLY text this product sends outside itself, which is exactly why it was
        never caught: no screen renders it and no test asserted a word of it.
        """
        subject = f"[toorow] Infra alert: {alert.signal}"
        link = f"{self.link_base_url}/{alert.signal}"
        source_line = f"\nSource    : {alert.connector}" if alert.connector else ""
        # A persisted firing carries the monitor's own sentence; the four infra
        # signals carry none, and an empty line is better than an invented one.
        message_line = f"\nMessage   : {alert.message}" if alert.message else ""
        body = (
            f"Infrastructure alert detected.\n\n"
            f"Signal    : {alert.signal}{source_line}{message_line}\n"
            f"Value     : {alert.value}\n"
            f"Threshold : {alert.threshold}\n"
            f"Severity  : {alert.severity}\n"
            f"Timestamp : {alert.timestamp}\n\n"
            f"Link      : {link}\n"
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
