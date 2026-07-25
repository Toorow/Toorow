"""Story 28.2 -- AI-58 dispatch: manifest report callables are exposed and
accept the synchronous queue contract; each maps to the right ReportTemplate.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

_MODULE_DIR = Path(__file__).parents[4] / "server" / "modules" / "thetradedesk"

_QUEUE_KWARGS = {
    "connection_id": "",
    "date_from": "",
    "date_to": "",
    "project_id": "",
    "pull_id": "",
}


def _manifest():
    return json.loads((_MODULE_DIR / "manifest.json").read_text(encoding="utf-8"))


def test_every_selectable_report_callable_binds_the_queue_contract(connector):
    """AI-58: each selectable report's dispatch callable exists, is synchronous,
    and accepts the core queue kwargs (loader's _dispatch_issues contract)."""
    manifest = _manifest()
    for report in manifest["source_capabilities"]["reports"]:
        if report["availability"]["status"] != "selectable":
            continue
        name = report["dispatch"]["callable"]
        fn = getattr(connector, name, None)
        assert callable(fn), f"missing dispatch callable {name!r}"
        assert not inspect.iscoroutinefunction(fn), f"{name} must be synchronous"
        inspect.signature(fn).bind(**_QUEUE_KWARGS)


def test_default_pull_is_daily_performance(connector):
    """The profile-less default pull() dispatches the daily_performance
    ReportTemplate (AI-58 default dispatch)."""
    src = inspect.getsource(connector.pull)
    assert "daily_performance" in src


def test_profile_specs_map_to_managed_templates(connector):
    """exact_bundle + catalog_daily both resolve to managed ReportTemplate ids."""
    templates = set(
        json.loads(
            (_MODULE_DIR / "catalog_sources" / "catalog_sources.json").read_text(
                encoding="utf-8"
            )
        )["report_template_compatibility"]["templates"]
    )
    for profile, template in connector._PROFILE_SPECS.items():
        assert template in templates, f"{profile} -> unknown template {template!r}"
