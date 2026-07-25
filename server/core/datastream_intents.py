"""Immutable Datastream intent validation and persistence (Story 12.2).

The module is source-agnostic. Connector-specific availability and field rules
arrive exclusively through the normalized Story 12.1 capability catalog.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import jsonschema
from ulid import ULID

from core.geographic_reporting import GeographicPosture

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "datastream-intent.schema.json"
_WRITER_BY_SOURCE = {
    "connector_pull": "toorow",
    "external_bq": "external",
    "managed_feed": "toorow",
}


@dataclass(frozen=True)
class IntentIssue:
    """One deterministic and actionable intent problem."""

    code: str
    path: str
    message: str
    repair: dict[str, Any]
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "repair": self.repair,
            "details": self.details,
        }


@dataclass(frozen=True)
class IntentValidationResult:
    """Normalized immutable payload plus its execution eligibility evidence."""

    normalized_intent: dict[str, Any]
    content_hash: str
    executable: bool
    issues: tuple[IntentIssue, ...]
    capability_contract_version: str | None
    capability_fingerprint: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalized_intent": self.normalized_intent,
            "content_hash": self.content_hash,
            "executable": self.executable,
            "issues": [issue.as_dict() for issue in self.issues],
            "capability_contract_version": self.capability_contract_version,
            "capability_fingerprint": self.capability_fingerprint,
        }


class DatastreamIntentStructuralError(ValueError):
    """Raised when an intent is unsafe to persist even as a draft."""

    def __init__(self, issues: list[IntentIssue]):
        self.issues = tuple(_sorted_issues(issues))
        super().__init__("; ".join(f"{item.path}: {item.message}" for item in self.issues))


class DatastreamIntentNotFound(LookupError):
    """The project-scoped Datastream does not exist."""


class DatastreamIntentConflict(ValueError):
    """An idempotency key was reused with different content."""


class DatastreamIntentUnavailable(RuntimeError):
    """The intent could not be validated or stored safely."""


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_path(parts: Any) -> str:
    values = [str(part) for part in parts]
    return "$" if not values else "$." + ".".join(values)


def _sorted_issues(issues: list[IntentIssue]) -> list[IntentIssue]:
    return sorted(issues, key=lambda item: (item.path, item.code, item.message))


def _schema_validator() -> jsonschema.Draft202012Validator:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(
        schema,
        format_checker=jsonschema.Draft202012Validator.FORMAT_CHECKER,
    )


def _normalize_sequence(values: list[Any]) -> list[Any]:
    if all(isinstance(value, str) for value in values):
        return sorted(values)
    return sorted(values, key=_canonical_json)


def _normalize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _normalize_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    return value


def normalize_intent(intent: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Derive ownership, validate structural safety, and canonicalize an intent."""

    if not isinstance(intent, dict):
        raise DatastreamIntentStructuralError(
            [
                IntentIssue(
                    code="invalid_intent_schema",
                    path="$",
                    message="Intent must be an object.",
                    repair={"action": "send_object"},
                    details={},
                )
            ]
        )

    normalized = deepcopy(intent)
    source = normalized.get("source")
    if isinstance(source, dict):
        source_kind = source.get("kind")
        expected_writer = _WRITER_BY_SOURCE.get(source_kind)
        if expected_writer is not None and "writer_kind" not in source:
            source["writer_kind"] = expected_writer
        selection = source.get("selection")
        if isinstance(selection, dict):
            for key in ("metrics", "dimensions", "grain"):
                if isinstance(selection.get(key), list):
                    selection[key] = _normalize_sequence(selection[key])
            if isinstance(selection.get("filters"), list):
                selection["filters"] = sorted(
                    [_normalize_value(item) for item in selection["filters"]],
                    key=_canonical_json,
                )

    normalized = _normalize_value(normalized)
    issues: list[IntentIssue] = []
    for error in sorted(
        _schema_validator().iter_errors(normalized),
        key=lambda item: (_json_path(item.absolute_path), item.message),
    ):
        issues.append(
            IntentIssue(
                code="invalid_intent_schema",
                path=_json_path(error.absolute_path),
                message=error.message,
                repair={"action": "satisfy_schema"},
                details={},
            )
        )

    timezone_name = normalized.get("schedule", {}).get("timezone")
    if isinstance(timezone_name, str):
        try:
            ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            issues.append(
                IntentIssue(
                    code="invalid_timezone",
                    path="$.schedule.timezone",
                    message=f"Unknown IANA timezone {timezone_name!r}.",
                    repair={"timezone": "UTC"},
                    details={"requested_timezone": timezone_name},
                )
            )

    if issues:
        raise DatastreamIntentStructuralError(issues)

    encoded = _canonical_json(normalized)
    return normalized, _sha256(encoded)


def _posture_fingerprint(posture: GeographicPosture) -> str:
    return _sha256(_canonical_json(posture.as_dict()))


def geographic_snapshot_matches(
    snapshot: dict[str, Any] | None, posture: GeographicPosture
) -> bool:
    return bool(snapshot) and snapshot.get("posture_fingerprint") == _posture_fingerprint(posture)


def _country_fields(report: dict[str, Any], capabilities: dict[str, Any]) -> list[str]:
    report_dimensions = set(report.get("dimensions", []))
    candidates = {
        str(field.get("field_id"))
        for field in capabilities.get("fields", [])
        if field.get("kind") == "dimension"
        and field.get("canonical_target") == "country"
        and field.get("field_id") in report_dimensions
    }
    if "country" in report_dimensions:
        candidates.add("country")
    return sorted(candidates)


def compile_geographic_intent(
    intent: dict[str, Any],
    posture: GeographicPosture,
    capabilities: dict[str, Any] | None,
) -> dict[str, Any]:
    """Compile project geography into a deterministic immutable plan snapshot."""

    compiled = deepcopy(intent)
    source = compiled.get("source", {})
    selection = source.get("selection", {})
    previous_snapshot = compiled.get("geographic")
    previous_impact = (
        previous_snapshot.get("impact", {}) if isinstance(previous_snapshot, dict) else {}
    )
    previous_added = set(previous_impact.get("added_dimensions", []))
    dimensions = sorted(set(selection.get("dimensions", [])) - previous_added)
    grain_before = sorted(set(previous_impact.get("grain_before", selection.get("grain", []))))
    selection["dimensions"] = dimensions
    selection["grain"] = grain_before

    report = None
    if capabilities:
        report = next(
            (
                item
                for item in capabilities.get("reports", [])
                if item.get("id") == source.get("report_id")
            ),
            None,
        )
    quota_cost = report.get("quota_cost") if report else None
    status = "consolidated"
    effective_country_field = None
    grain_after = list(grain_before)
    added_dimensions: list[str] = []

    if posture.mode == "global":
        existing_country_fields: list[str] = []
        if source.get("kind") == "connector_pull" and report and capabilities:
            existing_country_fields = [
                field
                for field in _country_fields(report, capabilities)
                if field in dimensions and field in grain_before
            ]
        elif "country" in dimensions and "country" in grain_before:
            existing_country_fields = ["country"]
        if existing_country_fields:
            status = "preserved_full_grain"
            effective_country_field = existing_country_fields[0]
    elif source.get("kind") == "connector_pull" and report and capabilities:
        compatible: list[tuple[list[str], str]] = []
        before_set = set(grain_before)
        for country_field in _country_fields(report, capabilities):
            for supported in report.get("supported_grains", []):
                candidate = sorted(set(supported))
                if before_set <= set(candidate) and country_field in candidate:
                    compatible.append((candidate, country_field))
        if compatible:
            grain_after, effective_country_field = min(
                compatible, key=lambda item: (len(item[0]), item[0], item[1])
            )
            added_dimensions = sorted(set(grain_after) - set(dimensions))
            selection["dimensions"] = sorted(set(dimensions) | set(grain_after))
            selection["grain"] = grain_after
            status = "country_complete"
        else:
            status = "blocked"
    elif "country" in dimensions and "country" in grain_before:
        status = "preserved_full_grain"
        effective_country_field = "country"
    else:
        status = "blocked"

    snapshot = {
        "mode": posture.mode,
        "country_codes": list(posture.country_codes),
        "posture_fingerprint": _posture_fingerprint(posture),
        "compilation_status": status,
        "effective_country_field": effective_country_field,
        "impact": {
            "grain_before": grain_before,
            "grain_after": grain_after,
            "added_dimensions": added_dimensions,
            "tracked_country_count": len(posture.country_codes),
            "quota_cost": quota_cost,
            "cardinality_estimate": (
                "provider_country_cardinality" if status == "country_complete" else "unchanged"
            ),
        },
    }
    compiled["geographic"] = snapshot
    return compiled


def _validate_geographic(
    intent: dict[str, Any],
    capabilities: dict[str, Any] | None,
    issues: list[IntentIssue],
) -> None:
    snapshot = intent.get("geographic")
    if not isinstance(snapshot, dict) or snapshot.get("compilation_status") != "blocked":
        return
    source = intent.get("source", {})
    module_name = (
        (capabilities or {}).get("module", {}).get("name") or source.get("kind") or "unknown"
    )
    report_id = source.get("report_id") or source.get("kind") or "unknown"
    _add_issue(
        issues,
        code="geographic_country_unavailable",
        path="$.source.selection.grain",
        message=(
            f"Source {module_name!r} report {report_id!r} cannot provide a "
            "country-compatible joint grain for this Local markets project."
        ),
        repair={
            "action": "select_country_compatible_report",
            "alternatives": ["change_report", "change_metrics", "use_global_mode"],
        },
        details={"module_name": module_name, "report_id": report_id},
    )


def _add_issue(
    issues: list[IntentIssue],
    *,
    code: str,
    path: str,
    message: str,
    repair: dict[str, Any],
    details: dict[str, Any] | None = None,
) -> None:
    issues.append(
        IntentIssue(
            code=code,
            path=path,
            message=message,
            repair=repair,
            details=details or {},
        )
    )


def _validate_time_policy(intent: dict[str, Any], issues: list[IntentIssue]) -> None:
    historical = intent["historical"]
    if historical["start"] is not None and historical["end_exclusive"] is not None:
        start = datetime.fromisoformat(historical["start"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(historical["end_exclusive"].replace("Z", "+00:00"))
        if start >= end:
            _add_issue(
                issues,
                code="invalid_historical_window",
                path="$.historical",
                message="Historical start must be before end_exclusive.",
                repair={"action": "move_end_after_start"},
            )

    retry = intent["schedule"]["retry"]
    if retry["max_backoff_seconds"] < retry["initial_backoff_seconds"]:
        _add_issue(
            issues,
            code="invalid_retry_policy",
            path="$.schedule.retry.max_backoff_seconds",
            message="Maximum backoff cannot be below initial backoff.",
            repair={"minimum": retry["initial_backoff_seconds"]},
        )


def _selected_fields(selection: dict[str, Any]) -> set[str]:
    return set(selection["metrics"] + selection["dimensions"])


def _validate_compatibility(
    selection: dict[str, Any], report: dict[str, Any], issues: list[IntentIssue]
) -> None:
    selected = _selected_fields(selection)
    for constraint in report.get("compatibility", []):
        constrained = set(constraint.get("field_ids", []))
        kind = constraint.get("constraint")
        violated = (kind == "incompatible_with" and constrained <= selected) or (
            kind == "requires_all" and bool(selected & constrained) and not constrained <= selected
        )
        if violated:
            _add_issue(
                issues,
                code=constraint.get("reason_code", "incompatible_report_fields"),
                path="$.source.selection",
                message=constraint.get("description", "Selected fields are incompatible."),
                repair=constraint.get("suggested_repair", {}),
                details={"field_ids": sorted(constrained)},
            )


def _validate_connector(
    intent: dict[str, Any], capabilities: dict[str, Any] | None, issues: list[IntentIssue]
) -> tuple[str | None, str | None]:
    if capabilities is None:
        _add_issue(
            issues,
            code="capabilities_unavailable",
            path="$.source",
            message="Connector capabilities are unavailable.",
            repair={"action": "retry_validation"},
        )
        return None, None

    capability_fingerprint = _sha256(_canonical_json(capabilities))
    contract_version = str(capabilities.get("contract_version", "")) or None
    source = intent["source"]
    if capabilities.get("connection_ref_id") != source.get("connection_ref_id"):
        _add_issue(
            issues,
            code="capability_scope_mismatch",
            path="$.source.connection_ref_id",
            message="Capability catalog does not belong to the selected connection.",
            repair={"action": "reload_source_catalog"},
        )
        return contract_version, capability_fingerprint

    report = next(
        (
            item
            for item in capabilities.get("reports", [])
            if item.get("id") == source.get("report_id")
        ),
        None,
    )
    if report is None:
        _add_issue(
            issues,
            code="unknown_report",
            path="$.source.report_id",
            message="Selected report is not present in the governed catalog.",
            repair={"action": "select_available_report"},
        )
        return contract_version, capability_fingerprint

    availability = report.get("availability", {})
    if availability.get("status") != "selectable":
        _add_issue(
            issues,
            code=availability.get("reason_code", "report_unavailable"),
            path="$.source.report_id",
            message="Selected report is not currently selectable.",
            repair={"follow_up": availability.get("follow_up")},
        )

    selection = source["selection"]
    if not selection["metrics"] or not selection["dimensions"] or not selection["grain"]:
        _add_issue(
            issues,
            code="incomplete_selection",
            path="$.source.selection",
            message="Connector selection requires metrics, dimensions, and grain.",
            repair={"action": "complete_selection"},
        )

    allowed_metrics = set(report.get("metrics", []))
    allowed_dimensions = set(report.get("dimensions", []))
    unknown = sorted(
        (set(selection["metrics"]) - allowed_metrics)
        | (set(selection["dimensions"]) - allowed_dimensions)
    )
    for field_id in unknown:
        _add_issue(
            issues,
            code="unknown_report_field",
            path="$.source.selection",
            message=f"Field {field_id!r} is not available for this report.",
            repair={"remove_fields": [field_id]},
            details={"field_id": field_id},
        )

    if report.get("selection_mode") == "exact_bundle" and (
        set(selection["metrics"]) != allowed_metrics
        or set(selection["dimensions"]) != allowed_dimensions
    ):
        _add_issue(
            issues,
            code="exact_bundle_required",
            path="$.source.selection",
            message="This report requires its complete field bundle.",
            repair={
                "metrics": sorted(allowed_metrics),
                "dimensions": sorted(allowed_dimensions),
            },
        )

    requested_grain = tuple(sorted(selection["grain"]))
    supported_grains = {tuple(sorted(grain)) for grain in report.get("supported_grains", [])}
    if selection["grain"] and requested_grain not in supported_grains:
        _add_issue(
            issues,
            code="unsupported_grain",
            path="$.source.selection.grain",
            message="Selected grain is not supported by this report.",
            repair={"supported_grains": [list(item) for item in sorted(supported_grains)]},
        )

    filter_specs = {item["field_id"]: set(item["operators"]) for item in report.get("filters", [])}
    for index, selected_filter in enumerate(selection["filters"]):
        supported = filter_specs.get(selected_filter["field_id"], set())
        if selected_filter["operator"] not in supported:
            _add_issue(
                issues,
                code="unsupported_filter",
                path=f"$.source.selection.filters.{index}",
                message="Selected filter is not supported by this report.",
                repair={"supported_operators": sorted(supported)},
            )

    _validate_compatibility(selection, report, issues)

    schedule = intent["schedule"]
    cadence = report.get("cadence", {})
    supported_modes = cadence.get("supported_modes", [])
    minimum = cadence.get("minimum_interval_minutes")
    interval = schedule["interval_minutes"]
    if schedule["mode"] not in supported_modes or (
        interval is not None and isinstance(minimum, int) and interval < minimum
    ):
        preferred_mode = "daily" if "daily" in supported_modes else supported_modes[0]
        _add_issue(
            issues,
            code="unsupported_cadence",
            path="$.schedule",
            message="Selected cadence is not supported by this report.",
            repair={"mode": preferred_mode, "minimum_interval_minutes": minimum},
            details={
                "requested_mode": schedule["mode"],
                "requested_interval_minutes": interval,
                "supported_modes": supported_modes,
                "minimum_interval_minutes": minimum,
                "quota_cost": report.get("quota_cost"),
            },
        )

    return contract_version, capability_fingerprint


def validate_intent(
    intent: dict[str, Any], *, capabilities: dict[str, Any] | None = None
) -> IntentValidationResult:
    """Validate normalized intent semantics without any provider or warehouse call."""

    normalized, content_hash = normalize_intent(intent)
    issues: list[IntentIssue] = []
    _validate_time_policy(normalized, issues)
    contract_version = None
    capability_fingerprint = None
    if normalized["source"]["kind"] == "connector_pull":
        contract_version, capability_fingerprint = _validate_connector(
            normalized, capabilities, issues
        )
    elif not _selected_fields(normalized["source"]["selection"]):
        _add_issue(
            issues,
            code="incomplete_selection",
            path="$.source.selection",
            message="At least one selected field is required for execution.",
            repair={"action": "select_fields"},
        )

    _validate_geographic(normalized, capabilities, issues)

    ordered = tuple(_sorted_issues(issues))
    return IntentValidationResult(
        normalized_intent=normalized,
        content_hash=content_hash,
        executable=not ordered,
        issues=ordered,
        capability_contract_version=contract_version,
        capability_fingerprint=capability_fingerprint,
    )


def _mint_plan_id() -> str:
    return f"dsp_{ULID()}"


def _version_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    keys = (
        "id",
        "datastream_id",
        "project_id",
        "version_number",
        "contract_version",
        "normalized_payload",
        "content_hash",
        "capability_contract_version",
        "capability_fingerprint",
        "executable",
        "validation_issues",
        "created_by",
        "created_at",
    )
    result = dict(zip(keys, row))
    if result.get("created_at") is not None:
        result["created_at"] = result["created_at"].isoformat()
    return result


def save_datastream_intent(
    *,
    datastream_id: str,
    project_id: str,
    intent: dict[str, Any],
    identity: str,
    idempotency_key: str,
    conn: Any,
    loaded_modules: list[Any] | None = None,
    geographic_posture: GeographicPosture | None = None,
    capabilities: dict[str, Any] | None = None,
    reason: str = "draft_saved",
    trace_id: str | None = None,
    now_utc: datetime | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Append one immutable version and atomically move its Datastream pointer."""

    if not idempotency_key.strip():
        raise ValueError("Idempotency-Key is required")
    idempotency_hash = _sha256(idempotency_key)
    normalized, _initial_hash = normalize_intent(intent)
    source = normalized["source"]

    if source["kind"] == "connector_pull" and capabilities is None:
        if loaded_modules is None:
            raise DatastreamIntentUnavailable("loaded module catalog is required")
        from core.source_capabilities import (  # noqa: PLC0415
            SourceCapabilitiesNotFound,
            SourceCapabilitiesUnavailable,
            get_scoped_source_capabilities,
        )

        try:
            capabilities = get_scoped_source_capabilities(
                project_id=project_id,
                connection_ref_id=source["connection_ref_id"],
                identity=identity,
                loaded_modules=loaded_modules,
                conn=conn,
            )
        except SourceCapabilitiesNotFound as exc:
            raise DatastreamIntentNotFound from exc
        except SourceCapabilitiesUnavailable as exc:
            raise DatastreamIntentUnavailable from exc

    compiled = (
        compile_geographic_intent(normalized, geographic_posture, capabilities)
        if geographic_posture is not None
        else normalized
    )
    normalized, content_hash = normalize_intent(compiled)
    validation = validate_intent(normalized, capabilities=capabilities)
    issues_json = json.dumps([issue.as_dict() for issue in validation.issues])
    payload_json = _canonical_json(validation.normalized_intent)

    from core.datastream_schedule import calculate_schedule_window  # noqa: PLC0415

    schedule_window = calculate_schedule_window(
        validation.normalized_intent["schedule"],
        now_utc=now_utc or datetime.now(UTC),
    )

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, current_plan_version_id
                FROM app.datastreams
                WHERE id = %s AND project_id = %s
                FOR UPDATE
                """,
                (datastream_id, project_id),
            )
            datastream_row = cur.fetchone()
            if datastream_row is None:
                raise DatastreamIntentNotFound
            previous_version_id = datastream_row[1]
            # The row lock serializes version allocation and same-key retries.
            cur.execute(
                """
                SELECT id, datastream_id, project_id, version_number, contract_version,
                       normalized_payload, content_hash, capability_contract_version,
                       capability_fingerprint, executable, validation_issues,
                       created_by, created_at
                FROM app.datastream_plan_versions
                WHERE project_id = %s AND idempotency_key_hash = %s
                """,
                (project_id, idempotency_hash),
            )
            existing = cur.fetchone()
            if existing is not None:
                result = _version_dict(existing)
                if (
                    result["datastream_id"] != datastream_id
                    or result["content_hash"] != content_hash
                ):
                    raise DatastreamIntentConflict("idempotency_conflict")
                result["idempotent_replay"] = True
                return result
            cur.execute(
                """
                SELECT COALESCE(MAX(version_number), 0) + 1
                FROM app.datastream_plan_versions
                WHERE datastream_id = %s AND project_id = %s
                """,
                (datastream_id, project_id),
            )
            version_number = int(cur.fetchone()[0])

        plan_id = _mint_plan_id()
        with conn.cursor() as cur:
            try:
                cur.execute(
                    """
                    INSERT INTO app.datastream_plan_versions
                        (id, datastream_id, project_id, version_number, contract_version,
                         source_kind, writer_kind, destination_policy, normalized_payload,
                         content_hash, capability_contract_version, capability_fingerprint,
                         executable, validation_issues, idempotency_key_hash, created_by)
                    VALUES
                        (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb,
                         %s, %s, %s, %s, %s::jsonb, %s, %s)
                    RETURNING id, datastream_id, project_id, version_number,
                              contract_version, normalized_payload, content_hash,
                              capability_contract_version, capability_fingerprint,
                              executable, validation_issues, created_by, created_at
                    """,
                    (
                        plan_id,
                        datastream_id,
                        project_id,
                        version_number,
                        validation.normalized_intent["contract_version"],
                        source["kind"],
                        source["writer_kind"],
                        validation.normalized_intent["destination"]["policy"],
                        payload_json,
                        content_hash,
                        validation.capability_contract_version,
                        validation.capability_fingerprint,
                        validation.executable,
                        issues_json,
                        idempotency_hash,
                        identity,
                    ),
                )
            except Exception as _insert_exc:  # noqa: BLE001
                import psycopg.errors  # noqa: PLC0415

                if isinstance(_insert_exc, psycopg.errors.UniqueViolation):
                    # Concurrent race on UNIQUE(project_id, idempotency_key_hash)
                    # for a DIFFERENT datastream: deterministic 409, not 503.
                    raise DatastreamIntentConflict("idempotency_conflict") from _insert_exc
                raise
            inserted = cur.fetchone()
            cur.execute(
                """
                INSERT INTO app.datastream_schedule_state
                    (plan_version_id, datastream_id, project_id, scheduled_for,
                     window_start, window_end, next_run_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    plan_id,
                    datastream_id,
                    project_id,
                    schedule_window.scheduled_for,
                    schedule_window.window_start,
                    schedule_window.window_end,
                    schedule_window.next_run_at,
                ),
            )
            cur.execute(
                """
                UPDATE app.datastreams
                SET current_plan_version_id = %s,
                    source_kind = %s,
                    module_name = %s,
                    enabled = FALSE,
                    archived_at = NULL,
                    archived_by = NULL
                WHERE id = %s AND project_id = %s
                """,
                (
                    plan_id,
                    source["kind"],
                    capabilities["module"]["name"]
                    if source["kind"] == "connector_pull" and capabilities
                    else None,
                    datastream_id,
                    project_id,
                ),
            )

        from core.audit import ACTION_DATASTREAM_INTENT_VERSIONED, insert_audit_row  # noqa: PLC0415

        insert_audit_row(
            conn,
            identity=identity,
            action=ACTION_DATASTREAM_INTENT_VERSIONED,
            provider_account="",
            connection_ref=source.get("connection_ref_id", ""),
            metadata={
                "project_id": project_id,
                "datastream_id": datastream_id,
                "previous_version_id": previous_version_id,
                "current_version_id": plan_id,
                "idempotency_key_hash": idempotency_hash,
                "reason": reason,
                "capability_fingerprint": validation.capability_fingerprint,
                "trace_id": trace_id,
            },
        )
        if commit:
            conn.commit()
    except (DatastreamIntentConflict, DatastreamIntentNotFound):
        conn.rollback()
        raise
    except Exception as exc:
        conn.rollback()
        raise DatastreamIntentUnavailable("intent persistence failed") from exc

    result = _version_dict(inserted)
    result["idempotent_replay"] = False
    return result


def list_intent_versions(datastream_id: str, project_id: str, conn: Any) -> list[dict[str, Any]]:
    """Return immutable versions newest-first within one project scope."""

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, datastream_id, project_id, version_number, contract_version,
                   normalized_payload, content_hash, capability_contract_version,
                   capability_fingerprint, executable, validation_issues,
                   created_by, created_at
            FROM app.datastream_plan_versions
            WHERE datastream_id = %s AND project_id = %s
            ORDER BY version_number DESC
            """,
            (datastream_id, project_id),
        )
        return [_version_dict(row) for row in cur.fetchall()]
