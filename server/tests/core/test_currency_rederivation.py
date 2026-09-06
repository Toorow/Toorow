"""Currency re-derivation proof test — Story 39.10 / Story 4.2.

Proves that changing the loaded fx_rates relation and re-running dbt produces
updated converted cost values at READ from unchanged source values.
Validates the FX-AT-READ invariant (Amendment 2026-07-23 [[fx-locus-read-not-staging]]):
"conversion happens once, at READ. Staging keeps the source-currency amount."

Scenario A: fx_rate = 0.92 USD→EUR → spend=100.0 → converted cost = 92.0
Scenario B: fx_rate = 0.85 USD→EUR → spend=100.0 → converted cost = 85.0

The test seeds the governed 0.85 fixture once, changes only its disposable
DuckDB copy, then re-runs dbt. It never mutates a committed seed under xdist.
Source values (spend/cost in staging) are unchanged (100.0); only the read conversion changes.

Guard: skipped when dbt is not importable (same pattern as test_seed_to_mart_loop.py).
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from decimal import Decimal
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).parents[3]
SEEDS_DIR = REPO_ROOT / "server" / "modules" / "google-analytics" / "seeds"
META_SEEDS_DIR = REPO_ROOT / "server" / "modules" / "meta-ads" / "seeds"
DBT_DIR = REPO_ROOT / "dbt"

# ---------------------------------------------------------------------------
# Skip the whole module when dbt is not installed
# ---------------------------------------------------------------------------
try:
    import dbt.cli.main  # noqa: F401

    _DBT_AVAILABLE = True
except ImportError:
    _DBT_AVAILABLE = False

pytestmark = [
    # Pilote dbt en sous-processus : sans plafond propre il n'echoue pas, il
    # ARRETE LA SESSION (voir tests/conformance/test_dbt_tests_declare_their_time.py).
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        not _DBT_AVAILABLE,
        reason="dbt not found — skipping currency re-derivation integration test",
    ),
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _dbt_run(profiles_dir: Path, *extra_args: str) -> None:
    """Run a dbt command against the test DuckDB profile."""
    target_dir = profiles_dir.parent / "target"
    cmd = [
        sys.executable,
        "-m",
        "dbt.cli.main",
        *extra_args,
        "--project-dir",
        str(DBT_DIR),
        "--profiles-dir",
        str(profiles_dir),
        "--target-path",
        str(target_dir),
    ]
    result = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"dbt {' '.join(extra_args)} failed:\n{result.stdout}\n{result.stderr}")


def _set_loaded_fx_rate(duckdb_path: str, usd_rate: float) -> None:
    """Change only the disposable warehouse copy of the governed seed."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(
            "UPDATE fx_rates SET rate = ? WHERE from_currency = 'USD' "
            "AND to_currency = 'EUR'",
            [usd_rate],
        )
    finally:
        con.close()


def _query_cost(duckdb_path: str) -> list[Decimal | None]:
    """Query read-converted cost from stg_meta_ads_daily in DuckDB.

    Story 39.10: staging emits cost (source amount) + fx_rate (provenance).

    Story 48.3: the read rule is no longer ``cost * COALESCE(fx_rate, 1.0)``. That
    COALESCE converted an unresolvable row AT PARITY and let it into a total, which
    is the Currency & FX criterion "a cross-currency total succeeds without
    explicit FX evidence". This helper mirrors the repaired
    ``fx_convert_at_read`` macro exactly -- NULL propagates and the row is excluded
    from the total rather than added as though a dollar were a euro. Restating the
    macro's rule in a second place is only safe while the two say the same thing,
    and this comment exists so the next reader checks.
    """
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        query = (
            "SELECT CASE WHEN fx_rate IS NULL THEN NULL "
            "ELSE CAST(cost AS DECIMAL(38, 9)) * CAST(fx_rate AS DECIMAL(38, 9)) END "
            "FROM main_staging.stg_meta_ads_daily ORDER BY 1"
        )
        rows = con.execute(query).fetchall()
        # Decimal, not float: Story 48.3 made the read layer exact, and coercing
        # here would throw away the property the change exists to give.
        return [row[0] for row in rows]
    finally:
        con.close()


def _write_profiles_yml(profiles_dir: Path, duckdb_path: str) -> None:
    """Write a temporary dbt profiles.yml for the test DuckDB."""
    profiles_dir.mkdir(parents=True, exist_ok=True)
    content = f"""connector:
  target: local
  outputs:
    local:
      type: duckdb
      path: {duckdb_path!r}
      threads: 1
"""
    (profiles_dir / "profiles.yml").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Test: re-derivation proof
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def rederivation_db(tmp_path_factory):
    """Set up a seeded DuckDB + dbt profile for re-derivation tests.

    Seeds a small set of Meta rows with spend=100.0 USD (one row), then yields
    the DuckDB path and profiles dir so scenario A/B can re-run dbt and re-query.
    """
    tmp_path = tmp_path_factory.mktemp("rederivation")
    duckdb_path = str(tmp_path / "test.duckdb")
    profiles_dir = tmp_path / "profiles"

    meta_loader_mod = _import_module("load_meta_seed", META_SEEDS_DIR / "load_meta_seed.py")

    rows = [
        {
            "date": "2026-07-01",
            "campaign_id": "camp_rederiv_001",
            "campaign_name": "Rederiv Test",
            "adset_id": "adset_001",
            "adset_name": "Test Adset",
            "ad_id": "ad_001",
            "creative_id": "cr_001",
            "spend": 100.0,
            "impressions": 1000,
            "clicks": 50,
            "conversions": 5,
            "cost_source_currency": "USD",
            "project_id": "default",
        }
    ]

    ga4_loader = _import_module("load_seed", SEEDS_DIR / "load_seed.py")
    ga4_gen = _import_module("generate_seed", SEEDS_DIR / "generate_seed.py")
    ga4_rows = ga4_gen.generate_rows()
    ga4_loader.load_duckdb(ga4_rows, "pull_ga4_rederiv", "2026-07-01T00:00:00Z", duckdb_path)

    meta_loader_mod.load_duckdb(rows, "pull_rederiv_001", "2026-07-01T00:00:00Z", duckdb_path)

    import duckdb as _duckdb  # noqa: PLC0415

    _mirror_conn = _duckdb.connect(duckdb_path)
    _mirror_conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    _mirror_conn.execute(
        """
        CREATE TABLE IF NOT EXISTS mirror.project_preferences (
            project_id VARCHAR,
            canonical_currency VARCHAR,
            reporting_timezone VARCHAR,
            verification_source_type VARCHAR,
            verification_source_id VARCHAR,
            lead_event_name VARCHAR,
            created_at TIMESTAMP,
            updated_at TIMESTAMP
        )
        """
    )
    _mirror_conn.execute(
        """
        INSERT INTO mirror.project_preferences
            (project_id, canonical_currency, reporting_timezone,
             verification_source_type, verification_source_id, lead_event_name,
             created_at, updated_at)
        VALUES ('default', 'EUR', 'Europe/Paris', NULL, NULL, NULL, now(), now())
        """
    )
    _mirror_conn.execute(
        """
        -- Story 67.20: the staging models join the GOVERNED projection now
        -- (migration 282). Empty here, so the LEFT JOIN matches nothing and the
        -- COALESCE falls back to the raw source currency -- which is exactly the
        -- state this fixture wants.
        CREATE TABLE IF NOT EXISTS mirror.fx_source_currency_bindings (
            project_id VARCHAR,
            target_field VARCHAR,
            source_module VARCHAR,
            resolved_source_currency VARCHAR,
            decided_by VARCHAR,
            decided_at TIMESTAMP,
            note VARCHAR,
            rule_set_id VARCHAR,
            rule_set_version_id VARCHAR
        )
        """
    )
    _mirror_conn.close()

    _write_profiles_yml(profiles_dir, duckdb_path)

    yield {"duckdb_path": duckdb_path, "profiles_dir": profiles_dir}


def test_scenario_a_rate_092(rederivation_db, tmp_path):
    """Scenario A: USD→EUR rate=0.92, spend=100 → cost should be 92.0 at READ."""
    profiles_dir = rederivation_db["profiles_dir"]
    duckdb_path = rederivation_db["duckdb_path"]

    _dbt_run(profiles_dir, "seed", "--select", "fx_rates")
    _set_loaded_fx_rate(duckdb_path, 0.92)
    _dbt_run(profiles_dir, "seed", "--select", "dim_country")
    _dbt_run(profiles_dir, "seed", "--select", "dim_device")
    _dbt_run(profiles_dir, "seed", "--select", "dim_metric")
    _dbt_run(profiles_dir, "seed", "--select", "metric_source_priority")
    _dbt_run(profiles_dir, "seed", "--select", "project_preferences")
    _dbt_run(profiles_dir, "run", "--select", "dim_project", "stg_meta_ads_daily")

    costs = _query_cost(duckdb_path)
    assert Decimal("92.000000000") in costs, (
        f"Scenario A: expected read cost exactly 92 (spend=100 * rate=0.92), got {costs}"
    )


def test_scenario_b_rate_085(rederivation_db):
    """Scenario B: USD→EUR rate=0.85, spend=100 → cost should be 85.0 at READ."""
    profiles_dir = rederivation_db["profiles_dir"]
    duckdb_path = rederivation_db["duckdb_path"]

    _set_loaded_fx_rate(duckdb_path, 0.85)
    _dbt_run(profiles_dir, "run", "--select", "dim_project", "stg_meta_ads_daily")

    costs = _query_cost(duckdb_path)
    assert Decimal("85.000000000") in costs, (
        f"Scenario B: expected read cost exactly 85 (spend=100 * rate=0.85), got {costs}"
    )


def test_source_values_unchanged_between_scenarios(rederivation_db):
    """Verify cost_source_value and staging cost are unchanged across scenario
    A→B (only READ value changes).

    This is the AD-6 / Story 39.10 proof: staging holds immutable source amounts (spend=100.0);
    only the read-time conversion re-derives.
    """
    import duckdb  # noqa: PLC0415

    duckdb_path = rederivation_db["duckdb_path"]
    con = duckdb.connect(duckdb_path)
    try:
        rows = con.execute(
            "SELECT cost_source_value, cost_source_currency, cost "
            "FROM main_staging.stg_meta_ads_daily"
        ).fetchall()
    finally:
        con.close()

    for source_val, source_cur, cost_val in rows:
        assert abs(source_val - 100.0) < 0.01, (
            f"cost_source_value changed! Expected 100.0, got {source_val}"
        )
        assert source_cur == "USD", f"cost_source_currency should be USD, got {source_cur}"
        assert abs(cost_val - 100.0) < 0.01, (
            f"stg_meta_ads_daily.cost changed! Expected 100.0 (source amount), got {cost_val}"
        )
