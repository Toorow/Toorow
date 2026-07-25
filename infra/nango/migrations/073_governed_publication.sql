-- Story 36.18: Governed mapping-publication confirmations (AD-27 / AD-28).
-- Additive and replayable. Zero provider/source vocabulary (AD-2).
--
-- WHY A SEPARATE TABLE (NOT app.mapping_proposals, NOT app.operations)
-- --------------------------------------------------------------------
-- Story 36.18 is the ONE place a 'ready' mapping proposal (Story 36.17) becomes
-- LIVE: it advances app.datastreams.current_mapping_version_id via a prepare-
-- confirm-commit publication. That flow needs a durable record that:
--   1. binds the OPAQUE, MODEL-HIDDEN confirmation secret (stored only as a
--      one-way sha256 `confirmation_reference_hash`, never the raw secret) to the
--      exact reviewed proposal/version/policy/hashes, so a replay/tamper is
--      detectable and the secret never re-enters model context;
--   2. carries a prepared->confirmed->dispatched lifecycle plus expired/failed
--      terminals -- app.mapping_proposals CANNOT host this: its immutability
--      trigger (migration 071) freezes everything but the proposed->...->ready
--      lifecycle + operation_id, and adding a publication lifecycle there would
--      loosen that frozen contract (forbidden by the epic);
--   3. exists BEFORE the durable operation does -- app.operations.request_payload
--      cannot hold the reviewed-hash bundle because the operation is only minted
--      at confirmation time. This mirrors migration 070 (operation_preparations)
--      and migration 071 (mapping_proposals): the immutable review evidence lives
--      in its own row, the single durable operation is linked via operation_id.
--
-- INVARIANT: creating a confirmation record NEVER advances a pointer. The pointer
-- move happens only inside the single durable operation the confirm mints, atomic
-- with audit + outbox. The prior version stays rollbackable (recorded as
-- prior_mapping_version_id); rollback is a DISTINCT confirmed operation.

BEGIN;

CREATE TABLE IF NOT EXISTS app.publication_confirmations (
    id                          TEXT PRIMARY KEY,
    -- The 'ready' proposal (Story 36.17) this publication consumes, and the exact
    -- immutable candidate version it will make live. The composite scope (datastream
    -- + project) is pinned so a confirmation cannot cross into another resource.
    proposal_id                 TEXT NOT NULL REFERENCES app.mapping_proposals(id) ON DELETE RESTRICT,
    mapping_version_id          TEXT NOT NULL,
    datastream_id               TEXT NOT NULL,
    project_id                  TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    org_id                      TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    -- The actor who opened the review (the accountable owner). Re-checked at confirm
    -- time: an actor mismatch refuses publication (AD-27 confirmation is bound).
    actor                       TEXT NOT NULL,
    -- One-way digest of the opaque confirmation secret minted server-side at review.
    -- The RAW secret is NEVER stored and NEVER returned to model context; only this
    -- hash lives here so confirm can verify the presented secret and detect replay.
    confirmation_reference_hash TEXT NOT NULL CHECK (confirmation_reference_hash ~ '^[0-9a-f]{64}$'),
    -- The bundle of hashes/versions the reviewer saw, frozen: content_hash and
    -- source_schema_hash of the candidate, the prior version id (rollback target),
    -- and the pinned policy/catalog/tool versions. Re-checked at confirm: a
    -- changed-pointer / stale review refuses publication.
    reviewed_hashes             JSONB NOT NULL CHECK (jsonb_typeof(reviewed_hashes) = 'object'),
    -- The prior live mapping version at review time (NULL when the datastream had no
    -- live mapping). Recorded so the prior version stays rollbackable after publish.
    prior_mapping_version_id    TEXT,
    -- Immutable policy version the review/confirmation was pinned to (changed policy
    -- since review refuses publication).
    policy_version              TEXT,
    -- One-way digest of the durable-operation idempotency key: confirm reuses it so a
    -- duplicate confirm / timeout replays the SAME publication operation (never a dup).
    idempotency_key_hash        TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    expires_at                  TIMESTAMPTZ NOT NULL,
    -- Publication lifecycle. 'prepared' is the only state a confirm may consume; a
    -- confirmed/dispatched record carries the linked durable operation_id. 'failed'
    -- and 'expired' are terminal (no pointer advanced).
    state                       TEXT NOT NULL DEFAULT 'prepared'
                                CHECK (state IN ('prepared', 'confirmed', 'dispatched', 'expired', 'failed')),
    operation_id                TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    immutable                   BOOLEAN NOT NULL DEFAULT TRUE,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One in-flight publication confirmation per proposal: a second prepare for the
    -- same proposal is refused so a proposal cannot be published twice concurrently.
    CONSTRAINT uq_publication_confirmations_proposal UNIQUE (proposal_id),
    -- Pin the wrapped version to the proposal's 1:1 version binding (migration 071's
    -- uq_mapping_proposals_version). Together with proposal_id -> app.mapping_proposals(id)
    -- this guarantees the confirmation cannot point at a version of another proposal.
    CONSTRAINT fk_publication_confirmations_version
        FOREIGN KEY (mapping_version_id)
        REFERENCES app.mapping_proposals (mapping_version_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS publication_confirmations_datastream
    ON app.publication_confirmations (datastream_id, created_at DESC);

CREATE INDEX IF NOT EXISTS publication_confirmations_prepared
    ON app.publication_confirmations (org_id, state)
    WHERE state = 'prepared';

-- Append-only mapping-publication log: the rollback evidence for the mapping-pointer
-- advance (mirrors app.datastream_publication_log for the EXECUTION pointer). Each
-- row records the version that went live, the prior version it replaced (the
-- rollback target), and whether this was a forward publish or a rollback. It is the
-- durable proof that the prior version stays rollbackable (NFR15). Never mutated.
CREATE TABLE IF NOT EXISTS app.datastream_mapping_publication_log (
    id                          TEXT PRIMARY KEY,
    datastream_id               TEXT NOT NULL,
    project_id                  TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    mapping_version_id          TEXT NOT NULL,
    prior_mapping_version_id    TEXT,
    command                     TEXT NOT NULL
                                CHECK (command IN ('datastream.mapping.publish', 'datastream.mapping.rollback')),
    published_by                TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS datastream_mapping_publication_log_ds
    ON app.datastream_mapping_publication_log (datastream_id, created_at DESC);

-- Append-only: no UPDATE/DELETE (rollback evidence must not be rewritten).
CREATE OR REPLACE FUNCTION app.protect_mapping_publication_log()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'datastream_mapping_publication_log is append-only';
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_mapping_publication_log_append_only
    ON app.datastream_mapping_publication_log;
CREATE TRIGGER trg_mapping_publication_log_append_only
    BEFORE UPDATE OR DELETE ON app.datastream_mapping_publication_log
    FOR EACH ROW EXECUTE FUNCTION app.protect_mapping_publication_log();

-- The review evidence is immutable (AD-27): confirmation may ONLY transition the
-- lifecycle (state prepared->confirmed->dispatched / expired / failed) and attach the
-- durable operation_id. Every substantive field (proposal/version scope, actor,
-- confirmation reference hash, reviewed hashes, prior version, policy, idempotency)
-- is frozen in place -- a re-review creates a NEW confirmation. Mirrors migrations
-- 070 / 071.
CREATE OR REPLACE FUNCTION app.protect_publication_confirmation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.immutable
       AND (
           NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
           OR NEW.mapping_version_id IS DISTINCT FROM OLD.mapping_version_id
           OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
           OR NEW.project_id IS DISTINCT FROM OLD.project_id
           OR NEW.org_id IS DISTINCT FROM OLD.org_id
           OR NEW.actor IS DISTINCT FROM OLD.actor
           OR NEW.confirmation_reference_hash IS DISTINCT FROM OLD.confirmation_reference_hash
           OR NEW.reviewed_hashes IS DISTINCT FROM OLD.reviewed_hashes
           OR NEW.prior_mapping_version_id IS DISTINCT FROM OLD.prior_mapping_version_id
           OR NEW.policy_version IS DISTINCT FROM OLD.policy_version
           OR NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash
       ) THEN
        RAISE EXCEPTION 'publication confirmation review evidence is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_publication_confirmation_immutable ON app.publication_confirmations;
CREATE TRIGGER trg_publication_confirmation_immutable
    BEFORE UPDATE ON app.publication_confirmations
    FOR EACH ROW EXECUTE FUNCTION app.protect_publication_confirmation();

COMMIT;
