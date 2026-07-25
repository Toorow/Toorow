"""Story 26.5 -- the module loads through the REAL core loader.

Review-google-ads lesson applied d'office: run scan_and_load_modules on the
real modules directory and assert amazon-ads is LOADED (no module_skipped
event), with the core 26.1 field_compatibility rules attached to the
LoadedModule (schema_version '1' block validated at load, F-10).
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

_MODULES_DIR = Path(__file__).parents[4] / "server" / "modules"


@pytest.fixture(scope="module")
def loaded_modules(request):
    from core.loader import scan_and_load_modules

    caplog = logging.getLogger("core.loader")
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture(level=logging.WARNING)
    caplog.addHandler(handler)
    try:
        loaded = scan_and_load_modules(_MODULES_DIR)
    finally:
        caplog.removeHandler(handler)
    return loaded, records


def test_amazon_ads_loads_without_module_skipped(loaded_modules):
    loaded, records = loaded_modules
    names = [m.name for m in loaded]
    assert "amazon-ads" in names

    skipped = [
        r.getMessage()
        for r in records
        if "module_skipped" in r.getMessage() and "amazon-ads" in r.getMessage()
    ]
    assert skipped == [], f"loader skipped amazon-ads: {skipped}"


def test_field_compat_rules_attached_by_the_loader(loaded_modules):
    loaded, _records = loaded_modules
    module = next(m for m in loaded if m.name == "amazon-ads")
    assert module.field_compat_rules is not None
    assert module.field_compat_rules.get("schema_version") == "1"
    kinds = {rule["kind"] for rule in module.field_compat_rules["rules"]}
    assert kinds == {"selectable_set"}
    assert module.capabilities_available is True


def test_report_pack_loaded(loaded_modules):
    loaded, _records = loaded_modules
    module = next(m for m in loaded if m.name == "amazon-ads")
    assert [r["id"] for r in module.reports] == ["campaign_overview"]
