"""Prequalified Tax & Fee presets: versioned PROPOSALS, never executable truth (AC3).

A preset is a versioned statement about the world. A rule is a Project's decision.
Story 48.4 keeps them apart because Epic 41 did not: ``auto_populate_tax_rules``
read ``country_tax_defaults.csv`` and INSERTED rows straight into
``app.fee_tax_rules`` -- inert by ``status='proposed'``, but already shaped as
rules, already scoped, and with the seed's ``source_note`` dropped on the floor
before persistence. The one sentence that said "confirm this against your invoice
and your tax advisor" did not survive the trip into the store.

The load-bearing idea here is **qualification**. AC3: "Country, source type and
observed invoice/source evidence narrow candidate rules. They do not by themselves
prove VAT, DST, withholding, reverse charge, payment fees or contractual
pass-through."

Take the seed's own worst case. The UK Digital Services Tax is a narrow tax on the
qualifying group revenues of large digital businesses, with activity and threshold
tests. ``GB + 2%`` is therefore evidence that a tax EXISTS, and no evidence at all
that a particular media buyer is being charged it. Whether it reaches an invoice
depends on whether the platform passes it through, on which service was bought,
and on tests about a party that is not the buyer. Those are four separate
questions, and the difference between a helpful starting point and a fabricated
surcharge is whether they were asked.

So a preset carries its qualifications as DATA, a proposal reports which of them
are proven, and a proposal with unproven qualifications compiles to something an
operator must answer -- never to a rule that quietly starts adding 2% to a total.

Nothing in this module ships a rate. Every value comes from a governed preset row
or from the dbt seed, and the seed importer preserves the notes rather than
summarizing them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

PRESET_STATUSES: tuple[str, ...] = ("draft", "published", "superseded", "withdrawn")
VERIFICATION_STATUSES: tuple[str, ...] = ("unverified", "verified", "stale")
JURISDICTION_KINDS: tuple[str, ...] = ("none", "country", "market", "region")

#: The questions a candidate must answer before it is a rule rather than a rumour.
#: DATA, not an if/elif chain, for the reason `_CATEGORY_FORMS` is data: a chain is
#: where the next jurisdiction's extra test gets forgotten.
QUALIFICATION_CODES: tuple[str, ...] = (
    # Is the thing actually bought inside the taxed scope? An EU standard VAT rate
    # applies to a supply category, not to "everything sold in that country".
    "taxable_service_matches",
    # Does the provider actually pass this through, and on which invoice line? A
    # statutory tax that the platform absorbs never reaches the buyer's total.
    "provider_passes_through",
    # Activity and revenue thresholds. The UK DST's tests are about the PROVIDER's
    # group revenues; nothing observable in a media buyer's own data settles them.
    "threshold_met",
    # Where the supply is deemed to happen, which is not always where the campaign ran.
    "jurisdiction_of_supply",
    # A contract, for anything whose authority is an agreement rather than a statute.
    "contractual_basis",
    # Reverse charge and withholding shift WHO remits; getting it wrong double counts.
    "reverse_charge_assessed",
)


class PresetError(ValueError):
    """A preset version or proposal violates the contract."""

    code = "invalid_tax_fee_preset"


@dataclass(frozen=True, slots=True)
class PresetVersion:
    """One immutable, versioned proposal input."""

    id: str
    preset_key: str
    version_number: int
    status: str
    label: str
    issuer: str
    source_reference: str
    source_reference_version: str
    authoritative_url: str | None
    document_ref: str | None
    jurisdiction_kind: str
    jurisdiction_code: str | None
    taxable_subject: str
    service_scope: str | None
    category: str
    form: str
    rate: Decimal | None
    amount_micros: int | None
    cpm_micros: int | None
    currency: str | None
    base_target: str
    thresholds: tuple[dict[str, Any], ...]
    qualifications: tuple[dict[str, Any], ...]
    assumptions: tuple[str, ...]
    published_on: date | None
    effective_from: date
    effective_to: date | None
    last_verified_on: date | None
    verification_status: str
    content_hash: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "preset_version_id": self.id,
            "preset_key": self.preset_key,
            "version_number": self.version_number,
            "label": self.label,
            "issuer": self.issuer,
            "source_reference": self.source_reference,
            "source_reference_version": self.source_reference_version,
            "authoritative_url": self.authoritative_url,
            "document_ref": self.document_ref,
            "jurisdiction_kind": self.jurisdiction_kind,
            "jurisdiction_code": self.jurisdiction_code,
            "taxable_subject": self.taxable_subject,
            "service_scope": self.service_scope,
            "category": self.category,
            "form": self.form,
            "rate": str(self.rate) if self.rate is not None else None,
            "base_target": self.base_target,
            "thresholds": [dict(item) for item in self.thresholds],
            "qualifications": [dict(item) for item in self.qualifications],
            "assumptions": list(self.assumptions),
            "effective_from": self.effective_from.isoformat(),
            "effective_to": self.effective_to.isoformat() if self.effective_to else None,
            "verification_status": self.verification_status,
            "content_hash": self.content_hash,
        }


# ---------------------------------------------------------------------------
# Validation. Pure.
# ---------------------------------------------------------------------------


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PresetError(f"{label} is required")
    return value.strip()


def _as_date(value: Any, label: str, *, required: bool = False) -> date | None:
    if value in (None, ""):
        if required:
            raise PresetError(f"{label} is required")
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise PresetError(f"{label} must be an ISO date (YYYY-MM-DD)") from exc


def validate_qualifications(value: Any) -> list[dict[str, Any]]:
    """Normalize the questions a candidate must answer.

    An EMPTY list is refused for a jurisdiction-bearing preset by
    :func:`validate_preset`, not here, so the refusal can name the reason. Each
    entry records the question in words: the operator answering it is not reading
    this source file, and a bare code is not a question.
    """

    if not isinstance(value, (list, tuple)):
        raise PresetError("qualifications must be a list")
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping):
            raise PresetError("each qualification must be an object")
        code = _text(item.get("code"), "qualification.code")
        if code not in QUALIFICATION_CODES:
            raise PresetError(
                f"qualification code {code!r} is not one of {list(QUALIFICATION_CODES)}"
            )
        if code in seen:
            raise PresetError(f"qualification {code!r} is declared twice")
        seen.add(code)
        entries.append(
            {
                "code": code,
                "question": _text(item.get("question"), f"qualification[{code}].question"),
                # Whether an operator has ANSWERED it. A preset ships them all
                # unproven: the shared knowledge cannot know one Project's contract.
                "proven": bool(item.get("proven", False)),
                "evidence": str(item.get("evidence") or "").strip() or None,
            }
        )
    return sorted(entries, key=lambda entry: entry["code"])


def validate_preset(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize and validate ONE preset version. Pure: no DB, no clock.

    The routed-pair guard is reused from Epic 41 rather than re-implemented: a
    preset that proposes a ``(category, form, base)`` triple no evaluator routes
    would compile to a rule contributing exactly zero behind a complete flag, and
    catching that at the preset is catching it one step earlier than at the rule.
    """

    from core.fee_tax_rules import (  # noqa: PLC0415 -- reuse, do not re-implement
        FeeTaxRuleValidationError,
        _require_routed_pair,
    )

    if not isinstance(payload, Mapping):
        raise PresetError("a preset must be an object")

    jurisdiction_kind = str(payload.get("jurisdiction_kind") or "none")
    if jurisdiction_kind not in JURISDICTION_KINDS:
        raise PresetError(f"jurisdiction_kind must be one of {list(JURISDICTION_KINDS)}")
    jurisdiction_code = str(payload.get("jurisdiction_code") or "").strip() or None
    if (jurisdiction_kind == "none") != (jurisdiction_code is None):
        raise PresetError(
            "a jurisdiction-bearing preset must name its jurisdiction_code, and a "
            "jurisdiction-free one must not carry one"
        )

    category = _text(payload.get("category"), "category")
    form = _text(payload.get("form"), "form")
    base_target = _text(payload.get("base_target"), "base_target")
    try:
        _require_routed_pair(category, form, base_target)
    except FeeTaxRuleValidationError as exc:
        raise PresetError(str(exc)) from exc

    qualifications = validate_qualifications(payload.get("qualifications") or [])
    if jurisdiction_kind != "none" and not qualifications:
        # The whole point of AC3. A jurisdiction and a headline rate look like a
        # complete answer and are not one; shipping them with nothing to prove is
        # how a statutory reference becomes an invoice surcharge.
        raise PresetError(
            "a jurisdiction-bearing preset must declare what still has to be proven "
            "before it applies; a country and a rate are not a qualification"
        )

    rate = payload.get("rate")
    if rate is not None:
        try:
            rate = Decimal(str(rate))
        except (ArithmeticError, ValueError) as exc:
            raise PresetError("rate must be an exact decimal") from exc

    effective_from = _as_date(payload.get("effective_from"), "effective_from", required=True)
    effective_to = _as_date(payload.get("effective_to"), "effective_to")
    if effective_to and effective_from and effective_to < effective_from:
        raise PresetError("effective_to cannot precede effective_from")

    verification_status = str(payload.get("verification_status") or "unverified")
    if verification_status not in VERIFICATION_STATUSES:
        raise PresetError(f"verification_status must be one of {list(VERIFICATION_STATUSES)}")

    return {
        "preset_key": _text(payload.get("preset_key"), "preset_key"),
        "label": _text(payload.get("label"), "label"),
        "issuer": _text(payload.get("issuer"), "issuer"),
        "source_reference": _text(payload.get("source_reference"), "source_reference"),
        "source_reference_version": _text(
            payload.get("source_reference_version"), "source_reference_version"
        ),
        "authoritative_url": str(payload.get("authoritative_url") or "").strip() or None,
        "document_ref": str(payload.get("document_ref") or "").strip() or None,
        "jurisdiction_kind": jurisdiction_kind,
        "jurisdiction_code": jurisdiction_code,
        "taxable_subject": _text(payload.get("taxable_subject"), "taxable_subject"),
        "service_scope": str(payload.get("service_scope") or "").strip() or None,
        "category": category,
        "form": form,
        "rate": rate,
        "amount_micros": payload.get("amount_micros"),
        "cpm_micros": payload.get("cpm_micros"),
        "currency": str(payload.get("currency") or "").strip() or None,
        "base_target": base_target,
        "thresholds": [dict(item) for item in (payload.get("thresholds") or [])],
        "qualifications": qualifications,
        "assumptions": [str(item) for item in (payload.get("assumptions") or [])],
        "published_on": _as_date(payload.get("published_on"), "published_on"),
        "effective_from": effective_from,
        "effective_to": effective_to,
        "last_verified_on": _as_date(payload.get("last_verified_on"), "last_verified_on"),
        "verification_status": verification_status,
    }


# ---------------------------------------------------------------------------
# Proposals. A preset plus a Project's evidence yields a CANDIDATE, not a rule.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PresetProposal:
    """Why a preset MAY apply here, and exactly what is still unproven (AC3)."""

    preset: PresetVersion
    matched_on: tuple[str, ...]
    unproven: tuple[dict[str, Any], ...]
    confidence: str
    #: A ladder rule an operator can adopt once the qualifications are answered.
    #: Never published by this module: it is a form to fill in, not a decision.
    draft_rule: dict[str, Any]

    def as_payload(self) -> dict[str, Any]:
        return {
            "preset": self.preset.as_payload(),
            "matched_on": list(self.matched_on),
            "why_it_may_apply": (
                f"{self.preset.issuer} states this for "
                f"{self.preset.jurisdiction_code or 'any jurisdiction'} "
                f"({self.preset.taxable_subject})."
            ),
            "unproven_qualifications": [dict(item) for item in self.unproven],
            "confidence": self.confidence,
            "operator_must_confirm": [item["question"] for item in self.unproven],
            "draft_rule": dict(self.draft_rule),
        }


def _confidence(unproven_count: int, total: int) -> str:
    """Confidence is a count of answered questions, not a feeling.

    Anything with an open question is ``low``: a candidate that is 80% proven still
    produces a wrong invoice if the remaining 20% is "does the platform actually
    charge this".
    """
    if total == 0:
        return "unqualified"
    if unproven_count == 0:
        return "high"
    if unproven_count < total:
        return "low"
    return "unqualified"


def propose_from_presets(
    presets: Sequence[PresetVersion],
    *,
    jurisdiction_codes: Sequence[str] = (),
    hierarchy_version_id: str | None = None,
    on_date: date | None = None,
) -> list[PresetProposal]:
    """Narrow presets to candidates for one Project's geography, with their gaps.

    ``jurisdiction_codes`` NARROWS. It never proves: a proposal for a country the
    Project reports on still carries every unproven qualification, and its draft
    rule is inert until an operator supplies what is missing.

    A jurisdiction-bearing preset with no ``hierarchy_version_id`` produces a draft
    rule whose ``jurisdiction`` is deliberately incomplete, so publishing it fails
    the ladder profile rather than pinning nothing (AC5).
    """

    codes = {str(code).upper() for code in jurisdiction_codes}
    proposals: list[PresetProposal] = []
    for preset in presets:
        if preset.status != "published":
            continue
        if on_date is not None:
            if preset.effective_from > on_date:
                continue
            if preset.effective_to is not None and preset.effective_to < on_date:
                continue
        matched: list[str] = []
        if preset.jurisdiction_kind == "none":
            matched.append("jurisdiction_independent")
        elif preset.jurisdiction_code and preset.jurisdiction_code.upper() in codes:
            matched.append(f"jurisdiction:{preset.jurisdiction_code}")
        else:
            continue

        unproven = tuple(item for item in preset.qualifications if not item.get("proven"))
        proposals.append(
            PresetProposal(
                preset=preset,
                matched_on=tuple(matched),
                unproven=unproven,
                confidence=_confidence(len(unproven), len(preset.qualifications)),
                draft_rule=_draft_rule(preset, hierarchy_version_id=hierarchy_version_id),
            )
        )
    return proposals


def _draft_rule(preset: PresetVersion, *, hierarchy_version_id: str | None) -> dict[str, Any]:
    """Shape a preset as a ladder rule an operator can edit and adopt.

    The geographic postures are set to ``unresolved`` rather than guessed. That is
    the one honest default: shared knowledge cannot know whether a Project wants a
    French DST rule to include or exclude the rest of the world, and picking one
    silently is the "catch-all inferred from missing membership" AC5 forbids.
    """

    geographic = preset.jurisdiction_kind != "none"
    rule: dict[str, Any] = {
        "rule_key": preset.preset_key,
        "label": preset.label,
        "scope_kind": "project",
        "scope_ref": None,
        "category": preset.category,
        "form": preset.form,
        "base_target": preset.base_target,
        "cascade_phase": _CASCADE_PHASE_BY_CATEGORY.get(preset.category),
        "sequence_order": 0,
        "money_basis": "native_source",
        "effective_from": preset.effective_from.isoformat(),
        "effective_to": preset.effective_to.isoformat() if preset.effective_to else None,
        "authority_kind": "statutory_reference",
        "origin": "auto_country" if geographic else "operator",
        "source_evidence": {
            "issuer": preset.issuer,
            "reference": preset.source_reference,
            "reference_version": preset.source_reference_version,
            "authoritative_url": preset.authoritative_url,
            "document_ref": preset.document_ref,
            "preset_version_id": preset.id,
            "published_on": (preset.published_on.isoformat() if preset.published_on else None),
            "assumptions": list(preset.assumptions),
        },
    }
    if preset.rate is not None:
        rule["rate"] = str(preset.rate)
    if preset.amount_micros is not None:
        rule["amount_micros"] = preset.amount_micros
    if preset.cpm_micros is not None:
        rule["cpm_micros"] = preset.cpm_micros
    if preset.currency:
        rule["currency"] = preset.currency
    if geographic:
        rule["conditions"] = {preset.jurisdiction_kind: [preset.jurisdiction_code]}
        rule["jurisdiction"] = {
            "kind": preset.jurisdiction_kind,
            "id": None,
            "hierarchy_version_id": hierarchy_version_id,
        }
        rule["rest_of_world_posture"] = "unresolved"
        rule["unknown_posture"] = "unresolved"
    return rule


#: Where each seed category sits in the E41-FR03 ladder. Copied from Story 41.2's
#: own table rather than re-derived, because the two must agree or a migrated rule
#: composes in a different order than the one it composed in before.
_CASCADE_PHASE_BY_CATEGORY: dict[str, int] = {
    "PLATFORM_FEE": 2,
    "REGULATORY_TAX": 3,
    "WHT_GROSS_UP": 4,
    "AGENCY_FEE": 5,
    "SALES_TAX": 6,
}


# ---------------------------------------------------------------------------
# The seed importer. Preserves the notes; supplies nothing it does not have.
# ---------------------------------------------------------------------------

#: The questions the dbt seed's own rows raise and cannot answer. Attached to every
#: imported candidate so the import cannot look more complete than the source.
_SEED_QUALIFICATIONS: tuple[dict[str, Any], ...] = (
    {
        "code": "provider_passes_through",
        "question": (
            "Does this platform actually pass this tax through on its invoice to you, "
            "and on which line?"
        ),
    },
    {
        "code": "taxable_service_matches",
        "question": "Is the service you buy inside the scope this tax applies to?",
    },
    {
        "code": "threshold_met",
        "question": (
            "Are the activity and revenue thresholds this tax depends on met by the "
            "party that would charge it?"
        ),
    },
)


def seed_rows_as_preset_payloads(
    rows: Sequence[Any], *, source_reference_version: str
) -> list[dict[str, Any]]:
    """Turn ``country_tax_defaults.csv`` rows into DRAFT preset payloads.

    The seed's ``source_note`` -- the sentence that says "confirm against your
    platform invoice and your tax advisor before use" -- is carried into
    ``assumptions`` verbatim. Story 41.2's auto-population dropped it before
    persistence, so the one honest caveat the platform shipped never reached the
    operator reading the rule.

    Draft, not published: an imported row is a reference candidate. Publishing it
    is a decision someone makes after reading it.
    """

    payloads: list[dict[str, Any]] = []
    for row in rows:
        iso = str(getattr(row, "iso_code", "")).upper()
        category = str(getattr(row, "tax_category", ""))
        note = str(getattr(row, "source_note", "")).strip()
        if not note:
            # The loader already enforces this; refusing again here means an import
            # can never be the path that loses the caveat.
            raise PresetError(f"seed row {iso} has no source_note to preserve")
        payloads.append(
            {
                "preset_key": f"seed_{category.lower()}_{iso.lower()}",
                "label": str(getattr(row, "label", "")) or f"{iso} {category}",
                "issuer": "toorow shared reference (unattributed seed row)",
                "source_reference": "dbt/seeds/country_tax_defaults.csv",
                "source_reference_version": source_reference_version,
                "jurisdiction_kind": "country",
                "jurisdiction_code": iso,
                "taxable_subject": (
                    "Unspecified by the seed: the row carries a headline rate, not a "
                    "taxable subject."
                ),
                "category": category,
                "form": str(getattr(row, "form", "PERCENTAGE")),
                "rate": getattr(row, "rate", None),
                "base_target": _SEED_BASE_TARGET_BY_CATEGORY.get(category, "NET_MEDIA"),
                "qualifications": [dict(item) for item in _SEED_QUALIFICATIONS],
                "assumptions": [note],
                "effective_from": getattr(row, "effective_from", None),
                "verification_status": "unverified",
            }
        )
    return payloads


_SEED_BASE_TARGET_BY_CATEGORY: dict[str, str] = {
    "REGULATORY_TAX": "NET_MEDIA",
    "SALES_TAX": "RUNNING_SUBTOTAL",
}


# ---------------------------------------------------------------------------
# Persistence.
# ---------------------------------------------------------------------------

_COLUMNS = (
    "id",
    "preset_key",
    "version_number",
    "status",
    "label",
    "issuer",
    "source_reference",
    "source_reference_version",
    "authoritative_url",
    "document_ref",
    "jurisdiction_kind",
    "jurisdiction_code",
    "taxable_subject",
    "service_scope",
    "category",
    "form",
    "rate",
    "amount_micros",
    "cpm_micros",
    "currency",
    "base_target",
    "thresholds",
    "qualifications",
    "assumptions",
    "published_on",
    "effective_from",
    "effective_to",
    "last_verified_on",
    "verification_status",
    "content_hash",
)


def record_preset_version(
    conn, *, payload: Mapping[str, Any], org_id: str | None, actor: str, publish: bool = False
) -> str:
    """Persist one preset version. Returns its id.

    Idempotent by content: an identical preset returns the stored row rather than
    minting a rival version saying the same thing.
    """

    from ulid import ULID  # noqa: PLC0415

    normalized = validate_preset(payload)
    digest = content_hash({**normalized, "org_id": org_id})
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.tax_fee_preset_versions "
            "WHERE COALESCE(org_id, '') = COALESCE(%s, '') AND content_hash = %s",
            (org_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        cur.execute(
            "SELECT COALESCE(MAX(version_number), 0) + 1 FROM app.tax_fee_preset_versions "
            "WHERE COALESCE(org_id, '') = COALESCE(%s, '') AND preset_key = %s",
            (org_id, normalized["preset_key"]),
        )
        version_number = int(cur.fetchone()[0])
        preset_id = f"tfpv_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.tax_fee_preset_versions
                (id, org_id, preset_key, version_number, status, label, issuer,
                 source_reference, source_reference_version, authoritative_url,
                 document_ref, jurisdiction_kind, jurisdiction_code, taxable_subject,
                 service_scope, category, form, rate, amount_micros, cpm_micros,
                 currency, base_target, thresholds, qualifications, assumptions,
                 published_on, effective_from, effective_to, last_verified_on,
                 verification_status, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s, %s, %s,
                    %s, %s, %s)
            """,
            (
                preset_id,
                org_id,
                normalized["preset_key"],
                version_number,
                "published" if publish else "draft",
                normalized["label"],
                normalized["issuer"],
                normalized["source_reference"],
                normalized["source_reference_version"],
                normalized["authoritative_url"],
                normalized["document_ref"],
                normalized["jurisdiction_kind"],
                normalized["jurisdiction_code"],
                normalized["taxable_subject"],
                normalized["service_scope"],
                normalized["category"],
                normalized["form"],
                normalized["rate"],
                normalized["amount_micros"],
                normalized["cpm_micros"],
                normalized["currency"],
                normalized["base_target"],
                canonical_json(normalized["thresholds"]),
                canonical_json(normalized["qualifications"]),
                canonical_json(normalized["assumptions"]),
                normalized["published_on"],
                normalized["effective_from"],
                normalized["effective_to"],
                normalized["last_verified_on"],
                normalized["verification_status"],
                digest,
                actor,
            ),
        )
    return preset_id


def list_preset_versions(
    conn, *, org_id: str | None, status: str = "published"
) -> list[PresetVersion]:
    """Presets visible to one organization: its own, plus platform-shared rows."""

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.tax_fee_preset_versions "
            "WHERE (org_id IS NULL OR org_id = %s) AND status = %s "
            "ORDER BY jurisdiction_code NULLS FIRST, preset_key, version_number DESC",
            (org_id, status),
        )
        return [_preset(row) for row in cur.fetchall()]


def _preset(row: Sequence[Any]) -> PresetVersion:
    record = dict(zip(_COLUMNS, row))
    return PresetVersion(
        id=str(record["id"]),
        preset_key=str(record["preset_key"]),
        version_number=int(record["version_number"]),
        status=str(record["status"]),
        label=str(record["label"]),
        issuer=str(record["issuer"]),
        source_reference=str(record["source_reference"]),
        source_reference_version=str(record["source_reference_version"]),
        authoritative_url=record["authoritative_url"],
        document_ref=record["document_ref"],
        jurisdiction_kind=str(record["jurisdiction_kind"]),
        jurisdiction_code=record["jurisdiction_code"],
        taxable_subject=str(record["taxable_subject"]),
        service_scope=record["service_scope"],
        category=str(record["category"]),
        form=str(record["form"]),
        rate=record["rate"],
        amount_micros=record["amount_micros"],
        cpm_micros=record["cpm_micros"],
        currency=record["currency"],
        base_target=str(record["base_target"]),
        thresholds=tuple(record["thresholds"] or ()),
        qualifications=tuple(record["qualifications"] or ()),
        assumptions=tuple(record["assumptions"] or ()),
        published_on=record["published_on"],
        effective_from=record["effective_from"],
        effective_to=record["effective_to"],
        last_verified_on=record["last_verified_on"],
        verification_status=str(record["verification_status"]),
        content_hash=str(record["content_hash"]),
    )
