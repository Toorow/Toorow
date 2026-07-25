-- Story 22.2: Media plan Excel import contracts (FR38 / CAP-26).
--
-- Additive after migrations 001-044. IF NOT EXISTS everywhere; the whole file is
-- idempotent and replayable. Objects live in schema app (Postgres is the sole
-- writer of the plan; dbt reads a governed mirror -- AD-8).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/045_media_plan_import_contracts.sql
--
-- ============================================================================
-- DESIGN NOTES
--
--   * An import contract is the REUSABLE, per-sheet mapping (Excel columns ->
--     plan fields, header row, options) that lets the same monthly workbook be
--     re-imported month after month by re-playing the SAME contract. It is keyed
--     on (plan_id, sheet_name) and UPSERTED -- one living contract per sheet, not
--     a per-import snapshot.
--   * The contract also carries the merged-cell policy (exigence Jean 2026-07-21):
--     descriptive fields forward-fill within a merge range; a merged BUDGET cell
--     collapses the covered sub-rows into ONE plan line (never a forward-fill of
--     the budget, which would multiply it by N = double-count; never a silent
--     split). Explicit "explode the group" is opt-in via contract options
--     (explode_merged_budget) with a caller-supplied split that must sum exactly.
--   * The parser + orchestration live in server/core/mediaplan_import.py; the
--     versioning machinery it drives is Story 22.1's create_version_with_lines /
--     publish_version (the import produces a CANDIDATE version; publication stays
--     an explicit second step via the 22.1 API). Convergence with the generic
--     Epic 12 import contracts (FR30/FR31) is intentionally OUT OF SCOPE here
--     (orchestration decision Jean 2026-07-21): this contract is media-plan
--     specific and stands on 22.1's already-shipped store, not on the Epic 12
--     managed-feed writer.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- gen_random_uuid() is core since PostgreSQL 13; pgcrypto also provides it.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- app.media_plan_import_contracts -- reusable per-sheet column->field mapping.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.media_plan_import_contracts (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id     UUID        NOT NULL,
    sheet_name  TEXT        NOT NULL,
    contract    JSONB       NOT NULL,   -- {header_row, columns{}, line_key, date_format, explode_merged_budget{}, ...}
    created_by  TEXT        NOT NULL,   -- identity subject (AD-14)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ON DELETE RESTRICT: a plan cannot be dropped while a contract references it
-- (same discipline as media_plan_versions -> media_plans).
ALTER TABLE app.media_plan_import_contracts
    DROP CONSTRAINT IF EXISTS fk_media_plan_import_contracts_plan;
ALTER TABLE app.media_plan_import_contracts
    ADD CONSTRAINT fk_media_plan_import_contracts_plan
    FOREIGN KEY (plan_id) REFERENCES app.media_plans(id) ON DELETE RESTRICT;

-- One living contract per (plan, sheet): the PUT endpoint upserts on this key so
-- next month's import re-uses the exact same mapping.
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_plan_import_contracts_plan_sheet
    ON app.media_plan_import_contracts (plan_id, sheet_name);

CREATE INDEX IF NOT EXISTS idx_media_plan_import_contracts_plan
    ON app.media_plan_import_contracts (plan_id);

COMMENT ON TABLE app.media_plan_import_contracts IS
    'Story 22.2 (FR38/CAP-26): reusable per-sheet Excel import contract (columns->plan fields, header row, options). Upserted per (plan, sheet) so the same monthly workbook re-imports by replaying the contract. Carries the merged-cell policy (Jean 2026-07-21): descriptive forward-fill within a merge range; a merged BUDGET cell collapses N sub-rows into ONE plan line (never forward-filled, never silently split); explicit explode is opt-in with a summing split.';
COMMENT ON COLUMN app.media_plan_import_contracts.contract IS
    'JSONB: {header_row int>=1 (1-based), columns {excel_header_or_letter: field}, line_key ("auto"|column), date_format optional (e.g. %d/%m/%Y), is_plan_only optional bool default per sheet, explode_merged_budget {range_id: [weights...]} optional}. Target fields: line_key, label, channel, start_date, end_date, budget, buy_mode, is_plan_only.';

COMMIT;
