"""Pure timezone-aware half-open Datastream schedule calculations."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ScheduleWindow:
    """One eligible source interval and its explicit overlap metadata."""

    scheduled_for: datetime | None
    base_window_start: datetime | None
    base_window_end: datetime | None
    window_start: datetime | None
    window_end: datetime | None
    next_run_at: datetime | None
    lookback_overlap_minutes: int
    coalesced_windows: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "scheduled_for": _iso(self.scheduled_for),
            "base_window_start": _iso(self.base_window_start),
            "base_window_end": _iso(self.base_window_end),
            "window_start": _iso(self.window_start),
            "window_end": _iso(self.window_end),
            "next_run_at": _iso(self.next_run_at),
            "lookback_overlap_minutes": self.lookback_overlap_minutes,
            "coalesced_windows": self.coalesced_windows,
        }


def _iso(value: datetime | None) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if value is not None else None


def _require_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset() != timedelta(0):
        raise ValueError(f"{label} must be an aware UTC datetime")
    return value.astimezone(UTC)


def _local_midnight(local_date: date, zone: ZoneInfo) -> datetime:
    return datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)


def _daily_bounds(effective_now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    current_local_date = effective_now.astimezone(zone).date()
    end = _local_midnight(current_local_date, zone)
    if end > effective_now:
        current_local_date -= timedelta(days=1)
        end = _local_midnight(current_local_date, zone)
    start = _local_midnight(current_local_date - timedelta(days=1), zone)
    return start, end


def _hourly_bounds(effective_now: datetime, interval: timedelta) -> tuple[datetime, datetime]:
    interval_seconds = int(interval.total_seconds())
    end_timestamp = int(effective_now.timestamp()) // interval_seconds * interval_seconds
    end = datetime.fromtimestamp(end_timestamp, tz=UTC)
    return end - interval, end


def calculate_schedule_window(
    policy: dict[str, Any],
    *,
    now_utc: datetime,
    last_committed_watermark: datetime | None = None,
) -> ScheduleWindow:
    """Calculate a due half-open interval without enqueueing or mutating state.

    Daily windows use local calendar midnights and may therefore span 23 or 25
    UTC hours at DST transitions. Hourly windows use a monotonic UTC timeline so
    repeated local labels never own the same source instant twice.
    """

    now = _require_utc(now_utc, "now_utc")
    if last_committed_watermark is not None:
        watermark = _require_utc(last_committed_watermark, "last_committed_watermark")
    else:
        watermark = None

    mode = policy["mode"]
    if mode == "manual":
        return ScheduleWindow(None, None, None, None, None, None, 0, 0)

    delay_minutes = int(policy["watermark"]["delay_minutes"])
    delay = timedelta(minutes=delay_minutes)
    effective_now = now - delay
    zone = ZoneInfo(policy["timezone"])

    if mode == "daily":
        interval = timedelta(days=1)
        base_start, base_end = _daily_bounds(effective_now, zone)
        next_boundary = _local_midnight(base_end.astimezone(zone).date() + timedelta(days=1), zone)
    elif mode == "hourly":
        interval = timedelta(minutes=int(policy["interval_minutes"]))
        base_start, base_end = _hourly_bounds(effective_now, interval)
        next_boundary = base_end + interval
    else:
        raise ValueError(f"unsupported schedule mode: {mode}")

    missed = policy["missed_run"]
    if watermark is not None and watermark < base_start and missed["mode"] == "coalesce":
        earliest = base_end - interval * int(missed["max_catchup_windows"])
        base_start = max(watermark, earliest)

    duration = max(timedelta(0), base_end - base_start)
    coalesced_windows = max(1, math.ceil(duration / interval))
    lookback_minutes = int(policy["late_arrival"]["lookback_minutes"])
    window_start = base_start - timedelta(minutes=lookback_minutes)

    return ScheduleWindow(
        scheduled_for=base_end + delay,
        base_window_start=base_start,
        base_window_end=base_end,
        window_start=window_start,
        window_end=base_end,
        next_run_at=next_boundary + delay,
        lookback_overlap_minutes=lookback_minutes,
        coalesced_windows=coalesced_windows,
    )
