"""Story 28.1 -- the REAL loader loads piano (epic-26 lesson).

The module must load through core.loader.scan_and_load_modules with NO
module_skipped event. Piano ships NO field_compatibility block (its per-site
schema is fully general -> no selectable_set rules), so the loader's compat
hook is a NO-OP: field_compat_rules is None. This is the epic-28 leçon: still
prove the module loads via the REAL core loader.
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
        root / "piano",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "local.duckdb"),
    )
    return root


def test_real_loader_loads_module_without_skip(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="core.loader"):
        loaded = scan_and_load_modules(_make_modules_root(tmp_path))
    assert [m.name for m in loaded] == ["piano"]
    assert "module_skipped" not in caplog.text, caplog.text


def test_loader_field_compat_is_noop_none(tmp_path):
    """No field_compatibility block -> the core compat hook is a no-op (None).
    Piano's schema is fully general (no selectable_set rules)."""
    loaded = scan_and_load_modules(_make_modules_root(tmp_path))
    assert loaded[0].field_compat_rules is None


def test_loader_module_exposes_all_dispatch_callables(tmp_path):
    loaded = scan_and_load_modules(_make_modules_root(tmp_path))
    mod = loaded[0].connector_module
    for name in (
        "pull", "pull_baseline_daily", "pull_audience_daily",
        "pull_sources_daily", "pull_events_daily", "pull_catalog_daily",
        "discover_accounts", "check_account_access", "transform",
    ):
        assert callable(getattr(mod, name, None)), name
