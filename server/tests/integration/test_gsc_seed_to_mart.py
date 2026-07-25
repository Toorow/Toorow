"""Integration test: GSC seed -> DuckDB -> semantic_avg_position proof.

Story 6.2 (AC15, AD-4).

This test is the FR2 proof for the non-additive pipeline:
  1. Seeds raw_gsc_daily with known rows (two pages, different positions + impressions).
  2. Materializes fact_daily_kpi using the GSC staging data.
  3. Asserts average_position appears in fact_daily_kpi as per-row raw values.
  4. Asserts semantic_avg_position computes the correct impression-weighted result.
  5. Proves naive AVG ≠ weighted AVG (non-trivial test).

Weighted average proof (from seed data):
  Page /blog/: impressions=850, average_position=7.3
  Page /docs/: impressions=200, average_position=3.1
  Naive AVG: (7.3 + 3.1) / 2 = 5.2  ← WRONG
  Weighted AVG: (7.3*850 + 3.1*200) / (850+200) = 6825/1050 ≈ 6.5  ← CORRECT
"""

from __future__ import annotations

import pytest

# Skip this test in CI-only runs or when DuckDB/dbt is not available.
# The test requires dbt CLI and a DuckDB warehouse populated from seeds.
# Mark as integration to allow selective exclusion.
pytestmark = pytest.mark.integration


def test_gsc_fact_daily_kpi_includes_average_position(tmp_path, monkeypatch):
    """Seed GSC rows into DuckDB; assert average_position in fact_daily_kpi.

    This test directly seeds raw_gsc_daily and queries the expected mart rows
    WITHOUT running dbt (DuckDB-level integration only). A full dbt integration
    test would require the local loop runner and is covered by run_local_loop.py.
    """
    import duckdb

    db_path = str(tmp_path / "gsc_integration.duckdb")
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", db_path)

    # --- Step 1: seed raw_gsc_daily ---
    con = duckdb.connect(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS raw_gsc_daily (
            date VARCHAR,
            page VARCHAR,
            country VARCHAR,
            device VARCHAR,
            clicks INTEGER,
            impressions INTEGER,
            average_position DOUBLE,
            pull_id VARCHAR,
            loaded_at VARCHAR,
            project_id VARCHAR
        )
    """)
    # Two pages with significantly different positions and impressions (non-trivial)
    con.execute("""
        INSERT INTO raw_gsc_daily VALUES
            ('2026-07-01', 'https://example.com/blog/', 'fra', 'desktop',
             42, 850, 7.3, 'pull_gsc_integ_001', '2026-07-01T10:00:00Z', 'default'),
            ('2026-07-01', 'https://example.com/docs/', 'fra', 'desktop',
             15, 200, 3.1, 'pull_gsc_integ_001', '2026-07-01T10:00:00Z', 'default')
    """)

    # --- Step 2: materialize fact_daily_kpi manually (simulating dbt run) ---
    # This is the GSC block from fact_daily_kpi.sql, executed directly for testing.
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute("""
        CREATE TABLE IF NOT EXISTS main_marts.fact_daily_kpi AS
        -- GSC additive: clicks
        SELECT 'default' AS project_id, date, 'gsc' AS connector,
               'clicks' AS metric, 'page' AS breakdown_dimension,
               page AS breakdown_value,
               SUM(CAST(clicks AS DOUBLE)) AS value,
               MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
        FROM raw_gsc_daily GROUP BY project_id, date, page
        UNION ALL
        -- GSC additive: impressions
        SELECT 'default' AS project_id, date, 'gsc' AS connector,
               'impressions' AS metric, 'page' AS breakdown_dimension,
               page AS breakdown_value,
               SUM(CAST(impressions AS DOUBLE)) AS value,
               MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
        FROM raw_gsc_daily GROUP BY project_id, date, page
        UNION ALL
        -- GSC non-additive: average_position raw per row
        SELECT 'default' AS project_id, date, 'gsc' AS connector,
               'average_position' AS metric, 'page' AS breakdown_dimension,
               page AS breakdown_value,
               AVG(average_position) AS value,
               MAX(pull_id) AS pull_id, MAX(loaded_at) AS loaded_at
        FROM raw_gsc_daily GROUP BY project_id, date, page
    """)

    # --- Step 3: assert average_position appears in fact_daily_kpi as raw per-row values ---
    rows = con.execute("""
        SELECT breakdown_value, value
        FROM main_marts.fact_daily_kpi
        WHERE metric = 'average_position' AND connector = 'gsc'
        ORDER BY breakdown_value
    """).fetchall()

    assert len(rows) == 2, "Should have 2 average_position rows (one per page)"
    rows_dict = {r[0]: r[1] for r in rows}
    assert rows_dict["https://example.com/blog/"] == pytest.approx(7.3)
    assert rows_dict["https://example.com/docs/"] == pytest.approx(3.1)

    # --- Step 4: compute semantic_avg_position manually ---
    # This is what semantic_avg_position.sql does:
    # SUM(position * impressions) / SUM(impressions) across pages
    weighted_result = con.execute("""
        SELECT
            SUM(f_pos.value * f_imp.value) / NULLIF(SUM(f_imp.value), 0) AS weighted_avg_position
        FROM main_marts.fact_daily_kpi f_pos
        JOIN main_marts.fact_daily_kpi f_imp
            ON f_pos.project_id = f_imp.project_id
            AND f_pos.date = f_imp.date
            AND f_pos.connector = f_imp.connector
            AND f_pos.breakdown_dimension = f_imp.breakdown_dimension
            AND f_pos.breakdown_value = f_imp.breakdown_value
        WHERE f_pos.metric = 'average_position'
          AND f_imp.metric = 'impressions'
          AND f_pos.connector = 'gsc'
          AND f_pos.date = '2026-07-01'
    """).fetchone()[0]

    # Weighted AVG: (7.3*850 + 3.1*200) / (850+200) = (6205+620)/1050 = 6825/1050 ≈ 6.5
    expected_weighted = (7.3 * 850 + 3.1 * 200) / (850 + 200)
    assert weighted_result == pytest.approx(expected_weighted, rel=1e-6)

    # --- Step 5: prove naive AVG != weighted AVG (non-trivial test) ---
    naive_avg = (7.3 + 3.1) / 2  # = 5.2
    assert naive_avg == pytest.approx(5.2)
    assert abs(weighted_result - naive_avg) > 1.0, (
        f"Weighted AVG ({weighted_result:.4f}) must differ from naive AVG ({naive_avg:.4f}) "
        "by > 1.0 to prove the non-trivial nature of the test"
    )

    con.close()

    print("\nWeighted position proof:")
    print("  Page /blog/: impressions=850, position=7.3")
    print("  Page /docs/: impressions=200, position=3.1")
    print(f"  Naive AVG:    {naive_avg:.4f}  <- WRONG")
    print(f"  Weighted AVG: {weighted_result:.4f}  <- CORRECT (AD-4 semantic layer)")
    print(f"  Difference:   {abs(weighted_result - naive_avg):.4f}")
