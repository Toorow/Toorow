-- 338 -- the Knowledge Graph projection dies with the project it names, and the
--        eraser SAYS SO.
--
-- THE DEFECT, and it is on the RGPD path. `app.context_graph` was created by
-- migration 031 with NINE columns and NO foreign key at all (031:41-56) --
-- migration 317's own header says it: *"`app.context_graph` also carries no
-- `org_id`, no RLS and no foreign key at all"*. The organization eraser derives
-- its whole plan from the foreign-key graph:
--
--     core/org_purge.py:_FK_GRAPH_SQL
--       WHERE c.contype = 'f'
--         AND c.confdeltype IN ('a', 'r')   -- NO ACTION / RESTRICT only
--
-- A table with no foreign key is therefore invisible to BOTH mechanisms: the
-- eraser never visits it, and Postgres has nothing to cascade through. Since
-- migration 317 the authority `app.context_relationships` hangs off
-- `app.projects (org_id, id)` ON DELETE CASCADE, so an org erasure deletes the
-- AUTHORITY rows and STRANDS the projection rows that were written with them.
--
-- What is left behind is not inert. `context_api.delete_graph_edge`
-- (`server/core/context_api.py:1648-1670`) reads the projection, looks for the
-- relation behind it, finds none, and answers **410 `relation_not_governed`** --
-- correctly, because a projection with no authority means a second writer. The
-- erasure manufactured exactly the state that door exists to refuse, on rows of
-- a person who asked to be forgotten.
--
-- MEASURED BEFORE A LINE, 2026-09-02, on a disposable cluster carrying all 337
-- migrations (port 55432):
--
--   SELECT count(*) FROM app.context_graph;                            ->    0
--   SELECT count(*) FROM app.context_graph e                                    -- orphans
--    WHERE e.project_id IS NOT NULL
--      AND NOT EXISTS (SELECT 1 FROM app.projects p WHERE p.id = e.project_id); ->  0
--   SELECT conname FROM pg_constraint
--    WHERE conrelid = 'app.context_graph'::regclass AND contype = 'f';  -> (none)
--
--   grep -rn "INSERT INTO app.context_graph" server/tests                -> 7 hits
--     * 3 in `tests/integration/test_context_constraints.py` (145, 184, 197):
--       no `project_id` column at all -> NULL -> the PLATFORM scope, which this
--       foreign key leaves unconstrained under MATCH SIMPLE. Untouched.
--     * 1 in `tests/integration/test_context_relationships_pg.py:387`
--       (`_seed_incumbent_edge`): `world["project_a"]`, a project the fixture
--       INSERTs into `app.projects` first. Untouched.
--     * 3 in `tests/core/*` (`test_context_relationships.py`,
--       `test_context_seed.py`, `test_context_store_target_field_nodes.py`):
--       assertions against a FAKE cursor, no database. Untouched.
--   The only other writer of an edge is `core/context_relationships.py:443`,
--   which takes the project id from the relation it is projecting.
--
--   So NO fixture writes an edge under a fictive project id. Nothing below has
--   to be repaired for this constraint to land, and nothing is deleted.
--
-- ============================================================================
-- RESTRICT, NOT CASCADE -- AND THAT IS THE WHOLE POINT
-- ============================================================================
--
-- A read projection dying with its project is what CASCADE says, and CASCADE
-- would indeed stop the rows from being stranded. It would also make the
-- erasure MUTE. `plan_purge` walks `confdeltype IN ('a','r')` only: a
-- CASCADE-only table is erased by Postgres, from a statement about somebody
-- else, and `org_purge` names it in ZERO of its own -- so it appears in no
-- `rows_by_table` entry of the audited erasure report, and no test can assert
-- that the projection was reached rather than merely absent.
--
-- This repository has already decided that question twice, and both times the
-- same way:
--
--   * migration 337, three days ago, for `app.semantic_recompile_attempts`:
--     *"RESTRICT, not CASCADE, and that is the whole point [...] this edge is
--     what makes that claim true"*;
--   * `docs/product-architecture/context-hub.md:912-926`, ratified 2026-09-01
--     (AI-344), for `app.context_events`: `fk_context_events_project` is ON
--     DELETE RESTRICT and the document states the consequence in the words this
--     migration is following -- *"The parent therefore IS in the tenant erasure
--     plan and its rows are deleted explicitly"*.
--
-- 58 of the 124 foreign keys pointing at `app.projects` already carry RESTRICT
-- (measured 2026-09-02), which is also the shape `core/org_purge.py` was built
-- around: *"deleting a project must not silently erase its datastreams"*.
--
-- WHAT THIS CHANGES OUTSIDE THE ERASURE, stated rather than discovered. A bare
-- `DELETE FROM app.projects` on a project that still holds Knowledge Graph
-- edges is now REFUSED instead of leaving them behind. There is one production
-- caller of that statement (`core/projects_api.py`, the rollback of a project
-- whose tenant key failed to provision -- a project that cannot yet hold an
-- edge), and the org and project erasures, which now emit a statement that
-- NAMES `app.context_graph`. The endpoint path is `plan_purge`, and it deletes
-- the projection post-order, before the project it hangs off.
--
-- THE PLATFORM SCOPE IS UNTOUCHED. `context_graph.project_id` is NULLABLE and
-- NULL means the platform scope (317's header spells this out). Under the
-- default MATCH SIMPLE a foreign key is not enforced on a NULL column, so a
-- platform edge is neither constrained nor erased by a tenant purge -- which is
-- what a platform edge is.
--
-- IT MEASURES BEFORE IT TIGHTENS, AND IT NEVER REPAIRS DATA. The `DO` block
-- below counts the orphaned edges PER MISSING PROJECT and RAISES with the
-- counts. It does not delete them and does not invent a project for them: an
-- orphaned projection is evidence that the erasure ran without this edge in
-- place, and a person decides what those rows mean. In a fixture the repair is
-- to seed the project; in production it is a decision, named, with its count.
--
-- Not applied to production by the session that wrote it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. THE MEASUREMENT. Nothing is tightened until this block has said zero.
-- ---------------------------------------------------------------------------

DO $orphans$
DECLARE
    total  BIGINT;
    report TEXT := '';
    row_probe RECORD;
BEGIN
    IF to_regclass('app.context_graph') IS NULL THEN
        RAISE EXCEPTION
            'app.context_graph is missing -- apply migration 031 first (338 '
            'gives its projection rows the parent the org eraser walks)';
    END IF;

    SELECT count(*) INTO total
      FROM app.context_graph e
     WHERE e.project_id IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM app.projects p WHERE p.id = e.project_id);

    IF total > 0 THEN
        FOR row_probe IN
            SELECT e.project_id AS missing_project, count(*) AS edges
              FROM app.context_graph e
             WHERE e.project_id IS NOT NULL
               AND NOT EXISTS (SELECT 1 FROM app.projects p WHERE p.id = e.project_id)
             GROUP BY e.project_id
             ORDER BY 2 DESC, 1
        LOOP
            report := report || format(E'\n    %s: %s edge(s)',
                                       row_probe.missing_project, row_probe.edges);
        END LOOP;

        RAISE EXCEPTION
            'migration 338 refuses to tighten: % Knowledge Graph edge(s) name a '
            'project that no longer exists, per missing project: %', total, report
        USING HINT =
            'those rows are the defect this migration closes, already realised: '
            'an organization erasure deleted the relationship authority and left '
            'its read projection behind, and `DELETE /api/context/graph/edges` '
            'answers 410 relation_not_governed on each of them. This migration '
            'will NOT delete them and will not invent a project for them. '
            'Decide what they mean first -- in a test fixture, seed the project '
            'the edge names; in production, retire each edge through the '
            'Knowledge Graph -- then apply 338 again.';
    END IF;

    RAISE NOTICE 'migration 338: 0 orphaned context_graph edge(s)';
END
$orphans$;

-- ---------------------------------------------------------------------------
-- 1. The parent the eraser walks.
-- ---------------------------------------------------------------------------

ALTER TABLE app.context_graph
    DROP CONSTRAINT IF EXISTS context_graph_project_id_fkey;
ALTER TABLE app.context_graph
    ADD CONSTRAINT context_graph_project_id_fkey
        FOREIGN KEY (project_id) REFERENCES app.projects (id) ON DELETE RESTRICT;

COMMENT ON CONSTRAINT context_graph_project_id_fkey ON app.context_graph IS
    '338: the read projection of a context relation dies with its project, and '
    '`core.org_purge` SAYS so. Migration 031 gave this table no foreign key at '
    'all, so the FK-derived purge plan never visited it: an org erasure '
    'cascaded `app.context_relationships` (317, ON DELETE CASCADE) and stranded '
    'the projection rows, which `context_api.delete_graph_edge` then reported '
    'as 410 relation_not_governed. RESTRICT rather than CASCADE for the reason '
    'migration 337 states about `semantic_recompile_attempts`: `plan_purge` '
    'walks `confdeltype IN (''a'',''r'')` only, so a CASCADE-only table is '
    'erased by Postgres and named in NONE of the eraser''s own statements. '
    'project_id stays NULLABLE -- NULL is the platform scope, which MATCH '
    'SIMPLE leaves unconstrained.';

COMMIT;
