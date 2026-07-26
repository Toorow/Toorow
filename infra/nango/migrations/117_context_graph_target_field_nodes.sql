-- infra/nango/migrations/117_context_graph_target_field_nodes.sql
--
-- Story 44.10: dictionary fields become first-class knowledge-graph nodes.
-- app.context_graph's from_type / to_type CHECK constraints were written by
-- migration 031 as `CHECK (from_type IN ('topic','procedure','schema_doc'))`
-- (one inline, auto-named CHECK per column). This migration widens BOTH to
-- also allow 'target_field', so an operator can link a topic/procedure to the
-- data-dictionary field it explains.
--
-- Node identity for a target_field edge is the field NAME (app.target_fields'
-- primary key), not a surrogate id -- app.target_fields has no id column.
-- No FK is added: app.context_graph is deliberately un-FK'd across all node
-- types (topics, procedures and schema docs are not FK'd either, because the
-- table stores heterogeneous endpoints in one pair of TEXT columns).
-- Existence is enforced in application code by
-- core.context_store._node_exists_in_scope, which for target_field checks
-- app.target_fields by name and EXCLUDES status='deleted' (soft delete,
-- migration 107).
--
-- ORG SCOPING / RGPD: nothing to add to the migration-099 allowlist.
-- app.context_graph already predates this change and app.target_fields is
-- PLATFORM-GLOBAL (no org_id/project_id column -- see 107's header and
-- core.datamodel.get_target_field). This migration creates no table.
--
-- R44-NFR01 (the Supabase schema is NOT linear): the constraint NAMES are
-- never assumed. Both are located by reading pg_get_constraintdef() out of
-- pg_constraint for the ACTUAL current definition. Ambiguity (more than one
-- CHECK matching a column's node-type enum) raises instead of guessing.
--
-- Idempotent: re-running finds 'target_field' already in each definition and
-- skips (RAISE NOTICE), touching nothing.

BEGIN;

DO $$
DECLARE
    col           TEXT;
    con           RECORD;
    match_count   INTEGER;
BEGIN
    IF to_regclass('app.context_graph') IS NULL THEN
        -- Fail loud rather than silently "succeeding": every other statement
        -- of story 44.10 assumes this table exists.
        RAISE EXCEPTION
            'app.context_graph is missing -- apply migration 031 first (117 cannot proceed)';
    END IF;

    FOREACH col IN ARRAY ARRAY['from_type', 'to_type'] LOOP
        -- Narrow the search deliberately: the CHECK must mention the column
        -- AND both the 'topic' and 'schema_doc' literals. A bare '%from_type%'
        -- ILIKE would also match an unrelated CHECK that merely references the
        -- column (e.g. a future cross-column constraint), and rewriting that
        -- as a node-type enum would silently destroy it.
        SELECT count(*) INTO match_count
        FROM pg_constraint c
        WHERE c.conrelid = 'app.context_graph'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%' || col || '%'
          AND pg_get_constraintdef(c.oid) ILIKE '%''topic''%'
          AND pg_get_constraintdef(c.oid) ILIKE '%''schema_doc''%';

        IF match_count = 0 THEN
            RAISE EXCEPTION
                'app.context_graph has no CHECK constraint enumerating node types for % -- '
                'the 031 schema this migration widens is not the one deployed; refusing to '
                'invent one', col;
        END IF;

        IF match_count > 1 THEN
            RAISE EXCEPTION
                'app.context_graph has % CHECK constraints matching the % node-type enum -- '
                'expected exactly 1, refusing to guess which to rewrite', match_count, col;
        END IF;

        FOR con IN
            SELECT c.conname, pg_get_constraintdef(c.oid) AS def
            FROM pg_constraint c
            WHERE c.conrelid = 'app.context_graph'::regclass
              AND c.contype = 'c'
              AND pg_get_constraintdef(c.oid) ILIKE '%' || col || '%'
              AND pg_get_constraintdef(c.oid) ILIKE '%''topic''%'
              AND pg_get_constraintdef(c.oid) ILIKE '%''schema_doc''%'
        LOOP
            IF con.def ILIKE '%''target_field''%' THEN
                RAISE NOTICE 'skip %: already allows target_field', con.conname;
                CONTINUE;
            END IF;

            EXECUTE format('ALTER TABLE app.context_graph DROP CONSTRAINT %I', con.conname);
            EXECUTE format(
                'ALTER TABLE app.context_graph ADD CONSTRAINT %I '
                'CHECK (%I IN (''topic'', ''procedure'', ''schema_doc'', ''target_field''))',
                con.conname, col
            );
            RAISE NOTICE 'extended % (%) to allow target_field', con.conname, col;
        END LOOP;
    END LOOP;
END
$$;

COMMENT ON TABLE app.context_graph IS
    'Knowledge-graph edges between context nodes. from_type/to_type enum: '
    'topic | procedure | schema_doc | target_field (story 44.10). Endpoint ids '
    'are the node''s own key -- context_topics.id, procedures.id, '
    'schema_context.id, and for target_field the field NAME '
    '(app.target_fields.name). No FK by design: one TEXT column pair holds '
    'heterogeneous endpoints; existence is enforced by '
    'core.context_store._node_exists_in_scope.';

COMMIT;
