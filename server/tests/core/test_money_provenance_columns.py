"""What may call a column an amount, and what dates a rate -- story 58.7.

TWO THINGS ARE HELD HERE AND THEY ARE BOTH ABOUT BELIEVING THE WRONG SOURCE.

ONE: only the governed Semantic Model designates an amount. Story 48.3 already
paid for the other answer -- ``currency_scope IS NOT NULL OR unit IS NOT NULL``
classified most of the catalogue as money, because ``unit`` is populated for
sessions, impressions and seconds. A column called `cost` is not an amount because
it is called `cost`; it is an amount when a published Concept version says
``value_type = 'money'``. Measured 2026-08-07 on the disposable cluster:
``select value_type, count(*) from app.semantic_concept_versions group by 1`` ->
``integer 6, decimal 3, string 3, date 1``, and ZERO ``money``. So the honest
answer today is an empty designation with its reason, and these tests are what
stop a heuristic from being added later to make the screen look alive.

TWO: the date printed under an amount has to be the date the rate was QUOTED. The
seed carries two dates per rate and the staging models read the wrong one:

    dbt/seeds/fx_rates.csv
    from_currency,to_currency,rate,rate_date,rate_policy,valid_from,valid_to
    USD,EUR,0.92,2026-07-01,static_dev_rate,2020-01-01,2099-12-31

``valid_from`` is where the validity WINDOW opens; ``rate_date`` is when the rate
was quoted. Publishing the first under the name of the second is why
``fx_as_of_date`` was ``2020-01-01`` on 43 of the 43 converted rows of the mart --
a constant, wearing the clothes of a measurement. Printing it under every amount
would have been exactly the invented provenance this story exists to prevent.

THE PIN BELOW IS THE MUTATION PROOF, AND IT RUNS ON A FRESH CHECKOUT WITH NO
DATABASE AT ALL. Put ``fx.valid_from`` back in any one of the eight converting
staging models and it reddens by name. The warehouse half -- that the value really
lands in ``fact_daily_kpi`` -- is ``dbt/tests/test_fx_rate_date_is_the_quotation
_date.sql``, over a versioned seed, so it too holds without a hand-landed fixture.

AND THE TWO ROLES ARE NOT SWAPPED. ``valid_from``/``valid_to`` CHOOSE the rate
(``raw.date BETWEEN valid_from AND valid_to``); ``rate_date`` DATES it. Confusing
them would break the choice, so the join key is pinned here too.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.money_provenance_columns import (
    FX_COLUMNS_ABSENT_IN_RAW_ZONE,
    FX_COLUMNS_ABSENT_IN_RELATION,
    GAP_NO_CURRENCY,
    GAP_NO_RATE,
    GAP_NO_RATE_DATE,
    MONEY_AUTHORITY,
    NO_MONETARY_COLUMN_IN_RELATION,
    NO_MONETARY_CONCEPT_DECLARED,
    RELATION_NOT_READ,
    ZONE_COLLECTED,
    ZONE_MAPPED,
    designate,
    monetary_concept_names,
    row_provenance,
)

_ROOT = Path(__file__).resolve().parents[3]
_MODULES = _ROOT / "server" / "modules"

#: The columns `stg_meta_ads_daily` really emits, in its own order.
_MAPPED_COLUMNS = [
    "date", "campaign_id", "cost_source_value", "cost_source_currency", "cost",
    "fx_rate", "fx_as_of_date", "fx_source", "fx_tier", "impressions", "pull_id",
]


class _Cursor:
    def __init__(self, state: dict) -> None:
        self._state = state

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None) -> None:
        self._state["sql"] = " ".join(str(query).split())
        self._state["params"] = params

    def fetchall(self):
        return self._state["rows"]


class _Conn:
    def __init__(self, rows) -> None:
        self.state = {"rows": list(rows), "sql": None, "params": None}

    def cursor(self, *a, **k):
        return _Cursor(self.state)


# ---------------------------------------------------------------------------
# The authority, and nothing beside it.
# ---------------------------------------------------------------------------


def test_the_designator_asks_the_governed_semantic_model_and_only_it() -> None:
    conn = _Conn([("cost",), ("revenue",)])
    assert monetary_concept_names(conn, project_id="proj_EXAMPLE") == ["cost", "revenue"]

    sql = conn.state["sql"]
    assert "app.semantic_concept_versions" in sql
    assert "v.value_type = 'money'" in sql
    assert "v.kind = 'metric'" in sql
    assert "c.lifecycle_status = 'published'" in sql
    # AC8 by name: none of these may appear, because each of them is a classifier
    # story 48.3 removed for calling most of the catalogue money.
    for forbidden in ("unit", "currency_scope", "connector", "ILIKE"):
        assert forbidden not in sql, forbidden
    assert conn.state["params"] == ("proj_EXAMPLE",)


def test_with_no_money_concept_nothing_is_designated_whatever_the_columns_are_called(
    ) -> None:
    """The state of the estate, and the one a name heuristic would have hidden.

    `cost` is right there in the column list and it stays undesignated, because
    the authority declares nothing.
    """
    out = designate(columns=_MAPPED_COLUMNS, monetary_names=[], zone=ZONE_MAPPED)
    assert out["columns"] == []
    assert out["reason"] == NO_MONETARY_CONCEPT_DECLARED
    assert MONEY_AUTHORITY in out["message"]
    # And it names what would fill it, so nobody has to go looking.
    assert "value_type" in out["message"]


def test_a_declared_amount_is_associated_with_what_explains_it() -> None:
    out = designate(columns=_MAPPED_COLUMNS, monetary_names=["cost"], zone=ZONE_MAPPED)
    assert out["reason"] is None
    assert out["columns"] == [
        {
            "column": "cost",
            "native_currency": "cost_source_currency",
            "native_value": "cost_source_value",
            "fx_rate": "fx_rate",
            "fx_as_of_date": "fx_as_of_date",
        }
    ]


def test_a_declared_name_no_column_answers_to_is_not_invented() -> None:
    out = designate(columns=_MAPPED_COLUMNS, monetary_names=["revenue"], zone=ZONE_MAPPED)
    assert out["columns"] == []
    assert out["reason"] == NO_MONETARY_COLUMN_IN_RELATION


@pytest.mark.parametrize(
    "zone,reason",
    [
        (ZONE_COLLECTED, FX_COLUMNS_ABSENT_IN_RAW_ZONE),
        (ZONE_MAPPED, FX_COLUMNS_ABSENT_IN_RELATION),
    ],
)
def test_an_amount_with_no_rate_column_names_which_absence_it_is(zone, reason) -> None:
    """Two causes, two words. The raw zone carries no rate BY CONSTRUCTION -- the
    conversion is evidenced one model later -- while a staging model that carries
    an amount and no rate is a fact about that model (4 of the 12 are)."""
    out = designate(
        columns=["date", "cost", "cost_source_currency"],
        monetary_names=["cost"],
        zone=zone,
    )
    assert out["columns"] == []
    assert out["reason"] == reason


def test_a_relation_that_was_not_read_says_that_and_not_that_it_declares_nothing(
    ) -> None:
    out = designate(
        columns=[], monetary_names=["cost"], zone=ZONE_MAPPED, readable=False
    )
    assert out["reason"] == RELATION_NOT_READ


def test_the_key_names_no_reporting_currency_on_any_branch() -> None:
    """Arbitrage 8, on every answer this module can give -- including the refusals.

    0 project of 18 has confirmed one and `governance_rule_sets` is empty, so
    printing `EUR` would present a column DEFAULT as a decision.
    """
    for out in (
        designate(columns=_MAPPED_COLUMNS, monetary_names=["cost"], zone=ZONE_MAPPED),
        designate(columns=_MAPPED_COLUMNS, monetary_names=[], zone=ZONE_MAPPED),
        designate(columns=[], monetary_names=["cost"], zone=ZONE_MAPPED, readable=False),
    ):
        assert out["reporting_currency"] is None
        assert out["reporting_currency_gate"]
        assert "EUR" not in str(out)


# ---------------------------------------------------------------------------
# One row, three outcomes, and never a default.
# ---------------------------------------------------------------------------


def _designation():
    return designate(columns=_MAPPED_COLUMNS, monetary_names=["cost"], zone=ZONE_MAPPED)


@pytest.mark.parametrize(
    "row,gap",
    [
        ({"cost_source_currency": "USD", "fx_rate": "0.92",
          "fx_as_of_date": "2026-07-01"}, None),
        ({"cost_source_currency": "USD", "fx_rate": None,
          "fx_as_of_date": None}, GAP_NO_RATE),
        ({"cost_source_currency": None, "fx_rate": "0.92",
          "fx_as_of_date": "2026-07-01"}, GAP_NO_CURRENCY),
        # A rate with no date. `money_derivation` has no word for it -- its rate
        # object always carries an as-of date -- and a RELATION can carry exactly
        # this, which the story refuses outright.
        ({"cost_source_currency": "USD", "fx_rate": "0.92",
          "fx_as_of_date": None}, GAP_NO_RATE_DATE),
        # A currency column that landed BLANK is a currency nobody reported.
        ({"cost_source_currency": "   ", "fx_rate": "0.92",
          "fx_as_of_date": "2026-07-01"}, GAP_NO_CURRENCY),
    ],
)
def test_a_row_carries_its_three_values_or_the_gap_that_names_why(row, gap) -> None:
    out = row_provenance(designation=_designation(), rows=[row])[0]["cost"]
    assert out["money_gap_code"] == gap
    if gap is None:
        assert out["fx_rate"] == "0.92"
        assert out["money_gap_message"] is None
    else:
        # A SENTENCE, never a dash: "—" under an amount reads as "there is no rate
        # to show", which is the same glyph a converted row would use for nothing.
        assert out["money_gap_message"]


def test_no_default_rate_and_no_default_date_are_ever_substituted() -> None:
    out = row_provenance(
        designation=_designation(),
        rows=[{"cost_source_currency": "USD", "fx_rate": None, "fx_as_of_date": None}],
    )[0]["cost"]
    assert out["fx_rate"] is None
    assert out["fx_as_of_date"] is None
    assert "1.0" not in str(out)


# ---------------------------------------------------------------------------
# The class repair (arbitrage 3): the staging models date their rate.
# ---------------------------------------------------------------------------


def _staging_models() -> list[Path]:
    return sorted(_MODULES.glob("*/dbt/staging/*.sql"))


#: WHERE THE FX EVIDENCE IS WRITTEN SINCE THE STORY 67.13 CUTOVER.
#:
#: This guard used to read `AS fx_as_of_date` out of each staging model, because
#: each staging model wrote those five columns itself -- thirteen copies of one
#: decision. The cutover moved them into `toorow_fx_evidence_columns`, and the
#: guard IMMEDIATELY WENT VACUOUS: `_emits_fx_as_of_date()` returned [] and its
#: own anti-vacuity line was what said so. That line is why this repair is a
#: repair and not a discovery six months from now.
#:
#: An instrument must not look only where the text USED to be. It now asks two
#: separate questions -- every model reaches the one emitter, and the one emitter
#: dates its rate correctly -- because either half alone can be satisfied by a
#: model that quietly writes its own columns again.
_FX_MACROS = _ROOT / "dbt" / "macros" / "fx_posed_resolution.sql"


def _emits_fx_evidence() -> list[Path]:
    """The staging models that publish FX evidence, however they publish it."""
    return [
        path
        for path in _staging_models()
        if "AS fx_as_of_date" in (text := path.read_text(encoding="utf-8"))
        or "toorow_fx_evidence_columns" in text
    ]


def test_every_staging_model_publishes_its_fx_evidence_through_the_one_emitter() -> None:
    """ONE definition site, thirteen call sites -- and no thirteenth-and-a-half.

    A model that goes back to writing `'seed' AS fx_source` by hand would be
    stamping a literal on a rate the governed store may have served, which is the
    capability's bullet 6 ("a rate somebody POSED is presented under the label of
    an observed one") arriving through maintenance rather than through a bug.
    """
    emitting = _emits_fx_evidence()
    # ANTI-VACUITY. If the pattern stops matching, every assertion below passes by
    # having nothing to look at.
    assert len(emitting) >= 13, [path.name for path in emitting]

    offenders = [
        path.name
        for path in emitting
        if "toorow_fx_evidence_columns" not in path.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "these staging models write their own FX evidence columns instead of "
        f"calling toorow_fx_evidence_columns(): {offenders}"
    )


def test_the_one_emitter_dates_its_rate_by_the_QUOTATION_and_never_by_the_window() -> None:
    """The mutation proof. Put `fx.valid_from` back and this names it.

    Both arms are checked, because the cutover gave `fx_as_of_date` a second
    source: the seed arm must read `fx.rate_date` and the posed arm must read the
    observation's `effective_date` -- the day the rate was DECLARED. Neither may
    read a `valid_from`, which is where a validity window opens and dates nothing.
    Reading the second under the name of the first printed `2020-01-01` under
    every converted amount of the mart (story 58.7, 43 rows of 43).
    """
    macro = _FX_MACROS.read_text(encoding="utf-8")
    block = re.search(
        r"AS fx_rate,(?P<body>.*?)AS fx_as_of_date,", macro, re.S
    )
    assert block is not None, (
        "toorow_fx_evidence_columns no longer emits fx_as_of_date -- this guard "
        "cannot see what the thirteen staging models publish"
    )
    body = re.sub(r"--[^\n]*", "", block.group("body"))
    assert "rate_date" in body, body
    assert "effective_date" in body, body
    assert "valid_from" not in body, (
        "the FX evidence emitter dates its rate by the opening of a validity "
        f"window instead of by the quotation:\n{body}"
    )


def test_the_validity_window_is_still_what_CHOOSES_the_rate() -> None:
    """`valid_from`/`valid_to` select; `rate_date` dates. Swapping the two roles
    would silently change WHICH rate a row is converted at.

    TWO windows since the cutover, and both must still choose: the seed's
    `BETWEEN fx.valid_from AND fx.valid_to` in each model, and the posed rate's
    half-open segment, which the macro derives from the declared window and the
    model reads as `>= fxp.seg_from AND < fxp.seg_until`.
    """
    offenders = [
        path.name
        for path in _emits_fx_evidence()
        if not re.search(
            r"BETWEEN\s+fx\.valid_from\s+AND\s+fx\.valid_to",
            (text := path.read_text(encoding="utf-8")),
        )
        or not re.search(r">=\s*fxp\.seg_from", text)
        or not re.search(r"<\s*fxp\.seg_until", text)
    ]
    assert not offenders, (
        "these models no longer choose their rate by a validity window -- the "
        f"seed's, the posed rate's, or both: {offenders}"
    )


def test_the_three_connectors_that_declare_no_money_still_declare_none() -> None:
    """Arbitrage 5, pinned rather than assumed.

    `int_country_daily_kpi` calls `money_evidence_absent()` on its three branches,
    and that is CORRECT only for as long as cm360, dv360 and taboola carry no
    source currency. The day one of them gains one, this reddens and the question
    reopens -- instead of a `cost` being published unconverted and without a gap
    code, indistinguishable from a count of impressions.
    """
    carriers = []
    for connector in ("cm360", "dv360", "taboola"):
        models = sorted((_MODULES / connector / "dbt" / "staging").glob("*.sql"))
        assert models, connector
        for path in models:
            text = path.read_text(encoding="utf-8")
            if "_source_currency" in text or "fx_rate" in text:
                carriers.append(path.name)
    assert not carriers, (
        "int_country_daily_kpi declares money evidence ABSENT for these "
        f"connectors and they now carry a currency: {carriers}"
    )


def test_every_refusal_carries_a_heading_from_the_same_authority_as_its_sentence() -> None:
    """A heading written on the screen contradicted the body it sat on.

    `DateBreakdownGrid` titled EVERY empty provenance « No monetary column in this
    reading ». Two of the five codes mean the opposite -- the amount IS there and
    its rate is not -- so for half the vocabulary the heading denied the sentence
    printed underneath it. The heading now comes from the same table as the
    sentence, and this pins both halves: every refusal has one, and the two codes
    that carry an amount never claim there is none.
    """
    from core.money_provenance_columns import (
        FX_COLUMNS_ABSENT_IN_RAW_ZONE,
        FX_COLUMNS_ABSENT_IN_RELATION,
        NO_MONETARY_COLUMN_IN_RELATION,
        NO_MONETARY_CONCEPT_DECLARED,
        RELATION_NOT_READ,
        message_for,
        title_for,
    )

    refusals = (
        NO_MONETARY_CONCEPT_DECLARED,
        NO_MONETARY_COLUMN_IN_RELATION,
        FX_COLUMNS_ABSENT_IN_RAW_ZONE,
        FX_COLUMNS_ABSENT_IN_RELATION,
        RELATION_NOT_READ,
    )
    missing = [code for code in refusals if not title_for(code)]
    assert not missing, f"a refusal with a sentence and no heading: {missing}"

    # The two that say an amount IS present must never be headed as an absence of
    # one -- that is the exact contradiction this replaced.
    for code in (FX_COLUMNS_ABSENT_IN_RAW_ZONE, FX_COLUMNS_ABSENT_IN_RELATION):
        heading = title_for(code) or ""
        assert "no monetary column" not in heading.lower(), (
            f"{code} says the amount is present and its rate is not; its heading "
            f"denies the amount: {heading!r}"
        )
        assert "amount" in message_for(code).lower()
