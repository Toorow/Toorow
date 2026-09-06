"""Story 70.3 -- Analytics Alignment: the seventh Project capability.

WHAT IT IS. The capability that aligns what a media Datastream OBSERVED with
what an analytics Datastream MEASURED, so a spend and a conversion can be read
on one row without either side being rewritten. It belongs to the family that
ADDS COLUMNS to an aggregation -- Country, Placement Mapping -- and it is off by
default.

WHAT THIS MODULE OWNS.

  * the CLOSED, ORDERED vocabulary of methods that can resolve one row, and the
    cascade that executes them in the declared order;
  * the read that says whether the capability may be activated at all, which is
    three declared dependencies and no more;
  * the store of the two acts a person may take on a row the machine could not
    settle -- an arbitration and an acceptance.

WHAT IT DELIBERATELY DOES NOT OWN, so nobody looks for it here: the PRORATA
VENTILATION of the metrics an `ambiguous` row carries. That is
`core.analytics_ventilation` (story 70.4) -- weights, their conservation to the
cent, and the rule that a ventilated figure is never emitted in the same column
as a directly-matched one. Four fields and no metric is what this module is; the
split is the other half and it lives next door.

WHAT IT ALSO DOES NOT OWN, and for the same kind of reason: the states.
They are `matched | ambiguous | unmatched | accepted`, imported from
`core.plan_matching_states` and not respelled. Story 61.2 already paid the price
of that vocabulary in `docs/product-architecture/glossary.md`; inventing a fifth
set of words for the same four situations is the exact fault that module exists
to refuse, and it would cost the glossary entry a second time.

NO SCORE. `plan_mapping_suggest` carries a `similarity` tier with a 0.88 ratio,
and that tier is NOT reused here. A cascade whose stages are declared words is
readable by the person who owns the result: `name_prefix` says what happened to
the row. A 0.87 says nothing a person can act on, and it invites a threshold
argument in place of a declaration. The nearest thing to a knob in this module is
:data:`NAME_PREFIX_MIN_LENGTH`, and it is a LENGTH -- how much of a name has to
be shared before sharing it means anything -- not a confidence.

ONE ROW, ONE METHOD, AND THE CASCADE STOPS. The first stage that produces any
candidate at all decides the row. It does not fall through to a looser stage when
it produced TWO: falling through would resolve an ambiguity with a weaker method,
which is auto-arbitration through the back door, and the whole point of the
`ambiguous` state is that nobody has arbitrated.

`unmatched` IS LISTED, NEVER HIDDEN -- it is the work. :func:`alignment_counts`
returns all four states, `unmatched` at zero rather than absent, for the reason
`observed_entities.resolution_counts` states about `unresolved`: a state that
disappears when it is empty cannot be watched.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence

from ulid import ULID

from core.audit import declare_action, insert_audit_row
from core.dimension_conformance import normalize_value
from core.plan_matching_states import (
    MATCHING_STATE_ACCEPTED,
    MATCHING_STATE_AMBIGUOUS,
    MATCHING_STATE_MATCHED,
    MATCHING_STATE_UNMATCHED,
    PLAN_MATCHING_STATES,
)
from core.project_capability_states import capability_is_active, read_capability_state

# ---------------------------------------------------------------------------
# The key, and the columns it adds.
# ---------------------------------------------------------------------------

#: The seventh key `app.project_capabilities` accepts (migration 309).
ANALYTICS_ALIGNMENT_CAPABILITY_KEY = "analytics_alignment"

#: Every column name below is DERIVED from this prefix, on the patron story 70.1
#: set for `schema_splits`: a name that can be computed from the declaration is a
#: second place for the declaration to be contradicted.
COLUMN_PREFIX = "analytics_alignment"

#: The identity of the entity on the OTHER side, as the family's two columns:
#: an id and a name, exactly as Placement Mapping adds `plan_line_key` and
#: `plan_line_label` and as Country adds a country.
ENTITY_ID_COLUMN = f"{COLUMN_PREFIX}_entity_id"
ENTITY_NAME_COLUMN = f"{COLUMN_PREFIX}_entity_name"

#: The THIRD column, and this capability is the first of its family to need one.
#: The method cannot be re-derived at read: `human_arbitration` is a stored act,
#: and the four automatic stages read a mapping version that will be superseded.
#: A reader who cannot tell an id equality from a shared name prefix cannot
#: defend the number, so the method travels ON the row rather than in a report
#: beside it.
METHOD_COLUMN = f"{COLUMN_PREFIX}_method"

#: The three, in the order a reader meets them.
ADDED_COLUMNS: tuple[str, ...] = (ENTITY_ID_COLUMN, ENTITY_NAME_COLUMN, METHOD_COLUMN)


# ---------------------------------------------------------------------------
# The cascade -- a closed, ORDERED vocabulary of methods.
# ---------------------------------------------------------------------------

#: The two sides publish the same platform entity id. Nothing is interpreted.
METHOD_ID_EXACT = "id_exact"

#: The two sides publish the same declared business key -- the value bound to the
#: MDM common key's components. Not an id: a key is what the client declared the
#: identity to be, an id is what a platform minted.
METHOD_KEY_EXACT = "key_exact"

#: Their names are equal once normalized by `dimension_conformance.normalize_value`
#: -- the FIXED pipeline (bracket tags, datestamps, diacritics, case, separators,
#: affixes) nobody configures. It is not a rule, and it is not called one.
METHOD_NAME_NORMALIZED = "name_normalized"

#: One normalized name begins the other, over at least
#: :data:`NAME_PREFIX_MIN_LENGTH` characters. This is the loosest automatic
#: stage and the one most likely to produce two candidates, which is exactly what
#: `ambiguous` is for.
METHOD_NAME_PREFIX = "name_prefix"

#: A named person picked one candidate on a dated row. LAST in the cascade, and
#: that position is the arbitrage: a stored arbitration settles the rows the four
#: automatic stages left `ambiguous` or `unmatched`, and never overwrites a row
#: an automatic stage resolved. If an `id_exact` alignment is wrong, the mapping
#: is wrong, and letting a click contradict it would hide the real defect.
METHOD_HUMAN_ARBITRATION = "human_arbitration"

#: The closed vocabulary, IN THE ORDER THE CASCADE EXECUTES. Closed rather than
#: open for the reason `plan_matching_states` gives about its own four words: the
#: fault this module exists to avoid is a sixth method appearing quietly, and a
#: tuple somebody has to edit is the moment that decision becomes visible.
ALIGNMENT_METHODS: tuple[str, ...] = (
    METHOD_ID_EXACT,
    METHOD_KEY_EXACT,
    METHOD_NAME_NORMALIZED,
    METHOD_NAME_PREFIX,
    METHOD_HUMAN_ARBITRATION,
)

#: The four the machine may run on its own. `human_arbitration` is not one of
#: them, and this tuple is what makes that impossible to forget in a loop.
AUTOMATIC_METHODS: tuple[str, ...] = ALIGNMENT_METHODS[:-1]

#: What each method is CALLED to a person. The word names what happened to the
#: row, because that is what a person has to defend.
ALIGNMENT_METHOD_LABELS: dict[str, str] = {
    METHOD_ID_EXACT: "Same entity id",
    METHOD_KEY_EXACT: "Same declared key",
    METHOD_NAME_NORMALIZED: "Same normalized name",
    METHOD_NAME_PREFIX: "Shared name prefix",
    METHOD_HUMAN_ARBITRATION: "Arbitrated by hand",
}

#: How much of a normalized name has to be shared before sharing it means
#: anything. A LENGTH and not a confidence: `Q3` is a prefix of half a catalogue,
#: and a prefix stage with no floor produces an ambiguity on every row -- which
#: reads as work when it is noise.
NAME_PREFIX_MIN_LENGTH = 12


def alignment_method_label(method: Any) -> str | None:
    """The product word for a method, or ``None`` for a word nobody declared.

    ``None`` and never the raw token: a screen that receives it renders an
    absence instead of leaking a machine key, which is the rule
    `plan_matching_states.match_method_label` already holds for its own axis.
    """
    return ALIGNMENT_METHOD_LABELS.get(str(method or "").strip())


# ---------------------------------------------------------------------------
# The two sides, and one aligned row.
# ---------------------------------------------------------------------------


class AlignmentRefused(ValueError):
    """An alignment that would produce a wrong number. Refused, never warned."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


@dataclass(frozen=True, slots=True)
class AlignmentSide:
    """One row of one Datastream, as the cascade may read it.

    Four fields and no metric: this module aligns IDENTITIES. What the aligned
    rows then carry in money is the business of the reading that joins them, and
    it is gated by :class:`AlignmentCurrencies` rather than assumed here.
    """

    row_key: str
    entity_id: str | None = None
    entity_key: str | None = None
    entity_name: str | None = None

    def normalized_name(self) -> str:
        return normalize_value(_text(self.entity_name))


@dataclass(frozen=True, slots=True)
class AlignmentCurrencies:
    """What currency each side states, and the one the Project reports in.

    Optional on :func:`run_cascade` for a reason worth stating: a run that
    declares nothing about money makes no claim about money, and the three
    columns this capability adds carry no amount. The moment a caller DOES state
    the currencies, an unconverted pair is REFUSED -- `placement-mapping.md`
    already writes why, and it is the same sentence: a figure matched across
    unconverted currencies is a wrong number that looks right.
    """

    left: str | None
    right: str | None
    reporting: str | None


@dataclass(frozen=True, slots=True)
class AlignedRow:
    """One row of the left Datastream, its state, and how it got there."""

    row_key: str
    state: str
    method: str | None = None
    entity_id: str | None = None
    entity_name: str | None = None
    #: Named whenever the state is `ambiguous`, and empty otherwise. An ambiguity
    #: whose candidates are not carried beside it is a decoration -- the rule
    #: `plan_matching_states.AMBIGUITY_IS_COMPUTED_ON_DEMAND` states for the plan
    #: side, and it holds here for the same reason.
    candidates: tuple[str, ...] = ()
    decided_by: str | None = None
    decided_at: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in PLAN_MATCHING_STATES:
            raise AlignmentRefused("unknown_alignment_state", f"unknown state {self.state!r}")
        if self.state == MATCHING_STATE_MATCHED and self.method not in ALIGNMENT_METHODS:
            raise AlignmentRefused(
                "matched_without_method",
                "A matched row names the method that resolved it. A match with no method "
                "is a number nobody can defend.",
            )
        if self.state != MATCHING_STATE_MATCHED and self.entity_id is not None:
            raise AlignmentRefused(
                "unmatched_row_carries_an_entity",
                "Only a matched row carries the aligned entity. A row that is not matched "
                "carries null, never a sentinel.",
            )

    def columns(self) -> dict[str, str | None]:
        """The three added columns for this row.

        NULL ON EVERY ROW THAT IS NOT `matched`, and that includes `accepted`.
        Accepting says *nothing on the other side answers this row, and we know*;
        filling an entity in would say the opposite. A `0`, an empty string or an
        `Unaligned` sentinel would each make an unaligned row look aligned, which
        is the fault `placement-mapping.md` names for its own two columns.
        """
        if self.state != MATCHING_STATE_MATCHED:
            return dict.fromkeys(ADDED_COLUMNS, None)
        return {
            ENTITY_ID_COLUMN: self.entity_id,
            ENTITY_NAME_COLUMN: self.entity_name,
            METHOD_COLUMN: self.method,
        }

    def as_dict(self) -> dict[str, Any]:
        return {
            "row_key": self.row_key,
            "state": self.state,
            "method": self.method,
            "method_label": alignment_method_label(self.method),
            "candidates": list(self.candidates),
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "reason": self.reason,
            **self.columns(),
        }


# ---------------------------------------------------------------------------
# The decisions a person may take, as the cascade reads them.
# ---------------------------------------------------------------------------

#: A person picked ONE candidate for a row the machine left `ambiguous` or
#: `unmatched`. The row becomes `matched` under `human_arbitration`.
DECISION_ARBITRATED = "arbitrated"

#: A person stated that nothing on the other side answers this row. The row
#: becomes `accepted`; it stays LISTED, with its columns null.
DECISION_ACCEPTED = "accepted"

DECISION_KINDS: tuple[str, ...] = (DECISION_ARBITRATED, DECISION_ACCEPTED)


@dataclass(frozen=True, slots=True)
class AlignmentDecision:
    """One dated human act on one row of the left Datastream."""

    left_row_key: str
    decision: str
    decided_by: str
    decided_at: str | None = None
    right_row_key: str | None = None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.decision not in DECISION_KINDS:
            raise AlignmentRefused("unknown_decision", f"unknown decision {self.decision!r}")
        if self.decision == DECISION_ARBITRATED and not _text(self.right_row_key):
            raise AlignmentRefused(
                "arbitration_names_nothing",
                "An arbitration names the row it picked. Deciding without naming a side "
                "is not a decision.",
            )
        if self.decision == DECISION_ACCEPTED and _text(self.right_row_key):
            raise AlignmentRefused(
                "acceptance_names_a_side",
                "An acceptance states that nothing answers this row, so it names no side.",
            )
        if not _text(self.decided_by):
            raise AlignmentRefused(
                "decision_without_an_author",
                "A decision carries the person who took it. An anonymous arbitration is "
                "indistinguishable from a row somebody clicked past.",
            )


def decisions_by_row(
    decisions: Iterable[AlignmentDecision | Mapping[str, Any]],
) -> dict[str, AlignmentDecision]:
    """Index the decisions by the row they settle -- the FIRST one stands.

    The first and not the last, on the precedent of
    `plan_spend_decisions.accept_unmatched_spend`: a second click, or a second
    person, never rewrites who decided and when. The database says the same thing
    with a unique index and `ON CONFLICT DO NOTHING`; this function is what makes
    the pure cascade agree with it without a connection.
    """
    indexed: dict[str, AlignmentDecision] = {}
    for item in decisions:
        decision = item if isinstance(item, AlignmentDecision) else AlignmentDecision(**dict(item))
        indexed.setdefault(_text(decision.left_row_key), decision)
    return indexed


# ---------------------------------------------------------------------------
# The cascade itself. Pure, so every stage is proved directly.
# ---------------------------------------------------------------------------


def _stage_candidates(
    method: str, left: AlignmentSide, right_rows: Sequence[AlignmentSide], *, prefix_min: int
) -> list[AlignmentSide]:
    """Every right-hand row this ONE stage proposes for *left*. Never ranked.

    Ranking would pick a winner out of two, which is arbitration, and arbitration
    is a stage of its own with a person's name on it.
    """
    if method == METHOD_ID_EXACT:
        wanted = _text(left.entity_id)
        return [r for r in right_rows if wanted and _text(r.entity_id) == wanted]
    if method == METHOD_KEY_EXACT:
        wanted = _text(left.entity_key)
        return [r for r in right_rows if wanted and _text(r.entity_key) == wanted]
    if method == METHOD_NAME_NORMALIZED:
        wanted = left.normalized_name()
        return [r for r in right_rows if wanted and r.normalized_name() == wanted]
    if method == METHOD_NAME_PREFIX:
        wanted = left.normalized_name()
        if len(wanted) < prefix_min:
            # The floor applies to the LEFT name too: a name shorter than the
            # floor cannot share enough of itself with anything, and letting it
            # through would make the floor a property of the right-hand side.
            return []
        matches = []
        for right in right_rows:
            other = right.normalized_name()
            if len(other) < prefix_min:
                continue
            if other.startswith(wanted) or wanted.startswith(other):
                matches.append(right)
        return matches
    raise AlignmentRefused("unknown_method", f"{method!r} is not a declared alignment method")


def _refuse_unconverted(currencies: AlignmentCurrencies | None) -> None:
    if currencies is None:
        return
    reporting = _text(currencies.reporting)
    left, right = _text(currencies.left), _text(currencies.right)
    if not reporting:
        raise AlignmentRefused(
            "reporting_currency_unresolved",
            "Nothing names the currency this Project reports in, so no aligned figure can "
            "be stated. Confirm the Money Policy in Project Settings, then align again.",
        )
    off = sorted({side for side in (left, right) if side and side != reporting})
    if off:
        raise AlignmentRefused(
            "unconverted_currency",
            f"One side states {', '.join(off)} where this Project reports in {reporting}. "
            f"Aligning a figure across unconverted currencies produces a wrong number that "
            f"looks right. Land a validated FX rate batch so both sides convert, then align "
            f"again.",
        )
    if not left or not right:
        raise AlignmentRefused(
            "native_currency_missing",
            "One side publishes no currency at all, so nothing states what its amounts are "
            "in. Republish the mapping that leaves the currency unbound, then align again.",
        )


def run_cascade(
    left_rows: Sequence[AlignmentSide],
    right_rows: Sequence[AlignmentSide],
    *,
    decisions: Iterable[AlignmentDecision | Mapping[str, Any]] = (),
    currencies: AlignmentCurrencies | None = None,
    prefix_min_length: int = NAME_PREFIX_MIN_LENGTH,
) -> tuple[AlignedRow, ...]:
    """Align every row of *left_rows*, in the declared order, one method each.

    The contract, stage by stage:

      1. :data:`AUTOMATIC_METHODS` run in order. The FIRST stage that proposes
         anything decides the row: one candidate is `matched` under that method,
         two or more is `ambiguous` under it with its candidates named, and the
         cascade STOPS either way.
      2. a stored arbitration settles a row the four stages left `ambiguous` or
         `unmatched`: `matched` under `human_arbitration`, carrying who and when.
      3. a stored acceptance turns an `unmatched` row into `accepted`, columns
         still null, carrying who and when.
      4. everything else stays `unmatched`, and it is RETURNED. Hiding it would
         hide the work.

    ``currencies`` is optional and refusing rather than warning: see
    :class:`AlignmentCurrencies`.
    """
    _refuse_unconverted(currencies)
    if prefix_min_length < 1:
        raise AlignmentRefused(
            "prefix_floor_below_one",
            "A shared-name-prefix floor below one character shares nothing.",
        )
    settled = decisions_by_row(decisions)
    right_by_key = {_text(row.row_key): row for row in right_rows}

    aligned: list[AlignedRow] = []
    for left in left_rows:
        row_key = _text(left.row_key)
        state = MATCHING_STATE_UNMATCHED
        method: str | None = None
        candidates: tuple[str, ...] = ()
        entity_id: str | None = None
        entity_name: str | None = None

        for stage in AUTOMATIC_METHODS:
            found = _stage_candidates(stage, left, right_rows, prefix_min=prefix_min_length)
            if not found:
                continue
            method = stage
            if len(found) == 1:
                state = MATCHING_STATE_MATCHED
                entity_id = _text(found[0].entity_id) or None
                entity_name = _text(found[0].entity_name) or None
            else:
                state = MATCHING_STATE_AMBIGUOUS
                candidates = tuple(_text(item.row_key) for item in found)
            break

        decision = settled.get(row_key)
        if decision is not None and state != MATCHING_STATE_MATCHED:
            if decision.decision == DECISION_ARBITRATED:
                picked = right_by_key.get(_text(decision.right_row_key))
                if picked is None:
                    raise AlignmentRefused(
                        "arbitration_names_an_absent_row",
                        f"The arbitration on {row_key!r} names a row this Datastream does "
                        f"not publish. Arbitrate again against a row that is observed.",
                    )
                aligned.append(
                    AlignedRow(
                        row_key=row_key,
                        state=MATCHING_STATE_MATCHED,
                        method=METHOD_HUMAN_ARBITRATION,
                        entity_id=_text(picked.entity_id) or None,
                        entity_name=_text(picked.entity_name) or None,
                        decided_by=decision.decided_by,
                        decided_at=decision.decided_at,
                        reason=decision.reason,
                    )
                )
                continue
            if state == MATCHING_STATE_UNMATCHED:
                aligned.append(
                    AlignedRow(
                        row_key=row_key,
                        state=MATCHING_STATE_ACCEPTED,
                        decided_by=decision.decided_by,
                        decided_at=decision.decided_at,
                        reason=decision.reason,
                    )
                )
                continue

        aligned.append(
            AlignedRow(
                row_key=row_key,
                state=state,
                method=method
                if state in (MATCHING_STATE_MATCHED, MATCHING_STATE_AMBIGUOUS)
                else None,
                entity_id=entity_id,
                entity_name=entity_name,
                candidates=candidates,
            )
        )
    return tuple(aligned)


def alignment_counts(rows: Iterable[AlignedRow]) -> dict[str, int]:
    """How many rows are in each of the four states. ALL FOUR KEYS, ALWAYS.

    `unmatched` at zero rather than absent, for the reason
    `observed_entities.resolution_counts` states about `unresolved`: a state that
    vanishes when it is empty cannot be watched, and watching it is how coverage
    moves.
    """
    counts = {state: 0 for state in PLAN_MATCHING_STATES}
    for row in rows:
        counts[row.state] = counts.get(row.state, 0) + 1
    return counts


def unmatched_rows(rows: Iterable[AlignedRow]) -> tuple[AlignedRow, ...]:
    """Every row nobody has settled -- the work, returned rather than counted."""
    return tuple(row for row in rows if row.state == MATCHING_STATE_UNMATCHED)


def added_columns(capability_state: str) -> tuple[str, ...]:
    """The columns an aggregation gains, which is NOTHING while the switch is off.

    Read through `capability_is_active` and never against a literal: the lens of
    `governance_read_model` compared a state with `"enabled"` -- a word migration
    131's CHECK does not admit -- and could therefore never be true for anybody.
    That defect survived seventeen days, and it is the reason this function
    exists instead of a comparison at each call site.
    """
    return ADDED_COLUMNS if capability_is_active(capability_state) else ()


# ---------------------------------------------------------------------------
# The dependencies. Declared, and BLOCKING.
# ---------------------------------------------------------------------------

#: An MDM common key version whose every component BOTH Datastreams publish.
#: `mdm_common_keys` owns it: "these canonical fields are ONE business identity".
DEPENDENCY_COMMON_KEY = "mdm_common_key"

#: A PUBLISHED Semantic View relationship between the two Datastreams, pinned on
#: that exact key version. `governance.md`: "a key without an approved
#: relationship remains non-executable". A declaration of MEANING is not a
#: permission to EXECUTE, and this is the dependency that keeps them apart.
DEPENDENCY_APPROVED_RELATIONSHIP = "approved_relationship"

#: Currency & FX, ready or degraded. An aligned figure computed across
#: unconverted currencies is a wrong number that looks right.
DEPENDENCY_CURRENCY_FX = "currency_fx"

#: The three, in the order a person has to satisfy them: an identity, then the
#: permission to cross on it, then the money that makes the crossing readable.
ALIGNMENT_DEPENDENCIES: tuple[str, ...] = (
    DEPENDENCY_COMMON_KEY,
    DEPENDENCY_APPROVED_RELATIONSHIP,
    DEPENDENCY_CURRENCY_FX,
)


@dataclass(frozen=True, slots=True)
class MissingDependency:
    """One unmet dependency, and the gesture that meets it.

    ``gesture`` is not decoration. A refusal that names a cause and no act sends
    a person looking for a control; every sentence below names something they can
    actually do, on a surface that exists.
    """

    code: str
    reason: str
    gesture: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "reason": self.reason, "gesture": self.gesture}


_MISSING_COMMON_KEY = MissingDependency(
    code=DEPENDENCY_COMMON_KEY,
    reason=(
        "No declared common key has every one of its components published by both "
        "Datastreams, so nothing states that these two sources speak about the same thing."
    ),
    gesture=(
        "Declare the common key in Governance > Master Data, then publish the mapping of "
        "whichever Datastream leaves one of its components unbound."
    ),
)

_MISSING_RELATIONSHIP = MissingDependency(
    code=DEPENDENCY_APPROVED_RELATIONSHIP,
    reason=(
        "A common key covers both Datastreams, but no published Semantic View relationship "
        "pins that key version between them, so nobody has approved crossing on it."
    ),
    gesture=(
        "Add the relationship between these two Datastreams to a Semantic View, pin the "
        "common key version it crosses on, and publish that view."
    ),
)

_MISSING_CURRENCY_FX = MissingDependency(
    code=DEPENDENCY_CURRENCY_FX,
    reason=(
        "Currency & FX is not ready on this Project, so an aligned amount could be composed "
        "of two currencies nothing converted."
    ),
    gesture=(
        "Confirm the Money Policy in Project Settings and land a validated FX rate batch, "
        "then activate Analytics Alignment."
    ),
)

MISSING_DEPENDENCY_BY_CODE: dict[str, MissingDependency] = {
    DEPENDENCY_COMMON_KEY: _MISSING_COMMON_KEY,
    DEPENDENCY_APPROVED_RELATIONSHIP: _MISSING_RELATIONSHIP,
    DEPENDENCY_CURRENCY_FX: _MISSING_CURRENCY_FX,
}


@dataclass(frozen=True, slots=True)
class AlignmentDependencies:
    """What the two Datastreams have, and what they are missing."""

    left_datastream_id: str
    right_datastream_id: str
    common_key_version_ids: tuple[str, ...] = ()
    relationships: tuple[Mapping[str, Any], ...] = ()
    currency_fx_state: str = ""
    missing: tuple[MissingDependency, ...] = ()

    @property
    def satisfied(self) -> bool:
        return not self.missing

    def as_dict(self) -> dict[str, Any]:
        return {
            "left_datastream_id": self.left_datastream_id,
            "right_datastream_id": self.right_datastream_id,
            "common_key_version_ids": list(self.common_key_version_ids),
            "relationships": [dict(item) for item in self.relationships],
            "currency_fx_state": self.currency_fx_state,
            "satisfied": self.satisfied,
            "missing": [item.as_dict() for item in self.missing],
        }


def covering_key_versions(
    coverage: Mapping[str, Any],
    *,
    left_datastream_id: str,
    right_datastream_id: str,
) -> bool:
    """Does this key's coverage put EVERY component on BOTH Datastreams?

    Split from its reader so it can be proved without a connection, and so the
    answer is one function rather than a predicate re-parsed per caller -- the
    defect `multi_source_plan` names about `unsupported_relationship`.

    A key with NO component answers ``False``: an identity that declares nothing
    is not an identity, which is exactly what `_compile_edge` refuses with
    `common_key_has_no_component`.
    """
    components = list(coverage.get("components") or [])
    if not components:
        return False
    for component in components:
        implementers = {
            _text(item.get("datastream_id")) for item in component.get("implemented_by") or []
        }
        if left_datastream_id not in implementers or right_datastream_id not in implementers:
            return False
    return True


def resolve_dependencies(
    conn, *, project_id: str, left_datastream_id: str, right_datastream_id: str
) -> AlignmentDependencies:
    """The three dependencies, read once, with EVERY unmet one named.

    EVERY one and not the first: a refusal that reveals its blockers one at a
    time makes a person fix, retry, and be refused again, which is how a
    dependency chain becomes a guessing game.

    Nothing here writes. This is the read that decides whether an activation may
    proceed, and `project_capability_states` already states the rule it obeys:
    reading a state can never turn one on.
    """
    from core.datastream_matches import executable_key_versions  # noqa: PLC0415
    from core.mdm_common_keys import list_common_keys, mapping_coverage  # noqa: PLC0415

    left, right = _text(left_datastream_id), _text(right_datastream_id)
    if not left or not right or left == right:
        raise AlignmentRefused(
            "one_datastream_is_not_a_pair",
            "Analytics Alignment crosses TWO designated Datastreams. Name the second one.",
        )

    covering: list[str] = []
    for key in list_common_keys(conn, project_id=project_id):
        if str(key.get("status") or "") != "active":
            continue
        current = key.get("current_version") or {}
        version_id = _text(current.get("id"))
        if not version_id:
            continue
        coverage = mapping_coverage(
            conn, project_id=project_id, components=current.get("components") or []
        )
        if coverage.get("state") != "available":
            # "I could not look" is not "nobody binds it", and only one of the
            # two invites a person to go and map something.
            continue
        if covering_key_versions(coverage, left_datastream_id=left, right_datastream_id=right):
            covering.append(version_id)

    pinned = executable_key_versions(conn, project_id)
    relationships: list[Mapping[str, Any]] = []
    for version_id in covering:
        for relationship in pinned.get(version_id) or []:
            sides = {
                _text(relationship.get("left_datastream_id")),
                _text(relationship.get("right_datastream_id")),
            }
            if sides == {left, right}:
                relationships.append({**relationship, "common_key_version_id": version_id})

    fx_state = read_capability_state(
        conn, project_id=project_id, capability_key=DEPENDENCY_CURRENCY_FX
    )

    missing: list[MissingDependency] = []
    if not covering:
        missing.append(_MISSING_COMMON_KEY)
    elif not relationships:
        # Only when a key EXISTS: telling somebody to publish a relationship on a
        # key they have not declared names a gesture they cannot take yet.
        missing.append(_MISSING_RELATIONSHIP)
    if not capability_is_active(fx_state):
        missing.append(_MISSING_CURRENCY_FX)

    return AlignmentDependencies(
        left_datastream_id=left,
        right_datastream_id=right,
        common_key_version_ids=tuple(covering),
        relationships=tuple(relationships),
        currency_fx_state=fx_state,
        missing=tuple(missing),
    )


#: Said on the Project-wide read when no pair of Datastreams can carry the
#: capability at all. An absence with its reason, never an empty block.
NO_ALIGNABLE_PAIR = (
    "No two Datastreams of this Project share a declared common key with a published "
    "Semantic View relationship, so there is no pair Analytics Alignment could align."
)

#: The blocker code carried when an activation is requested on a Project that has
#: NO alignable pair at all. Distinct from the three per-pair dependencies: there
#: is not yet a pair to resolve them for, so the gesture is to CREATE one.
DEPENDENCY_ALIGNABLE_PAIR = "alignable_pair"

_MISSING_ALIGNABLE_PAIR = MissingDependency(
    code=DEPENDENCY_ALIGNABLE_PAIR,
    reason=NO_ALIGNABLE_PAIR,
    gesture=(
        "Declare a common key in Governance > Master Data, publish the mapping of the two "
        "Datastreams that share it, then add and publish a Semantic View relationship that "
        "crosses them -- then activate Analytics Alignment."
    ),
)


def no_alignable_pair_dependencies(currency_fx_state: str) -> tuple[MissingDependency, ...]:
    """The dependencies an activation must satisfy on a Project with NO alignable pair.

    ``resolve_dependencies`` needs two named Datastreams to read one specific
    pair; on a Project where no pair is alignable at all there is none to read,
    yet an activation requested there may not land ``ready`` on a promise nothing
    can keep. This names the gesture that would create a first pair, and -- the
    part that was silently skipped before -- reads Currency & FX HERE too, so the
    money dependency is named exactly when nothing else is met either. Every unmet
    one is named, never the first: the rule the per-pair path already obeys.
    """
    missing: list[MissingDependency] = [_MISSING_ALIGNABLE_PAIR]
    if not capability_is_active(currency_fx_state):
        missing.append(_MISSING_CURRENCY_FX)
    return tuple(missing)


def alignable_pairs(conn, *, project_id: str) -> list[dict[str, Any]]:
    """Every ordered pair a published relationship already authorizes crossing.

    Derived at read and never stored: which Datastreams can be crossed changes
    the next time a view is published, and a stored figure would be wrong
    silently -- the rule `mdm_common_keys` states about its own coverage.
    """
    from core.datastream_matches import executable_key_versions  # noqa: PLC0415

    pairs: dict[tuple[str, str], dict[str, Any]] = {}
    for version_id, relationships in executable_key_versions(conn, project_id).items():
        for relationship in relationships:
            left = _text(relationship.get("left_datastream_id"))
            right = _text(relationship.get("right_datastream_id"))
            if not left or not right or left == right:
                continue
            pairs.setdefault(
                (left, right),
                {
                    "left_datastream_id": left,
                    "right_datastream_id": right,
                    "common_key_version_id": version_id,
                    "relationship_name": relationship.get("relationship_name"),
                    "view_version_id": relationship.get("view_version_id"),
                },
            )
    return [pairs[key] for key in sorted(pairs)]


# ---------------------------------------------------------------------------
# The store. Two acts, both dated, both with a name on them.
#
# AD-42: an action is declared where it is WRITTEN, never in a central list
# nobody owns.
# ---------------------------------------------------------------------------

ACTION_ALIGNMENT_ARBITRATED = declare_action("analytics_alignment.row.arbitrated")
ACTION_ALIGNMENT_ACCEPTED = declare_action("analytics_alignment.row.accepted")

_ACTION_BY_DECISION = {
    DECISION_ARBITRATED: ACTION_ALIGNMENT_ARBITRATED,
    DECISION_ACCEPTED: ACTION_ALIGNMENT_ACCEPTED,
}

_INSERT_DECISION = """
    INSERT INTO app.analytics_alignment_decisions
        (id, org_id, project_id, left_datastream_id, right_datastream_id,
         common_key_version_id, left_row_key, decision, right_row_key, reason, decided_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (project_id, left_datastream_id, right_datastream_id,
                 common_key_version_id, left_row_key) DO NOTHING
"""

_SELECT_DECISIONS = """
    SELECT left_row_key, decision, right_row_key, reason, decided_by, decided_at
      FROM app.analytics_alignment_decisions
     WHERE project_id = %s AND left_datastream_id = %s AND right_datastream_id = %s
       AND common_key_version_id = %s
     ORDER BY decided_at, left_row_key
"""

_SELECT_ONE_DECISION = """
    SELECT left_row_key, decision, right_row_key, reason, decided_by, decided_at
      FROM app.analytics_alignment_decisions
     WHERE project_id = %s AND left_datastream_id = %s AND right_datastream_id = %s
       AND common_key_version_id = %s AND left_row_key = %s
"""


def _decision_from_row(row: Sequence[Any]) -> AlignmentDecision:
    decided_at = row[5]
    return AlignmentDecision(
        left_row_key=str(row[0]),
        decision=str(row[1]),
        right_row_key=str(row[2]) if row[2] is not None else None,
        reason=str(row[3]) if row[3] is not None else None,
        decided_by=str(row[4]),
        decided_at=decided_at.isoformat() if isinstance(decided_at, datetime) else decided_at,
    )


def list_alignment_decisions(
    conn,
    *,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    common_key_version_id: str,
) -> list[AlignmentDecision]:
    """Every decision taken on this exact pair, under this exact key version."""
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_DECISIONS,
            (project_id, left_datastream_id, right_datastream_id, common_key_version_id),
        )
        return [_decision_from_row(row) for row in cur.fetchall()]


def record_alignment_decision(
    conn,
    *,
    org_id: str,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    common_key_version_id: str,
    decision: AlignmentDecision,
    actor: str,
) -> AlignmentDecision:
    """Write one decision and return the one that STANDS, which may be older.

    THE FIRST DECISION STANDS. `ON CONFLICT DO NOTHING` above, then a re-read: a
    second click, or a second person, gets the first author and the first date
    back rather than overwriting them. That is the shape
    `plan_spend_decisions.accept_unmatched_spend` already has, and it is what
    makes an arbitration a dated act instead of a mutable field.

    The audit row is written on THIS transaction (`insert_audit_row`, never
    `write_audit_row`): the caller has not committed, so a journal on its own
    connection would assert a decision a rollback erased.
    """
    if not _text(actor):
        raise AlignmentRefused(
            "decision_without_an_author",
            "A decision carries the person who took it.",
        )
    row_id = f"aad_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            _INSERT_DECISION,
            (
                row_id,
                org_id,
                project_id,
                left_datastream_id,
                right_datastream_id,
                common_key_version_id,
                decision.left_row_key,
                decision.decision,
                decision.right_row_key,
                decision.reason,
                decision.decided_by,
            ),
        )
        cur.execute(
            _SELECT_ONE_DECISION,
            (
                project_id,
                left_datastream_id,
                right_datastream_id,
                common_key_version_id,
                decision.left_row_key,
            ),
        )
        row = cur.fetchone()
    if row is None:  # pragma: no cover -- the insert above either wrote or conflicted
        raise AlignmentRefused(
            "decision_not_stored",
            "The decision was neither written nor already present.",
        )
    stored = _decision_from_row(row)
    insert_audit_row(
        conn,
        identity=actor,
        action=_ACTION_BY_DECISION[stored.decision],
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "left_datastream_id": left_datastream_id,
            "right_datastream_id": right_datastream_id,
            "common_key_version_id": common_key_version_id,
            "left_row_key": stored.left_row_key,
            "right_row_key": stored.right_row_key,
            "decision": stored.decision,
            # Whether THIS call is the one that stored it. A journal that reads
            # the same for a write and for a no-op cannot be used to count acts.
            "already_decided": stored.decided_by != decision.decided_by
            or stored.reason != decision.reason,
        },
    )
    return stored


__all__ = [
    "ACTION_ALIGNMENT_ACCEPTED",
    "ACTION_ALIGNMENT_ARBITRATED",
    "ADDED_COLUMNS",
    "ALIGNMENT_DEPENDENCIES",
    "ALIGNMENT_METHODS",
    "ALIGNMENT_METHOD_LABELS",
    "ANALYTICS_ALIGNMENT_CAPABILITY_KEY",
    "AUTOMATIC_METHODS",
    "AlignedRow",
    "AlignmentCurrencies",
    "AlignmentDecision",
    "AlignmentDependencies",
    "AlignmentRefused",
    "AlignmentSide",
    "COLUMN_PREFIX",
    "DECISION_ACCEPTED",
    "DECISION_ARBITRATED",
    "DEPENDENCY_ALIGNABLE_PAIR",
    "DEPENDENCY_APPROVED_RELATIONSHIP",
    "DEPENDENCY_COMMON_KEY",
    "DEPENDENCY_CURRENCY_FX",
    "ENTITY_ID_COLUMN",
    "ENTITY_NAME_COLUMN",
    "METHOD_COLUMN",
    "METHOD_HUMAN_ARBITRATION",
    "METHOD_ID_EXACT",
    "METHOD_KEY_EXACT",
    "METHOD_NAME_NORMALIZED",
    "METHOD_NAME_PREFIX",
    "MISSING_DEPENDENCY_BY_CODE",
    "MissingDependency",
    "NAME_PREFIX_MIN_LENGTH",
    "NO_ALIGNABLE_PAIR",
    "added_columns",
    "alignable_pairs",
    "alignment_counts",
    "alignment_method_label",
    "covering_key_versions",
    "decisions_by_row",
    "list_alignment_decisions",
    "no_alignable_pair_dependencies",
    "record_alignment_decision",
    "resolve_dependencies",
    "run_cascade",
    "unmatched_rows",
]
