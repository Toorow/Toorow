"""The `Cost` tab: the governed fee/tax cascade, applied to THIS Datastream.

WHAT OPENS THIS TAB. The capability. `datastream-workbench-and-wizard.md`,
amendment « Une capacité activée AJOUTE son onglet » -- cited by name because
that document's line numbers moved in the commit that applied its predecessor:
"`Tax & fees` ajoute l'onglet **Cost** [...] Éteinte, la capacité n'apparaît
nulle part -- ni onglet, ni panneau, ni colonne." The state is read from
`app.project_capabilities` in ONE statement, before anything else, and the
warehouse is asked only when it is active.

THIS TAB IS A READ. The editing surface for fee/tax rules is the MCP and the
governed Rule Set ([[fee-tax-surface-mcp-not-console]]); the analysis waterfall
lives in Analyze (`alignment-register.md`, "waterfall in Analyze; applicability
in Datastream"). Nothing here writes, and the only gesture it offers is a
semantic `owner_reference` to Governance.

THE MARCHE IS THE PHASE, AND EACH PHASE NAMES THE LEVELS THAT LAID IT.
`fee_tax_ladder_daily` aggregates by PHASE: five phase columns, one
`applied_rule_ids` string per row, and no relation of (rule, contribution). So a
step per rule WITH ITS AMOUNT cannot be read from anything shipped, and this
surface SAYS so instead of inventing it -- an amount attributed to the wrong rule
is worse than an aggregation that admits what it aggregates.

What IS readable, and what Jean asked for, is the LEVEL: `applied_rule_ids` is a
`STRING_AGG(f.rule_id, '|')` and `app.fee_tax_rules_dim_v` carries `scope_kind`,
so the identifiers resolve into levels. Each phase therefore names the levels
that laid it and how many rules each level contributed -- a COUNT, never an
identifier printed beside a number it did not produce.

THIS PARAGRAPH SAID `app.fee_tax_rules` UNTIL 2026-08-17, AND THAT IS WHERE THE
MISTAKE WAS RECORDED AS A FACT. Story 48.4 redefined `app.fee_tax_rules_dim_v`
over the PUBLISHED `tax_fee` Rule Set version (migration 146), so the mart the
sentence above describes stopped mirroring the mutable table on that day. The
view mints `<rule_set_version_id>:<rule_key>`; the table mints `ftr_<ULID>`. The
resolution therefore matched NOTHING, every time, and the tab served
`LEVEL_UNRESOLVED_REASON` as a permanent state -- a sentence about the Project's
data, caused by a cutover the Project had no part in.

THREE LEVELS EXIST, TWO DO NOT, AND BOTH FACTS ARE PUBLISHED.
`SCOPE_KINDS = {project, plan_version, datastream}` (`fee_tax_rules.py`), a
mirror of migration 119's CHECK. `organisation` is not a `scope_kind` at all.
`source_category` exists AS DATA -- eight closed values, declared by 39 of the 39
connectors through `public_catalog.category` -- but not as a configuration level,
because it is a property of the connector and not of the Datastream (glossary,
entry « Source category »). And `plan_version` is rendered for what it is: a
version of a media plan, not a storey of configuration.

ONE RULE AUTHORITY, READ THROUGH TWO PROJECTIONS OF IT. Both the applied
identifiers and the refused rules now come from the same published `tax_fee`
Rule Set version: the mart mirrors `app.fee_tax_rules_dim_v`, and the compiled
capability projection carries the ladder. What must still never be paired on
resemblance is a bare `rule_key` against a full identifier -- the view's id is
`<rule_set_version_id>:<rule_key>`, so the version half is part of the identity
and dropping it would join rules of two different published versions.

NO DATA IS LIT ANYWHERE TODAY, and this tab says it rather than hiding it.
Measured 2026-08-07 on the disposable cluster: `tax_fees` is `disabled` on every
Project, and `app.fee_tax_rules`, `app.governance_rule_sets`,
`app.datastream_tax_evidence` and `app.tax_fee_preset_versions` hold 0 rows. The
only lit cascade in the repository is the DuckDB fixture `feetax_dev_complete`.
So every branch below is reachable, and the one a real Project reaches today is
the empty one.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.project_capability_states import (
    TAX_FEES_CAPABILITY_KEY,
    capability_is_active,
    read_capability_state,
)

logger = logging.getLogger(__name__)

#: How many calendar days of cascade this tab reads, ending today.
#:
#: STATED ON THE PAYLOAD, never silent. The tab offers no window control -- its
#: `Permet` line offers exactly one gesture, opening the governed Rule Set -- so
#: a bound is unavoidable, and a bound nobody can see reads as "this is the whole
#: history". 30 is the width of a monthly close, which is the question this
#: cascade answers.
COST_WINDOW_DAYS = 30

#: The five phase columns of `fee_tax_ladder_daily`, in cascade order, each with
#: the rule category that routes into it (`fee_tax_rules._CATEGORY_FORMS` and the
#: phase CTEs of the mart). DATA, never an if/elif chain: a chain is where the
#: sixth phase gets forgotten and falls through as a zero.
CASCADE_PHASES: tuple[dict[str, str], ...] = (
    {
        "key": "platform_fee",
        "column": "platform_fee_micros",
        "category": "PLATFORM_FEE",
        "label": "Platform fee",
    },
    {
        "key": "regulatory_tax",
        "column": "regulatory_tax_micros",
        "category": "REGULATORY_TAX",
        "label": "Regulatory tax",
    },
    {
        "key": "wht_gross_up",
        "column": "wht_gross_up_micros",
        "category": "WHT_GROSS_UP",
        "label": "Withholding gross-up",
    },
    {
        "key": "agency_fee",
        "column": "agency_fee_micros",
        "category": "AGENCY_FEE",
        "label": "Agency fee",
    },
    {
        "key": "sales_tax",
        "column": "sales_tax_micros",
        "category": "SALES_TAX",
        "label": "Sales tax",
    },
)

#: What the surface says about the aggregation it is showing, in its own words.
CASCADE_AGGREGATION = "phase"
CASCADE_AGGREGATION_REASON = (
    "The cascade is aggregated by PHASE, not by rule: the mart carries five phase "
    "columns and one list of applied rule identifiers per row, and no relation of "
    "rule to contributed amount. Each phase names the levels that laid it; an "
    "amount per rule needs a relation that does not exist yet."
)

#: The three levels a rule can be laid at, with what each covers. The vocabulary
#: is `fee_tax_rules.SCOPE_KINDS`, which mirrors migration 119's CHECK.
CASCADE_LEVELS: tuple[dict[str, str], ...] = (
    {
        "kind": "project",
        "label": "Project",
        "covers": "Every Datastream of this Project.",
    },
    {
        "kind": "plan_version",
        "label": "Plan version",
        "covers": (
            "One published version of a media plan. It is a version of a plan, not a "
            "storey of configuration."
        ),
    },
    {
        "kind": "datastream",
        "label": "Datastream",
        "covers": "This Datastream alone.",
    },
)

#: The two levels a person asks for and the store does not carry. Published with
#: their substance so the next story starts from a measurement instead of a wish.
ABSENT_LEVELS: tuple[dict[str, str], ...] = (
    {
        "name": "Organisation",
        "reason": (
            "There is no organisation level: `scope_kind` admits project, plan version "
            "and Datastream, and nothing else. Opening one is a migration."
        ),
    },
    {
        "name": "Source category",
        "reason": (
            "A source category exists as data -- eight closed values, declared by all 39 "
            "connectors -- but not as a level a rule can be laid at, because it is a "
            "property of the connector and not of the Datastream."
        ),
    },
)

#: Why a rule identifier could not be resolved into a level. Counted and named,
#: never dropped: a level silently missing reads as a level nobody used.
LEVEL_UNRESOLVED_REASON = (
    "applied by rules this Project's rule store no longer carries, so their level "
    "cannot be named"
)

#: The four headline measures, in reading order.
MEASURE_NET_MEDIA = "net_media"
MEASURE_WHAT_WE_ADD = "what_we_add"
MEASURE_TOTAL = "total"
MEASURE_UPLIFT = "uplift"

#: Why a measure is absent. NEVER a `0`: the mart makes a phase column NULL
#: exactly so a `0` can always be read as a real zero.
GAP_LADDER_INCOMPLETE = "ladder_incomplete"
GAP_PHASE_NOT_EVALUATED = "phase_not_evaluated"
GAP_NO_NET_MEDIA = "no_net_media_to_compare"
GAP_MIXED_CURRENCIES = "mixed_currencies_in_window"

#: The three states this evidence can be in. A fourth -- "broken" -- is not a
#: state: it is `CostCascadeUnavailable`, a 503, and it never renders as a shape.
STATE_AVAILABLE = "available"
STATE_EMPTY = "empty"
STATE_CAPABILITY_INACTIVE = "capability_inactive"

#: The two ways this tab is empty. They are different sentences because they send
#: a person to different doors, and one of them is not a problem.
EMPTY_NO_RULE_PUBLISHED = "no_rule_published"
EMPTY_NO_LADDER_ROW = "no_ladder_row_in_window"

#: The sentence of the empty case the ratified `Vide` line fixes, and who writes
#: the thing that would fill it.
EMPTY_NO_RULE_PUBLISHED_MESSAGE = "No fee or tax rule has been published for this Project"
EMPTY_OWNER = "Governance"

#: The sentence of the broken case. It shares no word with the empty one, which
#: is the point: "there is nothing" and "we could not look" are not the same
#: report and must never be rendered for one another.
COST_CASCADE_UNAVAILABLE_MESSAGE = "The cost cascade could not be read"

#: Why the cascade of a connector is not the cascade of this Datastream.
GRAIN_AMBIGUOUS = "datastream_scope_ambiguous"


class CostCascadeUnavailable(RuntimeError):
    """The warehouse relation behind the cascade is absent or unreachable."""

    def __init__(self, message: str = COST_CASCADE_UNAVAILABLE_MESSAGE) -> None:
        super().__init__(message)


#: Every rule of the Project, with the two fields that name a level and a phase.
#: One statement: the resolution of `applied_rule_ids` and the count that decides
#: the empty sentence are the same read, and reading them twice would let the two
#: halves of one answer disagree.
#:
#: IT READS THE VIEW, NOT THE TABLE, AND THAT IS THE WHOLE POINT (2026-08-17).
#: It selected `app.fee_tax_rules` -- the mutable pre-governance store -- while
#: the numbers beside it come from `fee_tax_ladder_daily`, whose
#: `applied_rule_ids` are minted by `app.fee_tax_rules_dim_v`. Story 48.4
#: redefined that view over the PUBLISHED `tax_fee` Rule Set version (migration
#: 146), so its ids are `<rule_set_version_id>:<rule_key>` while the table mints
#: `ftr_<ULID>`. The two namespaces cannot collide: `rules_by_id.get()` missed
#: every applied id, every time, and the screen served
#: `LEVEL_UNRESOLVED_REASON` -- a sentence about the Project's data -- as the
#: permanent consequence of a cutover the Project had nothing to do with.
#: Same shape as the `_resolve_members` read AI-295 removed: a lookup that works
#: only because it always fails.
_PROJECT_RULES_SQL = """
    SELECT id, scope_kind, scope_ref, category, status
    FROM app.fee_tax_rules_dim_v
    WHERE project_id = %s
    ORDER BY cascade_phase, sequence_order, id
"""

#: How many live Datastreams of this Project land in the same connector slice.
#: The VERDICT is `datastream_sample_api.datastream_materialization_is_ambiguous`, which is
#: the one authority and fails closed; this only supplies the number the sentence
#: needs, and is asked only once the verdict has already said yes.
_DATASTREAMS_ON_CONNECTOR_SQL = """
    SELECT COUNT(*)
    FROM app.datastreams
    WHERE project_id = %s AND module_name = %s AND archived_at IS NULL
"""


def _micros(value: Any) -> int | None:
    """A micros column, or `None`. A NULL is an absence and never a zero."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(Decimal(str(value)))
    except (InvalidOperation, ValueError, TypeError):
        return None


def _window(today: date) -> dict[str, Any]:
    start = today - timedelta(days=COST_WINDOW_DAYS - 1)
    return {
        "start": start.isoformat(),
        "end": today.isoformat(),
        "days": COST_WINDOW_DAYS,
        "reason": (
            f"The cascade is read over the last {COST_WINDOW_DAYS} days. This tab holds no "
            "window control, so the bound is stated rather than left to be mistaken for "
            "the whole history."
        ),
    }


def _measure(
    key: str,
    label: str,
    *,
    micros: int | None = None,
    unit: str = "micros",
    currency: str | None = None,
    percent: str | None = None,
    gap_code: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "micros": micros,
        "percent": percent,
        "currency": currency,
        "gap_code": gap_code,
        "reason": reason,
    }


def _uplift_percent(added: int | None, net_media: int | None) -> str | None:
    """`what we add` over `net media`, to one decimal, or `None`.

    Computed in `Decimal` over integers, so the ratio of two exact micros amounts
    is never rounded through a float.
    """
    if added is None or not net_media:
        return None
    return str(
        (Decimal(added) * Decimal(100) / Decimal(net_media)).quantize(Decimal("0.1"))
    )


def _levels_of_phase(
    category: str,
    rule_ids: Sequence[str],
    rules_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Which levels laid this phase, and how many DISTINCT rules each contributed.

    A COUNT PER LEVEL, NEVER AN IDENTIFIER BESIDE AN AMOUNT. The phase carries one
    number for every rule that fired into it; printing the rules' names next to
    that number invites reading the number as one rule's contribution, and a false
    attribution is worse than an aggregation that admits what it is.

    ⚠️ IT COUNTS RULES, NOT OCCURRENCES, AND THE CALLER MUST HAND IT DISTINCT IDS.
    `applied_rule_ids` is a per-ROW column: the same rule fires on every row of the
    window, so a window of six rows -- the ordinary shape, three days x two
    campaigns in the DuckDB fixture -- made this read `Project · 6 rule(s)` for one
    single rule. Beside it, on the same panel, `applied_rule_count` said `1`: two
    numbers contradicting each other, and nobody able to tell which one lied. The
    de-duplication happens in `_compose_cascade`, and this signature says so.
    """
    counts: dict[str, int] = {}
    unresolved = 0
    for rule_id in rule_ids:
        rule = rules_by_id.get(rule_id)
        if rule is None:
            unresolved += 1
            continue
        if rule.get("category") != category:
            continue
        kind = str(rule.get("scope_kind") or "")
        counts[kind] = counts.get(kind, 0) + 1
    levels = [
        {
            "kind": level["kind"],
            "label": level["label"],
            "covers": level["covers"],
            "rule_count": counts[level["kind"]],
        }
        for level in CASCADE_LEVELS
        if counts.get(level["kind"])
    ]
    return {
        "levels": levels,
        "unresolved_rule_count": unresolved,
        "unresolved_reason": LEVEL_UNRESOLVED_REASON if unresolved else None,
    }


def _read_project_rules(conn, project_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(_PROJECT_RULES_SQL, (project_id,))
        rows = cur.fetchall()
    return [
        {
            "id": str(row[0]),
            "scope_kind": row[1],
            "scope_ref": row[2],
            "category": row[3],
            "status": row[4],
        }
        for row in rows
    ]


def _read_ladder_rows(project_id: str, connector: str, window: dict[str, Any]) -> list[dict]:
    """The mart slice of ONE connector over the window, or a typed refusal.

    `WarehouseUnavailable` is re-raised as `CostCascadeUnavailable` rather than
    swallowed: an unreachable relation must not render as a cascade with nothing
    in it, which is the same shape as a Project that has published no rule.
    """
    from core.warehouse import (  # noqa: PLC0415
        WarehouseUnavailable,
        query_fee_tax_ladder_daily,
    )

    try:
        rows = query_fee_tax_ladder_daily(project_id, window["start"], window["end"])
    except WarehouseUnavailable as exc:
        logger.warning("workbench_cost: warehouse_unavailable connector=%s: %s", connector, exc)
        raise CostCascadeUnavailable() from exc
    return [row for row in rows if str(row.get("connector") or "") == connector]


def _compose_measures(
    rows: list[dict], currency: str | None, cascade: dict[str, Any]
) -> list[dict[str, Any]]:
    """The four headline measures, each absent WITH ITS REASON when it is absent.

    `What we add` IS THE SUM OF THE STEPS ALREADY COMPOSED, and is not summed a
    second time from the rows: a headline that adds up its own phases and a
    cascade that adds up the same ones are two answers to one question, and the
    day one of them is fixed they disagree on screen.
    """
    net_media = sum((_micros(row.get("net_media_micros")) or 0) for row in rows)
    added: int | None = 0
    added_gap: str | None = None
    for step in cascade["steps"]:
        if step["micros"] is None:
            added = None
            added_gap = step["gap_code"]
        elif added is not None:
            added += step["micros"]

    total: int | None = 0
    total_gap: str | None = None
    for row in rows:
        row_total = _micros(row.get("total_ttc_micros"))
        if row_total is None or not row.get("is_ladder_complete", True):
            total = None
            total_gap = GAP_LADDER_INCOMPLETE
        elif total is not None:
            total += row_total

    if currency is None:
        # Several currencies in the slice: the mart's grain carries one per row and
        # adding across them would compose a number in no currency at all.
        return [
            _measure(
                MEASURE_NET_MEDIA, "Net media", gap_code=GAP_MIXED_CURRENCIES,
                reason="This window carries more than one currency; the amounts are not summable.",
            ),
            _measure(
                MEASURE_WHAT_WE_ADD, "What we add", gap_code=GAP_MIXED_CURRENCIES,
                reason="This window carries more than one currency; the amounts are not summable.",
            ),
            _measure(
                MEASURE_TOTAL, "Total", gap_code=GAP_MIXED_CURRENCIES,
                reason="This window carries more than one currency; the amounts are not summable.",
            ),
            _measure(
                MEASURE_UPLIFT, "Uplift", unit="percent", gap_code=GAP_MIXED_CURRENCIES,
                reason="This window carries more than one currency; the amounts are not summable.",
            ),
        ]

    uplift = _uplift_percent(added, net_media)
    return [
        _measure(MEASURE_NET_MEDIA, "Net media", micros=net_media, currency=currency),
        _measure(
            MEASURE_WHAT_WE_ADD,
            "What we add",
            micros=added,
            currency=currency,
            gap_code=added_gap,
            reason=(
                "At least one phase could not be evaluated on this window, so what is added "
                "is not known in full."
                if added_gap
                else None
            ),
        ),
        _measure(
            MEASURE_TOTAL,
            "Total",
            micros=total,
            currency=currency,
            gap_code=total_gap,
            reason=(
                "The ladder is incomplete on at least one row of this window, so no total is "
                "composed. The parts above may be read; the total may not."
                if total_gap
                else None
            ),
        ),
        _measure(
            MEASURE_UPLIFT,
            "Uplift",
            unit="percent",
            percent=uplift,
            gap_code=None if uplift is not None else (added_gap or GAP_NO_NET_MEDIA),
            reason=(
                None
                if uplift is not None
                else (
                    "What is added is not known in full, so the ratio it belongs to is not either."
                    if added_gap
                    else "There is no net media on this window to compare what is added with."
                )
            ),
        ),
    ]


def _compose_cascade(rows: list[dict], rules_by_id: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Five steps, each with its amount, its gaps and the levels that laid it.

    THE AMOUNTS ARE SUMMED OVER THE ROWS; THE RULES ARE NOT. A phase's amount is a
    real total over the window -- six rows of the fixture Project add up to a real
    six-row total. A rule, on the other hand, is the SAME rule on each of those
    rows: `applied_rule_ids` is a per-row provenance column, not a per-row rule.
    Adding the two the same way is what printed `Project · 6 rule(s)` beside an
    `applied_rule_count` of `1`.
    """
    seen: set[str] = set()
    applied_ids: list[str] = []
    for row in rows:
        raw = str(row.get("applied_rule_ids") or "")
        for part in raw.split("|"):
            if part and part not in seen:
                seen.add(part)
                applied_ids.append(part)

    steps: list[dict[str, Any]] = []
    for phase in CASCADE_PHASES:
        amount: int | None = 0
        gap_code: str | None = None
        for row in rows:
            value = _micros(row.get(phase["column"]))
            if value is None:
                amount = None
                gap_code = GAP_PHASE_NOT_EVALUATED
            elif amount is not None:
                amount += value
        levels = _levels_of_phase(phase["category"], applied_ids, rules_by_id)
        steps.append(
            {
                "key": phase["key"],
                "label": phase["label"],
                "category": phase["category"],
                "micros": amount,
                "gap_code": gap_code,
                "reason": (
                    "A rule routed to this phase could not be evaluated on this window, so "
                    "the phase has no amount. It is not a zero."
                    if gap_code
                    else None
                ),
                **levels,
            }
        )
    return {
        "steps": steps,
        "aggregation": CASCADE_AGGREGATION,
        "aggregation_reason": CASCADE_AGGREGATION_REASON,
        # Already distinct, and the same list the levels were counted from -- so
        # this number and the per-level counts can never contradict each other on
        # the panel that shows both.
        "applied_rule_count": len(applied_ids),
    }


def _compose_grain(conn, *, project_id: str, connector: str) -> dict[str, Any]:
    """Whose cascade this is -- and the sentence when it cannot be this one's.

    `fee_tax_ladder_daily` is grained on (project, date, connector, breakdown,
    currency) and carries no Datastream discriminator, so two live Datastreams on
    one connector make every row of that slice unattributable. The verdict is the
    shared one, fails closed, and the count is read only to write the sentence.
    """
    from core.datastream_sample_api import datastream_materialization_is_ambiguous  # noqa: PLC0415

    ambiguous = datastream_materialization_is_ambiguous(
        conn, data_project_id=project_id, connector=connector
    )
    grain: dict[str, Any] = {
        "connector": connector,
        "datastream_grain": False,
        "ambiguous": bool(ambiguous),
        "reason_code": GRAIN_AMBIGUOUS if ambiguous else None,
        "datastreams_on_connector": None,
        "reason": (
            "The cascade is computed per connector, not per Datastream: the mart carries no "
            "Datastream discriminator."
        ),
    }
    if not ambiguous:
        return grain
    with conn.cursor() as cur:
        cur.execute(_DATASTREAMS_ON_CONNECTOR_SQL, (project_id, connector))
        count = int(cur.fetchone()[0])
    grain["datastreams_on_connector"] = count
    grain["reason"] = (
        f"{count} Datastreams of this Project collect from {connector}, and the cascade "
        "carries no Datastream discriminator, so this slice cannot be attributed to this "
        "Datastream alone."
    )
    return grain


def _governance_owner_reference(project_id: str) -> dict[str, Any]:
    """The ONE gesture this tab offers: open the governed rule set.

    Semantic, never a composed address -- the console builds the href from the
    canonical navigation registry. Borrowed from `fee_tax_mcp.console_deep_link`
    so the console and the MCP name the same door.
    """
    from core.fee_tax_mcp import console_deep_link  # noqa: PLC0415

    return console_deep_link(project_id)["owner_reference"]


def read_cost_evidence(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    connector: str,
    today: date | None = None,
) -> dict[str, Any]:
    """The `Cost` tab's evidence. The capability decides alone whether to read.

    Order matters and is the acceptance: the capability state is read FIRST, in
    one statement, and an inactive capability returns with no measure, no cascade
    and no warehouse round trip at all.
    """
    state = read_capability_state(
        conn, project_id=project_id, capability_key=TAX_FEES_CAPABILITY_KEY
    )
    capability = {
        "key": TAX_FEES_CAPABILITY_KEY,
        "state": state,
        "active": capability_is_active(state),
    }
    levels = {"rendered": [dict(level) for level in CASCADE_LEVELS],
              "absent": [dict(level) for level in ABSENT_LEVELS]}

    if not capability["active"]:
        return {
            "state": STATE_CAPABILITY_INACTIVE,
            "capability": capability,
            "reason": (
                "Tax & fees is not active on this Project, so no cost cascade is computed "
                "and none is read."
            ),
            "window": None,
            "grain": None,
            "measures": None,
            "cascade": None,
            "refused_rules": None,
            "levels": levels,
            "governance_owner_reference": None,
        }

    rules = _read_project_rules(conn, project_id)
    rules_by_id = {rule["id"]: rule for rule in rules}
    window = _window(today or date.today())
    grain = _compose_grain(conn, project_id=project_id, connector=connector)
    rows = _read_ladder_rows(project_id, connector, window) if connector else []

    common: dict[str, Any] = {
        "capability": capability,
        "window": window,
        "grain": grain,
        "levels": levels,
        "refused_rules": _refused_rules(conn, project_id=project_id, datastream_id=datastream_id),
        "governance_owner_reference": _governance_owner_reference(project_id),
    }

    if not rows:
        empty_code = EMPTY_NO_RULE_PUBLISHED if not rules else EMPTY_NO_LADDER_ROW
        return {
            **common,
            "state": STATE_EMPTY,
            "empty_code": empty_code,
            "reason": (
                EMPTY_NO_RULE_PUBLISHED_MESSAGE
                if empty_code == EMPTY_NO_RULE_PUBLISHED
                else (
                    "No cascade row was composed for "
                    f"{connector or 'this Datastream'} on this window."
                )
            ),
            "owner": EMPTY_OWNER if empty_code == EMPTY_NO_RULE_PUBLISHED else None,
            "measures": None,
            "cascade": None,
        }

    currencies = sorted({str(row.get("currency") or "") for row in rows})
    currency = currencies[0] if len(currencies) == 1 else None
    cascade = _compose_cascade(rows, rules_by_id)
    return {
        **common,
        "state": STATE_AVAILABLE,
        "empty_code": None,
        "reason": None,
        "owner": None,
        "currency": currency,
        "currencies": currencies,
        "measures": _compose_measures(rows, currency, cascade),
        "cascade": cascade,
    }


def _refused_rules(conn, *, project_id: str, datastream_id: str) -> dict[str, Any]:
    """The rules examined and NOT applied, with their code and their reason.

    Read from the compiled capability projection, which is the one place that
    answers "does this rule resolve against this Datastream" -- a second matcher
    would be a second answer, and the two would diverge the day one is fixed.

    THE IDENTIFIERS HERE ARE `rule_key`s OF A GOVERNED RULE SET VERSION, which is
    not the store the mart reads. They are carried under their own name and never
    matched with the applied identifiers above.
    """
    from core.capability_proposals import read_datastream_capabilities  # noqa: PLC0415

    try:
        projection = read_datastream_capabilities(
            conn, project_id=project_id, datastream_id=datastream_id
        )
    except Exception as exc:  # noqa: BLE001 -- an absent projection is not a broken tab
        logger.warning("workbench_cost: capability_projection_unavailable: %s", exc)
        return {
            "rules": [],
            "state": "unavailable",
            "reason": "The compiled capability projection could not be read.",
        }

    for capability in projection.get("capabilities") or []:
        if capability.get("capability_key") != TAX_FEES_CAPABILITY_KEY:
            continue
        # `detected_support_selection` is an IMPACT BLOCK, not a top-level field.
        # Reading it one level too high returns `{}` on every Datastream, which is
        # the exact shape of "no rule was refused" -- a silent, permanent lie.
        support = (capability.get("impact") or {}).get("detected_support_selection") or {}
        if support.get("state") != "detected":
            return {
                "rules": [],
                "state": "unavailable",
                "reason": (
                    "No published fee/tax ladder has been matched against this Datastream, "
                    "so no rule has been examined and refused."
                ),
            }
        refused = [dict(rule) for rule in support.get("refused_rules") or []]
        return {
            "rules": refused,
            "state": "available",
            "reason": (
                None
                if refused
                else "Every rule of the published ladder resolves against this Datastream."
            ),
        }
    return {
        "rules": [],
        "state": "unavailable",
        "reason": (
            "No Tax & fees capability projection has been compiled for this Datastream, so "
            "no rule has been examined against it."
        ),
    }
