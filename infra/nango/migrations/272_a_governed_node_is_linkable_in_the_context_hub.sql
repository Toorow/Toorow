-- Story 37.9: a governed Master Data node becomes a knowledge-graph endpoint.
--
-- THE MEASURED GAP. `docs/product-architecture/capabilities/country.md`'s coverage
-- matrix requires, of Context Hub: "Geographic nodes linkable to Business Domains,
-- knowledge, Skills and AI paths". Measured 2026-08-17, `app.context_graph`'s
-- from_type / to_type enum was `topic | procedure | schema_doc | target_field`, so a
-- governed Market or Region could not be an endpoint of ANY edge -- and
-- `core.context_search`'s one-hop walk (the corpus an agent actually reads) could
-- therefore never reach a geographic node. Nothing was broken; the link had never
-- existed.
--
-- WHY `master_data_node` AND NOT `market` / `region` / `country`. Country is the FIRST
-- capability to need this, not the only one: Competitors, Brands and every future
-- registry mount their identities on the same generic owner
-- (`app.master_data_nodes`, migration 140). One type serves all of them, its id is the
-- node's own `mdnode_...` id, and the node's KIND stays readable where it is governed
-- (`master_data_nodes.node_kind`). Three geographic types would have been three answers
-- to "is this node a market or a region", and would have needed a fourth the day a
-- Business Domain arrived.
--
-- A COUNTRY IS DELIBERATELY NOT LINKABLE, and for the same reason it declares no
-- used-by dependency: an ISO code is a `child_value` of the hierarchy, never a node.
-- Making it an endpoint would put a VALUE in a column pair whose whole contract is
-- governed identities, and the country's meaning is reached through its market anyway.
--
-- FOLLOWS MIGRATION 117 EXACTLY, because 117 already solved the hard part. The CHECK
-- constraints were written inline and auto-named by migration 031, and R44-NFR01 says
-- the Supabase schema is NOT linear -- so the names are never assumed. Each is located
-- by reading `pg_get_constraintdef()`, ambiguity RAISES rather than guessing, and
-- re-running finds the literal already present and skips.
--
-- NO FK, by design and unchanged: one TEXT column pair holds heterogeneous endpoints.
-- Existence is enforced in application code by
-- `core.context_store._node_exists_in_scope`, which for `master_data_node` checks
-- `app.master_data_nodes` with `archived_at IS NULL` -- an archived identity must not
-- become a FRESH edge endpoint, exactly as a soft-deleted target_field must not.
-- Existing edges are untouched: history outlives visibility.
--
-- ORG SCOPING / RGPD: nothing to add to the migration-099 allowlist. This migration
-- creates no table, and `app.context_graph` predates it.
--
-- WHAT THIS DOES NOT CLOSE, stated rather than left to be discovered. The same matrix
-- line also asks for Business Domains, and `app.mdm_business_domains` (migration 130)
-- is not a `context_graph` node type either -- for ANY object, not only geographic
-- ones. That is a Context Hub gap at the level of the surface, not a Country one, and
-- widening the enum for it would be deciding a Business Domain's graph identity here,
-- in a migration, on the way past.

BEGIN;

DO $$
DECLARE
    col           TEXT;
    con           RECORD;
    match_count   INTEGER;
BEGIN
    IF to_regclass('app.context_graph') IS NULL THEN
        RAISE EXCEPTION
            'app.context_graph is missing -- apply migration 031 first (272 cannot proceed)';
    END IF;

    FOREACH col IN ARRAY ARRAY['from_type', 'to_type'] LOOP
        -- Narrowed the same way 117 narrowed it: the CHECK must mention the column AND
        -- both the 'topic' and 'schema_doc' literals, so an unrelated CHECK that merely
        -- references the column is never rewritten as a node-type enum.
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
                'the schema this migration widens is not the one deployed; refusing to '
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
            IF con.def ILIKE '%''master_data_node''%' THEN
                RAISE NOTICE 'skip %: already allows master_data_node', con.conname;
                CONTINUE;
            END IF;

            -- The literal list is rewritten in full rather than appended to, because a
            -- CHECK definition is text and there is no "add a value" operation. 117's
            -- four values are carried forward verbatim: dropping one here would delete
            -- a link type that already ships.
            IF con.def NOT ILIKE '%''target_field''%' THEN
                RAISE EXCEPTION
                    'app.context_graph.% does not allow target_field -- migration 117 is '
                    'not applied, and rewriting the enum now would silently DROP it; '
                    'apply 117 first', col;
            END IF;

            EXECUTE format('ALTER TABLE app.context_graph DROP CONSTRAINT %I', con.conname);
            EXECUTE format(
                'ALTER TABLE app.context_graph ADD CONSTRAINT %I '
                'CHECK (%I IN (''topic'', ''procedure'', ''schema_doc'', ''target_field'', '
                '''master_data_node''))',
                con.conname, col
            );
            RAISE NOTICE 'extended % (%) to allow master_data_node', con.conname, col;
        END LOOP;
    END LOOP;
END
$$;

COMMENT ON TABLE app.context_graph IS
    'Knowledge-graph edges between context nodes. from_type/to_type enum: '
    'topic | procedure | schema_doc | target_field (story 44.10) | master_data_node '
    '(story 37.9). Endpoint ids are the node''s own key -- context_topics.id, '
    'procedures.id, schema_context.id, the field NAME for target_field '
    '(app.target_fields.name), and master_data_nodes.id for master_data_node. A '
    'country ISO code is a child_value of a hierarchy, never a node, and is therefore '
    'NOT an endpoint. No FK by design: one TEXT column pair holds heterogeneous '
    'endpoints; existence is enforced by core.context_store._node_exists_in_scope.';

COMMIT;
