"""Story 60.2 -- a calculated field declares its additivity, and the render reads it.

Three things are held in place here, and they are not the same thing:

1. THE NAMED REFUSALS. `validate_aggregation` already carried them; what was
   missing is a test that pins the CODE and the JSON `path` of each, so a later
   rewording of a message cannot quietly rename the code the console displays.
   No code is invented in this file: every string asserted below is read back
   from `core.semantic_expressions`.

2. THE THREE CLASSES ARE THREE. `additive` / `semi_additive` / `non_additive` is
   what migration 142 writes and what `ADDITIVITY_CLASSES` enforces. The epic plan
   speaks of "Additive / Non-Additive Ratio / Non-Additive Snapshot": those are
   CASES, not classes -- a snapshot is `semi_additive` plus the dimensions it may
   not cross. A fourth vocabulary in a repository that already had three is the
   defect, not the feature.

3. THE RENDER READS THE DECLARATION. This is the half that did not exist. A
   client metric declared `non_additive` was summed anyway, because two frozensets
   of four literal names and one name-suffix rule decided the question. The metric
   used here is deliberately named so that `is_ratio_name` says NO: a test whose
   metric is called `custom_rate` proves the heuristic, not the declaration.
"""

from __future__ import annotations

import pytest
from core.metric_semantics import (
    ADDITIVITY_CLASSES,
    declared_non_additive,
    is_ratio_name,
)
from core.semantic_expressions import (
    ADDITIVITY_CLASSES as CONTRACT_CLASSES,
)
from core.semantic_expressions import (
    ConceptResolver,
    validate_aggregation,
    validate_expression,
)

#: A client ratio whose NAME carries no ratio token. `is_ratio_name` answers False
#: for it, `cards._NON_ADDITIVE_METRICS` and `rollup._NON_ADDITIVE_METRICS` do not
#: contain it, and `_RATIO_SUFFIX_RE` does not match it. Everything this metric is
#: refused for, it is refused for because someone DECLARED it.
UNDECLARABLE_BY_NAME = "efficiency_index"


def _codes(refusals) -> list[str]:
    return [refusal.code for refusal in refusals]


def _by_code(refusals, code: str):
    return next(refusal for refusal in refusals if refusal.code == code)


def _resolver(**versions) -> ConceptResolver:
    return ConceptResolver({tuple(key.split("@")): value for key, value in versions.items()})


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_the_render_and_the_contract_name_the_same_three_classes():
    # Two modules must agree or a metric declared in one is unreadable by the
    # other. Asserted against the modules, not against a copied literal.
    assert ADDITIVITY_CLASSES == CONTRACT_CLASSES
    assert ADDITIVITY_CLASSES == {"additive", "semi_additive", "non_additive"}


def test_the_metric_this_file_uses_is_invisible_to_the_name_heuristic():
    # If this ever starts matching, every assertion below stops proving what it
    # says it proves -- the guard would fire on the NAME and the declaration
    # would never be consulted.
    assert is_ratio_name(UNDECLARABLE_BY_NAME) is False
    assert is_ratio_name("custom_rate") is True  # the contrast, on purpose


# ---------------------------------------------------------------------------
# The refusals, by exact code and exact path
# ---------------------------------------------------------------------------


def test_a_ratio_declared_additive_is_refused_by_name():
    refusals = validate_aggregation(
        {"function": "average"},
        additivity_class="additive",
        non_additive_dimensions=None,
        value_type="ratio",
    )
    assert _codes(refusals) == ["additivity_contradiction"]
    contradiction = _by_code(refusals, "additivity_contradiction")
    assert contradiction.path == "$.additivity_class"
    assert "additive" in contradiction.message


def test_summing_a_ratio_is_unsafe_sum_and_says_where():
    refusals = validate_aggregation(
        {"function": "sum"},
        additivity_class="additive",
        non_additive_dimensions=None,
        value_type="ratio",
    )
    assert "unsafe_sum" in _codes(refusals)
    assert _by_code(refusals, "unsafe_sum").path == "$.aggregation.function"
    # `sum` IS one of the additive aggregations, so the contradiction is the
    # value type, not the function: only one refusal fires.
    assert "additivity_contradiction" not in _codes(refusals)


def test_a_ratio_expression_declared_additive_is_refused_at_the_expression_too():
    # The other half of the same story: the refusal exists on the EXPRESSION path
    # as well, so a formula that sums a ratio cannot slip through by declaring a
    # harmless aggregation on the metric.
    analysis = validate_expression(
        {
            "op": "aggregate",
            "function": "sum",
            "operand": {
                "op": "ratio",
                "zero_denominator": "null",
                "numerator": {"op": "literal", "value_type": "decimal", "value": 1},
                "denominator": {"op": "literal", "value_type": "decimal", "value": 2},
            },
        },
        _resolver(),
    )
    assert _codes(analysis.refusals) == ["unsafe_sum"]
    assert analysis.refusals[0].path == "$"
    assert not analysis.ok


def test_semi_additive_without_its_dimensions_is_refused_by_name():
    refusals = validate_aggregation(
        {"function": "sum"},
        additivity_class="semi_additive",
        non_additive_dimensions=[],
        value_type="decimal",
    )
    assert _codes(refusals) == ["undeclared_non_additive_dimensions"]
    assert refusals[0].path == "$.non_additive_dimensions"


def test_semi_additive_that_names_its_dimensions_passes():
    # The Non-Additive Snapshot case of the plan, expressed in the three classes
    # the schema has: a balance is summable across products and never across days.
    assert (
        validate_aggregation(
            {"function": "sum"},
            additivity_class="semi_additive",
            non_additive_dimensions=["date"],
            value_type="decimal",
        )
        == []
    )


def test_neither_an_aggregation_nor_a_class_is_refused_by_name():
    refusals = validate_aggregation(
        None,
        additivity_class=None,
        non_additive_dimensions=None,
        value_type="decimal",
    )
    assert _codes(refusals) == ["undeclared_aggregation"]
    assert refusals[0].path == "$.aggregation"


def test_an_aggregation_without_a_class_is_refused_by_the_application():
    """The gap migration 237 closed in the schema, closed in the application too.

    This function used to return the moment it saw an aggregation, so
    `{"function": "sum"}` with no class was accepted. That is the row the
    constraint now refuses — and a constraint reached at INSERT time surfaces as
    `CheckViolation`, which `semantic_model_api.py:131-138` turns into a 503
    `semantic_model_unavailable`. A person told "the service is unavailable" goes
    to look at the infrastructure for a field they did not fill.
    """
    refusals = validate_aggregation(
        {"function": "sum"},
        additivity_class=None,
        non_additive_dimensions=None,
        value_type="decimal",
    )
    assert _codes(refusals) == ["undeclared_aggregation"]
    # The PATH is what distinguishes it from the no-aggregation-at-all case above,
    # which points at `$.aggregation`. A form needs to know which field to mark.
    assert refusals[0].path == "$.additivity_class"
    assert "sum" in refusals[0].message


def test_declaring_the_class_is_all_it_takes_to_pass():
    # Same metric, one field filled: no refusal. A guard that refused something
    # else as well would be refusing more than it announced.
    assert (
        validate_aggregation(
            {"function": "sum"},
            additivity_class="additive",
            non_additive_dimensions=None,
            value_type="decimal",
        )
        == []
    )


def test_non_additive_needs_no_aggregation_and_that_is_the_only_exemption():
    assert (
        validate_aggregation(
            None,
            additivity_class="non_additive",
            non_additive_dimensions=None,
            value_type="ratio",
        )
        == []
    )


def test_an_unknown_class_is_named_rather_than_coerced():
    refusals = validate_aggregation(
        {"function": "sum"},
        # The plan's vocabulary, submitted verbatim. It must be REFUSED, not
        # mapped onto a neighbouring class by a helpful reader.
        additivity_class="Non-Additive Ratio",
        non_additive_dimensions=None,
        value_type="decimal",
    )
    assert "unknown_additivity_class" in _codes(refusals)
    assert _by_code(refusals, "unknown_additivity_class").path == "$.additivity_class"


# ---------------------------------------------------------------------------
# The declaration reaches the render
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "declared,expected",
    [
        ({UNDECLARABLE_BY_NAME: "non_additive"}, {UNDECLARABLE_BY_NAME}),
        ({UNDECLARABLE_BY_NAME: "semi_additive"}, {UNDECLARABLE_BY_NAME}),
        ({UNDECLARABLE_BY_NAME: "additive"}, set()),
        ({}, set()),
    ],
)
def test_only_a_declaration_other_than_additive_stops_a_sum(declared, expected):
    assert declared_non_additive(declared) == expected


def test_semi_additive_counts_as_not_summable_here():
    # Deliberate and worth stating: `semi_additive` means "summable across SOME
    # dimensions". A roll-up that does not know WHICH cannot tell whether the one
    # in front of it is allowed, so it does not sum. The alternative is to sum and
    # hope, which is the failure the class was invented to name.
    assert declared_non_additive({"balance": "semi_additive"}) == {"balance"}
