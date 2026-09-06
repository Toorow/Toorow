-- Story 64.1 (AI-232): a client-declared object kind names what feeds it.
--
-- Numbered 236: parallel sessions took 234 and 235 while this was being written
-- (234_an_alert_names_the_destination_it_leaves_by.sql) while this was being
-- written. Renumbering mine is the cheap half of that collision; renumbering
-- theirs would rewrite a file I have not read.
--
-- WHAT WAS MISSING. `app.master_data_registries` mints one owner per (Project,
-- object_kind) and the core is already generic -- `object_kind` is an opaque
-- string, never compared to a literal. But `create_registry` has exactly ONE
-- caller in the repository, `core.country_registry`, and nothing anywhere says
-- WHICH source feeds a registry. A client who brings a workbook of videos,
-- venues or products can therefore have the identity model and no way to say
-- where its rows come from.
--
-- WHY THE IDENTITY COLUMN IS NOT A COLUMN OF THIS TABLE. It was going to be, and
-- that would have been the second mapping store `governance.md:82` forbids by
-- name -- "Governance consumes those versions; it does not create a second
-- mapping store". A Datastream's published mapping ALREADY declares which
-- columns identify a row: `mapping_payload.grain`, an array of field ids
-- (`server/core/schemas/datastream-field-mapping.schema.json`). So the binding
-- PINS a mapping version and reads the grain from it. Copying the grain here
-- would let the two disagree, and the copy would be the one Governance believed.
--
-- THE COMPOSITE FOREIGN KEY IS THE POINT. `(mapping_version_id, datastream_id,
-- project_id)` references `uq_datastream_mapping_scope`, so pinning a mapping
-- version that belongs to another Datastream -- or another Project -- is not a
-- rule somebody has to remember, it is a write the database refuses.
--
-- ONE LIVE FEEDER PER REGISTRY. A registry fed by two sources has two answers to
-- "what is the identity of this object", which is the ambiguity the whole story
-- exists to refuse. Superseding is releasing the old binding and writing a new
-- one, both in one transaction; nothing is deleted, because an old Result may
-- pin what was true then.

BEGIN;

CREATE TABLE IF NOT EXISTS app.master_data_source_bindings (
    id TEXT PRIMARY KEY CHECK (id ~ '^mdsrc_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,

    -- The object kind's owner. Its `object_kind` is the opaque string the core
    -- never interprets; this table never repeats it.
    registry_id TEXT NOT NULL,

    -- What feeds it, and the exact mapping version whose grain IS the identity.
    datastream_id TEXT NOT NULL,
    mapping_version_id TEXT NOT NULL,

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    released_at TIMESTAMPTZ,
    released_by TEXT,

    FOREIGN KEY (project_id, registry_id)
        REFERENCES app.master_data_registries(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    -- Reads uq_datastream_mapping_scope (id, datastream_id, project_id): a pinned
    -- version that belongs to another Datastream cannot be written at all.
    FOREIGN KEY (mapping_version_id, datastream_id, project_id)
        REFERENCES app.datastream_mapping_versions(id, datastream_id, project_id)
        ON DELETE RESTRICT,

    CHECK ((released_at IS NULL) = (released_by IS NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_source_bindings_live
    ON app.master_data_source_bindings (project_id, registry_id)
    WHERE released_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_master_data_source_bindings_datastream
    ON app.master_data_source_bindings (project_id, datastream_id)
    WHERE released_at IS NULL;

COMMENT ON TABLE app.master_data_source_bindings IS
    'Story 64.1: which Datastream feeds a Master Data registry, and the exact mapping version whose grain is the object identity. The grain is NOT copied here -- Governance consumes the mapping version, it does not keep a second copy of it.';

COMMENT ON COLUMN app.master_data_source_bindings.mapping_version_id IS
    'Pinned, never `latest`. The identity of an object must not change because somebody republished a mapping; moving to a new grain is releasing this binding and writing another, which is a dated act.';

COMMIT;
