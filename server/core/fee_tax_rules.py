"""Fee and tax alignment: activation, rule model, validation, governed store (Epic 41, 41.1).

Governance lives in Postgres; the cascade math lives in additive dbt views (amendment B2).
This module owns the CONTROL PLANE half: is the module on for this project, and what rules
has the project declared. It computes no ladder and reads no fact.

Layering (mirrors file_source_template.py / mediaplan_store.py): a PURE validation layer
(no I/O, offline-testable) + a DB layer (audited writes through
``operations.execute_operation``, pg-gated). The API layer (41.6) maps the typed errors to
4xx. This module never commits: the caller owns the transaction.

E41-AD9 distinction, load-bearing: a rule whose condition is KNOWN FALSE contributes
+0 micros at cascade time (41.3); an attribute that is UNRESOLVABLE is a typed gap. This
module only guarantees a rule is WELL FORMED -- it never asserts a condition will match.

Exactness (C.6 / E41-NFR02): money is exact integer micros and rates are exact ``Decimal``.
A ``float`` is REFUSED, never rounded. Nothing here divides.

No country, no rate, no client policy ships in this module (E41-NFR04 / C.3). Country tax
defaults are the dbt seed ``country_tax_defaults.csv`` and that seed is Story 41.2's.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ulid import ULID

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed vocabularies -- a ONE-FOR-ONE mirror of the CHECK constraints in
# infra/nango/migrations/119_fee_tax_alignment.sql. A story-local test parses the
# SQL and asserts the two agree, so a future vocabulary change cannot drift.
# ---------------------------------------------------------------------------

SCOPE_KINDS = frozenset({"project", "plan_version", "datastream"})
CATEGORIES = frozenset(
    {
        "PLATFORM_FEE",
        "VERIFICATION",
        "REGULATORY_TAX",
        "WHT_GROSS_UP",
        "AGENCY_FEE",
        "SALES_TAX",
        "PAYMENT_FEE",
    }
)
FORMS = frozenset({"PERCENTAGE", "CPM", "FLAT", "SPEND_TIERS", "GROSS_UP", "PER_TRANSACTION"})
BASE_TARGETS = frozenset(
    {
        "NET_MEDIA",
        "RUNNING_SUBTOTAL",
        "MEASURED_IMPRESSIONS",
        "NET_REVENUE",
        "GROSS_REVENUE",
    }
)
STATUSES = frozenset({"proposed", "confirmed", "disabled"})
ORIGINS = frozenset({"auto_country", "operator", "media_plan", "llm"})
SOURCE_TYPES = frozenset(
    {
        "PAID_MEDIA",
        "LEAD_GEN_MEDIA",
        "DIRECT_SERVICE_COST",
        "COMMERCE_REVENUE",
        "ORGANIC_ANALYTICS",
        "UNKNOWN",
    }
)
# `market` sits beside `country` because a market is a NAMED GROUP of countries
# (France = FR + MC + DOM-TOM). A multi-country market resolves a market_id but NOT a
# single country, so a `market` condition MATCHES there while a `country` condition
# stays UNRESOLVED. A rule that can only say "country" cannot express the model Jean's
# geography epic is actually built on.
# The JSONB stays OPEN, but an unrecognised key is UNRESOLVED, never ignored: it is
# refused at declaration here, and 41.3 must treat any key it cannot evaluate as a
# typed gap rather than "no match, move on".
CONDITION_KEYS = frozenset({"country", "market", "placement_type", "connector", "tax_code"})
TIER_MODES = frozenset({"cliff", "marginal"})
TIER_MODE_DEFAULT = "cliff"
STATUS_DEFAULT = "proposed"
CASCADE_PHASES = range(1, 7)

# ---------------------------------------------------------------------------
# ROUTED-PAIR COHERENCE (Story 41.5 + adversarial-review finding F2, 2026-07-27).
# A ONE-FOR-ONE mirror of migration 119's ck_fee_tax_rules_category_form and
# ck_fee_tax_rules_form_base_target. This is the API/MCP half: a governed-LLM or REST
# caller gets a NAMED, actionable 422 instead of a raw constraint violation, and the DB
# CHECK is the backstop.
#
# WHY A PAIR TABLE AT ALL. Before this, nothing validated `category x form` or
# `form x base_target`, and the cascade's phase CTEs in
# dbt/models/marts/fee_tax_ladder_daily.sql route a FIXED set of forms per category.
# So (AGENCY_FEE, GROSS_UP) -- which passes every other constraint -- reached the
# cascade, fell through to a silent `ELSE 0`, stayed in `applied_rule_ids`, and left
# the row reporting `is_ladder_complete = TRUE`: an invoice understated by the whole
# withholding component behind a green flag. Same for a CPM rule over NET_MEDIA, which
# no cost phase routes at all. Story 41.8 hands rule creation to a governed LLM, so
# this is reachable without anyone hand-writing SQL.
#
# THE SET IS DERIVED FROM THE ENGINE, NOT FROM JUDGEMENT. Read out of
# fee_tax_ladder_daily.sql: `rules_ladder` admits PLATFORM_FEE / REGULATORY_TAX /
# WHT_GROSS_UP / AGENCY_FEE / SALES_TAX over NET_MEDIA / RUNNING_SUBTOTAL, and its
# `form_unroutable` CTE enumerates routability POSITIVELY -- WHT_GROSS_UP admits
# GROSS_UP as well as PERCENTAGE / FLAT / SPEND_TIERS, every other category admits
# only PERCENTAGE / FLAT / SPEND_TIERS. VERIFICATION -> CPM is Story 41.4's overlay
# (E41-FR04); PAYMENT_FEE -> PERCENTAGE / FLAT / PER_TRANSACTION is Story 41.5's.
#
# BEFORE WIDENING EITHER TABLE: add the routing to the evaluator FIRST (the phase CTEs
# of fee_tax_ladder_daily.sql, or whichever overlay owns the new pair), then widen
# migration 119's CHECK and this table in the SAME wave. Widening one of these alone
# does not enable a feature -- it re-opens the silent-zero hole.
#
# DATA, NEVER AN if/elif CHAIN: the ladder itself is data (arbitration B2), and so is
# its coherence table. A chain of branches is where the next form gets forgotten.
# ---------------------------------------------------------------------------

_CATEGORY_FORMS: dict[str, frozenset[str]] = {
    "PLATFORM_FEE": frozenset({"PERCENTAGE", "FLAT", "SPEND_TIERS"}),
    "REGULATORY_TAX": frozenset({"PERCENTAGE", "FLAT", "SPEND_TIERS"}),
    "WHT_GROSS_UP": frozenset({"GROSS_UP", "PERCENTAGE", "FLAT", "SPEND_TIERS"}),
    "AGENCY_FEE": frozenset({"PERCENTAGE", "FLAT", "SPEND_TIERS"}),
    "SALES_TAX": frozenset({"PERCENTAGE", "FLAT", "SPEND_TIERS"}),
    # Story 41.4's KEEP_SEPARATE overlay: the verification fee arrives in CPM over
    # measured impressions and in no other shape (E41-FR04).
    "VERIFICATION": frozenset({"CPM"}),
    # Story 41.5's revenue overlay. PER_TRANSACTION appears HERE AND NOWHERE ELSE,
    # which IS the per-transaction scope guard -- expressed once inside the coherent
    # set instead of as a separate one-off constraint.
    "PAYMENT_FEE": frozenset({"PERCENTAGE", "FLAT", "PER_TRANSACTION"}),
}

_FORM_BASE_TARGETS: dict[str, frozenset[str]] = {
    # A percentage of an impression COUNT is not money, so PERCENTAGE / FLAT may not
    # target MEASURED_IMPRESSIONS: no evaluator computes that.
    "PERCENTAGE": frozenset({"NET_MEDIA", "RUNNING_SUBTOTAL", "NET_REVENUE", "GROSS_REVENUE"}),
    "FLAT": frozenset({"NET_MEDIA", "RUNNING_SUBTOTAL", "NET_REVENUE", "GROSS_REVENUE"}),
    # Nothing else forced CPM -> MEASURED_IMPRESSIONS before this table, and a CPM rule
    # over NET_MEDIA is routed by no phase at all: the verification cost silently
    # vanishes (F2's second instance).
    "CPM": frozenset({"MEASURED_IMPRESSIONS"}),
    # The cumulative tier base IS net media (`tier_rules` reads net_media_micros), so a
    # tiered rule on a revenue base has no evaluator anywhere.
    "SPEND_TIERS": frozenset({"NET_MEDIA", "RUNNING_SUBTOTAL"}),
    # Phase 4's `phase_base` resolves to net media or the running subtotal, and to
    # nothing else.
    "GROSS_UP": frozenset({"NET_MEDIA", "RUNNING_SUBTOTAL"}),
    # Its base is the transaction count of a revenue row; there is no cost-side
    # evaluator for it.
    "PER_TRANSACTION": frozenset({"GROSS_REVENUE"}),
}

# The half-sentence that makes the refusal actionable rather than bureaucratic. Kept
# beside the tables so a new pair cannot be added without a reason to quote.
_UNROUTED_PAIR_REASON = (
    "no evaluator routes that pair, so the rule would contribute 0 micros while the "
    "composed total reported itself complete"
)

ACTION_ALIGNMENT_SET = "fee_tax.alignment.set"
ACTION_RULE_CREATED = "fee_tax.rule.created"
ACTION_RULE_UPDATED = "fee_tax.rule.updated"
ACTION_RULE_STATUS_SET = "fee_tax.rule.status_set"

_POLICY_VERSION = "fee-tax-rule-v1"

# Rates are exact to six decimals (the NUMERIC(12,6) column). Built from an integer so
# no decimal literal -- which a reader could mistake for a shipped tax rate -- appears
# anywhere in this module (E41-NFR04).
_RATE_SCALE = 6
_RATE_QUANTUM = Decimal(1).scaleb(-_RATE_SCALE)

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# The status machine (C.2, human-in-the-loop). Only `confirmed` ever enters the cascade.
# A `disabled` rule must go back through `proposed` -- i.e. through human review -- before
# it may re-enter the cascade; `disabled -> confirmed` is therefore refused (409).
_STATUS_TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"confirmed", "disabled"}),
    "confirmed": frozenset({"proposed", "disabled"}),
    "disabled": frozenset({"proposed"}),
}

# The validatable field set. validate_rule() accepts exactly these keys and returns
# exactly these keys, so validate_rule(validate_rule(x)) == validate_rule(x).
_RULE_FIELDS = (
    "scope_kind",
    "scope_ref",
    "category",
    "form",
    "rate",
    "amount_micros",
    "cpm_micros",
    "tiers",
    "currency",
    "base_target",
    "cascade_phase",
    "sequence_order",
    "conditions",
    "source_type_scope",
    "effective_from",
    "effective_to",
    "status",
    "origin",
    "dedup_hash",
    "label",
)

# Columns of app.fee_tax_rules, in the order _row_to_rule() expects.
_RULE_COLS = (
    "id",
    "project_id",
    "scope_kind",
    "scope_ref",
    "category",
    "form",
    "rate",
    "amount_micros",
    "cpm_micros",
    "tiers",
    "currency",
    "base_target",
    "cascade_phase",
    "sequence_order",
    "conditions",
    "source_type_scope",
    "effective_from",
    "effective_to",
    "status",
    "origin",
    "dedup_hash",
    "label",
    "created_by",
    "created_at",
    "updated_at",
)

_IDENTITY_FIELDS = frozenset({"id", "project_id", "created_by", "created_at", "updated_at"})

# update_rule refuses these. The identity fields are immutable; `status` is refused
# because the status machine must have exactly ONE door (review finding F3). Allowing
# it in a patch would let update_rule perform the one transition set_rule_status
# refuses -- disabled -> confirmed -- and audit it as `fee_tax.rule.updated`, so an
# audit query on `fee_tax.rule.status_set` would never see the re-confirmation and
# the rule would silently re-enter the cascade.
_PATCH_FORBIDDEN = _IDENTITY_FIELDS | {"status"}

# SQL type bounds, enforced in Python so an out-of-range value is a typed 422 rather
# than a raw psycopg DataError surfacing as a 500.
_MAX_BIGINT = 2**63 - 1              # amount_micros / cpm_micros / threshold_micros
_MIN_BIGINT = -(2**63)
_MAX_INT32 = 2**31 - 1               # sequence_order
_RATE_INTEGER_DIGITS = 6             # NUMERIC(12,6) => 12 total digits, 6 after the point


# ---------------------------------------------------------------------------
# Typed errors (stable code -> HTTP status at the 41.6 API layer).
# ---------------------------------------------------------------------------


class FeeTaxRuleError(ValueError):
    """Base for fee/tax rule errors (carries a stable code)."""

    code = "fee_tax_rule_error"


class FeeTaxRuleValidationError(FeeTaxRuleError):
    """A caller-supplied value is invalid (maps to 422)."""

    code = "invalid_param"


class FeeTaxRuleNotFoundError(FeeTaxRuleError):
    """A referenced rule or project does not exist (maps to 404)."""

    code = "not_found"


class FeeTaxRuleStateError(FeeTaxRuleError):
    """The operation is illegal in the current state (maps to 409)."""

    code = "invalid_state"


# ---------------------------------------------------------------------------
# Pure scalar coercion helpers (no I/O, no clock).
# ---------------------------------------------------------------------------


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise FeeTaxRuleValidationError(f"{field} must be a non-empty string")
    return value


def _coerce_int(value: Any, field: str) -> int:
    """Accept a real int only. A bool, a float, a numeric str or a Decimal is refused.

    C.6: the engine normalises at its boundary and NEVER re-divides, so a fractional
    micro amount is a declaration error, not something to round.

    Also bounds-checked against BIGINT: without this an oversized amount reaches
    Postgres and comes back as a raw psycopg DataError, i.e. a 500 where the caller
    deserves a 422.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise FeeTaxRuleValidationError(f"{field} must be an integer number of micros")
    if not _MIN_BIGINT <= value <= _MAX_BIGINT:
        raise FeeTaxRuleValidationError(f"{field} is out of range for a 64-bit integer")
    return value


def _coerce_rate(value: Any, field: str) -> Decimal:
    """Accept str / int / Decimal and return an exact Decimal at six decimals.

    A ``float`` is refused: 0.03 is not exactly three percent in binary and the
    difference would silently enter an invoice total (E41-NFR02).
    """
    if isinstance(value, bool) or isinstance(value, float):
        raise FeeTaxRuleValidationError(
            f"{field} must be an exact decimal (str, int or Decimal), not a float"
        )
    if not isinstance(value, (str, int, Decimal)):
        raise FeeTaxRuleValidationError(f"{field} must be an exact decimal")
    try:
        parsed = Decimal(value) if not isinstance(value, Decimal) else value
        quantised = parsed.quantize(_RATE_QUANTUM)
    except (InvalidOperation, ArithmeticError, ValueError) as exc:
        raise FeeTaxRuleValidationError(f"{field} is not a valid decimal") from exc
    if quantised != parsed:
        raise FeeTaxRuleValidationError(
            f"{field} carries more than {_RATE_SCALE} decimals and would be rounded"
        )
    if quantised < 0:
        raise FeeTaxRuleValidationError(f"{field} must be zero or positive")
    # NUMERIC(12,6) holds at most six digits before the point. Catching it here keeps
    # an over-large rate a typed 422 instead of a raw psycopg DataError (a 500).
    if quantised >= Decimal(10) ** _RATE_INTEGER_DIGITS:
        raise FeeTaxRuleValidationError(
            f"{field} is too large: at most {_RATE_INTEGER_DIGITS} digits before the point"
        )
    return quantised


def _coerce_date(value: Any, field: str) -> date:
    """Accept a ``date`` or an ISO ``str``. A ``datetime`` is refused: the platform
    invariant is a DATE everywhere, never an hour."""
    if isinstance(value, datetime):
        raise FeeTaxRuleValidationError(f"{field} must be a date, not a timestamp")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise FeeTaxRuleValidationError(f"{field} must be an ISO date") from exc
    raise FeeTaxRuleValidationError(f"{field} must be an ISO date")


def _validate_conditions(value: Any) -> dict[str, list[str]]:
    """Row-level match declaration. ``{}`` is valid and means "matches every row"."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise FeeTaxRuleValidationError("conditions must be an object")
    unknown = sorted(set(value) - CONDITION_KEYS)
    if unknown:
        raise FeeTaxRuleValidationError(
            "conditions contains an unrecognised key (an unknown key is UNRESOLVED, "
            "never ignored): " + ", ".join(unknown)
        )
    out: dict[str, list[str]] = {}
    for key in value:
        values = value[key]
        if isinstance(values, str) or not isinstance(values, (list, tuple)):
            raise FeeTaxRuleValidationError(f"conditions.{key} must be a list of strings")
        if not values:
            raise FeeTaxRuleValidationError(f"conditions.{key} must not be empty")
        out[key] = [_require_str(item, f"conditions.{key}[]") for item in values]
    return out


def _validate_tiers(value: Any) -> dict[str, Any]:
    """Validate the SPEND_TIERS payload.

    Shape policy (ruled, single shape): ``tiers`` is an OBJECT
    ``{"mode": "cliff"|"marginal", "bands": [{threshold_micros, rate}, ...]}``.
    ``mode`` defaults to ``"cliff"`` when absent. A bare ARRAY of bands is REJECTED --
    no row exists yet, so there is no legacy shape to support, and two shapes would
    have to be carried by Postgres, the flattening view and the dbt engine alike.

    The evaluation mode is RULE DATA, not a platform choice: 41.3 implements both
    ``cliff`` and ``marginal``; 41.1 must not pick one.
    """
    if not isinstance(value, dict):
        raise FeeTaxRuleValidationError(
            "tiers must be an object {mode, bands}; a bare array of bands is refused"
        )
    unknown = sorted(set(value) - {"mode", "bands"})
    if unknown:
        raise FeeTaxRuleValidationError("tiers contains unsupported keys: " + ", ".join(unknown))
    mode = value.get("mode", TIER_MODE_DEFAULT)
    if mode is None:
        mode = TIER_MODE_DEFAULT
    if mode not in TIER_MODES:
        raise FeeTaxRuleValidationError(
            "tiers.mode must be one of: " + ", ".join(sorted(TIER_MODES))
        )
    bands = value.get("bands")
    if not isinstance(bands, (list, tuple)) or not bands:
        raise FeeTaxRuleValidationError("tiers.bands must be a non-empty array")

    out_bands: list[dict[str, Any]] = []
    previous: int | None = None
    for index, band in enumerate(bands):
        if not isinstance(band, dict):
            raise FeeTaxRuleValidationError(f"tiers.bands[{index}] must be an object")
        extra = sorted(set(band) - {"threshold_micros", "rate"})
        if extra:
            raise FeeTaxRuleValidationError(
                f"tiers.bands[{index}] contains unsupported keys: " + ", ".join(extra)
            )
        if "threshold_micros" not in band:
            raise FeeTaxRuleValidationError(f"tiers.bands[{index}].threshold_micros is required")
        if "rate" not in band:
            raise FeeTaxRuleValidationError(f"tiers.bands[{index}].rate is required")
        threshold = _coerce_int(band["threshold_micros"], f"tiers.bands[{index}].threshold_micros")
        if threshold < 0:
            raise FeeTaxRuleValidationError(
                f"tiers.bands[{index}].threshold_micros must be zero or positive"
            )
        if previous is not None and threshold <= previous:
            raise FeeTaxRuleValidationError(
                "tiers.bands thresholds must be strictly increasing "
                f"(band {index} is not greater than band {index - 1})"
            )
        previous = threshold
        out_bands.append(
            {
                "threshold_micros": threshold,
                "rate": _coerce_rate(band["rate"], f"tiers.bands[{index}].rate"),
            }
        )
    return {"mode": mode, "bands": out_bands}


def _validate_source_type_scope(value: Any) -> list[str]:
    """``[]`` means "every source type" (E41-AD8)."""
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise FeeTaxRuleValidationError("source_type_scope must be a list")
    out: list[str] = []
    for item in value:
        if item not in SOURCE_TYPES:
            raise FeeTaxRuleValidationError(
                "source_type_scope contains an unknown source type: " + str(item)
            )
        out.append(item)
    return out


# ---------------------------------------------------------------------------
# The pure validator.
# ---------------------------------------------------------------------------


def validate_rule(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalise and validate ONE declared rule. Pure: no DB, no clock, no network.

    Returns a canonical dict carrying exactly ``_RULE_FIELDS``. Idempotent:
    ``validate_rule(validate_rule(p)) == validate_rule(p)``.

    Raises ``FeeTaxRuleValidationError`` (-> 422) on any incoherent payload. It never
    invents a ``dedup_hash``: Story 41.2 computes that for ``origin='auto_country'``.

    ``dedup_hash`` is deliberately NOT named ``dedup_key``. It is a content hash of
    the facts that make an auto-populated rule unique, and ``operations._is_secret_key``
    treats any identifier ending in ``_key`` as secret-like unless it ends in
    ``_id`` / ``_ref`` / ``_hash`` -- so under the old name the field could never
    appear in an audited ``request_payload``, and every auto-populated rule failed at
    write time. Do not rename it back.
    """
    if not isinstance(payload, dict):
        raise FeeTaxRuleValidationError("a rule must be an object")
    unknown = sorted(set(payload) - set(_RULE_FIELDS))
    if unknown:
        raise FeeTaxRuleValidationError("rule contains unsupported keys: " + ", ".join(unknown))

    scope_kind = payload.get("scope_kind")
    if scope_kind not in SCOPE_KINDS:
        raise FeeTaxRuleValidationError(
            "scope_kind must be one of: " + ", ".join(sorted(SCOPE_KINDS))
        )
    scope_ref = payload.get("scope_ref")
    if scope_kind == "project":
        if scope_ref is not None:
            raise FeeTaxRuleValidationError("scope_ref must be NULL when scope_kind is 'project'")
    else:
        scope_ref = _require_str(scope_ref, "scope_ref")

    category = payload.get("category")
    if category not in CATEGORIES:
        raise FeeTaxRuleValidationError("category must be one of: " + ", ".join(sorted(CATEGORIES)))

    form = payload.get("form")
    if form not in FORMS:
        raise FeeTaxRuleValidationError("form must be one of: " + ", ".join(sorted(FORMS)))

    base_target = payload.get("base_target")
    if base_target not in BASE_TARGETS:
        raise FeeTaxRuleValidationError(
            "base_target must be one of: " + ", ".join(sorted(BASE_TARGETS))
        )

    # F2 / Story 41.5: the pair must be one an evaluator ACTUALLY implements. Checked
    # here, after the three vocabularies are known good, so the message can name the
    # offending pair rather than a field.
    _require_routed_pair(category, form, base_target)

    cascade_phase = payload.get("cascade_phase")
    if isinstance(cascade_phase, bool) or not isinstance(cascade_phase, int):
        raise FeeTaxRuleValidationError("cascade_phase must be an integer")
    if cascade_phase not in CASCADE_PHASES:
        raise FeeTaxRuleValidationError("cascade_phase must be between 1 and 6")

    sequence_order = payload.get("sequence_order", 0)
    if sequence_order is None:
        sequence_order = 0
    if isinstance(sequence_order, bool) or not isinstance(sequence_order, int):
        raise FeeTaxRuleValidationError("sequence_order must be an integer")
    if sequence_order < 0:
        raise FeeTaxRuleValidationError("sequence_order must be zero or positive")
    if sequence_order > _MAX_INT32:
        raise FeeTaxRuleValidationError("sequence_order is out of range for a 32-bit integer")

    origin = payload.get("origin")
    if origin not in ORIGINS:
        raise FeeTaxRuleValidationError("origin must be one of: " + ", ".join(sorted(ORIGINS)))

    status = payload.get("status") or STATUS_DEFAULT
    if status not in STATUSES:
        raise FeeTaxRuleValidationError("status must be one of: " + ", ".join(sorted(STATUSES)))

    rate = payload.get("rate")
    amount_micros = payload.get("amount_micros")
    cpm_micros = payload.get("cpm_micros")
    tiers = payload.get("tiers")
    currency = payload.get("currency")
    if currency is not None:
        currency = _require_str(currency, "currency")
        if not _CURRENCY_RE.match(currency):
            raise FeeTaxRuleValidationError("currency must be a 3-letter uppercase code")

    rate, amount_micros, cpm_micros, tiers, currency = _validate_form_payload(
        form,
        rate=rate,
        amount_micros=amount_micros,
        cpm_micros=cpm_micros,
        tiers=tiers,
        currency=currency,
    )

    effective_from = _coerce_date(payload.get("effective_from"), "effective_from")
    effective_to = payload.get("effective_to")
    if effective_to is not None:
        effective_to = _coerce_date(effective_to, "effective_to")
        if effective_to < effective_from:
            raise FeeTaxRuleValidationError("effective_to must not precede effective_from")

    dedup_hash = payload.get("dedup_hash")
    if dedup_hash is not None:
        dedup_hash = _require_str(dedup_hash, "dedup_hash")

    label = payload.get("label") or ""
    if not isinstance(label, str):
        raise FeeTaxRuleValidationError("label must be a string")

    return {
        "scope_kind": scope_kind,
        "scope_ref": scope_ref,
        "category": category,
        "form": form,
        "rate": rate,
        "amount_micros": amount_micros,
        "cpm_micros": cpm_micros,
        "tiers": tiers,
        "currency": currency,
        "base_target": base_target,
        "cascade_phase": cascade_phase,
        "sequence_order": sequence_order,
        "conditions": _validate_conditions(payload.get("conditions")),
        "source_type_scope": _validate_source_type_scope(payload.get("source_type_scope")),
        "effective_from": effective_from,
        "effective_to": effective_to,
        "status": status,
        "origin": origin,
        "dedup_hash": dedup_hash,
        "label": label,
    }


def _require_routed_pair(category: str, form: str, base_target: str) -> None:
    """Refuse a ``(category, form)`` or ``(form, base_target)`` pair no evaluator routes.

    The API/MCP mirror of migration 119's ``ck_fee_tax_rules_category_form`` and
    ``ck_fee_tax_rules_form_base_target``. Raises ``FeeTaxRuleValidationError``, which
    the fee/tax surface maps to **422** exactly as it does for every other incoherent
    payload -- no new error type, because a caller does not need a second taxonomy to
    learn: an unroutable pair is a malformed declaration, not a state conflict.

    The message names the pair AND the reason AND where the value IS legal, because the
    caller may be a governed LLM (Story 41.8) whose only feedback channel is this string.
    """
    allowed_forms = _CATEGORY_FORMS.get(category, frozenset())
    if form not in allowed_forms:
        elsewhere = sorted(
            other for other, forms in _CATEGORY_FORMS.items() if form in forms
        )
        hint = (
            f" form={form} is only valid with category=" + " | ".join(elsewhere) + "."
            if elsewhere
            else f" No category routes form={form}."
        )
        raise FeeTaxRuleValidationError(
            f"category={category} does not support form={form}: {_UNROUTED_PAIR_REASON}."
            f" category={category} supports: " + ", ".join(sorted(allowed_forms)) + "." + hint
        )

    allowed_targets = _FORM_BASE_TARGETS.get(form, frozenset())
    if base_target not in allowed_targets:
        raise FeeTaxRuleValidationError(
            f"form={form} does not support base_target={base_target}:"
            f" {_UNROUTED_PAIR_REASON}."
            f" form={form} supports: " + ", ".join(sorted(allowed_targets)) + "."
        )


def _validate_form_payload(
    form: str,
    *,
    rate: Any,
    amount_micros: Any,
    cpm_micros: Any,
    tiers: Any,
    currency: Any,
) -> tuple[Decimal | None, int | None, int | None, dict[str, Any] | None, str | None]:
    """Enforce form <-> payload coherence (C.2): a stored rule must be executable.

    Currency is REQUIRED for FLAT, PER_TRANSACTION and CPM (they carry an absolute
    amount), OPTIONAL for SPEND_TIERS (its thresholds are absolute amounts too, so a
    declared currency is meaningful), and REFUSED for PERCENTAGE and GROSS_UP where it
    would be meaningless and would confuse 41.3's cross-currency refusal.

    PER_TRANSACTION (Story 41.5) shares FLAT's PAYLOAD shape and nothing else: the
    amount is charged once PER TRANSACTION rather than once for the row, and 41.5
    multiplies it by a real transaction count or emits a typed gap. It must never fall
    into the ("PERCENTAGE", "GROSS_UP") currency-refusal branch -- an absolute amount
    with no currency cannot be checked against the row's currency, and 41.5 REFUSES a
    foreign-currency amount rather than converting it (Epic 39.10).
    """
    if form in ("PERCENTAGE", "GROSS_UP"):
        if rate is None:
            raise FeeTaxRuleValidationError(f"{form} requires a rate")
        rate = _coerce_rate(rate, "rate")
        if form == "GROSS_UP" and rate >= 1:
            raise FeeTaxRuleValidationError(
                "GROSS_UP requires 0 <= rate < 1 (net/(1-rate) would divide by zero)"
            )
        _refuse(amount_micros, "amount_micros", form)
        _refuse(cpm_micros, "cpm_micros", form)
        _refuse(tiers, "tiers", form)
        _refuse(currency, "currency", form)
        return rate, None, None, None, None

    if form in ("FLAT", "PER_TRANSACTION"):
        if amount_micros is None:
            raise FeeTaxRuleValidationError(f"{form} requires amount_micros")
        if currency is None:
            raise FeeTaxRuleValidationError(f"{form} requires a currency")
        _refuse(rate, "rate", form)
        _refuse(cpm_micros, "cpm_micros", form)
        _refuse(tiers, "tiers", form)
        return None, _coerce_int(amount_micros, "amount_micros"), None, None, currency

    if form == "CPM":
        if cpm_micros is None:
            raise FeeTaxRuleValidationError("CPM requires cpm_micros")
        if currency is None:
            raise FeeTaxRuleValidationError("CPM requires a currency")
        _refuse(rate, "rate", form)
        _refuse(amount_micros, "amount_micros", form)
        _refuse(tiers, "tiers", form)
        return None, None, _coerce_int(cpm_micros, "cpm_micros"), None, currency

    # SPEND_TIERS
    if tiers is None:
        raise FeeTaxRuleValidationError("SPEND_TIERS requires tiers")
    _refuse(rate, "rate", form)
    _refuse(amount_micros, "amount_micros", form)
    _refuse(cpm_micros, "cpm_micros", form)
    return None, None, None, _validate_tiers(tiers), currency


def _refuse(value: Any, field: str, form: str) -> None:
    if value is not None:
        raise FeeTaxRuleValidationError(f"{field} is not allowed on a {form} rule")


# ---------------------------------------------------------------------------
# JSON boundary helpers.
#
# operations._validate_json accepts only str|int|float|bool|None|dict|list, and
# _canonical_hash json.dumps() the same values. A Decimal rate or a date effective_from
# in request_payload / MutationResult.result / outbox_payload raises
# OperationValidationError at write time. Stringify at the boundary -- and only there:
# psycopg adapts Decimal and date natively as SQL parameters, so the stored values stay
# exact.
# ---------------------------------------------------------------------------


def _jsonable(value: Any) -> Any:
    """Recursively turn Decimal -> str and date -> ISO str for the audit boundary."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def content_hash(project_id: str, normalised: dict[str, Any]) -> str:
    """sha256 of the canonical normalised rule JSON, scoped to the project."""
    document = {"project_id": project_id, "rule": _jsonable(normalised)}
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _mint_rule_id() -> str:
    return f"ftr_{ULID()}"


def _mint_idempotency_suffix() -> str:
    """A per-call nonce for writes whose repetition is a NEW act, not a replay.

    A stable content key would be wrong for a toggle or a status change: turning a
    module on, off, then on again would replay the first operation and silently skip
    the third write. Callers that DO want replay semantics pass their own key.
    """
    return str(ULID())


# ---------------------------------------------------------------------------
# Row reading.
# ---------------------------------------------------------------------------


def _tiers_from_db(value: Any) -> dict[str, Any] | None:
    """Rebuild the exact in-memory tiers shape from the stored JSONB.

    Band rates are stored as JSON STRINGS so no float ever touches them; they come back
    as exact Decimals here, which makes a list_rules() row re-validatable as-is.
    """
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        return value
    bands = value.get("bands") or []
    return {
        "mode": value.get("mode") or TIER_MODE_DEFAULT,
        "bands": [
            {
                "threshold_micros": int(band["threshold_micros"]),
                "rate": Decimal(str(band["rate"])).quantize(_RATE_QUANTUM),
            }
            for band in bands
        ],
    }


def _row_to_rule(row: tuple) -> dict[str, Any]:
    out = dict(zip(_RULE_COLS, row))
    conditions = out.get("conditions")
    if isinstance(conditions, str):
        conditions = json.loads(conditions)
    out["conditions"] = conditions or {}
    out["tiers"] = _tiers_from_db(out.get("tiers"))
    out["source_type_scope"] = list(out.get("source_type_scope") or [])
    return out


_SELECT_RULE_SQL = f"SELECT {', '.join(_RULE_COLS)} FROM app.fee_tax_rules WHERE id = %s"


def _select_rule(conn, rule_id: str) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(_SELECT_RULE_SQL, (rule_id,))
        row = cur.fetchone()
    return _row_to_rule(row) if row else None


def get_rule(conn, rule_id: str) -> dict[str, Any]:
    """Read one rule or raise ``FeeTaxRuleNotFoundError`` (-> 404)."""
    row = _select_rule(conn, rule_id)
    if row is None:
        raise FeeTaxRuleNotFoundError(f"fee/tax rule not found: {rule_id}")
    return row


def _project_org_id(project_id: str, conn) -> str:
    """Resolve the org that owns *project_id*, on the CALLER's connection.

    Deliberately FAIL-CLOSED, unlike ``metric_semantics._project_org_id`` which falls
    soft to ``None``: that is fine for a READ of platform defaults, and wrong for a
    WRITE that must be attributable. ``prepare_operation`` treats ``None`` as platform
    scope (migration 109) -- auditing a project write under platform scope would be a
    lie, and a blank string would surface as an opaque FK violation.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
        row = cur.fetchone()
    if row is None or not row[0]:
        raise FeeTaxRuleNotFoundError(f"project not found or has no org: {project_id}")
    return row[0]


# ---------------------------------------------------------------------------
# Activation (C.1) -- read + audited write.
# ---------------------------------------------------------------------------


def is_fee_tax_alignment_active(project_id: str, conn) -> bool:
    """Whether Tax & Fees is active, read from the ONE authority (Story 48.4, AC1).

    This used to read ``app.project_preferences.fee_tax_alignment_enabled``, which
    was a second activation authority beside ``app.project_capabilities``. Two
    booleans meaning the same thing diverge eventually, and this one did: a
    capability disabled through a Project Change Set left the preference TRUE, and
    every dbt Tax model reads the preference -- so a Project that had switched Tax
    & Fees off kept producing Tax-derived rows.

    It now reads ``app.project_tax_fee_activation_v``, where active means all three
    of: the capability is not disabled, its pin names the Project's CURRENT active
    configuration version, and a ladder version is published. A Project with no
    projected row reads OFF, which is the same fail-closed default as before.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tax_fees_active FROM app.project_tax_fee_activation_v WHERE project_id = %s",
            (project_id,),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


class FeeTaxActivationRetired(FeeTaxRuleError):
    """The direct activation door was removed; use the Project Change Set (AC1)."""

    code = "fee_tax_activation_retired"


#: The one door. Named here so the refusal below can point at it without a caller
#: having to read a story file to find out where activation moved to.
ACTIVATION_OWNER_HINT = (
    "Tax & Fees is activated by confirming a Project Change Set in Project Settings "
    "> Capabilities (POST /api/projects/{project_id}/change-sets). The active Project "
    "Configuration Version is the only activation authority."
)


def set_fee_tax_alignment(conn=None, **_kwargs) -> bool:
    """Refuse. Retained as a named refusal, not deleted (Story 48.4, AC1).

    This used to upsert ``project_preferences.fee_tax_alignment_enabled`` and audit
    the flip as ``fee_tax.alignment.set``. The audit was right and the authority was
    wrong: Epic 41 shipped a per-capability boolean beside
    ``app.project_capabilities``, so a Project could be enabled here and disabled
    there, and the dbt models read this one.

    Migration 146 makes the column a projection that cannot diverge -- a direct
    write raises in Postgres. This function is kept as a TYPED refusal rather than
    removed because deleting it would turn every caller into an ``AttributeError``
    at import time, with no sentence naming where activation went. The refusal is
    the migration note.
    """
    _ = conn
    raise FeeTaxActivationRetired(ACTIVATION_OWNER_HINT)


# ---------------------------------------------------------------------------
# The audited rule store (AD-27).
# ---------------------------------------------------------------------------

_INSERT_RULE_SQL = """
    INSERT INTO app.fee_tax_rules
        (id, project_id, scope_kind, scope_ref, category, form, rate, amount_micros,
         cpm_micros, tiers, currency, base_target, cascade_phase, sequence_order,
         conditions, source_type_scope, effective_from, effective_to, status, origin,
         dedup_hash, label, created_by)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s::jsonb, %s, %s, %s, %s,
            %s::jsonb, %s::text[], %s, %s, %s, %s,
            %s, %s, %s)
    ON CONFLICT (project_id, dedup_hash) WHERE dedup_hash IS NOT NULL DO NOTHING
    RETURNING id
"""

# list_rules(datastream_id=...) resolves the rules that APPLY to one datastream.
_DATASTREAM_SCOPE_CLAUSE = (
    "(scope_kind = 'project' OR (scope_kind = 'datastream' AND scope_ref = %s))"
)

_UPDATE_RULE_SQL = """
    UPDATE app.fee_tax_rules SET
        scope_kind = %s, scope_ref = %s, category = %s, form = %s, rate = %s,
        amount_micros = %s, cpm_micros = %s, tiers = %s::jsonb, currency = %s,
        base_target = %s, cascade_phase = %s, sequence_order = %s,
        conditions = %s::jsonb, source_type_scope = %s::text[], effective_from = %s,
        effective_to = %s, status = %s, origin = %s, dedup_hash = %s, label = %s,
        updated_at = NOW()
    WHERE id = %s
"""


def _insert_params(rule_id: str, project_id: str, rule: dict[str, Any], created_by: str) -> tuple:
    return (
        rule_id,
        project_id,
        rule["scope_kind"],
        rule["scope_ref"],
        rule["category"],
        rule["form"],
        rule["rate"],
        rule["amount_micros"],
        rule["cpm_micros"],
        _dump_tiers(rule["tiers"]),
        rule["currency"],
        rule["base_target"],
        rule["cascade_phase"],
        rule["sequence_order"],
        json.dumps(rule["conditions"], sort_keys=True),
        rule["source_type_scope"],
        rule["effective_from"],
        rule["effective_to"],
        rule["status"],
        rule["origin"],
        rule["dedup_hash"],
        rule["label"],
        created_by,
    )


def _update_params(rule_id: str, rule: dict[str, Any]) -> tuple:
    return (
        rule["scope_kind"],
        rule["scope_ref"],
        rule["category"],
        rule["form"],
        rule["rate"],
        rule["amount_micros"],
        rule["cpm_micros"],
        _dump_tiers(rule["tiers"]),
        rule["currency"],
        rule["base_target"],
        rule["cascade_phase"],
        rule["sequence_order"],
        json.dumps(rule["conditions"], sort_keys=True),
        rule["source_type_scope"],
        rule["effective_from"],
        rule["effective_to"],
        rule["status"],
        rule["origin"],
        rule["dedup_hash"],
        rule["label"],
        rule_id,
    )


def _dump_tiers(tiers: dict[str, Any] | None) -> str | None:
    """Serialise tiers with band rates as JSON STRINGS so exactness survives.

    ``app.fee_tax_rule_tiers_v`` reads them with ``->>'rate'`` and casts to NUMERIC, so
    the warehouse sees an exact numeric either way -- and a JSON number would have gone
    through a float on the way in.
    """
    if tiers is None:
        return None
    return json.dumps(_jsonable(tiers), sort_keys=True)


def _select_id_by_dedup(conn, project_id: str, dedup_hash: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.fee_tax_rules WHERE project_id = %s AND dedup_hash = %s",
            (project_id, dedup_hash),
        )
        row = cur.fetchone()
    return row[0] if row else None


def create_rule_spec(
    *,
    project_id: str,
    org_id: str,
    normalised: dict[str, Any],
    created_by: str,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
    confirmation_mode: str = "server",
):
    """Build the OperationSpec for a rule creation. PURE and DETERMINISTIC.

    Every field is a function of (project_id, org_id, the normalised rule, the actor)
    -- so two successive calls with the same inputs produce the SAME spec, hence the
    same ``request_hash``, hence a genuine idempotent REPLAY.

    This is extracted from ``create_rule`` for one reason: it is the only way to
    prove that property offline. ``prepare_operation`` is pure, so a test can build
    two specs and compare their hashes without a database.

    Review finding F1 -- what this MUST NOT do. ``resource_path`` used to end with
    the freshly minted ``fee_tax_rule:<rule_id>``. ``prepare_operation`` folds
    ``resource_path`` into ``request_hash`` while ``_existing_operation`` matches on
    (org, command_type, idempotency-key hash) ONLY, so the second call with the same
    key found the first operation, saw a different request hash and raised
    ``OperationIdempotencyConflict`` -- an untyped RuntimeError, i.e. a 500, instead
    of a replay. The last node is therefore the CONTENT HASH, which is stable across
    identical re-creations and still differs when the payload differs (so re-using a
    key for different content correctly still conflicts). Same shape as
    ``file_source_template.create_file_source_template``, whose last node is the
    stable ``template_code``, not the minted ``fst_`` id. The concrete rule id is
    carried in ``MutationResult.result``, which is written AFTER hashing.
    """
    from core.operations import OperationSpec  # noqa: PLC0415

    digest = content_hash(project_id, normalised)
    return OperationSpec(
        command_type=ACTION_RULE_CREATED,
        actor=created_by,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"fee_tax_rule:{digest}",
        ),
        idempotency_key=idempotency_key or f"fee-tax-rule:{project_id}:{digest}",
        host_context=host_context or {},
        versions={"policy": _POLICY_VERSION},
        request_payload={
            "project_id": project_id,
            "content_hash": digest,
            "scope_kind": normalised["scope_kind"],
            "scope_ref": normalised["scope_ref"],
            "category": normalised["category"],
            "form": normalised["form"],
            "cascade_phase": normalised["cascade_phase"],
            "status": normalised["status"],
            "origin": normalised["origin"],
            "dedup_hash": normalised["dedup_hash"],
        },
        provider_references={},
        confirmation_mode=confirmation_mode,
        confirmation_reference=f"fee-tax-rule:{project_id}:{digest}",
        trace_id=trace_id,
    )


def create_rule(
    conn,
    *,
    project_id: str,
    rule: dict[str, Any],
    created_by: str,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
    confirmation_mode: str = "server",
) -> dict[str, Any]:
    """Declare one fee/tax rule through ``operations.execute_operation`` (AD-27).

    Returns the persisted row plus an explicit ``inserted: bool``. ``inserted`` is True
    only when THIS call created the row: a ``dedup_hash`` collision
    (``ON CONFLICT ... DO NOTHING``) and an idempotent operation replay both report
    False, so 41.2's auto-population can honestly count "N created, M already present"
    instead of inferring it from an empty RETURNING.

    The caller owns the transaction; this never commits.
    """
    normalised = validate_rule(rule)
    org_id = _project_org_id(project_id, conn)
    digest = content_hash(project_id, normalised)
    rule_id = _mint_rule_id()
    dedup_hash = normalised["dedup_hash"]

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        _canonical_hash,
        execute_operation,
    )

    spec = create_rule_spec(
        project_id=project_id,
        org_id=org_id,
        normalised=normalised,
        created_by=created_by,
        idempotency_key=idempotency_key,
        host_context=host_context,
        trace_id=trace_id,
        confirmation_mode=confirmation_mode,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        params = _insert_params(rule_id, project_id, normalised, created_by)
        with operation_conn.cursor() as cur:
            cur.execute(_INSERT_RULE_SQL, params)
            returned = cur.fetchone()
        inserted = returned is not None
        if inserted:
            persisted_id = returned[0]
        else:
            # The only way DO NOTHING fires is a (project_id, dedup_hash) collision.
            persisted_id = _select_id_by_dedup(operation_conn, project_id, dedup_hash)
            if persisted_id is None:  # pragma: no cover - the insert above just ran
                raise FeeTaxRuleError("fee/tax rule row not found after insert")
        result = {
            "rule_id": persisted_id,
            "project_id": project_id,
            "content_hash": digest,
            "inserted": inserted,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "rule_id": persisted_id,
                "project_id": project_id,
                "category": normalised["category"],
                "form": normalised["form"],
                "status": normalised["status"],
                "cascade_phase": normalised["cascade_phase"],
                "inserted": inserted,
            },
        )

    operation = execute_operation(conn, spec, mutation=mutation)
    persisted_id = (operation.result or {}).get("rule_id", rule_id)
    row = _select_rule(conn, persisted_id)
    if row is None:  # pragma: no cover
        raise FeeTaxRuleError("fee/tax rule row not found after operation")
    row["inserted"] = (not operation.replayed) and bool((operation.result or {}).get("inserted"))
    return row


def update_rule(
    conn,
    *,
    rule_id: str,
    patch: dict[str, Any],
    updated_by: str,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
    confirmation_mode: str = "server",
) -> dict[str, Any]:
    """Patch one rule. The MERGED rule is re-validated, never the patch alone.

    404 on an unknown id; 422 when the merged rule would be incoherent (and then NO row
    changes, because validation happens before the operation opens).

    ``status`` is NOT patchable here (review finding F3): the status machine has one
    door, ``set_rule_status``. See ``_PATCH_FORBIDDEN``.
    """
    if not isinstance(patch, dict):
        raise FeeTaxRuleValidationError("patch must be an object")
    if "status" in patch:
        raise FeeTaxRuleValidationError(
            "status is not patchable: use set_rule_status, which enforces the "
            "transition rules and audits as " + ACTION_RULE_STATUS_SET
        )
    forbidden = sorted(set(patch) & _PATCH_FORBIDDEN)
    if forbidden:
        raise FeeTaxRuleValidationError("these fields are immutable: " + ", ".join(forbidden))

    existing = get_rule(conn, rule_id)
    project_id = existing["project_id"]
    merged = {field: existing[field] for field in _RULE_FIELDS}
    merged.update(patch)
    normalised = validate_rule(merged)
    if normalised["status"] != existing["status"]:  # pragma: no cover - defence in depth
        raise FeeTaxRuleValidationError("status is not patchable: use set_rule_status")

    # A colliding dedup_hash would otherwise surface as a raw psycopg UniqueViolation
    # (a 500) from the partial unique index. Pre-check it into a typed 409. The index
    # remains the real guarantee -- this only turns the common case into an honest
    # error; a concurrent writer can still lose the race and hit the constraint.
    new_dedup = normalised["dedup_hash"]
    if new_dedup is not None and new_dedup != existing["dedup_hash"]:
        clash = _select_id_by_dedup(conn, project_id, new_dedup)
        if clash is not None and clash != rule_id:
            raise FeeTaxRuleStateError(
                f"dedup_hash {new_dedup!r} is already used by rule {clash} in this project"
            )

    before = {field: existing[field] for field in _RULE_FIELDS}
    org_id = _project_org_id(project_id, conn)
    digest = content_hash(project_id, normalised)

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    idem = idempotency_key or f"fee-tax-rule-update:{rule_id}:{_mint_idempotency_suffix()}"
    spec = OperationSpec(
        command_type=ACTION_RULE_UPDATED,
        actor=updated_by,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"fee_tax_rule:{rule_id}",
        ),
        idempotency_key=idem,
        host_context=host_context or {},
        versions={"policy": _POLICY_VERSION},
        request_payload={
            "project_id": project_id,
            "rule_id": rule_id,
            "content_hash": digest,
            "fields": sorted(patch),
        },
        provider_references={},
        confirmation_mode=confirmation_mode,
        confirmation_reference=f"fee-tax-rule-update:{rule_id}:{digest}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(_UPDATE_RULE_SQL, _update_params(rule_id, normalised))
        result = {"rule_id": rule_id, "project_id": project_id, "content_hash": digest}
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash(_jsonable(before)),
            after_hash=_canonical_hash(_jsonable(normalised)),
            result=result,
            outbox_payload={
                "rule_id": rule_id,
                "project_id": project_id,
                "status": normalised["status"],
                "category": normalised["category"],
                "form": normalised["form"],
            },
        )

    execute_operation(conn, spec, mutation=mutation)
    return get_rule(conn, rule_id)


def set_rule_status(
    conn,
    *,
    rule_id: str,
    status: str,
    actor: str,
    idempotency_key: str | None = None,
    host_context: dict[str, Any] | None = None,
    trace_id: str | None = None,
    confirmation_mode: str = "server",
) -> dict[str, Any]:
    """Move a rule between ``proposed | confirmed | disabled``.

    404 on an unknown id, 422 (``FeeTaxRuleValidationError``) on a status outside the
    closed vocabulary, 409 (``FeeTaxRuleStateError``) on a forbidden transition. A no-op
    transition returns the row unchanged and writes NO second audit event.
    """
    if status not in STATUSES:
        raise FeeTaxRuleValidationError("status must be one of: " + ", ".join(sorted(STATUSES)))
    existing = get_rule(conn, rule_id)
    current = existing["status"]
    if current == status:
        # A true no-op. Returning early rather than leaning on the operation replay keeps
        # confirmed -> disabled -> confirmed honest: a content-stable idempotency key
        # would have replayed the FIRST confirm and silently skipped the third write.
        return existing
    if status not in _STATUS_TRANSITIONS.get(current, frozenset()):
        raise FeeTaxRuleStateError(f"transition {current} -> {status} is not allowed")

    project_id = existing["project_id"]
    org_id = _project_org_id(project_id, conn)

    from core.operations import (  # noqa: PLC0415
        MutationResult,
        OperationSpec,
        _canonical_hash,
        execute_operation,
    )

    idem = idempotency_key or f"fee-tax-rule-status:{rule_id}:{_mint_idempotency_suffix()}"
    spec = OperationSpec(
        command_type=ACTION_RULE_STATUS_SET,
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            f"fee_tax_rule:{rule_id}",
        ),
        idempotency_key=idem,
        host_context=host_context or {},
        versions={"policy": _POLICY_VERSION},
        request_payload={
            "project_id": project_id,
            "rule_id": rule_id,
            "from_status": current,
            "to_status": status,
        },
        provider_references={},
        confirmation_mode=confirmation_mode,
        confirmation_reference=f"fee-tax-rule-status:{rule_id}:{status}",
        trace_id=trace_id,
    )

    def mutation(operation_conn, _operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.fee_tax_rules SET status = %s, updated_at = NOW() WHERE id = %s",
                (status, rule_id),
            )
        result = {"rule_id": rule_id, "project_id": project_id, "status": status}
        return MutationResult(
            outcome="succeeded",
            before_hash=_canonical_hash({"status": current}),
            after_hash=_canonical_hash({"status": status}),
            result=result,
            outbox_payload={
                "rule_id": rule_id,
                "project_id": project_id,
                "from_status": current,
                "to_status": status,
            },
        )

    execute_operation(conn, spec, mutation=mutation)
    return get_rule(conn, rule_id)


def list_rules(
    project_id: str,
    conn,
    *,
    scope_kind: str | None = None,
    scope_ref: str | None = None,
    datastream_id: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    """List a project's rules, always ordered by ``(cascade_phase, sequence_order, id)``.

    ``scope_kind`` / ``scope_ref`` are EXACT filters (the 41.7 Global Rule Matrix).

    ``datastream_id`` is a RESOLUTION, not a filter: it returns the rules that APPLY to
    that datastream = every ``scope_kind='project'`` rule UNION every rule with
    ``scope_kind='datastream' AND scope_ref = datastream_id``. A rule scoped to another
    datastream is excluded, and ``plan_version`` rules are excluded (they resolve through
    the media plan, 41.3). Combining it with ``scope_kind``/``scope_ref`` is refused:
    the two answer different questions and silently ANDing them would produce a
    plausible-but-wrong empty list.

    No implicit ``status`` filter: 41.7 must show ``proposed`` rules so a human can
    confirm them. The CASCADE filters to ``confirmed`` -- that is 41.3's job, in SQL.
    """
    if datastream_id is not None and (scope_kind is not None or scope_ref is not None):
        raise FeeTaxRuleValidationError(
            "datastream_id resolves the applicable rules; it cannot be combined with "
            "scope_kind / scope_ref"
        )
    if scope_kind is not None and scope_kind not in SCOPE_KINDS:
        raise FeeTaxRuleValidationError(
            "scope_kind must be one of: " + ", ".join(sorted(SCOPE_KINDS))
        )
    if status is not None and status not in STATUSES:
        raise FeeTaxRuleValidationError("status must be one of: " + ", ".join(sorted(STATUSES)))

    clauses = ["project_id = %s"]
    params: list[Any] = [project_id]
    if datastream_id is not None:
        clauses.append(_DATASTREAM_SCOPE_CLAUSE)
        params.append(datastream_id)
    if scope_kind is not None:
        clauses.append("scope_kind = %s")
        params.append(scope_kind)
    if scope_ref is not None:
        clauses.append("scope_ref = %s")
        params.append(scope_ref)
    if status is not None:
        clauses.append("status = %s")
        params.append(status)

    sql = (
        f"SELECT {', '.join(_RULE_COLS)} FROM app.fee_tax_rules "
        f"WHERE {' AND '.join(clauses)} "
        "ORDER BY cascade_phase, sequence_order, id"
    )
    with conn.cursor() as cur:
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
    return [_row_to_rule(row) for row in rows]
