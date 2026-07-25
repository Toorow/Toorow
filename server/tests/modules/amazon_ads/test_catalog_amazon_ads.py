"""Story 26.5 -- amazon-ads catalog completeness invariants (epic-26).

The catalog counts MUST equal the research dossier / committed official
snapshot (Annex B of amazon-ads-catalog-research.md): 740 unique columns,
per ad product SP 95 / SB 152 / SD 91 / ST 60 / DSP 541, ZERO truncation,
ZERO `planned` exposure (Jean's hard rule: the catalog is complete AT
EXECUTION). DSP-only columns are excluded dsp-seat-gated; the Airbyte v2
leftovers (leads / leadFormOpens) are deliberately ABSENT.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from core.catalog_contract import diff_catalog_manifest, validate_catalog_schema

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "amazon-ads"
_OFFICIAL = _MODULE_DIR / "catalog_sources" / "official_fields.json"
_REPORT_TYPE_COLUMNS = _MODULE_DIR / "catalog_sources" / "report_type_columns.json"
_CATALOG_SOURCES = _MODULE_DIR / "catalog_sources" / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _MODULE_DIR / "catalog_sources" / "fusion-report.json"
_MANIFEST = _MODULE_DIR / "manifest.json"

# The frozen dossier counts (amazon-ads-catalog-research.md, 2026-07-21).
#
# Review 26.5 F-3: the dictionary (Annex B) is 740 columns. The catalog is 742
# because the dossier section-4 rule catalogs a column present in EITHER the
# dictionary OR a report-type page: longTermSales / longTermROAS are documented
# on the sbCampaigns / sdCampaigns / stCampaigns pages (groupBy 'campaign'
# additional metrics) but ABSENT from the dictionary, so they are cataloged
# exposure=excluded probe-to-confirm (+2 total, +2 excluded, exposed unchanged).
# The PER-PRODUCT counts stay at the Annex B dictionary values (SP 95 / SB 152 /
# SD 91 / ST 60 / DSP 541): page-only columns are deliberately kept OUT of
# report_type_columns.json (the dictionary-derived requestable set).
DICTIONARY_TOTAL = 740
EXPECTED_TOTAL = 742
EXPECTED_PER_PRODUCT = {
    "SPONSORED_PRODUCTS": 95,
    "SPONSORED_BRANDS": 152,
    "SPONSORED_DISPLAY": 91,
    "SPONSORED_TELEVISION": 60,
    "AMAZON_DSP": 541,
}
EXPECTED_EXPOSED = 202
EXPECTED_EXCLUDED = 540
# The two page-only columns catalogued past the 740-column dictionary (F-3).
PAGE_ONLY_COLUMNS = ("longTermSales", "longTermROAS")

_PRODUCT_PREFIX = {
    "SPONSORED_PRODUCTS": "sp",
    "SPONSORED_BRANDS": "sb",
    "SPONSORED_DISPLAY": "sd",
    "SPONSORED_TELEVISION": "st",
    "AMAZON_DSP": "dsp",
}

# Dictionary-only leftovers Airbyte still requests but the official columns
# dictionary does NOT list (dossier section 5 drift): never cataloged.
_V2_LEFTOVERS = ("leads", "leadFormOpens")


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Curated official snapshot (committed, deterministic builder output).
# ---------------------------------------------------------------------------


class TestOfficialSnapshot:
    @pytest.fixture(scope="class")
    def official(self):
        return _load(_OFFICIAL)

    @pytest.fixture(scope="class")
    def by_type(self):
        return _load(_REPORT_TYPE_COLUMNS)

    def test_total_is_the_full_dictionary(self, official):
        """742 = the 740-column Annex B dictionary + 2 page-only columns (F-3);
        the dictionary is transposed INTEGRALLY, zero truncation."""
        assert len(official) == EXPECTED_TOTAL
        ids = {f["field_id"] for f in official}
        dictionary_ids = ids - set(PAGE_ONLY_COLUMNS)
        assert len(dictionary_ids) == DICTIONARY_TOTAL
        for page_only in PAGE_ONLY_COLUMNS:
            assert page_only in ids, page_only

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids))

    def test_per_ad_product_counts_equal_annex_b(self, by_type):
        """SP 95 / SB 152 / SD 91 / ST 60 / DSP 541 (dossier section 0)."""
        for product, expected in EXPECTED_PER_PRODUCT.items():
            prefix = _PRODUCT_PREFIX[product]
            product_types = [t for t in by_type if t.startswith(prefix)]
            # DSP excludes the cross-program 'benchmarks' bucket by the
            # dossier's own counting (dsp union without benchmarks = 541).
            union: set[str] = set()
            for t in product_types:
                union.update(by_type[t])
            assert len(union) == expected, (
                f"{product}: {len(union)} columns, dossier says {expected}"
            )

    def test_v2_leftovers_never_cataloged(self, official):
        ids = {f["field_id"] for f in official}
        for leftover in _V2_LEFTOVERS:
            assert leftover not in ids, (
                f"{leftover} is an Airbyte v2 leftover absent from the official "
                "dictionary -- it must NOT be cataloged"
            )

    def test_column_charset(self, official):
        import re

        charset = re.compile(r"^[a-zA-Z0-9]+$")
        bad = [f["field_id"] for f in official if not charset.match(f["field_id"])]
        assert bad == []

    def test_manifest_fields_present_with_matching_kind_and_source(self, official):
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MANIFEST)
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from snapshot"
            assert by_id[fid]["kind"] == m["kind"], f"kind mismatch for {fid}"
            assert by_id[fid].get("source_field", fid) == m["source_field"]

    def test_ratio_columns_carry_non_additive_note(self, official):
        by_id = {f["field_id"]: f for f in official}
        for ratio in ("clickThroughRate", "costPerClick", "acosClicks14d",
                      "roasClicks14d", "addToCartRate", "viewabilityRate"):
            assert "NON-ADDITIVE" in by_id[ratio]["description"], ratio


# ---------------------------------------------------------------------------
# catalog_sources declaration + core field_compatibility contract.
# ---------------------------------------------------------------------------


class TestCatalogSourcesDeclaration:
    @pytest.fixture(scope="class")
    def cfg(self):
        return _load(_CATALOG_SOURCES)

    def test_pinned_api_version_matches_manifest_pin(self, cfg):
        manifest = _load(_MANIFEST)
        assert cfg["api_version"] == manifest["provider_api_version"] == "reporting-v3"

    def test_field_compatibility_is_the_core_26_1_contract(self, cfg):
        """schema_version '1' + selectable_set rules load through core (loud)."""
        from core import field_compat

        block = cfg["field_compatibility"]
        assert block["schema_version"] == "1"
        rules = field_compat.load_rules(block)  # raises on any contract violation
        selectable = [r for r in rules["rules"] if r["kind"] == "selectable_set"]
        # 26 sponsored report types + 3 spCampaigns per-groupBy narrowings.
        assert len(selectable) == 29
        by_id = {r["id"]: r for r in selectable}
        assert len(by_id["selectable_spCampaigns"]["allowed_fields"]) == 53
        assert by_id["selectable_spCampaigns_campaign"]["scope"] == {
            "report_type": "spCampaigns",
            "group_by": "campaign",
        }
        # No DSP report type gets a selectable rule (whitelist refusal instead).
        assert not any(rid.startswith("selectable_dsp") for rid in by_id)

    def test_report_type_whitelist_is_the_26_sponsored_types(self, cfg):
        specs = cfg["report_type_compatibility"]["report_types"]
        assert len(specs) == 26
        assert all(not rt.startswith("dsp") for rt in specs)
        assert "conversionPath" not in specs
        # SB retention floor (60 days) is declared for the pre-call refusal.
        assert specs["sbCampaigns"]["retention_days"] == 60

    def test_excluded_fields_reasons(self, cfg):
        excluded = cfg["excluded_fields"]
        assert all(reason.strip() for reason in excluded.values())
        probe = sorted(k for k, v in excluded.items() if v.startswith("probe-to-confirm"))
        # linkOuts (dictionary-vs-page divergence) + the 2 page-only columns (F-3).
        assert probe == ["linkOuts", "longTermROAS", "longTermSales"]
        # The page-only columns carry the section-4 'dictionary OR page' rule.
        for page_only in PAGE_ONLY_COLUMNS:
            assert "page-only" in excluded[page_only]
            assert "section-4" in excluded[page_only]


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
        assert api_catalog["connector"] == "amazon-ads"
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

    def test_dsp_only_columns_excluded_dsp_seat_gated(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        # dspCampaign/dspInventory fee family: no sponsored availability.
        for dsp_col in ("3pFeeAutomotive", "totalCost", "agencyFee"):
            entry = by_id.get(dsp_col)
            if entry is None:
                continue
            assert entry["exposure"] == "excluded", dsp_col
            assert "dsp-seat-gated" in entry["exclusion_reason"], dsp_col
        gated = [
            f for f in api_catalog["fields"]
            if f["exposure"] == "excluded"
            and "dsp-seat-gated" in f.get("exclusion_reason", "")
        ]
        assert len(gated) == 486

    def test_link_outs_excluded_probe_to_confirm(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        entry = by_id["linkOuts"]
        assert entry["exposure"] == "excluded"
        assert "probe-to-confirm" in entry["exclusion_reason"]

    def test_page_only_long_term_columns_excluded_probe_to_confirm(self, api_catalog):
        """F-3: longTermSales/longTermROAS are cataloged (section-4 'dictionary
        OR page' rule) but excluded probe-to-confirm -- they are page-only
        (documented on sb/sd/stCampaigns pages, absent from the dictionary)."""
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for page_only in PAGE_ONLY_COLUMNS:
            entry = by_id[page_only]
            assert entry["exposure"] == "excluded", page_only
            assert "probe-to-confirm" in entry["exclusion_reason"], page_only
            assert "page-only" in entry["exclusion_reason"], page_only

    def test_page_only_columns_absent_from_report_type_columns(self):
        """F-3: page-only columns stay OUT of the dictionary-derived per-type
        set (report_type_columns.json) so the per-product Annex B counts and the
        field_compat selectable_set rules are not inflated by non-dictionary
        columns (they are refused at the excluded-columns gate instead)."""
        by_type = _load(_REPORT_TYPE_COLUMNS)
        for cols in by_type.values():
            for page_only in PAGE_ONLY_COLUMNS:
                assert page_only not in cols

    def test_sponsored_core_columns_exposed(self, api_catalog):
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for col in ("impressions", "clicks", "cost", "purchases14d", "sales",
                    "unitsSold", "campaignId", "date", "searchTerm",
                    "advertisedAsin", "purchasedAsin"):
            assert by_id[col]["exposure"] == "exposed", col

    def test_sd_targeting_video_family_exposed_dictionary_authority(self, api_catalog):
        """Page-vs-dictionary divergence 2: the sdTargeting page omits the video
        family but the dictionary includes it (Airbyte extracts it live) --
        EXPOSED, probe confirms (ROLLOUT_NOTES)."""
        by_id = {f["field_id"]: f for f in api_catalog["fields"]}
        for col in ("videoFirstQuartileViews", "videoMidpointViews",
                    "videoThirdQuartileViews", "videoUnmutes", "viewabilityRate"):
            assert by_id[col]["exposure"] == "exposed", col

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

    def test_diff_against_manifest_clean(self, api_catalog):
        assert diff_catalog_manifest(api_catalog, _load(_MANIFEST)) == []


class TestFusionReport:
    def test_drift_empty(self):
        report = _load(_FUSION)
        assert report["drift_ids"] == []
        assert report["official_total"] == EXPECTED_TOTAL
        assert report["official_duplicate_ids"] == []
