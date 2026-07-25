"""Story 26.3 -- the REAL loader loads pinterest-ads (google-ads review lesson).

The module (with its core-contract field_compatibility block) must load
through core.loader.scan_and_load_modules with NO module_skipped event, and
the parsed compat rules must be ATTACHED to the LoadedModule (26.1 F-10).
The real module directory is copied into a tmp modules root so the scan only
exercises this module (fast) while every file is the real committed artifact.
"""

from __future__ import annotations

import logging
import shutil

from core.loader import scan_and_load_modules

from .conftest import MODULE_DIR


def _make_modules_root(tmp_path):
    root = tmp_path / "modules"
    root.mkdir()
    shutil.copytree(
        MODULE_DIR,
        root / "pinterest-ads",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "local.duckdb"),
    )
    return root


def test_real_loader_loads_module_without_skip(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="core.loader"):
        loaded = scan_and_load_modules(_make_modules_root(tmp_path))
    assert [m.name for m in loaded] == ["pinterest-ads"]
    assert "module_skipped" not in caplog.text, caplog.text


def test_loader_attaches_validated_field_compat_rules(tmp_path):
    loaded = scan_and_load_modules(_make_modules_root(tmp_path))
    rules = loaded[0].field_compat_rules
    assert rules is not None, (
        "field_compatibility block must OPT IN to the core contract"
        " (schema_version '1') and be attached by the loader"
    )
    assert rules["schema_version"] == "1"
    # Review 26.3 F-3: KEYWORD out of the v1 whitelist (no identity column).
    assert {r["scope"]["level"] for r in rules["rules"]} == {
        "CAMPAIGN", "AD_GROUP", "PIN_PROMOTION", "PRODUCT_GROUP",
    }


def test_loader_module_exposes_all_dispatch_callables(tmp_path):
    loaded = scan_and_load_modules(_make_modules_root(tmp_path))
    mod = loaded[0].connector_module
    for name in (
        "pull", "pull_campaign_daily", "pull_ad_group_daily", "pull_ad_daily",
        "pull_catalog_daily", "discover_accounts", "check_account_access",
        "transform",
    ):
        assert callable(getattr(mod, name, None)), name
