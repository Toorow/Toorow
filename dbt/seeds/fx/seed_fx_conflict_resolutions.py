"""Deterministic FX conflict-resolution seeder for Story 13.2 dbt tests.

Story 13.2 (AC11 / AD-6). The table mirror.fx_conflict_resolutions is written
exclusively by Postgres in production (AD-8) and mirrored to DuckDB by
mirror_sync.py. In dev/CI (seed-only mode) the table is empty, which makes
dbt/tests/test_fx_resolution_applied.sql trivially green (0 overridden rows).
This script inserts a deterministic fixture so the reconciliation check is
actually exercised.

Fixture (single resolution, project 'default'):
  project_id             = 'default'
  target_field           = 'cost'
  source_module          = 'meta-ads'
  resolved_source_currency = 'USD'

Context: the meta-ads seed rows use cost_source_currency = 'EUR'. The
resolution overrides that to 'USD'. The fx_rates seed has USD->EUR = 0.92 with
valid_from=2020-01-01 / valid_to=2099-12-31.  Therefore, for any meta-ads row
with cost_source_value V, after staging:

    cost = V * 0.92     (because COALESCE('USD', 'EUR') = 'USD' -> rate 0.92)

The stg_meta_ads_daily model uses:
    COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
in the JOIN fx key, so a resolved_source_currency='USD' makes cost differ from
cost_source_value * 1.0 (the EUR identity rate) and instead use 0.92.

For test_fx_resolution_applied.sql to see overridden_rows:
  1. staged.cost_source_currency must be 'EUR' (raw seed value).
  2. resolution.resolved_source_currency must be 'USD' != 'EUR'.
  3. fx.from_currency='USD', fx.to_currency='EUR', fx.rate=0.92.
  4. cost must equal cost_source_value * 0.92 within 0.01% tolerance.

Convention (same as dbt/seeds/mediaplan/seed_plan_mirror.py):
    uv run python dbt/seeds/fx/seed_fx_conflict_resolutions.py --duckdb-path /path/to/dev.duckdb

The orchestrator runs this BEFORE `dbt build` on the same DuckDB file.

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import argparse
import os

# ---------------------------------------------------------------------------
# Fixture constants
# ---------------------------------------------------------------------------

PROJECT_ID = "default"
TARGET_FIELD = "cost"
SOURCE_MODULE = "meta-ads"
RESOLVED_SOURCE_CURRENCY = "USD"
DECIDED_BY = "seed_fx_13_2"
NOTE = "Story 13.2 test fixture -- USD override to exercise 0.92 rate path"

# DDL mirrors migration 053 (subset needed by DuckDB; no BIGSERIAL -> INTEGER).
_MIRROR_DDL = """
CREATE SCHEMA IF NOT EXISTS mirror;

CREATE TABLE IF NOT EXISTS mirror.fx_conflict_resolutions (
    id                        INTEGER,
    project_id                VARCHAR NOT NULL,
    target_field              VARCHAR NOT NULL,
    source_module             VARCHAR NOT NULL,
    resolved_source_currency  VARCHAR NOT NULL,
    decided_by                VARCHAR NOT NULL,
    decided_at                TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    note                      VARCHAR
);
"""

_INSERT_RESOLUTION = """
INSERT INTO mirror.fx_conflict_resolutions
    (id, project_id, target_field, source_module,
     resolved_source_currency, decided_by, decided_at, note)
VALUES (1, ?, ?, ?, ?, ?, NOW(), ?)
"""


def run(duckdb_path: str) -> None:
    """Idempotent: delete this fixture's row, then re-insert."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_MIRROR_DDL)

        # Idempotent: remove our fixture row only (id=1, specific grain).
        con.execute(
            """
            DELETE FROM mirror.fx_conflict_resolutions
            WHERE project_id   = ?
              AND target_field  = ?
              AND source_module = ?
            """,
            [PROJECT_ID, TARGET_FIELD, SOURCE_MODULE],
        )

        con.execute(
            _INSERT_RESOLUTION,
            [
                PROJECT_ID,
                TARGET_FIELD,
                SOURCE_MODULE,
                RESOLVED_SOURCE_CURRENCY,
                DECIDED_BY,
                NOTE,
            ],
        )

        count = con.execute(
            "SELECT COUNT(*) FROM mirror.fx_conflict_resolutions"
        ).fetchone()[0]

        print(
            "seed_fx_conflict_resolutions OK  "
            f"project={PROJECT_ID}  field={TARGET_FIELD}  module={SOURCE_MODULE}  "
            f"resolved_currency={RESOLVED_SOURCE_CURRENCY}  "
            f"total_rows_in_table={count}  -> {duckdb_path}"
        )
        print(
            "  Test proof: meta-ads rows have cost_source_currency='EUR'; "
            "resolution forces 'USD' -> rate 0.92; "
            "test_fx_resolution_applied will see overridden_rows>0 and verify "
            "cost == cost_source_value * 0.92 within 0.01%."
        )
    finally:
        con.close()


def main() -> None:
    default_duckdb = os.environ.get("TOOROW_DUCKDB_PATH", "")
    parser = argparse.ArgumentParser(
        description=(
            "Seed mirror.fx_conflict_resolutions for Story 13.2 dbt test. "
            "Run BEFORE `dbt build` on the same DuckDB path."
        )
    )
    parser.add_argument(
        "--duckdb-path",
        default=default_duckdb,
        help="DuckDB file path (or TOOROW_DUCKDB_PATH env var)",
    )
    args = parser.parse_args()
    if not args.duckdb_path:
        raise SystemExit("ERROR: --duckdb-path (or TOOROW_DUCKDB_PATH) is required")
    run(args.duckdb_path)


if __name__ == "__main__":
    main()
