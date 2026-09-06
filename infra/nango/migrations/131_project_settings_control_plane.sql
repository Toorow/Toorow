-- Story 46.3: Project Settings capability control plane.
-- Postgres owns Project intent, immutable configuration versions and Change Sets.

BEGIN;

ALTER TABLE app.projects ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS canonical_currency_origin TEXT NOT NULL DEFAULT 'suggestion';
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS canonical_currency_confirmation_status TEXT NOT NULL DEFAULT 'unconfirmed';
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS reporting_timezone_origin TEXT NOT NULL DEFAULT 'suggestion';
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS reporting_timezone_confirmation_status TEXT NOT NULL DEFAULT 'unconfirmed';
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS verification_source_origin TEXT NOT NULL DEFAULT 'unset';
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS verification_source_confirmation_status TEXT NOT NULL DEFAULT 'unconfirmed';

ALTER TABLE app.project_preferences DROP CONSTRAINT IF EXISTS chk_project_preferences_currency_confirmation;
ALTER TABLE app.project_preferences ADD CONSTRAINT chk_project_preferences_currency_confirmation
    CHECK (canonical_currency_confirmation_status IN ('unconfirmed', 'confirmed', 'pending'));
ALTER TABLE app.project_preferences DROP CONSTRAINT IF EXISTS chk_project_preferences_timezone_confirmation;
ALTER TABLE app.project_preferences ADD CONSTRAINT chk_project_preferences_timezone_confirmation
    CHECK (reporting_timezone_confirmation_status IN ('unconfirmed', 'confirmed', 'pending'));
ALTER TABLE app.project_preferences DROP CONSTRAINT IF EXISTS chk_project_preferences_verification_confirmation;
ALTER TABLE app.project_preferences ADD CONSTRAINT chk_project_preferences_verification_confirmation
    CHECK (verification_source_confirmation_status IN ('unconfirmed', 'confirmed', 'pending'));

CREATE TABLE IF NOT EXISTS app.project_configuration_versions (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    version_number INTEGER NOT NULL CHECK (version_number > 0),
    posture JSONB NOT NULL CHECK (jsonb_typeof(posture) = 'object'),
    dependency_fingerprint TEXT NOT NULL CHECK (dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    previous_version_id TEXT,
    activated_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, version_number),
    UNIQUE (project_id, id),
    UNIQUE (project_id, content_hash),
    FOREIGN KEY (project_id, previous_version_id)
        REFERENCES app.project_configuration_versions(project_id, id) ON DELETE RESTRICT
);

ALTER TABLE app.projects ADD COLUMN IF NOT EXISTS active_configuration_version_id TEXT;
ALTER TABLE app.projects DROP CONSTRAINT IF EXISTS fk_projects_active_configuration;
ALTER TABLE app.projects ADD CONSTRAINT fk_projects_active_configuration
    FOREIGN KEY (id, active_configuration_version_id)
    REFERENCES app.project_configuration_versions(project_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE TABLE IF NOT EXISTS app.project_change_sets (
    id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    state TEXT NOT NULL DEFAULT 'draft'
        CHECK (state IN ('draft', 'prepared', 'confirmed', 'blocked', 'stale', 'failed')),
    intent JSONB NOT NULL CHECK (jsonb_typeof(intent) = 'object'),
    owner_references JSONB NOT NULL DEFAULT '[]'::jsonb CHECK (jsonb_typeof(owner_references) = 'array'),
    prepared_payload JSONB CHECK (prepared_payload IS NULL OR jsonb_typeof(prepared_payload) = 'object'),
    prepared_payload_hash TEXT CHECK (prepared_payload_hash IS NULL OR prepared_payload_hash ~ '^[0-9a-f]{64}$'),
    dependency_fingerprint TEXT CHECK (dependency_fingerprint IS NULL OR dependency_fingerprint ~ '^[0-9a-f]{64}$'),
    rollback_version_id TEXT,
    activated_version_id TEXT,
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    prepared_at TIMESTAMPTZ,
    confirmed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (project_id, id),
    UNIQUE (project_id, idempotency_key_hash),
    CHECK ((state = 'draft' AND prepared_payload IS NULL AND prepared_payload_hash IS NULL)
        OR (state <> 'draft' AND prepared_payload IS NOT NULL AND prepared_payload_hash IS NOT NULL
            AND dependency_fingerprint IS NOT NULL AND prepared_at IS NOT NULL)),
    CHECK (state <> 'confirmed' OR (activated_version_id IS NOT NULL AND confirmed_at IS NOT NULL)),
    FOREIGN KEY (project_id, rollback_version_id)
        REFERENCES app.project_configuration_versions(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, activated_version_id)
        REFERENCES app.project_configuration_versions(project_id, id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS app.project_change_set_references (
    change_set_id TEXT NOT NULL REFERENCES app.project_change_sets(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN ('data', 'governance')),
    owner_object_type TEXT NOT NULL,
    owner_object_id TEXT NOT NULL,
    owner_version_id TEXT NOT NULL,
    owner_route TEXT NOT NULL,
    evidence_hash TEXT NOT NULL CHECK (evidence_hash ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (change_set_id, owner_kind, owner_object_type, owner_object_id, owner_version_id),
    FOREIGN KEY (project_id, change_set_id) REFERENCES app.project_change_sets(project_id, id) ON DELETE RESTRICT
);

CREATE OR REPLACE FUNCTION app.protect_project_change_set_reference()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    parent_state TEXT;
BEGIN
    SELECT state INTO parent_state
    FROM app.project_change_sets
    WHERE id = COALESCE(NEW.change_set_id, OLD.change_set_id)
      AND project_id = COALESCE(NEW.project_id, OLD.project_id);
    IF parent_state IS DISTINCT FROM 'draft' THEN
        RAISE EXCEPTION 'prepared project change set references are immutable';
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_project_change_set_references_immutable
    ON app.project_change_set_references;
CREATE TRIGGER trg_project_change_set_references_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON app.project_change_set_references
    FOR EACH ROW EXECUTE FUNCTION app.protect_project_change_set_reference();

CREATE TABLE IF NOT EXISTS app.project_capabilities (
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    capability_key TEXT NOT NULL,
    availability TEXT NOT NULL CHECK (availability IN ('always_present', 'optional')),
    state TEXT NOT NULL CHECK (state IN ('disabled', 'draft', 'ready', 'degraded', 'blocked')),
    active_version_id TEXT REFERENCES app.project_configuration_versions(id) ON DELETE RESTRICT,
    pending_change_set_id TEXT REFERENCES app.project_change_sets(id) ON DELETE RESTRICT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (project_id, capability_key),
    CHECK (capability_key IN (
        'country', 'currency_fx', 'reporting_timezone', 'tax_fees', 'competitors'
    )),
    CHECK (NOT (availability = 'always_present' AND state = 'disabled')),
    CHECK ((capability_key IN ('currency_fx', 'reporting_timezone') AND availability = 'always_present')
        OR (capability_key IN ('country', 'tax_fees', 'competitors') AND availability = 'optional')),
    FOREIGN KEY (project_id, active_version_id)
        REFERENCES app.project_configuration_versions(project_id, id) DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (project_id, pending_change_set_id)
        REFERENCES app.project_change_sets(project_id, id) DEFERRABLE INITIALLY DEFERRED
);

CREATE OR REPLACE FUNCTION app.reject_project_configuration_version_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'project configuration versions are immutable';
END;
$$;
DROP TRIGGER IF EXISTS trg_project_configuration_versions_immutable ON app.project_configuration_versions;
CREATE TRIGGER trg_project_configuration_versions_immutable
    BEFORE UPDATE OR DELETE ON app.project_configuration_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_project_configuration_version_mutation();

CREATE OR REPLACE FUNCTION app.protect_project_change_set_prepared_payload()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.state <> 'draft' AND (
        NEW.intent IS DISTINCT FROM OLD.intent
        OR NEW.owner_references IS DISTINCT FROM OLD.owner_references
        OR NEW.prepared_payload IS DISTINCT FROM OLD.prepared_payload
        OR NEW.prepared_payload_hash IS DISTINCT FROM OLD.prepared_payload_hash
        OR NEW.dependency_fingerprint IS DISTINCT FROM OLD.dependency_fingerprint
        OR NEW.rollback_version_id IS DISTINCT FROM OLD.rollback_version_id
        OR NEW.created_by IS DISTINCT FROM OLD.created_by
        OR NEW.created_at IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION 'prepared project change set is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_project_change_sets_prepared_immutable ON app.project_change_sets;
CREATE TRIGGER trg_project_change_sets_prepared_immutable
    BEFORE UPDATE ON app.project_change_sets
    FOR EACH ROW EXECUTE FUNCTION app.protect_project_change_set_prepared_payload();

CREATE OR REPLACE FUNCTION app.seed_project_capabilities(target_project_id TEXT)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO app.project_capabilities (project_id, capability_key, availability, state)
    VALUES
        (target_project_id, 'country', 'optional', 'disabled'),
        (target_project_id, 'currency_fx', 'always_present', 'draft'),
        (target_project_id, 'reporting_timezone', 'always_present', 'draft'),
        (target_project_id, 'tax_fees', 'optional', 'disabled'),
        (target_project_id, 'competitors', 'optional', 'disabled')
    ON CONFLICT (project_id, capability_key) DO NOTHING;
END;
$$;
SELECT app.seed_project_capabilities(id) FROM app.projects;

CREATE OR REPLACE FUNCTION app.seed_project_capabilities_after_insert()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM app.seed_project_capabilities(NEW.id);
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_projects_seed_capabilities ON app.projects;
CREATE TRIGGER trg_projects_seed_capabilities
    AFTER INSERT ON app.projects FOR EACH ROW EXECUTE FUNCTION app.seed_project_capabilities_after_insert();

ALTER TABLE app.entry_confirmations DROP CONSTRAINT IF EXISTS entry_confirmations_command_type_check;
ALTER TABLE app.entry_confirmations ADD CONSTRAINT entry_confirmations_command_type_check
    CHECK (command_type IN (
        'hosted.entry_scope.create', 'instance.claim', 'project.settings.activate'
    ));

-- Existing non-null legacy values are not proof of confirmation.
ALTER TABLE app.projects DROP COLUMN IF EXISTS currency;
ALTER TABLE app.projects DROP COLUMN IF EXISTS timezone;

COMMIT;
