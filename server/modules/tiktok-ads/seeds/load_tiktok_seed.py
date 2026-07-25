"""TikTok Ads seed loader — Story 15.2 (Epic 15).

Lands a deterministic set of ``raw_tiktok_ads_daily`` rows into a DuckDB file so the
local dev loop (and the seed->mart integration test) prove the TikTok join end-to-end:
rows appear in ``fact_daily_kpi`` with ``connector = 'tiktok-ads'`` after ``dbt run``.

Mirrors the meta-ads / shopify seed loader shape. Append-only (AD-7): each invocation
mints a fresh pull_id and never overwrites existing rows. ASCII-only stdout (AI-03).

Usage:
    uv run python server/modules/tiktok-ads/seeds/load_tiktok_seed.py \
        --duckdb-path server/modules/google-analytics/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
import os

# Reuse the generator so the seed rows are the canonical parse-shape (AI-54).
import sys as _sys
from datetime import datetime, timezone
from pathlib import Path

from ulid import ULID

_sys.path.insert(0, str(Path(__file__).parent))
from generate_tiktok_seed import (  # noqa: E402
    generate_multigrain_rows,
    generate_rows,
)

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_tiktok_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    adgroup_id            VARCHAR,
    adgroup_name          VARCHAR,
    ad_id                 VARCHAR,
    spend                 DOUBLE,
    impressions           INTEGER,
    clicks                INTEGER,
    conversions           INTEGER,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR,
    cost_source_currency  VARCHAR DEFAULT 'EUR'
)
"""

_ALTER_ADD_CURRENCY = (
    "ALTER TABLE raw_tiktok_ads_daily ADD COLUMN IF NOT EXISTS "
    "cost_source_currency VARCHAR DEFAULT 'EUR'"
)

# review-15-2 F-1: additive guard for data_level on pre-existing seed tables.
_ALTER_ADD_DATA_LEVEL = (
    "ALTER TABLE raw_tiktok_ads_daily ADD COLUMN IF NOT EXISTS data_level VARCHAR"
)

_INSERT_SQL = """
INSERT INTO raw_tiktok_ads_daily
    (date, data_level, campaign_id, campaign_name, adgroup_id, adgroup_name, ad_id,
     spend, impressions, clicks, conversions, pull_id, loaded_at, project_id,
     cost_source_currency)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    """Insert *rows* into raw_tiktok_ads_daily in a DuckDB file (append-only)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    con.execute(_CREATE_DDL)
    try:
        con.execute(_ALTER_ADD_CURRENCY)
    except Exception:
        pass  # column already present -- safe to ignore.
    try:
        con.execute(_ALTER_ADD_DATA_LEVEL)
    except Exception:
        pass  # column already present -- safe to ignore.
    values = [
        (
            r["date"],
            r.get("data_level"),
            r["campaign_id"],
            r.get("campaign_name"),
            r.get("adgroup_id"),  # None -> NULL (campaign-grain rows)
            r.get("adgroup_name"),
            r.get("ad_id"),
            float(r["spend"]),
            int(r["impressions"]),
            int(r["clicks"]),
            int(r["conversions"]),
            pull_id,
            loaded_at,
            project_id,
            r.get("cost_source_currency", "EUR"),
        )
        for r in rows
    ]
    con.executemany(_INSERT_SQL, values)
    con.close()
    return len(values)


def run(
    duckdb_path: str,
    days: int = 40,
    project_id: str = "default",
    grains: str = "campaign",
) -> tuple[str, int]:
    """Generate + load TikTok seed rows. Returns (pull_id, row_count).

    grains='campaign' (default) lands the campaign grain only; grains='multi' lands the
    three grains coexisting (F-1) so the local mart exercises the data_level filter.
    """
    pull_id = _mint_pull_id()
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if grains == "multi":
        rows = generate_multigrain_rows(days=days)
    else:
        rows = generate_rows(days=days)
    count = load_duckdb(rows, pull_id, loaded_at, duckdb_path, project_id=project_id)
    return pull_id, count


def main() -> None:
    here = Path(__file__).parent
    default_duckdb = os.environ.get(
        "TOOROW_DUCKDB_PATH",
        str(here.parents[1] / "google-analytics" / "seeds" / "local.duckdb"),
    )
    parser = argparse.ArgumentParser(description="Load TikTok Ads seed rows into DuckDB")
    parser.add_argument("--duckdb-path", default=default_duckdb, help="DuckDB file path")
    parser.add_argument("--days", type=int, default=40, help="Number of days to seed")
    parser.add_argument(
        "--grains",
        choices=["campaign", "multi"],
        default="campaign",
        help="campaign = campaign grain only (default); multi = 3 grains coexisting (F-1)",
    )
    args = parser.parse_args()

    pull_id, count = run(args.duckdb_path, days=args.days, grains=args.grains)
    print(
        f"Loaded {count} TikTok rows ({args.grains} grains)  "
        f"pull_id={pull_id}  -> {args.duckdb_path}"
    )


if __name__ == "__main__":
    main()
