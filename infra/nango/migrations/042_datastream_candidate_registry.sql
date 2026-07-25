-- infra/nango/migrations/042_datastream_candidate_registry.sql
--
-- Story 12.5: atomic candidate publication -- the candidate registry, the
-- append-only publication log, the publication-event outbox, and the atomic
-- published pointer on app.datastreams.
--
-- MIGRATION NUMBER: 042. The story text was authored against a provisional 040,
-- but 040_media_plans.sql and 041_media_plan_mappings.sql (Epic 22, a parallel
-- session) landed first in the SINGLE shared migrations directory. 041 is the
-- highest applied migration; 042 is the next free integer. All 12.5 story/test
-- references use 042.
--
-- Additive and idempotent after migration 041. Every statement is
-- IF NOT EXISTS / OR REPLACE / pg_constraint-guarded so re-application is a
-- no-op (matches the 030/032/033 style exactly).
--
-- It introduces:
--   * app.datastream_executions      -- the candidate registry (typed state
--                                        machine + provenance: execution_id,
--                                        mapping_version_id, plan_version_id).
--   * app.datastream_publication_log  -- append-only history of every pointer
--                                        swap, with rollback evidence. Immutable
--                                        via a BEFORE UPDATE OR DELETE trigger
--                                        mirroring trg_datastream_plan_versions_
--                                        immutable from 030.
--   * app.datastream_outbox           -- publication-event relay. NOT append-only
--                                        (processed_at may be set by a poller).
--   * app.datastreams.current_published_execution_id -- the atomic pointer.
--   * app.project_preferences.max_row_count_delta_pct / allow_empty_publication
--                                        -- project-scoped DQ-gate thresholds.
--
-- Invariants held:
--   AD-8  : Postgres is the sole writer of these tables; dbt owns the mart write.
--           12.5 writes NO BigQuery data -- this migration is pure Postgres state.
--   AD-9  : provenance columns are honest -- content_hash/row_count are NULL until
--           the candidate is validated, never coerced to 0.
--   NFR14 : the atomic 4-write publish is enabled by the pointer column + log +
--           outbox living in one schema so commit_publication can write them in
--           ONE transaction.
--   NFR15 : the publication log is append-only; a rollback is a NEW log row.
--
-- Apply with (human-gated on Jean's authorization -- Supabase only):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/042_datastream_candidate_registry.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- Referenced by composite foreign keys to guarantee project ownership.
-- (Idempotent -- may already exist from 030/032.)
-- ---------------------------------------------------------------------------
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastreams_id_project
    ON app.datastreams (id, project_id);

-- ---------------------------------------------------------------------------
-- app.datastream_executions -- the candidate registry + typed state machine.
--
-- A dse_<ULID> row per publication attempt. It carries the provenance chain
-- (plan_version_id, mapping_version_id) that 12.4 left as honest NULLs in
-- candidate_full_grain, plus the compiled 12.4 projection plan snapshot (for
-- provenance -- immutable after insert, enforced by the app layer, not a trigger
-- so operational columns like state/content_hash/row_count/error_* stay mutable
-- through the state machine).
--
-- The full state machine: created -> loading -> validating -> ready ->
-- publishing -> published, with -> failed / -> cancelled from any non-terminal
-- state. published/failed/cancelled are terminal.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_executions (
    id                      TEXT        NOT NULL,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    plan_version_id         TEXT        NOT NULL,
    mapping_version_id      TEXT        NOT NULL,
    projection_plan_ref     JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(projection_plan_ref) = 'object'),
    state                   TEXT        NOT NULL DEFAULT 'created'
                            CHECK (state IN (
                                'created', 'loading', 'validating', 'ready',
                                'publishing', 'published', 'failed', 'cancelled'
                            )),
    state_changed_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash            TEXT        CHECK (
                                content_hash IS NULL
                                OR content_hash ~ '^[0-9a-f]{64}$'
                            ),
    row_count               BIGINT      CHECK (row_count IS NULL OR row_count >= 0),
    error_code              TEXT,
    error_detail            TEXT,
    idempotency_key_hash    TEXT        CHECK (
                                idempotency_key_hash IS NULL
                                OR idempotency_key_hash ~ '^[0-9a-f]{64}$'
                            ),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_executions PRIMARY KEY (id),
    -- House-style ULID id (cf. dsp_/dmap_/mdm_). Crockford base32, 26 chars.
    CONSTRAINT ck_datastream_executions_id
        CHECK (id ~ '^dse_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- Composite uniqueness enables project-scoped composite FKs from callers and
    -- from the app.datastreams pointer.
    CONSTRAINT uq_datastream_executions_scope UNIQUE (id, datastream_id, project_id),
    CONSTRAINT fk_datastream_executions_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_datastream_executions_plan_scope
        FOREIGN KEY (plan_version_id, datastream_id, project_id)
        REFERENCES app.datastream_plan_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_datastream_executions_mapping_scope
        FOREIGN KEY (mapping_version_id, datastream_id, project_id)
        REFERENCES app.datastream_mapping_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_datastream_executions_ds_state
    ON app.datastream_executions (datastream_id, state);

CREATE INDEX IF NOT EXISTS idx_datastream_executions_project_state
    ON app.datastream_executions (project_id, state);

-- Idempotency: one execution per (datastream, idempotency_key_hash). Partial so
-- executions with no idempotency key never collide.
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastream_executions_idempotency
    ON app.datastream_executions (datastream_id, idempotency_key_hash)
    WHERE idempotency_key_hash IS NOT NULL;

-- Serialize concurrent publishes: at most ONE non-terminal execution per
-- datastream may exist (created/loading/validating/ready/publishing). A second
-- attempt collides here (concurrent_execution_active is also enforced in the app
-- layer, but this index is the hard structural guard).
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastream_executions_active
    ON app.datastream_executions (datastream_id)
    WHERE state IN ('created', 'loading', 'validating', 'ready', 'publishing');

-- ---------------------------------------------------------------------------
-- app.datastream_publication_log -- append-only history of every pointer swap.
--
-- One row per successful commit_publication. prior_execution_id is the pre-swap
-- pointer (rollback evidence). Immutable via trigger (mirrors 030 exactly): a
-- rollback (Story 12.12) is a NEW row pointing back at a prior execution, never a
-- mutation of an existing row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_publication_log (
    id                      TEXT        NOT NULL,
    execution_id            TEXT        NOT NULL,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    plan_version_id         TEXT        NOT NULL,
    mapping_version_id      TEXT        NOT NULL,
    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    row_count               BIGINT      NOT NULL CHECK (row_count >= 0),
    prior_execution_id      TEXT,
    published_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_by            TEXT        NOT NULL,
    CONSTRAINT pk_datastream_publication_log PRIMARY KEY (id),
    CONSTRAINT ck_datastream_publication_log_id
        CHECK (id ~ '^dplog_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT fk_datastream_publication_log_execution
        FOREIGN KEY (execution_id)
        REFERENCES app.datastream_executions (id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_datastream_publication_log_ds
    ON app.datastream_publication_log (datastream_id, published_at DESC);

CREATE OR REPLACE FUNCTION app.reject_publication_log_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'datastream publication log is append-only'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_datastream_publication_log_immutable
    ON app.datastream_publication_log;
CREATE TRIGGER trg_datastream_publication_log_immutable
    BEFORE UPDATE OR DELETE ON app.datastream_publication_log
    FOR EACH ROW EXECUTE FUNCTION app.reject_publication_log_mutation();

-- ---------------------------------------------------------------------------
-- app.datastream_outbox -- lightweight publication-event relay.
--
-- Written inside the same commit_publication transaction (published) or on the
-- failure/cancel paths. Consumed by a downstream poller that sets processed_at.
-- NOT append-only: the processed_at update is permitted (no immutability trigger).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_outbox (
    id                      BIGSERIAL,
    execution_id            TEXT        NOT NULL,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    event_type              TEXT        NOT NULL
                            CHECK (event_type IN ('published', 'failed', 'cancelled')),
    payload                 JSONB       NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at            TIMESTAMPTZ,
    CONSTRAINT pk_datastream_outbox PRIMARY KEY (id)
);

-- Poll unprocessed events efficiently.
CREATE INDEX IF NOT EXISTS idx_datastream_outbox_pending
    ON app.datastream_outbox (created_at)
    WHERE processed_at IS NULL;

-- ---------------------------------------------------------------------------
-- app.datastreams.current_published_execution_id -- the atomic pointer.
--
-- NULL = no published execution yet. The composite FK (mirrors
-- fk_datastreams_current_plan_scope from 030) guarantees the pointer references
-- an execution owned by the same project and datastream. DEFERRABLE so the
-- pointer swap and the execution's published-state advance commit together.
-- ---------------------------------------------------------------------------
ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS current_published_execution_id TEXT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_datastreams_current_published_execution_scope'
          AND conrelid = 'app.datastreams'::regclass
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT fk_datastreams_current_published_execution_scope
            FOREIGN KEY (current_published_execution_id, id, project_id)
            REFERENCES app.datastream_executions (id, datastream_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- app.project_preferences DQ-gate thresholds (project-scoped, not platform-wide
-- hardcodes -- "prefer per-project preferences"). Both NULL-able + additive, same
-- pattern as 033's projection-governance columns.
--   max_row_count_delta_pct : DECIMAL(5,2) NULL -- max allowed row-count delta %
--       vs the prior published execution before row_count_delta_exceeded blocks
--       (Owner approval overrides). NULL => documented default 50.00 in
--       datastream_publication.py; the DQ result records threshold_source.
--   allow_empty_publication : BOOLEAN NULL -- when TRUE (and force_empty_publish
--       is set) a zero-row candidate may publish. NULL => documented default
--       FALSE (empty candidate blocked).
-- ---------------------------------------------------------------------------
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS max_row_count_delta_pct DECIMAL(5, 2);

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS allow_empty_publication BOOLEAN;

-- A negative delta percentage is nonsensical. NULL means "use the documented
-- default". (0 is permitted: it means "any change requires approval".)
ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_max_row_count_delta_pct;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_max_row_count_delta_pct
    CHECK (
        max_row_count_delta_pct IS NULL
        OR max_row_count_delta_pct >= 0
    );

COMMIT;
