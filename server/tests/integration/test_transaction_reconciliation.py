"""Epic 17, story 17.4 -- transaction_reconciliation mart VALUE tests.

transaction_reconciliation[_daily] is a READ-ONLY set of views over
stg_shopify_orders_daily + stg_ga4_transactions_daily that MEASURE the Shopify x GA4 sales
overlap via transaction_id (doc/synthese §3.2). This file proves the mart's VALUE SEMANTICS
on a REAL DuckDB, WITHOUT running dbt inside pytest, mirroring the two mart .sql files
EXACTLY:

  * FULL OUTER JOIN by (project_id, date, transaction_id) -> match_status in
    {matched, shopify_only, ga4_only}, computed by hand on a concrete day.
  * coverage_rate = matched_count / NULLIF(shopify_orders_with_txn, 0).
  * F-7 blind spot: Shopify orders WITHOUT a transaction_id are counted separately
    (shopify_orders_without_txn) and EXCLUDED from the coverage denominator.
  * coverage_rate NULL when zero reconcilable Shopify orders (honest degrade).
  * NULL-id GA4 transactions are unreconcilable and excluded from the join.
  * NON-INTERFERENCE with dedup_estimate: the transaction grain is NOT in fact_daily_kpi;
    building the reconciliation marts leaves the dedup_estimate numbers unchanged.
  * seeded_con: the correlated generator yields matched ~= 85% of reconcilable orders,
    with a few ga4_only / shopify_only (magnitudes SUBSET of the Shopify source).

ASCII-only (AI-03). No live GA4 / Shopify / Postgres required.
"""

from __future__ import annotations

import importlib.util
import os
import random
from datetime import date
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

duckdb = pytest.importorskip(
    "duckdb", reason="duckdb not installed -- transaction reconciliation skipped"
)

_MODULES_DIR = Path(__file__).resolve().parents[3] / "server" / "modules"


def _load(module_path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, str(module_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


def _load_ga4_seed():
    return _load(_MODULES_DIR / "google-analytics" / "seeds" / "generate_seed.py",
                 "_ga4_gen_txn")


def _load_shopify_seed():
    return _load(_MODULES_DIR / "shopify" / "seeds" / "generate_shopify_seed.py",
                 "_shopify_gen_txn")


# ---------------------------------------------------------------------------
# Inlined staging + mart views mirroring the .sql files EXACTLY (ref -> local names).
# ---------------------------------------------------------------------------

# stg_shopify_orders_daily (grain project,date,order; carries transaction_id nullable).
# We inline the SELECT columns the reconciliation marts read (date, order_id,
# transaction_id, revenue, orders_count, pull_id, project_id).
_STG_SHOPIFY = """
CREATE OR REPLACE TEMP VIEW stg_shopify AS
SELECT date, order_id, transaction_id, revenue, orders_count, pull_id, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, order_id ORDER BY pull_id DESC
    ) AS rn
    FROM raw_shopify_orders
) WHERE rn = 1
"""

_STG_GA4_TXN = """
CREATE OR REPLACE TEMP VIEW stg_ga4_txn AS
SELECT date, transaction_id, conversions, purchase_revenue, pull_id, project_id
FROM (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY project_id, date, transaction_id ORDER BY pull_id DESC
    ) AS rn
    FROM raw_ga4_transactions_daily
) WHERE rn = 1
"""

# transaction_reconciliation.sql (detail grain), mirrored EXACTLY.
_RECON_DETAIL = """
CREATE OR REPLACE TEMP VIEW transaction_reconciliation AS
WITH shopify_txn AS (
    SELECT project_id, date, transaction_id,
           SUM(revenue) AS shopify_revenue, SUM(orders_count) AS shopify_orders,
           MAX(pull_id) AS shopify_pull_id
    FROM stg_shopify WHERE transaction_id IS NOT NULL
    GROUP BY project_id, date, transaction_id
),
ga4_txn AS (
    SELECT project_id, date, transaction_id,
           SUM(conversions) AS ga4_conversions, SUM(purchase_revenue) AS ga4_revenue,
           MAX(pull_id) AS ga4_pull_id
    FROM stg_ga4_txn WHERE transaction_id IS NOT NULL
    GROUP BY project_id, date, transaction_id
)
SELECT
    COALESCE(s.project_id, g.project_id) AS project_id,
    COALESCE(s.date, g.date) AS date,
    COALESCE(s.transaction_id, g.transaction_id) AS transaction_id,
    CASE
        WHEN s.transaction_id IS NOT NULL AND g.transaction_id IS NOT NULL THEN 'matched'
        WHEN s.transaction_id IS NOT NULL THEN 'shopify_only'
        ELSE 'ga4_only'
    END AS match_status,
    s.shopify_revenue, s.shopify_orders, g.ga4_conversions, g.ga4_revenue,
    'measured_reconciliation' AS estimate_label, s.shopify_pull_id, g.ga4_pull_id
FROM shopify_txn s
FULL OUTER JOIN ga4_txn g
    ON g.project_id = s.project_id AND g.date = s.date
   AND g.transaction_id = s.transaction_id
"""

# transaction_reconciliation_daily.sql, mirrored EXACTLY.
_RECON_DAILY = """
CREATE OR REPLACE TEMP VIEW transaction_reconciliation_daily AS
WITH detail AS (SELECT * FROM transaction_reconciliation),
agg AS (
    SELECT project_id, date,
        SUM(CASE WHEN match_status IN ('matched','shopify_only') THEN 1 ELSE 0 END)
            AS shopify_orders_with_txn,
        SUM(CASE WHEN match_status IN ('matched','ga4_only') THEN 1 ELSE 0 END)
            AS ga4_transactions,
        SUM(CASE WHEN match_status = 'matched' THEN 1 ELSE 0 END) AS matched_count,
        MAX(shopify_pull_id) AS shopify_pull_id, MAX(ga4_pull_id) AS ga4_pull_id
    FROM detail GROUP BY project_id, date
),
shopify_no_txn AS (
    SELECT project_id, date,
        SUM(CASE WHEN transaction_id IS NULL THEN orders_count ELSE 0 END)
            AS shopify_orders_without_txn
    FROM stg_shopify GROUP BY project_id, date
)
SELECT a.project_id, a.date, a.shopify_orders_with_txn, a.ga4_transactions,
    a.matched_count,
    a.matched_count::DOUBLE / NULLIF(a.shopify_orders_with_txn, 0) AS coverage_rate,
    COALESCE(n.shopify_orders_without_txn, 0) AS shopify_orders_without_txn,
    'measured_reconciliation' AS method, a.shopify_pull_id, a.ga4_pull_id
FROM agg a
LEFT JOIN shopify_no_txn n ON n.project_id = a.project_id AND n.date = a.date
"""

_SHOPIFY_DDL = """
CREATE TABLE raw_shopify_orders (
    date VARCHAR, order_id VARCHAR, transaction_id VARCHAR, revenue DOUBLE,
    refund_amount DOUBLE, orders_count INTEGER, revenue_source_currency VARCHAR,
    pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""

_GA4_TXN_DDL = """
CREATE TABLE raw_ga4_transactions_daily (
    date VARCHAR, transaction_id VARCHAR, conversions INTEGER, purchase_revenue DOUBLE,
    pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR
)
"""


def _build_views(con):
    con.execute(_STG_SHOPIFY)
    con.execute(_STG_GA4_TXN)
    con.execute(_RECON_DETAIL)
    con.execute(_RECON_DAILY)


# ---------------------------------------------------------------------------
# Hand-authored fixture: one concrete day with known matched / only rows.
# ---------------------------------------------------------------------------


@pytest.fixture()
def hand_con():
    """A single day with:
      Shopify orders: T1(rev 100), T2(rev 50), T3(rev 30 -- shopify_only, not in GA4),
                      T4 with NO txn id (F-7 blind spot).
      GA4 txns: T1(rev 100), T2(rev 50), G9(ga4_only), and one NULL-id GA4 txn (excluded).
    Expected: matched=2 (T1,T2), shopify_only=1 (T3), ga4_only=1 (G9),
              shopify_orders_with_txn=3 (T1,T2,T3), ga4_transactions=3 (T1,T2,G9),
              coverage_rate=2/3, shopify_orders_without_txn=1 (T4).
    """
    con = duckdb.connect(":memory:")
    con.execute(_SHOPIFY_DDL)
    con.execute(_GA4_TXN_DDL)
    d = "2026-06-01"
    con.executemany(
        "INSERT INTO raw_shopify_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (d, "o1", "T1", 100.0, 0.0, 1, "EUR", "pull_shop_1", "2026-06-01T00:00:00Z", "p"),
            (d, "o2", "T2", 50.0, 0.0, 1, "EUR", "pull_shop_1", "2026-06-01T00:00:00Z", "p"),
            (d, "o3", "T3", 30.0, 0.0, 1, "EUR", "pull_shop_1", "2026-06-01T00:00:00Z", "p"),
            # F-7 blind spot: no transaction_id.
            (d, "o4", None, 75.0, 0.0, 1, "EUR", "pull_shop_1", "2026-06-01T00:00:00Z", "p"),
        ],
    )
    con.executemany(
        "INSERT INTO raw_ga4_transactions_daily VALUES (?,?,?,?,?,?,?)",
        [
            (d, "T1", 1, 100.0, "pull_ga4_1", "2026-06-01T00:00:00Z", "p"),
            (d, "T2", 1, 50.0, "pull_ga4_1", "2026-06-01T00:00:00Z", "p"),
            (d, "G9", 1, 42.0, "pull_ga4_1", "2026-06-01T00:00:00Z", "p"),  # ga4_only
            # NULL-id GA4 txn: unreconcilable, must be excluded from the join.
            (d, None, 1, 12.0, "pull_ga4_1", "2026-06-01T00:00:00Z", "p"),
        ],
    )
    _build_views(con)
    try:
        yield con
    finally:
        con.close()


def test_match_status_counts_by_hand(hand_con):
    """Detail grain: matched=2, shopify_only=1, ga4_only=1 (NULL-id GA4 excluded)."""
    rows = hand_con.execute(
        "SELECT match_status, COUNT(*) FROM transaction_reconciliation "
        "GROUP BY match_status ORDER BY match_status"
    ).fetchall()
    counts = dict(rows)
    assert counts.get("matched") == 2, counts
    assert counts.get("shopify_only") == 1, counts
    assert counts.get("ga4_only") == 1, counts


def test_null_id_ga4_txn_excluded_from_join(hand_con):
    """The NULL-id GA4 transaction never appears in the detail view (no join key)."""
    total = hand_con.execute(
        "SELECT COUNT(*) FROM transaction_reconciliation"
    ).fetchone()[0]
    # 2 matched + 1 shopify_only + 1 ga4_only = 4 (the NULL-id GA4 row is excluded).
    assert total == 4, total


def test_coverage_rate_by_hand(hand_con):
    """Daily: coverage_rate = matched / shopify_orders_with_txn = 2/3; blind spot = 1."""
    row = hand_con.execute(
        "SELECT shopify_orders_with_txn, ga4_transactions, matched_count, coverage_rate, "
        "shopify_orders_without_txn, method FROM transaction_reconciliation_daily "
        "WHERE project_id='p' AND date='2026-06-01'"
    ).fetchone()
    with_txn, ga4_txns, matched, coverage, without_txn, method = row
    assert with_txn == 3, "T1,T2,T3 have a txn id"
    assert ga4_txns == 3, "T1,T2,G9 (NULL-id excluded)"
    assert matched == 2, "T1,T2"
    assert coverage == pytest.approx(2.0 / 3.0)
    assert without_txn == 1, "T4 has no txn id (F-7 blind spot, counted separately)"
    assert method == "measured_reconciliation"


def test_blind_spot_excluded_from_coverage_denominator(hand_con):
    """F-7: shopify_orders_without_txn (T4) is NOT in the coverage_rate denominator.

    Denominator is shopify_orders_with_txn=3, NOT total Shopify orders (4). If T4 were
    folded in, coverage would be 2/4=0.5, not 2/3 -- prove it is 2/3."""
    coverage = hand_con.execute(
        "SELECT coverage_rate FROM transaction_reconciliation_daily WHERE date='2026-06-01'"
    ).fetchone()[0]
    assert coverage == pytest.approx(2.0 / 3.0), (
        "the no-txn order must be excluded from the coverage denominator (F-7)"
    )


# ---------------------------------------------------------------------------
# Honest degrade: zero reconcilable Shopify orders -> coverage_rate NULL.
# ---------------------------------------------------------------------------


def test_coverage_null_when_no_reconcilable_orders():
    """A day where every Shopify order lacks a transaction_id -> coverage_rate NULL (never
    a disguised 0). The blind spot is still counted."""
    con = duckdb.connect(":memory:")
    try:
        con.execute(_SHOPIFY_DDL)
        con.execute(_GA4_TXN_DDL)
        d = "2026-06-02"
        con.executemany(
            "INSERT INTO raw_shopify_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                (d, "o1", None, 100.0, 0.0, 1, "EUR", "pull_shop_1", "2026-06-02T00:00:00Z", "p"),
                (d, "o2", None, 50.0, 0.0, 1, "EUR", "pull_shop_1", "2026-06-02T00:00:00Z", "p"),
            ],
        )
        # A GA4 transaction exists but has no Shopify counterpart (ga4_only) -- still, no
        # reconcilable Shopify order -> coverage NULL.
        con.execute(
            "INSERT INTO raw_ga4_transactions_daily VALUES "
            "('2026-06-02','GX',1,20.0,'pull_ga4_1','2026-06-02T00:00:00Z','p')"
        )
        _build_views(con)
        row = con.execute(
            "SELECT shopify_orders_with_txn, matched_count, coverage_rate, "
            "shopify_orders_without_txn FROM transaction_reconciliation_daily "
            "WHERE date='2026-06-02'"
        ).fetchone()
        with_txn, matched, coverage, without_txn = row
        assert with_txn == 0
        assert matched == 0
        assert coverage is None, "coverage_rate must be NULL, never a disguised 0 (AD-9)"
        assert without_txn == 2, "both no-txn orders counted as the blind spot"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# seeded_con: correlated generator -> matched ~= 85% of reconcilable orders,
# magnitudes a SUBSET of the Shopify source (leçon 16.1).
# ---------------------------------------------------------------------------


@pytest.fixture()
def seeded_con():
    """DuckDB seeded from the correlated GA4-transactions + Shopify generators over 90
    days ending 2026-06-01 (AI-45: many contiguous days)."""
    ga4_gen = _load_ga4_seed()
    shopify_gen = _load_shopify_seed()

    end_date = date(2026, 6, 1)
    shopify_rows = shopify_gen.generate_rows(days=90, end_date=end_date)
    ga4_txn_rows = ga4_gen.generate_transactions_rows(
        end_date=end_date, rng=random.Random(4242), shopify_rows=shopify_rows
    )

    con = duckdb.connect(":memory:")
    con.execute(_SHOPIFY_DDL)
    con.execute(_GA4_TXN_DDL)
    con.executemany(
        "INSERT INTO raw_shopify_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["order_id"], r["transaction_id"], float(r["revenue"]),
                float(r["refund_amount"]), int(r["orders_count"]),
                r["revenue_source_currency"], "pull_shop_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in shopify_rows
        ],
    )
    con.executemany(
        "INSERT INTO raw_ga4_transactions_daily VALUES (?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["transaction_id"], int(r["conversions"]),
                float(r["purchase_revenue"]), "pull_ga4_1", "2026-06-01T00:00:00Z", "default",
            )
            for r in ga4_txn_rows
        ],
    )
    _build_views(con)
    try:
        yield con
    finally:
        con.close()


def test_seeded_coverage_is_realistic(seeded_con):
    """Overall coverage_rate across the seed sits in a realistic band around ~85%.

    matched must be a SUBSET of the reconcilable Shopify orders (never more) -- coverage
    cannot exceed 1.0 (leçon 16.1: an overlap is a subset of its source)."""
    con = seeded_con
    row = con.execute(
        "SELECT SUM(matched_count), SUM(shopify_orders_with_txn) "
        "FROM transaction_reconciliation_daily"
    ).fetchone()
    matched, with_txn = row
    assert with_txn and with_txn > 0
    overall = matched / with_txn
    # ~85% recovery with independent sampling noise: assert a generous but bounded band.
    assert 0.75 <= overall <= 0.95, f"overall coverage {overall:.3f} outside realistic band"
    assert matched <= with_txn, "matched can NEVER exceed reconcilable orders (subset rule)"


def test_seeded_has_all_three_match_statuses(seeded_con):
    """The correlated seed produces matched, shopify_only AND ga4_only rows (realistic)."""
    statuses = {
        r[0]
        for r in seeded_con.execute(
            "SELECT DISTINCT match_status FROM transaction_reconciliation"
        ).fetchall()
    }
    assert {"matched", "shopify_only", "ga4_only"} <= statuses, statuses


def test_seeded_grain_unique_both_levels(seeded_con):
    """AI-05 grain uniqueness at BOTH levels: detail (project,date,txn) + daily (project,date)."""
    con = seeded_con
    detail_total = con.execute(
        "SELECT COUNT(*) FROM transaction_reconciliation"
    ).fetchone()[0]
    detail_distinct = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT project_id, date, transaction_id "
        "FROM transaction_reconciliation)"
    ).fetchone()[0]
    assert detail_total == detail_distinct, "detail grain not unique"

    daily_total = con.execute(
        "SELECT COUNT(*) FROM transaction_reconciliation_daily"
    ).fetchone()[0]
    daily_distinct = con.execute(
        "SELECT COUNT(*) FROM (SELECT DISTINCT project_id, date "
        "FROM transaction_reconciliation_daily)"
    ).fetchone()[0]
    assert daily_total == daily_distinct, "daily grain not unique"


# ---------------------------------------------------------------------------
# Non-interference with dedup_estimate: the transaction grain is NOT in fact_daily_kpi.
# Building the reconciliation marts leaves the dedup_estimate numbers UNCHANGED.
# ---------------------------------------------------------------------------

_DEDUP_MINIMAL = """
CREATE OR REPLACE TEMP VIEW dedup_estimate AS
WITH prefs AS (
    SELECT project_id, verification_source_type FROM dim_project
    WHERE verification_source_type IS NOT NULL
),
conv AS (
    SELECT project_id, date, connector, breakdown_dimension, value
    FROM fact_daily_kpi WHERE metric = 'conversions'
),
claimed AS (
    SELECT c.project_id, c.date, c.connector AS channel_connector,
           SUM(c.value) AS claimed_conversions
    FROM conv c
    JOIN conversion_claimants cl ON cl.connector = c.connector
       AND cl.canonical_breakdown_dimension = c.breakdown_dimension
    JOIN prefs p ON p.project_id = c.project_id
    GROUP BY c.project_id, c.date, c.connector
),
verified_shopify AS (
    SELECT project_id, date, SUM(value) AS verified_total
    FROM fact_daily_kpi
    WHERE connector = 'shopify' AND metric = 'orders_count'
      AND breakdown_dimension = 'day_total'
    GROUP BY project_id, date
)
SELECT cl.project_id, cl.date, cl.channel_connector, cl.claimed_conversions,
       v.verified_total,
       cl.claimed_conversions / NULLIF(v.verified_total, 0) AS duplication_rate
FROM claimed cl
JOIN verified_shopify v ON v.project_id = cl.project_id AND v.date = cl.date
"""


def test_dedup_estimate_unchanged_by_reconciliation():
    """The reconciliation marts read ONLY the staging models -- they add NO row to
    fact_daily_kpi. dedup_estimate (which reads fact_daily_kpi) yields the SAME numbers
    whether or not the transaction reconciliation views exist. Proven by computing
    dedup_estimate, then building the reconciliation views over the SAME connection, then
    recomputing -- byte-identical."""
    con = duckdb.connect(":memory:")
    try:
        # fact_daily_kpi + dim_project + claimants for dedup_estimate (shopify truth).
        con.execute(
            "CREATE TABLE fact_daily_kpi (project_id VARCHAR, date VARCHAR, connector VARCHAR,"
            " metric VARCHAR, breakdown_dimension VARCHAR, breakdown_value VARCHAR, value DOUBLE)"
        )
        con.execute(
            "CREATE TABLE dim_project (project_id VARCHAR, verification_source_type VARCHAR)"
        )
        con.execute(
            "CREATE TABLE conversion_claimants (connector VARCHAR, "
            "canonical_breakdown_dimension VARCHAR)"
        )
        con.execute("INSERT INTO conversion_claimants VALUES ('meta-ads','ad_id')")
        con.execute("INSERT INTO dim_project VALUES ('p','shopify')")
        con.executemany(
            "INSERT INTO fact_daily_kpi VALUES (?,?,?,?,?,?,?)",
            [
                ("p", "2026-06-01", "shopify", "orders_count", "day_total", "all", 50.0),
                ("p", "2026-06-01", "meta-ads", "conversions", "ad_id", "ad_1", 90.0),
            ],
        )
        con.execute(_DEDUP_MINIMAL)
        before = con.execute(
            "SELECT project_id, date, channel_connector, claimed_conversions, "
            "verified_total, duplication_rate FROM dedup_estimate ORDER BY 1,2,3"
        ).fetchall()

        # Now build the reconciliation marts over the SAME connection (transaction grain).
        con.execute(_SHOPIFY_DDL)
        con.execute(_GA4_TXN_DDL)
        con.execute(
            "INSERT INTO raw_shopify_orders VALUES "
            "('2026-06-01','o1','T1',100.0,0.0,1,'EUR','pull_shop_1','x','p')"
        )
        con.execute(
            "INSERT INTO raw_ga4_transactions_daily VALUES "
            "('2026-06-01','T1',1,100.0,'pull_ga4_1','x','p')"
        )
        _build_views(con)
        # dedup_estimate is unaffected: recompute and compare.
        con.execute(_DEDUP_MINIMAL)
        after = con.execute(
            "SELECT project_id, date, channel_connector, claimed_conversions, "
            "verified_total, duplication_rate FROM dedup_estimate ORDER BY 1,2,3"
        ).fetchall()

        assert before == after, (
            "building the transaction reconciliation marts changed dedup_estimate -- the "
            "transaction grain must NOT interfere (it is not in fact_daily_kpi)"
        )
        # And the reconciliation itself is queryable in parallel.
        recon = con.execute(
            "SELECT matched_count FROM transaction_reconciliation_daily WHERE date='2026-06-01'"
        ).fetchone()[0]
        assert recon == 1
    finally:
        con.close()
