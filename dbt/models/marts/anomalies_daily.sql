-- anomalies_daily: daily z-score anomaly detection per (project, connector, metric, date).
-- Story 5.4, AD-13.
--
-- Method: simple z-score = (observed_value - rolling_mean) / rolling_stddev
-- Threshold: |z| >= ANOMALY_Z_THRESHOLD (default 3.0, env var).
-- Severity: warning for 3.0 <= |z| < 5.0, error for |z| >= 5.0.
--
-- Transparent method — no black box (AD-9). The exact formula is visible here.
-- Full documentation: docs/anomaly-detection-method.md
--
-- Double-count guard: inherited from metric_baselines (canonical_dim pattern).
-- No additional breakdown filter needed here.
--
-- When rolling_stddev is NULL or 0 (insufficient history, e.g. < 2 days), no
-- anomaly is flagged. This prevents division by zero and spurious day-1 alerts.
--
-- ANOMALY_Z_THRESHOLD env var (default 3.0). Documented in .env.example.
{{
  config(materialized='table')
}}
WITH baselines AS (
    SELECT * FROM {{ ref('metric_baselines') }}
),
zscores AS (
    SELECT
        project_id,
        connector,
        metric,
        date,
        metric_value AS observed_value,
        rolling_mean AS expected_value,
        rolling_stddev,
        CASE
            WHEN rolling_stddev IS NULL OR rolling_stddev = 0 THEN NULL
            ELSE (metric_value - rolling_mean) / rolling_stddev
        END AS zscore
    FROM baselines
)
SELECT *
FROM zscores
WHERE zscore IS NOT NULL
  AND ABS(zscore) >= COALESCE(TRY_CAST({{ env_var('ANOMALY_Z_THRESHOLD', '3.0') }} AS FLOAT), 3.0)
