-- Story 50.1: the analytical object model -- Query Specs and immutable Results.
--
-- WHY THIS EXISTS. Epic 50 needs one server-owned analytical capability that every
-- caller shares: Explore, Reports, Notebooks, Renders, the Console and MCP. Before
-- this migration there was no analytical Query Spec at all. The one column that
-- carries the name, `app.entity_source_bindings.query_spec` (migration 089), is
-- opaque SOURCE-CONNECTOR configuration -- what to pull from a provider. Story 50.1
-- AC1 forbids treating it as the analytical object, and nothing here references it.
--
-- THE IDENTITY CHAIN this schema encodes, and refuses to let drift:
--
--     Semantic View / version -> Query Spec / version -> execution attempt -> Result
--
-- Each arrow is a composite Project-scoped foreign key, never a bare id, so a row
-- cannot point across Projects even if the application layer is wrong (AC10).
--
-- WHAT IS IMMUTABLE, and why it is enforced in the database rather than in Python:
--
--   * a Query Spec VERSION, once written, is the analytical intent. Editing creates
--     the next version with a `predecessor_version_id`. AC2.
--   * a RESULT is the answer that was actually given. Refresh, retry and semantic
--     change each create a DISTINCT Result (AC8). No path relabels a prior one --
--     which is exactly the property a UI cannot be trusted to preserve, so the
--     trigger below refuses UPDATE and DELETE outright.
--   * a Result PAYLOAD is content-addressed evidence. AC5 forbids depending on an
--     expiring cache, a mutable source or a `deferred` marker, so the payload is a
--     mandatory row, not a nullable pointer.
--
-- THE ATTEMPT/RESULT SPLIT is deliberate and is what makes AC4 honest. Progress is
-- mutable and belongs on the attempt head plus append-only events; the Result is
-- inserted ONCE, when terminal evidence is durable. An interrupted attempt is
-- terminalized as `unavailable` rather than left without a Result -- an accepted
-- attempt that never resolves is the failure mode this split exists to prevent.
--
-- WHAT THIS IS NOT. A Result is not a `render_snapshot`, a notebook run envelope or
-- a report artifact (AC12). It carries no layout, no narrative and no chart: those
-- belong to Stories 50.4-50.5 and would make the Result a presentation contract.

-- ---------------------------------------------------------------------------
-- 1. Query Spec: a stable head and its immutable versions.
--
--    The head carries identity and a pointer to the current version. It is the ONLY
--    mutable row in this migration, and it may advance only through the authorized
--    service transaction (AC10).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.query_specs (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    semantic_view_id    TEXT NOT NULL,
    name                TEXT,
    current_version_id  TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_query_specs_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_query_specs_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_query_specs_name_bounded
        CHECK (name IS NULL OR char_length(name) <= 200)
);

CREATE INDEX IF NOT EXISTS idx_query_specs_project
    ON app.query_specs (project_id, created_at DESC);

-- An immutable analytical intent. `spec` is the canonical typed request; the
-- individual pins are lifted into columns ONLY where the database must enforce a
-- scoped foreign key or an index. Everything AC2 lists that is not a column lives
-- inside `spec` and is covered by `content_hash`.
CREATE TABLE IF NOT EXISTS app.query_spec_versions (
    id                          TEXT PRIMARY KEY,
    query_spec_id               TEXT NOT NULL,
    org_id                      TEXT NOT NULL,
    project_id                  TEXT NOT NULL,
    version_number              INTEGER NOT NULL,
    semantic_view_id            TEXT NOT NULL,
    semantic_view_version_id    TEXT NOT NULL,
    spec                        JSONB NOT NULL,
    content_hash                TEXT NOT NULL,
    predecessor_version_id      TEXT,
    created_by                  TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_query_spec_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_query_spec_version_number UNIQUE (query_spec_id, version_number),
    CONSTRAINT fk_query_spec_versions_head
        FOREIGN KEY (query_spec_id, org_id, project_id)
        REFERENCES app.query_specs (id, org_id, project_id),
    -- The pin that makes AC3 enforceable: a version can only reference a Semantic
    -- View version that belongs to the SAME Project. Migration 142 exposes
    -- `uq_semantic_view_version_scope (id, view_id, project_id)` for exactly this.
    CONSTRAINT fk_query_spec_versions_semantic_scope
        FOREIGN KEY (semantic_view_version_id, semantic_view_id, project_id)
        REFERENCES app.semantic_view_versions (id, view_id, project_id),
    CONSTRAINT fk_query_spec_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT ck_query_spec_versions_number_positive CHECK (version_number >= 1),
    CONSTRAINT ck_query_spec_versions_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_query_spec_versions_spec_is_object
        CHECK (jsonb_typeof(spec) = 'object'),
    -- Version 1 has no predecessor; every later version must name one. A revision
    -- that loses its lineage is indistinguishable from a fresh intent.
    CONSTRAINT ck_query_spec_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_query_spec_versions_head
    ON app.query_spec_versions (query_spec_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_query_spec_versions_semantic
    ON app.query_spec_versions (semantic_view_version_id);

ALTER TABLE app.query_specs
    DROP CONSTRAINT IF EXISTS fk_query_specs_current_version;
ALTER TABLE app.query_specs
    ADD CONSTRAINT fk_query_specs_current_version
    FOREIGN KEY (current_version_id, org_id, project_id)
    REFERENCES app.query_spec_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 2. Execution attempts: the mutable half, kept strictly apart from the Result.
--
--    `result_id` is allocated when the attempt is ACCEPTED, before any data exists,
--    so a caller holds a stable Result address while execution is still running.
--    That is what lets AC4 promise "an accepted attempt cannot remain permanently
--    without its Result".
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.query_execution_attempts (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    query_spec_version_id   TEXT NOT NULL,
    result_id               TEXT NOT NULL,
    state                   TEXT NOT NULL DEFAULT 'accepted',
    requested_by            TEXT NOT NULL,
    accepted_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    terminal_at             TIMESTAMPTZ,

    CONSTRAINT uq_query_execution_attempts_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_query_execution_attempts_result UNIQUE (result_id),
    CONSTRAINT fk_query_execution_attempts_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT ck_query_execution_attempts_state
        CHECK (state IN ('accepted', 'running', 'terminal')),
    CONSTRAINT ck_query_execution_attempts_terminal_time CHECK (
        (state = 'terminal' AND terminal_at IS NOT NULL)
        OR (state <> 'terminal' AND terminal_at IS NULL)
    ),
    CONSTRAINT ck_query_execution_attempts_ends_after_start
        CHECK (terminal_at IS NULL OR terminal_at >= accepted_at)
);

CREATE INDEX IF NOT EXISTS idx_query_execution_attempts_spec
    ON app.query_execution_attempts (query_spec_version_id, accepted_at DESC);
-- Recovery reads this: accepted attempts with no terminal time are the ones that
-- must be honestly terminalized as `unavailable`.
CREATE INDEX IF NOT EXISTS idx_query_execution_attempts_open
    ON app.query_execution_attempts (project_id, accepted_at)
    WHERE terminal_at IS NULL;

CREATE TABLE IF NOT EXISTS app.query_execution_attempt_events (
    id              TEXT PRIMARY KEY,
    attempt_id      TEXT NOT NULL,
    org_id          TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    ordinal         INTEGER NOT NULL,
    state           TEXT NOT NULL,
    detail          JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_query_execution_attempt_events_attempt
        FOREIGN KEY (attempt_id, org_id, project_id)
        REFERENCES app.query_execution_attempts (id, org_id, project_id),
    CONSTRAINT uq_query_execution_attempt_events_ordinal UNIQUE (attempt_id, ordinal),
    CONSTRAINT ck_query_execution_attempt_events_ordinal CHECK (ordinal >= 1),
    CONSTRAINT ck_query_execution_attempt_events_detail_is_object
        CHECK (jsonb_typeof(detail) = 'object')
);

CREATE INDEX IF NOT EXISTS idx_query_execution_attempt_events_attempt
    ON app.query_execution_attempt_events (attempt_id, ordinal);

-- ---------------------------------------------------------------------------
-- 3. The immutable Result, and its mandatory evidence payload.
--
--    AC7 is enforced as a CHECK rather than left to the service: a Result either
--    names an AI Path, or carries the exact literal `No AI path`. There is no third
--    state -- no NULL, no 'deferred', no empty string. A nullable ai_path_id with a
--    "we'll fill it in later" convention is precisely the substitute AC7 refuses.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.query_results (
    id                      TEXT PRIMARY KEY,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    attempt_id              TEXT NOT NULL,
    query_spec_version_id   TEXT NOT NULL,
    outcome                 TEXT NOT NULL,
    ai_path_id              TEXT,
    ai_path_absent_literal  TEXT,
    content_hash            TEXT NOT NULL,
    row_count               BIGINT NOT NULL DEFAULT 0,
    cell_count              BIGINT NOT NULL DEFAULT 0,
    byte_count              BIGINT NOT NULL DEFAULT 0,
    truncated               BOOLEAN NOT NULL DEFAULT FALSE,
    predecessor_result_id   TEXT,
    started_at              TIMESTAMPTZ NOT NULL,
    ended_at                TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_query_results_scope UNIQUE (id, org_id, project_id),
    -- One Result per accepted attempt. AC4 says "exactly one", and this is where
    -- "exactly" is enforced rather than hoped for.
    CONSTRAINT uq_query_results_attempt UNIQUE (attempt_id),
    CONSTRAINT fk_query_results_attempt
        FOREIGN KEY (attempt_id, org_id, project_id)
        REFERENCES app.query_execution_attempts (id, org_id, project_id),
    CONSTRAINT fk_query_results_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_query_results_ai_path
        FOREIGN KEY (ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id),
    CONSTRAINT fk_query_results_predecessor
        FOREIGN KEY (predecessor_result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT ck_query_results_outcome
        CHECK (outcome IN ('success', 'empty', 'degraded', 'refused', 'unavailable')),
    CONSTRAINT ck_query_results_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_query_results_counts_non_negative
        CHECK (row_count >= 0 AND cell_count >= 0 AND byte_count >= 0),
    CONSTRAINT ck_query_results_ends_after_start CHECK (ended_at >= started_at),
    -- AC7, exactly: one of the two, never both, never neither, and the literal is
    -- spelled out here so no caller can invent a different wording.
    CONSTRAINT ck_query_results_ai_path_is_honest CHECK (
        (ai_path_id IS NOT NULL AND ai_path_absent_literal IS NULL)
        OR (ai_path_id IS NULL AND ai_path_absent_literal = 'No AI path')
    ),
    -- Only a `success` or `degraded` Result may carry rows. An `empty`, `refused`
    -- or `unavailable` Result that reported rows would be the dishonest state the
    -- outcome vocabulary exists to prevent.
    CONSTRAINT ck_query_results_rows_match_outcome CHECK (
        outcome IN ('success', 'degraded') OR row_count = 0
    )
);

CREATE INDEX IF NOT EXISTS idx_query_results_project
    ON app.query_results (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_results_spec_version
    ON app.query_results (query_spec_version_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_query_results_content_hash
    ON app.query_results (project_id, content_hash);

-- Content-addressed evidence. Mandatory for EVERY outcome: AC5 requires empty and
-- refused Results to retain a complete manifest explaining why no rows exist, so
-- this table has no "results without payload" state to fall into.
CREATE TABLE IF NOT EXISTS app.query_result_payloads (
    result_id       TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL,
    project_id      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    result_schema   JSONB NOT NULL,
    manifest        JSONB NOT NULL,
    rows_chunk      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_query_result_payloads_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT ck_query_result_payloads_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_query_result_payloads_schema_is_object
        CHECK (jsonb_typeof(result_schema) = 'object'),
    CONSTRAINT ck_query_result_payloads_manifest_is_object
        CHECK (jsonb_typeof(manifest) = 'object'),
    CONSTRAINT ck_query_result_payloads_rows_is_array
        CHECK (jsonb_typeof(rows_chunk) = 'array')
);

CREATE INDEX IF NOT EXISTS idx_query_result_payloads_hash
    ON app.query_result_payloads (project_id, content_hash);

-- ---------------------------------------------------------------------------
-- 4. Immutability, enforced where it cannot be argued with.
--
--    The `app.rgpd_erasure` escape hatch is the same one migrations 098/099 and 150
--    use: erasure is the ONLY legitimate reason these rows disappear, and it is
--    explicit rather than implicit.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_analytical_evidence_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'analytical evidence is immutable: % rows are insert-once (refresh or retry creates a new Result)',
        TG_TABLE_NAME
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_query_spec_versions_immutable ON app.query_spec_versions;
CREATE TRIGGER trg_query_spec_versions_immutable
BEFORE UPDATE OR DELETE ON app.query_spec_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_query_results_immutable ON app.query_results;
CREATE TRIGGER trg_query_results_immutable
BEFORE UPDATE OR DELETE ON app.query_results
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_query_result_payloads_immutable ON app.query_result_payloads;
CREATE TRIGGER trg_query_result_payloads_immutable
BEFORE UPDATE OR DELETE ON app.query_result_payloads
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

DROP TRIGGER IF EXISTS trg_query_execution_attempt_events_append_only
    ON app.query_execution_attempt_events;
CREATE TRIGGER trg_query_execution_attempt_events_append_only
BEFORE UPDATE OR DELETE ON app.query_execution_attempt_events
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- The attempt head IS allowed to advance, but only forwards, and never off its
-- Result or its Query Spec version. A retry is a new attempt, not a rewind.
CREATE OR REPLACE FUNCTION app.reject_query_attempt_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'an accepted execution attempt cannot be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.state = 'terminal' THEN
        RAISE EXCEPTION 'a terminal execution attempt cannot be reopened'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.result_id <> OLD.result_id
       OR NEW.query_spec_version_id <> OLD.query_spec_version_id
       OR NEW.accepted_at <> OLD.accepted_at
    THEN
        RAISE EXCEPTION 'an execution attempt may not be rebound to another Result or Query Spec version'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_query_execution_attempts_forward_only
    ON app.query_execution_attempts;
CREATE TRIGGER trg_query_execution_attempts_forward_only
BEFORE UPDATE OR DELETE ON app.query_execution_attempts
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_query_attempt_rewrite();

CREATE OR REPLACE FUNCTION app.reject_analytical_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'analytical evidence cannot be truncated'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_query_results_block_truncate ON app.query_results;
CREATE TRIGGER trg_query_results_block_truncate
BEFORE TRUNCATE ON app.query_results
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_analytical_truncate();

DROP TRIGGER IF EXISTS trg_query_result_payloads_block_truncate ON app.query_result_payloads;
CREATE TRIGGER trg_query_result_payloads_block_truncate
BEFORE TRUNCATE ON app.query_result_payloads
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_analytical_truncate();

DROP TRIGGER IF EXISTS trg_query_spec_versions_block_truncate ON app.query_spec_versions;
CREATE TRIGGER trg_query_spec_versions_block_truncate
BEFORE TRUNCATE ON app.query_spec_versions
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_analytical_truncate();

-- ---------------------------------------------------------------------------
-- 5. Fail-closed Row Level Security.
--
--    Same contract as migrations 149 and 150: RLS enabled with no applicable policy
--    exposes no rows, FORCE applies it to the table owner too, and the application
--    authorization check still runs first. RLS is the floor, not the door (AC10).
-- ---------------------------------------------------------------------------
ALTER TABLE app.query_specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.query_specs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_specs_strict ON app.query_specs;
CREATE POLICY query_specs_strict ON app.query_specs
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.query_spec_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.query_spec_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_spec_versions_strict ON app.query_spec_versions;
CREATE POLICY query_spec_versions_strict ON app.query_spec_versions
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.query_execution_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.query_execution_attempts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_execution_attempts_strict ON app.query_execution_attempts;
CREATE POLICY query_execution_attempts_strict ON app.query_execution_attempts
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.query_execution_attempt_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.query_execution_attempt_events FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_execution_attempt_events_strict
    ON app.query_execution_attempt_events;
CREATE POLICY query_execution_attempt_events_strict ON app.query_execution_attempt_events
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.query_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.query_results FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_results_strict ON app.query_results;
CREATE POLICY query_results_strict ON app.query_results
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.query_result_payloads ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.query_result_payloads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS query_result_payloads_strict ON app.query_result_payloads;
CREATE POLICY query_result_payloads_strict ON app.query_result_payloads
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );
