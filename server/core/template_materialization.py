"""Story 72.6 -- from a Chart Template to a project-owned Visualization Spec version.

WHAT THIS OWNS. One function, `materialize_template`: it takes one Chart Template
version and one Result, resolves every `requires` entry into concrete
`member_id`s BY THE SCHEMA THE RESULT DECLARES, and writes an ordinary
Visualization Spec version. Nothing else: the tables belong to 72.1, the document
grammar to 72.2, the compatibility verdict to 72.3, the seed projection to 72.4,
the screens to 72.5 and the MCP door to 72.7.

THE SENTENCE THIS FILE IMPLEMENTS.
`docs/product-architecture/visualization-and-rendering.md`, § *Amendment,
2026-08-31 -- Chart Template, the ratified target*: *"from the workbench the
template is applied to a Result and yields a Visualization Spec version of the
Project, without leaving the screen"*, bounded by the criterion *"a
materialisation from a template creates a Result, re-executes a query, or
produces a Visualization Spec version the ordinary validator would have
refused"*.

FOUR THINGS THIS FILE DOES NOT DO, EACH ONE ON PURPOSE.

  * IT DOES NOT VALIDATE. `validate_visualization_spec` does, the same call the
    Builder makes. A materialised document that the ordinary validator refuses is
    refused here too, with the ordinary refusals, and nothing is written (AC23).
  * IT DOES NOT WRITE. `create_visualization_spec_version` does, and it is the
    only writer of a Spec version in this repository. A second INSERT would be a
    second head-and-lineage rule, and the two would disagree on the first edit.
  * IT DOES NOT JUDGE COMPATIBILITY. `check_template_compatibility` (story 72.3)
    does, on the predicates its own constructor read. This file calls it FIRST and
    refuses on anything but `compatible`, before it has composed a document, let
    alone opened a write (AC25).
  * IT DOES NOT EXECUTE ANYTHING. No query runs, no Result is created, no row of
    a Result is re-read for anything but the cardinality the verdict already
    counted. The Spec pins the `query_spec_version_id` THE RESULT ALREADY CARRIES
    (AC26, AD-10): a materialisation is a presentation act, and a presentation act
    that re-ran the question would answer a different one.

CHOOSING WHICH MEMBER GOES WHERE -- the one decision this story adds. Story 72.3
answers whether a choice EXISTS, predicate by predicate; this file makes it, and
makes it the same way twice:

  * a well takes exactly the number of members its predicate asks for. The
    verdict says `compatible` when at least `min` candidates exist, so binding
    `min` is the choice the verdict already vouched for. Binding every candidate
    a well could hold would put a member on the chart that no predicate asked
    for, and a template is a starting point rather than a layout of somebody
    else's Result;
  * candidates are taken IN THE ORDER THE RESULT DECLARES THEM, never sorted by
    name -- the schema's order is the only order either object states;
  * a member already placed in another well is used again only when nothing else
    fits. Nothing forbids one member occupying two wells (72.3 judges wells
    independently and says so), so refusing here would refuse a materialisation
    the verdict called compatible; preferring an unplaced member is what keeps
    the ordinary case sane.

THE RE-ANCHORING IS INVERTED, NOT RETYPED. A Chart Template document is the Spec
grammar with every member anchor re-anchored on a WELL (story 72.2). Turning one
back into a Spec means walking those exact anchors backwards, and there are five
of them today. `_MEMBER_ANCHOR_SITES` names the ones this file handles, and
`_verify_every_member_anchor_is_handled` reads them off `GRAMMAR` itself at import
time: a Spec grammar that grows a sixth anchor raises here rather than silently
producing a Spec with a well name where a member id belongs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from core.template_compatibility import (
    COMPATIBLE,
    CompatibilityVerdict,
    OfferedMember,
    ResultFacts,
    TemplatePredicates,
    check_template_compatibility,
    read_result_facts,
    read_template_predicates,
)
from core.visualization_families import (
    AVAILABLE_ROLES,
    WELL_LABELS,
    WELL_ROLES,
    VisualFamily,
    get_family,
)
from core.visualization_specs import (
    GRAMMAR,
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    ArrayOf,
    Leaf,
    MapOf,
    Node,
    VisualizationNotFound,
    VisualizationRefusal,
    VisualizationSpecRefused,
    create_visualization_spec_version,
    validate_visualization_spec,
)
from core.visualization_templates import (
    CHART_TEMPLATE_NOUN,
    validate_template_document,
)

__all__ = [
    "MaterializationRefused",
    "TemplateMaterializationUnsupported",
    "WellPlan",
    "materialize_template",
    "plan_bindings",
    "spec_payload_from_template",
]


class TemplateMaterializationUnsupported(RuntimeError):
    """The Visualization Spec grammar grew a member anchor this file cannot invert.

    Raised at IMPORT time, like `TemplateGrammarDerivationError` in story 72.2 and
    for the same reason: a materialisation that quietly left a well name where a
    member id belongs would produce a document the validator refuses -- or worse,
    one it accepts because the well name happens to look like a member id.
    """


class MaterializationRefused(VisualizationSpecRefused):
    """This template cannot be materialised against this Result, and why.

    Carries the whole three-state verdict rather than a boolean, so a caller
    renders "nobody knows yet" and "this Result cannot answer this way"
    differently -- the confusion story 72.3 exists to prevent. `code` says which
    of the two it is, and `refusals` carries every unsatisfied predicate at once,
    each already anchored on the control it is about in the TEMPLATE document.
    """

    def __init__(self, verdict: CompatibilityVerdict, *, code: str, message: str):
        super().__init__(
            code,
            message,
            list(verdict.unmet) or ([verdict.unreadable] if verdict.unreadable else []),
        )
        self.verdict = verdict

    def as_dict(self) -> dict[str, Any]:
        payload = super().as_dict()
        payload["verdict"] = self.verdict.as_dict()
        return payload


# ---------------------------------------------------------------------------
# The anchors this file inverts, read off the Spec grammar rather than listed.
# ---------------------------------------------------------------------------

#: Leaf kinds and map key kinds that name a CONCRETE member of a pinned Query Spec
#: version. Same two words story 72.2 re-anchors on a well, in the same order of
#: authority: the grammar says which they are, this file says how to fill them.
_MEMBER_ANCHOR_KINDS = frozenset({"member_id", "member_id_list"})

#: The template key that replaces `bindings`, and the two keys the template
#: grammar renamed. Imported meanings, spelled once.
_REQUIRES = "requires"
_WELL_KEY = "well"
_DATUM_WELLS = "datum_wells"
_DATUM_FIELDS = "datum_fields"

#: Where a member anchor lives in a Visualization Spec document, as paths through
#: `GRAMMAR`. `[]` stands for "every item of this array". Every one of them is
#: filled below; the import-time check proves the list is exhaustive.
_MEMBER_ANCHOR_SITES: frozenset[tuple[str, ...]] = frozenset(
    {("bindings", well) for well in WELL_ROLES}
    | {
        ("thresholds", "[]", "member_id"),
        ("reference_lines", "[]", "member_id"),
        ("evidence", "datum_fields"),
        ("labels", "override"),
    }
)


def _member_anchor_paths(spec: Any, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Every path of `GRAMMAR` that names a concrete member. Read, never typed."""
    if isinstance(spec, Leaf):
        return {path} if spec.kind in _MEMBER_ANCHOR_KINDS else set()
    if isinstance(spec, Node):
        found: set[tuple[str, ...]] = set()
        for key, child in spec.keys.items():
            found |= _member_anchor_paths(child, path + (key,))
        return found
    if isinstance(spec, ArrayOf):
        return _member_anchor_paths(spec.item, path + ("[]",))
    if isinstance(spec, MapOf):
        found = _member_anchor_paths(spec.value, path)
        if spec.key_kind in _MEMBER_ANCHOR_KINDS:
            found.add(path)
        return found
    raise TemplateMaterializationUnsupported(  # pragma: no cover - unreachable today
        f"unhandled grammar node {type(spec).__name__}"
    )


def _verify_every_member_anchor_is_handled() -> None:
    found: set[tuple[str, ...]] = set()
    for key, child in GRAMMAR.items():
        found |= _member_anchor_paths(child, (key,))
    unhandled = found - _MEMBER_ANCHOR_SITES
    if unhandled:
        raise TemplateMaterializationUnsupported(
            "the Visualization Spec grammar names a member at "
            + ", ".join("/" + "/".join(path) for path in sorted(unhandled))
            + f", and this materialisation has no rule for it. A {CHART_TEMPLATE_NOUN} "
            "re-anchors it on a well (core.visualization_templates); teach "
            "`spec_payload_from_template` how to fill it back in."
        )


_verify_every_member_anchor_is_handled()


# ---------------------------------------------------------------------------
# The choice: which member occupies which well.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WellPlan:
    """The members chosen for one well, in the order they will be bound."""

    well: str
    members: tuple[str, ...]


def _accepted_roles(entry: Mapping[str, Any] | None, well) -> frozenset[str]:
    """Which roles may occupy this well -- the template's narrowing, or the family's.

    The same reading `template_compatibility._accepted_roles` makes, and it must
    stay the same reading: a choice made on a wider role set than the verdict
    judged would bind a member the verdict never vouched for.
    """
    narrowed = [role for role in (entry or {}).get("accepts") or [] if isinstance(role, str)]
    if narrowed:
        return frozenset(narrowed) & frozenset(well.accepts)
    return frozenset(well.accepts)


def _cardinality_bound(entry: Mapping[str, Any] | None, well) -> int | None:
    raw = (entry or {}).get("max_cardinality")
    return raw if isinstance(raw, int) else well.max_cardinality


def _needed(entry: Mapping[str, Any] | None) -> int:
    raw = (entry or {}).get("min")
    return raw if isinstance(raw, int) and raw > 0 else 1


def _wells_named_by_a_control(document: Mapping[str, Any]) -> list[str]:
    """Wells a control names without `requires` declaring them.

    A threshold, a reference line, an evidence field or a label override names a
    WELL in a template document and a MEMBER in a Spec, so a well named there must
    be filled even when the template declares no requirement for it -- otherwise
    the inverted document would carry an empty member id, which the ordinary
    validator refuses. Filling it needs one member, which is the least a control
    about it can mean.
    """
    named: list[str] = []
    for block in ("thresholds", "reference_lines"):
        for entry in document.get(block) or []:
            well = (entry or {}).get(_WELL_KEY)
            if isinstance(well, str) and well:
                named.append(well)
    named.extend(
        well
        for well in (document.get("evidence") or {}).get(_DATUM_WELLS) or []
        if isinstance(well, str) and well
    )
    named.extend(
        well
        for well in ((document.get("labels") or {}).get("override") or {})
        if isinstance(well, str) and well
    )
    return named


def _candidates(
    result: ResultFacts, accepts: Iterable[str], bound: int | None
) -> tuple[OfferedMember, ...]:
    """The members this Result offers for one well, in the order it declares them.

    Filtered by the cardinality bound exactly as the verdict filtered them, and
    only when the rows were read: `distinct_values` is `None` when they were not,
    and the verdict is then `unavailable`, so this branch never runs on a
    materialisation.
    """
    offered = result.members_for(frozenset(accepts) & AVAILABLE_ROLES)
    if bound is None or result.distinct_values is None:
        return offered
    return tuple(
        member for member in offered if result.distinct_values.get(member.member_id, 0) <= bound
    )


def plan_bindings(
    template: TemplatePredicates,
    family: VisualFamily,
    result: ResultFacts,
    *,
    control_wells: Sequence[str] = (),
) -> tuple[dict[str, list[str]], list[VisualizationRefusal]]:
    """`{well: [member_id]}` for every well that must be filled, or why it cannot be.

    Pure: it decides nothing about the database and reads no connection. Returns
    the refusals rather than raising them, so a caller reports every unfillable
    well at once -- the same rule every refusal on this path follows.
    """
    requires = template.requires
    wanted = [
        well
        for well in WELL_ROLES
        if well in requires
        or well in control_wells
        or (
            (found := family.well(well)) is not None and found.required and found.available
        )
    ]

    plan: dict[str, list[str]] = {}
    refusals: list[VisualizationRefusal] = []
    placed: set[str] = set()

    for well_name in wanted:
        well = family.well(well_name)
        if well is None:
            #  Unreachable from a document this deployment can read: story 72.2
            #  refuses a `requires` naming a well the family has not, and a control
            #  can only name a well of `WELL_ROLES`. Named rather than skipped.
            refusals.append(
                VisualizationRefusal(
                    "role_mismatch",
                    f"this {CHART_TEMPLATE_NOUN} names {WELL_LABELS[well_name]}, and the "
                    f"{family.label} family has no such well",
                    f"/{_REQUIRES}/{well_name}",
                    f"Open the {CHART_TEMPLATE_NOUN} and save a new version whose "
                    f"requirements match its family.",
                )
            )
            continue

        entry = requires.get(well_name)
        needed = _needed(entry)
        accepts = _accepted_roles(entry, well)
        available = _candidates(result, accepts, _cardinality_bound(entry, well))

        #  An unplaced member first, then one already placed elsewhere. Both lists
        #  keep the Result's own order.
        ordered = [m for m in available if m.member_id not in placed]
        ordered += [m for m in available if m.member_id in placed]
        chosen = ordered[:needed]

        if len(chosen) < needed:
            roles = " or ".join(sorted(accepts)) or "member"
            refusals.append(
                VisualizationRefusal(
                    "missing_role",
                    f"this {CHART_TEMPLATE_NOUN} needs {needed} {roles} member(s) in "
                    f"{WELL_LABELS[well_name]}, and this Result offers "
                    f"{len(available)}",
                    f"/{_REQUIRES}/{well_name}/min",
                    f"Ask the question again in Explore with {needed} {roles} member(s) "
                    f"-- which produces a new Result -- or choose a template that needs "
                    f"fewer.",
                )
            )
            continue

        plan[well_name] = [m.member_id for m in chosen]
        placed.update(plan[well_name])

    return plan, refusals


# ---------------------------------------------------------------------------
# The inversion: one template document plus one plan, as a Spec document.
# ---------------------------------------------------------------------------

#: Keys of a Chart Template document that do not cross over, and why. `requires`
#: becomes `bindings`; `answers_question` is what the TEMPLATE claims to answer
#: and belongs to the template's own version -- a Visualization answers the
#: question its pinned Query Spec asks, and a second sentence inside the Spec
#: would be a claim nobody validated against the query.
_NOT_CARRIED_OVER: frozenset[str] = frozenset({_REQUIRES, "answers_question"})


def _first_member(plan: Mapping[str, Sequence[str]], well: str) -> str | None:
    members = plan.get(well) or []
    return members[0] if members else None


def spec_payload_from_template(
    document: Mapping[str, Any], plan: Mapping[str, Sequence[str]]
) -> dict[str, Any]:
    """One validated template document, re-anchored on the members of one Result.

    Pure, and deliberately NOT a validator: what comes out goes straight into
    `validate_visualization_spec`, which is the only judge of a Visualization Spec
    document there is. The five member anchors of the grammar are filled here and
    nowhere else -- the import-time check above proves there is no sixth.
    """
    payload: dict[str, Any] = {
        key: value for key, value in document.items() if key not in _NOT_CARRIED_OVER
    }

    #  The two identity keys of the OTHER contract. A template document carries
    #  `chart-template.v1`; the Spec it becomes carries the Spec's literal, which
    #  is what makes the two hashes incomparable and both of them honest.
    payload["spec_contract_version"] = VISUALIZATION_SPEC_CONTRACT_VERSION
    payload["schema_version"] = VISUALIZATION_SPEC_SCHEMA_VERSION

    #  1. `requires` -> `bindings`. Every well of the vocabulary is present, so a
    #     well nobody filled reads as the empty list the grammar defaults to.
    payload["bindings"] = {well: list(plan.get(well) or []) for well in WELL_ROLES}

    #  2 and 3. A threshold and a reference line name a well in a template and a
    #     member in a Spec. The FIRST member of the well: a control is about one
    #     series, and the well that holds several holds them in the Result's order.
    for block in ("thresholds", "reference_lines"):
        entries = []
        for entry in document.get(block) or []:
            rewritten = {key: value for key, value in (entry or {}).items() if key != _WELL_KEY}
            member = _first_member(plan, str((entry or {}).get(_WELL_KEY) or ""))
            if member is not None:
                rewritten["member_id"] = member
            entries.append(rewritten)
        payload[block] = entries

    #  4. `evidence.datum_wells` -> `evidence.datum_fields`, every member of every
    #     named well: evidence names what a datum carries, not one series of it.
    evidence = dict(document.get("evidence") or {})
    wells = [w for w in evidence.pop(_DATUM_WELLS, []) or [] if isinstance(w, str)]
    fields: list[str] = []
    for well in wells:
        for member in plan.get(well) or []:
            if member not in fields:
                fields.append(member)
    evidence[_DATUM_FIELDS] = fields
    payload["evidence"] = evidence

    #  5. A label override is keyed by a well in a template and by a member in a
    #     Spec. Every member of the well takes the override: the template said
    #     "call whatever lands here this", and it lands on all of them.
    overrides = (document.get("labels") or {}).get("override") or {}
    resolved: dict[str, Any] = {}
    for well, label in overrides.items():
        for member in plan.get(str(well)) or []:
            resolved[member] = label
    payload["labels"] = {"override": resolved}

    return payload


# ---------------------------------------------------------------------------
# The one entry point.
# ---------------------------------------------------------------------------


def _load_template_document(
    conn, *, org_id: str, project_id: str, template_version_id: str
) -> tuple[dict[str, Any], str]:
    """The stored document and its head's label, scoped to the Project.

    Read AFTER the verdict, and only when the verdict is `compatible`: a template
    nobody can read is answered by story 72.3's own sentence, in its own state,
    rather than by a second one written here.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT v.document, t.label
            FROM app.visualization_template_versions v
            JOIN app.visualization_templates t
              ON t.id = v.template_id AND t.org_id = v.org_id AND t.project_id = v.project_id
            WHERE v.id = %s AND v.org_id = %s AND v.project_id = %s
            """,
            (template_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None or not isinstance(row[0], dict):
        raise VisualizationNotFound(  # pragma: no cover - the verdict read it a moment ago
            f"{CHART_TEMPLATE_NOUN} version not found in this Project"
        )
    return row[0], str(row[1] or "")


def materialize_template(
    conn,
    *,
    org_id: str,
    project_id: str,
    template_version_id: str,
    result_id: str,
    actor: str,
    proposed_by: str = "person",
    visualization_id: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    """Apply one Chart Template version to one Result. The whole story, in order.

    Returns exactly what `create_visualization_spec_version` returns, plus the
    verdict that allowed it -- the Spec itself is an ordinary Spec, and a caller
    that cannot tell it from a hand-built one is the point (AC23).

    Raises `MaterializationRefused` when the verdict is not `compatible` (AC25) or
    when a well a control names cannot be filled, `VisualizationSpecRefused` when
    the ordinary validator refuses the composed document, and
    `VisualizationNotFound` when the Result's pinned question does not resolve.
    NOTHING is written on any of those paths: the first write of this function is
    the INSERT inside `create_visualization_spec_version`, and every refusal above
    happens before it.
    """
    predicates = read_template_predicates(
        conn, org_id=org_id, project_id=project_id, template_version_id=template_version_id
    )
    facts = read_result_facts(conn, org_id=org_id, project_id=project_id, result_id=result_id)
    verdict = check_template_compatibility(predicates, facts)

    if verdict.state != COMPATIBLE:
        raise MaterializationRefused(
            verdict,
            code=f"chart_template_{verdict.state}",
            message=(
                f"this {CHART_TEMPLATE_NOUN} cannot be applied to this Result: "
                f"{len(verdict.unmet)} predicate(s) unsatisfied"
                if verdict.unmet
                else f"this {CHART_TEMPLATE_NOUN} and this Result cannot be compared right now"
            ),
        )

    #  Both are values from here on: a non-compatible verdict is the only way
    #  either of them is `Unreadable`, and that path returned above.
    assert isinstance(predicates, TemplatePredicates)
    assert isinstance(facts, ResultFacts)

    family = get_family(predicates.family)
    assert family is not None  # a missing family makes the verdict `unavailable`

    document, template_label = _load_template_document(
        conn, org_id=org_id, project_id=project_id, template_version_id=template_version_id
    )
    validated_template = validate_template_document(document)

    plan, refusals = plan_bindings(
        predicates,
        family,
        facts,
        control_wells=_wells_named_by_a_control(validated_template.document),
    )
    if refusals:
        raise VisualizationSpecRefused(
            "chart_template_not_materialisable",
            f"this {CHART_TEMPLATE_NOUN} could not be filled from this Result on "
            f"{len(refusals)} point(s)",
            refusals,
        )

    payload = spec_payload_from_template(validated_template.document, plan)

    #  THE PIN THE RESULT ALREADY CARRIES (AC26). No question is re-executed and no
    #  Result is created: this identifier was read off `app.query_results` by the
    #  verdict's own gate a few lines above.
    validated = validate_visualization_spec(
        conn,
        project_id=project_id,
        query_spec_version_id=facts.query_spec_version_id,
        payload=payload,
    )

    created = create_visualization_spec_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        validated=validated,
        actor=actor,
        proposed_by=proposed_by,
        visualization_id=visualization_id,
        name=name or template_label or None,
        materialized_from_template_version_id=template_version_id,
    )
    created["verdict"] = verdict.as_dict()
    created["result_id"] = facts.result_id
    return created
