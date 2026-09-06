-- A mapping review freezes the four bindings named by Story 38.16:
-- plan, mapping, retained raw import and file-source Template version.
--
-- The first two have been optimistic locks since migration 138.  The last two
-- were rendered in the review but absent from the preparation row, so a file
-- repair could be confirmed after the retained input or the Datastream's
-- Template binding changed.  Rendering a value is not freezing it.
--
-- Nullable is intentional.  A generic connector-pull mapping change has no raw
-- import and no file Template.  NULL therefore means "this axis was not
-- applicable at preparation time" and confirmation revalidates that no
-- Template appeared in the meantime.  Existing preparations keep their former
-- meaning and remain confirmable: they predate file-bound reviews and carry no
-- new expected values.

ALTER TABLE app.datastream_change_preparations
    ADD COLUMN IF NOT EXISTS expected_raw_import_id TEXT;

ALTER TABLE app.datastream_change_preparations
    ADD COLUMN IF NOT EXISTS expected_template_id TEXT;

ALTER TABLE app.datastream_change_preparations
    ADD COLUMN IF NOT EXISTS expected_template_version INTEGER;

ALTER TABLE app.datastream_change_preparations
    ADD COLUMN IF NOT EXISTS binding_snapshot_version SMALLINT NOT NULL DEFAULT 0;

-- A preparation already carries its Datastream and Project. These composite
-- foreign keys make a direct SQL write obey the same scope rule as the service;
-- a valid raw import or Template from another tenant is not a valid binding.
CREATE UNIQUE INDEX IF NOT EXISTS uq_inbound_raw_imports_id_datastream
    ON app.inbound_raw_imports (id, datastream_id);

CREATE UNIQUE INDEX IF NOT EXISTS uq_file_source_templates_id_project_version
    ON app.file_source_templates (id, project_id, version);

ALTER TABLE app.datastream_change_preparations
    DROP CONSTRAINT IF EXISTS datastream_change_preparations_expected_raw_import_id_fkey;
ALTER TABLE app.datastream_change_preparations
    DROP CONSTRAINT IF EXISTS datastream_change_preparations_expected_template_id_fkey;
ALTER TABLE app.datastream_change_preparations
    DROP CONSTRAINT IF EXISTS fk_datastream_change_raw_import_scope;
ALTER TABLE app.datastream_change_preparations
    ADD CONSTRAINT fk_datastream_change_raw_import_scope
    FOREIGN KEY (expected_raw_import_id, datastream_id)
    REFERENCES app.inbound_raw_imports (id, datastream_id) ON DELETE RESTRICT;

ALTER TABLE app.datastream_change_preparations
    DROP CONSTRAINT IF EXISTS fk_datastream_change_template_scope;
ALTER TABLE app.datastream_change_preparations
    ADD CONSTRAINT fk_datastream_change_template_scope
    FOREIGN KEY (expected_template_id, project_id, expected_template_version)
    REFERENCES app.file_source_templates (id, project_id, version) ON DELETE RESTRICT;

COMMENT ON COLUMN app.datastream_change_preparations.expected_raw_import_id IS
    'Retained inbound_raw_import whose evidence opened this mapping review; NULL for a '
    'generic mapping change not opened from one file.';

COMMENT ON COLUMN app.datastream_change_preparations.expected_template_id IS
    'Exact immutable file_source_templates row bound to the Datastream at preparation '
    'time; NULL when no file-source Template was applicable.';

COMMENT ON COLUMN app.datastream_change_preparations.expected_template_version IS
    'Human-readable version carried by expected_template_id and revalidated at '
    'confirmation; NULL exactly when expected_template_id is NULL.';

COMMENT ON COLUMN app.datastream_change_preparations.binding_snapshot_version IS
    '0 for preparations created before the file/Template binding snapshot existed; '
    '1 when absence or presence of those bindings was explicitly frozen.';

ALTER TABLE app.datastream_change_preparations
    DROP CONSTRAINT IF EXISTS ck_datastream_change_template_binding;

ALTER TABLE app.datastream_change_preparations
    ADD CONSTRAINT ck_datastream_change_template_binding CHECK (
        (expected_template_id IS NULL AND expected_template_version IS NULL)
        OR
        (expected_template_id IS NOT NULL AND expected_template_version IS NOT NULL)
    );

ALTER TABLE app.datastream_change_preparations
    DROP CONSTRAINT IF EXISTS ck_datastream_change_binding_snapshot_version;

ALTER TABLE app.datastream_change_preparations
    ADD CONSTRAINT ck_datastream_change_binding_snapshot_version CHECK (
        binding_snapshot_version IN (0, 1)
    );
