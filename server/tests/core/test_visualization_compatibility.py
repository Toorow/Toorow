"""Story 50.4 -- AC4: the six compatibility checks, and the refusal that names them all.

Each check is exercised against the real production function, not a re-statement
of it. Two properties matter more than the individual verdicts:

  1. EVERY reason arrives at once. Returning the first failure makes a person fix
     one well, resubmit, and discover the next -- so the last test in this file
     builds a document that is wrong in five distinct ways and asserts all five.
  2. NO role is ever derived from a member's name. A member literally called
     `date` bound into the Time well is REFUSED, because the pinned Query Spec
     version says it is a dimension and says nothing about time. An invented role
     is worse than a missing well, and this file makes inventing one red.
"""

from __future__ import annotations

import pytest
from core.visualization_families import (
    ROLE_UNAVAILABLE_OWNER,
    ROLE_UNAVAILABLE_REASON,
    VISUAL_FAMILIES,
    VisualFamily,
    Well,
    get_family,
)
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    PinnedMembers,
    check_grain_and_comparison,
    check_shape_compatibility,
    evaluate_result_disclosures,
    normalize_document,
)


def pinned(*, grain: str | None = "day", comparison: str = "none") -> PinnedMembers:
    """A Query Spec version that selected two measures and two dimensions.

    `date` is deliberately among the DIMENSIONS. The compiled artifact carries no
    time role (`server/core/semantic_compiler.py:431-450`), so a member whose name
    reads like a date is a dimension and nothing more.
    """
    return PinnedMembers(
        roles={
            "clicks": "measure",
            "cost": "measure",
            "channel": "dimension",
            "date": "dimension",
        },
        labels={"clicks": "clicks", "cost": "cost", "channel": "channel", "date": "date"},
        grain=grain,
        comparison=comparison,
        row_limit=1000,
        query_spec_id="qs_example",
        semantic_view_id="sv_example",
        semantic_view_version_id="svv_example",
    )


def document(family: str = "bar", **overrides) -> dict:
    base = {
        "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": VISUALIZATION_SPEC_SCHEMA_VERSION,
        "family": family,
        "bindings": {"measure": ["clicks"], "dimension": ["channel"]},
    }
    base.update(overrides)
    normalized, refusals = normalize_document(base)
    assert refusals == [], f"the fixture itself was refused: {[r.as_dict() for r in refusals]}"
    return normalized


def verdicts(normalized: dict, family_id: str, members: PinnedMembers) -> list[dict]:
    family = get_family(family_id)
    assert family is not None
    return [r.as_dict() for r in check_shape_compatibility(normalized, family, members)]


def codes(items: list[dict]) -> list[str]:
    return [i["code"] for i in items]


# ---------------------------------------------------------------------------
# Clause 1 -- field existence.
# ---------------------------------------------------------------------------


def test_a_binding_naming_an_unselected_member_is_refused():
    spec = document(bindings={"measure": ["impressions"], "dimension": ["channel"]})
    found = verdicts(spec, "bar", pinned())
    assert "unknown_member" in codes(found)
    assert any(v["subject"] == "/bindings/measure/0" for v in found)


def test_a_valid_document_produces_no_refusal_at_all():
    assert verdicts(document(), "bar", pinned()) == []


# ---------------------------------------------------------------------------
# Clause 2 -- semantic role, and ONLY the two roles that have a source.
# ---------------------------------------------------------------------------


def test_a_measure_in_a_dimension_well_is_refused_with_role_mismatch():
    spec = document(bindings={"measure": ["clicks"], "dimension": ["cost"]})
    found = verdicts(spec, "bar", pinned())
    assert "role_mismatch" in codes(found)
    mismatch = next(v for v in found if v["code"] == "role_mismatch")
    assert "measure" in mismatch["message"]
    assert mismatch["remedy"]


def test_a_dimension_in_a_measure_well_is_refused_with_role_mismatch():
    spec = document(bindings={"measure": ["channel"], "dimension": ["channel"]})
    assert "role_mismatch" in codes(verdicts(spec, "bar", pinned()))


def test_the_time_well_refuses_every_member_because_the_role_has_no_source():
    """AC4 clause 2. The Time well is not validated against a guess, so nothing
    binds into it -- not a measure, not a dimension, and not a member called `date`."""
    for member in ("date", "channel", "clicks"):
        spec = document(
            bindings={"measure": ["clicks"], "dimension": ["channel"], "time": [member]}
        )
        found = verdicts(spec, "bar", pinned())
        assert "role_unavailable" in codes(found), member
        reason = next(v for v in found if v["code"] == "role_unavailable")
        assert ROLE_UNAVAILABLE_REASON in reason["message"]
        assert ROLE_UNAVAILABLE_OWNER in reason["remedy"]


def test_a_member_named_date_is_a_dimension_and_nothing_more():
    """Deriving a time role from a name is a TEST FAILURE, not a style preference.

    `date` binds happily into the Dimension well, because that is what the pinned
    Query Spec version says it is. It does NOT become a time member because of how
    it is spelled.
    """
    spec = document(family="line", bindings={"measure": ["clicks"], "dimension": ["date"]})
    assert verdicts(spec, "line", pinned()) == []


def test_no_source_file_derives_a_role_from_a_member_name():
    """The heuristic this story is forbidden to grow, asserted as absence."""
    import inspect

    import core.visualization_families as families
    import core.visualization_specs as specs

    for module in (specs, families):
        source = inspect.getsource(module).lower()
        for heuristic in ('endswith("_date")', "'date' in", '"date" in name', "startswith('dt_')"):
            assert heuristic not in source, f"{module.__name__} infers a role from a name"


def test_a_well_the_family_does_not_declare_refuses_its_binding():
    spec = document(family="kpi", bindings={"measure": ["clicks"], "series": ["channel"]})
    found = verdicts(spec, "kpi", pinned())
    assert "role_mismatch" in codes(found)


def test_a_required_well_left_empty_is_refused():
    spec = document(
        family="stacked_bar", bindings={"measure": ["clicks"], "dimension": ["channel"]}
    )
    found = verdicts(spec, "stacked_bar", pinned())
    assert "missing_binding" in codes(found)
    assert any(v["subject"] == "/bindings/breakdown" for v in found)


def test_too_many_members_in_one_well_is_refused():
    spec = document(family="kpi", bindings={"measure": ["clicks", "cost"]})
    assert "too_many_members" in codes(verdicts(spec, "kpi", pinned()))


# ---------------------------------------------------------------------------
# Clause 5 -- comparison and grain metadata, read off the QUERY.
# ---------------------------------------------------------------------------


def test_a_family_that_needs_a_grain_is_refused_when_the_query_carries_none():
    spec = document(family="line", bindings={"measure": ["clicks"], "dimension": ["date"]})
    found = verdicts(spec, "line", pinned(grain=None))
    assert "missing_grain" in codes(found)
    grain = next(v for v in found if v["code"] == "missing_grain")
    assert "Explore" in grain["remedy"], "the remedy must name where the query is changed"


def test_a_family_that_needs_no_grain_is_unaffected_by_its_absence():
    assert verdicts(document(family="bar"), "bar", pinned(grain=None)) == []


def test_a_family_that_needs_a_comparison_is_refused_when_the_query_carries_none():
    """No v1 family declares `requires_comparison`, and the registry docstring says
    so. The CHECK is generic over the registry, so it is proved here against a real
    `VisualFamily` record rather than against a family that does not exist -- which
    would be testing the fixture, not the code."""
    family = VisualFamily(
        id="bar",
        label="Bar",
        description="fixture",
        wells=(Well("measure", frozenset({"measure"}), True, 1, None),),
        requires_time_grain=False,
        requires_comparison=True,
        max_marks=100,
        capabilities=(),
        table_fallback_wells=("measure",),
    )
    found = [r.as_dict() for r in check_grain_and_comparison(family, pinned(comparison="none"))]
    assert codes(found) == ["missing_comparison"]
    assert not check_grain_and_comparison(family, pinned(comparison="previous_period"))


def test_no_first_release_family_requires_a_comparison():
    """Stated as a test so the registry docstring cannot drift from the registry."""
    assert [f.id for f in VISUAL_FAMILIES if f.requires_comparison] == []
    assert [f.id for f in VISUAL_FAMILIES if f.requires_time_grain] == ["line", "area"]


# ---------------------------------------------------------------------------
# Clauses 3, 4, 6 -- the SECOND tier, against one exact Result (decision D4).
# ---------------------------------------------------------------------------


def rows(count: int, distinct: int) -> list[dict]:
    return [{"channel": f"c{i % distinct}", "clicks": i} for i in range(count)]


def test_cardinality_over_the_family_limit_is_disclosed_not_persisted():
    spec = document(family="bar")
    family = get_family("bar")
    disclosures = evaluate_result_disclosures(
        spec, family, rows=rows(120, 120), row_count=120, truncated=False, outcome="success"
    )
    assert disclosures["compatible"] is False
    assert "cardinality_over_limit" in [r["code"] for r in disclosures["refusals"]]
    # The verdict is a DISCLOSURE. Nothing about it enters the stored document.
    assert "cardinalities" not in spec
    assert "returned_row_count" not in spec


def test_volume_over_the_family_limit_is_refused_with_the_mark_count():
    spec = document(
        family="bar", bindings={"measure": ["clicks", "cost"], "dimension": ["channel"]}
    )
    family = get_family("bar")
    disclosures = evaluate_result_disclosures(
        spec, family, rows=rows(300, 20), row_count=300, truncated=True, outcome="success"
    )
    volume = [r for r in disclosures["refusals"] if r["code"] == "volume_over_limit"]
    assert volume, disclosures["refusals"]
    assert "600" in volume[0]["message"]
    assert disclosures["marks"] == 600
    assert disclosures["truncated"] is True


def test_a_result_inside_every_limit_is_compatible():
    disclosures = evaluate_result_disclosures(
        document(family="bar"),
        get_family("bar"),
        rows=rows(40, 8),
        row_count=40,
        truncated=False,
        outcome="success",
    )
    assert disclosures["compatible"] is True
    assert disclosures["refusals"] == []


def test_a_top_n_larger_than_the_returned_rows_is_refused():
    spec = document(family="bar", top_n={"n": 50, "display_only": True})
    disclosures = evaluate_result_disclosures(
        spec,
        get_family("bar"),
        rows=rows(10, 5),
        row_count=10,
        truncated=False,
        outcome="success",
    )
    codes_found = [r["code"] for r in disclosures["refusals"]]
    assert "truncation_not_disclosed" in codes_found


def test_the_disclosures_always_restate_the_table_fallback_obligation():
    for family in VISUAL_FAMILIES:
        disclosures = evaluate_result_disclosures(
            document(family="bar"),
            family,
            rows=[],
            row_count=0,
            truncated=False,
            outcome="empty",
        )
        assert disclosures["table_fallback"] == "required"
        assert disclosures["table_fallback_columns"], family.id


# ---------------------------------------------------------------------------
# AC8 -- the class-level assertion over EVERY family.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", VISUAL_FAMILIES, ids=[f.id for f in VISUAL_FAMILIES])
def test_every_family_declares_non_empty_table_fallback_columns(family):
    """Testing one family would be repairing the instance shown (CLAUDE.md §4)."""
    assert family.table_fallback_wells, f"{family.id} cannot name its fallback columns"
    declared = {w.name for w in family.wells}
    for well in family.table_fallback_wells:
        assert well in declared, f"{family.id} derives a fallback column from a well it lacks"
    assert family.as_dict()["table_fallback"] == "required"


@pytest.mark.parametrize("family", VISUAL_FAMILIES, ids=[f.id for f in VISUAL_FAMILIES])
def test_no_family_claims_more_marks_than_the_platform_returns(family):
    from core.query_execution import MAX_INLINE_ROWS

    assert family.max_marks <= MAX_INLINE_ROWS * 5
    for well in family.wells:
        if well.max_cardinality is not None:
            assert well.max_cardinality <= MAX_INLINE_ROWS


# ---------------------------------------------------------------------------
# The property this AC turns on: EVERY reason at once.
# ---------------------------------------------------------------------------


def test_a_document_wrong_in_five_ways_reports_all_five():
    spec = document(
        family="stacked_bar",
        bindings={
            "measure": ["not_a_member"],  # 1. unknown_member
            "dimension": ["cost"],  # 2. role_mismatch (a measure here)
            "time": ["date"],  # 3. role_unavailable
            # 4. breakdown is REQUIRED on stacked_bar and is absent
        },
        thresholds=[{"member_id": "ga4_sessions", "comparator": "gt", "value": 1}],  # 5.
    )
    found = verdicts(spec, "stacked_bar", pinned())
    assert {"unknown_member", "role_mismatch", "role_unavailable", "missing_binding"} <= set(
        codes(found)
    )
    assert len([v for v in found if v["code"] == "unknown_member"]) == 2
    # Each refusal is attached to the control it is about, not to one banner.
    assert len({v["subject"] for v in found}) == len(found)


def test_returning_only_the_first_failure_would_be_a_failure_here():
    spec = document(bindings={"measure": ["nope1"], "dimension": ["nope2"]})
    found = verdicts(spec, "bar", pinned())
    assert len([v for v in found if v["code"] == "unknown_member"]) == 2
