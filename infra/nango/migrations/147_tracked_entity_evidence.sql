-- Story 48.5: the evidence a competitor claim rests on, and the two owners it belongs to.
--
-- Epic 40 landed four tables that each answered half a question. This migration
-- keeps the questions and replaces the answers.
--
-- WHAT 086/089/090/092 GOT WRONG, precisely:
--
--   * `app.tracked_entities` is MUTABLE. Its canonical name and its JSONB alias
--     array are edited in place, so a Result that pinned "the entity" cannot be
--     replayed: the entity it pinned no longer exists in the form it pinned.
--     An alias array also cannot say WHICH KIND of claim an alias is -- exact,
--     close, related and refused all look identical inside a list of strings.
--   * `app.entity_project_roles` is a mutable flat row. A role change rewrites
--     history rather than adding to it.
--   * `app.entity_source_bindings` binds an entity to an OPAQUE `source` string
--     with an unvalidated free-form `query_spec`. Nothing checks that the source
--     can do this, that the report exists, or that any Datastream uses it -- and
--     the compiler joined it to Datastreams by `module_name == binding.source`,
--     which is how one binding could make an entire Project Complete.
--   * `app.inbound_brand_match_decisions` records `outcome IN ('resolved','alert')`.
--     `resolved` means a machine decided; there is no seat at the table for the
--     human confirmation the ratified contract requires, and no place to record
--     which algorithm, corpus or policy produced the score.
--
-- WHERE THE REPLACEMENTS LIVE:
--
--   Identity, aliases and Project roles are NOT here. They are the generic
--   Master Data objects migration 143 (Story 49.2) already created:
--   `master_data_registries` (organization scope), `master_data_nodes` (one
--   identity), `master_data_object_versions` (node scope: each identity has its
--   own immutable history), `master_data_aliases` (SKOS-typed: exact, close,
--   broader, narrower, related, negative) and `master_data_project_associations`
--   (an organization identity REUSED by a Project with a role, never copied).
--   Building a second identity store beside them is exactly the duplicate
--   authority Story 49.2 exists to remove, so this migration builds none.
--
--   What is genuinely missing is the EVIDENCE layer, and it splits across two
--   owners the ratified architecture keeps apart:
--
--     Governance owns  what an identity MEANS at a source, and which candidate
--                      decisions a human made.
--     Data owns        what an exact Datastream OBSERVED, and the exact physical
--                      binding that would make a future run collect it.
--
--   Five tables, in that order. Every one of them is append-only or immutable,
--   because every one of them is pinned by something downstream.
--
-- WHAT IS NOT DROPPED HERE. The four Epic 40 tables stay in place. Their rows
-- are the only record of what was configured, and `inbound_brand_match_decisions`
-- is an append-only ledger whose rows cannot be re-created once gone. The Python
-- authorities are removed in this story; a later migration may drop the tables
-- once a real Project has been migrated and the operator has said so. Deleting
-- the evidence of a manual configuration in the same commit that replaces the
-- code reading it is how a migration becomes irreversible by accident.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
--
-- Numbering: 145 and 146 were taken by two parallel sessions (Stories 49.4 and
-- 48.4) while this one was being written. 147 is the next free identifier.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. The matching policy. GOVERNANCE.
--
--    A score is meaningless without the thing that produced it. Epic 40 had one
--    global constant -- 0.88 -- compiled into Python, which meant two decisions
--    taken six months apart could not be compared and a threshold change
--    silently reinterpreted every past decision.
--
--    Immutable: a decision pins a policy version, and a policy edited underneath
--    a decision would make that decision unreadable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_match_policy_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^emp_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL CHECK (version_number > 0),

    -- Which ranking implementation ran. Bumped by the code, not by an operator.
    algorithm_version TEXT NOT NULL CHECK (length(btrim(algorithm_version)) BETWEEN 1 AND 40),
    -- A hash over the alias corpus the ranking saw. Two runs over different
    -- corpora are different facts even at identical scores.
    corpus_version TEXT NOT NULL CHECK (corpus_version ~ '^[0-9a-f]{64}$'),

    -- {relation: threshold}. Per-relation, because "close enough to be the same"
    -- and "close enough to be related" were never one number.
    thresholds JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(thresholds) = 'object'),
    -- Score gap under which two candidates are declared AMBIGUOUS rather than
    -- ranked. Ambiguity is an outcome, not a tie-break.
    ambiguity_band NUMERIC(5, 4) NOT NULL DEFAULT 0.03
        CHECK (ambiguity_band >= 0 AND ambiguity_band <= 1),

    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (org_id, version_number),
    UNIQUE (org_id, content_hash)
);

CREATE OR REPLACE FUNCTION app.reject_entity_match_policy_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'entity match policy versions are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_entity_match_policy_immutable ON app.entity_match_policy_versions;
CREATE TRIGGER trg_entity_match_policy_immutable
    BEFORE UPDATE OR DELETE ON app.entity_match_policy_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_entity_match_policy_mutation();

COMMENT ON TABLE app.entity_match_policy_versions IS
    'Story 48.5: the ranking policy a candidate decision pins. Immutable, because a threshold edited underneath a past decision silently reinterprets it.';

-- ---------------------------------------------------------------------------
-- 2. The governed source identity. GOVERNANCE.
--
--    "This organization identity is known to THAT connector as THAT identifier."
--    It is a Governance fact about meaning. It carries NO report, NO filter and
--    NO account configuration -- those are physical, they differ per Datastream,
--    and putting them here is what made `entity_source_bindings` unusable as an
--    organization master.
--
--    One governed identity may be reused by several compatible Datastream
--    bindings. That reuse is the whole point.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_source_identity_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^esi_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,

    -- The organization identity this represents, and the exact version of it.
    node_id TEXT NOT NULL REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT,
    node_version_id TEXT REFERENCES app.master_data_object_versions(id) ON DELETE RESTRICT,

    -- The Connector whose vocabulary this identifier belongs to. A name, not an
    -- interpretation: core never branches on it (AD-2).
    connector_name TEXT NOT NULL CHECK (connector_name ~ '^[a-z][a-z0-9-]{1,60}$'),
    -- Optional narrower scope inside that Connector (an account, a property).
    -- NULL means the identifier is valid for any account of that Connector.
    account_scope TEXT,

    -- The source's own identifier, verbatim. Opaque.
    external_id TEXT NOT NULL CHECK (length(btrim(external_id)) BETWEEN 1 AND 400),
    -- What the source calls it, for display beside the identifier. A projection,
    -- never a join key.
    external_label TEXT,

    version_number INTEGER NOT NULL CHECK (version_number > 0),

    -- WHY this identifier is believed to denote this identity: the observation,
    -- the candidate decision, or the operator who typed it. Never empty.
    provenance TEXT NOT NULL
        CHECK (provenance IN ('operator', 'confirmed_candidate', 'connector_declared')),
    provenance_reference TEXT,
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(evidence) = 'object'),

    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retired_at TIMESTAMPTZ,
    retired_by TEXT,

    UNIQUE (org_id, node_id, connector_name, version_number)
);

-- One LIVE identifier per (connector, account scope, external id) inside an org.
-- Two live rows would mean one source identifier denotes two organization
-- identities, which is a collision the server must refuse rather than resolve
-- by write order.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_source_identity_live
    ON app.entity_source_identity_versions
       (org_id, connector_name, COALESCE(account_scope, ''), external_id)
    WHERE retired_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_entity_source_identity_node
    ON app.entity_source_identity_versions (node_id) WHERE retired_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_entity_source_identity_connector
    ON app.entity_source_identity_versions (org_id, connector_name) WHERE retired_at IS NULL;

-- Content is frozen; only retirement moves. A binding pins this row.
CREATE OR REPLACE FUNCTION app.protect_entity_source_identity()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a governed source identity is retired, never deleted';
    END IF;
    IF (
           NEW.node_id IS DISTINCT FROM OLD.node_id
        OR NEW.connector_name IS DISTINCT FROM OLD.connector_name
        OR NEW.account_scope IS DISTINCT FROM OLD.account_scope
        OR NEW.external_id IS DISTINCT FROM OLD.external_id
        OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
        OR NEW.version_number IS DISTINCT FROM OLD.version_number
    ) THEN
        RAISE EXCEPTION 'a governed source identity is immutable; retire it and record a new version';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_entity_source_identity_protect
    ON app.entity_source_identity_versions;
CREATE TRIGGER trg_entity_source_identity_protect
    BEFORE UPDATE OR DELETE ON app.entity_source_identity_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.protect_entity_source_identity();

COMMENT ON TABLE app.entity_source_identity_versions IS
    'Story 48.5: what one Connector calls an organization identity. Governance-owned MEANING; carries no report, filter or account configuration -- those are physical, differ per Datastream, and belong to app.datastream_entity_binding_versions.';

-- ---------------------------------------------------------------------------
-- 3. What a Datastream actually saw. DATA. Append-only.
--
--    Epic 40's matcher had no production caller, so nothing was ever observed:
--    candidate matching existed only in tests. An observation is the missing
--    input, and it is worth nothing without the evidence that says WHERE it came
--    from -- which is why every column below is NOT NULL by default.
--
--    The value itself is stored as a HASH plus a permitted display projection.
--    Provider values are untrusted and may be personal; the hash is what a
--    decision matches on, and the display value is what an authorized view
--    renders (AD-31).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_observation_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^eobs_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    datastream_id TEXT NOT NULL,

    -- The exact authorized scope the value was read under.
    connection_ref_id TEXT,
    account_scope TEXT,
    connector_name TEXT NOT NULL CHECK (connector_name ~ '^[a-z][a-z0-9-]{1,60}$'),
    -- The Connector contract that was in force. A changed contract invalidates
    -- what this observation is evidence OF.
    connector_fingerprint TEXT NOT NULL CHECK (connector_fingerprint ~ '^[0-9a-f]{64}$'),

    report_id TEXT NOT NULL CHECK (length(btrim(report_id)) BETWEEN 1 AND 120),
    field_id TEXT NOT NULL CHECK (length(btrim(field_id)) BETWEEN 1 AND 120),
    grain TEXT[] NOT NULL DEFAULT '{}',

    -- Which run produced it, and which published version made it readable.
    pull_id TEXT,
    publication_version_id TEXT,
    plan_version_id TEXT,
    mapping_version_id TEXT,

    -- The value. `raw_value_hash` is the match key; `display_value` is the
    -- projection an authorized reader may see. A privacy-truncated or withheld
    -- value keeps its own state rather than being dropped.
    raw_value_hash TEXT NOT NULL CHECK (raw_value_hash ~ '^[0-9a-f]{64}$'),
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) BETWEEN 1 AND 400),
    display_value TEXT,
    value_state TEXT NOT NULL DEFAULT 'observed'
        CHECK (value_state IN ('observed', 'privacy_truncated', 'withheld', 'unresolved')),

    -- The population caveat the Connector declared for this report, carried with
    -- the row so a coverage number can never quietly claim a complete universe.
    population_completeness TEXT NOT NULL DEFAULT 'declared_scope'
        CHECK (population_completeness IN ('declared_scope', 'reportable_subset')),

    observed_from DATE NOT NULL,
    observed_to DATE NOT NULL,
    occurrence_count BIGINT NOT NULL DEFAULT 1 CHECK (occurrence_count >= 0),

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CHECK (observed_to >= observed_from),
    UNIQUE (project_id, id),
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

-- The same value seen by the same report over the same window is ONE
-- observation, not one per replay -- otherwise a re-run inflates every count.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_observation_window
    ON app.entity_observation_versions
       (project_id, datastream_id, report_id, field_id, raw_value_hash, observed_from, observed_to);

CREATE INDEX IF NOT EXISTS idx_entity_observation_project
    ON app.entity_observation_versions (project_id, datastream_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_entity_observation_value
    ON app.entity_observation_versions (org_id, normalized_value);

CREATE OR REPLACE FUNCTION app.reject_entity_observation_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'entity observations are append-only';
END;
$$;
DROP TRIGGER IF EXISTS trg_entity_observation_append_only ON app.entity_observation_versions;
CREATE TRIGGER trg_entity_observation_append_only
    BEFORE UPDATE OR DELETE ON app.entity_observation_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_entity_observation_mutation();

COMMENT ON TABLE app.entity_observation_versions IS
    'Story 48.5: what an exact Datastream report/field observed under authorization. Append-only. The raw value is hashed because provider values are untrusted; display_value is the projection an authorized view may render.';

-- ---------------------------------------------------------------------------
-- 4. The candidate decision. GOVERNANCE. Append-only.
--
--    Ranking proposes; a human decides. Epic 40 collapsed the two into
--    `outcome='resolved'`, which is why a machine could write an active match.
--    Here `state` starts at `proposed` and only an authorized confirmation
--    writes a row carrying `decided_by`.
--
--    `relation` is SKOS-typed for the reason SKOS types it: exact, close,
--    broader, narrower and related are five different claims, a close match is
--    not transitive, and `negative`/`refused` must survive so a rejected mapping
--    does not resurface every time the matcher runs again.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_match_decisions (
    id TEXT PRIMARY KEY CHECK (id ~ '^emd_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- The observation this decision is ABOUT. The server reads the value from
    -- here; a caller correcting a decision cannot substitute another string.
    observation_id TEXT REFERENCES app.entity_observation_versions(id) ON DELETE RESTRICT,
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) BETWEEN 1 AND 400),
    raw_value_hash TEXT NOT NULL CHECK (raw_value_hash ~ '^[0-9a-f]{64}$'),

    state TEXT NOT NULL DEFAULT 'proposed'
        CHECK (state IN ('proposed', 'confirmed', 'refused', 'superseded')),
    relation TEXT NOT NULL
        CHECK (relation IN ('exact', 'close', 'broader', 'narrower', 'related', 'negative', 'none')),

    -- The identity the decision lands on. NULL while `proposed` with no
    -- candidate, and on a refusal that names nothing.
    node_id TEXT REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT,
    node_version_id TEXT REFERENCES app.master_data_object_versions(id) ON DELETE RESTRICT,

    -- The ranked alternatives, with scores and the features that produced them.
    -- Kept even on a confirmation: what was NOT chosen is part of the evidence.
    ranked_candidates JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(ranked_candidates) = 'array'),
    confidence NUMERIC(5, 4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    reason_code TEXT NOT NULL DEFAULT 'ranked'
        CHECK (reason_code IN (
            'ranked', 'no_candidate', 'below_threshold', 'ambiguous',
            'negative_alias', 'operator_choice', 'privacy_truncated'
        )),
    reason_features JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(reason_features) = 'object'),

    policy_version_id TEXT NOT NULL
        REFERENCES app.entity_match_policy_versions(id) ON DELETE RESTRICT,

    -- Supersession is a NEW row pointing back, never an edit.
    supersedes_id TEXT REFERENCES app.entity_match_decisions(id) ON DELETE RESTRICT,

    proposed_by TEXT NOT NULL,
    decided_by TEXT,
    decided_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A decision that left `proposed` names the human who took it. This is the
    -- constraint that makes "matching cannot write an active match" a database
    -- fact rather than a convention in a service.
    CHECK ((state IN ('confirmed', 'refused')) = (decided_by IS NOT NULL)),
    CHECK ((state IN ('confirmed', 'refused')) = (decided_at IS NOT NULL)),
    -- A confirmation that claims a relation names the identity it relates to.
    CHECK (
        state <> 'confirmed'
        OR relation = 'none'
        OR node_id IS NOT NULL
    )
);

-- At most one LIVE decision per observed value per Project. A second one would
-- make "what does this string denote here?" ambiguous.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_match_decision_live
    ON app.entity_match_decisions (org_id, COALESCE(project_id, ''), raw_value_hash)
    WHERE state IN ('proposed', 'confirmed', 'refused');

CREATE INDEX IF NOT EXISTS idx_entity_match_decision_node
    ON app.entity_match_decisions (node_id) WHERE state = 'confirmed';
CREATE INDEX IF NOT EXISTS idx_entity_match_decision_queue
    ON app.entity_match_decisions (org_id, project_id, state, created_at DESC);

CREATE OR REPLACE FUNCTION app.protect_entity_match_decision()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'entity match decisions are append-only';
    END IF;
    -- Only the walk to `superseded` is allowed, and nothing else moves with it.
    IF NEW.state IS DISTINCT FROM OLD.state AND NEW.state <> 'superseded' THEN
        RAISE EXCEPTION 'a match decision is superseded by a new row, never edited';
    END IF;
    IF (
           NEW.relation IS DISTINCT FROM OLD.relation
        OR NEW.node_id IS DISTINCT FROM OLD.node_id
        OR NEW.raw_value_hash IS DISTINCT FROM OLD.raw_value_hash
        OR NEW.confidence IS DISTINCT FROM OLD.confidence
        OR NEW.policy_version_id IS DISTINCT FROM OLD.policy_version_id
        OR NEW.decided_by IS DISTINCT FROM OLD.decided_by
    ) THEN
        RAISE EXCEPTION 'match decision content is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_entity_match_decision_protect ON app.entity_match_decisions;
CREATE TRIGGER trg_entity_match_decision_protect
    BEFORE UPDATE OR DELETE ON app.entity_match_decisions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.protect_entity_match_decision();

COMMENT ON TABLE app.entity_match_decisions IS
    'Story 48.5: ranking proposes, a human decides. A row leaving `proposed` must name decided_by -- the CHECK is what makes "matching never writes an active match" a database fact. SKOS-typed relations keep exact, close and refused as three different claims.';

-- ---------------------------------------------------------------------------
-- 5. The physical binding. DATA. Immutable versions.
--
--    This is what `entity_source_bindings` should have been: not an
--    organization-wide opaque handle, but one EXACT Datastream, its account, its
--    report, its fields, the query driver the Connector declared, and the state
--    of its application. It pins the Connector contract it was validated
--    against, so a changed contract invalidates it instead of silently
--    continuing to drive a request that no longer exists.
--
--    `application_state` is deliberately not a boolean. `candidate` means Data
--    has a non-live plan/mapping proposal; `published` means a run may consume
--    it. Confirmation of a Project registry edit produces `candidate` and never
--    `published`: normal Data review stays required.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.datastream_entity_binding_versions (
    id TEXT PRIMARY KEY CHECK (id ~ '^deb_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    datastream_id TEXT NOT NULL,

    -- The governed identity this Datastream will collect or observe.
    source_identity_version_id TEXT
        REFERENCES app.entity_source_identity_versions(id) ON DELETE RESTRICT,
    node_id TEXT NOT NULL REFERENCES app.master_data_nodes(id) ON DELETE RESTRICT,
    -- The Project role at the moment of binding, pinned so a later role change
    -- does not rewrite what a past run collected.
    project_role TEXT NOT NULL CHECK (project_role ~ '^[a-z][a-z0-9_]{1,39}$'),
    project_association_id TEXT
        REFERENCES app.master_data_project_associations(id) ON DELETE RESTRICT,

    version_number INTEGER NOT NULL CHECK (version_number > 0),

    -- The exact physical application.
    connector_name TEXT NOT NULL CHECK (connector_name ~ '^[a-z][a-z0-9-]{1,60}$'),
    connection_ref_id TEXT,
    account_scope TEXT,
    report_id TEXT NOT NULL CHECK (length(btrim(report_id)) BETWEEN 1 AND 120),
    field_ids TEXT[] NOT NULL DEFAULT '{}',
    direction TEXT NOT NULL CHECK (direction IN ('observe', 'collect')),
    -- {parameter, value_source, cardinality, own_marker_parameter, values}. The
    -- shape the Connector declared, resolved to exact values. Core passes it
    -- through and interprets nothing about it.
    query_driver JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(query_driver) = 'object'),

    -- The contract this was validated against. Drift is detected by comparing.
    connector_fingerprint TEXT NOT NULL CHECK (connector_fingerprint ~ '^[0-9a-f]{64}$'),

    plan_version_id TEXT,
    mapping_version_id TEXT,
    publication_version_id TEXT,

    application_state TEXT NOT NULL DEFAULT 'candidate'
        CHECK (application_state IN ('candidate', 'published', 'superseded', 'excluded')),

    -- A Datastream-local decision that deliberately departs from the Project
    -- intent. Confirmation of a Project edit must PRESERVE it, never overwrite.
    exception_reason_code TEXT,
    exception_reason TEXT,

    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    published_at TIMESTAMPTZ,
    published_by TEXT,

    CHECK ((application_state = 'published') = (published_at IS NOT NULL)),
    -- An exclusion says why. An unreasoned exclusion is indistinguishable from
    -- a forgotten one.
    CHECK (application_state <> 'excluded' OR exception_reason_code IS NOT NULL),
    -- A collect binding drives a request; it must name the identity it drives with.
    CHECK (direction <> 'collect' OR source_identity_version_id IS NOT NULL),

    UNIQUE (project_id, id),
    UNIQUE (project_id, datastream_id, node_id, version_number),
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

-- One live binding per (Datastream, identity). Two would mean the same entity is
-- collected twice from the same place.
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastream_entity_binding_live
    ON app.datastream_entity_binding_versions (project_id, datastream_id, node_id)
    WHERE application_state IN ('candidate', 'published', 'excluded');

CREATE INDEX IF NOT EXISTS idx_datastream_entity_binding_datastream
    ON app.datastream_entity_binding_versions
       (project_id, datastream_id, application_state, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_datastream_entity_binding_node
    ON app.datastream_entity_binding_versions (node_id, application_state);

CREATE OR REPLACE FUNCTION app.protect_datastream_entity_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a Datastream entity binding version is superseded, never deleted';
    END IF;
    IF (
           NEW.node_id IS DISTINCT FROM OLD.node_id
        OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
        OR NEW.report_id IS DISTINCT FROM OLD.report_id
        OR NEW.query_driver IS DISTINCT FROM OLD.query_driver
        OR NEW.content_hash IS DISTINCT FROM OLD.content_hash
        OR NEW.connector_fingerprint IS DISTINCT FROM OLD.connector_fingerprint
        OR NEW.version_number IS DISTINCT FROM OLD.version_number
    ) THEN
        RAISE EXCEPTION 'binding content is immutable; record a new version';
    END IF;
    IF OLD.application_state = 'superseded' AND NEW.application_state <> 'superseded' THEN
        RAISE EXCEPTION 'a superseded binding never returns to service';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_datastream_entity_binding_protect
    ON app.datastream_entity_binding_versions;
CREATE TRIGGER trg_datastream_entity_binding_protect
    BEFORE UPDATE OR DELETE ON app.datastream_entity_binding_versions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.protect_datastream_entity_binding();

COMMENT ON TABLE app.datastream_entity_binding_versions IS
    'Story 48.5: the EXACT physical application of a governed identity to one Datastream. Replaces app.entity_source_bindings, whose opaque org-wide `source` string plus unvalidated query_spec let one row mark an entire Project Complete. application_state=candidate is what a confirmed Project edit produces; publication stays a separate Data decision.';

-- ---------------------------------------------------------------------------
-- 6. RGPD erasure, stated rather than assumed.
--
--    Migration 099 retro-fitted a `WHEN (current_setting('app.rgpd_erasure',
--    true) IS DISTINCT FROM 'on')` condition onto the protective DELETE triggers
--    of fifteen org-scoped tables, because an unconditional guard makes an
--    organization undeletable -- silently, months later. Its own header says a
--    new append-only table that forgets this breaks the erasure again.
--
--    All five triggers above carry that condition FROM BIRTH, so 099 has nothing
--    to retro-fit and no follow-up migration is owed. The other half is
--    `core/org_purge.py`, which reads the foreign-key graph from `pg_constraint`
--    at call time rather than from a list: every table here references
--    `app.organizations` with ON DELETE RESTRICT, so the purge walks them
--    automatically.
--
--    Verifiable after applying:
--      SELECT c.relname, pg_get_triggerdef(t.oid) ILIKE '%rgpd_erasure%' AS yields
--        FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid
--       WHERE c.relnamespace = 'app'::regnamespace AND NOT t.tgisinternal
--         AND (t.tgtype & 8) > 0
--         AND c.relname LIKE 'entity_%' OR c.relname = 'datastream_entity_binding_versions';
-- ---------------------------------------------------------------------------

COMMIT;
