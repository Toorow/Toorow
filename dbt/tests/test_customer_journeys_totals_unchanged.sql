-- Story 16.2 non-interference (AC (b)): customer_journeys is a READ. It must NOT change
-- the acquisition partition totals in fact_daily_kpi -- the mart re-exposes them, it does
-- not re-aggregate them at a different grain.
--
-- This is the "totaux existants inchanges" guard for a READ mart. A dbt test cannot diff
-- "fact_daily_kpi with vs without customer_journeys" (customer_journeys does not write
-- into fact_daily_kpi -- that non-editing is the point, and is also verified structurally:
-- customer_journeys.sql only SELECTs from ref('fact_daily_kpi')). What CAN and MUST hold
-- is that for BOTH metrics carried by the axes (conversions AND sessions), the mart's
-- per-axis total equals the fact partition total -- i.e. the read is lossless and adds
-- nothing. If customer_journeys ever silently pooled marginals or dropped a metric, this
-- fails.
--
-- conversions is covered on all three axes; sessions on the two last_click axes only
-- (first_click never carried sessions -- 16.1). We assert per (axis, metric).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.001.

WITH mart_conv AS (
    SELECT project_id, date, attribution_model, breakdown_kind,
           SUM(conversions) AS v
    FROM {{ ref('customer_journeys') }}
    GROUP BY project_id, date, attribution_model, breakdown_kind
),

mart_sess AS (
    -- sessions only exists on last_click axes; SUM over NULLs (first_click) is NULL and
    -- is excluded by the axis filter below (we only join the last_click session axes).
    SELECT project_id, date, attribution_model, breakdown_kind,
           SUM(sessions) AS v
    FROM {{ ref('customer_journeys') }}
    WHERE attribution_model = 'last_click'
    GROUP BY project_id, date, attribution_model, breakdown_kind
),

fact_part AS (
    SELECT
        project_id, date, metric, breakdown_dimension,
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
        SUM(value) AS v
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-analytics'
      AND breakdown_dimension IN (
          'session_source_medium', 'session_campaign', 'first_user_source_medium'
      )
    GROUP BY project_id, date, metric, breakdown_dimension
),

-- conversions on all axes
conv_diff AS (
    SELECT f.project_id, f.date, 'conversions' AS metric,
           f.attribution_model, f.breakdown_kind, f.v AS fact_v, m.v AS mart_v
    FROM fact_part f
    JOIN mart_conv m
      ON m.project_id = f.project_id AND m.date = f.date
     AND m.attribution_model = f.attribution_model AND m.breakdown_kind = f.breakdown_kind
    WHERE f.metric = 'conversions'
),

-- sessions on the two last_click axes
sess_diff AS (
    SELECT f.project_id, f.date, 'sessions' AS metric,
           f.attribution_model, f.breakdown_kind, f.v AS fact_v, m.v AS mart_v
    FROM fact_part f
    JOIN mart_sess m
      ON m.project_id = f.project_id AND m.date = f.date
     AND m.attribution_model = f.attribution_model AND m.breakdown_kind = f.breakdown_kind
    WHERE f.metric = 'sessions' AND f.attribution_model = 'last_click'
)

SELECT * FROM conv_diff  WHERE ABS(COALESCE(fact_v,0) - COALESCE(mart_v,0)) > 0.001
UNION ALL
SELECT * FROM sess_diff  WHERE ABS(COALESCE(fact_v,0) - COALESCE(mart_v,0)) > 0.001
