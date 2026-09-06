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


def _arrival_hour(value: Any) -> int:
    """The hour of the LOCAL day a daily pull is expected to arrive (story 57.8).

    A closed 0..23 range, refused here rather than clamped: an hour outside the
    day is a caller mistake, and silently folding 24 into 0 would move a run
    twenty-four hours away from where the caller believed they put it.
    """
    hour = int(value)
    if hour < 0 or hour > 23:
        raise ValueError("arrival_hour_local must be an hour of the local day, between 0 and 23")
    return hour


def _local_arrival(local_date: date, hour: int, zone: ZoneInfo) -> datetime:
    """The named local hour of *local_date*, resolved in *zone*, as a UTC instant.

    Combined in the LOCAL calendar and converted afterwards. Adding an offset to
    a UTC instant instead walks off the chosen hour every time the zone changes
    offset, and nothing corrects the drift: the hour a person named quietly
    becomes another one.
    """
    return datetime.combine(local_date, time(hour=hour), tzinfo=zone).astimezone(UTC)


def _daily_bounds(effective_now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    current_local_date = effective_now.astimezone(zone).date()
    end = _local_midnight(current_local_date, zone)
    if end > effective_now:
        current_local_date -= timedelta(days=1)
        end = _local_midnight(current_local_date, zone)
    start = _local_midnight(current_local_date - timedelta(days=1), zone)
    return start, end


def _weekly_bounds(effective_now: datetime, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Seven LOCAL days, ending at the same local midnight a daily window ends on.

    AI-217. Computed in the local calendar and converted afterwards, like every
    other bound here: subtracting 168 hours from a UTC instant crosses a
    daylight-saving change one hour off, and a window that starts an hour early
    every autumn re-reads a day nobody asked for.

    Seven and not three: a run must cover the interval it is responsible for, or
    the days between two runs are never fetched and every run still reports
    success. The dispatcher floors the same number for the same reason
    (`scheduler._dispatch_nightly_datastreams`).
    """
    _previous_day_start, end = _daily_bounds(effective_now, zone)
    start = _local_midnight(end.astimezone(zone).date() - timedelta(days=7), zone)
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

    Daily and weekly windows use local calendar midnights and may therefore span
    23 or 25 UTC hours at DST transitions. Hourly windows use a monotonic UTC
    timeline so repeated local labels never own the same source instant twice.
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

    # Story 57.8. The hour the daily pull is expected to ARRIVE, chosen per
    # Datastream. Absent means local midnight, which is exactly what activation
    # has written since it started writing `next_run_at` -- the addition never
    # moves a row that names no hour.
    #
    # `run_at_hour` is the key the intent schema ALREADY declares for this
    # ("Hour of day (0-23, in `timezone`) the collection should run"), unused
    # until now. The stable row calls the same value `arrival_hour_local`
    # because that is the product's word for it and the column is on
    # `app.datastreams`; they are one setting, not two.
    arrival_hour = policy.get("run_at_hour")

    if mode in ("daily", "weekly"):
        # AI-217: `weekly` was legal in the database, offered by three doors and
        # dispatched -- and this function raised `unsupported schedule mode` on
        # it, with a bare ValueError the caller could not even name. The two
        # cadences share everything but the length of one period: both run once
        # per period, both may name an arrival hour (A3), both measure a window
        # of whole local days.
        interval = timedelta(days=7 if mode == "weekly" else 1)
        base_start, base_end = (
            _weekly_bounds(effective_now, zone)
            if mode == "weekly"
            else _daily_bounds(effective_now, zone)
        )
        # THE FIRST RUN IS THE NEXT ARRIVAL, NOT A WHOLE PERIOD AWAY, and the
        # choice is deliberate for `weekly`: the window this run fetches covers
        # the seven days that have already happened, so waiting a week to ask for
        # data that exists would be a week of empty screen bought for nothing.
        # The CADENCE begins after it -- `scheduler._advance_next_run` moves the
        # row seven days on each dispatch, from the arrival hour it names.
        next_local_date = base_end.astimezone(zone).date() + timedelta(days=1)
        next_run = (
            _local_midnight(next_local_date, zone) + delay
            if arrival_hour is None
            else _local_arrival(next_local_date, _arrival_hour(arrival_hour), zone)
        )
    elif mode == "hourly":
        # A3: an intraday cadence re-pulls the accumulating day, so there is no
        # single moment for it to arrive. Honouring an arrival hour here would
        # collapse every hourly run onto one hour a day.
        interval = timedelta(minutes=int(policy["interval_minutes"]))
        base_start, base_end = _hourly_bounds(effective_now, interval)
        next_run = base_end + interval + delay
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
        next_run_at=next_run,
        lookback_overlap_minutes=lookback_minutes,
        coalesced_windows=coalesced_windows,
    )
