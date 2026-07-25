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
-- ANOMALY_BASELINE_WINDOW env var (default 29): trailing days preceding + current.
-- 30-day window total: 29 preceding rows + current row.
--
-- See docs/anomaly-detection-method.md for method documentation (AD-9).
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
    ) AS rolling_stddev
FROM single_dim_totals
WHERE metric_value IS NOT NULL
