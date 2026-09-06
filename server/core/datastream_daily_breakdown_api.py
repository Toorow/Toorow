"""A Datastream read BY DAY, one row per date -- story 58.1, epic 58.

WHY A ROUTE OF ITS OWN. The `Data` tab reads a flux by STAGE and never by date:
its only table carries `Stage, Execution, Versions, Schema, Rows, Grain,
Observed` and not one of those is a day. None of the questions an operator
actually arrives with -- which day is missing, which day came back empty, which
day was re-pulled -- can be asked of it. The day-grain registry to answer them
has existed since story 8.3 (`core.extract_ledger`); what was missing was an
address that serves it inside the project scope, with the columns of the mapping
beside it.

IT DOES NOT DUPLICATE `/ledger` (arbitrage 3). `GET /api/datastreams/{id}/ledger`
keeps answering, and this route calls THE SAME `get_extract_ledger` -- two
envelopes over one reader, never two implementations of "the latest pull covering
this day wins". Retiring the older address belongs to story 58.8, which rebuilds
its one caller.

AND IT IS NOT POLLED. `datastream_progress_api` beside it claims
`pair_proven_by_read=True` because it runs on a five-second timer and a second
membership statement per tick is a real bill. This route is opened by a click.
It claims nothing: the shared guard of AI-219 proves the stream belongs to the
project, and `tests/conformance/test_datastream_readers_carry_project_scope.py`
holds the ratchet at ONE claim so that reasoning cannot spread by copy-paste.

THREE BLOCKS, AND ONLY ONE OF THEM IS COMPLETE (arbitrage 1):

  * `days[]`  -- the extract registry, read from `app.pull_jobs`. It covers every
    connector by CONSTRUCTION and not by measurement: exactly one production
    module inserts into that table (`core/queue.py`), and it holds no
    connector-specific branch, so there is no connector that could be missing
    from it. `tests/conformance/test_pull_jobs_has_one_writer.py` is what keeps
    that sentence true; 39 connectors cannot be pulled to prove it, and the
    disposable cluster carries jobs for one module out of six.

    AND ITS SCOPE IS `source_kind = 'connector_pull'`, WHICH IS A NAMED LIMIT.
    A `managed_feed` (file drop, inbound email) enqueues no pull window at all --
    `dq_monitors.py` states it in those words -- and its `module_name` is `NULL`
    by CHECK (migration 030). Such a flux therefore answers `days: []`,
    `connector: null` and `rows: null` here: it has no extract registry to read,
    not an empty one. That is the honest answer today and it is NOT a complete
    `Data` tab for those fluxes; the day grain of an inbound feed has to be
    derived from the import ledger, which is its own story.
    `tests/core/test_datastream_daily_breakdown.py` pins the absence exactly as
    it is, so whoever closes it starts from a fact rather than a discovery.
  * `columns[]` -- the source-to-canonical pairs, read from the MAPPING and
    never from the mart. The mart is long-form (`project_id, date, connector,
    metric, breakdown_dimension, breakdown_value, value`); it has no column per
    field to read a header from.

    AND THE MAPPING LIVES IN TWO STORES THAT DO NOT OVERLAP (story 58.2,
    arbitrage 2): 44 Datastreams in the flat `app.datastream_mappings`, 328 in
    `app.datastream_mapping_versions`, 0 in both, both written the same day. The
    versioned store answers first, the flat table is the fallback, and
    `columns_source` names the one that spoke -- otherwise an empty header
    cannot be told apart from a reader looking in the wrong place.
  * `days[].rows` -- the mart volume of the day, from `fact_daily_kpi`, which is
    built from 16 of the 39 connectors. For the other 23 the answer is an
    absence WITH ITS REASON, never a zero.

AND THE FIRST TWO DO NOT JOIN THE THIRD. `datastream_mappings.target_field`
points at `app.target_fields`; the mart's `metric` is a literal typed into the
dbt model. Nothing keys one to the other, so this route publishes the header, the
long-form rows, and `column_row_join_available: false` with its reason. Pairing
them on a string that happens to match would be a join that does not exist,
served as a fact -- which is the whole subject of story 58.3.

AND THAT FLAG IS ABOUT THE MART, NOT ABOUT THE READING (amendment 12, lot B2). It
answers "can `columns[]` be drawn over `days[].rows`", and its answer is still no.
It never answered "can the collected reading of a day be put on one row with the
mapped reading of the same day", which is a question about two OTHER relations and
whose key the mapping itself carries. That one is laid in `collected_mapped_pairing`
and travels as `reading.pairing`.

WHAT IT REFUSES, AND WHY EACH REFUSAL IS THE HONEST ANSWER:

  * A window is MANDATORY and capped at `MAX_WINDOW_DAYS`. No default: this
    route is new, it has no caller to spare, and an unbounded read of this table
    is what migration 221 was written against.
  * `collected` and `mapped` are refused with 422 and the warehouse's OWN
    sentence. All four stages collapse onto one materialisation today
    (`cache_warehouse._SAMPLE_SERVED_STAGE`), so serving them as if they
    differed would be a fabricated distinction -- and a reason written on the
    screen instead would be a second, unverifiable answer.
  * Two live Datastreams on one (owner project, connector) make the mart slice
    unattributable -- and that refuses the ROWS, not the request. It answers
    `200` with `rows: null`, `rows_reason: "ambiguous_materialization"` and the
    sentence already written for it. An earlier version answered `409` for the
    whole payload: measured on the disposable cluster, 17 ambiguous groups, so
    34 Datastreams would have had no strip of days at all -- withheld for a
    reason that says nothing about their extract registry.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route

from core import cache_warehouse
from core.extract_ledger import get_extract_ledger

# The payload carries `date` and `timestamptz` values straight off the row; the
# stock `JSONResponse` raises inside `render()`, after the handler returned and
# outside its `try`, which is a bare 500 no error branch ever sees.
from core.json_encoding import SafeJSONResponse as JSONResponse

logger = logging.getLogger(__name__)

#: The envelope contract 58.2 reads, named and versioned the way
#: `datastream_progress.v1` is.
DAILY_BREAKDOWN_SCHEMA = "datastream_daily_breakdown.v1"

DAILY_BREAKDOWN_ROUTE_PATH = (
    "/api/projects/{project_id}/datastreams/{datastream_id}/daily-breakdown"
)

#: ONE number for the two bounded reads of this surface (arbitrage 4). The
#: bounded sample already refuses past it; a strip of days that went wider would
#: show dates the sample beside it cannot open.
MAX_WINDOW_DAYS = cache_warehouse.SAMPLE_MAX_DAYS

#: The stage the MART serves. `collected` and `mapped` have their own relations
#: since story 58.3; `published` still has none.
SERVED_VIEW_MODE = cache_warehouse.SAMPLE_SERVED_STAGE
AVAILABLE_VIEW_MODES: tuple[str, ...] = (SERVED_VIEW_MODE,)

#: THE AVAILABILITY OF EVERY MODE TRAVELS ON THE `200` (story 58.3, arbitrage 2).
#:
#: Until this story the reason a mode could not be served existed ONLY inside the
#: `422` -- so a screen that has to offer `Mapped` greyed out WITH ITS REASON,
#: before anyone clicks, had no source for that sentence and would have had to
#: write one. A sentence written on a screen is a second answer that can disagree
#: with the first, and the story's `Refuse` line forbids it outright.
#:
#: So the payload carries `view_mode.available[] = [{mode, available, reason}]`
#: for the four stages, the sentences come from `stage_relation_resolver` and
#: `cache_warehouse._sample_stage_note`, and the `422` on a FORCED call quotes the
#: same words. Two doors, one wording.
VIEW_MODE_COLLECTED = "collected"
VIEW_MODE_MAPPED = "mapped"
VIEW_MODE_PUBLISHED = "published"

#: Why a response carries no day, or only days nothing ever covered.
REASON_NO_RUN_IN_WINDOW = "no_run_in_window"

#: Why the header is empty. A Datastream whose mapping has not been written yet
#: has no source-to-canonical pair to show, and inventing one column would be
#: the fabrication this repository forbids.
COLUMNS_NO_MAPPING = "no_mapping"

#: THE MAPPING HEADER AND THE COUNTRY SPLIT LIVE BESIDE THIS ROUTE -- lot B2.
#:
#: This module stood at 1293 lines against a repository that refuses 1000, and this
#: lot added a pairing to it. Two responsibilities moved out whole and not one line
#: of them changed: which fields the mapping declares (`datastream_mapping_header`)
#: and what a day unfolds into by country (`datastream_daily_country_split`). They
#: are re-exported here, so every caller and every test that reaches for them
#: through this address still finds them.
from core.datastream_daily_country_split import (  # noqa: E402,F401,I001
    COUNTRY_ACTIVE_STATES,
    COUNTRY_CAPABILITY_KEY,
    COUNTRY_CAPABILITY_NOT_ACTIVE,
    COUNTRY_CONNECTOR_REPORTS_NONE,
    COUNTRY_NO_VALUE_IN_WINDOW,
    COUNTRY_STATE_DEGRADED,
    COUNTRY_STATE_READY,
    COUNTRY_STATE_UNSET,
    MAX_COUNTRY_VALUES_PER_DAY,
    country_split,
    read_country_capability_state,
)
from core.datastream_mapping_header import (  # noqa: E402,F401,I001
    COLUMNS_FROM_FLAT_TABLE,
    COLUMNS_FROM_MAPPING_VERSION,
    classifications_of,
    read_mapping_columns,
    read_stream_facts,
)

#: AND WHAT AN ACTIVE CAPABILITY DOES TO THE READING -- lot B3, amendment 11.
#: « Une capacite activee AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS L'APERCU --
#: jamais une liste de modules. » The tab half is `datastream_workbench_cost` and
#: `datastream_workbench_placements`; this is the field half, for the three
#: capabilities whose whole effect happens inside a row. It lives beside this
#: route for the same reason the country split does: this module refuses 1000
#: lines and the responsibility is one from end to end.
from core import datastream_reading_capabilities  # noqa: E402,I001
from core.project_capability_states import (  # noqa: E402,I001
    capability_is_active,
    read_capability_states,
)

#: either direction. So `columns[]` and `days[].rows` are published as two blocks
#: with the missing pairing STATED -- pairing them on a name that happens to
#: match would be a join this product does not have, presented as a fact.
#:
#: IT SAYS NOTHING ABOUT `reading.pairing`. The header against the MART is one
#: question; the collected reading against the mapped reading is another, its key
#: is the mapping's own `source -> target`, and lot B2 laid it. Reading this flag
#: as "the two readings cannot be paired" is what shipped an explanation in place
#: of the epic's central promise.
COLUMN_ROW_JOIN_AVAILABLE = False
COLUMN_ROW_JOIN_REASON = "mart_metric_is_a_dbt_literal"

#: Why `rows` is absent. Never a `0`: the mart models 16 of the 39 connectors,
#: and answering zero for the other 23 would publish a measurement nobody made.
ROWS_CONNECTOR_NOT_IN_MART = "connector_not_in_mart"
ROWS_WAREHOUSE_UNAVAILABLE = "warehouse_unavailable"

#: AND AMBIGUITY IS A REFUSAL OF THE ROWS, NOT OF THE REQUEST.
#: `fact_daily_kpi` carries `(project_id, connector)` and no Datastream
#: discriminator, so two live Datastreams on one owner-project/connector make
#: that mart slice unattributable -- which is a fact about the MART and about
#: nothing else. The first version of this route answered `409` and took the
#: whole payload with it; measured on the disposable cluster, 17 ambiguous
#: (project, connector) groups meant 34 Datastreams whose `Data` tab would have
#: had no strip at all -- for a reason that has no bearing on the extract
#: registry, which is read from `app.pull_jobs` and is per-Datastream by
#: construction. The days are served, the rows say why they are not.
ROWS_AMBIGUOUS_MATERIALIZATION = "ambiguous_materialization"


#: The day's DATA-QUALITY verdict, which is a different question from the day's
#: EXTRACT verdict and must not borrow its answer (arbitrage 6). Measured
#: 2026-08-06: 0 monitors, 0 evaluations, 0 issues -- and `app.dq_evaluations` is
#: keyed by monitor x window, never by a day of a Datastream. The field exists
#: EMPTY, with its reason, because stories 59.3 and 59.4 are what fill it; a
#: field that borrowed the extract verdict would have to be un-taught later on
#: every reader that had believed it.
DQ_NO_MONITOR = "no_monitor"

_UNAVAILABLE_MESSAGE = "Daily breakdown is unavailable"


#: Raised where the fact is measured -- `datastream_mapping_header` -- and named
#: here because this is the address that turns it into a `404`. ONE class, so an
#: `isinstance` on this side and a `raise` on the other cannot drift apart.
from core.datastream_mapping_header import DatastreamNotFound  # noqa: E402,I001



class RequestRefused(ValueError):
    """A well-formed refusal: a code, a sentence, a status, and what to do next."""

    def __init__(self, code: str, message: str, status: int, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


# ---------------------------------------------------------------------------
# The window, and the view mode. Both decided before anything is read.
# ---------------------------------------------------------------------------


def parse_window(start: str | None, end: str | None) -> tuple[str, str, int]:
    """Return `(start, end, span_days)` or raise the refusal that names the fault.

    NO DEFAULT WINDOW, deliberately. `/ledger` defaults to 35 days because it has
    callers older than the rule; this address has none -- `daily-breakdown` had
    zero occurrences of code on 2026-08-06 -- so it can ask for what it needs
    instead of inheriting a number nobody chose.
    """
    for name, value in (("start", start), ("end", end)):
        if not value:
            raise RequestRefused(
                "missing_param",
                f"{name} is required (YYYY-MM-DD)",
                400,
                parameter=name,
            )
    parsed: dict[str, date] = {}
    for name, value in (("start", start), ("end", end)):
        try:
            parsed[name] = date.fromisoformat(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise RequestRefused(
                "invalid_date",
                f"{name} must be a calendar date in YYYY-MM-DD form",
                400,
                parameter=name,
            ) from exc
    span = (parsed["end"] - parsed["start"]).days + 1
    if span < 1:
        raise RequestRefused(
            "invalid_range", "end is before start", 400, parameter="end"
        )
    if span > MAX_WINDOW_DAYS:
        # The bound is SAID, with the width that was asked for. A list that is
        # silently narrowed reads as the whole answer, which is a different and
        # false statement.
        raise RequestRefused(
            "window_too_wide",
            f"the window spans {span} days; at most {MAX_WINDOW_DAYS} may be read at once",
            400,
            requested_days=span,
            bounded_at=MAX_WINDOW_DAYS,
        )
    return parsed["start"].isoformat(), parsed["end"].isoformat(), span


def _parse_day(requested: str | None, start: str, end: str) -> str | None:
    """The ONE day whose rows are read, and it has to be inside the window.

    A day outside it would be a second, unbounded window arriving under another
    name -- the exact hole `parse_window` exists to close. No default: a reading
    nobody asked for is a warehouse round trip nobody asked for.
    """
    if not requested:
        return None
    value = str(requested).strip()
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise RequestRefused(
            "invalid_date", "day must be a calendar date in YYYY-MM-DD form", 400,
            parameter="day",
        ) from exc
    if not (date.fromisoformat(start) <= parsed <= date.fromisoformat(end)):
        raise RequestRefused(
            "day_outside_window",
            f"day {parsed.isoformat()} is outside the window {start} to {end}",
            400,
            parameter="day",
        )
    return parsed.isoformat()


def resolve_view_mode(requested: str | None) -> str:
    """The requested stage, checked against the ratified vocabulary.

    WHAT MOVED IN 58.3, and why it had to. This function used to refuse anything
    but `processed` outright, before a connection was even opened. It cannot any
    more: whether `collected` and `mapped` can be served is a fact about THIS
    Datastream -- which relation its report profile declares -- and that is read
    from Postgres. So the vocabulary is checked here, and the availability is
    decided in `view_mode_availability` once the stream's facts are known.

    `published` is the one stage still refused without asking: publication is a
    Postgres POINTER over the same mart rows, so there is no relation to read for
    it, for any flux.
    """
    stage = (requested or SERVED_VIEW_MODE).strip() or SERVED_VIEW_MODE
    if stage not in cache_warehouse.SAMPLE_STAGES:
        raise RequestRefused(
            "invalid_stage",
            f"unknown view_mode {stage!r}",
            400,
            available_view_modes=list(AVAILABLE_VIEW_MODES),
        )
    return stage


def view_mode_availability(
    pair: dict[str, Any], description: dict[str, Any]
) -> list[dict[str, Any]]:
    """`[{mode, available, reason}]` for the four stages, in pipeline order.

    AVAILABILITY IS DECIDED ON WHAT IS KNOWN OF THE RELATION, NEVER ON ITS NAME.
    A first version answered `available: True` as soon as the manifest declared a
    relation, and that offered an ENABLED `Collected` to 14 report profiles over 7
    connectors whose relation has no `date` column -- `raw_x_ads_daily`,
    `raw_taboola_history`, `raw_strava_club_daily`, `raw_monday_board_snapshot`,
    `raw_gbp_review`, `raw_gbp_search_keyword_monthly`,
    `raw_linkedin_company_pages_daily`. They would have been refused after the
    click, which is the one thing arbitrage 2 exists to prevent. The description
    beside it costs one statement per relation and answers before it.

    Every sentence is quoted, never composed: the warehouse-shaped refusal comes
    from `cache_warehouse._sample_stage_note`, the relation refusals from
    `collected_mapped_reader`, and the address refusals from
    `stage_relation_resolver` -- each from where its measurement lives.
    """
    collected = description.get("collected") or {}
    mapped = description.get("mapped") or {}
    return [
        {
            "mode": VIEW_MODE_COLLECTED,
            "available": bool(collected.get("readable")),
            "reason": None if collected.get("readable") else collected.get("message"),
            "relation": pair.get("collected_relation"),
        },
        {
            "mode": VIEW_MODE_MAPPED,
            "available": bool(mapped.get("readable")),
            "reason": None if mapped.get("readable") else mapped.get("message"),
            "relation": pair.get("mapped_relation"),
        },
        {
            "mode": SERVED_VIEW_MODE,
            "available": True,
            "reason": None,
            "relation": cache_warehouse._MART_RELATION,
        },
        {
            "mode": VIEW_MODE_PUBLISHED,
            "available": False,
            "reason": cache_warehouse._sample_stage_note(VIEW_MODE_PUBLISHED),
            "relation": None,
        },
    ]


def _served_view_mode(stage: str, available: list[dict[str, Any]]) -> dict[str, Any]:
    """The `view_mode` block, or the `422` a FORCED call earns.

    The refusal carries the availability of all four, so a caller that asked for
    the wrong one is told what it may ask for instead -- and in the same words the
    greyed control was already showing.
    """
    entry = next((item for item in available if item["mode"] == stage), None)
    if entry is None or not entry["available"]:
        raise RequestRefused(
            "stage_not_materialized",
            (entry or {}).get("reason") or cache_warehouse._sample_stage_note(stage),
            422,
            requested_view_mode=stage,
            available_view_modes=[
                item["mode"] for item in available if item["available"]
            ],
            available=available,
        )
    return {
        "requested": stage,
        "served": stage,
        "note": None if stage == SERVED_VIEW_MODE else entry.get("reason"),
        "available": available,
    }


# ---------------------------------------------------------------------------
# The reads. Every one of them carries the pair.
# ---------------------------------------------------------------------------


#: Read ONLY on the branch where nothing covered the window -- the branch that
#: has to say WHY, and which runs once per empty answer rather than on every
#: call. It separates "this stream has never collected anything" from "this
#: stream has collected, just not here", which are two different sentences and
#: one of them is not an error.
_EVER_COLLECTED_SQL = """
    SELECT 1
    FROM app.pull_jobs j
    JOIN app.datastreams d ON d.id = j.datastream_id
    WHERE j.datastream_id = %s AND d.project_id = %s
    LIMIT 1
"""





def stream_has_ever_collected(conn, *, project_id: str, datastream_id: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(_EVER_COLLECTED_SQL, (datastream_id, project_id))
        return cur.fetchone() is not None


def _absent_rows(reason: str, note: str | None = None) -> dict[str, Any]:
    return {"counts": {}, "present": False, "reason": reason, "note": note}


def _mart_rows(
    project_id: str, connector: str, start: str, end: str, *, ambiguous: bool
) -> dict[str, Any]:
    """Per-day mart volume, or the named absence that replaces it.

    EVERY FAILURE HERE IS LOCAL TO THE ROWS. The extract registry lives in
    Postgres and is the half a person came for; nothing the warehouse does --
    being unreachable, not modelling this connector, or holding a slice that
    cannot be attributed to one Datastream -- may take the strip of days down
    with it. Each case says which one it was, on every row, and the days are
    served regardless.
    """
    if ambiguous:
        from core.datastream_sample_api import AMBIGUOUS_MATERIALIZATION_MESSAGE  # noqa: PLC0415

        # The sentence already written for this refusal, carried rather than
        # rewritten: three other readers answer it and a fourth wording would be
        # the one a person quotes back at the wrong moment.
        return _absent_rows(
            ROWS_AMBIGUOUS_MATERIALIZATION, AMBIGUOUS_MATERIALIZATION_MESSAGE
        )
    if not connector:
        return _absent_rows(ROWS_CONNECTOR_NOT_IN_MART)
    try:
        measured = cache_warehouse.read_daily_row_counts(
            project_id=project_id, connector=connector, date_from=start, date_to=end
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "daily_breakdown: warehouse_unavailable connector=%s: %s",
            connector,
            type(exc).__name__,
        )
        return _absent_rows(ROWS_WAREHOUSE_UNAVAILABLE)
    if not measured["connector_present"]:
        return _absent_rows(ROWS_CONNECTOR_NOT_IN_MART)
    return {
        "counts": measured["counts"],
        "present": True,
        "reason": None,
        "note": None,
    }



def read_day_reading(
    *,
    project_id: str,
    pair: dict[str, Any],
    day: str,
    columns: list[dict[str, Any]],
    description: dict[str, Any] | None = None,
    monetary_names: list[str] | None = None,
) -> dict[str, Any]:
    """The two readings of ONE day, or their named absences.

    BOTH SIDES, ALWAYS, FROM ONE CALL. The screen's three positions -- `Collected`,
    `Mapped`, `Side by side` -- are then a choice of what to render: switching
    costs no request, and therefore cannot move the window, the day grid or the
    opened day. That is the story's `Permet` line held by construction rather than
    by a careful component.

    AND THE TWO SIDES ARE PAIRED ON THE ROW -- amendment 12, lot B2. The mapping's
    `source -> target` pairs are handed to the reader, which lays the key while the
    unmasked rows still exist and publishes only masked values. Nothing is paired
    that the mapping does not pair, and every refusal travels as a sentence.

    AND EACH SIDE SAYS WHICH OF ITS COLUMNS IS AN AMOUNT -- story 58.7. The
    designation is made from the SAME description the availability was computed
    from, so it costs no extra statement, and `monetary_names` is the governed
    Semantic Model's answer, read once by the caller. The key is on both sides
    always: empty with its reason when nothing declares an amount (which is the
    state of every project today), never missing -- a key a screen cannot find is
    indistinguishable from a route that could not read the provenance at all, and
    those are the two different sentences the story asks the screen to say.
    """
    from core import collected_mapped_reader, money_provenance_columns  # noqa: PLC0415

    described = description or collected_mapped_reader.describe_pair(
        project_id=project_id, pair=pair
    )
    designations = {
        zone: money_provenance_columns.designate(
            columns=described.get(zone, {}).get("columns"),
            monetary_names=monetary_names,
            zone=zone,
            readable=bool(described.get(zone, {}).get("readable")),
        )
        for zone in (
            money_provenance_columns.ZONE_COLLECTED,
            money_provenance_columns.ZONE_MAPPED,
        )
    }
    # The provenance of a shown amount is shown, of a masked amount masked: the FX
    # columns are named by no mapping, so without this they are unclassified and
    # the second line would read `[MASKED]` under a value the same policy had just
    # decided to show.
    classifications = classifications_of(columns)
    for designation in designations.values():
        classifications = money_provenance_columns.inherited_classifications(
            designation["columns"], classifications
        )

    reading = collected_mapped_reader.read_collected_and_mapped(
        project_id=project_id,
        pair=pair,
        start=day,
        end=day,
        classifications=classifications,
        description=described,
        # THE KEY BETWEEN THE TWO SIDES, and it is the mapping's own -- amendment 12.
        # `columns` already carries `source_field -> target_field` with the grain
        # flagged; nothing else in this payload may decide what pairs with what, and
        # the mart's `metric` literal in particular decides nothing here.
        fields=columns,
    )
    for zone, designation in designations.items():
        side = reading.get(zone)
        if not isinstance(side, dict):
            continue
        side["money_provenance"] = {
            **designation,
            # Read off the rows the payload ALREADY carries -- masking included --
            # so the cell and the line under it can never show two values.
            "rows": money_provenance_columns.row_provenance(
                designation=designation, rows=side.get("rows")
            ),
        }
    reading["day"] = day
    return reading


def read_daily_breakdown(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    start: str,
    end: str,
    span: int,
    view_mode: str = SERVED_VIEW_MODE,
    day: str | None = None,
) -> dict[str, Any]:
    """The whole payload, from one connection and a constant number of statements."""
    from core import money_provenance_columns  # noqa: PLC0415
    from core.datastream_sample_api import datastream_materialization_is_ambiguous  # noqa: PLC0415
    from core.stage_relation_resolver import resolve_stage_relations  # noqa: PLC0415

    facts = read_stream_facts(conn, project_id=project_id, datastream_id=datastream_id)
    connector = facts["connector"]
    # Asked BEFORE the mart is read and NEVER as a gate on the ledger: an
    # unattributable mart slice is a fact about the mart, and `app.pull_jobs`
    # carries its own `datastream_id`.
    ambiguous = datastream_materialization_is_ambiguous(
        conn, data_project_id=project_id, connector=connector
    )

    header = read_mapping_columns(
        conn, project_id=project_id, datastream_id=datastream_id
    )
    columns = header["columns"]

    # The pair of relations, what the warehouse actually knows of each, and the
    # availability of the four modes over BOTH. Read before the ledger so a forced
    # `view_mode` is refused before anything else is measured -- a refusal that
    # arrives after the reads is a refusal that was paid for.
    #
    # The description costs one statement per named relation and is what makes the
    # greyed control honest: a relation with no `date` column is refused HERE and
    # not after the click. The same description is handed to the reading below, so
    # the two can never disagree.
    from core.collected_mapped_reader import describe_pair  # noqa: PLC0415

    pair = resolve_stage_relations(
        connector=connector, report_profile_id=facts["report_profile_id"]
    )
    description = describe_pair(project_id=project_id, pair=pair)
    available = view_mode_availability(pair, description)
    mode = _served_view_mode(view_mode, available)

    # EVERY CAPABILITY THIS PAYLOAD PROJECTS, IN ONE STATEMENT -- lot B3.
    #
    # The day grid needed `country` alone and read it with a statement of its own.
    # Amendment 11 gives `currency_fx` and `reporting_timezone` an effect on the
    # reading too, and asking three times would put three round trips on the table
    # a person opens most. `read_capability_states` answers all of them at once and
    # the country split is HANDED its state rather than re-reading it: two reads of
    # one switch is how two halves of one screen end up disagreeing about it.
    capability_states = {
        key: entry["state"]
        for key, entry in read_capability_states(
            conn,
            project_id=project_id,
            capability_keys=datastream_reading_capabilities.READING_CAPABILITY_KEYS,
        ).items()
    }

    ledger = get_extract_ledger(datastream_id, start, end, conn)
    rows = _mart_rows(project_id, connector, start, end, ambiguous=ambiguous)
    # Story 58.5. One grouping over the window, and only when the capability is on
    # -- never a query per day, on 1 day or on 92.
    country = country_split(
        conn,
        project_id=project_id,
        connector=connector,
        start=start,
        end=end,
        rows=rows,
        state=capability_states.get(COUNTRY_CAPABILITY_KEY),
    )

    days = [_day(entry, rows) for entry in ledger]

    # Story 58.7. WHICH columns are amounts, from the ONE authority allowed to say
    # so -- and only when a day is actually opened, because that is the only answer
    # that carries rows to annotate. Measured 2026-08-07: this returns `[]` on every
    # project of the estate, and the payload says so with the name of what would
    # fill it rather than falling back to a column-name heuristic.
    # AND ONLY WHEN `currency_fx` IS ON -- lot B3, amendment 11. A capability
    # nobody turned on annotates nothing, so the authority it would be read
    # against is not asked either: the statement is saved and, more importantly,
    # the reading never says "no Concept declares an amount" about a projection
    # this Project never requested. `project_onto_reading` puts the refusal that
    # names the shut door on both sides instead.
    monetary_names = (
        money_provenance_columns.monetary_concept_names(conn, project_id=project_id)
        if day
        and capability_is_active(
            capability_states.get(
                datastream_reading_capabilities.CURRENCY_FX_CAPABILITY_KEY, ""
            )
        )
        else None
    )

    covered = any(entry.get("extract_count") for entry in ledger)
    reason: str | None = None
    if not covered:
        reason = REASON_NO_RUN_IN_WINDOW
        # A stream that has never collected ANYTHING has no strip to draw: 92
        # identical rows saying "never fetched" is noise, not evidence. One that
        # HAS collected keeps its strip, because the gap is the information.
        if not stream_has_ever_collected(
            conn, project_id=project_id, datastream_id=datastream_id
        ):
            days = []

    return {
        "schema": DAILY_BREAKDOWN_SCHEMA,
        "project_id": project_id,
        "datastream_id": datastream_id,
        # NAMED, because `rows_reason` is about this connector and a reader that
        # cannot see which one is reading an unattributed absence.
        "connector": connector or None,
        "window": {
            "start": start,
            "end": end,
            "bounded_at": MAX_WINDOW_DAYS,
            "bound_reached": span >= MAX_WINDOW_DAYS,
        },
        "view_mode": mode,
        # The pair, NAMED. Each position of the selector shows the relation it
        # reads, and a position that reads nothing shows why -- the sentence comes
        # from here and the screen holds none of its own.
        "stage_relations": {
            "report_profile_id": pair["report_profile_id"],
            "collected_relation": pair["collected_relation"],
            "mapped_relation": pair["mapped_relation"],
            "reason": pair["reason"],
            "message": pair["message"],
        },
        # Present only when a day was asked for: the reading is one day's rows and
        # nothing on this route reads them uninvited.
        #
        # AND EVERY ACTIVE CAPABILITY LANDS ON IT -- lot B3, amendment 11. The
        # projection is the last thing done to the reading and it is done in one
        # place: an inventory of modules is not a functionality, the effect is, so
        # what travels is the field a capability colours and never a row naming the
        # capability. Off, it adds nothing at all.
        "reading": datastream_reading_capabilities.project_onto_reading(
            conn,
            project_id=project_id,
            reading=(
                read_day_reading(
                    project_id=project_id,
                    pair=pair,
                    day=day,
                    columns=columns,
                    description=description,
                    monetary_names=monetary_names,
                )
                if day
                else None
            ),
            columns=columns,
            states=capability_states,
        ),
        "versions": {
            "plan_version_id": facts["plan_version_id"],
            "mapping_version_id": facts["mapping_version_id"],
            # The ratified `Data` cell asks for samples "tied to exact
            # run/plan/mapping versions". No such binding exists: the mart is
            # the top of the dbt pipeline and carries no version column, which
            # is why `_get_datastream_sample` already answers False here. The
            # honest answer is the same one, said once.
            "version_binding_available": False,
        },
        # The one sentence the rows half has when it cannot be served. Only the
        # ambiguity refusal owns a ratified wording; the other two absences are
        # machine names a screen renders, and writing server prose for them here
        # would be inventing copy nobody ratified.
        "rows_note": rows["note"],
        # THE STATE OF A PROJECT CAPABILITY, ON THE PAYLOAD THAT CARRIES THE DATA IT
        # QUALIFIES (arbitrage 6). This envelope is the only thing the day grid reads,
        # and the rule this lot settled in 58.1 and 58.3 is that the reason arrives
        # WITH the number it explains -- never from a second source a screen would
        # have to reconcile. Story 58.6 (`Cost`) and the `Placements` tab reuse this
        # seam instead of each re-opening `app.project_capabilities`.
        "country": country,
        "columns": columns,
        "columns_reason": None if columns else COLUMNS_NO_MAPPING,
        # WHICH store answered. An empty header is otherwise a guess: "no mapping
        # yet" and "this reader looks in one place out of two" render the same.
        "columns_source": header["source"],
        "column_row_join_available": COLUMN_ROW_JOIN_AVAILABLE,
        "column_row_join_reason": COLUMN_ROW_JOIN_REASON,
        "days": days,
        "reason": reason,
    }


def _day(entry: dict[str, Any], rows: dict[str, Any]) -> dict[str, Any]:
    """One ledger day, re-said in this envelope's words."""
    day = entry["date"]
    landed = rows["counts"].get(day, 0) if rows["present"] else None
    payload = {
        "date": day,
        # TWO verdicts, named apart (arbitrage 6). `extract_status` is what the
        # pull did; `dq_verdict` is what a control said about the rows.
        "extract_status": entry.get("status"),
        # The window's own state, so a day somebody STOPPED does not read as a
        # day nobody ever asked for (arbitrage 7).
        "job_state": entry.get("job_state"),
        "extract_count": entry.get("extract_count", 0),
        # Story 58.1, arbitrage 9: a window's total is not a day's total. The
        # ledger now says so itself, so `/ledger` and `CoverageStrip` are
        # repaired by the same change.
        "row_count": entry.get("row_count"),
        "row_count_reason": entry.get("row_count_reason"),
        "expected_rows": entry.get("expected_rows"),
        "completeness_ratio": entry.get("completeness_ratio"),
        "pull_id": entry.get("pull_id"),
        "loaded_at": entry.get("loaded_at"),
        "execution_id": entry.get("execution_id"),
        "provenance": entry.get("provenance"),
        "dq_verdict": None,
        "dq_reason": DQ_NO_MONITOR,
        "rows": landed,
        "rows_reason": rows["reason"],
    }
    # Additive, and only where it means something: `error_class` / `user_action`
    # exist on a failed day and nowhere else (story 25.2), and the prevented pair
    # exists on a prevented day and nowhere else (AI-307). The sentence is the
    # CONNECTOR'S, carried whole from the window that recorded it: the grid says
    # which grant to go and obtain, or it says nothing a person can act on.
    for field in ("error_class", "user_action", "prevented_reason", "prevented_message"):
        if field in entry:
            payload[field] = entry[field]
    return payload


# ---------------------------------------------------------------------------
# The address.
# ---------------------------------------------------------------------------


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, DatastreamNotFound):
        return JSONResponse({"code": "not_found", "message": "Datastream not found"}, 404)
    if isinstance(exc, RequestRefused):
        return JSONResponse(
            {"code": exc.code, "message": exc.message, **exc.extra}, exc.status
        )
    # Without the type in the log the 503 is a wall: the Cloud Run entry
    # otherwise carries the HTTP line and nothing else.
    logger.error(
        "datastream_daily_breakdown: unmapped_error %s", type(exc).__name__, exc_info=exc
    )
    return JSONResponse({"code": "unavailable", "message": _UNAVAILABLE_MESSAGE}, 503)


async def _read_daily_breakdown(request: Request) -> Response:
    """GET /api/projects/{project_id}/datastreams/{datastream_id}/daily-breakdown."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, 401
        )

    project_id = request.path_params["project_id"]
    datastream_id = request.path_params["datastream_id"]
    try:
        # Both refusals are decided before a connection is opened: a malformed
        # window has nothing to read and must not pay for a round trip.
        start, end, span = parse_window(
            request.query_params.get("start"), request.query_params.get("end")
        )
        view_mode = resolve_view_mode(request.query_params.get("view_mode"))
        day = _parse_day(request.query_params.get("day"), start, end)
    except RequestRefused as refused:
        return _error(refused)

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            denied = _require_datastream_role(
                project_id,
                identity,
                "viewer",
                conn,
                datastream_id=datastream_id,
                # NO `pair_proven_by_read`: this route is opened by a click, not
                # polled, so it pays for the membership proof like every other
                # surface of this family. The ratchet in
                # `test_datastream_readers_carry_project_scope` holds at one.
            )
            if denied is not None:
                return denied
            payload = read_daily_breakdown(
                conn,
                project_id=project_id,
                datastream_id=datastream_id,
                start=start,
                end=end,
                span=span,
                view_mode=view_mode,
                day=day,
            )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    return JSONResponse(payload)


datastream_daily_breakdown_routes = [
    Route(
        DAILY_BREAKDOWN_ROUTE_PATH,
        endpoint=_read_daily_breakdown,
        methods=["GET"],
        name="datastream-daily-breakdown",
    ),
]
