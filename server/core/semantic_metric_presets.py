"""Preconfigured calculated metrics -- offered to a Project, never minted for it.

WHAT THIS MODULE ANSWERS, AND WHY IT IS NOT THE OTHER TWO ANSWERS. The delivered
catalogue (`dbt/seeds/dim_metric.csv`) declares twenty metrics; production
carries nine of them as PLATFORM Concepts and the audit of 2026-08-25 left the
remaining twelve -- `roas`, `ctr`, `cpa`, `conversions_value`, `ad_revenue` and
the rest -- named as an open question with two answers and no third: mint them
into the vocabulary EVERY Project reads, or leave them missing. Both are wrong
in the same direction. Minting `roas` platform-wide publishes a formula in a
scope where its operands cannot be pinned -- a platform ratio can only carry its
operands by NAME (`concept_name`), and `ExpressionAnalysis.publishable` refuses
exactly that, so the vocabulary would grow by a definition nothing may compile.
Leaving them missing makes every Project that wants a return-on-ad-spend build a
typed tree by hand, twice, and disagree about it.

THE ARBITRATION (Jean, 2026-08-25) IS THE THIRD ANSWER: a preconfigured OPTION a
Project adopts. Not platform vocabulary, not absent -- a proposal, resolved
against what THIS Project can actually read, that becomes an ordinary
project-scoped Concept the moment someone adopts it. Adoption is the change set
the console and the MCP door already drive; this module writes nothing.

WHAT IT IS DERIVED FROM, AND WHY THAT IS THE WHOLE POINT. Nothing here is a
second catalogue. `core.platform_semantic_concepts.project_delivered_catalogue`
already projects `dim_metric.csv` into typed Concepts -- value type, additivity,
aggregation and formula -- and it is the same projection the provisioning script
uses. A preset IS one of its rows, with two things added that only a Project can
answer: whether the name is already readable here, and whether every operand of
its formula is.

THE ONE TRANSFORMATION, AND IT IS THE REASON THE SCOPE MATTERS. A projected
ratio carries `{"op": "concept_name", "name": "cost"}`. That parses and can
never be published. Resolving it against THIS Project turns it into
`{"op": "concept_ref", "concept_id": ..., "version_id": ...}` -- the exact pair
`semantic_expressions` demands -- and a Project-scoped Concept of that name wins
over the platform one, because E39-NFR04 says a Project that reclassified a
metric wins. When an operand resolves to nothing the preset is BLOCKED and says
which Concept to declare first; it is never adopted with a dangling reference,
and it is never adopted with the name left unresolved.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from core.platform_semantic_concepts import (
    PlatformConcept,
    project_delivered_catalogue,
)

#: The three states a preset can be in for one Project. They are exclusive, and
#: only the first offers an intent.
ADOPTABLE = "adoptable"
ALREADY_DECLARED = "already_declared"
BLOCKED = "blocked"

#: Where every preset comes from. Quoted in the payload so a reader can check the
#: claim rather than trust it.
PRESET_SOURCE = "dbt/seeds/dim_metric.csv"


@dataclass(frozen=True)
class ResolvedConcept:
    """A Concept name resolved to the EXACT version this Project would pin."""

    concept_id: str
    version_id: str
    scope: str  # "project" or "platform"
    label: str

    def as_ref(self) -> dict[str, str]:
        return {
            "op": "concept_ref",
            "concept_id": self.concept_id,
            "version_id": self.version_id,
        }


@dataclass(frozen=True)
class MetricPreset:
    """One preconfigured metric, judged against one Project."""

    concept: PlatformConcept
    state: str
    #: The Concepts the formula references, by name, in the order it reads them.
    dependencies: tuple[str, ...]
    #: The subset of `dependencies` this Project cannot resolve. Empty unless BLOCKED.
    missing_dependencies: tuple[str, ...]
    #: What resolving produced. `None` unless ADOPTABLE.
    expression: dict[str, Any] | None
    #: Where an already-declared name is declared. `None` unless ALREADY_DECLARED.
    declared: ResolvedConcept | None
    #: The gesture that unblocks this preset. `None` unless BLOCKED.
    gesture: str | None

    @property
    def calculated(self) -> bool:
        """A metric whose value is computed from OTHER Concepts, rather than read
        from its own mapped source measure. It is the distinction the arbitration
        is about, and it is a fact of the formula, not a label."""
        return bool(self.dependencies)

    def intent(self) -> dict[str, Any] | None:
        """The `create_concept` intent the governed change set takes, verbatim.

        Returned rather than posted. The adoption travels through
        `POST .../governance/semantic-model/change-sets` -> `prepare` ->
        `confirm`, the three functions the console dialogs and
        `governance_mcp.publish_semantic_model_change` already call. A writer
        here would be a second answer to "what does this metric mean", which is
        the one thing the Semantic Model exists to prevent.
        """
        if self.state != ADOPTABLE or self.expression is None:
            return None
        concept = self.concept
        return {
            "action": "create_concept",
            "concept": {
                "kind": concept.kind,
                "name": concept.name,
                "label": concept.label,
                "value_type": concept.value_type,
                "definition": _definition(concept, self.dependencies),
                "business_domain_refs": [],
                "expression": self.expression,
                "aggregation": dict(concept.aggregation) if concept.aggregation else None,
                "additivity_class": concept.additivity_class,
                "non_additive_dimensions": list(concept.non_additive_dimensions),
            },
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.concept.name,
            "label": self.concept.label,
            "value_type": self.concept.value_type,
            "additivity_class": self.concept.additivity_class,
            "aggregation": self.concept.aggregation,
            "calculated": self.calculated,
            "dependencies": list(self.dependencies),
            "missing_dependencies": list(self.missing_dependencies),
            "state": self.state,
            "gesture": self.gesture,
            "declared_scope": self.declared.scope if self.declared else None,
            "intent": self.intent(),
            "source": PRESET_SOURCE,
        }


def _definition(concept: PlatformConcept, dependencies: tuple[str, ...]) -> str:
    """A sentence DERIVED from the projected formula, never authored here.

    A preset that arrived with invented business prose would put words in the
    mouth of whoever adopts it. What this says is only what the seed already
    declares, in a sentence.
    """
    if len(dependencies) == 2 and concept.expression.get("op") == "ratio":
        return (
            f"{dependencies[0]} divided by {dependencies[1]}, as the delivered metric "
            f"catalogue declares it ({PRESET_SOURCE})."
        )
    return f"Preconfigured metric from the delivered catalogue ({PRESET_SOURCE})."


# ---------------------------------------------------------------------------
# The formula: which names it reads, and what they become once resolved
# ---------------------------------------------------------------------------


def dependency_names(expression: Any) -> tuple[str, ...]:
    """Every `concept_name` the projected formula reads, in reading order.

    Only `concept_name`: a projected formula never carries a `concept_ref`,
    because the projection has no Project to resolve one against. Duplicates are
    kept out; a ratio of a Concept by itself names it once.
    """
    found: list[str] = []

    def walk(node: Any) -> None:
        if not isinstance(node, Mapping):
            return
        if node.get("op") == "concept_name":
            name = str(node.get("name") or "")
            if name and name not in found:
                found.append(name)
            return
        for key in ("numerator", "denominator", "operand", "otherwise"):
            if key in node:
                walk(node[key])
        for child in node.get("operands") or ():
            walk(child)

    walk(expression)
    return tuple(found)


def resolve_expression(
    expression: Any, index: Mapping[str, ResolvedConcept]
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Replace every `concept_name` with the EXACT pair, or name what is missing.

    All or nothing: a formula half-resolved is a formula that cannot be
    published, and returning one would move the refusal from here -- where the
    missing Concept can be named -- to `prepare`, where it arrives as
    `unresolved_reference` after someone has already acted.
    """
    missing: list[str] = []

    def walk(node: Any) -> Any:
        if not isinstance(node, Mapping):
            return node
        if node.get("op") == "concept_name":
            name = str(node.get("name") or "")
            resolved = index.get(name)
            if resolved is None:
                if name and name not in missing:
                    missing.append(name)
                return node
            return resolved.as_ref()
        rebuilt = dict(node)
        for key in ("numerator", "denominator", "operand", "otherwise"):
            if key in rebuilt:
                rebuilt[key] = walk(rebuilt[key])
        if isinstance(rebuilt.get("operands"), list):
            rebuilt["operands"] = [walk(child) for child in rebuilt["operands"]]
        return rebuilt

    resolved_expression = walk(expression)
    if missing:
        return None, tuple(missing)
    return resolved_expression, ()


# ---------------------------------------------------------------------------
# The Project: what it can already read
# ---------------------------------------------------------------------------


def readable_concepts(conn: Any, project_id: str) -> dict[str, ResolvedConcept]:
    """`{name -> the version this Project would pin}`, Project scope winning.

    The SAME visibility rule `concept_resolver` applies -- this Project's
    Concepts plus the platform ones -- narrowed to published heads, because a
    formula may only pin a published version. When both scopes carry the name,
    the Project's own definition is the one that answers: E39-NFR04, *a project
    that reclassified a metric wins*.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT c.name, c.id, c.project_id, v.id AS version_id, v.label
              FROM app.semantic_concepts c
              JOIN app.semantic_concept_versions v ON v.id = c.current_version_id
             WHERE (c.project_id = %(project_id)s OR c.project_id IS NULL)
               AND c.lifecycle_status <> 'archived'
               AND v.status = 'published'
            """,
            {"project_id": project_id},
        )
        rows = cur.fetchall()

    index: dict[str, ResolvedConcept] = {}
    for name, concept_id, row_project_id, version_id, label in rows:
        scope = "project" if row_project_id else "platform"
        current = index.get(str(name))
        # A platform row never displaces a Project row; a Project row always
        # displaces a platform one.
        if current is not None and current.scope == "project" and scope == "platform":
            continue
        index[str(name)] = ResolvedConcept(
            concept_id=str(concept_id),
            version_id=str(version_id),
            scope=scope,
            label=str(label or name),
        )
    return index


def build_presets(
    catalogue: Iterable[PlatformConcept], index: Mapping[str, ResolvedConcept]
) -> list[MetricPreset]:
    """Judge every projected metric against one Project's readable Concepts. Pure."""
    presets: list[MetricPreset] = []
    for concept in catalogue:
        if concept.kind != "metric":
            continue
        dependencies = dependency_names(concept.expression)
        declared = index.get(concept.name)
        if declared is not None:
            presets.append(
                MetricPreset(
                    concept=concept,
                    state=ALREADY_DECLARED,
                    dependencies=dependencies,
                    missing_dependencies=(),
                    expression=None,
                    declared=declared,
                    gesture=None,
                )
            )
            continue
        expression, missing = resolve_expression(concept.expression, index)
        if missing:
            presets.append(
                MetricPreset(
                    concept=concept,
                    state=BLOCKED,
                    dependencies=dependencies,
                    missing_dependencies=missing,
                    expression=None,
                    declared=None,
                    gesture=_gesture(missing),
                )
            )
            continue
        presets.append(
            MetricPreset(
                concept=concept,
                state=ADOPTABLE,
                dependencies=dependencies,
                missing_dependencies=(),
                expression=expression,
                declared=None,
                gesture=None,
            )
        )
    return presets


def _gesture(missing: tuple[str, ...]) -> str:
    """The move that unblocks, named. Never the reason it is blocked.

    "This formula has an unresolved reference" tells someone a fact about a tree
    they did not write. "Declare cost first" tells them what to do next, and the
    two words after it are the name of the thing to declare.
    """
    if len(missing) == 1:
        return (
            f"Declare {missing[0]} first: this metric divides by it, and a formula pins "
            "a Concept at an exact version rather than a name."
        )
    listed = ", ".join(missing[:-1]) + f" and {missing[-1]}"
    return (
        f"Declare {listed} first: this metric is computed from them, and a formula pins "
        "each one at an exact version rather than a name."
    )


def presets_for_project(conn: Any, project_id: str) -> dict[str, Any]:
    """The whole answer one Project's console or agent reads.

    Sorted so the offer is stable between two reads: what can be adopted now,
    then what is blocked and says why, then what this Project already has.
    """
    from core.platform_semantic_concepts import load_dictionary_types  # noqa: PLC0415

    catalogue, refused = project_delivered_catalogue(load_dictionary_types(conn))
    presets = build_presets(catalogue, readable_concepts(conn, project_id))
    order = {ADOPTABLE: 0, BLOCKED: 1, ALREADY_DECLARED: 2}
    presets.sort(key=lambda preset: (order[preset.state], preset.concept.name))
    return {
        "source": PRESET_SOURCE,
        "presets": [preset.as_dict() for preset in presets],
        "counts": {
            state: sum(1 for preset in presets if preset.state == state)
            for state in (ADOPTABLE, BLOCKED, ALREADY_DECLARED)
        },
        # What the projection itself refuses to express, carried through rather
        # than dropped: a catalogue row nobody can offer is a fact about the
        # offer, and hiding it would make the list look complete.
        "not_offered": refused,
    }
