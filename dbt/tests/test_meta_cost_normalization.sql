-- test_meta_cost_normalization.sql — Story 4.2 (AC5) / G-05 update
--
-- Reconciliation test: normalized cost must equal cost_source_value * fx_rate.
-- Fails (returns rows) when the normalization drift exceeds 0.01% of cost_source_value.
--
-- AD-6: normalization happens once, at staging. No downstream conversion.
-- HG-2: cost_source_value and cost_source_currency are both preserved in staging.
-- HG-3: no FX multiplication anywhere in server/core/ -- this test verifies dbt staging.
-- G-05: to_currency is canonical_currency per project (not hardcoded EUR).
--
-- Run via: dbt test --select test_meta_cost_normalization
--
-- This test PASSES (returns 0 rows) when:
--   ABS(cost - cost_source_value * fx_rate) <= cost_source_value * 0.0001
--
-- Same-currency rows (cost_source_currency = canonical_currency) are excluded: rate = 1.0.

WITH staged AS (
    SELECT
        s.project_id,
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        COALESCE(dp.canonical_currency, 'EUR') AS to_currency
    FROM {{ ref('stg_meta_ads_daily') }} s
    LEFT JOIN {{ ref('dim_project') }} dp ON dp.project_id = s.project_id
    WHERE s.cost_source_currency != COALESCE(dp.canonical_currency, 'EUR')
),
rates AS (
    SELECT from_currency, to_currency, rate
    FROM {{ ref('fx_rates') }}
),
normalization_check AS (
    SELECT
        s.cost_source_value,
        s.cost,
        s.cost_source_currency,
        s.to_currency,
        r.rate,
        ABS(s.cost - s.cost_source_value * r.rate) AS drift
    FROM staged s
    JOIN rates r
        ON r.from_currency = s.cost_source_currency
       AND r.to_currency   = s.to_currency
)
SELECT * FROM normalization_check WHERE drift > cost_source_value * 0.0001
