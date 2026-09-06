"""The ONE resolution of the window a Datastream pull covers.

WHY THIS MODULE EXISTS. The rule was written once, in
`scheduler._dispatch_nightly_datastreams`, and then re-typed: the hourly
dispatcher carries a second copy, `datastream_first_candidate` a third with its
own ceiling. Three copies of one arbitration (AI-46) is three chances for a
first candidate to cover a window that the nightly run after it does not, with
nothing on any screen able to say why. The arbitration itself is NOT re-decided
here -- it is moved, comment included, from the dispatcher that owned it.

THE PRECEDENCE (AI-46), highest to lowest:

    1. `date_window_days`   -- the per-stream retrieval window. The surface
                               setting (`docs/product-architecture/
                               datastream-workbench-and-wizard.md`, "Retrieval
                               window"). Schema: NOT NULL DEFAULT 30.
    2. `refetch_days`       -- the LEGACY ALIAS of the same setting, not a
                               second one. Read only when `date_window_days` is
                               absent or 0, which the schema prevents for real
                               rows (NOT NULL DEFAULT 3) and only a test mock or
                               a row written before migration 023 can produce.
    3. `DEFENSIVE_WINDOW_DAYS` -- 3. Defensive only, for the mocks above. It is
                               NOT the product's default: the product's default
                               is the column default, 30, and it arrives through
                               rung 1 for every row Postgres ever wrote.

THE FORMULA. `window_offset_days` says how many days back from the last complete
day the window ENDS (documented default 1 = yesterday; 3 for a source with a
two-to-three day lag). The window is inclusive and spans exactly `days`:

    end   = end_reference - (offset_days - 1)
    start = end - (days - 1)

`end_reference` is the caller's last COMPLETE day -- the project's yesterday, in
the project's own timezone (AI-117), never the deployment's. It is a parameter
and not a `date.today()` call here because that resolution needs a database and
this module must stay pure: every invariant below is offline-testable.

THE CADENCE FLOOR (AI-217). A run never fetches less than the interval it
covers: three days fetched once a week asks for Friday to Sunday and never asks
for Monday to Thursday again, and nothing reports the gap because each run
succeeds. The floor WIDENS and never narrows -- a person who set thirty days
keeps thirty.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Mapping, NamedTuple

__all__ = [
    "DEFAULT_OFFSET_DAYS",
    "DEFENSIVE_WINDOW_DAYS",
    "PullWindow",
    "WindowLength",
    "resolve_window",
    "resolve_window_days",
]

#: Rung 3 of the precedence above. Reachable only by a row that declares neither
#: column, which the schema forbids -- see the module docstring.
DEFENSIVE_WINDOW_DAYS = 3

#: `window_offset_days` when the row does not declare one. Ratified default: the
#: window ends at J-1, the last complete day.
DEFAULT_OFFSET_DAYS = 1

#: Cadence -> the minimum number of days one run must cover (AI-217).
CADENCE_FLOOR_DAYS = {"weekly": 7}


class WindowLength(NamedTuple):
    """How many days one run covers, and WHICH declaration answered."""

    days: int
    #: `date_window_days` | `refetch_days` | `defensive_default`
    source: str
    #: The cadence whose floor widened the window, or None.
    widened_for: str | None


class PullWindow(NamedTuple):
    """An inclusive ISO window, and everything that produced it."""

    date_from: str
    date_to: str
    length: WindowLength
    offset_days: int

    def as_interval(self) -> dict[str, str]:
        """The vocabulary the activation drivers read.

        `connector_pull_candidate` accepts `from`/`date_from` and
        `to_exclusive`/`date_to`; the worker's `_driver_interval` normalises to
        `date_from`/`date_to`. One spelling leaves this module, so a window
        resolved here and a window pinned by a caller are indistinguishable
        downstream.
        """
        return {"date_from": self.date_from, "date_to": self.date_to}


def _positive_int(value: Any) -> int | None:
    """`value` as a day count, or None when it does not name one.

    A string, a Decimal from psycopg, None and 0 all mean "this rung does not
    answer" -- 0 deliberately included: a zero-day window is not a window, and
    reading it as one would ask a provider for nothing and call it a success.
    """
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None
    return days if days >= 1 else None


def resolve_window_days(row: Mapping[str, Any], *, cadence: str | None = None) -> WindowLength:
    """The number of days ONE run of this Datastream fetches.

    *row* is any mapping carrying `date_window_days` and `refetch_days` -- a
    dispatcher's SELECT row, an activation context, a test double.
    """
    days = _positive_int(row.get("date_window_days"))
    source = "date_window_days"
    if days is None:
        days = _positive_int(row.get("refetch_days"))
        source = "refetch_days"
    if days is None:
        days = DEFENSIVE_WINDOW_DAYS
        source = "defensive_default"

    floor = CADENCE_FLOOR_DAYS.get(str(cadence or row.get("schedule_mode") or "").strip())
    if floor is not None and days < floor:
        return WindowLength(floor, source, str(cadence or row.get("schedule_mode")))
    return WindowLength(days, source, None)


def resolve_window(
    row: Mapping[str, Any],
    *,
    end_reference: date,
    cadence: str | None = None,
) -> PullWindow:
    """The inclusive window one run of this Datastream covers.

    *end_reference* is the last COMPLETE day in the project's timezone. The
    caller resolves it (`scheduler.project_yesterday(scheduler.project_timezone(
    conn, project_id))`) because doing so needs a database and this stays pure.
    """
    length = resolve_window_days(row, cadence=cadence)
    offset_days = _positive_int(row.get("window_offset_days")) or DEFAULT_OFFSET_DAYS
    end_date = end_reference - timedelta(days=offset_days - 1)
    start_date = end_date - timedelta(days=length.days - 1)
    return PullWindow(
        date_from=start_date.isoformat(),
        date_to=end_date.isoformat(),
        length=length,
        offset_days=offset_days,
    )
