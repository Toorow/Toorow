-- infra/nango/migrations/118_context_owner_column.sql
--
-- Story 44.11: Node Ownership & Request Review.
--
-- Adds a nullable `owner` TEXT column to app.context_topics and
-- app.procedures, PLUS their two version-snapshot tables
-- (app.context_topics_versions / app.procedures_versions) -- the
-- version-append INSERTs in core.context_store enumerate columns
-- explicitly, so a version row without `owner` would silently lose it on
-- every edit. `owner` is a free-form human identifier (email, by
-- convention) set explicitly by a curator; when null, the graph bundle
-- (server/core/context_api.py::_get_graph) resolves display as
-- created_by, else the literal "auto" -- that resolution happens in
-- application code, not in SQL, so this column itself stays nullable with
-- no default.
--
-- Deliberately NOT touched (out of scope, per the story record):
--   - app.schema_context / app.schema_context_versions: generated docs have
--     no human owner (AD-17).
--   - app.target_fields / app.target_fields_versions: owner there is
--     `created_by` (44.10 already renders it); no separate owner column.
--
-- Idempotent: ADD COLUMN IF NOT EXISTS on each table, guarded by
-- to_regclass so a table that does not exist yet (schema is NOT linear,
-- R44-NFR01) is skipped with a NOTICE instead of raising.
--
-- ORG SCOPING / RGPD: nothing to add to the migration-099 allowlist. This
-- migration only adds a column to tables that already exist in the 099
-- allowlist (or predate org scoping entirely) -- it creates no table and
-- changes no FK/ownership semantics of the rows themselves.

BEGIN;

DO $$
BEGIN
    IF to_regclass('app.context_topics') IS NULL THEN
        RAISE NOTICE 'skip: app.context_topics does not exist';
    ELSE
        ALTER TABLE app.context_topics ADD COLUMN IF NOT EXISTS owner TEXT;
        -- COMMENT inside the guard (44.11 re-review): a bare COMMENT after the
        -- DO block would raise 42P01 on a database where the table is absent,
        -- exactly the non-linear-schema case this guard exists for.
        EXECUTE 'COMMENT ON COLUMN app.context_topics.owner IS '
            || quote_literal(
                'Story 44.11: nullable explicit human owner (free-form, email by '
                'convention). Null means "no explicit owner set" -- display resolution '
                '(explicit -> created_by -> ''auto'') happens in '
                'core.context_api._resolve_owner, not here.');
    END IF;

    IF to_regclass('app.context_topics_versions') IS NULL THEN
        RAISE NOTICE 'skip: app.context_topics_versions does not exist';
    ELSE
        ALTER TABLE app.context_topics_versions ADD COLUMN IF NOT EXISTS owner TEXT;
    END IF;

    IF to_regclass('app.procedures') IS NULL THEN
        RAISE NOTICE 'skip: app.procedures does not exist';
    ELSE
        ALTER TABLE app.procedures ADD COLUMN IF NOT EXISTS owner TEXT;
        EXECUTE 'COMMENT ON COLUMN app.procedures.owner IS '
            || quote_literal(
                'Story 44.11: nullable explicit human owner (free-form, email by '
                'convention). Null means "no explicit owner set" -- display resolution '
                '(explicit -> created_by -> ''auto'') happens in '
                'core.context_api._resolve_owner, not here.');
    END IF;

    IF to_regclass('app.procedures_versions') IS NULL THEN
        RAISE NOTICE 'skip: app.procedures_versions does not exist';
    ELSE
        ALTER TABLE app.procedures_versions ADD COLUMN IF NOT EXISTS owner TEXT;
    END IF;
END
$$;

COMMIT;
