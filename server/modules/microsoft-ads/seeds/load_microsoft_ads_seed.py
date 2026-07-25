"""Microsoft Ads seed loader -- Story 26.4.

Lands a small, deterministic set of ``raw_microsoft_ads_daily`` LONG rows into
a DuckDB file so the local dev loop can prove the staging join end-to-end
(``dbt run`` -> stg_microsoft_ads_daily). Append-only (AD-7): each invocation
mints a fresh pull_id and never overwrites existing rows.

AI-03: ASCII-only stdout.

Usage:
    uv run python server/modules/microsoft-ads/seeds/load_microsoft_ads_seed.py \
        --duckdb-path server/modules/microsoft-ads/seeds/local.duckdb
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import duckdb
from ulid import ULID

_CREATE_DDL = """
CREATE TABLE IF NOT EXISTS raw_microsoft_ads_daily (
    date                  VARCHAR,
    data_level            VARCHAR,
    account_id            VARCHAR,
    campaign_id           VARCHAR,
    campaign_name         VARCHAR,
    ad_group_id           VARCHAR,
    ad_group_name         VARCHAR,
    ad_id                 VARCHAR,
    keyword_id            VARCHAR,
    keyword               VARCHAR,
    search_query          VARCHAR,
    segments_json         VARCHAR,
    attributes_json       VARCHAR,
    metric                VARCHAR,
    value_num             DOUBLE,
    cost_source_currency  VARCHAR,
    pull_id               VARCHAR,
    loaded_at             VARCHAR,
    project_id            VARCHAR
)
"""

# Story 26.6 Part A: attributes_json holds the latest-wins entity status/type
# attributes (out of the supersede grain). NULL in this seed (no mutable attrs).
_INSERT_SQL = """
INSERT INTO raw_microsoft_ads_daily
    (date, data_level, account_id, campaign_id, campaign_name, ad_group_id,
     ad_group_name, ad_id, keyword_id, keyword, search_query, segments_json,
     attributes_json, metric, value_num, cost_source_currency, pull_id,
     loaded_at, project_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

# Deterministic campaign-grain seed: 2 campaigns x 3 days x 5 metrics.
# cost is ALREADY in currency units; conversions restate on re-pull (AD-7
# QUALIFY supersede makes that safe).
_CAMPAIGNS = (
    ("22334455", "Brand - Search - FR"),
    ("22334466", "Generic - Shopping - FR"),
)
_DAYS = ("2026-07-01", "2026-07-02", "2026-07-03")
_METRIC_BASE = {
    "cost": 118.4,
    "impressions": 12040.0,
    "clicks": 640.0,
    "conversions": 42.0,
    "revenue": 3120.75,
}
_ACCOUNT_ID = "135792468"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--duckdb-path",
        default=str(Path(__file__).with_name("local.duckdb")),
        help="Target DuckDB file (created if absent).",
    )
    parser.add_argument("--project-id", default="default")
    args = parser.parse_args()

    pull_id = f"pull_{ULID()}"
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")

    values = []
    for day_index, day in enumerate(_DAYS):
        for campaign_index, (campaign_id, campaign_name) in enumerate(_CAMPAIGNS):
            for metric, base in _METRIC_BASE.items():
                value = round(base * (1 + 0.1 * day_index + 0.5 * campaign_index), 4)
                values.append(
                    (
                        day, "CAMPAIGN", _ACCOUNT_ID, campaign_id, campaign_name,
                        "", "", "", "", "", "", None, None, metric, value, "EUR",
                        pull_id, loaded_at, args.project_id,
                    )
                )

    con = duckdb.connect(args.duckdb_path)
    con.execute(_CREATE_DDL)
    con.executemany(_INSERT_SQL, values)
    con.close()
    print(
        f"Seeded {len(values)} raw_microsoft_ads_daily rows "
        f"(pull_id={pull_id}) into {args.duckdb_path}"
    )


if __name__ == "__main__":
    main()
