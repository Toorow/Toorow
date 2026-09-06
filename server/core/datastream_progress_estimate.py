"""How much longer this collection has -- story 63.4, epic 63.

WHY THIS DOES NOT ESTIMATE ON THE RUN IN FLIGHT. The epic asked for "the days
already done of THIS run, never a historical average". Measured on 2026-08-06,
that rule can almost never speak: a run is cut into windows, `days_done` moves
only at a window BOUNDARY, and the largest window count any path in this
repository produces is THREE (`refetch.py:57,196-204`, `monthly_days: 90` ->
31/31/28, and only on the 1st of the month). So the maximum number of finished
windows a run can offer BEFORE it ends is two -- and on the nightly cadence of 32
of the 39 modules it is ZERO: the first window boundary is also the last, and
`close_collection_run_if_complete` makes the run terminal on the same call. An
estimate divided by "the days already done of this run" would therefore be a
permanent silence on the dominant traffic, which is the one case an operator
watches.

WHAT IT ESTIMATES ON INSTEAD, and it is an explicit amendment to the plan: what
THIS Datastream has already cost. `app.pull_jobs.started_at` / `completed_at`
exist since migration 006 and are never purged in production, and migration 218
bound each window to its run. The last runs of this Datastream are therefore a
real, measured sample -- and the only one that carries enough observations.

THE UNIT IS THE DAY, NEVER THE WINDOW. A 31-day window is one provider call that
pages internally over its own rows, so it does not cost what a one-day window
costs. Dividing an elapsed time by a number of WINDOWS is the arithmetic this
module refuses; every rate below is seconds per day of window.

AND THE RATE IS A MEDIAN, NOT A MEAN. One night where a provider throttled must
not move the estimate of the next ten.

THE RIGHT TO SAY NOTHING IS PART OF THE ANSWER. `anomaly_alerts` set the
precedent: below its structural bound the detector says nothing AND DISCLOSES THE
BOUND (`observations`, `minimum_observations`, `armed`). The same shape is used
here -- a silence with its count and its minimum is a measurement; a missing line
is not. A median over one observation is an invented number.

A RUN THAT HAS OUTRUN THE SAMPLE IS THE FOURTH SILENCE, and it is the one that
matters most. The first version of this module clamped the countdown with
`max(0, expected - elapsed)`, so a window stalled for 48 hours answered
`armed: True, seconds_remaining: 0` -- a `0` standing in for an absence, on the
exact screen an operator opens when something is wrong, and pinned by a test that
made it read as ratified. Once the window in flight has been running longer than
ANYTHING measured for this Datastream, the sample no longer describes this run:
the estimate stops estimating and says by how much it has been beaten.

AND THE SPREAD OF THE SAMPLE TRAVELS WITH THE NUMBER. A median over rates of 10,
120 and 1200 seconds per day is a factor of 120 between the fastest and slowest
run ever measured; publishing "about 20 minutes" off it, in the same words as a
tight sample, is a confidence the measurement does not have. So `spread_ratio` is
published beside every answer, and beyond `MAX_SPREAD_RATIO` the sentence DEGRADES
to a range -- `precision` says which of the two it is, and `seconds_remaining` is
`null` rather than a point nobody should read as one. The point value is also
withheld the moment it would collapse to zero: a countdown may not reach 0 while
the run is still moving.

TWO SERVER INSTANTS, NEVER A BROWSER CLOCK. Both ends of every subtraction below
come from the database: `measured_at` is `now()` taken by the same statement that
read the run. Comparing a browser epoch with a server timestamp manufactures time
the moment one clock runs ahead, and nobody would ever see it happen.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any

#: How many finished runs of this Datastream the median is taken over. Ten
#: nights for a nightly cadence: long enough that one throttled night cannot
#: move the number, short enough that a change of volume shows within two weeks.
HISTORY_RUNS = 10

#: The largest number of windows any dispatch path in this repository cuts a run
#: into -- measured 2026-08-06 (`refetch.py:57,196-204`, `monthly_days: 90` ->
#: 31/31/28). It is here because it is what bounds the ROW read of the rate
#: query before its aggregate; see migration 219.
MAX_WINDOWS_PER_RUN = 3

#: So the rate read never touches more than this many pull jobs, whatever the
#: age of the Datastream. Derived, never chosen.
HISTORY_WINDOWS = HISTORY_RUNS * MAX_WINDOWS_PER_RUN

#: Below this many measured runs the estimate does not speak. Three is the
#: smallest sample a median means anything on -- with two, the "median" is the
#: mean of the only two numbers there are, and with one it is that one number
#: presented as a rate.
MINIMUM_FINISHED_RUNS = 3

#: Beyond this factor between the fastest and the slowest run ever measured, a
#: single number is a confidence the sample does not support: the answer becomes
#: a RANGE. Three is the point at which the two ends of the band stop rounding
#: to the same sentence -- "about 20 minutes" and "about an hour" are different
#: decisions for a person waiting on a collection.
MAX_SPREAD_RATIO = 3.0

#: The four silences, each a sentence of its own. One silence for all four would
#: be the defect `idle` already refuses one layer above.
ESTIMATE_RUN_NOT_MEASURED_YET = "run_not_measured_yet"
ESTIMATE_NOT_ENOUGH_HISTORY = "not_enough_history"
ESTIMATE_INCONSISTENT_RATE = "inconsistent_rate"
ESTIMATE_LONGER_THAN_MEASURED = "running_longer_than_measured"

ESTIMATE_REASONS = (
    ESTIMATE_RUN_NOT_MEASURED_YET,
    ESTIMATE_NOT_ENOUGH_HISTORY,
    ESTIMATE_INCONSISTENT_RATE,
    ESTIMATE_LONGER_THAN_MEASURED,
)

#: How much the payload is claiming: one number, or a band. Never a bare value
#: whose precision the reader has to guess.
PRECISION_POINT = "point"
PRECISION_RANGE = "range"

_MINUTE = 60
_HOUR = 3600
_DAY = 86400


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def format_duration_english(seconds: float | int | None) -> str:
    """A duration a person reads, in English, at the precision it deserves.

    The console has no duration formatter of its own
    (`grep -rn "formatDuration|Intl.RelativeTimeFormat" ui/admin/src` -> nothing),
    and this one is deliberately NOT written there: the MCP reads the same
    payload as the screen, and two formatters are two answers to "how long is
    left" for the same run.

    Precision falls as the number grows, because that is what the measurement is
    worth: minutes below an hour, hours and minutes below a day, days and hours
    above. A seconds-exact countdown off a median of ten runs would be a claim
    the sample cannot support.
    """
    if seconds is None or not math.isfinite(float(seconds)) or seconds < 0:
        return "an unknown time"
    total = float(seconds)
    if total < _MINUTE:
        return "less than a minute"
    if total < _HOUR:
        return _plural(max(1, round(total / _MINUTE)), "minute")
    if total < _DAY:
        hours = int(total // _HOUR)
        minutes = round((total - hours * _HOUR) / _MINUTE)
        if minutes == 60:
            hours, minutes = hours + 1, 0
        if hours >= 24:
            return _plural(1, "day")
        return _plural(hours, "hour") if minutes == 0 else (
            f"{_plural(hours, 'hour')} {_plural(minutes, 'minute')}"
        )
    days = int(total // _DAY)
    hours = round((total - days * _DAY) / _HOUR)
    if hours == 24:
        days, hours = days + 1, 0
    return _plural(days, "day") if hours == 0 else (
        f"{_plural(days, 'day')} {_plural(hours, 'hour')}"
    )


def median(values: list[float]) -> float | None:
    """The middle of the sample, or None when there is no sample.

    A mean would let one throttled night decide what the next ten look like;
    that is exactly the shape a provider incident has.
    """
    ordered = sorted(values)
    size = len(ordered)
    if size == 0:
        return None
    middle = size // 2
    if size % 2 == 1:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def read_samples(samples: Any) -> tuple[int, list[float]]:
    """Return (runs measured, the rates among them that ARE rates).

    The two numbers are kept apart on purpose, because they are two different
    silences. A run whose rate is non-positive or non-finite still HAPPENED -- it
    is a clock that went backwards, or a window that completed in the instant it
    started -- so it counts as an observation and is refused as a divisor. Folding
    the two together would report "not enough history" for a Datastream that has
    run twenty times, which is a wrong sentence rather than a cautious one.
    """
    if not samples:
        return 0, []
    observations = 0
    usable: list[float] = []
    for raw in samples:
        if raw is None:
            continue
        observations += 1
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0:
            usable.append(value)
    return observations, usable


def spread_ratio(usable: list[float]) -> float | None:
    """Slowest measured rate over fastest -- the dispersion, published, always.

    `fetch_detector_readiness` discloses `observations` / `minimum_observations`
    beside its verdict rather than only when it declines. Same rule: a reader has
    to be able to tell a median over three runs that agreed from a median over
    three runs that disagreed by a factor of 120, and no payload of derived
    numbers in this repository did that before.
    """
    if not usable:
        return None
    low, high = min(usable), max(usable)
    if low <= 0:
        return None
    return high / low


def _silent(
    reason: str,
    sentence: str,
    observations: int,
    measured_at: Any,
    *,
    usable: list[float] | None = None,
    behind_by: float | None = None,
) -> dict[str, Any]:
    """A silence, with everything that was measured about it.

    `seconds_remaining` is `null` on every one of them -- never a `0`, which
    would be a countdown that reached its end while the run is still moving.
    """
    ratio = spread_ratio(usable or [])
    return {
        "armed": False,
        "observations": observations,
        "minimum_observations": MINIMUM_FINISHED_RUNS,
        "precision": None,
        "seconds_remaining": None,
        "seconds_remaining_low": None,
        "seconds_remaining_high": None,
        "spread_ratio": None if ratio is None else round(ratio, 2),
        "behind_by_seconds": None if behind_by is None else int(round(behind_by)),
        "measured_at": measured_at,
        "reason": reason,
        "sentence": sentence,
    }


def _elapsed_seconds(start: Any, measured_at: Any) -> float | None:
    """Seconds between two SERVER instants, or None when either is missing."""
    if not isinstance(start, datetime) or not isinstance(measured_at, datetime):
        return None
    if (start.tzinfo is None) != (measured_at.tzinfo is None):
        # One naive and one aware: subtracting them raises, and guessing which
        # zone the naive one meant is how a screen ends up an hour out.
        return None
    return (measured_at - start).total_seconds()


def estimate_time_left(
    *,
    days_done: Any,
    days_total: Any,
    window_days: Any,
    window_started_at: Any,
    samples: Any,
    measured_at: Any,
) -> dict[str, Any]:
    """How much longer this run has, or why that cannot be said.

    ``samples`` is one seconds-per-day rate per finished run of this Datastream,
    newest first, as `PROGRESS_SQL` aggregates them -- no extra statement and no
    extra round trip.

    THE ARITHMETIC, in full. `remaining_at(r)` is what is left if this run turns
    out to cost `r` seconds per day:

        days_remaining  = days_total - days_done          days the run still owes
        elapsed_window  = measured_at - window.started_at what the window in
                                                          flight has cost so far
        remaining_at(r) = max(0, r * window_days - elapsed_window)
                          + r * (days_remaining - window_days)

    applied at the SLOWEST, the MEDIAN and the FASTEST rate ever measured for this
    Datastream, which gives a band and a point inside it.

    The window in flight is subtracted rather than counted whole because it is
    already running: without it the estimate cannot see a run DECELERATING, which
    is the one moment an operator needs it.

    AND THE POINT IS WITHHELD RATHER THAN CLAMPED. `max(0, ...)` inside
    `remaining_at` is what a *band edge* does when that edge has been passed; it
    is NOT allowed to become the answer. If the window has outrun even the
    slowest run ever measured, nothing in the sample describes what is happening
    and the estimate says so (`running_longer_than_measured`, with the overrun).
    If the median has been beaten but the slow end has not, the answer degrades
    to that band. A countdown never reaches zero while the run is still moving.
    """
    observations, usable = read_samples(samples)

    # 1. The run has not said what it is going to do. Nothing to multiply.
    if days_total is None or days_done is None:
        return _silent(
            ESTIMATE_RUN_NOT_MEASURED_YET,
            "This run has not said how much it will collect yet — no estimate.",
            observations,
            measured_at,
            usable=usable,
        )

    # 2. Not enough finished runs of this Datastream. The count AND the minimum
    #    travel with the silence, the way `fetch_detector_readiness` discloses
    #    `observations` / `minimum_observations`.
    if observations < MINIMUM_FINISHED_RUNS:
        return _silent(
            ESTIMATE_NOT_ENOUGH_HISTORY,
            "Not enough finished runs of this Datastream to estimate — "
            f"{observations} of {MINIMUM_FINISHED_RUNS} measured.",
            observations,
            measured_at,
            usable=usable,
        )

    rate = median(usable) if len(usable) >= MINIMUM_FINISHED_RUNS else None

    # 3. There are enough runs, and not enough of them describe a rate.
    if rate is None or not math.isfinite(rate) or rate <= 0:
        return _silent(
            ESTIMATE_INCONSISTENT_RATE,
            "The finished runs of this Datastream do not give a usable rate — "
            "no estimate.",
            observations,
            measured_at,
            usable=usable,
        )

    rate_low, rate_high = min(usable), max(usable)
    ratio = spread_ratio(usable) or 1.0

    days_remaining = max(0, int(days_total) - int(days_done))
    in_flight_days = 0
    if window_days is not None:
        in_flight_days = max(0, min(int(window_days), days_remaining))

    elapsed_window = _elapsed_seconds(window_started_at, measured_at)
    if elapsed_window is None or elapsed_window < 0:
        elapsed_window = 0.0

    def remaining_at(per_day: float) -> float:
        return (
            max(0.0, per_day * in_flight_days - elapsed_window)
            + per_day * (days_remaining - in_flight_days)
        )

    # 4. The window in flight has been running longer than the SLOWEST run ever
    #    measured for this Datastream would have taken over its days. The sample
    #    no longer describes this run, so it may not be multiplied by it -- and a
    #    stalled window is exactly when a `0` would be read as "nearly done".
    overrun = elapsed_window - rate_high * in_flight_days
    if in_flight_days > 0 and overrun > 0:
        return _silent(
            ESTIMATE_LONGER_THAN_MEASURED,
            f"This window has been running {format_duration_english(overrun)} "
            "longer than anything measured for this Datastream — no estimate.",
            observations,
            measured_at,
            usable=usable,
            behind_by=overrun,
        )

    low, high, point = remaining_at(rate_low), remaining_at(rate_high), remaining_at(rate)
    behind_by = max(0.0, elapsed_window - rate * in_flight_days)

    # A single number is only honest when the sample agrees with itself AND the
    # median still has time on it. Otherwise: the band, said as a band.
    if ratio > MAX_SPREAD_RATIO or point <= 0:
        return {
            "armed": True,
            "observations": observations,
            "minimum_observations": MINIMUM_FINISHED_RUNS,
            "precision": PRECISION_RANGE,
            "seconds_remaining": None,
            "seconds_remaining_low": int(round(low)),
            "seconds_remaining_high": int(round(high)),
            "spread_ratio": round(ratio, 2),
            "behind_by_seconds": int(round(behind_by)),
            "measured_at": measured_at,
            "reason": None,
            "sentence": (
                f"Between {format_duration_english(low)} and "
                f"{format_duration_english(high)} left."
            ),
        }

    return {
        "armed": True,
        "observations": observations,
        "minimum_observations": MINIMUM_FINISHED_RUNS,
        "precision": PRECISION_POINT,
        "seconds_remaining": int(round(point)),
        "seconds_remaining_low": int(round(low)),
        "seconds_remaining_high": int(round(high)),
        "spread_ratio": round(ratio, 2),
        "behind_by_seconds": int(round(behind_by)),
        "measured_at": measured_at,
        "reason": None,
        "sentence": f"About {format_duration_english(point)} left.",
    }
