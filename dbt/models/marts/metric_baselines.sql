-- metric_baselines: rolling mean and stddev per (project, connector, metric, date).
-- Story 5.4, AD-13.
--
-- ANTI-DOUBLE-COUNT GUARD (same pattern as cross_source_conversions.sql canonical_dim):
-- fact_daily_kpi has parallel breakdown_dimension series (e.g. device_category AND
-- country) that EACH independently total the day. Summing across ALL breakdown rows
-- would produce values 2-5× too large. We pick ONE canonical breakdown_dimension per
-- (project_id, connector, metric) deterministically (MIN), then aggregate only over
-- that dimension's rows — exactly as cross_source_conversions.sql does.
-- This is an architectural requirement (Story 4.4, AD-13, review-1-6 lesson).
--
-- ANOMALY_BASELINE_WINDOW env var (default 29): trailing ROWS preceding + current.
-- 30-ROW window total, not 30 days (story 53.8): the frame is ROWS, not RANGE, so
-- a series with gaps stretches the same 30 rows over 40+ calendar days silently.
--
-- See docs/anomaly-detection-method.mdx for method documentation (AD-9), and its
-- section 1b for the (n-1)/sqrt(n) bound this frame imposes on the z-score.
{{
  config(materialized='table')
}}

WITH canonical_dim AS (
    -- Pick ONE breakdown_dimension per (project_id, date, connector, metric) deterministically.
    -- Partitioned by date so a dimension appearing mid-history does not drop earlier dates
    -- silently (review-global-gaps canonical_dim fix). Each dimension series independently
    -- totals the day; picking one per date avoids double-count.
    SELECT
        project_id,
        date,
        connector,
        metric,
        MIN(breakdown_dimension) AS dim
    FROM {{ ref('fact_daily_kpi') }}
    GROUP BY project_id, date, connector, metric
),

single_dim_totals AS (
    -- Day-level totals on the canonical dimension only (avoids double-count).
    SELECT
        f.project_id,
        f.connector,
        f.metric,
        f.date,
        SUM(f.value) AS metric_value
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN canonical_dim d
        ON d.project_id = f.project_id
       AND d.date       = f.date
       AND d.connector  = f.connector
       AND d.metric     = f.metric
       AND d.dim        = f.breakdown_dimension
    GROUP BY f.project_id, f.connector, f.metric, f.date
)

SELECT
    project_id,
    connector,
    metric,
    date,
    metric_value,
    AVG(metric_value) OVER (
        PARTITION BY project_id, connector, metric
        ORDER BY date
        ROWS BETWEEN {{ env_var('ANOMALY_BASELINE_WINDOW', '29') }} PRECEDING AND CURRENT ROW
    ) AS rolling_mean,
    STDDEV_SAMP(metric_value) OVER (
        PARTITION BY project_id, connector, metric
        ORDER BY date
        ROWS BETWEEN {{ env_var('ANOMALY_BASELINE_WINDOW', '29') }} PRECEDING AND CURRENT ROW
    ) AS rolling_stddev,
    -- Story 53.8 (CAV-13): how many observations the estimator above actually
    -- had. Same PARTITION BY / ORDER BY / ROWS frame, character for character:
    -- a count over a different frame would describe a different estimator.
    --
    -- Why it must be materialised HERE and not derived downstream:
    -- anomalies_daily keeps only rows that already cleared the threshold, so
    -- below the threshold there is no row and no count. metric_baselines has one
    -- row per project x connector x metric x date INCLUDING the days nothing
    -- fires -- which is exactly where "the detector could not yet speak" has to
    -- be readable. Nothing about the estimator changes: this is a count, not a
    -- filter, and the two frames above are untouched.
    COUNT(metric_value) OVER (
        PARTITION BY project_id, connector, metric
        ORDER BY date
        ROWS BETWEEN {{ env_var('ANOMALY_BASELINE_WINDOW', '29') }} PRECEDING AND CURRENT ROW
    ) AS observation_count
FROM single_dim_totals
WHERE metric_value IS NOT NULL
