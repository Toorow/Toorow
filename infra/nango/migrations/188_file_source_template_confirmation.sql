-- Epic 22: durable human proof for each exact Template + Mapping decision.
-- A project Template is reusable across Datastreams, therefore confirmation is a
-- separate immutable relation and never a set-once column on the Template row.
BEGIN;

CREATE TABLE IF NOT EXISTS app.file_source_template_confirmations (
    operation_id          TEXT        NOT NULL PRIMARY KEY,
    template_id           TEXT        NOT NULL,
    project_id            TEXT        NOT NULL,
    datastream_id         TEXT        NOT NULL,
    mapping_version_id    TEXT        NOT NULL,
    plan_version_id       TEXT        NOT NULL,
    template_content_hash TEXT        NOT NULL
        CHECK (template_content_hash ~ '^[0-9a-f]{64}$'),
    sample_content_hash   TEXT        NOT NULL
        CHECK (sample_content_hash ~ '^[0-9a-f]{64}$'),
    sample_filename       TEXT        NOT NULL DEFAULT '',
    evidence              JSONB       NOT NULL
        CHECK (jsonb_typeof(evidence) = 'object'),
    confirmed_by          TEXT        NOT NULL CHECK (btrim(confirmed_by) <> ''),
    confirmed_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_file_source_confirmation_operation
        FOREIGN KEY (operation_id)
        REFERENCES app.operations(id) ON DELETE RESTRICT,
    CONSTRAINT fk_file_source_confirmation_template
        FOREIGN KEY (template_id)
        REFERENCES app.file_source_templates(id) ON DELETE RESTRICT,
    CONSTRAINT fk_file_source_confirmation_mapping_scope
        FOREIGN KEY (mapping_version_id, datastream_id, project_id)
        REFERENCES app.datastream_mapping_versions(id, datastream_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT uq_file_source_confirmation_template_mapping
        UNIQUE (template_id, mapping_version_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_file_source_template_project'
          AND conrelid = 'app.file_source_templates'::regclass
    ) THEN
        ALTER TABLE app.file_source_templates
            ADD CONSTRAINT fk_file_source_template_project
            FOREIGN KEY (project_id)
            REFERENCES app.projects(id) ON DELETE RESTRICT
            NOT VALID;
    END IF;
END
$$;
CREATE INDEX IF NOT EXISTS ix_file_source_confirmation_scope
    ON app.file_source_template_confirmations
        (project_id, datastream_id, mapping_version_id);

CREATE OR REPLACE FUNCTION app.validate_file_source_template_confirmation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
DECLARE
    operation_row RECORD;
    template_row RECORD;
    mapping_row RECORD;
    project_org_id TEXT;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        IF TG_OP = 'DELETE'
           AND current_setting('app.rgpd_erasure', true) = 'on'
        THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'file_source_template_confirmations is append-only';
    END IF;

    SELECT project_id, content_hash
    INTO template_row
    FROM app.file_source_templates
    WHERE id = NEW.template_id;

    SELECT project_id, datastream_id, plan_version_id
    INTO mapping_row
    FROM app.datastream_mapping_versions
    WHERE id = NEW.mapping_version_id;

    SELECT org_id INTO project_org_id FROM app.projects WHERE id = NEW.project_id;

    SELECT command_type, effective_org_id, actor, confirmation_mode, state,
           resource_path
    INTO operation_row
    FROM app.operations
    WHERE id = NEW.operation_id;

    IF template_row.project_id IS DISTINCT FROM NEW.project_id
       OR template_row.content_hash IS DISTINCT FROM NEW.template_content_hash
       OR mapping_row.project_id IS DISTINCT FROM NEW.project_id
       OR mapping_row.datastream_id IS DISTINCT FROM NEW.datastream_id
       OR mapping_row.plan_version_id IS DISTINCT FROM NEW.plan_version_id
       OR NEW.evidence->>'template_id' IS DISTINCT FROM NEW.template_id
       OR NEW.evidence->>'template_content_hash'
            IS DISTINCT FROM NEW.template_content_hash
       OR NEW.evidence->>'sample_content_hash'
            IS DISTINCT FROM NEW.sample_content_hash
       OR NEW.evidence->>'mapping_version_id'
            IS DISTINCT FROM NEW.mapping_version_id
       OR NEW.evidence->>'plan_version_id' IS DISTINCT FROM NEW.plan_version_id
       OR COALESCE(NEW.evidence->>'sample_filename', '')
            IS DISTINCT FROM NEW.sample_filename
       OR NEW.evidence->>'actor' IS DISTINCT FROM NEW.confirmed_by
    THEN
        RAISE EXCEPTION 'file-source confirmation evidence is incomplete or stale';
    END IF;

    IF operation_row.command_type
            IS DISTINCT FROM 'file_source.template.gate_confirmed'
       OR operation_row.effective_org_id IS DISTINCT FROM project_org_id
       OR operation_row.actor IS DISTINCT FROM NEW.confirmed_by
       OR operation_row.confirmation_mode IS DISTINCT FROM 'human'
       OR operation_row.state IS DISTINCT FROM 'pending'
       OR NOT (
            operation_row.resource_path
                @> jsonb_build_array('project:' || NEW.project_id)
            AND operation_row.resource_path
                @> jsonb_build_array('datastream:' || NEW.datastream_id)
            AND operation_row.resource_path
                @> jsonb_build_array('file_source_template:' || NEW.template_id)
       )
    THEN
        RAISE EXCEPTION 'file-source confirmation operation is invalid';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_file_source_template_confirmation_immutable
    ON app.file_source_template_confirmations;
CREATE TRIGGER trg_file_source_template_confirmation_immutable
    BEFORE INSERT OR UPDATE OR DELETE ON app.file_source_template_confirmations
    FOR EACH ROW
    EXECUTE FUNCTION app.validate_file_source_template_confirmation();

-- Keep Template identity immutable. For a normal tenant RGPD purge the project
-- still exists and the established transaction hatch applies. The historical
-- orphan has no project, so its exceptional delete additionally requires the
-- exact pending, human operation created by the bounded cleanup command.
CREATE OR REPLACE FUNCTION app.reject_file_source_template_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
DECLARE
    erasure_operation RECORD;
    erasure_operation_id TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on' THEN
            RAISE EXCEPTION
                'file_source_templates is append-only: DELETE of id=% is forbidden.',
                OLD.id;
        END IF;

        IF EXISTS (SELECT 1 FROM app.projects WHERE id = OLD.project_id) THEN
            RETURN OLD;
        END IF;

        erasure_operation_id :=
            NULLIF(current_setting('app.file_source_erasure_operation_id', true), '');
        SELECT command_type, confirmation_mode, state, resource_path,
               effective_org_id
        INTO erasure_operation
        FROM app.operations
        WHERE id = erasure_operation_id;

        IF erasure_operation.command_type
                IS DISTINCT FROM 'file_source.template.orphan_erased'
           OR erasure_operation.confirmation_mode IS DISTINCT FROM 'human'
           OR erasure_operation.state IS DISTINCT FROM 'pending'
           OR erasure_operation.effective_org_id IS NULL
           OR NOT (
                erasure_operation.resource_path
                    @> jsonb_build_array('project:' || OLD.project_id)
                AND erasure_operation.resource_path
                    @> jsonb_build_array('file_source_template:' || OLD.id)
           )
        THEN
            RAISE EXCEPTION
                'orphan Template erasure requires its exact pending human operation';
        END IF;
        RETURN OLD;
    END IF;

    IF NEW.id                   IS DISTINCT FROM OLD.id                   OR
       NEW.project_id           IS DISTINCT FROM OLD.project_id           OR
       NEW.template_code        IS DISTINCT FROM OLD.template_code        OR
       NEW.version              IS DISTINCT FROM OLD.version              OR
       NEW.kind                 IS DISTINCT FROM OLD.kind                 OR
       NEW.content_hash         IS DISTINCT FROM OLD.content_hash         OR
       NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash OR
       NEW.contract             IS DISTINCT FROM OLD.contract             OR
       NEW.placement_class      IS DISTINCT FROM OLD.placement_class      OR
       NEW.grain                IS DISTINCT FROM OLD.grain                OR
       NEW.created_by           IS DISTINCT FROM OLD.created_by           OR
       NEW.created_at           IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'file_source_templates identity fields are immutable (id=%).', OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_file_source_template_immutable
    ON app.file_source_templates;
CREATE TRIGGER trg_file_source_template_immutable
    BEFORE UPDATE OR DELETE ON app.file_source_templates
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_file_source_template_mutation();

COMMENT ON TABLE app.file_source_template_confirmations IS
    'Immutable, operation-backed human proof for one exact Template + Mapping.';

COMMIT;
