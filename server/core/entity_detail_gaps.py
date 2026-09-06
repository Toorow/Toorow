"""Chantier C -- which observed entities have no detail, and which do.

WHAT THIS IS FOR. A Datastream measures 519 videos; three of them have a title.
Nothing in the product could state that sentence, so nothing could act on it: an
inventory of the holes is what makes a context improvable rather than merely
incomplete.

IT STORES NOTHING. The observed side is a bounded `DISTINCT` over the published
relation -- the same relation `query_execution` reads, resolved the same way. The
detailed side is the set of `entity_key` values that have landed on
`app.context_events` (migration 262). Both are read at the moment the question is
asked, because a stored copy is wrong the day a pull lands and the screen would
still be sure of itself.

THE THREE STATES DO NOT COLLAPSE, and this is the whole of the module's honesty:

* a count that came back           -> a number;
* a relation that could not be read -> `unavailable`, never `0 missing`. "We could
  not look" and "nothing is missing" are opposite answers, and the second one
  closes a case that is still open;
* an entity with no event          -> listed, bounded, by key.

AND IT NAMES NO DOMAIN. `entity_kind` travels from the Connector's own vocabulary
and the dimension is the governed concept's name. Nothing here knows what a video
is.

Contract: docs/product-architecture/data.md, "Amendment, chantier C".
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: How many missing keys one answer carries. The COUNT is always exact; the list
#: is a page of it, and the answer says when it was cut rather than looking whole.
MAX_LISTED_MISSING = 100

#: Ceiling on the DISTINCT scan, so an unbounded dimension cannot turn a screen
#: into a warehouse bill. Reached -> the answer says the observed set was capped
#: and no count is claimed.
MAX_OBSERVED = 5000


class EntityGapsUnavailable(RuntimeError):
    """The observed side could not be read. Callers must report `unavailable`."""


def detailed_keys(conn, *, project_id: str, entity_kind: str) -> set[str]:
    """Every entity of this kind for which an event has landed."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT DISTINCT entity_key
            FROM app.context_events
            WHERE project_id = %s AND entity_kind = %s AND entity_key IS NOT NULL
            """,
            (project_id, entity_kind),
        )
        return {str(row[0]) for row in cur.fetchall()}


def observed_keys(plan: dict[str, Any], *, member_id: str) -> list[str]:
    """The DISTINCT values of one governed dimension in the published relation.

    Takes the plan `query_execution.resolve_physical_plan` already composed, so
    the identifier reaching the warehouse is the same allowlisted physical column
    an execution would use -- there is no second path from a request to SQL here.
    """
    from core import query_execution, warehouse  # noqa: PLC0415

    columns = plan.get("columns") or {}
    physical = columns.get(member_id)
    if not physical:
        raise EntityGapsUnavailable("this member has no mapped physical field")
    column = query_execution._safe_identifier(str(physical))

    raw_relation = str(plan["relation"])
    if "." in raw_relation:
        relation = ".".join(
            query_execution._safe_identifier(part) for part in raw_relation.split(".")
        )
    else:
        dataset = str(plan.get("dataset") or "")
        relation = (
            f"{query_execution._safe_identifier(dataset)}."
            f"{query_execution._safe_identifier(raw_relation)}"
            if dataset
            else query_execution._safe_identifier(raw_relation)
        )

    # A breakdown landing holds its dimensions in rows, not columns -- the same
    # shape `build_sql` learned in chantier B. Reusing the reader keeps this
    # module from being the one place that disagrees about what a relation holds.
    present = set(plan.get("present_columns") or [])
    breakdown = query_execution.breakdown_pivot(present)
    conditions: list[str] = []
    params: list[Any] = []
    if breakdown and str(physical) not in present:
        dimension_column, value_column = breakdown
        column = query_execution._safe_identifier(value_column)
        conditions.append(f"{query_execution._safe_identifier(dimension_column)} = ?")
        params.append(str(physical))
    conditions.append(f"{column} IS NOT NULL")

    sql = (  # noqa: S608 - every identifier is allowlisted above
        f"SELECT DISTINCT {column} AS k FROM {relation} "
        f"WHERE {' AND '.join(conditions)} LIMIT {MAX_OBSERVED + 1}"
    )
    try:
        bigquery_mode = warehouse._db_mode() == "bigquery"
        runner = warehouse._query_bigquery if bigquery_mode else warehouse._query_duckdb
        if bigquery_mode:
            sql = query_execution._bind_positional(sql)
        rows = runner(sql, params)
    except Exception as exc:  # noqa: BLE001
        # THE ANSWER IS `unavailable`; THE TRACE NAMES THE CAUSE. A swallowed
        # reason here would make "we could not look" indistinguishable from
        # "nothing is missing", which is the one confusion this module exists
        # to prevent.
        logger.warning(
            "entity_detail_gaps: relation unreadable %s: %s: %s",
            relation, type(exc).__name__, exc, exc_info=True,
        )
        raise EntityGapsUnavailable("the published relation could not be read") from exc
    return [str(row["k"]) for row in rows if row.get("k") is not None]


def inventory(
    conn,
    *,
    project_id: str,
    plan: dict[str, Any],
    member_id: str,
    member_name: str,
    entity_kind: str,
) -> dict[str, Any]:
    """Observed, detailed, missing -- and the bounded list of what is missing."""
    detailed = detailed_keys(conn, project_id=project_id, entity_kind=entity_kind)
    try:
        observed = observed_keys(plan, member_id=member_id)
    except EntityGapsUnavailable as exc:
        return {
            "member_id": member_id,
            "member_name": member_name,
            "entity_kind": entity_kind,
            "state": "unavailable",
            "unavailable_reason": str(exc),
            "observed_count": None,
            "detailed_count": len(detailed),
            "missing_count": None,
            "missing": [],
            "missing_truncated": False,
            "observed_truncated": False,
            "next_gesture": (
                "What this Datastream measures could not be read, so nothing is "
                "claimed about what is missing. Re-run the question once its "
                "Output is readable."
            ),
        }

    observed_truncated = len(observed) > MAX_OBSERVED
    observed = observed[:MAX_OBSERVED]
    missing = sorted(key for key in observed if key not in detailed)
    listed = missing[:MAX_LISTED_MISSING]
    return {
        "member_id": member_id,
        "member_name": member_name,
        "entity_kind": entity_kind,
        "state": "capped" if observed_truncated else "exact",
        "unavailable_reason": None,
        "observed_count": len(observed),
        "detailed_count": len(observed) - len(missing),
        "missing_count": len(missing),
        "missing": listed,
        "missing_truncated": len(missing) > len(listed),
        "observed_truncated": observed_truncated,
        "next_gesture": _next_gesture(
            missing=len(missing),
            observed=len(observed),
            member_name=member_name,
            capped=observed_truncated,
        ),
    }


def _next_gesture(*, missing: int, observed: int, member_name: str, capped: bool) -> str | None:
    """One sentence, or None when nothing is owed.

    It names what would fill the holes -- an event stream that carries the entity
    key -- and never the column it would write to.
    """
    if capped:
        return (
            f"More than {MAX_OBSERVED} distinct `{member_name}` were measured, so "
            "this inventory covers only part of them. Narrow the question to a "
            "window or a segment to get an exact one."
        )
    if observed == 0:
        return (
            f"This Datastream has measured no `{member_name}` yet. Its inventory "
            "fills itself as it collects."
        )
    if missing == 0:
        return None
    return (
        f"{missing} of the {observed} `{member_name}` measured here carry no "
        "detail. Arm an event stream on a Datastream that publishes them, so "
        "each one arrives with its own name."
    )
