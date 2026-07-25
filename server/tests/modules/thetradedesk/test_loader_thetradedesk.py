"""Story 28.2 -- the module loads through the REAL core loader.

Epic-26 lesson applied d'office: run scan_and_load_modules on the real modules
directory and assert thetradedesk is LOADED (no module_skipped event), with the
core 26.1 field_compatibility rules attached to the LoadedModule (schema_version
'1' block validated at load, F-10) -- never a module_skipped shortcut.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

_MODULES_DIR = Path(__file__).parents[4] / "server" / "modules"


@pytest.fixture(scope="module")
def loaded_modules():
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


def test_thetradedesk_loads_without_module_skipped(loaded_modules):
    loaded, records = loaded_modules
    names = [m.name for m in loaded]
    assert "thetradedesk" in names

    skipped = [
        r.getMessage()
        for r in records
        if "module_skipped" in r.getMessage() and "thetradedesk" in r.getMessage()
    ]
    assert skipped == [], f"loader skipped thetradedesk: {skipped}"


def test_field_compat_rules_attached_by_the_loader(loaded_modules):
    loaded, _records = loaded_modules
    module = next(m for m in loaded if m.name == "thetradedesk")
    assert module.field_compat_rules is not None
    assert module.field_compat_rules.get("schema_version") == "1"
    kinds = {rule["kind"] for rule in module.field_compat_rules["rules"]}
    assert kinds == {"selectable_set"}
    ids = {rule["id"] for rule in module.field_compat_rules["rules"]}
    assert "selectable_daily_performance" in ids
    assert module.capabilities_available is True


def test_report_pack_loaded(loaded_modules):
    loaded, _records = loaded_modules
    module = next(m for m in loaded if m.name == "thetradedesk")
    assert [r["id"] for r in module.reports] == ["campaign_overview"]
