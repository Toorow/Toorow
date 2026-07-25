-- 013: alert_firings fixes from review-epic-5 (F-1, F-2).
--
-- F-1: anomaly firings need project scoping and a metric name — and the
--      NOT NULL definition_id would have REJECTED every anomaly insert
--      (type='anomaly' has no definition; masked by mocked tests).
-- F-2: nightly dedup — one firing per (type, definition/metric, window_date);
--      a persistent breach must not spam a new row every night.

BEGIN;

ALTER TABLE app.alert_firings ALTER COLUMN definition_id DROP NOT NULL;
ALTER TABLE app.alert_firings ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT 'default';
ALTER TABLE app.alert_firings ADD COLUMN IF NOT EXISTS metric TEXT NOT NULL DEFAULT '';

-- F-2 dedup guards (partial unique indexes per firing kind)
CREATE UNIQUE INDEX IF NOT EXISTS alert_firings_dedup_business
    ON app.alert_firings (definition_id, window_date)
    WHERE type = 'business_threshold' AND definition_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS alert_firings_dedup_anomaly
    ON app.alert_firings (project_id, metric, window_date)
    WHERE type = 'anomaly';

COMMIT;
