"""The formula-parity verdict is COMPOSED by the read model, not only by a script.

THE DEFECT THIS FILE CLOSES, measured 2026-08-31.
`scripts/check_metric_formula_parity.py --gate` exits 0 on 3 declared ratios with
0 divergence, and had been confronting the three copies of a ratio's formula --
`dbt/seeds/dim_metric.csv`, `dbt/models/marts/semantic_<metric>.sql`,
`app.semantic_concept_versions.expression` -- since 2026-08-16. Its verdict was
visible to whoever ran the script and to nobody else:
`governance_read_model._semantic_concept` composed no flag and
`ui/admin/src/governance/SemanticModelTabs.tsx` rendered none. A Project that
governs `roas` on its own components could therefore read the Semantic Model all
day without being told that the delivered table its numbers come from does not
implement that formula.

ONE RULE, TWO READERS. The verdict lives in
`core.platform_semantic_concepts.formula_parity`, projected from the SAME seed
the script reads; the script imports it and keeps only its own printed
sentences. A second comparison written in the read model would have been exactly
the defect the gate exists to catch -- one formula, written twice.

WHY THE RULE MOVED OUT OF THE SCRIPT. `scripts/` is not in the deployment image
(`infra/docker/mcp-server/Dockerfile` copies `server/` and `dbt/`), so a runtime
reader importing the script would have crashed in production and nowhere else.
"""

from __future__ import annotations

from core import governance_read_model as read_model
from core import platform_semantic_concepts as referential

_RATIO_NAMES = ("cpa", "ctr", "roas")


def _ratio(numerator: str, denominator: str) -> dict:
    return {
        "op": "ratio",
        "zero_denominator": "null",
        "numerator": {"op": "concept_name", "name": numerator},
        "denominator": {"op": "concept_name", "name": denominator},
    }


def _concept(**overrides) -> dict:
    row = {
        "id": "sc_01EXAMPLE0000000000000000",
        "name": "roas",
        "label": "Roas",
        "kind": "metric",
        "project_id": "proj_EXAMPLE",
        "lifecycle_status": "published",
        "expression": _ratio("revenue", "cost"),
    }
    row.update(overrides)
    return row


def _verdict(**overrides):
    item = read_model._semantic_concept(
        _concept(**overrides),
        project_id="proj_EXAMPLE",
        operand_names=overrides.pop("_operand_names", None),
    )
    return item["summary"]["formula_parity"]


# ---------------------------------------------------------------------------
# The rule itself -- pure, and the one the gate reads.
# ---------------------------------------------------------------------------


def test_the_three_wired_ratios_are_the_ones_the_delivered_catalogue_names():
    """A regression that EMPTIES the seed would make every test below vacuous."""
    assert referential.declared_ratios() == {
        "cpa": ("cost", "conversions"),
        "ctr": ("clicks", "impressions"),
        "roas": ("revenue", "cost"),
    }


def test_a_concept_the_catalogue_does_not_declare_has_no_verdict():
    """`None` means "nothing to confront", and it is the case of almost every
    Concept. A "nothing to report" badge on each of them teaches a reader to
    stop looking at the one that matters."""
    assert (
        referential.formula_parity(
            name="a_metric_of_this_project",
            project_id="proj_EXAMPLE",
            expression=_ratio("revenue", "cost"),
        )
        is None
    )


def test_a_formula_that_is_not_a_ratio_has_no_verdict():
    assert (
        referential.formula_parity(
            name="roas",
            project_id="proj_EXAMPLE",
            expression={"op": "source_measure", "concept": "roas"},
        )
        is None
    )


def test_a_concept_ref_operand_is_resolved_through_the_version_table():
    """A PUBLISHED ratio pins exact versions; reading only `concept_name` would
    make the verdict blind to every formula that actually serves."""
    resolved = referential.formula_parity(
        name="roas",
        project_id="proj_EXAMPLE",
        expression={
            "op": "ratio",
            "numerator": {"op": "concept_ref", "concept_id": "sc_r", "version_id": "scv_r"},
            "denominator": {"op": "concept_ref", "concept_id": "sc_c", "version_id": "scv_c"},
        },
        names_by_version_id={"scv_r": "revenue", "scv_c": "cost"},
    )
    assert resolved["verdict"] == referential.PARITY_ALIGNED


def test_an_operand_this_read_cannot_name_admits_it_rather_than_passing():
    """"Not compared" is neither green nor a fault -- it is an admission, and
    reporting it as `aligned` would be the worst of the three answers."""
    unreadable = referential.formula_parity(
        name="roas",
        project_id="proj_EXAMPLE",
        expression={
            "op": "ratio",
            "numerator": {"op": "concept_ref", "concept_id": "sc_r", "version_id": "scv_old"},
            "denominator": {"op": "concept_name", "name": "cost"},
        },
        names_by_version_id={},
    )
    assert unreadable["verdict"] == referential.PARITY_UNREADABLE
    assert unreadable["governed"] is None
    assert "pin each operand" in unreadable["message"]


def test_the_two_negative_verdicts_do_not_say_the_same_thing():
    """A Project has the RIGHT to its own definition; the platform catalogue
    contradicting itself is a fault. One alert language, two meanings."""
    override = referential.formula_parity(
        name="roas", project_id="proj_EXAMPLE", expression=_ratio("revenue", "spend")
    )
    divergence = referential.formula_parity(
        name="roas", project_id=None, expression=_ratio("revenue", "spend")
    )
    assert override["verdict"] == referential.PARITY_PROJECT_OVERRIDE
    assert divergence["verdict"] == referential.PARITY_PLATFORM_DIVERGENCE
    # Each refusal names a gesture, never only the cause.
    assert "semantic_roas` does not implement it" in override["message"]
    assert "Publish a new version" in divergence["message"]


# ---------------------------------------------------------------------------
# The read model carries it -- which is the half nothing did.
# ---------------------------------------------------------------------------


def test_the_read_model_carries_the_aligned_verdict():
    verdict = _verdict()
    assert verdict["verdict"] == referential.PARITY_ALIGNED
    assert verdict["declared"] == "revenue / cost"
    assert verdict["governed"] == "revenue / cost"


def test_the_read_model_carries_an_override_with_both_formulas():
    verdict = _verdict(expression=_ratio("revenue", "spend"))
    assert verdict["verdict"] == referential.PARITY_PROJECT_OVERRIDE
    assert verdict["governed"] == "revenue / spend"


def test_the_read_model_calls_a_platform_contradiction_by_its_name():
    verdict = _verdict(project_id=None, expression=_ratio("revenue", "clicks"))
    assert verdict["verdict"] == referential.PARITY_PLATFORM_DIVERGENCE


def test_the_read_model_carries_nothing_for_a_concept_with_no_formula():
    assert _verdict(name="cost", expression=None) is None


def test_every_declared_ratio_is_judged_by_the_read_model():
    """Not only `roas`: a guard that names the metric of the day goes silent for
    the next one."""
    for name in _RATIO_NAMES:
        numerator, denominator = referential.declared_ratios()[name]
        item = read_model._semantic_concept(
            _concept(name=name, expression=_ratio(numerator, denominator)),
            project_id="proj_EXAMPLE",
        )
        assert item["summary"]["formula_parity"]["verdict"] == referential.PARITY_ALIGNED, name


def test_the_version_to_name_table_is_built_from_the_rows_already_loaded():
    """No second query: the lens composes it from `load_concepts`'s own rows."""
    table = read_model._concept_name_by_version_id(
        [
            {"id": "sc_a", "name": "revenue", "current_version_id": "scv_a"},
            {"id": "sc_b", "name": "cost", "current_version_id": "scv_b"},
            # A Concept with no published version contributes nothing rather
            # than a `None` key that would resolve an operand to nothing.
            {"id": "sc_c", "name": "draft_only", "current_version_id": None},
        ]
    )
    assert table == {"scv_a": "revenue", "scv_b": "cost"}
