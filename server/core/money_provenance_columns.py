"""Which column of a reading is an AMOUNT, and what explains it -- story 58.7.

THE FIVE FIELDS THE EPIC PLAN NAMES DO NOT EXIST UNDER THOSE NAMES, AND THIS IS
THE ONE PLACE THE TRANSLATION IS WRITTEN. Epic 58 asks 58.7 for
``source_currency``, ``source_amount_micros``, ``fx_rate_applied``,
``fx_rate_date`` and ``reporting_currency``. Measured 2026-08-07, three
vocabularies already answer those five facts and none of them spells them that
way: ``dbt/macros/money_evidence.sql`` emits the warehouse block,
``core.money_derivation`` serialises the application one and ``core.fx_rate_sets``
the rate one. Adopting the plan's spelling would have created a FOURTH name for
the same column, so the plan designates five FACTS and this module serves the
names already in place (arbitrage 1, option b). The map, once:

    source_currency       -> native_currency      (staging: `<metric>_source_currency`)
    source_amount_micros  -> native_value         (staging: `<metric>_source_value`)
                             + native_unit, which says WHICH unit -- and no micros
                             exist in the warehouse at all: `native_unit` is
                             'decimal' on 63 of the 63 monetary rows
    fx_rate_applied       -> fx_rate
    fx_rate_date          -> fx_as_of_date
    reporting_currency    -> NOT SERVED, deliberately. See below.

NO REPORTING CURRENCY IS NAMED HERE, AND THAT IS A DECISION, NOT AN OMISSION
(arbitrage 8). Measured on the disposable cluster: ``app.project_preferences``
answers ``[('EUR','unconfirmed',16), (None,'unconfirmed',2)]`` -- 0 project of 18
has CONFIRMED a reporting currency -- and ``app.governance_rule_sets`` is empty,
so ``core.money_policy.try_resolve_money_policy`` returns ``None`` everywhere.
Printing `EUR` would present a column DEFAULT as a decision, which is the exact
defect migration 144 opens by naming. And there is a second reason that holds even
on the day a project confirms one: the relation this reading opens is a STAGING
model, which by [[fx-locus-read-not-staging]] carries the amount in its SOURCE
currency and converts nothing. The value on screen IS native. So the line says the
native currency, which is measured and true, and names the door that confirms the
other one.

WHAT MAY DESIGNATE AN AMOUNT: ONE AUTHORITY, AND IT IS NOT A NAME PATTERN. A
Concept version published with ``value_type = 'money'`` in the governed Semantic
Model -- the same predicate ``capability_compilers.MoneyCompiler.project_evidence``
reads, and story 48.3 wrote it there precisely because the previous classifier
(``currency_scope IS NOT NULL OR unit IS NOT NULL``) called most of the catalogue
money: ``unit`` is populated for sessions, impressions and seconds. Field names,
connector guesses and column suffixes are NOT classifiers. Measured 2026-08-07:
``select value_type, count(*) from app.semantic_concept_versions group by 1`` ->
``integer 6, decimal 3, string 3, date 1``, ZERO ``money``. So today this module
designates nothing, on every relation, and SAYS so with the name of what would
fill it. An empty key with its reason is the honest answer; a heuristic that found
`cost` anyway would be the invented one.

THE EXPLAINING COLUMNS ARE FOUND, NEVER ASSUMED. Once the authority has named an
amount, the columns that explain it are looked for BY NAME IN THE RELATION and the
association is published only if they are there -- ``dbt/macros/money_evidence.sql``
and the 8 staging models that join FX declare the shape (``<metric>_source_value``,
``<metric>_source_currency``, then the relation-wide ``fx_rate`` / ``fx_as_of_date``).
Measured: 12 of the 53 staging models carry a source currency at all and 8 of those
12 join a rate; the raw zone carries none -- ``raw_meta_ads_daily`` holds ``spend``
and ``cost_source_currency`` and no rate column whatsoever -- which is why the raw
zone earns its own word, ``fx_columns_absent_in_raw_zone``, instead of an
association nobody could check.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

# ONE spelling of a gap, not two. `money_derivation` owns these two words and the
# dbt macro emits the same two into `fact_daily_kpi.money_gap_code`; a copy here
# would be the third vocabulary this module exists to avoid.
from core.money_derivation import GAP_NO_CURRENCY, GAP_NO_RATE

#: The zones of the reading, in pipeline order -- the reader's own constants.
ZONE_COLLECTED = "collected"
ZONE_MAPPED = "mapped"

#: The authority that may say "this column is an amount", named so a payload that
#: designates nothing also says what would make it designate something.
MONEY_AUTHORITY = (
    "a published Semantic Concept version whose value_type is 'money' "
    "(app.semantic_concept_versions)"
)

#: Where a reporting currency would be confirmed. NAMED, never read: see the
#: module docstring, arbitrage 8.
REPORTING_CURRENCY_GATE = "the project's governed Money Policy"

#: Why a side designates no amount. Five causes, five words -- « vide » and
#: « cassé » are not the same sentence, and neither is « nothing declares it ».
NO_MONETARY_CONCEPT_DECLARED = "no_monetary_concept_declared"
NO_MONETARY_COLUMN_IN_RELATION = "no_monetary_column_in_relation"
FX_COLUMNS_ABSENT_IN_RAW_ZONE = "fx_columns_absent_in_raw_zone"
FX_COLUMNS_ABSENT_IN_RELATION = "fx_columns_absent_in_relation"
RELATION_NOT_READ = "relation_not_read"

#: Why ONE row of a designated column carries no usable rate. The first two are
#: `money_derivation`'s, imported rather than respelled. The third is this
#: reading's own and it is stated as such: a derivation's rate object always
#: carries its as-of date, so `money_derivation` has no word for a rate whose date
#: is missing -- a RELATION can carry exactly that, and the story refuses « a rate
#: without its date » outright.
GAP_NO_RATE_DATE = "fx_as_of_date_missing"

#: The sentence of each code. Written on the server, once, because the screen must
#: hold none of its own -- the rule `collected_mapped_reader` already holds.
_MESSAGES = {
    NO_MONETARY_CONCEPT_DECLARED: (
        "No column of this reading is an amount: nothing declares one. A column "
        "becomes an amount through " + MONEY_AUTHORITY + ", and this project has "
        "published none. Column names, units and connector conventions are not "
        "classifiers."
    ),
    NO_MONETARY_COLUMN_IN_RELATION: (
        "This relation carries no column that the governed Semantic Model declares "
        "to be an amount."
    ),
    FX_COLUMNS_ABSENT_IN_RAW_ZONE: (
        "The collected zone carries the amount exactly as the source reported it "
        "and no rate at all, so nothing here can say how it was converted. The "
        "conversion is evidenced one stage later, in the mapped reading."
    ),
    FX_COLUMNS_ABSENT_IN_RELATION: (
        "This relation carries an amount but no rate column, so no conversion "
        "evidence travels with its rows."
    ),
    RELATION_NOT_READ: (
        "This relation was not read, so no column of it is designated. The reading "
        "beside this key says why."
    ),
    GAP_NO_CURRENCY: (
        "This amount carries no currency, so what it counts is unknown and no rate "
        "could apply to it."
    ),
    GAP_NO_RATE: (
        "No rate covered this row, so the amount stayed in the currency the source "
        "reported and was not converted."
    ),
    GAP_NO_RATE_DATE: (
        "A rate travelled with this row and no quotation date did, so which day it "
        "was quoted on cannot be said."
    ),
}

#: The relation-wide provenance columns, exactly as the 8 FX-joining staging
#: models emit them. Names, not patterns: a column called `rate` somewhere else is
#: not this one.
_RATE_COLUMN = "fx_rate"
_RATE_DATE_COLUMN = "fx_as_of_date"

#: The per-metric suffixes the same models declare beside the amount.
_CURRENCY_SUFFIX = "_source_currency"
_NATIVE_VALUE_SUFFIX = "_source_value"


#: The HEADING of each code, beside its sentence and for the same reason: the screen
#: holds none of its own. Two of these codes mean the opposite of "no amount here" --
#: `fx_columns_absent_in_raw_zone` and `fx_columns_absent_in_relation` say the amount
#: IS there and its rate is not -- so a single hard-coded title contradicted the
#: sentence printed under it for half the vocabulary. One authority, both lines.
_TITLES = {
    NO_MONETARY_CONCEPT_DECLARED: "No monetary column in this reading",
    NO_MONETARY_COLUMN_IN_RELATION: "No monetary column in this reading",
    FX_COLUMNS_ABSENT_IN_RAW_ZONE: "Amounts without conversion evidence",
    FX_COLUMNS_ABSENT_IN_RELATION: "Amounts without conversion evidence",
    RELATION_NOT_READ: "Money provenance was not read",
}


def message_for(code: str | None) -> str | None:
    """The sentence of a code, or `None`."""
    if not code:
        return None
    return _MESSAGES.get(code)


def title_for(code: str | None) -> str | None:
    """The heading of a code, or `None`. Declared beside `message_for`."""
    if not code:
        return None
    return _TITLES.get(code)


def monetary_concept_names(conn, *, project_id: str) -> list[str]:
    """The names the governed Semantic Model declares to be amounts.

    THE SAME PREDICATE AS ``capability_compilers.MoneyCompiler.project_evidence``,
    and deliberately no wider: published concept, ``kind = 'metric'``,
    ``value_type = 'money'``, scoped to this project or global. One statement, and
    it is only paid when a day is actually opened.
    """
    with conn.cursor() as cur:
        cur.execute(
            """SELECT c.name
                 FROM app.semantic_concepts c
                 JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
                WHERE c.lifecycle_status = 'published'
                  AND (c.project_id = %s OR c.project_id IS NULL)
                  AND v.kind = 'metric'
                  AND v.value_type = 'money'
                ORDER BY c.name""",
            (project_id,),
        )
        return [str(row[0]) for row in cur.fetchall()]


def designate(
    *,
    columns: Sequence[str] | None,
    monetary_names: Iterable[str] | None,
    zone: str,
    readable: bool = True,
) -> dict[str, Any]:
    """Which columns of one relation are amounts, and what explains each.

    Present ALWAYS, empty with its reason when nothing is designated: a key that
    disappears is a key a screen cannot tell from a route that never sent it, and
    those two are the two different sentences the story asks for.
    """
    declared = sorted({str(name) for name in (monetary_names or []) if name})
    present = [str(column) for column in (columns or [])]
    envelope: dict[str, Any] = {
        "zone": zone,
        "columns": [],
        "reason": None,
        "message": None,
        # Declared beside the sentence, and for the same reason: the screen holds
        # no wording of its own. Half this vocabulary means "the amount IS here and
        # its rate is not", which a single fixed heading contradicted.
        "title": None,
        "authority": MONEY_AUTHORITY,
        # STATED on every payload, whether or not an amount was designated: a
        # reading that shows native amounts and says nothing about the reporting
        # currency invites the reader to assume one.
        "reporting_currency": None,
        "reporting_currency_reason": (
            "No reporting currency is named by this reading. These amounts are in "
            "the currency the source reported."
        ),
        "reporting_currency_gate": REPORTING_CURRENCY_GATE,
    }

    def _refuse(code: str) -> dict[str, Any]:
        envelope["reason"] = code
        envelope["message"] = message_for(code)
        envelope["title"] = title_for(code)
        return envelope

    # THE AUTHORITY IS ASKED FIRST, AND THE ORDER IS THE ANSWER. "Nothing declares
    # an amount" is a fact about the PROJECT and stays true whatever relation is
    # being read, so it is the sentence that names the repair. "This relation was
    # not read" is a fact about one relation, and it only earns the answer when an
    # amount really is declared -- otherwise it would echo the reason the side
    # beside it already carries and hide the one nobody knows.
    if not declared:
        return _refuse(NO_MONETARY_CONCEPT_DECLARED)
    if not readable:
        return _refuse(RELATION_NOT_READ)

    designated = [column for column in present if column in declared]
    if not designated:
        return _refuse(NO_MONETARY_COLUMN_IN_RELATION)

    if _RATE_COLUMN not in present:
        # TWO CAUSES, TWO WORDS. The raw zone has no rate column BY CONSTRUCTION --
        # the conversion is evidenced one stage later -- while a staging model that
        # carries an amount and no rate is a fact about that model. Measured: 4 of
        # the 12 staging models carrying a source currency join no rate at all.
        return _refuse(
            FX_COLUMNS_ABSENT_IN_RAW_ZONE
            if zone == ZONE_COLLECTED
            else FX_COLUMNS_ABSENT_IN_RELATION
        )

    envelope["columns"] = [
        {
            "column": column,
            # The ROLE is the mart's vocabulary; the value is the relation's own
            # spelling. A reader never has to guess that `cost_source_currency` is
            # what `native_currency` means one model downstream.
            "native_currency": _column_if_present(column + _CURRENCY_SUFFIX, present),
            "native_value": _column_if_present(column + _NATIVE_VALUE_SUFFIX, present),
            "fx_rate": _RATE_COLUMN,
            "fx_as_of_date": (
                _RATE_DATE_COLUMN if _RATE_DATE_COLUMN in present else None
            ),
        }
        for column in designated
    ]
    return envelope


def _column_if_present(name: str, present: Sequence[str]) -> str | None:
    return name if name in present else None


def native_currency_column(amount_column: str, present: Sequence[str]) -> str | None:
    """The column that says which currency *amount_column* is denominated in.

    THE SAME SPELLING :func:`designate` USES, exported because a second reader
    needed it and a second reader must not respell it. Story 62.1's MMM extract
    asks a landing for the distinct currencies of one amount before it will put
    that amount in a file, and a landing of the collected zone carries the
    currency and NO rate at all -- which is exactly the case :func:`designate`
    answers with `fx_columns_absent_in_raw_zone`, an envelope whose `columns` is
    empty. So the extract cannot read the currency off that envelope, and writing
    `column + "_source_currency"` at its own call site would have made
    `_CURRENCY_SUFFIX` a convention held in two files.

    "The amount has no currency here" is `None`, and the caller decides what that
    means: for the extract it is `UNKNOWN_CURRENCY_GAP` and the file is refused.
    """
    if not amount_column:
        return None
    return _column_if_present(str(amount_column) + _CURRENCY_SUFFIX, present)


def inherited_classifications(
    designations: Iterable[Mapping[str, Any]],
    classifications: Mapping[str, str],
) -> dict[str, str]:
    """The provenance of a shown amount is shown; of a masked amount, masked.

    NOT AN EXEMPTION BY NAME -- A DERIVATION OF THE EXISTING POLICY. Masking is a
    refusal by default: a value appears only when its column is classified `none`,
    and a classification comes from the MAPPING. The FX columns are emitted by a
    dbt macro and named by no mapping, so every one of them is unclassified and
    therefore masked -- which would have printed `[MASKED] · [MASKED] · [MASKED]`
    under an amount the same policy had just decided to show. So each provenance
    column inherits the classification of the amount it explains: shown when the
    amount is shown, masked when it is masked, and never on its own authority.
    """
    inherited = dict(classifications)
    for designation in designations:
        amount = str(designation.get("column") or "")
        sensitivity = classifications.get(amount)
        if not amount or sensitivity is None:
            continue
        for role in ("native_currency", "native_value", "fx_rate", "fx_as_of_date"):
            column = designation.get(role)
            if isinstance(column, str) and column and column not in inherited:
                inherited[column] = sensitivity
    return inherited


def row_provenance(
    *, designation: Mapping[str, Any], rows: Sequence[Mapping[str, Any]] | None
) -> list[dict[str, Any]]:
    """Per row, per designated column: the three values, or the gap that names why.

    Read off the row the payload ALREADY carries, masking included, so what the
    screen draws under a cell and what it draws in it come from one and the same
    value. The gap is derived by the very expression ``money_evidence_present()``
    uses in the warehouse -- currency first, then rate -- so a row read here and
    the same row read at the mart cannot answer two different words.
    """
    designated = list(designation.get("columns") or [])
    if not designated or not rows:
        return []
    provenance: list[dict[str, Any]] = []
    for row in rows:
        entry: dict[str, Any] = {}
        for column in designated:
            currency = _value(row, column.get("native_currency"))
            rate = _value(row, column.get("fx_rate"))
            as_of = _value(row, column.get("fx_as_of_date"))
            gap = None
            if currency is None:
                gap = GAP_NO_CURRENCY
            elif rate is None:
                gap = GAP_NO_RATE
            elif as_of is None:
                gap = GAP_NO_RATE_DATE
            entry[str(column["column"])] = {
                "native_currency": currency,
                "native_value": _value(row, column.get("native_value")),
                "fx_rate": rate,
                "fx_as_of_date": as_of,
                "money_gap_code": gap,
                "money_gap_message": message_for(gap),
            }
        provenance.append(entry)
    return provenance


def _value(row: Mapping[str, Any], column: Any) -> Any:
    """The row's value for a role's column, as a string or `None`.

    An empty string is `None`: a currency column that landed blank is a currency
    nobody reported, and rendering `'' · 0.92 · 2026-07-01` would show a rate
    applied to nothing.
    """
    if not isinstance(column, str) or not column:
        return None
    value = row.get(column)
    if value is None:
        return None
    text = str(value)
    return text if text.strip() else None
