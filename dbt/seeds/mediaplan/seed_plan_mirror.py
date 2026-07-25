"""Deterministic plan-mirror + fact-cost seeder for the Story 22.4 dbt tests.

Story 22.4 (FR38 / CAP-26). The plan tables (media_plans / media_plan_versions /
media_plan_lines / plan_allocation_daily / plan_line_mappings) reach the warehouse
by MIRROR (mirror_sync.py in production; AD-8 -- Postgres is the sole writer). There
is no dbt seed for the mirror.* schema: the established repo convention for feeding
mirror tables into a DEV DuckDB (see server/tests/integration/test_seed_to_mart_loop.py
and server/modules/gsc/seeds/load_gsc_seed.py) is a DIRECT DuckDB CREATE + INSERT into
mirror.<table>. This script follows that path and, in the SAME DuckDB file, lands the
matching Meta-Ads cost rows (via the meta seed loader) so fact_daily_kpi carries the
real campaign spend the plan marts ventilate.

It is DETERMINISTIC with FIXED DATES (never Date.now()) so the dbt singular tests
(test_plan_vs_actual_ventilation_sum / _plan_pacing_extrapolation / _channel_sum /
_pace_null_when_alloc_zero / _plan_only_actual_null) are reproducible.

Fixture (project 'default', 1 active plan, 3 lines):
  * line-digital-a  budget 3000 EUR, 2026-03-01..2026-03-30 (30 j, 100/j), channel
      'digital'. Maps camp_a_solo (100 %) + camp_shared (0.5). Real spend over the
      first 10 days: camp_a_solo 100/j + camp_shared 50/j -> line-a ventilated
      100 + 0.5*50 = 125/j -> 1 250 over 10 j. => the story's worked example
      (consumed 41,7 %, pace +25 %, remaining 1 750, extrapolated 3 750).
  * line-digital-b  budget 1500 EUR, 2026-03-01..2026-03-30, channel 'digital'.
      Maps camp_shared (0.5) -> ventilated 0.5*50 = 25/j -> 250 over 10 j. Proves a
      SHARED campaign is split 0.5/0.5 (50/50), never double-counted (100/100).
  * line-tv         budget 5000 EUR, 2026-03-01..2026-03-30, channel 'tv',
      is_plan_only=TRUE. NO mapping -> actual NULL (plan-only honesty, décision 7).

The newest real-spend day in the plan is 2026-03-10, so the plan-wide to-date anchor
(as_of_day) = 2026-03-10 -> 10 days elapsed on the digital lines.

Usage (the orchestrator runs this before `dbt build` on the same DuckDB path):
    uv run python dbt/seeds/mediaplan/seed_plan_mirror.py --duckdb-path /tmp/ci_test.duckdb

ASCII-only stdout (project rule).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Reuse the meta-ads seed loader so the cost rows are the canonical parse shape and
# land in fact_daily_kpi with metric='cost', breakdown_dimension='campaign_id'
# (breakdown_value = campaign_id) exactly like a real Meta pull.
_META_SEEDS = Path(__file__).resolve().parents[3] / "server" / "modules" / "meta-ads" / "seeds"
sys.path.insert(0, str(_META_SEEDS))
from load_meta_seed import load_duckdb as _load_meta_duckdb  # noqa: E402

PROJECT_ID = "default"
PLAN_ID = "00000000-0000-4000-8000-000000000022"
VERSION_ID = "00000000-0000-4000-8000-000000000221"
CONNECTOR = "meta-ads"

PLAN_START = date(2026, 3, 1)
PLAN_END = date(2026, 3, 30)
SPEND_START = date(2026, 3, 1)
SPEND_END = date(2026, 3, 10)  # the to-date anchor (as_of_day)

_CENT = Decimal("0.01")

# --- line + mapping fixture --------------------------------------------------
# (line_id, line_key, label, channel, budget, is_plan_only, sort_order)
_LINES = [
    ("00000000-0000-4000-8000-0000000022a1", "line-digital-a", "Digital A",
     "digital", "3000.00", False, 0),
    ("00000000-0000-4000-8000-0000000022a2", "line-digital-b", "Digital B",
     "digital", "1500.00", False, 1),
    ("00000000-0000-4000-8000-0000000022a3", "line-tv", "TV Brand",
     "tv", "5000.00", True, 2),
]

# (line_key, connector, campaign_ref, split_weight)
_MAPPINGS = [
    ("line-digital-a", CONNECTOR, "camp_a_solo", "1.000000"),
    ("line-digital-a", CONNECTOR, "camp_shared", "0.500000"),
    ("line-digital-b", CONNECTOR, "camp_shared", "0.500000"),
    # line-tv has NO mapping (plan-only).
]

# Real daily spend per campaign over SPEND_START..SPEND_END (EUR, so cost == spend).
_CAMPAIGN_SPEND = {
    "camp_a_solo": 100.0,
    "camp_shared": 50.0,
}


def _compute_spread(budget: Decimal, start: date, end: date) -> list[tuple[date, Decimal]]:
    """Cent-exact linear spread (mirrors mediaplan_store.compute_spread)."""
    n_days = (end - start).days + 1
    budget_cents = int((budget / _CENT).to_integral_value())
    base, remainder = divmod(budget_cents, n_days)
    out: list[tuple[date, Decimal]] = []
    for i in range(n_days):
        cents = base + (1 if i < remainder else 0)
        out.append((start + timedelta(days=i), Decimal(cents) * _CENT))
    return out


def _seed_meta_cost(duckdb_path: str) -> int:
    """Land the campaign cost rows into raw_meta_ads_daily (EUR, campaign grain)."""
    rows: list[dict] = []
    day = SPEND_START
    while day <= SPEND_END:
        for campaign_ref, spend in _CAMPAIGN_SPEND.items():
            rows.append(
                {
                    "date": day.isoformat(),
                    "data_level": "CAMPAIGN",
                    "campaign_id": campaign_ref,
                    "campaign_name": campaign_ref,
                    "adset_id": None,
                    "adset_name": None,
                    "ad_id": None,
                    "creative_id": None,
                    "spend": spend,
                    "impressions": 1000,
                    "clicks": 10,
                    "conversions": 1,
                    "project_id": PROJECT_ID,
                    # EUR so fact cost == spend exactly (EUR->EUR rate 1.0).
                    "cost_source_currency": "EUR",
                }
            )
        day += timedelta(days=1)
    loaded_at = datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")
    return _load_meta_duckdb(rows, "pull_plan_22_4", loaded_at, duckdb_path, project_id=PROJECT_ID)


_MIRROR_DDL = """
CREATE SCHEMA IF NOT EXISTS mirror;

CREATE TABLE IF NOT EXISTS mirror.media_plans (
    id VARCHAR, project_id VARCHAR, name VARCHAR, currency VARCHAR,
    created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP, archived_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mirror.media_plan_versions (
    id VARCHAR, plan_id VARCHAR, version_number INTEGER, status VARCHAR,
    is_active BOOLEAN, source_note VARCHAR, created_by VARCHAR, created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS mirror.media_plan_lines (
    id VARCHAR, version_id VARCHAR, line_key VARCHAR, label VARCHAR, channel VARCHAR,
    start_date DATE, end_date DATE, budget DECIMAL(14,2), buy_mode VARCHAR,
    is_plan_only BOOLEAN, sort_order INTEGER
);
CREATE TABLE IF NOT EXISTS mirror.plan_allocation_daily (
    version_id VARCHAR, line_id VARCHAR, day DATE, amount DECIMAL(14,2)
);
CREATE TABLE IF NOT EXISTS mirror.plan_line_mappings (
    id VARCHAR, plan_id VARCHAR, line_key VARCHAR, connector VARCHAR,
    campaign_ref VARCHAR, split_weight DECIMAL(7,6), status VARCHAR,
    created_by VARCHAR, created_at TIMESTAMP, updated_at TIMESTAMP
);
"""


def _seed_plan_mirror(duckdb_path: str) -> None:
    """CREATE + INSERT the plan mirror tables (idempotent: delete our fixture first)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_MIRROR_DDL)

        # Idempotent replace of THIS fixture only (never touch other plans' rows).
        con.execute("DELETE FROM mirror.plan_allocation_daily WHERE version_id = ?", [VERSION_ID])
        con.execute("DELETE FROM mirror.media_plan_lines WHERE version_id = ?", [VERSION_ID])
        con.execute("DELETE FROM mirror.plan_line_mappings WHERE plan_id = ?", [PLAN_ID])
        con.execute("DELETE FROM mirror.media_plan_versions WHERE id = ?", [VERSION_ID])
        con.execute("DELETE FROM mirror.media_plans WHERE id = ?", [PLAN_ID])

        con.execute(
            """
            INSERT INTO mirror.media_plans
                (id, project_id, name, currency, created_by, created_at, updated_at, archived_at)
            VALUES (?, ?, 'Plan 22.4', 'EUR', 'seed', now(), now(), NULL)
            """,
            [PLAN_ID, PROJECT_ID],
        )
        con.execute(
            """
            INSERT INTO mirror.media_plan_versions
                (id, plan_id, version_number, status, is_active, source_note,
                 created_by, created_at)
            VALUES (?, ?, 1, 'published', TRUE, 'seed', 'seed', now())
            """,
            [VERSION_ID, PLAN_ID],
        )

        for line_id, line_key, label, channel, budget, is_plan_only, sort_order in _LINES:
            con.execute(
                """
                INSERT INTO mirror.media_plan_lines
                    (id, version_id, line_key, label, channel, start_date, end_date,
                     budget, buy_mode, is_plan_only, sort_order)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                [
                    line_id, VERSION_ID, line_key, label, channel,
                    PLAN_START, PLAN_END, budget, is_plan_only, sort_order,
                ],
            )
            # Materialised daily spread (cent-exact), matching publish_version().
            for day, amount in _compute_spread(Decimal(budget), PLAN_START, PLAN_END):
                con.execute(
                    """
                    INSERT INTO mirror.plan_allocation_daily (version_id, line_id, day, amount)
                    VALUES (?, ?, ?, ?)
                    """,
                    [VERSION_ID, line_id, day, str(amount)],
                )

        for i, (line_key, connector, campaign_ref, weight) in enumerate(_MAPPINGS):
            con.execute(
                """
                INSERT INTO mirror.plan_line_mappings
                    (id, plan_id, line_key, connector, campaign_ref, split_weight,
                     status, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'active', 'seed', now(), now())
                """,
                [
                    f"00000000-0000-4000-8000-0000000022b{i}",
                    PLAN_ID, line_key, connector, campaign_ref, weight,
                ],
            )
    finally:
        con.close()


def run(duckdb_path: str) -> None:
    n_cost = _seed_meta_cost(duckdb_path)
    _seed_plan_mirror(duckdb_path)
    print(
        "seed_plan_mirror OK  "
        f"plan={PLAN_ID}  lines={len(_LINES)}  mappings={len(_MAPPINGS)}  "
        f"meta_cost_rows={n_cost}  -> {duckdb_path}"
    )


def main() -> None:
    default_duckdb = os.environ.get("TOOROW_DUCKDB_PATH", "")
    parser = argparse.ArgumentParser(
        description="Seed the Story 22.4 plan mirror + fact cost rows into a DuckDB file"
    )
    parser.add_argument(
        "--duckdb-path", default=default_duckdb, help="DuckDB file path (or TOOROW_DUCKDB_PATH)"
    )
    args = parser.parse_args()
    if not args.duckdb_path:
        raise SystemExit("ERROR: --duckdb-path (or TOOROW_DUCKDB_PATH) is required")
    run(args.duckdb_path)


if __name__ == "__main__":
    main()
