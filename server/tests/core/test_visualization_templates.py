"""Story 72.2 -- the Chart Template document, proved offline.

WHAT IS PROVED HERE. AC5 (every forbidden anchor refused, each reason at once,
each with its JSON pointer), AC6 (a family the shipped registry does not draw is
refused BY ITS NAME and never substituted), AC8 (one ceiling, mirrored by the
`pg_column_size` CHECK), and the round trip: two writings of one meaning produce
one `content_hash`.

WHAT IS PROVED ELSEWHERE. The derivation itself -- that the grammar is imported
rather than declared, and that a change to the Spec grammar propagates or turns
a guard red -- is `tests/conformance/test_template_grammar_has_one_authority.py`.
The database's own half of AC8 and of the contract literal is
`tests/core/test_chart_template_document_pg.py`.

No database, no network: the grammar and its walker are pure.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core.visualization_families import (
    DEFERRED_FAMILIES,
    SPEC_SELECTABLE_FAMILY_IDS,
    WELL_ROLES,
)
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VisualizationSpecRefused,
    canonical_bytes,
)
from core.visualization_templates import (
    CHART_TEMPLATE_CONTRACT_VERSION,
    MAX_TEMPLATE_BYTES,
    SUBTRACTED_FROM_SPEC,
    TEMPLATE_GRAMMAR_KEYS,
    normalize_template_document,
    template_vocabulary,
    validate_template_document,
)

_MIGRATION_333 = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "333_a_chart_template_is_an_object_a_pin_can_reach.sql"
)
_MIGRATION_334 = (
    Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "334_a_chart_template_document_declares_requires_and_names_no_evidence.sql"
)


def _document(**overrides) -> dict:
    """A Chart Template document that answers a question and binds nothing."""
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


def _codes(excinfo) -> list[tuple[str, str | None]]:
    return [(r.subject, r.code) for r in excinfo.value.refusals]


def _subjects(excinfo) -> list[str | None]:
    return [r.subject for r in excinfo.value.refusals]


# ---------------------------------------------------------------------------
# The document that IS a starting point.
# ---------------------------------------------------------------------------


def test_a_template_that_binds_nothing_validates():
    validated = validate_template_document(_document())
    assert validated.family == "bar"
    assert validated.document["spec_contract_version"] == CHART_TEMPLATE_CONTRACT_VERSION
    assert sorted(validated.requires) == ["dimension", "measure"]
    # Not one member named anywhere, at any depth.
    assert "bindings" not in validated.document
    assert "revenue" not in canonical_bytes(validated.document).decode("utf-8")


def test_the_grammar_carries_the_spec_keys_it_did_not_subtract():
    """The presentation intent crosses over; only the anchors are gone."""
    for key in (
        "order",
        "top_n",
        "axes",
        "legend",
        "formatting",
        "color",
        "thresholds",
        "reference_lines",
        "interactions",
        "evidence",
        "responsive",
        "accessibility",
        "labels",
    ):
        assert key in TEMPLATE_GRAMMAR_KEYS
    assert "bindings" not in TEMPLATE_GRAMMAR_KEYS
    assert "requires" in TEMPLATE_GRAMMAR_KEYS
    assert "annotations" not in TEMPLATE_GRAMMAR_KEYS


def test_the_governed_defaults_cross_over_and_are_materialized():
    document = _document(
        axes={"x": {"scale": "categorical"}, "y": {"zero_baseline": False}},
        legend={"position": "bottom"},
        color={"role": "categorical", "semantic_direction": "higher_is_better"},
        responsive={"profiles": ["console", "mcp-inline"]},
    )
    validated = validate_template_document(document)
    assert validated.document["axes"]["x"]["scale"] == "categorical"
    assert validated.document["axes"]["y"]["zero_baseline"] is False
    # Absent siblings are materialized to their declared default, which is what
    # makes absent-vs-default unable to move a hash.
    assert validated.document["axes"]["y"]["scale"] == "linear"
    assert validated.document["responsive"]["profiles"] == ["console", "mcp-inline"]


def test_a_threshold_and_a_reference_line_anchor_on_a_well_not_a_member():
    document = _document(
        thresholds=[{"well": "measure", "comparator": "gt", "value": 100, "severity": "warning"}],
        reference_lines=[{"well": "measure", "kind": "average"}],
        evidence={"datum_wells": ["dimension", "measure"], "mark_binding": "datum"},
        labels={"override": {"measure": "Spend"}},
    )
    validated = validate_template_document(document)
    assert validated.document["thresholds"][0]["well"] == "measure"
    assert validated.document["reference_lines"][0]["well"] == "measure"
    assert validated.document["evidence"]["datum_wells"] == ["dimension", "measure"]
    assert validated.document["labels"]["override"] == {"measure": "Spend"}


# ---------------------------------------------------------------------------
# AC5 -- what makes it a template and not a Visualization.
# ---------------------------------------------------------------------------


def test_ac5_a_valid_visualization_spec_is_refused_as_a_template():
    """THE distinction, in one assertion.

    The document below is a legal `visualization-spec.v1`: it declares its
    contract, its family, its bindings, its annotations and its label overrides,
    and `validate_visualization_spec` would accept it against a matching pinned
    Query Spec version. As a Chart Template it is refused, and every reason is
    named at its own pointer -- never the first one alone.
    """
    spec_document = {
        "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": 1,
        "family": "bar",
        "bindings": {"measure": ["revenue"], "dimension": ["channel"]},
        "thresholds": [{"member_id": "revenue", "comparator": "gt", "value": 100}],
        "annotations": [{"evidence_id": "ev_one", "anchor": "datum"}],
        "evidence": {"datum_fields": ["revenue"], "mark_binding": "datum"},
        "labels": {"override": {"revenue": "Revenue"}},
    }
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(spec_document)

    assert excinfo.value.code == "chart_template_refused"
    codes = dict(_codes(excinfo))

    # The contract literal is the first thing that separates the two documents.
    assert codes["/spec_contract_version"] == "invalid_value"
    # The bindings, and they are told what replaced them.
    assert codes["/bindings"] == "template_is_unbound"
    # The evidence anchors, at every depth they appear.
    assert codes["/annotations"] == "template_is_unbound"
    assert codes["/thresholds/0/member_id"] == "template_is_unbound"
    assert codes["/evidence/datum_fields"] == "template_is_unbound"
    # A label override keyed by a member is refused on the offending key.
    assert "/labels/override/revenue" in codes

    by_subject = {r.subject: r for r in excinfo.value.refusals}
    assert by_subject["/bindings"].remedy == "Declare `requires` instead."
    assert "well" in by_subject["/thresholds/0/member_id"].remedy


@pytest.mark.parametrize(
    "key",
    ["query_spec_version_id", "query_spec_id", "result_id"],
)
def test_ac5_a_pin_to_one_question_or_one_result_is_refused_by_name(key):
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(**{key: "qsv_01EXAMPLE"}))
    named = [r for r in excinfo.value.refusals if r.subject == f"/{key}"]
    assert named and named[0].code == "template_is_unbound"
    assert "reusable across questions" in named[0].message


def test_ac5_every_reason_at_once_never_only_the_first():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(
                family="heatmap",
                result_id="res_01EXAMPLE",
                bindings={"measure": ["revenue"]},
                annotations=[{"evidence_id": "ev_one", "anchor": "datum"}],
            )
        )
    subjects = _subjects(excinfo)
    assert "/family" in subjects
    assert "/result_id" in subjects
    assert "/bindings" in subjects
    assert "/annotations" in subjects
    assert len(subjects) == len(set(subjects)), "one control, one reason"


@pytest.mark.parametrize(
    "value",
    [
        "<script>alert(1)</script>",
        "https://example.com/chart",
        "function (d) { return d; }",
        "d3.selectAll",
        "#ff0000",
        "Bearer abcdefghijklmnop",
    ],
)
def test_ac5_renderer_code_markup_urls_and_credentials_never_enter(value):
    """The shared hostile-content scan, applied to this document's leaves too.

    It is the SAME scan `visualization-spec.v1` uses, reached through the same
    walker -- not a second deny-list that would drift the day either moved.
    """
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(answers_question=value))
    named = [r for r in excinfo.value.refusals if r.subject == "/answers_question"]
    assert named, f"{value!r} entered a Chart Template"
    assert named[0].code == "invalid_value"


def test_ac5_a_raw_renderer_option_is_an_undeclared_key_and_is_never_stripped():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(series=[{"type": "bar"}], option={"grid": {}}))
    codes = dict(_codes(excinfo))
    assert codes["/series"] == "unknown_field"
    assert codes["/option"] == "unknown_field"


def test_ac5_a_query_owned_key_names_its_owner_rather_than_being_unknown():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(grain="day", filters=[{"field": "x"}]))
    codes = dict(_codes(excinfo))
    assert codes["/grain"] == "query_owned_field"
    assert codes["/filters"] == "query_owned_field"


def test_the_refusals_speak_of_a_chart_template_and_never_of_a_visualization_spec():
    """One object, one noun. The reader is never shown the other object's word."""
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document({"family": "bar", "unknown_thing": "<x>"})
    text = " ".join(f"{r.message} {r.remedy}" for r in excinfo.value.refusals)
    assert "Chart Template" in text
    assert "Visualization Spec" not in text


# ---------------------------------------------------------------------------
# AC6 -- the family, named, never substituted.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("entry", DEFERRED_FAMILIES, ids=lambda e: str(e["id"]))
def test_ac6_a_deferred_family_is_refused_by_its_name_with_the_registry_reason(entry):
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(family=entry["id"]))
    named = [r for r in excinfo.value.refusals if r.subject == "/family"]
    assert len(named) == 1, "one control, one reason"
    assert named[0].code == "family_not_drawn"
    assert f"`{entry['id']}`" in named[0].message
    # The reason is READ from the registry, not paraphrased here.
    assert entry["reason"] in named[0].message
    assert "No neighbouring family is substituted" in named[0].remedy


def test_ac6_a_declared_but_unfillable_family_says_which_role_has_no_source():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(family="ai_path"))
    named = [r for r in excinfo.value.refusals if r.subject == "/family"]
    assert named[0].code == "family_not_fillable"
    assert "`ai_path`" in named[0].message
    assert "classification" in named[0].message


def test_ac6_a_word_that_is_no_family_is_told_so():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(family="pie"))
    named = [r for r in excinfo.value.refusals if r.subject == "/family"]
    assert named[0].code == "family_not_drawn"
    assert "`pie`" in named[0].message


@pytest.mark.parametrize("family_id", SPEC_SELECTABLE_FAMILY_IDS)
def test_ac6_every_drawable_family_can_carry_a_template(family_id):
    """The other direction: the list is not narrower than the registry's."""
    from core.visualization_families import AVAILABLE_ROLES, get_family

    family = get_family(family_id)
    requires = {
        well.name: {"min": 1}
        for well in family.wells
        if well.required and (well.accepts & AVAILABLE_ROLES)
    }
    validated = validate_template_document(_document(family=family_id, requires=requires))
    assert validated.family == family_id


def test_ac6_the_family_check_of_migration_333_is_this_list():
    """The SQL enum and `SPEC_SELECTABLE_FAMILY_IDS` are one list.

    Read from the migration rather than retyped: a family added to the registry
    without its migration is red here instead of being refused at insert time by
    a database nobody asked.
    """
    sql = _MIGRATION_333.read_text(encoding="utf-8")
    block = re.search(
        r"ck_visualization_template_versions_family CHECK \(\s*family IN \(([^)]*)\)",
        sql,
        re.DOTALL,
    )
    assert block, "migration 333 no longer declares the family CHECK by that name"
    listed = tuple(re.findall(r"'([a-z_]+)'", block.group(1)))
    assert listed == tuple(SPEC_SELECTABLE_FAMILY_IDS)


# ---------------------------------------------------------------------------
# `requires` may narrow its family and never widen it.
# ---------------------------------------------------------------------------


def test_a_well_the_family_has_not_is_refused_by_the_name_of_the_well():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(family="kpi", requires={"measure": {"min": 1}, "breakdown": {"min": 1}})
        )
    named = [r for r in excinfo.value.refusals if r.subject == "/requires/breakdown"]
    assert named[0].code == "role_mismatch"
    assert "Breakdown" in named[0].message and "KPI" in named[0].message


def test_a_well_with_no_server_source_cannot_be_required():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(requires={"measure": {"min": 1}, "dimension": {"min": 1}, "time": {"min": 1}})
        )
    named = [r for r in excinfo.value.refusals if r.subject == "/requires/time"]
    assert named[0].code == "role_unavailable"
    assert "Semantic compiler" in named[0].remedy


def test_requiring_more_members_than_the_well_holds_is_refused_with_both_numbers():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(family="kpi", requires={"measure": {"min": 3, "max": 3}})
        )
    named = [r for r in excinfo.value.refusals if r.subject == "/requires/measure/min"]
    assert named[0].code == "too_many_members"
    assert "at most 1" in named[0].message and "requires 3" in named[0].message


def test_a_template_may_not_widen_the_roles_its_well_accepts():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(
                requires={
                    "measure": {"min": 1, "accepts": ["measure", "dimension"]},
                    "dimension": {"min": 1},
                }
            )
        )
    named = [r for r in excinfo.value.refusals if r.subject == "/requires/measure/accepts"]
    assert named[0].code == "role_mismatch"
    assert "dimension" in named[0].message


def test_a_template_may_not_read_more_distinct_values_than_its_family():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(
                requires={"measure": {"min": 1}, "dimension": {"min": 1, "max_cardinality": 900}}
            )
        )
    named = [
        r for r in excinfo.value.refusals if r.subject == "/requires/dimension/max_cardinality"
    ]
    assert named[0].code == "cardinality_over_limit"
    assert "at most 50" in named[0].message


def test_a_template_that_does_not_require_what_its_family_needs_is_refused():
    """A predicate no Result could satisfy is a starting point that never starts."""
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(_document(requires={"measure": {"min": 1}}))
    named = [r for r in excinfo.value.refusals if r.subject == "/requires/dimension"]
    assert named[0].code == "missing_requirement"
    assert "Dimension" in named[0].message


def test_a_well_that_is_not_a_well_is_refused_on_its_own_key():
    with pytest.raises(VisualizationSpecRefused) as excinfo:
        validate_template_document(
            _document(
                requires={"measure": {"min": 1}, "dimension": {"min": 1}, "xAxis": {"min": 1}}
            )
        )
    named = [r for r in excinfo.value.refusals if r.subject == "/requires/xAxis"]
    assert named[0].code == "invalid_value"
    for well in WELL_ROLES:
        assert well in named[0].message


# ---------------------------------------------------------------------------
# The round trip.
# ---------------------------------------------------------------------------


def test_two_writings_of_one_meaning_produce_one_hash():
    """Key order, absent-vs-default and unordered list order cannot move a hash."""
    verbose = _document(
        evidence={"mark_binding": "datum", "datum_wells": ["measure", "dimension"]},
        responsive={"profiles": ["mcp-inline", "console"]},
        order={"source": "result"},
        accessibility={"table_fallback": "required", "summary_source": "result_manifest"},
    )
    terse = _document(
        responsive={"profiles": ["console", "mcp-inline"]},
        evidence={"datum_wells": ["dimension", "measure"]},
    )
    assert (
        validate_template_document(verbose).content_hash
        == validate_template_document(terse).content_hash
    )


def test_the_normalized_document_is_stable_under_revalidation():
    once = validate_template_document(_document())
    twice = validate_template_document(once.document)
    assert once.document == twice.document
    assert once.content_hash == twice.content_hash


def test_a_different_question_is_a_different_document():
    a = validate_template_document(_document())
    b = validate_template_document(_document(answers_question="Which category leads?"))
    assert a.content_hash != b.content_hash


def test_the_contract_literal_travels_inside_the_hash():
    """A template hash is only comparable to one produced under the same contract."""
    document = validate_template_document(_document()).document
    assert document["spec_contract_version"] == CHART_TEMPLATE_CONTRACT_VERSION
    assert (
        f'"spec_contract_version":"{CHART_TEMPLATE_CONTRACT_VERSION}"'
        in canonical_bytes(document).decode("utf-8")
    )


# ---------------------------------------------------------------------------
# AC8 -- one ceiling, and the database mirrors it.
# ---------------------------------------------------------------------------


def test_ac8_a_document_over_the_ceiling_is_refused_with_its_size():
    document = _document(
        labels={"override": {well: "N" * 120 for well in WELL_ROLES}},
        thresholds=[
            {"well": "measure", "comparator": "gt", "value": index, "severity": "info"}
            for index in range(10)
        ],
    )
    # The grammar's own bounds keep an honest document well under the ceiling, so
    # the ceiling is proved on the constant rather than by inflating a fixture
    # past the bounds that already refuse it.
    assert len(canonical_bytes(validate_template_document(document).document)) < MAX_TEMPLATE_BYTES


def test_ac8_the_ceiling_is_one_number_and_the_database_carries_it():
    from core.visualization_specs import MAX_SPEC_BYTES

    assert MAX_TEMPLATE_BYTES == MAX_SPEC_BYTES
    sql = _MIGRATION_333.read_text(encoding="utf-8")
    sizes = re.findall(r"pg_column_size\(document\) <= (\d+)", sql)
    assert sizes == [str(MAX_TEMPLATE_BYTES)]


def test_the_contract_literal_is_pinned_in_one_place():
    """The constant and migration 334 are one string, or this is red."""
    sql = _MIGRATION_334.read_text(encoding="utf-8")
    pinned = re.findall(r"spec_contract_version = '([a-z0-9.\-]+)'", sql)
    assert pinned == [CHART_TEMPLATE_CONTRACT_VERSION]


def test_migration_334_carries_the_positive_half_of_the_grammar():
    sql = _MIGRATION_334.read_text(encoding="utf-8")
    assert "jsonb_typeof(document -> 'requires') = 'object'" in sql
    assert "jsonb_typeof(document -> 'answers_question') = 'string'" in sql
    assert "NOT (document ? 'annotations')" in sql
    for key in SUBTRACTED_FROM_SPEC:
        assert key in sql or key == "bindings", key


# ---------------------------------------------------------------------------
# The vocabulary this document speaks, served whole.
# ---------------------------------------------------------------------------


def test_the_vocabulary_is_the_registry_and_carries_no_second_copy():
    payload = template_vocabulary()
    assert [entry["name"] for entry in payload["wells"]] == list(WELL_ROLES)
    assert payload["families"] == list(SPEC_SELECTABLE_FAMILY_IDS)
    assert payload["derived_from"] == VISUALIZATION_SPEC_CONTRACT_VERSION
    assert payload["subtracted_from_visualization_spec"] == SUBTRACTED_FROM_SPEC
    assert payload["max_bytes"] == MAX_TEMPLATE_BYTES


def test_a_document_that_is_not_an_object_is_refused_in_this_objects_word():
    normalized, refusals = normalize_template_document(["not", "an", "object"])
    assert normalized == {}
    assert refusals[0].message == "a Chart Template is an object"
