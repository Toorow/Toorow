-- Story 48.2, correcting migration 140 forward: take the geography back out.
--
-- Migration 140 landed the generic Master Data owner, and Story 49.2's
-- Implementation Gate -- written after it, against it -- named what was still
-- Country-shaped in it. Two of those findings are correct and are fixed here
-- rather than argued with. 140 is already applied, so it is never re-edited:
-- this is the forward migration the repository rule requires.
--
-- 1. `uq_master_data_nodes_singleton_catch_all` hard-coded
--    `node_kind = 'rest_of_world'` into the generic core. That is exactly the
--    Country-specific invariant the generic model must not carry: a Business
--    Domain registry has no catch-all, and a future kind might legitimately
--    have two. The rule is real, but it belongs to the Country adapter, which
--    is where core.country_registry.ensure_rest_of_world now enforces it.
--
-- 2. `vocabulary_version_id NOT NULL` forced every object kind to be backed by
--    a controlled value set. Country has one (ISO); Business Domains, Products
--    and Activities have none, and would have had to invent an empty vocabulary
--    to use the lifecycle at all. A version that pins no vocabulary is now
--    legal, and a version that pins one still cannot pin a missing row.
--
-- What is deliberately NOT done here: platform/organization scope for
-- registries. Story 49.2 owns the org-scoped object model, and reshaping five
-- tables' project_id half-way would leave precisely the dual-scope
-- compatibility branch that both stories forbid. Recorded rather than started.

BEGIN;

-- 1. The catch-all singleton stops being a database-wide law about geography.
DROP INDEX IF EXISTS app.uq_master_data_nodes_singleton_catch_all;

-- 2. A version may pin a vocabulary; it is no longer required to.
ALTER TABLE app.master_data_object_versions
    ALTER COLUMN vocabulary_version_id DROP NOT NULL;

COMMENT ON COLUMN app.master_data_object_versions.vocabulary_version_id IS
    'Optional. The controlled value set this version resolves member values against. Country pins the ISO snapshot; an object kind whose members are all nodes pins nothing.';

COMMENT ON TABLE app.master_data_nodes IS
    'Story 48.2: stable Master Data identities whose label may change. Kind-specific cardinality rules (one Rest of World per Country registry) belong to the capability adapter, never to this table.';

COMMIT;
