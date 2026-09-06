-- Story 47.1: stable Data identities and immutable version evidence.
-- Additive after Story 46 migrations 131 and 132. No provider vocabulary.

BEGIN;

ALTER TABLE app.credential_accounts
    ADD COLUMN IF NOT EXISTS source_account_id TEXT;

UPDATE app.credential_accounts
SET source_account_id = 'sacct_' || md5(credential_id || ':' || external_account_id)
WHERE source_account_id IS NULL;

ALTER TABLE app.credential_accounts
    ALTER COLUMN source_account_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_credential_accounts_source_account_id
    ON app.credential_accounts (source_account_id);

-- The unique index above already exists under this name, so ADD CONSTRAINT ...
-- UNIQUE (...) would raise 42P07 rather than adopt it. Promote the index in
-- place: later migrations reference source_account_id as a foreign key target.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'uq_credential_accounts_source_account_id'
          AND conrelid = 'app.credential_accounts'::regclass
    ) THEN
        ALTER TABLE app.credential_accounts
            ADD CONSTRAINT uq_credential_accounts_source_account_id
            UNIQUE USING INDEX uq_credential_accounts_source_account_id;
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS app.connector_contract_versions (
    id                       TEXT        NOT NULL,
    installation_id          TEXT        NOT NULL
                             REFERENCES app.connector_installations(id) ON DELETE RESTRICT,
    environment              TEXT        NOT NULL,
    connector_id             TEXT        NOT NULL,
    version_number           INTEGER     NOT NULL CHECK (version_number >= 1),
    contract_schema_version  TEXT        NOT NULL,
    connector_fingerprint    TEXT        NOT NULL CHECK (connector_fingerprint ~ '^[0-9a-f]{64}$'),
    contract_snapshot        JSONB       NOT NULL CHECK (jsonb_typeof(contract_snapshot) = 'object'),
    validation_evidence      JSONB       NOT NULL CHECK (jsonb_typeof(validation_evidence) = 'object'),
    verification_run_id      TEXT REFERENCES app.connector_verification_runs(id) ON DELETE RESTRICT,
    created_by               TEXT        NOT NULL,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_connector_contract_versions PRIMARY KEY (id),
    CONSTRAINT ck_connector_contract_versions_id
        CHECK (id ~ '^ccv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_connector_contract_version_number
        UNIQUE (installation_id, version_number),
    CONSTRAINT uq_connector_contract_fingerprint
        UNIQUE (installation_id, connector_fingerprint),
    CONSTRAINT uq_connector_contract_version_scope
        UNIQUE (id, connector_id, connector_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_connector_contract_versions_connector
    ON app.connector_contract_versions (connector_id, environment, version_number DESC);

CREATE OR REPLACE FUNCTION app.reject_connector_contract_version_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'connector contract versions are immutable'
        USING ERRCODE = '23000';
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_contract_versions_immutable
    ON app.connector_contract_versions;
CREATE TRIGGER trg_connector_contract_versions_immutable
    BEFORE UPDATE OR DELETE ON app.connector_contract_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_connector_contract_version_mutation();

CREATE TABLE IF NOT EXISTS app.event_configurations (
    id                 TEXT        NOT NULL,
    datastream_id      TEXT        NOT NULL,
    project_id         TEXT        NOT NULL,
    org_id             TEXT        NOT NULL,
    name               TEXT        NOT NULL,
    lifecycle_state    TEXT        NOT NULL DEFAULT 'draft'
                       CHECK (lifecycle_state IN ('draft', 'active', 'paused', 'archived')),
    active_version_id  TEXT,
    created_by         TEXT        NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_event_configurations PRIMARY KEY (id),
    CONSTRAINT ck_event_configurations_id
        CHECK (id ~ '^ecfg_[0-9A-HJKMNP-TV-Z]{26}$' OR id ~ '^ecfg_legacy_[0-9a-f]{32}$'),
    CONSTRAINT uq_event_configuration_scope UNIQUE (id, datastream_id, project_id),
    CONSTRAINT uq_event_configuration_name UNIQUE (datastream_id, name),
    CONSTRAINT fk_event_configuration_datastream
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_event_configuration_org
        FOREIGN KEY (org_id) REFERENCES app.organizations(id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS app.event_configuration_versions (
    id                             TEXT        NOT NULL,
    event_configuration_id         TEXT        NOT NULL,
    datastream_id                  TEXT        NOT NULL,
    project_id                     TEXT        NOT NULL,
    version_number                 INTEGER     NOT NULL CHECK (version_number >= 1),
    connector_contract_version_id  TEXT,
    connector_fingerprint          TEXT CHECK (
        connector_fingerprint IS NULL OR connector_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    source_mapping                 JSONB       NOT NULL CHECK (jsonb_typeof(source_mapping) = 'object'),
    collection_policy              JSONB       NOT NULL CHECK (jsonb_typeof(collection_policy) = 'object'),
    normalized_payload_hash        TEXT        NOT NULL CHECK (normalized_payload_hash ~ '^[0-9a-f]{64}$'),
    review_state                   TEXT        NOT NULL DEFAULT 'draft'
                                   CHECK (review_state IN ('draft', 'confirmed', 'active', 'superseded')),
    confirmation_id                TEXT,
    operation_id                   TEXT REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_by                     TEXT        NOT NULL,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_event_configuration_versions PRIMARY KEY (id),
    CONSTRAINT ck_event_configuration_versions_id
        CHECK (id ~ '^ecv_[0-9A-HJKMNP-TV-Z]{26}$' OR id ~ '^ecv_legacy_[0-9a-f]{32}$'),
    CONSTRAINT uq_event_configuration_version UNIQUE (event_configuration_id, version_number),
    CONSTRAINT uq_event_configuration_version_scope UNIQUE (id, datastream_id, project_id),
    CONSTRAINT fk_event_configuration_version_parent
        FOREIGN KEY (event_configuration_id, datastream_id, project_id)
        REFERENCES app.event_configurations(id, datastream_id, project_id) ON DELETE RESTRICT,
    CONSTRAINT fk_event_configuration_version_connector
        FOREIGN KEY (connector_contract_version_id)
        REFERENCES app.connector_contract_versions(id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_event_configurations_project
    ON app.event_configurations(project_id, lifecycle_state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_event_configuration_versions_parent
    ON app.event_configuration_versions(event_configuration_id, version_number DESC);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_event_configuration_active_version'
          AND conrelid = 'app.event_configurations'::regclass
    ) THEN
        ALTER TABLE app.event_configurations
            ADD CONSTRAINT fk_event_configuration_active_version
            FOREIGN KEY (active_version_id, datastream_id, project_id)
            REFERENCES app.event_configuration_versions(id, datastream_id, project_id)
            DEFERRABLE INITIALLY IMMEDIATE;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.protect_event_configuration_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'event configuration versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.event_configuration_id IS DISTINCT FROM OLD.event_configuration_id
       OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.version_number IS DISTINCT FROM OLD.version_number
       OR NEW.connector_contract_version_id IS DISTINCT FROM OLD.connector_contract_version_id
       OR NEW.connector_fingerprint IS DISTINCT FROM OLD.connector_fingerprint
       OR NEW.source_mapping IS DISTINCT FROM OLD.source_mapping
       OR NEW.collection_policy IS DISTINCT FROM OLD.collection_policy
       OR NEW.normalized_payload_hash IS DISTINCT FROM OLD.normalized_payload_hash
       OR NEW.created_by IS DISTINCT FROM OLD.created_by
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'event configuration version evidence is immutable'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_event_configuration_versions_immutable
    ON app.event_configuration_versions;
CREATE TRIGGER trg_event_configuration_versions_immutable
    BEFORE UPDATE OR DELETE ON app.event_configuration_versions
    FOR EACH ROW EXECUTE FUNCTION app.protect_event_configuration_version();

ALTER TABLE app.context_events
    ADD COLUMN IF NOT EXISTS event_configuration_version_id TEXT,
    ADD COLUMN IF NOT EXISTS datastream_id TEXT,
    ADD COLUMN IF NOT EXISTS execution_id TEXT,
    ADD COLUMN IF NOT EXISTS binding_state TEXT NOT NULL DEFAULT 'unavailable'
        CHECK (binding_state IN ('linked', 'unavailable'));

-- Deterministic legacy binding only where exactly one owning Datastream can be proven.
WITH candidates AS (
    SELECT e.id AS event_id, MIN(d.id) AS datastream_id, COUNT(*) AS candidate_count
    FROM app.context_events e
    JOIN app.datastreams d ON d.project_id = e.project_id
      AND (
        (e.platform IS NOT NULL AND d.module_name = e.platform)
        OR (e.source <> 'manual' AND d.module_name = e.source)
        OR (e.platform IS NULL AND e.source = 'manual')
      )
    WHERE e.event_configuration_version_id IS NULL
    GROUP BY e.id
), targets AS (
    SELECT e.id AS event_id, e.project_id, e.type, e.platform, e.source,
           c.datastream_id, d.org_id
    FROM app.context_events e
    JOIN candidates c ON c.event_id = e.id AND c.candidate_count = 1
    JOIN app.datastreams d ON d.id = c.datastream_id AND d.project_id = e.project_id
), configs AS (
    SELECT DISTINCT
        'ecfg_legacy_' || md5(project_id || ':' || datastream_id || ':' || type || ':' || COALESCE(platform, '') || ':' || source) AS id,
        datastream_id, project_id, org_id,
        'Legacy ' || type || ' observations' AS name
    FROM targets
)
INSERT INTO app.event_configurations
    (id, datastream_id, project_id, org_id, name, lifecycle_state, created_by)
SELECT id, datastream_id, project_id, org_id, name, 'active', 'migration-133'
FROM configs
ON CONFLICT (id) DO NOTHING;

WITH targets AS (
    SELECT e.id AS event_id, e.project_id, e.type, e.platform, e.source,
           d.id AS datastream_id,
           'ecfg_legacy_' || md5(e.project_id || ':' || d.id || ':' || e.type || ':' || COALESCE(e.platform, '') || ':' || e.source) AS config_id
    FROM app.context_events e
    JOIN app.datastreams d ON d.project_id = e.project_id
      AND (
        (e.platform IS NOT NULL AND d.module_name = e.platform)
        OR (e.source <> 'manual' AND d.module_name = e.source)
        OR (e.platform IS NULL AND e.source = 'manual')
      )
    WHERE e.event_configuration_version_id IS NULL
      AND 1 = (SELECT COUNT(*) FROM app.datastreams only_d
               WHERE only_d.project_id = e.project_id
                 AND ((e.platform IS NOT NULL AND only_d.module_name = e.platform)
                   OR (e.source <> 'manual' AND only_d.module_name = e.source)
                   OR (e.platform IS NULL AND e.source = 'manual')))
), versions AS (
    SELECT DISTINCT
        'ecv_legacy_' || md5(config_id) AS id,
        config_id, datastream_id, project_id,
        jsonb_build_object('legacy_source', source, 'legacy_platform', platform, 'event_type', type) AS source_mapping,
        jsonb_build_object('legacy_observation', true) AS collection_policy
    FROM targets
)
INSERT INTO app.event_configuration_versions
    (id, event_configuration_id, datastream_id, project_id, version_number,
     source_mapping, collection_policy, normalized_payload_hash, review_state, created_by)
SELECT id, config_id, datastream_id, project_id, 1,
       source_mapping, collection_policy,
       md5(source_mapping::text || ':' || collection_policy::text) || md5(config_id),
       'active', 'migration-133'
FROM versions
ON CONFLICT (id) DO NOTHING;

UPDATE app.event_configurations c
SET active_version_id = v.id, updated_at = NOW()
FROM app.event_configuration_versions v
WHERE v.event_configuration_id = c.id
  AND c.active_version_id IS NULL;

WITH candidates AS (
    SELECT e.id AS event_id, MIN(d.id) AS datastream_id, COUNT(*) AS candidate_count
    FROM app.context_events e
    JOIN app.datastreams d ON d.project_id = e.project_id
      AND (
        (e.platform IS NOT NULL AND d.module_name = e.platform)
        OR (e.source <> 'manual' AND d.module_name = e.source)
        OR (e.platform IS NULL AND e.source = 'manual')
      )
    WHERE e.event_configuration_version_id IS NULL
    GROUP BY e.id
), targets AS (
    SELECT e.id AS event_id, c.datastream_id
    FROM app.context_events e
    JOIN candidates c ON c.event_id = e.id AND c.candidate_count = 1
)
UPDATE app.context_events e
SET event_configuration_version_id = v.id,
    datastream_id = v.datastream_id,
    binding_state = 'linked'
FROM targets t
JOIN app.event_configuration_versions v ON v.datastream_id = t.datastream_id
JOIN app.event_configurations c ON c.id = v.event_configuration_id
WHERE e.id = t.event_id
  AND e.event_configuration_version_id IS NULL
  AND c.project_id = e.project_id
  AND (v.source_mapping->>'event_type') = e.type
  AND COALESCE(v.source_mapping->>'legacy_platform', '') = COALESCE(e.platform, '')
  AND (v.source_mapping->>'legacy_source') = e.source;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_context_events_configuration_version'
          AND conrelid = 'app.context_events'::regclass
    ) THEN
        ALTER TABLE app.context_events
            ADD CONSTRAINT fk_context_events_configuration_version
            FOREIGN KEY (event_configuration_version_id, datastream_id, project_id)
            REFERENCES app.event_configuration_versions(id, datastream_id, project_id)
            ON DELETE RESTRICT;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.require_event_observation_binding()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.event_configuration_version_id IS NULL OR NEW.datastream_id IS NULL THEN
        RAISE EXCEPTION 'new event observations require a Datastream-owned Event Configuration version'
            USING ERRCODE = '23514';
    END IF;
    NEW.binding_state = 'linked';
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_context_events_require_binding ON app.context_events;
CREATE TRIGGER trg_context_events_require_binding
    BEFORE INSERT ON app.context_events
    FOR EACH ROW EXECUTE FUNCTION app.require_event_observation_binding();

COMMIT;
