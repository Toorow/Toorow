"""Story 26.3 -- pinterest-ads catalog completeness invariants (epic-26).

The catalog counts MUST equal the research dossier / committed official
snapshot: 626 ReportingColumnAsync values + 1 sync-only column
(QUIZ_PIN_RESULT_OPEN) = 627 official columns, plus the single 'date'
structural grain dimension of the report response = 628 catalog entries.
ZERO truncation, ZERO `planned` exposure (Jean's hard rule: the catalog is
complete AT EXECUTION). The api_catalog is generated -- these tests prove the
generated artifact still equals the snapshot byte-for-count.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from core.catalog_contract import diff_catalog_manifest, validate_catalog_schema
from core.field_compat import load_rules

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "pinterest-ads"
_OFFICIAL = _MODULE_DIR / "catalog_sources" / "official_fields.json"
_CATALOG_SOURCES = _MODULE_DIR / "catalog_sources" / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _MODULE_DIR / "catalog_sources" / "fusion-report.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

# The frozen dossier counts (pinterest-ads-catalog-research.md, v5 / OpenAPI
# 5.28.0, 2026-07-21): Annex A = 626 async columns (184 with the [S] marker,
# 53 with the [dim] marker) + the sync-only QUIZ_PIN_RESULT_OPEN.
EXPECTED_ASYNC = 626
EXPECTED_SYNC_SHARED = 184
EXPECTED_SYNC_TOTAL = 185  # 184 shared + QUIZ_PIN_RESULT_OPEN
EXPECTED_UNION = 627  # the official column space (async U sync)
EXPECTED_DIM_MARKERS = 53
EXPECTED_TOTAL = EXPECTED_UNION + 1  # + the 'date' structural grain dimension

_COLUMN_CHARSET = re.compile(r"^[A-Z][A-Z0-9_]*$")


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
        """626 async + 1 sync-only + 1 date grain -- zero truncation."""
        by_id = {f["field_id"]: f for f in official}
        assert len(official) == EXPECTED_TOTAL
        assert "QUIZ_PIN_RESULT_OPEN" in by_id
        assert "date" in by_id
        async_cols = [
            f for f in official
            if f["field_id"] not in ("QUIZ_PIN_RESULT_OPEN", "date")
        ]
        assert len(async_cols) == EXPECTED_ASYNC
        # The official column space (union of the two enums) is exactly 627.
        assert len(official) - 1 == EXPECTED_UNION  # minus the date grain

    def test_sync_marker_counts(self, official):
        """184 shared columns carry the ReportingColumnSync marker; sync total
        = 184 + the sync-only column = 185 (the ReportingColumnSync size)."""
        shared = [
            f for f in official
            if "ReportingColumnSync member" in f["description"]
        ]
        assert len(shared) == EXPECTED_SYNC_SHARED
        quiz = next(f for f in official if f["field_id"] == "QUIZ_PIN_RESULT_OPEN")
        assert "SYNC-ONLY" in quiz["description"]
        assert len(shared) + 1 == EXPECTED_SYNC_TOTAL

    def test_dimension_counts(self, official):
        """53 [dim]-marked entity attributes + the date grain dimension."""
        dims = [f for f in official if f["kind"] == "dimension"]
        assert len(dims) == EXPECTED_DIM_MARKERS + 1
        metrics = [f for f in official if f["kind"] == "metric"]
        assert len(metrics) == EXPECTED_TOTAL - len(dims)

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids))

    def test_source_field_charset(self, official):
        """Every provider column token matches ^[A-Z][A-Z0-9_]*$ (request
        builder charset gate); 'date' maps to the DATE response key."""
        for f in official:
            assert _COLUMN_CHARSET.match(f["source_field"]), f["field_id"]
        by_id = {f["field_id"]: f for f in official}
        assert by_id["date"]["source_field"] == "DATE"

    def test_micros_rule_documented_from_catalog(self, official):
        """Every *_IN_MICRO_DOLLAR metric documents the value/1e6 landing rule
        -- THE marker the connector's conversion set is derived from (26.2
        review lesson: catalog-driven, never an id-suffix heuristic)."""
        micro = [
            f for f in official
            if f["kind"] == "metric" and "_IN_MICRO_DOLLAR" in f["field_id"]
        ]
        assert micro, "expected micro-currency metrics in the snapshot"
        for f in micro:
            assert "value/1e6" in f["description"], f["field_id"]

    def test_manifest_fields_present_with_matching_kind_and_source(self, official):
        """Every manifest capability field exists in the snapshot (drift == [])."""
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MANIFEST)
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from snapshot"
            assert by_id[fid]["kind"] == m["kind"], f"kind mismatch for {fid}"
            assert by_id[fid]["source_field"] == m["source_field"], (
                f"source_field mismatch for {fid}"
            )


# ---------------------------------------------------------------------------
# catalog_sources declaration.
# ---------------------------------------------------------------------------


class TestCatalogSourcesDeclaration:
    @pytest.fixture(scope="class")
    def cfg(self):
        return _load(_CATALOG_SOURCES)

    def test_pinned_api_version_matches_manifest_pin(self, cfg):
        manifest = _load(_MANIFEST)
        assert cfg["api_version"] == manifest["provider_api_version"] == "v5"

    def test_official_source_with_fetch_command(self, cfg):
        official = [s for s in cfg["sources"] if s["kind"] == "official"]
        assert len(official) == 1
        assert "pinterest/api-description" in official[0]["url"]
        assert "curl" in official[0]["_fetch"]
        kinds = {s["kind"] for s in cfg["sources"]}
        assert kinds == {"official", "enrichment"}

    def test_section_tier_map_core_families(self, cfg):
        stm = cfg["section_tier_map"]
        for core_section in (
            "SPEND", "IMPRESSION", "CLICKTHROUGH", "PERFORMANCE", "ENTITY", "TIME",
        ):
            assert stm.get(core_section) == "core"
        for advanced_section in (
            "VIDEO", "CONVERSION DEVICE PATH", "PRODUCT ITEM", "COLLECTION",
        ):
            assert stm.get(advanced_section) == "advanced"

    def test_field_compatibility_is_valid_core_contract(self, cfg):
        """Review google-ads lesson: the block MUST conform to the core 26.1
        schema (schema_version '1', rules as objects, selectable_set scoped
        {'level': ...}) -- validated by core.field_compat itself."""
        block = cfg["field_compatibility"]
        assert block["schema_version"] == "1"
        load_rules(block)  # raises FieldCompatibilityError when malformed
        rules = block["rules"]
        assert {r["kind"] for r in rules} == {"selectable_set"}
        scopes = {r["scope"]["level"] for r in rules}
        # Review 26.3 F-3: KEYWORD is OUT of the v1 whitelist (no observable
        # keyword identity column in the enum -- grain would collapse).
        assert scopes == {
            "CAMPAIGN", "AD_GROUP", "PIN_PROMOTION", "PRODUCT_GROUP",
        }
        by_level = {r["scope"]["level"]: set(r["allowed_fields"]) for r in rules}
        # AD columns only at PIN_PROMOTION; PRODUCT_ITEM_* / QUIZ nowhere.
        assert "AD_ID" in by_level["PIN_PROMOTION"]
        assert "AD_ID" not in by_level["CAMPAIGN"]
        assert "PRODUCT_GROUP_ID" in by_level["PRODUCT_GROUP"]
        for level_fields in by_level.values():
            assert "QUIZ_PIN_RESULT_OPEN" not in level_fields
            assert "PRODUCT_ITEM_NAME" not in level_fields
            assert "date" in level_fields

    def test_request_limits_block(self, cfg):
        limits = cfg["_request_limits"]
        assert limits["date_window_limits"]["async_default"] == {
            "max_days_back": 914, "max_range_days": 186,
        }
        assert limits["date_window_limits"]["sync_default"] == {
            "max_days_back": 90, "max_range_days": 90,
        }
        # Reserved (not wired v1) windows are explicit scope, not silence
        # (review 26.3 F-4 / F-8).
        reserved_pi = limits["date_window_limits"]["_reserved_product_item"]
        assert reserved_pi["max_days_back"] == 92
        assert reserved_pi["max_range_days"] == 31
        reserved_hour = limits["date_window_limits"]["_reserved_hour"]
        assert reserved_hour["max_days_back"] == 8
        assert reserved_hour["max_range_days"] == 3
        assert limits["levels_whitelist"]["PIN_PROMOTION"] == "AD"
        # Review 26.3 F-3: KEYWORD out of the v1 whitelist.
        assert "KEYWORD" not in limits["levels_whitelist"]
        assert limits["attribution_pinned"] == {
            "click_window_days": 30,
            "engagement_window_days": 30,
            "view_window_days": 1,
            "conversion_report_time": "TIME_OF_AD_ACTION",
        } | {"_note": limits["attribution_pinned"]["_note"]}

    def test_async_report_lifecycle_block(self, cfg):
        """The async lifecycle facts live under the '_'-prefixed key."""
        lifecycle = cfg["_async_report_lifecycle"]
        assert lifecycle["download_url_validity_minutes"] == 5
        assert lifecycle["statuses"]["completed"] == ["FINISHED"]
        assert set(lifecycle["statuses"]["expired_resubmit_once"]) == {
            "EXPIRED", "CANCELLED", "DOES_NOT_EXIST",
        }
        assert lifecycle["statuses"]["failed"] == ["FAILED"]

    def test_exclusions_all_have_reasons(self, cfg):
        # Review 26.3 F-1: CONVERSION DEVICE PATH is NOT excluded anymore --
        # the 99 columns ride the standard async request shape (exposed).
        assert set(cfg["excluded_sections"]) == {"PRODUCT ITEM"}
        product_item_reason = cfg["excluded_sections"]["PRODUCT ITEM"]
        assert product_item_reason.strip()
        # Review 26.3 F-4: a REAL operational refusal, not a deferral.
        assert "unreachable" in product_item_reason
        assert "until" not in product_item_reason
        assert set(cfg["excluded_fields"]) == {"QUIZ_PIN_RESULT_OPEN"}
        assert "SYNC-ONLY" in cfg["excluded_fields"]["QUIZ_PIN_RESULT_OPEN"]


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
        assert api_catalog["connector"] == "pinterest-ads"
        assert api_catalog["api_version"] == _load(_MANIFEST)["provider_api_version"]
        assert api_catalog["exposure_policy"] == "catalog_driven"

    def test_counts_equal_official_snapshot(self, api_catalog):
        official_ids = {f["field_id"] for f in _load(_OFFICIAL)}
        catalog_ids = {f["field_id"] for f in api_catalog["fields"]}
        assert catalog_ids == official_ids
        assert len(api_catalog["fields"]) == EXPECTED_TOTAL

    def test_zero_planned_every_exclusion_reasoned(self, api_catalog):
        """Jean's invariant: complete AT EXECUTION -- exposed or excluded+reason.

        Post-review 26.3 F-1: the 99 CONVERSION DEVICE PATH columns are
        EXPOSED (same async request shape as every other column); excluded =
        the 11 PRODUCT_ITEM_* (operational v1 refusal) + the 1 sync-only
        column = 12.
        """
        by_exposure: dict[str, int] = {}
        for f in api_catalog["fields"]:
            by_exposure[f["exposure"]] = by_exposure.get(f["exposure"], 0) + 1
        assert by_exposure.get("planned", 0) == 0
        assert by_exposure["exposed"] == 616
        assert by_exposure["excluded"] == 12
        excluded = [f for f in api_catalog["fields"] if f["exposure"] == "excluded"]
        assert all(f.get("exclusion_reason") for f in excluded)
        # 11 PRODUCT_ITEM_* + 1 sync-only; ZERO device-path exclusions.
        device_path = [f for f in excluded if f["section"] == "CONVERSION DEVICE PATH"]
        product_item = [f for f in excluded if f["section"] == "PRODUCT ITEM"]
        assert len(device_path) == 0
        assert len(product_item) == 11
        assert len(product_item) + 1 == len(excluded)
        quiz = next(
            f for f in api_catalog["fields"]
            if f["field_id"] == "QUIZ_PIN_RESULT_OPEN"
        )
        assert quiz["exposure"] == "excluded"
        assert "SYNC-ONLY" in quiz["exclusion_reason"]

    def test_device_path_family_fully_exposed(self, api_catalog):
        """Review 26.3 F-1: all 99 device-path columns exposed, no reason key."""
        device_path = [
            f for f in api_catalog["fields"]
            if f["section"] == "CONVERSION DEVICE PATH"
        ]
        assert len(device_path) == 99
        assert all(f["exposure"] == "exposed" for f in device_path)
        assert all("exclusion_reason" not in f for f in device_path)

    def test_product_item_exclusion_is_operational_not_deferral(self, api_catalog):
        """Review 26.3 F-4: the PRODUCT_ITEM_* reason states a real v1 refusal
        (unreachable at any whitelisted level), never 'until' language."""
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        entry = by_id["PRODUCT_ITEM_NAME"]
        assert entry["exposure"] == "excluded"
        assert "unreachable" in entry["exclusion_reason"]
        assert "until" not in entry["exclusion_reason"]

    def test_manifest_fields_are_exposed(self, api_catalog):
        manifest_ids = {
            f["field_id"] for f in _load(_MANIFEST)["source_capabilities"]["fields"]
        }
        exposed_ids = {
            f["field_id"] for f in api_catalog["fields"] if f["exposure"] == "exposed"
        }
        assert manifest_ids <= exposed_ids

    def test_every_field_tiered(self, api_catalog):
        assert all(
            f["tier"] in ("core", "standard", "advanced")
            for f in api_catalog["fields"]
        )

    def test_ratio_metrics_carry_non_additive_note(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for fid in ("CTR", "ECPC_IN_MICRO_DOLLAR", "CHECKOUT_ROAS",
                    "TOTAL_IMPRESSION_FREQUENCY"):
            assert "NON-ADDITIVE" in by_id[fid]["description"], fid

    def test_diff_against_manifest_clean(self, api_catalog):
        assert diff_catalog_manifest(api_catalog, _load(_MANIFEST)) == []


class TestFusionReport:
    def test_drift_empty(self):
        report = _load(_FUSION)
        assert report["drift_ids"] == []
        assert report["official_total"] == EXPECTED_TOTAL
        assert report["official_duplicate_ids"] == []
        # Post-review 26.3 F-1: 616 exposed / 12 excluded (11 PRODUCT_ITEM_*
        # + 1 sync-only QUIZ_PIN_RESULT_OPEN).
        assert report["exposure_counts"] == {"excluded": 12, "exposed": 616}
