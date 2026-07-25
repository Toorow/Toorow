-- semantic_roas: Return On Ad Spend view (Story 4.1, AC7; Story 39.2 canonical-micros-aware)
-- ROAS = revenue / cost (ratio metric, AD-4)
--
-- This view is materialized as a VIEW, never as a table (HG-3).
-- Ratio metrics are NOT stored in fact_daily_kpi (AD-4 enforcement).
-- The fact_kpi_metric_additive_only test verifies fact_daily_kpi stays ratio-free.
--
-- HG-1 (AD-2): no module-specific strings in this file.
-- IMPORTANT: no module currently supplies a 'revenue' metric. This view returns NULL
-- for all roas values until a revenue-reporting module is added in a future epic.
-- The NULLIF on cost prevents division-by-zero when cost = 0.
--
-- Story 39.2 (E39-FR10, AC2, NFR01) — canonical-micros-aware ratio reconstruction:
--   Each monetary component is reconstructed SUM-THEN-DIVIDE, and normalized to DISPLAY
--   units BEFORE the ratio divide: a monetary component whose canonical metric is declared
--   native_unit='micros' in dbt/seeds/money_metric_units.csv lands canonical micros in the
--   mart, so its SUM is divided by 1e6 ONCE (view-level, outside the SUM — never per row).
--   revenue and cost are declared 'decimal' (they land decimal into fact_daily_kpi via the
--   incumbent AD-6 FX-at-staging), so the /1e6 branch is NEVER taken here and the output is
--   BYTE-IDENTICAL for the currently-wired connectors (E39-NFR06). The branch activates only
--   the day a canonical-micros money metric reaches the mart (e.g. GAM wired later), keeping
--   the ratio unit-correct then. The 1e6 divide and the ratio divide both stay OUTSIDE the
--   SUM (NFR01: sum-then-divide, never divide-then-sum, never a per-row /1e6).
--   `semantic_numerator` / `semantic_denominator` carry the DISPLAY-unit-normalized
--   components so a drill-down reads true display-unit money.

{{ config(materialized='view') }}

WITH money_units AS (
    -- Canonical-unit map (Story 39.2). Only 'micros'-declared metrics owe a read-time /1e6;
    -- everything else (decimal money, counts) is read as-is. The 'micros' set drives the
    -- CASE below with NO hardcoded metric list (AD-2, source-agnostic).
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
        -- 1.0 for a canonical-micros money metric (=> summed value is /1e6 at view level),
        -- else NULL. Kept as a divisor multiplier so the divide stays OUTSIDE the SUM.
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
        -- numerator (revenue), display-unit-normalized: SUM then /1e6 iff canonical micros
        (SUM(CASE WHEN metric = 'revenue' THEN value ELSE 0 END)
         / CASE WHEN MAX(CASE WHEN metric = 'revenue' AND is_canonical_micros THEN 1 ELSE 0 END) = 1
                THEN 1000000 ELSE 1 END)
        /
        NULLIF(
            -- denominator (cost), display-unit-normalized: SUM then /1e6 iff canonical micros
            (SUM(CASE WHEN metric = 'cost' THEN value ELSE 0 END)
             / CASE WHEN MAX(CASE WHEN metric = 'cost' AND is_canonical_micros THEN 1 ELSE 0 END) = 1
                    THEN 1000000 ELSE 1 END),
            0)
    ) AS roas,
    (SUM(CASE WHEN metric = 'revenue' THEN value ELSE 0 END)
     / CASE WHEN MAX(CASE WHEN metric = 'revenue' AND is_canonical_micros THEN 1 ELSE 0 END) = 1
            THEN 1000000 ELSE 1 END) AS semantic_numerator,
    (SUM(CASE WHEN metric = 'cost' THEN value ELSE 0 END)
     / CASE WHEN MAX(CASE WHEN metric = 'cost' AND is_canonical_micros THEN 1 ELSE 0 END) = 1
            THEN 1000000 ELSE 1 END) AS semantic_denominator,
    MAX(pull_id) AS pull_id
FROM kpi
GROUP BY project_id, date, connector, breakdown_dimension, breakdown_value
