from __future__ import annotations

import json
from pathlib import Path

MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "monday"


def test_manifest_declares_two_explicit_landings_and_blocked_live_gate():
    manifest = json.loads((MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.2"
    assert manifest["auth_type"] == "oauth2"
    assert manifest["provider_api_version"] == "2026-07"
    assert manifest["public_catalog"]["verification"]["status"] == "blocked"
    profiles = {profile["id"]: profile for profile in manifest["report_profiles"]}
    assert set(profiles) == {"board_snapshot", "board_events"}
    assert profiles["board_snapshot"]["landing"] == "fact_daily_kpi"
    assert profiles["board_events"]["landing"] == "context_events"
    assert manifest["canonical_metric_mapping"] == {}
    reports = {report["id"]: report for report in manifest["source_capabilities"]["reports"]}
    assert reports["board_snapshot"]["dispatch"]["callable"] == "pull_board_snapshot"
    assert reports["board_events"]["dispatch"]["callable"] == "pull_board_events"


def test_generated_catalog_has_no_planned_fields(catalog):
    assert catalog["connector"] == "monday"
    assert catalog["api_version"] == "2026-07"
    assert catalog["fields"]
    assert not [field for field in catalog["fields"] if field.get("exposure") == "planned"]
