-- Story 41.5 AC9 (Epic 27 invariant 4) -- CLAIMED REVENUE IS NEVER A ROAS NUMERATOR.
--
-- A channel-CLAIMED metric (attributed_revenue, conversion_value, ad_revenue,
-- all_revenue) is normalized and VISIBLE as an overlay, and never enters the ratio. A
-- PLAN metric (target_revenue) enters neither. Both halves matter: deleting claimed
-- revenue from the model entirely would also satisfy "not in the numerator", and would
-- destroy the overlay the epic exists to provide.
--
--   A. the alignment view's revenue must be reconstructible from SALES rows ALONE. If an
--      ATTRIBUTED or PLAN row had been added in, the aligned figure would exceed the
--      SALES total for that day;
--   B. an ATTRIBUTED row, where one exists, must still be PRESENT in the normalized
--      model (the KEEP_SEPARATE overlay);
--   C. no PLAN row may ever be a revenue_source.
--
-- ✅ THE LIMITATION THIS COMMENT RECORDED IS GONE, since 2026-08-04. It said no
-- claimed-revenue emitter shipped a DuckDB seed fixture, so B had no positive example
-- and A could not be falsified by a real ATTRIBUTED row. `adjust` does ship one
-- (Story 53.10); the 41.5 seeder wires it into `feetaxrev_dev_posture`, which lands
-- FOUR ATTRIBUTED rows (`ad_revenue`, `all_revenue`) on the same days as its SALES
-- revenue and with comparable magnitude -- exactly the shape that would inflate a ROAS
-- if one leaked into the numerator. B now has its positive example and A is falsifiable.
-- klaviyo and cm360 still ship none (epic retro item C.9/7), which costs nothing here:
-- the invariant is written over whatever ATTRIBUTED rows exist, not over a connector.

WITH normalized AS (
    SELECT * FROM {{ ref('fee_tax_revenue_normalized_daily') }}
),

alignment AS (
    SELECT * FROM {{ ref('fee_tax_revenue_alignment_daily') }}
),

sales_total AS (
    SELECT project_id, date, currency, connector,
           SUM(gross_revenue_ttc_micros) AS sales_ttc_micros
    FROM normalized
    WHERE revenue_role = 'SALES'
    GROUP BY project_id, date, currency, connector
),

case_a AS (
    SELECT 'ALIGNED_REVENUE_EXCEEDS_THE_SALES_ROWS' AS failure,
           a.project_id AS project_id,
           'aligned=' || CAST(a.revenue_micros AS STRING)
               || ' sales=' || CAST(s.sales_ttc_micros AS STRING) AS detail
    FROM alignment a
    JOIN sales_total s
        ON  s.project_id = a.project_id
        AND s.date       = a.date
        AND s.currency   = a.currency
        AND s.connector  = a.revenue_source
    WHERE a.tax_basis = 'TTC'
      AND a.revenue_micros IS NOT NULL
      AND a.revenue_micros > s.sales_ttc_micros
),

case_b AS (
    SELECT 'CLAIMED_REVENUE_WAS_DROPPED_INSTEAD_OF_OVERLAID' AS failure,
           f.project_id AS project_id,
           f.connector || '/' || f.metric AS detail
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN {{ ref('fee_tax_revenue_scope') }} s
        ON  s.connector        = f.connector
        AND s.canonical_metric = f.metric
    JOIN {{ ref('fee_tax_revenue_normalized_daily') }} live
        ON live.project_id = f.project_id
    WHERE s.revenue_role = 'ATTRIBUTED'
      AND NOT EXISTS (
          SELECT 1 FROM normalized n
          WHERE n.project_id = f.project_id
            AND n.date       = CAST(f.date AS DATE)
            AND n.connector  = f.connector
            AND n.metric     = f.metric
      )
),

case_c AS (
    SELECT 'A_PLAN_METRIC_BECAME_A_REVENUE_SOURCE' AS failure,
           a.project_id AS project_id, a.revenue_source AS detail
    FROM alignment a
    JOIN normalized n
        ON  n.project_id = a.project_id
        AND n.date       = a.date
        AND n.connector  = a.revenue_source
    WHERE n.revenue_role IN ('PLAN', 'ATTRIBUTED')
      AND NOT EXISTS (
          SELECT 1 FROM normalized s
          WHERE s.project_id = a.project_id
            AND s.date       = a.date
            AND s.connector  = a.revenue_source
            AND s.revenue_role = 'SALES'
      )
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
