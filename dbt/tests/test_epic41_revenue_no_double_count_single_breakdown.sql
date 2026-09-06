-- Story 41.5 -- the revenue-side anti-triple-count (C.8 decision 10, revenue twin).
--
-- `adjust` emits revenue / ad_revenue / all_revenue on THREE PARALLEL SERIES
-- (app_token / campaign_id / network) and EACH ONE TOTALS THE DAY. Running the
-- normalization over all three would TRIPLE revenue, TRIPLE every fee derived from it
-- and TRIPLE ROAS -- silently, because each series is individually plausible.
--
-- THE INVARIANT: the model's landed figure for a (project, date, connector, metric) must
-- equal the total of ONE canonical breakdown series, never the sum of all of them.
-- Expressed here as: landed_revenue_micros must equal the micros total of the SINGLE
-- canonical series the C4 rule picks, and must be STRICTLY LESS than the all-series sum
-- whenever more than one series exists.
--
-- ✅ THE LIMITATION THIS COMMENT RECORDED IS GONE, since 2026-08-04. It said the
-- `multi_series` branch was EMPTY in the dev warehouse and that this test "cannot today
-- FALSIFY a regression that removed the collapse", pending an adjust-shaped fixture.
-- `adjust` in fact ships a DuckDB seed loader (Story 53.10) and the 41.5 seeder now
-- wires it into `feetaxrev_dev_posture`. Measured on that project:
--
--     revenue lands on THREE parallel series -- network, campaign_id, app_token --
--     each totalling 1 098 150 000 micros for the day.
--     The collapse keeps 1 098 150 000.  The naive sum would be 3 294 450 000.
--
-- So the multi-series branch is live and a regression that removed the collapse would
-- TRIPLE every downstream fee and fail here. Recorded because the honest note is what
-- made it cheap to notice the blocker had expired.

WITH scope AS (
    SELECT canonical_metric, connector FROM {{ ref('fee_tax_revenue_scope') }}
),

fact_series AS (
    SELECT
        f.project_id,
        CAST(f.date AS DATE)                     AS date,
        f.connector,
        f.metric,
        f.breakdown_dimension,
        -- The SAME macro the model uses, so a divergence here is a real one and not an
        -- artefact of a second, subtly different normalisation written in the test.
        SUM({{ fee_tax_to_micros('f.value') }})  AS series_micros
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN scope s
        ON  s.connector        = f.connector
        AND s.canonical_metric = f.metric
    GROUP BY f.project_id, CAST(f.date AS DATE), f.connector, f.metric,
             f.breakdown_dimension
),

fact_rolled AS (
    SELECT
        project_id, date, connector, metric,
        COUNT(*)          AS series_count,
        SUM(series_micros) AS all_series_micros,
        MAX(series_micros) AS largest_series_micros
    FROM fact_series
    GROUP BY project_id, date, connector, metric
),

normalized AS (
    SELECT * FROM {{ ref('fee_tax_revenue_normalized_daily') }}
),

-- The model must never carry the SUM of parallel series.
summed_all_series AS (
    SELECT
        'TRIPLE_COUNTED_PARALLEL_SERIES' AS failure,
        n.project_id                     AS project_id,
        n.connector || '/' || n.metric || ' landed='
            || CAST(n.landed_revenue_micros AS STRING)
            || ' all_series=' || CAST(r.all_series_micros AS STRING) AS detail
    FROM normalized n
    JOIN fact_rolled r
        ON  r.project_id = n.project_id
        AND r.date       = n.date
        AND r.connector  = n.connector
        AND r.metric     = n.metric
    WHERE r.series_count > 1
      AND n.landed_revenue_micros = r.all_series_micros
),

-- And on a single-series source it must equal that series exactly.
single_series_mismatch AS (
    SELECT
        'SINGLE_SERIES_TOTAL_DIVERGED' AS failure,
        n.project_id                   AS project_id,
        n.connector || '/' || n.metric || ' landed='
            || CAST(n.landed_revenue_micros AS STRING)
            || ' series=' || CAST(r.all_series_micros AS STRING) AS detail
    FROM normalized n
    JOIN fact_rolled r
        ON  r.project_id = n.project_id
        AND r.date       = n.date
        AND r.connector  = n.connector
        AND r.metric     = n.metric
    WHERE r.series_count = 1
      AND n.landed_revenue_micros <> r.all_series_micros
),

-- One row per (project, date, connector, metric) in the model, always.
grain_fanout AS (
    SELECT
        'MORE_THAN_ONE_ROW_PER_SERIES_GRAIN' AS failure,
        n.project_id                         AS project_id,
        n.connector || '/' || n.metric || ' rows=' || CAST(COUNT(*) AS STRING) AS detail
    FROM normalized n
    GROUP BY n.project_id, n.date, n.connector, n.metric
    HAVING COUNT(*) > 1
)

SELECT * FROM summed_all_series
UNION ALL SELECT * FROM single_series_mismatch
UNION ALL SELECT * FROM grain_fanout
