-- Story 53.8 (CAV-13 / AC7, AC10): observation_count describes the SAME frame as
-- the estimator it is meant to qualify.
--
-- The frame is written three times in metric_baselines.sql (AVG, STDDEV_SAMP,
-- COUNT) because AC10 requires the two estimator frames to stay byte-identical
-- — the moment one of them is rewritten to share a named window, the diff stops
-- proving the estimator was not touched. Three copies can drift, so the
-- agreement is PROVEN here rather than asserted by convention:
--
--   * STDDEV_SAMP over a frame of n rows is NULL exactly when n < 2. So
--     `rolling_stddev IS NULL` must coincide with `observation_count < 2`. If
--     the COUNT frame ever widens or narrows relative to the STDDEV frame, the
--     two disagree on some row and this test returns it.
--   * The frame can never hold more than ANOMALY_BASELINE_WINDOW + 1 rows.
--   * A count is never below 1 on a row that exists.
--
-- A failing row here means the count no longer measures the estimator's own
-- window — which would make the disclosed `insufficient observations (n/N)` a
-- number about something else.

WITH baselines AS (
    SELECT * FROM {{ ref('metric_baselines') }}
)

SELECT
    project_id,
    connector,
    metric,
    date,
    observation_count,
    rolling_stddev
FROM baselines
WHERE observation_count IS NULL
   OR observation_count < 1
   OR observation_count > {{ env_var('ANOMALY_BASELINE_WINDOW', '29') | int + 1 }}
   OR (observation_count < 2 AND rolling_stddev IS NOT NULL)
   OR (observation_count >= 2 AND rolling_stddev IS NULL)
