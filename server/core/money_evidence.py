"""Observed per-publication money evidence (Story 48.3, AC3 and AC8).

The money half of what :mod:`core.time_boundary` does for day boundaries, and it
exists for the same reason: a Connector descriptor and a mapping state what a
Datastream is SUPPOSED to publish; only a publication states what it DID.

``CurrencyFxCompiler`` used to answer "is this Datastream's money governed?" from
a mapping alone. A mapping can bind a monetary field and the pull can still land
rows with no currency column, or two currencies where one was expected, or a
value whose unit nobody recorded -- and each of those is a different repair.
Coverage compiled without reading a publication is a guess formatted as a verdict.

One row per (Datastream, execution, content). ``observed_fields`` records what the
rows actually carried; ``gaps`` records every field whose currency, unit or exact
adapter could not be established. A gap is retained, never repaired into a default:
that is the difference between "we do not know" and "we assumed euros".
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core.governance_rule_sets import canonical_json, content_hash

logger = logging.getLogger(__name__)

GAP_NO_CURRENCY = "native_currency_missing"
GAP_NO_UNIT = "native_unit_missing"
GAP_NO_ADAPTER = "money_adapter_missing"
GAP_MIXED_CURRENCY = "multiple_native_currencies_in_one_field"


@dataclass(frozen=True, slots=True)
class MoneyEvidence:
    """What one publication actually showed about its monetary fields."""

    datastream_id: str
    execution_id: str | None
    observed_fields: tuple[dict[str, Any], ...]
    gaps: tuple[dict[str, Any], ...]
    confidence: str
    assumptions: tuple[dict[str, Any], ...]
    source_lever: Mapping[str, Any]

    @property
    def native_currencies(self) -> set[str]:
        found: set[str] = set()
        for field in self.observed_fields:
            for code in field.get("distinct_currencies") or []:
                if code:
                    found.add(str(code))
            if field.get("native_currency"):
                found.add(str(field["native_currency"]))
        return found

    def needs_conversion(self, reporting_currency: str) -> bool:
        """True when at least one observed currency differs from the reporting one.

        A Datastream publishing only the reporting currency needs no rate at all,
        so a missing Rate Set is not a gap for it. Asking this question is what
        keeps the compiler from blocking a Project that never converts anything.
        """
        return any(code != reporting_currency for code in self.native_currencies)

    def as_payload(self) -> dict[str, Any]:
        return {
            "datastream_id": self.datastream_id,
            "execution_id": self.execution_id,
            "observed_fields": [dict(item) for item in self.observed_fields],
            "gaps": [dict(item) for item in self.gaps],
            "confidence": self.confidence,
            "assumptions": [dict(item) for item in self.assumptions],
            "source_lever": dict(self.source_lever),
            "native_currencies": sorted(self.native_currencies),
        }


def classify_gaps(observed_fields: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Turn observed field facts into typed gaps. Pure, so it is testable offline.

    Each gap names a DIFFERENT missing thing, because "money is broken here" is not
    actionable and "this field published two currencies in one column" is.
    """

    gaps: list[dict[str, Any]] = []
    for field in observed_fields:
        name = str(field.get("canonical_field") or "unknown")
        distinct = [str(code) for code in (field.get("distinct_currencies") or []) if code]
        if not field.get("native_currency") and not distinct:
            gaps.append(
                {
                    "code": GAP_NO_CURRENCY,
                    "canonical_field": name,
                    "message": (
                        f"{name} published no source currency, so its values cannot be "
                        "compared or summed."
                    ),
                }
            )
        elif len(set(distinct)) > 1:
            gaps.append(
                {
                    "code": GAP_MIXED_CURRENCY,
                    "canonical_field": name,
                    "currencies": sorted(set(distinct)),
                    "message": (
                        f"{name} published {len(set(distinct))} different source currencies "
                        "in one field; each row must carry its own currency for a total to "
                        "be safe."
                    ),
                }
            )
        if not field.get("native_unit"):
            gaps.append(
                {
                    "code": GAP_NO_UNIT,
                    "canonical_field": name,
                    "message": f"{name} published no native unit, so its magnitude is unknown.",
                }
            )
        if not field.get("adapter"):
            gaps.append(
                {
                    "code": GAP_NO_ADAPTER,
                    "canonical_field": name,
                    "message": (
                        f"{name} records no exact money adapter, so the unit it claims cannot "
                        "be trusted."
                    ),
                }
            )
    return gaps


def record_money_evidence(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str | None,
    observed_fields: Sequence[Mapping[str, Any]],
    plan_version_id: str | None = None,
    mapping_version_id: str | None = None,
    confidence: str = "observed",
    assumptions: Sequence[Mapping[str, Any]] = (),
    source_lever: Mapping[str, Any] | None = None,
) -> str:
    """Persist one publication's observed money evidence. Returns its id.

    Idempotent by content, like every other evidence writer here: re-observing an
    identical publication returns the stored row instead of growing the history
    with duplicates that say the same thing.
    """

    from ulid import ULID  # noqa: PLC0415

    fields = [dict(item) for item in observed_fields]
    fields.sort(key=lambda item: str(item.get("canonical_field") or ""))
    gaps = classify_gaps(fields)
    lever = dict(source_lever or {"available": False})

    digest = content_hash(
        {
            "datastream_id": datastream_id,
            "execution_id": execution_id,
            "fields": fields,
            "lever": lever,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastream_money_evidence "
            "WHERE datastream_id = %s AND execution_id IS NOT DISTINCT FROM %s "
            "AND content_hash = %s",
            (datastream_id, execution_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        evidence_id = f"dsme_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.datastream_money_evidence
                (id, project_id, datastream_id, execution_id, plan_version_id,
                 mapping_version_id, observed_fields, gaps, confidence, assumptions,
                 source_lever, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s)
            """,
            (
                evidence_id,
                project_id,
                datastream_id,
                execution_id,
                plan_version_id,
                mapping_version_id,
                canonical_json(fields),
                canonical_json(gaps),
                confidence,
                canonical_json([dict(item) for item in assumptions]),
                canonical_json(lever),
                digest,
            ),
        )
    return evidence_id


_COLUMNS = (
    "datastream_id",
    "execution_id",
    "observed_fields",
    "gaps",
    "confidence",
    "assumptions",
    "source_lever",
)


def latest_money_evidence(
    conn, *, project_id: str, datastream_id: str
) -> MoneyEvidence | None:
    """The most recent observation for one Datastream, or ``None`` if never observed.

    ``None`` means "no publication has proved this yet". The compiler reads that as
    ``unavailable`` coverage with a repair route -- never as a healthy default.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_COLUMNS)} FROM app.datastream_money_evidence "
            "WHERE project_id = %s AND datastream_id = %s ORDER BY observed_at DESC LIMIT 1",
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    return _evidence(row) if row else None


def money_evidence_for_project(conn, *, project_id: str) -> dict[str, MoneyEvidence]:
    """The latest observation per Datastream, in one query rather than one per stream."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (datastream_id) {', '.join(_COLUMNS)}
            FROM app.datastream_money_evidence
            WHERE project_id = %s
            ORDER BY datastream_id, observed_at DESC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return {str(row[0]): _evidence(row) for row in rows}


def _evidence(row: Sequence[Any]) -> MoneyEvidence:
    record = dict(zip(_COLUMNS, row))
    return MoneyEvidence(
        datastream_id=str(record["datastream_id"]),
        execution_id=record["execution_id"],
        observed_fields=tuple(record["observed_fields"] or ()),
        gaps=tuple(record["gaps"] or ()),
        confidence=str(record["confidence"]),
        assumptions=tuple(record["assumptions"] or ()),
        source_lever=record["source_lever"] or {},
    )
