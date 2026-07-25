"""Tests for the manifest JSON Schema v1 (Story 1.3 — T1.3).

Covers:
  * Valid manifest passes validation.
  * Each individually omitted required field fails with the field name in the error.
  * GA4 manifest (T5.2) passes validation.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

# ---------------------------------------------------------------------------
# Schema loading
# ---------------------------------------------------------------------------
_SCHEMA_PATH = Path(__file__).parent.parent.parent / "core" / "schemas" / "manifest.schema.json"
_GA4_MANIFEST_PATH = (
    Path(__file__).parent.parent.parent / "modules" / "google-analytics" / "manifest.json"
)


def _load_schema() -> dict:
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


def _load_ga4_manifest() -> dict:
    return json.loads(_GA4_MANIFEST_PATH.read_text(encoding="utf-8"))


def _valid_manifest() -> dict:
    """Return a minimal but fully valid manifest for testing."""
    return {
        "schema_version": "1",
        "name": "test-module",
        "display_name": "Test Module",
        "auth_type": "none",
        "report_profiles": [
            {
                "id": "default",
                "display_name": "Default Report",
                "metrics": ["metric_a"],
                "dimensions": ["dim_a"],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            }
        ],
        "canonical_metric_mapping": {"metric_a": "metric_a"},
        "canonical_dimension_mapping": {"dim_a": "dim_a"},
        "widget_ref": "ui://test-module/default",
    }


def _validate(manifest: dict) -> list[jsonschema.ValidationError]:
    from referencing import Registry, Resource

    schema = _load_schema()
    capability_schema = json.loads(_CAPABILITY_SCHEMA_PATH.read_text(encoding="utf-8"))
    registry = Registry().with_resource(
        capability_schema["$id"], Resource.from_contents(capability_schema)
    )
    validator = jsonschema.Draft202012Validator(schema, registry=registry)
    return list(validator.iter_errors(manifest))


# ---------------------------------------------------------------------------
# AC4 — Valid manifest passes
# ---------------------------------------------------------------------------
def test_valid_manifest_passes():
    errors = _validate(_valid_manifest())
    assert errors == [], f"Expected no errors, got: {errors}"


def test_ga4_manifest_is_valid():
    """T5.2 — GA4 manifest passes schema v1 validation."""
    manifest = _load_ga4_manifest()
    errors = _validate(manifest)
    assert errors == [], f"GA4 manifest has validation errors: {errors}"


# ---------------------------------------------------------------------------
# AC4 — Each individually omitted required field fails
# Schema v1.1 (Story 4.5): widget_ref is now optional (removed from 'required').
# context modules (module_kind='context') omit widget_ref intentionally.
# KPI modules still declare widget_ref by convention, but schema no longer enforces it.
# ---------------------------------------------------------------------------
TOP_LEVEL_REQUIRED = [
    "schema_version",
    "name",
    "display_name",
    "auth_type",
    "report_profiles",
    "canonical_metric_mapping",
    "canonical_dimension_mapping",
    # widget_ref: removed from required in schema v1.1 (Story 4.5, Option A).
    # Context modules legitimately omit it; see test_widget_ref_optional below.
]


@pytest.mark.parametrize("field", TOP_LEVEL_REQUIRED)
def test_missing_required_field_fails(field: str):
    manifest = _valid_manifest()
    del manifest[field]
    errors = _validate(manifest)
    assert errors, f"Expected validation error for missing field '{field}', got none"
    # The field name must appear in at least one error message or path
    error_context = " ".join(
        [e.message + str(list(e.path)) + str(list(e.absolute_path)) for e in errors]
    )
    assert field in error_context, (
        f"Field name '{field}' not found in error context: {error_context}"
    )


def test_widget_ref_optional():
    """Schema v1.1 (Story 4.5): widget_ref is optional — context modules omit it."""
    manifest = _valid_manifest()
    del manifest["widget_ref"]
    errors = _validate(manifest)
    assert errors == [], (
        f"Schema v1.1: widget_ref is optional (Story 4.5, Option A). Unexpected errors: {errors}"
    )


# ---------------------------------------------------------------------------
# AC4 — Invalid enum / type / pattern values fail
# ---------------------------------------------------------------------------
def test_invalid_auth_type_fails():
    manifest = _valid_manifest()
    manifest["auth_type"] = "password"
    errors = _validate(manifest)
    assert errors, "Expected error for invalid auth_type"


def test_invalid_name_pattern_fails():
    """name must match ^[a-z0-9-]+$ (no uppercase, no underscores)."""
    manifest = _valid_manifest()
    manifest["name"] = "My_Module"
    errors = _validate(manifest)
    assert errors, "Expected error for name with invalid characters"


def test_invalid_widget_ref_fails():
    """widget_ref must start with ui://."""
    manifest = _valid_manifest()
    manifest["widget_ref"] = "https://example.com"
    errors = _validate(manifest)
    assert errors, "Expected error for widget_ref without ui:// prefix"


def test_wrong_schema_version_fails():
    """schema_version must be exactly '1'."""
    manifest = _valid_manifest()
    manifest["schema_version"] = "2"
    errors = _validate(manifest)
    assert errors, "Expected error for schema_version != '1'"


def test_report_profiles_empty_array_fails():
    """report_profiles must have at least one item."""
    manifest = _valid_manifest()
    manifest["report_profiles"] = []
    errors = _validate(manifest)
    assert errors, "Expected error for empty report_profiles array"


# ---------------------------------------------------------------------------
# AC4 — report_profile required sub-fields
# ---------------------------------------------------------------------------
PROFILE_REQUIRED = ["id", "display_name", "metrics", "dimensions", "extraction_capabilities"]


@pytest.mark.parametrize("field", PROFILE_REQUIRED)
def test_missing_profile_required_field_fails(field: str):
    manifest = _valid_manifest()
    del manifest["report_profiles"][0][field]
    errors = _validate(manifest)
    assert errors, f"Expected error for missing report_profile field '{field}'"


# ---------------------------------------------------------------------------
# AC4 — extraction_capabilities required sub-fields
# ---------------------------------------------------------------------------
EC_REQUIRED = ["row_limit", "filters_supported", "realtime"]


@pytest.mark.parametrize("field", EC_REQUIRED)
def test_missing_extraction_capabilities_field_fails(field: str):
    manifest = _valid_manifest()
    del manifest["report_profiles"][0]["extraction_capabilities"][field]
    errors = _validate(manifest)
    assert errors, f"Expected error for missing extraction_capabilities field '{field}'"


# ---------------------------------------------------------------------------
# Story 12.1 — canonical source-capability contract v1.2
# ---------------------------------------------------------------------------
_CAPABILITY_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "core" / "schemas" / "source-capabilities.schema.json"
)


def _valid_capability_descriptor() -> dict:
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
                "semantic_hints": ["spend"],
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
                "filters": [{"field_id": "date", "operators": ["gte", "lte"]}],
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
                    "supported_modes": ["manual", "daily"],
                },
            }
        ],
    }


def _validate_with_capability_registry(instance: dict, ref: str) -> list:
    from referencing import Registry, Resource

    schema = json.loads(_CAPABILITY_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    registry = Registry().with_resource(schema["$id"], Resource.from_contents(schema))
    validator = jsonschema.Draft202012Validator(
        {"$ref": ref},
        registry=registry,
    )
    return list(validator.iter_errors(instance))


def test_source_capability_schema_is_canonical_and_valid():
    schema = json.loads(_CAPABILITY_SCHEMA_PATH.read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    assert set(schema["$defs"]) >= {"descriptor", "public_response"}


def test_source_capability_required_fields_and_enums_are_versioned_contract():
    defs = json.loads(_CAPABILITY_SCHEMA_PATH.read_text(encoding="utf-8"))["$defs"]
    required = {
        "descriptor": {"contract_version", "field_discovery", "fields", "reports"},
        "field_discovery": {"mode", "allowed_targets"},
        "field": {
            "field_id",
            "source_field",
            "kind",
            "physical_type",
            "description",
            "semantic_hints",
            "canonical_target",
            "aggregation",
            "non_additive",
        },
        "report": {
            "id",
            "selection_mode",
            "availability",
            "metrics",
            "dimensions",
            "supported_grains",
            "compatibility",
            "filters",
            "pagination",
            "quota_cost",
            "incremental",
            "cadence",
        },
        "pagination": {
            "mode",
            "completeness",
            "max_pages",
            "row_limit",
            "truncation_signal",
        },
        "incremental": {"mode", "cursor_field"},
        "cadence": {"minimum_interval_minutes", "supported_modes"},
    }
    for definition, names in required.items():
        assert set(defs[definition]["required"]) == names

    # Epic 31: "event" is a first-class field kind (a source field that carries an
    # event date, declared like a metric/dimension). Additive to the versioned contract.
    assert defs["field"]["properties"]["kind"]["enum"] == ["metric", "dimension", "event"]
    # "catalog_driven" (Epic 25.8) was added to the schema but this versioned-contract
    # assertion was left out of sync (pre-existing red on main). Realigned here.
    assert defs["report"]["properties"]["selection_mode"]["enum"] == [
        "exact_bundle",
        "subset",
        "catalog_driven",
    ]
    assert defs["availability"]["properties"]["status"]["enum"] == ["selectable", "unavailable"]
    assert defs["pagination"]["properties"]["mode"]["enum"] == [
        "none",
        "offset",
        "cursor",
        "page",
        "link",
    ]
    assert defs["cadence"]["properties"]["supported_modes"]["items"]["enum"] == [
        "manual",
        "hourly",
        "daily",
        "weekly",
    ]


@pytest.mark.parametrize("missing", ["contract_version", "field_discovery", "fields", "reports"])
def test_capability_descriptor_rejects_each_missing_top_level_field(missing):
    descriptor = _valid_capability_descriptor()
    del descriptor[missing]
    errors = _validate_with_capability_registry(
        descriptor,
        "https://toorow.dev/schemas/source-capabilities.schema.json#/$defs/descriptor",
    )
    assert any(missing in error.message for error in errors)


def test_manifest_schema_references_canonical_descriptor():
    schema = _load_schema()
    assert schema["properties"]["source_capabilities"] == {
        "$ref": "source-capabilities.schema.json#/$defs/descriptor"
    }


def test_manifest_v12_requires_valid_capabilities():
    manifest = _valid_manifest()
    manifest["schema_version"] = "1.2"
    manifest["source_capabilities"] = _valid_capability_descriptor()
    assert _validate(manifest) == []
    assert (
        _validate_with_capability_registry(
            manifest["source_capabilities"],
            "https://toorow.dev/schemas/source-capabilities.schema.json#/$defs/descriptor",
        )
        == []
    )


def test_capability_descriptor_rejects_unknown_property():
    descriptor = _valid_capability_descriptor()
    descriptor["unexpected"] = True
    errors = _validate_with_capability_registry(
        descriptor,
        "https://toorow.dev/schemas/source-capabilities.schema.json#/$defs/descriptor",
    )
    assert any("Additional properties are not allowed" in error.message for error in errors)


def test_public_response_uses_same_canonical_schema():
    descriptor = _valid_capability_descriptor()
    response = {
        **descriptor,
        "project_id": "project-a",
        "connection_ref_id": "connection-a",
        "module": {
            "name": "test-module",
            "display_name": "Test Module",
            "module_kind": "kpi",
        },
    }
    assert (
        _validate_with_capability_registry(
            response,
            "https://toorow.dev/schemas/source-capabilities.schema.json#/$defs/public_response",
        )
        == []
    )


def test_legacy_manifest_remains_structurally_valid_without_capabilities():
    assert _validate(_valid_manifest()) == []
