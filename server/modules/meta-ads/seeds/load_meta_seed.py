"""Meta Ads seed loader — Story 3.6.

Lands a small, deterministic set of ``raw_meta_ads_daily`` rows into a DuckDB
file so the local dev loop (and the seed->mart integration test) can prove the
Meta Ads join end-to-end: rows appear in ``fact_daily_kpi`` with
``connector = 'meta-ads'`` after ``dbt run``.

This mirrors the GA4 seed loader shape but is intentionally tiny (a handful of
campaign/adset/ad rows across a few days). Append-only (AD-7): each invocation
mints a fresh pull_id and never overwrites existing rows.

Usage:
    uv run python server/modules/meta-ads/seeds/load_meta_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import os

# Reuse the generator so the seed rows are the canonical parse-shape (review-15-9 F-1).
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

_sys.path.insert(0, str(Path(__file__).parent))
from generate_meta_seed import (  # noqa: E402
    generate_multigrain_rows,
    generate_rows,
)

# review-15-9 F-1: data_level distinguishes WHICH report grain landed each row
# (CAMPAIGN | ADSET | CREATIVE). Part of the grain key so coexisting grains never
# double-count. Legacy rows (pre-migration) stay NULL -> staging COALESCEs to CAMPAIGN.
_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_meta_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    adset_id              VARCHAR,
    adset_name            VARCHAR,
    ad_id                 VARCHAR,
    creative_id           VARCHAR,
    spend                 DOUBLE,
    impressions           INTEGER,
    clicks                INTEGER,
    conversions           INTEGER,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR,
    cost_source_currency  VARCHAR DEFAULT 'USD'
)
"""

# Story 4.2 (AC3): ALTER TABLE to add cost_source_currency if the table already exists
# without the column (e.g. DuckDB file created before Story 4.2).
# Option 2 from Dev Notes: safer than drop/recreate; existing test suites keep their data.
_ALTER_ADD_COST_SOURCE_CURRENCY = """
ALTER TABLE raw_meta_ads_daily ADD COLUMN IF NOT EXISTS cost_source_currency VARCHAR DEFAULT 'USD'
"""

# review-15-9 F-1: additive guard for data_level on pre-existing seed tables.
_ALTER_ADD_DATA_LEVEL = (
    "ALTER TABLE raw_meta_ads_daily ADD COLUMN IF NOT EXISTS data_level VARCHAR"
)

_INSERT_SQL = """
INSERT INTO raw_meta_ads_daily
    (date, data_level, campaign_id, campaign_name, adset_id, adset_name, ad_id, creative_id,
     spend, impressions, clicks, conversions, pull_id, loaded_at, project_id,
     cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _mint_pull_id() -> str:
    return f"pull_{ULID()}"


def load_duckdb(
    rows: list[dict],
    pull_id: str,
    loaded_at: str,
    duckdb_path: str,
    project_id: str = "default",
) -> int:
    """Insert *rows* into raw_meta_ads_daily in a DuckDB file (append-only)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    # Story 4.2 (AC3 Dev Notes Option 2): add cost_source_currency to existing tables.
    # DuckDB supports ALTER TABLE ... ADD COLUMN IF NOT EXISTS, so this is idempotent.
    try:
        con.execute(_ALTER_ADD_COST_SOURCE_CURRENCY)
    except Exception:
        pass  # Column already exists or DDL already includes it — safe to ignore.
    try:
        con.execute(_ALTER_ADD_DATA_LEVEL)  # review-15-9 F-1: migrate legacy tables.
    except Exception:
        pass  # Column already present — safe to ignore.
    values = [
        (
            r["date"],
            # review-15-9 F-1: default to CAMPAIGN when absent (retro-compat rows).
            r.get("data_level", "CAMPAIGN"),
            r["campaign_id"],
            r["campaign_name"],
            r.get("adset_id"),      # None -> NULL (campaign-grain rows)
            r.get("adset_name"),
            r.get("ad_id"),
            r.get("creative_id"),
            float(r["spend"]),
            int(r["impressions"]),
            int(r["clicks"]),
            int(r["conversions"]),
            pull_id,
            loaded_at,
            r.get("project_id", project_id),
            r.get("cost_source_currency", "USD"),
        )
        for r in rows
    ]
    con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def run(
    duckdb_path: str,
    days: int = 30,
    project_id: str = "default",
    grains: str = "campaign",
) -> tuple[str, int]:
    """Generate + load Meta seed rows. Returns (pull_id, row_count).

    grains='campaign' (default, retro-compat: 60 rows for 30 days) lands the campaign
    grain only; grains='multi' lands the three grains coexisting (F-1) so the local mart
    exercises the data_level filter.
    """
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if grains == "multi":
        rows = generate_multigrain_rows(days=days, project_id=project_id)
    else:
        rows = generate_rows(days=days, project_id=project_id)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load Meta Ads seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="DuckDB file path")
    parser.add_argument("--days", type=int, default=30, help="Number of days to seed")
    parser.add_argument(
        "--grains",
        choices=["campaign", "multi"],
        default="campaign",
        help="campaign = campaign grain only (default); multi = 3 grains coexisting (F-1)",
    )
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days, grains=args.grains)
    print(
        f"Loaded {count} Meta rows ({args.grains} grains)  "
        f"pull_id={pull_id}  -> {args.duckdb_path}"
    )


if __name__ == "__main__":
    main()
