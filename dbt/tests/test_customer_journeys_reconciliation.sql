-- Story 16.2 reconciliation (AC (a)): customer_journeys re-exposes the acquisition
-- conversions EXACTLY -- it neither creates nor loses rows relative to its source
-- partitions in fact_daily_kpi.
--
-- The mart's axes map 1:1 to fact_daily_kpi acquisition partitions:
--   (last_click , channel ) <- breakdown_dimension 'session_source_medium'
--   (last_click , campaign) <- breakdown_dimension 'session_campaign'
--   (first_click, channel ) <- breakdown_dimension 'first_user_source_medium'
-- For EACH axis, SUM(conversions) per (project_id, date) in the mart MUST equal
-- SUM(value) of the corresponding fact partition (metric='conversions'). We compare
-- PER AXIS (not per model): last_click has TWO marginal axes and summing both would
-- double-count the same conversions -- exactly what this mart must NOT do.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
-- Tolerance 0.001 absolute -- any real drop/duplication (whole conversions) fails loudly.

WITH mart_axis AS (
    SELECT
        project_id,
        date,
        attribution_model,
        breakdown_kind,
        SUM(conversions) AS mart_conversions
    FROM {{ ref('customer_journeys') }}
    GROUP BY project_id, date, attribution_model, breakdown_kind
),

fact_axis AS (
    SELECT
        project_id,
        date,
        CASE breakdown_dimension
            WHEN 'session_source_medium'     THEN 'last_click'
            WHEN 'session_campaign'          THEN 'last_click'
            WHEN 'first_user_source_medium'  THEN 'first_click'
        END AS attribution_model,
        CASE breakdown_dimension
            WHEN 'session_source_medium'     THEN 'channel'
            WHEN 'session_campaign'          THEN 'campaign'
            WHEN 'first_user_source_medium'  THEN 'channel'
        END AS breakdown_kind,
        SUM(value) AS fact_conversions
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-analytics'
      AND metric = 'conversions'
      AND breakdown_dimension IN (
          'session_source_medium', 'session_campaign', 'first_user_source_medium'
      )
    GROUP BY project_id, date, breakdown_dimension
)

SELECT
    f.project_id,
    f.date,
    f.attribution_model,
    f.breakdown_kind,
    f.fact_conversions,
    m.mart_conversions
FROM fact_axis f
FULL OUTER JOIN mart_axis m
    ON  m.project_id        = f.project_id
    AND m.date              = f.date
    AND m.attribution_model = f.attribution_model
    AND m.breakdown_kind    = f.breakdown_kind
-- A missing side (NULL) means a row was dropped or invented -- fail.
WHERE f.project_id IS NULL
   OR m.project_id IS NULL
   OR ABS(COALESCE(f.fact_conversions, 0) - COALESCE(m.mart_conversions, 0)) > 0.001
