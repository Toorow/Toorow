-- Migration 011 (Story 5.3, AC1) -- Declared business threshold alert tables.
--
-- Design decision (T1.2): ONE alert_firings table with a `type` column.
-- Story 5.4 will reuse this table by inserting rows with type='anomaly'.
-- Keeping one table reduces schema fragmentation and simplifies cross-type queries
-- (e.g. "show all firings for this project today"). The `type` column is an
-- explicit discriminator column pattern (not inheritance). If Story 5.4 needs
-- additional columns specific to anomaly firings, they can be added as nullable
-- columns to this table without breaking existing business_threshold rows.
--
-- ID format: alrt_<ULID> and fire_<ULID> per ARCHITECTURE-SPINE §IDs pattern.
-- pull_ids TEXT[]: Postgres array of pull_id strings providing provenance (AD-9).

BEGIN;

CREATE TABLE IF NOT EXISTS app.alert_definitions (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'alrt_'
    project_id      TEXT        NOT NULL,
    metric          TEXT        NOT NULL,             -- canonical metric name (dim_metric or semantic view)
    operator        TEXT        NOT NULL,             -- '>' | '<' | '>=' | '<=' (whitelist enforced at API)
    threshold       NUMERIC     NOT NULL,
    connector       TEXT,                            -- NULL = all connectors for this project
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS alert_def_project ON app.alert_definitions (project_id, enabled);

CREATE TABLE IF NOT EXISTS app.alert_firings (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'fire_'
    definition_id   TEXT        NOT NULL REFERENCES app.alert_definitions(id),
    type            TEXT        NOT NULL DEFAULT 'business_threshold',  -- 'business_threshold' | 'anomaly' (Story 5.4 reuses)
    fired_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    observed_value  NUMERIC     NOT NULL,
    threshold       NUMERIC     NOT NULL,
    pull_ids        TEXT[]      NOT NULL DEFAULT '{}',  -- provenance: pull_ids contributing to observed_value
    window_date     DATE        NOT NULL,               -- which business day this firing covers
    severity        TEXT        NOT NULL DEFAULT 'error'  -- 'error' | 'warning'
);

CREATE INDEX IF NOT EXISTS alert_firings_def ON app.alert_firings (definition_id, fired_at DESC);
CREATE INDEX IF NOT EXISTS alert_firings_project ON app.alert_firings (window_date DESC);

COMMIT;
