"""Tests for the module loader (Story 1.3 — T2.4).

Covers:
  * Happy path: valid module is loaded successfully.
  * Invalid manifest (missing required field): module is skipped with error log.
  * Missing manifest.json: module folder is skipped with warning log.
  * Non-directory entries in modules/ are silently ignored.
  * Missing mcp_app attribute: module is skipped.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from core.loader import LoadedModule, scan_and_load_modules

# ---------------------------------------------------------------------------
# Fixtures / helpers
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
                "metrics": ["metric_a"],
                "dimensions": ["dim_a"],
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


def _make_connector_py(with_mcp_app: bool = True) -> str:
    if with_mcp_app:
        return textwrap.dedent("""\
            from fastmcp import FastMCP
            mcp_app = FastMCP("test-module")

            @mcp_app.tool()
            def stub_tool() -> dict:
                return {"ok": True}
        """)
    return textwrap.dedent("""\
        # connector without mcp_app
        def some_function():
            pass
    """)


def _scaffold_module(
    modules_dir: Path,
    folder_name: str,
    manifest: dict | None = None,
    connector_content: str | None = None,
    write_manifest: bool = True,
    write_connector: bool = True,
) -> Path:
    """Create a module folder with optional manifest.json and connector.py."""
    module_dir = modules_dir / folder_name
    module_dir.mkdir(parents=True)

    if write_manifest and manifest is not None:
        (module_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    if write_connector and connector_content is not None:
        (module_dir / "connector.py").write_text(connector_content, encoding="utf-8")

    return module_dir


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_module_is_loaded(tmp_path: Path):
    """T2.4 — happy path: valid module loaded."""
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    manifest = _make_valid_manifest("test-module")
    connector = _make_connector_py(with_mcp_app=True)
    _scaffold_module(modules_dir, "test-module", manifest=manifest, connector_content=connector)

    result = scan_and_load_modules(modules_dir)

    assert len(result) == 1
    loaded = result[0]
    assert isinstance(loaded, LoadedModule)
    assert loaded.name == "test-module"
    assert loaded.manifest["schema_version"] == "1"
    assert hasattr(loaded.connector_module, "mcp_app")


# ---------------------------------------------------------------------------
# Invalid manifest — skipped with error log
# ---------------------------------------------------------------------------


def test_invalid_manifest_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """T2.4 — invalid manifest (missing 'name') → module skipped, server continues."""
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    manifest = _make_valid_manifest("test-module")
    del manifest["name"]  # intentionally invalid
    connector = _make_connector_py()
    _scaffold_module(modules_dir, "broken-module", manifest=manifest, connector_content=connector)

    with caplog.at_level("ERROR"):
        result = scan_and_load_modules(modules_dir)

    assert result == [], "Expected no loaded modules"
    assert "module_skipped" in caplog.text, "Expected module_skipped in log"
    assert "broken-module" in caplog.text, "Expected module name in log"


# ---------------------------------------------------------------------------
# Missing manifest.json — skipped with warning log
# ---------------------------------------------------------------------------


def test_missing_manifest_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """T2.4 — folder without manifest.json → skipped with warning."""
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    module_dir = modules_dir / "no-manifest"
    module_dir.mkdir()
    # No manifest.json — only a connector.py
    (module_dir / "connector.py").write_text(_make_connector_py(), encoding="utf-8")

    with caplog.at_level("WARNING"):
        result = scan_and_load_modules(modules_dir)

    assert result == []
    assert "module_skipped" in caplog.text


# ---------------------------------------------------------------------------
# Non-directory entries ignored
# ---------------------------------------------------------------------------


def test_non_directory_entries_ignored(tmp_path: Path):
    """T2.4 — files (e.g. .gitkeep) in modules/ are silently ignored."""
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    # Create a .gitkeep file (non-directory)
    (modules_dir / ".gitkeep").write_text("", encoding="utf-8")
    # Also a valid module to confirm we get exactly 1 result
    manifest = _make_valid_manifest("real-module")
    manifest["widget_ref"] = "ui://real-module/default"
    connector = _make_connector_py()
    _scaffold_module(
        modules_dir,
        "real-module",
        manifest=manifest,
        connector_content=connector,
    )

    result = scan_and_load_modules(modules_dir)
    assert len(result) == 1
    assert result[0].name == "real-module"


# ---------------------------------------------------------------------------
# Missing mcp_app attribute — skipped
# ---------------------------------------------------------------------------


def test_missing_mcp_app_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """T2.4 — connector.py without mcp_app → module skipped with error."""
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    manifest = _make_valid_manifest("no-app-module")
    manifest["widget_ref"] = "ui://no-app-module/default"
    connector = _make_connector_py(with_mcp_app=False)
    _scaffold_module(modules_dir, "no-app-module", manifest=manifest, connector_content=connector)

    with caplog.at_level("ERROR"):
        result = scan_and_load_modules(modules_dir)

    assert result == []
    assert "module_skipped" in caplog.text


# ---------------------------------------------------------------------------
# Missing modules/ directory itself
# ---------------------------------------------------------------------------


def test_missing_modules_dir_returns_empty(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    """scan_and_load_modules returns [] gracefully when dir doesn't exist."""
    missing_dir = tmp_path / "nonexistent_modules"

    with caplog.at_level("WARNING"):
        result = scan_and_load_modules(missing_dir)

    assert result == []


# ---------------------------------------------------------------------------
# Multiple modules loaded in sorted order
# ---------------------------------------------------------------------------


def test_multiple_modules_loaded(tmp_path: Path):
    """Two valid modules → both returned."""
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()

    for mod_name in ("alpha-module", "beta-module"):
        manifest = _make_valid_manifest(mod_name)
        manifest["widget_ref"] = f"ui://{mod_name}/default"
        connector = _make_connector_py()
        _scaffold_module(modules_dir, mod_name, manifest=manifest, connector_content=connector)

    result = scan_and_load_modules(modules_dir)
    assert len(result) == 2
    names = {r.name for r in result}
    assert names == {"alpha-module", "beta-module"}


# ---------------------------------------------------------------------------
# Story 12.1 — capability-aware discovery and deterministic legacy policy
# ---------------------------------------------------------------------------


def _v12_manifest(name: str = "test-module") -> dict:
    manifest = _make_valid_manifest(name)
    manifest["schema_version"] = "1.2"
    manifest["report_profiles"][0].update(
        {
            "id": "daily",
            "metrics": ["metric_a"],
            "dimensions": ["dim_a"],
        }
    )
    manifest["source_capabilities"] = {
        "contract_version": "1",
        "field_discovery": {"mode": "static", "allowed_targets": []},
        "fields": [
            {
                "field_id": "metric_a",
                "source_field": "metricA",
                "kind": "metric",
                "physical_type": "decimal",
                "description": "A metric.",
                "semantic_hints": ["measure"],
                "canonical_target": None,
                "aggregation": "sum",
                "non_additive": False,
            },
            {
                "field_id": "dim_a",
                "source_field": "dimensionA",
                "kind": "dimension",
                "physical_type": "string",
                "description": "A dimension.",
                "semantic_hints": ["dimension"],
                "canonical_target": None,
                "aggregation": "none",
                "non_additive": False,
            },
        ],
        "reports": [
            {
                "id": "daily",
                "selection_mode": "exact_bundle",
                "availability": {"status": "selectable"},
                "dispatch": {"callable": "pull_daily"},
                "metrics": ["metric_a"],
                "dimensions": ["dim_a"],
                "supported_grains": [["dim_a"]],
                "compatibility": [],
                "filters": [],
                "pagination": {
                    "mode": "none",
                    "completeness": "complete",
                    "max_pages": None,
                    "row_limit": None,
                    "truncation_signal": "none",
                },
                "quota_cost": {"read_points": 1, "unit": "request"},
                "incremental": {"mode": "full_refresh", "cursor_field": None},
                "cadence": {
                    "minimum_interval_minutes": 1440,
                    "supported_modes": ["manual", "daily"],
                },
            }
        ],
    }
    return manifest


def _v12_connector() -> str:
    return _make_connector_py() + textwrap.dedent("""

        def pull_daily(**kwargs):
            return {"profile": "daily"}
    """)


def test_v12_semantic_failure_is_actionable_and_other_modules_continue(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    invalid = _v12_manifest("invalid-module")
    invalid["source_capabilities"]["reports"][0]["metrics"] = ["dim_a"]
    _scaffold_module(
        modules_dir,
        "invalid-module",
        manifest=invalid,
        connector_content=_v12_connector(),
    )
    valid = _v12_manifest("valid-module")
    _scaffold_module(
        modules_dir,
        "valid-module",
        manifest=valid,
        connector_content=_v12_connector(),
    )

    with caplog.at_level("ERROR"):
        loaded = scan_and_load_modules(modules_dir)

    assert [module.name for module in loaded] == ["valid-module"]
    records = [
        json.loads(record.message) for record in caplog.records if record.message.startswith("{")
    ]
    skipped = next(item for item in records if item.get("event") == "module_skipped")
    assert skipped["errors"][0].keys() >= {"code", "path", "message", "suggested_repair"}
    assert {error["code"] for error in skipped["errors"]} >= {
        "field_kind_mismatch",
        "legacy_profile_mismatch",
    }


def test_legacy_module_loads_but_capabilities_are_unavailable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    _scaffold_module(
        modules_dir,
        "legacy-module",
        manifest=_make_valid_manifest("legacy-module"),
        connector_content=_make_connector_py(),
    )

    with caplog.at_level("WARNING"):
        loaded = scan_and_load_modules(modules_dir)

    assert len(loaded) == 1
    assert loaded[0].capabilities_available is False
    assert "legacy_capabilities_unavailable" in caplog.text


def test_v12_missing_declared_dispatch_callable_is_skipped(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    _scaffold_module(
        modules_dir,
        "missing-dispatch",
        manifest=_v12_manifest("missing-dispatch"),
        connector_content=_make_connector_py(),
    )

    with caplog.at_level("ERROR"):
        loaded = scan_and_load_modules(modules_dir)

    assert loaded == []
    assert "dispatch_callable_missing" in caplog.text


def test_v12_valid_dispatch_is_capability_available(tmp_path: Path):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    _scaffold_module(
        modules_dir,
        "valid-module",
        manifest=_v12_manifest("valid-module"),
        connector_content=_v12_connector(),
    )

    loaded = scan_and_load_modules(modules_dir)

    assert len(loaded) == 1
    assert loaded[0].capabilities_available is True

@pytest.mark.parametrize(
    ("connector_body", "error_code"),
    [
        (
            """
def pull_daily(
    connection_id, date_from, date_to, project_id, pull_id, required_config
):
    return {}
""",
            "dispatch_signature_incompatible",
        ),
        (
            """
async def pull_daily(connection_id, date_from, date_to, project_id, pull_id):
    return {}
""",
            "dispatch_callable_async",
        ),
    ],
)
def test_v12_dispatch_must_match_synchronous_queue_contract(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    connector_body: str,
    error_code: str,
):
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    connector = _make_connector_py() + textwrap.dedent(connector_body)
    _scaffold_module(
        modules_dir,
        "invalid-dispatch",
        manifest=_v12_manifest("invalid-dispatch"),
        connector_content=connector,
    )

    with caplog.at_level("ERROR"):
        loaded = scan_and_load_modules(modules_dir)

    assert loaded == []
    assert error_code in caplog.text
