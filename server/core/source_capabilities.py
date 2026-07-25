"""Generic validation and normalization for source capability descriptors.

The module deliberately contains no connector identifiers or provider field names.
Source vocabulary lives in manifests; core only enforces the versioned contract.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

_SCHEMA_PATH = Path(__file__).parent / "schemas" / "source-capabilities.schema.json"
_SCHEMA_ID = "https://toorow.dev/schemas/source-capabilities.schema.json"
_DESCRIPTOR_REF = f"{_SCHEMA_ID}#/$defs/descriptor"
_PUBLIC_RESPONSE_REF = f"{_SCHEMA_ID}#/$defs/public_response"


@dataclass(frozen=True, order=True)
class CapabilityIssue:
    """One deterministic, actionable capability-contract problem."""

    path: str
    code: str
    message: str
    suggested_repair: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "suggested_repair": self.suggested_repair,
        }


class CapabilityValidationError(ValueError):
    """Raised when a descriptor cannot be normalized safely."""

    def __init__(self, issues: list[CapabilityIssue]):
        self.issues = sorted(issues)
        super().__init__("; ".join(f"{item.path}: {item.message}" for item in self.issues))


def _load_schema() -> dict[str, Any]:
    schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return schema


def _validator(ref: str) -> jsonschema.Draft202012Validator:
    schema = _load_schema()
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    return jsonschema.Draft202012Validator({"$ref": ref}, registry=registry)


def _json_path(parts: Any) -> str:
    values = [str(part) for part in parts]
    return "$" if not values else "$." + ".".join(values)


def _schema_issues(instance: dict[str, Any], ref: str) -> list[CapabilityIssue]:
    issues = []
    errors = sorted(
        _validator(ref).iter_errors(instance),
        key=lambda error: (tuple(str(item) for item in error.absolute_path), error.message),
    )
    for error in errors:
        issues.append(
            CapabilityIssue(
                path=_json_path(error.absolute_path),
                code="invalid_capability_schema",
                message=error.message,
                suggested_repair="Update the value to satisfy the source-capabilities schema.",
            )
        )
    return issues


def validate_descriptor_schema(descriptor: dict[str, Any]) -> list[CapabilityIssue]:
    """Return structural errors for a source capability descriptor."""

    return _schema_issues(descriptor, _DESCRIPTOR_REF)


def validate_public_response(response: dict[str, Any]) -> list[CapabilityIssue]:
    """Return structural errors for a normalized public response."""

    return _schema_issues(response, _PUBLIC_RESPONSE_REF)


def _add_issue(
    issues: list[CapabilityIssue],
    *,
    path: str,
    code: str,
    message: str,
    suggested_repair: str,
) -> None:
    issues.append(
        CapabilityIssue(
            path=path,
            code=code,
            message=message,
            suggested_repair=suggested_repair,
        )
    )


def _canonical_mapping_target(value: Any) -> tuple[str | None, str | None, bool | None]:
    """Return canonical target plus optional aggregation metadata from a legacy mapping."""

    if isinstance(value, str):
        return value, None, None
    if isinstance(value, dict):
        return (
            value.get("canonical"),
            value.get("aggregation_rule"),
            value.get("non_additive"),
        )
    return None, None, None


def validate_manifest_capabilities(manifest: dict[str, Any]) -> list[CapabilityIssue]:
    """Validate one manifest descriptor structurally and semantically.

    Structural errors short-circuit semantic checks because required shapes may be
    absent. Returned issues are sorted by JSON path, code, and message.
    """

    descriptor = manifest.get("source_capabilities")
    if not isinstance(descriptor, dict):
        return [
            CapabilityIssue(
                path="$.source_capabilities",
                code="capabilities_unavailable",
                message="Manifest does not contain a source capability descriptor.",
                suggested_repair="Add a schema-version 1.2 source_capabilities block.",
            )
        ]

    structural = validate_descriptor_schema(descriptor)
    if structural:
        return sorted(structural)

    issues: list[CapabilityIssue] = []
    fields = descriptor["fields"]
    reports = descriptor["reports"]
    field_by_id: dict[str, dict[str, Any]] = {}
    canonical_mappings = {
        **manifest.get("canonical_metric_mapping", {}),
        **manifest.get("canonical_dimension_mapping", {}),
    }

    for index, field in enumerate(fields):
        field_id = field["field_id"]
        if field_id in field_by_id:
            _add_issue(
                issues,
                path=f"$.source_capabilities.fields.{index}.field_id",
                code="duplicate_field_id",
                message=f"Field ID {field_id!r} is declared more than once.",
                suggested_repair="Give every field a unique stable field_id.",
            )
        else:
            field_by_id[field_id] = field

        field_path = f"$.source_capabilities.fields.{index}"
        mapping_value = canonical_mappings.get(field["source_field"])
        if mapping_value is not None:
            mapped_target, mapped_aggregation, mapped_non_additive = (
                _canonical_mapping_target(mapping_value)
            )
            if mapped_target != field["canonical_target"]:
                _add_issue(
                    issues,
                    path=f"{field_path}.canonical_target",
                    code="canonical_mapping_mismatch",
                    message=(
                        f"Field {field_id!r} targets {field['canonical_target']!r}, "
                        f"but the manifest mapping targets {mapped_target!r}."
                    ),
                    suggested_repair="Align canonical_target with the manifest mapping.",
                )
            if (
                mapped_aggregation is not None
                and mapped_aggregation != field["aggregation"]
            ) or (
                mapped_non_additive is not None
                and bool(mapped_non_additive) != field["non_additive"]
            ):
                _add_issue(
                    issues,
                    path=f"{field_path}.aggregation",
                    code="canonical_semantics_mismatch",
                    message=f"Field {field_id!r} disagrees with its canonical mapping semantics.",
                    suggested_repair=(
                        "Align aggregation and non_additive with the manifest mapping."
                    ),
                )

        aggregation = field["aggregation"]
        non_additive = field["non_additive"]
        semantics_invalid = (
            field["kind"] == "dimension"
            and (aggregation != "none" or non_additive)
        ) or (
            field["kind"] == "metric"
            and (
                (aggregation == "sum" and non_additive)
                or (aggregation != "sum" and not non_additive)
            )
        )
        if semantics_invalid:
            _add_issue(
                issues,
                path=f"{field_path}.aggregation",
                code="invalid_additivity",
                message=f"Field {field_id!r} has contradictory kind/additivity semantics.",
                suggested_repair=(
                    "Use aggregation='none' for dimensions, additive sum for additive "
                    "metrics, and non_additive=true for every other metric aggregation."
                ),
            )

    report_by_id: dict[str, dict[str, Any]] = {}
    for index, report in enumerate(reports):
        report_id = report["id"]
        report_path = f"$.source_capabilities.reports.{index}"
        if report_id in report_by_id:
            _add_issue(
                issues,
                path=f"{report_path}.id",
                code="duplicate_report_id",
                message=f"Report ID {report_id!r} is declared more than once.",
                suggested_repair="Give every report a unique stable id.",
            )
        else:
            report_by_id[report_id] = report

        report_field_ids = set(report["metrics"] + report["dimensions"])
        if not report_field_ids or (
            report["availability"]["status"] == "selectable"
            and not report["supported_grains"]
        ):
            _add_issue(
                issues,
                path=report_path,
                code="empty_report_template",
                message=(
                    f"Report {report_id!r} does not expose a usable field and grain contract."
                ),
                suggested_repair=(
                    "Declare at least one field and one supported grain, or mark the "
                    "profile unavailable until its scalar/grain behavior is modeled."
                ),
            )

        for kind, expected_kind in (("metrics", "metric"), ("dimensions", "dimension")):
            for field_index, field_id in enumerate(report[kind]):
                field = field_by_id.get(field_id)
                path = f"{report_path}.{kind}.{field_index}"
                if field is None:
                    _add_issue(
                        issues,
                        path=path,
                        code="unknown_report_field",
                        message=f"Report references unknown field {field_id!r}.",
                        suggested_repair="Declare the field or remove it from the report.",
                    )
                elif field["kind"] != expected_kind:
                    _add_issue(
                        issues,
                        path=path,
                        code="field_kind_mismatch",
                        message=f"Field {field_id!r} is not a {expected_kind}.",
                        suggested_repair=f"Move the field to the {field['kind']} collection.",
                    )

        for grain_index, grain in enumerate(report["supported_grains"]):
            for field_index, field_id in enumerate(grain):
                field = field_by_id.get(field_id)
                if (
                    field is None
                    or field["kind"] != "dimension"
                    or field_id not in report["dimensions"]
                ):
                    _add_issue(
                        issues,
                        path=f"{report_path}.supported_grains.{grain_index}.{field_index}",
                        code="unsupported_grain",
                        message=f"Grain field {field_id!r} is not a declared report dimension.",
                        suggested_repair=(
                            "Use only declared dimension field IDs in supported_grains."
                        ),
                    )

        for filter_index, filter_spec in enumerate(report["filters"]):
            field_id = filter_spec["field_id"]
            if (
                field_id not in field_by_id
                or field_id not in report["metrics"] + report["dimensions"]
            ):
                _add_issue(
                    issues,
                    path=f"{report_path}.filters.{filter_index}.field_id",
                    code="unsupported_filter",
                    message=f"Filter targets unsupported field {field_id!r}.",
                    suggested_repair="Target a field declared by this report.",
                )

        for constraint_index, constraint in enumerate(report["compatibility"]):
            constraint_path = f"{report_path}.compatibility.{constraint_index}"
            repair = constraint["suggested_repair"]

            untyped_refs = (
                list(constraint["field_ids"])
                + list(repair["remove_fields"])
                + list(repair["add_fields"])
            )
            for field_id in sorted(set(untyped_refs)):
                if field_id not in report_field_ids:
                    _add_issue(
                        issues,
                        path=constraint_path,
                        code="unknown_constraint_field",
                        message=(
                            f"Compatibility metadata references field {field_id!r} "
                            "outside the declaring report."
                        ),
                        suggested_repair=(
                            "Reference only fields declared by the current report."
                        ),
                    )

            alternatives = list(constraint["supported_alternatives"]) + list(
                repair["split_into_reports"]
            )
            for alternative_index, alternative in enumerate(alternatives):
                for kind, expected_kind in (
                    ("metrics", "metric"),
                    ("dimensions", "dimension"),
                ):
                    for field_id in alternative[kind]:
                        field = field_by_id.get(field_id)
                        if (
                            field_id not in report_field_ids
                            or field is None
                            or field["kind"] != expected_kind
                        ):
                            _add_issue(
                                issues,
                                path=f"{constraint_path}.{kind}.{alternative_index}",
                                code="invalid_constraint_alternative",
                                message=(
                                    f"Alternative {kind} field {field_id!r} is not a "
                                    f"declared report {expected_kind}."
                                ),
                                suggested_repair=(
                                    "Use current-report fields in their matching metric "
                                    "or dimension collection."
                                ),
                            )

            for grain_index, grain in enumerate(repair.get("supported_grains", [])):
                for field_id in grain:
                    field = field_by_id.get(field_id)
                    if (
                        field_id not in report["dimensions"]
                        or field is None
                        or field["kind"] != "dimension"
                    ):
                        _add_issue(
                            issues,
                            path=f"{constraint_path}.suggested_repair.supported_grains.{grain_index}",
                            code="invalid_repair_grain",
                            message=(
                                f"Repair grain field {field_id!r} is not a current-report "
                                "dimension."
                            ),
                            suggested_repair=(
                                "Use only dimensions declared by the current report."
                            ),
                        )

            reason_code = constraint["reason_code"]
            typed_repair_missing = (
                reason_code == "unsupported_cadence"
                and not (
                    repair.get("minimum_interval_minutes")
                    or repair.get("supported_modes")
                )
            ) or (
                reason_code == "unsupported_grain"
                and not (
                    repair.get("supported_grains")
                    or repair.get("split_into_reports")
                )
            ) or (
                reason_code == "unsupported_filter"
                and not (
                    repair.get("supported_filter_operators")
                    or repair.get("remove_fields")
                )
            )
            if typed_repair_missing:
                _add_issue(
                    issues,
                    path=f"{constraint_path}.suggested_repair",
                    code="invalid_repair_metadata",
                    message=f"Constraint {reason_code!r} lacks a typed deterministic repair.",
                    suggested_repair=(
                        "Declare supported cadence, grain, or filter alternatives for "
                        "the constraint reason."
                    ),
                )

        pagination = report["pagination"]
        bounded = pagination["completeness"] in {"bounded", "hard_limit", "top_n"}
        pagination_invalid = (
            (pagination["mode"] == "none" and pagination["max_pages"] is not None)
            or (bounded and pagination["max_pages"] is None and pagination["row_limit"] is None)
            or (bounded and pagination["truncation_signal"] == "none")
            or (
                pagination["completeness"] == "complete"
                and pagination["truncation_signal"] != "none"
            )
        )
        if pagination_invalid:
            _add_issue(
                issues,
                path=f"{report_path}.pagination",
                code="contradictory_pagination",
                message="Pagination, completeness, bound, and truncation declarations conflict.",
                suggested_repair=(
                    "Declare a truthful bound and truncation signal, or mark the report complete."
                ),
            )

        incremental = report["incremental"]
        cursor_id = incremental["cursor_field"]
        cursor_field = field_by_id.get(cursor_id) if cursor_id is not None else None
        cursor_invalid = (
            incremental["mode"] == "cursor"
            and (
                cursor_field is None
                or cursor_id not in report_field_ids
                or cursor_field["kind"] != "dimension"
            )
        ) or (incremental["mode"] != "cursor" and cursor_id is not None)
        if cursor_invalid:
            _add_issue(
                issues,
                path=f"{report_path}.incremental.cursor_field",
                code="invalid_cursor",
                message=(
                    "Incremental cursor must be an eligible dimension in the declaring "
                    "report and may only be set for cursor mode."
                ),
                suggested_repair=(
                    "Use a declared report dimension as the cursor, or clear it for "
                    "non-cursor modes."
                ),
            )

        cadence = report["cadence"]
        cadence_limits = {"hourly": 60, "daily": 1440, "weekly": 10080}
        contradictory_modes = sorted(
            mode
            for mode in cadence["supported_modes"]
            if mode in cadence_limits
            and cadence["minimum_interval_minutes"] > cadence_limits[mode]
        )
        if contradictory_modes:
            _add_issue(
                issues,
                path=f"{report_path}.cadence",
                code="unsupported_cadence",
                message=(
                    "Minimum interval conflicts with advertised cadence modes: "
                    + ", ".join(contradictory_modes)
                    + "."
                ),
                suggested_repair=(
                    "Remove the contradictory modes or lower minimum_interval_minutes "
                    "to their supported bound."
                ),
            )

        if report["availability"]["status"] == "unavailable" and "dispatch" in report:
            _add_issue(
                issues,
                path=f"{report_path}.dispatch",
                code="unavailable_profile_dispatch",
                message="An unavailable profile must not expose a runtime callable.",
                suggested_repair="Remove dispatch until the follow-up story proves the behavior.",
            )

    discovery = descriptor["field_discovery"]
    if discovery["mode"] == "runtime" and not discovery["allowed_targets"]:
        _add_issue(
            issues,
            path="$.source_capabilities.field_discovery.allowed_targets",
            code="runtime_discovery_unbounded",
            message="Runtime discovery has no bounded canonical targets.",
            suggested_repair="Declare the canonical targets runtime fields may map to.",
        )

    legacy_profiles: dict[str, dict[str, Any]] = {}
    for profile_index, profile in enumerate(manifest.get("report_profiles", [])):
        profile_id = profile.get("id")
        if profile_id in legacy_profiles:
            _add_issue(
                issues,
                path=f"$.report_profiles.{profile_index}.id",
                code="duplicate_profile_id",
                message=f"Transitional profile ID {profile_id!r} is declared more than once.",
                suggested_repair="Give every transitional report profile a unique stable id.",
            )
        elif isinstance(profile_id, str):
            legacy_profiles[profile_id] = profile
    for report_id, report in report_by_id.items():
        legacy = legacy_profiles.get(report_id)
        if legacy is None:
            _add_issue(
                issues,
                path="$.report_profiles",
                code="legacy_profile_missing",
                message=f"Capability report {report_id!r} has no transitional report profile.",
                suggested_repair="Add the same report ID to report_profiles.",
            )
            continue
        legacy_caps = legacy.get("extraction_capabilities", {})
        expected_realtime = report["cadence"]["minimum_interval_minutes"] <= 5
        aligned = (
            set(legacy.get("metrics", [])) == set(report["metrics"])
            and set(legacy.get("dimensions", [])) == set(report["dimensions"])
            and legacy_caps.get("row_limit") == report["pagination"]["row_limit"]
            and legacy_caps.get("filters_supported") == bool(report["filters"])
            and legacy_caps.get("realtime") == expected_realtime
        )
        if not aligned:
            _add_issue(
                issues,
                path=f"$.report_profiles.{report_id}",
                code="legacy_profile_mismatch",
                message=f"Transitional profile {report_id!r} disagrees with source_capabilities.",
                suggested_repair=(
                    "Align metrics, dimensions, row limit, filter support, and realtime metadata."
                ),
            )

    for report_id in sorted(set(legacy_profiles) - set(report_by_id)):
        _add_issue(
            issues,
            path="$.source_capabilities.reports",
            code="capability_profile_missing",
            message=f"Transitional profile {report_id!r} has no capability report.",
            suggested_repair="Add a capability report for every transitional profile.",
        )

    return sorted(issues)


def _normalize_field(field: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_id": field["field_id"],
        "source_field": field["source_field"],
        "kind": field["kind"],
        "physical_type": field["physical_type"],
        "description": field["description"],
        "semantic_hints": sorted(field["semantic_hints"]),
        "canonical_target": field["canonical_target"],
        "aggregation": field["aggregation"],
        "non_additive": field["non_additive"],
    }


def _normalize_alternative(alternative: dict[str, Any]) -> dict[str, Any]:
    return {
        "metrics": list(alternative["metrics"]),
        "dimensions": list(alternative["dimensions"]),
    }


def _normalize_constraint(constraint: dict[str, Any]) -> dict[str, Any]:
    repair = constraint["suggested_repair"]
    normalized_repair: dict[str, Any] = {
        "remove_fields": sorted(repair["remove_fields"]),
        "add_fields": sorted(repair["add_fields"]),
        "split_into_reports": [
            _normalize_alternative(item) for item in repair["split_into_reports"]
        ],
    }
    if "minimum_interval_minutes" in repair:
        normalized_repair["minimum_interval_minutes"] = repair[
            "minimum_interval_minutes"
        ]
    if "supported_modes" in repair:
        normalized_repair["supported_modes"] = sorted(repair["supported_modes"])
    if "supported_grains" in repair:
        normalized_repair["supported_grains"] = sorted(
            [list(grain) for grain in repair["supported_grains"]],
            key=lambda item: tuple(item),
        )
    if "supported_filter_operators" in repair:
        normalized_repair["supported_filter_operators"] = sorted(
            repair["supported_filter_operators"]
        )

    return {
        "reason_code": constraint["reason_code"],
        "description": constraint["description"],
        "constraint": constraint["constraint"],
        "field_ids": sorted(constraint["field_ids"]),
        "supported_alternatives": [
            _normalize_alternative(item) for item in constraint["supported_alternatives"]
        ],
        "suggested_repair": normalized_repair,
    }


def _normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    normalized: dict[str, Any] = {
        "id": report["id"],
        "selection_mode": report["selection_mode"],
        "availability": {
            key: report["availability"][key]
            for key in ("status", "reason_code", "follow_up")
            if key in report["availability"]
        },
        "metrics": list(report["metrics"]),
        "dimensions": list(report["dimensions"]),
        "supported_grains": sorted(
            [list(grain) for grain in report["supported_grains"]], key=lambda item: tuple(item)
        ),
        "compatibility": sorted(
            [_normalize_constraint(item) for item in report["compatibility"]],
            key=lambda item: (item["reason_code"], tuple(item["field_ids"])),
        ),
        "filters": sorted(
            [
                {"field_id": item["field_id"], "operators": sorted(item["operators"])}
                for item in report["filters"]
            ],
            key=lambda item: item["field_id"],
        ),
        "pagination": {
            key: report["pagination"][key]
            for key in ("mode", "completeness", "max_pages", "row_limit", "truncation_signal")
        },
        "quota_cost": {
            "read_points": report["quota_cost"]["read_points"],
            "unit": report["quota_cost"]["unit"],
        },
        "incremental": {
            "mode": report["incremental"]["mode"],
            "cursor_field": report["incremental"]["cursor_field"],
        },
        "cadence": {
            "minimum_interval_minutes": report["cadence"]["minimum_interval_minutes"],
            "supported_modes": sorted(report["cadence"]["supported_modes"]),
        },
    }
    if "dispatch" in report:
        normalized["dispatch"] = {"callable": report["dispatch"]["callable"]}
    return normalized


def normalize_capabilities(
    manifest: dict[str, Any], *, project_id: str, connection_ref_id: str
) -> dict[str, Any]:
    """Return the deterministic, allow-listed project/connection catalog."""

    issues = validate_manifest_capabilities(manifest)
    if issues:
        raise CapabilityValidationError(issues)
    descriptor = manifest["source_capabilities"]
    response = {
        "contract_version": descriptor["contract_version"],
        "project_id": project_id,
        "connection_ref_id": connection_ref_id,
        "module": {
            "name": manifest["name"],
            "display_name": manifest["display_name"],
            "module_kind": manifest.get("module_kind", "kpi"),
        },
        "field_discovery": {
            "mode": descriptor["field_discovery"]["mode"],
            "allowed_targets": sorted(descriptor["field_discovery"]["allowed_targets"]),
        },
        "fields": sorted(
            [_normalize_field(field) for field in descriptor["fields"]],
            key=lambda field: field["field_id"],
        ),
        "reports": sorted(
            [_normalize_report(report) for report in descriptor["reports"]],
            key=lambda report: report["id"],
        ),
    }
    # Story 39.7: project the descriptor-level time_context CAPTURE declaration deterministically
    # into the public response when present (one report timezone per datastream). ADDITIVE:
    # absent => the key is omitted (backward-compatible; a connector that declares no capture).
    time_context = descriptor.get("time_context")
    if time_context is not None:
        response["time_context"] = time_context
    public_issues = validate_public_response(response)
    if public_issues:
        raise CapabilityValidationError(public_issues)
    return response


class SourceCapabilitiesNotFound(LookupError):
    """Raised when the scoped resource must remain indistinguishable from absent."""


class SourceCapabilitiesUnavailable(RuntimeError):
    """Raised when capability scope or metadata cannot be proven safely."""


def get_scoped_source_capabilities(
    *,
    project_id: str,
    connection_ref_id: str,
    identity: str,
    loaded_modules: list[Any],
    conn: Any,
) -> dict[str, Any]:
    """Return one governed capability catalog for a project-owned connection.

    Scope checks are deliberately fail-closed. Unknown, cross-project, inactive,
    disabled, and provider-mismatched resources share the same not-found result;
    infrastructure or contract failures are surfaced as unavailable.
    """

    project_id = project_id.strip()
    connection_ref_id = connection_ref_id.strip()
    if not project_id or not connection_ref_id:
        raise ValueError("project_id and connection_ref_id are required")

    from core.module_enablement import (  # noqa: PLC0415
        ModuleEnablementUnavailable,
        is_module_enabled,
    )
    from core.project_access import (  # noqa: PLC0415
        ProjectAccessUnavailable,
        identity_has_project_access,
    )

    try:
        if not identity_has_project_access(project_id, identity, conn, fail_closed=True):
            raise SourceCapabilitiesNotFound

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT provider, status, enabled
                FROM app.connection_ref
                WHERE id = %s AND project_id = %s
                """,
                (connection_ref_id, project_id),
            )
            row = cur.fetchone()
    except SourceCapabilitiesNotFound:
        raise
    except (ProjectAccessUnavailable, ModuleEnablementUnavailable) as exc:
        raise SourceCapabilitiesUnavailable("capability scope is unavailable") from exc
    except Exception as exc:  # noqa: BLE001
        raise SourceCapabilitiesUnavailable("connection scope is unavailable") from exc

    if not isinstance(row, (tuple, list)) or len(row) < 3:
        raise SourceCapabilitiesNotFound
    module_name, connection_status, connection_enabled = row[:3]
    if connection_status != "active" or not bool(connection_enabled):
        raise SourceCapabilitiesNotFound

    loaded = next(
        (candidate for candidate in loaded_modules if candidate.name == module_name),
        None,
    )
    if loaded is None:
        raise SourceCapabilitiesNotFound
    if not getattr(loaded, "capabilities_available", False):
        raise SourceCapabilitiesUnavailable("capability metadata is unavailable")

    try:
        enabled = is_module_enabled(str(module_name), project_id, conn, fail_closed=True)
    except ModuleEnablementUnavailable as exc:
        raise SourceCapabilitiesUnavailable("module scope is unavailable") from exc
    if not enabled:
        raise SourceCapabilitiesNotFound

    try:
        return normalize_capabilities(
            loaded.manifest,
            project_id=project_id,
            connection_ref_id=connection_ref_id,
        )
    except CapabilityValidationError as exc:
        raise SourceCapabilitiesUnavailable("capability metadata is invalid") from exc
