"""Review 26.2 F-1 -- the google-ads module MUST load through the REAL loader.

The REJECTed revision shipped a field_compatibility block that opted into the
core 26.1 contract vocabulary without conforming to it: with 26.1 merged, the
loader's Step 1b (core.field_compat.load_module_rules) would have skipped the
whole module at startup. THIS is the test that would have caught it: scan the
real server/modules tree with core.loader.scan_and_load_modules and assert
that google-ads (a) is loaded, (b) emitted no module_skipped event, (c) has
its VALIDATED field_compat_rules attached to the LoadedModule (the registry
consumers read them from there).
"""

from __future__ import annotations

import logging

from .conftest import MODULE_DIR

_MODULES_DIR = MODULE_DIR.parent


def _google_ads_skip_events(records) -> list[str]:
    return [
        r.getMessage()
        for r in records
        if "module_skipped" in r.getMessage() and "google-ads" in r.getMessage()
    ]


def test_module_loads_via_real_loader_with_validated_compat_rules(caplog):
    from core.loader import scan_and_load_modules

    with caplog.at_level(logging.WARNING, logger="core.loader"):
        loaded = scan_and_load_modules(_MODULES_DIR)

    assert _google_ads_skip_events(caplog.records) == []

    by_name = {module.name: module for module in loaded}
    assert "google-ads" in by_name, "google-ads must survive the real loader scan"
    module = by_name["google-ads"]
    assert module.capabilities_available is True
    assert hasattr(module.connector_module, "mcp_app")

    # Step 1b attached the VALIDATED core-contract rules (never None: the
    # module opts in via schema_version, and a malformed block would have
    # been a loud skip caught above).
    rules = module.field_compat_rules
    assert rules is not None
    assert rules["schema_version"] == "1"
    assert {r["kind"] for r in rules["rules"]} == {"selectable_set"}
    assert {r["scope"]["resource"] for r in rules["rules"]} == {
        "campaign", "ad_group", "ad_group_ad", "keyword_view", "search_term_view",
    }
