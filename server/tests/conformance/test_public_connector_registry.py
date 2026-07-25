"""Fail-closed public connector registry contract tests."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[3]
MODULES_DIR = ROOT / "server" / "modules"
SCRIPT_PATH = ROOT / "scripts" / "export_connector_registry.py"
REGISTRY_PATH = ROOT / "web" / "src" / "generated" / "connector-registry.json"


def _load_exporter():
    spec = importlib.util.spec_from_file_location("export_connector_registry", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _manifests() -> list[tuple[Path, dict]]:
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(MODULES_DIR.glob("*/manifest.json"))
    ]


def test_every_builtin_declares_fail_closed_public_catalog_metadata():
    manifests = _manifests()
    assert len(manifests) == 37

    for path, manifest in manifests:
        assert path.read_text(encoding="utf-8").count('"public_catalog"') == 1, path
        catalog = manifest["public_catalog"]
        assert catalog["visibility"] in {"public", "hidden"}, path
        if catalog["visibility"] == "public":
            assert catalog["category"], path
            assert catalog["onboarding_modes"], path
            assert catalog["verification"]["status"] == "blocked", path
            assert catalog["verification"]["reason_code"], path
            assert catalog["verification"]["follow_up"], path


def test_projection_is_deterministic_complete_and_fail_closed():
    exporter = _load_exporter()
    first = exporter.build_registry(MODULES_DIR)
    second = exporter.build_registry(MODULES_DIR)

    assert exporter.serialize_registry(first) == exporter.serialize_registry(second)
    assert first["contractVersion"] == "1"
    assert len(first["connectors"]) == 37
    # GSC full searchanalytics.query coverage added 6 profiles: 29 -> 35.
    # Story 25.8 added meta-ads catalog_daily (catalog_driven): 35 -> 36.
    # adjust + ias + doubleverify connectors landed alongside the catalog_daily
    # rollout: 36 -> 45 total report profiles across 15 connectors.
    # epic-26 (google-ads/pinterest/microsoft/amazon SEA) + epic-28 (piano,
    # thetradedesk) + square/strava/woocommerce/google-business-profile/
    # youtube-analytics: 15 -> 26 connectors, 45 -> 88 total report profiles.
    # epic-31: youtube-analytics becomes a MIXED connector -- its new event
    # profile (video_upload, landing=context_events) is the 89th report profile.
    # epic-31.6: meta-ads (campaign_launch), shopify (product_launch) and
    # google-business-profile (social_post) each gain one context_events event
    # profile -> three more MIXED connectors: 125 -> 128 report profiles.
    assert sum(len(item["reports"]) for item in first["connectors"]) == 128
    assert len({item["id"] for item in first["connectors"]}) == len(first["connectors"])
    assert {item["readiness"]["status"] for item in first["connectors"]} == {"validation_required"}


def test_projection_preserves_public_report_facts_and_omits_internals():
    exporter = _load_exporter()
    registry = exporter.build_registry(MODULES_DIR)
    for connector in registry["connectors"]:
        for report in connector["reports"]:
            assert report["displayName"]
            if report["availability"] == "unavailable":
                assert report["reasonCode"]
                assert report["followUp"]

    def keys(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield key.lower()
                yield from keys(child)
        elif isinstance(value, list):
            for child in value:
                yield from keys(child)

    public_keys = set(keys(registry))
    for forbidden in (
        "dispatch",
        "callable",
        "token",
        "connection_ref",
        "project_id",
        "private_notes",
        "secret",
    ):
        assert forbidden not in public_keys


def test_manifest_authored_passed_verification_is_rejected(tmp_path: Path):
    exporter = _load_exporter()
    source_path, manifest = _manifests()[0]
    manifest["public_catalog"]["verification"]["status"] = "passed"
    module_dir = tmp_path / source_path.parent.name
    module_dir.mkdir()
    (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(exporter.RegistryValidationError, match=r"verification\.status"):
        exporter.build_registry(tmp_path)


def test_missing_builtin_visibility_names_module_and_json_path(tmp_path: Path):
    exporter = _load_exporter()
    source_path, manifest = _manifests()[0]
    manifest.pop("public_catalog", None)
    module_dir = tmp_path / source_path.parent.name
    module_dir.mkdir()
    (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        exporter.RegistryValidationError,
        match=rf"{manifest['name']}.*\$\.public_catalog\.visibility",
    ):
        exporter.build_registry(tmp_path)


def test_invalid_manifest_roots_and_encoding_name_module_and_json_path(tmp_path: Path):
    exporter = _load_exporter()

    list_dir = tmp_path / "list-root"
    list_dir.mkdir()
    (list_dir / "manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(
        exporter.RegistryValidationError,
        match=r"list-root: \$: manifest root must be an object",
    ):
        exporter.build_registry(tmp_path)

    (list_dir / "manifest.json").unlink()
    invalid_dir = tmp_path / "invalid-encoding"
    invalid_dir.mkdir()
    (invalid_dir / "manifest.json").write_bytes(b"\xff")
    with pytest.raises(
        exporter.RegistryValidationError,
        match=r"invalid-encoding: \$: manifest must be valid UTF-8",
    ):
        exporter.build_registry(tmp_path)


def test_empty_public_display_name_is_rejected(tmp_path: Path):
    exporter = _load_exporter()
    source_path, manifest = _manifests()[0]
    manifest["display_name"] = ""
    module_dir = tmp_path / source_path.parent.name
    module_dir.mkdir()
    (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(exporter.RegistryValidationError, match=r"\$\.display_name"):
        exporter.build_registry(tmp_path)


def test_capability_error_names_module_and_json_path(tmp_path: Path):
    exporter = _load_exporter()
    source_path, manifest = _manifests()[0]
    manifest["source_capabilities"]["reports"][0]["metrics"].append("unknown_public_field")
    # Derive the report id from the first report so this stays robust to which
    # module sorts first alphabetically (module set grows across epics).
    report_id = manifest["source_capabilities"]["reports"][0]["id"]
    module_dir = tmp_path / source_path.parent.name
    module_dir.mkdir()
    (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(
        exporter.RegistryValidationError,
        match=rf"{manifest['name']}.*\$\.report_profiles\.{report_id}.*legacy_profile_mismatch",
    ):
        exporter.build_registry(tmp_path)


def test_check_cli_fails_closed_without_rewriting(tmp_path: Path):
    env = {**os.environ, "UV_CACHE_DIR": str(ROOT / ".uv-cache")}
    stale = tmp_path / "stale.json"
    stale.write_bytes(b"{}\n")
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check", str(stale)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Registry is stale" in result.stderr
    assert stale.read_bytes() == b"{}\n"

    missing = tmp_path / "missing.json"
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--check", str(missing)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "Registry is missing" in result.stderr
    assert not missing.exists()


def test_web_prebuild_exercises_registry_and_typescript_gates():
    package = json.loads((ROOT / "web" / "package.json").read_text(encoding="utf-8"))
    assert package["scripts"]["prebuild"] == "npm run check:connectors && npm run check"
    assert "--check src/generated/connector-registry.json" in package["scripts"]["check:connectors"]
    assert "tsc --noEmit" in package["scripts"]["check"]
    assert "test:connectors" in package["scripts"]["check"]

    npm = "npm.cmd" if os.name == "nt" else "npm"
    env = {**os.environ, "UV_CACHE_DIR": str(ROOT / ".uv-cache")}
    original = REGISTRY_PATH.read_bytes()
    try:
        fresh = subprocess.run(
            [npm, "run", "prebuild"],
            cwd=ROOT / "web",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert fresh.returncode == 0, fresh.stderr

        REGISTRY_PATH.write_bytes(b"{}\n")
        stale = subprocess.run(
            [npm, "run", "prebuild"],
            cwd=ROOT / "web",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert stale.returncode != 0
        assert "Registry is stale" in stale.stderr

        REGISTRY_PATH.unlink()
        missing = subprocess.run(
            [npm, "run", "prebuild"],
            cwd=ROOT / "web",
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert missing.returncode != 0
        assert "Registry is missing" in missing.stderr
    finally:
        REGISTRY_PATH.write_bytes(original)


def test_public_values_do_not_expose_internal_planning_references():
    exporter = _load_exporter()
    registry = exporter.build_registry(MODULES_DIR)
    story_reference = re.compile(r"^\d+(?:[.-]\d+|-[a-z])", re.IGNORECASE)
    for connector in registry["connectors"]:
        assert "epic " not in connector["displayName"].lower()
        for report in connector["reports"]:
            assert "epic " not in report["displayName"].lower()
            assert "top-n" not in report["displayName"].lower()
            if "followUp" in report:
                assert not story_reference.search(report["followUp"])


def test_committed_registry_matches_fresh_projection():
    exporter = _load_exporter()
    assert REGISTRY_PATH.read_bytes() == exporter.serialize_registry(
        exporter.build_registry(MODULES_DIR)
    )


@pytest.mark.parametrize("cwd", [ROOT, ROOT / "web"])
def test_check_cli_runs_from_repository_root_and_web(cwd: Path):
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--check",
            str(REGISTRY_PATH),
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
