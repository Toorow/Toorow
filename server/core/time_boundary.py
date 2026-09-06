"""Observed day-boundary evidence, and the only place a reporting date is derived.

Story 48.3, AC6 and AC7. Three things were missing and one was wrong.

Missing:

* **Observed evidence.** :mod:`core.report_timezone` resolves a Datastream's
  boundary from its Connector's ``time_context`` *declaration*. A declaration
  states what a source CAN do; it cannot state what a pull DID. Coverage compiled
  from it was therefore an assumption wearing the costume of a measurement, which
  is exactly what "the compiler never marks coverage complete from the descriptor
  alone" forbids.
* **A reporting-date derivation that can be reproduced.** Time zone rules change
  several times a year. A ``reporting_date`` derived under tzdb ``2024a`` and one
  derived under ``2026c`` are not necessarily the same date, and neither is
  reproducible unless the derivation records which release it used, alongside the
  timezone policy version and the source zone.
* **The DATE / timestamp distinction, enforced.** A source ``DATE`` with no
  timestamp cannot be re-sliced onto another day: there is no sub-day data to
  re-slice. :func:`derive_reporting_date` refuses it structurally rather than by
  convention.

Wrong: :func:`core.timezone_signal.check_cross_source_day_offset` EXCLUDED streams
whose boundary was unknown. With two known streams agreeing and three unknown, it
returned "no offset" -- an incomplete comparison rendered as a healthy one.
:func:`compare_boundaries` here keeps the unknowns and reports them as typed gaps,
so the answer is "these two agree and three cannot be placed", which is the truth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from core.governance_rule_sets import canonical_json, content_hash
from core.money_policy import TimezonePolicy

logger = logging.getLogger(__name__)

# Typed gap codes. Each names a different missing thing.
GAP_NO_EVIDENCE = "time_boundary_unobserved"
GAP_NO_SOURCE_ZONE = "source_report_timezone_unknown"
GAP_DATE_ONLY = "date_grain_not_realignable"
GAP_DST_GAP = "reporting_time_in_dst_gap"
GAP_TZDB_MISMATCH = "tzdb_version_mismatch"

# Not a GAP -- nothing is missing. Every source was placed, and they disagree.
# Named here rather than left a literal (AI-167) because `core.timezone_signal`
# reports the same finding on the live path, and two spellings of one verdict is
# how a consumer comes to handle one of them.
SIGNAL_BOUNDARY_DIFFERS = "cross_source_day_boundary_differs"

GRAIN_DATE_ONLY = "date_only"
GRAIN_TIMESTAMP = "timestamp"
GRAIN_UNKNOWN = "unknown"

SUFFICIENT = "sufficient"
INSUFFICIENT_NO_TIMESTAMP = "insufficient_no_timestamp"
INSUFFICIENT_NO_SOURCE_ZONE = "insufficient_no_source_zone"
SUFFICIENCY_UNKNOWN = "unknown"

ORIGIN_PULL_METADATA = "pull_metadata"
ORIGIN_PUBLICATION_PROBE = "publication_probe"
ORIGIN_DECLARATION = "declaration"
ORIGIN_OPERATOR = "operator_statement"
ORIGIN_NONE = "none"

#: Evidence origins that may support a `complete` coverage verdict. A declaration
#: is deliberately absent: that is the whole point of AC6.
OBSERVED_ORIGINS = frozenset({ORIGIN_PULL_METADATA, ORIGIN_PUBLICATION_PROBE})


@dataclass(frozen=True, slots=True)
class BoundaryEvidence:
    """What one publication actually showed about its day boundary."""

    datastream_id: str
    execution_id: str | None
    grain: str
    timestamp_sufficiency: str
    observed_report_timezone: str | None
    evidence_origin: str
    confidence: str
    assumed: bool
    assumptions: tuple[dict[str, Any], ...]
    adjustment_lever: Mapping[str, Any]
    tzdb_version: str | None
    gap_code: str | None
    observed_at: datetime | None = None

    @property
    def is_observed(self) -> bool:
        """True only when a pull or a publication proved this, not a descriptor."""
        return self.evidence_origin in OBSERVED_ORIGINS

    @property
    def can_derive_reporting_date(self) -> bool:
        return (
            self.grain == GRAIN_TIMESTAMP
            and self.timestamp_sufficiency == SUFFICIENT
            and bool(self.observed_report_timezone)
        )

    def as_payload(self) -> dict[str, Any]:
        return {
            "datastream_id": self.datastream_id,
            "execution_id": self.execution_id,
            "grain": self.grain,
            "timestamp_sufficiency": self.timestamp_sufficiency,
            "source_report_timezone": self.observed_report_timezone,
            "evidence_origin": self.evidence_origin,
            "observed": self.is_observed,
            "confidence": self.confidence,
            "timezone_assumed": self.assumed,
            "assumptions": [dict(item) for item in self.assumptions],
            "adjustment_lever": dict(self.adjustment_lever),
            "tzdb_version": self.tzdb_version,
            "time_boundary_gap_code": self.gap_code,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }


def record_boundary_evidence(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str | None,
    grain: str,
    observed_report_timezone: str | None,
    evidence_origin: str,
    plan_version_id: str | None = None,
    confidence: str = "observed",
    assumed: bool = False,
    assumptions: Sequence[Mapping[str, Any]] = (),
    adjustment_lever: Mapping[str, Any] | None = None,
    tzdb_version: str | None = None,
) -> str:
    """Persist one publication's observed boundary evidence. Returns its id.

    Idempotent by content: re-observing an identical boundary for the same
    execution returns the stored row rather than growing the history with
    duplicates that say the same thing.
    """

    from ulid import ULID  # noqa: PLC0415

    from core.timezone_vocabulary import canonical_zone  # noqa: PLC0415
    from core.timezone_vocabulary import tzdb_version as read_tzdb

    zone = canonical_zone(observed_report_timezone) if observed_report_timezone else None
    sufficiency, gap_code = _sufficiency(grain, zone)
    pinned_tzdb = tzdb_version or (read_tzdb() if zone else None)
    lever = dict(adjustment_lever or {"available": False})

    digest = content_hash(
        {
            "datastream_id": datastream_id,
            "execution_id": execution_id,
            "grain": grain,
            "zone": zone,
            "origin": evidence_origin,
            "assumed": assumed,
            "lever": lever,
            "tzdb_version": pinned_tzdb,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastream_time_boundary_evidence "
            "WHERE datastream_id = %s AND execution_id IS NOT DISTINCT FROM %s "
            "AND content_hash = %s",
            (datastream_id, execution_id, digest),
        )
        existing = cur.fetchone()
        if existing:
            return str(existing[0])
        evidence_id = f"dstbe_{ULID()}"
        cur.execute(
            """
            INSERT INTO app.datastream_time_boundary_evidence
                (id, project_id, datastream_id, execution_id, plan_version_id, grain,
                 timestamp_sufficiency, observed_report_timezone, evidence_origin,
                 confidence, assumed, assumptions, adjustment_lever, tzdb_version,
                 gap_code, content_hash)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s)
            """,
            (
                evidence_id,
                project_id,
                datastream_id,
                execution_id,
                plan_version_id,
                grain,
                sufficiency,
                zone,
                evidence_origin,
                confidence,
                assumed,
                canonical_json([dict(item) for item in assumptions]),
                canonical_json(lever),
                pinned_tzdb,
                gap_code,
                digest,
            ),
        )
    return evidence_id


def _sufficiency(grain: str, zone: str | None) -> tuple[str, str | None]:
    if grain == GRAIN_DATE_ONLY:
        # Not a defect: a source DATE is a legitimate grain. It is simply not
        # re-alignable, and saying so is what keeps a comparison honest.
        return INSUFFICIENT_NO_TIMESTAMP, GAP_DATE_ONLY
    if grain == GRAIN_TIMESTAMP and not zone:
        return INSUFFICIENT_NO_SOURCE_ZONE, GAP_NO_SOURCE_ZONE
    if grain == GRAIN_TIMESTAMP:
        return SUFFICIENT, None
    return SUFFICIENCY_UNKNOWN, GAP_NO_EVIDENCE


_EVIDENCE_COLUMNS = (
    "datastream_id",
    "execution_id",
    "grain",
    "timestamp_sufficiency",
    "observed_report_timezone",
    "evidence_origin",
    "confidence",
    "assumed",
    "assumptions",
    "adjustment_lever",
    "tzdb_version",
    "gap_code",
    "observed_at",
)


def latest_boundary_evidence(
    conn, *, project_id: str, datastream_id: str
) -> BoundaryEvidence | None:
    """The most recent observation for one Datastream, or ``None`` if never observed.

    ``None`` means "no publication has proved this yet", which the compiler reads
    as `unavailable` coverage -- never as a healthy default.
    """

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_EVIDENCE_COLUMNS)} "
            "FROM app.datastream_time_boundary_evidence "
            "WHERE project_id = %s AND datastream_id = %s "
            "ORDER BY observed_at DESC LIMIT 1",
            (project_id, datastream_id),
        )
        row = cur.fetchone()
    return _evidence(row) if row else None


def boundary_evidence_for_project(
    conn, *, project_id: str
) -> dict[str, BoundaryEvidence]:
    """The latest observation per Datastream, in one query rather than one per stream."""

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT ON (datastream_id) {', '.join(_EVIDENCE_COLUMNS)}
            FROM app.datastream_time_boundary_evidence
            WHERE project_id = %s
            ORDER BY datastream_id, observed_at DESC
            """,
            (project_id,),
        )
        rows = cur.fetchall()
    return {str(row[0]): _evidence(row) for row in rows}


def _evidence(row: Sequence[Any]) -> BoundaryEvidence:
    record = dict(zip(_EVIDENCE_COLUMNS, row))
    return BoundaryEvidence(
        datastream_id=str(record["datastream_id"]),
        execution_id=record["execution_id"],
        grain=str(record["grain"]),
        timestamp_sufficiency=str(record["timestamp_sufficiency"]),
        observed_report_timezone=record["observed_report_timezone"],
        evidence_origin=str(record["evidence_origin"]),
        confidence=str(record["confidence"]),
        assumed=bool(record["assumed"]),
        assumptions=tuple(record["assumptions"] or ()),
        adjustment_lever=record["adjustment_lever"] or {},
        tzdb_version=record["tzdb_version"],
        gap_code=record["gap_code"],
        observed_at=record["observed_at"],
    )


# ---------------------------------------------------------------------------
# Derivation. The only place a reporting date is computed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ReportingDate:
    """A derived reporting date plus everything needed to reproduce it."""

    reporting_date: date | None
    source_timestamp: datetime | None
    source_date: date | None
    source_report_timezone: str | None
    timezone_policy_version_id: str
    tzdb_version: str
    timezone_assumed: bool
    time_boundary_gap_code: str | None
    #: Populated when the instant fell in a DST discontinuity, naming which one.
    dst_note: str | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "source_timestamp": (
                self.source_timestamp.isoformat() if self.source_timestamp else None
            ),
            "source_date": self.source_date.isoformat() if self.source_date else None,
            "source_report_timezone": self.source_report_timezone,
            "reporting_date": self.reporting_date.isoformat() if self.reporting_date else None,
            "timezone_policy_version_id": self.timezone_policy_version_id,
            "tzdb_version": self.tzdb_version,
            "timezone_assumed": self.timezone_assumed,
            "time_boundary_gap_code": self.time_boundary_gap_code,
            "dst_note": self.dst_note,
        }


def derive_reporting_date(
    *,
    policy: TimezonePolicy,
    evidence: BoundaryEvidence,
    source_timestamp: datetime | None = None,
    source_date: date | None = None,
) -> ReportingDate:
    """Project one fact onto the Project's reporting day, or refuse with a reason.

    The refusals, and why each is a refusal rather than a best effort:

    * **Source ``DATE`` with no timestamp.** There is no sub-day information, so
      any "re-aligned" date would be manufactured. Returned with the SOURCE date
      echoed and ``date_grain_not_realignable`` -- the value is usable, it simply
      may not be declared equivalent to another source's day.
    * **Unknown source zone.** An instant without a zone is not an instant.
    * **Policy says never derive.** A Project may decide that no re-projection
      happens; that is a confirmed decision and is honoured.
    * **DST gap** under a ``refuse`` policy. The local wall time does not exist
      that day; ``shift_forward`` is the other confirmed option.

    The source timestamp and source date are echoed UNCHANGED on every branch:
    a preference change re-derives, it never rewrites a native fact.
    """

    base = {
        "source_timestamp": source_timestamp,
        "source_date": source_date,
        "source_report_timezone": evidence.observed_report_timezone,
        "timezone_policy_version_id": policy.version_id,
        "tzdb_version": policy.tzdb_version,
        "timezone_assumed": evidence.assumed,
    }

    if evidence.grain == GRAIN_DATE_ONLY or source_timestamp is None:
        return ReportingDate(
            reporting_date=None, time_boundary_gap_code=GAP_DATE_ONLY, **base
        )
    if not evidence.observed_report_timezone:
        return ReportingDate(
            reporting_date=None, time_boundary_gap_code=GAP_NO_SOURCE_ZONE, **base
        )
    if not policy.derives_reporting_date:
        return ReportingDate(
            reporting_date=None, time_boundary_gap_code=GAP_DATE_ONLY, **base
        )
    # A derivation computed under one tzdb release and pinned to another is not
    # reproducible; refusing is the only honest answer.
    if evidence.tzdb_version and evidence.tzdb_version != policy.tzdb_version:
        return ReportingDate(
            reporting_date=None, time_boundary_gap_code=GAP_TZDB_MISMATCH, **base
        )

    try:
        source_zone = ZoneInfo(evidence.observed_report_timezone)
        target_zone = ZoneInfo(policy.reporting_timezone)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return ReportingDate(
            reporting_date=None, time_boundary_gap_code=GAP_NO_SOURCE_ZONE, **base
        )

    aware = (
        source_timestamp
        if source_timestamp.tzinfo is not None
        else source_timestamp.replace(tzinfo=source_zone)
    )
    note = _dst_note(aware, source_zone)
    if note == "gap" and policy.dst_gap_policy == "refuse":
        return ReportingDate(
            reporting_date=None,
            time_boundary_gap_code=GAP_DST_GAP,
            dst_note="The local wall time does not exist on this date in the source zone.",
            **base,
        )
    if note == "overlap" and policy.dst_overlap_policy == "refuse":
        return ReportingDate(
            reporting_date=None,
            time_boundary_gap_code=GAP_DST_GAP,
            dst_note="The local wall time occurs twice on this date in the source zone.",
            **base,
        )
    if note == "overlap" and policy.dst_overlap_policy == "second_occurrence":
        aware = aware.replace(fold=1)

    return ReportingDate(
        reporting_date=aware.astimezone(target_zone).date(),
        time_boundary_gap_code=None,
        dst_note=(
            None
            if note is None
            else f"Resolved a DST {note} under the confirmed policy."
        ),
        **base,
    )


def _dst_note(moment: datetime, zone: ZoneInfo) -> str | None:
    """``"gap"``, ``"overlap"`` or ``None`` for a local wall time in *zone*.

    Uses ``fold``, which is what it exists for: a wall time whose two folds map to
    different UTC instants is an overlap, and one whose round trip through UTC does
    not come back to itself is a gap.
    """
    naive = moment.replace(tzinfo=None)
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    # The GAP test comes first, and the order is load-bearing. Under PEP 495 the
    # two folds resolve to different offsets in a gap as well as in an overlap, so
    # checking the offsets first labels a spring-forward gap an "overlap" -- and a
    # `refuse` gap policy then silently produces a date for a wall time that does
    # not exist. Only a gap fails to survive the round trip through UTC.
    round_trip = first.astimezone(ZoneInfo("UTC")).astimezone(zone)
    if round_trip.replace(tzinfo=None) != naive:
        return "gap"
    if first.utcoffset() != second.utcoffset():
        return "overlap"
    return None


# ---------------------------------------------------------------------------
# Comparison. Unknown boundaries stay visible.
# ---------------------------------------------------------------------------


def observed_zones_for_datastreams(
    project_id: str | None, datastream_ids: Sequence[str]
) -> dict[str, str]:
    """The zone each Datastream's latest run OBSERVED, keyed by id (fail-soft) -- AI-167.

    ONE query for the whole set, and its own connection, because the caller is
    ``datamodel._detect_conflicts`` -- a per-field read that would otherwise open a round
    trip per stream on a page that renders many fields.

    Why this exists at all: the used-by SELECT surfaces NO report timezone. Its own
    ``_stream_report_timezone`` says so ("today the used-by SELECT carries none of these"),
    so ``check_cross_source_day_offset`` saw every stream as unplaceable and could only
    ever report "N streams have no resolvable reporting timezone" -- never "these draw
    their day on different clocks", which is the signal the epic asks for. The zone now
    exists: a pull observes it and the worker records it (AI-161). This is the read that
    turns that record into the signal.

    Only OBSERVED origins are returned. A declaration states what a source can do; it is
    not evidence of what a run did, and letting it through here would put a promise on a
    screen that claims to show a measurement.
    """
    ids = [str(i) for i in datastream_ids if i]
    if not ids:
        return {}
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ON (datastream_id) datastream_id, observed_report_timezone
                  FROM app.datastream_time_boundary_evidence
                 WHERE datastream_id = ANY(%s)
                   AND (%s::text IS NULL OR project_id = %s)
                   AND evidence_origin = ANY(%s)
                   AND observed_report_timezone IS NOT NULL
                 ORDER BY datastream_id, observed_at DESC
                """,
                (ids, project_id, project_id, sorted(OBSERVED_ORIGINS)),
            )
            return {str(row[0]): str(row[1]) for row in cur.fetchall() if row[1]}
    except Exception as exc:  # noqa: BLE001 -- fail-soft: a read path never crashes here.
        logger.warning("time_boundary: observed_zones_for_datastreams fell back to {}: %s", exc)
        return {}


def compare_boundaries(
    evidences: Sequence[BoundaryEvidence], *, policy: TimezonePolicy | None = None
) -> dict[str, Any]:
    """Compare day boundaries across streams, keeping the unknowns visible.

    The predecessor (:func:`core.timezone_signal.check_cross_source_day_offset`)
    EXCLUDED a stream whose boundary was unknown and returned ``None`` when fewer
    than two known zones remained. Two streams agreeing and three unplaceable
    therefore rendered as "no offset" -- an incomplete comparison presented as a
    healthy one, which is the Reporting Timezone criterion "cross-source daily
    reconciliation ignores different day boundaries" arriving through the back door.

    Here every stream appears. ``comparable`` is true only when every stream is
    placed AND they agree; otherwise the reason is typed and the specific streams
    are named.
    """

    # AI-167: "not re-alignable" and "not placeable" are TWO facts, and bucketing them
    # exclusively made this function blind in the only grain this product has.
    #
    # `date_only` used to win the classification, so a DATE-grain stream never reached
    # `known` and its zone never reached `zones`. Measured: two streams on Europe/Paris
    # and America/New_York at DATE grain returned `distinct_source_timezones: []` and the
    # single signal `date_grain_not_realignable` -- the divergence invisible. Since the
    # reporting grain is DATE everywhere (never hourly), that was EVERY stream: the
    # `cross_source_day_boundary_differs` branch could not be reached by any real
    # comparison. The criterion this function exists to close -- "cross-source daily
    # reconciliation ignores different day boundaries" -- arriving through the back door
    # a second time, in the very function written to shut that door.
    #
    # The ratified posture settles it (epic 39, AC 39.8): at DATE grain the platform
    # SIGNALS the possible day-offset as provenance and does not realign. Non-realignable
    # therefore must not silence "different clocks" -- it is a separate fact, reported
    # separately, about a stream that may well be perfectly placed on a clock.
    known: list[BoundaryEvidence] = [e for e in evidences if e.observed_report_timezone]
    unplaced: list[BoundaryEvidence] = [e for e in evidences if not e.observed_report_timezone]
    date_only: list[BoundaryEvidence] = [e for e in evidences if e.grain == GRAIN_DATE_ONLY]

    zones = sorted(
        {item.observed_report_timezone for item in known if item.observed_report_timezone}
    )
    signals: list[dict[str, Any]] = []
    if len(zones) > 1:
        signals.append(
            {
                "code": SIGNAL_BOUNDARY_DIFFERS,
                "severity": "advisory",
                "message": (
                    "These streams draw their reporting day on different clocks "
                    f"({', '.join(zones)}). A daily total can be one day out at the "
                    "boundary. The offset is signalled, never corrected: at DATE grain "
                    "there is no sub-day data to re-slice."
                ),
                "timezones": zones,
                "datastream_ids": [item.datastream_id for item in known],
            }
        )
    if unplaced:
        signals.append(
            {
                "code": GAP_NO_SOURCE_ZONE,
                "severity": "gap",
                "message": (
                    f"{len(unplaced)} stream(s) have no observed reporting timezone, so "
                    "their days cannot be declared equivalent to any other stream's."
                ),
                "datastream_ids": [item.datastream_id for item in unplaced],
            }
        )
    if date_only:
        signals.append(
            {
                "code": GAP_DATE_ONLY,
                "severity": "gap",
                "message": (
                    f"{len(date_only)} stream(s) publish a source DATE with no timestamp. "
                    "The date is usable as published and cannot be re-aligned."
                ),
                "datastream_ids": [item.datastream_id for item in date_only],
            }
        )

    return {
        # AI-167: comparability is about whether these streams' DAYS can be declared
        # equivalent, which a source DATE does not prevent -- two sources both drawing
        # their day on Europe/Paris line up whether or not a timestamp exists to re-slice.
        # Keeping `date_only` in this condition made `comparable` constantly false in a
        # DATE-grain product, which flags everything and therefore says nothing. It stays
        # reported as its own signal: usable as published, not re-alignable.
        "comparable": not unplaced and len(zones) <= 1,
        "reporting_timezone": policy.reporting_timezone if policy else None,
        "timezone_policy_version_id": policy.version_id if policy else None,
        "tzdb_version": policy.tzdb_version if policy else None,
        "distinct_source_timezones": zones,
        "placed_count": len(known),
        "unplaced_count": len(unplaced),
        "date_only_count": len(date_only),
        "signals": signals,
        "streams": [item.as_payload() for item in evidences],
    }


def days_between(start: date, end: date) -> int:
    """Whole days between two reporting dates. Exists so no caller re-implements it."""
    return (end - start) // timedelta(days=1) if isinstance(end - start, timedelta) else 0
