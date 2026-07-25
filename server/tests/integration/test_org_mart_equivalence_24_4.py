"""Story 24.4 (AC4/AC8) -- bit-identical equivalence: legacy marts vs per-org marts.

Proves the org topology changes ONLY the physical LOCATION of the marts, never a
single number (pattern AD-22, ordered hash -- not just COUNT). From ONE set of
seeds we materialise:

  (a) LEGACY  : ``dbt build`` with no ``--vars`` -> ``main_marts.fact_daily_kpi``,
  (b) ORG     : the SAME raw copied into ``org_test_raw`` + ``dbt build --vars
                org=test`` -> ``org_test_marts.fact_daily_kpi``.

Then it asserts COUNT(*) equality AND an ordered md5 checksum equality of
fact_daily_kpi across the two schemas. It also proves the per-org loop of AC8
runs end-to-end in LOCAL (no CI job added).

IMPORTANT (source routing caveat, AC2/T3 deferred): the ``raw_source_schema()``
macro is in place, but the 10 module ``schema.yml`` files still declare
``schema: main`` for their raw_* sources (their edit is deferred behind the
epic-25 parallel-session lock on server/modules/**). Until those edits land,
``dbt build --vars org=test`` routes the MARTS/STAGING to ``org_test_*`` but the
raw sources still read ``main.raw_*``. That is HARMLESS for THIS equivalence
proof (both builds derive from the same raw) -- it proves AC4 (marts location is
the only change). Once the schema.yml edits land, re-running with the
``_copy_raw_into_org`` step below ALSO proves AC2 (staging reads org_test_raw).
The copy is kept so the test upgrades automatically the day the module edits land.

Skipped when dbt is not on the PATH (same gate as test_seed_to_mart_loop.py).
This module is [dbt-local][pg-gated] -- run locally by the orchestrator, never CI.
"""

from __future__ import annotations

import importlib.util
import os
import pathlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parents[3]
GA4_SEEDS = REPO_ROOT / "server" / "modules" / "google-analytics" / "seeds"
DBT_DIR = REPO_ROOT / "dbt"

try:
    import dbt.cli.main  # noqa: F401
    _DBT_AVAILABLE = True
except ImportError:
    _DBT_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _DBT_AVAILABLE,
    reason="dbt not found in PATH -- skipping org-mart equivalence integration test",
)

# The ordered checksum of fact_daily_kpi -- every value column, deterministically
# ordered so the md5 is stable across engines/schemas (AD-22 / piege n4).
_FACT_CHECKSUM_SQL = (
    "SELECT COUNT(*), md5(string_agg("
    "coalesce(project_id,'') || '|' || coalesce(CAST(date AS VARCHAR),'') || '|' || "
    "coalesce(connector,'') || '|' || coalesce(metric,'') || '|' || "
    "coalesce(breakdown_dimension,'') || '|' || coalesce(breakdown_value,'') || '|' || "
    "coalesce(CAST(value AS VARCHAR),'') "
    "ORDER BY project_id, date, connector, metric, breakdown_dimension, breakdown_value, value)) "
    "FROM {schema}fact_daily_kpi"
)


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_profiles(profiles_dir: Path, db_path: str) -> Path:
    profiles_dir.mkdir(parents=True, exist_ok=True)
    dst = profiles_dir / "profiles.yml"
    dst.write_text(
        "connector:\n"
        "  target: local\n"
        "  outputs:\n"
        "    local:\n"
        "      type: duckdb\n"
        f'      path: "{pathlib.PurePath(db_path).as_posix()}"\n'
        "      threads: 1\n",
        encoding="utf-8",
    )
    return dst


def _create_mirror(db_path: str) -> None:
    import duckdb

    con = duckdb.connect(db_path)
    con.execute("CREATE SCHEMA IF NOT EXISTS mirror")
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.project_preferences ("
        "project_id VARCHAR, canonical_currency VARCHAR, reporting_timezone VARCHAR, "
        "verification_source_type VARCHAR, verification_source_id VARCHAR, "
        "lead_event_name VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP)"
    )
    con.execute(
        "INSERT INTO mirror.project_preferences VALUES "
        "('default','EUR','Europe/Paris',NULL,NULL,NULL,current_timestamp,current_timestamp)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.context_events ("
        "id VARCHAR, project_id VARCHAR, event_date DATE, type VARCHAR, label VARCHAR, "
        "description VARCHAR, created_by VARCHAR, created_at TIMESTAMP)"
    )
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.connection_ref_dim ("
        "project_id VARCHAR, connector_name VARCHAR, display_name VARCHAR)"
    )
    # Story 13.2: staging models LEFT JOIN mirror.fx_conflict_resolutions (AD-6).
    # Empty here -> JOIN matches nothing -> COALESCE falls back to raw currency.
    con.execute(
        "CREATE TABLE IF NOT EXISTS mirror.fx_conflict_resolutions ("
        "id VARCHAR, project_id VARCHAR, target_field VARCHAR, source_module VARCHAR, "
        "resolved_source_currency VARCHAR, decided_by VARCHAR, decided_at TIMESTAMP, note VARCHAR)"
    )
    con.close()


def _dbt(cmd_tail: list[str], profiles_dir: Path, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "dbt.cli.main", *cmd_tail,
         "--profiles-dir", str(profiles_dir), "--project-dir", str(DBT_DIR)],
        capture_output=True, text=True, env=env,
    )


def _copy_raw_into_org(db_path: str, org_raw_schema: str) -> None:
    """Copy every main.raw_* table into *org_raw_schema* (simulates 24.3 per-org raw)."""
    import duckdb

    con = duckdb.connect(db_path)
    try:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {org_raw_schema}")
        rows = con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_name LIKE 'raw_%'"
        ).fetchall()
        for (tbl,) in rows:
            con.execute(
                f"CREATE TABLE {org_raw_schema}.{tbl} AS SELECT * FROM main.{tbl}"
            )
    finally:
        con.close()


def _checksum(db_path: str, schema_prefix: str) -> tuple[int, str]:
    import duckdb

    con = duckdb.connect(db_path, read_only=True)
    try:
        count, digest = con.execute(
            _FACT_CHECKSUM_SQL.format(schema=schema_prefix)
        ).fetchone()
    finally:
        con.close()
    return int(count), digest


def test_org_marts_bit_identical_to_legacy(tmp_path):
    """AC4: fact_daily_kpi is identical (count + ordered md5) in main_marts vs org_test_marts."""
    from datetime import date, timedelta

    db_path = str(tmp_path / "equiv.duckdb")
    csv_path = str(tmp_path / "ga4_seed.csv")

    # 1 -- seed GA4 raw into main.raw_* (the legacy landing zone), mirroring the
    # invocation of test_seed_to_mart_loop.py so the fixture stays consistent.
    gen = _import("generate_seed", GA4_SEEDS / "generate_seed.py")
    loader = _import("load_seed", GA4_SEEDS / "load_seed.py")
    rows = gen.generate_rows(date.today())
    rows += [r for r in gen.generate_rows(date.today() - timedelta(days=5))]
    gen.write_csv(rows, csv_path)
    _pull_id, count = loader.run(
        csv_path=csv_path, mode="duckdb", duckdb_path=db_path, bq_project=None
    )
    assert count > 0, "GA4 seed must land raw rows"

    _create_mirror(db_path)
    profiles_dir = tmp_path / "profiles"
    _write_profiles(profiles_dir, db_path)
    env = {**os.environ, "TOOROW_DUCKDB_PATH": db_path}

    # 2 -- LEGACY materialisation (no --vars) -> main_marts.
    assert _dbt(["seed"], profiles_dir, env).returncode == 0
    legacy = _dbt(["build"], profiles_dir, env)
    assert legacy.returncode == 0, legacy.stdout + legacy.stderr
    legacy_count, legacy_hash = _checksum(db_path, "main_marts.")
    assert legacy_count > 0, "legacy fact_daily_kpi must have rows"

    # 3 -- ORG materialisation: copy the SAME raw into org_test_raw, build with --vars.
    _copy_raw_into_org(db_path, "org_test_raw")
    org = _dbt(["build", "--vars", '{"org": "test"}'], profiles_dir, env)
    assert org.returncode == 0, org.stdout + org.stderr
    org_count, org_hash = _checksum(db_path, "org_test_marts.")

    # 4 -- EQUIVALENCE (AD-22): topology moves data, never a number.
    assert org_count == legacy_count, (
        f"row count differs: legacy={legacy_count} org={org_count}"
    )
    assert org_hash == legacy_hash, (
        "ordered fact_daily_kpi checksum differs between main_marts and org_test_marts "
        "-- the org topology changed a VALUE, not just the location (AC4 violation)"
    )
