"""Story 70.4 -- the prorata ventilation engine of Analytics Alignment.

WHAT IT IS. When one analytics key is COARSER than the media entities it covers
-- several legitimate candidates on the same day, which the cascade of
`core.analytics_alignment` leaves `ambiguous` -- the metrics measured on that one
key are split across the entities in proportion to a DECLARED VOLUME. Clicks per
entity per day, in the reference case. Never in equal parts.

WHY IT IS A MODULE OF ITS OWN AND NOT A SECTION OF `analytics_alignment.py`.
That module says what it owns in its own words: *"Four fields and no metric: this
module aligns IDENTITIES. What the aligned rows then carry in money is the
business of the reading that joins them."* Ventilation is the opposite half --
it carries no identity and does nothing but metrics, weights and their
conservation. Two responsibilities, two files, which is the rule
`docs/product-architecture/module-boundaries.md` states for a file where foreign
subjects would otherwise rewrite each other's logic. The identity module is
already 1 012 lines and every line of it is about the cascade; a reader looking
for "why does this figure not add up" would have to walk all of them.
`AlignedRow` is imported from there and never modified here: the observed
alignment is the input of this engine, not its output.

THE FOUR RULES, AND EACH ONE IS A REFUSAL BEFORE IT IS A FEATURE.

1. THE VOLUME IS DECLARED. :class:`VentilationBasis` carries a name and the
   version that published it, both non-empty, and there is NO default. A default
   volume would be this engine choosing what a client's numbers mean.

2. THE WEIGHTS SUM TO ONE, EXACTLY, UNDER ASSERTION. Decimal, zero tolerance --
   the discipline `core.plan_actual_alignment` states for `split_weight`
   (`SUM(split_weight) = 1.0` per `(plan_id, connector, campaign_ref)`, Decimal,
   never a float on a weight). A sum that deviates RAISES; it is never rounded
   into agreement, because a weight set that does not sum to one is a split
   somebody can no longer defend and silence would publish it anyway.

3. THE OBSERVED IS NEVER OVERWRITTEN, AND THE SPLIT IS REVERSIBLE. A share
   carries its weight, its volume and the total it was taken over.
   ``weight x observed`` summed over the shares of one key re-gives the observed
   figure EXACTLY -- :func:`reassemble` is that sum, and it is a function rather
   than a comment so a test can run it.

4. OBSERVED AND VENTILATED ARE DIFFERENT COLUMNS. A metric that was matched
   one-to-one and a metric that was split are never added inside one column.
   :func:`ventilated_metric_column` derives the ventilated name from the observed
   one, :func:`refuse_mixed_columns` refuses a payload that carries both meanings
   under one name, and a reader who reads only the observed columns still gets a
   total that is true. This is the CENTRAL refusal of the story: *un total qui
   additionne les deux ne peut plus se defendre devant le client.*

A KEY WHOSE VOLUME IS ABSENT OR ZERO IS NOT VENTILATED. It stays whatever the
cascade left it -- `unmatched` or `ambiguous` -- and it is COUNTED, with the code
that says which of the three refusals happened and the gesture that lifts it.
:func:`ventilation_counts` returns every refusal code, at zero rather than
absent, for the reason `analytics_alignment.alignment_counts` states about its
own four states: a state that vanishes when it is empty cannot be watched.

OFF ADDS NOTHING. :func:`ventilate` takes the capability state and returns an
outcome that ran nothing when Analytics Alignment is not active, and
:func:`ventilation_columns` returns `()` -- the shape
`analytics_alignment.added_columns` already has, read through
`capability_is_active` and never against a literal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Iterable, Mapping, Sequence

from ulid import ULID

from core.analytics_alignment import (
    ANALYTICS_ALIGNMENT_CAPABILITY_KEY,
    COLUMN_PREFIX,
    AlignedRow,
    AlignmentRefused,
)
from core.audit import declare_action, insert_audit_row
from core.plan_matching_states import MATCHING_STATE_MATCHED
from core.project_capability_states import capability_is_active

_ZERO = Decimal(0)
_ONE = Decimal(1)

#: How many decimal places a stored weight carries. Eighteen, and the number is
#: a DECLARATION rather than a tuning: it is the scale `NUMERIC(19, 18)` of
#: migration 310 stores, so a weight that round-trips through the database is the
#: same weight that was asserted, to the digit. A scale the code and the column
#: disagreed on would make the assertion true in Python and false in the table.
WEIGHT_SCALE = 18

#: `Decimal("1E-18")` -- the smallest weight this engine distinguishes.
WEIGHT_QUANTUM = Decimal(1).scaleb(-WEIGHT_SCALE)

#: The precision every product and sum below is computed at. Wide on purpose: an
#: observed figure of twenty digits times an eighteen-place weight is thirty-eight
#: significant digits, and the default context of 28 would ROUND it -- which is
#: exactly how a conservation proof turns into a conservation claim.
VENTILATION_PRECISION = 60


# ---------------------------------------------------------------------------
# The columns. Derived from ONE prefix, on the patron story 70.1 set for
# `schema_splits` and story 70.3 reused for its three: a name that can be
# computed from the declaration is a second place for the declaration to be
# contradicted.
# ---------------------------------------------------------------------------

VENTILATION_COLUMN_PREFIX = f"{COLUMN_PREFIX}_ventilation"

#: The share this row received, as a Decimal in [0, 1]. Null on a row nothing
#: ventilated -- never `0`, which would state that this entity received nothing
#: out of a split that happened.
WEIGHT_COLUMN = f"{VENTILATION_COLUMN_PREFIX}_weight"

#: The NAME of the declared volume the split rode on. This column is the reason
#: the story says "aucun volume par defaut implicite": a ventilated figure whose
#: basis is not written beside it cannot be re-derived by the person defending it.
VOLUME_COLUMN = f"{VENTILATION_COLUMN_PREFIX}_volume"

#: The version at which that volume was declared. A declaration that changed is a
#: different split, and a figure published under the old one is not a figure about
#: the new one -- the rule migration 309 states for `common_key_version_id`.
VOLUME_VERSION_COLUMN = f"{VENTILATION_COLUMN_PREFIX}_volume_version"

#: This entity's own volume, and the total it was taken over. Both travel, because
#: `weight` alone cannot be checked: a reader who has the two can recompute the
#: weight, and a reader who has only the weight can only believe it.
VOLUME_VALUE_COLUMN = f"{VENTILATION_COLUMN_PREFIX}_volume_value"
VOLUME_TOTAL_COLUMN = f"{VENTILATION_COLUMN_PREFIX}_volume_total"

#: Whether this row carries the residual -- see :func:`_shares_of_one_key`. Named
#: on the row rather than inferred, because a share that is a hair away from its
#: exact ratio and a share that is exactly its ratio are different facts.
RESIDUAL_COLUMN = f"{VENTILATION_COLUMN_PREFIX}_carries_residual"

#: The provenance columns a ventilated row gains, in the order a reader meets them.
VENTILATION_COLUMNS: tuple[str, ...] = (
    WEIGHT_COLUMN,
    VOLUME_COLUMN,
    VOLUME_VERSION_COLUMN,
    VOLUME_VALUE_COLUMN,
    VOLUME_TOTAL_COLUMN,
    RESIDUAL_COLUMN,
)

#: What a ventilated metric is called. The observed metric keeps its own name and
#: its own meaning; the split share is a SECOND column beside it, never the same
#: one with different contents on different rows.
VENTILATED_METRIC_SUFFIX = "_ventilated"


def ventilated_metric_column(metric_name: Any) -> str:
    """The ventilated twin of an observed metric column.

    Refuses a name that already carries the suffix. Ventilating a ventilated
    column would produce `x_ventilated_ventilated`, and long before that it would
    mean somebody split a figure that was already a share -- the compounding
    version of the fault this module exists to refuse.
    """
    name = str(metric_name or "").strip()
    if not name:
        raise VentilationRefused(
            "metric_without_a_name",
            "A ventilated column is named after the observed metric it splits. Name the "
            "metric, then ventilate it.",
        )
    if name.endswith(VENTILATED_METRIC_SUFFIX):
        raise VentilationRefused(
            "metric_already_ventilated",
            f"{name!r} is already a ventilated share, so splitting it again would state a "
            f"share of a share. Ventilate the observed metric instead.",
        )
    return f"{name}{VENTILATED_METRIC_SUFFIX}"


def refuse_mixed_columns(columns: Iterable[str]) -> None:
    """Refuse a payload where an observed metric and its share share a meaning.

    THE CENTRAL REFUSAL OF STORY 70.4, and it is mechanical rather than a
    judgement. A mixed column is EXACTLY one thing: a name emitted twice. The
    pair (`clicks`, `clicks_ventilated`) is the separation working -- two names,
    two meanings, two totals a reader can take apart. `clicks` appearing twice is
    one column that would carry an exact attribution on some rows and an estimate
    on others, and a total over it could no longer be defended.

    The other half of the same fault is refused upstream, at the moment a name is
    derived: :func:`ventilated_metric_column` will not build the ventilated twin
    of a metric that already carries the suffix, so an observed metric called
    `conversions_ventilated` can never collide with the split of `conversions`.
    """
    names = [str(item) for item in columns]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise VentilationRefused(
            "observed_and_ventilated_in_one_column",
            f"{', '.join(duplicates)} is emitted twice, so one column would carry both an "
            f"observed figure and a ventilated share. Give the ventilated share its own "
            f"column, then read the two separately.",
        )


def ventilation_columns(
    capability_state: str, metric_names: Sequence[str] = ()
) -> tuple[str, ...]:
    """Every column a ventilated reading gains -- and NOTHING while the switch is off.

    Read through `capability_is_active` and never against a literal, for the
    reason `analytics_alignment.added_columns` gives: the lens of
    `governance_read_model` compared a state with `"enabled"`, a word no CHECK
    admits, and could therefore never be true for anybody.
    """
    if not capability_is_active(capability_state):
        return ()
    emitted = [*VENTILATION_COLUMNS, *(ventilated_metric_column(m) for m in metric_names)]
    refuse_mixed_columns(emitted)
    return tuple(emitted)


# ---------------------------------------------------------------------------
# The refusals. A key that is not ventilated says WHICH of the three happened.
# ---------------------------------------------------------------------------


class VentilationRefused(AlignmentRefused):
    """A split that would produce a wrong number. Refused, never approximated.

    A subclass of :class:`core.analytics_alignment.AlignmentRefused` so a caller
    that already guards the cascade catches this too: an alignment and the split
    of its ambiguities are one gesture for the person who runs them, and two
    exception families would let one of the halves escape a handler.
    """


#: At least one candidate publishes NO value for the declared volume. The whole
#: key is refused rather than split over the candidates that do publish one:
#: dropping an entity from the denominator would hand its share to its neighbours
#: and the total would still look right.
REFUSAL_VOLUME_NOT_DECLARED = "volume_not_declared"

#: Every candidate publishes the volume and they all publish zero. There is
#: nothing to be proportional TO, and equal parts is the answer this story
#: refuses by name.
REFUSAL_VOLUME_IS_ZERO = "volume_is_zero"

#: A negative volume. Not a split with a sign: a volume is a count of something
#: that happened, and a negative one is a defect upstream.
REFUSAL_VOLUME_IS_NEGATIVE = "volume_is_negative"

VENTILATION_REFUSALS: tuple[str, ...] = (
    REFUSAL_VOLUME_NOT_DECLARED,
    REFUSAL_VOLUME_IS_ZERO,
    REFUSAL_VOLUME_IS_NEGATIVE,
)

#: What each refusal SAYS, and the act that lifts it. A refusal that names a cause
#: and no gesture sends a person looking for a control -- the rule
#: `analytics_alignment.MissingDependency` already holds for its three blockers.
VENTILATION_REFUSAL_GESTURES: dict[str, str] = {
    REFUSAL_VOLUME_NOT_DECLARED: (
        "Publish the mapping of the Datastream that leaves the declared volume unbound, "
        "then ventilate again."
    ),
    REFUSAL_VOLUME_IS_ZERO: (
        "Nothing to split proportionally: choose a volume the entities of this day actually "
        "carry, declare it in the ventilation basis, then ventilate again."
    ),
    REFUSAL_VOLUME_IS_NEGATIVE: (
        "Open the Datastream that published a negative volume and correct the collection, "
        "then ventilate again."
    ),
}


# ---------------------------------------------------------------------------
# The declaration, the candidates, and one share.
# ---------------------------------------------------------------------------


def _text(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _decimal(value: Any) -> Decimal | None:
    """Coerce a volume to Decimal. NEVER through float, and `None` stays `None`.

    A float on a weight is the one thing `plan_actual_alignment` names twice, and
    a volume becomes a weight one division later. `Decimal(str(x))` is the same
    coercion `_to_decimal` performs there, and it is exact for anything a
    warehouse returns.
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):  # a boolean is not a volume, and `int` would take it
        raise VentilationRefused(
            "volume_is_not_a_number",
            "A distribution volume is a count, not a flag. Declare the column that carries "
            "the count.",
        )
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise VentilationRefused(
            "volume_is_not_a_number",
            f"{value!r} is not a volume this engine can split on. Declare a numeric column, "
            f"then ventilate again.",
        ) from exc


def _day(value: Any) -> str:
    """The day, as the DATE grain this product has everywhere. ISO, never a moment."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = _text(value)
    if not text:
        raise VentilationRefused(
            "ventilation_without_a_day",
            "A split is proportional to a volume OF A DAY. Name the day, then ventilate "
            "again.",
        )
    return text


@dataclass(frozen=True, slots=True)
class VentilationBasis:
    """The DECLARED volume a split rides on, and the version that published it.

    NO DEFAULT, and that is the whole point. A `volume_name` that fell back to
    something when a caller said nothing would be this engine deciding what a
    client's numbers mean, and the ventilated figure would carry a basis nobody
    chose. Both fields are required and both are refused empty.
    """

    volume_name: str
    declared_version: str

    def __post_init__(self) -> None:
        if not _text(self.volume_name):
            raise VentilationRefused(
                REFUSAL_VOLUME_NOT_DECLARED,
                "A prorata split names the volume it is proportional to. Declare the volume "
                "in the ventilation basis, then ventilate again.",
            )
        if not _text(self.declared_version):
            raise VentilationRefused(
                "basis_without_a_version",
                "A declared volume carries the version it was published at, so a figure "
                "split under it can be re-derived. Publish the declaration, then ventilate "
                "again.",
            )

    def as_dict(self) -> dict[str, str]:
        return {
            "volume_name": _text(self.volume_name),
            "declared_version": _text(self.declared_version),
        }


@dataclass(frozen=True, slots=True)
class VentilationCandidate:
    """One media entity that legitimately answers a coarse analytics key on a day.

    ``volume`` is what that entity published for the DECLARED volume of the basis
    -- and `None` is a real answer, distinct from `0`: nobody measured, versus
    somebody measured nothing. The two lead to two different refusals.
    """

    row_key: str
    volume: Any = None

    def declared_volume(self) -> Decimal | None:
        return _decimal(self.volume)


@dataclass(frozen=True, slots=True)
class VentilationGroup:
    """One coarse key, one day, and every candidate the cascade left beside it."""

    alignment_key: str
    day: Any
    candidates: tuple[VentilationCandidate, ...] = ()

    @classmethod
    def of(
        cls, alignment_key: str, day: Any, candidates: Iterable[VentilationCandidate]
    ) -> "VentilationGroup":
        return cls(alignment_key=alignment_key, day=day, candidates=tuple(candidates))


@dataclass(frozen=True, slots=True)
class VentilationShare:
    """The share ONE entity received of ONE coarse key on ONE day.

    Everything needed to re-derive the weight travels with it: the volume this
    entity published, the total it was taken over, the name of the volume and the
    version that declared it. A share carrying only its weight would be a number
    a reader can believe and not check.
    """

    alignment_key: str
    day: str
    right_row_key: str
    weight: Decimal
    volume: Decimal
    volume_total: Decimal
    volume_name: str
    volume_version: str
    carries_residual: bool = False

    def columns(self) -> dict[str, Any]:
        """The provenance columns of a ventilated row. NEVER a metric."""
        return {
            WEIGHT_COLUMN: self.weight,
            VOLUME_COLUMN: self.volume_name,
            VOLUME_VERSION_COLUMN: self.volume_version,
            VOLUME_VALUE_COLUMN: self.volume,
            VOLUME_TOTAL_COLUMN: self.volume_total,
            RESIDUAL_COLUMN: self.carries_residual,
        }

    def ventilate(self, observed: Mapping[str, Any]) -> dict[str, Decimal]:
        """This entity's share of every observed metric, in its OWN columns.

        The returned keys are all `<metric>_ventilated`: the observed mapping is
        read and never returned, so no caller can accidentally emit a share under
        the observed name. Exact Decimal, computed at
        :data:`VENTILATION_PRECISION` -- the same choice `_ventilate_actuals`
        makes when it keeps `spend * split_weight` as an exact Decimal and lets
        the float projection happen once, per cell, at the very end.
        """
        with localcontext() as ctx:
            ctx.prec = VENTILATION_PRECISION
            out: dict[str, Decimal] = {}
            for metric, value in observed.items():
                amount = _decimal(value)
                if amount is None:
                    # An observed metric nobody measured stays unmeasured. A `0`
                    # here would state that this entity received nothing out of a
                    # figure that was never taken.
                    out[ventilated_metric_column(metric)] = None  # type: ignore[assignment]
                    continue
                out[ventilated_metric_column(metric)] = amount * self.weight
            return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "alignment_key": self.alignment_key,
            "day": self.day,
            "right_row_key": self.right_row_key,
            "weight": str(self.weight),
            "volume": str(self.volume),
            "volume_total": str(self.volume_total),
            "volume_name": self.volume_name,
            "volume_version": self.volume_version,
            "carries_residual": self.carries_residual,
        }


@dataclass(frozen=True, slots=True)
class VentilationRefusal:
    """One coarse key that was NOT split, why, and the act that would let it be.

    It is returned rather than logged: a key that is not ventilated stays whatever
    the cascade left it -- `unmatched` or `ambiguous` -- and that is the work.
    Counting it away would hide exactly the rows a person has to go and fix.
    """

    alignment_key: str
    day: str
    code: str
    reason: str
    gesture: str
    candidates: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "alignment_key": self.alignment_key,
            "day": self.day,
            "code": self.code,
            "reason": self.reason,
            "gesture": self.gesture,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True, slots=True)
class VentilationOutcome:
    """Everything one ventilation run produced: the shares, and what it refused."""

    shares: tuple[VentilationShare, ...] = ()
    refusals: tuple[VentilationRefusal, ...] = ()
    basis: VentilationBasis | None = None
    capability_state: str = ""
    #: `False` when the capability was not active. Not an error and not an empty
    #: result to be read as "nothing to split": the run did not happen.
    ran: bool = False

    def shares_of(self, alignment_key: str, day: Any) -> tuple[VentilationShare, ...]:
        wanted_day = _day(day)
        return tuple(
            share
            for share in self.shares
            if share.alignment_key == _text(alignment_key) and share.day == wanted_day
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ran": self.ran,
            "capability_state": self.capability_state,
            "basis": self.basis.as_dict() if self.basis is not None else None,
            "shares": [share.as_dict() for share in self.shares],
            "refusals": [refusal.as_dict() for refusal in self.refusals],
            "counts": ventilation_counts(self),
        }


def ventilation_counts(outcome: VentilationOutcome) -> dict[str, int]:
    """How many keys were split, and how many were refused for each reason.

    EVERY refusal code, at zero rather than absent -- the rule
    `analytics_alignment.alignment_counts` states for its four states and
    `observed_entities.resolution_counts` for `unresolved`: a state that vanishes
    when it is empty cannot be watched, and watching it is how coverage moves.
    """
    counts = {"ventilated_keys": 0, "ventilated_rows": 0, "refused_keys": 0}
    counts.update({code: 0 for code in VENTILATION_REFUSALS})
    counts["ventilated_rows"] = len(outcome.shares)
    counts["ventilated_keys"] = len({(s.alignment_key, s.day) for s in outcome.shares})
    counts["refused_keys"] = len(outcome.refusals)
    for refusal in outcome.refusals:
        counts[refusal.code] = counts.get(refusal.code, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# The weights themselves.
# ---------------------------------------------------------------------------


def assert_weights_sum_to_one(weights: Iterable[Any]) -> Decimal:
    """The invariant, as a function that RAISES. Decimal, zero tolerance.

    The discipline is copied, not re-invented: `plan_actual_alignment` states
    `SUM(split_weight) = 1.0` per `(plan_id, connector, campaign_ref)`, Decimal,
    zero tolerance, and its ventilation re-sums to the original spend EXACTLY
    because of it. Here the same sentence is a callable, so it can be run over
    weights that came back OUT of the database as well as over weights this
    module just computed -- a stored weight set that drifted is exactly the case
    an assertion at computation time cannot see.

    It never rounds into agreement. A deviation of one unit in the last place is
    a split that no longer conserves, and the difference between raising and
    rounding is the difference between a caller who knows and a client who is
    handed a wrong total.
    """
    values = [_decimal(weight) for weight in weights]
    if not values:
        raise VentilationRefused(
            "no_weights_to_sum",
            "A split with no weights conserves nothing. Ventilate the key, then assert.",
        )
    if any(value is None for value in values):
        raise VentilationRefused(
            "weight_is_not_a_number",
            "A weight with no value cannot be summed. Re-read the stored weights of this "
            "key, then assert again.",
        )
    with localcontext() as ctx:
        ctx.prec = VENTILATION_PRECISION
        total = sum(values, _ZERO)
    if total != _ONE:
        raise VentilationRefused(
            "weights_do_not_sum_to_one",
            f"The weights of this key sum to {total}, not to 1. A split that does not "
            f"conserve publishes a total nobody can defend. Ventilate the key again from "
            f"its declared volume, and do not round the difference away.",
        )
    return total


def _shares_of_one_key(
    group: VentilationGroup, *, basis: VentilationBasis
) -> tuple[tuple[VentilationShare, ...], VentilationRefusal | None]:
    """Split ONE (key, day). Either every candidate gets a share, or none does.

    THE RESIDUAL, AND WHY IT HAS A CARRIER RATHER THAN A ROUNDING. Three equal
    candidates have exact weights of 1/3, which no finite decimal writes. Quantizing
    all three to eighteen places gives 0.999999999999999999, and the assertion
    above would then raise on a split that is perfectly legitimate -- an engine
    that refuses thirds is not an engine. So N-1 weights are quantized and the LAST
    one is `1 - sum(others)`: the sum is exactly one BY CONSTRUCTION, and the
    assertion stays a real check on everything else.

    The carrier is the candidate with the LARGEST volume, ties broken by the
    lowest row key. Largest, because the residual is at most (N-1) x 1e-18 and it
    belongs on the share least distorted by it; deterministic, because a residual
    that lands on a different row between two runs makes two runs of one
    declaration disagree. It is NAMED on the share
    (:data:`RESIDUAL_COLUMN`) rather than inferred, and the distance between the
    carrier's weight and its exact ratio is itself asserted below -- a residual
    wider than the quantization that produced it is a defect, not a rounding.
    """
    day = _day(group.day)
    key = _text(group.alignment_key)
    if not key:
        raise VentilationRefused(
            "ventilation_without_a_key",
            "A split is a split OF a coarse key. Name the key, then ventilate again.",
        )
    ordered = sorted(group.candidates, key=lambda item: _text(item.row_key))
    names = tuple(_text(item.row_key) for item in ordered)
    if not ordered:
        # Nothing to split across. Not a refusal with a gesture -- a key with no
        # candidate is `unmatched`, and the cascade already returns it as such.
        return (), None
    if len(set(names)) != len(names):
        # One entity offered twice would receive two shares of the same key, and
        # the store would reject the second on its unique index AFTER the first
        # was written -- a run half stored, with weights that no longer sum to one.
        raise VentilationRefused(
            "candidate_named_twice",
            f"An entity answers {key!r} on {day} twice, so it would receive two shares of "
            f"one split. De-duplicate the candidates of this key, then ventilate again.",
        )

    def _refuse(code: str, reason: str) -> tuple[tuple[()], VentilationRefusal]:
        return (), VentilationRefusal(
            alignment_key=key,
            day=day,
            code=code,
            reason=reason,
            gesture=VENTILATION_REFUSAL_GESTURES[code],
            candidates=names,
        )

    volumes: list[Decimal] = []
    for candidate in ordered:
        volume = candidate.declared_volume()
        if volume is None:
            return _refuse(
                REFUSAL_VOLUME_NOT_DECLARED,
                f"{_text(candidate.row_key)!r} publishes no {basis.volume_name!r} on {day}, "
                f"so the share of every candidate of this key would be taken over a total "
                f"that is missing one of them.",
            )
        if volume < _ZERO:
            return _refuse(
                REFUSAL_VOLUME_IS_NEGATIVE,
                f"{_text(candidate.row_key)!r} publishes a negative {basis.volume_name!r} "
                f"({volume}) on {day}, which is not a share of anything.",
            )
        volumes.append(volume)

    with localcontext() as ctx:
        ctx.prec = VENTILATION_PRECISION
        total = sum(volumes, _ZERO)
        if total == _ZERO:
            return _refuse(
                REFUSAL_VOLUME_IS_ZERO,
                f"Every candidate of this key publishes {basis.volume_name!r} at zero on "
                f"{day}, so there is nothing to be proportional to. Splitting in equal "
                f"parts would invent a distribution nobody measured.",
            )

        carrier = 0
        for index in range(1, len(ordered)):
            if volumes[index] > volumes[carrier] or (
                volumes[index] == volumes[carrier] and names[index] < names[carrier]
            ):
                carrier = index
        weights: list[Decimal] = []
        for index, volume in enumerate(volumes):
            if index == carrier:
                weights.append(_ZERO)  # placeholder, replaced below
                continue
            weights.append((volume / total).quantize(WEIGHT_QUANTUM))
        weights[carrier] = _ONE - sum(
            (w for i, w in enumerate(weights) if i != carrier), _ZERO
        )
        exact_carrier = volumes[carrier] / total
        # The residual is at most one quantum per non-carrier. Wider than that and
        # something other than quantization moved the weight, which is a defect
        # rather than a rounding, and it is refused instead of published.
        if abs(weights[carrier] - exact_carrier) > WEIGHT_QUANTUM * max(len(ordered) - 1, 1):
            raise VentilationRefused(
                "ventilation_residual_out_of_range",
                f"The residual share of {names[carrier]!r} on {day} is further from its "
                f"declared ratio than quantization can explain. Ventilate this key again "
                f"from its declared volume.",
            )

    assert_weights_sum_to_one(weights)
    if any(weight < _ZERO or weight > _ONE for weight in weights):
        raise VentilationRefused(
            "weight_outside_zero_and_one",
            f"A share of {key!r} on {day} fell outside [0, 1], so it is not a share. "
            f"Ventilate this key again from its declared volume.",
        )

    return (
        tuple(
            VentilationShare(
                alignment_key=key,
                day=day,
                right_row_key=names[index],
                weight=weights[index],
                volume=volumes[index],
                volume_total=total,
                volume_name=_text(basis.volume_name),
                volume_version=_text(basis.declared_version),
                carries_residual=index == carrier,
            )
            for index in range(len(ordered))
        ),
        None,
    )


def ventilate(
    groups: Sequence[VentilationGroup],
    *,
    basis: VentilationBasis,
    capability_state: str,
) -> VentilationOutcome:
    """Split every group in proportion to the DECLARED volume of *basis*.

    OFF SPLITS NOTHING. When Analytics Alignment is not active on this Project the
    outcome carries no share, no refusal and `ran=False` -- which is a different
    fact from "every key was refused" and reads as one. `capability_is_active` is
    the reader, never a literal comparison.

    Pure: it opens no connection and writes nothing. Storing the weights is
    :func:`record_ventilation_weights`, a separate call with an author on it,
    because a read that wrote would make re-reading a figure change it.
    """
    if not capability_is_active(capability_state):
        return VentilationOutcome(
            basis=basis, capability_state=_text(capability_state), ran=False
        )

    shares: list[VentilationShare] = []
    refusals: list[VentilationRefusal] = []
    for group in groups:
        produced, refusal = _shares_of_one_key(group, basis=basis)
        shares.extend(produced)
        if refusal is not None:
            refusals.append(refusal)
    return VentilationOutcome(
        shares=tuple(shares),
        refusals=tuple(refusals),
        basis=basis,
        capability_state=_text(capability_state),
        ran=True,
    )


def reassemble(
    shares: Sequence[VentilationShare], observed: Mapping[str, Any]
) -> dict[str, Decimal]:
    """Sum the ventilated shares back. It re-gives the observed figure EXACTLY.

    THE REVERSIBILITY, AS A FUNCTION RATHER THAN A CLAIM. Because the weights sum
    to exactly one and every product is computed at :data:`VENTILATION_PRECISION`
    -- wide enough that nothing rounds -- the sum of the shares is
    `observed x 1 = observed`, to the last digit and therefore to the cent. It is
    the same conservation `plan_actual_alignment` proves for `split_weight`, and
    it is exposed so a caller can re-run it on weights that came back out of the
    database rather than trusting the ones it just computed.

    The observed figure itself is NEVER touched: this reads it and returns a new
    mapping, keyed on the OBSERVED names so the caller can compare the two totals
    side by side.
    """
    assert_weights_sum_to_one([share.weight for share in shares])
    with localcontext() as ctx:
        ctx.prec = VENTILATION_PRECISION
        out: dict[str, Decimal] = {}
        for metric, value in observed.items():
            amount = _decimal(value)
            if amount is None:
                continue
            column = ventilated_metric_column(metric)
            total = _ZERO
            for share in shares:
                total += share.ventilate({metric: amount})[column]
            out[str(metric)] = total
        return out


# ---------------------------------------------------------------------------
# One row of the reading: the observed, and the ventilated, side by side and
# NEVER in one column.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VentilatedRow:
    """One entity row of an aligned, ventilated reading.

    THREE BLOCKS, AND THE MIDDLE ONE IS THE POINT.

    * :meth:`observed_columns` -- the three identity columns story 70.3 adds, plus
      the observed metrics AS MEASURED. On a ventilated row the observed metrics
      are `None`, because the observed figure belongs to the COARSE KEY and not to
      this entity: repeating it on each of N rows would multiply it by N the first
      time somebody summed the column.
    * :meth:`ventilated_columns` -- the share, its provenance, and `<metric>_ventilated`.
      Empty on a row nothing split.
    * :meth:`columns` -- the two together, and :func:`refuse_mixed_columns` runs
      over the union before it is returned. A reader may take either block on its
      own and its total stays true.
    """

    aligned: AlignedRow
    observed: Mapping[str, Any] = field(default_factory=dict)
    share: VentilationShare | None = None

    def __post_init__(self) -> None:
        # The guard `ventilated_rows` enforces, made structural: a row the cascade
        # resolved one-to-one is a fact, and pairing it with a share would replace
        # that fact with an estimate. Living only in the composer left a direct
        # construction free to ventilate a matched row in silence.
        if self.share is not None and self.aligned.state == MATCHING_STATE_MATCHED:
            raise VentilationRefused(
                "matched_row_was_ventilated",
                f"{self.aligned.row_key!r} was resolved one-to-one by the cascade; a matched "
                f"row is a fact and is never split into shares.",
            )

    @property
    def ventilated(self) -> bool:
        return self.share is not None

    def observed_columns(self) -> dict[str, Any]:
        columns: dict[str, Any] = dict(self.aligned.columns())
        for metric in self.observed:
            # `None` on a ventilated row, and the observed value everywhere else.
            # Never `0`: a zero is a measurement, and this row was not measured.
            columns[str(metric)] = None if self.ventilated else self.observed[metric]
        return columns

    def ventilated_columns(self) -> dict[str, Any]:
        if self.share is None:
            # Null and not absent: a reading whose columns appear and disappear per
            # row cannot be a table. The names stay, the values are `None`.
            columns: dict[str, Any] = dict.fromkeys(VENTILATION_COLUMNS, None)
            for metric in self.observed:
                columns[ventilated_metric_column(metric)] = None
            return columns
        return {**self.share.columns(), **self.share.ventilate(self.observed)}

    def columns(self) -> dict[str, Any]:
        observed = self.observed_columns()
        ventilated = self.ventilated_columns()
        refuse_mixed_columns([*observed, *ventilated])
        return {**observed, **ventilated}


def ventilated_rows(
    aligned: Sequence[AlignedRow],
    *,
    outcome: VentilationOutcome,
    observed_by_key: Mapping[str, Mapping[str, Any]],
    day: Any,
) -> tuple[VentilatedRow, ...]:
    """Compose the aligned rows of one day with the shares that split them.

    A row the cascade `matched` one-to-one keeps its observed metrics and gains no
    share -- that figure was attributed, not estimated, and the two must not be
    added inside one column. A key the engine split produces ONE row per candidate,
    each carrying the share and `None` for the observed metrics.

    ``observed_by_key`` is read and never written: the observed figures of the
    coarse keys stay exactly as they were measured, which is the reversibility
    half of the story -- `l'observe d'origine n'est jamais ecrase`.
    """
    wanted_day = _day(day)
    by_key: dict[str, list[VentilationShare]] = {}
    for share in outcome.shares:
        if share.day == wanted_day:
            by_key.setdefault(share.alignment_key, []).append(share)

    rows: list[VentilatedRow] = []
    for row in aligned:
        observed = dict(observed_by_key.get(row.row_key, {}))
        shares = by_key.get(row.row_key)
        if not shares:
            rows.append(VentilatedRow(aligned=row, observed=observed))
            continue
        if row.state == MATCHING_STATE_MATCHED:
            # A row the cascade resolved one-to-one is NOT ventilated, even if a
            # share was computed for its key. Splitting an attributed figure would
            # replace a fact with an estimate, and the caller would never see it.
            raise VentilationRefused(
                "matched_row_was_ventilated",
                f"{row.row_key!r} was resolved one-to-one by the cascade and a split was "
                f"also computed for it. Ventilate the keys the cascade left ambiguous, and "
                f"leave the matched rows as they were attributed.",
            )
        for share in sorted(shares, key=lambda item: item.right_row_key):
            rows.append(VentilatedRow(aligned=row, observed=observed, share=share))
    return tuple(rows)


# ---------------------------------------------------------------------------
# The store. Row by row, because a weight is the record of HOW a published
# figure was split, and the volume it was taken over is re-fetched and moves.
#
# AD-42: the action is declared where it is WRITTEN.
# ---------------------------------------------------------------------------

ACTION_VENTILATION_RECORDED = declare_action("analytics_alignment.ventilation.recorded")

_INSERT_WEIGHT = """
    INSERT INTO app.analytics_alignment_weights
        (id, org_id, project_id, left_datastream_id, right_datastream_id,
         common_key_version_id, alignment_key, activity_date, right_row_key,
         volume_name, volume_version, volume_value, volume_total, weight,
         carries_residual, computed_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (project_id, left_datastream_id, right_datastream_id,
                 common_key_version_id, alignment_key, activity_date, right_row_key)
    DO NOTHING
"""

_SELECT_WEIGHTS = """
    SELECT alignment_key, activity_date, right_row_key, weight, volume_value,
           volume_total, volume_name, volume_version, carries_residual
      FROM app.analytics_alignment_weights
     WHERE project_id = %s AND left_datastream_id = %s AND right_datastream_id = %s
       AND common_key_version_id = %s
     ORDER BY alignment_key, activity_date, right_row_key
"""

#: The stored shares of ONE (key, day). No `FOR UPDATE`: migration 310 REVOKES
#: UPDATE from the writer role, and a row lock needs it. Concurrent writers of
#: one (key, day) are serialised by the advisory transaction lock taken just
#: before this read in `record_ventilation_weights`; this plain read then makes a
#: re-run (sequential OR the loser of that lock) skip an already-written group.
#: The AFTER re-read in the same function stays as a second net over any drift
#: the in-memory computation could carry, but it is the LOCK, not the re-read,
#: that closes the disjoint-write-skew a post-write assertion cannot see.
_SELECT_DAY_SHARES = """
    SELECT right_row_key, weight
      FROM app.analytics_alignment_weights
     WHERE project_id = %s AND left_datastream_id = %s AND right_datastream_id = %s
       AND common_key_version_id = %s AND alignment_key = %s AND activity_date = %s
"""


def _share_from_row(row: Sequence[Any]) -> VentilationShare:
    return VentilationShare(
        alignment_key=str(row[0]),
        day=_day(row[1]),
        right_row_key=str(row[2]),
        weight=_decimal(row[3]),
        volume=_decimal(row[4]),
        volume_total=_decimal(row[5]),
        volume_name=str(row[6]),
        volume_version=str(row[7]),
        carries_residual=bool(row[8]),
    )


def list_ventilation_weights(
    conn,
    *,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    common_key_version_id: str,
) -> list[VentilationShare]:
    """Every stored share of one pair under one key version. A READ that writes nothing."""
    with conn.cursor() as cur:
        cur.execute(
            _SELECT_WEIGHTS,
            (project_id, left_datastream_id, right_datastream_id, common_key_version_id),
        )
        return [_share_from_row(row) for row in cur.fetchall()]


def assert_stored_weights_conserve(shares: Sequence[VentilationShare]) -> dict[str, Decimal]:
    """Re-run the invariant over shares that came back OUT of the database.

    The assertion at computation time cannot see a stored weight set that drifted
    -- a partially written run, a row erased by a cascade, a column with the wrong
    scale. This groups by `(key, day)` and asserts each group, so a re-read is a
    real check rather than a re-statement of what was already believed.
    """
    grouped: dict[tuple[str, str], list[VentilationShare]] = {}
    for share in shares:
        grouped.setdefault((share.alignment_key, share.day), []).append(share)
    totals: dict[str, Decimal] = {}
    for (key, day), group in sorted(grouped.items()):
        totals[f"{key}|{day}"] = assert_weights_sum_to_one([item.weight for item in group])
    return totals


def record_ventilation_weights(
    conn,
    *,
    org_id: str,
    project_id: str,
    left_datastream_id: str,
    right_datastream_id: str,
    common_key_version_id: str,
    shares: Sequence[VentilationShare],
    actor: str,
) -> int:
    """Store the shares of one run. THE FIRST RUN OF A (key, day) STANDS.

    `ON CONFLICT DO NOTHING` on the unique index of migration 310 -- the shape
    migration 245 chose for `app.plan_unmatched_spend_decisions` and migration 309
    reused for the alignment decisions. A second run over a volume that has since
    been re-fetched does NOT rewrite a split a client was already shown: the
    figure that was published stays re-derivable from the weights that produced
    it. Re-splitting under a corrected volume is a gesture no story has opened,
    and migration 310 REVOKES UPDATE rather than leaving the privilege lying
    about for one to arrive without one. DELETE is deliberately kept: this table
    is an ON DELETE CASCADE child of `app.organizations`, and a role with no
    DELETE turns a right-to-erasure request into a 42501 -- the fault
    `test_the_erasure_hatch_is_a_privilege_too.py` exists to catch.

    The invariant is asserted twice: BEFORE the first INSERT over the shares in
    hand, and AFTER, re-read from the database over every (key, day) this call
    touched. The pre-check refuses a run that does not conserve; the post-check
    catches the drift the pre-check cannot see -- an ADDITIVE re-run, where a new
    `right_row_key` answers a (key, day) already split. That row does not conflict
    on the per-row unique index, so `ON CONFLICT DO NOTHING` would insert it on
    top of a finished split and leave the group summing past 1. So a (key, day)
    that already carries ANY stored share is written ALL-OR-NOTHING and this run
    writes NONE of it: the first run stands, exactly as the paragraph above
    promises, and the client keeps the split that already sums to 1.

    The audit row is written on THIS transaction (`insert_audit_row`, never
    `write_audit_row`): the caller has not committed, so a journal on its own
    connection would assert a split a rollback erased.
    """
    if not _text(actor):
        raise VentilationRefused(
            "ventilation_without_an_author",
            "A stored split carries the person who ran it. Name the actor, then record the "
            "weights again.",
        )
    if not shares:
        raise VentilationRefused(
            "no_shares_to_record",
            "There is no split to store. Ventilate the keys the cascade left ambiguous, "
            "then record the weights.",
        )
    assert_stored_weights_conserve(shares)

    groups: dict[tuple[str, str], list[VentilationShare]] = {}
    for share in shares:
        groups.setdefault((share.alignment_key, share.day), []).append(share)

    written = 0
    written_groups: list[tuple[str, str]] = []
    with conn.cursor() as cur:
        # Acquire the per-(key, day) locks in a TOTAL ORDER -- sorted by the group
        # key. Two runs whose groups overlap would otherwise be free to take the
        # same two locks in opposite orders and deadlock; a consistent order makes
        # that impossible. (A deadlock would roll back, never commit a corrupt
        # split, so this is hardening rather than a correctness fix -- but it turns
        # a possible error into no error.)
        for (key, day), group in sorted(groups.items()):
            # SERIALISE concurrent writers of THIS (key, day). The per-row unique
            # index cannot: two runs writing DISJOINT `right_row_key` sets to one
            # (key, day) never conflict on a row, so under READ COMMITTED both see
            # an empty group, both insert a full split, and both commit -- the
            # store carries a (key, day) summing to 2. A transaction-scoped
            # advisory lock keyed on the (pair, key, day) closes that write-skew
            # without the UPDATE privilege a row lock needs (migration 310 revoked
            # it): the second writer blocks here, and once through sees the first's
            # committed rows and skips. A different (key, day) hashes elsewhere and
            # never contends; a hash collision only over-serialises, never
            # under-serialises.
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (
                    "\x1f".join(
                        str(part)
                        for part in (
                            project_id,
                            left_datastream_id,
                            right_datastream_id,
                            common_key_version_id,
                            key,
                            day,
                        )
                    ),
                ),
            )
            cur.execute(
                _SELECT_DAY_SHARES,
                (
                    project_id,
                    left_datastream_id,
                    right_datastream_id,
                    common_key_version_id,
                    key,
                    day,
                ),
            )
            if cur.fetchall():
                # THE FIRST RUN OF THIS (key, day) STANDS. A second run -- a
                # re-fetched volume, an enlarged candidate set -- writes NONE of
                # this group rather than adding a share on top of a finished
                # split. Silent by design: re-splitting is a gesture no story has
                # opened, and the published figure stays re-derivable.
                continue
            for share in group:
                cur.execute(
                    _INSERT_WEIGHT,
                    (
                        f"aaw_{ULID()}",
                        org_id,
                        project_id,
                        left_datastream_id,
                        right_datastream_id,
                        common_key_version_id,
                        share.alignment_key,
                        share.day,
                        share.right_row_key,
                        share.volume_name,
                        share.volume_version,
                        share.volume,
                        share.volume_total,
                        share.weight,
                        share.carries_residual,
                        actor,
                    ),
                )
                written += cur.rowcount or 0
            written_groups.append((key, day))

        # The invariant re-read from the database, over what this call wrote. A
        # group that did not sum to 1 in the store raises here and the caller's
        # transaction rolls back -- the split is never left half-written.
        for key, day in written_groups:
            cur.execute(
                _SELECT_DAY_SHARES,
                (
                    project_id,
                    left_datastream_id,
                    right_datastream_id,
                    common_key_version_id,
                    key,
                    day,
                ),
            )
            assert_weights_sum_to_one([_decimal(row[1]) for row in cur.fetchall()])

    insert_audit_row(
        conn,
        identity=actor,
        action=ACTION_VENTILATION_RECORDED,
        provider_account="",
        connection_ref="",
        metadata={
            "project_id": project_id,
            "left_datastream_id": left_datastream_id,
            "right_datastream_id": right_datastream_id,
            "common_key_version_id": common_key_version_id,
            "capability_key": ANALYTICS_ALIGNMENT_CAPABILITY_KEY,
            "volume_name": shares[0].volume_name,
            "volume_version": shares[0].volume_version,
            "shares_offered": len(shares),
            # How many rows THIS call actually stored. A journal that reads the
            # same for a write and for a no-op cannot be used to count acts.
            "shares_written": written,
        },
    )
    return written


__all__ = [
    "ACTION_VENTILATION_RECORDED",
    "REFUSAL_VOLUME_IS_NEGATIVE",
    "REFUSAL_VOLUME_IS_ZERO",
    "REFUSAL_VOLUME_NOT_DECLARED",
    "RESIDUAL_COLUMN",
    "VENTILATED_METRIC_SUFFIX",
    "VENTILATION_COLUMNS",
    "VENTILATION_COLUMN_PREFIX",
    "VENTILATION_PRECISION",
    "VENTILATION_REFUSALS",
    "VENTILATION_REFUSAL_GESTURES",
    "VOLUME_COLUMN",
    "VOLUME_TOTAL_COLUMN",
    "VOLUME_VALUE_COLUMN",
    "VOLUME_VERSION_COLUMN",
    "WEIGHT_COLUMN",
    "WEIGHT_QUANTUM",
    "WEIGHT_SCALE",
    "VentilatedRow",
    "VentilationBasis",
    "VentilationCandidate",
    "VentilationGroup",
    "VentilationOutcome",
    "VentilationRefusal",
    "VentilationRefused",
    "VentilationShare",
    "assert_stored_weights_conserve",
    "assert_weights_sum_to_one",
    "list_ventilation_weights",
    "reassemble",
    "record_ventilation_weights",
    "refuse_mixed_columns",
    "ventilate",
    "ventilated_metric_column",
    "ventilated_rows",
    "ventilation_columns",
    "ventilation_counts",
]
