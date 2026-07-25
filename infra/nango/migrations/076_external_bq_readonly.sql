-- infra/nango/migrations/076_external_bq_readonly.sql
--
-- Story 12.7: register an EXISTING BigQuery table/view as a READ-ONLY Datastream
-- WITHOUT taking ownership (no second writer). Additive and idempotent after
-- migration 075. Zero provider/source vocabulary in server/core consumes this
-- (AD-2): the external-BQ logic lives in its own module (external_bq_registration)
-- and the generic publication path (Story 12.5) is reused unchanged.
--
-- WHY A SEPARATE OBSERVATION LEDGER (NOT app.datastream_executions ALONE)
-- ----------------------------------------------------------------------
-- A connector pull LANDS raw rows toorow owns; an external-BQ registration only
-- OBSERVES an object another pipeline owns. The 12.5 candidate registry
-- (app.datastream_executions) records the publication attempt + provenance, but it
-- has nowhere to persist the read-only OBSERVATION evidence that MINTS a virtual
-- pull commit: the object coordinates, the watermark, the content/schema hash, the
-- row count, the location/region, and the safe-fail verdict of one probe. That
-- evidence is the "equivalent provenance / replay evidence" the story requires --
-- it is what makes a repeated observation of unchanged content an AUDITABLE no-op.
-- It CANNOT ride app.datastream_executions.projection_plan_ref (that is the 12.4
-- compiled plan, immutable at insert) and it exists BEFORE any execution is minted
-- (the observation is what DECIDES whether an execution is worth minting). So it
-- lives in its own append-only ledger, linked forward to the execution it mints via
-- execution_id (NULL until an observation actually mints one -- an unchanged-content
-- no-op observation mints none).
--
-- INVARIANTS
-- ----------
--   * READ-ONLY: this migration NEVER creates a writer against the external object.
--     It stores OBSERVATIONS of an object another pipeline owns. The destination
--     policy for these datastreams is `external_read_only` (intent schema $def
--     external_object), enforced at intent time; this ledger only records what was
--     observed, never a write.
--   * APPEND-ONLY: every observation is permanent evidence (a BEFORE UPDATE OR
--     DELETE trigger rejects mutation, mirroring 073's mapping-publication log).
--     A later re-observation is a NEW row, never a mutation of a prior one.
--   * AD-8 / AD-9: Postgres is the sole writer of this ledger; the row_count /
--     content_hash / schema_hash are honest NULLs until a probe actually observes
--     them (never coerced to 0). A `blocked`/`failed` verdict is honest.
--   * NFR14/NFR20: the virtual pull commit an observation mints feeds the SAME
--     12.5 atomic candidate registry + publication log; the observation ledger is
--     the auditable provenance chain external -> observation -> execution ->
--     publication.

BEGIN;

-- ---------------------------------------------------------------------------
-- app.external_bq_observations -- append-only read-only observation ledger.
--
-- One row per probe of an external BigQuery object registered as a read-only
-- Datastream. It carries the object coordinates (project/dataset/object), the
-- declared external writer_identity (evidence that toorow is NOT the writer), the
-- watermark + content/schema hash + row count that define a "virtual pull commit",
-- the observed location/region, and the safe-fail verdict. When an observation
-- MINTS a virtual pull commit (i.e. the content changed and a candidate is worth
-- publishing) it links forward to the 12.5 execution via execution_id; an
-- unchanged-content no-op observation records verdict='unchanged_noop' with a NULL
-- execution_id (auditable no-op, no candidate minted).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.external_bq_observations (
    id                      TEXT        NOT NULL,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    -- The immutable plan version (Story 12.2) whose external_object this observation
    -- probed. Recorded so the observation is provably tied to a reviewed plan.
    plan_version_id         TEXT,
    -- External object coordinates + the DECLARED external writer identity. The
    -- writer_identity is evidence toorow is NOT the writer (read-only registration).
    object_project          TEXT        NOT NULL,
    object_dataset          TEXT        NOT NULL,
    object_name             TEXT        NOT NULL,
    writer_identity         TEXT        NOT NULL,
    -- The virtual-pull-commit evidence. All honest NULLs until a probe observes
    -- them (a `blocked`/`absent`/`permission_revoked` verdict leaves them NULL --
    -- never a fabricated 0). content_hash / schema_hash are 64-hex when present.
    watermark               TIMESTAMPTZ,
    content_hash            TEXT        CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),
    schema_hash             TEXT        CHECK (schema_hash IS NULL OR schema_hash ~ '^[0-9a-f]{64}$'),
    row_count               BIGINT      CHECK (row_count IS NULL OR row_count >= 0),
    object_location         TEXT,
    -- The safe-fail verdict of this probe (closed set). `ok` = a fresh observable
    -- object; `unchanged_noop` = observed but identical to the last committed
    -- observation (auditable no-op, no execution minted); the rest are the safe-fail
    -- matrix outcomes. NONE of them replace a published version -- the app layer
    -- reads this verdict and, on any non-ok/non-unchanged verdict, leaves the prior
    -- published pointer intact.
    verdict                 TEXT        NOT NULL
                            CHECK (verdict IN (
                                'ok', 'unchanged_noop', 'absent', 'empty', 'moved',
                                'permission_revoked', 'region_incompatible',
                                'changed_after_preview'
                            )),
    -- The prepared/expected content_hash a preview pinned (for the
    -- changed_after_preview safe-fail check). NULL outside a preview->activate flow.
    expected_content_hash   TEXT        CHECK (expected_content_hash IS NULL OR expected_content_hash ~ '^[0-9a-f]{64}$'),
    -- Human-visible repair action for a non-ok verdict (code + hint), honest empty
    -- object for `ok`/`unchanged_noop`.
    repair                  JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(repair) = 'object'),
    -- Forward link to the 12.5 candidate execution this observation minted (NULL
    -- for a no-op / non-ok observation that minted nothing).
    execution_id            TEXT,
    observed_by             TEXT        NOT NULL,
    observed_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_external_bq_observations PRIMARY KEY (id),
    -- House-style ULID id (cf. dse_/dsp_/dmap_). Crockford base32, 26 chars.
    CONSTRAINT ck_external_bq_observations_id
        CHECK (id ~ '^ebqo_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- Project-scoped ownership: the observation cannot cross into another project's
    -- datastream (mirrors 042's fk_datastream_executions_datastream_scope).
    CONSTRAINT fk_external_bq_observations_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    -- When an observation mints an execution, the link is project + datastream
    -- scoped (mirrors 042's uq_datastream_executions_scope composite).
    CONSTRAINT fk_external_bq_observations_execution_scope
        FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_external_bq_observations_ds
    ON app.external_bq_observations (datastream_id, observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_external_bq_observations_project
    ON app.external_bq_observations (project_id, observed_at DESC);

-- Fast lookup of the last COMMITTED (ok) observation per datastream, so the app
-- layer can detect unchanged content (content_hash equality) -> unchanged_noop.
CREATE INDEX IF NOT EXISTS idx_external_bq_observations_last_ok
    ON app.external_bq_observations (datastream_id, observed_at DESC)
    WHERE verdict = 'ok';

-- Append-only: every observation is permanent read-only evidence. A re-observation
-- is a NEW row, never a mutation. Mirrors 073's protect_mapping_publication_log.
CREATE OR REPLACE FUNCTION app.reject_external_bq_observation_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'external_bq_observations is append-only'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_external_bq_observations_append_only
    ON app.external_bq_observations;
CREATE TRIGGER trg_external_bq_observations_append_only
    BEFORE UPDATE OR DELETE ON app.external_bq_observations
    FOR EACH ROW EXECUTE FUNCTION app.reject_external_bq_observation_mutation();

-- ---------------------------------------------------------------------------
-- app.datastreams.external_dispatch_excluded -- the EXPLICIT scheduler-guard flag.
--
-- The scheduler must NEVER dispatch a provider pull for an external_bq datastream
-- (there is no provider to pull -- the object is maintained by another pipeline).
-- Relying on `connection_ref_id IS NULL` alone would be an ACCIDENTAL side effect;
-- this column makes the invariant EXPLICIT and queryable. It is generated from
-- source_kind so it can never drift: any external_bq datastream is excluded, every
-- other kind is not. The scheduler dispatch query adds
-- `AND ds.external_dispatch_excluded = FALSE` (integration spec).
-- ---------------------------------------------------------------------------
ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS external_dispatch_excluded BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill: any existing external_bq datastream is excluded from provider dispatch.
UPDATE app.datastreams
SET external_dispatch_excluded = TRUE
WHERE source_kind = 'external_bq' AND external_dispatch_excluded = FALSE;

-- Keep the flag in lock-step with source_kind so it is never an accidental value.
-- (A trigger, not a GENERATED column, because source_kind is set AFTER insert by
-- the intent path and a GENERATED STORED column cannot reference a later UPDATE
-- cheaply across the additive-migration boundary.)
CREATE OR REPLACE FUNCTION app.sync_external_dispatch_excluded()
RETURNS TRIGGER AS $$
BEGIN
    NEW.external_dispatch_excluded := (NEW.source_kind = 'external_bq');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_datastreams_sync_external_dispatch ON app.datastreams;
CREATE TRIGGER trg_datastreams_sync_external_dispatch
    BEFORE INSERT OR UPDATE OF source_kind ON app.datastreams
    FOR EACH ROW EXECUTE FUNCTION app.sync_external_dispatch_excluded();

COMMIT;
