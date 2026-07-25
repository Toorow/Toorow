"""Layer 6: Report-pack definition validation — Story 6.1 (AC5).

For each ``*.json`` in the module's ``reports/`` folder this layer asserts:
  1. it validates against ``server/core/schemas/report.schema.json``;
  2. every metric/dimension exists in the canonical dbt dictionary;
  3. ``get_report("{module}/{report_id}", ...)`` against the module's golden data
     fixture returns a valid canonical envelope (schema_version, meta, data).

Modules with no ``reports/`` folder (e.g. context modules) or an empty one skip
cleanly — a report pack is optional (AC2.7). This test plugs into pytest's module
discovery via the shared ``module_path`` / ``manifest`` fixtures; it needs no
conftest changes beyond adding this file to the layer-order list (kept minimal).
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

pytestmark = pytest.mark.conformance_layer_6

_REPORT_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent / "core" / "schemas" / "report.schema.json"
)


def _load_report_schema() -> dict:
    return json.loads(_REPORT_SCHEMA_PATH.read_text(encoding="utf-8"))


def _report_files(module_path: Path) -> list[Path]:
    reports_dir = module_path / "reports"
    if not reports_dir.is_dir():
        return []
    return sorted(reports_dir.glob("*.json"))


# ---------------------------------------------------------------------------
# Layer 6.1 — schema validation
# ---------------------------------------------------------------------------


def test_reports_validate_against_schema(
    module_path: Path, _layer1_status: list[bool]
) -> None:
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping report layer")

    files = _report_files(module_path)
    if not files:
        pytest.skip("[reports] no reports/*.json in module — report pack is optional")

    schema = _load_report_schema()
    validator = jsonschema.Draft7Validator(schema)
    failures: list[str] = []
    for f in files:
        report = json.loads(f.read_text(encoding="utf-8"))
        for err in validator.iter_errors(report):
            path = "$." + ".".join(str(p) for p in err.absolute_path)
            failures.append(f"[reports] {f.name} {path}: {err.message}")
    if failures:
        pytest.fail("\n".join(failures))


# ---------------------------------------------------------------------------
# Layer 6.2 — metric/dimension dictionary validation
# ---------------------------------------------------------------------------


def test_report_metrics_dimensions_known(
    manifest: dict, module_path: Path, _layer1_status: list[bool]
) -> None:
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping report layer")

    files = _report_files(module_path)
    if not files:
        pytest.skip("[reports] no reports/*.json in module — report pack is optional")

    from core import report_dictionary

    # Register this module's declared breakdown dimensions so connector-specific
    # dims (e.g. campaign_id) validate — mirrors the loader (AD-2).
    report_dictionary.register_dimensions(
        manifest.get("canonical_dimension_mapping", {}).values()
    )

    failures: list[str] = []
    for f in files:
        report = json.loads(f.read_text(encoding="utf-8"))
        for metric in report.get("metrics", []):
            if not report_dictionary.is_known_metric(metric):
                failures.append(f"[reports] {f.name}: unknown metric {metric!r}")
        for dim in report.get("dimensions", []):
            if not report_dictionary.is_known_dimension(dim):
                failures.append(f"[reports] {f.name}: unknown dimension {dim!r}")
    if failures:
        pytest.fail("\n".join(failures))


# ---------------------------------------------------------------------------
# Layer 6.3 — get_report against golden data returns a valid envelope
# ---------------------------------------------------------------------------

_ENVELOPE_SCHEMA_PATH = Path(__file__).parent / "schemas" / "envelope.schema.json"


def _build_golden_duckdb(module_path: Path, module_name: str, db_path: Path) -> bool:
    """Materialise a fact_daily_kpi mart from the module's golden expected_facts.

    Unpivots ``tests/fixtures/expected_facts.json`` (canonical fact rows keyed by
    metric + dimension columns) into fact_daily_kpi rows. Returns False when no
    golden fixture exists (the envelope sub-test then skips gracefully).
    """
    import duckdb

    expected = module_path / "tests" / "fixtures" / "expected_facts.json"
    if not expected.exists():
        return False
    rows = json.loads(expected.read_text(encoding="utf-8"))
    if not rows:
        return False

    con = duckdb.connect(str(db_path))
    try:
        con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
        con.execute(
            """CREATE TABLE main_marts.fact_daily_kpi (
                project_id TEXT, date DATE, connector TEXT, metric TEXT,
                breakdown_dimension TEXT, breakdown_value TEXT, value DOUBLE,
                pull_id TEXT, loaded_at TIMESTAMP)"""
        )
        # Canonical dimension column names present in the fixture. Map them to the
        # report dimension vocabulary (device_category -> device is aliased below).
        _DIM_COL_TO_NAME = {
            "device_category": "device",
            "country": "country",
            "campaign_id": "campaign_id",
            "adset_id": "adset_id",
            "ad_id": "ad_id",
            "campaign_name": "campaign_name",
        }
        metric_keys = {
            k
            for row in rows
            for k in row
            if k not in {"pull_id", "connector", "date", "loaded_at"}
            and k not in _DIM_COL_TO_NAME
            and isinstance(row[k], (int, float))
        }
        for row in rows:
            connector = row.get("connector", module_name)
            pull_id = row.get("pull_id", "pull_golden")
            for metric in metric_keys:
                if metric not in row:
                    continue
                for col, dim_name in _DIM_COL_TO_NAME.items():
                    if col in row:
                        con.execute(
                            "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
                            [
                                "default",
                                row["date"],
                                connector,
                                metric,
                                dim_name,
                                row[col],
                                float(row[metric]),
                                pull_id,
                                "2026-07-01T00:00:00",
                            ],
                        )
    finally:
        con.close()
    return True


def test_get_report_returns_valid_envelope(
    manifest: dict, module_path: Path, _layer1_status: list[bool], tmp_path: Path, monkeypatch
) -> None:
    if not _layer1_status[0]:
        pytest.skip("Layer 1 (manifest) failed — skipping report layer")

    files = _report_files(module_path)
    if not files:
        pytest.skip("[reports] no reports/*.json in module — report pack is optional")

    db_path = tmp_path / "golden.duckdb"
    if not _build_golden_duckdb(module_path, manifest.get("name", ""), db_path):
        pytest.skip("[reports] no golden expected_facts.json fixture for envelope check")

    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_path))

    from core import reports as reports_module

    # Build a minimal loaded-module registry from the report files under test so
    # the renderer is exercised without depending on server/modules discovery.
    class _M:
        def __init__(self, name, manifest, reports):
            self.name = name
            self.manifest = manifest
            self.reports = reports

    module_name = manifest.get("name", "")
    module_reports = [json.loads(f.read_text(encoding="utf-8")) for f in files]
    registry = [_M(module_name, manifest, module_reports)]

    schema = json.loads(_ENVELOPE_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)

    failures: list[str] = []
    for report in module_reports:
        report_id = f"{module_name}/{report['id']}"
        summary, envelope, _uri = reports_module.render_report(
            registry,
            project_id="conformance-test",
            report_id=report_id,
            date_from="2026-07-01",
            date_to="2026-07-31",
        )
        for err in validator.iter_errors(envelope):
            path = "$." + ".".join(str(p) for p in err.absolute_path)
            failures.append(f"[reports] {report_id} envelope {path}: {err.message}")
        # Summary must respect the ≤30-line AD-1 ceiling.
        if len(summary.splitlines()) > 30:
            failures.append(f"[reports] {report_id}: summary exceeds 30 lines")

    if failures:
        pytest.fail("\n".join(failures))
