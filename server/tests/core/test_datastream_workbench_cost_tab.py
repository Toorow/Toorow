"""The `Cost` tab -- story 58.6, epic 58.

In-memory doubles, on the pattern of `tests/core/test_datastream_daily_breakdown.py`:
what is proved here is WHO DECIDES and WHAT IS REFUSED. That the address is
mounted and answers its schema is proved in
`tests/integration/test_datastream_workbench_api.py`.

THE FIVE THINGS THIS FILE EXISTS TO HOLD:

  * the capability state decides ALONE whether the warehouse is asked, and it is
    read before it -- `disabled`, `draft`, `blocked` and `unset` never read a mart;
  * a phase the mart could not evaluate carries its gap code and no amount, and a
    total is absent rather than `0` when the ladder is incomplete;
  * each phase names the LEVELS that laid it, resolved through `scope_kind`, and
    never a rule identifier beside an amount;
  * an unreachable warehouse is a typed refusal, not an empty cascade -- "there is
    nothing" and "we could not look" share no word;
  * the vocabulary of levels is the store's own: three exist, `organisation` and
    `source_category` do not, and both absences are published with their reason.

THE NUMBERS BELOW ARE MEASURED, NOT CHOSEN. Every micros amount is a row of
`main_marts.fee_tax_ladder_daily` for the fixture Project `feetax_dev_complete`,
read from the DuckDB build on 2026-08-07:

    net_media 12345670000 | platform_fee 493826800 | regulatory_tax 0
    wht_gross_up 2265793553 | agency_fee 1510529035 | sales_tax 3323163878
    subtotal_ht 16615819388 | total_ttc 19938983266 | applied_rule_count 5

and the two rules that lay the platform-fee phase are the fixture's own:
`ftr_dev_complete_platform` (scope_kind `project`) and `ftr_dev_complete_ds`
(scope_kind `datastream`). A fixture invented to read well is the fault this
repository pays for most often.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from core import datastream_workbench_cost as cost
from core.datastream_workbench_cost import (
    ABSENT_LEVELS,
    CASCADE_LEVELS,
    CASCADE_PHASES,
    COST_WINDOW_DAYS,
    EMPTY_NO_RULE_PUBLISHED,
    EMPTY_NO_RULE_PUBLISHED_MESSAGE,
    GAP_LADDER_INCOMPLETE,
    GAP_PHASE_NOT_EVALUATED,
    STATE_AVAILABLE,
    STATE_CAPABILITY_INACTIVE,
    STATE_EMPTY,
    CostCascadeUnavailable,
    read_cost_evidence,
)
from core.fee_tax_rules import SCOPE_KINDS
from core.project_capability_states import (
    CAPABILITY_ACTIVE_STATES,
    CAPABILITY_STATE_UNSET,
    CAPABILITY_STATES,
)

_TODAY = date(2026, 8, 7)

#: One row of the fixture Project, column for column.
_LADDER_ROW = {
    "project_id": "proj_EXAMPLE",
    "date": "2026-05-01",
    "connector": "meta-ads",
    "breakdown_dimension": "campaign_id",
    "breakdown_value": "camp_EXAMPLE_1",
    "currency": "EUR",
    "net_media_micros": 12345670000,
    "platform_fee_micros": 493826800,
    "regulatory_tax_micros": 0,
    "wht_gross_up_micros": 2265793553,
    "agency_fee_micros": 1510529035,
    "subtotal_ht_micros": 16615819388,
    "sales_tax_micros": 3323163878,
    "total_ttc_micros": 19938983266,
    "applied_rule_ids": (
        "ftr_EXAMPLE_agency|ftr_EXAMPLE_ds|ftr_EXAMPLE_platform|ftr_EXAMPLE_vat|ftr_EXAMPLE_wht"
    ),
    "applied_rule_count": 5,
    "gap_codes": "",
    "is_ladder_complete": True,
}

#: The fixture's rules, with the level each is laid at.
_RULES = [
    ("ftr_EXAMPLE_platform", "project", None, "PLATFORM_FEE", "confirmed"),
    ("ftr_EXAMPLE_ds", "datastream", "ds_EXAMPLE", "PLATFORM_FEE", "confirmed"),
    ("ftr_EXAMPLE_wht", "project", None, "WHT_GROSS_UP", "confirmed"),
    ("ftr_EXAMPLE_agency", "project", None, "AGENCY_FEE", "confirmed"),
    ("ftr_EXAMPLE_vat", "project", None, "SALES_TAX", "confirmed"),
]


class _Cursor:
    """A cursor that answers by SQL SHAPE, so the ORDER of the reads is provable."""

    def __init__(self, owner: "_Connection") -> None:
        self._owner = owner
        self._rows: list[tuple] = []

    def __enter__(self) -> "_Cursor":
        return self

    def __exit__(self, *_exc) -> bool:
        return False

    def execute(self, sql: str, params=None) -> None:
        self._owner.statements.append(sql)
        if "project_capabilities" in sql:
            self._rows = [] if self._owner.state is None else [(self._owner.state,)]
        elif "fee_tax_rules" in sql:
            self._rows = list(self._owner.rules)
        elif "COUNT(*)" in sql:
            self._rows = [(self._owner.datastreams_on_connector,)]
        else:  # pragma: no cover -- an unexpected read must not answer silently
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _Connection:
    def __init__(self, *, state: str | None, rules=None, datastreams_on_connector: int = 1):
        self.state = state
        self.rules = rules if rules is not None else _RULES
        self.datastreams_on_connector = datastreams_on_connector
        self.statements: list[str] = []

    def cursor(self):
        return _Cursor(self)


def _read(conn, *, rows=None, refused=None, raises=None):
    """`read_cost_evidence` with the warehouse and the projection doubled."""

    def _ladder(*_args, **_kwargs):
        if raises is not None:
            raise raises
        return list(rows or [])

    projection = {
        "capabilities": [
            {
                "capability_key": "tax_fees",
                "impact": {
                    "detected_support_selection": {
                        "state": "detected",
                        "refused_rules": list(refused or []),
                    }
                },
            }
        ]
    }
    with (
        patch("core.warehouse.query_fee_tax_ladder_daily", side_effect=_ladder) as ladder,
        patch(
            "core.capability_proposals.read_datastream_capabilities", return_value=projection
        ),
        patch(
            "core.datastream_sample_api.datastream_materialization_is_ambiguous",
            return_value=conn.datastreams_on_connector != 1,
        ),
    ):
        evidence = read_cost_evidence(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            connector="meta-ads",
            today=_TODAY,
        )
    return evidence, ladder


# ---------------------------------------------------------------------------
# The capability decides, and it decides first.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["disabled", "draft", "blocked", None])
def test_an_inactive_capability_returns_no_measure_and_reads_no_warehouse(state) -> None:
    conn = _Connection(state=state)
    evidence, ladder = _read(conn)

    assert evidence["state"] == STATE_CAPABILITY_INACTIVE
    assert evidence["capability"]["active"] is False
    assert evidence["measures"] is None and evidence["cascade"] is None
    # THE POINT OF THE ORDER: not one warehouse round trip, and not one statement
    # beyond the switch itself.
    ladder.assert_not_called()
    assert len(conn.statements) == 1
    assert "project_capabilities" in conn.statements[0]


def test_a_project_the_control_plane_never_wrote_reads_unset_not_disabled() -> None:
    conn = _Connection(state=None)
    evidence, _ = _read(conn)

    # `unset` is not `disabled`: one is a decision somebody took, the other is a
    # row that was never written, and they are repaired at different doors.
    assert evidence["capability"]["state"] == CAPABILITY_STATE_UNSET
    assert CAPABILITY_STATE_UNSET not in CAPABILITY_STATES


@pytest.mark.parametrize("state", CAPABILITY_ACTIVE_STATES)
def test_an_active_capability_reads_the_ladder_after_the_switch(state) -> None:
    conn = _Connection(state=state)
    evidence, ladder = _read(conn, rows=[_LADDER_ROW])

    assert evidence["state"] == STATE_AVAILABLE
    ladder.assert_called_once()
    # The switch is the FIRST statement, before the rules and before the count.
    assert "project_capabilities" in conn.statements[0]
    # And the window it read is the stated one, ending today.
    assert ladder.call_args.args[1:] == ("2026-07-09", "2026-08-07")
    assert evidence["window"]["days"] == COST_WINDOW_DAYS


def test_the_active_states_are_not_a_second_opinion_of_the_country_ones() -> None:
    """The two lists are the same list, and this is what stops them forking.

    `datastream_daily_breakdown_api` declares `COUNTRY_ACTIVE_STATES` for the same
    column. Story 58.6 could not consolidate it -- that file belongs to another
    story -- so the duplication is PINNED instead of left to drift silently.
    """
    from core.datastream_daily_breakdown_api import (
        COUNTRY_ACTIVE_STATES,
        COUNTRY_STATE_UNSET,
    )

    assert tuple(COUNTRY_ACTIVE_STATES) == tuple(CAPABILITY_ACTIVE_STATES)
    assert COUNTRY_STATE_UNSET == CAPABILITY_STATE_UNSET


# ---------------------------------------------------------------------------
# The four measures, and what an absent one says instead of `0`.
# ---------------------------------------------------------------------------


def test_the_four_measures_are_read_from_the_mart_and_the_uplift_is_exact() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    measures = {measure["key"]: measure for measure in evidence["measures"]}
    assert [measure["key"] for measure in evidence["measures"]] == [
        "net_media", "what_we_add", "total", "uplift",
    ]
    assert measures["net_media"]["micros"] == 12345670000
    # The five phase columns of the fixture row, summed: this is what we add.
    assert measures["what_we_add"]["micros"] == 493826800 + 0 + 2265793553 + 1510529035 + 3323163878
    assert measures["total"]["micros"] == 19938983266
    # 7593313266 / 12345670000 -- computed in Decimal over integers, never a float.
    assert measures["uplift"]["percent"] == "61.5"
    assert all(measure["gap_code"] is None for measure in evidence["measures"])
    assert measures["net_media"]["currency"] == "EUR"


def test_an_incomplete_ladder_has_no_total_and_says_why_rather_than_showing_zero() -> None:
    conn = _Connection(state="ready")
    row = {**_LADDER_ROW, "total_ttc_micros": None, "is_ladder_complete": False}
    evidence, _ = _read(conn, rows=[row])

    measures = {measure["key"]: measure for measure in evidence["measures"]}
    assert measures["total"]["micros"] is None
    assert measures["total"]["gap_code"] == GAP_LADDER_INCOMPLETE
    assert measures["total"]["reason"]
    # The parts stay readable: you may look at them, you may not read a total we
    # could not compute.
    assert measures["net_media"]["micros"] == 12345670000


def test_a_phase_the_mart_could_not_evaluate_is_absent_and_never_zero() -> None:
    conn = _Connection(state="ready")
    row = {**_LADDER_ROW, "agency_fee_micros": None, "is_ladder_complete": False,
           "total_ttc_micros": None}
    evidence, _ = _read(conn, rows=[row])

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    assert steps["agency_fee"]["micros"] is None
    assert steps["agency_fee"]["gap_code"] == GAP_PHASE_NOT_EVALUATED
    # A real zero stays a zero: the fixture's regulatory tax is 0 because no rule
    # routed to it, and that is a measurement.
    assert steps["regulatory_tax"]["micros"] == 0
    assert steps["regulatory_tax"]["gap_code"] is None
    # And what we add cannot be known in full either -- it does not silently drop
    # the phase it could not read.
    measures = {measure["key"]: measure for measure in evidence["measures"]}
    assert measures["what_we_add"]["micros"] is None
    assert measures["uplift"]["percent"] is None


def test_two_currencies_in_one_window_compose_no_total_at_all() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(
        conn, rows=[_LADDER_ROW, {**_LADDER_ROW, "currency": "USD"}]
    )

    assert evidence["currencies"] == ["EUR", "USD"]
    # Adding across currencies composes a number in no currency at all.
    assert all(measure["micros"] is None for measure in evidence["measures"])
    assert all(measure["gap_code"] for measure in evidence["measures"])


# ---------------------------------------------------------------------------
# The level -- what this story makes readable for the first time.
# ---------------------------------------------------------------------------


def test_each_phase_names_the_levels_that_laid_it() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    # The platform fee is laid at TWO levels in this fixture: a Project rule at 3%
    # and a Datastream rule at 1%. That is the reading Jean asked for.
    assert [level["kind"] for level in steps["platform_fee"]["levels"]] == [
        "project", "datastream",
    ]
    assert [level["rule_count"] for level in steps["platform_fee"]["levels"]] == [1, 1]
    # The sales tax is laid at one.
    assert [level["kind"] for level in steps["sales_tax"]["levels"]] == ["project"]
    # A phase no rule reached names no level rather than an empty one.
    assert steps["regulatory_tax"]["levels"] == []


def test_no_rule_identifier_is_ever_printed_beside_a_phase_amount() -> None:
    """An identifier next to a number invites reading the number as that rule's.

    The refusal footer names `rule_key`s, and that is legitimate: nothing there
    carries an amount. A numbered phase carries counts per level and nothing else.
    """
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    for step in evidence["cascade"]["steps"]:
        assert "rule_ids" not in step and "applied_rule_ids" not in step
        for level in step["levels"]:
            assert set(level) == {"kind", "label", "covers", "rule_count"}


def test_a_rule_the_store_no_longer_carries_is_counted_not_dropped() -> None:
    conn = _Connection(state="ready", rules=_RULES[:1])
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    # Four of the five applied identifiers no longer resolve. A level silently
    # missing would read as a level nobody used.
    assert steps["platform_fee"]["unresolved_rule_count"] == 4
    assert steps["platform_fee"]["unresolved_reason"]


def test_the_three_levels_are_the_store_s_own_and_two_absences_are_published() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    rendered = {level["kind"] for level in evidence["levels"]["rendered"]}
    # The vocabulary is `fee_tax_rules.SCOPE_KINDS`, not a list this module chose.
    assert rendered == set(SCOPE_KINDS)
    assert {level["kind"] for level in CASCADE_LEVELS} == set(SCOPE_KINDS)
    # And the two a person asks for, named absent WITH their substance.
    absent = {level["name"] for level in evidence["levels"]["absent"]}
    assert absent == {"Organisation", "Source category"}
    assert all(level["reason"] for level in ABSENT_LEVELS)
    # `plan_version` is rendered for what it is, and never as a storey.
    plan = next(level for level in CASCADE_LEVELS if level["kind"] == "plan_version")
    assert "not a storey of configuration" in plan["covers"]


def test_the_surface_says_the_cascade_is_aggregated_by_phase() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    assert evidence["cascade"]["aggregation"] == "phase"
    assert "no relation of rule to contributed amount" in (
        evidence["cascade"]["aggregation_reason"]
    )
    assert len(evidence["cascade"]["steps"]) == len(CASCADE_PHASES) == 5


# ---------------------------------------------------------------------------
# Grain, empty, broken, and the one gesture.
# ---------------------------------------------------------------------------


def test_a_connector_shared_by_two_datastreams_says_so_with_its_number() -> None:
    conn = _Connection(state="ready", datastreams_on_connector=3)
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    assert evidence["grain"]["ambiguous"] is True
    assert evidence["grain"]["datastreams_on_connector"] == 3
    assert "3 Datastreams" in evidence["grain"]["reason"]
    # The grain is never claimed to be the Datastream's.
    assert evidence["grain"]["datastream_grain"] is False


def test_a_project_with_no_published_rule_is_empty_in_the_ratified_sentence() -> None:
    conn = _Connection(state="ready", rules=[])
    evidence, _ = _read(conn, rows=[])

    assert evidence["state"] == STATE_EMPTY
    assert evidence["empty_code"] == EMPTY_NO_RULE_PUBLISHED
    assert evidence["reason"] == EMPTY_NO_RULE_PUBLISHED_MESSAGE
    # And who writes the thing that would fill it.
    assert evidence["owner"] == "Governance"


def test_rules_published_but_no_row_on_the_window_is_a_different_sentence() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[])

    assert evidence["state"] == STATE_EMPTY
    # Saying "no rule has been published" to a Project that published five would
    # send a person to a door they have already walked through.
    assert evidence["reason"] != EMPTY_NO_RULE_PUBLISHED_MESSAGE
    assert "meta-ads" in evidence["reason"]


def test_an_unreachable_warehouse_is_a_refusal_and_never_an_empty_cascade() -> None:
    from core.warehouse import WarehouseUnavailable

    conn = _Connection(state="ready")
    with pytest.raises(CostCascadeUnavailable) as raised:
        _read(conn, raises=WarehouseUnavailable("dataset absent"))

    # "There is nothing" and "we could not look" must never render for one
    # another, so the two sentences share no word.
    broken = str(raised.value).lower().split()
    empty = EMPTY_NO_RULE_PUBLISHED_MESSAGE.lower().split()
    assert not set(broken) & set(empty)


def test_the_only_gesture_is_a_semantic_owner_reference_never_an_address() -> None:
    conn = _Connection(state="ready")
    evidence, _ = _read(conn, rows=[_LADDER_ROW])

    reference = evidence["governance_owner_reference"]
    assert reference["workspace"] == "governance"
    assert reference["section"] == "controls-quality"
    assert reference["object_type"] == "rule-set"
    # Never a composed URL: only the console builds an address.
    assert not any(isinstance(value, str) and value.startswith("/") for value in reference.values())


def test_the_refused_rules_carry_their_code_and_their_reason() -> None:
    conn = _Connection(state="ready")
    refused = [
        {
            "rule_key": "platform_fee_search",
            "code": "source_type_out_of_scope",
            "reason": "scoped to SEARCH; this Datastream is PAID_MEDIA",
        }
    ]
    evidence, _ = _read(conn, rows=[_LADDER_ROW], refused=refused)

    assert evidence["refused_rules"]["rules"] == refused
    assert evidence["refused_rules"]["state"] == "available"


def test_a_projection_that_never_compiled_says_nothing_was_examined() -> None:
    conn = _Connection(state="ready")
    with (
        patch("core.warehouse.query_fee_tax_ladder_daily", return_value=[_LADDER_ROW]),
        patch(
            "core.capability_proposals.read_datastream_capabilities",
            return_value={"capabilities": [{"capability_key": "tax_fees", "impact": {}}]},
        ),
        patch(
            "core.datastream_sample_api.datastream_materialization_is_ambiguous",
            return_value=False,
        ),
    ):
        evidence = read_cost_evidence(
            conn,
            project_id="proj_EXAMPLE",
            datastream_id="ds_EXAMPLE",
            connector="meta-ads",
            today=_TODAY,
        )

    # An absent projection is not an empty refusal list: nobody examined anything,
    # and "no rule was refused" would be a claim.
    assert evidence["refused_rules"]["state"] == "unavailable"
    assert evidence["refused_rules"]["rules"] == []


# ---------------------------------------------------------------------------
# The guard on the guards.
# ---------------------------------------------------------------------------


def test_the_fixture_row_carries_every_column_this_module_reads() -> None:
    """A double that answers `{}` would make half this file pass on nothing."""
    for phase in CASCADE_PHASES:
        assert phase["column"] in _LADDER_ROW
    for column in ("net_media_micros", "total_ttc_micros", "applied_rule_ids", "currency"):
        assert column in _LADDER_ROW
    assert len(_RULES) == 5 and len(_LADDER_ROW["applied_rule_ids"].split("|")) == 5
    # And every rule of the fixture is laid at a level the store admits.
    assert {rule[1] for rule in _RULES} <= set(SCOPE_KINDS)
    assert cost.COST_WINDOW_DAYS > 0


# ---------------------------------------------------------------------------
# The window is SIX ROWS, not one -- and that is the ordinary shape.
# ---------------------------------------------------------------------------

#: The second fixture Project, column for column: `feetax_dev_gapped`, read from
#: the same DuckDB build on 2026-08-07. SIX rows -- three days x two campaigns --
#: which is what `feetax_dev_complete` carries too, and what every assertion above
#: was blind to because they all ran on a single row.
#:
#: It is the gapped arm on purpose: `regulatory_tax_micros` is NULL because the
#: country never resolved (`country_gap_reason = no_binding_and_posture_global`),
#: so `total_ttc_micros` and `sales_tax_micros` are NULL and `is_ladder_complete`
#: is false. One rule fires -- the unconditioned agency fee -- and it fires on all
#: six rows.
_GAPPED_ROWS = [
    {
        "project_id": "proj_EXAMPLE",
        "date": day,
        "connector": "meta-ads",
        "breakdown_dimension": "campaign_id",
        "breakdown_value": campaign,
        "currency": "EUR",
        "net_media_micros": net,
        "platform_fee_micros": 0,
        "regulatory_tax_micros": None,
        "wht_gross_up_micros": 0,
        "agency_fee_micros": net // 10,
        "subtotal_ht_micros": None,
        "sales_tax_micros": None,
        "total_ttc_micros": None,
        "applied_rule_ids": "ftr_EXAMPLE_gapped_agency",
        "applied_rule_count": 1,
        "gap_codes": "COUNTRY_UNRESOLVED",
        "is_ladder_complete": False,
    }
    for day in ("2026-05-01", "2026-05-02", "2026-05-03")
    for campaign, net in (("camp_EXAMPLE_1", 2000000000), ("camp_EXAMPLE_2", 3000000000))
]

#: That Project's rules. TWO of them sit at the SAME level in the SAME category --
#: the two country-conditioned digital-services taxes -- which is why a per-level
#: count is a measurement and not a constant. They do not fire in the DuckDB build
#: because the country is unresolved; `_GAPPED_ROWS_RESOLVED` below is the same
#: rules on the same Project with that one gap closed.
_GAPPED_RULES = [
    ("ftr_EXAMPLE_gapped_dst_gb", "project", None, "REGULATORY_TAX", "confirmed"),
    ("ftr_EXAMPLE_gapped_dst_fr", "project", None, "REGULATORY_TAX", "confirmed"),
    ("ftr_EXAMPLE_gapped_agency", "project", None, "AGENCY_FEE", "confirmed"),
    ("ftr_EXAMPLE_gapped_vat_fr", "project", None, "SALES_TAX", "confirmed"),
]


def test_a_rule_that_fires_on_every_row_is_one_rule_not_one_per_row() -> None:
    """THE DEFECT THIS FILE COULD NOT SEE: every assertion above ran on one row.

    `applied_rule_ids` is a per-ROW provenance column, so the same rule appears on
    all six rows of the window. Counted per occurrence, one agency-fee rule read
    `Project · 6 rule(s)` while `applied_rule_count` on the same panel said `1`.
    """
    conn = _Connection(state="ready", rules=_GAPPED_RULES)
    evidence, _ = _read(conn, rows=_GAPPED_ROWS)

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    assert [level["rule_count"] for level in steps["agency_fee"]["levels"]] == [1]
    assert [level["kind"] for level in steps["agency_fee"]["levels"]] == ["project"]
    # The two numbers on the same panel agree, which is the whole point.
    assert evidence["cascade"]["applied_rule_count"] == 1
    assert steps["agency_fee"]["unresolved_rule_count"] == 0


def test_the_amounts_are_summed_over_the_rows_even_though_the_rules_are_not() -> None:
    """Six rows really do add up; only the provenance is de-duplicated."""
    conn = _Connection(state="ready", rules=_GAPPED_RULES)
    evidence, _ = _read(conn, rows=_GAPPED_ROWS)

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    # 10% of (2 000 + 3 000) EUR a day, three days.
    assert steps["agency_fee"]["micros"] == 3 * (200000000 + 300000000)
    measures = {measure["key"]: measure for measure in evidence["measures"]}
    assert measures["net_media"]["micros"] == 3 * (2000000000 + 3000000000)
    # And the gap survives the six rows: no total, no uplift, no fabricated zero.
    assert steps["regulatory_tax"]["micros"] is None
    assert measures["total"]["micros"] is None
    assert measures["total"]["gap_code"] == GAP_LADDER_INCOMPLETE
    assert measures["uplift"]["percent"] is None


def test_two_rules_at_one_level_read_as_two_and_not_as_one() -> None:
    """A per-level count is a MEASUREMENT, and this is what makes it falsifiable.

    `feetax_dev_gapped` publishes two confirmed REGULATORY_TAX rules at the SAME
    level, `ftr_dev_gapped_dst_gb` and `ftr_dev_gapped_dst_fr`. In the DuckDB build
    neither fires -- both are country-conditioned and the country never resolved --
    so the shipped fixture can never show a level carrying more than one rule. The
    rows below are that Project's own rules with that one gap closed, which is what
    the mart emits the day a country binding exists; nothing here is a rule the
    store does not carry.

    Without this case, `counts[kind] = 1` passes every assertion in this file.
    """
    conn = _Connection(state="ready", rules=_GAPPED_RULES)
    resolved = [
        {
            **row,
            "regulatory_tax_micros": row["net_media_micros"] // 20,
            "applied_rule_ids": (
                "ftr_EXAMPLE_gapped_agency|ftr_EXAMPLE_gapped_dst_fr|ftr_EXAMPLE_gapped_dst_gb"
            ),
            "applied_rule_count": 3,
            "gap_codes": "",
        }
        for row in _GAPPED_ROWS
    ]
    evidence, _ = _read(conn, rows=resolved)

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    assert [level["kind"] for level in steps["regulatory_tax"]["levels"]] == ["project"]
    assert [level["rule_count"] for level in steps["regulatory_tax"]["levels"]] == [2]
    # Still one for the phase laid by one rule, over the same six rows.
    assert [level["rule_count"] for level in steps["agency_fee"]["levels"]] == [1]
    assert evidence["cascade"]["applied_rule_count"] == 3


def test_the_unresolved_count_is_rules_not_occurrences_either() -> None:
    conn = _Connection(state="ready", rules=[])
    evidence, _ = _read(conn, rows=_GAPPED_ROWS)

    steps = {step["key"]: step for step in evidence["cascade"]["steps"]}
    # One rule the store no longer carries, on six rows. Counted six times, the
    # sentence beside it would report a fleet of vanished rules.
    assert steps["agency_fee"]["unresolved_rule_count"] == 1
    assert evidence["cascade"]["applied_rule_count"] == 1


def test_the_multi_row_fixture_is_really_multi_row() -> None:
    """A cardinality guard on the guard: one row would make four tests vacuous."""
    assert len(_GAPPED_ROWS) == 6
    assert len({row["date"] for row in _GAPPED_ROWS}) == 3
    assert len({row["breakdown_value"] for row in _GAPPED_ROWS}) == 2
    # Every row repeats the same provenance string -- that IS the shape that broke.
    assert len({row["applied_rule_ids"] for row in _GAPPED_ROWS}) == 1
    # And two of the rules sit at one level in one category, or the count above
    # could never be anything but 1.
    same_level = [r for r in _GAPPED_RULES if (r[1], r[3]) == ("project", "REGULATORY_TAX")]
    assert len(same_level) == 2
