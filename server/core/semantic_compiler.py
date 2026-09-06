"""Compilation of a Semantic View version (Story 49.3, AC3 and AC4).

Compilation answers ONE question, for every metric/dimension pair a View
declares: *can this pair be queried, and if not, exactly why not?* It never
answers "probably". The output is a matrix in which every cell is either an
accepted path with its join chain, or a refusal carrying a named reason — the
same reason vocabulary the workbench renders and the Explore handoff reports.

Three things this module refuses to do:

* **Assume compatibility.** A View that lists twelve metrics and nine dimensions
  does not thereby have 108 queryable pairs. Each pair is proved through the
  relationship graph or refused.
* **Trust a submitted join.** A many-to-many hop fans the measure out and
  multiplies it. It is accepted only through a declared bridge, and a
  ``fan_out_policy`` of ``forbid`` is honoured even when a path exists.
* **Let a compiled artifact become a definition.** Dialect SQL and the Apache
  Ossie projection are OUTPUTS, stamped with the exact source version, the
  compiler version and a content hash. Nothing reads them back as truth.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Sequence

from core.semantic_expressions import (
    EXPRESSION_CONTRACT_VERSION,
    ConceptResolver,
    Refusal,
    detect_cycle,
    validate_expression,
)

#: Bumped whenever the compiler's OUTPUT changes for unchanged input. It is
#: stored on every artifact, so an artifact is never reinterpreted under a
#: compiler that would have produced something else.
#: v2 (2026-08-22, story 27.8): `dimensions[]` gained `name`, the STABLE conformed
#: dimension name. The language-family guard judges on it, so an artifact without it
#: cannot be judged -- and v1 stayed put while the output changed, which is the one
#: thing this constant exists to prevent. `query_specs._load_pinned_view` refuses a
#: stale artifact rather than executing it under a guard that abstains.
COMPILER_VERSION = "semantic-compiler.v2"

OSSIE_SPEC_VERSION = "0.1.1"
#: Namespaced toorow payload version. Lifecycle, MDM and quality metadata travel
#: here and NEVER as extra Ossie properties, so a conformant reader that drops
#: every extension still reads a correct model.
TOOROW_EXTENSION_VERSION = "2"
#: `toorow` is not an admitted `vendor_name` in Ossie 0.1.1 (the enumeration is
#: COMMON, SNOWFLAKE, SALESFORCE, DBT, DATABRICKS, GOODDATA). The conformant
#: namespacing is COMMON with the payload nested under a `toorow` key.
OSSIE_VENDOR_NAME = "COMMON"

#: A join chain longer than this is refused rather than walked. Deep chains are
#: where fan-out hides, and an unbounded search runs on a request path.
MAX_JOIN_HOPS = 4


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Relationship:
    name: str
    from_dataset: str
    to_dataset: str
    from_columns: tuple[str, ...]
    to_columns: tuple[str, ...]
    cardinality_type: str
    fan_out_policy: str
    bridge_dataset: str | None = None


@dataclass(frozen=True, slots=True)
class ConceptMember:
    """One Concept version selected into a View, with the dataset that carries it."""

    concept_id: str
    version_id: str
    name: str
    label: str
    role: str  # "metric" | "dimension"
    dataset: str
    value_type: str
    unit: str | None = None
    currency: str | None = None
    timezone: str | None = None
    grain: str | None = None
    aggregation: Mapping[str, Any] | None = None
    additivity_class: str | None = None
    non_additive_dimensions: tuple[str, ...] = ()
    expression: Mapping[str, Any] | None = None
    semantic_type: str | None = None

    @property
    def is_time(self) -> bool:
        """Derived, never stored. A boolean beside `semantic_type` is a second
        copy of the same fact, and the two disagreed the moment one caller set
        one and not the other — which is how a timezone refusal stops firing."""
        return self.semantic_type == "temporal"


@dataclass(frozen=True, slots=True)
class DatasetRef:
    name: str
    source: str
    primary_key: tuple[str, ...] = ()
    unique_keys: tuple[tuple[str, ...], ...] = ()


@dataclass(slots=True)
class CompilationResult:
    matrix: dict[str, Any] = field(default_factory=dict)
    ossie_projection: dict[str, Any] = field(default_factory=dict)
    refusals: list[Refusal] = field(default_factory=list)
    dbt_relation_refs: list[dict[str, Any]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.refusals

    @property
    def queryable_pairs(self) -> int:
        return int(self.matrix.get("summary", {}).get("accepted", 0))


# ---------------------------------------------------------------------------
# Relationship graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JoinHop:
    relationship: str
    from_dataset: str
    to_dataset: str
    cardinality_type: str
    bridge_dataset: str | None


@dataclass(frozen=True, slots=True)
class PathVerdict:
    accepted: bool
    hops: tuple[JoinHop, ...] = ()
    code: str | None = None
    message: str | None = None


class RelationshipGraph:
    """Undirected traversal over declared relationships, with the cardinality of
    each hop kept in the direction it is TRAVERSED.

    Direction matters for fan-out: walking many-to-one from the fact to the
    dimension is safe; walking the same edge one-to-many multiplies the measure.
    Reversing an edge therefore reverses its cardinality here rather than
    reusing the declared one.
    """

    _REVERSED = {
        "one_to_many": "many_to_one",
        "many_to_one": "one_to_many",
        "one_to_one": "one_to_one",
        "many_to_many": "many_to_many",
    }

    def __init__(self, relationships: Sequence[Relationship]):
        self._edges: dict[str, list[tuple[str, Relationship, str]]] = {}
        for rel in relationships:
            self._edges.setdefault(rel.from_dataset, []).append(
                (rel.to_dataset, rel, rel.cardinality_type)
            )
            self._edges.setdefault(rel.to_dataset, []).append(
                (rel.from_dataset, rel, self._REVERSED.get(rel.cardinality_type, "many_to_many"))
            )

    def path(self, measure_dataset: str, dimension_dataset: str) -> PathVerdict:
        """Prove a join chain from the dataset holding the MEASURE to the one
        holding the dimension, or refuse with the exact reason.

        AN UNDECLARED DATASET PROVES NOTHING, and this guard is the whole reason
        the line below is no longer a bare equality. A member whose dataset is
        empty compared equal to every other empty one, so the first branch
        returned "joinable" for EVERY pair of a View that declared no datasets --
        without ever consulting a relationship. Measured 2026-08-19 on the one
        production View: 22 pairs of 2 measures x 11 dimensions, all answered
        `queryable: true`, and four of them (views and minutes by age or by
        gender) return zero rows because the source publishes only
        `viewer_percentage` on those axes. The catalogue promised four analyses
        that cannot exist.

        Two empty strings are not the same dataset -- they are the absence of the
        information, and an absence cannot stand as proof.
        """
        if not measure_dataset or not dimension_dataset:
            return PathVerdict(
                False,
                code="dataset_undeclared",
                message=(
                    "This View does not say which dataset carries the metric or the "
                    "dimension, so no join can be proven. Bind the Concept to the "
                    "Datastream that produces it."
                ),
            )
        if measure_dataset == dimension_dataset:
            return PathVerdict(True, ())
        if measure_dataset not in self._edges:
            return PathVerdict(
                False,
                code="unrelated_dataset",
                message=f"No relationship reaches {measure_dataset!r}. The metric and "
                "the dimension are not connected in this Semantic View.",
            )

        # Breadth-first so the SHORTEST proven chain wins: a longer chain that
        # happens to be safe is still more fan-out surface than necessary.
        queue: list[tuple[str, tuple[JoinHop, ...], set[str]]] = [
            (measure_dataset, (), {measure_dataset})
        ]
        best_refusal: PathVerdict | None = None
        while queue:
            current, hops, visited = queue.pop(0)
            if len(hops) >= MAX_JOIN_HOPS:
                best_refusal = best_refusal or PathVerdict(
                    False,
                    code="join_path_too_long",
                    message=f"Reaching this dimension needs more than {MAX_JOIN_HOPS} "
                    "joins. Model the intermediate step as its own Concept.",
                )
                continue
            for neighbour, rel, traversed in self._edges.get(current, ()):
                if neighbour in visited:
                    continue
                hop = JoinHop(rel.name, current, neighbour, traversed, rel.bridge_dataset)
                # A hop that multiplies the measure is refused HERE rather than
                # accepted and warned about later.
                if traversed in {"one_to_many", "many_to_many"}:
                    if rel.fan_out_policy == "forbid":
                        best_refusal = best_refusal or PathVerdict(
                            False,
                            code="fan_out_forbidden",
                            message=f"Relationship {rel.name!r} is {traversed} in this "
                            "direction and its fan-out policy forbids the join. The "
                            "measure would be counted once per matching row.",
                        )
                        continue
                    if traversed == "many_to_many" and (
                        rel.fan_out_policy != "bridge" or not rel.bridge_dataset
                    ):
                        best_refusal = best_refusal or PathVerdict(
                            False,
                            code="unbridged_many_to_many",
                            message=f"Relationship {rel.name!r} is many-to-many and "
                            "declares no bridge. Without one the measure fans out and "
                            "is silently multiplied.",
                        )
                        continue
                extended = (*hops, hop)
                if neighbour == dimension_dataset:
                    return PathVerdict(True, extended)
                queue.append((neighbour, extended, visited | {neighbour}))
        return best_refusal or PathVerdict(
            False,
            code="no_join_path",
            message=f"No declared relationship chain connects {measure_dataset!r} to "
            f"{dimension_dataset!r}.",
        )


# ---------------------------------------------------------------------------
# Pair-level safety, beyond the join
# ---------------------------------------------------------------------------


def _pair_refusal(metric: ConceptMember, dimension: ConceptMember) -> tuple[str, str] | None:
    """Reasons a proven join is still not a safe question to ask."""
    if dimension.name in metric.non_additive_dimensions:
        return (
            "non_additive_dimension",
            f"{metric.label} must not be aggregated across {dimension.label}; it is "
            "declared non-additive on exactly this dimension.",
        )
    if metric.additivity_class == "non_additive" and not dimension.is_time:
        return (
            "non_additive_metric",
            f"{metric.label} is non-additive and cannot be rolled up by "
            f"{dimension.label}. Recompute it at the requested grain instead.",
        )
    if dimension.is_time:
        if metric.grain and dimension.grain and metric.grain != dimension.grain:
            return (
                "incompatible_grain",
                f"{metric.label} is produced at {metric.grain} grain and "
                f"{dimension.label} is {dimension.grain}. The finer one cannot be "
                "recovered from the coarser.",
            )
        # A "day" that means two different local midnights is not one day. This
        # is the false day-equivalence the module refuses to paper over.
        if metric.timezone and dimension.timezone and metric.timezone != dimension.timezone:
            return (
                "false_day_equivalence",
                f"{metric.label} is stamped in {metric.timezone} and {dimension.label} "
                f"in {dimension.timezone}. Joining them on the calendar date compares "
                "two different days and reports the difference as a trend.",
            )
    return None


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def compile_semantic_view(
    *,
    view_name: str,
    view_label: str,
    description: str | None,
    concepts: Sequence[ConceptMember],
    relationships: Sequence[Relationship],
    datasets: Sequence[DatasetRef],
    resolver: ConceptResolver,
    dbt_relation_refs: Sequence[Mapping[str, Any]] = (),
    query_policy: Mapping[str, Any] | None = None,
) -> CompilationResult:
    """Compile one View version into its matrix, projection and dbt pins."""
    result = CompilationResult()
    metrics = [c for c in concepts if c.role == "metric"]
    dimensions = [c for c in concepts if c.role == "dimension"]
    dataset_names = {d.name for d in datasets}

    if not metrics:
        result.refusals.append(
            Refusal(
                "no_metrics_selected",
                "A Semantic View publishes at least one metric. Publishing a View "
                "nobody can measure anything with would be publishing an empty promise.",
                "$.concepts",
            )
        )

    # Every selected Concept must sit on a dataset the View actually declares.
    for member in concepts:
        if member.dataset not in dataset_names:
            result.refusals.append(
                Refusal(
                    "unknown_dataset",
                    f"{member.label} is bound to dataset {member.dataset!r}, which this "
                    "Semantic View does not declare.",
                    f"$.concepts.{member.name}",
                )
            )

    # Formula validation, then the DAG across the whole selection.
    edges: dict[str, list[str]] = {}
    for member in metrics:
        if member.expression is None:
            continue
        analysis = validate_expression(
            member.expression,
            resolver,
            owning_concept_name=member.name,
            owning_value_type=member.value_type,
        )
        for refusal in analysis.refusals:
            result.refusals.append(
                Refusal(refusal.code, f"{member.label}: {refusal.message}", refusal.path)
            )
        if analysis.unresolved_names:
            result.refusals.append(
                Refusal(
                    "unresolved_reference",
                    f"{member.label} still refers to "
                    + ", ".join(sorted(set(analysis.unresolved_names)))
                    + " by name. A published version pins exact Concept versions; a "
                    "name follows whatever that name later means.",
                    f"$.concepts.{member.name}.expression",
                )
            )
        edges.setdefault(member.version_id, []).extend(
            ref.version_id for ref in analysis.dependencies
        )

    for member in metrics:
        cycle = detect_cycle(member.version_id, edges)
        if cycle:
            result.refusals.append(
                Refusal(
                    "formula_cycle",
                    "This formula depends on itself: " + " -> ".join(cycle),
                    f"$.concepts.{member.name}.expression",
                )
            )
            break

    graph = RelationshipGraph(relationships)
    cells: list[dict[str, Any]] = []
    accepted = 0
    for metric in metrics:
        for dimension in dimensions:
            verdict = graph.path(metric.dataset, dimension.dataset)
            if not verdict.accepted:
                cells.append(
                    {
                        "metric_id": metric.concept_id,
                        "metric_version_id": metric.version_id,
                        "dimension_id": dimension.concept_id,
                        "dimension_version_id": dimension.version_id,
                        "queryable": False,
                        "refusal": {"code": verdict.code, "message": verdict.message},
                    }
                )
                continue
            pair_refusal = _pair_refusal(metric, dimension)
            if pair_refusal is not None:
                code, message = pair_refusal
                cells.append(
                    {
                        "metric_id": metric.concept_id,
                        "metric_version_id": metric.version_id,
                        "dimension_id": dimension.concept_id,
                        "dimension_version_id": dimension.version_id,
                        "queryable": False,
                        "refusal": {"code": code, "message": message},
                    }
                )
                continue
            accepted += 1
            cells.append(
                {
                    "metric_id": metric.concept_id,
                    "metric_version_id": metric.version_id,
                    "dimension_id": dimension.concept_id,
                    "dimension_version_id": dimension.version_id,
                    "queryable": True,
                    "join_path": [
                        {
                            "relationship": hop.relationship,
                            "from": hop.from_dataset,
                            "to": hop.to_dataset,
                            "cardinality": hop.cardinality_type,
                            "bridge": hop.bridge_dataset,
                        }
                        for hop in verdict.hops
                    ],
                }
            )

    result.matrix = {
        "compiler_version": COMPILER_VERSION,
        "expression_contract_version": EXPRESSION_CONTRACT_VERSION,
        "metrics": [
            {"concept_id": m.concept_id, "version_id": m.version_id, "label": m.label}
            for m in metrics
        ],
        "dimensions": [
            # `name` travels beside `label` because they answer different questions:
            # `label` is what a human reads and may be renamed freely, `name` is the
            # STABLE conformed-dimension identifier. A validator that must know
            # WHAT a dimension is -- rather than what it is called today -- has no
            # other honest key: matching on a display label would break the moment a
            # client renames it. Additive: existing readers ignore the extra key.
            {
                "concept_id": d.concept_id,
                "version_id": d.version_id,
                "name": d.name,
                "label": d.label,
            }
            for d in dimensions
        ],
        "cells": cells,
        "summary": {
            "metrics": len(metrics),
            "dimensions": len(dimensions),
            "pairs": len(cells),
            "accepted": accepted,
            "refused": len(cells) - accepted,
        },
    }

    result.ossie_projection = project_ossie_view(
        view_name=view_name,
        view_label=view_label,
        description=description,
        concepts=concepts,
        relationships=relationships,
        datasets=datasets,
        query_policy=query_policy,
    )
    result.dbt_relation_refs = [dict(ref) for ref in dbt_relation_refs]
    return result


# ---------------------------------------------------------------------------
# Apache Ossie 0.1.1 projection
# ---------------------------------------------------------------------------


def _toorow_extension(payload: Mapping[str, Any]) -> dict[str, str]:
    """One namespaced extension, conformant with the 0.1.1 vendor enumeration."""
    return {
        "vendor_name": OSSIE_VENDOR_NAME,
        "data": canonical_json(
            {"toorow": {"extension_version": TOOROW_EXTENSION_VERSION, **dict(payload)}}
        ),
    }


def project_ossie_view(
    *,
    view_name: str,
    view_label: str,
    description: str | None,
    concepts: Sequence[ConceptMember],
    relationships: Sequence[Relationship],
    datasets: Sequence[DatasetRef],
    query_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic Apache Ossie 0.1.1 projection of a published Semantic View.

    Conformant with the normative 0.1.1 field table (see
    ``schemas/ossie-0.1.1-verification.json`` for the pinned source commit).
    Every toorow-specific fact — lifecycle, aggregation safety, MDM references,
    query policy — travels inside a namespaced ``custom_extension`` so a reader
    that ignores extensions still gets a correct, if plainer, model.
    """
    by_dataset: dict[str, list[ConceptMember]] = {}
    for member in concepts:
        by_dataset.setdefault(member.dataset, []).append(member)

    dataset_objects: list[dict[str, Any]] = []
    for dataset in sorted(datasets, key=lambda d: d.name):
        fields: list[dict[str, Any]] = []
        for member in sorted(by_dataset.get(dataset.name, ()), key=lambda m: m.name):
            if member.role != "dimension":
                continue
            field_object: dict[str, Any] = {
                "name": member.name,
                # `expression` is REQUIRED by 0.1.1. The restricted typed tree is
                # what toorow means by it; the compiled dialect SQL never
                # substitutes for it.
                "expression": dict(member.expression or {"op": "column", "name": member.name}),
                "label": member.label,
                "custom_extensions": [
                    _toorow_extension(
                        {
                            "concept_id": member.concept_id,
                            "concept_version_id": member.version_id,
                            "semantic_type": member.semantic_type,
                            "value_type": member.value_type,
                            "grain": member.grain,
                            "timezone": member.timezone,
                        }
                    )
                ],
            }
            if member.is_time:
                field_object["dimension"] = {"is_time": True}
            fields.append(field_object)
        dataset_object: dict[str, Any] = {
            "name": dataset.name,
            "source": dataset.source,
        }
        if dataset.primary_key:
            dataset_object["primary_key"] = list(dataset.primary_key)
        if dataset.unique_keys:
            dataset_object["unique_keys"] = [list(key) for key in dataset.unique_keys]
        if fields:
            dataset_object["fields"] = fields
        dataset_objects.append(dataset_object)

    metric_objects = [
        {
            "name": member.name,
            "expression": dict(
                member.expression or {"op": "source_measure", "concept": member.name}
            ),
            **({"description": member.label} if member.label else {}),
            "custom_extensions": [
                _toorow_extension(
                    {
                        "concept_id": member.concept_id,
                        "concept_version_id": member.version_id,
                        "dataset": member.dataset,
                        "value_type": member.value_type,
                        "unit": member.unit,
                        "currency": member.currency,
                        "aggregation": dict(member.aggregation) if member.aggregation else None,
                        "additivity_class": member.additivity_class,
                        "non_additive_dimensions": list(member.non_additive_dimensions),
                    }
                )
            ],
        }
        for member in sorted((c for c in concepts if c.role == "metric"), key=lambda m: m.name)
    ]

    relationship_objects = [
        {
            "name": rel.name,
            "from": rel.from_dataset,
            "to": rel.to_dataset,
            "from_columns": list(rel.from_columns),
            "to_columns": list(rel.to_columns),
            "custom_extensions": [
                _toorow_extension(
                    {
                        "cardinality_type": rel.cardinality_type,
                        "fan_out_policy": rel.fan_out_policy,
                        "bridge_dataset": rel.bridge_dataset,
                    }
                )
            ],
        }
        for rel in sorted(relationships, key=lambda r: r.name)
    ]

    model: dict[str, Any] = {
        "name": view_name,
        "datasets": dataset_objects,
    }
    if description:
        model["description"] = description
    if relationship_objects:
        model["relationships"] = relationship_objects
    if metric_objects:
        model["metrics"] = metric_objects
    model["custom_extensions"] = [
        _toorow_extension(
            {
                "label": view_label,
                "query_policy": dict(query_policy or {}),
                "expression_contract_version": EXPRESSION_CONTRACT_VERSION,
                "compiler_version": COMPILER_VERSION,
            }
        )
    ]
    return {"ossie_spec_version": OSSIE_SPEC_VERSION, "semantic_model": model}


def dependency_fingerprint(
    *,
    concept_versions: Iterable[str],
    mapping_versions: Iterable[str],
    master_data_versions: Iterable[str],
    policy_version: str | None,
) -> str:
    """Everything a prepared change set assumed. Confirm recomputes it and
    refuses when it moved: that is the whole stale-state check."""
    return content_hash(
        {
            "concept_versions": sorted(set(concept_versions)),
            "mapping_versions": sorted(set(mapping_versions)),
            "master_data_versions": sorted(set(master_data_versions)),
            "policy_version": policy_version,
            "compiler_version": COMPILER_VERSION,
            "expression_contract_version": EXPRESSION_CONTRACT_VERSION,
        }
    )
