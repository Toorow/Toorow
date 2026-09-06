"""The country split of a day -- story 58.5, extracted whole in lot B2.

WHY IT IS ITS OWN MODULE NOW. `datastream_daily_breakdown_api.py` stood at 1293
lines against a repository that refuses 1000, and lot B2 added to it. This block is
one responsibility from end to end -- the project capability, its states, the
bounded unfolding of a day into countries -- and it moved without a line changing.

WHAT IT KEEPS FROM ITS OLD HOME: every refusal of the mart applies to the split
unchanged, and the split is never safer than the total it decomposes. The route
still owns those reasons, and this module quotes them rather than restating them.
"""

from __future__ import annotations

import logging
from typing import Any

from core import cache_warehouse

logger = logging.getLogger(__name__)

#: THE COUNTRY SPLIT, AND WHO DECIDES IT -- story 58.5.
#:
#: TWO SENTENCES, NEVER ONE (arbitrage 7). The PROJECT decides whether the country
#: capability is on; the DATASTREAM decides whether this flux can carry a country at
#: all. "The capability is off" and "this connector reports no country" send a person
#: to two different places, and a single merged refusal would send half of them to the
#: wrong one. Measured 2026-08-07: 5 connectors of the 39 carry a country at staging,
#: so the second case is the common one, not the edge.
#:
#: AND THE STATE IS READ, NEVER DERIVED. `app.project_capabilities.state` is the
#: switch (migration 131); `read_datastream_capabilities` beside it answers
#: availability/coverage of the last COMPILED proposal, which is a different question
#: and answers `pending` when no Change Set has compiled. Measured on the disposable
#: cluster 2026-08-07: `country` is `disabled` on 1891 projects out of 1891, so the
#: honest render of this block across the whole estate today is NO COLUMN ANYWHERE.
COUNTRY_CAPABILITY_KEY = "country"

#: The states that SHOW the split (arbitrage 5). `reports.py` already gates the
#: geography projection on exactly these two, and a second list would be a second
#: opinion on what "active" means. `degraded` shows the split AND says it is degraded:
#: hiding data already collected is worse than showing it diminished.
COUNTRY_STATE_READY = "ready"
COUNTRY_STATE_DEGRADED = "degraded"
COUNTRY_ACTIVE_STATES: tuple[str, ...] = (COUNTRY_STATE_READY, COUNTRY_STATE_DEGRADED)

#: No row in `app.project_capabilities` at all. Distinct from `disabled`, which is a
#: decision somebody took; this is a project the control plane never wrote.
COUNTRY_STATE_UNSET = "unset"

#: Why there is no split. Each one is a different door.
COUNTRY_CAPABILITY_NOT_ACTIVE = "capability_not_active"
COUNTRY_CONNECTOR_REPORTS_NONE = "connector_reports_no_country"
COUNTRY_NO_VALUE_IN_WINDOW = "no_country_row_in_window"

#: How many country values one day may unfold to, and the payload SAYS it
#: (arbitrage 2). The vocabulary holds 250 codes and a day may legitimately touch
#: most of them; an unfolding that dumped them all would turn one row of the strip
#: into a page. The bound is stated with the count that was measured, so a truncated
#: list can never read as the whole answer -- the rule `parse_window` already holds
#: one call up.
MAX_COUNTRY_VALUES_PER_DAY = 12

#: The switch, read from the control plane and from nowhere else.
_COUNTRY_CAPABILITY_SQL = """
    SELECT state
    FROM app.project_capabilities
    WHERE project_id = %s AND capability_key = %s
"""


def read_country_capability_state(conn, *, project_id: str) -> str:
    """`disabled` / `draft` / `ready` / `degraded` / `blocked`, or `unset`.

    ONE STATEMENT, on the branch every call takes -- the state is what decides
    whether the warehouse is asked at all, so it cannot be read after it.
    """
    with conn.cursor() as cur:
        cur.execute(_COUNTRY_CAPABILITY_SQL, (project_id, COUNTRY_CAPABILITY_KEY))
        row = cur.fetchone()
    if row is None or not row[0]:
        return COUNTRY_STATE_UNSET
    return str(row[0])


def _country_values(counts: dict[str, int]) -> dict[str, Any]:
    """One day's country values, ordered, bounded, and the absence bucket LAST.

    THE ABSENCE IS NOT IN THE RANKING (arbitrage 2). It is not a country, so it does
    not compete with countries for a place in the top of the list and it is never cut
    off by the bound -- a bucket that disappeared at position 13 would read as a bucket
    that was empty. It is appended, always, with its own kind, and the screen draws it
    apart.
    """
    from core.geographic_semantics import (  # noqa: PLC0415
        COUNTRY_ABSENT_BUCKET_KIND,
        COUNTRY_ABSENT_BUCKET_LABEL,
        is_country_absent_bucket,
    )

    countries = sorted(
        ((value, rows) for value, rows in counts.items() if not is_country_absent_bucket(value)),
        # Volume first, then the code, so two equal days order identically.
        key=lambda item: (-item[1], item[0]),
    )
    absent = [(value, rows) for value, rows in counts.items() if is_country_absent_bucket(value)]

    values = [
        {
            "value": value,
            "kind": "country",
            # The LABEL is what a screen renders, on every entry, so the sentinel's
            # raw identity can never reach a person as if it were a place.
            "label": value,
            "rows": rows,
        }
        for value, rows in countries[:MAX_COUNTRY_VALUES_PER_DAY]
    ]
    values.extend(
        {
            "value": value,
            "kind": COUNTRY_ABSENT_BUCKET_KIND,
            "label": COUNTRY_ABSENT_BUCKET_LABEL,
            "rows": rows,
        }
        for value, rows in absent
    )
    return {
        "values": values,
        # THE NUMBER SAYS WHAT IT COUNTS, and only ever that. It counts NAMED
        # COUNTRIES: measured, not the length of the bounded list above, because a
        # truncated list that stated its own length would say the bound was the
        # answer.
        #
        # IT IS ZERO ON A DAY WHOSE ROWS ALL LACK A COUNTRY, and that day is the
        # likely one -- 34 connectors of the 39 report no country at all. Zero named
        # countries is not zero rows: the absence bucket beside it carries them. It
        # was called `value_count` for one afternoon and a screen printed `0` over
        # forty rows with it, which is the exact `0`-for-an-absence this story's
        # `Refuse` line forbids. A caller that has no country to name must say so in
        # words, and this name is what stops it reaching for the number.
        "country_count": len(countries),
    }


def country_split(
    conn,
    *,
    project_id: str,
    connector: str,
    start: str,
    end: str,
    rows: dict[str, Any],
    state: str | None = None,
) -> dict[str, Any]:
    """The country block of the payload -- the state always, the measure only when it exists.

    `state` IS THE SWITCH, HANDED DOWN RATHER THAN RE-READ -- lot B3. The route now
    reads every capability it projects in one statement, and asking this table
    twice per call would let the day grid and the day reading disagree about the
    same switch. Absent, this function reads it itself exactly as it always did, so
    every caller written before that change keeps working.

    WHAT IT REFUSES, AND HOW. When the capability is not active the answer is the
    STATE and nothing else: no `days` key, no empty list, no `null` where a number
    would go. An empty list is a measurement ("we looked and there were none") and
    publishing one for a capability nobody turned on would be a claim about a project
    that never asked the question.

    EVERY MART REFUSAL OF `_mart_rows` APPLIES HERE UNCHANGED. A split cannot be safer
    than the total it decomposes: an unattributable slice, a connector the mart does
    not model and an unreachable warehouse refuse the country block for exactly the
    same reasons and in exactly the same words. The days survive all of them.
    """
    state = state or read_country_capability_state(conn, project_id=project_id)
    active = state in COUNTRY_ACTIVE_STATES
    block: dict[str, Any] = {
        "capability_state": state,
        "active": active,
        "degraded": state == COUNTRY_STATE_DEGRADED,
    }
    if not active:
        block["reason"] = COUNTRY_CAPABILITY_NOT_ACTIVE
        return block

    # The rows half already decided whether the mart can be read for this flux at
    # all. Re-deciding it here would let the two halves disagree about the same
    # warehouse, and a screen would have no way to tell which one to believe.
    if not rows["present"]:
        block["reason"] = rows["reason"]
        block["note"] = rows["note"]
        return block

    try:
        measured = cache_warehouse.read_daily_country_counts(
            project_id=project_id, connector=connector, date_from=start, date_to=end
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "daily_breakdown: country warehouse_unavailable connector=%s: %s",
            connector,
            type(exc).__name__,
        )
        # The ROUTE's word for an unreachable warehouse, quoted rather than
        # restated: a split cannot refuse in different words from the total it
        # decomposes, and a second spelling here is how a screen ends up with two
        # reasons for one outage. Imported late because the route imports this
        # module.
        from core.datastream_daily_breakdown_api import (  # noqa: PLC0415
            ROWS_WAREHOUSE_UNAVAILABLE,
        )

        block["reason"] = ROWS_WAREHOUSE_UNAVAILABLE
        return block

    if not measured["dimension_present"]:
        # ARBITRAGE 7, in its own word. The connector IS in the mart -- `rows` just
        # said so -- and it has never carried a country row for this project. That is
        # a fact about the FLUX, not about the capability, and calling it "off" would
        # send somebody to flip a switch that is already on.
        block["reason"] = COUNTRY_CONNECTOR_REPORTS_NONE
        return block

    days = {day: _country_values(counts) for day, counts in measured["counts"].items()}
    block["bounded_at"] = MAX_COUNTRY_VALUES_PER_DAY
    block["days"] = days
    # The window was read and it carried no country row. That is an EMPTY answer, not
    # a broken one, and it is a different sentence from the two above.
    block["reason"] = None if days else COUNTRY_NO_VALUE_IN_WINDOW
    return block

