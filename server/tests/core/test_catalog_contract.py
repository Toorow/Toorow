"""Unit tests for the generic API catalog diff engine (Story 25.1).

All fixtures use neutral vocabulary (alpha_metric, beta_dimension, ...).
No provider names or real field names appear in this test file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.catalog_contract import (
    CatalogIssue,
    CatalogValidationError,
    catalog_default_selection,
    diff_catalog_manifest,
    load_catalog,
    validate_catalog_schema,
    validate_selection,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "catalog"


def _load(name: str) -> dict:
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# load_catalog
# ---------------------------------------------------------------------------


def test_load_catalog_returns_none_when_absent(tmp_path: Path) -> None:
    """load_catalog returns None when api_catalog.json is not present."""
    result = load_catalog(tmp_path)
    assert result is None


def test_load_catalog_returns_dict_when_present(tmp_path: Path) -> None:
    """load_catalog parses and returns the catalog dict when file exists."""
    catalog_data = _load("valid.json")
    (tmp_path / "api_catalog.json").write_text(
        json.dumps(catalog_data), encoding="utf-8"
    )
    result = load_catalog(tmp_path)
    assert isinstance(result, dict)
    assert result["catalog_version"] == "1"


# ---------------------------------------------------------------------------
# validate_catalog_schema
# ---------------------------------------------------------------------------


def test_valid_catalog_has_no_schema_issues() -> None:
    """A well-formed catalog produces zero schema issues."""
    catalog = _load("valid.json")
    issues = validate_catalog_schema(catalog)
    assert issues == []


def test_schema_issues_returned_for_missing_required_field() -> None:
    """Missing required header field produces invalid_catalog_schema issues."""
    catalog = _load("valid.json")
    del catalog["api_version"]
    issues = validate_catalog_schema(catalog)
    assert len(issues) >= 1
    assert all(issue.code == "invalid_catalog_schema" for issue in issues)


def test_excluded_field_without_reason_fails_schema() -> None:
    """A catalog field with exposure=excluded but no exclusion_reason fails schema."""
    catalog = _load("valid.json")
    catalog["fields"].append({
        "field_id": "zeta_field",
        "source_field": "zeta_raw",
        "kind": "metric",
        "physical_type": "integer",
        "description": "Test field without exclusion reason.",
        "section": "ZETA",
        "tier": "core",
        "exposure": "excluded",
    })
    issues = validate_catalog_schema(catalog)
    assert len(issues) >= 1
    assert all(issue.code == "invalid_catalog_schema" for issue in issues)


def test_excluded_field_with_reason_passes_schema() -> None:
    """A catalog field with exposure=excluded and an exclusion_reason passes schema."""
    catalog = _load("excluded_with_reason.json")
    issues = validate_catalog_schema(catalog)
    assert issues == []


def test_no_official_source_fails_schema() -> None:
    """A catalog without at least one official source fails schema validation."""
    catalog = _load("no_official_source.json")
    issues = validate_catalog_schema(catalog)
    assert len(issues) >= 1
    assert all(issue.code == "invalid_catalog_schema" for issue in issues)


def test_planned_field_passes_schema() -> None:
    """A catalog field with exposure=planned is valid schema."""
    catalog = _load("planned.json")
    issues = validate_catalog_schema(catalog)
    assert issues == []


# ---------------------------------------------------------------------------
# diff_catalog_manifest — catalog_field_not_exposed
# ---------------------------------------------------------------------------


def test_catalog_field_not_exposed_when_exposed_field_missing_from_manifest() -> None:
    """diff raises catalog_field_not_exposed for an exposed field absent from manifest."""
    catalog = _load("valid.json")
    # valid.json has alpha_metric (exposed) and beta_dimension (exposed)
    # manifest only contains alpha_metric
    manifest = _load("manifest_alpha_only.json")
    issues = diff_catalog_manifest(catalog, manifest)
    codes = [issue.code for issue in issues]
    assert "catalog_field_not_exposed" in codes
    # The missing field is beta_dimension
    paths = [issue.path for issue in issues]
    assert any("beta_dimension" in p for p in paths)


def test_planned_field_does_not_trigger_catalog_field_not_exposed() -> None:
    """Fields with exposure=planned do not trigger catalog_field_not_exposed."""
    catalog = _load("planned.json")
    # manifest has no fields at all
    manifest = {"source_capabilities": {"fields": []}}
    issues = diff_catalog_manifest(catalog, manifest)
    assert not any(issue.code == "catalog_field_not_exposed" for issue in issues)


def test_excluded_field_does_not_trigger_catalog_field_not_exposed() -> None:
    """Fields with exposure=excluded do not trigger catalog_field_not_exposed."""
    catalog = _load("excluded_with_reason.json")
    manifest = {"source_capabilities": {"fields": []}}
    issues = diff_catalog_manifest(catalog, manifest)
    assert not any(issue.code == "catalog_field_not_exposed" for issue in issues)


# ---------------------------------------------------------------------------
# diff_catalog_manifest — manifest_field_not_in_catalog
# ---------------------------------------------------------------------------


def test_manifest_field_not_in_catalog_for_phantom_field() -> None:
    """diff raises manifest_field_not_in_catalog for a manifest field absent from catalog."""
    catalog = _load("valid.json")
    # add a phantom field to the manifest
    manifest = {
        "source_capabilities": {
            "fields": [
                {
                    "field_id": "phantom_field",
                    "source_field": "phantom_raw",
                    "kind": "metric",
                    "physical_type": "integer",
                    "description": "A phantom field not in the catalog.",
                    "semantic_hints": ["count"],
                    "canonical_target": None,
                    "aggregation": "sum",
                    "non_additive": False,
                }
            ]
        }
    }
    issues = diff_catalog_manifest(catalog, manifest)
    codes = [issue.code for issue in issues]
    assert "manifest_field_not_in_catalog" in codes
    assert any("phantom_field" in issue.path for issue in issues)


# ---------------------------------------------------------------------------
# diff_catalog_manifest — excluded_without_reason
# ---------------------------------------------------------------------------


def test_excluded_without_reason_in_diff() -> None:
    """diff raises excluded_without_reason for an excluded catalog field without reason."""
    catalog = _load("valid.json")
    # inject a field with no exclusion_reason (bypassing schema)
    catalog["fields"].append({
        "field_id": "eta_field",
        "source_field": "eta_raw",
        "kind": "metric",
        "physical_type": "integer",
        "description": "Field excluded without reason.",
        "section": "ETA",
        "tier": "core",
        "exposure": "excluded",
        # No exclusion_reason
    })
    manifest = {"source_capabilities": {"fields": []}}
    issues = diff_catalog_manifest(catalog, manifest)
    codes = [issue.code for issue in issues]
    assert "excluded_without_reason" in codes
    assert any("eta_field" in issue.path for issue in issues)


# ---------------------------------------------------------------------------
# diff_catalog_manifest — kind_mismatch
# ---------------------------------------------------------------------------


def test_kind_mismatch_when_catalog_and_manifest_disagree() -> None:
    """diff raises kind_mismatch when catalog and manifest differ on field kind."""
    catalog = _load("valid.json")
    # alpha_metric is kind=metric in catalog; make manifest say dimension
    manifest = {
        "source_capabilities": {
            "fields": [
                {
                    "field_id": "alpha_metric",
                    "source_field": "alpha_metric_raw",
                    "kind": "dimension",  # wrong kind
                    "physical_type": "decimal",
                    "description": "A neutral test metric.",
                    "semantic_hints": ["count"],
                    "canonical_target": None,
                    "aggregation": "none",
                    "non_additive": False,
                }
            ]
        }
    }
    issues = diff_catalog_manifest(catalog, manifest)
    codes = [issue.code for issue in issues]
    assert "kind_mismatch" in codes
    # beta_dimension is also exposed but missing -> catalog_field_not_exposed
    assert "catalog_field_not_exposed" in codes


# ---------------------------------------------------------------------------
# diff_catalog_manifest — source_field_mismatch
# ---------------------------------------------------------------------------


def test_source_field_mismatch_when_catalog_and_manifest_disagree() -> None:
    """diff raises source_field_mismatch when catalog and manifest disagree on source_field."""
    catalog = _load("valid.json")
    manifest = {
        "source_capabilities": {
            "fields": [
                {
                    "field_id": "alpha_metric",
                    "source_field": "wrong_raw_field",  # differs from catalog's alpha_metric_raw
                    "kind": "metric",
                    "physical_type": "decimal",
                    "description": "A neutral test metric.",
                    "semantic_hints": ["count"],
                    "canonical_target": "alpha_canonical",
                    "aggregation": "sum",
                    "non_additive": False,
                }
            ]
        }
    }
    issues = diff_catalog_manifest(catalog, manifest)
    codes = [issue.code for issue in issues]
    assert "source_field_mismatch" in codes
    assert any("alpha_metric" in issue.path for issue in issues)


# ---------------------------------------------------------------------------
# diff_catalog_manifest — tier_missing
# ---------------------------------------------------------------------------


def test_tier_missing_in_diff() -> None:
    """diff raises tier_missing for a catalog field with a falsy tier."""
    catalog = _load("valid.json")
    # inject a field with empty tier (bypassing schema enforcement)
    catalog["fields"].append({
        "field_id": "theta_field",
        "source_field": "theta_raw",
        "kind": "metric",
        "physical_type": "decimal",
        "description": "Field with missing tier.",
        "section": "THETA",
        "tier": "",  # falsy, bypassing schema for diff-level check
        "exposure": "exposed",
    })
    manifest = {
        "source_capabilities": {
            "fields": [
                {
                    "field_id": "theta_field",
                    "source_field": "theta_raw",
                    "kind": "metric",
                    "physical_type": "decimal",
                    "description": "Field with missing tier.",
                    "semantic_hints": [],
                    "canonical_target": None,
                    "aggregation": "sum",
                    "non_additive": False,
                }
            ]
        }
    }
    issues = diff_catalog_manifest(catalog, manifest)
    codes = [issue.code for issue in issues]
    assert "tier_missing" in codes
    assert any("theta_field" in issue.path for issue in issues)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_diff_output_is_deterministic() -> None:
    """diff_catalog_manifest returns the same sorted list on repeated calls."""
    catalog = _load("valid.json")
    # Produce multiple issues: phantom field + missing exposed field
    manifest = {
        "source_capabilities": {
            "fields": [
                {
                    "field_id": "phantom_z",
                    "source_field": "raw_z",
                    "kind": "dimension",
                    "physical_type": "string",
                    "description": "phantom",
                    "semantic_hints": [],
                    "canonical_target": None,
                    "aggregation": "none",
                    "non_additive": False,
                },
                {
                    "field_id": "phantom_a",
                    "source_field": "raw_a",
                    "kind": "metric",
                    "physical_type": "integer",
                    "description": "phantom",
                    "semantic_hints": [],
                    "canonical_target": None,
                    "aggregation": "sum",
                    "non_additive": False,
                },
            ]
        }
    }
    run1 = diff_catalog_manifest(catalog, manifest)
    run2 = diff_catalog_manifest(catalog, manifest)
    assert run1 == run2
    # Sorted: check ordering is stable
    assert run1 == sorted(run1)


# ---------------------------------------------------------------------------
# CatalogIssue dataclass
# ---------------------------------------------------------------------------


def test_catalog_issue_as_dict() -> None:
    """CatalogIssue.as_dict returns the expected keys."""
    issue = CatalogIssue(
        path="$.fields.alpha_metric",
        code="test_code",
        message="Test message.",
        suggested_repair="Do something.",
    )
    d = issue.as_dict()
    assert set(d.keys()) == {"path", "code", "message", "suggested_repair"}
    assert d["code"] == "test_code"


def test_catalog_issue_is_frozen() -> None:
    """CatalogIssue is immutable (frozen dataclass)."""
    issue = CatalogIssue(
        path="$.x", code="c", message="m", suggested_repair="r"
    )
    with pytest.raises(Exception):
        issue.path = "modified"  # type: ignore[misc]


def test_catalog_issue_ordering() -> None:
    """CatalogIssue supports ordering for deterministic sorting."""
    a = CatalogIssue(path="$.a", code="aaa", message="m", suggested_repair="r")
    b = CatalogIssue(path="$.b", code="bbb", message="m", suggested_repair="r")
    assert a < b


# ---------------------------------------------------------------------------
# CatalogValidationError
# ---------------------------------------------------------------------------


def test_catalog_validation_error_sorts_issues() -> None:
    """CatalogValidationError stores issues sorted."""
    issues = [
        CatalogIssue(path="$.z", code="z_code", message="z", suggested_repair="r"),
        CatalogIssue(path="$.a", code="a_code", message="a", suggested_repair="r"),
    ]
    err = CatalogValidationError(issues)
    assert err.issues == sorted(issues)
    assert "$.a" in str(err)


# ---------------------------------------------------------------------------
# Round-trip: valid catalog + matching manifest produces zero issues
# ---------------------------------------------------------------------------


def test_valid_round_trip_produces_no_issues() -> None:
    """A valid catalog paired with a fully-matching manifest yields no diff issues."""
    catalog = _load("valid.json")
    # Build a manifest that exactly mirrors the catalog's exposed fields
    manifest = {
        "source_capabilities": {
            "fields": [
                {
                    "field_id": "alpha_metric",
                    "source_field": "alpha_metric_raw",
                    "kind": "metric",
                    "physical_type": "decimal",
                    "description": "A neutral test metric.",
                    "semantic_hints": ["count"],
                    "canonical_target": "alpha_canonical",
                    "aggregation": "sum",
                    "non_additive": False,
                },
                {
                    "field_id": "beta_dimension",
                    "source_field": "beta_dimension_raw",
                    "kind": "dimension",
                    "physical_type": "string",
                    "description": "A neutral test dimension.",
                    "semantic_hints": [],
                    "canonical_target": None,
                    "aggregation": "none",
                    "non_additive": False,
                },
            ]
        }
    }
    schema_issues = validate_catalog_schema(catalog)
    assert schema_issues == []
    diff_issues = diff_catalog_manifest(catalog, manifest)
    assert diff_issues == []


# ---------------------------------------------------------------------------
# Story 25.8 — validate_selection + catalog_default_selection (generic, AD-2)
# ---------------------------------------------------------------------------


def test_catalog_default_selection_is_the_tier_core_partition() -> None:
    """The default selection = every non-excluded tier-core field, split by kind."""
    catalog = _load("valid.json")
    default = catalog_default_selection(catalog)
    # valid.json: alpha_metric (core metric), beta_dimension (standard dimension).
    assert default == {"metrics": ["alpha_metric"], "dimensions": []}


def test_validate_selection_resolves_source_fields_from_catalog() -> None:
    """A fully-known selection resolves source_fields from the catalog, no issues."""
    catalog = _load("valid.json")
    selection = {"metrics": ["alpha_metric"], "dimensions": ["beta_dimension"]}
    resolved, issues = validate_selection(catalog, selection)
    assert issues == []
    assert resolved["metrics"] == ["alpha_metric"]
    assert resolved["dimensions"] == ["beta_dimension"]
    assert resolved["source_fields"] == {
        "alpha_metric": "alpha_metric_raw",
        "beta_dimension": "beta_dimension_raw",
    }


def test_validate_selection_unknown_field_is_a_typed_refusal_listing_ids() -> None:
    """An unknown selection id becomes ONE typed issue that lists the offending ids
    (the drift signal — never silently dropped)."""
    catalog = _load("valid.json")
    selection = {"metrics": ["alpha_metric", "gamma_ghost"], "dimensions": ["delta_ghost"]}
    resolved, issues = validate_selection(catalog, selection)
    assert len(issues) == 1
    issue = issues[0]
    assert isinstance(issue, CatalogIssue)
    assert issue.code == "unknown_selection_field"
    assert "gamma_ghost" in issue.message
    assert "delta_ghost" in issue.message
    # the known field still resolves; callers refuse on non-empty issues.
    assert resolved["source_fields"] == {"alpha_metric": "alpha_metric_raw"}


def test_validate_selection_none_falls_back_to_tier_core_default() -> None:
    """A None selection resolves to the catalog's tier-core default (no issues)."""
    catalog = _load("valid.json")
    resolved, issues = validate_selection(catalog, None)
    assert issues == []
    assert resolved["metrics"] == ["alpha_metric"]
    assert resolved["source_fields"] == {"alpha_metric": "alpha_metric_raw"}


class TestCatalogDrivenProjection:
    """Story 25.8: a catalog_driven report profile makes the projection dynamic --
    exposed fields need not be enumerated in the manifest; phantom-field and
    mismatch checks still apply."""

    @staticmethod
    def _diff(reports, manifest_fields, catalog_fields):
        from core.catalog_contract import diff_catalog_manifest

        catalog = {
            "catalog_version": "1",
            "connector": "zz-cd",
            "api_version": "v1",
            "sources": [{"url": "https://o.example", "kind": "official",
                         "fetched_at": "2026-07-21T00:00:00Z"}],
            "generated_at": "2026-07-21T00:00:00Z",
            "fields": catalog_fields,
        }
        manifest = {"source_capabilities": {"fields": manifest_fields,
                                            "reports": reports}}
        return diff_catalog_manifest(catalog, manifest)

    def _field(self, fid, exposure="exposed", kind="metric"):
        return {"field_id": fid, "source_field": fid, "kind": kind,
                "physical_type": "integer", "description": fid,
                "section": "ALPHA", "tier": "core", "exposure": exposure}

    def test_catalog_driven_suppresses_not_exposed_issue(self):
        issues = self._diff(
            reports=[{"id": "cd", "selection_mode": "catalog_driven"}],
            manifest_fields=[],
            catalog_fields=[self._field("alpha_metric")],
        )
        assert [i.code for i in issues] == []

    def test_exact_bundle_still_requires_enumeration(self):
        issues = self._diff(
            reports=[{"id": "eb", "selection_mode": "exact_bundle"}],
            manifest_fields=[],
            catalog_fields=[self._field("alpha_metric")],
        )
        assert [i.code for i in issues] == ["catalog_field_not_exposed"]

    def test_phantom_manifest_field_still_flagged_with_catalog_driven(self):
        issues = self._diff(
            reports=[{"id": "cd", "selection_mode": "catalog_driven"}],
            manifest_fields=[{"field_id": "ghost", "source_field": "ghost",
                              "kind": "metric"}],
            catalog_fields=[self._field("alpha_metric")],
        )
        assert [i.code for i in issues] == ["manifest_field_not_in_catalog"]
