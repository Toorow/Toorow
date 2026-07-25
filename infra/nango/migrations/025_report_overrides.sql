-- infra/nango/migrations/025_report_overrides.sql
--
-- Story 8.7: MCP flow interface (Epic 8, Part D + Refinement R6).
--
-- Creates:
--   app.report_overrides -- per-project report-override flow documents.
--
-- WHY a new table (design decision, recorded in the story Dev Agent Record):
--   app.project_reports (migration 014) is an ENABLEMENT table
--   (id, project_id, module_name, report_id, enabled, display_order) with no JSONB
--   column to hold an override DOCUMENT. It cannot store the full flow.report doc.
--   This table holds that document additively. Nothing in migration 014 is touched.
--
-- The flows layer (server/core/flows.py) reads the base module report pack and
-- deep-merges the stored `doc` JSONB on top for flows_get. reports.py is untouched;
-- Story 8.9 wires the merged doc into rendering.
--
-- ID format: rovr_<ULID> (natural extension of the prefixed-ULID pattern:
--   conn_, evt_, alrt_, fb_, rpt_, ds_).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/025_report_overrides.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md AI-29):
--   [x] Migration is additive and idempotent (IF NOT EXISTS throughout)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
--   [x] AD-2: no module or provider names hardcoded
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.report_overrides (
    id             TEXT        NOT NULL,                 -- prefixed ULID: 'rovr_'
    project_id     TEXT        NOT NULL
                   REFERENCES app.projects(id) ON DELETE CASCADE,
    base_report_id TEXT        NOT NULL,                 -- '<module_name>/<report_def_id>'
    doc            JSONB       NOT NULL,                 -- the flow.report override document
    created_by     TEXT        NOT NULL DEFAULT 'system',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_report_overrides PRIMARY KEY (id),
    CONSTRAINT uq_report_overrides_project_base UNIQUE (project_id, base_report_id)
);

CREATE INDEX IF NOT EXISTS idx_report_overrides_project
    ON app.report_overrides (project_id);

-- updated_at trigger (shared function app.set_updated_at() created in migration 023).
DROP TRIGGER IF EXISTS trg_report_overrides_updated_at ON app.report_overrides;
CREATE TRIGGER trg_report_overrides_updated_at
    BEFORE UPDATE ON app.report_overrides
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMIT;
