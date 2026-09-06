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

STORY 61.4 (AI-266) ADDS TWO MORE PLANS, and they exist to be REFUSED.

Until 61.4 this file seeded one plan whose currency, whose Project's reporting
currency and whose FX target were all EUR. Every currency question therefore had
the same answer, so the marts could label an amount with the wrong currency and
no fixture could tell. The two plans below are the discriminants:

  * PLAN_ID_FX  -- currency USD, on a Project whose spend is converted into EUR.
      Both amounts are real and each keeps its own currency; NOTHING COMPOSED of
      the two is produced. `currency` is NULL, `money_gap_code` is
      `plan_currency_mismatch`, and consumed_pct / pace / remaining_budget are
      NULL. Before 61.4 this plan would have rendered a consumed_pct made of a
      USD budget and a EUR spend, labelled "USD".
  * PLAN_ID_GAP -- one line whose ventilation draws on a campaign billed in JPY,
      for which dbt/seeds/fx_rates.csv carries NO rate (it holds USD->EUR and
      EUR->EUR, and that is the whole file). `fx_convert_at_read` yields NULL, and
      before 61.4 `SUM()` skipped that NULL and the line read as spending less
      than it did -- which is what made `mediaplan_alerts` fire a
      `mediaplan_pace_underdelivery` over a missing exchange rate. The plan
      carries a SECOND, healthy line so the plan-wide as_of_day is set and the
      withholding is measured rather than hidden behind "no spend yet", and the
      channel rollup of the two is the discriminant: it must state no actual at
      all rather than the healthy line's spend wearing the channel's name.

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
#: Story 61.4: the plan whose currency is NOT the one its spend was converted into.
PLAN_ID_FX = "00000000-0000-4000-8000-000000000023"
VERSION_ID_FX = "00000000-0000-4000-8000-000000000231"
#: Story 61.4: the plan one of whose campaigns has no resolvable exchange rate.
PLAN_ID_GAP = "00000000-0000-4000-8000-000000000024"
VERSION_ID_GAP = "00000000-0000-4000-8000-000000000241"
CONNECTOR = "meta-ads"

#: The Project's confirmed Money Policy, mirrored from app.project_money_policy_v.
#: EUR, and it MATCHES dim_project.canonical_currency: the fixture's point is to
#: vary the PLAN's currency against a settled Project, not to vary both at once.
MONEY_POLICY_CURRENCY = "EUR"

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

# --- Story 61.4 fixtures -----------------------------------------------------
# (campaign_ref, daily amount, billing currency). `JPY` has NO row in
# dbt/seeds/fx_rates.csv, which is the whole point: the conversion cannot be
# resolved, so `fact_daily_kpi.value` is NULL and `money_gap_code` is
# `fx_rate_unavailable`.
_FX_CAMPAIGN_SPEND = [
    ("camp_usd_line", 20.0, "EUR"),
]
#: `camp_gap_unrated` bills only over the FIRST HALF of the spend window, on
#: purpose: `line-gap-withheld` then has days it CAN state and days it cannot, so
#: the guard at the line grain (a to-date sum refuses when any of its days
#: withheld) is exercised rather than merely written. With the unrated campaign
#: spending every day, every day of the line is withheld and the daily guard alone
#: would carry the whole proof.
_GAP_UNRATED_LAST_DAY = date(2026, 3, 5)
_GAP_CAMPAIGN_SPEND = [
    ("camp_gap_unrated", 30.0, "JPY"),
    ("camp_gap_rated", 10.0, "EUR"),
    ("camp_gap_healthy", 10.0, "EUR"),
]

# PLAN_ID_FX: one line, in USD, on a Project reporting in EUR.
_FX_LINES = [
    ("00000000-0000-4000-8000-0000000023a1", "line-usd", "Search USD",
     "search", "1000.00", False, 0),
]
_FX_MAPPINGS = [("line-usd", CONNECTOR, "camp_usd_line", "1.000000")]

# PLAN_ID_GAP: two lines of the SAME channel, so the channel rollup is the
# discriminant. `line-gap-withheld` draws on the unrated campaign and can state no
# actual; `line-gap-healthy` spends 10/j against an allocation of 10/j, so its own
# pace is exactly 0 and any pace the channel shows can only come from the rollup
# having skipped the withheld line.
_GAP_LINES = [
    ("00000000-0000-4000-8000-0000000024a1", "line-gap-withheld", "Display withheld",
     "display", "600.00", False, 0),
    ("00000000-0000-4000-8000-0000000024a2", "line-gap-healthy", "Display healthy",
     "display", "300.00", False, 1),
]
_GAP_MAPPINGS = [
    ("line-gap-withheld", CONNECTOR, "camp_gap_unrated", "1.000000"),
    ("line-gap-withheld", CONNECTOR, "camp_gap_rated", "1.000000"),
    ("line-gap-healthy", CONNECTOR, "camp_gap_healthy", "1.000000"),
]


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


def _cost_row(day: date, campaign_ref: str, spend: float, currency: str) -> dict:
    return {
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
        "cost_source_currency": currency,
    }


def _seed_meta_cost(duckdb_path: str) -> int:
    """Land the campaign cost rows into raw_meta_ads_daily (campaign grain).

    The 22.4 campaigns are billed in EUR so fact cost == spend exactly (EUR->EUR
    rate 1.0) and the story's worked example stays arithmetic. The 61.4 campaigns
    carry their own currencies, one of which has no rate at all.
    """
    rows: list[dict] = []
    day = SPEND_START
    while day <= SPEND_END:
        for campaign_ref, spend in _CAMPAIGN_SPEND.items():
            rows.append(_cost_row(day, campaign_ref, spend, "EUR"))
        for campaign_ref, spend, currency in _FX_CAMPAIGN_SPEND + _GAP_CAMPAIGN_SPEND:
            if campaign_ref == "camp_gap_unrated" and day > _GAP_UNRATED_LAST_DAY:
                continue
            rows.append(_cost_row(day, campaign_ref, spend, currency))
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
-- Story 61.4: app.project_money_policy_v (migration 148). Declared here so this
-- script still runs standalone; when the shared fixture created it first,
-- CREATE TABLE IF NOT EXISTS is a no-op and the shapes must therefore agree --
-- see seed_all_connectors.create_mirror, which is the ONE definition.
CREATE TABLE IF NOT EXISTS mirror.project_money_policy (
    project_id VARCHAR, money_policy_rule_set_id VARCHAR,
    money_policy_version_id VARCHAR, money_policy_content_hash VARCHAR,
    reporting_currency VARCHAR, reporting_currency_minor_unit BIGINT,
    money_rounding VARCHAR
);
"""


def _seed_one_plan(
    con,
    *,
    plan_id: str,
    version_id: str,
    plan_name: str,
    currency: str,
    lines: list[tuple],
    mappings: list[tuple],
    mapping_prefix: str,
) -> None:
    """CREATE + INSERT one plan (idempotent: delete its own rows first)."""
    # Idempotent replace of THIS plan only (never touch other plans' rows).
    con.execute("DELETE FROM mirror.plan_allocation_daily WHERE version_id = ?", [version_id])
    con.execute("DELETE FROM mirror.media_plan_lines WHERE version_id = ?", [version_id])
    con.execute("DELETE FROM mirror.plan_line_mappings WHERE plan_id = ?", [plan_id])
    con.execute("DELETE FROM mirror.media_plan_versions WHERE id = ?", [version_id])
    con.execute("DELETE FROM mirror.media_plans WHERE id = ?", [plan_id])

    con.execute(
        """
        INSERT INTO mirror.media_plans
            (id, project_id, name, currency, created_by, created_at, updated_at, archived_at)
        VALUES (?, ?, ?, ?, 'seed', now(), now(), NULL)
        """,
        [plan_id, PROJECT_ID, plan_name, currency],
    )
    con.execute(
        """
        INSERT INTO mirror.media_plan_versions
            (id, plan_id, version_number, status, is_active, source_note,
             created_by, created_at)
        VALUES (?, ?, 1, 'published', TRUE, 'seed', 'seed', now())
        """,
        [version_id, plan_id],
    )

    for line_id, line_key, label, channel, budget, is_plan_only, sort_order in lines:
        con.execute(
            """
            INSERT INTO mirror.media_plan_lines
                (id, version_id, line_key, label, channel, start_date, end_date,
                 budget, buy_mode, is_plan_only, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            [
                line_id, version_id, line_key, label, channel,
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
                [version_id, line_id, day, str(amount)],
            )

    for i, (line_key, connector, campaign_ref, weight) in enumerate(mappings):
        con.execute(
            """
            INSERT INTO mirror.plan_line_mappings
                (id, plan_id, line_key, connector, campaign_ref, split_weight,
                 status, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'active', 'seed', now(), now())
            """,
            [f"{mapping_prefix}{i}", plan_id, line_key, connector, campaign_ref, weight],
        )


def _seed_money_policy(con) -> None:
    """The Project's CONFIRMED Money Policy -- the authority a label needs.

    A Project with no row here has not decided, and the marts say so
    (`money_policy_unconfirmed`) rather than assuming. This fixture HAS decided,
    so the three plans below are measured against a settled Project and the only
    thing varying is what the story varies.
    """
    con.execute("DELETE FROM mirror.project_money_policy WHERE project_id = ?", [PROJECT_ID])
    con.execute(
        """
        INSERT INTO mirror.project_money_policy
            (project_id, money_policy_rule_set_id, money_policy_version_id,
             money_policy_content_hash, reporting_currency,
             reporting_currency_minor_unit, money_rounding)
        VALUES (?, 'rs_money_EXAMPLE_LOCAL', 'rsv_money_EXAMPLE_LOCAL',
                'sha256:EXAMPLE_LOCAL', ?, 2, 'half_even')
        """,
        [PROJECT_ID, MONEY_POLICY_CURRENCY],
    )


def _seed_plan_mirror(duckdb_path: str) -> None:
    """CREATE + INSERT the plan mirror tables (idempotent: delete our fixture first)."""
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(duckdb_path)
    try:
        con.execute(_MIRROR_DDL)
        _seed_money_policy(con)
        _seed_one_plan(
            con, plan_id=PLAN_ID, version_id=VERSION_ID, plan_name="Plan 22.4",
            currency="EUR", lines=_LINES, mappings=_MAPPINGS,
            mapping_prefix="00000000-0000-4000-8000-0000000022b",
        )
        _seed_one_plan(
            con, plan_id=PLAN_ID_FX, version_id=VERSION_ID_FX,
            plan_name="Plan 61.4 currency divergence", currency="USD",
            lines=_FX_LINES, mappings=_FX_MAPPINGS,
            mapping_prefix="00000000-0000-4000-8000-0000000023b",
        )
        _seed_one_plan(
            con, plan_id=PLAN_ID_GAP, version_id=VERSION_ID_GAP,
            plan_name="Plan 61.4 unresolved rate", currency="EUR",
            lines=_GAP_LINES, mappings=_GAP_MAPPINGS,
            mapping_prefix="00000000-0000-4000-8000-0000000024b",
        )
    finally:
        con.close()


def run(duckdb_path: str) -> None:
    n_cost = _seed_meta_cost(duckdb_path)
    _seed_plan_mirror(duckdb_path)
    print(
        "seed_plan_mirror OK  "
        f"plans=3 ({PLAN_ID}, {PLAN_ID_FX}, {PLAN_ID_GAP})  "
        f"lines={len(_LINES) + len(_FX_LINES) + len(_GAP_LINES)}  "
        f"mappings={len(_MAPPINGS) + len(_FX_MAPPINGS) + len(_GAP_MAPPINGS)}  "
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
