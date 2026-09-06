"""What an ACTIVE Project capability does TO THE READING of one day -- lot B3.

THE RULE THIS MODULE EXISTS FOR, in the words that ratified it (amendment 11 of
the second-pass review, `docs/product-architecture/datastream-workbench-and-wizard.md`):
"Une capacite activee AJOUTE UN ONGLET, OU COLORE LE CHAMP DANS L'APERCU -- jamais
une liste de modules. [...] Un inventaire de modules n'est pas une fonctionnalite :
l'effet l'est." Amendment 2 of the 2026-08-05 review opened the tab half --
`tax_fees` adds `Cost`, `placement_mapping` adds `Placements`. This is the other
half, for the three capabilities that deserve no tab of their own because what
they do happens INSIDE a row: `currency_fx`, `country`, `reporting_timezone`.

SO NOTHING HERE EMITS A ROW SAYING "Currency & FX - Ready". What travels is the
EFFECT and the FIELDS it lands on: which column of this reading is an amount and
what converted it, which column of this reading is a country, and -- for the one
capability that colours nothing -- the day-boundary risk that qualifies the whole
reading. A screen given this block draws on the data; it has nothing to enumerate.

AND OFF IS ABSENT, NOT GREY. `capability_is_active` is the same threshold that
opens the `Cost` tab (story 58.6) and shows the country split of the day grid
(story 58.5): `ready` or `degraded`, read from `app.project_capabilities.state`
and never derived. A capability in any other state produces NO entry at all --
"Eteinte, la capacite n'apparait nulle part -- ni onglet, ni panneau, ni colonne"
-- and the money provenance of both sides is replaced by the refusal that names
which door is shut, so an API reader is told the same thing the screen shows.

MEASURED, AND IT MATTERS FOR READING WHAT THIS DOES. ``select capability_key,
state, count(*) from app.project_capabilities group by 1, 2`` on 2026-08-12
answers SIX rows over 31 projects and not one of them is `ready` or `degraded`:
`competitors`, `country`, `placement_mapping` and `tax_fees` are `disabled` on
31/31; `currency_fx` and `reporting_timezone` are `draft` on 31/31. So NOTHING
below lights up on a live project today -- that is the honest render, and the
branch that fills it is proved by fixtures and by the sandbox rather than by a
screenshot nobody can take.

WHAT IT NEVER DOES. It does not turn a capability on -- reading a state cannot --
it does not classify a column by its spelling, and it does not re-align a day.
`reporting_timezone` SIGNALS a boundary and corrects nothing: at DATE grain there
is no hour to re-slice, which is a ratified rule of this product and the reason
that entry carries a sentence and not a value.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from core.project_capability_states import capability_is_active

#: The three capabilities whose whole effect happens inside a reading. The other
#: four declared keys open a tab instead (`tax_fees` -> `Cost`,
#: `placement_mapping` -> `Placements`) or have no reading-level effect at all
#: (`competitors` specialises a conformed dimension, which is a governance
#: surface; `analytics_alignment` ADDS COLUMNS to an aggregation, story 70.3,
#: which is a different effect from re-expressing a measure inside a day's
#: reading), and adding them here would put a second answer beside the tab.
CURRENCY_FX_CAPABILITY_KEY = "currency_fx"
COUNTRY_CAPABILITY_KEY = "country"
REPORTING_TIMEZONE_CAPABILITY_KEY = "reporting_timezone"

READING_CAPABILITY_KEYS: tuple[str, ...] = (
    CURRENCY_FX_CAPABILITY_KEY,
    COUNTRY_CAPABILITY_KEY,
    REPORTING_TIMEZONE_CAPABILITY_KEY,
)

#: WHAT the capability does to the reading, named so the screen branches on a
#: word rather than on the key. Two capabilities land on a FIELD and one lands on
#: the DAY; a screen that had to infer which from the key would be holding the
#: product's arbitrage in a switch statement.
EFFECT_MONEY_LINE = "money_line"
EFFECT_FIELD_MARKED = "field_marked"
EFFECT_DAY_BOUNDARY_SIGNAL = "day_boundary_signal"

#: Why a capability that IS on has no field to land on. Not a failure -- a state
#: of the mapping, and each one names the gesture that repairs it.
NO_MONETARY_COLUMN = "no_monetary_column_in_reading"
NO_COUNTRY_FIELD_IN_MAPPING = "no_country_field_in_mapping"
NO_COUNTRY_COLUMN_IN_RELATION = "no_country_column_in_relation"
NO_TIMEZONE_POLICY = "no_reporting_timezone_policy"

#: Why the money provenance of a side says nothing at all: the capability that
#: owns it is off. DISTINCT from `no_monetary_concept_declared`, which is a fact
#: about the Semantic Model and only worth saying once somebody turned the
#: capability on -- reporting it while the switch is off would send a person to
#: publish a Concept for a projection nobody asked for.
MONEY_CAPABILITY_NOT_ACTIVE = "currency_fx_not_active"

_TITLES = {
    CURRENCY_FX_CAPABILITY_KEY: "Currency & FX",
    COUNTRY_CAPABILITY_KEY: "Country",
    REPORTING_TIMEZONE_CAPABILITY_KEY: "Reporting timezone",
}

#: Every sentence a screen shows about these three, written here because the
#: screen holds none of its own -- the rule `collected_mapped_reader` and
#: `money_provenance_columns` already hold for their own refusals.
_MESSAGES = {
    CURRENCY_FX_CAPABILITY_KEY: (
        "Each amount below carries the currency the source reported, the rate that "
        "was applied and the day that rate was quoted. The figures are the "
        "relation's own and nothing is converted here."
    ),
    NO_MONETARY_COLUMN: (
        "This capability is on and no column of this reading is an amount, so "
        "nothing here carries a currency or a rate. The reading beside each side "
        "says which of the causes it is."
    ),
    COUNTRY_CAPABILITY_KEY: (
        "The field marked below is the one the active mapping binds to the "
        "canonical country dimension, so a row of this day can be read by country."
    ),
    NO_COUNTRY_FIELD_IN_MAPPING: (
        "This capability is on and no field of the active mapping is bound to the "
        "canonical country dimension, so no column of this reading is a country. "
        "Bind one on Mapping and it is marked here."
    ),
    NO_COUNTRY_COLUMN_IN_RELATION: (
        "The active mapping binds a country field and neither relation of this day "
        "carries its column, so there is nothing to mark. The rows this flux "
        "collects do not hold the field the mapping names."
    ),
    NO_TIMEZONE_POLICY: (
        "This capability is on and no Reporting Timezone Policy is confirmed for "
        "this Project, so the day drawn below cannot be compared with a Project "
        "boundary. Confirm one in Project Settings and the comparison appears here."
    ),
}

#: The one sentence that must never be mistaken for a correction. Held apart from
#: the map above because it is a RULE of this product and not a wording choice:
#: the module signals a boundary and re-aligns nothing.
_TIMEZONE_SIGNAL = (
    "The rows below are the day the SOURCE drew. This Project reports on {zone}, and "
    "where the source's boundary differs some of these rows belong to the "
    "neighbouring day at the Project's. The offset is signalled and never "
    "corrected: at DATE grain there is no hour to re-slice, so not one value below "
    "is moved."
)

#: The whole money-provenance shape, refusing because the capability is off. NEVER
#: a missing key: a screen that cannot find `money_provenance` and one that found
#: it refusing are two different sentences, and story 58.7 already asks the screen
#: to say both.
_MONEY_OFF_MESSAGE = (
    "Currency & FX is not active on this Project, so no amount of this reading "
    "carries a rate, a rate date or a currency. Nothing is hidden: nothing was "
    "computed."
)


def message_for(reason: str | None) -> str | None:
    """The sentence of a reason of this module, or `None`."""
    if not reason:
        return None
    return _MESSAGES.get(reason)


def money_provenance_off(zone: str, state: str) -> dict[str, Any]:
    """The money provenance of one side when `currency_fx` is not active.

    Same keys as `money_provenance_columns.designate`, so no caller has to tell a
    refusal apart from an answer by counting keys.
    """
    return {
        "zone": zone,
        "columns": [],
        "reason": MONEY_CAPABILITY_NOT_ACTIVE,
        "message": _MONEY_OFF_MESSAGE,
        "title": None,
        "authority": None,
        "capability_key": CURRENCY_FX_CAPABILITY_KEY,
        "capability_state": state,
        "reporting_currency": None,
        "reporting_currency_reason": None,
        "reporting_currency_gate": None,
        "rows": [],
    }


def _entry(key: str, state: str, effect: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "key": key,
        "state": state,
        # `degraded` SHOWS the effect and says it is degraded: hiding evidence
        # already collected is worse than showing it diminished, which is the rule
        # `reports.py` and the country split of story 58.5 already hold.
        "degraded": state == "degraded",
        "effect": effect,
        "title": _TITLES[key],
        "message": None,
        "reason": None,
        "fields": {"collected": [], "mapped": []},
    }
    entry.update(extra)
    return entry


def _designated_money_columns(side: Mapping[str, Any] | None) -> list[str]:
    """The columns the SERVER designated as amounts on one side, in its order.

    Read off the provenance the route already computed. Nothing here decides what
    an amount is: `money_provenance_columns` owns that, and story 48.3 removed the
    spelling heuristic from the server precisely so no second one could grow.
    """
    if not isinstance(side, Mapping):
        return []
    provenance = side.get("money_provenance")
    if not isinstance(provenance, Mapping):
        return []
    return [
        str(column["column"])
        for column in (provenance.get("columns") or [])
        if isinstance(column, Mapping) and column.get("column")
    ]


def _country_fields(
    columns: Sequence[Mapping[str, Any]] | None,
) -> tuple[list[str], list[str]]:
    """`(source names, target names)` of the fields bound to the country dimension.

    THE MAPPING SAYS IT, NOT THE SPELLING. A field is a country here because its
    target is the canonical country dimension -- the same identity
    `fact_daily_kpi.breakdown_dimension` carries and `geographic_semantics` groups
    on -- and for no other reason. A column called `pays`, `market` or `geo` that
    the mapping binds elsewhere is not one, and a column called `country` that the
    mapping does not bind is not one either.
    """
    from core.country_vocabulary import CANONICAL_COUNTRY_DIMENSION  # noqa: PLC0415

    sources: list[str] = []
    targets: list[str] = []
    for field in columns or []:
        if not isinstance(field, Mapping):
            continue
        target = field.get("target_field")
        if not target or str(target) != CANONICAL_COUNTRY_DIMENSION:
            continue
        source = field.get("source_field")
        if source and str(source) not in sources:
            sources.append(str(source))
        if str(target) not in targets:
            targets.append(str(target))
    return sources, targets


def _present(names: Sequence[str], side: Mapping[str, Any] | None) -> list[str]:
    """Those of `names` the side really carries. A name a relation does not hold
    is not marked: a mark over a column that is not on screen marks nothing."""
    if not isinstance(side, Mapping):
        return []
    carried = {str(column) for column in (side.get("columns") or [])}
    return [name for name in names if name in carried]


def _currency_fx_entry(state: str, reading: Mapping[str, Any]) -> dict[str, Any]:
    collected = _designated_money_columns(reading.get("collected"))
    mapped = _designated_money_columns(reading.get("mapped"))
    if not collected and not mapped:
        return _entry(
            CURRENCY_FX_CAPABILITY_KEY,
            state,
            EFFECT_MONEY_LINE,
            reason=NO_MONETARY_COLUMN,
            message=message_for(NO_MONETARY_COLUMN),
        )
    return _entry(
        CURRENCY_FX_CAPABILITY_KEY,
        state,
        EFFECT_MONEY_LINE,
        message=_MESSAGES[CURRENCY_FX_CAPABILITY_KEY],
        fields={"collected": collected, "mapped": mapped},
    )


def _country_entry(
    state: str, reading: Mapping[str, Any], columns: Sequence[Mapping[str, Any]] | None
) -> dict[str, Any]:
    sources, targets = _country_fields(columns)
    if not sources and not targets:
        return _entry(
            COUNTRY_CAPABILITY_KEY,
            state,
            EFFECT_FIELD_MARKED,
            reason=NO_COUNTRY_FIELD_IN_MAPPING,
            message=message_for(NO_COUNTRY_FIELD_IN_MAPPING),
        )
    collected = _present(sources, reading.get("collected"))
    mapped = _present(targets, reading.get("mapped"))
    if not collected and not mapped:
        return _entry(
            COUNTRY_CAPABILITY_KEY,
            state,
            EFFECT_FIELD_MARKED,
            reason=NO_COUNTRY_COLUMN_IN_RELATION,
            message=message_for(NO_COUNTRY_COLUMN_IN_RELATION),
        )
    return _entry(
        COUNTRY_CAPABILITY_KEY,
        state,
        EFFECT_FIELD_MARKED,
        message=_MESSAGES[COUNTRY_CAPABILITY_KEY],
        fields={"collected": collected, "mapped": mapped},
    )


def _reporting_timezone_entry(conn, *, project_id: str, state: str) -> dict[str, Any]:
    """The day-boundary signal, and it moves nothing.

    The Policy is read ONLY on this branch -- the capability is `draft` on every
    project of the estate, so the statement is paid by nobody today and by one
    read on the day somebody turns it on.
    """
    from core.money_policy import try_resolve_timezone_policy  # noqa: PLC0415

    policy = None
    if conn is not None:
        policy = try_resolve_timezone_policy(conn, project_id=project_id)
    if policy is None:
        return _entry(
            REPORTING_TIMEZONE_CAPABILITY_KEY,
            state,
            EFFECT_DAY_BOUNDARY_SIGNAL,
            reason=NO_TIMEZONE_POLICY,
            message=message_for(NO_TIMEZONE_POLICY),
        )
    return _entry(
        REPORTING_TIMEZONE_CAPABILITY_KEY,
        state,
        EFFECT_DAY_BOUNDARY_SIGNAL,
        message=_TIMEZONE_SIGNAL.format(zone=policy.reporting_timezone),
        project_reporting_timezone=policy.reporting_timezone,
    )


def _carry_money_onto_the_pairing(reading: dict[str, Any]) -> None:
    """The line under the amount, ON THE PAIRED ROW too -- amendment 11 and 12 met.

    THE SAME VALUES, READ AT THE SAME INDEX. The pairing publishes which row of
    each side a paired row came from (`collected_index` / `mapped_index`), and the
    provenance is a list parallel to those rows, so the line under a paired cell
    and the line under the same cell of the single reading can never be two
    different rates. Nothing is recomputed and nothing is matched by name.

    A cell of an unpaired row carries the provenance of the side it HAS and
    `None` for the side it does not: a rate shown under an absent value would be
    evidence about a row that is not there.
    """
    pairing = reading.get("pairing")
    if not isinstance(pairing, dict) or not pairing.get("available"):
        return
    provenance = {
        zone: (reading.get(zone) or {}).get("money_provenance") or {}
        for zone in ("collected", "mapped")
    }
    rows = {
        zone: list(provenance[zone].get("rows") or []) for zone in ("collected", "mapped")
    }
    designated = {
        zone: {column["column"] for column in (provenance[zone].get("columns") or [])}
        for zone in ("collected", "mapped")
    }
    if not designated["collected"] and not designated["mapped"]:
        return

    def _values(zone: str, index: Any, column: str) -> Any:
        if column not in designated[zone] or not isinstance(index, int):
            return None
        if index < 0 or index >= len(rows[zone]):
            return None
        entry = rows[zone][index]
        return entry.get(column) if isinstance(entry, Mapping) else None

    for row in pairing.get("rows") or []:
        for cell in row.get("cells") or []:
            cell["raw_money"] = _values(
                "collected", row.get("collected_index"), str(cell["source_field"])
            )
            cell["mapped_money"] = _values(
                "mapped", row.get("mapped_index"), str(cell["target_field"])
            )


def project_onto_reading(
    conn,
    *,
    project_id: str,
    reading: dict[str, Any] | None,
    columns: Sequence[Mapping[str, Any]] | None,
    states: Mapping[str, str],
) -> dict[str, Any] | None:
    """Put every ACTIVE capability's effect on the reading, and nothing else on it.

    `states` is the one plural read of `app.project_capabilities` the route
    already paid for. An inactive key produces no entry -- the screen therefore
    has nothing to enumerate and cannot hold a column open for it -- and
    `currency_fx` being inactive additionally replaces the money provenance of
    both sides, so an amount is never annotated by a capability nobody turned on.
    """
    if reading is None:
        return None

    currency_state = str(states.get(CURRENCY_FX_CAPABILITY_KEY) or "")
    entries: list[dict[str, Any]] = []

    if capability_is_active(currency_state):
        entries.append(_currency_fx_entry(currency_state, reading))
        _carry_money_onto_the_pairing(reading)
    else:
        for zone in ("collected", "mapped"):
            side = reading.get(zone)
            if isinstance(side, dict):
                side["money_provenance"] = money_provenance_off(zone, currency_state)

    country_state = str(states.get(COUNTRY_CAPABILITY_KEY) or "")
    if capability_is_active(country_state):
        entries.append(_country_entry(country_state, reading, columns))

    timezone_state = str(states.get(REPORTING_TIMEZONE_CAPABILITY_KEY) or "")
    if capability_is_active(timezone_state):
        entries.append(
            _reporting_timezone_entry(conn, project_id=project_id, state=timezone_state)
        )

    # ALWAYS PRESENT, EMPTY WHEN NOTHING IS ON. An absent key and an empty list are
    # two different sentences -- "this route does not answer that question" and
    # "no capability of this Project touches this reading" -- and only the second
    # is a measurement.
    reading["capabilities"] = entries
    return reading
