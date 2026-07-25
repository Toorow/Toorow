"""Repository-wide source-capability contract gate (Story 12.1)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.loader import scan_and_load_modules
from core.source_capabilities import (
    normalize_capabilities,
    validate_manifest_capabilities,
    validate_public_response,
)

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

MODULES_DIR = Path(__file__).parents[2] / "modules"
CORE_DIR = Path(__file__).parents[2] / "core"


def _manifests() -> list[tuple[Path, dict]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(MODULES_DIR.glob("*/manifest.json"))
    ]


def test_every_builtin_has_a_valid_normalizable_capability_contract():
    manifests = _manifests()
    # Concurrent sessions add modules: assert a fail-closed MINIMUM, not an
    # exact count that churns per session.
    assert len(manifests) >= 12
    # GSC full searchanalytics.query coverage added 6 profiles (discover, googleNews,
    # news, image, video, searchAppearance): 29 -> 35.
    # Story 25.8 added meta-ads catalog_daily (catalog_driven): 35 -> 36.
    # Connector wave 2026-07-21 (adjust 1, doubleverify 4, ias 3, stripe catalog_daily 1): 36 -> 45.
    assert sum(len(item[1]["report_profiles"]) for item in manifests) >= 44

    for path, manifest in manifests:
        assert manifest["schema_version"] == "1.2", path
        assert validate_manifest_capabilities(manifest) == [], path
        response = normalize_capabilities(
            manifest,
            project_id="project-conformance",
            connection_ref_id="connection-conformance",
        )
        assert validate_public_response(response) == [], path
        serialized = json.dumps(response, sort_keys=True)
        assert "_ai_53" not in serialized
        assert "secret" not in serialized.lower()


def test_every_profile_is_dispatchable_or_explicitly_unavailable():
    loaded = {item.name: item for item in scan_and_load_modules(MODULES_DIR)}
    manifests = _manifests()
    assert set(loaded) == {manifest["name"] for _, manifest in manifests}

    profile_count = 0
    for path, manifest in manifests:
        module = loaded[manifest["name"]]
        reports = {report["id"]: report for report in manifest["source_capabilities"]["reports"]}
        assert set(reports) == {profile["id"] for profile in manifest["report_profiles"]}, path

        for profile_id, report in reports.items():
            profile_count += 1
            availability = report["availability"]
            if availability["status"] == "selectable":
                callable_name = report["dispatch"]["callable"]
                assert callable(getattr(module.connector_module, callable_name, None)), (
                    f"{path}: {profile_id} -> {callable_name}"
                )
            else:
                assert availability["reason_code"], f"{path}: {profile_id}"
                assert availability["follow_up"], f"{path}: {profile_id}"
                assert "dispatch" not in report, f"{path}: {profile_id}"

    # 25.9 added one catalog_daily per module and concurrent sessions add
    # modules: assert the fail-closed minimum instead of an exact total.
    assert profile_count >= 44


def test_catalog_driven_profiles_have_an_api_catalog():
    """Story 25.8 (AC4): a catalog_driven profile is only honest if its module ships
    an api_catalog.json — the catalog IS the execution contract core validates the
    selection against. Source-agnostic: scans the generic selection_mode field."""
    manifests = _manifests()
    catalog_driven_modules = {
        manifest["name"]
        for path, manifest in manifests
        for report in manifest["source_capabilities"]["reports"]
        if report.get("selection_mode") == "catalog_driven"
    }
    # meta-ads is the 25.8 reference; the check is generic for any future module.
    assert "meta-ads" in catalog_driven_modules
    for module_name in catalog_driven_modules:
        catalog_path = MODULES_DIR / module_name / "api_catalog.json"
        assert catalog_path.is_file(), (
            f"{module_name}: declares a catalog_driven profile but has no "
            f"api_catalog.json (the execution contract)"
        )


def test_capability_contract_core_files_contain_no_builtin_vocabulary():
    module_names = {
        manifest["name"] for _, manifest in _manifests() if manifest["name"] != "generic"
    }
    vocabulary = module_names | {name.replace("-", "_") for name in module_names}
    governed_files = [
        CORE_DIR / "source_capabilities.py",
        CORE_DIR / "schemas" / "source-capabilities.schema.json",
        CORE_DIR / "catalog_contract.py",
        CORE_DIR / "schemas" / "api-catalog.schema.json",
    ]

    import re
    violations = []
    for path in governed_files:
        source = path.read_text(encoding="utf-8").lower()
        for term in sorted(vocabulary):
            if re.search(rf"\b{re.escape(term)}\b", source):
                violations.append(f"{path.name}: {term}")

    assert violations == []

def test_configuration_dependent_profiles_are_conservatively_unavailable():
    manifests = {manifest["name"]: manifest for _, manifest in _manifests()}
    # GSC full coverage: gsc/page_daily is no longer configuration-blocked — every GSC
    # profile now has a pull_<profile_id> dispatch shim with the GSC_SITE_URL env
    # fallback, the same mechanics query_page_daily already shipped as selectable.
    expected = {
        ("generic", "tabular_daily"): "Complete managed CSV and Excel import support.",
        ("google-sheets", "sheet_daily"): "Complete recurring Google Sheets synchronization.",
    }

    for (module_name, report_id), follow_up in expected.items():
        reports = {
            report["id"]: report
            for report in manifests[module_name]["source_capabilities"]["reports"]
        }
        report = reports[report_id]
        assert report["availability"]["status"] == "unavailable"
        assert report["availability"]["follow_up"] == follow_up
        assert "dispatch" not in report

    linkedin_reports = manifests["linkedin-ads"]["source_capabilities"]["reports"]
    assert {
        report["quota_cost"]["unit"] for report in linkedin_reports
    } == {"returned_metric_value"}
