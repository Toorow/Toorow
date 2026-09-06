"""Observed per-publication Tax evidence (Story 48.4, AC4).

The tax half of what :mod:`core.money_evidence` does for currency, and it exists
for the same reason: a Connector contract and a mapping state what a Datastream is
SUPPOSED to publish; only a publication states what it DID.

The specific defect it replaces is worse than a guess. ``app.datastream_source_types``
(migration 119) was created with no production writer, so the table stayed empty --
and ``TaxFeesCompiler`` read that emptiness as "this Datastream declares no source
type", which its scope filter then treated as matching every scoped rule. A ladder
scoped to ``PAID_MEDIA`` therefore applied to a Shopify revenue stream, silently,
and coverage reported ``complete``.

Three distinctions this module keeps that the single column could not:

* ``UNKNOWN`` is a TYPED GAP, not a value. AC4: "``UNKNOWN`` remains a typed gap,
  not ``PAID_MEDIA`` or known-false." A rule cannot match against it and a
  proposal that meets it says ``unresolved``, never ``not applicable``.
* A source type carries its ORIGIN and CONFIDENCE. A Connector contract may
  PROPOSE ``PAID_MEDIA``; an operator override CONFIRMS it; an observed mapping
  INFERS it. Three different strengths of the same sentence, and only the second
  should silence a review.
* Tax posture -- whether a revenue figure is inclusive or exclusive of tax -- is
  observed per publication, never assumed from the Connector's identity. AC7 makes
  ``unknown`` BLOCK HT/TTC normalization rather than default to exclusive, because
  defaulting is what turned two incompatible numbers into a plausible ROAS.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

SOURCE_TYPES: tuple[str, ...] = (
    "PAID_MEDIA",
    "LEAD_GEN_MEDIA",
    "DIRECT_SERVICE_COST",
    "COMMERCE_REVENUE",
    "ORGANIC_ANALYTICS",
    "UNKNOWN",
)
SOURCE_TYPE_ORIGINS: tuple[str, ...] = (
    "connector_contract",
    "observed_mapping",
    "operator_override",
    "unresolved",
)
CONFIDENCES: tuple[str, ...] = ("high", "medium", "low", "none")
TAX_POSTURES: tuple[str, ...] = ("inclusive", "exclusive", "not_applicable", "unknown")
TAX_POSTURE_ORIGINS: tuple[str, ...] = ("observed_field", "operator_override", "unresolved")
APPLICABILITY_STATES: tuple[str, ...] = (
    "applicable",
    "not_applicable",
    "unavailable",
    "excluded",
    "unresolved",
)

GAP_SOURCE_TYPE_UNRESOLVED = "source_type_unresolved"
GAP_TAX_POSTURE_UNKNOWN = "tax_posture_unknown"
GAP_NO_NATIVE_MONEY = "native_money_unobserved"
GAP_GEOGRAPHY_UNOBSERVED = "geography_unobserved"

_PULL_INPUT_KEYS: tuple[str, ...] = (
    "native_amount_micros",
    "native_currency",
    "measured_impressions",
    "transaction_count",
    "tax_code",
    "placement_type",
)
_PULL_GEOGRAPHY_KEYS: tuple[str, ...] = ("country_field", "market_field")


class TaxEvidenceError(ValueError):
    """A recorded observation violates the evidence contract."""

    code = "invalid_tax_evidence"


@dataclass(frozen=True, slots=True)
class TaxEvidence:
    """What one publication actually showed about this Datastream's tax facts."""

    evidence_version_id: str
    datastream_id: str
    execution_id: str | None
    source_type: str
    source_type_origin: str
    source_type_confidence: str
    tax_posture: str
    tax_posture_origin: str
    applicability: str
    observed_inputs: Mapping[str, Any]
    geography_evidence: Mapping[str, Any]
    gaps: tuple[dict[str, Any], ...]

    @property
    def available_inputs(self) -> frozenset[str]:
        """The physical inputs this publication actually landed.

        A key present with a falsy value counts as ABSENT. ``measured_impressions:
        0`` from a source that reports no impressions and a missing key are the same
        thing for a CPM rule -- and treating the first as available is exactly how a
        verification fee becomes a confident zero.
        """
        return frozenset(
            key for key, value in self.observed_inputs.items() if value not in (None, False, "", [])
        )

    @property
    def source_type_is_proved(self) -> bool:
        """Whether the source type may be relied on to REFUSE a rule.

        An inferred type is enough to propose; only a confirmed or contracted one is
        enough to conclude "not applicable", which is a verdict rather than a guess.
        """
        return self.source_type != "UNKNOWN" and self.source_type_origin in {
            "connector_contract",
            "operator_override",
        }

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_version_id": self.evidence_version_id,
            "datastream_id": self.datastream_id,
            "execution_id": self.execution_id,
            "source_type": self.source_type,
            "source_type_origin": self.source_type_origin,
            "source_type_confidence": self.source_type_confidence,
            "tax_posture": self.tax_posture,
            "tax_posture_origin": self.tax_posture_origin,
            "applicability": self.applicability,
            "observed_inputs": dict(self.observed_inputs),
            "geography_evidence": dict(self.geography_evidence),
            "gaps": [dict(gap) for gap in self.gaps],
            "available_inputs": sorted(self.available_inputs),
        }


def classify_gaps(
    *,
    source_type: str,
    source_type_origin: str,
    tax_posture: str,
    observed_inputs: Mapping[str, Any],
    geography_evidence: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Turn observed facts into TYPED gaps. Pure, so it is testable offline.

    Each gap names a different missing thing, because "tax is broken here" is not
    actionable and "this publication never showed whether its revenue includes tax"
    is.
    """

    gaps: list[dict[str, Any]] = []
    if source_type == "UNKNOWN" or source_type_origin == "unresolved":
        gaps.append(
            {
                "code": GAP_SOURCE_TYPE_UNRESOLVED,
                "message": (
                    "No publication or operator has established what kind of source this "
                    "is, so a source-scoped rule can neither match nor be refused."
                ),
            }
        )
    if tax_posture == "unknown":
        gaps.append(
            {
                "code": GAP_TAX_POSTURE_UNKNOWN,
                "message": (
                    "Whether these values include tax was never observed, so they cannot "
                    "be normalized to HT or compared with a TTC figure."
                ),
            }
        )
    if not observed_inputs.get("native_amount_micros") and not observed_inputs.get(
        "native_currency"
    ):
        gaps.append(
            {
                "code": GAP_NO_NATIVE_MONEY,
                "message": (
                    "This publication landed no native monetary value, so no component "
                    "has a base to be computed from."
                ),
            }
        )
    if not geography_evidence.get("country_field") and not geography_evidence.get("market_field"):
        gaps.append(
            {
                "code": GAP_GEOGRAPHY_UNOBSERVED,
                "message": (
                    "No country or market field was published, so a geography-dependent "
                    "rule reads Unknown for every row."
                ),
            }
        )
    return gaps


def _one_of(value: Any, allowed: Sequence[str], label: str) -> str:
    text = str(value or "").strip()
    if text not in allowed:
        raise TaxEvidenceError(f"{label} must be one of {list(allowed)}, not {text!r}")
    return text


def record_tax_evidence(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str | None,
    source_type: str = "UNKNOWN",
    source_type_origin: str = "unresolved",
    source_type_confidence: str = "none",
    tax_posture: str = "unknown",
    tax_posture_origin: str = "unresolved",
    observed_inputs: Mapping[str, Any] | None = None,
    geography_evidence: Mapping[str, Any] | None = None,
    plan_version_id: str | None = None,
    mapping_version_id: str | None = None,
    publication_id: str | None = None,
    recorded_by: str = "system",
) -> str:
    """Persist one publication's observed Tax evidence. Returns its id.

    Applicability is DERIVED here, never accepted from the caller: it is a verdict
    over the gaps, and letting a writer state it would let a Connector declare
    itself applicable. Idempotent by content, like every other evidence writer:
    re-observing an identical publication returns the stored row rather than
    growing the history with rows that say the same thing.
    """

    from ulid import ULID  # noqa: PLC0415

    source_type = _one_of(source_type, SOURCE_TYPES, "source_type")
    source_type_origin = _one_of(source_type_origin, SOURCE_TYPE_ORIGINS, "source_type_origin")
    source_type_confidence = _one_of(source_type_confidence, CONFIDENCES, "source_type_confidence")
    tax_posture = _one_of(tax_posture, TAX_POSTURES, "tax_posture")
    tax_posture_origin = _one_of(tax_posture_origin, TAX_POSTURE_ORIGINS, "tax_posture_origin")

    inputs = dict(observed_inputs or {})
    geography = dict(geography_evidence or {})
    gaps = classify_gaps(
        source_type=source_type,
        source_type_origin=source_type_origin,
        tax_posture=tax_posture,
        observed_inputs=inputs,
        geography_evidence=geography,
    )
    applicability = derive_applicability(
        source_type=source_type, source_type_origin=source_type_origin, gaps=gaps
    )

    digest = content_hash(
        {
            "datastream_id": datastream_id,
            "execution_id": execution_id,
            "source_type": source_type,
            "source_type_origin": source_type_origin,
            "tax_posture": tax_posture,
            "inputs": inputs,
            "geography": geography,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastream_tax_evidence "
            "WHERE datastream_id = %s AND content_hash = %s",
            (datastream_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        evidence_id = f"dste_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.datastream_tax_evidence
                (id, project_id, datastream_id, execution_id, source_type,
                 source_type_origin, source_type_confidence, tax_posture,
                 tax_posture_origin, applicability, observed_inputs,
                 geography_evidence, gaps, plan_version_id, mapping_version_id,
                 publication_id, content_hash, recorded_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s, %s, %s, %s, %s)
            """,
            (
                evidence_id,
                project_id,
                datastream_id,
                execution_id,
                source_type,
                source_type_origin,
                source_type_confidence,
                tax_posture,
                tax_posture_origin,
                applicability,
                canonical_json(inputs),
                canonical_json(geography),
                canonical_json(gaps),
                plan_version_id,
                mapping_version_id,
                publication_id,
                digest,
                recorded_by,
            ),
        )
    return evidence_id


def record_tax_evidence_for_pull(
    conn,
    *,
    project_id: str,
    datastream: Mapping[str, Any],
    pull_id: str,
    pull_result: Mapping[str, Any] | None,
    recorded_by: str = "system",
) -> str:
    """Record the Tax facts explicitly returned by one successful pull.

    This is the production bridge between extraction and the immutable evidence
    history.  It deliberately does not infer money, geography or tax posture from a
    connector name: a missing returned field becomes a typed gap.  Source type is the
    sole exception because it already has a governed declaration/derivation ladder.
    """
    from core import fee_tax_source_types as source_types  # noqa: PLC0415

    datastream_id = str(datastream.get("id") or "").strip()
    if not datastream_id:
        raise TaxEvidenceError("datastream.id is required to record pull evidence")

    resolved = source_types.resolve_source_type(datastream_id, project_id, conn)
    if resolved.source == source_types.RESOLVED_BY_DECLARATION:
        source_origin, source_confidence = "operator_override", "high"
    elif resolved.source == source_types.RESOLVED_BY_DERIVATION:
        source_origin, source_confidence = "observed_mapping", "medium"
    else:
        source_origin, source_confidence = "unresolved", "none"

    result = dict(pull_result or {})
    declared = result.get("tax_evidence")
    payload = dict(declared) if isinstance(declared, Mapping) else result

    nested_inputs = payload.get("observed_inputs")
    input_source = nested_inputs if isinstance(nested_inputs, Mapping) else payload
    observed_inputs = {
        key: input_source[key]
        for key in _PULL_INPUT_KEYS
        if key in input_source and input_source[key] not in (None, "")
    }

    nested_geography = payload.get("geography_evidence")
    geography_source = (
        nested_geography if isinstance(nested_geography, Mapping) else payload
    )
    geography_evidence = {
        key: geography_source[key]
        for key in _PULL_GEOGRAPHY_KEYS
        if key in geography_source and geography_source[key] not in (None, "")
    }

    posture = str(payload.get("tax_posture") or "unknown").strip()
    if posture not in TAX_POSTURES:
        posture = "unknown"
    posture_origin = "observed_field" if posture != "unknown" else "unresolved"

    return record_tax_evidence(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        execution_id=pull_id,
        source_type=resolved.value,
        source_type_origin=source_origin,
        source_type_confidence=source_confidence,
        tax_posture=posture,
        tax_posture_origin=posture_origin,
        observed_inputs=observed_inputs,
        geography_evidence=geography_evidence,
        plan_version_id=(
            str(datastream["current_plan_version_id"])
            if datastream.get("current_plan_version_id")
            else None
        ),
        mapping_version_id=(
            str(datastream["current_mapping_version_id"])
            if datastream.get("current_mapping_version_id")
            else None
        ),
        recorded_by=recorded_by,
    )


def derive_applicability(
    *, source_type: str, source_type_origin: str, gaps: Sequence[Mapping[str, Any]]
) -> str:
    """The five-state verdict over observed evidence (AC4). Pure.

    ``ORGANIC_ANALYTICS`` is the only state that can conclude ``not_applicable`` on
    its own: a stream carrying no money and no revenue has nothing for a fee or tax
    ladder to compose. Every other absence is ``unresolved`` or ``unavailable``,
    because AC8 is explicit that a rule is Not applicable "only when sufficient
    evidence proves that conclusion" -- and a missing fact proves nothing.
    """
    codes = {str(gap.get("code")) for gap in gaps}
    if source_type == "UNKNOWN" or source_type_origin == "unresolved":
        return "unresolved"
    if source_type == "ORGANIC_ANALYTICS" and source_type_origin in {
        "connector_contract",
        "operator_override",
    }:
        return "not_applicable"
    if GAP_NO_NATIVE_MONEY in codes:
        return "unavailable"
    return "applicable"


_COLUMNS = (
    "id",
    "datastream_id",
    "execution_id",
    "source_type",
    "source_type_origin",
    "source_type_confidence",
    "tax_posture",
    "tax_posture_origin",
    "applicability",
    "observed_inputs",
    "geography_evidence",
    "gaps",
)


def latest_tax_evidence(conn, *, project_id: str, datastream_id: str) -> TaxEvidence | None:
    """The most recent observation for one Datastream, or ``None`` if never observed.

    ``None`` means "no publication has proved this yet", which the compiler reads as
    ``unavailable`` coverage with a repair route -- never as a healthy default.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.datastream_tax_evidence "
            "WHERE project_id = %s AND datastream_id = %s ORDER BY created_at DESC LIMIT 1",
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    return _evidence(row) if row else None


def tax_evidence_for_project(conn, *, project_id: str) -> dict[str, TaxEvidence]:
    """The latest observation per Datastream, in one query rather than one per stream."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (datastream_id) {', '.join(_COLUMNS)}
            FROM app.datastream_tax_evidence
            WHERE project_id = %s
            ORDER BY datastream_id, created_at DESC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return {str(row[1]): _evidence(row) for row in rows}


def _evidence(row: Sequence[Any]) -> TaxEvidence:
    record = dict(zip(_COLUMNS, row))
    return TaxEvidence(
        evidence_version_id=str(record["id"]),
        datastream_id=str(record["datastream_id"]),
        execution_id=record["execution_id"],
        source_type=str(record["source_type"]),
        source_type_origin=str(record["source_type_origin"]),
        source_type_confidence=str(record["source_type_confidence"]),
        tax_posture=str(record["tax_posture"]),
        tax_posture_origin=str(record["tax_posture_origin"]),
        applicability=str(record["applicability"]),
        observed_inputs=record["observed_inputs"] or {},
        geography_evidence=record["geography_evidence"] or {},
        gaps=tuple(record["gaps"] or ()),
    )
