"""Story 72.2 -- the Chart Template document: the Spec grammar, DERIVED.

WHAT THIS OWNS. One grammar and one validator for the document of a Chart
Template version (`app.visualization_template_versions.document`, migration 333)
and the contract literal that names it. Nothing else: the tables belong to 72.1,
the compatibility verdict against a real Result to 72.3, the seed projection to
72.4, the materialisation into a Visualization Spec version to 72.6.

THE ONE SENTENCE THIS FILE IMPLEMENTS.
`docs/product-architecture/visualization-and-rendering.md`, § *Amendment,
2026-08-31 -- Chart Template, the ratified target*: a Chart Template owns "a
visual family and a presentation intent" and "compatibility predicates: which
roles it requires in which well, at which cardinality"; it does not own "a
concrete `member_id` -- which is exactly what separates it from a Visualization".

DERIVED, NOT COPIED, AND WHY THAT IS THE WHOLE POINT. The ratified criterion
reads: *"a Chart Template declares a well, a role, a visual family or a
responsive profile instead of importing it from the authority that defines it"*.
So `TEMPLATE_GRAMMAR` is not written here. It is COMPUTED from `GRAMMAR`
(`core.visualization_specs`), `WELL_ROLES`, `SEMANTIC_ROLES` and
`SPEC_SELECTABLE_FAMILY_IDS` (`core.visualization_families`) by
`derive_template_grammar`, under three mechanical rules:

  1. every leaf that anchors on a CONCRETE member of a pinned Query Spec version
     (`member_id`, `member_id_list`) becomes a leaf that anchors on a WELL. That
     is the same substitution the plan states for the root -- where a Spec writes
     `bindings: {well: [member_id]}` a template writes `requires: {well: {...}}`
     -- applied at every depth, because a `member_id` under `/thresholds/0` is a
     bound member exactly as much as one under `/bindings`;
  2. every leaf that anchors on evidence ONE Result carries (`evidence_id`) is
     subtracted, and a node that loses an anchor is subtracted whole: the
     qualifiers of an anchor mean nothing without it. Exactly one root key falls
     to this rule, `annotations`, whose `anchor` alone would say where to draw a
     note that names nothing;
  3. `bindings` is REPLACED by `requires`, the only block this document adds to
     the grammar it derives from.

A change to the Spec grammar therefore propagates on the next import, or makes
`test_template_grammar_has_one_authority.py` red -- it cannot silently produce a
template that carries a member. The single decision the derivation cannot make
for itself is what a well-anchored key is CALLED once its member anchor is gone;
`_WELL_ANCHOR_NAMES` holds those names, and a member anchor with no entry raises
rather than defaulting.

ONE WALKER. `walk_document` is imported. Deriving the grammar and then copying
the walker would have moved the second authority one file to the left: the
closed key set, the JSON pointers, the hostile-content scan, the materialised
defaults and the ordering that makes two equal documents hash equal all live in
`core.visualization_specs`, once. The walker is told this document's product
noun, so a Chart Template is refused in the words of a Chart Template.

THE CONTRACT LITERAL, PINNED HERE. Migration 333 deliberately left
`spec_contract_version` bounded but not fixed, saying in the file that story 72.2
owns the grammar and therefore owns the name of its contract. It is
`chart-template.v1`, and migration 334 pins it. This is also the line that makes
a valid Visualization Spec document REFUSED as a Chart Template: its
`spec_contract_version` reads `visualization-spec.v1`, its `bindings` name
members, and each of those is a separate, named refusal with its own pointer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.query_specs import canonical_hash
from core.visualization_families import (
    AVAILABLE_ROLES,
    DEFERRED_FAMILIES,
    FAMILY_IDS,
    ROLE_UNAVAILABLE_OWNER,
    ROLE_UNAVAILABLE_REASON,
    SEMANTIC_ROLES,
    SPEC_SELECTABLE_FAMILY_IDS,
    WELL_LABELS,
    WELL_ROLES,
    VisualFamily,
    get_family,
)
from core.visualization_specs import (
    GRAMMAR,
    MAX_SPEC_BYTES,
    RESPONSIVE_PROFILES,
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    ArrayOf,
    Leaf,
    MapOf,
    Node,
    VisualizationNotFound,
    VisualizationRefusal,
    VisualizationSpecRefused,
    canonical_bytes,
    walk_document,
)

__all__ = [
    "CHART_TEMPLATE_CONTRACT_VERSION",
    "CHART_TEMPLATE_SCHEMA_VERSION",
    "MAX_TEMPLATE_BYTES",
    "SUBTRACTED_FROM_SPEC",
    "TEMPLATE_GRAMMAR",
    "TEMPLATE_GRAMMAR_KEYS",
    "TemplateGrammarDerivationError",
    "ValidatedChartTemplate",
    "VisualizationNotFound",
    "VisualizationRefusal",
    "VisualizationSpecRefused",
    "derive_template_grammar",
    "normalize_template_document",
    "template_vocabulary",
    "validate_template_document",
]

#: The literal migration 334 pins, and the string that makes two template hashes
#: comparable. It travels INSIDE the hashed document, exactly as the Spec's does.
CHART_TEMPLATE_CONTRACT_VERSION = "chart-template.v1"

#: NOT a second integer. The template document is the Spec grammar derived, so it
#: is the same schema generation, and migration 333 already writes
#: `CHECK (schema_version = 1)`. A separate counter would drift the first time
#: either moved.
CHART_TEMPLATE_SCHEMA_VERSION = VISUALIZATION_SPEC_SCHEMA_VERSION

#: AC8. The same ceiling, from the same constant, measured with the same
#: serializer, mirrored by the same `pg_column_size` CHECK value in migration 333
#: -- `test_the_document_ceiling_is_one_number` reads the SQL and asserts it.
MAX_TEMPLATE_BYTES = MAX_SPEC_BYTES

#: The product word every refusal of this document uses.
CHART_TEMPLATE_NOUN = "Chart Template"


class TemplateGrammarDerivationError(RuntimeError):
    """The Spec grammar grew an anchor this derivation has no name for.

    Raised at import time on purpose. A grammar that cannot be derived must stop
    the module, not silently produce a template document that carries a member.
    """


# ---------------------------------------------------------------------------
# The derivation.
# ---------------------------------------------------------------------------

#: Leaf kinds that anchor on a CONCRETE member of a pinned Query Spec version.
_MEMBER_ANCHOR_KINDS = frozenset({"member_id", "member_id_list"})

#: Leaf kinds that anchor on evidence ONE Result carries.
_RESULT_ANCHOR_KINDS = frozenset({"evidence_id"})

#: The one decision the derivation cannot make for itself: what a well-anchored
#: key is CALLED once its member anchor is gone. Every member-anchored key of the
#: grammar being derived must have an entry here, or the derivation raises -- so
#: a new member anchor in the Spec turns this file red rather than producing a
#: template that names data.
_WELL_ANCHOR_NAMES: dict[str, str] = {
    "member_id": "well",
    "datum_fields": "datum_wells",
}

#: The root key that is not subtracted but REPLACED, with the sentence that says
#: so when a caller sends it.
_REPLACED_ROOT_KEY = "bindings"
_REPLACEMENT_ROOT_KEY = "requires"

#: Names the ratified criterion spells out one by one -- "a Chart Template
#: carries a `member_id`, a `query_spec_version_id`, a `result_id` or a data
#: value". `member_id` and the rest of the grammar's own anchors are refused by
#: the derivation; these three are not grammar keys at all, so without an entry
#: here they would be refused as merely "unknown", which reads as "spell it
#: differently" rather than "this object never carries one".
_DATA_ANCHOR_KEYS: tuple[str, ...] = (
    "query_spec_version_id",
    "query_spec_id",
    "result_id",
)

_UNBOUND_REMEDY = (
    "Remove it. A Chart Template declares the roles it requires; the data is named "
    "only when the template is applied to a Result."
)


def _well_leaf(spec: Leaf, wells: Sequence[str]) -> Leaf:
    """A member anchor, re-anchored on the well the member would occupy."""
    values = frozenset(wells)
    if spec.kind == "member_id":
        return Leaf("enum", values, default=spec.default)
    return Leaf(
        "enum_list",
        values,
        default=list(spec.default or []),
        unordered=True,
        item_noun="wells",
    )


def _requires_block(wells: Sequence[str], roles: Sequence[str]) -> MapOf:
    """`requires: {well: {min, max, accepts, max_cardinality}}` -- the one addition.

    A well ABSENT from the map is not required, which is why `min` starts at one:
    there is no "require zero of these", there is only not saying so. `max`,
    `accepts` and `max_cardinality` left out mean "whatever the family's own well
    allows" -- the template may narrow its family, never widen it, and
    `_check_requires_against_family` refuses every widening by name.
    """
    return MapOf(
        "enum",
        Node(
            {
                "min": Leaf("positive_int", default=1),
                "max": Leaf("positive_int", default=None),
                "accepts": Leaf(
                    "enum_list",
                    frozenset(roles),
                    default=[],
                    unordered=True,
                    item_noun="semantic roles",
                ),
                "max_cardinality": Leaf("positive_int", default=None),
            }
        ),
        len(wells),
        key_values=frozenset(wells),
    )


def _anchor_kinds(spec: Any) -> frozenset[str]:
    """Every leaf kind reachable from `spec`. Used to say WHY a key was subtracted."""
    if isinstance(spec, Leaf):
        return frozenset({spec.kind})
    if isinstance(spec, Node):
        return frozenset().union(*(_anchor_kinds(c) for c in spec.keys.values())) or frozenset()
    if isinstance(spec, ArrayOf):
        return _anchor_kinds(spec.item)
    if isinstance(spec, MapOf):
        return _anchor_kinds(spec.value) | frozenset({spec.key_kind})
    raise TemplateGrammarDerivationError(f"unhandled grammar node {type(spec).__name__}")


def _derive_spec(spec: Any, wells: Sequence[str], renamed: dict[str, str]) -> Any | None:
    """One grammar node, derived. `None` means the node is subtracted.

    `renamed` accumulates every member anchor that changed name, AT EVERY DEPTH.
    It is what lets a caller sending `/thresholds/0/member_id` be told the key
    that replaced it instead of being told the key is unknown.
    """
    if isinstance(spec, Leaf):
        if spec.kind in _RESULT_ANCHOR_KINDS:
            return None
        if spec.kind in _MEMBER_ANCHOR_KINDS:
            return _well_leaf(spec, wells)
        return spec

    if isinstance(spec, Node):
        keys: dict[str, Any] = {}
        for key, child in spec.keys.items():
            derived = _derive_key(key, child, wells, renamed)
            # An anchor's qualifiers have no meaning without the anchor: a node
            # that loses one is subtracted whole rather than left half-standing.
            if derived is None:
                return None
            keys[derived[0]] = derived[1]
        if not keys:
            return None
        return Node(keys, spec.default_absent, spec.nullable)

    if isinstance(spec, ArrayOf):
        item = _derive_spec(spec.item, wells, renamed)
        return None if item is None else ArrayOf(item, spec.max_items)

    if isinstance(spec, MapOf):
        value = _derive_spec(spec.value, wells, renamed)
        if value is None:
            return None
        if spec.key_kind in _MEMBER_ANCHOR_KINDS:
            return MapOf("enum", value, spec.max_items, key_values=frozenset(wells))
        if spec.key_kind in _RESULT_ANCHOR_KINDS:
            return None
        return MapOf(spec.key_kind, value, spec.max_items, spec.key_values)

    raise TemplateGrammarDerivationError(f"unhandled grammar node {type(spec).__name__}")


def _derive_key(
    key: str, spec: Any, wells: Sequence[str], renamed: dict[str, str]
) -> tuple[str, Any] | None:
    derived = _derive_spec(spec, wells, renamed)
    if derived is None:
        return None
    if isinstance(spec, Leaf) and spec.kind in _MEMBER_ANCHOR_KINDS:
        name = _WELL_ANCHOR_NAMES.get(key)
        if name is None:
            raise TemplateGrammarDerivationError(
                f"`{key}` anchors on a member and this derivation has no well-anchored name "
                f"for it. Name it in `_WELL_ANCHOR_NAMES` -- a Chart Template may not carry "
                f"`{key}` as it stands."
            )
        renamed[key] = name
        return name, derived
    return key, derived


def _subtraction_reason(key: str, spec: Any) -> str:
    kinds = _anchor_kinds(spec)
    if kinds & _RESULT_ANCHOR_KINDS:
        return (
            f"`{key}` anchors on evidence that one Result carries; a Chart Template names "
            f"no Result."
        )
    return (
        f"`{key}` anchors on data a Chart Template does not name."
    )  # pragma: no cover - no such key today


@dataclass(frozen=True)
class DerivedGrammar:
    """The derivation's whole answer: the grammar, and what it did to get there."""

    grammar: dict[str, Any]
    #: `{spec root key: why it is not in this grammar}`. Rendered as refusals, so
    #: a Spec document sent as a template is told what happened to each key.
    subtracted: dict[str, str]
    #: `{spec key: template key}` for the anchors that were re-anchored on a well.
    renamed: dict[str, str]


def derive_template_grammar(
    spec_grammar: Mapping[str, Any],
    *,
    wells: Sequence[str],
    roles: Sequence[str],
    families: Sequence[str],
    contract_version: str,
) -> DerivedGrammar:
    """The Chart Template grammar, computed from the Visualization Spec grammar.

    Pure, and parametrized on every authority it reads, so the conformance test
    can MUTATE an authority and prove the derivation followed -- a well added to
    `WELL_ROLES` reaches `requires`, a root key added to `GRAMMAR` crosses over,
    a member anchor added without a well-anchored name raises.
    """
    grammar: dict[str, Any] = {}
    subtracted: dict[str, str] = {}
    renamed: dict[str, str] = {}

    for key, child in spec_grammar.items():
        if key == _REPLACED_ROOT_KEY:
            grammar[_REPLACEMENT_ROOT_KEY] = _requires_block(wells, roles)
            subtracted[key] = (
                "a binding names a member of one pinned question; a Chart Template "
                "declares `requires` and names no member."
            )
            renamed[key] = _REPLACEMENT_ROOT_KEY
            continue
        derived = _derive_key(key, child, wells, renamed)
        if derived is None:
            subtracted[key] = _subtraction_reason(key, child)
            continue
        name, spec = derived
        if name != key:
            renamed[key] = name
        grammar[name] = spec

    # The two identity narrowings, in place so they keep their position in the
    # document. Both are IMPORTED values, never transcribed: the contract literal
    # is this module's own, and the family enum is the shipped registry's list of
    # families a Result can actually fill.
    grammar["spec_contract_version"] = Leaf("literal_str", literal=contract_version)
    grammar["family"] = Leaf("enum", frozenset(families))

    # The declared question. Ratified in the same amendment: the list screen
    # "states the question the template answers" and the workbench Overview shows
    # "The question". It is versioned with the document rather than held on the
    # head, because changing what a template claims to answer changes what it is,
    # and that is an edit that must produce a new immutable version.
    grammar["answers_question"] = Leaf("label")

    return DerivedGrammar(grammar=grammar, subtracted=subtracted, renamed=renamed)


_DERIVED = derive_template_grammar(
    GRAMMAR,
    wells=WELL_ROLES,
    roles=SEMANTIC_ROLES,
    families=SPEC_SELECTABLE_FAMILY_IDS,
    contract_version=CHART_TEMPLATE_CONTRACT_VERSION,
)

#: THE grammar of a Chart Template document. Derived, never written.
TEMPLATE_GRAMMAR: dict[str, Any] = _DERIVED.grammar

TEMPLATE_GRAMMAR_KEYS: tuple[str, ...] = tuple(TEMPLATE_GRAMMAR.keys())

#: `{Visualization Spec key: why a Chart Template does not carry it}`.
SUBTRACTED_FROM_SPEC: dict[str, str] = dict(_DERIVED.subtracted)

#: `{Visualization Spec key: the Chart Template key that replaced it}`.
RE_ANCHORED_ON_A_WELL: dict[str, str] = dict(_DERIVED.renamed)

#: The identity keys a document may not leave to a default.
REQUIRED_KEYS: tuple[str, ...] = (
    "spec_contract_version",
    "schema_version",
    "family",
    "answers_question",
    "requires",
)


def _owned_map() -> dict[str, tuple[str, str, str]]:
    """The keys this grammar does not declare but whose owner is known.

    Built from the derivation, not typed: a key subtracted or re-anchored by the
    rules above is named by the same sentence the rules produced.
    """
    owned: dict[str, tuple[str, str, str]] = {}
    for key, reason in SUBTRACTED_FROM_SPEC.items():
        remedy = (
            f"Declare `{RE_ANCHORED_ON_A_WELL[key]}` instead."
            if key in RE_ANCHORED_ON_A_WELL
            else _UNBOUND_REMEDY
        )
        owned[key] = ("template_is_unbound", reason, remedy)
    for spec_key, template_key in RE_ANCHORED_ON_A_WELL.items():
        owned.setdefault(
            spec_key,
            (
                "template_is_unbound",
                f"`{spec_key}` names data; a Chart Template names the well the data will "
                f"occupy.",
                f"Declare `{template_key}` instead, with one of "
                f"{', '.join(WELL_ROLES)}.",
            ),
        )
    for key in _DATA_ANCHOR_KEYS:
        owned.setdefault(
            key,
            (
                "template_is_unbound",
                f"`{key}` names one exact question or one exact Result; a Chart Template is "
                f"reusable across questions and names neither.",
                _UNBOUND_REMEDY,
            ),
        )
    return owned


#: Passed to the shared walker so every subtracted key is refused BY NAME with
#: its owner, at whatever depth it is sent.
TEMPLATE_OWNED_KEYS: dict[str, tuple[str, str, str]] = _owned_map()


# ---------------------------------------------------------------------------
# The family verdict (AC6): refused by its own name, never substituted.
# ---------------------------------------------------------------------------

_DEFERRED_FAMILY_REASONS: dict[str, str] = {
    str(entry["id"]): str(entry["reason"]) for entry in DEFERRED_FAMILIES
}

_DRAWABLE = ", ".join(SPEC_SELECTABLE_FAMILY_IDS)


def _family_refusal(value: str) -> VisualizationRefusal:
    """Why THIS family cannot be a Chart Template's, in its own words.

    Three distinct sentences, because three distinct facts. A deferred family is
    named with the reason the registry records; a family declared but unfillable
    is named with the role that has no source and the surface that owns it; a
    word that is no family at all is told so. None of the three is answered by
    quietly choosing a neighbouring family, which the ratified criterion forbids.
    """
    reason = _DEFERRED_FAMILY_REASONS.get(value)
    if reason is not None:
        return VisualizationRefusal(
            "family_not_drawn",
            f"`{value}` is not a family this deployment draws: {reason}",
            "/family",
            f"Choose one of {_DRAWABLE}. No neighbouring family is substituted for it.",
        )
    if value in FAMILY_IDS:
        return VisualizationRefusal(
            "family_not_fillable",
            f"`{value}` is declared by the registry, but no Result can fill its required "
            f"wells: {ROLE_UNAVAILABLE_REASON}",
            "/family",
            f"Choose one of {_DRAWABLE}, or wait for {ROLE_UNAVAILABLE_OWNER}.",
        )
    return VisualizationRefusal(
        "family_not_drawn",
        f"`{value}` is not a visual family this deployment declares",
        "/family",
        f"Choose one of {_DRAWABLE}.",
    )


def _refine_family_refusal(payload: Any, refusals: list[VisualizationRefusal]) -> None:
    """Replace the enum's generic verdict with the named one. One control, one reason."""
    if not isinstance(payload, dict):
        return
    value = payload.get("family")
    if not isinstance(value, str) or not value:
        return
    if value in SPEC_SELECTABLE_FAMILY_IDS:
        return
    at_family = [index for index, r in enumerate(refusals) if r.subject == "/family"]
    named = _family_refusal(value)
    if at_family:
        refusals[at_family[0]] = named
        for index in reversed(at_family[1:]):
            del refusals[index]
    else:  # pragma: no cover - the enum always refuses first
        refusals.append(named)


# ---------------------------------------------------------------------------
# `requires` against the family it names.
#
# A template may NARROW its family and never widen it. The deterministic rules
# stay authoritative (`visualization-and-rendering.md:254-256`): a template that
# required a well its family has not, or more members than the well holds, would
# be a compatibility predicate no Result could ever satisfy -- a starting point
# that never starts.
# ---------------------------------------------------------------------------


def _check_requires_against_family(
    normalized: Mapping[str, Any], family: VisualFamily
) -> list[VisualizationRefusal]:
    refusals: list[VisualizationRefusal] = []
    requires = normalized.get("requires") or {}

    for well_name in sorted(requires):
        entry = requires.get(well_name) or {}
        pointer = f"/{_REPLACEMENT_ROOT_KEY}/{well_name}"
        well = family.well(well_name)

        if well is None:
            refusals.append(
                VisualizationRefusal(
                    "role_mismatch",
                    f"the {family.label} family has no {WELL_LABELS[well_name]} well",
                    pointer,
                    f"Remove this requirement, or choose a family that declares "
                    f"{WELL_LABELS[well_name]}.",
                )
            )
            continue

        if not well.available:
            refusals.append(
                VisualizationRefusal(
                    "role_unavailable",
                    f"the {WELL_LABELS[well_name]} well cannot be required: "
                    f"{ROLE_UNAVAILABLE_REASON}",
                    pointer,
                    f"Leave it out until {ROLE_UNAVAILABLE_OWNER} carries the role.",
                )
            )
            continue

        minimum = entry.get("min")
        maximum = entry.get("max")
        accepts = list(entry.get("accepts") or [])
        cardinality = entry.get("max_cardinality")

        if isinstance(minimum, int) and minimum > well.max_members:
            refusals.append(
                VisualizationRefusal(
                    "too_many_members",
                    f"{WELL_LABELS[well_name]} holds at most {well.max_members} member(s) on "
                    f"the {family.label} family; this template requires {minimum}",
                    f"{pointer}/min",
                    f"Require at most {well.max_members}, or choose a family whose "
                    f"{WELL_LABELS[well_name]} well holds more.",
                )
            )
        if isinstance(maximum, int) and maximum > well.max_members:
            refusals.append(
                VisualizationRefusal(
                    "too_many_members",
                    f"{WELL_LABELS[well_name]} holds at most {well.max_members} member(s) on "
                    f"the {family.label} family; this template allows {maximum}",
                    f"{pointer}/max",
                    f"Allow at most {well.max_members}.",
                )
            )
        if isinstance(minimum, int) and isinstance(maximum, int) and minimum > maximum:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"{WELL_LABELS[well_name]} requires at least {minimum} member(s) and "
                    f"allows at most {maximum}",
                    f"{pointer}/max",
                    f"Allow at least {minimum}.",
                )
            )

        widened = sorted(set(accepts) - set(well.accepts))
        if widened:
            refusals.append(
                VisualizationRefusal(
                    "role_mismatch",
                    f"the {WELL_LABELS[well_name]} well of the {family.label} family accepts "
                    f"{' or '.join(sorted(well.accepts))}; this template also asks for "
                    f"{' and '.join(widened)}",
                    f"{pointer}/accepts",
                    f"Ask for {' or '.join(sorted(well.accepts))}, or choose a family whose "
                    f"{WELL_LABELS[well_name]} well accepts more.",
                )
            )
        unavailable = sorted(set(accepts) - set(AVAILABLE_ROLES))
        if unavailable:
            refusals.append(
                VisualizationRefusal(
                    "role_unavailable",
                    f"{' and '.join(unavailable)} cannot be required: {ROLE_UNAVAILABLE_REASON}",
                    f"{pointer}/accepts",
                    f"Ask for {' or '.join(sorted(AVAILABLE_ROLES))} until "
                    f"{ROLE_UNAVAILABLE_OWNER} carries the role.",
                )
            )

        if (
            isinstance(cardinality, int)
            and well.max_cardinality is not None
            and cardinality > well.max_cardinality
        ):
            refusals.append(
                VisualizationRefusal(
                    "cardinality_over_limit",
                    f"the {WELL_LABELS[well_name]} well of the {family.label} family reads at "
                    f"most {well.max_cardinality} distinct values; this template allows "
                    f"{cardinality}",
                    f"{pointer}/max_cardinality",
                    f"Allow at most {well.max_cardinality}, or choose a family that reads more.",
                )
            )
        if isinstance(cardinality, int) and well.max_cardinality is None:
            refusals.append(
                VisualizationRefusal(
                    "invalid_value",
                    f"the {WELL_LABELS[well_name]} well of the {family.label} family carries no "
                    f"cardinality risk, so a cardinality bound says nothing about it",
                    f"{pointer}/max_cardinality",
                    "Remove the bound, or set it on a well that carries a dimension.",
                )
            )

    # The other direction, and it is the one that keeps a template a STARTING
    # POINT: a family's required well that the template does not require would
    # materialise into a Visualization Spec the ordinary validator refuses with
    # `missing_binding` -- a template that can never be applied.
    for well in family.wells:
        if not well.required or not well.available:
            continue
        entry = requires.get(well.name)
        if not isinstance(entry, dict) or not isinstance(entry.get("min"), int):
            refusals.append(
                VisualizationRefusal(
                    "missing_requirement",
                    f"the {family.label} family needs a member in {WELL_LABELS[well.name]}, and "
                    f"this template does not require one",
                    f"/{_REPLACEMENT_ROOT_KEY}/{well.name}",
                    f"Require at least one member in {WELL_LABELS[well.name]}.",
                )
            )
    return refusals


# ---------------------------------------------------------------------------
# Validation, and the hashable document it produces.
# ---------------------------------------------------------------------------


def _check_well_anchors(normalized: Mapping[str, Any]) -> list[VisualizationRefusal]:
    """Every re-anchored control names the well it is about.

    A threshold or a reference line whose `well` is absent anchors on nothing.
    In a Visualization Spec the equivalent absence is survivable -- the member is
    optional there and the compatibility pass simply skips it -- but a template
    IS its predicates, and a predicate about no well says nothing a Result could
    satisfy or fail.
    """
    refusals: list[VisualizationRefusal] = []
    for block in ("thresholds", "reference_lines"):
        for index, entry in enumerate(normalized.get(block) or []):
            if not (entry or {}).get("well"):
                refusals.append(
                    VisualizationRefusal(
                        "missing_field",
                        f"this {block[:-1].replace('_', ' ')} names no well, so nothing in a "
                        f"Result can satisfy or fail it",
                        f"/{block}/{index}/well",
                        f"Name one of {', '.join(WELL_ROLES)}.",
                    )
                )
    return refusals


@dataclass(frozen=True)
class ValidatedChartTemplate:
    """A presentation starting point proven legal, and bound to nothing."""

    document: dict[str, Any]
    content_hash: str
    family: str
    answers_question: str
    #: `{well: {min, max, accepts, max_cardinality}}` -- the compatibility
    #: predicates, carried so 72.3 does not re-read the document to state them.
    requires: dict[str, Any]


def normalize_template_document(
    payload: Any,
) -> tuple[dict[str, Any], list[VisualizationRefusal]]:
    """Walk one proposed Chart Template document. Returns (normalized, refusals)."""
    normalized, refusals = walk_document(
        payload,
        TEMPLATE_GRAMMAR,
        noun=CHART_TEMPLATE_NOUN,
        required=REQUIRED_KEYS,
        contract_literal=CHART_TEMPLATE_CONTRACT_VERSION,
        owned=TEMPLATE_OWNED_KEYS,
    )
    _refine_family_refusal(payload, refusals)
    return normalized, refusals


def validate_template_document(payload: Any) -> ValidatedChartTemplate:
    """Validate one proposed Chart Template document. Every reason at once.

    Raises `VisualizationSpecRefused` carrying EVERY refusal, each anchored by a
    JSON pointer on the well or the field it is about. Never repairs, never
    substitutes, never strips: a stripped key is a rejected instruction the next
    reader cannot see.

    A valid Visualization Spec document is REFUSED here, and not by accident: its
    `spec_contract_version` is the Spec's, its `bindings` name members, its
    `annotations` name evidence. Each is a separate refusal with its own subject.
    """
    normalized, refusals = normalize_template_document(payload)

    family_id = normalized.get("family")
    family = get_family(family_id) if isinstance(family_id, str) else None
    if family is None:
        # A family the walker already refused produces no second refusal at the
        # same pointer: one control, one reason. Mirrors the Spec validator.
        if not any(r.subject == "/family" for r in refusals):
            refusals.append(
                VisualizationRefusal(
                    "family_not_drawn",
                    f"a visual family is required; this deployment draws {_DRAWABLE}",
                    "/family",
                    f"Choose one of {_DRAWABLE}.",
                )
            )
    elif family_id in SPEC_SELECTABLE_FAMILY_IDS:
        refusals.extend(_check_requires_against_family(normalized, family))

    refusals.extend(_check_well_anchors(normalized))

    size = len(canonical_bytes(normalized))
    if size > MAX_TEMPLATE_BYTES:
        refusals.append(
            VisualizationRefusal(
                "invalid_value",
                f"this document is {size} bytes; a {CHART_TEMPLATE_NOUN} may not exceed "
                f"{MAX_TEMPLATE_BYTES}",
                "",
                "Remove thresholds, reference lines or label overrides.",
            )
        )

    if refusals:
        raise VisualizationSpecRefused(
            "chart_template_refused",
            f"the template was refused on {len(refusals)} point(s)",
            refusals,
        )

    return ValidatedChartTemplate(
        document=normalized,
        content_hash=canonical_hash(normalized),
        family=str(family_id),
        answers_question=str(normalized.get("answers_question") or ""),
        requires=dict(normalized.get("requires") or {}),
    )


def template_vocabulary() -> dict[str, Any]:
    """The whole vocabulary a Chart Template speaks, as the console receives it.

    Every entry is IMPORTED from the authority that defines it. The console
    renders this and keeps no second copy: a browser carrying its own well list
    would be a second vocabulary, and the ratified criterion refuses one.
    """
    return {
        "contract_version": CHART_TEMPLATE_CONTRACT_VERSION,
        "schema_version": CHART_TEMPLATE_SCHEMA_VERSION,
        "max_bytes": MAX_TEMPLATE_BYTES,
        "wells": [{"name": name, "label": WELL_LABELS[name]} for name in WELL_ROLES],
        "roles": list(SEMANTIC_ROLES),
        "available_roles": sorted(AVAILABLE_ROLES),
        "families": list(SPEC_SELECTABLE_FAMILY_IDS),
        #: EACH FAMILY WITH ITS OWN WELLS, so a person editing a template is
        #: offered the wells the family HAS, at the bounds the family holds, and
        #: is never offered a requirement `_check_requires_against_family` would
        #: refuse a moment later. Read entirely off `VisualFamily`/`Well`: the
        #: label, the description, `required`, `max_members`, `max_cardinality`,
        #: `accepts` and the availability sentence are the registry's own, so the
        #: editing screen carries no second copy of a rule and no family name of
        #: its own -- the ratified criterion refuses both.
        "family_catalogue": [
            {
                "id": family.id,
                "label": family.label,
                "description": family.description,
                "wells": [well.as_dict() for well in family.wells],
            }
            for family in (get_family(fid) for fid in SPEC_SELECTABLE_FAMILY_IDS)
            if family is not None
        ],
        "deferred_families": [dict(entry) for entry in DEFERRED_FAMILIES],
        "responsive_profiles": list(RESPONSIVE_PROFILES),
        "document_keys": list(TEMPLATE_GRAMMAR_KEYS),
        "subtracted_from_visualization_spec": dict(SUBTRACTED_FROM_SPEC),
        # Named so a reader who knows the Spec can see what this document is NOT,
        # rather than discovering it one refusal at a time.
        "derived_from": VISUALIZATION_SPEC_CONTRACT_VERSION,
    }
