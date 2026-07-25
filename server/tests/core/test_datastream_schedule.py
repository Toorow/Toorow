"""Pure half-open Datastream scheduling tests (Story 12.2)."""

from __future__ import annotations

from datetime import datetime

import pytest


def _policy(
    *,
    mode: str = "hourly",
    interval_minutes: int | None = 60,
    timezone_name: str = "UTC",
    delay_minutes: int = 0,
    lookback_minutes: int = 0,
    missed_mode: str = "skip",
    max_catchup_windows: int = 7,
) -> dict:
    return {
        "mode": mode,
        "interval_minutes": interval_minutes,
        "timezone": timezone_name,
        "watermark": {"kind": "date_window", "delay_minutes": delay_minutes},
        "late_arrival": {"lookback_minutes": lookback_minutes},
        "retry": {
            "max_attempts": 5,
            "initial_backoff_seconds": 60,
            "max_backoff_seconds": 3600,
        },
        "missed_run": {
            "mode": missed_mode,
            "max_catchup_windows": max_catchup_windows,
        },
    }


def _utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def test_manual_schedule_has_no_window_or_next_run() -> None:
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="manual", interval_minutes=None),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    assert result.scheduled_for is None
    assert result.window_start is None
    assert result.window_end is None
    assert result.next_run_at is None


@pytest.mark.parametrize(
    ("now", "expected_start", "expected_end", "hours"),
    [
        ("2026-03-30T01:00:00Z", "2026-03-28T23:00:00Z", "2026-03-29T22:00:00Z", 23),
        ("2026-10-26T01:00:00Z", "2026-10-24T22:00:00Z", "2026-10-25T23:00:00Z", 25),
    ],
)
def test_daily_local_calendar_windows_respect_paris_dst(
    now: str, expected_start: str, expected_end: str, hours: int
) -> None:
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="daily", interval_minutes=1440, timezone_name="Europe/Paris"),
        now_utc=_utc(now),
    )
    assert result.base_window_start == _utc(expected_start)
    assert result.base_window_end == _utc(expected_end)
    assert (result.base_window_end - result.base_window_start).total_seconds() == hours * 3600


def test_repeated_fall_back_hour_has_single_utc_ownership() -> None:
    from core.datastream_schedule import calculate_schedule_window

    first = calculate_schedule_window(
        _policy(timezone_name="Europe/Paris"),
        now_utc=_utc("2026-10-25T01:05:00Z"),
    )
    second = calculate_schedule_window(
        _policy(timezone_name="Europe/Paris"),
        now_utc=_utc("2026-10-25T02:05:00Z"),
    )
    assert first.base_window_end == second.base_window_start
    assert (
        first.base_window_end - first.base_window_start
        == second.base_window_end - second.base_window_start
    )
    assert first.base_window_start.fold == 0


def test_watermark_delay_and_late_lookback_remain_explicit() -> None:
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(delay_minutes=120, lookback_minutes=180),
        now_utc=_utc("2026-07-19T10:30:00Z"),
    )
    assert result.scheduled_for == _utc("2026-07-19T10:00:00Z")
    assert result.base_window_start == _utc("2026-07-19T07:00:00Z")
    assert result.base_window_end == _utc("2026-07-19T08:00:00Z")
    assert result.window_start == _utc("2026-07-19T04:00:00Z")
    assert result.window_end == result.base_window_end
    assert result.next_run_at == _utc("2026-07-19T11:00:00Z")
    assert result.lookback_overlap_minutes == 180


def test_coalesce_is_one_bounded_half_open_catchup_window() -> None:
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(missed_mode="coalesce", max_catchup_windows=3),
        now_utc=_utc("2026-07-19T10:30:00Z"),
        last_committed_watermark=_utc("2026-07-19T01:00:00Z"),
    )
    assert result.base_window_start == _utc("2026-07-19T07:00:00Z")
    assert result.base_window_end == _utc("2026-07-19T10:00:00Z")
    assert result.coalesced_windows == 3


def test_non_hour_offset_zone_and_naive_now_guard() -> None:
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="daily", interval_minutes=1440, timezone_name="Asia/Kathmandu"),
        now_utc=_utc("2026-07-19T20:00:00Z"),
    )
    assert result.base_window_end == _utc("2026-07-19T18:15:00Z")
    with pytest.raises(ValueError, match="aware UTC"):
        calculate_schedule_window(
            _policy(),
            now_utc=datetime(2026, 7, 19, 12),
        )
