"""Integration seam test: Shopify seed -> DuckDB mart -> get_shopify_report envelope.

Story 15.4 (Epic 15), AI-56 (seam test), AI-54 (fixture = real payload).

This is the FR2 proof for the Shopify pipeline through the MCP tool seam:
  1. Generate multi-day Shopify orders via the SAME generator that feeds the seed
     loader / conformance fixture (generate_shopify_seed.generate_rows) -- AI-54.
  2. Land them in raw_shopify_orders + materialize the shopify fact_daily_kpi block
     (the exact SQL from dbt/models/marts/fact_daily_kpi.sql: day-total aggregation).
  3. Call the module's get_shopify_report tool through the FastMCP tool layer
     (FastMCPTransport, the established in-process MCP seam convention) and assert on
     VALUES, not just HTTP 200 (AI-56).
  4. Prove the story invariants on real mart values:
     - revenue is GROSS (refunds NOT subtracted): mart revenue == SUM(gross revenue).
     - refund_amount is a SEPARATE positive metric (dedicated column decision 15.4).
     - orders_count == number of orders that day (a distinct metric, NOT 'conversions').
     - breakdown_dimension is the bare 'day_total' (transaction_id is NOT a partition).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parents[3]
SHOPIFY_DIR = REPO_ROOT / "server" / "modules" / "shopify"


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


# The shopify fact block, transcribed 1:1 from fact_daily_kpi.sql (day-total aggregation).
# Materialized directly here so the seam does not require the full dbt run (same technique
# as test_gsc_seed_to_mart.py). GROUP BY project_id, date -> one row per (metric, day).
_SHOPIFY_MART_SQL = """
CREATE SCHEMA IF NOT EXISTS main_marts;
CREATE TABLE IF NOT EXISTS main_marts.fact_daily_kpi AS
SELECT project_id, date, 'shopify' AS connector, 'revenue' AS metric,
       'day_total' AS breakdown_dimension, 'all' AS breakdown_value,
       SUM(CAST(revenue AS DOUBLE)) AS value,
       MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
FROM raw_shopify_orders GROUP BY project_id, date
UNION ALL
SELECT project_id, date, 'shopify' AS connector, 'refund_amount' AS metric,
       'day_total' AS breakdown_dimension, 'all' AS breakdown_value,
       SUM(CAST(refund_amount AS DOUBLE)) AS value,
       MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
FROM raw_shopify_orders GROUP BY project_id, date
UNION ALL
SELECT project_id, date, 'shopify' AS connector, 'orders_count' AS metric,
       'day_total' AS breakdown_dimension, 'all' AS breakdown_value,
       SUM(CAST(orders_count AS DOUBLE)) AS value,
       MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
FROM raw_shopify_orders GROUP BY project_id, date
"""


def _seed_mart(db_path: str, rows: list[dict]) -> None:
    import duckdb

    con = duckdb.connect(db_path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_shopify_orders (
            date VARCHAR, order_id VARCHAR, transaction_id VARCHAR,
            revenue DOUBLE, refund_amount DOUBLE, orders_count INTEGER,
            revenue_source_currency VARCHAR, pull_id VARCHAR, loaded_at VARCHAR,
            project_id VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO raw_shopify_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r["order_id"], r.get("transaction_id"),
                float(r["revenue"]), float(r["refund_amount"]), int(r["orders_count"]),
                r.get("revenue_source_currency", "EUR"),
                "pull_shop_seam_001", "2026-07-01T00:00:00Z", "default",
            )
            for r in rows
        ],
    )
    con.execute(_SHOPIFY_MART_SQL)
    con.close()


@pytest.mark.anyio
async def test_shopify_report_seam_asserts_values(tmp_path, monkeypatch):
    """Seam: generator -> mart -> get_shopify_report tool, asserting on values (AI-56)."""
    from fastmcp.client import Client, FastMCPTransport

    gen = _import("shopify_seed_gen", SHOPIFY_DIR / "seeds" / "generate_shopify_seed.py")
    connector = _import("shopify_conn_seam", SHOPIFY_DIR / "connector.py")

    from datetime import date as _date

    end = _date(2026, 7, 15)
    rows = gen.generate_rows(days=14, end_date=end)  # multi-day (AI-56)
    assert rows, "generator must produce rows"

    db_path = str(tmp_path / "shop_seam.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    _seed_mart(db_path, rows)

    # Expected values computed directly from the generator rows (independent of the mart SQL).
    from collections import defaultdict

    gross_rev = defaultdict(float)
    refunds = defaultdict(float)
    counts = defaultdict(int)
    for r in rows:
        gross_rev[r["date"]] += float(r["revenue"])
        refunds[r["date"]] += float(r["refund_amount"])
        counts[r["date"]] += int(r["orders_count"])

    date_from = min(r["date"] for r in rows)
    date_to = max(r["date"] for r in rows)

    async with Client(FastMCPTransport(connector.mcp_app)) as client:
        result = await client.call_tool(
            "get_shopify_report",
            {
                "project_id": "default",
                "report_profile": "orders_daily",
                "date_from": date_from,
                "date_to": date_to,
            },
        )

    envelope = result.structured_content or result.data
    assert envelope["schema_version"] == "1"
    metrics = envelope["data"]["metrics"]
    assert set(metrics) == {"revenue", "refund_amount", "orders_count"}

    # The mart aggregates across the window: the tool SUMs over the date range per
    # breakdown, so the returned single 'day_total' value equals the window total.
    def _series_total(metric: str) -> float:
        entries = metrics[metric]
        # All entries carry breakdown_dimension='day_total' -- assert no partition leaked.
        assert all(e["breakdown_dimension"] == "day_total" for e in entries)
        return sum(e["value"] for e in entries)

    window_gross = sum(gross_rev.values())
    window_refunds = sum(refunds.values())
    window_orders = sum(counts.values())

    # revenue is GROSS -- refunds NOT subtracted (decision de story 15.4).
    assert _series_total("revenue") == pytest.approx(window_gross, rel=1e-6)
    # refund_amount is a SEPARATE positive metric.
    assert _series_total("refund_amount") == pytest.approx(window_refunds, rel=1e-6)
    assert window_refunds > 0, "seed must carry at least one refund to exercise the column"
    # orders_count is a distinct metric equal to the number of orders (NOT conversions).
    assert _series_total("orders_count") == pytest.approx(float(window_orders), rel=1e-6)
    assert "conversions" not in metrics, "orders_count must NOT be mapped onto conversions"


def test_staging_fx_normalizes_non_eur_revenue(tmp_path):
    """review-15-4 F-4: le pattern FX 4.2 est prouvé sur une devise NON-EUR.

    Exécute le VRAI SQL de staging (stg_shopify_orders_daily.sql, substitutions Jinja
    minimales) sur une ligne EUR (identité) et une ligne USD avec fx_rates USD->EUR 0.9 :
    le montant normalisé DOIT différer du montant source pour USD, et les valeurs source
    doivent rester préservées (re-dérivables, AD-6).
    """
    import duckdb

    stg_sql = (SHOPIFY_DIR / "dbt" / "staging" / "stg_shopify_orders_daily.sql").read_text(
        encoding="utf-8"
    )
    stg_sql = (
        stg_sql
        .replace("{{ source('raw_shopify', 'raw_shopify_orders') }}", "raw_shopify_orders")
        .replace("{{ ref('dim_project') }}", "dim_project")
        .replace("{{ ref('fx_rates') }}", "fx_rates")
        # Story 13.2: FX conflict resolution source (AD-6). Empty table -> no override.
        .replace(
            "{{ source('mirror', 'fx_conflict_resolutions') }}",
            "fx_conflict_resolutions",
        )
    )
    # Strip the leading '-- ...' comment lines so only the SQL body remains executable.
    body = "\n".join(
        line for line in stg_sql.splitlines() if not line.strip().startswith("--")
    )

    con = duckdb.connect(str(tmp_path / "fx.duckdb"))
    con.execute(
        """
        CREATE TABLE raw_shopify_orders (
            date VARCHAR, order_id VARCHAR, transaction_id VARCHAR,
            revenue DOUBLE, refund_amount DOUBLE, orders_count INTEGER,
            revenue_source_currency VARCHAR, pull_id VARCHAR, loaded_at VARCHAR,
            project_id VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO raw_shopify_orders VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("2026-07-01", "o-eur", "t-eur", 100.0, 10.0, 1, "EUR",
             "pull_fx_001", "2026-07-01T00:00:00Z", "default"),
            ("2026-07-01", "o-usd", "t-usd", 100.0, 10.0, 1, "USD",
             "pull_fx_001", "2026-07-01T00:00:00Z", "default"),
        ],
    )
    con.execute(
        "CREATE TABLE dim_project (project_id VARCHAR, canonical_currency VARCHAR)"
    )
    con.execute("INSERT INTO dim_project VALUES ('default', 'EUR')")
    con.execute(
        """
        CREATE TABLE fx_rates (
            from_currency VARCHAR, to_currency VARCHAR, rate DOUBLE,
            valid_from DATE, valid_to DATE
        )
        """
    )
    con.execute(
        "INSERT INTO fx_rates VALUES ('USD', 'EUR', 0.9, DATE '2026-01-01', DATE '2026-12-31')"
    )
    # Story 13.2: empty FX resolution table -> LEFT JOIN matches nothing -> fallback.
    con.execute(
        """
        CREATE TABLE fx_conflict_resolutions (
            id VARCHAR, project_id VARCHAR, target_field VARCHAR, source_module VARCHAR,
            resolved_source_currency VARCHAR, decided_by VARCHAR, decided_at TIMESTAMP, note VARCHAR
        )
        """
    )

    rows = con.execute(
        f"SELECT order_id, revenue_source_value, revenue, refund_source_value, "
        f"refund_amount FROM ({body}) ORDER BY order_id"
    ).fetchall()
    con.close()

    by_order = {r[0]: r for r in rows}
    # EUR -> EUR : pas de ligne fx EUR->EUR => fallback identité (COALESCE).
    assert by_order["o-eur"][2] == pytest.approx(100.0)
    assert by_order["o-eur"][4] == pytest.approx(10.0)
    # USD -> EUR : conversion RÉELLE appliquée (0.9), source préservée.
    assert by_order["o-usd"][1] == pytest.approx(100.0), "source value must be preserved"
    assert by_order["o-usd"][2] == pytest.approx(90.0), "USD revenue must be converted"
    assert by_order["o-usd"][3] == pytest.approx(10.0)
    assert by_order["o-usd"][4] == pytest.approx(9.0), "USD refund must be converted"
