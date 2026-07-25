-- semantic_cpa: Cost Per Acquisition view (Story 4.1, AC7; Story 39.2 canonical-micros-aware)
-- CPA = cost / conversions (ratio metric, AD-4)
--
-- This view is materialized as a VIEW, never as a table (HG-3).
-- Ratio metrics are NOT stored in fact_daily_kpi (AD-4 enforcement).
-- The fact_kpi_metric_additive_only test verifies fact_daily_kpi stays ratio-free.
--
-- HG-1 (AD-2): no module-specific strings in this file.
-- If no module supplies 'cost' or 'conversions', returns NULL rows (NULLIF).
--
-- Story 39.2 (E39-FR10, AC2, NFR01) — canonical-micros-aware ratio reconstruction:
--   The monetary NUMERATOR (cost) is reconstructed SUM-THEN-DIVIDE and normalized to
--   DISPLAY units BEFORE the ratio divide: if 'cost' is declared native_unit='micros' in
--   dbt/seeds/money_metric_units.csv it lands canonical micros in the mart, so its SUM is
--   divided by 1e6 ONCE (view-level, outside the SUM). 'cost' is declared 'decimal' today
--   (AD-6 FX-at-staging lands decimal into fact_daily_kpi), so the /1e6 branch is NEVER
--   taken and the output is BYTE-IDENTICAL for the currently-wired connectors (E39-NFR06).
--   The DENOMINATOR 'conversions' is a COUNT (non-money) — its math is unchanged. The 1e6
--   divide and the ratio divide both stay OUTSIDE the SUM (NFR01: sum-then-divide). The
--   provenance columns carry the display-unit-normalized components.

{{ config(materialized='view') }}

WITH money_units AS (
    -- Canonical-unit map (Story 39.2). Only 'micros'-declared metrics owe a read-time /1e6.
    SELECT canonical_metric
    FROM {{ ref('money_metric_units') }}
    WHERE canonical_native_unit = 'micros'
),
kpi AS (
    SELECT
        f.project_id,
        f.date,
        f.connector,
        f.breakdown_dimension,
        f.breakdown_value,
        f.metric,
        f.value,
        f.pull_id,
        (f.metric IN (SELECT canonical_metric FROM money_units)) AS is_canonical_micros
    FROM {{ ref('fact_daily_kpi') }} f
)
SELECT
    project_id,
    date,
    connector,
    breakdown_dimension,
    breakdown_value,
    (
        -- numerator (cost), display-unit-normalized: SUM then /1e6 iff canonical micros
        (SUM(CASE WHEN metric = 'cost' THEN value ELSE 0 END)
         / CASE WHEN MAX(CASE WHEN metric = 'cost' AND is_canonical_micros THEN 1 ELSE 0 END) = 1
                THEN 1000000 ELSE 1 END)
        /
        -- denominator (conversions) is a count — not money, never normalized
        NULLIF(SUM(CASE WHEN metric = 'conversions' THEN value ELSE 0 END), 0)
    ) AS cpa,
    (SUM(CASE WHEN metric = 'cost' THEN value ELSE 0 END)
     / CASE WHEN MAX(CASE WHEN metric = 'cost' AND is_canonical_micros THEN 1 ELSE 0 END) = 1
            THEN 1000000 ELSE 1 END) AS semantic_numerator,
    SUM(CASE WHEN metric = 'conversions' THEN value ELSE 0 END) AS semantic_denominator,
    MAX(pull_id) AS pull_id
FROM kpi
GROUP BY project_id, date, connector, breakdown_dimension, breakdown_value
