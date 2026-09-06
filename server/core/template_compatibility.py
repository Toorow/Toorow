"""Story 72.3 -- the compatibility verdict: DERIVED at the moment of the question.

WHAT THIS OWNS. One function that answers, for one Chart Template version and one
Result, whether that Result satisfies the template's predicates -- and, when it
does not, WHICH predicate it fails, one by one, in the words of the role. Nothing
else: the tables belong to 72.1, the template grammar to 72.2, the seed
projection to 72.4, the screens to 72.5, and the materialisation into a
Visualization Spec version to 72.6.

THE SENTENCE THIS FILE IMPLEMENTS.
`docs/product-architecture/visualization-and-rendering.md`, § *Amendment,
2026-08-31 -- Chart Template, the ratified target*: *"from a Result the list
narrows to the templates that Result satisfies, and the ones it does not say
which predicate is missing instead of disappearing"*, and the criterion that
governs the shape of the answer: *"a compatibility verdict is stored rather than
derived, returned as a bare boolean without naming the missing predicate, or
confuses 'incompatible' with 'unreadable'"*.

DERIVED, NEVER STORED (AC12). There is no compatibility column, no verdict table,
no cache. A template does not move and a Result does not move, but the SHIPPED
REGISTRY does -- a family gains a well by migration, a role gains a server
source -- and a verdict written yesterday would then be a value nobody computed
at the moment it is read. This module only ever SELECTs, and that is asserted
twice: on this file's source (`test_template_compatibility.py`) and on the real
`information_schema`, which carries no compatibility column on any table of this
story (`test_template_compatibility_pg.py`).

THREE STATES, NEVER TWO (AC11).

  compatible    every predicate the template declares, and every predicate its
                visual family declares, is satisfied by what this Result offers.
  incompatible  at least one predicate is NOT satisfied, and every one of them is
                named with its JSON pointer into the template document and the
                gesture that repairs it. Never a bare boolean.
  unavailable   something could not be READ -- the template document does not
                validate under the contract this deployment ships, the Result
                declares no schema, the question behind it does not resolve, or a
                cardinality bound could not be counted because the rows were not
                read. A read that fails is never rendered as "incompatible": one
                says "this Result cannot answer this way", the other says
                "nobody knows yet", and a person acts on them differently.

WHERE THE JUDGEMENT COMES FROM, AND WHY NOT FROM HERE.

  * grain and comparison are decided by `check_grain_and_comparison`
    (`core.visualization_specs`), the SAME function the Visualization Spec path
    calls, reading `requires_time_grain` / `requires_comparison` off the family
    registry. This module never names either flag; a conformance test asserts it
    (one judge for two objects, and a second copy would drift on the first edit).
  * which members a Result offers, and under which role, is read from the pinned
    Query Spec version by `load_pinned_query_spec_version` -- the same read the
    Spec validator uses. No role is ever derived from a member's name.
  * which column carries a member is read from the Result's own schema through
    `member_columns`, the server-side twin of the runtime's `memberColumns`
    (`ui/cards/shell/src/viz/compile/dataset.ts:173`, AI-337): a binding names a
    MEMBER, a row is keyed by a COLUMN, and the map between them is the server's,
    never a guess.
  * what the template itself requires is read from its validated `requires`
    block, which `core.visualization_templates` derived from the Spec grammar.

WHAT THIS VERDICT DELIBERATELY DOES NOT ANSWER. Mark volume -- would this chart
be legible with this many rows -- is a DISCLOSURE of a materialised Visualization
Spec (decision D4, `evaluate_result_disclosures`), not a predicate any template
declares. A template that never mentions volume cannot be said to fail on it, and
inventing the rule here would be a second authority over `family.max_marks`. The
verdict answers exactly one question: can this Result satisfy what this template
ASKS FOR.

WELLS ARE JUDGED ONE BY ONE, AND INDEPENDENTLY. Nothing in the grammar forbids
one member occupying two wells, so a Result offering one measure satisfies two
wells that each ask for one measure. Choosing WHICH member goes where is
materialisation, and that is story 72.6's; this file answers whether a choice
exists at all, predicate by predicate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from core.visualization_families import (
    AVAILABLE_ROLES,
    ROLE_UNAVAILABLE_OWNER,
    ROLE_UNAVAILABLE_REASON,
    WELL_LABELS,
    WELL_ROLES,
    VisualFamily,
    get_family,
)
from core.visualization_specs import (
    PinnedMembers,
    VisualizationNotFound,
    VisualizationRefusal,
    VisualizationSpecRefused,
    check_grain_and_comparison,
    count_distinct_values,
    load_pinned_query_spec_version,
    member_columns,
)
from core.visualization_templates import (
    CHART_TEMPLATE_NOUN,
    validate_template_document,
)

__all__ = [
    "COMPATIBLE",
    "INCOMPATIBLE",
    "UNAVAILABLE",
    "CompatibilityVerdict",
    "OfferedMember",
    "ResultFacts",
    "TemplatePredicates",
    "Unreadable",
    "check_template_compatibility",
    "describe_result",
    "predicates_of",
    "read_result_facts",
    "read_template_compatibility",
    "read_template_predicates",
    "requirements_sentence",
]

#: The three states, spelled once. A caller comparing against a literal it typed
#: itself is how "unavailable" becomes "incompatible" in one screen and not the
#: other.
COMPATIBLE = "compatible"
INCOMPATIBLE = "incompatible"
UNAVAILABLE = "unavailable"


# ---------------------------------------------------------------------------
# What could not be read. Never a refusal of the Result, always of the reading.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Unreadable:
    """One thing that could not be read, and the gesture that repairs it.

    Carried rather than raised, because the verdict is a VALUE with three states
    and an exception would make two of them a control-flow accident. A caller
    that receives this and renders "incompatible" has committed the exact
    confusion the ratified criterion names.
    """

    refusal: VisualizationRefusal


def _unreadable(code: str, message: str, subject: str | None, remedy: str) -> Unreadable:
    return Unreadable(VisualizationRefusal(code, message, subject, remedy))


# ---------------------------------------------------------------------------
# The template side: the predicates, read from a validated document.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemplatePredicates:
    """What one Chart Template version ASKS FOR, and nothing about any Result."""

    version_id: str
    family: str
    answers_question: str
    #: `{well: {min, max, accepts, max_cardinality}}`, normalized -- every key
    #: present, defaults materialised by the grammar walker.
    requires: dict[str, dict[str, Any]]
    content_hash: str


def predicates_of(document: Any, *, version_id: str = "") -> TemplatePredicates | Unreadable:
    """The predicates of one template document, or the reason it cannot be read.

    THE ONLY CONSTRUCTOR, on purpose. Every field below is taken from
    `validate_template_document`, so a `requires` block that names a well its
    family has not, widens a role, or asks for more distinct values than the
    family reads cannot reach the judgement at all -- 72.2 refuses it first. A
    hand-built `TemplatePredicates` would let an impossible predicate be judged
    against a Result and reported as the Result's fault.

    A document stored under an older shape of the grammar therefore lands as
    UNREADABLE rather than as a verdict, which is the honest answer: this
    deployment cannot say what that template asks for.
    """
    try:
        validated = validate_template_document(document)
    except VisualizationSpecRefused as exc:
        return _unreadable(
            "template_unreadable",
            f"this {CHART_TEMPLATE_NOUN} version cannot be read under the document contract "
            f"this deployment ships, so what it requires is unknown -- not unsatisfied "
            f"({len(exc.refusals)} point(s))",
            "/requires",
            f"Open the {CHART_TEMPLATE_NOUN} and save a new version under the current "
            f"contract.",
        )
    return TemplatePredicates(
        version_id=version_id,
        family=validated.family,
        answers_question=validated.answers_question,
        requires=dict(validated.requires),
        content_hash=validated.content_hash,
    )


# ---------------------------------------------------------------------------
# The Result side: what it OFFERS, in the vocabulary the predicates are written in.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OfferedMember:
    """One member this Result actually landed, with the role the question gave it."""

    member_id: str
    #: The canonical NAME, never the identifier, whenever the pinned question
    #: knows one -- the rule of `visualization-and-rendering.md:202-212`, which
    #: applies to a compatibility sentence exactly as it applies to a legend.
    label: str
    role: str
    #: The Result column the member landed in. `member_columns` produced it; a
    #: field with no member id maps to itself, so an envelope keyed by column
    #: name reads exactly as before (AI-337).
    column: str


@dataclass(frozen=True)
class ResultFacts:
    """What ONE exact Result offers. Nothing here is stored anywhere."""

    result_id: str
    #: The roles, labels, grain and comparison of the pinned Query Spec version --
    #: the same object the Visualization Spec validator judges against, so grain
    #: and comparison are decided by the same function for both objects.
    pinned: PinnedMembers
    members: tuple[OfferedMember, ...]
    row_count: int
    truncated: bool
    outcome: str
    #: `{member_id: distinct values in the rows that were read}`, or `None` when
    #: the rows were NOT read. `None` is not zero: a cardinality bound that could
    #: not be counted makes the verdict unavailable, never compatible.
    distinct_values: Mapping[str, int] | None
    #: The Query Spec version THIS Result was executed against, carried verbatim
    #: off `app.query_results`. The verdict itself never reads it -- grain and
    #: comparison come from `pinned` -- but story 72.6 pins the materialised
    #: Visualization Spec on it (AC26, AD-10), and re-reading the same column
    #: through a second SELECT would be a second answer to "which question is
    #: this?". Empty only when a caller built the facts by hand.
    query_spec_version_id: str = ""

    def members_for(self, roles: Iterable[str]) -> tuple[OfferedMember, ...]:
        accepted = frozenset(roles)
        return tuple(m for m in self.members if m.role in accepted)


#: The outcomes that carry an answer. `refused` and `unavailable` Results carry
#: none by construction (migration 151 forces `row_count = 0`), and a Result that
#: never answered cannot be said to be incompatible with anything.
_OUTCOMES_WITH_AN_ANSWER = frozenset({"success", "degraded"})


def describe_result(
    *,
    result_id: str,
    pinned: PinnedMembers,
    result_schema: Mapping[str, Any] | None,
    rows: Sequence[Mapping[str, Any]] | None,
    row_count: int,
    truncated: bool,
    outcome: str,
    query_spec_version_id: str = "",
) -> ResultFacts | Unreadable:
    """What this Result offers, or the reason it cannot be read. Pure.

    THE SCHEMA IS THE OFFER, AND THE QUESTION IS THE ROLE. A field the Result
    landed that the pinned question does not name carries no role -- there is
    nowhere to read one from and deriving it from the field's name would
    manufacture a semantic authority this surface is forbidden to have
    (`visualization_families.py`, "the two roles that have no source"). It is
    therefore not offered, rather than offered as a guess.

    A Result with no schema is UNREADABLE, not incompatible. An empty Result is
    exactly that case: `query_execution.shape_result_payload` builds its fields
    from the first row, so a Result that returned none declares no field, and
    "this Result offers nothing" is a thing nobody measured rather than a
    predicate it failed.
    """
    if outcome not in _OUTCOMES_WITH_AN_ANSWER:
        return _unreadable(
            "result_unreadable",
            f"this Result did not return an answer ({outcome}), so what it offers cannot be "
            f"read",
            "/requires",
            "Run the question again in Explore -- which produces a new Result -- and judge "
            "that one.",
        )

    fields = list((result_schema or {}).get("fields") or [])
    if not fields:
        return _unreadable(
            "result_unreadable",
            "this Result declares no schema, so which members it carries cannot be read",
            "/requires",
            "Run the question again in Explore -- which produces a new Result -- and judge "
            "that one.",
        )

    columns = member_columns(result_schema)
    members: list[OfferedMember] = []
    for field in fields:
        if not isinstance(field, Mapping):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue
        raw_id = field.get("id")
        member_id = raw_id if isinstance(raw_id, str) and raw_id else name
        role = pinned.role_of(member_id)
        if role is None:
            continue
        members.append(
            OfferedMember(
                member_id=member_id,
                label=pinned.labels.get(member_id) or name,
                role=role,
                column=columns.get(member_id, name),
            )
        )

    if not members:
        return _unreadable(
            "result_unreadable",
            "none of this Result's fields is a member of the question behind it, so no role "
            "can be read for any of them",
            "/requires",
            "Run the question again in Explore -- which produces a new Result -- and judge "
            "that one.",
        )

    distinct: dict[str, int] | None = None
    if rows is not None:
        distinct = {
            member.member_id: count_distinct_values(rows, member.column) for member in members
        }

    return ResultFacts(
        result_id=result_id,
        pinned=pinned,
        members=tuple(members),
        row_count=row_count,
        truncated=truncated,
        outcome=outcome,
        distinct_values=distinct,
        query_spec_version_id=query_spec_version_id,
    )


# ---------------------------------------------------------------------------
# The verdict.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompatibilityVerdict:
    """One of three states, and the named reason for the two that are not `compatible`."""

    state: str
    template_version_id: str
    result_id: str
    #: The visual family the template names, or `None` when the template itself
    #: could not be read.
    family: str | None
    answers_question: str
    #: Every unsatisfied predicate, each with its JSON pointer into the template
    #: document and the gesture that repairs it. Empty unless `state` is
    #: `incompatible`.
    unmet: tuple[VisualizationRefusal, ...] = ()
    #: What could not be read. Present only when `state` is `unavailable`.
    unreadable: VisualizationRefusal | None = None

    def as_dict(self) -> dict[str, Any]:
        """The wire shape. There is deliberately NO boolean key.

        A `compatible: true/false` field would be read on its own by the first
        caller in a hurry, and "unavailable" would arrive at a screen as "no".
        The state is the answer; everything else explains it.
        """
        return {
            "state": self.state,
            "template_version_id": self.template_version_id,
            "result_id": self.result_id,
            "family": self.family,
            "answers_question": self.answers_question,
            "unmet": [refusal.as_dict() for refusal in self.unmet],
            "unreadable": self.unreadable.as_dict() if self.unreadable else None,
        }


def _unavailable_verdict(
    reason: VisualizationRefusal,
    *,
    template_version_id: str = "",
    result_id: str = "",
    family: str | None = None,
    answers_question: str = "",
) -> CompatibilityVerdict:
    return CompatibilityVerdict(
        state=UNAVAILABLE,
        template_version_id=template_version_id,
        result_id=result_id,
        family=family,
        answers_question=answers_question,
        unreadable=reason,
    )


#: The family predicates, restated in the words of the two objects being compared.
#: The DECISION stays `check_grain_and_comparison`'s -- this only says whose
#: predicate it is and what the Result carries instead. A code with no entry
#: passes through with the judge's own sentence rather than being dropped: a
#: predicate this file has not learned to phrase is still a predicate.
_FAMILY_PREDICATE_SENTENCES: dict[str, str] = {
    "missing_grain": (
        "this template's {family} family needs a time grain, and the question behind this "
        "Result carries none"
    ),
    "missing_comparison": (
        "this template's {family} family needs a comparison, and the question behind this "
        "Result carries none"
    ),
}


def _in_the_words_of_both_objects(
    refusal: VisualizationRefusal, family: VisualFamily
) -> VisualizationRefusal:
    sentence = _FAMILY_PREDICATE_SENTENCES.get(refusal.code)
    if sentence is None:
        return refusal
    return VisualizationRefusal(
        refusal.code,
        sentence.format(family=family.label),
        refusal.subject,
        refusal.remedy,
    )


def _role_words(roles: Iterable[str]) -> str:
    return " or ".join(sorted(roles))


def _asked_for(count: int, roles: Iterable[str]) -> str:
    words = _role_words(roles)
    return f"one {words}" if count == 1 else f"{count} {words} members"


def requirements_sentence(requires: Mapping[str, Mapping[str, Any]]) -> str:
    """What a template asks for, in the words of the roles. Composed HERE.

    Story 72.5 renders this on every list row and on the Overview of the
    workbench, and it is composed on the server for the reason the ratified
    criterion states: "a refusal, an empty state or a compatibility sentence
    renders a product identifier where a name is expected, or is composed in the
    browser". It reuses `_asked_for` rather than paraphrasing it, so a template
    describes itself in the same words the verdict uses when it is not satisfied.

    A template that requires nothing says so; the empty string would render as a
    missing sentence rather than as the fact that any Result fits.
    """
    parts: list[str] = []
    for well in sorted(requires):
        entry = requires.get(well) or {}
        needed = entry.get("min")
        needed = needed if isinstance(needed, int) and needed > 0 else 1
        accepts = [role for role in entry.get("accepts") or [] if isinstance(role, str)]
        phrase = _asked_for(needed, accepts or sorted(AVAILABLE_ROLES))
        bound = entry.get("max_cardinality")
        if isinstance(bound, int):
            phrase = f"{phrase} with at most {bound} distinct values"
        parts.append(f"{phrase} in {WELL_LABELS.get(well, well)}")
    return ", ".join(parts) if parts else "nothing in particular -- any Result fits"


def _carried(members: Sequence[OfferedMember]) -> str:
    if not members:
        return "none"
    names = ", ".join(sorted(m.label for m in members))
    return f"only {len(members)} ({names})"


def _accepted_roles(
    entry: Mapping[str, Any], family: VisualFamily, well_name: str
) -> frozenset[str]:
    """Which roles may occupy this well: the template's narrowing, or the family's.

    An empty `accepts` is the grammar's way of saying "whatever the family's own
    well allows" (`visualization_templates._requires_block`), so the registry is
    the authority whenever the template did not narrow it.
    """
    narrowed = [role for role in entry.get("accepts") or [] if isinstance(role, str)]
    if narrowed:
        return frozenset(narrowed)
    well = family.well(well_name)
    return frozenset(well.accepts) if well is not None else frozenset()


def _requirement_verdicts(
    template: TemplatePredicates, family: VisualFamily, result: ResultFacts
) -> tuple[list[VisualizationRefusal], list[str], list[str]]:
    """Every declared requirement, judged against what the Result offers.

    Returns `(unmet, wells whose cardinality could not be counted, wells that
    could not be judged at all)`. The three are kept apart because they land in
    two different states and confusing them is the criterion this story exists
    for.
    """
    unmet: list[VisualizationRefusal] = []
    uncounted: list[str] = []
    unjudgeable: list[str] = []

    for well_name in WELL_ROLES:
        entry = template.requires.get(well_name)
        well = family.well(well_name)

        if entry is None:
            #  The template is silent. A well the family REQUIRES is still a
            #  predicate -- the registry is authoritative and may have gained the
            #  requirement by migration after this version was written -- so it is
            #  judged from the family's own record rather than skipped.
            if well is None or not well.required or not well.available:
                continue
            needed = 1
            accepts = frozenset(well.accepts)
            bound = well.max_cardinality
            pointer = f"/requires/{well_name}"
        else:
            if well is None:
                #  Refused by `validate_template_document`, so unreachable from a
                #  document this deployment can read. Named rather than skipped:
                #  a predicate nobody judged is not a predicate nobody failed.
                unjudgeable.append(well_name)
                continue
            raw_min = entry.get("min")
            needed = raw_min if isinstance(raw_min, int) and raw_min > 0 else 1
            accepts = _accepted_roles(entry, family, well_name)
            raw_bound = entry.get("max_cardinality")
            bound = raw_bound if isinstance(raw_bound, int) else well.max_cardinality
            pointer = f"/requires/{well_name}"

        label = WELL_LABELS[well_name]

        if not (accepts & AVAILABLE_ROLES):
            #  A well whose every accepted role has no server source. No Result
            #  can ever carry one, and saying so with the owner attached is the
            #  difference between "not yet" and "not possible".
            unmet.append(
                VisualizationRefusal(
                    "role_unavailable",
                    f"this template needs {_asked_for(needed, accepts)} in {label}, and no "
                    f"Result can carry one: {ROLE_UNAVAILABLE_REASON}",
                    f"{pointer}/accepts",
                    f"Choose a template that asks for {_role_words(AVAILABLE_ROLES)}, or wait "
                    f"for {ROLE_UNAVAILABLE_OWNER}.",
                )
            )
            continue

        candidates = result.members_for(accepts)
        if len(candidates) < needed:
            unmet.append(
                VisualizationRefusal(
                    "missing_role",
                    f"this template needs {_asked_for(needed, accepts)} in {label}, and this "
                    f"Result carries {_carried(candidates)}",
                    f"{pointer}/min",
                    f"Ask the question again in Explore with {_asked_for(needed, accepts)} "
                    f"-- which produces a new Result -- or choose a template that needs "
                    f"fewer.",
                )
            )
            continue

        if bound is None:
            continue

        if result.distinct_values is None:
            uncounted.append(label)
            continue

        over = [
            (m, result.distinct_values.get(m.member_id, 0))
            for m in candidates
            if result.distinct_values.get(m.member_id, 0) > bound
        ]
        within = len(candidates) - len(over)
        if within < needed:
            ranked = sorted(over, key=lambda pair: -pair[1])
            worst = ", ".join(f"{m.label} ({count} values)" for m, count in ranked)
            unmet.append(
                VisualizationRefusal(
                    "cardinality_over_limit",
                    f"this template reads at most {bound} distinct values in {label}, and this "
                    f"Result carries more in {worst}",
                    f"{pointer}/max_cardinality",
                    f"Filter the question in Explore -- which produces a new Result -- or "
                    f"choose a template that reads more than {bound} values.",
                )
            )

    return unmet, uncounted, unjudgeable


def check_template_compatibility(
    template: TemplatePredicates | Unreadable,
    result: ResultFacts | Unreadable,
) -> CompatibilityVerdict:
    """The verdict. Pure, derived, and never written anywhere.

    THE ORDER MATTERS AND IT IS THIS. A reading that failed is answered first,
    because a predicate judged against something nobody could read is not a
    judgement. Then every predicate is collected -- the family's, from the
    registry, and the template's own, well by well -- and ALL of them are
    reported, never the first: a person who repairs one requirement and
    rediscovers the next has been made to guess.

    A predicate that FAILED outranks a predicate that could not be MEASURED: a
    named failure is an answer, and hiding it behind "unavailable" would tell a
    person nothing when this file already knows something. A verdict is only
    `unavailable` when nothing failed and something could not be read.
    """
    if isinstance(template, Unreadable):
        return _unavailable_verdict(
            template.refusal,
            result_id="" if isinstance(result, Unreadable) else result.result_id,
        )
    if isinstance(result, Unreadable):
        return _unavailable_verdict(
            result.refusal,
            template_version_id=template.version_id,
            family=template.family,
            answers_question=template.answers_question,
        )

    family = get_family(template.family)
    if family is None:
        #  A family the shipped registry no longer declares. The template is
        #  unreadable BY THIS DEPLOYMENT, which is not the Result's fault.
        return _unavailable_verdict(
            VisualizationRefusal(
                "family_not_drawn",
                f"this {CHART_TEMPLATE_NOUN} names a visual family this deployment does not "
                f"declare, so what it would draw cannot be read",
                "/family",
                f"Open the {CHART_TEMPLATE_NOUN} and save a new version on a family this "
                f"deployment draws.",
            ),
            template_version_id=template.version_id,
            result_id=result.result_id,
            family=template.family,
            answers_question=template.answers_question,
        )

    #  THE FAMILY'S OWN PREDICATES, DECIDED BY THE FUNCTION THAT ALREADY DECIDES
    #  THEM FOR A VISUALIZATION SPEC. Grain and comparison are read off the
    #  family registry and off the pinned question -- never declared a second
    #  time, here or in the template document.
    unmet = [
        _in_the_words_of_both_objects(refusal, family)
        for refusal in check_grain_and_comparison(family, result.pinned)
    ]

    required, uncounted, unjudgeable = _requirement_verdicts(template, family, result)
    unmet.extend(required)

    if unmet:
        return CompatibilityVerdict(
            state=INCOMPATIBLE,
            template_version_id=template.version_id,
            result_id=result.result_id,
            family=template.family,
            answers_question=template.answers_question,
            unmet=tuple(unmet),
        )

    if unjudgeable:
        return _unavailable_verdict(
            VisualizationRefusal(
                "template_unreadable",
                f"this {CHART_TEMPLATE_NOUN} requires "
                f"{', '.join(WELL_LABELS[w] for w in unjudgeable)}, which its own visual "
                f"family does not have, so the requirement cannot be judged",
                "/requires",
                f"Open the {CHART_TEMPLATE_NOUN} and save a new version whose requirements "
                f"match its family.",
            ),
            template_version_id=template.version_id,
            result_id=result.result_id,
            family=template.family,
            answers_question=template.answers_question,
        )

    if uncounted:
        return _unavailable_verdict(
            VisualizationRefusal(
                "cardinality_not_counted",
                f"the rows of this Result were not read, so how many distinct values it "
                f"carries in {', '.join(uncounted)} is unknown -- not over the limit",
                "/requires",
                "Open this Result and judge the template again; the count is read from the "
                "rows the Result carries.",
            ),
            template_version_id=template.version_id,
            result_id=result.result_id,
            family=template.family,
            answers_question=template.answers_question,
        )

    return CompatibilityVerdict(
        state=COMPATIBLE,
        template_version_id=template.version_id,
        result_id=result.result_id,
        family=template.family,
        answers_question=template.answers_question,
    )


# ---------------------------------------------------------------------------
# The reading gate. SELECT only -- there is nothing to write here (AC12).
# ---------------------------------------------------------------------------


def read_template_predicates(
    conn, *, org_id: str, project_id: str, template_version_id: str
) -> TemplatePredicates | Unreadable:
    """One stored Chart Template version's predicates, scoped to its Project.

    Absent, held by another Project, or unreachable all answer identically --
    the rule `VisualizationNotFound` states, applied to a value rather than an
    exception because "we could not read it" is one of this verdict's three
    states and not an error.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT document
            FROM app.visualization_template_versions
            WHERE id = %s AND org_id = %s AND project_id = %s
            """,
            (template_version_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return _unreadable(
            "template_unreadable",
            f"this {CHART_TEMPLATE_NOUN} version does not resolve in this Project, so what it "
            f"requires cannot be read",
            None,
            f"Choose a {CHART_TEMPLATE_NOUN} of this Project.",
        )
    document = row[0] if isinstance(row[0], dict) else None
    if document is None:
        return _unreadable(
            "template_unreadable",
            f"this {CHART_TEMPLATE_NOUN} version carries no document, so what it requires "
            f"cannot be read",
            None,
            f"Open the {CHART_TEMPLATE_NOUN} and save a new version.",
        )
    return predicates_of(document, version_id=template_version_id)


def read_result_facts(
    conn, *, org_id: str, project_id: str, result_id: str
) -> ResultFacts | Unreadable:
    """What one stored Result offers, scoped to its Project.

    The rows are read from `rows_chunk` -- the inline chunk the Result already
    carries -- because that is what a cardinality count can honestly be taken
    from without re-executing anything. A Result whose chunk is absent is
    counted from nothing, and the verdict says so rather than reporting zero.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.outcome, r.row_count, r.truncated, r.query_spec_version_id,
                   p.result_schema, p.rows_chunk
            FROM app.query_results r
            JOIN app.query_result_payloads p
              ON p.result_id = r.id AND p.org_id = r.org_id
            WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
            """,
            (result_id, org_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return _unreadable(
            "result_unreadable",
            "this Result does not resolve in this Project, so what it offers cannot be read",
            None,
            "Choose a Result of this Project.",
        )
    try:
        pinned = load_pinned_query_spec_version(
            conn, project_id=project_id, query_spec_version_id=str(row[3])
        )
    except VisualizationNotFound:
        return _unreadable(
            "result_unreadable",
            "the question behind this Result does not resolve in this Project, so the role of "
            "each of its fields is unknown",
            None,
            "Choose a Result whose question belongs to this Project.",
        )
    return describe_result(
        result_id=result_id,
        pinned=pinned,
        result_schema=row[4] if isinstance(row[4], dict) else {},
        rows=row[5] if isinstance(row[5], list) else None,
        row_count=int(row[1] or 0),
        truncated=bool(row[2]),
        outcome=str(row[0]),
        query_spec_version_id=str(row[3]),
    )


def read_template_compatibility(
    conn, *, org_id: str, project_id: str, template_version_id: str, result_id: str
) -> CompatibilityVerdict:
    """The verdict for one stored template version against one stored Result.

    Two SELECTs and a pure function. Nothing is written, and nothing is cached:
    the same call a moment later re-reads the shipped registry, which is the
    whole reason this value is not a column.
    """
    return check_template_compatibility(
        read_template_predicates(
            conn, org_id=org_id, project_id=project_id, template_version_id=template_version_id
        ),
        read_result_facts(conn, org_id=org_id, project_id=project_id, result_id=result_id),
    )
