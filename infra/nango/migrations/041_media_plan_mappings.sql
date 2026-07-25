-- Story 22.3: Media plan N:M line<->campaign mappings, splits & orphan status
-- (FR38 / CAP-26).
--
-- Additive after migrations 001-040. IF NOT EXISTS everywhere; the whole file is
-- idempotent and replayable. All objects live in schema app (Postgres is the sole
-- writer of the plan; dbt reads a governed mirror -- AD-8).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/041_media_plan_mappings.sql
--
-- ============================================================================
-- DESIGN NOTES (decisions from epic-22-import-mediaplans.md + spike-22-0 §5)
--
--   * A plan line is mapped N:M to 1..N real campaigns/placements; a campaign
--     may itself be shared across 1..N lines. The mapping NEVER touches facts
--     (AD-4/AD-6): it is a JOIN + weighting layer read by the 22.4 mart. The
--     connectors' totals stay byte-identical with or without a mapping.
--
--   * THE POINT DUR -- identity across versions. The mapping is keyed on the
--     STABLE line_key (plan_id, line_key), NOT on the versioned media_plan_lines
--     row id. A line re-imported into a new version keeps the SAME line_key, so
--     the mapping survives ("le mapping survit aux ré-imports"). A line that
--     disappears from the active version has its mapping flagged 'orphaned'
--     (visible, re-attachable), never silently deleted. That is why the FK is on
--     app.media_plans(id) (the plan root, which outlives every version) and NOT
--     on media_plan_lines(id).
--
--   * campaign_ref -- HONEST CHOICE, backed by the real fact_daily_kpi schema.
--     fact_daily_kpi has NO dedicated "campaign" column. Its grain is
--       (project_id, date, connector, metric, breakdown_dimension,
--        breakdown_value, value, pull_id, loaded_at)
--     [see dbt/models/marts/fact_daily_kpi.sql]. The campaign identity of a paid
--     connector is carried in breakdown_value WHEN breakdown_dimension =
--     'campaign_id' (proven by the meta-ads / tiktok-ads / linkedin-ads blocks,
--     each of which emits breakdown_dimension='campaign_id',
--     breakdown_value=<the régie campaign id>; klaviyo uses 'campaign_id' too).
--     So the finest campaign reference actually available is:
--         campaign_ref := fact_daily_kpi.breakdown_value
--                         WHERE breakdown_dimension = 'campaign_id'.
--     We store campaign_ref as this breakdown_value string, scoped by connector.
--     Spend is metric = 'cost' (the additive spend metric emitted by every paid
--     régie block). The 22.4 mart / list_unmapped_actuals join on
--         (connector, breakdown_dimension='campaign_id', breakdown_value=campaign_ref).
--
--   * SUM(split_weight) = 1.0 per (plan_id, connector, campaign_ref) is NOT a
--     CHECK: it is an AGGREGATE invariant across rows, which a row CHECK cannot
--     express. It is enforced by the store (mediaplan_mapping.set_line_mappings)
--     INSIDE the transaction, under a per-plan SELECT ... FOR UPDATE lock on
--     app.media_plans so concurrent writes cannot produce a sum != 1.0. See the
--     COMMENT ON CONSTRAINT / TABLE below.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- gen_random_uuid() is core since PostgreSQL 13; pgcrypto also provides it on
-- older servers (harmless IF NOT EXISTS on PG13+).
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- app.plan_line_mappings -- N:M line<->campaign mapping with split weights.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.plan_line_mappings (
    id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id      UUID          NOT NULL,             -- plan root (survives versions)
    line_key     TEXT          NOT NULL,             -- STABLE line identity (NOT the versioned row id)
    connector    TEXT          NOT NULL,             -- fact_daily_kpi.connector
    campaign_ref TEXT          NOT NULL,             -- fact_daily_kpi.breakdown_value @ breakdown_dimension='campaign_id'
    split_weight NUMERIC(7,6)  NOT NULL
                 CHECK (split_weight > 0 AND split_weight <= 1),
    status       TEXT          NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active', 'orphaned')),
    created_by   TEXT          NOT NULL,             -- identity subject (AD-14)
    created_at   TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ   NOT NULL DEFAULT now()
);

-- ON DELETE RESTRICT: a mapping pins the plan root; the plan cannot be hard
-- deleted while mappings reference it (mappings are removed explicitly first).
ALTER TABLE app.plan_line_mappings DROP CONSTRAINT IF EXISTS fk_plan_line_mappings_plan;
ALTER TABLE app.plan_line_mappings
    ADD CONSTRAINT fk_plan_line_mappings_plan
    FOREIGN KEY (plan_id) REFERENCES app.media_plans(id) ON DELETE RESTRICT;

-- One mapping row per (plan, line, connector, campaign): a line maps a given
-- campaign of a given connector at most once (its split_weight).
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_line_mappings_identity
    ON app.plan_line_mappings (plan_id, line_key, connector, campaign_ref);

-- Validation-sum index: the store aggregates split_weight per
-- (plan_id, connector, campaign_ref) to enforce SUM=1.0; this index makes that
-- grouping fast and is the natural read path for the 22.4 ventilation.
CREATE INDEX IF NOT EXISTS idx_plan_line_mappings_campaign
    ON app.plan_line_mappings (plan_id, connector, campaign_ref);

-- Read path for "all mappings of a plan, grouped by line".
CREATE INDEX IF NOT EXISTS idx_plan_line_mappings_plan_line
    ON app.plan_line_mappings (plan_id, line_key);

COMMENT ON TABLE app.plan_line_mappings IS
    'Story 22.3 (FR38/CAP-26): N:M plan line<->campaign mapping with split weights. '
    'Keyed on the STABLE (plan_id, line_key) so mappings survive re-imports (the "point dur"); '
    'a line dropped from the active version -> status=''orphaned'', never deleted. '
    'INVARIANT SUM(split_weight)=1.0 per (plan_id, connector, campaign_ref) is enforced in '
    'mediaplan_mapping.set_line_mappings inside the transaction under a per-plan '
    'SELECT ... FOR UPDATE on app.media_plans -- it is an aggregate rule a row CHECK cannot express.';
COMMENT ON COLUMN app.plan_line_mappings.line_key IS
    'STABLE cross-version line identity (media_plan_lines.line_key), NOT the versioned row id -- the mapping survives re-imports.';
COMMENT ON COLUMN app.plan_line_mappings.campaign_ref IS
    'fact_daily_kpi.breakdown_value WHERE breakdown_dimension=''campaign_id'' (the finest campaign reference the fact grain exposes; fact_daily_kpi has no dedicated campaign column). Scoped by connector.';
COMMENT ON COLUMN app.plan_line_mappings.split_weight IS
    'Fraction (0,1] of the campaign spend ventilated to this line. SUM over (plan_id, connector, campaign_ref) MUST equal 1.0 exactly (enforced in the store, Decimal, zero tolerance).';
COMMENT ON COLUMN app.plan_line_mappings.status IS
    'active | orphaned. orphaned = the line_key no longer exists in the plan''s active version (refresh_orphan_status recomputes it at each publication).';

COMMIT;
