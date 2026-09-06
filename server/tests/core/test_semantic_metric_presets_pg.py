"""A preconfigured metric is adopted through the governed gate, or not at all.

WHY PG-GATED AND NOT MOCKED. Every claim this module makes is a claim about a
Project's real vocabulary: which names it can read, which scope wins when two
carry the same name, and whether the resolved formula survives
`validate_expression` and reaches `app.semantic_concept_versions`. A mocked
resolver would let this file assert that the dictionary I built matches the
dictionary I built. Here the offer is measured against migrated tables and the
adoption is published by the SAME three functions the console dialog and
`governance_mcp.publish_semantic_model_change` call -- no second writer exists,
and this file would have to invent one to cheat.

WHAT EACH TEST FAILS ON, NAMED:

  * `test_a_ratio_of_the_delivered_catalogue_is_offered_as_calculated` fails if
    the preset list stops being derived from `dim_metric.csv` -- a hand-kept
    second catalogue would drift from the marts dbt builds;
  * `test_an_adoptable_preset_pins_exact_versions_never_names` fails if the
    projected `concept_name` operands survive into the intent: they parse and can
    never be published, so the person would meet `unresolved_reference` after
    acting rather than before;
  * `test_adopting_a_preset_publishes_through_the_governed_gate` fails if the
    intent stops being what `create_change_set` -> `prepare_change_set` ->
    `confirm_change_set` accept -- which is the whole invariant: one semantic
    truth, one door;
  * `test_a_missing_dependency_blocks_the_preset_and_names_the_gesture` fails if
    a preset is offered with a dangling operand, or refuses without saying which
    Concept to declare. Proved by MUTATION: `cost` is archived and `roas` must
    change state on its own;
  * `test_the_projects_own_concept_wins_over_the_platform_one` fails if the
    precedence is reversed -- the ratio would silently pin the platform
    definition of a metric this Project deliberately redefined (E39-NFR04);
  * `test_an_already_readable_name_is_not_offered_twice` fails if adoption does
    not change the offer, which would invite a second Concept shadowing the first.

Rolled back by the `live_postgres` fixture: nothing this file writes survives it.
"""

from __future__ import annotations

import pytest
from core.semantic_metric_presets import (
    ADOPTABLE,
    ALREADY_DECLARED,
    BLOCKED,
    build_presets,
    dependency_names,
    presets_for_project,
    readable_concepts,
    resolve_expression,
)
from ulid import ULID

pytestmark = pytest.mark.live_postgres

#: Seeded by migration 001 in every migrated database, with its organization —
#: `confirm_change_set` records an operation whose `effective_org_id` is a real
#: foreign key, so an invented one fails before anything semantic is proved.
_PROJECT = "default"
_ORG = "org_default"

_OVERRIDE = {
    "reason": "Preset adoption proved in the pg-gated suite; Test has no verdict for a "
    "change set created inside a rolled-back transaction."
}


def _preset(payload: dict, name: str) -> dict:
    matching = [entry for entry in payload["presets"] if entry["name"] == name]
    assert matching, f"{name} is expected in the delivered catalogue"
    return matching[0]


def _archive(conn, name: str) -> None:
    """Retire the PLATFORM Concept of this name, leaving the seed row alone."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.semantic_concepts SET lifecycle_status = 'archived' "
            "WHERE name = %s AND project_id IS NULL",
            (name,),
        )
        assert cur.rowcount == 1, f"{name} is expected as a platform Concept"


def _seed_project_concept(conn, *, name: str, value_type: str = "money") -> tuple[str, str]:
    """Publish a Project-scoped Concept the way a confirmed change set leaves it."""
    concept_id, version_id = f"sc_{ULID()}", f"scv_{ULID()}"
    with conn.cursor() as cur:
        # In this order, and the FK is why: `fk_semantic_concepts_current_version`
        # points at `(id, concept_id, project_id)` of the versions table, so a
        # Project-scoped head cannot be named before the version exists.
        cur.execute(
            """
            INSERT INTO app.semantic_concepts
                (id, project_id, kind, name, lifecycle_status, created_by)
            VALUES (%s, %s, 'metric', %s, 'published', 'qa-harness')
            """,
            (concept_id, _PROJECT, name),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 value_type, expression, aggregation, additivity_class,
                 content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'metric', %s, %s, %s,
                    %s::jsonb, %s::jsonb, 'additive', %s, 'qa-harness')
            """,
            (
                version_id,
                concept_id,
                _PROJECT,
                name,
                f"This Project's {name}",
                value_type,
                f'{{"op": "source_measure", "concept": "{name}"}}',
                '{"function": "sum"}',
                "0" * 64,
            ),
        )
        cur.execute(
            "UPDATE app.semantic_concepts SET current_version_id = %s WHERE id = %s",
            (version_id, concept_id),
        )
    return concept_id, version_id


# ---------------------------------------------------------------------------
# The offer
# ---------------------------------------------------------------------------


def test_a_ratio_of_the_delivered_catalogue_is_offered_as_calculated(live_postgres) -> None:
    """The three ratios of `dim_metric.csv` are the calculated metrics, derived."""
    payload = presets_for_project(live_postgres, _PROJECT)
    assert payload["source"] == "dbt/seeds/dim_metric.csv"

    calculated = {entry["name"] for entry in payload["presets"] if entry["calculated"]}
    assert calculated == {"roas", "ctr", "cpa"}

    roas = _preset(payload, "roas")
    # Read off the seed row, not decided here: `roas,false,ratio,revenue,cost`.
    assert roas["dependencies"] == ["revenue", "cost"]
    assert roas["value_type"] == "ratio"
    assert roas["additivity_class"] == "non_additive"
    assert roas["aggregation"] is None


def test_a_source_measure_preset_depends_on_nothing(live_postgres) -> None:
    """`conversions_value` is preconfigured too, and it is not a calculated metric.

    The arbitration is about calculated metrics; the offer is about the whole
    delivered catalogue. Conflating the two would make a plain sum look like a
    formula somebody has to understand before adopting it.
    """
    payload = presets_for_project(live_postgres, _PROJECT)
    preset = _preset(payload, "conversions_value")
    assert preset["calculated"] is False
    assert preset["dependencies"] == []
    assert preset["intent"]["concept"]["expression"] == {
        "op": "source_measure",
        "concept": "conversions_value",
    }


def test_an_adoptable_preset_pins_exact_versions_never_names(live_postgres) -> None:
    """`concept_name` parses and can never be published. It must not survive here."""
    payload = presets_for_project(live_postgres, _PROJECT)
    roas = _preset(payload, "roas")
    assert roas["state"] == ADOPTABLE

    expression = roas["intent"]["concept"]["expression"]
    assert expression["op"] == "ratio"
    assert expression["zero_denominator"] == "null"
    for side in ("numerator", "denominator"):
        assert expression[side]["op"] == "concept_ref", side
        assert expression[side]["concept_id"]
        assert expression[side]["version_id"]

    index = readable_concepts(live_postgres, _PROJECT)
    assert expression["numerator"]["version_id"] == index["revenue"].version_id
    assert expression["denominator"]["version_id"] == index["cost"].version_id


# ---------------------------------------------------------------------------
# The adoption -- through the gate that already exists, or not at all
# ---------------------------------------------------------------------------


def test_adopting_a_preset_publishes_through_the_governed_gate(live_postgres) -> None:
    """create -> prepare -> confirm, the three functions the console already calls."""
    from core.semantic_model import (
        confirm_change_set,
        create_change_set,
        prepare_change_set,
    )

    intent = _preset(presets_for_project(live_postgres, _PROJECT), "roas")["intent"]

    change_set = create_change_set(
        live_postgres,
        _PROJECT,
        actor="qa-harness",
        object_type="semantic-concept",
        object_id=None,
        base_version_id=None,
        intent=intent,
        idempotency_key=f"preset-roas-{ULID()}",
    )
    prepared = prepare_change_set(
        live_postgres,
        _PROJECT,
        change_set.id,
        actor="qa-harness",
        allow_test_override=_OVERRIDE,
    )
    # The expression cleared `validate_expression` with no refusal of its own.
    assert prepared["validation"]["expression"]["publishable"] is True
    assert prepared["validation"]["refusals"] == []
    assert prepared["validation"]["publishable"] is True

    confirm_change_set(
        live_postgres,
        _PROJECT,
        change_set.id,
        actor="qa-harness",
        confirmation_token=prepared["confirmation_token"],
        org_id=_ORG,
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT v.value_type, v.additivity_class, v.expression, v.id
              FROM app.semantic_concepts c
              JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
             WHERE c.project_id = %s AND c.name = 'roas'
            """,
            (_PROJECT,),
        )
        row = cur.fetchone()
    assert row is not None, "adopting a preset must publish a Project Concept"
    value_type, additivity, expression, version_id = row
    assert (value_type, additivity) == ("ratio", "non_additive")
    assert expression["numerator"]["op"] == "concept_ref"

    # The dependency rows are what makes the pin real: the exact versions the
    # formula reads are recorded, not the names it was projected with.
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT role, depends_on_version_id FROM app.semantic_concept_dependencies "
            "WHERE version_id = %s ORDER BY ordinal",
            (version_id,),
        )
        dependencies = cur.fetchall()
    index = readable_concepts(live_postgres, _PROJECT)
    assert [role for role, _ in dependencies] == ["numerator", "denominator"]
    assert dependencies[0][1] == index["revenue"].version_id


def test_an_already_readable_name_is_not_offered_twice(live_postgres) -> None:
    """A name this Project can already read is reported, never offered again."""
    payload = presets_for_project(live_postgres, _PROJECT)
    cost = _preset(payload, "cost")
    assert cost["state"] == ALREADY_DECLARED
    assert cost["declared_scope"] == "platform"
    assert cost["intent"] is None

    _seed_project_concept(live_postgres, name="installs", value_type="integer")
    after = _preset(presets_for_project(live_postgres, _PROJECT), "installs")
    assert after["state"] == ALREADY_DECLARED
    assert after["declared_scope"] == "project"
    assert after["intent"] is None


# ---------------------------------------------------------------------------
# The refusal -- named, with the gesture that repairs it
# ---------------------------------------------------------------------------


def test_a_missing_dependency_blocks_the_preset_and_names_the_gesture(live_postgres) -> None:
    """MUTATE the vocabulary: archive `cost`, and `roas` must stop being offered."""
    before = _preset(presets_for_project(live_postgres, _PROJECT), "roas")
    assert before["state"] == ADOPTABLE

    _archive(live_postgres, "cost")

    after = _preset(presets_for_project(live_postgres, _PROJECT), "roas")
    assert after["state"] == BLOCKED
    assert after["intent"] is None
    assert after["missing_dependencies"] == ["cost"]
    # The gesture names the Concept to declare -- not the shape of the tree.
    assert "Declare cost first" in after["gesture"]

    # `cpa` divides by `conversions` and also reads `cost`: the same archive
    # blocks it, and the offer stays honest about both.
    cpa = _preset(presets_for_project(live_postgres, _PROJECT), "cpa")
    assert cpa["state"] == BLOCKED
    assert cpa["missing_dependencies"] == ["cost"]

    # And `ctr`, whose operands are untouched, is still offered. A blanket
    # refusal would be as wrong as a blanket offer.
    ctr = _preset(presets_for_project(live_postgres, _PROJECT), "ctr")
    assert ctr["state"] == ADOPTABLE


def test_two_missing_dependencies_are_both_named(live_postgres) -> None:
    """A gesture that names one of two missing Concepts sends someone back twice."""
    _archive(live_postgres, "clicks")
    _archive(live_postgres, "impressions")
    ctr = _preset(presets_for_project(live_postgres, _PROJECT), "ctr")
    assert ctr["state"] == BLOCKED
    assert set(ctr["missing_dependencies"]) == {"clicks", "impressions"}
    assert "clicks and impressions" in ctr["gesture"]


def test_the_projects_own_concept_wins_over_the_platform_one(live_postgres) -> None:
    """E39-NFR04: a Project that redefined a metric is the one the formula pins."""
    platform = readable_concepts(live_postgres, _PROJECT)["cost"]
    assert platform.scope == "platform"

    _, project_version_id = _seed_project_concept(live_postgres, name="cost")

    index = readable_concepts(live_postgres, _PROJECT)
    assert index["cost"].scope == "project"
    assert index["cost"].version_id == project_version_id

    roas = _preset(presets_for_project(live_postgres, _PROJECT), "roas")
    denominator = roas["intent"]["concept"]["expression"]["denominator"]
    assert denominator["version_id"] == project_version_id
    assert denominator["version_id"] != platform.version_id


# ---------------------------------------------------------------------------
# The pure halves -- no database, so a reversal shows as a unit failure too
# ---------------------------------------------------------------------------


def test_dependency_names_reads_only_unresolved_names() -> None:
    """A resolved reference is not a dependency to declare; it is already pinned."""
    expression = {
        "op": "ratio",
        "numerator": {"op": "concept_name", "name": "revenue"},
        "denominator": {"op": "concept_ref", "concept_id": "sc_1", "version_id": "scv_1"},
        "zero_denominator": "null",
    }
    assert dependency_names(expression) == ("revenue",)
    assert dependency_names({"op": "source_measure", "concept": "clicks"}) == ()


def test_resolve_expression_is_all_or_nothing() -> None:
    """A half-resolved formula would move the refusal past the point of no return."""
    from core.semantic_metric_presets import ResolvedConcept

    index = {
        "revenue": ResolvedConcept("sc_rev", "scv_rev", "platform", "Revenue"),
    }
    expression = {
        "op": "ratio",
        "numerator": {"op": "concept_name", "name": "revenue"},
        "denominator": {"op": "concept_name", "name": "cost"},
        "zero_denominator": "null",
    }
    resolved, missing = resolve_expression(expression, index)
    assert resolved is None
    assert missing == ("cost",)

    index["cost"] = ResolvedConcept("sc_cost", "scv_cost", "platform", "Cost")
    resolved, missing = resolve_expression(expression, index)
    assert missing == ()
    assert resolved["numerator"] == {
        "op": "concept_ref",
        "concept_id": "sc_rev",
        "version_id": "scv_rev",
    }


def test_build_presets_offers_nothing_without_a_vocabulary() -> None:
    """An empty Project reads the platform vocabulary; without one, nothing is
    adoptable that depends on a name, and every refusal still says which."""
    from core.platform_semantic_concepts import project_delivered_catalogue

    catalogue, _ = project_delivered_catalogue({})
    presets = {preset.concept.name: preset for preset in build_presets(catalogue, {})}
    assert presets["roas"].state == BLOCKED
    assert presets["roas"].missing_dependencies == ("revenue", "cost")
    # A metric that reads only its own mapped measure is adoptable in an empty
    # Project, because it depends on nothing this Project has to declare first.
    assert presets["clicks"].state == ADOPTABLE
