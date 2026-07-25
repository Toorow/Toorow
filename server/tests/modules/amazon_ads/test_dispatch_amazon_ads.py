"""Story 26.5 -- AI-58 dispatch: all 4 amazon-ads profiles resolve.

get_module_pull_fn(module, profile) resolves the manifest capability report's
declared dispatch.callable off the connector module -- fail-closed for
unknown/unavailable profiles. Pattern: google-ads test_dispatch_google_ads.
"""

from __future__ import annotations

import json
import types
from unittest.mock import patch

import pytest
from core.main import get_module_pull_fn

from .conftest import MODULE_DIR, import_connector

_PROFILE_CALLABLES = {
    "sp_campaigns_daily": "pull_sp_campaigns_daily",
    "sb_campaigns_daily": "pull_sb_campaigns_daily",
    "sd_campaigns_daily": "pull_sd_campaigns_daily",
    "catalog_daily": "pull_catalog_daily",
}


def _loaded():
    mod = import_connector()
    return mod, types.SimpleNamespace(
        name="amazon-ads",
        connector_module=mod,
        manifest=json.loads(
            (MODULE_DIR / "manifest.json").read_text(encoding="utf-8")
        ),
    )


@pytest.mark.parametrize("profile_id,callable_name", sorted(_PROFILE_CALLABLES.items()))
def test_each_profile_dispatches_to_its_declared_callable(profile_id, callable_name):
    mod, loaded = _loaded()
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("amazon-ads", profile_id)
    assert fn is getattr(mod, callable_name), (
        f"{profile_id} must dispatch to {callable_name}"
    )


def test_default_dispatch_is_bare_pull():
    mod, loaded = _loaded()
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("amazon-ads", None)
    assert fn is mod.pull


def test_unknown_profile_fails_closed():
    _mod, loaded = _loaded()
    with patch("core.main._loaded_modules", [loaded]):
        assert get_module_pull_fn("amazon-ads", "dsp_daily") is None


def test_manifest_declares_exactly_four_selectable_reports():
    _mod, loaded = _loaded()
    reports = loaded.manifest["source_capabilities"]["reports"]
    assert {r["id"] for r in reports} == set(_PROFILE_CALLABLES)
    for report in reports:
        assert report["availability"]["status"] == "selectable"
        assert report["dispatch"]["callable"] == _PROFILE_CALLABLES[report["id"]]
    modes = {r["id"]: r["selection_mode"] for r in reports}
    assert modes["catalog_daily"] == "catalog_driven"
    assert all(
        mode == "exact_bundle" for rid, mode in modes.items() if rid != "catalog_daily"
    )


def test_refetch_ladder_declared_3_14_45():
    _mod, loaded = _loaded()
    from core.refetch import get_refetch

    ladder = get_refetch(loaded.manifest)
    assert ladder is not None
    assert (ladder["nightly_days"], ladder["weekly_days"], ladder["monthly_days"]) == (
        3, 14, 45,
    )


def test_topology_declared_region_to_profile():
    _mod, loaded = _loaded()
    from core.account_topology import get_topology

    topology = get_topology(loaded.manifest)
    assert topology is not None
    assert [level["id"] for level in topology["levels"]] == ["region", "profile"]
    assert topology["selection_level"] == "profile"
    assert topology["discovery"]["callable"] == "discover_accounts"


def test_bundle_columns_exist_in_their_report_type_dictionary():
    """Every exact_bundle column is in its reportTypeId's official set."""
    mod, loaded = _loaded()
    by_type = json.loads(
        (MODULE_DIR / "catalog_sources" / "report_type_columns.json").read_text(
            encoding="utf-8"
        )
    )
    for profile_id, (report_type, _gb) in mod._PROFILE_SPECS.items():
        if profile_id == "catalog_daily":
            continue
        dims, metrics = mod._profile_columns(profile_id)
        allowed = set(by_type[report_type])
        missing = sorted(set(dims + metrics) - allowed)
        assert missing == [], f"{profile_id}: not in {report_type} dictionary: {missing}"


def test_golden_transform_matches_expected_facts():
    """Cheap insurance on top of conformance Layer 4."""
    mod, _loaded_ns = _loaded()
    fixtures = MODULE_DIR / "tests" / "fixtures"
    golden = json.loads((fixtures / "golden_pull.json").read_text(encoding="utf-8"))
    expected = json.loads(
        (fixtures / "expected_facts.json").read_text(encoding="utf-8")
    )
    assert mod.transform(golden) == expected
