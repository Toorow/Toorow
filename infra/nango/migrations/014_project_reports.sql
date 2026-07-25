-- Migration 014 (Story 6.1, AC7) -- Per-project report enablement + meta-alert message.
--
-- app.project_reports records the per-project opt-in enablement for each report
-- shipped by a module's reports/ pack. A report NOT present here for a project is
-- treated as DISABLED by default (opt-in model, consistent with Epic 7 module
-- enablement).
--
-- ID format: rpt_<ULID>. Note: 'rpt_' is not listed in ARCHITECTURE-SPINE §IDs;
-- it is the natural extension of the existing prefixed-ULID pattern (conn_, evt_,
-- alrt_, fire_, fb_) for a "report" entity. Documented in the Dev Agent Record.
--
-- Also adds a nullable app.alert_firings.message column consumed by the AI-32
-- meta-alert mechanism (Story 6.1, AC10): scheduler-health meta_alert rows carry a
-- human description of which nightly step failed. Additive & idempotent per the
-- CONTRIBUTING Schema Change Checklist.

BEGIN;

CREATE TABLE IF NOT EXISTS app.project_reports (
    id              TEXT        PRIMARY KEY,   -- prefixed ULID: 'rpt_'
    project_id      TEXT        NOT NULL,
    module_name     TEXT        NOT NULL,      -- e.g. 'google-analytics'
    report_id       TEXT        NOT NULL,      -- e.g. 'overview_daily' (without module prefix)
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    display_order   INTEGER     NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, module_name, report_id)
);
CREATE INDEX IF NOT EXISTS project_reports_proj ON app.project_reports (project_id);

-- AI-32 meta-alert message column (nullable; additive).
ALTER TABLE app.alert_firings ADD COLUMN IF NOT EXISTS message TEXT;

COMMIT;
