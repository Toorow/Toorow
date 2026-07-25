"""Story 26.2 -- google-ads catalog completeness invariants (epic-26).

The catalog counts MUST equal the research dossier / committed official
snapshot: 278 metrics + 151 segments (+ 26 curated structural dimensions),
ZERO truncation, ZERO `planned` exposure (Jean's hard rule: the catalog is
complete AT EXECUTION). The api_catalog is generated -- these tests prove the
generated artifact still equals the snapshot byte-for-count.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.catalog_contract import diff_catalog_manifest, validate_catalog_schema

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "google-ads"
_OFFICIAL = _MODULE_DIR / "catalog_sources" / "official_fields.json"
_CATALOG_SOURCES = _MODULE_DIR / "catalog_sources" / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _MODULE_DIR / "catalog_sources" / "fusion-report.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

# The frozen dossier counts (google-ads-catalog-research.md, v24, 2026-07-21).
EXPECTED_METRICS = 278
EXPECTED_SEGMENTS = 151
EXPECTED_STRUCTURAL = 26
EXPECTED_TOTAL = EXPECTED_METRICS + EXPECTED_SEGMENTS + EXPECTED_STRUCTURAL


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
        """278 metrics + 151 segments + 26 structural -- zero truncation."""
        metrics = [f for f in official if f["kind"] == "metric"]
        segments = [
            f for f in official if f.get("source_field", "").startswith("segments.")
        ]
        structural = [
            f
            for f in official
            if f["kind"] == "dimension"
            and not f.get("source_field", "").startswith("segments.")
        ]
        assert len(metrics) == EXPECTED_METRICS
        assert len(segments) == EXPECTED_SEGMENTS
        assert len(structural) == EXPECTED_STRUCTURAL
        assert len(official) == EXPECTED_TOTAL

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids))

    def test_every_metric_source_is_gaql_metrics_token(self, official):
        for f in official:
            if f["kind"] == "metric":
                assert f["source_field"].startswith("metrics."), f["field_id"]

    def test_message_typed_segments_flattened_paths(self, official):
        """The 4 message-typed segments carry their flattened primary path."""
        by_id = {f["field_id"]: f for f in official}
        assert by_id["keyword"]["source_field"] == "segments.keyword.info.text"
        assert by_id["asset_interaction_target"]["source_field"].startswith(
            "segments.asset_interaction_target."
        )
        assert by_id["sk_ad_network_source_app"]["source_field"].startswith(
            "segments.sk_ad_network_source_app."
        )
        assert by_id["budget_campaign_association_status"]["source_field"].startswith(
            "segments.budget_campaign_association_status."
        )

    def test_manifest_fields_present_with_matching_kind_and_source(self, official):
        """Every manifest capability field exists in the snapshot (drift == [])."""
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MANIFEST)
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from snapshot"
            assert by_id[fid]["kind"] == m["kind"], f"kind mismatch for {fid}"
            assert by_id[fid].get("source_field", fid) == m["source_field"], (
                f"source_field mismatch for {fid}"
            )

    def test_micros_rule_documented(self, official):
        """Every money_micros field documents the value/1e6 landing rule and
        the 25.6 probe ratification (26.2 + review F-2)."""
        for f in official:
            if f.get("physical_type") == "money_micros":
                assert "value/1e6" in f["description"], f["field_id"]
                assert "25.6" in f["description"], f["field_id"]

    def test_money_micros_declaration_is_explicit_and_complete(self, official):
        """Review 26.2 F-2: the money_micros DECLARATION (the only thing
        transform() divides on) covers the *_micros int64 family AND the
        micros-denominated doubles -- and never the value_per_* conversion
        values or unit-less ratios."""
        money = {
            f["field_id"]
            for f in official
            if f.get("physical_type") == "money_micros"
        }
        # The monetary DOUBLES the suffix heuristic missed (the x1e6 bug).
        for fid in (
            "average_cpc", "average_cpm", "average_cost", "average_cpe",
            "active_view_cpm", "cost_per_conversion", "cost_per_all_conversions",
            "cost_per_current_model_attributed_conversion",
            "cost_per_platform_comparable_conversion",
        ):
            assert fid in money, f"{fid} must be declared money_micros"
        # Every *_micros metric is covered.
        for f in official:
            if f["kind"] == "metric" and f["field_id"].endswith("_micros"):
                assert f["field_id"] in money, f["field_id"]
        # Never micros: conversion VALUES, unit-less ratios, probabilities.
        assert not any(fid.startswith("value_per_") for fid in money)
        for fid in ("ctr", "conversions_value", "cost_per_conversion_p_value"):
            assert fid not in money, f"{fid} must NOT be divided by 1e6"
        # Only metrics carry the declaration.
        non_metric = [
            f["field_id"]
            for f in official
            if f.get("physical_type") == "money_micros" and f["kind"] != "metric"
        ]
        assert non_metric == []
        assert len(money) == 39  # 28 *_micros + 11 monetary doubles


# ---------------------------------------------------------------------------
# catalog_sources declaration.
# ---------------------------------------------------------------------------


class TestCatalogSourcesDeclaration:
    @pytest.fixture(scope="class")
    def cfg(self):
        return _load(_CATALOG_SOURCES)

    def test_pinned_api_version_matches_manifest_pin(self, cfg):
        """The version is pinned in ONE place (manifest); everything must agree."""
        manifest = _load(_MANIFEST)
        assert cfg["api_version"] == manifest["provider_api_version"] == "v24"

    def test_official_and_enrichment_sources(self, cfg):
        kinds = {s["kind"] for s in cfg["sources"]}
        assert kinds == {"official", "enrichment"}

    def test_section_tier_map_core_families(self, cfg):
        stm = cfg["section_tier_map"]
        for core_section in ("COST", "CLICKS", "IMPRESSIONS", "STRUCTURE", "TIME"):
            assert stm.get(core_section) == "core"
        assert len(stm) == 29

    def test_field_compatibility_block_is_core_schema_1(self, cfg):
        """Review 26.2 F-1: the block IS the core 26.1 contract (schema '1',
        typed selectable_set rules per FROM resource, gaql metadata in
        _-prefixed keys) and validates against core.field_compat."""
        from core.field_compat import load_rules

        compat = cfg["field_compatibility"]
        assert compat["schema_version"] == "1"
        load_rules(compat)  # raises FieldCompatibilityError when malformed
        rules = compat["rules"]
        assert {r["kind"] for r in rules} == {"selectable_set"}
        assert {r["scope"]["resource"] for r in rules} == {
            "campaign", "ad_group", "ad_group_ad",
            "keyword_view", "search_term_view",
        }
        # allowed_fields are catalog FIELD IDS covering all 278 metrics.
        for rule in rules:
            assert len(rule["allowed_fields"]) >= 278
        # gaql-specific metadata lives in _-prefixed keys (schema-tolerated).
        assert compat["_gaql_core_date_segments"] == [
            "date", "week", "month", "quarter", "year",
        ]
        assert "campaign" in compat["_gaql_selectable_attr_prefixes"]
        assert "product_" in compat["_gaql_resource_scoped_segment_prefixes"]
        assert "click_type" in compat["_row_restricting_segments"]  # F-3
        assert "PROHIBITED_SEGMENT_WITH_METRIC_IN_SELECT_OR_WHERE_CLAUSE" in (
            compat["_refusal_error_codes"]
        )

    def test_field_compatibility_rules_reproduce_from_generator(self, cfg):
        """The committed rules equal a fresh generator projection (F-1: the
        rules are generated, never hand-drifted)."""
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "gads_build_official_fields",
            _MODULE_DIR / "catalog_sources" / "build_official_fields.py",
        )
        builder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(builder)
        fields = _load(_OFFICIAL)
        expected = builder.build_compat_rules(fields, cfg["field_compatibility"])
        assert cfg["field_compatibility"]["rules"] == expected

    def test_excluded_sections_all_have_reasons(self, cfg):
        assert set(cfg["excluded_sections"]) == {"AUCTION INSIGHT", "EXPERIMENT", "SKAN"}
        assert all(reason.strip() for reason in cfg["excluded_sections"].values())


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
        assert api_catalog["connector"] == "google-ads"
        assert api_catalog["api_version"] == _load(_MANIFEST)["provider_api_version"]

    def test_counts_equal_official_snapshot(self, api_catalog):
        """The generated catalog declares EXACTLY the snapshot fields (26.2)."""
        official_ids = {f["field_id"] for f in _load(_OFFICIAL)}
        catalog_ids = {f["field_id"] for f in api_catalog["fields"]}
        assert catalog_ids == official_ids
        assert len(api_catalog["fields"]) == EXPECTED_TOTAL

    def test_metric_and_segment_counts(self, api_catalog):
        metrics = [f for f in api_catalog["fields"] if f["kind"] == "metric"]
        segments = [
            f
            for f in api_catalog["fields"]
            if f["source_field"].startswith("segments.")
        ]
        assert len(metrics) == EXPECTED_METRICS
        assert len(segments) == EXPECTED_SEGMENTS

    def test_zero_planned_every_exclusion_reasoned(self, api_catalog):
        """Jean's invariant: complete AT EXECUTION -- exposed or excluded+reason."""
        by_exposure: dict[str, int] = {}
        for f in api_catalog["fields"]:
            by_exposure[f["exposure"]] = by_exposure.get(f["exposure"], 0) + 1
        assert by_exposure.get("planned", 0) == 0
        assert by_exposure["exposed"] == 407
        assert by_exposure["excluded"] == 48
        excluded = [f for f in api_catalog["fields"] if f["exposure"] == "excluded"]
        assert all(f.get("exclusion_reason") for f in excluded)
        # The excluded families are exactly the 3 report-shape sections.
        assert {f["section"] for f in excluded} == {
            "AUCTION INSIGHT", "EXPERIMENT", "SKAN",
        }

    def test_auction_insight_metrics_excluded_with_reason(self, api_catalog):
        """Story DoD example: metrics.auction_insight_* -> excluded + reason."""
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        entry = by_id["auction_insight_search_impression_share"]
        assert entry["exposure"] == "excluded"
        assert "auction-insight" in entry["exclusion_reason"].replace(" ", "-")

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

    def test_diff_against_manifest_clean(self, api_catalog):
        assert diff_catalog_manifest(api_catalog, _load(_MANIFEST)) == []


class TestFusionReport:
    def test_drift_empty(self):
        report = _load(_FUSION)
        assert report["drift_ids"] == []
        assert report["official_total"] == EXPECTED_TOTAL
        assert report["official_duplicate_ids"] == []
