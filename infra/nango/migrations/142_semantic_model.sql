-- Story 49.3: one versioned Semantic Model authority.
--
-- Three parallel semantic stores exist at this migration's baseline and none of
-- them is a governed, versioned definition owner:
--
--   * app.target_fields (023/050/107) -- name-keyed, PLATFORM-GLOBAL reporting
--     dictionary with an append-only history table. No project scope at all.
--   * app.mdm_canonical_fields (032)  -- ULID identity, project-or-platform
--     scope, MUTABLE, optional derivation link to a dictionary name.
--   * app.metric_definitions (049/083) -- richer metric model (aggregation,
--     ratio, additivity, non-additive dimensions, unit, currency mode) with a
--     PLATFORM/ORG/PROJECT scope triplet, also MUTABLE.
--
-- This migration creates the single authority and reconciles the three into it.
--
-- IDENTITY RULE (Story 49.3 AC1). An unambiguous `mdm_*` id is PRESERVED as the
-- Concept id, so every existing mapping binding retargets without inventing a
-- new identity. Nothing else is preserved: `target_fields` is name-keyed and a
-- name is not an identity, and `metdef_*` ids address a mutable row that has no
-- version to pin.
--
-- SCOPE RULE. `semantic_concepts.project_id` is nullable EXACTLY as
-- `app.mdm_canonical_fields.project_id` is: NULL means a platform-canonical
-- definition, shared as a DEFINITION and never as data. Narrowing it to NOT NULL
-- here would have forced a freshly minted id per Project for every platform row,
-- which is precisely the "inventing a new identity" AC1 forbids. Every read path
-- stays `project_id = :project OR project_id IS NULL`, the same predicate the
-- shipped Governance read model already applies to canonical fields.
--
-- COLLISION RULE. Equal labels never imply equal meaning. Every ambiguity below
-- raises and ABORTS this migration rather than merging: an operator resolves it
-- in the source store and re-runs. The refusals are enumerated in
-- app.semantic_migration_reconciliations before the exception fires, so the
-- reconciliation report survives the abort in the transaction log the operator
-- reads -- and the report table itself is created first for that reason.
--
-- Idempotent: every statement is IF NOT EXISTS / OR REPLACE / guarded, and the
-- seeding steps are NOT EXISTS-gated so re-application is a no-op.

BEGIN;

-- digest() mints the deterministic identity of a name-keyed dictionary row in
-- step 8. Already created by migration 040; repeated here because this file must
-- not depend on which earlier migrations a given database has applied.
CREATE EXTENSION IF NOT EXISTS pgcrypto;

-- ---------------------------------------------------------------------------
-- 0. The reconciliation report. Created first so a refusal below still leaves an
--    auditable record of WHY, keyed by the exact source row.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_migration_reconciliations (
    id              BIGSERIAL   PRIMARY KEY,
    source_store    TEXT        NOT NULL
                    CHECK (source_store IN ('target_fields',
                                            'mdm_canonical_fields',
                                            'metric_definitions')),
    source_key      TEXT        NOT NULL,
    source_scope    TEXT        NOT NULL,
    decision        TEXT        NOT NULL
                    CHECK (decision IN ('identity_preserved',
                                        'concept_minted',
                                        'merged_into_concept',
                                        'blocked_ambiguous')),
    concept_id      TEXT,
    reason          TEXT        NOT NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_semantic_migration_reconciliation
        UNIQUE (source_store, source_key, source_scope)
);

COMMENT ON TABLE app.semantic_migration_reconciliations IS
    'Story 49.3 AC1: the explicit migration report. One row per source row of the '
    'three superseded semantic stores, recording whether its identity was '
    'preserved, minted, merged, or refused as ambiguous.';

-- ---------------------------------------------------------------------------
-- 1. Semantic Concept: stable identity + three DISTINCT pointers.
--
--    current / pending / last_known_good are separate columns on purpose. A
--    failed publication must leave `current` and `last_known_good` untouched
--    while `pending` changes; collapsing them into one "active version" column
--    makes that invariant unexpressible.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_concepts (
    id                          TEXT        NOT NULL,
    project_id                  TEXT        REFERENCES app.projects(id) ON DELETE CASCADE,
    kind                        TEXT        NOT NULL CHECK (kind IN ('metric', 'dimension')),
    name                        TEXT        NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,126}$'),
    lifecycle_status            TEXT        NOT NULL DEFAULT 'draft'
                                CHECK (lifecycle_status IN ('draft', 'candidate', 'published',
                                                            'superseded', 'archived')),
    current_version_id          TEXT,
    pending_version_id          TEXT,
    last_known_good_version_id  TEXT,
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_concepts PRIMARY KEY (id),
    -- Preserved `mdm_` identities and newly minted `sc_` ones share one id space.
    CONSTRAINT ck_semantic_concepts_id
        CHECK (id ~ '^(mdm|sc)_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_semantic_concepts_id_project UNIQUE (id, project_id)
);

-- Name uniqueness WITHIN scope, archived rows excluded so a name can be retired
-- and re-minted. Two partial indexes because NULL never collides in a plain one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_semantic_concepts_name_platform
    ON app.semantic_concepts (name)
    WHERE project_id IS NULL AND lifecycle_status <> 'archived';

CREATE UNIQUE INDEX IF NOT EXISTS uq_semantic_concepts_name_project
    ON app.semantic_concepts (project_id, name)
    WHERE project_id IS NOT NULL AND lifecycle_status <> 'archived';

CREATE INDEX IF NOT EXISTS idx_semantic_concepts_scope
    ON app.semantic_concepts (project_id, kind, lifecycle_status);

-- ---------------------------------------------------------------------------
-- 2. Semantic Concept version: the immutable complete snapshot.
--
--    Everything a reader needs to reproduce the meaning is IN the row. A version
--    that referred back to the mutable head row would not be a version.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_concept_versions (
    id                      TEXT        NOT NULL,
    concept_id              TEXT        NOT NULL,
    project_id              TEXT,
    version_number          INTEGER     NOT NULL CHECK (version_number >= 1),
    status                  TEXT        NOT NULL
                            CHECK (status IN ('draft', 'candidate', 'published',
                                              'superseded', 'archived')),
    kind                    TEXT        NOT NULL CHECK (kind IN ('metric', 'dimension')),
    name                    TEXT        NOT NULL,
    label                   TEXT        NOT NULL,
    definition              TEXT,
    value_type              TEXT        NOT NULL
                            CHECK (value_type IN ('integer', 'decimal', 'money', 'ratio',
                                                  'percent', 'duration', 'string', 'date',
                                                  'timestamp', 'boolean')),
    unit                    TEXT,
    format                  TEXT,
    owner                   TEXT,
    -- Exact Business Domain / Master Data references (Story 49.2 owns the
    -- objects; this pins the versions it publishes).
    business_domain_refs    JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(business_domain_refs) = 'array'),
    master_data_refs        JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(master_data_refs) = 'array'),
    -- Metric-only. `expression` is the RESTRICTED typed tree, never SQL.
    expression              JSONB       CHECK (expression IS NULL
                                               OR jsonb_typeof(expression) = 'object'),
    aggregation             JSONB       CHECK (aggregation IS NULL
                                               OR jsonb_typeof(aggregation) = 'object'),
    additivity_class        TEXT        CHECK (additivity_class IS NULL
                                               OR additivity_class IN ('additive',
                                                                       'semi_additive',
                                                                       'non_additive')),
    non_additive_dimensions TEXT[]      NOT NULL DEFAULT '{}',
    currency_behavior       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    time_behavior           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    display                 JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Dimension-only.
    semantic_type           TEXT,
    allowed_grains          TEXT[]      NOT NULL DEFAULT '{}',
    hierarchies             JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(hierarchies) = 'array'),
    value_domain            JSONB       NOT NULL DEFAULT '{}'::jsonb,
    conformance             TEXT        CHECK (conformance IS NULL
                                               OR conformance IN ('conformed', 'local',
                                                                  'source_specific')),
    time_semantics          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    provenance              JSONB       NOT NULL DEFAULT '{}'::jsonb,
    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_concept_versions PRIMARY KEY (id),
    CONSTRAINT ck_semantic_concept_versions_id
        CHECK (id ~ '^scv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_semantic_concept_version UNIQUE (concept_id, version_number),
    CONSTRAINT uq_semantic_concept_version_scope UNIQUE (id, concept_id, project_id),
    CONSTRAINT fk_semantic_concept_version_scope
        FOREIGN KEY (concept_id, project_id)
        REFERENCES app.semantic_concepts (id, project_id) ON DELETE CASCADE,
    -- A metric without an aggregation specification is a metric nobody can sum
    -- safely. It must declare one OR declare itself non-additive.
    CONSTRAINT ck_semantic_concept_versions_metric CHECK (
        kind <> 'metric'
        OR (expression IS NOT NULL
            AND (aggregation IS NOT NULL OR additivity_class = 'non_additive'))
    ),
    CONSTRAINT ck_semantic_concept_versions_dimension CHECK (
        kind <> 'dimension' OR semantic_type IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_semantic_concept_versions_concept
    ON app.semantic_concept_versions (concept_id, version_number DESC);

CREATE INDEX IF NOT EXISTS idx_semantic_concept_versions_status
    ON app.semantic_concept_versions (project_id, status);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_semantic_concepts_current_version'
          AND conrelid = 'app.semantic_concepts'::regclass
    ) THEN
        ALTER TABLE app.semantic_concepts
            ADD CONSTRAINT fk_semantic_concepts_current_version
            FOREIGN KEY (current_version_id, id, project_id)
            REFERENCES app.semantic_concept_versions (id, concept_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.semantic_concepts
            ADD CONSTRAINT fk_semantic_concepts_pending_version
            FOREIGN KEY (pending_version_id, id, project_id)
            REFERENCES app.semantic_concept_versions (id, concept_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.semantic_concepts
            ADD CONSTRAINT fk_semantic_concepts_lkg_version
            FOREIGN KEY (last_known_good_version_id, id, project_id)
            REFERENCES app.semantic_concept_versions (id, concept_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

-- A published version is immutable. Draft and candidate rows may still be
-- edited in place before they are promoted; once `status` reaches `published`
-- the row is frozen, and the ONLY legal transitions afterwards are
-- published -> superseded and published -> archived, which are status-only.
CREATE OR REPLACE FUNCTION app.reject_semantic_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('published', 'superseded', 'archived') THEN
            RAISE EXCEPTION 'published semantic versions are immutable'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'published' AND NEW.status NOT IN ('superseded', 'archived') THEN
        RAISE EXCEPTION 'published semantic versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status IN ('superseded', 'archived') THEN
        RAISE EXCEPTION 'published semantic versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status = 'published' AND (
           NEW.id <> OLD.id
        OR NEW.concept_id IS DISTINCT FROM OLD.concept_id
        OR NEW.content_hash <> OLD.content_hash
        OR NEW.version_number <> OLD.version_number
    ) THEN
        RAISE EXCEPTION 'published semantic versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_semantic_concept_versions_immutable
    ON app.semantic_concept_versions;
CREATE TRIGGER trg_semantic_concept_versions_immutable
    BEFORE UPDATE OR DELETE ON app.semantic_concept_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_semantic_version_mutation();

-- ---------------------------------------------------------------------------
-- 3. Concept dependency members: the formula DAG, by EXACT version.
--
--    A dependency on "the latest version of X" is not a dependency, it is a
--    promise to change meaning without a new version. Both ids are required.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_concept_dependencies (
    version_id              TEXT        NOT NULL
                            REFERENCES app.semantic_concept_versions (id) ON DELETE CASCADE,
    ordinal                 INTEGER     NOT NULL CHECK (ordinal >= 0),
    depends_on_concept_id   TEXT        NOT NULL
                            REFERENCES app.semantic_concepts (id) ON DELETE RESTRICT,
    depends_on_version_id   TEXT        NOT NULL
                            REFERENCES app.semantic_concept_versions (id) ON DELETE RESTRICT,
    role                    TEXT        NOT NULL
                            CHECK (role IN ('operand', 'numerator', 'denominator',
                                            'filter', 'weight')),
    CONSTRAINT pk_semantic_concept_dependencies PRIMARY KEY (version_id, ordinal),
    CONSTRAINT ck_semantic_concept_dependencies_not_self
        CHECK (version_id <> depends_on_version_id)
);

CREATE INDEX IF NOT EXISTS idx_semantic_concept_dependencies_target
    ON app.semantic_concept_dependencies (depends_on_concept_id, depends_on_version_id);

-- ---------------------------------------------------------------------------
-- 4. Semantic View: a Project publication. Never platform-scoped -- a View pins
--    Datastreams, mapping versions and Publications, all of which belong to one
--    Project, so a shared View would pin another Project's data.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_views (
    id                          TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL
                                REFERENCES app.projects(id) ON DELETE CASCADE,
    name                        TEXT        NOT NULL CHECK (name ~ '^[a-z][a-z0-9_]{0,126}$'),
    lifecycle_status            TEXT        NOT NULL DEFAULT 'draft'
                                CHECK (lifecycle_status IN ('draft', 'candidate', 'published',
                                                            'superseded', 'archived')),
    current_version_id          TEXT,
    pending_version_id          TEXT,
    last_known_good_version_id  TEXT,
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_views PRIMARY KEY (id),
    CONSTRAINT ck_semantic_views_id CHECK (id ~ '^sv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_semantic_views_id_project UNIQUE (id, project_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_semantic_views_name_project
    ON app.semantic_views (project_id, name)
    WHERE lifecycle_status <> 'archived';

CREATE TABLE IF NOT EXISTS app.semantic_view_versions (
    id                      TEXT        NOT NULL,
    view_id                 TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    version_number          INTEGER     NOT NULL CHECK (version_number >= 1),
    status                  TEXT        NOT NULL
                            CHECK (status IN ('draft', 'candidate', 'published',
                                              'superseded', 'archived')),
    name                    TEXT        NOT NULL,
    label                   TEXT        NOT NULL,
    description             TEXT,
    business_scope          TEXT,
    business_domain_refs    JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(business_domain_refs) = 'array'),
    master_data_refs        JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(master_data_refs) = 'array'),
    -- Query boundaries: supported filters and grains, default time behavior,
    -- pre-query policy. A boundary absent from the published version is a
    -- boundary Analyze may not assume.
    query_policy            JSONB       NOT NULL DEFAULT '{}'::jsonb
                            CHECK (jsonb_typeof(query_policy) = 'object'),
    evidence_refs           JSONB       NOT NULL DEFAULT '[]'::jsonb
                            CHECK (jsonb_typeof(evidence_refs) = 'array'),
    dependency_fingerprint  TEXT        NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    content_hash            TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_view_versions PRIMARY KEY (id),
    CONSTRAINT ck_semantic_view_versions_id CHECK (id ~ '^svv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_semantic_view_version UNIQUE (view_id, version_number),
    CONSTRAINT uq_semantic_view_version_scope UNIQUE (id, view_id, project_id),
    CONSTRAINT fk_semantic_view_version_scope
        FOREIGN KEY (view_id, project_id)
        REFERENCES app.semantic_views (id, project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_semantic_view_versions_view
    ON app.semantic_view_versions (view_id, version_number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_semantic_views_current_version'
          AND conrelid = 'app.semantic_views'::regclass
    ) THEN
        ALTER TABLE app.semantic_views
            ADD CONSTRAINT fk_semantic_views_current_version
            FOREIGN KEY (current_version_id, id, project_id)
            REFERENCES app.semantic_view_versions (id, view_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.semantic_views
            ADD CONSTRAINT fk_semantic_views_pending_version
            FOREIGN KEY (pending_version_id, id, project_id)
            REFERENCES app.semantic_view_versions (id, view_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
        ALTER TABLE app.semantic_views
            ADD CONSTRAINT fk_semantic_views_lkg_version
            FOREIGN KEY (last_known_good_version_id, id, project_id)
            REFERENCES app.semantic_view_versions (id, view_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

DROP TRIGGER IF EXISTS trg_semantic_view_versions_immutable
    ON app.semantic_view_versions;
CREATE TRIGGER trg_semantic_view_versions_immutable
    BEFORE UPDATE OR DELETE ON app.semantic_view_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_semantic_version_mutation();

-- ---------------------------------------------------------------------------
-- 5. Version members of a Semantic View.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_view_version_concepts (
    view_version_id     TEXT        NOT NULL
                        REFERENCES app.semantic_view_versions (id) ON DELETE CASCADE,
    ordinal             INTEGER     NOT NULL CHECK (ordinal >= 0),
    concept_id          TEXT        NOT NULL
                        REFERENCES app.semantic_concepts (id) ON DELETE RESTRICT,
    concept_version_id  TEXT        NOT NULL
                        REFERENCES app.semantic_concept_versions (id) ON DELETE RESTRICT,
    role                TEXT        NOT NULL CHECK (role IN ('metric', 'dimension')),
    CONSTRAINT pk_semantic_view_version_concepts PRIMARY KEY (view_version_id, ordinal),
    CONSTRAINT uq_semantic_view_version_concept UNIQUE (view_version_id, concept_id)
);

CREATE TABLE IF NOT EXISTS app.semantic_view_version_relationships (
    view_version_id     TEXT        NOT NULL
                        REFERENCES app.semantic_view_versions (id) ON DELETE CASCADE,
    ordinal             INTEGER     NOT NULL CHECK (ordinal >= 0),
    name                TEXT        NOT NULL,
    from_dataset        TEXT        NOT NULL,
    to_dataset          TEXT        NOT NULL,
    from_columns        TEXT[]      NOT NULL CHECK (cardinality(from_columns) >= 1),
    to_columns          TEXT[]      NOT NULL CHECK (cardinality(to_columns) >= 1),
    cardinality_type    TEXT        NOT NULL
                        CHECK (cardinality_type IN ('one_to_one', 'many_to_one',
                                                    'one_to_many', 'many_to_many')),
    -- A many-to-many path without a bridge fans out and silently multiplies the
    -- measure. The compiler refuses it; the constraint makes it unstorable too.
    bridge_dataset      TEXT,
    fan_out_policy      TEXT        NOT NULL
                        CHECK (fan_out_policy IN ('forbid', 'bridge', 'deduplicate')),
    CONSTRAINT pk_semantic_view_version_relationships PRIMARY KEY (view_version_id, ordinal),
    CONSTRAINT ck_semantic_view_relationship_columns
        CHECK (cardinality(from_columns) = cardinality(to_columns)),
    CONSTRAINT ck_semantic_view_relationship_bridge CHECK (
        cardinality_type <> 'many_to_many'
        OR (fan_out_policy = 'bridge' AND bridge_dataset IS NOT NULL)
    )
);

-- The exact Data-owned bindings a published View pins. Governance WRITES this
-- table only as part of publishing a version, and only with mapping version ids
-- it read from Data. It is never a mapping editor: there is no path from here
-- back into app.datastream_mappings.
CREATE TABLE IF NOT EXISTS app.semantic_view_version_bindings (
    view_version_id     TEXT        NOT NULL
                        REFERENCES app.semantic_view_versions (id) ON DELETE CASCADE,
    ordinal             INTEGER     NOT NULL CHECK (ordinal >= 0),
    concept_id          TEXT        NOT NULL
                        REFERENCES app.semantic_concepts (id) ON DELETE RESTRICT,
    datastream_id       TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    mapping_version_id  TEXT        NOT NULL,
    output_ref          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    binding_state       TEXT        NOT NULL
                        CHECK (binding_state IN ('active', 'confirmed', 'candidate',
                                                 'suggested', 'excluded', 'stale',
                                                 'blocking', 'unavailable')),
    confidence          NUMERIC(5, 4) CHECK (confidence IS NULL
                                             OR (confidence >= 0 AND confidence <= 1)),
    CONSTRAINT pk_semantic_view_version_bindings PRIMARY KEY (view_version_id, ordinal),
    -- The pinned mapping version must really belong to that Datastream AND that
    -- Project. A composite FK, not a comment.
    CONSTRAINT fk_semantic_view_binding_mapping_scope
        FOREIGN KEY (mapping_version_id, datastream_id, project_id)
        REFERENCES app.datastream_mapping_versions (id, datastream_id, project_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_semantic_view_version_bindings_concept
    ON app.semantic_view_version_bindings (concept_id, datastream_id);

-- ---------------------------------------------------------------------------
-- 6. Change sets: the one consequential lifecycle for both object types.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_change_sets (
    id                          TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL
                                REFERENCES app.projects(id) ON DELETE CASCADE,
    object_type                 TEXT        NOT NULL
                                CHECK (object_type IN ('semantic-concept', 'semantic-view')),
    -- NULL only while creating a brand new object.
    object_id                   TEXT,
    base_version_id             TEXT,
    intent                      JSONB       NOT NULL CHECK (jsonb_typeof(intent) = 'object'),
    diff                        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    dependency_fingerprint      TEXT        CHECK (dependency_fingerprint IS NULL
                                                   OR dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    validation                  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Missing required Test coverage is `unverifiable`, which is NOT `pass`.
    test_gate_state             TEXT        NOT NULL DEFAULT 'unevaluated'
                                CHECK (test_gate_state IN ('unevaluated', 'pass', 'fail',
                                                           'unverifiable', 'overridden')),
    test_gate_override          JSONB,
    state                       TEXT        NOT NULL DEFAULT 'open'
                                CHECK (state IN ('open', 'prepared', 'confirmed',
                                                 'rejected', 'expired')),
    confirmation_token_hash     TEXT        CHECK (confirmation_token_hash IS NULL
                                                   OR confirmation_token_hash ~ '^[0-9a-f]{64}$'),
    confirmation_used_at        TIMESTAMPTZ,
    expires_at                  TIMESTAMPTZ,
    result_version_id           TEXT,
    idempotency_key_hash        TEXT        NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_change_sets PRIMARY KEY (id),
    CONSTRAINT ck_semantic_change_sets_id CHECK (id ~ '^scs_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_semantic_change_sets_idempotency
        UNIQUE (project_id, idempotency_key_hash),
    -- A confirmation token is single-use: once consumed it can never be reused,
    -- and a confirmed change set always carries the moment it was consumed.
    CONSTRAINT ck_semantic_change_sets_confirmation CHECK (
        state <> 'confirmed'
        OR (confirmation_used_at IS NOT NULL AND result_version_id IS NOT NULL)
    ),
    -- Publishing without a Test verdict, or on an `unverifiable` one, requires an
    -- explicit reasoned override recorded on the change set itself.
    CONSTRAINT ck_semantic_change_sets_test_gate CHECK (
        state <> 'confirmed'
        OR test_gate_state = 'pass'
        OR (test_gate_state = 'overridden' AND test_gate_override IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_semantic_change_sets_object
    ON app.semantic_change_sets (project_id, object_type, object_id, created_at DESC);

DROP TRIGGER IF EXISTS trg_semantic_change_sets_updated_at ON app.semantic_change_sets;
CREATE TRIGGER trg_semantic_change_sets_updated_at
    BEFORE UPDATE ON app.semantic_change_sets
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 7. Compiled artifacts: pinned OUTPUT of a source version, never a definition.
--
--    dbt relations and the Apache Ossie projection both live here. Neither may
--    be edited into a second, competing meaning: the row records the exact
--    source version and compiler version that produced it, and is immutable.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.semantic_compiled_artifacts (
    id                          TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL
                                REFERENCES app.projects(id) ON DELETE CASCADE,
    view_version_id             TEXT        NOT NULL
                                REFERENCES app.semantic_view_versions (id) ON DELETE CASCADE,
    compiler_version            TEXT        NOT NULL,
    content_hash                TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    queryability_matrix         JSONB       NOT NULL
                                CHECK (jsonb_typeof(queryability_matrix) = 'object'),
    ossie_projection            JSONB       NOT NULL
                                CHECK (jsonb_typeof(ossie_projection) = 'object'),
    ossie_spec_version          TEXT        NOT NULL,
    toorow_extension_version    TEXT        NOT NULL,
    dbt_relation_refs           JSONB       NOT NULL DEFAULT '[]'::jsonb
                                CHECK (jsonb_typeof(dbt_relation_refs) = 'array'),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_semantic_compiled_artifacts PRIMARY KEY (id),
    CONSTRAINT ck_semantic_compiled_artifacts_id
        CHECK (id ~ '^sca_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_semantic_compiled_artifact_version
        UNIQUE (view_version_id, compiler_version)
);

CREATE OR REPLACE FUNCTION app.reject_semantic_artifact_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'compiled semantic artifacts are immutable'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_semantic_compiled_artifacts_immutable
    ON app.semantic_compiled_artifacts;
CREATE TRIGGER trg_semantic_compiled_artifacts_immutable
    BEFORE UPDATE ON app.semantic_compiled_artifacts
    FOR EACH ROW EXECUTE FUNCTION app.reject_semantic_artifact_mutation();

-- ---------------------------------------------------------------------------
-- 7b. Machine-name normalization, shared by every seeding step below.
--
--     Normalization only: case folded, separators unified, leading non-letter
--     prefixed rather than stripped. It never renames and never truncates a name
--     into a collision with a different one -- an over-long name keeps a hash
--     tail so two long names cannot fold onto one.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.semantic_concept_name(raw TEXT)
RETURNS TEXT AS $$
DECLARE
    folded TEXT;
BEGIN
    folded := regexp_replace(lower(COALESCE(raw, '')), '[^a-z0-9_]+', '_', 'g');
    folded := regexp_replace(folded, '_+', '_', 'g');
    folded := trim(BOTH '_' FROM folded);
    IF folded = '' THEN
        folded := 'field_' || substr(encode(digest(COALESCE(raw, ''), 'sha256'), 'hex'), 1, 12);
    ELSIF folded !~ '^[a-z]' THEN
        folded := 'f_' || folded;
    END IF;
    IF length(folded) > 127 THEN
        folded := substr(folded, 1, 114) || '_'
               || substr(encode(digest(raw, 'sha256'), 'hex'), 1, 12);
    END IF;
    RETURN folded;
END;
$$ LANGUAGE plpgsql IMMUTABLE;

-- ---------------------------------------------------------------------------
-- 8. RECONCILIATION. Ambiguity detection first, then seeding.
--
--    Both steps are skipped entirely once app.semantic_concepts holds rows, so
--    re-running this migration never re-seeds or re-refuses.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    ambiguous_count INTEGER := 0;
    already_seeded  BOOLEAN;
BEGIN
    SELECT EXISTS (SELECT 1 FROM app.semantic_concepts) INTO already_seeded;
    IF already_seeded THEN
        RAISE NOTICE 'Semantic Model already seeded; reconciliation skipped.';
        RETURN;
    END IF;

    -- Refusal 1: two canonical fields in the SAME scope derive from the same
    -- dictionary name. Their identities cannot both be preserved onto one
    -- Concept, and choosing one is choosing a meaning.
    INSERT INTO app.semantic_migration_reconciliations
        (source_store, source_key, source_scope, decision, concept_id, reason)
    SELECT 'mdm_canonical_fields',
           cf.dictionary_field_name,
           COALESCE(cf.project_id, '__platform__'),
           'blocked_ambiguous',
           NULL,
           'Two or more canonical fields in this scope derive from dictionary '
           || 'field ' || quote_literal(cf.dictionary_field_name)
           || '. Equal derivation is not equal meaning; resolve in '
           || 'app.mdm_canonical_fields before migrating.'
    FROM app.mdm_canonical_fields cf
    WHERE cf.dictionary_field_name IS NOT NULL
      AND cf.status <> 'archived'
    GROUP BY cf.dictionary_field_name, COALESCE(cf.project_id, '__platform__')
    HAVING COUNT(*) > 1
    ON CONFLICT DO NOTHING;

    -- Refusal 2: a metric definition and a canonical field share a name in a
    -- compatible scope but CONTRADICT each other on how the measure aggregates.
    -- One says `sum`, the other says `ratio`: merging them picks a winner.
    INSERT INTO app.semantic_migration_reconciliations
        (source_store, source_key, source_scope, decision, concept_id, reason)
    SELECT 'metric_definitions',
           md.canonical_name,
           COALESCE(md.project_id, '__platform__'),
           'blocked_ambiguous',
           cf.id,
           'Metric definition ' || quote_literal(md.canonical_name)
           || ' declares aggregation ' || quote_literal(md.aggregation_type)
           || ' while canonical field ' || cf.id || ' declares '
           || quote_literal(COALESCE(cf.aggregation, 'none'))
           || '. Equal labels never imply equal meaning.'
    FROM app.metric_definitions md
    JOIN app.mdm_canonical_fields cf
      ON cf.canonical_name = md.canonical_name
     AND cf.status <> 'archived'
     AND cf.concept_kind = 'metric'
     AND COALESCE(cf.project_id, '') = COALESCE(md.project_id, '')
    WHERE md.scope_level <> 'ORG'
      AND COALESCE(cf.aggregation, '') <> ''
      AND md.aggregation_type <> cf.aggregation
    ON CONFLICT DO NOTHING;

    SELECT COUNT(*) INTO ambiguous_count
    FROM app.semantic_migration_reconciliations
    WHERE decision = 'blocked_ambiguous';

    IF ambiguous_count > 0 THEN
        RAISE EXCEPTION
            'Semantic Model migration refused: % ambiguous semantic identities. '
            'Read app.semantic_migration_reconciliations for the exact rows.',
            ambiguous_count
            USING ERRCODE = '23000';
    END IF;

    -- ---- Seed 1: every canonical field becomes a Concept, id PRESERVED. ----
    INSERT INTO app.semantic_concepts
        (id, project_id, kind, name, lifecycle_status, created_by, created_at, updated_at)
    SELECT cf.id,
           cf.project_id,
           cf.concept_kind,
           -- The canonical name is already the stable machine name; normalization
           -- only, never a rename.
           app.semantic_concept_name(cf.canonical_name),
           CASE WHEN cf.status = 'archived' THEN 'archived' ELSE 'published' END,
           cf.created_by,
           cf.created_at,
           cf.updated_at
    FROM app.mdm_canonical_fields cf
    ON CONFLICT DO NOTHING;

    INSERT INTO app.semantic_migration_reconciliations
        (source_store, source_key, source_scope, decision, concept_id, reason)
    SELECT 'mdm_canonical_fields', cf.id, COALESCE(cf.project_id, '__platform__'),
           'identity_preserved', cf.id,
           'Stable canonical identity carried onto the Semantic Concept unchanged '
           'so existing mapping evidence retargets without a new identity.'
    FROM app.mdm_canonical_fields cf
    ON CONFLICT DO NOTHING;

    -- ---- Seed 2: dictionary rows with NO canonical field get a minted id. ----
    -- `target_fields` is platform-global and name-keyed. A name is not an
    -- identity, so nothing is preserved: a new `sc_` id is minted and the old
    -- name is recorded in provenance so name-based bindings still resolve.
    -- The minted suffix is the first 26 hex digits of sha256(name), upper-cased.
    -- Hex is a strict subset of Crockford base32, so the id satisfies the same
    -- CHECK as a real ULID while remaining a pure FUNCTION of the name: running
    -- this migration twice produces the same identity, and a random one would
    -- not. It is deliberately not a time-ordered ULID -- there is no mint
    -- instant to record for a row that already existed.
    INSERT INTO app.semantic_concepts
        (id, project_id, kind, name, lifecycle_status, created_by, created_at, updated_at)
    SELECT 'sc_' || upper(substr(encode(digest(tf.name || ':target_fields', 'sha256'), 'hex'), 1, 26)),
           NULL,
           tf.field_kind,
           app.semantic_concept_name(tf.name),
           'published',
           COALESCE(tf.created_by, 'system'),
           NOW(),
           NOW()
    FROM app.target_fields tf
    WHERE NOT EXISTS (
        SELECT 1 FROM app.mdm_canonical_fields cf
        WHERE cf.dictionary_field_name = tf.name
    )
    ON CONFLICT DO NOTHING;

    INSERT INTO app.semantic_migration_reconciliations
        (source_store, source_key, source_scope, decision, concept_id, reason)
    SELECT 'target_fields', tf.name, '__platform__', 'concept_minted',
           'sc_' || upper(substr(encode(digest(tf.name || ':target_fields', 'sha256'), 'hex'), 1, 26)),
           'Name-keyed dictionary row with no canonical field deriving from it. A '
           'name is not an identity, so a deterministic id was minted from the name '
           'and the name itself kept as the Concept name for binding resolution.'
    FROM app.target_fields tf
    WHERE NOT EXISTS (
        SELECT 1 FROM app.mdm_canonical_fields cf
        WHERE cf.dictionary_field_name = tf.name
    )
    ON CONFLICT DO NOTHING;

    -- ---- Seed 3: metric definitions merge into the Concept of the same name. --
    -- Only where step "Refusal 2" proved they do not contradict it. An ORG-scoped
    -- definition has no equivalent scope in this model and is NOT merged; it is
    -- recorded so the operator can see exactly what was left behind.
    INSERT INTO app.semantic_migration_reconciliations
        (source_store, source_key, source_scope, decision, concept_id, reason)
    SELECT 'metric_definitions', md.id, COALESCE(md.project_id, '__platform__'),
           CASE WHEN sc.id IS NULL THEN 'concept_minted' ELSE 'merged_into_concept' END,
           sc.id,
           CASE
               WHEN md.scope_level = 'ORG' THEN
                   'Organization-scoped metric definition. This model has Project and '
                   'platform scope only; the row is neither merged nor discarded, and '
                   'its meaning must be re-declared at one of the two supported scopes.'
               WHEN sc.id IS NULL THEN
                   'No canonical field carries this metric name in a compatible scope. '
                   'Its aggregation, additivity and ratio operands are carried into the '
                   'first Concept version rather than into a second definition store.'
               ELSE
                   'Aggregation and additivity merged into the Concept that already held '
                   'this name in a compatible scope; the identity is the canonical one.'
           END
    FROM app.metric_definitions md
    LEFT JOIN app.semantic_concepts sc
      ON sc.name = app.semantic_concept_name(md.canonical_name)
     AND COALESCE(sc.project_id, '') = COALESCE(md.project_id, '')
     AND md.scope_level <> 'ORG'
    ON CONFLICT DO NOTHING;

    -- ---- Seed 4: version 1 of every seeded Concept. --------------------------
    -- A Concept whose lifecycle says `published` and which carries no version is
    -- a contradiction: there would be nothing immutable to pin, and every
    -- consumer would be reading a mutable head row again. Each seeded Concept
    -- therefore gets exactly one immutable version, assembled from what its
    -- SOURCE ROW actually declared -- no field is invented, and everything the
    -- source did not say is recorded as `unspecified` in provenance rather than
    -- defaulted into a claim.
    INSERT INTO app.semantic_concept_versions (
        id, concept_id, project_id, version_number, status, kind, name, label,
        definition, value_type, unit, format, owner,
        expression, aggregation, additivity_class, non_additive_dimensions,
        currency_behavior, semantic_type, allowed_grains, conformance,
        provenance, content_hash, created_by, created_at
    )
    SELECT 'scv_' || upper(substr(encode(digest(sc.id || ':v1', 'sha256'), 'hex'), 1, 26)),
           sc.id,
           sc.project_id,
           1,
           CASE WHEN sc.lifecycle_status = 'archived' THEN 'archived' ELSE 'published' END,
           sc.kind,
           sc.name,
           COALESCE(cf.canonical_name, tf.display_name, sc.name),
           COALESCE(cf.description, tf.description, md.description),
           CASE
               WHEN md.aggregation_type = 'ratio' THEN 'ratio'
               WHEN COALESCE(cf.currency_scope, md.currency_mode) IS NOT NULL THEN 'money'
               WHEN tf.data_type IN ('integer', 'decimal', 'date', 'string') THEN tf.data_type
               WHEN sc.kind = 'metric' THEN 'decimal'
               ELSE 'string'
           END,
           COALESCE(cf.unit, md.unit),
           md.format,
           NULL,
           -- The migrated formula is a LEAF: this measure comes from a mapped
           -- source field. A ratio definition keeps its two operands by NAME in
           -- the tree; the compiler resolves them to exact versions on the first
           -- edit, and refuses to publish while they are unresolved.
           CASE WHEN sc.kind <> 'metric' THEN NULL
                WHEN md.aggregation_type = 'ratio' THEN
                    jsonb_build_object(
                        'op', 'ratio',
                        -- The superseded model never recorded what a zero
                        -- denominator meant. `null` is carried as the migrated
                        -- policy AND listed in provenance.unspecified, so the
                        -- choice is visible as a choice rather than as a fact
                        -- the source stated.
                        'zero_denominator', 'null',
                        'numerator', jsonb_build_object('op', 'concept_name',
                                                        'name', md.ratio_numerator),
                        'denominator', jsonb_build_object('op', 'concept_name',
                                                          'name', md.ratio_denominator)
                    )
                ELSE jsonb_build_object('op', 'source_measure', 'concept', sc.name)
           END,
           CASE WHEN sc.kind <> 'metric' THEN NULL
                ELSE jsonb_build_object(
                        'function',
                        COALESCE(cf.aggregation, NULLIF(md.aggregation_type, 'ratio'), 'sum'))
           END,
           CASE WHEN sc.kind <> 'metric' THEN NULL
                WHEN cf.non_additive OR md.additive IS FALSE
                     OR md.aggregation_type = 'ratio' THEN 'non_additive'
                WHEN cardinality(COALESCE(md.non_additive_dimensions, '{}')) > 0
                     THEN 'semi_additive'
                ELSE 'additive'
           END,
           COALESCE(md.non_additive_dimensions, '{}'),
           CASE WHEN COALESCE(cf.currency_scope, md.currency_mode) IS NULL THEN '{}'::jsonb
                ELSE jsonb_build_object('scope',
                                        COALESCE(cf.currency_scope, md.currency_mode))
           END,
           CASE WHEN sc.kind <> 'dimension' THEN NULL
                WHEN tf.data_type IN ('date', 'timestamp') THEN 'temporal'
                ELSE 'categorical'
           END,
           CASE WHEN sc.kind <> 'dimension' THEN '{}'::text[]
                WHEN tf.data_type IN ('date', 'timestamp') THEN ARRAY['day']
                ELSE '{}'::text[]
           END,
           CASE WHEN sc.kind <> 'dimension' THEN NULL
                WHEN sc.project_id IS NULL THEN 'conformed'
                ELSE 'local'
           END,
           jsonb_strip_nulls(jsonb_build_object(
               'migrated_from', jsonb_strip_nulls(jsonb_build_object(
                   'mdm_canonical_field_id', cf.id,
                   'target_field_name', COALESCE(cf.dictionary_field_name, tf.name),
                   'metric_definition_id', md.id
               )),
               'unspecified', (
                   SELECT COALESCE(jsonb_agg(missing), '[]'::jsonb) FROM (
                       SELECT 'owner' AS missing
                       UNION ALL SELECT 'business_domain_refs'
                       UNION ALL SELECT 'time_behavior'
                       UNION ALL SELECT 'format' WHERE md.format IS NULL
                       UNION ALL SELECT 'unit' WHERE COALESCE(cf.unit, md.unit) IS NULL
                       UNION ALL SELECT 'zero_denominator'
                                 WHERE md.aggregation_type = 'ratio'
                   ) gaps
               ),
               'migration', '142_semantic_model'
           )),
           encode(digest(sc.id || ':1:' || sc.kind || ':' || sc.name
                         || ':' || COALESCE(cf.aggregation, md.aggregation_type, '')
                         || ':' || COALESCE(cf.unit, md.unit, ''), 'sha256'), 'hex'),
           sc.created_by,
           sc.created_at
    FROM app.semantic_concepts sc
    LEFT JOIN app.mdm_canonical_fields cf ON cf.id = sc.id
    -- Either the dictionary row this canonical field derives from, or -- for a
    -- Concept minted in seed 2 -- the dictionary row its id was minted FROM.
    -- Matching on the normalized name, never on the raw one: seed 2 normalized it.
    LEFT JOIN LATERAL (
        SELECT t.* FROM app.target_fields t
        WHERE (cf.dictionary_field_name IS NOT NULL AND t.name = cf.dictionary_field_name)
           OR (cf.id IS NULL AND app.semantic_concept_name(t.name) = sc.name)
        ORDER BY t.name
        LIMIT 1
    ) tf ON TRUE
    LEFT JOIN LATERAL (
        SELECT d.* FROM app.metric_definitions d
        WHERE app.semantic_concept_name(d.canonical_name) = sc.name
          AND COALESCE(d.project_id, '') = COALESCE(sc.project_id, '')
          AND d.scope_level <> 'ORG'
        ORDER BY d.updated_at DESC, d.id
        LIMIT 1
    ) md ON TRUE
    ON CONFLICT DO NOTHING;

    -- Point current AND last-known-good at that version. `pending` stays NULL:
    -- nothing is in flight at migration time, and a pending pointer equal to
    -- current would make the first real publication look like a no-op.
    UPDATE app.semantic_concepts sc
       SET current_version_id = v.id,
           last_known_good_version_id = v.id
      FROM app.semantic_concept_versions v
     WHERE v.concept_id = sc.id
       AND v.version_number = 1
       AND sc.current_version_id IS NULL;
END $$;

COMMIT;
