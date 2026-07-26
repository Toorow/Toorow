"""Repository-wide API catalog contract gate (Story 25.1).

For every server/modules/*/manifest.json:
  - If api_catalog.json is present: validate schema + diff against manifest.
  - If api_catalog.json is absent: record as a missing_catalog warning.

Gate modes (CATALOG_GATE_MODE env var, default "fail" since story 25.7 --
all 12 modules carry a catalog):
  - "warn": collect issues and missing catalogs, print a per-module report,
            assert the report covers exactly 12 modules. Test always passes.
  - "fail": assert zero issues across all modules (the CI default).

The gate NEVER silently skips: warn-mode asserts coverage of all 12 modules.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from core.catalog_contract import (
    diff_catalog_manifest,
    load_catalog,
    validate_catalog_schema,
)

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

MODULES_DIR = Path(__file__).parents[2] / "modules"
# Two sessions add modules concurrently: assert a MINIMUM (fail-closed against
# accidental module deletion) instead of an exact count that churns per session.
EXPECTED_MODULE_COUNT_MIN = 12
_GATE_MODE_ENV = "CATALOG_GATE_MODE"
# Story 25.7: flipped from "warn" once all 12 modules landed their catalog.
_GATE_MODE_DEFAULT = "fail"


def _gate_mode() -> str:
    return os.environ.get(_GATE_MODE_ENV, _GATE_MODE_DEFAULT).lower().strip()


def _all_manifests() -> list[tuple[Path, dict]]:
    """Return (manifest_path, manifest_dict) for every builtin module, sorted."""
    return [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in sorted(MODULES_DIR.glob("*/manifest.json"))
    ]


# ---------------------------------------------------------------------------
# Per-module status accumulator (shared across helpers)
# ---------------------------------------------------------------------------


class _ModuleReport:
    """Accumulates gate results for one module."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.catalog_present: bool = False
        self.schema_issues: list[str] = []
        self.diff_issues: list[str] = []
        self.missing: bool = False
        self.counts: dict[str, int] = {}

    def has_issues(self) -> bool:
        return bool(self.schema_issues or self.diff_issues)

    def summary_line(self) -> str:
        if self.missing:
            return f"  [{self.name}] catalog=MISSING"
        counts_str = ", ".join(f"{k}={v}" for k, v in sorted(self.counts.items()))
        status = (
            "OK"
            if not self.has_issues()
            else f"ISSUES({len(self.schema_issues) + len(self.diff_issues)})"
        )
        return f"  [{self.name}] catalog=PRESENT {counts_str} status={status}"


def _build_module_report(module_dir: Path, manifest: dict) -> _ModuleReport:
    """Validate a single module's catalog and return a populated report."""
    module_name = manifest.get("name", module_dir.name)
    report = _ModuleReport(module_name)

    catalog = load_catalog(module_dir)
    if catalog is None:
        report.missing = True
        return report

    report.catalog_present = True

    # Count field exposures
    fields = catalog.get("fields", [])
    report.counts = {
        "exposed": sum(1 for f in fields if f.get("exposure") == "exposed"),
        "excluded": sum(1 for f in fields if f.get("exposure") == "excluded"),
        "planned": sum(1 for f in fields if f.get("exposure") == "planned"),
    }

    # Schema validation
    schema_issues = validate_catalog_schema(catalog)
    report.schema_issues = [f"{i.path}: [{i.code}] {i.message}" for i in schema_issues]

    # Diff against manifest (only when schema is clean)
    if not schema_issues:
        diff_issues = diff_catalog_manifest(catalog, manifest)
        report.diff_issues = [f"{i.path}: [{i.code}] {i.message}" for i in diff_issues]

    return report


def test_api_catalog_conformance_gate() -> None:
    """Validate every module's api_catalog.json against schema and manifest diff.

    In warn mode: collects and prints a full report covering every module
    manifest found. The test always passes in warn mode.

    In fail mode: asserts zero issues across all modules.
    """
    mode = _gate_mode()
    assert mode in {"warn", "fail"}, f"CATALOG_GATE_MODE must be 'warn' or 'fail', got {mode!r}"

    manifests = _all_manifests()
    assert len(manifests) >= EXPECTED_MODULE_COUNT_MIN, (
        f"Expected at least {EXPECTED_MODULE_COUNT_MIN} module manifests, found {len(manifests)}."
    )

    reports: list[_ModuleReport] = []
    for manifest_path, manifest in manifests:
        module_dir = manifest_path.parent
        reports.append(_build_module_report(module_dir, manifest))

    # Always print the per-module summary so CI logs are auditable
    lines = [
        "",
        f"=== API Catalog Gate ({mode.upper()} mode) ===",
    ]
    for rep in reports:
        lines.append(rep.summary_line())

    missing_names = [r.name for r in reports if r.missing]
    issue_names = [r.name for r in reports if r.has_issues()]
    lines.append(
        f"Summary: {len(reports)} modules checked, "
        f"{len(missing_names)} missing catalog, "
        f"{len(issue_names)} with issues."
    )
    if missing_names:
        lines.append("Missing: " + ", ".join(missing_names))
    if issue_names:
        lines.append("Issues:  " + ", ".join(issue_names))
    lines.append("=== end ===")
    print("\n".join(lines))

    # Gate assertion: report must cover all modules (never silent)
    assert len(reports) == len(manifests), (
        "Catalog gate did not produce a report entry for every module."
    )

    if mode == "fail":
        all_issues: list[str] = []
        for rep in reports:
            if rep.missing:
                all_issues.append(f"{rep.name}: missing api_catalog.json")
            all_issues.extend(f"{rep.name}: {line}" for line in rep.schema_issues + rep.diff_issues)
        assert all_issues == [], "CATALOG_GATE_MODE=fail: found catalog issues:\n" + "\n".join(
            all_issues
        )
    # In warn mode the test always passes after printing the report.
