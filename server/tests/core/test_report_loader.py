"""Tests for report-definition discovery in the module loader (Story 6.1, AC2, T2.5).

Covers:
  * A valid report JSON in a module's reports/ folder is loaded onto
    LoadedModule.reports.
  * An invalid report (schema violation — missing id) is SKIPPED with a WARNING;
    the module still loads.
  * A report referencing an unknown metric is SKIPPED with a WARNING.
  * A module with an empty reports/ folder loads normally (reports == []).
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from core.loader import scan_and_load_modules

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_valid_manifest(name: str = "test-module") -> dict:
    return {
        "schema_version": "1",
        "name": name,
        "display_name": "Test Module",
        "auth_type": "none",
        "report_profiles": [
            {
                "id": "default",
                "display_name": "Default Report",
                "metrics": ["sessions"],
                "dimensions": ["date"],
                "extraction_capabilities": {
                    "row_limit": None,
                    "filters_supported": False,
                    "realtime": False,
                },
            }
        ],
        "canonical_metric_mapping": {},
        "canonical_dimension_mapping": {},
        "widget_ref": "ui://test-module/default",
    }


_TOOROW_PY = textwrap.dedent("""\
    from fastmcp import FastMCP
    mcp_app = FastMCP("test-module")

    @mcp_app.tool()
    def stub_tool() -> dict:
        return {"ok": True}
""")


def _scaffold_module(modules_dir: Path, folder_name: str) -> Path:
    module_dir = modules_dir / folder_name
    (module_dir / "reports").mkdir(parents=True)
    (module_dir / "manifest.json").write_text(
        json.dumps(_make_valid_manifest(folder_name)), encoding="utf-8"
    )
    (module_dir / "connector.py").write_text(_TOOROW_PY, encoding="utf-8")
    return module_dir


_VALID_REPORT = {
    "id": "overview_daily",
    "display_name": "Vue d'ensemble",
    "metrics": ["sessions", "conversions"],
    "dimensions": ["date", "device"],
    "layout": {"chart_type": "line", "order_by": "date"},
    "narrative_prompt": "Analyse les tendances.",
    "date_window": {"default_days": 30},
}


def _write_report(module_dir: Path, filename: str, report: dict) -> None:
    (module_dir / "reports" / filename).write_text(json.dumps(report), encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_report_loaded(tmp_path: Path) -> None:
    module_dir = _scaffold_module(tmp_path, "mod-valid")
    _write_report(module_dir, "overview_daily.json", _VALID_REPORT)

    loaded = scan_and_load_modules(tmp_path)
    assert len(loaded) == 1
    assert len(loaded[0].reports) == 1
    assert loaded[0].reports[0]["id"] == "overview_daily"


def test_invalid_report_schema_skipped(tmp_path: Path, caplog) -> None:
    module_dir = _scaffold_module(tmp_path, "mod-badschema")
    bad = dict(_VALID_REPORT)
    del bad["id"]  # missing required "id" — schema violation
    _write_report(module_dir, "bad.json", bad)

    with caplog.at_level("WARNING"):
        loaded = scan_and_load_modules(tmp_path)

    # Module still loads, report skipped.
    assert len(loaded) == 1
    assert loaded[0].reports == []
    assert any("report_skipped" in r.message for r in caplog.records)


def test_unknown_metric_skipped(tmp_path: Path, caplog) -> None:
    module_dir = _scaffold_module(tmp_path, "mod-badmetric")
    bad = dict(_VALID_REPORT)
    bad["metrics"] = ["nonexistent_metric"]
    _write_report(module_dir, "bad.json", bad)

    with caplog.at_level("WARNING"):
        loaded = scan_and_load_modules(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].reports == []
    assert any(
        "report_skipped" in r.message and "nonexistent_metric" in r.message
        for r in caplog.records
    )


def test_unknown_dimension_skipped(tmp_path: Path, caplog) -> None:
    module_dir = _scaffold_module(tmp_path, "mod-baddim")
    bad = dict(_VALID_REPORT)
    bad["dimensions"] = ["date", "not_a_real_dimension"]
    _write_report(module_dir, "bad.json", bad)

    with caplog.at_level("WARNING"):
        loaded = scan_and_load_modules(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].reports == []


def test_empty_reports_folder_ok(tmp_path: Path) -> None:
    _scaffold_module(tmp_path, "mod-empty")  # reports/ exists but is empty
    loaded = scan_and_load_modules(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].reports == []


def test_no_reports_folder_ok(tmp_path: Path) -> None:
    module_dir = _scaffold_module(tmp_path, "mod-noreports")
    # Remove the reports/ folder entirely.
    (module_dir / "reports").rmdir()
    loaded = scan_and_load_modules(tmp_path)
    assert len(loaded) == 1
    assert loaded[0].reports == []
