-- test_fx_resolution_applied.sql -- Story 13.2 (AC11) / AD-6
--
-- Reconciliation test: when a fx_conflict_resolution exists for a (project, field, module),
-- the staging model uses the RESOLVED source currency instead of the raw column value.
--
-- Passes (returns 0 rows) when:
--   (a) overridden_rows is NON-EMPTY  (cardinality guard -- see below)
--   (b) For rows where a resolution exists AND the resolution changes the effective currency
--       (resolved_source_currency != raw.cost_source_currency), the normalized cost
--       matches cost_source_value * the rate for the RESOLVED currency (not the raw currency).
--
-- AD-6: normalization happens once, at dbt staging. No downstream Python conversion.
-- Non-retroactive: this test covers the forward path (new rows after migration 053).
--
-- CARDINALITY GUARD (Data MEDIUM-1 fix):
--   The previous version passed trivially when overridden_rows was empty (seeder not run,
--   JOIN silently producing 0 rows, or resolution not applied). An empty overridden_rows
--   means the drift check never fires -- the test returned 0 rows (pass) while proving
--   nothing. The guard below forces a failure row when overridden_rows COUNT = 0, so the
--   test only passes when the staging model ACTUALLY applies the resolution.
--
-- In the seed data:
--   - fx_rates has USD->EUR=0.92 (valid_from=2020-01-01, valid_to=2099-12-31)
--                     EUR->EUR=1.0 (valid_from=2020-01-01, valid_to=2099-12-31)
--   - The fixture resolution (dbt/seeds/fx/seed_fx_conflict_resolutions.py) declares
--     source_module='meta-ads', target_field='cost', resolved_source_currency='USD'
--     for project 'default', whose meta-ads rows have cost_source_currency='EUR'.
--   - After staging: cost = cost_source_value * 0.92 (USD->EUR rate).
--   - This test verifies that overridden_rows is NON-EMPTY and the drift is <= 0.01%.
--
-- HOW TO RUN (the seeder must be called before dbt build):
--   1. Seed meta-ads rows (provides cost_source_currency='EUR' rows):
--        uv run python dbt/seeds/mediaplan/seed_plan_mirror.py --duckdb-path /tmp/dev.duckdb
--   2. Seed the FX resolution (inserts mirror.fx_conflict_resolutions fixture):
--        uv run python dbt/seeds/fx/seed_fx_conflict_resolutions.py --duckdb-path /tmp/dev.duckdb
--   3. Build + test:
--        dbt build --select stg_meta_ads_daily --vars '{"duckdb_path": "/tmp/dev.duckdb"}'
--        dbt test --select test_fx_resolution_applied
--
-- The rates CTE filters by valid_from/valid_to window (aligned with the staging model JOIN
-- condition CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to). This prevents
-- the test from selecting a rate that falls outside the staging window constraint, which
-- would produce a false mismatch. All seed dates (2026-03-*) are within 2020-01-01..2099-12-31.
--
-- Run via: dbt test --select test_fx_resolution_applied

WITH staged AS (
    SELECT
        s.project_id,
        s.cost_source_value,
        s.cost,
        s.cost_source_currency
    FROM {{ ref('stg_meta_ads_daily') }} s
),
resolutions AS (
    SELECT
        r.project_id,
        r.resolved_source_currency
    FROM {{ source('mirror', 'fx_conflict_resolutions') }} r
    WHERE r.target_field  = 'cost'
      AND r.source_module = 'meta-ads'
),
-- Rates are filtered by the same valid_from/valid_to window the staging model uses
-- (CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to). The representative
-- date 2026-03-05 falls within the seed fx_rates window (2020-01-01..2099-12-31), so
-- for dev/CI the filter is always satisfied. If multiple time-versioned rates were ever
-- added, this ensures the test selects the same rate that staging would apply.
rates AS (
    SELECT from_currency, to_currency, rate
    FROM {{ ref('fx_rates') }}
    WHERE CAST('2026-03-05' AS DATE) BETWEEN valid_from AND valid_to
),
-- Join staged rows against resolutions to find rows where a resolution exists
-- and the resolved currency differs from the raw currency.
-- For these rows, verify cost = cost_source_value * rate(resolved_currency -> EUR).
overridden_rows AS (
    SELECT
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        r.resolved_source_currency,
        fx.rate AS resolved_rate
    FROM staged s
    JOIN resolutions r ON r.project_id = s.project_id
    JOIN rates fx
        ON fx.from_currency = r.resolved_source_currency
       AND fx.to_currency   = 'EUR'
    WHERE r.resolved_source_currency != s.cost_source_currency
      AND s.cost_source_value IS NOT NULL
      AND fx.rate IS NOT NULL
),
-- CARDINALITY GUARD: fail if overridden_rows is empty.
-- An empty set means either (a) the seeder was not run, (b) the resolution JOIN
-- produced no matches, or (c) the staging model did not apply the resolution at all.
-- In all three cases the drift check below would trivially return 0 rows (pass)
-- while proving nothing. We force a failure row so the test is only green when the
-- staging model has ACTUALLY overridden at least one row.
cardinality_guard AS (
    SELECT
        CAST(NULL AS DOUBLE) AS cost_source_value,
        CAST(NULL AS DOUBLE) AS cost,
        CAST(NULL AS VARCHAR) AS resolved_source_currency,
        CAST(NULL AS DOUBLE) AS resolved_rate,
        CAST(NULL AS DOUBLE) AS drift,
        'CARDINALITY_FAIL: overridden_rows is empty -- seeder not run or resolution not applied'
            AS failure_reason
    WHERE (SELECT COUNT(*) FROM overridden_rows) = 0
),
reconciliation_check AS (
    SELECT
        cost_source_value,
        cost,
        resolved_source_currency,
        resolved_rate,
        ABS(cost - cost_source_value * resolved_rate) AS drift,
        CAST(NULL AS VARCHAR) AS failure_reason
    FROM overridden_rows
    WHERE cost_source_value > 0
      AND ABS(cost - cost_source_value * resolved_rate) > cost_source_value * 0.0001
)
-- Fail (return rows) when:
--   (a) overridden_rows is empty (cardinality_guard fires), OR
--   (b) the drift exceeds 0.01% of cost_source_value.
-- No rows = test passes (resolution applied and cost = cost_source_value * resolved_rate).
SELECT cost_source_value, cost, resolved_source_currency, resolved_rate, drift, failure_reason
FROM cardinality_guard
UNION ALL
SELECT cost_source_value, cost, resolved_source_currency, resolved_rate, drift, failure_reason
FROM reconciliation_check
