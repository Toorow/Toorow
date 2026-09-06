"""Pure half-open Datastream scheduling tests (Story 12.2)."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

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
    arrival_hour_local: int | None = None,
) -> dict:
    return {
        "mode": mode,
        "interval_minutes": interval_minutes,
        "timezone": timezone_name,
        "run_at_hour": arrival_hour_local,
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


# ---------------------------------------------------------------------------
# Story 57.8 -- the hour a daily pull is expected to ARRIVE.
# ---------------------------------------------------------------------------


def test_a_daily_run_without_an_arrival_hour_still_lands_on_local_midnight() -> None:
    """The measured default of an activated row, kept exactly.

    `delay_minutes = 0` and a local-midnight boundary is what activation has
    always written. The arrival hour is an addition, not a replacement: a policy
    that names none must produce the instant it produced before.
    """
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="daily", interval_minutes=1440, timezone_name="Europe/Paris"),
        now_utc=_utc("2026-07-19T12:00:00Z"),
    )
    local = result.next_run_at.astimezone(ZoneInfo("Europe/Paris"))
    assert (local.hour, local.minute) == (0, 0)


def test_the_arrival_hour_decides_the_local_hour_of_the_next_run() -> None:
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(
            mode="daily", interval_minutes=1440,
            timezone_name="Europe/Paris", arrival_hour_local=6,
        ),
        now_utc=_utc("2026-07-19T12:00:00Z"),
    )
    local = result.next_run_at.astimezone(ZoneInfo("Europe/Paris"))
    assert (local.hour, local.minute) == (6, 0)
    assert local.date().isoformat() == "2026-07-20"


@pytest.mark.parametrize(
    ("now", "expected_utc_hour"),
    [
        # Before the spring change Paris is UTC+1, after it UTC+2. The SAME
        # local hour therefore has to land on two DIFFERENT UTC instants -- which
        # is what makes this a measurement rather than a restatement.
        ("2026-03-27T12:00:00Z", 5),
        ("2026-03-30T12:00:00Z", 4),
    ],
)
def test_the_arrival_hour_does_not_drift_across_a_dst_change(
    now: str, expected_utc_hour: int
) -> None:
    """06:00 stays 06:00 the week the clocks move.

    Adding a fixed offset to a UTC instant walks off the chosen hour whenever
    the zone changes offset, and nothing ever corrects it -- the hour a person
    named would silently become another one.
    """
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(
            mode="daily", interval_minutes=1440,
            timezone_name="Europe/Paris", arrival_hour_local=6,
        ),
        now_utc=_utc(now),
    )
    local = result.next_run_at.astimezone(ZoneInfo("Europe/Paris"))
    assert (local.hour, local.minute) == (6, 0)
    assert result.next_run_at.hour == expected_utc_hour


def test_an_hour_outside_the_local_day_is_refused() -> None:
    from core.datastream_schedule import calculate_schedule_window

    for refused in (-1, 24):
        with pytest.raises(ValueError, match="between 0 and 23"):
            calculate_schedule_window(
                _policy(
                    mode="daily", interval_minutes=1440,
                    timezone_name="Europe/Paris", arrival_hour_local=refused,
                ),
                now_utc=_utc("2026-07-19T12:00:00Z"),
            )


def test_an_hourly_cadence_ignores_an_arrival_hour() -> None:
    """A3: an arrival hour answers a question an intraday cadence does not ask.

    The grain is a DATE and an hourly run re-pulls the accumulating day; there
    is no single moment for it to arrive. Honouring the value here would move
    every hourly run onto one hour a day.
    """
    from core.datastream_schedule import calculate_schedule_window

    with_hour = calculate_schedule_window(
        _policy(timezone_name="Europe/Paris", arrival_hour_local=6),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    without_hour = calculate_schedule_window(
        _policy(timezone_name="Europe/Paris"),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    assert with_hour.next_run_at == without_hour.next_run_at


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


# ---------------------------------------------------------------------------
# AI-217 -- `weekly` is a legal cadence and this function raised on it.
# ---------------------------------------------------------------------------


def test_a_weekly_cadence_is_calculated_instead_of_raising() -> None:
    """`raise ValueError("unsupported schedule mode: weekly")` was reachable.

    `build_candidate_review` already accepted `weekly` (interval 10080, arrival
    hour honoured), and every review carrying it died here -- with a bare
    ValueError, not an ActivationValidationError, so the caller could not even
    name the refusal. There is ONE place that decides a schedule window and it
    has to know every cadence the constraint allows.
    """
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="weekly", interval_minutes=10080, timezone_name="Europe/Paris"),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    assert result.next_run_at is not None


def test_a_weekly_window_covers_the_week_it_reports_on() -> None:
    """Seven local days, ending at the last local midnight.

    A window narrower than the gap between two runs drops the days in between,
    and the dispatcher floors it at seven for the same reason
    (`scheduler._dispatch_nightly_datastreams`). Both say seven because it is
    one statement: a run covers the interval it is responsible for.
    """
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="weekly", interval_minutes=10080, timezone_name="Europe/Paris"),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    assert result.base_window_end == _utc("2026-07-18T22:00:00Z")
    assert result.base_window_start == _utc("2026-07-11T22:00:00Z")
    assert result.coalesced_windows == 1


def test_a_weekly_run_lands_on_the_arrival_hour_of_the_local_day() -> None:
    """The hour is a VALUE for weekly too (story 57.8, A3).

    `nightly` and `weekly` are the cadences that run once a period, and the
    Workbench, the MCP tool and the advance all already say so. A weekly plan
    whose next run ignored the chosen hour would contradict three doors.
    """
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(
            mode="weekly", interval_minutes=10080,
            timezone_name="Europe/Paris", arrival_hour_local=6,
        ),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    # 06:00 Paris on the next local day, expressed in UTC.
    assert result.next_run_at == _utc("2026-07-20T04:00:00Z")


def test_an_unset_arrival_hour_places_a_weekly_run_at_local_midnight() -> None:
    """Exactly what a daily row without an hour does -- absence is not 0."""
    from core.datastream_schedule import calculate_schedule_window

    result = calculate_schedule_window(
        _policy(mode="weekly", interval_minutes=10080, timezone_name="Europe/Paris"),
        now_utc=_utc("2026-07-19T12:30:00Z"),
    )
    assert result.next_run_at == _utc("2026-07-19T22:00:00Z")
