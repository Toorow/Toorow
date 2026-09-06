"""Money Policy and Reporting Timezone Policy: three profiles, one lifecycle.

Story 48.3 makes both foundations *always present* and *explicitly confirmed*.
Those are two different statements and the second is the one the code kept
losing: ``app.project_preferences`` carried ``'EUR'`` and ``'Europe/Paris'`` from
a column default, so every Project looked decided and nothing could name the
version a Result was derived under.

This module supplies the three governed profiles that replace those columns as
authority, mounted on the generic :mod:`core.governance_rule_sets` lifecycle:

* ``money_policy`` -- reporting currency, exact internal scale and rounding,
  rate-source priority, triangulation and carry-forward rules, maximum staleness;
* ``fx_ingestion`` -- who publishes rates, on what cadence, what validation a
  batch must pass, and whether a validated batch may activate without a human;
* ``timezone_policy`` -- reporting IANA zone, pinned tzdb version, and the
  ``DATE`` versus timestamp derivation and refusal rules.

Two rules hold across all three:

* **Fail closed, and say so with a type.** No confirmed policy is not "EUR" and
  not "UTC": it is :class:`PolicyGap`, which a caller must handle. Every read
  here either returns a pinned version or raises.
* **A resolved policy always names its version.** Every returned object carries
  the rule-set id, version id and content hash it came from, so a Result payload
  can pin them and a later reader can reproduce the meaning (AC9).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from core.governance_rule_sets import (
    RuleSetError,
    RuleSetFormField,
    RuleSetFormOption,
    RuleSetProfile,
    active_version,
    register_profile,
)

logger = logging.getLogger(__name__)

FAMILY_MONEY = "money_policy"
FAMILY_FX_INGESTION = "fx_ingestion"
FAMILY_TIMEZONE = "timezone_policy"

#: One head per family per Project. The name is fixed because there is exactly
#: one reporting currency and one reporting boundary per Project -- a second
#: named Money Policy would be a second authority, which is the defect this
#: story removes rather than parameterizes.
POLICY_NAME = "project_policy"

PROFILE_MONEY = "money_policy_v1"
PROFILE_FX_INGESTION = "fx_ingestion_v1"
PROFILE_TIMEZONE = "timezone_policy_v1"

ROUNDING_MODES = {"half_even": ROUND_HALF_EVEN, "half_up": ROUND_HALF_UP}

#: How a source ``DATE`` with no usable timestamp is treated. There is exactly
#: one legal value, and it is a constant rather than a setting on purpose: at
#: DATE grain there is no sub-day data to re-slice, so "shift it" is not a policy
#: choice a Project could make correctly.
DATE_ONLY_SIGNAL = "signal_never_shift"

TIMESTAMP_DERIVE = "derive_when_sufficient"
TIMESTAMP_NEVER = "never_derive"

DST_GAP_POLICIES = ("refuse", "shift_forward")
DST_OVERLAP_POLICIES = ("first_occurrence", "second_occurrence", "refuse")


class PolicyGap(RuntimeError):
    """A required governed policy is absent, unreadable or unconfirmed.

    Deliberately not a ``ValueError``: a caller catching bad input must not
    swallow "this Project never confirmed a reporting currency" and continue with
    an implicit one. Carries a typed ``code`` so a surface can render the gap and
    a repair route instead of a stack trace.
    """

    def __init__(self, code: str, message: str, *, capability: str):
        self.code = code
        self.capability = capability
        super().__init__(message)


# ---------------------------------------------------------------------------
# Profile validators. Each NORMALIZES, so two logically identical payloads hash
# identically and "has the policy changed?" stays answerable.
# ---------------------------------------------------------------------------


def _validate_money_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    currency = resolve_currency(payload.get("reporting_currency"))
    if currency is None:
        raise RuleSetError(
            "reporting_currency must resolve against the governed ISO 4217 vocabulary"
        )
    if not currency.is_tender:
        raise RuleSetError(
            f"{currency.code} is a {currency.kind} code, not legal tender; "
            "it cannot be a Project reporting currency"
        )
    if currency.minor_unit is None:
        raise RuleSetError(
            f"{currency.code} has no ISO minor unit, so a converted amount could not "
            "be rounded to a defined precision"
        )

    rounding = str(payload.get("rounding") or "half_even")
    if rounding not in ROUNDING_MODES:
        raise RuleSetError(f"rounding must be one of {sorted(ROUNDING_MODES)}")

    staleness = payload.get("max_staleness_days")
    if not isinstance(staleness, int) or isinstance(staleness, bool) or staleness < 0:
        raise RuleSetError("max_staleness_days must be a non-negative integer")
    if staleness > 365:
        raise RuleSetError("max_staleness_days above a year is not a staleness bound")

    priority = payload.get("rate_source_priority")
    if not isinstance(priority, (list, tuple)) or not priority:
        raise RuleSetError("rate_source_priority must list at least one source, in order")
    sources = [str(item).strip() for item in priority]
    if any(not item for item in sources):
        raise RuleSetError("rate_source_priority entries cannot be blank")
    if len(set(sources)) != len(sources):
        raise RuleSetError("rate_source_priority cannot name the same source twice")

    allow_triangulation = bool(payload.get("allow_triangulation", False))
    pivot = payload.get("triangulation_pivot")
    if allow_triangulation:
        pivot_currency = resolve_currency(pivot)
        if pivot_currency is None:
            raise RuleSetError(
                "allow_triangulation requires a triangulation_pivot that resolves"
            )
        pivot = pivot_currency.code
    else:
        pivot = None

    return {
        "reporting_currency": currency.code,
        "reporting_currency_minor_unit": currency.minor_unit,
        "internal_scale": "micros",
        "rounding": rounding,
        "rate_source_priority": sources,
        "allow_triangulation": allow_triangulation,
        "triangulation_pivot": pivot,
        "allow_carry_forward": bool(payload.get("allow_carry_forward", False)),
        "max_staleness_days": staleness,
        "reconciliation_refs": sorted(
            str(item) for item in (payload.get("reconciliation_refs") or [])
        ),
    }


def _validate_fx_ingestion(payload: Mapping[str, Any]) -> dict[str, Any]:
    from core.currency_vocabulary import resolve_currency  # noqa: PLC0415

    provider = str(payload.get("provider") or "").strip()
    if not provider:
        raise RuleSetError("an FX ingestion policy must name its provider")
    cadence = str(payload.get("cadence") or "daily").strip()
    if cadence not in {"daily", "weekly", "manual"}:
        raise RuleSetError("cadence must be daily, weekly or manual")
    base = resolve_currency(payload.get("base_currency"))
    if base is None:
        raise RuleSetError("base_currency must resolve against the governed vocabulary")
    move = payload.get("max_relative_move")
    if move is not None:
        try:
            move_value = float(move)
        except (TypeError, ValueError) as exc:
            raise RuleSetError("max_relative_move must be a number") from exc
        if not 0 < move_value <= 1:
            raise RuleSetError("max_relative_move must be a fraction in (0, 1]")
    else:
        move_value = None
    return {
        "provider": provider,
        "provider_reference": str(payload.get("provider_reference") or "").strip() or None,
        "cadence": cadence,
        "base_currency": base.code,
        # AC4: a validated batch may activate under the CONFIRMED policy without
        # asking a human every business day. Turning this off means a human
        # activates each batch, and stale rates then block conversions.
        "auto_activate_validated_batches": bool(
            payload.get("auto_activate_validated_batches", True)
        ),
        "max_relative_move": move_value,
        "contributor_priority": [
            str(item).strip()
            for item in (payload.get("contributor_priority") or [])
            if str(item).strip()
        ],
    }


def _validate_timezone_policy(payload: Mapping[str, Any]) -> dict[str, Any]:
    from core.timezone_vocabulary import resolve_timezone, tzdb_version  # noqa: PLC0415

    zone = resolve_timezone(payload.get("reporting_timezone"))
    if zone is None:
        raise RuleSetError(
            "reporting_timezone must resolve against the governed IANA vocabulary"
        )
    if not zone.selectable:
        raise RuleSetError(
            f"{zone.zone} is a compatibility identifier, not a reporting boundary"
        )

    derivation = str(payload.get("timestamp_derivation") or TIMESTAMP_DERIVE)
    if derivation not in {TIMESTAMP_DERIVE, TIMESTAMP_NEVER}:
        raise RuleSetError(
            f"timestamp_derivation must be {TIMESTAMP_DERIVE} or {TIMESTAMP_NEVER}"
        )
    gap_policy = str(payload.get("dst_gap_policy") or "refuse")
    if gap_policy not in DST_GAP_POLICIES:
        raise RuleSetError(f"dst_gap_policy must be one of {list(DST_GAP_POLICIES)}")
    overlap_policy = str(payload.get("dst_overlap_policy") or "first_occurrence")
    if overlap_policy not in DST_OVERLAP_POLICIES:
        raise RuleSetError(f"dst_overlap_policy must be one of {list(DST_OVERLAP_POLICIES)}")

    pinned = str(payload.get("tzdb_version") or "").strip() or tzdb_version()

    return {
        "reporting_timezone": zone.canonical_zone,
        "tzdb_version": pinned,
        # Not configurable: see the DATE_ONLY_SIGNAL comment.
        "date_only_policy": DATE_ONLY_SIGNAL,
        "timestamp_derivation": derivation,
        "dst_gap_policy": gap_policy,
        "dst_overlap_policy": overlap_policy,
        "assumptions": [dict(item) for item in (payload.get("assumptions") or [])],
        "reconciliation_refs": sorted(
            str(item) for item in (payload.get("reconciliation_refs") or [])
        ),
    }


# ---------------------------------------------------------------------------
# What a version of each family decides, in the words a person is asked it.
#
# These are the console's authoring door (`governance.md`, "A Rule Set version is
# drafted, then published"). The questions live HERE, beside the validators that
# refuse a wrong answer, so what is offered and what is judged cannot drift.
#
# The currency and the zone are asked as text rather than as a list of options:
# their vocabularies are governed tables of some hundreds of rows, and pasting
# one into a static declaration would be a fourth copy free to fall behind. The
# validators above resolve the answer against the governed vocabulary and refuse
# it by name, which is the check that matters.
# ---------------------------------------------------------------------------

_MONEY_FORM = (
    RuleSetFormField(
        key="reporting_currency",
        question="Which currency does this Project report in?",
        why=(
            "Every converted figure this Project states is stated in it. It must be legal "
            "tender with an ISO minor unit: a fund or metal code has no defined precision to "
            "round a converted amount to."
        ),
    ),
    RuleSetFormField(
        key="rounding",
        question="How is a converted amount rounded at the boundary?",
        kind="choice",
        options=(
            RuleSetFormOption(
                "half_even",
                "Half to even",
                "A half-way amount goes to the nearest even minor unit, so rounding does not "
                "drift upward across a long series.",
            ),
            RuleSetFormOption(
                "half_up",
                "Half away from zero",
                "A half-way amount always rounds away from zero. Familiar on an invoice, and "
                "it accumulates a small upward bias over many rows.",
            ),
        ),
    ),
    RuleSetFormField(
        key="max_staleness_days",
        question="How old may a rate be before a conversion is refused?",
        kind="integer",
        unit="days",
        why=(
            "Past this age the conversion is refused rather than computed with a rate nobody "
            "would defend. Zero means only the day's own rate converts."
        ),
    ),
    RuleSetFormField(
        key="rate_source_priority",
        question="Which rate sources apply, in order of precedence?",
        kind="text_list",
        why=(
            "The first source that carries the pair wins. Order is content here: two sources "
            "in the other order are a different policy and a different published version."
        ),
    ),
    RuleSetFormField(
        key="allow_triangulation",
        question="May a pair with no direct rate be converted through a pivot currency?",
        kind="boolean",
        required=False,
        why=(
            "Off, a missing pair is a refusal a person can see. On, it is converted in two "
            "steps and carries the rounding of both."
        ),
    ),
    RuleSetFormField(
        key="triangulation_pivot",
        question="Through which currency?",
        required=False,
        why="Only read when triangulation is allowed, and then it is required.",
    ),
    RuleSetFormField(
        key="allow_carry_forward",
        question="May yesterday's rate be used when today's has not arrived?",
        kind="boolean",
        required=False,
        why=(
            "Off, a missing day is a refusal. On, the last rate is carried forward until the "
            "staleness bound above is reached."
        ),
    ),
)

_FX_INGESTION_FORM = (
    RuleSetFormField(
        key="provider",
        question="Who publishes the rates this Project converts with?",
        why="The name of the publishing source, as it is known outside this product.",
    ),
    RuleSetFormField(
        key="provider_reference",
        question="Which of that provider's series, exactly?",
        required=False,
        why="Left blank when the provider publishes one series.",
    ),
    RuleSetFormField(
        key="cadence",
        question="How often does a batch arrive?",
        kind="choice",
        options=(
            RuleSetFormOption("daily", "Every day"),
            RuleSetFormOption("weekly", "Every week"),
            RuleSetFormOption(
                "manual",
                "Only when someone loads one",
                "No batch arrives on its own, so staleness is only ever cleared by a person.",
            ),
        ),
    ),
    RuleSetFormField(
        key="base_currency",
        question="Against which currency are the published rates quoted?",
        why="The provider's own base. It is not the reporting currency and need not match it.",
    ),
    RuleSetFormField(
        key="auto_activate_validated_batches",
        question="May a batch that passes validation take effect without a person?",
        kind="boolean",
        required=False,
        why=(
            "Off, someone activates each batch, and rates go stale — and conversions stop — "
            "on any day nobody does."
        ),
    ),
    RuleSetFormField(
        key="max_relative_move",
        question="Above which move against the previous batch is one held back?",
        kind="decimal",
        unit="fraction of the previous rate",
        required=False,
        why=(
            "0.1 holds back a batch moving more than 10 %. Left blank, no move is large "
            "enough to hold a batch back."
        ),
    ),
    RuleSetFormField(
        key="contributor_priority",
        question="Which contributors win, in order, when several quote one pair?",
        kind="text_list",
        required=False,
        why="Left empty when the provider publishes one quote per pair.",
    ),
)

_TIMEZONE_FORM = (
    RuleSetFormField(
        key="reporting_timezone",
        question="Which timezone decides what day a figure falls on?",
        why=(
            "An IANA zone, such as Europe/Paris. A compatibility alias is refused: it is an "
            "identifier kept for old data, not a reporting boundary."
        ),
    ),
    RuleSetFormField(
        key="tzdb_version",
        question="Which timezone database version is this pinned to?",
        required=False,
        why=(
            "Left blank, the version this deployment carries is pinned. Pinning it is what "
            "makes a past day's boundary reproducible after the database is updated."
        ),
    ),
    RuleSetFormField(
        key="timestamp_derivation",
        question="May a day be derived from a timestamp when a source sends one?",
        kind="choice",
        options=(
            RuleSetFormOption(
                TIMESTAMP_DERIVE,
                "Yes, when the timestamp is precise enough",
                "A source that sends a real instant is placed on the day this zone puts it on.",
            ),
            RuleSetFormOption(
                TIMESTAMP_NEVER,
                "No, take the day the source states",
                "The source's own day is kept, even where it disagrees with this zone.",
            ),
        ),
    ),
    RuleSetFormField(
        key="dst_gap_policy",
        question="What happens to a timestamp in an hour that does not exist?",
        kind="choice",
        options=(
            RuleSetFormOption(
                "refuse",
                "Refuse it",
                "The row is reported as unplaceable rather than moved to an hour nobody sent.",
            ),
            RuleSetFormOption(
                "shift_forward",
                "Move it forward out of the gap",
                "The row is kept, at an instant the source did not state.",
            ),
        ),
    ),
    RuleSetFormField(
        key="dst_overlap_policy",
        question="What happens to a timestamp in an hour that happens twice?",
        kind="choice",
        options=(
            RuleSetFormOption("first_occurrence", "Read it as the first pass"),
            RuleSetFormOption("second_occurrence", "Read it as the second pass"),
            RuleSetFormOption(
                "refuse", "Refuse it", "The ambiguity is reported rather than resolved by rule."
            ),
        ),
    ),
)


register_profile(
    RuleSetProfile(
        key=PROFILE_MONEY,
        family=FAMILY_MONEY,
        label="Money Policy",
        validate=_validate_money_policy,
        # The reporting currency is only meaningful against the exact vocabulary
        # snapshot it was chosen from: an unpinned choice silently changes meaning
        # when the snapshot is refreshed.
        required_reference_kinds=("currency_vocabulary_version",),
        form=_MONEY_FORM,
    )
)
register_profile(
    RuleSetProfile(
        key=PROFILE_FX_INGESTION,
        family=FAMILY_FX_INGESTION,
        label="FX Ingestion Policy",
        validate=_validate_fx_ingestion,
        form=_FX_INGESTION_FORM,
    )
)
register_profile(
    RuleSetProfile(
        key=PROFILE_TIMEZONE,
        family=FAMILY_TIMEZONE,
        label="Reporting Timezone Policy",
        validate=_validate_timezone_policy,
        required_reference_kinds=("timezone_vocabulary_version",),
        form=_TIMEZONE_FORM,
    )
)


# ---------------------------------------------------------------------------
# Resolved policies. Every one names the exact version it came from.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MoneyPolicy:
    rule_set_id: str
    version_id: str
    content_hash: str
    reporting_currency: str
    reporting_currency_minor_unit: int
    rounding: str
    rate_source_priority: tuple[str, ...]
    allow_triangulation: bool
    triangulation_pivot: str | None
    allow_carry_forward: bool
    max_staleness_days: int

    @property
    def rounding_mode(self) -> str:
        return ROUNDING_MODES[self.rounding]

    def quantum(self) -> Decimal:
        """The exact rounding step of the reporting currency (``0.01``, ``1``, ``0.001``)."""
        return Decimal(1).scaleb(-self.reporting_currency_minor_unit)

    def owner_reference(self) -> dict[str, Any]:
        return {
            "kind": "rule_set",
            "object_type": "rule-set",
            "object_id": self.rule_set_id,
            "version_id": self.version_id,
            "evidence_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class TimezonePolicy:
    rule_set_id: str
    version_id: str
    content_hash: str
    reporting_timezone: str
    tzdb_version: str
    timestamp_derivation: str
    dst_gap_policy: str
    dst_overlap_policy: str
    assumptions: tuple[dict[str, Any], ...]

    @property
    def derives_reporting_date(self) -> bool:
        return self.timestamp_derivation == TIMESTAMP_DERIVE

    def owner_reference(self) -> dict[str, Any]:
        return {
            "kind": "rule_set",
            "object_type": "rule-set",
            "object_id": self.rule_set_id,
            "version_id": self.version_id,
            "evidence_hash": self.content_hash,
        }


@dataclass(frozen=True, slots=True)
class FxIngestionPolicy:
    rule_set_id: str
    version_id: str
    content_hash: str
    provider: str
    provider_reference: str | None
    cadence: str
    base_currency: str
    auto_activate_validated_batches: bool
    max_relative_move: float | None
    contributor_priority: tuple[str, ...]


def resolve_money_policy(conn, *, project_id: str) -> MoneyPolicy:
    """The Project's confirmed Money Policy, or a typed gap. Never a default."""

    found = active_version(
        conn, project_id=project_id, family=FAMILY_MONEY, name=POLICY_NAME
    )
    if found is None:
        raise PolicyGap(
            "money_policy_unconfirmed",
            "This Project has no confirmed Money Policy, so no reporting currency, "
            "rounding or rate rule is in force.",
            capability="currency_fx",
        )
    head, version = found
    payload = version["payload"]
    return MoneyPolicy(
        rule_set_id=str(head["id"]),
        version_id=str(version["id"]),
        content_hash=str(version["content_hash"]),
        reporting_currency=str(payload["reporting_currency"]),
        reporting_currency_minor_unit=int(payload["reporting_currency_minor_unit"]),
        rounding=str(payload["rounding"]),
        rate_source_priority=tuple(payload["rate_source_priority"]),
        allow_triangulation=bool(payload["allow_triangulation"]),
        triangulation_pivot=payload.get("triangulation_pivot"),
        allow_carry_forward=bool(payload["allow_carry_forward"]),
        max_staleness_days=int(payload["max_staleness_days"]),
    )


def resolve_timezone_policy(conn, *, project_id: str) -> TimezonePolicy:
    """The Project's confirmed Reporting Timezone Policy, or a typed gap."""

    found = active_version(
        conn, project_id=project_id, family=FAMILY_TIMEZONE, name=POLICY_NAME
    )
    if found is None:
        raise PolicyGap(
            "timezone_policy_unconfirmed",
            "This Project has no confirmed Reporting Timezone Policy, so no day "
            "boundary is in force and cross-source days cannot be declared equivalent.",
            capability="reporting_timezone",
        )
    head, version = found
    payload = version["payload"]
    return TimezonePolicy(
        rule_set_id=str(head["id"]),
        version_id=str(version["id"]),
        content_hash=str(version["content_hash"]),
        reporting_timezone=str(payload["reporting_timezone"]),
        tzdb_version=str(payload["tzdb_version"]),
        timestamp_derivation=str(payload["timestamp_derivation"]),
        dst_gap_policy=str(payload["dst_gap_policy"]),
        dst_overlap_policy=str(payload["dst_overlap_policy"]),
        assumptions=tuple(payload.get("assumptions") or ()),
    )


def resolve_fx_ingestion_policy(conn, *, project_id: str) -> FxIngestionPolicy:
    """The Project's confirmed FX ingestion policy, or a typed gap."""

    found = active_version(
        conn, project_id=project_id, family=FAMILY_FX_INGESTION, name=POLICY_NAME
    )
    if found is None:
        raise PolicyGap(
            "fx_ingestion_unconfirmed",
            "This Project has no confirmed FX ingestion policy, so no rate batch may "
            "be activated and no conversion has governed rate evidence.",
            capability="currency_fx",
        )
    head, version = found
    payload = version["payload"]
    return FxIngestionPolicy(
        rule_set_id=str(head["id"]),
        version_id=str(version["id"]),
        content_hash=str(version["content_hash"]),
        provider=str(payload["provider"]),
        provider_reference=payload.get("provider_reference"),
        cadence=str(payload["cadence"]),
        base_currency=str(payload["base_currency"]),
        auto_activate_validated_batches=bool(payload["auto_activate_validated_batches"]),
        max_relative_move=payload.get("max_relative_move"),
        contributor_priority=tuple(payload.get("contributor_priority") or ()),
    )


def try_resolve_money_policy(conn, *, project_id: str) -> MoneyPolicy | None:
    """:func:`resolve_money_policy`, returning ``None`` instead of raising.

    For read surfaces that must render "not confirmed yet" as a state rather than
    an error. Callers that COMPUTE a monetary value must use the raising form:
    ``None`` there would become an implicit currency at the next line.
    """
    try:
        return resolve_money_policy(conn, project_id=project_id)
    except PolicyGap:
        return None


def try_resolve_timezone_policy(conn, *, project_id: str) -> TimezonePolicy | None:
    """:func:`resolve_timezone_policy`, returning ``None`` instead of raising."""
    try:
        return resolve_timezone_policy(conn, project_id=project_id)
    except PolicyGap:
        return None
