"""Integration seam test: TikTok seed -> DuckDB mart -> get_tiktok_ads_report envelope.

Story 15.2 (Epic 15), AI-56 (seam test via build_asgi_app / MCP tool layer), AI-54
(fixture = real payload from the generator).

This is the FR2 proof for the TikTok pipeline through the MCP tool seam:
  1. Generate multi-day TikTok rows via the SAME generator that feeds the seed loader /
     conformance fixture (generate_tiktok_seed.generate_rows) -- AI-54.
  2. Land them in raw_tiktok_ads_daily + materialize the tiktok fact_daily_kpi block
     (the exact SQL from dbt/models/marts/fact_daily_kpi.sql: campaign-grain aggregation).
  3. Call the module's get_tiktok_ads_report tool through the FastMCP tool layer
     (FastMCPTransport, the established in-process MCP seam convention) and assert on
     VALUES, not just HTTP 200 (AI-56).
  4. Prove the story invariants on real mart values:
     - cost/impressions/clicks/conversions equal the generator day totals per campaign.
     - conversions is a distinct CLAIMED metric (present, not fused into any other).
     - breakdown_dimension is 'campaign_id' (the seed lands the campaign grain only).

review-15-2 F-3 (KNOWN LIMITATION, documented on purpose): _TIKTOK_MART_SQL below is a
hand-transcribed slice of dbt/models/marts/fact_daily_kpi.sql, NOT the compiled dbt model.
It short-circuits the staging layer, so it does NOT exercise the FX normalization
(COALESCE(spend * fx.rate, spend)) nor the QUALIFY supersede -- the seed is EUR-only so
cost == spend here and the shortcut is faithful for these values. The REAL dbt path (staging
FX + mart data_level filter) is covered by the central `dbt build` + the singular tests
test_tiktok_grain_series_reconcile.sql / test_tiktok_no_grain_bleed.sql / test_tiktok_
totals_isolated.sql. This transcribed SQL is aligned on the mart's data_level filter (F-1)
so it stays representative: each series reads ONLY its own report grain.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).parents[3]
TIKTOK_DIR = REPO_ROOT / "server" / "modules" / "tiktok-ads"


def _import(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def anyio_backend():
    return "asyncio"


# The tiktok fact block, transcribed from fact_daily_kpi.sql. review-15-2 F-1/F-3: each
# breakdown series reads ONLY its own report grain (data_level = AUCTION_CAMPAIGN /
# AUCTION_ADGROUP / AUCTION_AD), exactly like the real mart -- so coexisting grains never
# double-count. WHERE ... IS NOT NULL mirrors the mart's NULL-id guard. NB (F-3): this is a
# hand slice that reads raw directly (no staging FX/QUALIFY); faithful for the EUR-only seed
# where cost == spend. The compiled dbt path is the authoritative coverage.
_TIKTOK_SERIES = [
    ("cost", "spend", "campaign_id", "AUCTION_CAMPAIGN"),
    ("impressions", "impressions", "campaign_id", "AUCTION_CAMPAIGN"),
    ("clicks", "clicks", "campaign_id", "AUCTION_CAMPAIGN"),
    ("conversions", "conversions", "campaign_id", "AUCTION_CAMPAIGN"),
    ("cost", "spend", "adgroup_id", "AUCTION_ADGROUP"),
    ("impressions", "impressions", "adgroup_id", "AUCTION_ADGROUP"),
    ("clicks", "clicks", "adgroup_id", "AUCTION_ADGROUP"),
    ("conversions", "conversions", "adgroup_id", "AUCTION_ADGROUP"),
    ("cost", "spend", "ad_id", "AUCTION_AD"),
    ("impressions", "impressions", "ad_id", "AUCTION_AD"),
    ("clicks", "clicks", "ad_id", "AUCTION_AD"),
    ("conversions", "conversions", "ad_id", "AUCTION_AD"),
]


def _build_tiktok_mart_sql() -> str:
    selects = []
    for metric, src_col, dim, data_level in _TIKTOK_SERIES:
        selects.append(
            f"SELECT project_id, date, 'tiktok-ads' AS connector, '{metric}' AS metric, "
            f"'{dim}' AS breakdown_dimension, {dim} AS breakdown_value, "
            f"SUM(CAST({src_col} AS DOUBLE)) AS value, "
            f"MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at "
            f"FROM raw_tiktok_ads_daily "
            f"WHERE data_level = '{data_level}' AND {dim} IS NOT NULL "
            f"GROUP BY project_id, date, {dim}"
        )
    body = "\nUNION ALL\n".join(selects)
    return (
        "CREATE SCHEMA IF NOT EXISTS main_marts;\n"
        "CREATE TABLE IF NOT EXISTS main_marts.fact_daily_kpi AS\n" + body
    )


_TIKTOK_MART_SQL = _build_tiktok_mart_sql()


def _seed_mart(db_path: str, rows: list[dict]) -> None:
    import duckdb

    con = duckdb.connect(db_path)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS raw_tiktok_ads_daily (
            date VARCHAR, data_level VARCHAR, campaign_id VARCHAR, campaign_name VARCHAR,
            adgroup_id VARCHAR, adgroup_name VARCHAR, ad_id VARCHAR,
            spend DOUBLE, impressions INTEGER, clicks INTEGER, conversions INTEGER,
            pull_id VARCHAR, loaded_at VARCHAR, project_id VARCHAR,
            cost_source_currency VARCHAR
        )
        """
    )
    con.executemany(
        "INSERT INTO raw_tiktok_ads_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                r["date"], r.get("data_level"), r["campaign_id"], r.get("campaign_name"),
                r.get("adgroup_id"), r.get("adgroup_name"), r.get("ad_id"),
                float(r["spend"]), int(r["impressions"]), int(r["clicks"]),
                int(r["conversions"]),
                "pull_tt_seam_001", "2026-07-01T00:00:00Z", "default",
                r.get("cost_source_currency", "EUR"),
            )
            for r in rows
        ],
    )
    con.execute(_TIKTOK_MART_SQL)
    con.close()


@pytest.mark.anyio
async def test_tiktok_report_seam_asserts_values(tmp_path, monkeypatch):
    """Seam: generator -> mart -> get_tiktok_ads_report tool, asserting on values (AI-56)."""
    from fastmcp.client import Client, FastMCPTransport

    gen = _import("tiktok_seed_gen", TIKTOK_DIR / "seeds" / "generate_tiktok_seed.py")
    connector = _import("tiktok_conn_seam", TIKTOK_DIR / "connector.py")

    from datetime import date as _date

    end = _date(2026, 7, 15)
    rows = gen.generate_rows(days=14, end_date=end)  # multi-day (AI-56)
    assert rows, "generator must produce rows"

    db_path = str(tmp_path / "tt_seam.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)
    _seed_mart(db_path, rows)

    # Expected window totals per metric, computed directly from the generator rows.
    from collections import defaultdict

    exp_cost = defaultdict(float)
    exp_impr = defaultdict(float)
    exp_clicks = defaultdict(float)
    exp_conv = defaultdict(float)
    for r in rows:
        exp_cost[r["campaign_id"]] += float(r["spend"])
        exp_impr[r["campaign_id"]] += float(r["impressions"])
        exp_clicks[r["campaign_id"]] += float(r["clicks"])
        exp_conv[r["campaign_id"]] += float(r["conversions"])

    date_from = min(r["date"] for r in rows)
    date_to = max(r["date"] for r in rows)

    async with Client(FastMCPTransport(connector.mcp_app)) as client:
        result = await client.call_tool(
            "get_tiktok_ads_report",
            {
                "project_id": "default",
                "report_profile": "campaign_daily",
                "date_from": date_from,
                "date_to": date_to,
            },
        )

    envelope = result.structured_content or result.data
    assert envelope["schema_version"] == "1"
    metrics = envelope["data"]["metrics"]
    assert set(metrics) == {"cost", "impressions", "clicks", "conversions"}

    def _by_campaign(metric: str) -> dict[str, float]:
        out: dict[str, float] = {}
        for e in metrics[metric]:
            # All entries carry breakdown_dimension='campaign_id' (no partition leak).
            assert e["breakdown_dimension"] == "campaign_id"
            out[e["breakdown_value"]] = e["value"]
        return out

    got_cost = _by_campaign("cost")
    got_conv = _by_campaign("conversions")
    # cost matches the summed spend per campaign over the window (spend -> cost rename).
    for cid, expected in exp_cost.items():
        assert got_cost[cid] == pytest.approx(expected, rel=1e-6)
    # conversions is a distinct CLAIMED metric, present and equal to the seed totals.
    for cid, expected in exp_conv.items():
        assert got_conv[cid] == pytest.approx(expected, rel=1e-6)
    assert sum(exp_conv.values()) > 0, "seed must carry conversions to exercise the CLAIMED metric"
    # impressions/clicks also flow through with their day totals.
    assert _by_campaign("impressions") == pytest.approx(
        {k: v for k, v in exp_impr.items()}
    )
    assert _by_campaign("clicks") == pytest.approx({k: v for k, v in exp_clicks.items()})


@pytest.mark.anyio
async def test_tiktok_multigrain_no_double_count(tmp_path, monkeypatch):
    """review-15-2 F-1: with the THREE grains coexisting in raw, each mart breakdown series
    reads ONLY its own data_level -> campaign series == adgroup series == ad series (inter-
    series reconciliation) AND the campaign series is NOT inflated by the campaign_id carried
    on adgroup/ad rows. Under the OLD schema (no data_level filter) the campaign series would
    equal ~3x the true total (campaign-grain + adgroup-grain campaign_id + ad-grain
    campaign_id) -> this test would FAIL. This is the pytest analogue of
    test_tiktok_no_grain_bleed.sql, self-contained without the central dbt build.
    """
    from datetime import date as _date

    import duckdb

    gen = _import("tiktok_seed_gen_mg", TIKTOK_DIR / "seeds" / "generate_tiktok_seed.py")

    end = _date(2026, 7, 15)
    rows = gen.generate_multigrain_rows(days=14, end_date=end)
    # Sanity: the three grains really coexist in the seed.
    levels = {r["data_level"] for r in rows}
    assert levels == {"AUCTION_CAMPAIGN", "AUCTION_ADGROUP", "AUCTION_AD"}

    db_path = str(tmp_path / "tt_mg.duckdb")
    _seed_mart(db_path, rows)

    con = duckdb.connect(db_path, read_only=True)
    series_totals = {
        (metric, dim): total
        for metric, dim, total in con.execute(
            "SELECT metric, breakdown_dimension, SUM(value) "
            "FROM main_marts.fact_daily_kpi WHERE connector = 'tiktok-ads' "
            "GROUP BY metric, breakdown_dimension"
        ).fetchall()
    }
    con.close()

    # True window total per metric = sum of the CAMPAIGN-grain rows only (the reconciled roll-up).
    from collections import defaultdict

    true_total: dict[str, float] = defaultdict(float)
    for r in rows:
        if r["data_level"] != "AUCTION_CAMPAIGN":
            continue
        true_total["cost"] += float(r["spend"])
        true_total["impressions"] += float(r["impressions"])
        true_total["clicks"] += float(r["clicks"])
        true_total["conversions"] += float(r["conversions"])

    for metric in ("cost", "impressions", "clicks", "conversions"):
        camp = series_totals[(metric, "campaign_id")]
        adg = series_totals[(metric, "adgroup_id")]
        ad = series_totals[(metric, "ad_id")]
        # Inter-series reconciliation: the three grain series total the SAME window value.
        assert camp == pytest.approx(true_total[metric], rel=1e-6), metric
        assert adg == pytest.approx(true_total[metric], rel=1e-6), metric
        assert ad == pytest.approx(true_total[metric], rel=1e-6), metric
        # Anti-regression: the campaign series is NOT the sum of all grains (old-schema bleed
        # would give ~3x). It equals exactly the single campaign-grain total.
        assert camp < true_total[metric] * 1.5, f"{metric}: campaign series inflated (grain bleed)"
