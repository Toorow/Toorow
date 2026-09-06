"""toorow -- the REST surface of `core.unresolved_values`.

Target: ``docs/product-architecture/unresolved-values.md``, screens S1 and S2.

WHY THIS MODULE EXISTS AT ALL. `core/unresolved_values.py` was 386 measured lines
imported by nobody outside its own test, which is the defect
`governance_surface_api.py:397-400` names in its own words: *a command whose only
caller is a test is the defect this repository keeps paying for*. The reading was
built, proved offline, and no address served it. This module is that address.

ONE READING, TWO PROJECTIONS -- and it is an acceptance criterion, not a
preference. `unresolved-values.md:482` refuses a product where "the same count is
answered differently by the `Map` tab and the `Value Tables` lens". So both routes
below call the SAME `read_datastream_unresolved`, the project-wide one being a
loop over the per-Datastream one; there is no second implementation to disagree
with the first.

NO NEW NAVIGATION NODE (`unresolved-values.md:367-374`). This is not a project
capability: no toggle, no tab, no lens, nothing added to `page-structure.md`. S1 is
a panel inside the Workbench `Map` tab that already exists and S2 is the same panel
inside the `value-tables` lens that already exists.

WHAT IS SWEPT, AND WHY IT IS THE GRAIN. "One pass, per Datastream and per dimension
its active mapping version declares" (`unresolved-values.md:84`), and S1 sits below
the field table because "a column that is not mapped to a dimension has no values
to resolve" (:394). So a column is swept when the mapping declares it in the
`grain` AND binds it to a canonical target. The grain is already the product's
answer to "what identifies a row" -- `dq_null_rate` reads it, the daily-breakdown
header reads it, and item 11 of the completeness pass asks the append key to be
read from it rather than declared twice. A field with no canonical target is
skipped rather than listed: its gesture is one panel above (bind the column), not
a value pair.

THREE STATES TRAVEL UNCHANGED. `read_unresolved_set` answers `measured`,
`unavailable` or `not_listable` and this module never folds them: a screen that had
to infer "did the read happen" from "is the list empty" is exactly what
`value_mapping_api._list_tables` puts `impact_state: "known"` in its envelope to
prevent. An unreadable window prints `unknown`, never `0`.

WHAT IS NOT BUILT HERE, AND IS NAMED RATHER THAN SILENTLY MISSING. S3 (the repair
drawer with its match modes, its rule reach and its impact) and the import half of
S4 are not delivered. The panel therefore carries a DISABLED `Map to...` with the
sentence naming where a pair is written today, and never an enabled control that
opens nothing. The export half of S4 IS delivered, in its two-column form, through
`/extract` below -- `unresolved-values.md:218` states that the two-column shape is
the special case where the destination is a value mapping table, so the route says
which destination it is filling instead of asking.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any, Callable, Mapping

from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

# Pure, no connection, no cycle -- the envelope names the modes so the drawer
# cannot invent a fifth one or spell one of these four differently.
from core.unresolved_rule_reach import MATCH_MODE_LABELS, MATCH_MODES

logger = logging.getLogger(__name__)

#: The envelope contract both screens read, named and versioned the way
#: `datastream_daily_breakdown.v1` is.
UNRESOLVED_VALUES_SCHEMA = "unresolved_values.v1"

_PROJECT_BASE = "/api/projects/{project_id}/unresolved-values"
_STREAM_BASE = (
    "/api/projects/{project_id}/datastreams/{datastream_id}/unresolved-values"
)

#: THE WINDOW, AND IT IS AN OPEN QUESTION THIS MODULE ANSWERS RATHER THAN HIDES.
#: `unresolved-values.md:577-580` leaves it undecided: the geography monitor reads a
#: project-wide 7-day window because it spans Datastreams with different offsets,
#: while a per-Datastream reading could use that Datastream's own last fetchable
#: day, "and the two answers give different counts". Seven days is taken here for
#: the reason the two surfaces must agree: S2 spans Datastreams, so a per-stream
#: window would make the Workbench and the lens answer two different numbers for
#: one Project, which is the criterion at :482. The window travels on every
#: payload, so the number a person reads always says what it was measured over.
WINDOW_DAYS = 7

#: How many dimensions one Datastream is swept for. Each one is a warehouse
#: statement, and `unresolved-values.md:512-520` asks the sweep to be bounded with
#: what was dropped STATED -- "a silent truncation reads as 'we looked at
#: everything'".
MAX_DIMENSIONS = 12

#: How many Datastreams the project-wide reading opens. Measured on the estate:
#: 842 live Datastreams, so an unbounded S2 would issue thousands of warehouse
#: statements on one page load. The bound is stated on the payload with the true
#: total beside it.
MAX_STREAMS = 8

#: Never swept: they are the axes of the reading, not values of it.
_NEVER_SWEPT = frozenset({"date", "project_id", "pull_id"})

#: `country>device` is ONE partition whose values are `FR>mobile`. Sweeping it
#: beside its components reports the same unmapped country twice under two
#: reasons (`unresolved-values.md:531-534`), so composites are refused.
_COMPOSITE = ">"


# ---------------------------------------------------------------------------
# The sentences. Written here, on the server, because the screen must hold none
# of its own -- one wording whichever door it comes through, the rule
# `stage_relation_resolver` and `collected_mapped_reader` already follow.
# ---------------------------------------------------------------------------

#: `unresolved-values.md:416`, verbatim. It is the ONE sentence that must never be
#: replaced by a zero.
WAREHOUSE_UNAVAILABLE_MESSAGE = (
    "The warehouse could not be read, so unresolved values are unknown for this "
    "window. This is not a count of zero."
)

#: `unresolved-values.md:413-414`, verbatim, with the two numbers it states.
def empty_message(dimensions: int, days: int = WINDOW_DAYS) -> str:
    """"Every value of the 3 mapped dimensions resolves over the last 7 days."

    It says WHAT WAS MEASURED, not that nothing exists -- which is the whole
    difference between an empty list and a silent one.
    """
    noun = "dimension" if dimensions == 1 else "dimensions"
    return (
        f"Every value of the {dimensions} mapped {noun} resolves over the last "
        f"{days} days."
    )


#: `unresolved-values.md:417-418`: "no dimension mapped yet: the sentence names the
#: gesture, which is one panel above, on the same screen".
NOT_APPLICABLE_MESSAGE = (
    "No column of this Datastream is both part of the declared grain and bound to "
    "a canonical field, so there is no dimension whose values could be resolved. "
    "Bind a grain column to a canonical field in the bindings panel above."
)

#: The ranking. `unresolved-values.md:402` asks each group for the share of the
#: window's rows AND of the ranking metric. The rows are measured -- they are what
#: `read_distinct_values` counts. The metric is not: nothing joins a metric to a
#: raw dimension value on this path, and printing a share nobody measured is the
#: fabricated green this repository forbids. So it is stated as absent, once.
RANKING_METRIC_UNMEASURED = (
    "Values are ranked by the rows that carry them. The share of a ranking metric "
    "is not shown: no metric is joined to a raw dimension value on this reading, "
    "and a share nobody measured is not a number."
)

#: Feature 3 of the target -- "a proposal per value, with its confidence and the
#: word that earned it". `dimension_conformance.suggest_mappings` exists and is
#: scored, but it needs a CANDIDATE VOCABULARY to score against, and outside the
#: geography axis nothing supplies one: a video id has no candidate list to be
#: `exact`, `normalized` or `similar` against. So the column is empty and says
#: why, rather than carrying a confidence nobody computed.
PROPOSALS_ABSENT = (
    "No proposal is offered on these rows. Scoring one needs a list of candidate "
    "canonical values to compare against, and no vocabulary is declared for this "
    "dimension yet. Nothing is ever auto-confirmed either way."
)

#: S4 HAS TWO DESTINATIONS AND ONLY ONE OF THEM IS SERVED, exactly as the export
#: half already is. The VALUE MAPPING TABLE is built (2026-08-22, story 67.20):
#: `import_pairs` IS an append by key -- the key is `source_value`, the unique
#: index of migration 235:128 is the contract, and `already_present` is what it
#: answers rather than overwriting. The MAPPING FILE BEHIND A TEMPLATE is not:
#: that one needs the stable-key contract of `unresolved-values.md:132-146`, which
#: no code in this repository writes, and a dialog that wrote without saying
#: whether it appends or replaces is refused at :481.
IMPORT_TEMPLATE_DESTINATION_ABSENT = (
    "Filling the mapping file behind a Template is not built here. That "
    "destination has to declare a stable key before anything may append to it, "
    "and none is declared. The value mapping table above takes the file."
)

#: What the write mode IS, stated on the dialog rather than assumed (S4, :447).
IMPORT_WRITE_MODE = (
    "This appends by source value. A value the table already carries is reported "
    "as already there and is never overwritten -- nothing you mapped by hand is "
    "replaced."
)

#: The drawer's own note, now that it exists: what it still cannot do. Kept as a
#: named absence rather than deleted, because the proposal column is the half
#: that is genuinely missing and a person looking for it must find a sentence.
#:
#: AMENDED 2026-08-30 to name the gap it was silent about. `unresolved-values.md`
#: refuses a screen that "says a value was repaired while the reading that showed
#: it still renders the old one -- the repair must land in a store the reading
#: consults, OR the surface must say which readings it does not yet reach". This
#: note is the second half of that sentence, and until 2026-08-30 it did not say
#: it: the drawer writes `the drawer's entry store` and the reading resolves
#: `app.dimension_value_mappings`, two stores no module joins
#: (`grep -c the entry-store literal in core/dimension_conformance.py` -> 0).
REPAIR_DRAWER_PARTIAL = (
    "The drawer writes pairs into a value mapping table. It does not propose a "
    "canonical value for you -- see the note on proposals -- and a rule is "
    "unfolded into one pair per value rather than stored as a pattern, so a "
    "value that arrives later is not matched by it retroactively. And the list "
    "above does not read the value tables: it resolves the confirmed mappings, "
    "so a pair written here may leave the value listed. What was written is "
    "checked against the list itself after every write, and the answer is "
    "stated rather than assumed."
)

#: WHAT A WRITE MAY CLAIM, AND WHAT IT MAY NOT -- measured 2026-08-30.
#:
#: Both surfaces used to print "It applies at the next read of this window" the
#: moment the write returned. Nothing measured that. The pairs land in
#: `the drawer's entry store` (`value_mapping_tables.import_pairs`) and the
#: unresolved reading resolves `app.dimension_value_mappings` through
#: `dimension_conformance.conform_value`; no module carries a row from one to the
#: other, which is the whole of **Open question 4** of the ratified page. So the
#: screens now take THE SAME READING again after writing and print what it
#: answered. Three answers, three sentences, and none of them is a promise.
#:
#: These are the words, and they live here rather than in the components for the
#: same reason every other sentence of this surface does: the drawer and the
#: import dialog would otherwise be two places free to describe one measured fact
#: differently.
REPAIR_WRITE_NOT_SEEN = (
    "The list that showed the gap was read again just now, and it still carries "
    "these values. A pair goes into the value table; this reading resolves the "
    "confirmed mappings instead, and nothing in the product carries a pair from "
    "one into the other. Which of the two a reading should resolve has not been "
    "decided, so nothing here can promise the next read will differ."
)

REPAIR_WRITE_CLEARED = (
    "The list that showed the gap was read again just now, and it no longer "
    "carries these values."
)

REPAIR_WRITE_UNKNOWN = (
    "The list that showed the gap could not be read again just now, so whether "
    "it still carries these values is unknown. This is not a repair that landed."
)

#: The reasons that mean THE READ DID NOT HAPPEN, as opposed to the ones that mean
#: the address was never declared. Only the first family gets the mandated
#: warehouse sentence; the second keeps the resolver's own words, because "the
#: warehouse could not be read" would be a false statement about a report profile
#: that declares no relation at all.
def _warehouse_reasons() -> frozenset[str]:
    from core import collected_mapped_reader as reader  # noqa: PLC0415

    return frozenset(
        {
            reader.WAREHOUSE_UNAVAILABLE,
            reader.RELATION_ABSENT,
            reader.PROJECT_SCOPE_ABSENT,
            reader.DATE_COLUMN_ABSENT,
        }
    )


# ---------------------------------------------------------------------------
# The action a row offers, BY REASON. This is the point of the typing.
# ---------------------------------------------------------------------------

def action_for(reason: str) -> dict[str, Any]:
    """The one gesture a row offers -- and `absent_at_source` offers NO editor.

    `unresolved-values.md:406-410`: "Offering a pair editor on a row no pair can
    repair is the defect this typing exists to prevent." A reason this function
    does not know falls through to no control at all, which is the safe side: an
    unknown reason with a `Map to...` under it would be a guess acted on.
    """
    from core.unresolved_values import (  # noqa: PLC0415
        REASON_NO_REFERENCE,
        REASON_UNMAPPED,
    )

    if reason == REASON_UNMAPPED:
        return {"kind": "map", "label": "Map to…", "pair_editor": True}
    if reason == REASON_NO_REFERENCE:
        return {"kind": "attach", "label": "Attach…", "pair_editor": False}
    # `absent_at_source`, and anything this function has not been taught.
    return {"kind": "repair_source", "label": None, "pair_editor": False}


# ---------------------------------------------------------------------------
# Auth and guard -- copied from `value_mapping_api.py:56-133`, which is the idiom.
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."},
        status_code=401,
    )


def _not_found(message: str = "Project not found.") -> Response:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _server_error() -> Response:
    return JSONResponse(
        {"code": "server_error", "message": "Server error."}, status_code=500
    )


def _guard(
    project_id: str, identity: str, *, manage: bool
) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may touch it.

    `unresolved-values.md:566-569`: read is org membership, write is `org-manage`,
    and a `project_id` is never believed alone -- it is checked against the guarded
    org, 404 otherwise. Existence-hiding, not 403: a reader who is not a member
    must not learn the Project exists.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                )
                row = cur.fetchone()
            if not row or not row[0]:
                return None, _not_found()
            org_id = str(row[0])
            allowed = (
                identity_can_manage_org(org_id, identity, conn)
                if manage
                else identity_has_org_access(org_id, identity, conn)
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "unresolved_values_api: guard failed project=%s: %s", project_id, exc
        )
        return None, _server_error()

    if not allowed:
        if manage:
            return None, JSONResponse(
                {"code": "forbidden", "message": "Insufficient rights."},
                status_code=403,
            )
        return None, _not_found()
    return org_id, None


# ---------------------------------------------------------------------------
# The window, and what is swept.
# ---------------------------------------------------------------------------


def window_for(today: date | None = None, days: int = WINDOW_DAYS) -> dict[str, Any]:
    """The `[start, end]` the whole payload was measured over, stated."""
    last = today or date.today()
    first = last - timedelta(days=max(1, days) - 1)
    return {"start": first.isoformat(), "end": last.isoformat(), "days": max(1, days)}


def sweepable_dimensions(columns: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """PURE: the grain columns bound to a canonical field, in declaration order.

    Three refusals, and each one is a sentence of the target:

      * not in the grain -- the sweep follows what the mapping says identifies a
        row, exactly as `dq_null_rate` does;
      * no canonical target -- "a column that is not mapped to a dimension has no
        values to resolve" (:394). Its gesture is one panel above;
      * composite -- `country>device` is one partition whose values are `FR>mobile`
        and sweeping it beside its components double-counts (:531).
    """
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for column in columns:
        source = str(column.get("source_field") or "").strip()
        target = column.get("target_field")
        if not source or source in seen or source in _NEVER_SWEPT:
            continue
        if not column.get("is_key_column"):
            continue
        if not isinstance(target, str) or not target.strip():
            continue
        if _COMPOSITE in source or _COMPOSITE in target:
            continue
        seen.add(source)
        found.append(
            {
                "dimension": source,
                "canonical_dimension": target.strip(),
                "sensitivity": str(column.get("sensitivity") or "unknown"),
            }
        )
    return found


# ---------------------------------------------------------------------------
# The two suppliers `unresolved_values.aggregate` takes and never builds itself.
# ---------------------------------------------------------------------------


def _resolver_for(
    project_id: str, canonical_dimension: str
) -> Callable[[Any, str], Any] | None:
    """`(value, connector) -> canonical value` out of the CONFIRMED pairs.

    `resolve_dimension_conformance` is the cascade `PROJECT > ORG > PLATFORM` over
    confirmed rows only -- a proposal is what the product suggested, not what a
    person agreed to, and `unresolved-values.md:186` keeps that door shut. One
    statement per dimension, never one per value.
    """
    from core.dimension_conformance import (  # noqa: PLC0415
        resolve_dimension_conformance,
    )

    try:
        pairs = resolve_dimension_conformance(project_id, canonical_dimension)
    except Exception as exc:  # noqa: BLE001 -- an unreadable store resolves nothing
        logger.warning(
            "unresolved_values_api: conformance unreadable dim=%s: %s",
            canonical_dimension,
            exc,
        )
        return None
    if not pairs:
        return None

    def resolve(value: Any, connector: str) -> Any:
        text = "" if value is None else str(value)
        return pairs.get((connector, text)) or pairs.get(("", text))

    return resolve


def _reference_for(
    conn,
    *,
    project_id: str,
    canonical_dimension: str,
    window: Mapping[str, Any],
) -> tuple[Callable[[Any, str], bool | None] | None, dict[str, Any]]:
    """The set that NAMES these values, or the named gap -- never a guess.

    `dimension_reference.read_reference` says WHICH Datastream names a dimension,
    derived from the mapping's own canonical targets and the `Reference & targets`
    role. It never raises. What it cannot give is the VALUES that stream carries --
    its `relation` column is not selected by its own statement -- so the relation is
    resolved here from the reference stream's connector and report profile, the
    same way the daily-breakdown route resolves its own.

    THE TRI-STATE IS LOAD-BEARING. `False` means the set was consulted and does not
    hold the value: the 516 videos. `None` means no set was consulted at all --
    silence, not a finding -- and the gap falls back to `unmapped`, which a pair
    would close. So every failure below answers `None` and states why; answering
    `False` on an unread catalogue would report every value as `no_reference` and
    send a person to the wrong repair.
    """
    from core.collected_mapped_reader import (  # noqa: PLC0415
        ZONE_COLLECTED,
        describe_relation,
        read_distinct_values,
    )
    from core.datastream_mapping_header import (  # noqa: PLC0415
        classifications_of,
        read_mapping_columns,
        read_stream_facts,
    )
    from core.dimension_reference import read_reference  # noqa: PLC0415
    from core.stage_relation_resolver import resolve_stage_relations  # noqa: PLC0415

    resolved = read_reference(
        conn, project_id=project_id, canonical_dimension=canonical_dimension
    )
    state = {
        "state": resolved.get("state"),
        "gap": resolved.get("gap"),
        "message": resolved.get("message"),
        "datastream_id": (resolved.get("reference") or {}).get("datastream_id"),
        "datastream_name": (resolved.get("reference") or {}).get("name"),
        "consulted": False,
        "window": dict(window),
    }
    reference = resolved.get("reference") or {}
    if resolved.get("state") != "declared" or not reference.get("datastream_id"):
        return None, state

    try:
        facts = read_stream_facts(
            conn, project_id=project_id, datastream_id=str(reference["datastream_id"])
        )
        pair = resolve_stage_relations(
            connector=facts["connector"], report_profile_id=facts["report_profile_id"]
        )
        relation = pair.get("collected_relation")
        if not relation:
            state["gap"] = pair.get("reason")
            state["message"] = pair.get("message")
            return None, state
        header = read_mapping_columns(
            conn, project_id=project_id, datastream_id=str(reference["datastream_id"])
        )
        read = read_distinct_values(
            description=describe_relation(
                project_id=project_id, relation=str(relation), zone=ZONE_COLLECTED
            ),
            project_id=project_id,
            start=str(window["start"]),
            end=str(window["end"]),
            field=str(reference.get("source_field") or ""),
            classifications=classifications_of(header["columns"]),
        )
    except Exception as exc:  # noqa: BLE001 -- an unread catalogue is silence
        logger.warning(
            "unresolved_values_api: reference unreadable dim=%s: %s",
            canonical_dimension,
            exc,
        )
        state["gap"] = "the_reference_values_could_not_be_read"
        state["message"] = (
            "The Datastream that names these values could not be read over this "
            "window, so no value was checked against it."
        )
        return None, state

    if read.get("reason") or not read.get("readable"):
        state["gap"] = read.get("reason")
        state["message"] = read.get("message")
        return None, state

    known = {
        str(row.get("value")) for row in (read.get("values") or []) if row.get("value")
    }
    state["consulted"] = True
    state["known_values"] = len(known)
    # STATED: the catalogue reading is bounded like every other, so a value absent
    # from a truncated page is not proof the catalogue lacks it.
    state["truncated"] = bool(read.get("truncated"))
    if state["truncated"]:
        state["message"] = (
            "The Datastream that names these values was read up to its bound, so "
            "the tail of its catalogue was not consulted."
        )

    def in_reference(value: Any, _connector: str) -> bool | None:
        return str(value) in known

    return in_reference, state


# ---------------------------------------------------------------------------
# The reading, per Datastream. S2 is a loop over this and nothing else.
# ---------------------------------------------------------------------------


def read_datastream_unresolved(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    today: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """One Datastream's unresolved set, one group per swept dimension.

    Raises `DatastreamNotFound` when the pair (stream, project) does not hold --
    the statement behind `read_stream_facts` names both columns, which is what
    AI-219 asks of any reader that takes a stream id from a caller.
    """
    from core.collected_mapped_reader import ZONE_COLLECTED  # noqa: PLC0415
    from core.datastream_mapping_header import (  # noqa: PLC0415
        classifications_of,
        read_mapping_columns,
        read_stream_facts,
    )
    from core.stage_relation_resolver import resolve_stage_relations  # noqa: PLC0415
    from core.unresolved_values import REASONS, read_unresolved_set  # noqa: PLC0415

    window = window_for(today)
    facts = read_stream_facts(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    connector = facts["connector"] or ""
    header = read_mapping_columns(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    columns = header["columns"]
    classifications = classifications_of(columns)
    wanted = sweepable_dimensions(columns)
    dimensions_truncated = len(wanted) > MAX_DIMENSIONS
    wanted = wanted[:MAX_DIMENSIONS]

    base: dict[str, Any] = {
        "datastream_id": datastream_id,
        "connector": connector or None,
        "window": window,
        "dimension_source": "mapping_grain",
        "dimensions_truncated": dimensions_truncated,
        "groups": [],
    }

    if not wanted:
        return {
            **base,
            "state": "not_applicable",
            "reason": "no_mapped_dimension",
            "message": NOT_APPLICABLE_MESSAGE,
        }

    pair = resolve_stage_relations(
        connector=connector, report_profile_id=facts["report_profile_id"]
    )
    relation = pair.get("collected_relation")
    if not relation:
        # The address was never DECLARED. That is not "the warehouse could not be
        # read", and saying so would be a false sentence about a report profile
        # that lands its rows outside the warehouse on purpose.
        return {
            **base,
            "state": "unavailable",
            "reason": pair.get("reason"),
            "message": pair.get("message"),
        }

    warehouse_reasons = _warehouse_reasons()
    groups: list[dict[str, Any]] = []
    for entry in wanted:
        canonical = entry["canonical_dimension"]
        reference, reference_state = _reference_for(
            conn,
            project_id=project_id,
            canonical_dimension=canonical,
            window=window,
        )
        try:
            answer = read_unresolved_set(
                project_id=project_id,
                relation=str(relation),
                zone=ZONE_COLLECTED,
                dimension=entry["dimension"],
                start=str(window["start"]),
                end=str(window["end"]),
                classifications=classifications,
                resolver=_resolver_for(project_id, canonical),
                reference=reference,
                connector=connector,
                limit=limit,
            )
        except Exception as exc:  # noqa: BLE001 -- one dimension takes down no panel
            logger.warning(
                "unresolved_values_api: %s unreadable on %s: %s",
                entry["dimension"],
                datastream_id,
                exc,
            )
            answer = {
                "dimension": entry["dimension"],
                "relation": relation,
                "zone": ZONE_COLLECTED,
                "window": window,
                "state": "unavailable",
                "reason": getattr(exc, "code", "warehouse_unavailable"),
                "message": None,
                "values": [],
                "summary": {
                    "unresolved": 0,
                    "observed_distinct": None,
                    "complete": True,
                    "by_reason": {reason: 0 for reason in REASONS},
                    "occurrences_by_reason": {reason: 0 for reason in REASONS},
                },
                "truncated": False,
            }
        groups.append(
            _group(
                answer,
                entry=entry,
                datastream_id=datastream_id,
                connector=connector,
                warehouse_reasons=warehouse_reasons,
                reference_state=reference_state,
            )
        )

    # ORDERED BY COST, NEVER ALPHABETICALLY. It is the one thing neither Adverity
    # nor Funnel does (`unresolved-values.md:272-276`): a list sorted by name puts
    # the value carrying 40 % of the month between two values carrying three rows.
    groups.sort(key=lambda group: (-(group["occurrences"] or 0), group["dimension"]))

    measured = [group for group in groups if group["state"] == "measured"]
    total = sum(group["unresolved"] or 0 for group in measured)
    if not measured:
        reason = next(
            (group["reason"] for group in groups if group["reason"] in warehouse_reasons),
            groups[0]["reason"] if groups else None,
        )
        return {
            **base,
            "groups": groups,
            "state": "unavailable",
            "reason": reason,
            "message": (
                WAREHOUSE_UNAVAILABLE_MESSAGE
                if reason in warehouse_reasons
                else (groups[0]["message"] if groups else None)
            ),
        }
    return {
        **base,
        "groups": groups,
        "state": "measured",
        "reason": None,
        # The empty case says WHAT WAS MEASURED. It counts the dimensions that were
        # really read, never the ones that were asked for -- an unreadable
        # dimension counted here would claim a check that did not happen.
        "message": empty_message(len(measured)) if total == 0 else None,
    }


def _group(
    answer: Mapping[str, Any],
    *,
    entry: Mapping[str, Any],
    datastream_id: str,
    connector: str,
    warehouse_reasons: frozenset[str],
    reference_state: Mapping[str, Any],
) -> dict[str, Any]:
    """One dimension's block, with its state carried through unchanged."""
    summary = dict(answer.get("summary") or {})
    window_rows = answer.get("window_rows")
    occurrences = sum(
        int(value or 0)
        for value in (summary.get("occurrences_by_reason") or {}).values()
    )
    state = str(answer.get("state") or "unavailable")
    reason = answer.get("reason")
    message = answer.get("message")
    if state == "unavailable" and reason in warehouse_reasons:
        # ONE wording for the one thing that must never read as a zero.
        message = WAREHOUSE_UNAVAILABLE_MESSAGE
    return {
        "key": f"{datastream_id}::{entry['dimension']}",
        "datastream_id": datastream_id,
        "dimension": entry["dimension"],
        "canonical_dimension": entry["canonical_dimension"],
        "connector": connector or None,
        # UNCHANGED: `measured`, `unavailable` or `not_listable`, exactly as the
        # module answered. A screen never infers "did the read happen" from "is the
        # list empty".
        "state": state,
        "reason": reason,
        "message": message,
        # `None`, never `0`, when the reading did not happen (:471).
        "unresolved": summary.get("unresolved") if state == "measured" else None,
        "observed_distinct": summary.get("observed_distinct"),
        "occurrences": occurrences if state == "measured" else None,
        "window_rows": window_rows,
        "row_share": _share(occurrences, window_rows) if state == "measured" else None,
        "complete": bool(summary.get("complete", True)),
        "truncated": bool(answer.get("truncated")),
        "by_reason": summary.get("by_reason"),
        "occurrences_by_reason": summary.get("occurrences_by_reason"),
        "reference": dict(reference_state),
        "values": [
            _value(row, window_rows=window_rows, datastream_id=datastream_id)
            for row in (answer.get("values") or [])
        ],
    }


def _value(
    row: Mapping[str, Any], *, window_rows: Any, datastream_id: str
) -> dict[str, Any]:
    reason = str(row.get("reason") or "")
    return {
        "dimension": row.get("dimension"),
        "source_value": row.get("source_value"),
        "connector": row.get("connector"),
        "occurrences": row.get("occurrences"),
        "row_share": _share(row.get("occurrences"), window_rows),
        # NOT MEASURED, and stated as such rather than defaulted to zero.
        "metric_share": None,
        "reason": reason,
        "repair": row.get("repair"),
        # THE POINT OF THE TYPING. `absent_at_source` carries `pair_editor: false`
        # and no label, so no screen can offer it a pair to add (:406-410).
        "action": action_for(reason),
        "datastream_id": datastream_id,
    }


def _share(part: Any, whole: Any) -> float | None:
    try:
        numerator = int(part or 0)
        denominator = int(whole or 0)
    except (TypeError, ValueError):
        return None
    if denominator <= 0:
        return None
    return round(numerator / denominator, 6)


# ---------------------------------------------------------------------------
# S2: the same reading, one projection wider.
# ---------------------------------------------------------------------------

#: The Datastreams a project-wide sweep opens. `archived_at IS NULL` because an
#: archived flux emits nothing to resolve, and the order is the name so two reads
#: of an unchanged project open the same streams.
_STREAMS_SQL = """
    SELECT id, name
      FROM app.datastreams
     WHERE project_id = %s AND archived_at IS NULL
     ORDER BY name ASC, id ASC
"""


def read_project_unresolved(
    conn,
    *,
    project_id: str,
    today: date | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Every swept Datastream of the Project, grouped by DESTINATION TABLE.

    `unresolved-values.md:420-428`: same reading, one projection wider -- a
    `Datastream` column appears and the grouping is by destination table first,
    "because here a person is looking at the tables, and the panel tells them which
    table is short of which rows".

    IT IS A LOOP OVER S1 AND NOTHING ELSE. Two implementations would be two answers
    to one question, which :482 refuses outright.
    """
    from core.datastream_mapping_header import DatastreamNotFound  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(_STREAMS_SQL, (project_id,))
        rows = cur.fetchall() or []
    names = {str(row[0]): (row[1] or str(row[0])) for row in rows}
    ordered = [str(row[0]) for row in rows]
    streams_truncated = len(ordered) > MAX_STREAMS
    opened = ordered[:MAX_STREAMS]

    window = window_for(today)
    groups: list[dict[str, Any]] = []
    states: list[str] = []
    reasons: list[Any] = []
    messages: list[Any] = []
    for datastream_id in opened:
        try:
            answer = read_datastream_unresolved(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                today=today,
                limit=limit,
            )
        except DatastreamNotFound:  # pragma: no cover -- the list came from the project
            continue
        except Exception as exc:  # noqa: BLE001 -- one flux takes down no lens
            logger.warning(
                "unresolved_values_api: %s unreadable project-wide: %s",
                datastream_id,
                exc,
            )
            states.append("unavailable")
            reasons.append("warehouse_unavailable")
            messages.append(WAREHOUSE_UNAVAILABLE_MESSAGE)
            continue
        states.append(str(answer["state"]))
        reasons.append(answer.get("reason"))
        messages.append(answer.get("message"))
        for group in answer["groups"]:
            groups.append({**group, "datastream_name": names.get(datastream_id)})

    _name_destinations(conn, groups)
    # DESTINATION TABLE FIRST, THEN COST. The table a person is looking at is what
    # they came here for; inside it the heaviest gap is still first.
    groups.sort(
        key=lambda group: (
            group["destination_table"] is None,
            str((group.get("destination_table") or {}).get("table_name") or ""),
            -(group["occurrences"] or 0),
            group["dimension"],
        )
    )

    measured = [group for group in groups if group["state"] == "measured"]
    total = sum(group["unresolved"] or 0 for group in measured)
    if measured:
        state, reason, message = "measured", None, (
            empty_message(len(measured)) if total == 0 else None
        )
    elif "unavailable" in states:
        state, reason = "unavailable", next(
            (one for one in reasons if one), "warehouse_unavailable"
        )
        message = WAREHOUSE_UNAVAILABLE_MESSAGE
    else:
        state, reason, message = "not_applicable", "no_mapped_dimension", (
            "No Datastream of this Project has a grain column bound to a canonical "
            "field, so there is no dimension whose values could be resolved. Bind "
            "one on the Map tab of the Datastream that carries the vocabulary."
        )

    return {
        "window": window,
        "state": state,
        "reason": reason,
        "message": message,
        "groups": groups,
        "datastreams_opened": len(opened),
        "datastreams_total": len(ordered),
        "datastreams_truncated": streams_truncated,
        "dimension_source": "mapping_grain",
    }


def _name_destinations(conn, groups: list[dict[str, Any]]) -> None:
    """Which value table each `(Datastream, column)` is short of rows for.

    Read THROUGH `value_mapping_tables` and never with SQL of our own: the store's
    statements stay in their owner, which `test_value_mapping_precedence` polices
    with a grep so no second module can answer the same question differently.

    A store that cannot be read leaves `destination_table: null` AND says so, so
    the lens never prints "unassigned" for a table it simply could not look up.
    """
    from core.value_mapping_tables import read_field_assignments  # noqa: PLC0415

    by_field: dict[str, list[str]] = {}
    for group in groups:
        by_field.setdefault(str(group["dimension"]), []).append(
            str(group["datastream_id"])
        )
    found: dict[tuple[str, str], dict[str, Any]] = {}
    unreadable = False
    for field, datastream_ids in by_field.items():
        try:
            for row in read_field_assignments(
                conn, datastream_ids=sorted(set(datastream_ids)), source_field=field
            ):
                found[(str(row["datastream_id"]), field)] = {
                    "table_id": row["table_id"],
                    "table_name": row["table_name"],
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "unresolved_values_api: assignments unreadable field=%s: %s", field, exc
            )
            unreadable = True
    for group in groups:
        key = (str(group["datastream_id"]), str(group["dimension"]))
        group["destination_table"] = found.get(key)
        group["destination_state"] = "unknown" if unreadable else "known"


# ---------------------------------------------------------------------------
# The envelope both screens read. ONE shape, so the two cannot disagree.
# ---------------------------------------------------------------------------


def _envelope(
    reading: Mapping[str, Any],
    *,
    project_id: str,
    scope: str,
    grouping: str,
    datastream_id: str | None = None,
) -> dict[str, Any]:
    groups = list(reading.get("groups") or [])
    measured = [group for group in groups if group["state"] == "measured"]
    from core.unresolved_values import REASONS  # noqa: PLC0415

    by_reason = {reason: 0 for reason in REASONS}
    for group in measured:
        for reason, count in (group.get("by_reason") or {}).items():
            if reason in by_reason:
                by_reason[reason] += int(count or 0)
    return {
        "schema": UNRESOLVED_VALUES_SCHEMA,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "scope": scope,
        "grouping": grouping,
        "title": "Values waiting to be mapped",
        "window": reading.get("window"),
        "state": reading.get("state"),
        "reason": reading.get("reason"),
        "message": reading.get("message"),
        "summary": {
            # `None`, never `0`, when no dimension could be read: a header that
            # printed a zero here is the first bullet of "Incomplete if" (:471).
            "unresolved": (
                sum(group["unresolved"] or 0 for group in measured) if measured else None
            ),
            "dimensions_measured": len(measured),
            "dimensions_listed": len(groups),
            "complete": all(group["complete"] for group in measured),
            # THREE REASONS, NEVER ONE COUNT (:470). Always all three keys, even at
            # zero -- "zero of this kind" is a result.
            "by_reason": by_reason,
        },
        "groups": groups,
        "ranking": {"by": "rows", "metric_share": None, "note": RANKING_METRIC_UNMEASURED},
        # WHAT IS BUILT AND WHAT IS NOT, BOTH NAMED. A control that opens nothing
        # is worse than a control that says why it is off, and a missing gesture
        # nobody names is worse than both.
        "repair_drawer": {
            "available": True,
            "note": REPAIR_DRAWER_PARTIAL,
            # The modes come from the server so the drawer cannot offer a fifth
            # one, or spell one of these four differently.
            "match_modes": [
                {"value": mode, "label": MATCH_MODE_LABELS[mode]}
                for mode in MATCH_MODES
            ],
            # WHAT MAY BE SAID AFTER A WRITE. Three answers to one re-read, and
            # the surface picks between them on what the re-read returned -- it
            # never composes a fourth of its own.
            "after_write": {
                "recheck": True,
                "still_listed_note": REPAIR_WRITE_NOT_SEEN,
                "cleared_note": REPAIR_WRITE_CLEARED,
                "unknown_note": REPAIR_WRITE_UNKNOWN,
            },
        },
        "proposals": {"available": False, "note": PROPOSALS_ABSENT},
        "extract": {"available": True, "destination": "value_mapping_table"},
        "import": {
            "available": True,
            "destination": "value_mapping_table",
            "write_mode": "append_by_source_value",
            "write_mode_note": IMPORT_WRITE_MODE,
            "other_destination_note": IMPORT_TEMPLATE_DESTINATION_ABSENT,
        },
        "bounds": {
            "max_dimensions": MAX_DIMENSIONS,
            "max_datastreams": MAX_STREAMS,
            "dimensions_truncated": bool(reading.get("dimensions_truncated")),
            "datastreams_opened": reading.get("datastreams_opened"),
            "datastreams_total": reading.get("datastreams_total"),
            "datastreams_truncated": bool(reading.get("datastreams_truncated")),
        },
        "destination_state": reading.get("destination_state"),
    }


# ---------------------------------------------------------------------------
# The routes.
# ---------------------------------------------------------------------------


async def _datastream_set(request: Request) -> Response:
    """S1 -- `GET /api/projects/{id}/datastreams/{id}/unresolved-values`."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    _org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    from core.admin_api import require_datastream_in_project  # noqa: PLC0415
    from core.datastream_mapping_header import DatastreamNotFound  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            # AI-219: the org was authorized above, the PAIR is proven here. One
            # statement, both columns, and the refusal is the same 404 an absent
            # stream gets -- a distinguishable "wrong project" would let a caller
            # enumerate stream ids by comparing answers.
            if not require_datastream_in_project(
                conn, datastream_id=datastream_id, project_id=project_id
            ):
                return _not_found("Datastream not found.")
            reading = read_datastream_unresolved(
                conn, project_id=project_id, datastream_id=datastream_id
            )
    except DatastreamNotFound:
        return _not_found("Datastream not found.")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "unresolved_values_api: read failed ds=%s: %s", datastream_id, exc
        )
        return _server_error()
    return JSONResponse(
        _envelope(
            reading,
            project_id=project_id,
            scope="datastream",
            grouping="dimension",
            datastream_id=datastream_id,
        )
    )


async def _project_set(request: Request) -> Response:
    """S2 -- `GET /api/projects/{id}/unresolved-values`, the same reading wider."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            reading = read_project_unresolved(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "unresolved_values_api: project read failed %s: %s", project_id, exc
        )
        return _server_error()
    return JSONResponse(
        _envelope(
            reading,
            project_id=project_id,
            scope="project",
            grouping="destination_table",
        )
    )


async def _extract(request: Request) -> Response:
    """S4, its export half -- the two-column file the importer already accepts.

    IT DECLARES ITS DESTINATION RATHER THAN ASKING FOR ONE. The ratified S4 offers
    a choice between the assigned value table and the mapping file behind a
    Template; the second needs the Template's declared fields and a keyed append
    that no code writes yet (`unresolved-values.md:132-146`). So this serves the
    special case the target itself names at :218 -- the value mapping table -- and
    says so in the filename and in the `Content-Disposition`.

    ONLY `unmapped` ROWS REACH THE FILE. `build_extract` filters them, and the
    reason is in its own docstring: an `absent_at_source` line would ask somebody to
    give a name to nothing, and the file would come back mapping `''` to a value.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    _org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    dimension = (request.query_params.get("dimension") or "").strip()
    if not dimension:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "Name the dimension to export with ?dimension=.",
            },
            status_code=422,
        )

    from core.admin_api import require_datastream_in_project  # noqa: PLC0415
    from core.datastream_mapping_header import DatastreamNotFound  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.unresolved_values import (  # noqa: PLC0415
        REASON_UNMAPPED,
        UnresolvedValue,
        build_extract,
    )

    try:
        with get_connection() as conn:
            # The same proof the listing takes, taken again on the door that
            # DOWNLOADS the values: an export is the one gesture where a missing
            # scope check leaves the file itself in the wrong hands.
            if not require_datastream_in_project(
                conn, datastream_id=datastream_id, project_id=project_id
            ):
                return _not_found("Datastream not found.")
            reading = read_datastream_unresolved(
                conn, project_id=project_id, datastream_id=datastream_id
            )
    except DatastreamNotFound:
        return _not_found("Datastream not found.")
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "unresolved_values_api: extract failed ds=%s: %s", datastream_id, exc
        )
        return _server_error()

    group = next(
        (one for one in reading["groups"] if one["dimension"] == dimension), None
    )
    if group is None:
        return _not_found("This dimension is not swept on this Datastream.")
    if group["state"] != "measured":
        # An extract taken from a reading that did not happen would be an empty
        # file presented as "there is nothing left to map".
        return JSONResponse(
            {
                "code": "unresolved_values_unavailable",
                "message": group.get("message") or WAREHOUSE_UNAVAILABLE_MESSAGE,
                "state": group["state"],
            },
            status_code=503,
        )

    text = build_extract(
        [
            UnresolvedValue(
                dimension=str(row["dimension"]),
                source_value=str(row["source_value"]),
                connector=str(row["connector"] or ""),
                occurrences=int(row["occurrences"] or 0),
                reason=str(row["reason"]),
            )
            for row in group["values"]
            if row["reason"] == REASON_UNMAPPED
        ]
    )
    filename = f"{dimension}-unresolved-pairs.csv"
    return PlainTextResponse(
        text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _reach(request: Request) -> Response:
    """S3.3 -- how far a rule reaches, asked BEFORE the confirmation.

    `POST` AND YET IT WRITES NOTHING, deliberately. A regex in a query string is
    a trap -- `+`, `?`, `%` and `&` each mean something else there, and the first
    person to type `a|b` gets a different rule than the one they read back. The
    body carries the pattern verbatim. Nothing on this path opens a cursor for
    writing; "une lecture n'ecrit pas" holds.

    THE READ IS THE PANEL'S OWN READ, not a second one. The reach must be a
    statement about the set the person is looking at; recomputing it from a
    narrower query would let the drawer promise a number the list contradicts.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    _org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, Mapping):
        body = {}
    dimension = str(body.get("dimension") or "").strip()
    pattern = body.get("pattern")
    mode = str(body.get("match_mode") or "exact")
    exclude = body.get("exclude")
    if not dimension:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "Name the dimension the rule belongs to.",
            },
            status_code=422,
        )
    if not isinstance(pattern, str):
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "Type the value, or the part of it the rule should recognise.",
            },
            status_code=422,
        )

    from core.admin_api import require_datastream_in_project  # noqa: PLC0415
    from core.datastream_mapping_header import DatastreamNotFound  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415
    from core.unresolved_rule_reach import rule_reach, sentence  # noqa: PLC0415

    try:
        with get_connection() as conn:
            if not require_datastream_in_project(
                conn, datastream_id=datastream_id, project_id=project_id
            ):
                return _not_found("Datastream not found.")
            reading = read_datastream_unresolved(
                conn, project_id=project_id, datastream_id=datastream_id
            )
    except DatastreamNotFound:
        return _not_found("Datastream not found.")
    except Exception as exc:  # noqa: BLE001
        logger.error("unresolved_values_api: reach failed ds=%s: %s", datastream_id, exc)
        return _server_error()

    group = next(
        (one for one in reading["groups"] if one["dimension"] == dimension), None
    )
    if group is None:
        return _not_found("This dimension is not swept on this Datastream.")
    if group["state"] != "measured":
        # A reach computed over a reading that did not happen would answer 0 and
        # read as "this rule is safe". AD-9: named absence, never a zero.
        return JSONResponse(
            {
                "code": "unresolved_values_unavailable",
                "message": group.get("message") or WAREHOUSE_UNAVAILABLE_MESSAGE,
                "state": group["state"],
            },
            status_code=503,
        )

    reach = rule_reach(
        group["values"],
        pattern=pattern,
        mode=mode,
        exclude=exclude if isinstance(exclude, str) else None,
    )
    reach["sentence"] = sentence(reach)
    reach["dimension"] = dimension
    reach["datastream_id"] = datastream_id
    return JSONResponse(reach, status_code=200 if reach["valid"] else 422)


UNRESOLVED_VALUES_ROUTES: list[Route] = [
    # The literal segment is declared BEFORE nothing here -- both stream paths are
    # distinct literals under the same parameter, so no parameter can swallow a
    # literal (`cleanup_rules_api.py:651-663` states the rule this follows).
    Route(f"{_STREAM_BASE}/extract", _extract, methods=["GET"]),
    Route(f"{_STREAM_BASE}/reach", _reach, methods=["POST"]),
    Route(_STREAM_BASE, _datastream_set, methods=["GET"]),
    Route(_PROJECT_BASE, _project_set, methods=["GET"]),
]

__all__ = [
    "UNRESOLVED_VALUES_ROUTES",
    "UNRESOLVED_VALUES_SCHEMA",
    "action_for",
    "empty_message",
    "read_datastream_unresolved",
    "read_project_unresolved",
    "sweepable_dimensions",
    "window_for",
]
