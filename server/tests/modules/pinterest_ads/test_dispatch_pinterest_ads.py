"""Story 26.3 -- AI-58 dispatch: all 4 pinterest-ads profiles resolve (AC).

get_module_pull_fn(module, profile) resolves the manifest capability report's
declared dispatch.callable off the connector module -- fail-closed for
unknown profiles. Pattern: google-ads test_dispatch_google_ads (26.2).
"""

from __future__ import annotations

import json
import types
from unittest.mock import patch

import pytest
from core.main import get_module_pull_fn

from .conftest import MODULE_DIR, import_connector

_PROFILE_CALLABLES = {
    "campaign_daily": "pull_campaign_daily",
    "ad_group_daily": "pull_ad_group_daily",
    "ad_daily": "pull_ad_daily",
    "catalog_daily": "pull_catalog_daily",
}


def _loaded():
    mod = import_connector()
    return mod, types.SimpleNamespace(
        name="pinterest-ads",
        connector_module=mod,
        manifest=json.loads(
            (MODULE_DIR / "manifest.json").read_text(encoding="utf-8")
        ),
    )


@pytest.mark.parametrize("profile_id,callable_name", sorted(_PROFILE_CALLABLES.items()))
def test_each_profile_dispatches_to_its_declared_callable(profile_id, callable_name):
    mod, loaded = _loaded()
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("pinterest-ads", profile_id)
    assert fn is getattr(mod, callable_name), (
        f"{profile_id} must dispatch to {callable_name}"
    )


def test_default_dispatch_is_bare_pull():
    mod, loaded = _loaded()
    with patch("core.main._loaded_modules", [loaded]):
        fn = get_module_pull_fn("pinterest-ads", None)
    assert fn is mod.pull


def test_unknown_profile_fails_closed():
    _mod, loaded = _loaded()
    with patch("core.main._loaded_modules", [loaded]):
        assert get_module_pull_fn("pinterest-ads", "nope_daily") is None


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
        mode == "exact_bundle"
        for rid, mode in modes.items()
        if rid != "catalog_daily"
    )
