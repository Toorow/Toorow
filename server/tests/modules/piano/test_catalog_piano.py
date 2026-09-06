"""Story 28.1 -- piano api_catalog / catalog_sources contract.

The committed catalog IS the execution contract. These assertions pin the
STANDARD BASELINE (45 metrics + 103 standard property keys of the dossier),
the dynamic-catalog seam (field_discovery.mode = dynamic in catalog_sources),
the non-additive marking (AD-4), and the api_version coupling to the manifest
pin. All offline (no network).
"""

from __future__ import annotations

import json

from core.catalog_contract import (
    catalog_default_selection,
    diff_catalog_manifest,
    validate_catalog_schema,
)

from .conftest import MANIFEST, MODULE_DIR


def test_catalog_validates_against_core_schema(catalog):
    assert validate_catalog_schema(catalog) == []


def test_catalog_matches_official_fields_generator_output(catalog):
    """The committed api_catalog fields must equal what the deterministic
    generator emits (baseline is generator-derived, byte-stable)."""
    official = json.loads(
        (MODULE_DIR / "catalog_sources" / "official_fields.json").read_text(
            encoding="utf-8"
        )
    )
    official_ids = {f["field_id"] for f in official}
    catalog_ids = {f["field_id"] for f in catalog["fields"]}
    assert catalog_ids == official_ids


def test_baseline_counts_match_the_dossier_folder(catalog):
    """Baseline = 45 explicit metrics + 103 standard property keys (dossier
    Annex A+B) = 148 fields -- NO round-number padding. A real site EXCEEDS this
    floor via custom keys resolved live (dynamic)."""
    metrics = [f for f in catalog["fields"] if f["kind"] == "metric"]
    dimensions = [f for f in catalog["fields"] if f["kind"] == "dimension"]
    assert len(metrics) == 45, f"expected 45 baseline metrics, got {len(metrics)}"
    assert len(dimensions) == 103, f"expected 103 baseline dimensions, got {len(dimensions)}"
    assert len(catalog["fields"]) == 148, len(catalog["fields"])
    # 'date' is a REQUESTABLE property column for Piano (not response-only).
    by_id = {f["field_id"]: f for f in catalog["fields"]}
    assert by_id["date"]["kind"] == "dimension"
    assert by_id["date"]["source_field"] == "date"


def test_api_version_matches_the_manifest_pin(catalog, catalog_sources):
    assert catalog["api_version"] == MANIFEST["provider_api_version"] == "v3"
    assert catalog_sources["api_version"] == "v3"


def test_field_discovery_dynamic_declared_in_catalog_sources(catalog_sources):
    """The Piano-specific seam: field_discovery.mode = dynamic (per-site keys
    validated live). The manifest carries the schema-legal 'runtime' variant."""
    fd = catalog_sources["field_discovery"]
    assert fd["mode"] == "dynamic"
    assert fd["discovery_strategy"] == "baseline_plus_validate_on_use"
    assert fd["allowed_targets"] == ["site"]
    assert MANIFEST["source_capabilities"]["field_discovery"]["mode"] == "runtime"


def test_exposure_policy_catalog_driven(catalog):
    assert catalog.get("exposure_policy") == "catalog_driven"


def test_non_additive_metrics_marked_and_visit_scoped(catalog_sources, catalog):
    """AD-4: visit/visitor-scoped metrics are marked non-additive in the
    catalog_sources block and exist as metrics in the catalog."""
    keys = set(catalog_sources["non_additive_metrics"]["keys"])
    assert {"m_visits", "m_unique_visitors"} <= keys
    by_id = {f["field_id"]: f for f in catalog["fields"]}
    for key in keys:
        assert by_id[key]["kind"] == "metric", key


def test_units_driven_by_physical_type_not_id_suffix(catalog):
    """A ratio metric (m_bounce_rate) is physical_type decimal; a count
    (m_visits) is integer -- driven by the catalogued type, never a suffix."""
    by_id = {f["field_id"]: f for f in catalog["fields"]}
    assert by_id["m_bounce_rate"]["physical_type"] == "decimal"
    assert by_id["m_visits"]["physical_type"] == "integer"
    assert by_id["m_conversion_rate"]["physical_type"] == "decimal"


def test_excluded_sections_carry_a_reason(catalog):
    """AV QoS / MV testing / consent families are excluded with a reason."""
    excluded = [f for f in catalog["fields"] if f["exposure"] == "excluded"]
    assert excluded, "expected some excluded baseline fields"
    for f in excluded:
        assert f.get("exclusion_reason"), f["field_id"]


def test_manifest_catalog_diff_is_clean(catalog):
    assert diff_catalog_manifest(catalog, MANIFEST) == []


def test_error_map_declares_only_matchable_prefix_keys(connector):
    """F-3 + AI-114: what error_map declares must be MATCHABLE *and* must REFINE.

    F-3 (unchanged, and still enforced for every key present):
    _raise_provider_error reduces the API-Code to its Category PREFIX before
    handing the payload to core, so a key must be '<status>:<Category>' with NO
    '_subcode' -- a full-code key (401:BadAuthentication_NoHeader) is
    unreachable and classifies nothing.

    AI-114 (2026-08-01) emptied the map, and the SECOND assertion below is the
    reason -- not a relaxation of the first. Every Category piano had declared
    (BadAuthentication/401, UnauthorizedSite+InvalidSpace/403, Invalid*/400,
    UnknownError/500) returned exactly the class core.pull_errors derives from
    the HTTP status alone, so not one entry could change a verdict: the map
    made the taxonomy LOOK refined where it was not. Pinning both halves is
    what stops a decorative entry from coming back unnoticed -- a restatement
    of pure HTTP now FAILS here instead of passing as documentation.
    """
    from core.pull_errors import classify_http_error

    for full_key, canonical in MANIFEST["error_map"].items():
        if full_key.startswith("_"):
            continue
        status, colon, code = full_key.partition(":")
        assert colon and status.isdigit(), full_key
        assert "_" not in code, (
            f"error_map key {full_key!r} carries a subcode -- unreachable "
            "(the connector matches on the Category prefix only)"
        )
        pure_http = classify_http_error(int(status), None, None).error_class
        assert canonical != pure_http, (
            f"error_map key {full_key!r} -> {canonical!r} restates the class "
            f"core already derives from HTTP {status} alone. It cannot change "
            "any verdict; declaring it claims a refinement that does not exist."
        )

    # The unreachable full-code keys stay gone.
    assert "403:InvalidSpace_NoActiveSite" not in MANIFEST["error_map"]
    assert "401:BadAuthentication_NoHeader" not in MANIFEST["error_map"]

    # The Category-prefix reduction is NOT dead code just because the map is
    # empty: it carries the InvalidColumns dynamic-catalog drift signal, and it
    # is what would make a future key matchable on the day one is justified.
    assert connector._code_prefix("InvalidColumns_UnknownColumn") == "InvalidColumns"
    assert connector._code_prefix("UnauthorizedSite") == "UnauthorizedSite"


def test_tier_core_default_selection_is_non_empty(catalog):
    sel = catalog_default_selection(catalog)
    assert sel["metrics"], "tier-core default must carry metrics"
    assert sel["dimensions"], "tier-core default must carry dimensions"
    assert "m_visits" in sel["metrics"]
    assert "date" in sel["dimensions"]
