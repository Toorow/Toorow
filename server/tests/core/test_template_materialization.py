"""Story 72.6 -- the resolution, proved offline: which member goes where, and what
the inverted document says.

WHAT IS PROVED HERE. The two pure halves of the materialisation: `plan_bindings`
(which member of a Result occupies which well, and when no member can) and
`spec_payload_from_template` (one Chart Template document re-anchored on those
members, key by key). Plus the import-time guard that reddens this file the day
the Visualization Spec grammar grows a member anchor nobody taught this story to
fill.

WHAT IS PROVED ELSEWHERE. That the produced Spec is INDISTINGUISHABLE from a
hand-built one -- same validator, same `content_hash`, same table -- that a
refusal leaves no row behind, that no Result is created and no query re-executed,
and that editing a derived visualization never mutates the seed:
`tests/core/test_template_materialization_pg.py`, against a real database,
because every one of those five sentences is about rows.

THE MUTATIONS THIS FILE INSISTS ON. A plan that never moves is a constant with
extra steps, so: raising a template's `min` moves which members are bound; a
cardinality bound that the Result exceeds removes a candidate; a second well that
accepts the same role takes a DIFFERENT member while one is free and the SAME one
when none is.

No database, no network.
"""

from __future__ import annotations

import pytest
from core import template_compatibility as tc
from core import template_materialization as tm
from core.visualization_families import get_family
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    Leaf,
    PinnedMembers,
)
from core.visualization_templates import (
    CHART_TEMPLATE_CONTRACT_VERSION,
    validate_template_document,
)

#: Identifiers shaped like the ones the platform really mints, and column names
#: that are deliberately NOT the identifiers (AI-337).
_CLICKS = "mdm_01KZEXAMPLE0000000000001"
_COST = "mdm_01KZEXAMPLE0000000000002"
_CHANNEL = "mdm_01KZEXAMPLE0000000000003"
_DEVICE = "mdm_01KZEXAMPLE0000000000004"

_COLUMNS = {_CLICKS: "clicks", _COST: "cost", _CHANNEL: "channel", _DEVICE: "device"}
_ROLES = {_CLICKS: "measure", _COST: "measure", _CHANNEL: "dimension", _DEVICE: "dimension"}
_LABELS = {_CLICKS: "Clicks", _COST: "Cost", _CHANNEL: "Channel", _DEVICE: "Device"}


def _pinned(**overrides) -> PinnedMembers:
    base = dict(
        roles=dict(_ROLES),
        labels=dict(_LABELS),
        grain="day",
        comparison="none",
        row_limit=1000,
        query_spec_id="qs_example",
        semantic_view_id="sv_example",
        semantic_view_version_id="svv_example",
    )
    base.update(overrides)
    return PinnedMembers(**base)


def _schema(*members: str) -> dict:
    return {"fields": [{"id": member, "name": _COLUMNS[member]} for member in members]}


def _rows(*, channels: int = 3, devices: int = 2, count: int = 6) -> list[dict]:
    return [
        {
            "clicks": index,
            "cost": index * 2,
            "channel": f"channel-{index % channels}",
            "device": f"device-{index % devices}",
        }
        for index in range(count)
    ]


def _facts(*members: str, rows: list[dict] | None = None, **overrides) -> tc.ResultFacts:
    facts = tc.describe_result(
        result_id="res_example",
        pinned=_pinned(),
        result_schema=_schema(*(members or (_CLICKS, _CHANNEL))),
        rows=_rows() if rows is None else rows,
        row_count=6,
        truncated=False,
        outcome="success",
        query_spec_version_id="qsv_example",
        **overrides,
    )
    assert isinstance(facts, tc.ResultFacts), facts
    return facts


def _document(**overrides) -> dict:
    document = {
        "spec_contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
        "schema_version": 1,
        "family": "bar",
        "answers_question": "How does one measure compare across a few categories?",
        "requires": {
            "measure": {"min": 1, "max": 1},
            "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
        },
    }
    document.update(overrides)
    return document


def _predicates(**overrides) -> tc.TemplatePredicates:
    predicates = tc.predicates_of(_document(**overrides), version_id="vtv_example")
    assert isinstance(predicates, tc.TemplatePredicates), predicates
    return predicates


def _plan(predicates=None, facts=None, **kwargs):
    predicates = predicates or _predicates()
    family = get_family(predicates.family)
    assert family is not None
    return tm.plan_bindings(predicates, family, facts or _facts(), **kwargs)


# ---------------------------------------------------------------------------
# The choice: exactly what the predicate asked for, in the Result's own order.
# ---------------------------------------------------------------------------


def test_a_well_takes_exactly_the_number_of_members_its_predicate_asks_for():
    plan, refusals = _plan(facts=_facts(_CLICKS, _COST, _CHANNEL))
    assert refusals == []
    #  Two measures offered, one required: one bound. A template is a starting
    #  point, not a layout of somebody else's Result.
    assert plan == {"measure": [_CLICKS], "dimension": [_CHANNEL]}


def test_the_order_is_the_one_the_result_declares_and_never_alphabetical():
    plan, _ = _plan(facts=_facts(_COST, _CLICKS, _CHANNEL))
    assert plan["measure"] == [_COST]


def test_raising_the_minimum_binds_more_members():
    plan, refusals = _plan(
        predicates=_predicates(
            family="line",
            requires={"measure": {"min": 2}, "dimension": {"min": 1}},
        ),
        facts=_facts(_CLICKS, _COST, _CHANNEL),
    )
    assert refusals == []
    assert plan["measure"] == [_CLICKS, _COST]


def test_a_well_the_template_is_silent_about_stays_unbound():
    plan, _ = _plan(facts=_facts(_CLICKS, _COST, _CHANNEL, _DEVICE))
    assert set(plan) == {"measure", "dimension"}


# ---------------------------------------------------------------------------
# The mutations. A plan that never moves is a constant with extra steps.
# ---------------------------------------------------------------------------


def test_a_cardinality_bound_the_result_exceeds_removes_that_candidate():
    """The same rows, the same template, one number changed on the predicate."""
    wide = _facts(_CLICKS, _CHANNEL, rows=_rows(channels=9))
    generous, refusals = _plan(
        predicates=_predicates(
            requires={"measure": {"min": 1}, "dimension": {"min": 1, "max_cardinality": 12}}
        ),
        facts=wide,
    )
    assert refusals == []
    assert generous["dimension"] == [_CHANNEL]

    _, refused = _plan(
        predicates=_predicates(
            requires={"measure": {"min": 1}, "dimension": {"min": 1, "max_cardinality": 4}}
        ),
        facts=wide,
    )
    assert [refusal.code for refusal in refused] == ["missing_role"]
    #  Named, anchored on the control it is about, and carrying the gesture.
    assert refused[0].subject == "/requires/dimension/min"
    assert refused[0].remedy


def test_a_second_well_takes_a_free_member_first_and_a_placed_one_only_when_it_must():
    """Nothing forbids one member in two wells -- but not while another is free."""
    two_dimensions = _predicates(
        family="bar",
        requires={
            "measure": {"min": 1},
            "dimension": {"min": 1},
            "series": {"min": 1},
        },
    )
    plan, refusals = _plan(
        predicates=two_dimensions, facts=_facts(_CLICKS, _CHANNEL, _DEVICE)
    )
    assert refusals == []
    assert plan["dimension"] == [_CHANNEL]
    assert plan["series"] == [_DEVICE]

    scarce, refusals = _plan(predicates=two_dimensions, facts=_facts(_CLICKS, _CHANNEL))
    assert refusals == []
    #  One dimension, two wells that each ask for one: story 72.3 calls that
    #  compatible, so the materialisation must not refuse it.
    assert scarce["dimension"] == [_CHANNEL]
    assert scarce["series"] == [_CHANNEL]


def test_a_well_that_cannot_be_filled_is_refused_and_no_plan_is_returned_for_it():
    plan, refusals = _plan(facts=_facts(_CLICKS))
    assert "dimension" not in plan
    assert [refusal.code for refusal in refusals] == ["missing_role"]


# ---------------------------------------------------------------------------
# The inversion: the five member anchors, filled back in.
# ---------------------------------------------------------------------------


def _payload(document: dict, **plan_kwargs) -> dict:
    validated = validate_template_document(document)
    predicates = tc.predicates_of(document, version_id="vtv_example")
    assert isinstance(predicates, tc.TemplatePredicates)
    family = get_family(predicates.family)
    assert family is not None
    plan, refusals = tm.plan_bindings(
        predicates,
        family,
        plan_kwargs.pop("facts", None) or _facts(_CLICKS, _COST, _CHANNEL, _DEVICE),
        control_wells=tm._wells_named_by_a_control(validated.document),
    )
    assert refusals == [], refusals
    return tm.spec_payload_from_template(validated.document, plan)


def test_requires_becomes_bindings_and_every_well_of_the_vocabulary_is_present():
    payload = _payload(_document())
    assert "requires" not in payload
    assert payload["bindings"]["measure"] == [_CLICKS]
    assert payload["bindings"]["dimension"] == [_CHANNEL]
    assert payload["bindings"]["series"] == []


def test_the_document_swaps_contracts_and_drops_the_question_the_template_answered():
    payload = _payload(_document())
    assert payload["spec_contract_version"] == VISUALIZATION_SPEC_CONTRACT_VERSION
    assert payload["schema_version"] == 1
    #  What the TEMPLATE claims to answer belongs to the template's own version. A
    #  Visualization answers the question its pinned Query Spec asks.
    assert "answers_question" not in payload


def test_the_presentation_intent_crosses_over_unchanged():
    payload = _payload(
        _document(
            legend={"position": "bottom", "visible": False},
            axes={"x": {"scale": "ordinal"}, "y": {"zero_baseline": False}},
            color={"role": "categorical", "semantic_direction": "higher_is_better"},
        )
    )
    assert payload["legend"] == {"position": "bottom", "visible": False}
    assert payload["axes"]["x"]["scale"] == "ordinal"
    assert payload["axes"]["y"]["zero_baseline"] is False
    assert payload["color"]["role"] == "categorical"


def test_a_threshold_names_a_well_in_a_template_and_a_member_in_a_spec():
    payload = _payload(
        _document(
            thresholds=[{"well": "measure", "comparator": "gt", "value": 100}],
        )
    )
    assert payload["thresholds"] == [
        {"comparator": "gt", "value": 100.0, "severity": "info", "member_id": _CLICKS}
    ]
    assert "well" not in payload["thresholds"][0]


def test_a_reference_line_is_re_anchored_the_same_way():
    payload = _payload(_document(reference_lines=[{"well": "measure", "kind": "average"}]))
    assert payload["reference_lines"][0]["member_id"] == _CLICKS
    assert payload["reference_lines"][0]["kind"] == "average"


def test_datum_wells_become_the_members_those_wells_hold():
    payload = _payload(
        _document(evidence={"datum_wells": ["measure", "dimension"], "mark_binding": "datum"})
    )
    #  `datum_wells` is an UNORDERED list in the grammar, so the walker sorted it
    #  before hashing -- two documents that mean the same thing must hash the
    #  same. The members come out in the order of the sorted wells.
    assert payload["evidence"]["datum_fields"] == [_CHANNEL, _CLICKS]
    assert payload["evidence"]["mark_binding"] == "datum"
    assert "datum_wells" not in payload["evidence"]


def test_a_label_override_keyed_by_a_well_becomes_one_keyed_by_a_member():
    payload = _payload(_document(labels={"override": {"measure": "Confirmed clicks"}}))
    assert payload["labels"]["override"] == {_CLICKS: "Confirmed clicks"}


def test_a_control_names_a_well_the_requires_block_does_not_and_it_is_filled_anyway():
    """Otherwise the inverted document would carry a threshold on no member."""
    payload = _payload(
        _document(
            family="bar",
            requires={"measure": {"min": 1}, "dimension": {"min": 1}},
            labels={"override": {"series": "Device family"}},
        )
    )
    assert payload["bindings"]["series"] == [_DEVICE]
    assert payload["labels"]["override"] == {_DEVICE: "Device family"}


# ---------------------------------------------------------------------------
# The instrument that reddens when the grammar moves.
# ---------------------------------------------------------------------------


def test_a_sixth_member_anchor_in_the_spec_grammar_raises_instead_of_being_ignored():
    """The measurement is taken on `GRAMMAR`, not on a copy of it in this file."""
    grown = dict(tm.GRAMMAR)
    grown["headline"] = Leaf("member_id")
    original = tm.GRAMMAR
    tm.GRAMMAR = grown
    try:
        with pytest.raises(tm.TemplateMaterializationUnsupported) as raised:
            tm._verify_every_member_anchor_is_handled()
    finally:
        tm.GRAMMAR = original
    assert "/headline" in str(raised.value)


def test_every_member_anchor_the_shipped_grammar_declares_is_handled_today():
    tm._verify_every_member_anchor_is_handled()
