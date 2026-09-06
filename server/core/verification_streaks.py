"""toorow -- A verdict, read per Datastream (AI-101).

`app.pull_verifications` has always carried the verdict of every pull, and
fourteen non-test modules read it. Not one of them read it PER DATASTREAM: the
only reaction, `infra_alerts._read_verification_failures`, is a platform-wide
`COUNT(*)` whose signal carries `connector=None`, so a one-off empty pull and a
source dead for a month produce the same line.

That is why the open question of `execution-substrate.md` -- *a Datastream whose
last N pulls all verify as empty, does it stay scheduled?* -- could not be
arbitrated: neither answer is implementable against a fact the substrate cannot
state. This module states it.

WHAT IT IS NOT. It is a MEASUREMENT, not a policy. It defines no N, suspends
nothing, throttles nothing and touches no schedule. It commits the product to
neither row of that table -- which is precisely why it can be built before the
arbitration rather than after it.

THE ONE JOIN. `pull_verifications.pull_id` references `pull_jobs`, which carries
`datastream_id`. The path from a verdict to the stream it judges was always one
join away; here it is travelled.

WHAT IT CANNOT SEE, AND SAYS SO. `pull_jobs.datastream_id` is NULLABLE (added by
migration 023 for backward compatibility), so a verdict on a job that predates
the Datastream model, or on one enqueued without a stream, cannot be attributed
to any stream. Those verdicts are COUNTED and returned alongside, so a reader
never mistakes an incomplete attribution for a quiet platform.

A STREAK IS THE CURRENT ONE. The streak counts only the verdicts more recent
than the last non-`empty` one: a stream that went empty for a week in June and
has verified `ok` since is not in the result at all. `first_empty_at` is
therefore the date the stream went quiet AND HAS STAYED QUIET -- the date row 2
of that table would need, and the date row 1 needs to know whether its alert has
become noise.

Never raises: like every reader in `infra_alerts`, a failure here returns an
empty reading and logs at WARNING. A measurement that aborts its caller would be
a worse defect than the blindness it repairs.

AD-2: source-agnostic -- no module or provider name appears in this file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

logger = logging.getLogger(__name__)

#: The verdict that means "this pull landed no rows" (`app.pull_verifications`
#: CHECK constraint, migration 007: 'ok' | 'partial' | 'empty'). A streak is
#: broken by ANY other verdict -- 'partial' is a stream that is still producing.
EMPTY_VERDICT = "empty"

#: Default cap on the number of streams returned. A reading is meant to be read;
#: an unbounded list in an alert body or a log line is not.
_DEFAULT_LIMIT = 50


@dataclass(frozen=True)
class DatastreamEmptyStreak:
    """One Datastream whose most recent verdicts are all `empty`.

    `empty_streak` is the number of consecutive `empty` verdicts ending at the
    latest one; `first_empty_at` is when that run started -- the date the stream
    went quiet. `last_non_empty_at` is None when the stream has NEVER verified
    anything but empty, which is a different fact from a stream that produced
    and then stopped: the first has arguably never worked, the second broke.
    """

    datastream_id: str
    datastream_name: str
    project_id: str
    module_name: str
    enabled: bool
    schedule_mode: str
    empty_streak: int
    first_empty_at: datetime
    last_empty_at: datetime
    last_non_empty_at: datetime | None

    @property
    def never_produced(self) -> bool:
        """True when no non-empty verdict was ever recorded for this stream."""
        return self.last_non_empty_at is None

    def describe(self) -> str:
        """One ASCII line naming the stream and the date it went quiet (AI-03)."""
        since = self.first_empty_at.date().isoformat()
        tail = "never non-empty" if self.never_produced else "was producing before"
        return (
            f"{self.datastream_name} ({self.datastream_id}): "
            f"{self.empty_streak} empty pull(s) since {since}, {tail}"
        )


@dataclass(frozen=True)
class EmptyStreakReading:
    """What the substrate can state about emptiness, and what it cannot.

    `streams` is the attributed part; `unattributed_empty_pulls` is the number of
    `empty` verdicts whose pull carries no `datastream_id` and which therefore
    belong to no stream in this reading. Both travel together on purpose -- a
    caller that reads only the first would report an empty list as "nothing is
    quiet" when the truth may be "nothing is attributable".
    """

    streams: tuple[DatastreamEmptyStreak, ...] = ()
    unattributed_empty_pulls: int = 0

    def __bool__(self) -> bool:
        return bool(self.streams)

    def describe(self) -> str:
        """The sentence an alert or a log line carries. ASCII only (AI-03)."""
        if not self.streams:
            base = "no Datastream is currently verifying empty"
        else:
            base = "; ".join(stream.describe() for stream in self.streams)
        if self.unattributed_empty_pulls:
            base += (
                f" | {self.unattributed_empty_pulls} empty pull(s) carry no "
                f"datastream_id and are not attributed"
            )
        return base


# ---------------------------------------------------------------------------
# The read
# ---------------------------------------------------------------------------

#: Gaps-and-islands, expressed without a window function: the CURRENT streak is
#: the set of `empty` verdicts more recent than the stream's last non-`empty`
#: one. A stream with no non-empty verdict at all keeps every empty verdict it
#: has, which is why the join is LEFT and the predicate tolerates NULL.
_STREAK_SQL = """
WITH v AS (
    SELECT j.datastream_id  AS datastream_id,
           pv.verdict       AS verdict,
           pv.verified_at   AS verified_at
    FROM app.pull_verifications pv
    JOIN app.pull_jobs j ON j.pull_id = pv.pull_id
    WHERE j.datastream_id IS NOT NULL
),
last_non_empty AS (
    SELECT datastream_id, MAX(verified_at) AS last_non_empty_at
    FROM v
    WHERE verdict <> %(empty)s
    GROUP BY datastream_id
),
streak AS (
    SELECT v.datastream_id     AS datastream_id,
           COUNT(*)            AS empty_streak,
           MIN(v.verified_at)  AS first_empty_at,
           MAX(v.verified_at)  AS last_empty_at
    FROM v
    LEFT JOIN last_non_empty n ON n.datastream_id = v.datastream_id
    WHERE v.verdict = %(empty)s
      AND (n.last_non_empty_at IS NULL OR v.verified_at > n.last_non_empty_at)
    GROUP BY v.datastream_id
)
SELECT s.datastream_id,
       d.name,
       d.project_id,
       d.module_name,
       d.enabled,
       d.schedule_mode,
       s.empty_streak,
       s.first_empty_at,
       s.last_empty_at,
       n.last_non_empty_at
FROM streak s
JOIN app.datastreams d ON d.id = s.datastream_id
LEFT JOIN last_non_empty n ON n.datastream_id = s.datastream_id
WHERE s.empty_streak >= %(min_streak)s
ORDER BY s.first_empty_at ASC, s.datastream_id ASC
LIMIT %(limit)s
"""

#: The blind spot, counted rather than assumed away.
_UNATTRIBUTED_SQL = """
SELECT COUNT(*)
FROM app.pull_verifications pv
JOIN app.pull_jobs j ON j.pull_id = pv.pull_id
WHERE j.datastream_id IS NULL
  AND pv.verdict = %(empty)s
"""


def read_empty_streaks(
    conn,
    *,
    min_streak: int = 1,
    limit: int = _DEFAULT_LIMIT,
) -> EmptyStreakReading:
    """Return, per Datastream, the current run of `empty` verdicts and its start.

    *min_streak* filters the result; it is NOT a policy threshold and nothing in
    this module reacts to it. A caller that wants "streams empty at least three
    times running" asks for it here and decides for itself what that means --
    which is exactly the arbitration this module refuses to pre-empt.

    Never raises: on any error an empty reading is returned and the failure is
    logged at WARNING.
    """
    params = {
        "empty": EMPTY_VERDICT,
        "min_streak": max(1, int(min_streak)),
        "limit": max(1, int(limit)),
    }
    try:
        with conn.cursor() as cur:
            cur.execute(_STREAK_SQL, params)
            rows = cur.fetchall() or []
            cur.execute(_UNATTRIBUTED_SQL, {"empty": EMPTY_VERDICT})
            blind = cur.fetchone()
    except Exception as exc:  # noqa: BLE001
        logger.warning("verification_streaks: read_empty_streaks_error: %s", exc)
        return EmptyStreakReading()

    streams: list[DatastreamEmptyStreak] = []
    for row in rows:
        try:
            streams.append(
                DatastreamEmptyStreak(
                    datastream_id=str(row[0]),
                    datastream_name=str(row[1]),
                    project_id=str(row[2]),
                    module_name=str(row[3]),
                    enabled=bool(row[4]),
                    schedule_mode=str(row[5]),
                    empty_streak=int(row[6]),
                    first_empty_at=row[7],
                    last_empty_at=row[8],
                    last_non_empty_at=row[9],
                )
            )
        except (IndexError, TypeError, ValueError) as exc:
            # One malformed row must not cost the reading its other streams.
            logger.warning("verification_streaks: unreadable_row: %s", exc)

    try:
        unattributed = int(blind[0]) if blind else 0
    except (IndexError, TypeError, ValueError):
        unattributed = 0

    return EmptyStreakReading(
        streams=tuple(streams), unattributed_empty_pulls=unattributed
    )
