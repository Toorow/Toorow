-- Story 68.5: a node version names the import that landed it.
--
-- WHAT A REFERENCE IMPORT IS. Epic 68 routes an imported file three ways --
-- facts (the warehouse), events (68.4), reference (this story). A file whose
-- pinned mapping declares ONE entity key (68.2's `designates_object_kind`) and
-- no measures is a reference file: its rows are attributes OF an entity, never
-- facts. They land as MDM node versions through the only writers that exist,
-- `master_data.create_node` / `create_node_version`.
--
-- WHY A COLUMN AND NOT A PAYLOAD KEY. The provenance of a version -- which
-- execution, which pinned mapping, which ledger row carried the row that
-- minted it -- is a fact ABOUT the landing, not part of the landed content.
-- `create_node_version`'s content_hash is the version's IDENTITY, and it is
-- computed over (node_id, payload, type_version_id, vocabulary_version_id).
-- Folding execution_id into the payload would make the same attributes under
-- a new execution hash differently, and the honest per-entity no-op -- "this
-- snapshot changes nothing for this entity, mint nothing" -- would become
-- unprovable. A column outside the hashed content keeps the two apart.
--
-- WHY NULLABLE. Every version written before this story, and every version
-- written by an operator gesture after it, carries no import provenance.
-- NULL reads as "not landed by an import", which is exactly what happened;
-- inventing a provenance for them would be a lie about the past.
--
-- WHAT IT HOLDS. One JSONB object, written once at INSERT:
--   {"execution_id", "mapping_version_id", "ledger_id", "datastream_id"}.
-- The shape is a bag rather than four columns because the ledger id, the
-- execution and the pinned mapping are ONE fact -- the governed bundle of the
-- import -- and a reader either names the whole act or none of it.
--
-- RLS is untouched: the policies of migration 273 are table-level and a new
-- column changes none of them (the same argument migration 293 makes).

BEGIN;

ALTER TABLE app.master_data_object_versions
    ADD COLUMN IF NOT EXISTS import_provenance JSONB;

COMMENT ON COLUMN app.master_data_object_versions.import_provenance IS
    'Story 68.5: the import that landed this version -- {execution_id, mapping_version_id, ledger_id, datastream_id} -- written once, never updated. NULL for operator-written versions and every version that predates the reference route. Deliberately OUTSIDE the content_hash identity: the same attributes under a new execution are the same version content, and the per-entity no-op depends on it.';

COMMIT;
