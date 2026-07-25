-- Story 22.1: Media plan domain model (FR38 / CAP-26).
--
-- Additive after migrations 001-039. IF NOT EXISTS everywhere; the whole file is
-- idempotent and replayable. All objects live in schema app (Postgres is the sole
-- writer of the plan; dbt reads a governed mirror -- AD-8).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/040_media_plans.sql
--
-- ============================================================================
-- DESIGN NOTES (decisions from spike-22-0-synthese-croisee.md §5, FIGE)
--
--   * A media plan is the top-down investment intent; connectors report the
--     bottom-up execution. The plan NEVER touches facts (AD-4/AD-6) -- these
--     tables only add a join layer.
--   * Versions are APPEND-ONLY (decision: each Excel import = a new version;
--     immutable history preserves "what was expected at the time"). A single
--     active pointer per plan; the pointer flips atomically at publication.
--     Same immutability pattern as app.*_versions in migration 031 (Story 11.1),
--     including the statement-level BEFORE TRUNCATE guard added in review 11.1.
--   * Lines carry their OWN date range (decision 4: nothing is monthly; the plan
--     has no dates of its own -- the plan range is merely the envelope of its
--     lines). Therefore there is NO plan-level "line dates within plan dates"
--     CHECK: the plan has no dates to constrain against. The only date CHECK is
--     line-internal (start_date <= end_date).
--   * plan_allocation_daily is the deterministic daily spread, materialised at
--     publication (decision 1/2). It is immutable once its version is published:
--     an allocation row may only be inserted/deleted while its version is still
--     'candidate'; a published version's allocations are frozen like the version.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- gen_random_uuid() is core since PostgreSQL 13; pgcrypto also provides it on
-- older servers. Creating the extension IF NOT EXISTS is harmless on PG13+ and
-- keeps the default UUID generator available everywhere.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 1. app.media_plans -- the plan root (project, name, currency).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.media_plans (
    id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    project_id  TEXT        NOT NULL,
    name        TEXT        NOT NULL,
    currency    TEXT        NOT NULL DEFAULT 'EUR',
    created_by  TEXT        NOT NULL,               -- identity subject (AD-14)
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at TIMESTAMPTZ                          -- set on archive, NULL while active
);

ALTER TABLE app.media_plans DROP CONSTRAINT IF EXISTS fk_media_plans_project;
ALTER TABLE app.media_plans
    ADD CONSTRAINT fk_media_plans_project
    FOREIGN KEY (project_id) REFERENCES app.projects(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_media_plans_project ON app.media_plans (project_id);

-- Unique plan name per project, EXCLUDING archived plans, so an archived plan
-- name can be reused (mirrors the partial-unique pattern of migration 031).
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_plans_project_name
    ON app.media_plans (project_id, name) WHERE archived_at IS NULL;

COMMENT ON TABLE app.media_plans IS
    'Story 22.1 (FR38/CAP-26): media plan root. Top-down investment intent; never mutates facts (AD-4/AD-6).';
COMMENT ON COLUMN app.media_plans.currency IS
    'Plan currency (decision: single currency per plan; multi-currency plan<->régie is Phase B, FX path AD-6).';

-- ---------------------------------------------------------------------------
-- 2. app.media_plan_versions -- append-only immutable versions, one active
--    pointer per plan, atomic flip at publication (decision 1/2, pattern 11.1).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.media_plan_versions (
    id             UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id        UUID        NOT NULL,
    version_number INTEGER     NOT NULL CHECK (version_number >= 1),
    status         TEXT        NOT NULL DEFAULT 'candidate'
                   CHECK (status IN ('candidate', 'published')),
    is_active      BOOLEAN     NOT NULL DEFAULT false,
    source_note    TEXT,                              -- e.g. imported file name (nullable)
    created_by     TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE app.media_plan_versions DROP CONSTRAINT IF EXISTS fk_media_plan_versions_plan;
ALTER TABLE app.media_plan_versions
    ADD CONSTRAINT fk_media_plan_versions_plan
    FOREIGN KEY (plan_id) REFERENCES app.media_plans(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_plan_versions_plan_number
    ON app.media_plan_versions (plan_id, version_number);

CREATE INDEX IF NOT EXISTS idx_media_plan_versions_plan
    ON app.media_plan_versions (plan_id);

-- Exactly one active version per plan (partial unique index).
CREATE UNIQUE INDEX IF NOT EXISTS uq_media_plan_versions_one_active
    ON app.media_plan_versions (plan_id) WHERE is_active;

COMMENT ON TABLE app.media_plan_versions IS
    'Story 22.1: immutable append-only plan versions. One active pointer per plan; atomic flip at publish (pattern 11.1).';

-- ---------------------------------------------------------------------------
-- 3. app.media_plan_lines -- version-scoped lines with a STABLE line_key that
--    survives re-imports (the mapping in 22.3 is keyed on (plan_id, line_key),
--    never on the versioned row id -- see epic "point dur").
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.media_plan_lines (
    id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id   UUID          NOT NULL,
    line_key     TEXT          NOT NULL,             -- stable inter-version identity / upsert key
    label        TEXT          NOT NULL DEFAULT '',
    channel      TEXT,                                -- "support" (nullable)
    start_date   DATE          NOT NULL,
    end_date     DATE          NOT NULL,
    budget       NUMERIC(14,2) NOT NULL CHECK (budget >= 0),
    buy_mode     TEXT,                                -- purchase mode (nullable)
    is_plan_only BOOLEAN       NOT NULL DEFAULT false,-- offline line (TV, OOH): no actuals to pace
    sort_order   INTEGER       NOT NULL DEFAULT 0,
    CONSTRAINT media_plan_lines_dates_check CHECK (start_date <= end_date)
);

-- ON DELETE RESTRICT: a line cannot be deleted while allocations reference it.
ALTER TABLE app.media_plan_lines DROP CONSTRAINT IF EXISTS fk_media_plan_lines_version;
ALTER TABLE app.media_plan_lines
    ADD CONSTRAINT fk_media_plan_lines_version
    FOREIGN KEY (version_id) REFERENCES app.media_plan_versions(id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_plan_lines_version_key
    ON app.media_plan_lines (version_id, line_key);

CREATE INDEX IF NOT EXISTS idx_media_plan_lines_version
    ON app.media_plan_lines (version_id);

COMMENT ON TABLE app.media_plan_lines IS
    'Story 22.1: version-scoped plan lines. line_key is the stable cross-version identity (upsert key for imports / mapping in 22.3).';
COMMENT ON COLUMN app.media_plan_lines.is_plan_only IS
    'Offline line without an actuals source (decision 7): honest plan-only, no pacing computed, never a misleading 0%.';
-- NO plan-level "dates within plan" CHECK: the plan owns no dates (decision 4).
-- The plan range is derived as the envelope of its lines; only the line-internal
-- start<=end invariant is enforced (media_plan_lines_dates_check above).

-- ---------------------------------------------------------------------------
-- 4. app.plan_allocation_daily -- deterministic daily spread, materialised at
--    publication (decision 1/2). Immutable once its version is published.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.plan_allocation_daily (
    version_id UUID          NOT NULL,
    line_id    UUID          NOT NULL,
    day        DATE          NOT NULL,
    amount     NUMERIC(14,2) NOT NULL,
    PRIMARY KEY (version_id, line_id, day)
);

ALTER TABLE app.plan_allocation_daily DROP CONSTRAINT IF EXISTS fk_plan_allocation_daily_version;
ALTER TABLE app.plan_allocation_daily
    ADD CONSTRAINT fk_plan_allocation_daily_version
    FOREIGN KEY (version_id) REFERENCES app.media_plan_versions(id) ON DELETE RESTRICT;

ALTER TABLE app.plan_allocation_daily DROP CONSTRAINT IF EXISTS fk_plan_allocation_daily_line;
ALTER TABLE app.plan_allocation_daily
    ADD CONSTRAINT fk_plan_allocation_daily_line
    FOREIGN KEY (line_id) REFERENCES app.media_plan_lines(id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_plan_allocation_daily_line
    ON app.plan_allocation_daily (line_id);

COMMENT ON TABLE app.plan_allocation_daily IS
    'Story 22.1: deterministic daily spread (largest-remainder, at the cent). SUM(amount)=line budget. Materialised at publish; never hand-edited.';

-- ---------------------------------------------------------------------------
-- 5. Immutability triggers.
-- ---------------------------------------------------------------------------

-- 5a. media_plan_versions: append-only EXCEPT the controlled transitions a
--     publish performs: flipping is_active (either direction) and status
--     candidate->published. Any other column change, and any DELETE, is blocked.
CREATE OR REPLACE FUNCTION app.reject_media_plan_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'app.media_plan_versions is append-only: DELETE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;

    -- UPDATE: only is_active and (candidate->published) status may change.
    IF NEW.id IS DISTINCT FROM OLD.id
        OR NEW.plan_id IS DISTINCT FROM OLD.plan_id
        OR NEW.version_number IS DISTINCT FROM OLD.version_number
        OR NEW.source_note IS DISTINCT FROM OLD.source_note
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'app.media_plan_versions is append-only: only is_active and status(candidate->published) may change'
            USING ERRCODE = 'raise_exception';
    END IF;

    -- status may only move candidate->published (never published->candidate, and
    -- an unchanged status is fine so a pure is_active flip passes).
    IF NEW.status IS DISTINCT FROM OLD.status
        AND NOT (OLD.status = 'candidate' AND NEW.status = 'published')
    THEN
        RAISE EXCEPTION 'app.media_plan_versions: status may only transition candidate->published'
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_media_plan_versions_immutable ON app.media_plan_versions;
CREATE TRIGGER trg_media_plan_versions_immutable
    BEFORE UPDATE OR DELETE ON app.media_plan_versions
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_media_plan_version_mutation();

-- 5b. plan_allocation_daily: a published version's allocations are frozen.
--     UPDATE is always blocked; DELETE is allowed ONLY while the owning version
--     is still 'candidate' (so a re-publication can regenerate a candidate's
--     rows). INSERT is unconstrained (the store only inserts on candidates).
CREATE OR REPLACE FUNCTION app.guard_plan_allocation_daily_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    v_status TEXT;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'app.plan_allocation_daily is immutable: UPDATE blocked'
            USING ERRCODE = 'raise_exception';
    END IF;

    -- DELETE: allowed only if the owning version is still a candidate.
    SELECT status INTO v_status
    FROM app.media_plan_versions
    WHERE id = OLD.version_id;

    IF v_status IS DISTINCT FROM 'candidate' THEN
        RAISE EXCEPTION 'app.plan_allocation_daily: cannot DELETE allocations of a published version'
            USING ERRCODE = 'raise_exception';
    END IF;

    RETURN OLD;
END;
$$;

DROP TRIGGER IF EXISTS trg_plan_allocation_daily_guard ON app.plan_allocation_daily;
CREATE TRIGGER trg_plan_allocation_daily_guard
    BEFORE UPDATE OR DELETE ON app.plan_allocation_daily
    FOR EACH ROW
    EXECUTE FUNCTION app.guard_plan_allocation_daily_mutation();

-- 5c. Statement-level BEFORE TRUNCATE guards. Row-level triggers do NOT fire on
--     TRUNCATE; the owner keeps TRUNCATE privilege regardless of REVOKE. A
--     statement-level trigger closes this gap (review 11.1 lesson, migration 004).
CREATE OR REPLACE FUNCTION app.reject_media_plan_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Media plan versioned/allocation tables are append-only: TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS trg_media_plan_versions_no_truncate ON app.media_plan_versions;
CREATE TRIGGER trg_media_plan_versions_no_truncate
    BEFORE TRUNCATE ON app.media_plan_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_media_plan_truncate();

DROP TRIGGER IF EXISTS trg_plan_allocation_daily_no_truncate ON app.plan_allocation_daily;
CREATE TRIGGER trg_plan_allocation_daily_no_truncate
    BEFORE TRUNCATE ON app.plan_allocation_daily
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_media_plan_truncate();

COMMIT;
