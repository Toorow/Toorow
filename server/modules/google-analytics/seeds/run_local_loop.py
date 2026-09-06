"""Local dev loop runner — Story 1.4, T5.1.

Executes the full generate -> load -> dbt run -> dbt test pipeline against
the local DuckDB fallback. Exits non-zero on first failure.

Usage:
    # from repo root
    uv run python server/modules/google-analytics/seeds/run_local_loop.py

    # with custom paths
    uv run python server/modules/google-analytics/seeds/run_local_loop.py \\
        --duckdb-path /tmp/my_test.duckdb

AI-213 (2026-08-17) — THIS SCRIPT HAND-LISTED ITS LOADERS AND HAD STOPPED
BUILDING. It generated the six GA4 CSVs, then landed exactly three connectors
(google-analytics, meta-ads, gsc) and went straight to an UNSELECTED `dbt run`,
which builds every staging model in the tree. Every connector that landed after
this file was written broke it with `Catalog Error: Table with name raw_<x> does
not exist` — the very defect `tests/integration/seed_all_connectors.py` was
written to remove (AI-164), and which `test_seed_to_mart_loop.py` fixed on its
side while this script kept its own stale copy of the list.

It now drives the ONE derived list: `seed_all_connectors.seed_all()` discovers
every `server/modules/*/seeds/load*.py` and threads the corpus anchor
(`TOOROW_SEED_END_DATE`, default 2026-07-19) into each loader that declares an
`end_date` seam. The three mirror steps that `dbt run` needs and that this script
never had — `create_mirror`, `seed_fee_tax`, and the media-plan seeder — run in
their ONLY valid order: `create_mirror` first (it is the sole definition of the
mirror relation shapes), the seeders after.

WHY THE FILENAME MATTERS. dbt-duckdb bakes the DATABASE name (the file stem) into
every view definition it materialises, so a warehouse built as `test.duckdb` and
then copied to `local.duckdb` raises `Binder Error: Catalog "test" does not
exist` on the first read. The evals fixture builder
(`server/tests/evals/build_eval_corpus_and_fixtures.py`) opens
`server/modules/google-analytics/seeds/local.duckdb` by name, so the warehouse it
re-pins against must be BUILT at that filename — which is what this script does
and what the pytest fixture, building into a tmp `test.duckdb`, cannot.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parents[4]  # connector/
SEEDS_DIR = Path(__file__).parent
DBT_DIR = REPO_ROOT / "dbt"
#: The ONE derived seeding driver. It lives under server/tests/integration/
#: because two fixtures need it; duplicating its list here is exactly the defect
#: this script had, so it is imported rather than restated.
SEED_ALL = REPO_ROOT / "server" / "tests" / "integration" / "seed_all_connectors.py"
PLAN_SEEDER = REPO_ROOT / "dbt" / "seeds" / "mediaplan" / "seed_plan_mirror.py"

#: The entity spine's seed lot (AI-303). It lives beside the capture that writes
#: its fixture rather than under `server/modules/*/seeds/`, because it belongs to
#: no connector -- the rows are a managed FILE feed and its governed MDM, which is
#: exactly the shape no connector produces.
ENTITY_SPINE_SEEDER = (
    REPO_ROOT / "server" / "tests" / "integration" / "load_entity_spine_seed.py"
)

#: (profile, output CSV) for every GA4 seed the loaders read. The generator is the
#: source of truth (the CSVs are gitignored); it is anchored on its own AI-66
#: anchor (2026-07-15) with a fixed RNG seed, so regenerating is byte-stable.
_GA4_PROFILES: list[tuple[str | None, str]] = [
    (None, "ga4_seed.csv"),
    ("user_type_daily", "ga4_user_type_seed.csv"),
    ("pages_daily_landing", "ga4_landing_seed.csv"),
    ("pages_daily_paths", "ga4_paths_seed.csv"),
    ("acquisition_session", "ga4_acquisition_session_seed.csv"),
    ("acquisition_first_user", "ga4_acquisition_first_user_seed.csv"),
]


def _import_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(
    label: str,
    cmd: list[str],
    cwd: Path | None = None,
    env: dict | None = None,
) -> None:
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"CMD:  {' '.join(cmd)}")
    print(f"{'='*60}")
    result = subprocess.run(cmd, cwd=str(cwd or REPO_ROOT), env=env)
    if result.returncode != 0:
        print(f"\nFAILED: {label} (exit {result.returncode})")
        sys.exit(result.returncode)
    print(f"OK: {label}")


def _step(label: str) -> None:
    print(f"\n{'='*60}")
    print(f"STEP: {label}")
    print(f"{'='*60}")


def _write_local_profiles_yml(profiles_dir: Path, duckdb_path: str) -> None:
    """Write a profiles.yml into dbt/profiles/ pointing at the given DuckDB path.

    This file is gitignored and created fresh each run so the loop is self-contained.
    Calling the loop multiple times is idempotent — the file is overwritten.
    """
    profiles_dir.mkdir(parents=True, exist_ok=True)
    profiles_yml = profiles_dir / "profiles.yml"
    # `threads: 1` is DBT's model concurrency; it does NOT pin DuckDB's own query
    # parallelism, and that distinction cost two rebuilds to find (AI-213,
    # measured 2026-08-17). With DuckDB free to parallelise `CREATE TABLE AS`,
    # fact_daily_kpi lands its ~30 UNION ALL branches in a different PHYSICAL row
    # order on every build. The rows are identical -- the linkedin-ads raw table
    # was byte-identical across both builds -- but a later SUM() over floats then
    # reduces the adds in a different order, and IEEE-754 keeps the difference:
    # 27056.899999999998 on one build, 27056.899999999994 on the next. Three
    # fixtures diverged on exactly that. The evals builder and
    # `test_reference_sql_green.py` already pin the READ side the same way; a
    # sha-exact fixture needs the WRITE side pinned too.
    content = f"""# Auto-generated by run_local_loop.py — DO NOT COMMIT (gitignored).
# To regenerate: uv run python server/modules/google-analytics/seeds/run_local_loop.py
connector:
  target: local
  outputs:
    local:
      type: duckdb
      path: {duckdb_path!r}
      threads: 1
      settings:
        threads: 1
        preserve_insertion_order: true
"""
    profiles_yml.write_text(content, encoding="utf-8")
    print(f"  Wrote dbt profiles.yml -> {profiles_yml}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full local seed loop")
    parser.add_argument(
        "--duckdb-path",
        default=str(SEEDS_DIR / "local.duckdb"),
        help="DuckDB file path for the local fallback warehouse",
    )
    parser.add_argument(
        "--skip-dbt-test",
        action="store_true",
        help="Build the warehouse but skip the final `dbt test` step.",
    )
    args = parser.parse_args()

    duckdb_path = str(Path(args.duckdb_path).resolve())
    Path(duckdb_path).parent.mkdir(parents=True, exist_ok=True)
    # A stale warehouse would keep relations no model rebuilds — the loop must
    # describe the tree as it is today, not as it was the last time it ran.
    for leftover in (Path(duckdb_path), Path(duckdb_path + ".wal")):
        if leftover.exists():
            leftover.unlink()

    # Ensure dbt profiles.yml exists and points to the chosen DuckDB path
    _write_local_profiles_yml(DBT_DIR / "profiles", duckdb_path)

    run_env = {**os.environ, "TOOROW_DUCKDB_PATH": duckdb_path}

    # 1 — Generate the GA4 seed CSVs the GA4 loaders read.
    for profile, csv_name in _GA4_PROFILES:
        cmd = [
            sys.executable,
            str(SEEDS_DIR / "generate_seed.py"),
            "--out", str(SEEDS_DIR / csv_name),
        ]
        if profile:
            cmd += ["--profile", profile]
        _run(f"Generate GA4 seed CSV ({profile or 'standard_daily'})", cmd, env=run_env)

    seed_all_connectors = _import_module("seed_all_connectors", SEED_ALL)

    # 2 — Land EVERY connector's seed. The list is DERIVED from the tree, never
    # written here: `dbt run` below is unselected, so a single absent raw_* table
    # fails the whole build.
    _step("Land every connector seed (derived loader list)")
    landed = seed_all_connectors.seed_all(duckdb_path)
    print(f"  {len(landed)} loader invocation(s) landed")
    print(f"  corpus anchor: TOOROW_SEED_END_DATE = {seed_all_connectors.SEED_END_DATE}")

    # 3 — mirror.* relations. In production `mirror_sync.py` lands them from
    # Postgres; locally this is the ONE definition of their shape. It MUST come
    # before the seeders: they write into these relations and cannot create them
    # without becoming a second, divergent definition.
    _step("Create mirror.* relations")
    seed_all_connectors.create_mirror(duckdb_path)

    # 4 — The fee/tax seeders, in their declared order. Every Tax model reads its
    # activation gate from `project_tax_fee_activation`; an empty gate means zero
    # active rows and zero rows everywhere downstream.
    _step("Seed fee/tax mirror relations")
    print(f"  {seed_all_connectors.seed_fee_tax(duckdb_path)}")

    # 5 — The media-plan seeder (three plans, including one whose spend currency
    # differs from the project's). Without it the plan/pacing marts build over
    # zero rows and their dbt tests pass by emptiness.
    _step("Seed media plan mirror relations")
    _import_module("seed_plan_mirror", PLAN_SEEDER).run(duckdb_path)

    # 5b — The entity spine (AI-303). Same reason as the line above, on the four
    # models epics 68-69 delivered: `managed_feed_superseding`,
    # `stg_managed_feed_facts`, the managed branch of `fact_daily_kpi` and
    # `semantic_fact_by_entity_attribute` all built over ZERO rows, so every one
    # of their tests passed by emptiness and no eval question could be written
    # against them without inventing its fixture.
    #
    # AFTER `create_mirror`, and the order is not free: this seeder creates the
    # five mirror relations the spine needs from the SHAPES IT CAPTURED, which
    # `create_mirror` does not carry. Running it first would have those relations
    # replaced by nothing.
    #
    # It replays `server/tests/fixtures/entity_spine/captured_spine.json`, written
    # by driving the real chain against a live Postgres. No Postgres is needed
    # here -- that is the whole point of the split.
    _step("Seed the entity spine (captured chain)")
    for relation, count in sorted(
        _import_module("load_entity_spine_seed", ENTITY_SPINE_SEEDER)
        .run(duckdb_path=duckdb_path)
        .items()
    ):
        print(f"  {relation:44s} {count:3d} row(s)")

    # 6 — dbt seed (declarative configs, e.g. metric_source_priority — Story 3.7)
    # Story 4.1: --project-dir required so dbt resolves seed-paths relative to dbt_project.yml
    # location (not the Python cwd). This avoids "dbt\seeds\..." path confusion on Windows.
    _run(
        "dbt seed",
        [sys.executable, "-m", "dbt.cli.main", "seed",
         "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR / "profiles")],
        cwd=REPO_ROOT,
        env=run_env,
    )

    # 7 — dbt run
    _run(
        "dbt run",
        [sys.executable, "-m", "dbt.cli.main", "run",
         "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR / "profiles")],
        cwd=REPO_ROOT,
        env=run_env,
    )

    # 8 — dbt test
    if not args.skip_dbt_test:
        _run(
            "dbt test",
            [sys.executable, "-m", "dbt.cli.main", "test",
             "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR / "profiles")],
            cwd=REPO_ROOT,
            env=run_env,
        )

    print("\n" + "="*60)
    print("LOCAL LOOP COMPLETE — all steps passed.")
    print(f"Warehouse: {duckdb_path}")
    print("="*60)


if __name__ == "__main__":
    main()
