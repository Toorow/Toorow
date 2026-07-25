"""Story 25.7 — LinkedIn Ads api_catalog sources + fusion-report sanity.

Validates the CURATED official snapshot the dev agent committed and, when the
ORCHESTRATOR has run the generator, the produced api_catalog.json + fusion-report.
Generated artifacts are asserted only when present so the suite stays green before
the generator run (the dev agent cannot run shell).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "linkedin-ads"
_SOURCES_DIR = _MODULE_DIR / "catalog_sources"
_OFFICIAL = _SOURCES_DIR / "official_fields.json"
_CATALOG_SOURCES = _SOURCES_DIR / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _SOURCES_DIR / "fusion-report.json"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Curated official snapshot (committed by the dev agent — always present).
# ---------------------------------------------------------------------------


class TestOfficialSnapshot:
    @pytest.fixture(scope="class")
    def official(self):
        return _load(_OFFICIAL)

    def test_is_nonempty_list(self, official):
        assert isinstance(official, list) and len(official) > 120

    def test_every_entry_has_required_keys(self, official):
        for f in official:
            assert f.get("field_id")
            assert f.get("kind") in ("metric", "dimension")

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids)), "duplicate field_id in official_fields.json"

    def test_metric_and_dimension_counts(self, official):
        metrics = [f for f in official if f["kind"] == "metric"]
        dims = [f for f in official if f["kind"] == "dimension"]
        # 125 metrics from the li-lms-2026-06 Metrics Available table (+ revenue
        # attribution flattened); 28 dimensions (structural + pivot enums).
        assert len(metrics) == 125, f"unexpected metric count: {len(metrics)}"
        assert len(dims) == 28, f"unexpected dimension count: {len(dims)}"

    def test_manifest_fields_present_with_matching_kind_and_source_field(self, official):
        """Every manifest source_capabilities field must be present with matching
        kind AND source_field (e.g. cost -> costInLocalCurrency, date -> dateRange)."""
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MODULE_DIR / "manifest.json")
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from official_fields.json"
            f = by_id[fid]
            assert f["kind"] == m["kind"], f"kind mismatch for {fid}"
            src = f.get("source_field", fid)
            assert src == m["source_field"], (
                f"source_field mismatch for {fid}: official {src!r} vs manifest {m['source_field']!r}"
            )

    def test_key_families_present(self, official):
        ids = {f["field_id"] for f in official}
        # video / viral / lead / conversion / demographics families are covered.
        assert "videoViews" in ids
        assert "viralImpressions" in ids
        assert "oneClickLeads" in ids
        assert "externalWebsitePostClickConversions" in ids
        assert "pivot_MEMBER_JOB_TITLE" in ids
        assert "pivot_MEMBER_COUNTRY_V2" in ids
        # revenue attribution flattened
        assert "revenueWonInUsd" in ids
        assert "returnOnAdSpend" in ids


class TestCatalogSourcesDeclaration:
    @pytest.fixture(scope="class")
    def cfg(self):
        return _load(_CATALOG_SOURCES)

    def test_connector_and_pinned_api_version(self, cfg):
        assert cfg["connector"] == "linkedin-ads"
        assert cfg["api_version"] == "202506"

    def test_generated_at_pinned(self, cfg):
        assert cfg["generated_at"] == "2026-07-21T00:00:00Z"

    def test_official_and_enrichment_sources(self, cfg):
        kinds = {s["kind"] for s in cfg["sources"]}
        assert "official" in kinds
        assert "enrichment" in kinds
        enrich = next(s for s in cfg["sources"] if s["kind"] == "enrichment")
        assert enrich["url"] == "https://docs.supermetrics.com/docs/linkedin-ads-fields.md"

    def test_derivation_note_present(self, cfg):
        assert "_derivation" in cfg
        assert "expansion_rule" in cfg["_derivation"]

    def test_section_tier_map_core_families(self, cfg):
        stm = cfg["section_tier_map"]
        for core_section in ("COST", "CLICKS", "IMPRESSION", "TIME", "STRUCTURE"):
            assert stm.get(core_section) == "core"
        assert stm.get("VIDEO") == "advanced"
        assert stm.get("DEMOGRAPHICS") == "advanced"

    def test_default_tier_is_standard(self, cfg):
        assert cfg["default_tier"] == "standard"


# ---------------------------------------------------------------------------
# Generated artifacts (present ONLY after the orchestrator generator run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _API_CATALOG.exists(), reason="api_catalog.json not generated yet (orchestrator step)")
class TestGeneratedCatalog:
    @pytest.fixture(scope="class")
    def catalog(self):
        return _load(_API_CATALOG)

    def test_connector_and_version(self, catalog):
        assert catalog["connector"] == "linkedin-ads"
        assert catalog["api_version"] == "202506"

    def test_catalog_complete_at_execution(self, catalog):
        """Story 25.9: the catalog is complete at execution -- zero planned;
        every field exposed or excluded with a reason; manifest fields exposed."""
        by_exposure = {}
        for f in catalog["fields"]:
            by_exposure[f["exposure"]] = by_exposure.get(f["exposure"], 0) + 1
        assert by_exposure.get("planned", 0) == 0
        excluded = [f for f in catalog["fields"] if f["exposure"] == "excluded"]
        assert all(f.get("exclusion_reason") for f in excluded)
        manifest = _load(_MODULE_DIR / "manifest.json")
        manifest_ids = {f["field_id"] for f in manifest["source_capabilities"]["fields"]}
        exposed_ids = {f["field_id"] for f in catalog["fields"] if f["exposure"] == "exposed"}
        assert manifest_ids <= exposed_ids


@pytest.mark.skipif(not _FUSION.exists(), reason="fusion-report.json not generated yet (orchestrator step)")
class TestFusionReport:
    @pytest.fixture(scope="class")
    def report(self):
        return _load(_FUSION)

    def test_drift_empty(self, report):
        """No manifest field is absent from the official reference."""
        assert report["drift_ids"] == []

    def test_counts_reconcile(self, report):
        assert report["matched"] + report["official_only"] == report["official_total"]
        if _API_CATALOG.exists():
            catalog = _load(_API_CATALOG)
            assert report["official_total"] == len(catalog["fields"])
