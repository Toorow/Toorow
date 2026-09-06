-- Story 64.13 (AI-232): a source says HOW its rows become objects.
--
-- WHAT MIGRATION 236 GOT WRONG, MEASURED ON THE DERIVATION DOSSIER. It assumed
-- one shape: an object comes from a file that carries its key, so the mapping's
-- grain IS the identity, and a source with no grain is refused. On the client
-- workbook that shipped the whole use case, ONE entity of five carries a key:
--
--     video       ID (video_id)   531 rows, 527 distinct
--     restaurant  NONE            260 mentions, 253 distinct labels
--     produit     NONE            404 mentions, 556 distinct labels
--     recette     NONE            309 mentions, 332 distinct labels
--     film        NONE            175 mentions, 182 distinct labels
--
-- So 236 served the minority case and refused the majority -- including
-- `restaurant`, the entity that carries the measured factor 3 in audience
-- (Burger King 12 332 median views against 4 000 for the channel).
--
-- THE MODE BELONGS TO THE SOURCE, NOT TO THE KIND. The first draft of this
-- migration put `identity_mode` on the registry, one answer per object kind. That
-- is wrong for the case that motivates it: the same `restaurant` may arrive from a
-- workbook that only spells its name and, later, from a point-of-sale export that
-- carries a real id. Two sources, two modes, ONE identity -- because both land on
-- the same `node_id`, which `master_data_aliases` already exists to guarantee.
-- Putting the mode on the registry would have made the second source a new object
-- kind.
--
-- AND N LIVE SOURCES, ONE PER NAMESPACE. 236's partial unique index allowed a
-- single live binding per registry. The dossier's own `video` breaks it
-- immediately: it is fed by the public connector (the measures) AND by the
-- workbook (the annotations). The invariant that mattered -- "one answer to what
-- identifies this object" -- is kept by the node, not by forbidding a second
-- feeder. The index moves to (project, registry, namespace).
--
-- ALL FOUR MASTER DATA TABLES ARE EMPTY IN PRODUCTION (measured 2026-08-08:
-- 0 registries, 0 nodes, 0 bindings). That is why the new columns are plain
-- NOT NULL with no default and no backfill: there is no row to migrate, and a
-- default here would outlive the reason for it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Where the rows come from, in the alias vocabulary's own word.
--
-- `namespace` is the same opaque token `master_data_aliases.namespace` carries --
-- "a connector, a locale catalogue, a client spreadsheet". Naming it here is what
-- lets the resolver of Story 64.10 know which claims this feeder may make.
-- ---------------------------------------------------------------------------
ALTER TABLE app.master_data_source_bindings
    ADD COLUMN IF NOT EXISTS namespace TEXT NOT NULL
        CHECK (length(btrim(namespace)) BETWEEN 1 AND 80);

-- ---------------------------------------------------------------------------
-- 2. How a row of THIS source becomes an object.
--
--   source_key      the pinned mapping's grain is the identity (Story 64.1's
--                   original and only mode). `video`.
--   governed_label  the source carries no key: the normalized label resolves
--                   through master_data_aliases and the NODE is the identity.
--                   `restaurant`, `produit`, `film`.
--
-- `label_field` names WHICH mapped field carries that label. It is a POINTER into
-- the pinned mapping, never a copy of it: the mapping says which columns exist,
-- it does not say which one is a label, so this is a new fact rather than a
-- second mapping store.
-- ---------------------------------------------------------------------------
ALTER TABLE app.master_data_source_bindings
    ADD COLUMN IF NOT EXISTS identity_mode TEXT NOT NULL
        CHECK (identity_mode IN ('source_key', 'governed_label'));

ALTER TABLE app.master_data_source_bindings
    ADD COLUMN IF NOT EXISTS label_field TEXT;

-- Required by one mode and meaningless to the other. A nullable column with a
-- comment would let a `source_key` binding carry a label field nobody reads, and
-- a `governed_label` binding carry none and resolve nothing.
ALTER TABLE app.master_data_source_bindings
    DROP CONSTRAINT IF EXISTS ck_master_data_source_bindings_label_field;
ALTER TABLE app.master_data_source_bindings
    ADD CONSTRAINT ck_master_data_source_bindings_label_field
    CHECK ((identity_mode = 'governed_label') = (label_field IS NOT NULL));

-- ---------------------------------------------------------------------------
-- 3. One live feeder PER NAMESPACE, not one per registry.
-- ---------------------------------------------------------------------------
DROP INDEX IF EXISTS app.uq_master_data_source_bindings_live;

CREATE UNIQUE INDEX IF NOT EXISTS uq_master_data_source_bindings_live_namespace
    ON app.master_data_source_bindings (project_id, registry_id, namespace)
    WHERE released_at IS NULL;

COMMENT ON COLUMN app.master_data_source_bindings.identity_mode IS
    'Story 64.13: how a row of THIS source becomes an object. On the binding and not the registry, because the same kind may arrive keyed from one source and named-only from another -- both landing on one node_id.';

COMMENT ON COLUMN app.master_data_source_bindings.label_field IS
    'Story 64.13: which mapped field carries the label, for governed_label bindings. A pointer into the pinned mapping version, never a copy of it: the mapping declares which columns exist, not which one is a label.';

COMMIT;
