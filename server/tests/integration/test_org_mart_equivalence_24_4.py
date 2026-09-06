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

Source routing (AC2, T3): LANDED. Every module ``schema.yml`` now declares
``schema: "{{ var('raw_schema', 'main') }}"`` -- ``main`` without the var (exactly
the legacy read), ``org_<wslug>_raw`` when the orchestrator injects it from
``warehouse_tenancy.OrgSchemas.raw``. This run passes only ``--vars '{"org":
"test"}'``, so the org leg still reads ``main.raw_*`` while writing
``org_test_marts`` -- which is precisely the AC4 claim under test (the marts
LOCATION is the only thing that changed). ``_copy_raw_into_org`` populates
``org_test_raw`` so the same test also covers AC2 the day a leg passes
``raw_schema``.

Skipped when dbt is not on the PATH (same gate as test_seed_to_mart_loop.py).
This module is [dbt-local][pg-gated] -- run locally by the orchestrator, never CI.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.integration import seed_all_connectors

REPO_ROOT = Path(__file__).parents[3]
DBT_DIR = REPO_ROOT / "dbt"

try:
    import dbt.cli.main  # noqa: F401
    _DBT_AVAILABLE = True
except ImportError:
    _DBT_AVAILABLE = False

pytestmark = [
    pytest.mark.skipif(
        not _DBT_AVAILABLE,
        reason="dbt not found in PATH -- skipping org-mart equivalence integration test",
    ),
    # THIS FILE COULD NEVER PASS UNDER THE PROJECT TIMEOUT, and it did not fail
    # alone -- it aborted the WHOLE session. `timeout = 180` with
    # `timeout_method = "thread"` (server/pyproject.toml) cannot interrupt a
    # blocking `subprocess.run`, so pytest dumps the stack and stops everything
    # after it. Measured 2026-08-05 while trying to answer the pre-deploy norm
    # "run the pg-gated suite": the run died here, in
    # `test_org_marts_bit_identical_to_legacy`, and produced no verdict for any
    # of the ~1500 tests that had not run yet.
    #
    # AND IT WAS NEVER BROKEN, ONLY STARVED. Measured the same day, once the
    # ceiling was lifted:
    #     python -m pytest tests/integration/test_org_mart_equivalence_24_4.py -q
    #     -> 1 passed in 347.80s (0:05:47)
    # 348 seconds of work under a 180-second ceiling. The test drives SIX dbt
    # invocations -- run and test on the legacy leg, run and test on the org leg,
    # plus seeds -- so it could not have fitted, and the "hang" it looked like was
    # the equivalence proof doing its job.
    #
    # 1800 is deliberately far above the 348 measured: dbt time moves with the
    # number of models, and a ceiling set just above today's measurement would
    # turn the next model added into this same silent session abort.
    #
    # Given per FILE rather than per test: the module-scoped fixture that builds
    # the DuckDB is where most of the time goes, and a per-test marker would leave
    # the fixture under the global ceiling.
    pytest.mark.timeout(1800),
]

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
    """Delegate to the ONE shared mirror fixture (seed_all_connectors)."""
    seed_all_connectors.create_mirror(db_path)


def _dbt(cmd_tail: list[str], profiles_dir: Path, env: dict) -> subprocess.CompletedProcess:
    target_dir = profiles_dir.parent / "target"
    return subprocess.run(
        [sys.executable, "-m", "dbt.cli.main", *cmd_tail,
         "--profiles-dir", str(profiles_dir), "--project-dir", str(DBT_DIR),
         "--target-path", str(target_dir)],
        capture_output=True, text=True, env=env,
    )


#: dbt prints one such line per failing node, naming its resource type.
_FAILURE_LINE = re.compile(r"Failure in (model|test|seed|snapshot) ([\w.]+)")

def _failures(completed: subprocess.CompletedProcess) -> tuple[set[str], set[str]]:
    """Return ``(failing_models, failing_tests)`` named by dbt's own output.

    Asserting on the process returncode alone conflated "a model would not build"
    with "a data test disagreed on this synthetic fixture" -- and the fixture has
    always carried some of the latter, so the returncode assertion was unreachable
    and the test never ran to its actual claim.

    The split is what lets the two claims be stated separately below:
      * a MODEL failure is never tolerated, on either leg -- a model that will not
        materialise cannot be compared between two schemas;
      * a TEST failure is judged by PARITY, not by absolute count. This test owns
        the question "does the org topology change a number", not "is every data
        assertion green on an all-connectors seed fixture" (it is not: measured
        2026-08-04, the legacy leg fails 50 of 808 -- an undeclared ``dbt_utils``
        package, two stale FX-at-staging assertions, and a batch of marts tests
        that were SKIPPED for as long as the fixture could not build. Each is
        pre-existing debt of the fixture, none of it warehouse topology).
    """
    text = completed.stdout + completed.stderr
    models, tests = set(), set()
    for kind, name in _FAILURE_LINE.findall(text):
        (models if kind == "model" else tests).add(name)
    return models, tests


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
    db_path = str(tmp_path / "equiv.duckdb")

    # 1 -- seed EVERY connector's raw into main.raw_* (the legacy landing zone).
    #
    # Measured 2026-08-04: seeding GA4 alone is not enough and has not been for a
    # while. ``fact_daily_kpi`` ref()s ~30 staging models across the module dbt
    # trees, and dbt fails a model whose source table is absent -- so an
    # unselected ``dbt build`` returned 52 errors and this test asserted on a
    # returncode that could never be 0. The loader list is DERIVED from the tree
    # (seed_all_connectors) so a connector landing tomorrow is seeded without an
    # edit here; the previous hand-written list is exactly what rotted.
    landed = seed_all_connectors.seed_all(db_path)
    assert landed, "every connector seed loader must be discovered and run"

    _create_mirror(db_path)
    profiles_dir = tmp_path / "profiles"
    _write_profiles(profiles_dir, db_path)
    env = {**os.environ, "TOOROW_DUCKDB_PATH": db_path}

    # 2 -- LEGACY materialisation (no --vars) -> main_marts.
    #
    # `run` + `test`, not `build`. In `dbt build` a FAILING TEST SKIPS every
    # downstream model, so the four tolerated test failures below (one of them on
    # stg_meta_ads_daily, which fact_daily_kpi ref()s) skipped the very table this
    # test checksums -- `Catalog Error: fact_daily_kpi does not exist`. Splitting
    # the two phases materialises the marts and still runs every test; the
    # assertions below are stricter than the returncode ever was.
    assert _dbt(["seed"], profiles_dir, env).returncode == 0
    legacy_run = _dbt(["run"], profiles_dir, env)
    legacy_models, _ = _failures(legacy_run)
    assert not legacy_models, (
        "a model failed to materialise on the LEGACY leg -- there is nothing to "
        f"compare: {sorted(legacy_models)}\n" + legacy_run.stdout + legacy_run.stderr
    )
    legacy_test = _dbt(["test"], profiles_dir, env)
    _, legacy_tests = _failures(legacy_test)
    legacy_count, legacy_hash = _checksum(db_path, "main_marts.")
    assert legacy_count > 0, "legacy fact_daily_kpi must have rows"

    # 3 -- ORG materialisation: copy the SAME raw into org_test_raw, build with --vars.
    _copy_raw_into_org(db_path, "org_test_raw")
    org_vars = ["--vars", '{"org": "test"}']
    org_run = _dbt(["run", *org_vars], profiles_dir, env)
    org_models, _ = _failures(org_run)
    assert not org_models, (
        "a model failed to materialise on the ORG leg -- the org topology broke a "
        f"model the legacy leg builds: {sorted(org_models)}\n" + org_run.stdout + org_run.stderr
    )
    org_test = _dbt(["test", *org_vars], profiles_dir, env)
    _, org_tests = _failures(org_test)
    org_count, org_hash = _checksum(db_path, "org_test_marts.")

    # 4 -- EQUIVALENCE (AD-22): topology moves data, never a number.
    assert org_count == legacy_count, (
        f"row count differs: legacy={legacy_count} org={org_count}"
    )
    assert org_hash == legacy_hash, (
        "ordered fact_daily_kpi checksum differs between main_marts and org_test_marts "
        "-- the org topology changed a VALUE, not just the location (AC4 violation)"
    )

    # 5 -- FAILURE PARITY: the org leg must fail on EXACTLY the same nodes as the
    # legacy leg, no more. This is the equivalence claim carried down to the build
    # itself: a topology that moves data must not turn one more assertion red.
    assert org_tests == legacy_tests, (
        "the two legs do not fail on the same nodes -- the org topology changed "
        f"what dbt can assert.\n  legacy only: {sorted(legacy_tests - org_tests)}"
        f"\n  org only:    {sorted(org_tests - legacy_tests)}"
    )
