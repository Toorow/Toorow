"""Story 28.2 -- thetradedesk catalog completeness invariants (epic-26).

The catalog counts MUST equal the research dossier Annex A (Supermetrics
2026-04-23): 275 unique fields = 164 dimensions + 111 metrics, ZERO truncation,
ZERO `planned` exposure (Jean's hard rule: complete AT EXECUTION). The 12
DATASOURCE/QUERY system_metadata fields are excluded enrichment-only; the 14
DEPRECATED/LEGACY TTD columns stay exposed with a deprecated note. STATUS DRAFT:
the live ReportTemplate facet enum is authority and settles divergences.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from core.catalog_contract import diff_catalog_manifest, validate_catalog_schema

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "thetradedesk"
_OFFICIAL = _MODULE_DIR / "catalog_sources" / "official_fields.json"
_TEMPLATE_COLUMNS = _MODULE_DIR / "catalog_sources" / "template_columns.json"
_CATALOG_SOURCES = _MODULE_DIR / "catalog_sources" / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _MODULE_DIR / "catalog_sources" / "fusion-report.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

# Frozen Annex A counts (thetradedesk-catalog-research.md, 2026-07-21).
EXPECTED_TOTAL = 275
EXPECTED_DIMENSIONS = 164
EXPECTED_METRICS = 111
EXPECTED_EXPOSED = 263
EXPECTED_EXCLUDED = 12  # 12 DATASOURCE/QUERY enrichment-only fields.

# The 12 Supermetrics-plumbing fields (DATASOURCE x3 + QUERY x9).
_ENRICHMENT_ONLY = (
    "dataSourceName",
    "system_metadata.data_source_login_owners",
    "system_metadata.data_source_logins",
    "system_metadata.query_accounts",
    "system_metadata.query_date_end",
    "system_metadata.query_date_range_end",
    "system_metadata.query_date_range_start",
    "system_metadata.query_date_start",
    "system_metadata.query_duration",
    "system_metadata.query_excluded_accounts",
    "system_metadata.query_filters",
    "system_metadata.query_timezone",
)

# The 7 NON-ADDITIVE ratio metrics (AD-4). PlayerRewind is NOT one of them.
_NON_ADDITIVE = (
    "Cpm", "Cpc", "Ctr", "CpaClick", "CpaView", "CpaTouch", "CpaTimeWeightedDecay",
)

# 14 DEPRECATED (10) + LEGACY (4) columns kept exposed for history.
_DEPRECATED_LEGACY = (
    "AdGroupIntegerID", "AdvertiserIntegerID", "App", "CampaignIntegerID",
    "CreativeIntegerID", "PartnerIntegerID", "SiteListDescription", "SiteListID",
    "SiteListName", "SiteListPermissions",
    "Fold", "FoldBidFactor", "SupplyVendor", "SupplyVendorIntegerID",
)


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


class TestOfficialSnapshot:
    @pytest.fixture(scope="class")
    def official(self):
        return _load(_OFFICIAL)

    def test_total_is_the_full_annex_a(self, official):
        """275 = the full Annex A Supermetrics catalog, ZERO truncation."""
        assert len(official) == EXPECTED_TOTAL

    def test_dimension_metric_split(self, official):
        dims = [f for f in official if f["kind"] == "dimension"]
        mets = [f for f in official if f["kind"] == "metric"]
        assert len(dims) == EXPECTED_DIMENSIONS
        assert len(mets) == EXPECTED_METRICS

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids))

    def test_manifest_fields_present_with_matching_kind_and_source(self, official):
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MANIFEST)
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from snapshot"
            assert by_id[fid]["kind"] == m["kind"], f"kind mismatch for {fid}"
            assert by_id[fid].get("source_field", fid) == m["source_field"]

    def test_non_additive_ratio_metrics_carry_the_note(self, official):
        by_id = {f["field_id"]: f for f in official}
        for ratio in _NON_ADDITIVE:
            assert "NON-ADDITIVE" in by_id[ratio]["description"], ratio

    def test_player_rewind_is_additive_not_ratio(self, official):
        by_id = {f["field_id"]: f for f in official}
        assert "NON-ADDITIVE" not in by_id["PlayerRewind"]["description"]
        assert by_id["PlayerRewind"]["kind"] == "metric"


class TestCatalogSourcesDeclaration:
    @pytest.fixture(scope="class")
    def cfg(self):
        return _load(_CATALOG_SOURCES)

    def test_pinned_api_version_matches_manifest_pin(self, cfg):
        manifest = _load(_MANIFEST)
        assert cfg["api_version"] == manifest["provider_api_version"] == "v3"

    def test_status_is_draft(self, cfg):
        assert cfg["_status"].startswith("DRAFT")

    def test_field_compatibility_is_the_core_26_1_contract(self, cfg):
        """schema_version '1' + selectable_set rules load through core (loud)."""
        from core import field_compat

        block = cfg["field_compatibility"]
        assert block["schema_version"] == "1"
        rules = field_compat.load_rules(block)  # raises on any contract violation
        selectable = [r for r in rules["rules"] if r["kind"] == "selectable_set"]
        # One selectable_set per managed ReportTemplate (5 templates).
        assert len(selectable) == 5
        by_id = {r["id"]: r for r in selectable}
        assert by_id["selectable_daily_performance"]["scope"] == {
            "report_template": "daily_performance"
        }
        # No rule kind other than selectable_set.
        assert {r["kind"] for r in rules["rules"]} == {"selectable_set"}

    def test_report_templates_declared(self, cfg):
        templates = cfg["report_template_compatibility"]["templates"]
        assert set(templates) == {
            "daily_performance", "conversions", "creative", "geo_platform",
            "video_player",
        }

    def test_excluded_fields_are_the_12_enrichment_only(self, cfg):
        excluded = cfg["excluded_fields"]
        assert set(excluded) == set(_ENRICHMENT_ONLY)
        assert all(reason.strip() for reason in excluded.values())
        assert all("enrichment-only" in v for v in excluded.values())


class TestGeneratedCatalog:
    @pytest.fixture(scope="class")
    def api_catalog(self):
        return _load(_API_CATALOG)

    def test_schema_valid(self, api_catalog):
        assert validate_catalog_schema(api_catalog) == []

    def test_connector_and_pinned_version(self, api_catalog):
        assert api_catalog["connector"] == "thetradedesk"
        assert api_catalog["api_version"] == _load(_MANIFEST)["provider_api_version"]
        assert api_catalog["exposure_policy"] == "catalog_driven"

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
        assert by_exposure["exposed"] == EXPECTED_EXPOSED
        assert by_exposure["excluded"] == EXPECTED_EXCLUDED
        excluded = [f for f in api_catalog["fields"] if f["exposure"] == "excluded"]
        assert all(f.get("exclusion_reason") for f in excluded)

    def test_enrichment_only_columns_excluded(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for fid in _ENRICHMENT_ONLY:
            assert by_id[fid]["exposure"] == "excluded", fid
            assert "enrichment-only" in by_id[fid]["exclusion_reason"], fid

    def test_deprecated_legacy_columns_exposed(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for fid in _DEPRECATED_LEGACY:
            assert by_id[fid]["exposure"] == "exposed", fid

    def test_core_columns_exposed(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for col in ("Impressions", "Clicks", "Bids", "AdvertiserCostUSD",
                    "TtdCostUsd", "TotalClickConversions", "date", "CampaignId",
                    "AdvertiserId", "AdGroupId"):
            assert by_id[col]["exposure"] == "exposed", col

    def test_non_additive_metrics_physical_type_decimal(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for ratio in _NON_ADDITIVE:
            assert by_id[ratio]["physical_type"] == "decimal", ratio
            assert "NON-ADDITIVE" in by_id[ratio]["description"], ratio

    def test_every_field_tiered(self, api_catalog):
        assert all(
            f["tier"] in ("core", "standard", "advanced") for f in api_catalog["fields"]
        )

    def test_column_charset_of_requestable_fields(self, api_catalog):
        """Every EXPOSED field is a plain alphanumeric TTD column (the request
        charset). The dotted system_metadata.* ids are all excluded."""
        charset = re.compile(r"^[a-zA-Z0-9]+$")
        for f in api_catalog["fields"]:
            if f["exposure"] == "exposed":
                assert charset.match(f["field_id"]), f["field_id"]

    def test_diff_against_manifest_clean(self, api_catalog):
        assert diff_catalog_manifest(api_catalog, _load(_MANIFEST)) == []


class TestFusionReport:
    def test_drift_empty(self):
        report = _load(_FUSION)
        assert report["drift_ids"] == []
        assert report["official_total"] == EXPECTED_TOTAL
        assert report["official_duplicate_ids"] == []
        assert report["exposure_counts"] == {"excluded": 12, "exposed": 263}
