-- infra/nango/migrations/055_context_events_mmm_cols.sql
--
-- Epic 31 (story 31.2): events as a first-class capability + MMM finality.
--
-- Additive, NULLABLE columns on app.context_events -- zero impact on existing
-- consumers (all pre-31 rows keep NULL platform/value, source defaults 'manual').
--
--   platform : level 1 of the event identity platform>type>label (e.g. 'youtube',
--              'github'). NULL for legacy manual events.
--   value    : optional MMM regressor magnitude (e.g. promo depth, launch scale).
--              NULL = a unit pulse (the default: a pure temporal marker).
--   source   : 'manual' (console) vs '<connector>' (emitting connector). Lets the
--              overlay/filter and the MMM feature-builder split manual vs connector
--              events, and gates the canonical-vocabulary enforcement
--              (connector events must use a dim_event_type.csv type; manual are free).
--
-- 'type' stays free-form TEXT (migration 009) and now carries the canonical
-- event_type for connector events (governed by dbt/seeds/dim_event_type.csv).
--
-- The DuckDB governed mirror (mirror.context_events, mirror_sync.py) must select
-- the new columns so the analytical read path (overlay + MMM) sees them.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/055_context_events_mmm_cols.sql

BEGIN;

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS platform TEXT NULL;

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS value NUMERIC NULL;

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'manual';

-- Filter helper for the overlay/MMM read pattern (events by project, platform, date).
CREATE INDEX IF NOT EXISTS ctx_events_project_platform_date
    ON app.context_events (project_id, platform, event_date);

COMMIT;
