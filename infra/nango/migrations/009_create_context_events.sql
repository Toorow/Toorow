-- infra/nango/migrations/009_create_context_events.sql
--
-- Story 4.3: Create context_events table for project-scoped business annotations.
--
-- context_events: Postgres-owned, project-scoped annotations (AD-8, AD-9).
-- id:             Prefixed ULID: 'evt_' (ARCHITECTURE-SPINE §IDs).
-- event_date:     The business date of the event (not created_at).
-- type:           Free-form TEXT (not enum) so Story 4.5 can add 'deployment'/'release'
--                 without a migration.
-- created_by:     Identity from token (AD-14). Never stores token value (AD-3).
--
-- No FK at this stage (events exist per-project, not per-connection).
-- BigQuery access via governed read-only mirror (Story 4.4).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/009_create_context_events.sql
--

BEGIN;

-- Create schema if it does not exist (idempotent)
CREATE SCHEMA IF NOT EXISTS app;

-- context_events: one row per business event per project.
-- id:           Prefixed ULID primary key ('evt_<ULID>'). No serial -- application mints.
-- project_id:   Project scope (AD-8). No FK: connection_ref.project_id is not unique.
-- event_date:   DATE of the business event (not the insertion time).
-- type:         Free-form TEXT ('business', 'incident', 'deployment', 'release', ...).
--               Not an enum so Story 4.5 can insert new types without a migration.
-- label:        Short title (application enforces <=120 chars).
-- description:  Optional free-form detail.
-- created_by:   Identity string from token (AD-14). Never the token itself (AD-3).
-- created_at:   UTC insertion timestamp.
CREATE TABLE IF NOT EXISTS app.context_events (
    id              TEXT        PRIMARY KEY,
    project_id      TEXT        NOT NULL,
    event_date      DATE        NOT NULL,
    type            TEXT        NOT NULL,
    label           TEXT        NOT NULL,
    description     TEXT,
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Index for the primary query pattern: events by project and date window.
CREATE INDEX IF NOT EXISTS ctx_events_project_date
    ON app.context_events (project_id, event_date);

COMMIT;
