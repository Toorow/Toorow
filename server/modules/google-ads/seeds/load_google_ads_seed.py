"""Google Ads seed loader -- Story 26.2.

Lands a small, deterministic set of ``raw_google_ads_daily`` LONG rows into a
DuckDB file so the local dev loop can prove the staging join end-to-end
(``dbt run`` -> stg_google_ads_daily). Append-only (AD-7): each invocation
mints a fresh pull_id and never overwrites existing rows.

AI-03: ASCII-only stdout.

Usage:
    uv run python server/modules/google-ads/seeds/load_google_ads_seed.py \
        --duckdb-path server/modules/google-ads/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from ulid import ULID

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_google_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    customer_id           VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    ad_id                 VARCHAR,
    criterion_id          VARCHAR,
    keyword_text          VARCHAR,
    search_term           VARCHAR,
    segments_json         VARCHAR,
    metric                VARCHAR,
    value_num             DOUBLE,
    cost_source_currency  VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

_INSERT_SQL = """
INSERT INTO raw_google_ads_daily
    (date, data_level, customer_id, campaign_id, campaign_name, ad_group_id,
     ad_group_name, ad_id, criterion_id, keyword_text, search_term,
     segments_json, metric, value_num, cost_source_currency, pull_id,
     loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Deterministic campaign-grain seed: 2 campaigns x 3 days x 4 metrics.
# cost is ALREADY in currency units (micros/1e6 applied at landing -- 26.2).
_CAMPAIGNS = (
    ("22334455", "Brand - Search - FR"),
    ("22334466", "Generic - PMax - FR"),
)
_DAYS = ("2026-07-01", "2026-07-02", "2026-07-03")
_METRICS = {
    "cost": (118.40, 20.13),
    "impressions": (12040, 5310),
    "clicks": (640, 122),
    "conversions": (42.5, 6.0),
}


def load_seed(duckdb_path: str, project_id: str = "default") -> int:
    pull_id = f"pull_{ULID()}"
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    rows: list[tuple] = []
    for day_index, day in enumerate(_DAYS):
        for camp_index, (campaign_id, campaign_name) in enumerate(_CAMPAIGNS):
            for metric, base_values in _METRICS.items():
                value = float(base_values[camp_index]) * (1.0 + 0.05 * day_index)
                rows.append(
                    (
                        day, "CAMPAIGN", "9861234567", campaign_id, campaign_name,
                        "", "", "", "", "", "", None, metric, round(value, 2),
                        "EUR", pull_id, loaded_at, project_id,
                    )
                )

    Path(duckdb_path).parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_CREATE_DDL)
        con.executemany(_INSERT_SQL, rows)
    finally:
        con.close()
    print(f"Seeded {len(rows)} raw_google_ads_daily rows (pull_id={pull_id})")
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duckdb-path",
        default=str(Path(__file__).with_name("local.duckdb")),
        help="Target DuckDB file (default: seeds/local.duckdb next to this script).",
    )
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()
    load_seed(args.duckdb_path, args.project_id)


if __name__ == "__main__":
    main()
