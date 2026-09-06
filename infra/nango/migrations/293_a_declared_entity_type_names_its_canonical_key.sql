-- Story 68.1: a declared entity type names its canonical key.
--
-- WHAT AN ENTITY TYPE IS. Since migration 140 a registry IS the type: one stable
-- owner per (Project, object_kind), the kind an opaque string the core never
-- compares to a literal (AD-2). Story 64.1 declared such a kind only together
-- with the source that feeds it; Story 68.1 adds the feeder-less declaration --
-- name, canonical key, display label -- so reconciliation is data, never code.
--
-- WHY A COLUMN AND NOT A METADATA BAG. The registry row carries no generic
-- property field: `attribute_contract` (migration 239) is the contract of what
-- a NODE may carry, not metadata about the kind itself, and folding the kind's
-- own identity into it would make one column answer two questions. The
-- canonical key is a first-class fact of the declaration -- replay and refusal
-- compare it (the SAME declaration returns the existing registry; a DIFFERENT
-- one is the named conflict `entity_type_exists`) -- so it is a column, with
-- the same snake_case shape rule `object_kind` already carries.
--
-- WHY NULLABLE. Registries declared before this story -- Country, the Epic 49
-- object set, every client kind declared through Story 64.1 -- never named a
-- canonical key, and inventing one for them would be a lie about what was
-- declared. NULL reads as "no canonical key was declared", which is exactly
-- what happened. Only the governed declaration door writes it, and it always
-- does.
--
-- RLS is untouched: the policies of migration 273 are table-level and a new
-- column changes none of them.

BEGIN;

ALTER TABLE app.master_data_registries
    ADD COLUMN IF NOT EXISTS canonical_key TEXT;

ALTER TABLE app.master_data_registries
    DROP CONSTRAINT IF EXISTS ck_master_data_registries_canonical_key;
ALTER TABLE app.master_data_registries
    ADD CONSTRAINT ck_master_data_registries_canonical_key
    CHECK (canonical_key IS NULL OR canonical_key ~ '^[a-z][a-z0-9_]{1,39}$');

COMMENT ON COLUMN app.master_data_registries.canonical_key IS
    'Story 68.1: the business identity field a declared entity type reconciles on. NULL for registries declared before the governed declaration door existed -- "no key declared", not "unknown".';

COMMIT;
