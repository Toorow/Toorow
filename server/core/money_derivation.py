"""The one place a monetary value becomes comparable, or refuses (Story 48.3).

Before this module there were four: ``fx_helper.convert`` (float, seed fallback,
synchronous provider), the ``fx_convert_at_read`` dbt macro
(``CAST(... AS DOUBLE) * COALESCE(fx_rate, 1.0)``), ``money_reconciliation`` and
two public REST calculators that accepted caller-authored amounts. Four
implementations of "what is this worth in the reporting currency" is four answers,
and the ``COALESCE(rate, 1.0)`` one was the dangerous kind: it produced a number
whenever the rate was missing, so an unconvertible figure was added into a total
at parity and nothing anywhere said so.

What this module guarantees, and what each guarantee replaces:

* **A derived value always carries its evidence.** :class:`MoneyDerivation` holds
  the native amount, currency and unit UNCHANGED alongside the reporting value,
  the exact rate, its as-of date, method and source, and the policy and rate-set
  version ids. A payload with a bare ``value`` and a partial ``fx`` block is not
  enough for a reader to reproduce the meaning six months later (AC3, AC9).
* **Unknown is a typed gap, never a healthy-looking number.** Missing currency,
  missing unit, missing adapter, missing rate, stale rate: each is a
  ``money_gap_code`` on a derivation whose ``reporting_amount_micros`` is
  ``None``. There is no branch that substitutes 1.0, EUR or the native amount.
* **A mixed-currency total is refused unless every contribution converted.**
  :func:`aggregate` is the additive path: it sums only when each contribution
  carries compatible evidence under the SAME pinned policy and rate-set version.
  Same-currency identity is recorded as evidence with ``method='identity'``.
* **Ratios are computed from normalized components, never from an unsafe total.**
  :func:`ratio` normalizes numerator and denominator separately and refuses if
  either is unsafe -- it never converts an already-aggregated value.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence

from core.fx_rate_sets import FxGap, RateEvidence, resolve_rate
from core.money import MoneyAdapterError, convert_micros, micros_to_decimal, to_canonical_micros
from core.money_policy import MoneyPolicy

logger = logging.getLogger(__name__)

# Typed gap codes. Each names a DIFFERENT missing thing, because "it did not
# convert" is not actionable and "this Datastream never declared a currency" is.
GAP_NO_CURRENCY = "native_currency_missing"
GAP_NO_UNIT = "native_unit_missing"
GAP_NO_ADAPTER = "money_adapter_missing"
GAP_UNKNOWN_CURRENCY = "native_currency_unknown"
GAP_NO_RATE = "fx_rate_unavailable"
GAP_STALE_RATE = "fx_rate_too_stale"
GAP_MIXED_UNSAFE = "mixed_currency_without_evidence"
# Migration 288. Both are governance defects a person REPAIRS, not missing data --
# folding them into `fx_rate_unavailable` would send someone hunting for a rate that
# is already there. `conflict`: two equally specific posed rules matched and neither
# can be preferred. `unresolved`: a posed rule is conditioned on something this
# figure does not carry, so whether it applies is unknown.
GAP_RATE_CONFLICT = "fx_rate_rules_conflict"
GAP_RATE_CONDITION_UNRESOLVED = "fx_rate_condition_unresolved"

#: The FxGap code each typed refusal maps to. A table, not an if/elif chain: the next
#: gap added to `fx_rate_sets` must be given a name here rather than inherit
#: "unavailable" by falling off the end of a branch.
_FX_GAP_CODES = {
    "rate_too_stale": GAP_STALE_RATE,
    "fx_condition_conflict": GAP_RATE_CONFLICT,
    "fx_condition_unresolved": GAP_RATE_CONDITION_UNRESOLVED,
}


@dataclass(frozen=True, slots=True)
class NativeMoney:
    """What a publication actually landed. Immutable input; never rewritten."""

    amount: Decimal | int | str | float
    currency: str | None
    unit: str | None
    #: The exact adapter that produced the canonical micros. A value whose adapter
    #: is unknown cannot be trusted to be in the unit it claims.
    adapter: str | None = None
    on_date: date | None = None
    source_reference: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class MoneyDerivation:
    """One value's complete monetary meaning: native, reporting, and the proof."""

    native_amount_micros: int | None
    native_currency: str | None
    native_unit: str | None
    reporting_amount_micros: int | None
    reporting_currency: str
    money_policy_version_id: str
    rate: RateEvidence | None
    money_gap_code: str | None
    gap_message: str | None = None

    @property
    def is_safe(self) -> bool:
        """True when this value may take part in an additive total."""
        return self.money_gap_code is None and self.reporting_amount_micros is not None

    def reporting_decimal(self, minor_unit: int) -> Decimal | None:
        if self.reporting_amount_micros is None:
            return None
        return micros_to_decimal(self.reporting_amount_micros, minor_unit=minor_unit)

    def as_payload(self, *, minor_unit: int) -> dict[str, Any]:
        """The semantic evidence contract every consumer reads (AC3, AC9).

        Native and reporting are BOTH present, and reporting is nullable. A
        consumer that finds ``reporting_amount_micros`` null and
        ``money_gap_code`` set knows exactly what is missing and where to repair it.
        """
        payload: dict[str, Any] = {
            "native_amount_micros": self.native_amount_micros,
            "native_currency_code": self.native_currency,
            "native_unit": self.native_unit,
            "reporting_amount_micros": self.reporting_amount_micros,
            "reporting_amount_decimal": (
                str(self.reporting_decimal(minor_unit))
                if self.reporting_amount_micros is not None
                else None
            ),
            "reporting_currency_code": self.reporting_currency,
            "money_policy_version_id": self.money_policy_version_id,
            "money_gap_code": self.money_gap_code,
            "money_gap_message": self.gap_message,
        }
        if self.rate is not None:
            payload.update(self.rate.as_payload())
        else:
            payload.update(
                {
                    "fx_rate": None,
                    "fx_method": None,
                    "fx_as_of_date": None,
                    "fx_source": None,
                    "fx_rate_set_version_id": None,
                }
            )
        return payload


def _gap(
    native_micros: int | None,
    native: NativeMoney,
    policy: MoneyPolicy,
    code: str,
    message: str,
) -> MoneyDerivation:
    return MoneyDerivation(
        native_amount_micros=native_micros,
        native_currency=native.currency,
        native_unit=native.unit,
        reporting_amount_micros=None,
        reporting_currency=policy.reporting_currency,
        money_policy_version_id=policy.version_id,
        rate=None,
        money_gap_code=code,
        gap_message=message,
    )


def derive(
    conn,
    *,
    project_id: str,
    native: NativeMoney,
    policy: MoneyPolicy,
) -> MoneyDerivation:
    """Derive one value's reporting amount, or record exactly why it could not be.

    Total: it always returns a derivation. A caller never has to decide what an
    exception means, which is how the previous code ended up with a ``1.0``.

    THE CONDITION CONTEXT COMES FROM ``native.source_reference``. A Project may post
    a rate that holds only in a named case ("this rate when country = FR", migration
    288), and :func:`core.fx_rate_sets.resolve_rate` needs to know what this figure
    IS in order to rank those rules. ``source_reference`` is the only thing a
    :class:`NativeMoney` carries that describes where it came from, so it is the
    context -- filtered to the keys a rate may be conditioned on, so unrelated
    provenance never silently becomes a matching clause. A Project with no
    conditional rate is unaffected: an empty condition matches everything.
    """

    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415
    from core.fx_fixed_rates import CONDITION_KEYS  # noqa: PLC0415

    if not native.currency:
        return _gap(
            None,
            native,
            policy,
            GAP_NO_CURRENCY,
            "This value has no source currency, so it cannot be compared or summed.",
        )
    resolved = resolve_currency(native.currency)
    if resolved is None:
        return _gap(
            None,
            native,
            policy,
            GAP_UNKNOWN_CURRENCY,
            f"{native.currency!r} does not resolve against the governed ISO 4217 vocabulary.",
        )
    if not native.unit:
        return _gap(
            None,
            native,
            policy,
            GAP_NO_UNIT,
            "This value has no declared native unit, so its magnitude is unknown.",
        )
    if not native.adapter:
        return _gap(
            None,
            native,
            policy,
            GAP_NO_ADAPTER,
            "No exact money adapter is recorded for this value, so the unit it claims "
            "cannot be trusted.",
        )

    try:
        native_micros = to_canonical_micros(native.amount, native.unit)
    except MoneyAdapterError as exc:
        return _gap(None, native, policy, GAP_NO_UNIT, str(exc))

    on_date = native.on_date or date.today()
    try:
        rate = resolve_rate(
            conn,
            project_id=project_id,
            base_currency=resolved.code,
            quote_currency=policy.reporting_currency,
            on_date=on_date,
            policy=policy,
            context={
                key: value
                for key, value in (native.source_reference or {}).items()
                if key in CONDITION_KEYS
            },
        )
    except FxGap as gap:
        code = _FX_GAP_CODES.get(gap.code, GAP_NO_RATE)
        return MoneyDerivation(
            native_amount_micros=native_micros,
            native_currency=resolved.code,
            native_unit=native.unit,
            reporting_amount_micros=None,
            reporting_currency=policy.reporting_currency,
            money_policy_version_id=policy.version_id,
            rate=None,
            money_gap_code=code,
            gap_message=str(gap),
        )

    reporting_micros = convert_micros(
        native_micros,
        rate.rate,
        minor_unit=policy.reporting_currency_minor_unit,
        rounding=policy.rounding_mode,
    )
    return MoneyDerivation(
        native_amount_micros=native_micros,
        native_currency=resolved.code,
        native_unit=native.unit,
        reporting_amount_micros=reporting_micros,
        reporting_currency=policy.reporting_currency,
        money_policy_version_id=policy.version_id,
        rate=rate,
        money_gap_code=None,
    )


# ---------------------------------------------------------------------------
# Aggregation. The additive path, and the one that must refuse.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AggregateResult:
    """A total, or an explicit refusal with the contributions that caused it."""

    total_micros: int | None
    reporting_currency: str
    money_policy_version_id: str
    rate_set_version_ids: tuple[str, ...]
    contributions: tuple[MoneyDerivation, ...]
    refused: bool
    refusal_code: str | None = None
    refusal_message: str | None = None

    def as_payload(self, *, minor_unit: int) -> dict[str, Any]:
        return {
            "refused": self.refused,
            "refusal_code": self.refusal_code,
            "refusal_message": self.refusal_message,
            "total_amount_micros": self.total_micros,
            "total_amount_decimal": (
                str(micros_to_decimal(self.total_micros, minor_unit=minor_unit))
                if self.total_micros is not None
                else None
            ),
            "reporting_currency_code": self.reporting_currency,
            "money_policy_version_id": self.money_policy_version_id,
            "fx_rate_set_version_ids": list(self.rate_set_version_ids),
            "contributions": [
                item.as_payload(minor_unit=minor_unit) for item in self.contributions
            ],
        }


def aggregate(
    derivations: Sequence[MoneyDerivation], *, policy: MoneyPolicy
) -> AggregateResult:
    """Sum derived values, or refuse with the exact reason.

    The refusal is not "some rows failed": it names the gap codes and the
    contributions that carry them, so a surface can render which Datastream needs
    repairing rather than a red box.

    An EMPTY input is not a zero. Zero is a real total that means "nothing was
    spent"; nothing to sum means the question had no data, and returning ``0``
    for it is the same class of lie as returning parity for a missing rate.
    """

    contributions = tuple(derivations)
    rate_versions = tuple(
        sorted(
            {
                item.rate.rate_set_version_id
                for item in contributions
                if item.rate is not None and item.rate.rate_set_version_id
            }
        )
    )
    if not contributions:
        return AggregateResult(
            total_micros=None,
            reporting_currency=policy.reporting_currency,
            money_policy_version_id=policy.version_id,
            rate_set_version_ids=(),
            contributions=(),
            refused=True,
            refusal_code="no_contributions",
            refusal_message="There is nothing to total; an empty sum is not zero.",
        )

    unsafe = [item for item in contributions if not item.is_safe]
    if unsafe:
        codes = sorted({item.money_gap_code or "unknown" for item in unsafe})
        return AggregateResult(
            total_micros=None,
            reporting_currency=policy.reporting_currency,
            money_policy_version_id=policy.version_id,
            rate_set_version_ids=rate_versions,
            contributions=contributions,
            refused=True,
            refusal_code=GAP_MIXED_UNSAFE,
            refusal_message=(
                f"{len(unsafe)} of {len(contributions)} contributions have no compatible "
                f"conversion evidence ({', '.join(codes)}), so this total would be a "
                "plausible but false number."
            ),
        )

    mismatched = [
        item for item in contributions if item.money_policy_version_id != policy.version_id
    ]
    if mismatched:
        return AggregateResult(
            total_micros=None,
            reporting_currency=policy.reporting_currency,
            money_policy_version_id=policy.version_id,
            rate_set_version_ids=rate_versions,
            contributions=contributions,
            refused=True,
            refusal_code="policy_version_mismatch",
            refusal_message=(
                "Contributions were derived under more than one Money Policy version; "
                "summing them would mix two definitions of the same currency."
            ),
        )

    total = sum(item.reporting_amount_micros or 0 for item in contributions)
    return AggregateResult(
        total_micros=total,
        reporting_currency=policy.reporting_currency,
        money_policy_version_id=policy.version_id,
        rate_set_version_ids=rate_versions,
        contributions=contributions,
        refused=False,
    )


def ratio(
    numerator: Sequence[MoneyDerivation] | AggregateResult,
    denominator: Sequence[MoneyDerivation] | AggregateResult,
    *,
    policy: MoneyPolicy,
) -> dict[str, Any]:
    """A governed monetary ratio: normalize both sides first, then divide.

    AC5's second sentence, made structural: the two sides are aggregated
    INDEPENDENTLY through :func:`aggregate`, so a ratio can never be computed by
    converting a total that was itself unsafe. A zero denominator is ``None``
    with a stated reason, never an infinity and never a silent zero.
    """

    top = (
        numerator
        if isinstance(numerator, AggregateResult)
        else aggregate(numerator, policy=policy)
    )
    bottom = (
        denominator
        if isinstance(denominator, AggregateResult)
        else aggregate(denominator, policy=policy)
    )
    if top.refused or bottom.refused:
        failing = top if top.refused else bottom
        return {
            "refused": True,
            "refusal_code": failing.refusal_code,
            "refusal_message": failing.refusal_message,
            "value": None,
            "money_policy_version_id": policy.version_id,
        }
    if not bottom.total_micros:
        return {
            "refused": False,
            "refusal_code": None,
            "value": None,
            "reason": "zero_denominator",
            "money_policy_version_id": policy.version_id,
        }
    return {
        "refused": False,
        "refusal_code": None,
        "value": str(Decimal(top.total_micros or 0) / Decimal(bottom.total_micros)),
        "numerator_amount_micros": top.total_micros,
        "denominator_amount_micros": bottom.total_micros,
        "reporting_currency_code": policy.reporting_currency,
        "money_policy_version_id": policy.version_id,
        "fx_rate_set_version_ids": sorted(
            set(top.rate_set_version_ids) | set(bottom.rate_set_version_ids)
        ),
    }


def derive_many(
    conn,
    *,
    project_id: str,
    natives: Iterable[NativeMoney],
    policy: MoneyPolicy,
) -> list[MoneyDerivation]:
    """Derive a batch under one pinned policy. Convenience, not a second path."""
    return [derive(conn, project_id=project_id, native=item, policy=policy) for item in natives]
