"""Generic validation and normalization for source capability descriptors.

The module deliberately contains no connector identifiers or provider field names.
Source vocabulary lives in manifests; core only enforces the versioned contract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

logger = logging.getLogger(__name__)

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

        # THE ENTITY IDENTIFIER IS DECLARED, OR IT DOES NOT EXIST -- and a
        # declaration that cannot be honoured is refused here rather than dropped
        # in silence, which is how `entity_field` would otherwise have become a
        # key a manifest carries and nothing reads.
        declared_entity = str(report.get("entity_field") or "").strip()
        declared_kind = str(report.get("entity_kind") or "").strip()
        if declared_entity or declared_kind:
            entity_field = field_by_id.get(declared_entity)
            if str(report.get("landing") or "") != _EVENT_LANDING:
                _add_issue(
                    issues,
                    path=f"{report_path}.entity_field",
                    code="entity_identifier_without_event_landing",
                    message=(
                        f"Report {report_id!r} names an entity identifier but does not land "
                        "events, where an entity key is carried."
                    ),
                    suggested_repair=(
                        f"Declare landing='{_EVENT_LANDING}', or drop entity_field and "
                        "entity_kind."
                    ),
                )
            elif not declared_entity or not declared_kind:
                _add_issue(
                    issues,
                    path=report_path,
                    code="incomplete_entity_identifier",
                    message=(
                        f"Report {report_id!r} declares one half of its entity identifier; "
                        "the key and what it identifies travel together."
                    ),
                    suggested_repair="Declare both entity_field and entity_kind, or neither.",
                )
            elif entity_field is None:
                _add_issue(
                    issues,
                    path=f"{report_path}.entity_field",
                    code="unknown_report_field",
                    message=(
                        f"Report names unknown entity identifier {declared_entity!r}."
                    ),
                    suggested_repair="Declare the field or remove the entity identifier.",
                )
            elif entity_field["kind"] != "dimension":
                _add_issue(
                    issues,
                    path=f"{report_path}.entity_field",
                    code="field_kind_mismatch",
                    message=f"Entity identifier {declared_entity!r} is not a dimension.",
                    suggested_repair="Name a dimension field as the entity identifier.",
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

    issues.extend(_tracked_entity_issues(descriptor, field_by_id, report_by_id))

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


# ---------------------------------------------------------------------------
# Tracked entities (Story 48.5). A source that can see own brands, competitors
# or reference entities says so, names the exact report and the exact fields,
# and those must resolve inside the same descriptor.
#
# The whole point of the block is what it forbids. Core must never conclude that
# a connector supports tracked entities because of its name, because it has a
# field spelled `brand`, `advertiser`, `domain` or `keyword`, or because someone
# added it to a list here. Absence is `not_applicable` with a reason.
# ---------------------------------------------------------------------------

# The field roles a declaration may point at. Kept as data so the checks below
# stay one loop rather than four near-identical blocks.
_TRACKED_ENTITY_FIELD_ROLES = ("identity_field_id", "label_field_id", "own_marker_field_id")


def _tracked_entity_issues(
    descriptor: dict[str, Any],
    field_by_id: dict[str, dict[str, Any]],
    report_by_id: dict[str, dict[str, Any]],
) -> list[CapabilityIssue]:
    declaration = descriptor.get("tracked_entity")
    if not isinstance(declaration, dict):
        return []

    issues: list[CapabilityIssue] = []
    seen: set[str] = set()
    for index, entry in enumerate(declaration.get("reports", [])):
        path = f"$.source_capabilities.tracked_entity.reports.{index}"
        report_id = entry["report_id"]
        if report_id in seen:
            _add_issue(
                issues,
                path=f"{path}.report_id",
                code="duplicate_tracked_entity_report",
                message=f"Report {report_id!r} is declared more than once for tracked entities.",
                suggested_repair="Declare each report once; one report has one direction.",
            )
        seen.add(report_id)

        report = report_by_id.get(report_id)
        if report is None:
            _add_issue(
                issues,
                path=f"{path}.report_id",
                code="tracked_entity_unknown_report",
                message=f"Report {report_id!r} is not declared by this descriptor.",
                suggested_repair="Name a report this descriptor already declares.",
            )
            continue

        selected = set(report["metrics"]) | set(report["dimensions"])
        named: list[tuple[str, str]] = [
            (f"{path}.candidate_field_ids.{position}", field_id)
            for position, field_id in enumerate(entry["candidate_field_ids"])
        ]
        named.extend(
            (f"{path}.{role}", entry[role])
            for role in _TRACKED_ENTITY_FIELD_ROLES
            if isinstance(entry.get(role), str)
        )
        for field_path, field_id in named:
            if field_id not in field_by_id:
                _add_issue(
                    issues,
                    path=field_path,
                    code="tracked_entity_unknown_field",
                    message=f"Field {field_id!r} is not declared by this descriptor.",
                    suggested_repair="Name a field this descriptor already declares.",
                )
            elif field_id not in selected:
                _add_issue(
                    issues,
                    path=field_path,
                    code="tracked_entity_field_not_in_report",
                    message=(
                        f"Report {report_id!r} does not return field {field_id!r}, "
                        "so it can never observe it."
                    ),
                    suggested_repair=(
                        "Point at a field the named report selects, or name another report."
                    ),
                )

        driver = entry.get("query_driver")
        if entry["direction"] == "collect":
            if not entry.get("identity_field_id") or not isinstance(driver, dict):
                _add_issue(
                    issues,
                    path=path,
                    code="tracked_entity_collect_incomplete",
                    message=(
                        f"Report {report_id!r} collects by identity but declares no "
                        "identity field and query driver pair."
                    ),
                    suggested_repair=(
                        "Declare identity_field_id and query_driver, or use "
                        "direction='observe'."
                    ),
                )
        elif isinstance(driver, dict):
            _add_issue(
                issues,
                path=f"{path}.query_driver",
                code="tracked_entity_observe_declares_driver",
                message=(
                    f"Report {report_id!r} only observes, so nothing is requested by identity."
                ),
                suggested_repair="Remove query_driver, or declare direction='collect'.",
            )

        if isinstance(driver, dict):
            own_parameter = driver.get("own_marker_parameter")
            if own_parameter and own_parameter == driver["parameter"]:
                _add_issue(
                    issues,
                    path=f"{path}.query_driver.own_marker_parameter",
                    code="tracked_entity_driver_parameter_collision",
                    message=(
                        "The identity parameter and the own-marker parameter are the "
                        "same keyword argument."
                    ),
                    suggested_repair="Give the own-marker subset its own parameter name.",
                )

    return issues


def _normalize_tracked_entity_report(entry: dict[str, Any]) -> dict[str, Any]:
    """One declared report, with every optional key present and null when unset.

    A caller reading this reads one shape. The alternative -- omitting the keys a
    connector did not fill -- makes every consumer write the same four `.get()`
    calls and makes "declared nothing" indistinguishable from "declared null".
    """
    driver = entry.get("query_driver")
    normalized_driver = None
    if isinstance(driver, dict):
        normalized_driver = {
            "parameter": driver["parameter"],
            "value_source": driver["value_source"],
            "cardinality": driver["cardinality"],
            "own_marker_parameter": driver.get("own_marker_parameter"),
            "max_values_per_request": driver.get("max_values_per_request"),
        }
    population = entry["population"]
    return {
        "report_id": entry["report_id"],
        "direction": entry["direction"],
        "entity_kinds": sorted(entry["entity_kinds"]),
        "candidate_field_ids": sorted(entry["candidate_field_ids"]),
        "identity_field_id": entry.get("identity_field_id"),
        "label_field_id": entry.get("label_field_id"),
        "own_marker_field_id": entry.get("own_marker_field_id"),
        "population": {
            "completeness": population["completeness"],
            "note": population.get("note"),
        },
        "query_driver": normalized_driver,
    }


def normalize_tracked_entity(descriptor: dict[str, Any]) -> dict[str, Any]:
    """Project the tracked-entity declaration, or say `not_applicable` and why."""

    declaration = descriptor.get("tracked_entity")
    if not isinstance(declaration, dict):
        return {
            "support": "not_applicable",
            "reason": (
                "This Connector declares no tracked-entity contract, so the platform "
                "makes no claim about whether it can observe or collect entities."
            ),
            "declaration_version": None,
            "directions": [],
            "entity_kinds": [],
            "reports": [],
        }
    reports = [_normalize_tracked_entity_report(entry) for entry in declaration["reports"]]
    reports.sort(key=lambda item: item["report_id"])
    return {
        "support": "declared",
        "reason": (
            f"This Connector declares {len(reports)} tracked-entity report contract(s)."
        ),
        "declaration_version": declaration["declaration_version"],
        "directions": sorted({report["direction"] for report in reports}),
        "entity_kinds": sorted({kind for report in reports for kind in report["entity_kinds"]}),
        "reports": reports,
    }


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


#: Where an event bundle's rows land, and therefore the only landing on which an
#: entity identifier means anything: `app.context_events`, whose `entity_key` and
#: `entity_kind` the pull fills (`data.md`, "An event now names its entity").
_EVENT_LANDING = "context_events"


def event_bundle_entity(report: dict[str, Any]) -> tuple[str, str] | None:
    """``(field_id, entity_kind)`` an event bundle declares, or ``None``.

    WHAT EACH ROW OF THE BUNDLE IS ABOUT. A report that lands events carries one
    marker per day per THING -- a video, a product, a campaign -- and the pull
    already writes that thing's key to `app.context_events.entity_key`. Nothing
    declared it, so `_report_field_ids` filtered it out of the field universe and
    the identifier never reached a mapping: on the reference project the flow
    *video publications* mapped `date` and nothing else, `video` had one carrier
    where the project has two, and the key `video x date` could not be declared
    (measured 2026-09-04 through `/mdm/common-keys/proposals`).

    Read a DECLARATION, never a provider: no connector id, no report id and no
    entity kind is named here. A report that declares nothing gains nothing.
    """
    if str(report.get("landing") or "") != _EVENT_LANDING:
        return None
    field_id = str(report.get("entity_field") or "").strip()
    entity_kind = str(report.get("entity_kind") or "").strip()
    if not field_id or not entity_kind:
        return None
    return field_id, entity_kind


def _normalize_report(
    report: dict[str, Any], profiles: dict[str, dict[str, Any]] | None = None
) -> dict[str, Any]:
    """One capability report, with the NAME the connector gave it.

    The catalog carried only the id, so the console invented a table of twelve
    Search Console identifiers to make them readable and fell back to
    de-underscoring everything else -- `sp_campaigns_daily` became
    "Sp campaigns" for the other thirty-six connectors. The manifest has the
    answer in `report_profiles[].display_name`; it just never travelled.
    """
    profile = (profiles or {}).get(report["id"], {})
    normalized: dict[str, Any] = {
        "id": report["id"],
        "display_name": profile.get("display_name") or None,
        "description": profile.get("description") or None,
        "selection_mode": report["selection_mode"],
        "availability": {
            key: report["availability"][key]
            for key in ("status", "reason_code", "follow_up")
            if key in report["availability"]
        },
        "metrics": list(report["metrics"]),
        # THE BUNDLE'S ENTITY IDENTIFIER IS ONE OF ITS DIMENSIONS, and this is the
        # one seam that says so: every consumer of the governed catalogue reads
        # `dimensions`, so declaring it here makes it mappable in the wizard's
        # field universe (`datastream_preconfiguration._report_field_ids`) AND
        # allowed in a selection (`datastream_intents._validate_connector`, whose
        # `exact_bundle_required` compares against this very list) in one move.
        # Adding it to either consumer alone would have offered a column the other
        # then refused -- amendment 2026-09-06 of `datastream-workbench-and-wizard.md`.
        "dimensions": _report_dimensions(report),
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
    entity = event_bundle_entity(report)
    if entity is not None:
        # Carried, not merely folded into `dimensions`: a reader that needs to know
        # WHICH dimension identifies the entity -- and under which kind the rows
        # landed -- must not have to guess it back out of the list.
        normalized["entity_field"], normalized["entity_kind"] = entity
    return normalized


def _report_dimensions(report: dict[str, Any]) -> list[str]:
    """The report's dimensions, plus the entity identifier its event bundle names."""
    dimensions = list(report["dimensions"])
    entity = event_bundle_entity(report)
    if entity is not None and entity[0] not in dimensions:
        dimensions.append(entity[0])
    return dimensions


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
            [
                _normalize_report(
                    report,
                    {
                        str(profile.get("id")): profile
                        for profile in manifest.get("report_profiles", [])
                        if isinstance(profile, dict) and profile.get("id")
                    },
                )
                for report in descriptor["reports"]
            ],
            key=lambda report: report["id"],
        ),
    }
    # Story 39.7: project the descriptor-level time_context CAPTURE declaration deterministically
    # into the public response when present (one report timezone per datastream). ADDITIVE:
    # absent => the key is omitted (backward-compatible; a connector that declares no capture).
    time_context = descriptor.get("time_context")
    if time_context is not None:
        response["time_context"] = time_context
    # Story 48.5: ALWAYS present, unlike time_context above. A caller asking
    # "can this source see competitors?" must read a reason, and the absence of
    # a key is not a reason.
    response["tracked_entity"] = normalize_tracked_entity(descriptor)
    public_issues = validate_public_response(response)
    if public_issues:
        raise CapabilityValidationError(public_issues)
    return response


class SourceCapabilitiesNotFound(LookupError):
    """Raised when the scoped resource must remain indistinguishable from absent."""


class SourceCapabilitiesUnavailable(RuntimeError):
    """Raised when capability scope or metadata cannot be proven safely."""


def apply_installation_readiness(
    catalog: dict[str, Any],
    *,
    conn: Any,
    connector_name: str,
    environment: str | None = None,
) -> dict[str, Any]:
    """Attach the tenant-safe installation gate to a visible connector catalog."""
    import os  # noqa: PLC0415

    from core.connector_installation import get_installation_state  # noqa: PLC0415

    resolved_environment = (
        environment
        or os.environ.get("TOOROW_ENVIRONMENT", "production")
    ).strip() or "production"
    try:
        installation = get_installation_state(
            conn,
            environment=resolved_environment,
            connector_name=connector_name,
        )
    except Exception as exc:  # noqa: BLE001
        raise SourceCapabilitiesUnavailable(
            "installation readiness is unavailable"
        ) from exc

    ready = installation is not None and installation.get("state") == "READY"
    catalog["installation"] = {
        "catalog_availability": "selectable" if ready else "unavailable",
        "catalog_status": "ready" if ready else "setup_pending",
        "safe_next_action": (
            "no action required" if ready else "contact platform support"
        ),
    }
    if not ready:
        for report in catalog.get("reports", []):
            report["availability"] = {
                "status": "unavailable",
                "reason_code": "connector_setup_pending",
                "follow_up": "contact platform support",
            }

    public_issues = validate_public_response(catalog)
    if public_issues:
        raise CapabilityValidationError(public_issues)
    return catalog


def get_project_connection_state(
    *,
    project_id: str,
    connection_ref_id: str,
    identity: str,
    conn: Any,
    external_account_id: str | None = None,
) -> tuple[Any, Any, Any, Any] | None:
    """Resolve one exact provider account through the canonical grant authority.

    NOT EVERY CONNECTOR HAS AN ACCOUNT TO SELECT
    Nine of the thirty-seven modules declare no ``account_topology`` at all: the
    credential IS the entity, there is nothing to pick. The scope row this used to
    INNER JOIN can only be written by selecting an account, and that route answers
    409 for exactly those connectors -- so their catalog was unreachable by
    construction, and the wizard dead-ended on step 3 with an empty list and no
    explanation. (Naming them here was itself the rule this file enforces on
    everyone else: core carries no provider vocabulary. The set is derivable --
    ``jq 'select(.account_topology == null) | .name' server/modules/*/manifest.json``.)

    The join is therefore LEFT, and the account-level authority is consulted only
    when an account was actually selected. Without one, the credential-level
    decision governs -- the same authority, one rung up, never an open door.

    WHICH ACCOUNT. Pass ``external_account_id`` when the caller knows it -- a
    Datastream always does, via ``app.datastreams.source_account_id``. Since
    migration 211 an authorization can hold several verified accounts, and this
    query used to take whichever one the planner returned first: the catalog was
    then governed against an account the caller was not asking about. Without
    the argument it takes the most recently verified one, which is exactly the
    old answer for the one-account credentials that were the only legal case
    before 211.
    """

    from core.project_access import (  # noqa: PLC0415
        resolve_provider_account_access,
        resolve_strict_resource_access,
    )

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.provider, r.status, r.enabled, h.status,
                   s.account_id, p.org_id
            FROM app.connection_ref r
            JOIN app.projects p ON p.id = %s AND p.status = 'active'
            LEFT JOIN LATERAL (
                SELECT sc.account_id
                FROM app.connection_account_scope sc
                WHERE sc.connection_ref_id = r.id AND sc.state = 'ready'
                  AND (%s::text IS NULL OR sc.account_id = %s)
                ORDER BY sc.verified_at DESC NULLS LAST
                LIMIT 1
            ) s ON TRUE
            LEFT JOIN app.connection_health h ON h.connection_ref_id = r.id
            WHERE r.id = %s
            """,
            (project_id, external_account_id, external_account_id, connection_ref_id),
        )
        row = cur.fetchone()
    if not isinstance(row, (tuple, list)) or len(row) < 6:
        return None
    provider, status, enabled, health, external_account_id, beneficiary_org_id = row[:6]
    if external_account_id:
        decision = resolve_provider_account_access(
            identity,
            conn,
            credential_id=connection_ref_id,
            external_account_id=str(external_account_id),
            beneficiary_org_id=str(beneficiary_org_id),
            project_id=project_id,
        )
    else:
        decision = resolve_strict_resource_access(
            identity, conn, project_id=project_id, minimum_capability="view"
        )
    if not decision.allowed:
        return None
    return provider, status, enabled, health

def get_scoped_source_capabilities(
    *,
    project_id: str,
    connection_ref_id: str,
    identity: str,
    loaded_modules: list[Any],
    conn: Any,
    module_name: str | None = None,
    external_account_id: str | None = None,
) -> dict[str, Any]:
    """Return one governed capability catalog for a project-owned connection.

    Scope checks are deliberately fail-closed. Unknown, cross-project, inactive,
    disabled, and provider-mismatched resources share the same not-found result;
    infrastructure or contract failures are surfaced as unavailable.

    `module_name` names WHICH tool of the authorization to read, and matters only
    where one authorization opens several: Google direct OAuth grants seven scopes
    from one consent screen. It is validated against that authorization's own tool
    set by `connection_tools.resolve_connection_connector` -- a connector name arriving in a
    query string is a request, never a grant. Left None for the single-tool case,
    which is every Nango connection.
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
        identity_can_read_project,
    )

    try:
        if not identity_can_read_project(project_id, identity, conn, fail_closed=True):
            raise SourceCapabilitiesNotFound

        row = get_project_connection_state(
            project_id=project_id,
            connection_ref_id=connection_ref_id,
            identity=identity,
            conn=conn,
            external_account_id=external_account_id,
        )
    except SourceCapabilitiesNotFound:
        raise
    except (ProjectAccessUnavailable, ModuleEnablementUnavailable) as exc:
        # THE ANSWER STAYS OPAQUE; THE RECORD MUST NOT. "Capability catalog is
        # unavailable" is the right answer to a caller and useless to anyone
        # repairing it: the cause is chained on `__cause__` and reaches no log.
        # The same silence cost a full diagnosis pass on the Semantic Model 503
        # and another on the warehouse read.
        logger.warning(
            "source_capabilities: capability scope unavailable: %s: %s",
            type(exc).__name__,
            exc,
        )
        raise SourceCapabilitiesUnavailable("capability scope is unavailable") from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "source_capabilities: connection scope unavailable: %s: %s",
            type(exc).__name__,
            exc,
            exc_info=True,
        )
        raise SourceCapabilitiesUnavailable("connection scope is unavailable") from exc

    if row is None:
        raise SourceCapabilitiesNotFound
    _provider, connection_status, connection_enabled = row[:3]
    if connection_status != "active" or not bool(connection_enabled):
        raise SourceCapabilitiesNotFound

    # WHICH tool of this authorization. For a Nango connection the answer is the
    # provider itself and this resolves to what `_provider` already said; for a
    # Google-direct one the provider is 'google', which is not a module at all,
    # and only the granted scopes can say. Resolving through one authority keeps
    # a caller from reaching a catalog its authorization does not open.
    from core.connection_tools import (  # noqa: PLC0415
        ConnectionConnectorsNotFound,
        ConnectionConnectorsUnavailable,
        resolve_connection_connector,
    )

    try:
        module_name = resolve_connection_connector(
            project_id=project_id,
            connection_ref_id=connection_ref_id,
            identity=identity,
            loaded_modules=loaded_modules,
            conn=conn,
            requested_connector=module_name,
        )
    except ConnectionConnectorsNotFound as exc:
        raise SourceCapabilitiesNotFound from exc
    except ConnectionConnectorsUnavailable as exc:
        raise SourceCapabilitiesUnavailable("connector scope is unavailable") from exc

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
        catalog = normalize_capabilities(
            loaded.manifest,
            project_id=project_id,
            connection_ref_id=connection_ref_id,
        )
        return apply_installation_readiness(
            catalog,
            conn=conn,
            connector_name=str(module_name),
        )
    except CapabilityValidationError as exc:
        raise SourceCapabilitiesUnavailable("capability metadata is invalid") from exc
