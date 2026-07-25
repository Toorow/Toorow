-- Story 36.9: recent-vs-historical coverage markers for the recent-first pull.
-- Additive and idempotent (mirrors 065/069/070/071). Zero provider/source
-- vocabulary (AD-2). Human-gated on Jean's authorization; Supabase only.
--
-- WHY A NEW TABLE (assessed against reuse of migration 042):
--   The candidate registry (app.datastream_executions, migration 042) plus the
--   append-only publication log carry per-EXECUTION state and provenance, and
--   app.datastreams.current_published_execution_id is the single live pointer.
--   None of them expresses a per-datastream, per-COVERAGE-KIND marker that must
--   stay SEPARATE for the recent window and the background history window. Story
--   36.9's binding invariant is: "recent and historical coverage stay separate;
--   later history failures cannot make the recent result appear unavailable." A
--   single pointer / a per-execution row cannot hold two independently-updatable
--   coverage facts (one for `recent`, one for `historical`) without conflating
--   them -- a background-history failure would otherwise have to touch the same
--   row that records the recent result. Reusing 042 tables is therefore NOT
--   sufficient; a small dedicated marker table is the minimal real gap.
--
--   This table is a THIN coverage annotation. It holds NO rows, NO provider
--   payload, NO token, NO raw sample -- only the covered interval, the execution
--   that produced it, a bounded state, and honest counters. The authoritative
--   data stays in BigQuery (AD-8) and the authoritative publication state stays on
--   app.datastreams / app.datastream_publication_log. A `recent` marker never
--   references or depends on the `historical` marker: they are independent rows.

BEGIN;

-- ---------------------------------------------------------------------------
-- app.datastream_coverage -- one row per (datastream, project, coverage_kind).
--
-- coverage_kind:
--   'recent'     -- the bounded recent-first window (Story 36.9 candidate).
--   'historical' -- the progressive background-history window (parked chunked
--                   backfill stays conditional per Epic 32; this marker exists so
--                   the two coverages are SEPARATE, not so a new backfill engine
--                   is introduced).
--
-- state is a small, honest lifecycle:
--   'pending'     -- declared, not yet loaded.
--   'loading'     -- a candidate execution is running for this window.
--   'covered'     -- a candidate published successfully for this window.
--   'degraded'    -- partially covered (e.g. history still in progress or a
--                    non-fatal gap); the recent result remains usable.
--   'failed'      -- this window's last attempt failed. A 'failed' HISTORICAL row
--                    NEVER flips a 'covered' RECENT row (separate rows, separate
--                    updates) -- that is the whole point of this table.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_coverage (
    id                      TEXT        NOT NULL,
    datastream_id           TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    coverage_kind           TEXT        NOT NULL
                            CHECK (coverage_kind IN ('recent', 'historical')),
    state                   TEXT        NOT NULL DEFAULT 'pending'
                            CHECK (state IN (
                                'pending', 'loading', 'covered', 'degraded', 'failed'
                            )),
    -- The covered interval [interval_start, interval_end_exclusive). Honest NULLs
    -- until a window is actually declared/loaded (AD-9: never coerced).
    interval_start          TIMESTAMPTZ,
    interval_end_exclusive  TIMESTAMPTZ,
    -- The execution that last produced/attempted this coverage (provenance link
    -- into the 042 candidate registry). NULL until the first candidate runs.
    execution_id            TEXT,
    -- The pull that fed the candidate (Story 36.9 retains pull ID + provenance
    -- before read). Honest NULL until a pull is associated.
    pull_id                 TEXT,
    -- Honest counters -- NULL until validated, never a fabricated 0 (AD-9).
    row_count               BIGINT      CHECK (row_count IS NULL OR row_count >= 0),
    content_hash            TEXT        CHECK (
                                content_hash IS NULL
                                OR content_hash ~ '^[0-9a-f]{64}$'
                            ),
    covered_at              TIMESTAMPTZ,
    updated_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_coverage PRIMARY KEY (id),
    CONSTRAINT ck_datastream_coverage_id
        CHECK (id ~ '^dcov_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- Exactly ONE marker per (datastream, project, coverage_kind): the recent and
    -- historical facts are distinct rows that never overwrite one another.
    CONSTRAINT uq_datastream_coverage_kind
        UNIQUE (datastream_id, project_id, coverage_kind),
    CONSTRAINT fk_datastream_coverage_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    -- The execution reference (when set) must be owned by the same datastream and
    -- project (mirrors the 042 composite-FK ownership discipline).
    CONSTRAINT fk_datastream_coverage_execution_scope
        FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_datastream_coverage_ds
    ON app.datastream_coverage (datastream_id, project_id, coverage_kind);

-- The (datastream, project, coverage_kind) identity is immutable after insert;
-- only the mutable operational columns (state / interval / execution_id / pull_id
-- / row_count / content_hash / covered_at / updated_by / updated_at) may change.
-- Mirrors 069's protect_first_report_draft_binding.
CREATE OR REPLACE FUNCTION app.protect_datastream_coverage_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.coverage_kind IS DISTINCT FROM OLD.coverage_kind THEN
        RAISE EXCEPTION 'datastream coverage identity is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_datastream_coverage_binding_immutable
    ON app.datastream_coverage;
CREATE TRIGGER trg_datastream_coverage_binding_immutable
    BEFORE UPDATE ON app.datastream_coverage
    FOR EACH ROW EXECUTE FUNCTION app.protect_datastream_coverage_binding();

COMMIT;
