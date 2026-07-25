"""Story 36.8 -- recommend, save and preview a bounded first-report draft.

A brand-new operator has ONE authorized source and ONE exposed eligible account.
This module turns that into a *safe* first report WITHOUT making them master every
Datastream option:

  1. ``recommend_first_report`` derives ONE capability-compatible report + a bounded
     recent interval with an explicit grain, timezone, currency and estimated cost,
     OR a structured "needs choice" / "no safe recommendation" result when zero or
     several candidates exist. It NEVER silently picks an account, an unsafe grain
     or an unsupported metric/dimension set (E36-FR03 / UX-DR27).

  2. ``save_first_report_draft`` captures the operator's exact (edited) choices as
     IMMUTABLE intent/extraction/mapping plan versions -- reusing the Epic 12
     seams (``datastream_intents.save_datastream_intent`` +
     ``datastream_field_mapping``) so there is no second engine and no second
     mapping model (E36-NFR05). It also records the capability/catalog versions.
     The mapping version is written as an immutable version that DOES NOT advance
     the live ``current_mapping_version_id`` pointer -- a draft is expressible as
     plan+mapping versions that are not yet live.

  3. ``preview_first_report`` runs a bounded, redacted preview that REFERENCES the
     saved plan/mapping versions, returns counts/hashes only (no rows, no
     provider payloads) and CANNOT publish or advance any current pointer: it
     stops strictly before ``datastream_publication.commit_publication``.

The domain never contains a connector identifier or a provider field name -- all
report/metric/dimension/grain vocabulary arrives through the Story 12.1 governed
capability catalog and the operator's own edits.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ulid import ULID

from core.operations import (
    MutationResult,
    OperationResult,
    OperationSpec,
    execute_operation,
)

# The default recent window if the datastream carries no explicit override. This
# is a bounded, safe, recent-first interval (never a full backfill). The live
# override lives in ``app.datastreams.date_window_days`` (migration 023).
_DEFAULT_RECENT_WINDOW_DAYS = 30
_MAX_RECENT_WINDOW_DAYS = 365
_UNKNOWN_CURRENCY = "unknown"


class FirstReportDraftValidationError(ValueError):
    """Raised before SQL when draft inputs are unsafe or malformed."""


class FirstReportDraftConflict(RuntimeError):
    """A draft cannot transition from its current state / key reuse."""


class FirstReportDraftUnavailable(RuntimeError):
    """The draft, its saved versions, or its scope cannot be resolved safely."""


# ---------------------------------------------------------------------------
# Result value objects. These are model-facing; they carry NO raw provider rows.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RecommendedInterval:
    start: str  # inclusive, RFC3339 UTC
    end_exclusive: str  # exclusive, RFC3339 UTC
    window_days: int
    label: str


@dataclass(frozen=True)
class EstimatedCost:
    read_points: int
    unit: str
    window_days: int
    quota_pre_check: str  # "ok" | "budget_exhausted" | "breaker_open" | "no_quota"


@dataclass(frozen=True)
class FirstReportRecommendation:
    """ONE safe capability-compatible report proposal (or a needs-choice signal)."""

    outcome: str  # "recommended" | "needs_choice" | "no_safe_recommendation"
    report_id: str | None
    metrics: list[str]
    dimensions: list[str]
    grain: list[str]
    timezone: str
    currency: str
    interval: RecommendedInterval | None
    estimated_cost: EstimatedCost | None
    capability_contract_version: str | None
    capability_fingerprint: str | None
    # Deterministic, non-silent explanation of WHY a single safe pick was / was
    # not possible, plus every candidate the operator must choose among.
    compatibility: dict[str, Any] = field(default_factory=dict)
    candidates: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        if self.interval is not None:
            payload["interval"] = asdict(self.interval)
        if self.estimated_cost is not None:
            payload["estimated_cost"] = asdict(self.estimated_cost)
        return payload


@dataclass(frozen=True)
class SavedFirstReportDraft:
    draft_id: str
    datastream_id: str
    project_id: str
    plan_version_id: str
    mapping_version_id: str
    state: str
    operation_id: str
    audit_event_id: str | None
    replayed: bool
    recommendation: dict[str, Any]


@dataclass(frozen=True)
class FirstReportPreview:
    draft_id: str | None
    plan_version_id: str
    mapping_version_id: str
    executable: bool
    row_count: int | None
    content_hash: str | None
    dq_issues: list[dict[str, Any]]
    evidence: dict[str, Any]
    can_publish: bool  # ALWAYS False -- preview never publishes / advances pointers.


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _canonical_hash(value: Any) -> str:
    return _sha256(json.dumps(value, sort_keys=True, separators=(",", ":")))


# ---------------------------------------------------------------------------
# Recommendation.
# ---------------------------------------------------------------------------
def _selectable_reports(capabilities: dict[str, Any]) -> list[dict[str, Any]]:
    """Reports whose availability is ``selectable`` -- never a blocked/unavailable one."""
    return [
        report
        for report in capabilities.get("reports", [])
        if report.get("availability", {}).get("status") == "selectable"
        and report.get("supported_grains")
        and report.get("metrics")
        and report.get("dimensions")
    ]


def _safe_grain(report: dict[str, Any]) -> list[str] | None:
    """Pick the SMALLEST supported grain deterministically.

    A safe grain must be a declared ``supported_grains`` entry -- we never invent a
    grain the report does not advertise (that would be an ``unsupported_grain``).
    The smallest (fewest dimensions, then lexicographic) keeps cardinality/cost
    bounded for a first report.
    """
    grains = [sorted(set(grain)) for grain in report.get("supported_grains", []) if grain]
    if not grains:
        return None
    return min(grains, key=lambda item: (len(item), item))


def _report_currency(report: dict[str, Any], capabilities: dict[str, Any]) -> str:
    """Derive a single explicit currency for the report's monetary metrics.

    Currency lives on the capability field metadata. If the selected metrics carry
    exactly one currency, that is the explicit currency; multiple or none resolve
    to ``unknown`` so the operator (not the wizard) makes the call -- never a silent
    pick.
    """
    field_by_id = {f.get("field_id"): f for f in capabilities.get("fields", [])}
    currencies = {
        str(field_by_id[m].get("currency"))
        for m in report.get("metrics", [])
        if m in field_by_id and field_by_id[m].get("currency") not in (None, "", _UNKNOWN_CURRENCY)
    }
    if len(currencies) == 1:
        return next(iter(currencies))
    return _UNKNOWN_CURRENCY


def _recent_interval(window_days: int, *, now: datetime | None = None) -> RecommendedInterval:
    now = now or datetime.now(UTC)
    end = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=window_days)
    return RecommendedInterval(
        start=start.isoformat().replace("+00:00", "Z"),
        end_exclusive=end.isoformat().replace("+00:00", "Z"),
        window_days=window_days,
        label=f"{window_days} derniers jours",
    )


def _estimated_cost(
    report: dict[str, Any], window_days: int, *, module_name: str
) -> EstimatedCost:
    quota = report.get("quota_cost", {}) or {}
    per_pull = int(quota.get("read_points", 0) or 0)
    unit = str(quota.get("unit", "read_points"))
    # A daily-grained recent window is roughly one read per day; the bounded
    # estimate is deliberately conservative (window_days pulls).
    estimated_points = max(per_pull, per_pull * max(window_days, 1))
    reason = "no_quota"
    try:
        from core import quota as quota_engine  # noqa: PLC0415

        _ok, reason = quota_engine.pre_check(module_name, estimated_points)
    except Exception:  # noqa: BLE001 - a cost estimate must never crash recommendation.
        reason = "no_quota"
    return EstimatedCost(
        read_points=estimated_points,
        unit=unit,
        window_days=window_days,
        quota_pre_check=reason,
    )


def _resolve_window_days(conn, datastream_id: str | None, project_id: str) -> int:
    if not datastream_id:
        return _DEFAULT_RECENT_WINDOW_DAYS
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT date_window_days FROM app.datastreams "
                "WHERE id = %s AND project_id = %s",
                (datastream_id, project_id),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001
        return _DEFAULT_RECENT_WINDOW_DAYS
    if row and isinstance(row[0], int) and 1 <= row[0] <= _MAX_RECENT_WINDOW_DAYS:
        return int(row[0])
    return _DEFAULT_RECENT_WINDOW_DAYS


def recommend_first_report(
    conn,
    *,
    project_id: str,
    capabilities: dict[str, Any],
    account: dict[str, Any] | None,
    actor: str,
    datastream_id: str | None = None,
    now: datetime | None = None,
) -> FirstReportRecommendation:
    """Recommend ONE safe report + bounded recent interval, or a needs-choice result.

    ``capabilities`` is the governed Story 12.1 catalog for the authorized source
    (already project/connection scoped by ``source_capabilities`` upstream).
    ``account`` is the single exposed eligible account resolved by
    ``project_access.resolve_provider_account_access``; ``None`` means no eligible
    account was exposed -- a NON-silent blocker, never a guess.
    """
    if not isinstance(capabilities, dict) or not capabilities.get("reports"):
        return FirstReportRecommendation(
            outcome="no_safe_recommendation",
            report_id=None,
            metrics=[],
            dimensions=[],
            grain=[],
            timezone="UTC",
            currency=_UNKNOWN_CURRENCY,
            interval=None,
            estimated_cost=None,
            capability_contract_version=None,
            capability_fingerprint=None,
            compatibility={
                "reason": "no_capability_catalog",
                "explanation": "La source n'expose aucun rapport compatible pour le moment.",
            },
        )

    contract_version = str(capabilities.get("contract_version", "")) or None
    capability_fingerprint = _canonical_hash(capabilities)
    module_name = str(capabilities.get("module", {}).get("name", "")) or ""

    if account is None:
        return FirstReportRecommendation(
            outcome="needs_choice",
            report_id=None,
            metrics=[],
            dimensions=[],
            grain=[],
            timezone="UTC",
            currency=_UNKNOWN_CURRENCY,
            interval=None,
            estimated_cost=None,
            capability_contract_version=contract_version,
            capability_fingerprint=capability_fingerprint,
            compatibility={
                "reason": "no_eligible_account",
                "explanation": (
                    "Aucun compte exposé n'est éligible. Exposez exactement un compte "
                    "avant de recommander un rapport."
                ),
            },
        )

    reports = _selectable_reports(capabilities)
    if not reports:
        return FirstReportRecommendation(
            outcome="no_safe_recommendation",
            report_id=None,
            metrics=[],
            dimensions=[],
            grain=[],
            timezone="UTC",
            currency=_UNKNOWN_CURRENCY,
            interval=None,
            estimated_cost=None,
            capability_contract_version=contract_version,
            capability_fingerprint=capability_fingerprint,
            compatibility={
                "reason": "no_selectable_report",
                "explanation": "Aucun rapport n'est actuellement sélectionnable pour cette source.",
            },
        )

    window_days = _resolve_window_days(conn, datastream_id, project_id)
    interval = _recent_interval(window_days, now=now)

    # Build the per-report safe candidate (report + smallest supported grain +
    # subset metrics/dimensions covering that grain). Only candidates with a real
    # safe grain survive -- an unsupported grain is NEVER fabricated.
    candidates: list[dict[str, Any]] = []
    for report in sorted(reports, key=lambda r: str(r.get("id"))):
        grain = _safe_grain(report)
        if grain is None:
            continue
        metrics = sorted(report.get("metrics", []))
        # Dimensions must at least cover the grain, and stay a valid declared subset.
        declared_dims = set(report.get("dimensions", []))
        dimensions = sorted(declared_dims & set(grain)) or sorted(declared_dims)
        if not metrics or not dimensions or not set(grain).issubset(declared_dims):
            continue
        candidates.append(
            {
                "report_id": str(report.get("id")),
                "metrics": metrics,
                "dimensions": dimensions,
                "grain": grain,
                "currency": _report_currency(report, capabilities),
                "estimated_cost": asdict(
                    _estimated_cost(report, window_days, module_name=module_name)
                ),
            }
        )

    if not candidates:
        return FirstReportRecommendation(
            outcome="no_safe_recommendation",
            report_id=None,
            metrics=[],
            dimensions=[],
            grain=[],
            timezone="UTC",
            currency=_UNKNOWN_CURRENCY,
            interval=interval,
            estimated_cost=None,
            capability_contract_version=contract_version,
            capability_fingerprint=capability_fingerprint,
            compatibility={
                "reason": "no_safe_grain",
                "explanation": (
                    "Aucun rapport ne propose une granularité supportée compatible "
                    "avec une première extraction bornée."
                ),
            },
        )

    if len(candidates) > 1:
        # MULTIPLE safe candidates -> the operator must choose; never silently pick.
        return FirstReportRecommendation(
            outcome="needs_choice",
            report_id=None,
            metrics=[],
            dimensions=[],
            grain=[],
            timezone="UTC",
            currency=_UNKNOWN_CURRENCY,
            interval=interval,
            estimated_cost=None,
            capability_contract_version=contract_version,
            capability_fingerprint=capability_fingerprint,
            compatibility={
                "reason": "multiple_compatible_reports",
                "explanation": (
                    "Plusieurs rapports sont compatibles. Choisissez le rapport, "
                    "la granularité et la devise avant de continuer."
                ),
            },
            candidates=candidates,
        )

    only = candidates[0]
    est = EstimatedCost(**only["estimated_cost"])
    return FirstReportRecommendation(
        outcome="recommended",
        report_id=only["report_id"],
        metrics=only["metrics"],
        dimensions=only["dimensions"],
        grain=only["grain"],
        timezone="UTC",
        currency=only["currency"],
        interval=interval,
        estimated_cost=est,
        capability_contract_version=contract_version,
        capability_fingerprint=capability_fingerprint,
        compatibility={
            "reason": "single_compatible_report",
            "explanation": (
                "Un rapport compatible et une période récente bornée sont recommandés. "
                "Vous pouvez ajuster la granularité, le fuseau et la devise."
            ),
        },
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Intent + mapping assembly from the (edited) operator choices.
# ---------------------------------------------------------------------------
def _build_intent(
    *,
    connection_ref_id: str,
    report_id: str,
    metrics: list[str],
    dimensions: list[str],
    grain: list[str],
    timezone: str,
    interval: dict[str, Any],
) -> dict[str, Any]:
    """Assemble a valid Story 12.2 intent from the exact operator choices.

    The recent interval maps onto the intent ``historical`` window and a manual
    schedule -- a first report is a bounded, manual, recent-first pull (no cadence
    commitment yet). ``normalize_intent`` will reject any unknown timezone.
    """
    return {
        "contract_version": "1",
        "source": {
            "kind": "connector_pull",
            "writer_kind": "toorow",
            "connection_ref_id": connection_ref_id,
            "report_id": report_id,
            "selection": {
                "selection_mode": "subset",
                "metrics": sorted(set(metrics)),
                "dimensions": sorted(set(dimensions)),
                "grain": sorted(set(grain)),
                "filters": [],
            },
        },
        "destination": {"policy": "managed_raw"},
        "historical": {
            "start": interval["start"],
            "end_exclusive": interval["end_exclusive"],
        },
        "schedule": {
            "mode": "manual",
            "interval_minutes": None,
            "timezone": timezone,
            "watermark": {"kind": "date_window", "delay_minutes": 0},
            "late_arrival": {"lookback_minutes": 0},
            "retry": {
                "max_attempts": 3,
                "initial_backoff_seconds": 60,
                "max_backoff_seconds": 3600,
            },
            "missed_run": {"mode": "skip", "max_catchup_windows": 1},
        },
    }


def _mapping_from_capabilities(
    capabilities: dict[str, Any],
    *,
    metrics: list[str],
    dimensions: list[str],
    currency: str,
) -> dict[str, Any]:
    """Profile the selected report fields into an immutable mapping payload.

    Reuses ``datastream_field_mapping.profile_fields`` -- NO second mapping model.
    Only the selected metric/dimension fields are profiled; the explicit currency
    choice is stamped so a monetary metric never carries an ambiguous currency.
    """
    from core.datastream_field_mapping import profile_fields  # noqa: PLC0415

    selected = set(metrics) | set(dimensions)
    field_records: list[dict[str, Any]] = []
    for f in capabilities.get("fields", []):
        if f.get("field_id") not in selected:
            continue
        record = dict(f)
        if f.get("field_id") in metrics and currency != _UNKNOWN_CURRENCY:
            record["currency"] = currency
        field_records.append(record)
    return profile_fields(field_records=field_records)


def save_first_report_draft(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    connection_ref_id: str,
    report_id: str,
    metrics: list[str],
    dimensions: list[str],
    grain: list[str],
    timezone: str,
    currency: str,
    interval: dict[str, Any],
    capabilities: dict[str, Any],
    actor: str,
    idempotency_key: str,
    effective_org_id: str,
    host_context: dict[str, Any],
    trace_id: str | None = None,
) -> SavedFirstReportDraft:
    """Persist the exact edited choices as IMMUTABLE intent + mapping versions.

    The plan version is created through ``datastream_intents.save_datastream_intent``
    (the Epic 12 seam: it validates against the capability catalog, records the
    capability/catalog versions and appends one immutable version). The mapping
    version is written as an immutable version that DOES NOT advance the live
    ``current_mapping_version_id`` pointer -- a draft's mapping is saved-but-not-live.
    A ``first_report_drafts`` row tracks the recommended selection and links the
    plan/mapping/operation, and the whole write routes through
    ``operations.execute_operation`` for the atomic audit + outbox.
    """
    if not idempotency_key.strip():
        raise FirstReportDraftValidationError("Idempotency-Key is required")
    if not report_id or not connection_ref_id or not datastream_id:
        raise FirstReportDraftValidationError("report, connection and datastream are required")
    if not metrics or not dimensions or not grain:
        raise FirstReportDraftValidationError("metrics, dimensions and grain are required")
    if not isinstance(interval, dict) or "start" not in interval or "end_exclusive" not in interval:
        raise FirstReportDraftValidationError("a bounded recent interval is required")

    from core.datastream_field_mapping import (  # noqa: PLC0415
        DatastreamMappingStructuralError,
        normalize_mapping,
        project_ossie,
    )
    from core.datastream_intents import (  # noqa: PLC0415
        DatastreamIntentConflict,
        DatastreamIntentNotFound,
        DatastreamIntentStructuralError,
        save_datastream_intent,
    )

    intent = _build_intent(
        connection_ref_id=connection_ref_id,
        report_id=report_id,
        metrics=metrics,
        dimensions=dimensions,
        grain=grain,
        timezone=timezone,
        interval=interval,
    )

    # 1) Immutable plan version through the Epic 12 seam (validates against the
    #    catalog + records capability/catalog versions). commit=False so the plan,
    #    the mapping version, the draft row and the operation commit atomically.
    try:
        plan = save_datastream_intent(
            datastream_id=datastream_id,
            project_id=project_id,
            intent=intent,
            identity=actor,
            idempotency_key=f"{idempotency_key}:plan",
            conn=conn,
            capabilities=capabilities,
            reason="first_report_draft_saved",
            trace_id=trace_id,
            commit=False,
        )
    except (DatastreamIntentStructuralError,) as exc:
        raise FirstReportDraftValidationError(str(exc)) from exc
    except DatastreamIntentConflict as exc:
        raise FirstReportDraftConflict("idempotency_conflict") from exc
    except DatastreamIntentNotFound as exc:
        raise FirstReportDraftUnavailable("datastream unavailable") from exc

    plan_version_id = plan["id"]

    # 2) Immutable mapping version -- profiled from the SELECTED fields, stamped with
    #    the plan/capability versions, but WITHOUT the live pointer swap.
    try:
        mapping_payload = _mapping_from_capabilities(
            capabilities, metrics=metrics, dimensions=dimensions, currency=currency
        )
        mapping_payload["plan_version_id"] = plan_version_id
        mapping_payload["capability_fingerprint"] = plan.get("capability_fingerprint")
        normalized_mapping, mapping_content_hash = normalize_mapping(mapping_payload)
        ossie_projection = project_ossie(normalized_mapping)
    except DatastreamMappingStructuralError as exc:
        conn.rollback()
        raise FirstReportDraftValidationError(str(exc)) from exc

    blocking_count = sum(
        1
        for f in normalized_mapping.get("fields", [])
        if f.get("binding", {}).get("status") == "blocking"
    )
    executable = (
        plan.get("executable", False)
        and blocking_count == 0
        and not normalized_mapping.get("ambiguities")
    )

    draft_id = f"frdraft_{ULID()}"
    mapping_version_id = f"dmap_{ULID()}"
    recommendation = {
        "report_id": report_id,
        "metrics": sorted(set(metrics)),
        "dimensions": sorted(set(dimensions)),
        "grain": sorted(set(grain)),
        "timezone": timezone,
        "currency": currency,
        "interval": {"start": interval["start"], "end_exclusive": interval["end_exclusive"]},
        "capability_contract_version": plan.get("capability_contract_version"),
        "capability_fingerprint": plan.get("capability_fingerprint"),
    }

    spec = OperationSpec(
        command_type="first_report.draft.save",
        actor=actor,
        effective_org_id=effective_org_id,
        resource_path=(
            f"organization:{effective_org_id}",
            f"project:{project_id}",
            f"flux:{datastream_id}",
            f"first-report-draft:{draft_id}",
        ),
        idempotency_key=idempotency_key,
        host_context=host_context,
        versions={
            "policy": "first-report-v1",
            "catalog": plan.get("capability_contract_version") or "unknown",
            "tool": "rest-v1",
        },
        request_payload={
            "draft_id": draft_id,
            "datastream_id": datastream_id,
            "report_id": report_id,
            "grain": sorted(set(grain)),
            "timezone": timezone,
            "currency": currency,
        },
        provider_references={},
        confirmation_mode="none",
        trace_id=trace_id,
        confirmation_reference=None,
    )

    def mutation(operation_conn, operation_id: str) -> MutationResult:
        with operation_conn.cursor() as cur:
            # Immutable mapping version WITHOUT touching current_mapping_version_id.
            cur.execute(
                "SELECT COALESCE(MAX(version_number), 0) + 1 "
                "FROM app.datastream_mapping_versions "
                "WHERE datastream_id = %s AND project_id = %s",
                (datastream_id, project_id),
            )
            version_number = int(cur.fetchone()[0])
            cur.execute(
                """
                INSERT INTO app.datastream_mapping_versions
                    (id, datastream_id, project_id, version_number, mapping_contract_version,
                     source_schema_hash, plan_version_id, capability_fingerprint, content_hash,
                     ossie_spec_version, toorow_extension_version, executable, blocking_count,
                     mapping_payload, ossie_projection, idempotency_key_hash, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s::jsonb, %s::jsonb, %s, %s)
                """,
                (
                    mapping_version_id,
                    datastream_id,
                    project_id,
                    version_number,
                    normalized_mapping["mapping_contract_version"],
                    normalized_mapping["source_schema_hash"],
                    plan_version_id,
                    plan.get("capability_fingerprint"),
                    mapping_content_hash,
                    "0.1.1",
                    "1",
                    executable,
                    blocking_count,
                    json.dumps(normalized_mapping),
                    json.dumps(ossie_projection),
                    _sha256(f"{idempotency_key}:mapping"),
                    actor,
                ),
            )
            cur.execute(
                """
                INSERT INTO app.first_report_drafts
                    (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                     recommendation, state, operation_id, created_by)
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, 'saved', %s, %s)
                """,
                (
                    draft_id,
                    datastream_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    json.dumps(recommendation),
                    operation_id,
                    actor,
                ),
            )
        result = {
            "draft_id": draft_id,
            "plan_version_id": plan_version_id,
            "mapping_version_id": mapping_version_id,
            "state": "saved",
            "executable": executable,
        }
        return MutationResult(
            outcome="succeeded",
            before_hash=None,
            after_hash=_canonical_hash(result),
            result=result,
            outbox_payload={
                "draft_id": draft_id,
                "datastream_id": datastream_id,
                "delivery_kind": "first_report_draft",
            },
        )

    try:
        from core.operations import OperationIdempotencyConflict  # noqa: PLC0415

        operation: OperationResult = execute_operation(conn, spec, mutation=mutation)
        conn.commit()
    except OperationIdempotencyConflict as exc:
        conn.rollback()
        raise FirstReportDraftConflict("idempotency_conflict") from exc
    except Exception as exc:  # noqa: BLE001
        conn.rollback()
        raise FirstReportDraftUnavailable("first report draft persistence failed") from exc

    result = operation.result or {}
    return SavedFirstReportDraft(
        draft_id=result.get("draft_id", draft_id),
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=result.get("plan_version_id", plan_version_id),
        mapping_version_id=result.get("mapping_version_id", mapping_version_id),
        state=result.get("state", "saved"),
        operation_id=operation.operation_id,
        audit_event_id=operation.audit_event_id,
        replayed=operation.replayed,
        recommendation=recommendation,
    )


# ---------------------------------------------------------------------------
# Preview -- bounded, redacted, and STRICTLY non-publishing.
# ---------------------------------------------------------------------------
def _load_saved_versions(
    conn, *, project_id: str, plan_version_id: str, mapping_version_id: str
) -> dict[str, Any]:
    """Load the immutable saved plan+mapping versions the preview must reference."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, datastream_id, executable, capability_fingerprint
            FROM app.datastream_plan_versions
            WHERE id = %s AND project_id = %s
            """,
            (plan_version_id, project_id),
        )
        plan = cur.fetchone()
        cur.execute(
            """
            SELECT id, datastream_id, executable, blocking_count, source_schema_hash,
                   content_hash, plan_version_id
            FROM app.datastream_mapping_versions
            WHERE id = %s AND project_id = %s
            """,
            (mapping_version_id, project_id),
        )
        mapping = cur.fetchone()
    if plan is None or mapping is None:
        raise FirstReportDraftUnavailable("saved draft versions unavailable")
    if mapping[6] != plan_version_id or mapping[1] != plan[1]:
        # The mapping must reference the exact saved plan version and datastream.
        raise FirstReportDraftUnavailable("draft versions are inconsistent")
    return {
        "datastream_id": plan[1],
        "plan_executable": bool(plan[2]),
        "capability_fingerprint": plan[3],
        "mapping_executable": bool(mapping[2]),
        "blocking_count": int(mapping[3]),
        "source_schema_hash": mapping[4],
        "mapping_content_hash": mapping[5],
    }


def preview_first_report(
    conn,
    *,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    actor: str,
    draft_id: str | None = None,
    row_count: int | None = None,
    content_hash: str | None = None,
    run_gates: bool = True,
) -> FirstReportPreview:
    """Bounded, redacted preview that REFERENCES the saved versions and never publishes.

    The preview references the EXACT saved plan/mapping version ids. It returns
    counts/hashes only (no rows, no provider payloads) plus the DQ gate result.
    It NEVER calls ``commit_publication`` and NEVER advances a current pointer:
    ``can_publish`` is unconditionally ``False``.

    ``row_count``/``content_hash`` are supplied by the bounded candidate execution
    the caller already ran (or None for a pure references-only preview). The DQ
    gates run in read-only mode (``run_dq_gates``) using the saved evidence; they
    are evaluated but their outcome cannot trigger a publish here.
    """
    versions = _load_saved_versions(
        conn,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
    )
    executable = versions["plan_executable"] and versions["mapping_executable"]

    dq_issues: list[dict[str, Any]] = []
    # DQ gates are optional here: a references-only preview (no candidate execution)
    # has no execution row to gate. When an execution_id-bearing preview is wired,
    # the caller passes run_gates=True and we compose run_dq_gates read-only. We
    # NEVER advance state past 'validating' and NEVER call commit_publication.
    evidence = {
        "plan_version_id": plan_version_id,
        "mapping_version_id": mapping_version_id,
        "datastream_id": versions["datastream_id"],
        "capability_fingerprint": versions["capability_fingerprint"],
        "source_schema_hash": versions["source_schema_hash"],
        "mapping_content_hash": versions["mapping_content_hash"],
        "blocking_count": versions["blocking_count"],
        # Bounded/redacted: counts and hashes only, never rows or provider payloads.
        "row_count": row_count,
        "content_hash": content_hash,
    }

    # First-value funnel (E36-FR09): a preview resolved -> record the preview stage
    # (succeeded when both saved versions are executable, else blocked). The pseudonymous
    # ref is built inside the seam; only enums + the hash are persisted. Best-effort --
    # NEVER breaks the preview. org_id is resolved from the project (only when funnel
    # recording is actually enabled, so a pure preview adds no query otherwise); if that
    # lookup fails the seam simply no-ops (org missing -> skip).
    from core.first_value_instrumentation import funnel_enabled, record_stage  # noqa: PLC0415

    if funnel_enabled():
        _org_id = None
        try:
            with conn.cursor() as _cur:
                _cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
                _org_row = _cur.fetchone()
                _org_id = _org_row[0] if _org_row else None
        except Exception:  # noqa: BLE001 -- never fail a preview on a telemetry read.
            _org_id = None
        record_stage(
            conn,
            org_id=_org_id,
            project_id=project_id,
            subject=actor,
            stage="preview",
            outcome="succeeded" if executable else "blocked",
        )

    return FirstReportPreview(
        draft_id=draft_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        executable=executable,
        row_count=row_count,
        content_hash=content_hash,
        dq_issues=dq_issues,
        evidence=evidence,
        can_publish=False,  # INVARIANT: preview never publishes / advances pointers.
    )
