"""Story 72.3 -- the compatibility verdict, proved offline.

WHAT IS PROVED HERE. AC9 (a negative verdict names the unsatisfied predicate,
one by one, with its pointer and its remedy -- never a bare boolean), AC10 (the
sentences are composed on the server and a member is named by its NAME), AC11
(three states, and `unreadable` is never rendered as `incompatible`), AC12 (the
verdict is derived: this module only reads).

WHAT IS PROVED ELSEWHERE. That the two tables carry no compatibility column and
that the reading gate performs no write against a real schema is
`tests/core/test_template_compatibility_pg.py`.

THE TWO MUTATIONS THIS FILE INSISTS ON, because a verdict that never moves is a
constant with extra steps:

  * tightening a template's own predicate flips a compatible verdict to
    incompatible AND names the tightened predicate;
  * moving what the shipped REGISTRY declares -- which roles have a source --
    moves the verdict too, because the registry is the authority and this file
    holds no second copy of it.

No database, no network.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core import template_compatibility as tc
from core.visualization_families import ROLE_UNAVAILABLE_OWNER, get_family
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    PinnedMembers,
    VisualizationRefusal,
)
from core.visualization_templates import CHART_TEMPLATE_CONTRACT_VERSION

_MODULE = Path(tc.__file__)

#: A member id shaped like the identifiers the platform really mints. It exists so
#: no sentence in this epic can accidentally pass a test while printing a ULID.
_CHANNEL_ID = "mdm_01KZEXAMPLE0000000000000"
_CLICKS_ID = "mdm_01KZEXAMPLE0000000000001"


def _pinned(*, grain: str | None = "day", comparison: str = "none", **overrides) -> PinnedMembers:
    """A question that selected one measure and one dimension, with their names.

    The labels are the canonical NAMES; the ids are identifiers. Keeping them
    different is the only way a test can tell which one a sentence printed.
    """
    base = dict(
        roles={_CLICKS_ID: "measure", _CHANNEL_ID: "dimension"},
        labels={_CLICKS_ID: "Clicks", _CHANNEL_ID: "Channel"},
        grain=grain,
        comparison=comparison,
        row_limit=1000,
        query_spec_id="qs_example",
        semantic_view_id="sv_example",
        semantic_view_version_id="svv_example",
    )
    base.update(overrides)
    return PinnedMembers(**base)


def _schema(*members: str) -> dict:
    """The Result schema, keyed the way the server really ships it: `{id, name}`.

    The column NAME is deliberately not the member id (AI-337): a count taken on
    the id would read nothing on a Result a warehouse actually produced.
    """
    names = {_CLICKS_ID: "clicks", _CHANNEL_ID: "channel"}
    return {"fields": [{"id": member, "name": names[member]} for member in members]}


def _rows(channels: int = 3, count: int = 3) -> list[dict]:
    return [
        {"channel": f"channel-{index % channels}", "clicks": index}
        for index in range(max(count, channels))
    ]


def _template(**overrides) -> dict:
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
    predicates = tc.predicates_of(_template(**overrides), version_id="vtv_example")
    assert isinstance(predicates, tc.TemplatePredicates), predicates
    return predicates


#: `None` is a MEANING for `rows` -- "the rows were not read" -- so the fixture
#: cannot use it as "not supplied". A test that could not say "unread" would be
#: unable to reach the third state at all.
_UNSET = object()


def _facts(
    *,
    members: tuple[str, ...] = (_CLICKS_ID, _CHANNEL_ID),
    rows=_UNSET,
    pinned: PinnedMembers | None = None,
    outcome: str = "success",
    schema: dict | None = None,
) -> tc.ResultFacts | tc.Unreadable:
    return tc.describe_result(
        result_id="res_example",
        pinned=pinned or _pinned(),
        result_schema=_schema(*members) if schema is None else schema,
        rows=_rows() if rows is _UNSET else rows,
        row_count=3,
        truncated=False,
        outcome=outcome,
    )


def _verdict(**kwargs) -> tc.CompatibilityVerdict:
    template = kwargs.pop("template", None) or _predicates()
    result = kwargs.pop("result", None)
    if result is None:
        result = _facts(**kwargs)
    return tc.check_template_compatibility(template, result)


def _codes(verdict: tc.CompatibilityVerdict) -> list[str]:
    return [refusal.code for refusal in verdict.unmet]


# ---------------------------------------------------------------------------
# The verdict that says yes.
# ---------------------------------------------------------------------------


def test_a_result_that_satisfies_every_predicate_is_compatible():
    verdict = _verdict()
    assert verdict.state == tc.COMPATIBLE
    assert verdict.unmet == ()
    assert verdict.unreadable is None
    # The question the template declares travels with the verdict: a list screen
    # states "the question the template answers", never its identifier.
    assert verdict.answers_question.startswith("How does one measure compare")


def test_the_three_states_are_three_distinct_words():
    assert len({tc.COMPATIBLE, tc.INCOMPATIBLE, tc.UNAVAILABLE}) == 3


# ---------------------------------------------------------------------------
# AC9 -- the verdict names the predicate, one by one. Never a bare boolean.
# ---------------------------------------------------------------------------


def test_a_missing_role_is_named_with_its_well_its_role_and_a_remedy():
    verdict = _verdict(members=(_CLICKS_ID,))
    assert verdict.state == tc.INCOMPATIBLE
    assert _codes(verdict) == ["missing_role"]
    (refusal,) = verdict.unmet
    assert "Dimension" in refusal.message
    assert "dimension" in refusal.message
    assert "carries none" in refusal.message
    assert refusal.subject == "/requires/dimension/min"
    assert refusal.remedy and "Explore" in refusal.remedy


def test_a_family_predicate_is_named_in_the_words_of_both_objects():
    """Grain is the FAMILY's predicate, and the verdict says whose it is."""
    verdict = _verdict(
        template=_predicates(family="line"),
        result=_facts(pinned=_pinned(grain=None)),
    )
    assert verdict.state == tc.INCOMPATIBLE
    assert _codes(verdict) == ["missing_grain"]
    (refusal,) = verdict.unmet
    assert "this template's Line family needs a time grain" in refusal.message
    assert "this Result carries none" in refusal.message
    assert refusal.subject == "/family"
    assert refusal.remedy


def test_every_unmet_predicate_is_reported_and_not_only_the_first():
    """A person who repairs one requirement must not rediscover the next."""
    verdict = _verdict(
        template=_predicates(family="line"),
        result=_facts(members=(_CLICKS_ID,), pinned=_pinned(grain=None)),
    )
    assert verdict.state == tc.INCOMPATIBLE
    assert _codes(verdict) == ["missing_grain", "missing_role"]
    assert all(refusal.message and refusal.remedy for refusal in verdict.unmet)


def test_returning_only_the_first_failure_would_be_a_failure_here():
    verdict = _verdict(
        template=_predicates(family="line"),
        result=_facts(members=(_CLICKS_ID,), pinned=_pinned(grain=None)),
    )
    assert len(verdict.unmet) > 1


def test_the_wire_shape_carries_no_boolean_at_all():
    """AC9's "never a bare boolean", enforced on the shape a caller receives."""
    for verdict in (_verdict(), _verdict(members=(_CLICKS_ID,)), _verdict(rows=None)):
        payload = verdict.as_dict()
        assert payload["state"] in {tc.COMPATIBLE, tc.INCOMPATIBLE, tc.UNAVAILABLE}
        assert not any(isinstance(value, bool) for value in payload.values())
        assert "compatible" not in payload


def test_every_unmet_predicate_carries_a_pointer_into_the_template_document():
    verdict = _verdict(members=(_CLICKS_ID,), rows=[])
    assert verdict.state == tc.INCOMPATIBLE
    for refusal in verdict.unmet:
        assert refusal.subject and refusal.subject.startswith("/")


# ---------------------------------------------------------------------------
# AC10 -- the server composes the sentence, and it names a NAME.
# ---------------------------------------------------------------------------


def test_a_cardinality_verdict_names_the_member_by_its_name_never_its_identifier():
    verdict = _verdict(rows=_rows(channels=40, count=40))
    assert verdict.state == tc.INCOMPATIBLE
    assert _codes(verdict) == ["cardinality_over_limit"]
    (refusal,) = verdict.unmet
    assert "Channel" in refusal.message
    assert "40 values" in refusal.message
    assert _CHANNEL_ID not in refusal.message
    assert refusal.subject == "/requires/dimension/max_cardinality"


def test_no_sentence_of_any_verdict_leaks_a_product_identifier():
    """The identity fields carry ids; the SENTENCES never do."""
    verdicts = [
        _verdict(members=(_CLICKS_ID,)),
        _verdict(rows=_rows(channels=40, count=40)),
        _verdict(schema={"fields": []}),
        _verdict(rows=None),
    ]
    for verdict in verdicts:
        sentences = [r.message + " " + (r.remedy or "") for r in verdict.unmet]
        if verdict.unreadable is not None:
            sentences.append(verdict.unreadable.message + " " + (verdict.unreadable.remedy or ""))
        assert sentences
        for sentence in sentences:
            assert _CHANNEL_ID not in sentence
            assert _CLICKS_ID not in sentence
            assert "res_example" not in sentence
            assert "vtv_example" not in sentence


def test_every_reason_of_every_state_carries_a_remedy():
    """A screen has nothing left to compose, which is what keeps it off the browser."""
    for verdict in (
        _verdict(members=(_CLICKS_ID,)),
        _verdict(schema={"fields": []}),
        _verdict(rows=None),
        _verdict(outcome="unavailable"),
    ):
        reasons = list(verdict.unmet) + ([verdict.unreadable] if verdict.unreadable else [])
        assert reasons
        for reason in reasons:
            assert reason.remedy


# ---------------------------------------------------------------------------
# AC11 -- three states, and "unreadable" is never "incompatible".
# ---------------------------------------------------------------------------


def test_a_result_with_no_schema_is_unavailable_and_not_incompatible():
    verdict = _verdict(schema={"fields": []})
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unmet == ()
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "result_unreadable"
    assert "declares no schema" in verdict.unreadable.message


def test_a_result_that_never_answered_is_unavailable_and_not_incompatible():
    for outcome in ("refused", "unavailable", "empty"):
        verdict = _verdict(outcome=outcome)
        assert verdict.state == tc.UNAVAILABLE, outcome
        assert verdict.unmet == ()


def test_a_result_whose_fields_carry_no_role_is_unavailable():
    """A role is never derived from a field's name -- so it is UNKNOWN, not absent."""
    verdict = _verdict(pinned=_pinned(roles={}, labels={}))
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert "no role can be read" in verdict.unreadable.message


def test_a_template_the_shipped_grammar_refuses_is_unavailable_and_not_incompatible():
    """A Visualization Spec document sent as a template: unreadable, not a verdict."""
    spec_document = {
        "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": 1,
        "family": "bar",
        "bindings": {"measure": [_CLICKS_ID], "dimension": [_CHANNEL_ID]},
    }
    unreadable = tc.predicates_of(spec_document, version_id="vtv_example")
    assert isinstance(unreadable, tc.Unreadable)
    verdict = tc.check_template_compatibility(unreadable, _facts())
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "template_unreadable"
    assert verdict.unmet == ()


def test_a_cardinality_that_could_not_be_counted_is_unavailable_and_not_compatible():
    """Rows unread is not zero distinct values."""
    verdict = _verdict(rows=None)
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "cardinality_not_counted"
    assert "Dimension" in verdict.unreadable.message
    assert "not over the limit" in verdict.unreadable.message


def test_a_predicate_with_no_cardinality_bound_needs_no_rows():
    """Only a bound needs counting; a template without one answers without rows."""
    verdict = _verdict(
        template=_predicates(
            family="kpi",
            requires={"measure": {"min": 1, "max": 1}},
            answers_question="What is the figure right now?",
        ),
        result=_facts(rows=None),
    )
    assert verdict.state == tc.COMPATIBLE


def test_a_measured_failure_outranks_a_predicate_that_could_not_be_measured():
    """Knowing something beats knowing nothing; the named failure is the answer."""
    verdict = _verdict(members=(_CLICKS_ID,), rows=None)
    assert verdict.state == tc.INCOMPATIBLE
    assert _codes(verdict) == ["missing_role"]


def test_a_requirement_its_own_family_cannot_hold_is_unavailable_not_incompatible():
    """The state `predicates_of` exists to prevent, and what happens if it is bypassed.

    A hand-built predicate set can require a well the family has not. Judging it
    against a Result would blame the Result for a template defect, so the verdict
    says the requirement could not be judged -- and it NAMES the well rather than
    skipping it in silence.
    """
    handmade = tc.TemplatePredicates(
        version_id="vtv_example",
        family="kpi",
        answers_question="What is the figure right now?",
        requires={"measure": {"min": 1}, "series": {"min": 1}},
        content_hash="0" * 64,
    )
    verdict = tc.check_template_compatibility(handmade, _facts())
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "template_unreadable"
    assert "Series" in verdict.unreadable.message


def test_an_unreadable_template_and_an_unreadable_result_answer_the_template_first():
    unreadable_result = _facts(schema={"fields": []})
    assert isinstance(unreadable_result, tc.Unreadable)
    verdict = tc.check_template_compatibility(
        tc.Unreadable(VisualizationRefusal("template_unreadable", "unreadable", None, "fix it")),
        unreadable_result,
    )
    assert verdict.state == tc.UNAVAILABLE
    assert verdict.unreadable is not None
    assert verdict.unreadable.code == "template_unreadable"


# ---------------------------------------------------------------------------
# The mutations. A verdict that never moves proves nothing.
# ---------------------------------------------------------------------------


def test_tightening_a_requirement_flips_the_verdict_and_names_what_was_tightened():
    loose = _verdict()
    assert loose.state == tc.COMPATIBLE

    tightened = _verdict(
        template=_predicates(
            requires={
                "measure": {"min": 2, "max": 3},
                "dimension": {"min": 1, "max": 1, "max_cardinality": 12},
            }
        )
    )
    assert tightened.state == tc.INCOMPATIBLE
    (refusal,) = tightened.unmet
    assert refusal.code == "missing_role"
    assert refusal.subject == "/requires/measure/min"
    assert "2 measure members" in refusal.message
    assert "only 1 (Clicks)" in refusal.message


def test_tightening_a_cardinality_bound_flips_the_verdict_and_names_the_bound():
    rows = _rows(channels=6, count=12)
    assert _verdict(rows=rows).state == tc.COMPATIBLE

    tightened = _verdict(
        template=_predicates(
            requires={
                "measure": {"min": 1, "max": 1},
                "dimension": {"min": 1, "max": 1, "max_cardinality": 2},
            }
        ),
        result=_facts(rows=rows),
    )
    assert tightened.state == tc.INCOMPATIBLE
    (refusal,) = tightened.unmet
    assert refusal.code == "cardinality_over_limit"
    assert "at most 2 distinct values" in refusal.message
    assert "Channel (6 values)" in refusal.message


def test_the_family_registry_and_not_this_file_decides_whether_a_grain_is_needed():
    """Same Result, same predicates, two families: only the registry moved."""
    without_grain = _facts(pinned=_pinned(grain=None))
    assert tc.check_template_compatibility(_predicates(), without_grain).state == tc.COMPATIBLE
    line = tc.check_template_compatibility(_predicates(family="line"), without_grain)
    assert line.state == tc.INCOMPATIBLE
    assert _codes(line) == ["missing_grain"]


def test_a_role_losing_its_server_source_moves_the_verdict_and_names_the_owner(monkeypatch):
    """The registry says which roles have a source; this file holds no second copy."""
    assert _verdict().state == tc.COMPATIBLE
    monkeypatch.setattr(tc, "AVAILABLE_ROLES", frozenset({"measure"}))
    verdict = _verdict()
    assert verdict.state == tc.INCOMPATIBLE
    (refusal,) = verdict.unmet
    assert refusal.code == "role_unavailable"
    assert refusal.remedy and ROLE_UNAVAILABLE_OWNER in refusal.remedy


def test_a_well_the_template_leaves_silent_is_still_judged_from_its_family():
    """A family that gains a required well by migration judges an older template.

    `table` requires a Dimension; this template says nothing about one. The
    family's own record is the predicate, and a Result with no dimension fails
    it by name rather than passing because the document was silent.
    """
    silent = tc.TemplatePredicates(
        version_id="vtv_example",
        family="table",
        answers_question="What does the whole answer look like?",
        requires={"measure": {"min": 1}},
        content_hash="0" * 64,
    )
    verdict = tc.check_template_compatibility(silent, _facts(members=(_CLICKS_ID,)))
    assert verdict.state == tc.INCOMPATIBLE
    (refusal,) = verdict.unmet
    assert refusal.code == "missing_role"
    assert refusal.subject == "/requires/dimension/min"


# ---------------------------------------------------------------------------
# One judge for two objects.
# ---------------------------------------------------------------------------


def test_this_file_names_neither_family_flag():
    """The grain and comparison predicates are read, never restated.

    `check_grain_and_comparison` is the one judge. If this module ever spelled
    `requires_time_grain` it would be a second reading of the registry, and the
    two would answer differently the first time either was touched -- the exact
    defect `test_template_grammar_has_one_authority.py` guards for the grammar.
    """
    source = _MODULE.read_text(encoding="utf-8")
    body = source.split('"""', 2)[2]
    assert "requires_time_grain" not in body
    assert "requires_comparison" not in body
    assert "check_grain_and_comparison" in body


def test_a_family_predicate_this_file_cannot_phrase_still_travels():
    """A judge that grows a third predicate must not lose it in translation."""
    unknown = VisualizationRefusal(
        "missing_projection", "the judge's own words", "/family", "do it"
    )
    assert tc._in_the_words_of_both_objects(unknown, get_family("bar")) is unknown


def test_the_cardinality_count_reads_the_column_the_rows_are_keyed_by():
    """AI-337, server side: a binding names a member, a row is keyed by a column."""
    facts = _facts(rows=_rows(channels=5, count=10))
    assert isinstance(facts, tc.ResultFacts)
    assert facts.distinct_values == {_CLICKS_ID: 10, _CHANNEL_ID: 5}
    (channel,) = [m for m in facts.members if m.member_id == _CHANNEL_ID]
    assert channel.column == "channel"
    assert channel.label == "Channel"


# ---------------------------------------------------------------------------
# AC12 -- derived, never stored.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("statement", ["INSERT", "UPDATE", "DELETE", "CREATE", "MERGE"])
def test_the_verdict_is_derived_because_this_module_only_reads(statement):
    source = _MODULE.read_text(encoding="utf-8")
    assert not re.search(rf"\b{statement}\b\s+(INTO|TABLE|FROM|app\.)", source, re.IGNORECASE)


def test_no_verdict_object_carries_a_field_that_could_be_persisted():
    """There is no `computed_at`, no `cached`, no id of a stored verdict.

    A verdict with an identity is a verdict somebody will store. This one is a
    value about two other objects and carries only their identities.
    """
    verdict = _verdict()
    assert set(verdict.as_dict()) == {
        "state",
        "template_version_id",
        "result_id",
        "family",
        "answers_question",
        "unmet",
        "unreadable",
    }
