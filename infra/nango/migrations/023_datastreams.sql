-- infra/nango/migrations/023_datastreams.sql
--
-- Story 8.2: Datastream model and scheduler integration (Epic 8, Part A).
--
-- Creates:
--   app.datastreams          -- one extract stream per (connection, report_profile)
--   app.target_fields        -- data dictionary seeded from dim_metric + manifests
--   app.datastream_mappings  -- source_field -> target_field per datastream
--
-- Alters:
--   app.pull_jobs            -- adds datastream_id FK (nullable, for backward compat)
--
-- Seeds:
--   app.target_fields        -- canonical metrics + core dimensions (is_default=true)
--
-- Backfill note: one datastream per (connection_ref x report_profile) is NOT done
-- in SQL because manifest report_profile data lives in module files, not in SQL.
-- Run the Python backfill after applying this migration:
--
--   uv run python -c "from core.datastreams import backfill_datastreams; print(backfill_datastreams())"
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/023_datastreams.sql
--
-- Idempotency: uses IF NOT EXISTS / DROP CONSTRAINT IF EXISTS / ON CONFLICT DO NOTHING
-- throughout. Safe to re-run.
--
-- AI-44: file named 023_datastreams.sql (zero-padded 3-digit prefix, next in sequence).
-- AD-2: no module or provider names are hardcoded in this migration.
-- Schema-Change-Checklist (CONTRIBUTING.md AI-29):
--   [x] Migration is additive and idempotent
--   [x] New columns are NULL-able or have defaults
--   [x] IF NOT EXISTS used throughout
--   [x] No destructive DROP/ALTER on populated columns
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. updated_at trigger function (shared, reusable by new tables).
--    CREATE OR REPLACE is idempotent.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- 2. app.datastreams
--
-- One row per (project, connection_ref, report_profile).
-- Scheduler iterates enabled datastreams instead of bare connections (Story 8.2).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastreams (
    id                 TEXT        NOT NULL,
    project_id         TEXT        NOT NULL
                       REFERENCES app.projects(id) ON DELETE RESTRICT,
    name               TEXT        NOT NULL,
    module_name        TEXT        NOT NULL,
    connection_ref_id  TEXT
                       REFERENCES app.connection_ref(id) ON DELETE RESTRICT,
    report_profile_id  TEXT,
    enabled            BOOLEAN     NOT NULL DEFAULT TRUE,
    schedule_mode      TEXT        NOT NULL DEFAULT 'nightly'
                       CHECK (schedule_mode IN ('nightly', 'manual')),
    refetch_days       INT         NOT NULL DEFAULT 3
                       CHECK (refetch_days >= 1 AND refetch_days <= 90),
    date_window_days   INT         NOT NULL DEFAULT 30
                       CHECK (date_window_days >= 1 AND date_window_days <= 365),
    config             JSONB,
    created_by         TEXT        NOT NULL DEFAULT 'system',
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastreams PRIMARY KEY (id),
    CONSTRAINT uq_datastreams_project_name UNIQUE (project_id, name)
);

CREATE INDEX IF NOT EXISTS idx_datastreams_project_id
    ON app.datastreams (project_id);
CREATE INDEX IF NOT EXISTS idx_datastreams_connection_ref_id
    ON app.datastreams (connection_ref_id);
CREATE INDEX IF NOT EXISTS idx_datastreams_enabled
    ON app.datastreams (project_id, enabled);

-- updated_at trigger (idempotent: DROP IF EXISTS before CREATE).
DROP TRIGGER IF EXISTS trg_datastreams_updated_at ON app.datastreams;
CREATE TRIGGER trg_datastreams_updated_at
    BEFORE UPDATE ON app.datastreams
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. app.target_fields (data dictionary)
--
-- Seeded below with canonical metrics from dbt/seeds/dim_metric.csv semantics
-- and core dimensions. User-created fields have is_default=false.
-- PK is name (snake_case, immutable).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.target_fields (
    name          TEXT        NOT NULL,
    display_name  TEXT        NOT NULL,
    data_type     TEXT        NOT NULL
                  CHECK (data_type IN ('string', 'integer', 'decimal', 'date',
                                       'boolean', 'currency')),
    field_kind    TEXT        NOT NULL
                  CHECK (field_kind IN ('metric', 'dimension')),
    measure       TEXT
                  CHECK (measure IN ('sum', 'average', 'min', 'max', 'count', NULL)),
    description   TEXT,
    created_by    TEXT        NOT NULL DEFAULT 'system',
    is_default    BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_target_fields PRIMARY KEY (name)
);

CREATE INDEX IF NOT EXISTS idx_target_fields_kind
    ON app.target_fields (field_kind);

-- ---------------------------------------------------------------------------
-- 4. Seed app.target_fields — canonical metrics (is_default=true, created_by='system').
--    Sources: dbt/seeds/dim_metric.csv + Epic 8 Part A spec.
--    ON CONFLICT DO NOTHING makes this idempotent.
-- ---------------------------------------------------------------------------

-- Metrics
INSERT INTO app.target_fields (name, display_name, data_type, field_kind, measure, description, created_by, is_default)
VALUES
    ('sessions',          'Sessions',          'integer', 'metric', 'sum',
     'Number of sessions initiated by users.', 'system', TRUE),
    ('active_users',      'Active users',      'integer', 'metric', 'sum',
     'Number of unique active users.', 'system', TRUE),
    ('conversions',       'Conversions',       'integer', 'metric', 'sum',
     'Number of conversion events (goals reached).', 'system', TRUE),
    ('clicks',            'Clicks',            'integer', 'metric', 'sum',
     'Number of clicks on ads or links.', 'system', TRUE),
    ('impressions',       'Impressions',       'integer', 'metric', 'sum',
     'Number of times an ad or content was displayed.', 'system', TRUE),
    ('cost',              'Cost',              'currency', 'metric', 'sum',
     'Total advertising spend.', 'system', TRUE),
    ('revenue',           'Revenue',           'currency', 'metric', 'sum',
     'Total attributed revenue.', 'system', TRUE),
    ('average_position',  'Average position',  'decimal', 'metric', 'average',
     'Average position in search results. Note: impression-weighted average '
     '(non-additive) — use the AD-4 semantic views.', 'system', TRUE),
    ('deployments',       'Deployments',       'integer', 'metric', 'count',
     'Number of deployments (github context module).', 'system', TRUE)
ON CONFLICT (name) DO NOTHING;

-- Dimensions
INSERT INTO app.target_fields (name, display_name, data_type, field_kind, measure, description, created_by, is_default)
VALUES
    ('date',             'Date',               'date',    'dimension', NULL,
     'Daily time dimension (day granularity).', 'system', TRUE),
    ('device_category',  'Device category',    'string', 'dimension', NULL,
     'Device category (desktop, mobile, tablet).', 'system', TRUE),
    ('country',          'Country',            'string',  'dimension', NULL,
     'ISO-3166 country code.', 'system', TRUE),
    ('page',             'Page',               'string',  'dimension', NULL,
     'Page URL or path.', 'system', TRUE)
ON CONFLICT (name) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 5. app.datastream_mappings
--
-- Maps source_field (as reported by the connector) to target_field (data dict).
-- target_field is nullable when unmapped.
-- PK: (datastream_id, source_field).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_mappings (
    datastream_id  TEXT        NOT NULL
                   REFERENCES app.datastreams(id) ON DELETE CASCADE,
    source_field   TEXT        NOT NULL,
    target_field   TEXT
                   REFERENCES app.target_fields(name) ON DELETE SET NULL,
    is_key_column  BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_mappings PRIMARY KEY (datastream_id, source_field)
);

CREATE INDEX IF NOT EXISTS idx_datastream_mappings_target_field
    ON app.datastream_mappings (target_field);

-- ---------------------------------------------------------------------------
-- 6. ALTER app.pull_jobs -- add datastream_id FK (nullable, backward compat).
-- ---------------------------------------------------------------------------
ALTER TABLE app.pull_jobs
    ADD COLUMN IF NOT EXISTS datastream_id TEXT;

-- Add FK constraint (idempotent: DROP IF EXISTS first).
ALTER TABLE app.pull_jobs
    DROP CONSTRAINT IF EXISTS fk_pull_jobs_datastream;
ALTER TABLE app.pull_jobs
    ADD CONSTRAINT fk_pull_jobs_datastream
    FOREIGN KEY (datastream_id) REFERENCES app.datastreams(id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_pull_jobs_datastream_id
    ON app.pull_jobs (datastream_id)
    WHERE datastream_id IS NOT NULL;

COMMIT;
