"""The `Placements` tab: a media plan met by THIS connector's observed spend.

WHAT OPENS THIS TAB. The capability. `datastream-workbench-and-wizard.md`,
amendment « Une capacité activée AJOUTE son onglet » -- cited by name because
that document's line numbers moved in the commit that applied its predecessor:
"`Tax & fees` ajoute l'onglet **Cost**, `Placement mapping` ajoute l'onglet
**Placements**. Éteinte, la capacité n'apparaît nulle part -- ni onglet, ni
panneau, ni colonne." The state is read from `app.project_capabilities` in ONE
statement, before anything else, and no plan and no warehouse are read until it
says yes.

THE SCOPE IS THE CONNECTOR, NOT THE ACCOUNT -- arbitrage A6, and it is a
measurement. `dbt/models/marts/fact_daily_kpi.sql` is grained on
(project_id, date, connector, metric, breakdown_dimension, breakdown_value) and
carries neither `datastream_id` nor `source_account_ref`, so two Datastreams of
one connector on two accounts are indistinguishable in the mart. Filtering by
connector is possible; filtering by account is not. So this surface says HOW
MANY Datastreams share the slice instead of presenting it as this one's -- the
same admission the `Cost` tab already carries -- and never renders `0` for a
count it could not read.

WHICH LINES IT SHOWS: ALL OF THEM -- arbitrage A2. `app.media_plan_lines` has no
`connector` column and never had one; the connector is named by the MATCH,
`app.plan_line_mappings.connector`, which exists. So a line that names this
connector is a line that has a mapping on it, and a line that has none is not an
absence to hide: it is the material of the workshop. Every line of the chosen
plan is listed, each saying whether this connector is attached to it, and the
count says how many of how many.

WHICH PLAN -- arbitrage A5. A Project carries N plans (62 for 153 lines,
measured 2026-08-09) and NONE of them is "active at Project level": only a
VERSION carries `is_active`. Concatenating them would destroy the meaning of
"6 lines out of 24", so the tab carries a plan SELECTOR and the payload names
every plan it could have read. With no plan at all, the empty sentence speaks and
names who creates one.

WHAT IT NEVER FABRICATES. A placement is `(breakdown_dimension,
breakdown_value)` of a dimension the connector DECLARES (arbitrage A3), and 37 of
the 39 connectors declare none. On those, this tab says so with its reason and
shows no placement row, no empty table and no invented identity -- which is the
same answer `capability_compilers.PlacementMappingCompiler` already gives at the
capability level.

ONE WORD FOR THE STATES, AND IT IS BORROWED -- story 61.2, arbitrage A1. Every
plan line carries a `matching_state` and every out-of-plan spend row carries one
too, from `core.plan_matching_states`, whose vocabulary is the one
`file_source_recognizer` already writes for a source column. The tab used to
compose its own reading from a boolean (`attached`), a count (`line_counts`) and
a separate panel, which meant four states existed on screen and none of them had
a name anywhere. The homonym that reuse creates -- one word for a source column
and for a plan line -- is arbitrated in `docs/product-architecture/glossary.md`.

THE FOURTH STATE IS COMPUTED ON REQUEST, AND THE PAYLOAD SAYS SO -- story 61.3.
`ambiguous` needs a set of candidate campaigns and `core/plan_mapping_suggest.py`
is the only thing that can produce one. Story 61.2 said that engine had no caller;
`read_placement_suggestions` below is the one, so the state has a source. It is
still not computed HERE: the sweep is every line against every campaign of the
connector over the plan's window, and charging it to every open would make
everybody pay for a state most of them never look at. So this reading carries the
sentence and the name of the gesture, and the badge lives on the payload that
carries the candidates.

HOW EACH MATCH WAS OBTAINED TRAVELS WITH IT -- story 61.3, the second axis. A
campaign row now says `match_method` (`exact | normalized | similarity | manual`,
the vocabulary of `dimension_conformance`, stored by migration 246) with its
product label, and a score ONLY for `similarity` -- `exact` and `normalized` score
1.0 by construction, and printing that would make a tautology look like a
measurement. A match written before migration 246 has no level, and it renders as
a named absence with the reason: filling it with `manual` would claim a person
typed it, which nothing measured.
"""

from __future__ import annotations

import logging
from typing import Any

from core.placement_mapping import (
    NULL_IS_THE_MEASUREMENT,
    added_columns,
    plan_line_columns,
    unmatched_columns,
)
from core.plan_line_placements import (
    list_placements,
    placement_dimension_for,
)
from core.plan_matching_states import (
    AMBIGUITY_IS_COMPUTED_ON_DEMAND,
    MATCH_METHOD_UNRECORDED_REASON,
    MATCHING_STATE_ACCEPTED,
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
    PLAN_LINE_STATE_LABELS,
    SPEND_STATE_LABELS,
    SUGGEST_MATCHES_LABEL,
    campaign_matching_state,
    campaign_state_label,
    display_match_score,
    line_matching_state,
    mapping_status_label,
    match_method_label,
)
from core.plan_spend_decisions import REASON_REQUIRED_MESSAGE, list_decisions
from core.project_capability_states import (
    PLACEMENT_MAPPING_CAPABILITY_KEY,
    capability_is_active,
    read_capability_state,
)

logger = logging.getLogger(__name__)

#: The three shapes this evidence takes. A fourth -- "broken" -- is not a shape:
#: it is `PlacementEvidenceUnavailable`, a 503, and it never renders as a state.
STATE_AVAILABLE = "available"
STATE_CAPABILITY_INACTIVE = "capability_inactive"
STATE_NO_PLAN = "no_plan"

#: The two ways this tab is empty. Different sentences because they send a person
#: to different doors, and neither of them is the broken one.
EMPTY_NO_PLAN = "no_media_plan"
EMPTY_NO_LINE_NAMES_CONNECTOR = "no_line_names_this_connector"

#: The sentence of each empty case, and who writes the thing that would fill it.
#: Both are fixed by the ratified `Vide` line of story 61.1.
EMPTY_NO_PLAN_MESSAGE = "No media plan has been imported for this Project"
EMPTY_NO_LINE_MESSAGE = "No plan line names this connector yet"
#: WHO OWNS THE GESTURE THIS EMPTINESS NAMES -- amended 2026-08-24.
#:
#: It read `"Governance"` until the arbitrage of that day, and `grep -rln
#: "mediaplan" ui/admin/src/governance/` returned no file: no Governance screen
#: has ever created a media plan. Story 67.26 stopped the console reading this
#: key rather than let it draw a door to nothing. The decision named a real
#: owner -- « the media plan is created and imported in the carrier Datastream's
#: Workbench » -- so the key says it, and `carriers` beside it carries the
#: ADDRESS. An owner named without an address is what made this key unreadable
#: in the first place.
EMPTY_OWNER = "The Workbench of the Datastream that carries the plan"

#: The two emptinesses story 61.2 adds, one per state that can be empty. They are
#: MEASUREMENTS -- the panel keeps the window it read and says over what it looked
#: -- and they share no word with the broken sentence below.
NO_UNMATCHED_SPEND_MESSAGE = "No spend of this connector falls outside this plan"
NOTHING_AWAITING_DECISION_MESSAGE = "No plan line of this plan is waiting for a decision"

#: The sentence of the broken case. It shares no word with either empty sentence,
#: which is the point: "there is nothing" and "we could not look" are not the same
#: report and must never be rendered for one another.
PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE = (
    "The plan-versus-actual reading could not be completed"
)

#: Why the slice shown is the connector's and not this Datastream's.
GRAIN_AMBIGUOUS = "datastream_scope_ambiguous"

#: The emptiness of the suggestion reading -- story 61.3. A MEASUREMENT, and the
#: threshold and the window it looked over are carried beside it, because "nothing
#: resembles anything" without saying how close things had to be is an opinion.
#: It shares no word with the broken sentence above.
NO_CANDIDATE_MESSAGE = (
    "No campaign of this connector resembles a line of this plan closely enough to propose "
    "a match"
)

#: What the suggestion reading does NOT do, said in its own payload rather than
#: only in a docstring. `epic-61:111-112` refuses an autonomous match, and the
#: refusal is proven by a test that counts `app.plan_line_mappings` before and
#: after this call and requires the two counts to be equal.
SUGGESTIONS_WRITE_NOTHING_REASON = (
    "Asking for matches reads the plan and the observed campaigns and writes nothing. A "
    "match exists only once a named person confirms one, and confirming replaces every "
    "match that line already carries."
)

#: What a confirmation refuses, in the words the route answers with. Carried on
#: the suggestion payload so a second spelling of the refusal never appears in the
#: console -- the discipline `reason_required_message` already follows.
#:
#: IT NAMES THE GESTURE THAT REPAIRS IT, AND NO DOOR -- amended 2026-08-24. It
#: ended "A match nothing proposed is made in Governance, by hand", and no
#: Governance screen makes one: `governance` > `master-data` holds six lenses and
#: not one is a media plan, and `grep -rln "mediaplan" ui/admin/src/governance/`
#: returns no file. The two writers of `app.plan_line_mappings` are
#: `set_line_mappings` -- reached by the media plan API route and by the
#: confirmation below -- so a match nothing proposed is made NOWHERE in the
#: console. The repair is to read the proposals again, which is a control of the
#: `Placements` tab itself.
MATCH_NOT_PROPOSED_MESSAGE = (
    "This campaign is not among the matches proposed for this plan line, so there is no "
    "level to record for it. Ask for the matches again and confirm one of the campaigns "
    "proposed for this line."
)

#: The ambiguity IS on this payload, and its candidates with it -- story 61.3.
AMBIGUITY_IS_AVAILABLE_REASON = (
    "A plan line no campaign ventilates yet, with two or more candidates, is an arbitration "
    "somebody owes. So is a campaign two or more plan lines claim: the engine pairs both "
    "ways, and naming only the first side would have hidden half of them."
)

#: Why a Datastream count is absent. NEVER a `0`: nobody counted.
COUNT_UNREADABLE_REASON = (
    "The number of Datastreams collecting from this connector could not be read, so it "
    "is not stated. This is not a zero."
)


class PlacementEvidenceUnavailable(RuntimeError):
    """The warehouse behind the plan-versus-actual reading is unreachable."""

    def __init__(self, message: str = PLACEMENT_EVIDENCE_UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


class PlacementMatchNotProposed(ValueError):
    """This campaign is not a candidate the engine proposes for this line.

    ITS OWN TYPE, because it is the guard that keeps the LEVEL honest. The level a
    match carries is re-derived from the engine at the moment of the confirmation
    and never taken from the caller, so a campaign the engine did not propose has
    no level to write -- and inventing one is the whole fault this axis exists to
    stop. `invalid_request` would have said none of that.
    """


#: Every live plan of the Project, newest first. `archived_at IS NULL`: an
#: archived plan is not a plan somebody may match spend against, and offering it
#: in a selector would invite exactly that.
_PLANS_SQL = """
    SELECT id, name, currency, created_at
    FROM app.media_plans
    WHERE project_id = %s AND archived_at IS NULL
    ORDER BY created_at DESC, name
"""

#: The lines of the plan's ACTIVE version -- the same version
#: `mediaplan_mapping.list_unmapped_actuals` computes its window from, so the two
#: halves of this screen can never describe two different versions of one plan.
_LINES_SQL = """
    SELECT l.line_key, l.label, l.channel, l.start_date, l.end_date,
           l.budget, l.buy_mode, l.is_plan_only, l.sort_order
    FROM app.media_plan_lines l
    JOIN app.media_plan_versions v ON v.id = l.version_id
    WHERE v.plan_id = %s AND v.is_active
    ORDER BY l.sort_order, l.line_key
"""

#: The matches of this plan FOR THIS CONNECTOR. Filtered in the statement rather
#: than after it: another connector's campaigns have no business on this wire.
_MAPPINGS_SQL = """
    SELECT line_key, campaign_ref, split_weight, status, match_method, match_score
    FROM app.plan_line_mappings
    WHERE plan_id = %s AND connector = %s
    ORDER BY line_key, campaign_ref
"""

#: How many matches each line of this plan carries, ACROSS EVERY CONNECTOR.
#: `set_line_mappings` replaces a line's whole set, so what a confirmation is
#: about to rewrite is not the connector-scoped reading above: a line matched to
#: two campaigns of another connector would have been announced as carrying none.
#: The alias is what tells this statement apart from the connector-scoped read.
_LINE_MATCH_COUNTS_SQL = """
    SELECT line_key, COUNT(*) AS match_count
    FROM app.plan_line_mappings
    WHERE plan_id = %s
    GROUP BY line_key
"""

#: How many live Datastreams of this Project land in the same connector slice.
_DATASTREAMS_ON_CONNECTOR_SQL = """
    SELECT COUNT(*)
    FROM app.datastreams
    WHERE project_id = %s AND module_name = %s AND archived_at IS NULL
"""


def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _day(value: Any) -> str | None:
    """An ISO day, or `None`. Never today's date standing in for a missing one."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    return isoformat()[:10] if callable(isoformat) else str(value)[:10]


def _decimal_text(value: Any) -> str | None:
    """A NUMERIC as its exact string. Never a float: `0.333333` is not 1/3."""
    return None if value is None else str(value)


def _read_line_match_counts(conn, plan_id: str) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute(_LINE_MATCH_COUNTS_SQL, (plan_id,))
        rows = cur.fetchall()
    return {str(row[0]): int(row[1]) for row in rows}


def _read_carriers(conn, project_id: str) -> list[dict[str, Any]]:
    """The Datastreams of this Project that carry a media plan, or ``[]``.

    ONE IMPLEMENTATION, in `mediaplan_store`: the Pacing lens asks the same
    question of the same store, and two readings of "who carries a plan" is
    exactly the pair of answers free to diverge that this chantier keeps
    repairing.

    NOT WRAPPED IN A `try`. A project with no file source answers `[]` on its
    own; anything that RAISES here has aborted the Postgres transaction, and
    swallowing it would leave this function reading a connection that answers
    nothing -- handing back a short list of doors instead of a failure anybody
    can see.
    """
    from core.mediaplan_store import list_carrier_datastreams  # noqa: PLC0415

    return list_carrier_datastreams(conn, project_id=project_id)


def _read_plans(conn, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_PLANS_SQL, (project_id,))
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "name": _text(row[1]),
            "currency": _text(row[2]),
            "created_at": row[3],
        }
        for row in rows
    ]


def _read_lines(conn, plan_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_LINES_SQL, (plan_id,))
        rows = cur.fetchall()
    return [
        {
            "line_key": _text(row[0]),
            "label": _text(row[1]),
            "channel": _text(row[2]),
            "start_date": _day(row[3]),
            "end_date": _day(row[4]),
            "budget": _decimal_text(row[5]),
            "buy_mode": _text(row[6]),
            "is_plan_only": bool(row[7]),
        }
        for row in rows
    ]


def _read_mappings(conn, plan_id: str, connector: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_MAPPINGS_SQL, (plan_id, connector))
        rows = cur.fetchall()
    return [
        {
            "line_key": _text(row[0]),
            "campaign_ref": _text(row[1]),
            "split_weight": _decimal_text(row[2]),
            "status": _text(row[3]),
            # `None` on a match older than migration 246, and it stays `None` all
            # the way to the screen, where it renders as a named absence.
            "match_method": _text(row[4]),
            "match_score": row[5],
        }
        for row in rows
    ]


def _compose_grain(conn, *, project_id: str, connector: str) -> dict[str, Any]:
    """Whose slice this is -- with the number, or with the admission that it is absent.

    `datastreams_on_connector` is `None` when the count could not be read, and the
    sentence says so. A `0` here would report a Project with no Datastream on a
    connector whose Datastream is the one being looked at.
    """
    grain: dict[str, Any] = {
        "connector": connector,
        "datastream_grain": False,
        "reason_code": GRAIN_AMBIGUOUS,
        "datastreams_on_connector": None,
        "reason": None,
    }
    try:
        with conn.cursor() as cur:
            cur.execute(_DATASTREAMS_ON_CONNECTOR_SQL, (project_id, connector))
            count = int(cur.fetchone()[0])
    except Exception as exc:  # noqa: BLE001 -- an unread count is not a broken tab
        logger.warning("workbench_placements: datastream_count_unreadable: %s", exc)
        grain["reason"] = COUNT_UNREADABLE_REASON
        return grain

    grain["datastreams_on_connector"] = count
    grain["reason"] = (
        f"This reading is the {connector} slice of the Project, not this Datastream's: "
        f"the mart carries no Datastream discriminator, and {count} Datastream(s) of this "
        "Project collect from this connector."
    )
    return grain


def _read_unmapped(conn, *, plan_id: str, connector: str) -> dict[str, Any]:
    """The spend of THIS connector that no active match ventilates, WITH its decisions.

    `WarehouseUnavailable` becomes `PlacementEvidenceUnavailable` rather than an
    empty panel: an unreachable mart renders exactly like a plan whose every
    campaign is matched, and that shape claims a completeness nobody measured.

    STORY 61.2 -- AN ACCEPTED ROW STAYS. `app.plan_unmatched_spend_decisions` is
    read here and JOINED onto the derivation in memory; it never filters it. A
    campaign somebody accepted as unplanned is still spend the plan did not
    cover, and hiding it once decided would make unplanned money disappear from
    the one surface that reveals it. What changes is the state: `unmatched` is
    waiting for somebody, `accepted` has been answered, by whom and when.
    """
    from core.mediaplan_mapping import (
        UNMAPPED_REASON_LABELS,  # noqa: PLC0415
        list_unmapped_actuals,  # noqa: PLC0415
    )
    from core.warehouse import WarehouseUnavailable  # noqa: PLC0415

    try:
        payload = list_unmapped_actuals(conn, plan_id=plan_id)
    except WarehouseUnavailable as exc:
        logger.warning("workbench_placements: warehouse_unavailable plan=%s: %s", plan_id, exc)
        raise PlacementEvidenceUnavailable() from exc

    decisions = list_decisions(conn, plan_id=plan_id, connector=connector)

    rows: list[dict[str, Any]] = []
    for raw in payload.get("unmapped") or []:
        if str(raw.get("connector") or "") != connector:
            continue
        row = dict(raw)
        decision = decisions.get(str(row.get("campaign_ref") or ""))
        state = MATCHING_STATE_ACCEPTED if decision else MATCHING_STATE_UNMATCHED
        row["matching_state"] = state
        row["matching_state_label"] = SPEND_STATE_LABELS[state]
        # The sentence comes from the module that owns the value. `.get` rather
        # than `[...]`: a reason this module has never heard of renders as an
        # absence, never as the raw token leaking onto a screen.
        row["reason_label"] = row.get("reason_label") or UNMAPPED_REASON_LABELS.get(
            str(row.get("reason") or "")
        )
        # THE NULL HALF OF THE TWO ADDED COLUMNS, and it is why they exist. These
        # rows ARE the spend no active plan line ventilates, so `plan_line_key`
        # and `plan_line_label` are both empty on every one of them -- never a
        # `0`, never an empty string, never an `Unmapped` sentinel, each of which
        # would make unplanned spend look planned.
        row.update(unmatched_columns())
        row["decision"] = decision
        rows.append(row)

    awaiting = sum(1 for row in rows if row["matching_state"] == MATCHING_STATE_UNMATCHED)
    return {
        "window": payload.get("window"),
        "rows": rows,
        "counts": {
            "total": len(rows),
            "accepted": len(rows) - awaiting,
            "awaiting_decision": awaiting,
        },
    }


def _read_observed_placements(
    *, project_id: str, dimension: str | None, window: dict[str, Any] | None
) -> dict[str, Any]:
    """The placement values OBSERVED on this connector over the plan's own window.

    THE CANDIDATES OF THE ATTACH CONTROL, and the reason it reads the plan's
    window rather than a window of its own: a placement observed outside every
    line of the plan is not a placement this plan bought, and offering it would
    invite matching spend the plan never covered.

    `strict=True` is what makes this honest. `query_breakdown_values` swallows a
    backend failure and returns `[]`, which is the exact shape of "this connector
    ran and emitted no placement" -- an unreadable mart would have rendered as a
    measured absence.
    """
    if dimension is None:
        return {"state": "not_applicable", "values": [], "reason": None}
    if not window or not window.get("start") or not window.get("end"):
        return {
            "state": "no_window",
            "values": [],
            "reason": (
                "The active version of this plan has no dated line, so there is no window "
                "over which a placement could have been observed."
            ),
        }

    from core.warehouse import WarehouseUnavailable, query_breakdown_values  # noqa: PLC0415

    try:
        rows = query_breakdown_values(
            project_id, dimension, window["start"], window["end"], strict=True
        )
    except WarehouseUnavailable as exc:
        logger.warning("workbench_placements: placement_read_unavailable: %s", exc)
        raise PlacementEvidenceUnavailable() from exc

    values = [
        {
            "breakdown_dimension": dimension,
            "breakdown_value": row.get("breakdown_value"),
            "row_count": row.get("row_count"),
        }
        for row in rows
    ]
    return {"state": "available", "values": values, "reason": None}


def _governance_owner_reference() -> dict[str, Any]:
    """The ONE door this tab names: where a media plan is created and versioned.

    Semantic, never a composed address -- the console builds the href from the
    canonical navigation registry. Borrowed from
    `capability_proposals.governance_owner_reference`, which is where the section
    of `placement_mapping` is already declared, so the tab, the capability card
    and the MCP name the same door. Writing `master-data` here would be a second
    declaration, and the two would diverge the day one moved.
    """
    from core.capability_proposals import governance_owner_reference  # noqa: PLC0415

    return governance_owner_reference(PLACEMENT_MAPPING_CAPABILITY_KEY)


def _analyze_owner_reference() -> dict[str, Any]:
    """Where the planned-versus-observed reading LIVES -- story 61.4, arbitrage A1.

    It is not this tab. Two ratified documents put pacing in Analyze
    (`analyze-and-test.md`, "Pacing ... is an **Analyze** reading. It is not an
    ingestion surface"; `placement-mapping.md`, whose `Data` row lists what this
    tab must contain and names no budget-versus-actual), so 61.4 adds no
    budget-versus-actual panel here and adds this door instead.

    IT NOW NAMES THE READING, NOT ONLY THE WORKSPACE. Until story 67.26 this
    returned `analyze` > `explore` with no lens, for a measured reason recorded
    here: no console collection read the pacing Result, so there was nothing to
    land on and `sectionOwnsLens` refused every string for that section. 67.26
    built that collection -- `analyze` > `reports`, lens `pacing` -- so the door
    stops opening onto a workspace and opens onto the reading itself.

    The lens is declared in `ui/admin/src/shell/navigation/analyze.ts` beside
    `topics`; if it were ever retired, `sectionOwnsLens` DROPS an unknown lens and
    the reference still resolves to the Reports collection rather than breaking.
    That is why naming it here is safe and guessing a URL would not be.
    """
    from core.project_overview import owner_reference  # noqa: PLC0415 -- shared contract

    reference = owner_reference("analyze", "reports")
    reference["lens"] = "pacing"
    return reference


#: What the door says, and what it admits. The second half is a MEASUREMENT, not a
#: hedge: `known-debt.json` (AI-190) records that no console route reads the pacing
#: Result and that the Report presentation contract it needs does not exist yet, and
#: `analyze-and-test.md` says so in its own words ("Console parity is owed and not
#: delivered"). A door that promised the reading and opened on nothing would be
#: worse than the dead-end text this repo removes everywhere else.
ANALYZE_PACING_LABEL = "Open pacing"
#: Story 67.26 rewrote the second half of this sentence, and the rewrite is the
#: point of the story: it said "the console Report that would carry it is owed and
#: not delivered", which stopped being true the day Analyze > Reports > Pacing
#: shipped. A sentence naming a gap that has closed is a worse lie than one naming
#: a gap that is open -- it sends a person looking for a door they are standing in.
ANALYZE_PACING_REASON = (
    "Planned versus observed -- the consumed share, the pace against the allocation, the "
    "remainder and the extrapolation -- is an Analyze reading, with the reporting currency "
    "and its FX provenance. It is not read on this tab, because one figure with two homes "
    "is two answers. Both the MCP App and the console now carry the same Result: the "
    "context card `mediaplan_pacing` there, Analyze > Reports > Pacing here."
)

#: The KPI variance the plan asks for, and the count that proves it cannot be built.
#: Written rather than silently omitted (story 61.4, acceptance 6).
KPI_VARIANCE_UNAVAILABLE_REASON = (
    "No KPI target is stored anywhere, so no KPI variance can be computed. A media plan "
    "line carries eleven columns -- id, version, key, label, channel, start date, end "
    "date, budget, buy mode, plan-only flag, sort order -- and none of them is a target "
    "for impressions, clicks, CPM or conversions. Only the budget can be compared."
)

#: The sentence each money state says to a person. Every one of them NAMES the
#: currencies it is about: a refusal that does not say which two things disagree is
#: a refusal nobody can act on. `{plan}` / `{spend}` / `{reporting}` are filled in
#: with ISO codes, never with an amount.
MONEY_STATE_ALIGNED = "aligned"
MONEY_STATE_PLAN_MISMATCH = "plan_currency_mismatch"
MONEY_STATE_REPORTING_MISMATCH = "reporting_currency_mismatch"
MONEY_STATE_REPORTING_UNRESOLVED = "reporting_currency_unresolved"
MONEY_STATE_POLICY_UNCONFIRMED = "money_policy_unconfirmed"
#: AI-268. Without a plan currency there is no budget to read anything against,
#: and `aligned` is a statement ABOUT a plan. Composing it from a missing one
#: produced the sentence "This plan's budget and this connector's spend are both
#: in no currency" -- a claim of agreement between one amount and nothing.
MONEY_STATE_NO_PLAN_CURRENCY = "plan_currency_unresolved"

_MONEY_MESSAGES = {
    MONEY_STATE_ALIGNED: (
        "This plan's budget and this connector's spend are both in {plan}, so the two "
        "can be read against each other."
    ),
    MONEY_STATE_PLAN_MISMATCH: (
        "This plan's budget is in {plan} and this connector's spend is converted into "
        "{spend}. The two amounts are each shown under their own currency and nothing "
        "is computed across them -- a budget in {plan} minus a spend in {spend} is not "
        "a remainder."
    ),
    MONEY_STATE_REPORTING_MISMATCH: (
        "This Project's confirmed reporting currency is {reporting}, but its spend was "
        "converted into {spend}. No amount can be labelled until the two agree, so none "
        "is shown."
    ),
    MONEY_STATE_REPORTING_UNRESOLVED: (
        "Nothing names the currency this Project's spend was converted into, so no "
        "observed amount can be labelled and none is shown."
    ),
    MONEY_STATE_POLICY_UNCONFIRMED: (
        "This connector's spend is in {spend}, which an operator set rather than a "
        "confirmed Money Policy. The amounts are shown under {spend} and the Project has "
        "no governed reporting currency yet."
    ),
    MONEY_STATE_NO_PLAN_CURRENCY: (
        "This plan names no currency, so its budget cannot be read against this "
        "connector's spend in {spend}. Set the plan's currency and the two become "
        "comparable."
    ),
}

#: `app.project_preferences.canonical_currency` is the currency the warehouse FX
#: join actually converted INTO -- every `stg_*_daily` reads
#: `fx.to_currency = dim_project.canonical_currency` -- so it is what may label a
#: converted amount. Story 48.3 dropped its `'EUR'` DEFAULT, so NULL here means
#: nobody chose, not "euros by omission".
_CANONICAL_CURRENCY_SQL = """
    SELECT canonical_currency
    FROM app.project_preferences
    WHERE project_id = %s
"""


def _read_conversion_currency(conn, project_id: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(_CANONICAL_CURRENCY_SQL, (project_id,))
        row = cur.fetchone()
    return _text(row[0]) if row and row[0] is not None else None


def _read_money_frame(conn, *, project_id: str, plan: dict[str, Any]) -> dict[str, Any]:
    """WHICH CURRENCY EACH AMOUNT ON THIS TAB IS IN -- story 61.4 (AI-266).

    The tab drew two amounts and named no currency on either: a line's budget,
    exact to the cent, in `media_plans.currency`; and a campaign's observed spend,
    which the warehouse had already converted into the Project's currency. Side by
    side, with nothing saying they were different scales.

    Three currencies exist here and they are not interchangeable:
      * the PLAN's -- `media_plans.currency`, which labels every budget;
      * the CONVERSION's -- `app.project_preferences.canonical_currency`, which is
        what `fact_daily_kpi.value` was converted into and therefore the only
        currency that may label an observed spend;
      * the GOVERNED one -- the confirmed Money Policy's `reporting_currency`.

    An amount is shown only under a currency that produced it. When the governed
    currency and the conversion disagree, no observed amount is shown at all:
    either label would be a false statement, and this repo would rather say
    nothing than say something it cannot support.
    """
    from core.money_policy import PolicyGap, resolve_money_policy  # noqa: PLC0415

    plan_currency = _text(plan.get("currency"))
    conversion_currency = _read_conversion_currency(conn, project_id)

    reporting_currency: str | None = None
    money_policy_version_id: str | None = None
    try:
        policy = resolve_money_policy(conn, project_id=project_id)
    except PolicyGap:
        policy = None
    if policy is not None:
        reporting_currency = policy.reporting_currency
        money_policy_version_id = policy.version_id

    if conversion_currency is None:
        state = MONEY_STATE_REPORTING_UNRESOLVED
    elif reporting_currency is not None and reporting_currency != conversion_currency:
        state = MONEY_STATE_REPORTING_MISMATCH
    elif plan_currency is not None and plan_currency != conversion_currency:
        state = MONEY_STATE_PLAN_MISMATCH
    # AI-268 -- A PLAN WITH NO CURRENCY IS NOT AN ALIGNED PLAN. This test did not
    # exist, so a missing plan currency fell through to `aligned` and the message
    # said "This plan's budget and this connector's spend are both in no currency,
    # so the two can be read against each other" -- agreement asserted between one
    # amount and nothing. It is placed AFTER the mismatch tests on purpose: a
    # currency that disagrees is a sharper fact than one that is absent, and the
    # order of this chain is the order of severity.
    elif plan_currency is None:
        state = MONEY_STATE_NO_PLAN_CURRENCY
    elif reporting_currency is None:
        state = MONEY_STATE_POLICY_UNCONFIRMED
    else:
        state = MONEY_STATE_ALIGNED

    # The spend may be labelled only when the currency that produced it is
    # nameable AND not contradicted by the governed one.
    # `plan_currency_unresolved` is in this list: the SPEND is nameable -- it is
    # the plan side that is missing -- and withholding a label the conversion
    # supports would lose a true statement to protect against a claim nobody made.
    spend_currency = (
        conversion_currency
        if state in (MONEY_STATE_ALIGNED, MONEY_STATE_PLAN_MISMATCH,
                     MONEY_STATE_POLICY_UNCONFIRMED, MONEY_STATE_NO_PLAN_CURRENCY)
        else None
    )
    return {
        "state": state,
        "plan_currency": plan_currency,
        "spend_currency": spend_currency,
        "reporting_currency": reporting_currency,
        "money_policy_version_id": money_policy_version_id,
        "comparable": state == MONEY_STATE_ALIGNED,
        "message": _MONEY_MESSAGES[state].format(
            plan=plan_currency or "no currency",
            spend=conversion_currency or "no currency",
            reporting=reporting_currency or "no currency",
        ),
        # The reading itself is not here, and this says where it is instead of
        # letting a person hunt for a pacing panel that 61.4 deliberately did not
        # build (arbitrage A1).
        "analyze_reference": _analyze_owner_reference(),
        "analyze_label": ANALYZE_PACING_LABEL,
        "analyze_reason": ANALYZE_PACING_REASON,
        "kpi_variance_reason": KPI_VARIANCE_UNAVAILABLE_REASON,
    }


def _compose_lines(
    lines: list[dict[str, Any]],
    mappings: list[dict[str, Any]],
    placements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """The three levels, assembled: line -> campaign -> placements, each with its state.

    A line with no campaign of this connector keeps its row with an empty
    `campaigns` list and `matching_state: "unmatched"`. It is NOT dropped:
    dropping it is what made "only the lines that name this connector"
    unbuildable, and it is the line somebody opens this tab to match.

    `attached` IS GONE -- story 61.2, acceptance 2. It was a boolean the console
    turned into one badge while a count made another and a separate panel made a
    third, so the screen composed four readings out of parts that had no name.
    `matching_state` is that name, and it is not the same predicate: it is `true`
    only when a campaign is ACTIVELY ventilating the line, because
    `plan_vs_actual_daily.sql` gives no money to an `orphaned` match and a line
    whose every match is orphaned is a line still waiting for one.

    `plan_line_placement_mappings.status` IS GONE FROM THE WIRE -- arbitrage A6.
    It is written (`plan_line_placements.py`, `DEFAULT 'active'` and
    `DO UPDATE SET status = 'active'`) and it has never held any other value:
    nothing writes `orphaned` at the placement grain, which migration 244's own
    `COMMENT ON COLUMN` states. A field with one value crossing an API teaches the
    next reader that it means something.
    """
    placements_by_pair: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for placement in placements:
        key = (str(placement["line_key"]), str(placement["campaign_ref"]))
        placements_by_pair.setdefault(key, []).append(
            {
                "id": placement["id"],
                "breakdown_dimension": placement["breakdown_dimension"],
                "breakdown_value": placement["breakdown_value"],
            }
        )

    # THE LABEL COMES FROM THE ACTIVE VERSION'S LINE, and from nowhere else.
    # `plan_line_label` is `app.media_plan_lines.label` read at the plan's active
    # version (`capabilities/placement-mapping.md`), and `_read_lines` is the one
    # reader of that version here -- so the label is looked up rather than
    # re-derived beside the row that already carries it.
    labels_by_line_key = {str(line["line_key"]): line.get("label") for line in lines}

    campaigns_by_line: dict[str, list[dict[str, Any]]] = {}
    for mapping in mappings:
        line_key = str(mapping["line_key"])
        campaigns_by_line.setdefault(line_key, []).append(
            {
                "campaign_ref": mapping["campaign_ref"],
                "split_weight": mapping["split_weight"],
                # THE TWO COLUMNS PLACEMENT MAPPING ADDS TO AN AGGREGATION, on
                # the row where a plan line DID match. Their twins on the
                # out-of-plan rows below are both null, which is the measurement
                # `placement-mapping.md` asks this capability to make.
                **plan_line_columns(
                    line_key=line_key, label=labels_by_line_key.get(line_key)
                ),
                # KEPT, because this one DECIDES: it is the column
                # `plan_vs_actual_daily.sql:137` reads to ventilate. What never
                # travels alone is the raw word -- `status_label` is what a person
                # reads, and `None` when the database says something nobody named.
                "status": mapping["status"],
                "status_label": mapping_status_label(mapping["status"]),
                # STORY 61.3 -- HOW this match was obtained, and what it is worth
                # saying about it. The label is the product word (`None` for a
                # token nobody named, the named absence for a match older than
                # migration 246), and the score is present for `similarity` ALONE:
                # `exact` and `normalized` are 1.0 by construction and a number
                # beside them would read as a measurement of something.
                "match_method": mapping["match_method"],
                "match_method_label": match_method_label(mapping["match_method"]),
                "match_score": display_match_score(
                    mapping["match_method"], mapping["match_score"]
                ),
                "placements": placements_by_pair.get(
                    (line_key, str(mapping["campaign_ref"])), []
                ),
            }
        )

    composed = []
    for line in lines:
        campaigns = campaigns_by_line.get(str(line["line_key"]), [])
        state = line_matching_state(campaigns)
        composed.append(
            {
                **line,
                "matching_state": state,
                "matching_state_label": PLAN_LINE_STATE_LABELS[state],
                "campaigns": campaigns,
            }
        )
    return composed


def read_placements_evidence(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    connector: str,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """The `Placements` tab's evidence. The capability decides alone whether to read.

    Order matters and is the acceptance: the capability state is read FIRST, in
    one statement, and an inactive capability returns with no plan, no line and no
    warehouse round trip at all.
    """
    state = read_capability_state(
        conn, project_id=project_id, capability_key=PLACEMENT_MAPPING_CAPABILITY_KEY
    )
    capability = {
        "key": PLACEMENT_MAPPING_CAPABILITY_KEY,
        "state": state,
        "active": capability_is_active(state),
    }
    dimension = placement_dimension_for(connector)

    if not capability["active"]:
        return {
            "state": STATE_CAPABILITY_INACTIVE,
            "capability": capability,
            # OFF ADDS NOTHING, and this is what makes that checkable: the two
            # column names appear in NO payload an inactive capability produces.
            "added_columns": list(added_columns(state)),
            "added_columns_reason": None,
            "reason": (
                "Placement Mapping is not active on this Project, so no media plan is read "
                "and no placement is matched."
            ),
            "empty_code": None,
            "owner": None,
            "placement_dimension": dimension,
            "plans": None,
            "selected_plan": None,
            "grain": None,
            "lines": None,
            "line_counts": None,
            "unmapped": None,
            "observed_placements": None,
            "governance_owner_reference": None,
            "ambiguity": None,
            "match_level_unrecorded_reason": None,
            "money": None,
        }

    plans = _read_plans(conn, project_id)
    grain = _compose_grain(conn, project_id=project_id, connector=connector)
    owner_reference = _governance_owner_reference()
    common: dict[str, Any] = {
        "capability": capability,
        # WHAT ENABLING THIS CAPABILITY ADDS TO AN AGGREGATION -- the ratified
        # pair, named on the payload rather than left for each surface to guess
        # from the row keys. The reason travels with them because a column that
        # is empty on purpose has to say so once, beside itself.
        "added_columns": list(added_columns(state)),
        "added_columns_reason": NULL_IS_THE_MEASUREMENT,
        "placement_dimension": dimension,
        "plans": plans,
        "grain": grain,
        "governance_owner_reference": owner_reference,
        # THE FOURTH STATE IS ONE GESTURE AWAY, AND THE PAYLOAD NAMES IT -- story
        # 61.3. `available` is `False` on THIS reading because no candidate set was
        # computed for it, not because none can be: the sweep is paid on request,
        # by the control this sentence names, and the badge only ever appears on
        # the payload that carries the candidates. A tab that drew `ambiguous`
        # here, with nothing to show behind it, would be decorating.
        "ambiguity": {
            "available": False,
            "reason": AMBIGUITY_IS_COMPUTED_ON_DEMAND,
            "action_label": SUGGEST_MATCHES_LABEL,
        },
        # Said ONCE, beside the levels it explains, rather than repeated on every
        # match that has none.
        "match_level_unrecorded_reason": MATCH_METHOD_UNRECORDED_REASON,
    }

    if not plans:
        return {
            **common,
            "state": STATE_NO_PLAN,
            "empty_code": EMPTY_NO_PLAN,
            "reason": EMPTY_NO_PLAN_MESSAGE,
            "owner": EMPTY_OWNER,
            # THE ADDRESS OF THAT OWNER -- amended 2026-08-24, the same class fix
            # the Pacing lens gets. This tab asks for a media plan and had no
            # way to say where one is made; the carriers are the Datastreams
            # whose Template reads a file as plan lines, and their Workbench is
            # the ratified host of both gestures. An empty list is not a failure
            # of this tab: it means the Project has no file source that carries a
            # plan yet, and the console then names THAT gesture instead of
            # offering a door to nowhere.
            "carriers": _read_carriers(conn, project_id),
            "selected_plan": None,
            "lines": None,
            "line_counts": None,
            "unmapped": None,
            "observed_placements": None,
            # No plan, so no plan currency -- but the Project's own two currencies
            # are still a fact, and the door to Analyze still exists.
            "money": _read_money_frame(conn, project_id=project_id, plan={}),
        }

    # THE SELECTOR'S DEFAULT, and it is not a silent choice: `selected_plan` is on
    # the payload and the console draws the control from `plans`. An id that names
    # no plan of this Project falls back to the newest rather than 404ing -- a
    # stale bookmark must not make the tab unopenable -- and the payload says
    # which plan was really read.
    by_id = {plan["id"]: plan for plan in plans}
    selected = by_id.get(str(plan_id)) if plan_id else None
    if selected is None:
        selected = plans[0]

    lines = _read_lines(conn, selected["id"])
    mappings = _read_mappings(conn, selected["id"], connector)
    placements = list_placements(conn, plan_id=selected["id"], connector=connector)
    composed = _compose_lines(lines, mappings, placements)
    matched = sum(1 for line in composed if line["matching_state"] == MATCHING_STATE_MATCHED)
    # TWO DIFFERENT QUESTIONS, AND STORY 61.2 SEPARATED THEM. "How many lines does
    # this connector ventilate" is `matched`, which excludes an orphaned-only line
    # because the mart gives it nothing. "Does any line NAME this connector at
    # all" is this, and it is what the `no_line_names_this_connector` sentence
    # says -- an orphaned match still names the connector, so printing that
    # sentence over it would be false.
    named = sum(1 for line in composed if line["campaigns"])

    unmapped = _read_unmapped(conn, plan_id=selected["id"], connector=connector)
    observed = _read_observed_placements(
        project_id=project_id,
        dimension=dimension["dimension"],
        window=unmapped["window"],
    )

    empty_code = None if named else EMPTY_NO_LINE_NAMES_CONNECTOR
    return {
        **common,
        "state": STATE_AVAILABLE,
        "selected_plan": selected,
        # Story 61.4: which currency each amount below is in, and the refusal when
        # two of them are not the same one.
        "money": _read_money_frame(conn, project_id=project_id, plan=selected),
        "lines": composed,
        "line_counts": {
            "total": len(composed),
            "matched": matched,
            # The line-level emptiness of story 61.2: every line of this plan is
            # ventilated by this connector, so nothing here is waiting. `None`
            # when something IS waiting, because a sentence that is always
            # present is a sentence nobody reads.
            "nothing_awaiting_message": (
                NOTHING_AWAITING_DECISION_MESSAGE
                if composed and matched == len(composed)
                else None
            ),
        },
        "empty_code": empty_code,
        "reason": EMPTY_NO_LINE_MESSAGE if empty_code else None,
        "owner": EMPTY_OWNER if empty_code else None,
        "unmapped": {
            "window": unmapped["window"],
            "rows": unmapped["rows"],
            "counts": unmapped["counts"],
            "reason": (
                "Spend this connector reported inside the plan's window that no active match "
                "ventilates. It is a decision to take, not an error."
            ),
            "empty_message": NO_UNMATCHED_SPEND_MESSAGE,
            # The refusal the console shows BEFORE spending a round trip on it,
            # in the exact words the route would answer with. Carried rather than
            # copied: a second spelling of a refusal is a second refusal, and the
            # two diverge the day one is edited.
            "reason_required_message": REASON_REQUIRED_MESSAGE,
        },
        "observed_placements": observed,
    }


# ---------------------------------------------------------------------------
# Story 61.3 -- the candidate set, and the arbitrations it reveals.
# ---------------------------------------------------------------------------


def _suggest(plan_id: str, connector: str) -> dict[str, Any]:
    """The engine, called with the ONE scope this surface has.

    Imported inside the function like every other core.* read of this module, and
    named here rather than at three call sites so the fact that
    `plan_mapping_suggest` finally has a production caller is one line somebody
    can find.
    """
    from core.plan_mapping_suggest import (  # noqa: PLC0415
        suggest_line_mappings_for_plan,
    )

    return suggest_line_mappings_for_plan(plan_id, connector=connector)


def read_placement_suggestions(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    connector: str,
    plan_id: str,
    suggest_fn=None,
) -> dict[str, Any]:
    """Which campaigns of THIS connector could match which lines of this plan.

    A READ, AND IT SAYS SO IN ITS OWN PAYLOAD. `epic-61:111-112` refuses a match
    written without a human act; this reading proposes and writes nothing, and
    `SUGGESTIONS_WRITE_NOTHING_REASON` travels on the answer so the refusal is
    readable at the surface rather than only in this docstring. A test counts
    `app.plan_line_mappings` before and after the route and requires equality.

    THE CAPABILITY DECIDES FIRST, exactly as it does for the tab: a reading that
    answered while the tab it belongs to does not exist would be a second
    authority on whether `placement_mapping` is on.

    ON REQUEST, AND DATED -- arbitrage A4 (b). The sweep is every line of the plan
    against every campaign of this connector over the plan's window; paying it on
    every tab open would charge it to people who never asked for a suggestion, and
    the result would carry no moment. `computed_at` is that moment.

    THE LEVEL IS THE ENGINE'S, NEVER THE CALLER'S. Nothing on this payload lets a
    client state how a match was obtained -- the confirmation re-derives it here,
    from this same engine, which is why the candidate rows carry no ready-made
    entry to post back.

    `WarehouseUnavailable` becomes `PlacementEvidenceUnavailable`: an unreachable
    mart must never render as "nothing resembles anything", which is exactly what
    an empty candidate list says.
    """
    from datetime import datetime, timezone  # noqa: PLC0415

    from core.plan_spend_decisions import plan_belongs_to_project  # noqa: PLC0415
    from core.warehouse import WarehouseUnavailable  # noqa: PLC0415

    state = read_capability_state(
        conn, project_id=project_id, capability_key=PLACEMENT_MAPPING_CAPABILITY_KEY
    )
    capability = {
        "key": PLACEMENT_MAPPING_CAPABILITY_KEY,
        "state": state,
        "active": capability_is_active(state),
    }
    if not capability["active"]:
        return {
            "state": STATE_CAPABILITY_INACTIVE,
            "capability": capability,
            "reason": (
                "Placement Mapping is not active on this Project, so no match is proposed."
            ),
            "plan_id": None,
            "connector": connector,
            "computed_at": None,
            "window": None,
            "similarity_threshold": None,
            "writes": None,
            "counts": None,
            "lines": None,
            "ambiguity": None,
            "empty_message": None,
            "notes": None,
        }

    # THE PLAN IS THE PROJECT'S OR IT DOES NOT EXIST, and it is re-read here as
    # well as at the route: a reader reached directly must not be able to name
    # another Project's plan and read back its line labels.
    if not plan_belongs_to_project(conn, plan_id=plan_id, project_id=project_id):
        return {
            "state": STATE_NO_PLAN,
            "capability": capability,
            "reason": EMPTY_NO_PLAN_MESSAGE,
            "owner": EMPTY_OWNER,
            "plan_id": None,
            "connector": connector,
            "computed_at": None,
            "window": None,
            "similarity_threshold": None,
            "writes": None,
            "counts": None,
            "lines": None,
            "ambiguity": None,
            "empty_message": None,
            "notes": None,
        }

    try:
        computed = (suggest_fn or _suggest)(plan_id, connector)
    except WarehouseUnavailable as exc:
        logger.warning(
            "workbench_placements: suggestion_warehouse_unavailable plan=%s: %s", plan_id, exc
        )
        raise PlacementEvidenceUnavailable() from exc

    lines = _read_lines(conn, plan_id)
    mappings = _read_mappings(conn, plan_id, connector)
    match_counts = _read_line_match_counts(conn, plan_id)

    campaigns_by_line: dict[str, list[dict[str, Any]]] = {}
    matched_refs: set[tuple[str, str]] = set()
    for mapping in mappings:
        campaigns_by_line.setdefault(str(mapping["line_key"]), []).append(mapping)
        if str(mapping["status"] or "") == "active":
            matched_refs.add((connector, str(mapping["campaign_ref"])))

    # WHICH LINES CLAIM EACH CAMPAIGN -- the second side of the ambiguity, and it
    # is read from the CANDIDATES and never from the stored matches: a campaign two
    # lines deliberately share is a ventilation, settled by `split_weight`.
    claims: dict[tuple[str, str], list[str]] = {}
    suggestions_by_line: dict[str, list[dict[str, Any]]] = {}
    for suggestion in computed.get("suggestions") or []:
        pair = (str(suggestion.get("connector") or ""), str(suggestion.get("campaign_ref") or ""))
        # THE SCOPE IS CHECKED AGAIN HERE, and it is not a duplicate of the
        # engine's filter: that one decides what is READ, this one decides what is
        # EMITTED. The acceptance is about the payload -- no candidate of a
        # connector this tab cannot show reaches it -- so the payload is where it
        # is enforced, and a candidate arriving from anywhere else is dropped
        # rather than trusted.
        if pair[0] != connector:
            continue
        line_key = str(suggestion.get("line_key") or "")
        suggestions_by_line.setdefault(line_key, []).append(suggestion)
        claims.setdefault(pair, []).append(line_key)

    composed: list[dict[str, Any]] = []
    total_candidates = 0
    ambiguous_lines = 0
    for line in lines:
        line_key = str(line["line_key"])
        campaigns = campaigns_by_line.get(line_key, [])
        candidates: list[dict[str, Any]] = []
        for suggestion in suggestions_by_line.get(line_key, []):
            pair = (
                str(suggestion.get("connector") or ""),
                str(suggestion.get("campaign_ref") or ""),
            )
            claiming = sorted(set(claims.get(pair, [])))
            campaign_state = campaign_matching_state(len(claiming))
            method = suggestion.get("method")
            candidates.append(
                {
                    "connector": pair[0],
                    "campaign_ref": pair[1],
                    "match_method": method,
                    "match_method_label": match_method_label(method),
                    # `similarity` ALONE carries a number: the other two are 1.0 by
                    # construction, and a 1.0 beside a real 0.89 invites a
                    # comparison between two different kinds of thing.
                    "match_score": display_match_score(method, suggestion.get("score")),
                    "already_matched": pair in matched_refs,
                    "claimed_by_line_keys": claiming,
                    "claimed_by_line_count": len(claiming),
                    "campaign_state": campaign_state,
                    "campaign_state_label": campaign_state_label(campaign_state),
                }
            )
        total_candidates += len(candidates)
        state_word = line_matching_state(campaigns, candidate_count=len(candidates))
        if state_word == MATCHING_STATE_AMBIGUOUS:
            ambiguous_lines += 1
        composed.append(
            {
                "line_key": line_key,
                "label": line["label"],
                "channel": line["channel"],
                "budget": line["budget"],
                "start_date": line["start_date"],
                "end_date": line["end_date"],
                "matching_state": state_word,
                "matching_state_label": PLAN_LINE_STATE_LABELS[state_word],
                # THE COUNT BEFORE THE ACT, and across EVERY connector: confirming
                # a match rewrites this line's whole set, so this is the number the
                # confirmation has to name.
                "matches_today": match_counts.get(line_key, 0),
                "candidates": candidates,
            }
        )

    contested = sum(1 for claiming in claims.values() if len(set(claiming)) >= 2)
    window = computed.get("window")
    return {
        "state": STATE_AVAILABLE,
        "capability": capability,
        "reason": None,
        "plan_id": str(plan_id),
        "connector": connector,
        "computed_at": datetime.now(timezone.utc).isoformat(),
        "window": window,
        "similarity_threshold": computed.get("similarity_threshold"),
        "writes": {
            "any": False,
            "reason": SUGGESTIONS_WRITE_NOTHING_REASON,
            "refusal_message": MATCH_NOT_PROPOSED_MESSAGE,
        },
        "counts": {
            "lines": len(composed),
            "candidates": total_candidates,
            "lines_to_arbitrate": ambiguous_lines,
            "contested_campaigns": contested,
        },
        "lines": composed,
        "ambiguity": {"available": True, "reason": AMBIGUITY_IS_AVAILABLE_REASON},
        # A MEASUREMENT, NOT A BREAKDOWN. The sentence says nothing resembled
        # anything; the threshold and the window say how close things had to be and
        # over what we looked, which is what makes it a measurement.
        "empty_message": None if total_candidates else NO_CANDIDATE_MESSAGE,
        "notes": list(computed.get("notes") or []),
    }


def confirm_placement_match(
    conn,
    *,
    connector: str,
    plan_id: str,
    line_key: str,
    campaign_ref: str,
    actor: str,
    suggest_fn=None,
) -> dict[str, Any]:
    """Turn ONE proposed match into a real one, with the level it was obtained by.

    THE CONFIRMATION IS THE HUMAN ACT, AND IT IS THE ONLY WRITER. `epic-61:111-112`
    refuses a match written without one, and `052:82-88` (AD-9, "no silent value
    fusion") is the precedent that governs even the safest level: an `exact` match
    is a PROPOSAL, not a decision. Nothing in this module writes until this
    function is called with a named `actor`, and it writes through
    `mediaplan_mapping.set_line_mappings` -- the sole write path of epic 22, with
    its per-plan lock, its SUM(split_weight)=1.0 validation and its audit row --
    rather than reaching the table itself.

    THE LEVEL IS RE-DERIVED, NEVER ACCEPTED. The engine is asked again, scoped to
    this connector, and the campaign must be among the candidates it proposes for
    this line; otherwise `PlacementMatchNotProposed`. A caller that could name its
    own level could write `exact` over a 0.89 resemblance, and the whole point of
    this axis is that the number beside `Name similarity` is the engine's.

    WHAT IT REPLACES IS COUNTED BEFORE IT ACTS. `set_line_mappings` deletes a
    line's whole set and re-inserts it, ACROSS EVERY CONNECTOR, so every match the
    line already carries is re-sent here -- with its own level, and with an
    explicit `split_weight` so the ventilation of the campaigns already there does
    not move. `replaced` is that count, taken before the write, and it is what the
    confirmation names to a person.

    A neighbour whose level was never recorded is re-sent with `match_method:
    None`, which is how "nobody knows how this one was made" survives the rewrite.
    Re-sending it as `manual` would state that a person typed it.
    """
    from core.mediaplan_mapping import list_mappings, set_line_mappings  # noqa: PLC0415
    from core.warehouse import WarehouseUnavailable  # noqa: PLC0415

    try:
        computed = (suggest_fn or _suggest)(plan_id, connector)
    except WarehouseUnavailable as exc:
        logger.warning(
            "workbench_placements: confirm_warehouse_unavailable plan=%s: %s", plan_id, exc
        )
        raise PlacementEvidenceUnavailable() from exc

    proposed = None
    for suggestion in computed.get("suggestions") or []:
        if (
            str(suggestion.get("line_key") or "") == line_key
            and str(suggestion.get("connector") or "") == connector
            and str(suggestion.get("campaign_ref") or "") == campaign_ref
        ):
            proposed = suggestion
            break
    if proposed is None:
        raise PlacementMatchNotProposed(MATCH_NOT_PROPOSED_MESSAGE)

    method = str(proposed.get("method") or "")
    score = proposed.get("score")

    existing = []
    for line in list_mappings(conn, plan_id=plan_id).get("lines") or []:
        if str(line.get("line_key") or "") == line_key:
            existing = list(line.get("mappings") or [])
            break

    entries: list[dict[str, Any]] = []
    for mapping in existing:
        if (
            str(mapping.get("connector") or "") == connector
            and str(mapping.get("campaign_ref") or "") == campaign_ref
        ):
            # Already matched: the confirmation is idempotent on the pair, and the
            # level below replaces whatever it carried.
            continue
        entries.append(
            {
                "connector": mapping.get("connector"),
                "campaign_ref": mapping.get("campaign_ref"),
                "split_weight": mapping.get("split_weight"),
                "match_method": mapping.get("match_method"),
                "match_score": mapping.get("match_score"),
            }
        )
    entries.append(
        {
            "connector": connector,
            "campaign_ref": campaign_ref,
            "match_method": method,
            # `exact` and `normalized` are 1.0 by construction; the column keeps
            # what the engine computed and the screens print it for `similarity`
            # alone.
            "match_score": score,
        }
    )

    result = set_line_mappings(
        conn, plan_id=plan_id, line_key=line_key, entries=entries, actor=actor
    )
    return {
        **result,
        "connector": connector,
        "campaign_ref": campaign_ref,
        "match_method": method,
        "match_method_label": match_method_label(method),
        "match_score": display_match_score(method, score),
        "replaced": len(existing),
    }
