-- Story 36.17: Agentic mapping-correction proposals (E36-FR07 / E36-NFR05).
-- Additive and replayable. Zero provider/source vocabulary (AD-2).
--
-- WHY A SEPARATE TABLE (NOT A COLUMN ON app.datastream_mapping_versions)
-- ---------------------------------------------------------------------
-- app.datastream_mapping_versions (migration 032) is APPEND-ONLY IMMUTABLE: it
-- carries a BEFORE UPDATE OR DELETE trigger that raises on any row mutation, and a
-- (project_id, idempotency_key_hash) uniqueness contract. A proposal, however, has a
-- LIFECYCLE (proposed -> testing -> ready | rejected) that must be transitionable by
-- Story 36.18 at publication time. Adding a mutable state column to the immutable
-- version table would either break its immutability guarantee or require loosening
-- the trigger -- both forbidden by the epic (the version contract must not change).
--
-- So the immutable candidate VERSION stays in app.datastream_mapping_versions, and
-- this table records the proposal LIFECYCLE bound 1:1 to that version. The binding
-- (which version a proposal wraps) is frozen in place by an immutability trigger
-- (mirrors migration 070's approach for operation preparations); only the lifecycle
-- state and the linked operation may transition.
--
-- INVARIANT: this table stores NO live-pointer authority. Publishing a proposal
-- (advancing app.datastreams.current_mapping_version_id) belongs to Story 36.18 and
-- is intentionally NOT expressible here -- creating a proposal never moves a pointer.

BEGIN;

CREATE TABLE IF NOT EXISTS app.mapping_proposals (
    id                      TEXT PRIMARY KEY,
    -- The immutable candidate version this proposal wraps (migration 032). The
    -- composite FK pins datastream + project so a proposal cannot point at a
    -- version of another scope. ON DELETE RESTRICT: an immutable version is never
    -- deleted anyway, but the proposal must not outlive a (hypothetically) removed
    -- version binding.
    mapping_version_id      TEXT NOT NULL,
    datastream_id           TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    org_id                  TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    -- The agentic mode the proposal was created under (E36-FR07). Advisory never
    -- reaches this table (it may only explain); persistence is Delegated/Autonomous.
    mode                    TEXT NOT NULL CHECK (mode IN ('delegated', 'autonomous')),
    -- Proposal lifecycle. 'ready' is the only state Story 36.18 may publish from.
    -- 'rejected' is recorded when a gate check failed (the version row is NOT even
    -- inserted in that case, so a 'rejected' row without a version is the marker of
    -- a failed candidate); the live pointer never advances in any state.
    state                   TEXT NOT NULL DEFAULT 'proposed'
                            CHECK (state IN ('proposed', 'testing', 'ready', 'rejected')),
    -- The ordered gate-check verdicts (compile/semantic/sensitivity/authorization/
    -- DQ/cardinality/cost). Codes + booleans only -- never raw provider evidence.
    checks                  JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(checks) = 'object'),
    -- The deterministic human-readable diff (field adds/removes, type changes,
    -- canonical rebindings, currency/non-additive conflicts). No raw values.
    diff                    JSONB NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(diff) = 'object'),
    -- The durable operation (Story 36.2) that atomically committed this proposal
    -- (version insert + this row + audit + outbox). Reused on idempotent replay.
    operation_id            TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    -- Pinned evidence: the schema hash the candidate was profiled against and the
    -- canonical content hash of the candidate mapping payload.
    source_schema_hash      TEXT NOT NULL CHECK (source_schema_hash ~ '^[0-9a-f]{64}$'),
    content_hash            TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- Immutable policy / catalog / tool-contract versions the proposal was pinned to.
    policy_version          TEXT,
    catalog_version         TEXT,
    tool_version            TEXT,
    immutable               BOOLEAN NOT NULL DEFAULT TRUE,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_mapping_proposals_version UNIQUE (mapping_version_id),
    CONSTRAINT fk_mapping_proposals_version_scope
        FOREIGN KEY (mapping_version_id, datastream_id, project_id)
        REFERENCES app.datastream_mapping_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS mapping_proposals_datastream
    ON app.mapping_proposals (datastream_id, created_at DESC);

CREATE INDEX IF NOT EXISTS mapping_proposals_ready
    ON app.mapping_proposals (org_id, state)
    WHERE state = 'ready';

-- The proposal binding is immutable (E36-FR07): the wrapped version, scope, mode,
-- pinned hashes and versions are FROZEN in place. Only the lifecycle state, the
-- linked operation_id and updated_at may transition (Story 36.18 publishes from a
-- 'ready' row). A correction creates a NEW proposal + a NEW immutable version --
-- it never rewrites this row's binding. Mirrors migration 070.
CREATE OR REPLACE FUNCTION app.protect_mapping_proposal()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.immutable
       AND (
           NEW.mapping_version_id IS DISTINCT FROM OLD.mapping_version_id
           OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
           OR NEW.project_id IS DISTINCT FROM OLD.project_id
           OR NEW.org_id IS DISTINCT FROM OLD.org_id
           OR NEW.mode IS DISTINCT FROM OLD.mode
           OR NEW.source_schema_hash IS DISTINCT FROM OLD.source_schema_hash
           OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
           OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
           OR NEW.catalog_version IS DISTINCT FROM OLD.catalog_version
           OR NEW.tool_version IS DISTINCT FROM OLD.tool_version
           OR NEW.created_by IS DISTINCT FROM OLD.created_by
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
       ) THEN
        RAISE EXCEPTION 'mapping proposal binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mapping_proposal_immutable ON app.mapping_proposals;
CREATE TRIGGER trg_mapping_proposal_immutable
    BEFORE UPDATE ON app.mapping_proposals
    FOR EACH ROW EXECUTE FUNCTION app.protect_mapping_proposal();

COMMIT;
