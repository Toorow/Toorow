-- infra/nango/migrations/008_create_project_preferences.sql
--
-- Story 4.2: Create project_preferences table for canonical currency and timezone per project.
--
-- project_preferences: project-level configuration for cost/time normalization (AD-6, AD-8).
-- canonical_currency: the target currency for all cost normalization (default: EUR).
-- reporting_timezone: the project's reporting timezone for date labeling (default: Europe/Paris).
--
-- NOTE AD-8: Postgres is the sole writer for this table. BigQuery / DuckDB access is via a
-- governed read-only mirror (Story 4.4). At P4-dev (this story), a dbt seed CSV
-- (dbt/seeds/project_preferences.csv) acts as the interim bridge until Story 4.4 wires
-- the governed mirror. Do NOT treat the CSV as permanent.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/008_create_project_preferences.sql
--

BEGIN;

-- Create schema if it does not exist (idempotent)
CREATE SCHEMA IF NOT EXISTS app;

-- project_preferences: one row per project_id.
-- project_id:         Standalone PRIMARY KEY (TEXT). Note: app.connection_ref.project_id
--                     is not a unique column (multiple connections per project), so a FK
--                     reference is not feasible without a schema change to connection_ref.
--                     The application layer ensures project_id is valid (AD-8 constraint).
--                     Story 4.4 will revisit this when the governed mirror is established.
-- canonical_currency: ISO-4217 currency code (e.g. 'EUR', 'USD'). Normalization target (AD-6).
-- reporting_timezone: IANA timezone name (e.g. 'Europe/Paris'). Date-labeling policy (AD-6).
-- created_at:         UTC timestamp when this row was created.
-- updated_at:         UTC timestamp when this row was last modified.
CREATE TABLE IF NOT EXISTS app.project_preferences (
    project_id          TEXT        PRIMARY KEY,
    canonical_currency  TEXT        NOT NULL DEFAULT 'EUR',
    reporting_timezone  TEXT        NOT NULL DEFAULT 'Europe/Paris',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- NOTE: AD-8 — Postgres is the sole writer. BigQuery mirror is Story 4.4.
-- The governed mirror (mirror_*) for dbt access is deferred to 4.4.
-- This story uses a dbt vars / seed interim (see dbt/seeds/project_preferences.csv).

-- Index for project_id lookups (the primary query pattern)
CREATE INDEX IF NOT EXISTS idx_project_preferences_project_id
    ON app.project_preferences (project_id);

COMMIT;
