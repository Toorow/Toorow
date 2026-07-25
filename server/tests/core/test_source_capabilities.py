"""Story 12.1 tests for generic source capability validation and normalization."""

from __future__ import annotations

import copy

from core.source_capabilities import (
    normalize_capabilities,
    validate_manifest_capabilities,
    validate_public_response,
)


def _descriptor() -> dict:
    return {
        "contract_version": "1",
        "field_discovery": {"mode": "static", "allowed_targets": []},
        "fields": [
            {
                "field_id": "date",
                "source_field": "date",
                "kind": "dimension",
                "physical_type": "date",
                "description": "Reporting date.",
                "semantic_hints": ["date"],
                "canonical_target": "date",
                "aggregation": "none",
                "non_additive": False,
            },
            {
                "field_id": "spend",
                "source_field": "cost",
                "kind": "metric",
                "physical_type": "decimal",
                "description": "Advertising spend.",
                "semantic_hints": ["spend", "revenue_related"],
                "canonical_target": "cost",
                "aggregation": "sum",
                "non_additive": False,
            },
        ],
        "reports": [
            {
                "id": "daily",
                "selection_mode": "exact_bundle",
                "availability": {"status": "selectable"},
                "dispatch": {"callable": "pull_daily"},
                "metrics": ["spend"],
                "dimensions": ["date"],
                "supported_grains": [["date"]],
                "compatibility": [],
                "filters": [{"field_id": "date", "operators": ["lte", "gte"]}],
                "pagination": {
                    "mode": "none",
                    "completeness": "complete",
                    "max_pages": None,
                    "row_limit": None,
                    "truncation_signal": "none",
                },
                "quota_cost": {"read_points": 1, "unit": "request"},
                "incremental": {"mode": "date_window", "cursor_field": None},
                "cadence": {
                    "minimum_interval_minutes": 1440,
                    "supported_modes": ["daily", "manual"],
                },
            }
        ],
    }


def _manifest() -> dict:
    return {
        "schema_version": "1.2",
        "name": "test-module",
        "display_name": "Test Module",
        "auth_type": "none",
        "module_kind": "kpi",
        "report_profiles": [
            {
                "id": "daily",
                "display_name": "Daily",
                "metrics": ["spend"],
                "dimensions": ["date"],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": True,
                    "realtime": False,
                },
            }
        ],
        "canonical_metric_mapping": {"cost": "cost"},
        "canonical_dimension_mapping": {"date": "date"},
        "source_capabilities": _descriptor(),
        "private_note": "must-not-leak",
        "token": "must-not-leak",
    }


def _codes(manifest: dict) -> set[str]:
    return {issue.code for issue in validate_manifest_capabilities(manifest)}


def test_valid_descriptor_has_no_semantic_issues():
    assert validate_manifest_capabilities(_manifest()) == []


def test_transitional_report_profiles_cannot_drift():
    manifest = _manifest()
    manifest["report_profiles"][0]["metrics"] = ["other"]
    assert "legacy_profile_mismatch" in _codes(manifest)


def test_duplicate_field_and_report_ids_are_rejected():
    manifest = _manifest()
    descriptor = manifest["source_capabilities"]
    descriptor["fields"].append(copy.deepcopy(descriptor["fields"][0]))
    descriptor["reports"].append(copy.deepcopy(descriptor["reports"][0]))
    assert {"duplicate_field_id", "duplicate_report_id"} <= _codes(manifest)


def test_field_kind_and_all_cross_references_are_checked():
    manifest = _manifest()
    report = manifest["source_capabilities"]["reports"][0]
    report["metrics"] = ["date"]
    report["dimensions"] = ["missing_dimension"]
    report["supported_grains"] = [["missing_grain"]]
    report["filters"] = [{"field_id": "missing_filter", "operators": ["eq"]}]
    report["compatibility"] = [
        {
            "reason_code": "incompatible_report_fields",
            "description": "Unsupported pair.",
            "constraint": "incompatible_with",
            "field_ids": ["missing_constraint"],
            "supported_alternatives": [
                {"metrics": ["missing_alternative"], "dimensions": ["date"]}
            ],
            "suggested_repair": {
                "remove_fields": ["missing_repair"],
                "add_fields": [],
                "split_into_reports": [],
            },
        }
    ]
    codes = _codes(manifest)
    assert "field_kind_mismatch" in codes
    assert "unknown_report_field" in codes
    assert "unsupported_grain" in codes
    assert "unsupported_filter" in codes
    assert "unknown_constraint_field" in codes


def test_pagination_cursor_and_cadence_contradictions_are_rejected():
    manifest = _manifest()
    report = manifest["source_capabilities"]["reports"][0]
    report["pagination"] = {
        "mode": "none",
        "completeness": "bounded",
        "max_pages": 2,
        "row_limit": None,
        "truncation_signal": "none",
    }
    report["incremental"] = {"mode": "cursor", "cursor_field": None}
    report["cadence"] = {
        "minimum_interval_minutes": 120,
        "supported_modes": ["manual", "hourly"],
    }
    codes = _codes(manifest)
    assert "contradictory_pagination" in codes
    assert "invalid_cursor" in codes
    assert "unsupported_cadence" in codes


def test_runtime_discovery_requires_bounded_allowed_targets():
    manifest = _manifest()
    manifest["source_capabilities"]["field_discovery"] = {
        "mode": "runtime",
        "allowed_targets": [],
    }
    assert "runtime_discovery_unbounded" in _codes(manifest)


def test_unavailable_profile_has_no_dispatch_and_needs_follow_up():
    manifest = _manifest()
    report = manifest["source_capabilities"]["reports"][0]
    report["availability"] = {
        "status": "unavailable",
        "reason_code": "profile_behavior_unproven",
        "follow_up": "story-fix-profile",
    }
    report.pop("dispatch")
    assert validate_manifest_capabilities(manifest) == []
    report["dispatch"] = {"callable": "pull_daily"}
    assert "unavailable_profile_dispatch" in _codes(manifest)


def test_duplicate_transitional_profile_ids_are_rejected():
    manifest = _manifest()
    manifest["report_profiles"].append(copy.deepcopy(manifest["report_profiles"][0]))
    assert "duplicate_profile_id" in _codes(manifest)


def test_field_mapping_and_additivity_must_agree():
    manifest = _manifest()
    field = manifest["source_capabilities"]["fields"][1]
    field["canonical_target"] = "wrong"
    field["aggregation"] = "average"
    field["non_additive"] = False

    assert {"canonical_mapping_mismatch", "invalid_additivity"} <= _codes(manifest)


def test_constraint_references_stay_inside_the_report_and_keep_field_kinds():
    manifest = _manifest()
    descriptor = manifest["source_capabilities"]
    descriptor["fields"].append(
        {
            "field_id": "other_dimension",
            "source_field": "other_dimension",
            "kind": "dimension",
            "physical_type": "string",
            "description": "Only available in another report.",
            "semantic_hints": ["identifier"],
            "canonical_target": None,
            "aggregation": "none",
            "non_additive": False,
        }
    )
    report = descriptor["reports"][0]
    report["compatibility"] = [
        {
            "reason_code": "incompatible_report_fields",
            "description": "Invalid cross-report alternative.",
            "constraint": "incompatible_with",
            "field_ids": ["other_dimension"],
            "supported_alternatives": [
                {"metrics": ["date"], "dimensions": ["spend"]}
            ],
            "suggested_repair": {
                "remove_fields": ["other_dimension"],
                "add_fields": [],
                "split_into_reports": [],
            },
        }
    ]

    assert {
        "unknown_constraint_field",
        "invalid_constraint_alternative",
    } <= _codes(manifest)


def test_typed_cadence_repair_is_normalized_and_empty_repair_is_rejected():
    manifest = _manifest()
    constraint = {
        "reason_code": "unsupported_cadence",
        "description": "Hourly refresh is unavailable.",
        "constraint": "limit",
        "field_ids": ["date"],
        "supported_alternatives": [
            {"metrics": ["spend"], "dimensions": ["date"]}
        ],
        "suggested_repair": {
            "remove_fields": [],
            "add_fields": [],
            "split_into_reports": [],
            "minimum_interval_minutes": 1440,
            "supported_modes": ["manual", "daily"],
        },
    }
    manifest["source_capabilities"]["reports"][0]["compatibility"] = [constraint]
    assert validate_manifest_capabilities(manifest) == []

    normalized = normalize_capabilities(
        manifest, project_id="project-a", connection_ref_id="connection-a"
    )
    repair = normalized["reports"][0]["compatibility"][0]["suggested_repair"]
    assert repair["minimum_interval_minutes"] == 1440
    assert repair["supported_modes"] == ["daily", "manual"]

    constraint["suggested_repair"].pop("minimum_interval_minutes")
    constraint["suggested_repair"].pop("supported_modes")
    assert "invalid_capability_schema" in _codes(manifest)


def test_cursor_daily_cadence_and_empty_template_are_rejected():
    manifest = _manifest()
    descriptor = manifest["source_capabilities"]
    descriptor["fields"].append(
        {
            "field_id": "other_cursor",
            "source_field": "other_cursor",
            "kind": "dimension",
            "physical_type": "datetime",
            "description": "Cursor from another report.",
            "semantic_hints": ["date"],
            "canonical_target": None,
            "aggregation": "none",
            "non_additive": False,
        }
    )
    report = descriptor["reports"][0]
    report["incremental"] = {"mode": "cursor", "cursor_field": "other_cursor"}
    report["cadence"] = {
        "minimum_interval_minutes": 10080,
        "supported_modes": ["manual", "daily"],
    }
    assert {"invalid_cursor", "unsupported_cadence"} <= _codes(manifest)

    report["metrics"] = []
    report["dimensions"] = []
    report["supported_grains"] = []
    manifest["report_profiles"][0]["metrics"] = []
    manifest["report_profiles"][0]["dimensions"] = []
    assert "empty_report_template" in _codes(manifest)


def test_unavailable_profile_requires_non_empty_reason_and_follow_up():
    manifest = _manifest()
    report = manifest["source_capabilities"]["reports"][0]
    report["availability"] = {
        "status": "unavailable",
        "reason_code": None,
        "follow_up": "",
    }
    report.pop("dispatch")
    assert "invalid_capability_schema" in _codes(manifest)

def test_normalization_is_deterministic_allowlisted_and_schema_valid():
    manifest = _manifest()
    normalized = normalize_capabilities(
        manifest,
        project_id="project-a",
        connection_ref_id="connection-a",
    )
    assert normalized["project_id"] == "project-a"
    assert normalized["connection_ref_id"] == "connection-a"
    assert normalized["module"] == {
        "name": "test-module",
        "display_name": "Test Module",
        "module_kind": "kpi",
    }
    assert [field["field_id"] for field in normalized["fields"]] == ["date", "spend"]
    assert normalized["reports"][0]["filters"][0]["operators"] == ["gte", "lte"]
    assert normalized["reports"][0]["cadence"]["supported_modes"] == ["daily", "manual"]
    serialized = repr(normalized)
    assert "must-not-leak" not in serialized
    assert "private_note" not in serialized
    assert "token" not in serialized
    assert validate_public_response(normalized) == []


def test_exact_bundle_and_subset_are_preserved_without_inference():
    manifest = _manifest()
    assert normalize_capabilities(
        manifest, project_id="p", connection_ref_id="c"
    )["reports"][0]["selection_mode"] == "exact_bundle"
    manifest["source_capabilities"]["reports"][0]["selection_mode"] = "subset"
    assert normalize_capabilities(
        manifest, project_id="p", connection_ref_id="c"
    )["reports"][0]["selection_mode"] == "subset"
