-- Story 12.3: immutable, versioned Datastream field mappings, the MDM
-- canonical-identifier registry, and the Ossie 0.1.1 projection.
--
-- Additive and idempotent after migration 030 (the highest APPLIED migration).
-- The migration ordinal is PROVISIONAL: 031 is claimed on another branch by an
-- unmerged Story 11.1 migration (031_context_layer.sql), so this file may need to
-- be renumbered (032, or higher) whichever of {11.1, 12.3} merges second. Every
-- statement below is IF NOT EXISTS / OR REPLACE / guarded, so re-application is a
-- no-op. It creates two tables:
--   * app.datastream_mapping_versions -- append-only immutable mapping versions.
--   * app.mdm_canonical_fields        -- mutable-with-audit MDM canonical registry.
-- Registry rows are governed (Epic 13), NOT append-only; no immutability trigger
-- is attached to app.mdm_canonical_fields.

BEGIN;

ALTER TABLE app.datastreams
    ADD COLUMN IF NOT EXISTS current_mapping_version_id TEXT;

-- Referenced by composite foreign keys to guarantee project ownership.
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastreams_id_project
    ON app.datastreams (id, project_id);

CREATE TABLE IF NOT EXISTS app.datastream_mapping_versions (
    id                          TEXT        NOT NULL,
    datastream_id               TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    version_number              INTEGER     NOT NULL CHECK (version_number >= 1),
    mapping_contract_version    TEXT        NOT NULL,
    source_schema_hash          TEXT        NOT NULL CHECK (source_schema_hash ~ '^[0-9a-f]{64}$'),
    plan_version_id             TEXT        NOT NULL,
    capability_fingerprint      TEXT CHECK (
                                    capability_fingerprint IS NULL
                                    OR capability_fingerprint ~ '^[0-9a-f]{64}$'
                                ),
    content_hash                TEXT        NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    ossie_spec_version          TEXT        NOT NULL,
    toorow_extension_version    TEXT        NOT NULL,
    executable                  BOOLEAN     NOT NULL DEFAULT FALSE,
    blocking_count              INTEGER     NOT NULL DEFAULT 0 CHECK (blocking_count >= 0),
    mapping_payload             JSONB       NOT NULL CHECK (jsonb_typeof(mapping_payload) = 'object'),
    ossie_projection            JSONB       NOT NULL CHECK (jsonb_typeof(ossie_projection) = 'object'),
    idempotency_key_hash        TEXT        NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_datastream_mapping_versions PRIMARY KEY (id),
    CONSTRAINT uq_datastream_mapping_version UNIQUE (datastream_id, version_number),
    CONSTRAINT uq_datastream_mapping_idempotency UNIQUE (project_id, idempotency_key_hash),
    CONSTRAINT uq_datastream_mapping_scope UNIQUE (id, datastream_id, project_id),
    CONSTRAINT fk_datastream_mapping_datastream_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_datastream_mapping_plan_scope
        FOREIGN KEY (plan_version_id, datastream_id, project_id)
        REFERENCES app.datastream_plan_versions (id, datastream_id, project_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_datastream_mapping_versions_project
    ON app.datastream_mapping_versions (project_id, datastream_id, version_number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_datastreams_current_mapping_scope'
          AND conrelid = 'app.datastreams'::regclass
    ) THEN
        ALTER TABLE app.datastreams
            ADD CONSTRAINT fk_datastreams_current_mapping_scope
            FOREIGN KEY (current_mapping_version_id, id, project_id)
            REFERENCES app.datastream_mapping_versions (id, datastream_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.reject_datastream_mapping_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'datastream mapping versions are immutable'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_datastream_mapping_versions_immutable
    ON app.datastream_mapping_versions;
CREATE TRIGGER trg_datastream_mapping_versions_immutable
    BEFORE UPDATE OR DELETE ON app.datastream_mapping_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_datastream_mapping_version_mutation();

-- ---------------------------------------------------------------------------
-- MDM canonical-identifier registry (Story 12.3, Jean 2026-07-20).
--
-- The ULID-keyed canonical-identity layer that mapping bindings resolve against.
-- Distinct from the Story 8.5 reporting dictionary (app.target_fields): a
-- canonical id may optionally derive from a dictionary row (dictionary_field_name)
-- but the two layers are governed independently by Epic 13.
--
-- AD-5 scope: project_id NULL = platform/org-canonical, non-NULL = project-scoped,
-- exactly like app.context_topics / app.procedures. Org-scope of NULL rows is
-- provisional (a future org column may narrow NULL) and does not block 12.3.
--
-- Registry rows are MUTABLE-with-updated_at + a governance audit row (8.5
-- dictionary style), NOT append-only-versioned. Do NOT attach an immutability
-- trigger here: the append-only machine stays reserved for mapping *versions*.
-- The mapping save path is READ-ONLY against this table (Epic-13 governance
-- boundary): it resolves an mdm_target and blocks a binding whose target is
-- missing/archived/out-of-scope; it never inserts, renames, or re-types a row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.mdm_canonical_fields (
    id                      TEXT        NOT NULL,
    project_id              TEXT,                    -- NULL = platform/org-canonical (AD-5)
    concept_kind            TEXT        NOT NULL
                            CHECK (concept_kind IN ('metric', 'dimension')),
    canonical_name          TEXT        NOT NULL,
    unit                    TEXT,
    currency_scope          TEXT,
    aggregation             TEXT
                            CHECK (aggregation IN ('sum', 'average', 'min', 'max', 'count')),
    non_additive            BOOLEAN     NOT NULL DEFAULT FALSE,
    description             TEXT,
    dictionary_field_name   TEXT,
    status                  TEXT        NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'archived')),
    created_by              TEXT        NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_mdm_canonical_fields PRIMARY KEY (id),
    -- House-style ULID id (cf. dsp_/dmap_/top_/proc_). Crockford base32, 26 chars.
    CONSTRAINT ck_mdm_canonical_fields_id CHECK (id ~ '^mdm_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- A measure must never be silently non-summable: a metric concept must either
    -- declare an aggregation OR be explicitly flagged non_additive.
    CONSTRAINT ck_mdm_canonical_fields_metric_aggregation CHECK (
        concept_kind = 'dimension'
        OR aggregation IS NOT NULL
        OR non_additive = TRUE
    ),
    -- Optional derivation link to the 8.5 dictionary; RESTRICT so a governed
    -- dictionary row cannot be dropped while a canonical id derives from it.
    CONSTRAINT fk_mdm_canonical_fields_dictionary
        FOREIGN KEY (dictionary_field_name)
        REFERENCES app.target_fields (name) ON DELETE RESTRICT,
    -- Composite uniqueness enables project-scoped composite FKs from callers.
    CONSTRAINT uq_mdm_canonical_fields_id_project UNIQUE (id, project_id)
);

-- canonical_name is unique WITHIN scope (platform vs a given project) and the
-- same name may exist across scopes. Archived rows are excluded so a name can be
-- retired and re-minted. Two partial unique indexes because a NULL project_id
-- would otherwise never collide under a plain composite unique index.
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_canonical_name_platform
    ON app.mdm_canonical_fields (canonical_name)
    WHERE project_id IS NULL AND status <> 'archived';

CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_canonical_name_project
    ON app.mdm_canonical_fields (project_id, canonical_name)
    WHERE project_id IS NOT NULL AND status <> 'archived';

CREATE INDEX IF NOT EXISTS idx_mdm_canonical_fields_scope
    ON app.mdm_canonical_fields (project_id, status);

COMMIT;
