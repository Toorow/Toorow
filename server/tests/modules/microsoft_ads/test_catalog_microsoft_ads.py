"""Story 26.4 -- microsoft-ads catalog completeness invariants (epic-26).

Jean's hard rule: the catalog is COMPLETE AT EXECUTION -- the 8 primary
report types are exposed with their INTEGRAL live-XSD column enums
(Campaign 132, Account 111, AdGroup 104, Ad 92, Keyword 80, Geographic 80,
SearchQuery 61, AgeGenderAudience 38 -- dossier 2026-07-21), the 40 remaining
v13 report types are EXCLUDED with a report-type-next-tier reason, and ZERO
fields are 'planned'. The compat rules are loaded through the REAL core
loader (core.field_compat.load_module_rules), never re-parsed locally.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.account_topology import validate_topology
from core.catalog_contract import diff_catalog_manifest, validate_catalog_schema
from core.field_compat import load_module_rules, validate_selection
from core.refetch import validate_refetch

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "microsoft-ads"
_OFFICIAL = _MODULE_DIR / "catalog_sources" / "official_fields.json"
_CATALOG_SOURCES = _MODULE_DIR / "catalog_sources" / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _MODULE_DIR / "catalog_sources" / "fusion-report.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

# The frozen dossier counts (microsoft-ads-catalog-research.md, 2026-07-21).
EXPECTED_REPORT_COLUMN_COUNTS = {
    "CampaignPerformanceReportRequest": 132,
    "AccountPerformanceReportRequest": 111,
    "AdGroupPerformanceReportRequest": 104,
    "AdPerformanceReportRequest": 92,
    "KeywordPerformanceReportRequest": 80,
    "GeographicPerformanceReportRequest": 80,
    "SearchQueryPerformanceReportRequest": 61,
    "AgeGenderAudienceReportRequest": 38,
}
# Distinct columns across the 8 covered reports (deduplicated union).
EXPECTED_UNION = 192
EXPECTED_EXCLUDED_SURFACES = 40
EXPECTED_TOTAL = EXPECTED_UNION + EXPECTED_EXCLUDED_SURFACES
EXPECTED_TOTAL_REPORT_TYPES = 48


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Curated official snapshot (committed, deterministic builder output).
# ---------------------------------------------------------------------------


class TestOfficialSnapshot:
    @pytest.fixture(scope="class")
    def official(self):
        return _load(_OFFICIAL)

    def test_exact_dossier_counts(self, official):
        """192 distinct columns + 40 report surfaces -- zero truncation."""
        surfaces = [
            f for f in official if f["field_id"].startswith("report_surface_")
        ]
        columns = [
            f for f in official if not f["field_id"].startswith("report_surface_")
        ]
        assert len(surfaces) == EXPECTED_EXCLUDED_SURFACES
        assert len(columns) == EXPECTED_UNION
        assert len(official) == EXPECTED_TOTAL

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids))

    def test_report_surface_sources_are_report_request_names(self, official):
        for f in official:
            if f["field_id"].startswith("report_surface_"):
                assert f["source_field"].endswith("ReportRequest"), f["field_id"]
                assert f["section"] == "REPORT SURFACES"

    def test_column_sources_match_provider_charset(self, official):
        import re

        charset = re.compile(r"^[A-Za-z0-9]+$")
        for f in official:
            if not f["field_id"].startswith("report_surface_"):
                assert charset.match(f["source_field"]), f["source_field"]

    def test_manifest_fields_present_with_matching_kind_and_source(self, official):
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MANIFEST)
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from snapshot"
            assert by_id[fid]["kind"] == m["kind"], f"kind mismatch for {fid}"
            assert by_id[fid]["source_field"] == m["source_field"], (
                f"source_field mismatch for {fid}"
            )

    def test_non_additive_note_on_ratio_statistics(self, official):
        """Every *Percent/*Rate/Average* statistic documents AD-4."""
        by_id = {f["field_id"]: f for f in official}
        for fid in ("ctr", "average_cpc", "average_position",
                    "impression_share_percent", "return_on_ad_spend",
                    "historical_quality_score"):
            assert "NON-ADDITIVE" in by_id[fid]["description"], fid


# ---------------------------------------------------------------------------
# Generated api_catalog.json (committed alongside; regen must reproduce it).
# ---------------------------------------------------------------------------


class TestGeneratedCatalog:
    @pytest.fixture(scope="class")
    def api_catalog(self):
        return _load(_API_CATALOG)

    def test_schema_valid(self, api_catalog):
        assert validate_catalog_schema(api_catalog) == []

    def test_connector_and_pinned_version(self, api_catalog):
        assert api_catalog["connector"] == "microsoft-ads"
        manifest = _load(_MANIFEST)
        assert (
            api_catalog["api_version"]
            == manifest["provider_api_version"]
            == _load(_CATALOG_SOURCES)["api_version"]
            == "v13"
        )

    def test_counts_equal_official_snapshot(self, api_catalog):
        official_ids = {f["field_id"] for f in _load(_OFFICIAL)}
        catalog_ids = {f["field_id"] for f in api_catalog["fields"]}
        assert catalog_ids == official_ids
        assert len(api_catalog["fields"]) == EXPECTED_TOTAL

    def test_zero_planned_every_exclusion_reasoned(self, api_catalog):
        """Jean's invariant: complete AT EXECUTION -- exposed or excluded+reason."""
        by_exposure: dict[str, int] = {}
        for f in api_catalog["fields"]:
            by_exposure[f["exposure"]] = by_exposure.get(f["exposure"], 0) + 1
        assert by_exposure.get("planned", 0) == 0
        assert by_exposure["exposed"] == EXPECTED_UNION
        assert by_exposure["excluded"] == EXPECTED_EXCLUDED_SURFACES
        excluded = [f for f in api_catalog["fields"] if f["exposure"] == "excluded"]
        assert {f["section"] for f in excluded} == {"REPORT SURFACES"}
        for f in excluded:
            reason = f.get("exclusion_reason", "")
            assert reason.startswith("report-type-next-tier"), f["field_id"]
            # The reason names the exact report type it stands for.
            assert f["source_field"] in reason, f["field_id"]

    def test_the_40_excluded_surfaces_are_the_uncovered_report_types(
        self, api_catalog
    ):
        covered = set(EXPECTED_REPORT_COLUMN_COUNTS)
        excluded_types = {
            f["source_field"]
            for f in api_catalog["fields"]
            if f["exposure"] == "excluded"
        }
        assert len(excluded_types) == EXPECTED_EXCLUDED_SURFACES
        assert not (excluded_types & covered)
        assert (
            len(excluded_types) + len(covered) == EXPECTED_TOTAL_REPORT_TYPES
        )
        # Spot checks from the dossier inventory.
        for report_type in (
            "GoalsAndFunnelsReportRequest",
            "ProductDimensionPerformanceReportRequest",
            "UserLocationPerformanceReportRequest",
            "DSASearchQueryPerformanceReportRequest",
        ):
            assert report_type in excluded_types

    def test_manifest_fields_are_exposed(self, api_catalog):
        manifest_ids = {
            f["field_id"]
            for f in _load(_MANIFEST)["source_capabilities"]["fields"]
        }
        exposed_ids = {
            f["field_id"]
            for f in api_catalog["fields"]
            if f["exposure"] == "exposed"
        }
        assert manifest_ids <= exposed_ids

    def test_every_field_tiered(self, api_catalog):
        assert all(
            f["tier"] in ("core", "standard", "advanced")
            for f in api_catalog["fields"]
        )

    def test_core_tier_is_the_pruned_default_surface(self, api_catalog):
        """Review google-ads lesson: the tier-core default carries only the 5
        additive core metrics + structural id/name/date dims -- no ratios."""
        core_metrics = sorted(
            f["field_id"]
            for f in api_catalog["fields"]
            if f["tier"] == "core" and f["kind"] == "metric"
        )
        assert core_metrics == [
            "clicks", "conversions", "impressions", "revenue", "spend",
        ]
        core_dims = {
            f["field_id"]
            for f in api_catalog["fields"]
            if f["tier"] == "core" and f["kind"] == "dimension"
        }
        assert "time_period" in core_dims
        for ratio in ("ctr", "average_cpc", "average_cpm", "average_position"):
            entry = next(
                f for f in api_catalog["fields"] if f["field_id"] == ratio
            )
            assert entry["tier"] != "core", ratio

    def test_diff_against_manifest_clean(self, api_catalog):
        assert diff_catalog_manifest(api_catalog, _load(_MANIFEST)) == []


# ---------------------------------------------------------------------------
# field_compatibility loaded via the REAL core loader (26.1 socle).
# ---------------------------------------------------------------------------


class TestFieldCompatibilityContract:
    @pytest.fixture(scope="class")
    def rules(self):
        rules = load_module_rules(_MODULE_DIR)
        assert rules is not None, "field_compatibility block must opt in (schema_version)"
        return rules

    def test_rule_inventory(self, rules):
        by_kind: dict[str, list[str]] = {}
        for rule in rules["rules"]:
            by_kind.setdefault(rule["kind"], []).append(rule["id"])
        assert sorted(by_kind["mutually_exclusive"]) == [
            "audience-share-vs-customer-attributes",
            "impression-share-vs-restricted-attributes",
        ]
        assert len(by_kind["selectable_set"]) == 8

    def test_selectable_sets_carry_the_integral_enums(self, rules):
        """Per-report allowed_fields counts == the frozen dossier counts."""
        by_report = {
            rule["scope"]["report_type"]: rule["allowed_fields"]
            for rule in rules["rules"]
            if rule["kind"] == "selectable_set"
        }
        assert set(by_report) == set(EXPECTED_REPORT_COLUMN_COUNTS)
        for report_type, expected in EXPECTED_REPORT_COLUMN_COUNTS.items():
            assert len(by_report[report_type]) == expected, report_type
        union = set()
        for allowed in by_report.values():
            union |= set(allowed)
        assert len(union) == EXPECTED_UNION
        # Every enum field id exists in the catalog (single source, no drift).
        catalog_ids = {f["field_id"] for f in _load(_API_CATALOG)["fields"]}
        assert union <= catalog_ids

    def test_impression_share_rule_fires_through_core_engine(self, rules):
        violations = validate_selection(
            {
                "metrics": ["impression_share_percent", "impressions"],
                "dimensions": ["time_period", "campaign_id", "bid_match_type"],
            },
            rules,
            context={"report_type": "CampaignPerformanceReportRequest"},
        )
        assert any(
            v.rule_id == "impression-share-vs-restricted-attributes"
            for v in violations
        )

    def test_audience_share_rule_fires_through_core_engine(self, rules):
        violations = validate_selection(
            {
                "metrics": ["relative_ctr", "impressions"],
                "dimensions": ["time_period", "campaign_id", "customer_id"],
            },
            rules,
            context={"report_type": "CampaignPerformanceReportRequest"},
        )
        assert any(
            v.rule_id == "audience-share-vs-customer-attributes"
            for v in violations
        )

    def test_missing_context_is_a_loud_caller_bug(self, rules):
        with pytest.raises(ValueError):
            validate_selection(
                {"metrics": ["impressions"], "dimensions": ["time_period"]},
                rules,
            )


# ---------------------------------------------------------------------------
# Manifest declarations consumed by the core socle (error map, refetch,
# topology, quota).
# ---------------------------------------------------------------------------


class TestManifestSocleBlocks:
    @pytest.fixture(scope="class")
    def manifest(self):
        return _load(_MANIFEST)

    def test_error_map_body_code_keys(self, manifest):
        error_map = manifest["error_map"]
        assert error_map["401:109"] == "auth_expired"
        assert error_map["401:105"] == "auth_revoked"
        assert error_map["401:3101"] == "auth_revoked"
        assert error_map["401:124"] == "auth_revoked"
        assert error_map["403:106"] == "permission_denied"
        assert error_map["403:2003"] == "permission_denied"
        # The full ReportingService* validation family 2001..2101.
        for code in range(2001, 2102):
            assert error_map[f"400:{code}"] == "invalid_request", code
        assert error_map["500:0"] == "provider_transient"

    def test_rate_limit_codes_stay_out_of_the_error_map(self, manifest):
        assert manifest["rate_limit_codes"] == ["117", "207", "4204"]
        for code in manifest["rate_limit_codes"]:
            for status in (400, 403, 429):
                assert f"{status}:{code}" not in manifest["error_map"]

    def test_refetch_ladder_declared_and_valid(self, manifest):
        refetch = manifest["refetch"]
        assert refetch["nightly_days"] == 3
        assert refetch["weekly_days"] == 30
        assert refetch["monthly_days"] == 90
        assert validate_refetch(refetch) == []

    def test_account_topology_contract_valid(self, manifest):
        topology = manifest["account_topology"]
        assert validate_topology(topology) == []
        assert topology["selection_level"] == "account"
        assert [level["id"] for level in topology["levels"]] == [
            "customer", "account",
        ]
        assert topology["discovery"]["callable"] == "discover_accounts"

    def test_quota_block(self, manifest):
        quota = manifest["quota"]
        assert quota["window_seconds"] == 60
        assert quota["read_cost"] > 0 and quota["write_cost"] > 0


class TestFusionReport:
    def test_drift_empty_and_counts(self):
        report = _load(_FUSION)
        assert report["drift_ids"] == []
        assert report["official_total"] == EXPECTED_TOTAL
        assert report["official_duplicate_ids"] == []
        assert report["exposure_counts"] == {
            "excluded": EXPECTED_EXCLUDED_SURFACES,
            "exposed": EXPECTED_UNION,
        }
