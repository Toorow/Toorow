"""Unit tests for the catalog_gen tooling pipeline (Story 25.3).

Tests are pure-Python, no network, no shell.
Fixtures live under server/tests/tooling/fixtures/.

Coverage:
- Supermetrics parser: excerpt with 3 metrics, 2 dimensions, 1 DEPRECATED entry,
  escaped duplicate rows (must be ignored).
- Discovery parser: mini discovery doc with dimension/metric enum properties.
- Merge engine: all AC4 rules (authority, enrichment_only, exposure, tier mapping,
  tier override, determinism, schema validity via catalog_contract).
- physical_type mapping table.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# sys.path bootstrap so catalog_gen is importable from the scripts/ dir
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
_SERVER_DIR = _REPO_ROOT / "server"

for _p in (_SCRIPTS_DIR, _SERVER_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from catalog_gen.discovery_parser import parse_discovery_document  # noqa: E402
from catalog_gen.merge import (  # noqa: E402
    PHYSICAL_TYPE_MAP,
    EnrichmentField,
    OfficialField,
    map_physical_type,
    merge,
    serialize_catalog,
)
from catalog_gen.supermetrics_parser import parse_supermetrics_markdown  # noqa: E402
from core.catalog_contract import validate_catalog_schema  # noqa: E402

_FIXTURES = Path(__file__).parent / "fixtures"


# ===========================================================================
# Supermetrics parser tests
# ===========================================================================


class TestSupermetricsParser:
    """Test the supermetrics_parser on the excerpt fixture."""

    @pytest.fixture(scope="class")
    def entries(self):
        text = (_FIXTURES / "supermetrics_excerpt.md").read_text(encoding="utf-8")
        return parse_supermetrics_markdown(text)

    def test_total_count(self, entries):
        """Fixture has 3 metrics (impressions, clicks, spend) + 2 dims + 1 deprecated metric."""
        assert len(entries) == 6

    def test_metric_count(self, entries):
        metrics = [e for e in entries if e.kind == "metric"]
        # impressions, clicks, spend (AD section) + old_reach (DEPRECATED)
        assert len(metrics) == 4

    def test_dimension_count(self, entries):
        dims = [e for e in entries if e.kind == "dimension"]
        # country, ad_id
        assert len(dims) == 2

    def test_description_present(self, entries):
        """impressions has a description."""
        imp = next(e for e in entries if e.field_id == "impressions")
        assert imp.description is not None
        assert "times" in imp.description.lower()

    def test_description_absent(self, entries):
        """clicks has no description — should parse to None."""
        clicks = next(e for e in entries if e.field_id == "clicks")
        assert clicks.description is None

    def test_section_assignment(self, entries):
        imp = next(e for e in entries if e.field_id == "impressions")
        assert imp.section == "AD"
        country = next(e for e in entries if e.field_id == "country")
        assert country.section == "TARGETING"

    def test_deprecated_section(self, entries):
        old_reach = next(e for e in entries if e.field_id == "old_reach")
        assert old_reach.deprecated is True
        assert old_reach.section == "DEPRECATED"

    def test_non_deprecated_fields_not_flagged(self, entries):
        non_deprecated = [e for e in entries if e.field_id != "old_reach"]
        assert all(not e.deprecated for e in non_deprecated)

    def test_data_type_extraction(self, entries):
        """Data type bracket-token is extracted correctly."""
        imp = next(e for e in entries if e.field_id == "impressions")
        assert imp.data_type == "int"
        country = next(e for e in entries if e.field_id == "country")
        assert country.data_type == "string.geo.country.twolettercode"
        spend = next(e for e in entries if e.field_id == "spend")
        assert spend.data_type == "currency"

    def test_escaped_rows_not_parsed_as_fields(self, entries):
        """Escaped rows (\\|) must not create duplicate entries."""
        ids = [e.field_id for e in entries]
        assert len(ids) == len(set(ids)), "Duplicate field_ids detected (escaped rows leaked)"


# ===========================================================================
# Discovery parser tests
# ===========================================================================


class TestDiscoveryParser:
    @pytest.fixture(scope="class")
    def doc(self):
        return json.loads((_FIXTURES / "discovery_mini.json").read_text(encoding="utf-8"))

    def test_dimension_entries(self, doc):
        config = {
            "schema_path": ["schemas", "ReportDefinition", "properties"],
            "enum_property": "Dimension",
            "kind": "dimension",
        }
        entries = parse_discovery_document(doc, config)
        assert len(entries) == 3
        ids = [e.field_id for e in entries]
        assert "DATE" in ids
        assert "ADVERTISER_ID" in ids
        assert "ORDER_ID" in ids

    def test_metric_entries(self, doc):
        config = {
            "schema_path": ["schemas", "ReportDefinition", "properties"],
            "enum_property": "Metric",
            "kind": "metric",
        }
        entries = parse_discovery_document(doc, config)
        assert len(entries) == 2
        assert all(e.kind == "metric" for e in entries)

    def test_description_extraction(self, doc):
        config = {
            "schema_path": ["schemas", "ReportDefinition", "properties"],
            "enum_property": "Dimension",
            "kind": "dimension",
        }
        entries = parse_discovery_document(doc, config)
        date_entry = next(e for e in entries if e.field_id == "DATE")
        assert date_entry.description is not None
        assert "date" in date_entry.description.lower()

    def test_empty_description_is_none(self, doc):
        config = {
            "schema_path": ["schemas", "ReportDefinition", "properties"],
            "enum_property": "Dimension",
            "kind": "dimension",
        }
        entries = parse_discovery_document(doc, config)
        order_entry = next(e for e in entries if e.field_id == "ORDER_ID")
        assert order_entry.description is None

    def test_missing_path_returns_empty(self, doc):
        config = {
            "schema_path": ["schemas", "DoesNotExist", "properties"],
            "enum_property": "Dimension",
            "kind": "dimension",
        }
        assert parse_discovery_document(doc, config) == []

    def test_kind_propagated(self, doc):
        config = {
            "schema_path": ["schemas", "ReportDefinition", "properties"],
            "enum_property": "Dimension",
            "kind": "dimension",
        }
        entries = parse_discovery_document(doc, config)
        assert all(e.kind == "dimension" for e in entries)


# ===========================================================================
# physical_type mapping tests
# ===========================================================================


class TestPhysicalTypeMapping:
    @pytest.mark.parametrize(
        "token,expected",
        [
            ("int", "integer"),
            ("integer", "integer"),
            ("currency", "decimal"),
            ("decimal", "decimal"),
            ("float", "decimal"),
            ("date", "date"),
            ("datetime", "datetime"),
            ("timestamp", "datetime"),
            ("boolean", "boolean"),
            ("bool", "boolean"),
            ("string", "string"),
            ("text", "string"),
            ("json", "json"),
            ("object", "json"),
            # Compound tokens
            ("string.geo.country.twolettercode", "string"),
            ("int.count", "integer"),
            ("currency.usd", "decimal"),
            ("date.iso", "date"),
            ("", "string"),
        ],
    )
    def test_map_physical_type(self, token, expected):
        assert map_physical_type(token) == expected

    def test_all_map_values_are_valid(self):
        valid = {"string", "integer", "decimal", "boolean", "date", "datetime", "json"}
        for token, pt in PHYSICAL_TYPE_MAP.items():
            assert pt in valid, f"PHYSICAL_TYPE_MAP[{token!r}] = {pt!r} is not a valid physical_type"


# ===========================================================================
# Merge engine tests
# ===========================================================================


def _make_sources_config(**overrides) -> dict:
    base = {
        "connector": "test-connector",
        "api_version": "v1",
        "generated_at": "2026-07-21T00:00:00Z",
        "default_tier": "standard",
        "sources": [
            {
                "url": "https://example.dev/docs/api",
                "kind": "official",
                "fetched_at": "2026-07-21T00:00:00Z",
            }
        ],
        "section_tier_map": {"AD": "core", "TARGETING": "standard"},
        "field_tier_overrides": {},
    }
    base.update(overrides)
    return base


def _make_official(field_id: str, kind: str = "metric", data_type: str = "int", section: str = "AD") -> OfficialField:
    return OfficialField(field_id=field_id, kind=kind, data_type=data_type, section=section)


def _make_enrichment(field_id: str, kind: str = "metric", data_type: str = "int", section: str = "AD", description: str | None = None) -> EnrichmentField:
    return EnrichmentField(field_id=field_id, kind=kind, data_type=data_type, description=description, section=section)


class TestMergeAuthority:
    """Official source is the authority for field existence and kind."""

    def test_official_only_field_present_in_catalog(self):
        official = [_make_official("f1")]
        catalog, report = merge(official, [], {}, _make_sources_config())
        field_ids = [f["field_id"] for f in catalog["fields"]]
        assert "f1" in field_ids

    def test_enrichment_only_NOT_in_catalog(self):
        """Fields in enrichment only NEVER enter the catalog."""
        official = [_make_official("f1")]
        enrichment = [_make_enrichment("f1"), _make_enrichment("enrichment_only")]
        catalog, report = merge(official, enrichment, {}, _make_sources_config())
        field_ids = [f["field_id"] for f in catalog["fields"]]
        assert "enrichment_only" not in field_ids

    def test_enrichment_only_IN_fusion_report(self):
        official = [_make_official("f1")]
        enrichment = [_make_enrichment("f1"), _make_enrichment("enrichment_only")]
        catalog, report = merge(official, enrichment, {}, _make_sources_config())
        assert "enrichment_only" in report.enrichment_only_ids
        assert report.enrichment_only == 1

    def test_enrichment_description_used_when_official_has_none(self):
        official = [OfficialField(field_id="f1", kind="metric", data_type="int", section="AD", description=None)]
        enrichment = [EnrichmentField(field_id="f1", kind="metric", data_type="int", description="Enriched desc", section="AD")]
        catalog, _ = merge(official, enrichment, {}, _make_sources_config())
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["description"] == "Enriched desc"

    def test_official_description_takes_priority_over_enrichment(self):
        official = [OfficialField(field_id="f1", kind="metric", data_type="int", section="AD", description="Official desc")]
        enrichment = [EnrichmentField(field_id="f1", kind="metric", data_type="int", description="Enriched desc", section="AD")]
        catalog, _ = merge(official, enrichment, {}, _make_sources_config())
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["description"] == "Official desc"

    def test_kind_from_official_not_enrichment(self):
        """Even if enrichment says 'dimension', official kind wins."""
        official = [OfficialField(field_id="f1", kind="metric", data_type="int", section="AD")]
        enrichment = [EnrichmentField(field_id="f1", kind="dimension", data_type="int", description=None, section="AD")]
        catalog, _ = merge(official, enrichment, {}, _make_sources_config())
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["kind"] == "metric"


class TestMergeExposure:
    """Manifest-derived exposure rules."""

    def _manifest_with_fields(self, *field_ids: str) -> dict:
        return {
            "source_capabilities": {
                "fields": [{"field_id": fid, "kind": "metric"} for fid in field_ids]
            }
        }

    def test_manifest_field_is_exposed(self):
        official = [_make_official("f1"), _make_official("f2")]
        manifest = self._manifest_with_fields("f1")
        catalog, _ = merge(official, [], manifest, _make_sources_config())
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["exposure"] == "exposed"

    def test_non_manifest_field_is_planned(self):
        official = [_make_official("f1"), _make_official("f2")]
        manifest = self._manifest_with_fields("f1")
        catalog, _ = merge(official, [], manifest, _make_sources_config())
        f2 = next(f for f in catalog["fields"] if f["field_id"] == "f2")
        assert f2["exposure"] == "planned"

    def test_drift_detection(self):
        """Manifest field absent from official -> drift list in report."""
        official = [_make_official("f1")]
        manifest = self._manifest_with_fields("f1", "ghost_field")
        _, report = merge(official, [], manifest, _make_sources_config())
        assert "ghost_field" in report.drift_ids

    def test_no_drift_when_all_manifest_fields_in_official(self):
        official = [_make_official("f1"), _make_official("f2")]
        manifest = self._manifest_with_fields("f1", "f2")
        _, report = merge(official, [], manifest, _make_sources_config())
        assert report.drift_ids == []


class TestMergeTier:
    """Tier mapping: section map > field override > default."""

    def test_section_tier_mapping(self):
        official = [_make_official("f1", section="AD")]
        catalog, _ = merge(official, [], {}, _make_sources_config(section_tier_map={"AD": "core"}))
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["tier"] == "core"

    def test_default_tier_when_section_not_mapped(self):
        official = [_make_official("f1", section="UNKNOWN_SECTION")]
        catalog, _ = merge(official, [], {}, _make_sources_config(section_tier_map={}, default_tier="advanced"))
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["tier"] == "advanced"

    def test_field_tier_override(self):
        official = [_make_official("f1", section="AD")]
        cfg = _make_sources_config(
            section_tier_map={"AD": "core"},
            field_tier_overrides={"f1": "advanced"},
        )
        catalog, _ = merge(official, [], {}, cfg)
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["tier"] == "advanced"

    def test_default_tier_fallback_without_config(self):
        """No section_tier_map -> default_tier 'standard'."""
        official = [_make_official("f1", section="AD")]
        catalog, _ = merge(official, [], {}, _make_sources_config(section_tier_map={}))
        f1 = next(f for f in catalog["fields"] if f["field_id"] == "f1")
        assert f1["tier"] == "standard"


class TestMergeDeterminism:
    """Output must be byte-for-byte identical on two runs with the same inputs."""

    def _run(self) -> str:
        official = [
            _make_official("b_field", section="AD"),
            _make_official("a_field", section="TARGETING"),
            _make_official("c_field", section="AD"),
        ]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        return serialize_catalog(catalog)

    def test_determinism(self):
        out1 = self._run()
        out2 = self._run()
        assert out1 == out2, "Two runs produced different output (non-deterministic)"

    def test_fields_are_sorted(self):
        official = [
            _make_official("z_field", section="AD"),
            _make_official("a_field", section="AD"),
            _make_official("m_field", section="AD"),
        ]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        ids = [f["field_id"] for f in catalog["fields"]]
        assert ids == sorted(ids)

    def test_lf_newlines_and_trailing_newline(self):
        official = [_make_official("f1")]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        serialized = serialize_catalog(catalog)
        assert "\r\n" not in serialized, "Windows CRLF found in serialized output"
        assert serialized.endswith("\n"), "Missing trailing newline"

    def test_no_clock_in_generated_at(self):
        """generated_at must come from sources_config, not from the clock."""
        official = [_make_official("f1")]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        # The generated_at should be the one from config, not from datetime.now()
        assert catalog["generated_at"] == "2026-07-21T00:00:00Z"


class TestMergeSchemaValidity:
    """Output must pass validate_catalog_schema with zero issues."""

    def test_schema_valid_minimal(self):
        official = [_make_official("f1")]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        issues = validate_catalog_schema(catalog)
        assert issues == [], f"Schema issues found: {[i.message for i in issues]}"

    def test_schema_valid_full_fixture(self):
        """Use all fixture files to verify schema validity on a realistic catalog."""
        official_data = json.loads(
            (_FIXTURES / "official_fields_mini.json").read_text(encoding="utf-8")
        )
        official = [
            OfficialField(
                field_id=f["field_id"],
                kind=f["kind"],
                data_type=f.get("data_type", "string"),
                description=f.get("description"),
                section=f.get("section", "UNKNOWN"),
            )
            for f in official_data
        ]

        sm_text = (_FIXTURES / "supermetrics_excerpt.md").read_text(encoding="utf-8")
        from catalog_gen.supermetrics_parser import parse_supermetrics_markdown  # noqa: PLC0415
        sm_entries = parse_supermetrics_markdown(sm_text)
        enrichment = [
            EnrichmentField(
                field_id=e.field_id,
                kind=e.kind,
                data_type=e.data_type,
                description=e.description,
                section=e.section,
                deprecated=e.deprecated,
            )
            for e in sm_entries
        ]

        manifest = json.loads(
            (_FIXTURES / "manifest_mini.json").read_text(encoding="utf-8")
        )
        cfg = json.loads(
            (_FIXTURES / "sources_config_mini.json").read_text(encoding="utf-8")
        )

        catalog, report = merge(official, enrichment, manifest, cfg)
        issues = validate_catalog_schema(catalog)
        assert issues == [], f"Schema issues: {[i.message for i in issues]}"

    def test_enrichment_only_ids_in_report_not_catalog(self):
        """Enrichment-only IDs in report, absent from catalog fields."""
        official = [_make_official("f1")]
        enrichment = [_make_enrichment("f1"), _make_enrichment("enrichment_extra")]
        catalog, report = merge(official, enrichment, {}, _make_sources_config())
        field_ids = {f["field_id"] for f in catalog["fields"]}
        assert "enrichment_extra" not in field_ids
        assert "enrichment_extra" in report.enrichment_only_ids

    def test_report_counts_consistent(self):
        official = [_make_official("f1"), _make_official("f2")]
        enrichment = [_make_enrichment("f1"), _make_enrichment("enrich_only")]
        _, report = merge(official, enrichment, {}, _make_sources_config())
        assert report.official_total == 2
        assert report.enrichment_total == 2
        assert report.matched == 1
        assert report.enrichment_only == 1
        assert report.official_only == 1

    def test_source_field_defaults_to_field_id(self):
        """source_field defaults to field_id when not overridden."""
        official = [_make_official("my_field")]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        f = next(f for f in catalog["fields"] if f["field_id"] == "my_field")
        assert f["source_field"] == "my_field"

    def test_source_field_passthrough_when_provided(self):
        """25.4 AC2: an explicit OfficialField.source_field is emitted verbatim.

        Mirrors the Meta manifest mapping date -> date_start; without this the
        gate reports source_field_mismatch on the exposed `date` dimension.
        """
        official = [
            OfficialField(
                field_id="date",
                kind="dimension",
                data_type="date",
                section="TIME",
                source_field="date_start",
            )
        ]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        f = next(f for f in catalog["fields"] if f["field_id"] == "date")
        assert f["source_field"] == "date_start"

    def test_source_field_empty_falls_back_to_field_id(self):
        """A falsy source_field (None/"") falls back to field_id."""
        official = [
            OfficialField(field_id="spend", kind="metric", source_field=None),
            OfficialField(field_id="clicks", kind="metric", source_field=""),
        ]
        catalog, _ = merge(official, [], {}, _make_sources_config())
        by_id = {f["field_id"]: f for f in catalog["fields"]}
        assert by_id["spend"]["source_field"] == "spend"
        assert by_id["clicks"]["source_field"] == "clicks"

    def test_deprecated_flag_propagated(self):
        """Deprecated enrichment flag is reflected in catalog field."""
        official = [_make_official("old_metric")]
        enrichment = [
            EnrichmentField(
                field_id="old_metric",
                kind="metric",
                data_type="int",
                description="Old metric",
                section="DEPRECATED",
                deprecated=True,
            )
        ]
        catalog, _ = merge(official, enrichment, {}, _make_sources_config())
        f = next(f for f in catalog["fields"] if f["field_id"] == "old_metric")
        assert f.get("deprecated") is True


# ---------------------------------------------------------------------------
# CLI end-to-end (review F-A1/F-A2: the CLI layer was untested, and its config
# shape fed loader-only keys into the schema-validated output)
# ---------------------------------------------------------------------------


class TestCliEndToEnd:
    """Run build_catalog() on a realistic sources dir: loader config keys
    (file, parser) must never leak into the emitted catalog sources."""

    @staticmethod
    def _write_sources_dir(tmp_path: Path) -> Path:
        official = [
            {"field_id": "alpha_metric", "kind": "metric", "data_type": "int",
             "section": "ALPHA"},
            {"field_id": "beta_dimension", "kind": "dimension"},
            # duplicate on purpose: merge must dedup and report it (review F-B1)
            {"field_id": "alpha_metric", "kind": "metric"},
        ]
        (tmp_path / "official_fields.json").write_text(
            json.dumps(official), encoding="utf-8"
        )
        config = {
            "connector": "zz-e2e-module",
            "api_version": "v1-test",
            "generated_at": "2026-07-21T00:00:00Z",
            "section_tier_map": {"ALPHA": "core"},
            "sources": [
                {
                    "url": "https://official.example/reference",
                    "kind": "official",
                    "fetched_at": "2026-07-21T00:00:00Z",
                    # loader-only keys the schema forbids in output:
                    "file": "official_fields.json",
                    "parser": "official_fields_json",
                },
            ],
        }
        (tmp_path / "catalog_sources.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        return tmp_path

    def _build(self, tmp_path: Path, **kwargs):
        import build_api_catalog

        out_path = tmp_path / "out" / "api_catalog.json"
        build_api_catalog.build_catalog(
            module="zz-e2e-module-does-not-exist",
            sources_dir=self._write_sources_dir(tmp_path),
            out_path=out_path,
            **kwargs,
        )
        return out_path

    def test_end_to_end_emits_schema_valid_catalog(self, tmp_path):
        out_path = self._build(tmp_path)
        catalog = json.loads(out_path.read_text(encoding="utf-8"))
        assert validate_catalog_schema(catalog) == []
        # F-A1: only the emitted keys survive the projection
        assert all(
            set(entry) <= {"url", "kind", "fetched_at"}
            for entry in catalog["sources"]
        )
        ids = [f["field_id"] for f in catalog["fields"]]
        assert ids == ["alpha_metric", "beta_dimension"]  # deduped + sorted
        by_id = {f["field_id"]: f for f in catalog["fields"]}
        assert by_id["alpha_metric"]["tier"] == "core"  # section_tier_map applied
        assert by_id["alpha_metric"]["exposure"] == "planned"  # no manifest

    def test_dry_run_writes_nothing(self, tmp_path):
        out_path = self._build(tmp_path, dry_run=True)
        assert not out_path.exists()

    def test_report_written_with_duplicates(self, tmp_path):
        report_path = tmp_path / "report.json"
        self._build(tmp_path, report_path=report_path)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["official_total"] == 2  # after dedup
        assert report["official_duplicate_ids"] == ["alpha_metric"]

    def test_official_fields_json_loader_passes_source_field(self, tmp_path):
        """25.4 AC2: the official_fields_json loader carries source_field into the catalog."""
        import build_api_catalog

        official = [
            {"field_id": "date", "kind": "dimension", "data_type": "date",
             "section": "TIME", "source_field": "date_start"},
            {"field_id": "spend", "kind": "metric", "data_type": "currency",
             "section": "COST"},
        ]
        (tmp_path / "official_fields.json").write_text(
            json.dumps(official), encoding="utf-8"
        )
        config = {
            "connector": "zz-sf-module",
            "api_version": "v23.0",
            "generated_at": "2026-07-21T00:00:00Z",
            "section_tier_map": {"TIME": "core", "COST": "core"},
            "sources": [
                {
                    "url": "https://official.example/reference",
                    "kind": "official",
                    "fetched_at": "2026-07-21T00:00:00Z",
                    "file": "official_fields.json",
                    "parser": "official_fields_json",
                },
            ],
        }
        (tmp_path / "catalog_sources.json").write_text(
            json.dumps(config), encoding="utf-8"
        )
        out_path = tmp_path / "out" / "api_catalog.json"
        build_api_catalog.build_catalog(
            module="zz-sf-module-does-not-exist",
            sources_dir=tmp_path,
            out_path=out_path,
        )
        catalog = json.loads(out_path.read_text(encoding="utf-8"))
        by_id = {f["field_id"]: f for f in catalog["fields"]}
        assert by_id["date"]["source_field"] == "date_start"
        assert by_id["spend"]["source_field"] == "spend"  # default


class TestExposurePolicy:
    """Story 25.8: exposure_policy catalog_driven -- every field exposed except
    excluded_sections (mandatory reason); manifest fields exposed regardless."""

    @staticmethod
    def _merge(policy=None, excluded=None, manifest_ids=()):
        from catalog_gen.merge import OfficialField, merge

        config = {
            "connector": "zz-policy",
            "api_version": "v1",
            "generated_at": "2026-07-21T00:00:00Z",
            "sources": [
                {"url": "https://o.example", "kind": "official",
                 "fetched_at": "2026-07-21T00:00:00Z"}
            ],
        }
        if policy:
            config["exposure_policy"] = policy
        if excluded is not None:
            config["excluded_sections"] = excluded
        manifest = {"source_capabilities": {"fields": [
            {"field_id": i, "kind": "metric", "source_field": i} for i in manifest_ids
        ]}}
        fields = [
            OfficialField(field_id="alpha_metric", kind="metric", section="ALPHA"),
            OfficialField(field_id="beta_metric", kind="metric", section="BETA"),
        ]
        catalog, _ = merge(fields, [], manifest, config)
        return {f["field_id"]: f for f in catalog["fields"]}

    def test_default_policy_is_manifest(self):
        by_id = self._merge(manifest_ids=("alpha_metric",))
        assert by_id["alpha_metric"]["exposure"] == "exposed"
        assert by_id["beta_metric"]["exposure"] == "planned"

    def test_catalog_driven_exposes_everything_not_excluded(self):
        by_id = self._merge(policy="catalog_driven",
                            excluded={"BETA": "shape not implemented"})
        assert by_id["alpha_metric"]["exposure"] == "exposed"
        assert by_id["beta_metric"]["exposure"] == "excluded"
        assert by_id["beta_metric"]["exclusion_reason"] == "shape not implemented"

    def test_manifest_field_stays_exposed_even_in_excluded_section(self):
        by_id = self._merge(policy="catalog_driven",
                            excluded={"ALPHA": "reason"},
                            manifest_ids=("alpha_metric",))
        assert by_id["alpha_metric"]["exposure"] == "exposed"

    def test_invalid_policy_raises(self):
        import pytest as _pytest

        with _pytest.raises(ValueError, match="exposure_policy"):
            self._merge(policy="everything")

    def test_excluded_section_without_reason_raises(self):
        import pytest as _pytest

        with _pytest.raises(ValueError, match="requires a non-empty reason"):
            self._merge(policy="catalog_driven", excluded={"BETA": ""})
