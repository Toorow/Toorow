"""Conformance suite configuration — Story 1.8 (T1.1, T1.2).

Provides the ``module_path`` and ``manifest`` session-scoped fixtures
consumed by all four conformance layers.  The ``--module-path`` CLI option
is the only coupling point between the suite and a specific module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# CLI option registration
# ---------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--module-path",
        action="store",
        default=None,
        help="Path to the module folder to validate (e.g. server/modules/google-analytics/)",
    )


# ---------------------------------------------------------------------------
# Enforce layer execution order: manifest → envelope → bundle → golden_pull
# ---------------------------------------------------------------------------

_LAYER_ORDER = [
    "test_manifest",
    "test_envelope",
    "test_bundle",
    "test_golden_pull",
    "test_context_events",  # Layer 5 (Story 4.5): context module golden fixture
    "test_reports",  # Layer 6 (Story 6.1): report-pack definition validation
]


def pytest_collection_modifyitems(items: list) -> None:  # type: ignore[type-arg]
    """Sort conformance layer tests so layer 1 always runs before layers 2–4."""
    def _layer_key(item: pytest.Item) -> tuple[int, str]:
        module_name = item.module.__name__.split(".")[-1] if hasattr(item, "module") else ""
        try:
            idx = _LAYER_ORDER.index(module_name)
        except ValueError:
            idx = len(_LAYER_ORDER)
        return (idx, item.nodeid)

    conformance_items = [i for i in items if "conformance" in i.nodeid]
    other_items = [i for i in items if "conformance" not in i.nodeid]

    conformance_items.sort(key=_layer_key)
    items[:] = other_items + conformance_items


# ---------------------------------------------------------------------------
# Session-scoped fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def module_path(request: pytest.FixtureRequest) -> Path:
    """Resolved path to the module folder under test."""
    raw = request.config.getoption("--module-path")
    if raw is None:
        pytest.skip("No --module-path provided; skipping conformance suite")
    p = Path(raw).resolve()
    if not p.is_dir():
        pytest.fail(f"--module-path does not exist: {p}")
    return p


@pytest.fixture(scope="session")
def manifest(module_path: Path) -> dict:
    """Parsed manifest.json for the module under test."""
    manifest_path = module_path / "manifest.json"
    if not manifest_path.exists():
        pytest.fail(f"[manifest] manifest.json not found at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Layer-1 pass/fail flag — shared across layers via a mutable container
# ---------------------------------------------------------------------------
# We use a list[bool] (mutable singleton) so test_manifest.py can flip the
# flag inside the session-scoped fixture without needing monkeypatching.


@pytest.fixture(scope="session")
def _layer1_status() -> list[bool]:
    """Mutable container: [True] = layer 1 passed, [False] = layer 1 failed."""
    return [True]  # optimistic default; test_manifest.py flips to False on failure


# ---------------------------------------------------------------------------
# Story 6.3 — additive fixture: multi-connector golden DuckDB extension.
#
# For the organic_crossview report (gsc module), Layer 6 needs BOTH gsc and
# google-analytics rows in the golden DuckDB mart, plus a semantic view for
# the non-additive average_position routing (AD-4).
#
# This fixture patches test_reports._build_golden_duckdb to post-process the
# freshly created golden DuckDB and add:
#   1. main_marts.semantic_avg_position view (from fact_daily_kpi gsc rows)
#   2. google-analytics connector rows for organic_crossview cross-source join
#
# Additive only — no existing fixture or test logic is changed.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _extend_golden_duckdb_for_gsc(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Patch _build_golden_duckdb to add semantic view + GA4 rows for gsc module.

    Runs for every test in the conformance suite but only applies the patch
    when --module-path points at the gsc module (safe no-op otherwise).
    """
    if request.node.path.name in {
        "test_all_module_capabilities.py",
        "test_public_connector_registry.py",
        "test_api_catalog.py",
    }:
        return

    module_path = request.getfixturevalue("module_path")
    if module_path.name != "gsc":
        return

    try:
        from tests.conformance import test_reports as _tr  # noqa: PLC0415
    except ImportError:
        return

    original_build = _tr._build_golden_duckdb

    def _patched_build_golden_duckdb(
        module_path_arg: Path, module_name: str, db_path: Path
    ) -> bool:
        result = original_build(module_path_arg, module_name, db_path)
        if not result:
            return result
        try:
            import duckdb  # noqa: PLC0415

            con = duckdb.connect(str(db_path))
            try:
                # 1. Create semantic_avg_position view (AD-4: non-additive routing).
                #    View computes impression-weighted average position from fact_daily_kpi.
                #    Mirrors dbt/models/marts/semantic_avg_position.sql. The name is the
                #    canonical one after the 19.4 debt resolution (average_position lives
                #    in semantic_avg_position, NOT the pre-19.4 semantic_average_position
                #    which warehouse.py treats as inexistent).
                con.execute(
                    """
                    CREATE VIEW IF NOT EXISTS main_marts.semantic_avg_position AS
                    SELECT
                        project_id,
                        date,
                        connector,
                        breakdown_dimension,
                        breakdown_value,
                        CASE
                            WHEN SUM(
                                CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                            ) > 0
                            THEN SUM(
                                CASE WHEN metric = 'average_position' THEN value ELSE 0 END
                                * CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                            ) / NULLIF(SUM(
                                CASE WHEN metric = 'impressions' THEN value ELSE 0 END
                            ), 0)
                            ELSE AVG(CASE WHEN metric = 'average_position' THEN value END)
                        END AS average_position,
                        MAX(pull_id) AS pull_id,
                        MAX(loaded_at) AS loaded_at
                    FROM main_marts.fact_daily_kpi
                    WHERE metric IN ('average_position', 'impressions')
                      AND connector = 'gsc'
                    GROUP BY project_id, date, connector, breakdown_dimension, breakdown_value
                    """
                )
                # 2. Seed google-analytics sessions rows for organic_crossview.
                #    Multi-connector fixture: gsc + google-analytics on same dates (Story 6.3 AC7).
                sessions_rows = [
                    ("conformance-test", "2026-07-01", "google-analytics", "sessions",
                     "date", "2026-07-01", 320.0, "pull_ga4_golden", "2026-07-01T00:00:00"),
                    ("conformance-test", "2026-07-02", "google-analytics", "sessions",
                     "date", "2026-07-02", 295.0, "pull_ga4_golden", "2026-07-01T00:00:00"),
                    ("conformance-test", "2026-07-03", "google-analytics", "sessions",
                     "date", "2026-07-03", 310.0, "pull_ga4_golden", "2026-07-01T00:00:00"),
                ]
                for row in sessions_rows:
                    con.execute(
                        "INSERT INTO main_marts.fact_daily_kpi VALUES (?,?,?,?,?,?,?,?,?)",
                        list(row),
                    )
            finally:
                con.close()
        except Exception:
            pass  # best-effort; the main fixture succeeded
        return result

    monkeypatch.setattr(_tr, "_build_golden_duckdb", _patched_build_golden_duckdb)
