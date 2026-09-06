"""Story 49.3 — Semantic View compilation, join safety and the Ossie boundary.

What these tests hold in place:

* a View does not have every metric/dimension pair queryable just because it
  lists both — each pair is proved or refused with a named reason;
* fan-out is refused in the direction it happens, not in the direction it was
  declared, and a many-to-many hop needs a real bridge;
* a "day" in two timezones is two different days, and saying so is the point;
* the Apache Ossie projection conforms to the VERIFIED 0.1.1 contract, uses an
  admitted vendor name, and carries every toorow fact inside the extension.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
from core.semantic_compiler import (
    COMPILER_VERSION,
    MAX_JOIN_HOPS,
    OSSIE_SPEC_VERSION,
    ConceptMember,
    DatasetRef,
    Relationship,
    RelationshipGraph,
    compile_semantic_view,
    dependency_fingerprint,
    project_ossie_view,
)
from core.semantic_expressions import ConceptResolver

SCHEMAS = Path(__file__).resolve().parents[2] / "core" / "schemas"


def _schema() -> dict:
    return json.loads((SCHEMAS / "ossie-0.1.1.schema.json").read_text(encoding="utf-8"))


def _metric(name="revenue", dataset="fact", **overrides) -> ConceptMember:
    base = {
        "concept_id": f"sc_{name}",
        "version_id": f"scv_{name}",
        "name": name,
        "label": name.title(),
        "role": "metric",
        "dataset": dataset,
        "value_type": "money",
        "currency": "EUR",
        "aggregation": {"function": "sum"},
        "additivity_class": "additive",
        "expression": {"op": "source_measure", "concept": name},
    }
    base.update(overrides)
    return ConceptMember(**base)


def _dimension(name="country", dataset="fact", **overrides) -> ConceptMember:
    base = {
        "concept_id": f"sc_{name}",
        "version_id": f"scv_{name}",
        "name": name,
        "label": name.title(),
        "role": "dimension",
        "dataset": dataset,
        "value_type": "string",
        "semantic_type": "categorical",
        "expression": {"op": "column", "name": name},
    }
    base.update(overrides)
    return ConceptMember(**base)


def _compile(concepts, relationships=(), datasets=None, **kwargs):
    datasets = datasets or [
        DatasetRef("fact", "warehouse.fact_daily_kpi", ("id",)),
        DatasetRef("dim", "warehouse.dim"),
        DatasetRef("bridge", "warehouse.bridge"),
    ]
    return compile_semantic_view(
        view_name="marketing_core",
        view_label="Marketing core",
        description="Core marketing meaning",
        concepts=list(concepts),
        relationships=list(relationships),
        datasets=datasets,
        resolver=ConceptResolver({}),
        **kwargs,
    )


def _cell(result, dimension_name: str) -> dict:
    return next(
        cell
        for cell in result.matrix["cells"]
        if cell["dimension_id"] == f"sc_{dimension_name}"
    )


# ---------------------------------------------------------------------------
# Compatibility is proved, never assumed
# ---------------------------------------------------------------------------


def test_two_concepts_on_the_same_dataset_are_queryable_without_a_join():
    result = _compile([_metric(), _dimension()])
    assert result.ok
    cell = _cell(result, "country")
    assert cell["queryable"] is True
    assert cell["join_path"] == []
    assert result.matrix["summary"] == {
        "metrics": 1,
        "dimensions": 1,
        "pairs": 1,
        "accepted": 1,
        "refused": 0,
    }


def test_an_unrelated_dataset_is_refused_and_never_silently_cross_joined():
    result = _compile([_metric(), _dimension(dataset="dim")])
    cell = _cell(result, "country")
    assert cell["queryable"] is False
    assert cell["refusal"]["code"] in {"unrelated_dataset", "no_join_path"}


def test_a_many_to_one_join_from_the_fact_is_safe():
    result = _compile(
        [_metric(), _dimension(dataset="dim")],
        [Relationship("fact_dim", "fact", "dim", ("dim_id",), ("id",), "many_to_one", "forbid")],
    )
    cell = _cell(result, "country")
    assert cell["queryable"] is True
    assert [hop["cardinality"] for hop in cell["join_path"]] == ["many_to_one"]


def test_the_same_edge_traversed_the_other_way_fans_out_and_is_refused():
    # The SAME declaration, read from both ends. One dim row has many fact rows.
    # A measure on the `dim` side reaching an attribute on the `fact` side walks
    # one-to-many and is multiplied once per matching row; the reverse walk is
    # many-to-one and is safe. Only the direction differs.
    declaration = Relationship(
        "dim_fact", "dim", "fact", ("id",), ("dim_id",), "one_to_many", "forbid"
    )
    safe = _compile([_metric(dataset="fact"), _dimension(dataset="dim")], [declaration])
    assert _cell(safe, "country")["queryable"] is True

    fanned_out = _compile([_metric(dataset="dim"), _dimension(dataset="fact")], [declaration])
    cell = _cell(fanned_out, "country")
    assert cell["queryable"] is False
    assert cell["refusal"]["code"] == "fan_out_forbidden"


def test_is_time_is_derived_from_semantic_type_and_cannot_disagree_with_it():
    # These were two fields once, and the timezone refusal stopped firing the
    # first time a caller set one without the other.
    assert _dimension("date", semantic_type="temporal").is_time is True
    assert _dimension("country", semantic_type="categorical").is_time is False


def test_a_many_to_many_without_a_bridge_is_refused():
    result = _compile(
        [_metric(), _dimension(dataset="dim")],
        [
            Relationship(
                "fact_dim", "fact", "dim", ("id",), ("fact_id",), "many_to_many", "deduplicate"
            )
        ],
    )
    assert _cell(result, "country")["refusal"]["code"] == "unbridged_many_to_many"


def test_a_many_to_many_with_a_declared_bridge_is_accepted():
    result = _compile(
        [_metric(), _dimension(dataset="dim")],
        [
            Relationship(
                "fact_dim",
                "fact",
                "dim",
                ("id",),
                ("fact_id",),
                "many_to_many",
                "bridge",
                bridge_dataset="bridge",
            )
        ],
    )
    cell = _cell(result, "country")
    assert cell["queryable"] is True
    assert cell["join_path"][0]["bridge"] == "bridge"


def test_a_join_chain_longer_than_the_bound_is_refused_not_walked():
    datasets = [DatasetRef(f"d{i}", f"warehouse.d{i}") for i in range(MAX_JOIN_HOPS + 3)]
    relationships = [
        Relationship(f"r{i}", f"d{i}", f"d{i + 1}", ("id",), ("id",), "many_to_one", "forbid")
        for i in range(MAX_JOIN_HOPS + 2)
    ]
    result = _compile(
        [_metric(dataset="d0"), _dimension(dataset=f"d{MAX_JOIN_HOPS + 2}")],
        relationships,
        datasets=datasets,
    )
    assert _cell(result, "country")["refusal"]["code"] == "join_path_too_long"


# ---------------------------------------------------------------------------
# A proven join is not yet a safe question
# ---------------------------------------------------------------------------


def test_a_metric_is_refused_across_a_dimension_it_declares_non_additive():
    result = _compile(
        [_metric(non_additive_dimensions=("country",), additivity_class="semi_additive"),
         _dimension()]
    )
    cell = _cell(result, "country")
    assert cell["queryable"] is False
    assert cell["refusal"]["code"] == "non_additive_dimension"


def test_two_timezones_on_the_same_day_is_a_false_day_equivalence():
    result = _compile(
        [
            _metric(timezone="Europe/Paris", grain="day"),
            _dimension(
                "date", semantic_type="temporal", timezone="UTC", grain="day", value_type="date"
            ),
        ]
    )
    cell = _cell(result, "date")
    assert cell["queryable"] is False
    assert cell["refusal"]["code"] == "false_day_equivalence"
    assert "Europe/Paris" in cell["refusal"]["message"]


def test_a_finer_grain_cannot_be_recovered_from_a_coarser_one():
    result = _compile(
        [
            _metric(grain="month", timezone="UTC"),
            _dimension(
                "date", semantic_type="temporal", grain="day", timezone="UTC", value_type="date"
            ),
        ]
    )
    assert _cell(result, "date")["refusal"]["code"] == "incompatible_grain"


def test_a_view_with_no_metric_is_refused():
    result = _compile([_dimension()])
    assert [refusal.code for refusal in result.refusals] == ["no_metrics_selected"]


def test_a_concept_bound_to_an_undeclared_dataset_is_refused():
    result = _compile([_metric(dataset="ghost"), _dimension()])
    assert "unknown_dataset" in [refusal.code for refusal in result.refusals]


def test_a_formula_still_referring_to_a_name_cannot_be_compiled_into_a_publication():
    result = _compile(
        [
            _metric(
                expression={
                    "op": "ratio",
                    "zero_denominator": "null",
                    "numerator": {"op": "concept_name", "name": "clicks"},
                    "denominator": {"op": "concept_name", "name": "impressions"},
                }
            ),
            _dimension(),
        ]
    )
    assert "unresolved_reference" in [refusal.code for refusal in result.refusals]


# ---------------------------------------------------------------------------
# The Apache Ossie boundary
# ---------------------------------------------------------------------------


def test_the_projection_conforms_to_the_verified_0_1_1_contract():
    projection = project_ossie_view(
        view_name="marketing_core",
        view_label="Marketing core",
        description="Core marketing meaning",
        concepts=[_metric(), _dimension()],
        relationships=[
            Relationship("fact_dim", "fact", "dim", ("dim_id",), ("id",), "many_to_one", "forbid")
        ],
        datasets=[DatasetRef("fact", "warehouse.fact_daily_kpi", ("id",))],
        query_policy={"grains": ["day"]},
    )
    jsonschema.validate(projection, _schema())
    assert projection["ossie_spec_version"] == OSSIE_SPEC_VERSION


def test_the_model_carries_the_name_the_contract_requires():
    projection = project_ossie_view(
        view_name="marketing_core",
        view_label="Marketing core",
        description=None,
        concepts=[_metric()],
        relationships=[],
        datasets=[DatasetRef("fact", "warehouse.fact_daily_kpi")],
    )
    # The older local profile had no `name` at all. The contract requires it.
    assert projection["semantic_model"]["name"] == "marketing_core"


def test_a_metric_carries_no_dataset_property_because_0_1_1_has_none():
    projection = project_ossie_view(
        view_name="v",
        view_label="V",
        description=None,
        concepts=[_metric()],
        relationships=[],
        datasets=[DatasetRef("fact", "warehouse.fact_daily_kpi")],
    )
    assert "dataset" not in projection["semantic_model"]["metrics"][0]
    # It is not lost: it moves into the namespaced extension.
    payload = json.loads(projection["semantic_model"]["metrics"][0]["custom_extensions"][0]["data"])
    assert payload["toorow"]["dataset"] == "fact"


def test_every_extension_uses_an_admitted_vendor_name():
    projection = project_ossie_view(
        view_name="v",
        view_label="V",
        description=None,
        concepts=[_metric(), _dimension()],
        relationships=[
            Relationship("fact_dim", "fact", "dim", ("dim_id",), ("id",), "many_to_one", "forbid")
        ],
        datasets=[DatasetRef("fact", "warehouse.fact_daily_kpi")],
    )
    admitted = set(_schema()["$defs"]["custom_extension"]["properties"]["vendor_name"]["enum"])
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            for extension in node.get("custom_extensions", ()) or ():
                found.append(extension["vendor_name"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(projection)
    assert found, "the projection must carry the toorow payload somewhere"
    assert set(found) <= admitted
    # `toorow` was the value the pre-existing Data projection wrote, and it is
    # not an admitted vendor name.
    assert "toorow" not in found


def test_relationships_carry_the_join_columns_the_contract_requires():
    projection = project_ossie_view(
        view_name="v",
        view_label="V",
        description=None,
        concepts=[_metric()],
        relationships=[
            Relationship("fact_dim", "fact", "dim", ("dim_id",), ("id",), "many_to_one", "forbid")
        ],
        datasets=[DatasetRef("fact", "warehouse.fact_daily_kpi")],
    )
    relationship = projection["semantic_model"]["relationships"][0]
    assert relationship["from"] == "fact" and relationship["to"] == "dim"
    assert relationship["from_columns"] == ["dim_id"] and relationship["to_columns"] == ["id"]


def test_the_projection_is_deterministic_for_the_same_input():
    kwargs = dict(
        view_name="v",
        view_label="V",
        description=None,
        concepts=[_dimension("b"), _metric("a"), _dimension("a_dim")],
        relationships=[],
        datasets=[DatasetRef("fact", "warehouse.fact_daily_kpi")],
    )
    assert project_ossie_view(**kwargs) == project_ossie_view(**kwargs)


# ---------------------------------------------------------------------------
# The fingerprint is the whole stale-state check
# ---------------------------------------------------------------------------


def test_the_fingerprint_changes_when_any_pinned_version_moves():
    base = dict(
        concept_versions=["scv_1"],
        mapping_versions=["dmv_1"],
        master_data_versions=["mdv_1"],
        policy_version="p1",
    )
    original = dependency_fingerprint(**base)
    assert dependency_fingerprint(**{**base, "concept_versions": ["scv_2"]}) != original
    assert dependency_fingerprint(**{**base, "mapping_versions": ["dmv_2"]}) != original
    assert dependency_fingerprint(**{**base, "master_data_versions": ["mdv_2"]}) != original
    assert dependency_fingerprint(**{**base, "policy_version": "p2"}) != original
    # Order is not meaning: the same set in a different order is the same world.
    assert dependency_fingerprint(**{**base, "concept_versions": ["scv_1", "scv_1"]}) == original


def test_the_graph_reverses_cardinality_when_it_reverses_the_edge():
    graph = RelationshipGraph(
        [Relationship("r", "a", "b", ("id",), ("a_id",), "many_to_one", "deduplicate")]
    )
    assert graph.path("a", "b").hops[0].cardinality_type == "many_to_one"
    assert graph.path("b", "a").hops[0].cardinality_type == "one_to_many"


def test_the_compiler_version_is_stamped_on_the_matrix():
    result = _compile([_metric(), _dimension()])
    assert result.matrix["compiler_version"] == COMPILER_VERSION


class TestAnUndeclaredDatasetProvesNothing:
    """Le catalogue annonçait 22 analyses ; quatre ne pouvaient rien rendre.

    `semantic_view_version_concepts` stocke `(view_version_id, ordinal,
    concept_id, concept_version_id, role)` et AUCUNE colonne dataset, donc
    `entry.get("dataset")` rendait None pour chaque membre de chaque vue
    enregistrée. Deux chaînes vides comparées égales, et la première ligne de
    `RelationshipGraph.path` rendait « même jeu de données » -- donc « jointure
    prouvée » -- pour TOUTE paire, sans consulter une seule relation.

    Mesure du 2026-08-19 sur la seule vue de production : 2 mesures x 11
    dimensions = 22 paires, toutes `queryable: true`, et quatre d'entre elles
    (vues et minutes par tranche d'âge ou par genre) rendent zéro ligne parce que
    la source ne publie que `viewer_percentage` sur ces axes.
    """

    def test_a_member_without_a_dataset_refuses_instead_of_matching_another(self):
        graph = RelationshipGraph(())

        verdict = graph.path("", "")

        assert not verdict.accepted, "deux absences ont valu une preuve"
        assert verdict.code == "dataset_undeclared"

    def test_the_refusal_names_the_gesture_that_repairs_it(self):
        verdict = RelationshipGraph(()).path("", "fact_audience")

        assert "Bind the Concept to the Datastream" in verdict.message

    def test_one_declared_dataset_is_not_enough_either(self):
        """Une moitié de l'information ne prouve pas la moitié d'une jointure."""
        graph = RelationshipGraph(())

        assert not graph.path("fact_channel", "").accepted

    def test_two_members_of_the_SAME_declared_dataset_still_join(self):
        """La correction ne durcit rien pour une vue qui dit d'où viennent ses membres."""
        graph = RelationshipGraph(())

        verdict = graph.path("fact_channel", "fact_channel")

        assert verdict.accepted
        assert verdict.hops == ()
