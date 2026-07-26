"""Story 25.7 — TikTok Ads catalog_sources + official snapshot sanity (AC1, AC2).

These tests validate the CURATED official snapshot the dev agent committed and,
when the ORCHESTRATOR has run the generator, the produced api_catalog.json +
fusion-report.json. The generated artifacts are asserted only when present so
the suite stays green before the generator run (the dev agent cannot run shell
in the orchestrated flow -- the builder output is committed but api_catalog.json
is an orchestrator step).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "tiktok-ads"
_SOURCES_DIR = _MODULE_DIR / "catalog_sources"
_OFFICIAL = _SOURCES_DIR / "official_fields.json"
_CATALOG_SOURCES = _SOURCES_DIR / "catalog_sources.json"
_API_CATALOG = _MODULE_DIR / "api_catalog.json"
_FUSION = _SOURCES_DIR / "fusion-report.json"


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Curated official snapshot (committed by the dev agent -- always present).
# ---------------------------------------------------------------------------


class TestOfficialSnapshot:
    @pytest.fixture(scope="class")
    def official(self):
        return _load(_OFFICIAL)

    def test_is_nonempty_list(self, official):
        assert isinstance(official, list) and len(official) > 200

    def test_every_entry_has_required_keys(self, official):
        for f in official:
            assert f.get("field_id")
            assert f.get("kind") in ("metric", "dimension")

    def test_field_ids_unique(self, official):
        ids = [f["field_id"] for f in official]
        assert len(ids) == len(set(ids)), "duplicate field_id in official_fields.json"

    def test_metric_count_on_supermetrics_order(self, official):
        """AC1: metric total is on the order of the Supermetrics 387 figure."""
        metrics = [f for f in official if f["kind"] == "metric"]
        assert 260 <= len(metrics) <= 420, f"unexpected metric count: {len(metrics)}"

    def test_manifest_fields_present_with_matching_kind_and_source_field(self, official):
        """AC1: every manifest source_capabilities field must be present with
        matching kind AND source_field (e.g. date -> stat_time_day)."""
        by_id = {f["field_id"]: f for f in official}
        manifest = _load(_MODULE_DIR / "manifest.json")
        for m in manifest["source_capabilities"]["fields"]:
            fid = m["field_id"]
            assert fid in by_id, f"manifest field {fid!r} missing from official_fields.json"
            f = by_id[fid]
            assert f["kind"] == m["kind"], f"kind mismatch for {fid}"
            src = f.get("source_field", fid)
            assert src == m["source_field"], (
                f"source_field mismatch for {fid}: "
                f"official {src!r} vs manifest {m['source_field']!r}"
            )

    def test_event_family_expansion_present(self, official):
        """AC1: the conversion / in-app / SKAN / onsite families are flattened per event."""
        ids = {f["field_id"] for f in official}
        assert "conversion_complete_payment" in ids
        assert "cost_per_conversion_complete_payment" in ids
        assert "total_app_install" in ids
        assert "skan_total_purchase" in ids
        assert "onsite_on_web_order" in ids


class TestCatalogSourcesDeclaration:
    @pytest.fixture(scope="class")
    def cfg(self):
        return _load(_CATALOG_SOURCES)

    def test_pinned_api_version(self, cfg):
        assert cfg["api_version"] == "v1.3"

    def test_connector_name(self, cfg):
        assert cfg["connector"] == "tiktok-ads"

    def test_generated_at_pinned(self, cfg):
        assert cfg["generated_at"] == "2026-07-21T00:00:00Z"

    def test_official_and_enrichment_sources(self, cfg):
        kinds = {s["kind"] for s in cfg["sources"]}
        assert "official" in kinds
        assert "enrichment" in kinds

    def test_derivation_note_present(self, cfg):
        assert "_derivation" in cfg
        assert "expansion_rule" in cfg["_derivation"]

    def test_section_tier_map_core_families(self, cfg):
        stm = cfg["section_tier_map"]
        for core_section in ("BASIC", "COST", "CLICKS", "IMPRESSION"):
            assert stm.get(core_section) == "core"

    def test_default_tier_standard(self, cfg):
        assert cfg["default_tier"] == "standard"


# ---------------------------------------------------------------------------
# Generated artifacts (present ONLY after the orchestrator generator run).
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _API_CATALOG.exists(), reason="api_catalog.json not generated yet (orchestrator step)"
)
class TestGeneratedCatalog:
    @pytest.fixture(scope="class")
    def catalog(self):
        return _load(_API_CATALOG)

    def test_connector_and_version(self, catalog):
        assert catalog["connector"] == "tiktok-ads"
        assert catalog["api_version"] == "v1.3"

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


@pytest.mark.skipif(
    not _FUSION.exists(), reason="fusion-report.json not generated yet (orchestrator step)"
)
class TestFusionReport:
    @pytest.fixture(scope="class")
    def report(self):
        return _load(_FUSION)

    def test_drift_empty(self, report):
        """AC: no manifest field is absent from the official reference."""
        assert report["drift_ids"] == []

    def test_counts_reconcile(self, report):
        assert report["matched"] + report["official_only"] == report["official_total"]
        if _API_CATALOG.exists():
            catalog = _load(_API_CATALOG)
            assert report["official_total"] == len(catalog["fields"])
