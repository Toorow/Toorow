"""Country tax defaults: the governed seed, its loader, and auto-population (Story 41.2).

Third of the three resolvers Epic 41's cascade stands on: *which tax defaults does a
tracked country carry?* The answer is **seed data**, read from
``dbt/seeds/country_tax_defaults.csv`` -- never a Python dict of rates. A hard-coded
``{"..": 0.02}`` map in ``server/core/`` is a defect (E41-NFR04 / contract C.3), caught
both by ``server/tests/conformance/test_no_geographic_hardcode.py`` and by a story-local
AST guard. Adding a jurisdiction is a SEED ROW, not code.

> **The seed ships DEFAULTS REQUIRING OPERATOR CONFIRMATION, NOT TAX ADVICE.** Every
> auto-populated rule lands ``status='proposed'`` and is **inert** until a human confirms
> it. Rates and effective dates change by jurisdiction and by contract; the platform
> ships a starting point so the operator reviews instead of typing JSON. The same
> statement is carried by ``dbt/seeds/country_tax_defaults.NOTICE.md`` and by the
> ``source_note`` of every single row -- which this loader ENFORCES non-empty, so the
> honesty gate is code, not review.

Auto-population is inert by design and idempotent BY DATABASE CONSTRAINT: every rule it
proposes carries a stable ``dedup_hash`` and 41.1's store inserts with
``ON CONFLICT (project_id, dedup_hash) DO NOTHING``. Re-running creates no duplicate and
**never overwrites or resurrects a human decision**.
"""

from __future__ import annotations

import csv
import hashlib
import logging
import os
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parents[2]
_SEED_FILENAME = "country_tax_defaults.csv"

# The 7 columns of contract C.3, in order.
SEED_COLUMNS = (
    "iso_code",
    "tax_category",
    "form",
    "rate",
    "label",
    "source_note",
    "effective_from",
)

# Both are members of 41.1's CATEGORIES. A country default outside these two would not be
# a country default: an agency fee or a platform fee is per-contract, never per-country.
SEED_TAX_CATEGORIES = frozenset({"REGULATORY_TAX", "SALES_TAX"})

# A country default that is not a percentage is REFUSED rather than guessed: a flat
# amount would need a currency the seed cannot know, and tiers would need a contract.
SEED_FORM = "PERCENTAGE"

# The E41-FR03 cascade phases the two categories belong to, and what they apply to.
_CASCADE_PHASE_BY_CATEGORY: dict[str, int] = {"REGULATORY_TAX": 3, "SALES_TAX": 6}
_BASE_TARGET_BY_CATEGORY: dict[str, str] = {
    "REGULATORY_TAX": "NET_MEDIA",
    "SALES_TAX": "RUNNING_SUBTOTAL",
}

# WHY AN AUTO RULE CARRIES **NO** source_type_scope -- do not "restore" one.
#
# The applicability reasoning is sound and worth keeping written down: a digital services
# tax is levied on media buying, so it does not belong on a direct service cost; a sales
# tax applies to the whole cost ladder; and NEITHER ever applies to a revenue source, the
# revenue-side HT/TTC symmetry being Story 41.5's to declare. What is NOT sound is turning
# that reasoning into a stored condition.
#
# Migration 119 flattens source_type_scope into fee_tax_rule_conditions as
# condition_key='source_type' rows, and the cascade correctly ranks an UNRESOLVABLE
# attribute above a known-false one. But NOTHING writes app.datastream_source_types today,
# so attr_source_type is NULL on every row in production. A scope therefore attaches an
# UNSATISFIABLE PRECONDITION to every auto rule: the moment an operator confirms DST FR
# 3 %, the row returns a NULL total with gap SOURCE_TYPE_UNRESOLVED -- E41-FR02 can never
# compose a total, and the C5 posture rung that exists precisely to avoid blank day-one
# totals is neutralised. Verified in the built warehouse, not reasoned about.
#
# A country tax rule's applicability is ALREADY fully expressed by its country condition.
# The scope added a precondition for no benefit. When a declaration surface exists
# (41.6 / 41.8) a source-type scope can come back as an EXPLICIT OPERATOR CHOICE on a rule
# the operator can see and fix -- never as a silent default on a proposal.
#
# validate_rule() normalises an absent source_type_scope to [], the flattening view then
# yields no condition row at all, and an empty condition set reads as "applies to
# everything" -- exactly like an empty `conditions` object. Absence is never "no match".

# Every auto rule shares one slot. Safe: migration 119 ships NO
# UNIQUE (project_id, cascade_phase, sequence_order) because every rule inside a phase
# reads the subtotal AS AT PHASE ENTRY (C.8 decision 3), so sequence_order is a
# display/override key and two rules may legitimately share it.
AUTO_SEQUENCE_ORDER = 900

AUTO_ORIGIN = "auto_country"
AUTO_SCOPE_KIND = "project"
AUTO_STATUS = "proposed"

# The ONLY condition key an auto-populated rule may declare. Every stored precondition
# must be an attribute the warehouse can actually resolve today, otherwise confirming the
# rule yields a NULL total instead of a number. A story-local test asserts the emitted
# payloads never widen past this set.
AUTO_CONDITION_KEYS = frozenset({"country"})

_DEDUP_PREFIX = "ftk_"
_DEDUP_RECIPE_VERSION = "v1"
_DEDUP_DIGEST_CHARS = 32

SKIPPED_MODULE_OFF = "module_off"
SKIPPED_POSTURE_GLOBAL = "geographic_posture_global"


class CountryTaxDefaultsError(RuntimeError):
    """The country tax defaults seed cannot be trusted.

    FAIL-CLOSED on purpose, mirroring ``country_vocabulary.CountryVocabularyError``: a
    malformed rate or an empty ``source_note`` must stop the load, not silently propose a
    number nobody can explain.
    """


@dataclass(frozen=True, slots=True)
class CountryTaxDefault:
    """One seeded default. ``rate`` is an exact ``Decimal``, never a float.

    41.1's ``validate_rule`` refuses a ``float`` outright: 0.03 is not exactly three
    percent in binary and the difference would silently enter an invoice total.
    """

    iso_code: str
    tax_category: str
    form: str
    rate: Decimal
    label: str
    source_note: str
    effective_from: date

    def as_dict(self) -> dict[str, Any]:
        return {
            "iso_code": self.iso_code,
            "tax_category": self.tax_category,
            "form": self.form,
            "rate": str(self.rate),
            "label": self.label,
            "source_note": self.source_note,
            "effective_from": self.effective_from.isoformat(),
        }


def default_tax_seed_path() -> Path:
    """Same env contract as ``country_vocabulary.default_country_seed_path``."""
    seed_dir = Path(os.environ.get("TOOROW_DBT_SEEDS_DIR", str(_REPO_ROOT / "dbt" / "seeds")))
    return seed_dir / _SEED_FILENAME


def _parse_rate(raw: str, iso_code: str) -> Decimal:
    try:
        rate = Decimal(raw)
    except (InvalidOperation, ValueError, ArithmeticError) as exc:
        raise CountryTaxDefaultsError(
            f"country tax default for {iso_code} has an invalid rate: {raw!r}"
        ) from exc
    if rate < 0 or rate >= 1:
        raise CountryTaxDefaultsError(
            f"country tax default for {iso_code} must carry a rate fraction in [0, 1): {raw!r}"
        )
    return rate


def load_country_tax_defaults(path: Path | None = None) -> tuple[CountryTaxDefault, ...]:
    """Parse the governed seed, fail-closed on anything it cannot vouch for.

    > **These are DEFAULTS REQUIRING OPERATOR CONFIRMATION, NOT TAX ADVICE.** Every row
    > this loader returns becomes a ``status='proposed'`` rule that is inert until a human
    > confirms it. Rates and effective dates change by jurisdiction and by contract.

    Refused, one error each: a missing header column; an ``iso_code`` outside the governed
    ISO vocabulary; a ``tax_category`` outside ``REGULATORY_TAX`` / ``SALES_TAX``; a
    ``form`` other than ``PERCENTAGE``; a rate outside ``[0, 1)``; an **empty
    ``source_note``** (the honesty gate); an unparseable ``effective_from``; a duplicate
    ``(iso_code, tax_category, effective_from)``.
    """
    from core.country_vocabulary import get_supported_country_codes  # noqa: PLC0415

    seed_path = path or default_tax_seed_path()
    supported = get_supported_country_codes()
    records: list[CountryTaxDefault] = []
    seen: set[tuple[str, str, str]] = set()

    try:
        with seed_path.open(newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not set(SEED_COLUMNS) <= set(reader.fieldnames):
                raise CountryTaxDefaultsError(
                    "country tax defaults require the headers: " + ", ".join(SEED_COLUMNS)
                )
            for row in reader:
                iso_code = (row.get("iso_code") or "").strip().upper()
                if iso_code not in supported:
                    raise CountryTaxDefaultsError(
                        f"country tax default names a code outside the governed "
                        f"vocabulary: {iso_code!r}"
                    )
                tax_category = (row.get("tax_category") or "").strip()
                if tax_category not in SEED_TAX_CATEGORIES:
                    raise CountryTaxDefaultsError(
                        f"country tax default for {iso_code} has an unsupported "
                        f"tax_category: {tax_category!r}"
                    )
                form = (row.get("form") or "").strip()
                if form != SEED_FORM:
                    raise CountryTaxDefaultsError(
                        f"country tax default for {iso_code} must be {SEED_FORM}, "
                        f"not {form!r}"
                    )
                rate = _parse_rate((row.get("rate") or "").strip(), iso_code)
                label = (row.get("label") or "").strip()
                if not label:
                    raise CountryTaxDefaultsError(
                        f"country tax default for {iso_code} requires a label"
                    )
                source_note = (row.get("source_note") or "").strip()
                if not source_note:
                    # The honesty gate (E41-NFR03), enforced here and not only by review:
                    # a default nobody can explain must not reach an operator's screen.
                    raise CountryTaxDefaultsError(
                        f"country tax default for {iso_code} requires a non-empty "
                        "source_note: a shipped default must say what it is and what it "
                        "is not"
                    )
                raw_from = (row.get("effective_from") or "").strip()
                try:
                    effective_from = date.fromisoformat(raw_from)
                except ValueError as exc:
                    raise CountryTaxDefaultsError(
                        f"country tax default for {iso_code} has an invalid "
                        f"effective_from: {raw_from!r}"
                    ) from exc

                identity = (iso_code, tax_category, effective_from.isoformat())
                if identity in seen:
                    raise CountryTaxDefaultsError(
                        "duplicate country tax default: " + "/".join(identity)
                    )
                seen.add(identity)
                records.append(
                    CountryTaxDefault(
                        iso_code=iso_code,
                        tax_category=tax_category,
                        form=form,
                        rate=rate,
                        label=label,
                        source_note=source_note,
                        effective_from=effective_from,
                    )
                )
    except CountryTaxDefaultsError:
        raise
    except (OSError, csv.Error) as exc:
        raise CountryTaxDefaultsError("country tax defaults seed is unavailable") from exc

    if not records:
        raise CountryTaxDefaultsError("country tax defaults seed is empty")
    return tuple(records)


@lru_cache(maxsize=1)
def get_country_tax_defaults() -> tuple[CountryTaxDefault, ...]:
    return load_country_tax_defaults()


def defaults_for_country(iso_code: object) -> tuple[CountryTaxDefault, ...]:
    """Every default seeded for one country. An EMPTY result is honest, not an error.

    A tracked country with no seeded default (no federal VAT, no verified DST) is a
    ``no_default`` count and a prompt to declare -- never a fabricated zero rate.
    """
    wanted = str(iso_code or "").strip().upper()
    if not wanted:
        return ()
    return tuple(item for item in get_country_tax_defaults() if item.iso_code == wanted)


# ---------------------------------------------------------------------------
# Idempotency (D4).
# ---------------------------------------------------------------------------


def build_auto_rule_dedup_key(
    *,
    scope_kind: str,
    category: str,
    form: str,
    effective_from: date,
    countries: Sequence[str],
) -> str:
    """Stable identity of ONE auto-populated rule: the value of its ``dedup_hash`` field.

    Pure: no clock, no ULID, no I/O. Canonical: every component is normalised and the
    country list is SORTED, so the value is order-insensitive and byte-identical across
    runs and across machines.

    The field is ``dedup_hash``, not ``dedup_key``: every write travels through
    ``operations.execute_operation``, whose ``_is_secret_key`` guard REFUSES any payload
    key ending in ``_key`` unless it ends in ``_id`` / ``_ref`` / ``_hash``. A field named
    ``dedup_key`` would have raised ``OperationValidationError`` on every single
    auto-populated rule. ``_hash`` is also the accurate name -- this is an sha256 digest.

    DELIBERATELY EXCLUDED: ``rate``, ``label``, ``source_type_scope`` and
    ``sequence_order``. If an operator edits the rate on a ``proposed`` auto rule, the next
    run must recognise it as THE SAME RULE and leave it alone -- including the rate would
    mint a duplicate on every edit, which is the exact failure this value exists to
    prevent.

    ``scope_kind`` is included even though every auto rule is project-scoped today, so a
    future datastream-scoped auto rule cannot collide with the project-scoped one.

    The recipe carries a VERSION: bumping it is an explicit, reviewable decision to
    re-populate, never an accident.
    """
    payload = "|".join(
        (
            _DEDUP_RECIPE_VERSION,
            AUTO_ORIGIN,
            str(scope_kind).strip(),
            str(category).strip(),
            str(form).strip(),
            effective_from.isoformat(),
            "country=" + ",".join(sorted(str(item).strip().upper() for item in countries)),
        )
    )
    digest = hashlib.sha256(payload.encode("ascii")).hexdigest()[:_DEDUP_DIGEST_CHARS]
    return _DEDUP_PREFIX + digest


# ---------------------------------------------------------------------------
# Auto-population (E.1 + D4).
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutoPopulationResult:
    """What one auto-population run did, honestly counted."""

    created: int = 0
    unchanged: int = 0
    skipped_confirmed: int = 0
    skipped_disabled: int = 0
    no_default: int = 0
    rate_drift: int = 0
    drifted: tuple[dict[str, str], ...] = ()
    countries: tuple[str, ...] = ()
    skipped_reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "unchanged": self.unchanged,
            "skipped_confirmed": self.skipped_confirmed,
            "skipped_disabled": self.skipped_disabled,
            "no_default": self.no_default,
            "rate_drift": self.rate_drift,
            "drifted": [dict(item) for item in self.drifted],
            "countries": list(self.countries),
            "skipped_reason": self.skipped_reason,
        }


def build_auto_rule_payload(default: CountryTaxDefault) -> dict[str, Any]:
    """Seed row -> the ``create_rule`` payload of E.1. Pure.

    ``currency`` is OMITTED, not set to ``None``: 41.1 refuses a currency on a
    ``PERCENTAGE`` rule. ``rate`` travels as an exact ``Decimal``.

    ``conditions`` names ONE country per rule -- never a list of the project's countries.
    A per-country rule is what lets an operator confirm, edit or disable one jurisdiction
    without touching the others. **``country`` is the ONLY condition key an auto rule ever
    declares**, and no ``source_type_scope`` is set at all: every stored precondition must
    be one the warehouse can actually resolve, or confirming the rule produces a NULL total
    instead of a number (see the block comment above).

    EVERY key here is checked against ``operations._is_secret_key``: the whole payload
    reaches the audit record through ``execute_operation``, and a key that guard rejects
    would fail the write outright rather than degrade. That is why the idempotency field is
    ``dedup_hash`` and not ``dedup_key``.
    """
    category = default.tax_category
    return {
        "scope_kind": AUTO_SCOPE_KIND,
        "scope_ref": None,
        "category": category,
        "form": default.form,
        "rate": default.rate,
        "base_target": _BASE_TARGET_BY_CATEGORY[category],
        "cascade_phase": _CASCADE_PHASE_BY_CATEGORY[category],
        "sequence_order": AUTO_SEQUENCE_ORDER,
        "conditions": {"country": [default.iso_code]},
        "effective_from": default.effective_from,
        "effective_to": None,
        "status": AUTO_STATUS,
        "origin": AUTO_ORIGIN,
        "dedup_hash": build_auto_rule_dedup_key(
            scope_kind=AUTO_SCOPE_KIND,
            category=category,
            form=default.form,
            effective_from=default.effective_from,
            countries=(default.iso_code,),
        ),
        "label": default.label,
    }


def build_auto_rule_payloads(country_codes: Sequence[str]) -> tuple[dict[str, Any], ...]:
    """Every auto rule a set of tracked countries produces, in deterministic order. Pure.

    THE single source of truth for "what auto-population actually emits". Both
    ``auto_populate_tax_rules`` and any test fixture must go through here rather than
    hand-writing an approximation: a fixture that hand-writes rules cannot catch a
    divergence between what this story emits and what the 41.3 ladder can consume, and
    that gap is exactly what let an unsatisfiable ``source_type_scope`` ship unnoticed.

    A country with no seeded default contributes nothing (that is a ``no_default`` count,
    not an error). Order is (country, then seed order) so a fixture is byte-stable.
    """
    payloads: list[dict[str, Any]] = []
    for code in country_codes:
        for default in defaults_for_country(code):
            payloads.append(build_auto_rule_payload(default))
    return tuple(payloads)


def _rate_drift(stored: Any, seed_rate: Decimal) -> tuple[str, str] | None:
    """``(stored, seed)`` as strings when they differ, else ``None``. Never raises.

    Compared as exact ``Decimal``s, so ``0.02`` and ``0.020000`` are the SAME rate and do
    not raise a false drift. A stored rate that is absent or unparseable reports NO drift:
    inventing one from a value we could not read would be its own fabrication.
    """
    if stored is None:
        return None
    try:
        stored_rate = stored if isinstance(stored, Decimal) else Decimal(str(stored))
    except (InvalidOperation, ValueError, ArithmeticError):
        logger.warning("fee_tax_country_defaults: unreadable stored rate %r", stored)
        return None
    if stored_rate == seed_rate:
        return None
    return str(stored_rate), str(seed_rate)


def auto_populate_tax_rules(
    project_id: str,
    conn: object,
    *,
    identity: str = "system",
) -> AutoPopulationResult:
    """Propose the seeded tax defaults for every country this project already tracks.

    Two guards, both returning a TYPED ``skipped_reason`` with zero writes and zero side
    effects: the module must be ON for this project (E41-FR01), and the project's GOVERNED
    geography must be ``local_markets`` -- a Global project tracks no country, and a
    brand-new project reads as Global for free.

    Story 37.9: that second guard read ``project_preferences`` until 2026-08-17, which the
    ratified Country capability replaced and never writes back. A Project governed through
    the capability therefore read as Global and every tax default was skipped with
    ``geographic_posture_global`` -- a typed refusal naming a posture nobody had chosen.
    It now reads ``country_activation.governed_posture``, the one reader.

    Countries come from the tracked markets' DERIVED UNION, the same set the Tax cascade
    counts. One rule per (country, seeded default); never one rule per market and never
    one rule listing several countries.

    **Idempotency is a database constraint, and the pre-read is only a report.** One
    ``list_rules`` pre-read indexed by ``dedup_hash`` classifies each intended row as
    already-``proposed`` (``unchanged``), ``skipped_confirmed`` or ``skipped_disabled``, so
    the counters can say WHY nothing was written -- ``ON CONFLICT ... DO NOTHING`` cannot.
    The constraint is what makes the write race-safe: a row this run classed ``created``
    but that a concurrent run inserted first comes back ``inserted=False`` and is counted
    ``unchanged``.

    A ``confirmed`` or ``disabled`` rule is NEVER overwritten and NEVER resurrected: the
    store is not even called for it. A human decision outranks a proposal, always.

    **``rate_drift`` closes the hole that silence would otherwise leave.** ``rate`` is
    deliberately absent from ``dedup_hash`` so that an operator's rate edit is recognised
    as the same rule -- but the same property means a SEED CORRECTION is invisible: ship a
    rate, learn it was wrong, fix the seed in place, re-run, and the hash still matches, so
    the row is counted ``unchanged`` and the run reports success while the stale rate keeps
    invoicing. So every matched rule's STORED rate is compared with the SEED rate, and a
    difference is counted and named in ``drifted`` (with both values) and logged at
    WARNING. This function still changes nothing -- silently rewriting a rate an operator
    may have deliberately overridden would be worse -- but the operator is TOLD rather than
    reassured. Deciding what to do about a drift is a human act; hiding it is not an option.

    All writes go through 41.1's audited store (``operations.execute_operation``: audit +
    outbox + replay, AD-27). This function issues no SQL against ``app.fee_tax_rules``.
    """
    from core import fee_tax_rules  # noqa: PLC0415
    from core.country_activation import governed_posture  # noqa: PLC0415
    from core.geographic_reporting import GLOBAL  # noqa: PLC0415

    if not fee_tax_rules.is_fee_tax_alignment_active(project_id, conn):
        return AutoPopulationResult(skipped_reason=SKIPPED_MODULE_OFF)

    posture = governed_posture(conn, project_id=project_id)
    if posture.mode == GLOBAL:
        return AutoPopulationResult(skipped_reason=SKIPPED_POSTURE_GLOBAL)

    countries = tuple(posture.country_codes)
    existing_by_key: dict[str, tuple[str, Any]] = {}
    for rule in fee_tax_rules.list_rules(project_id, conn):
        key = rule.get("dedup_hash")
        if key:
            existing_by_key[str(key)] = (str(rule.get("status") or ""), rule.get("rate"))

    created = 0
    unchanged = 0
    skipped_confirmed = 0
    skipped_disabled = 0
    no_default = 0
    drifted: list[dict[str, str]] = []

    for code in countries:
        defaults = defaults_for_country(code)
        if not defaults:
            # Absence of a seeded default is not a zero and not an error: the operator
            # declares what this jurisdiction charges.
            no_default += 1
            continue
        for default in defaults:
            payload = build_auto_rule_payload(default)
            key = str(payload["dedup_hash"])
            prior = existing_by_key.get(key)
            if prior is not None:
                prior_status, prior_rate = prior
                drift = _rate_drift(prior_rate, default.rate)
                if drift is not None:
                    drifted.append(
                        {
                            "dedup_hash": key,
                            "iso_code": default.iso_code,
                            "tax_category": default.tax_category,
                            "stored_rate": drift[0],
                            "seed_rate": drift[1],
                            "status": prior_status,
                        }
                    )
                    logger.warning(
                        "fee_tax_country_defaults: rate drift project=%s %s/%s stored=%s "
                        "seed=%s status=%s -- the rule was NOT rewritten; an operator must "
                        "decide whether the seed correction applies",
                        project_id,
                        default.iso_code,
                        default.tax_category,
                        drift[0],
                        drift[1],
                        prior_status,
                    )
                if prior_status == "confirmed":
                    skipped_confirmed += 1
                    continue
                if prior_status == "disabled":
                    skipped_disabled += 1
                    continue
                unchanged += 1
                continue
            row = fee_tax_rules.create_rule(
                conn,
                project_id=project_id,
                rule=payload,
                created_by=identity,
            )
            if row.get("inserted"):
                created += 1
            else:
                # DO NOTHING fired: a concurrent run inserted the same dedup_hash first.
                unchanged += 1

    logger.info(
        "fee_tax_country_defaults: auto-population project=%s created=%s unchanged=%s "
        "skipped_confirmed=%s skipped_disabled=%s no_default=%s rate_drift=%s countries=%s",
        project_id,
        created,
        unchanged,
        skipped_confirmed,
        skipped_disabled,
        no_default,
        len(drifted),
        len(countries),
    )
    return AutoPopulationResult(
        created=created,
        unchanged=unchanged,
        skipped_confirmed=skipped_confirmed,
        skipped_disabled=skipped_disabled,
        no_default=no_default,
        rate_drift=len(drifted),
        drifted=tuple(drifted),
        countries=countries,
    )
