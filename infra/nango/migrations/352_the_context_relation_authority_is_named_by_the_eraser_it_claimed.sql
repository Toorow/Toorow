-- 352 -- the context relation authority and its versions are NAMED by the
--        organization eraser, which is what migration 317 already claimed.
--
-- WHAT 317 SAYS, VERBATIM (its "ERASURE, AND THE PRIVILEGE HALF OF IT"
-- section):
--
--     "Both tables hang off `app.projects (org_id, id)` ON DELETE CASCADE, so
--      `core.org_purge` reaches them through the graph it walks."
--
-- The first half is a fact and the second half is false, twice over.
--
--   * `core/org_purge.py:_FK_GRAPH_SQL` reads the graph it walks with
--         WHERE c.contype = 'f'
--           AND c.confdeltype IN ('a', 'r')   -- NO ACTION / RESTRICT only
--     A CASCADE edge is deliberately ABSENT from that plan, because Postgres
--     already does the work. So a table reached only by CASCADE is erased --
--     from somebody else's statement -- and `org_purge` names it in ZERO of its
--     own. It appears in no `rows_by_table` entry of the audited erasure report,
--     and no test can assert that it was reached rather than merely absent.
--
--   * `app.context_relationship_versions` does not hang off `app.projects` at
--     all. Its only parent is `app.context_relationships` (317's
--     `fk_context_relationship_versions_relation`), also ON DELETE CASCADE.
--
-- There is NO LEAK: both tables really are erased today, by Postgres, on the
-- final `DELETE FROM app.organizations`. What is wrong is the MECHANISM NAMED.
-- The next reader is sent to `core/org_purge.py`, greps for the table, finds
-- nothing, and concludes the rows are never erased -- which is exactly the
-- fault `server/tests/conformance/test_migration_erasure_claims.py` was written
-- to stop, and which it let through here on an INCIDENT.
--
-- THE INCIDENT, because the guard passing was not luck but a measurable
-- accident. That guard asks that at least one table the migration creates carry
-- a foreign key `confdeltype IN ('a','r')`. Both of 317's tables do -- and both
-- of those edges point at each other:
--
--   fk_context_relationships_current_version      -> context_relationship_versions
--   fk_context_relationship_versions_predecessor  -> context_relationship_versions
--
-- A closed pair, attached to the tenant tree by nothing. The traversal starts at
-- `app.organizations` and never reaches either table, so the structural check
-- said yes while the mechanism said no. This migration makes the sentence true;
-- the same commit makes the guard read `plan_purge` itself, so the next such
-- pair cannot buy a green with an edge that leads nowhere.
--
-- MEASURED BEFORE A LINE, 2026-09-06, on a disposable cluster carrying all 351
-- migrations (`python scripts/disposable_postgres.py up --port 55465`, which
-- reported `migration runner OK: 351 applied, 0 already applied`):
--
--   SELECT c.conname, c.conrelid::regclass::text, c.confrelid::regclass::text,
--          c.confdeltype
--     FROM pg_constraint c
--    WHERE c.contype = 'f'
--      AND c.conrelid IN ('app.context_relationships'::regclass,
--                         'app.context_relationship_versions'::regclass);
--
--     fk_context_relationship_versions_predecessor | versions | versions      | a
--     fk_context_relationship_versions_relation    | versions | relationships | c
--     fk_context_relationships_current_version     | relations| versions      | a
--     fk_context_relationships_project             | relations| app.projects  | c
--
--   plan_purge(conn, 'org_EXAMPLE')  -> 3430 statements over 209 tables
--     app.context_relationships          -> NOT NAMED
--     app.context_relationship_versions  -> NOT NAMED
--     app.context_graph                  -> NAMED        (migration 338)
--
--   SELECT count(*) FROM app.context_relationships;                      -> 0
--   SELECT count(*) FROM app.context_relationship_versions;              -> 0
--   -- rows this migration's two constraints could not validate:
--   relationships whose (org_id, project_id) names no project             -> 0
--   versions whose relationship_id names no relation                      -> 0
--
--   grep -rn "DELETE FROM app.context_relationship" server/core -> 0 hits
--   (nothing in production deletes a relation head or a version by hand: a
--    retirement is a `status = 'superseded'` row, never a DELETE)
--
-- ============================================================================
-- RESTRICT, NOT CASCADE -- AND THAT IS THE WHOLE POINT
-- ============================================================================
--
-- The owner's note on this action item offers two ways out and this migration
-- takes the first, because the repository has already decided the question
-- three times and always the same way:
--
--   * migration 337, for `app.semantic_recompile_attempts`: *"RESTRICT, not
--     CASCADE, and that is the whole point [...] this edge is what makes that
--     claim true"*;
--   * migration 338, for `app.context_graph` -- the READ PROJECTION of the very
--     relations this migration is about: *"a CASCADE-only table is erased by
--     Postgres, from a statement about somebody else, and `org_purge` names it
--     in ZERO of its own"*;
--   * `docs/product-architecture/context-hub.md`, ratified 2026-09-01 (AI-344),
--     for `app.context_events`: `fk_context_events_project` is ON DELETE
--     RESTRICT and the document states the consequence -- *"The parent therefore
--     IS in the tenant erasure plan and its rows are deleted explicitly"*.
--
-- 338 already flipped the PROJECTION. Leaving its AUTHORITY on CASCADE is the
-- odd half: the eraser names the copy and not the original.
--
-- 59 of the 132 foreign keys pointing at `app.projects` already carry RESTRICT
-- and 19 more carry NO ACTION (measured 2026-09-06 on the cluster above), which
-- is also the shape `core/org_purge.py` was built around: *"deleting a project
-- must not silently erase its datastreams"*.
--
-- ============================================================================
-- BOTH EDGES, AND IN THIS ORDER
-- ============================================================================
--
-- Flipping only the head would name the head and leave the versions invisible:
-- `fk_context_relationship_versions_relation` is CASCADE, so the versions would
-- still be swept up by the head's own DELETE. Both edges are flipped, and the
-- planner then emits them in the only order that can run --
-- `plan_purge` is a POST-ORDER DFS, so the children are queued before the
-- parent:
--
--     UPDATE app.context_relationship_versions SET supersedes_version_id = NULL
--     UPDATE app.context_relationships         SET current_version_id     = NULL
--     DELETE FROM app.context_relationship_versions ...
--     DELETE FROM app.context_relationships ...
--     DELETE FROM app.projects ...
--
-- The two UPDATEs are the CYCLE BREAKS `org_purge` emits for the deferred
-- head->version pointer and for the version->predecessor chain; both columns are
-- nullable, so the nullable-subset rule applies and nothing is deleted to break
-- the cycle. The second is an UPDATE against an APPEND-ONLY table, and it passes
-- the TRIGGER for one reason: 317 gave the immutability trigger the
-- `current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on'` WHEN clause,
-- and `purge_org_tree` opens exactly that hatch with `SET LOCAL`.
--
-- IT DOES NOT PASS THE PRIVILEGE, AND THAT IS THE HALF THIS MIGRATION HAD TO
-- ADD. Measured, not foreseen: with the two edges flipped and nothing else, the
-- whole end-to-end purge suite went red on
--
--     psycopg.errors.InsufficientPrivilege: permission denied for table
--     context_relationship_versions
--
-- PostgreSQL checks the TABLE PRIVILEGE before the trigger, and 317 REVOKED
-- UPDATE on this table on purpose (`REVOKE UPDATE ON
-- app.context_relationship_versions FROM connector`, migration 316's lesson:
-- 207's `ALTER DEFAULT PRIVILEGES` had already granted it, so a narrow
-- `GRANT SELECT, INSERT` alone said nothing). That REVOKE is right and stays.
--
-- What is granted back is ONE COLUMN: `UPDATE (supersedes_version_id)`. The
-- eraser needs to null exactly that column and nothing else, and a column-level
-- grant is invisible to `has_table_privilege(..., 'UPDATE')` -- so the pin in
-- `server/tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py`
-- (`_UPDATE_FREE_TABLES`) keeps meaning what it means: the row's FACT, its
-- hash, its lifecycle and its actor remain unwritable by the application role,
-- and the trigger still refuses even that one column outside an erasure. This
-- is the same shape migration 183 uses for column-level SELECT, and it is the
-- narrowest privilege that lets the eraser NAME this table.
--
-- WHAT THIS CHANGES OUTSIDE THE ERASURE, stated rather than discovered.
--
--   * A bare `DELETE FROM app.projects` on a project that still holds a context
--     relation is now REFUSED (`fk_context_relationships_project`) instead of
--     silently taking the relations and their versions with it. The one
--     production caller of that statement is `core/projects_api.py:467`, the
--     rollback of a project whose tenant key failed to provision -- a project
--     that cannot yet hold a relation. The test harness already routes around
--     it: `server/tests/conftest.py` deletes a project through the SAME
--     `plan_purge` when a blocking edge answers.
--   * A bare `DELETE FROM app.context_relationships` is now refused while the
--     relation still has versions. Nothing in `server/core` writes that
--     statement (measured above); the erasure emits it, after the versions.
--   * The PLATFORM SCOPE is untouched. `org_id`/`project_id` are NULL together
--     for a platform relation (317's `ck_context_relationships_scope_is_whole`),
--     and under the default MATCH SIMPLE a composite foreign key is not enforced
--     when a column is NULL -- so a platform relation is neither constrained nor
--     erased by a tenant purge, which is what a platform relation is.
--
-- IT MEASURES BEFORE IT TIGHTENS, AND IT NEVER REPAIRS DATA. The `DO` block
-- below counts the rows each constraint could not validate and RAISES with the
-- counts. It deletes nothing and invents no parent: a relation naming a project
-- that does not exist is evidence, and a person decides what it means.
--
-- Not applied to production by the session that wrote it.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. THE MEASUREMENT. Nothing is tightened until this block has said zero.
-- ---------------------------------------------------------------------------

DO $preflight$
DECLARE
    orphan_relations BIGINT;
    orphan_versions  BIGINT;
BEGIN
    IF to_regclass('app.context_relationships') IS NULL
       OR to_regclass('app.context_relationship_versions') IS NULL THEN
        RAISE EXCEPTION
            'the context relation authority is missing -- apply migration 317 '
            'first (352 only changes the delete rule of its foreign keys)';
    END IF;

    SELECT count(*) INTO orphan_relations
      FROM app.context_relationships r
     WHERE r.project_id IS NOT NULL
       AND NOT EXISTS (
            SELECT 1 FROM app.projects p
             WHERE p.org_id = r.org_id AND p.id = r.project_id);

    SELECT count(*) INTO orphan_versions
      FROM app.context_relationship_versions v
     WHERE NOT EXISTS (
            SELECT 1 FROM app.context_relationships r WHERE r.id = v.relationship_id);

    IF orphan_relations > 0 OR orphan_versions > 0 THEN
        RAISE EXCEPTION
            'migration 352 refuses to tighten: % context relation(s) name a '
            'project that does not exist and % version row(s) name a relation '
            'that does not exist', orphan_relations, orphan_versions
        USING HINT =
            'both classes are already impossible under 317''s CASCADE foreign '
            'keys, so a row here means the constraint was dropped or the rows '
            'were written around it. This migration will NOT delete them and '
            'will not invent a parent for them. Decide what they mean first -- '
            'in a fixture, seed the project or the relation the row names; in '
            'production, retire the relation through the Context Hub -- then '
            'apply 352 again.';
    END IF;

    RAISE NOTICE
        'migration 352: 0 orphaned context relation(s), 0 orphaned version(s)';
END
$preflight$;

-- ---------------------------------------------------------------------------
-- 1. The head: the parent the eraser walks down from.
-- ---------------------------------------------------------------------------

ALTER TABLE app.context_relationships
    DROP CONSTRAINT IF EXISTS fk_context_relationships_project;
ALTER TABLE app.context_relationships
    ADD CONSTRAINT fk_context_relationships_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE RESTRICT;

COMMENT ON CONSTRAINT fk_context_relationships_project ON app.context_relationships IS
    '352: a context relation dies with its project, and `core.org_purge` SAYS '
    'so. Migration 317 created this edge ON DELETE CASCADE while its header '
    'claimed the eraser "reaches them through the graph it walks": `plan_purge` '
    'walks `confdeltype IN (''a'',''r'')` only, so a CASCADE-only table is '
    'erased by Postgres from a statement about somebody else and is named in '
    'NONE of the eraser''s own -- it appears in no `rows_by_table` entry, and '
    'no test can tell "reached" from "absent". RESTRICT for the reason '
    'migrations 337 and 338 state, 338 being the READ PROJECTION of these very '
    'relations. The COUPLE (org_id, project_id) is kept from 317: it stops a '
    'relation from carrying another org''s id. Both columns NULL is the '
    'platform scope, which MATCH SIMPLE leaves unconstrained.';

-- ---------------------------------------------------------------------------
-- 2. The versions: erased BEFORE the head, by name, not swept up by it.
-- ---------------------------------------------------------------------------

ALTER TABLE app.context_relationship_versions
    DROP CONSTRAINT IF EXISTS fk_context_relationship_versions_relation;
ALTER TABLE app.context_relationship_versions
    ADD CONSTRAINT fk_context_relationship_versions_relation
        FOREIGN KEY (relationship_id) REFERENCES app.context_relationships (id)
        ON DELETE RESTRICT;

COMMENT ON CONSTRAINT fk_context_relationship_versions_relation
    ON app.context_relationship_versions IS
    '352: the append-only history of a relation is deleted BY THE ERASER, one '
    'statement that names this table, before the head it hangs off. Migration '
    '317 created this edge ON DELETE CASCADE, and its header claimed both '
    'tables hang off `app.projects` -- this one never did: its only parent is '
    'the relation head. Under CASCADE the versions were swept up by the head''s '
    'DELETE and named nowhere. RESTRICT puts them in `plan_purge`''s post-order '
    'walk, which queues children before parents; the cycle-breaking UPDATEs on '
    '`supersedes_version_id` and on the head''s `current_version_id` pass the '
    'immutability trigger because 317 gave it the `app.rgpd_erasure` WHEN '
    'clause and `purge_org_tree` opens it with SET LOCAL.';

-- ---------------------------------------------------------------------------
-- 3. The privilege half of the cycle break -- ONE column, and only one.
-- ---------------------------------------------------------------------------

DO $grants$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '352: role `connector` does not exist on this cluster -- the two '
            'foreign keys above are the enforcement and are unaffected.';
        RETURN;
    END IF;

    -- `plan_purge` breaks the version -> predecessor cycle with
    --     UPDATE app.context_relationship_versions SET supersedes_version_id = NULL
    -- and PostgreSQL checks the privilege BEFORE the trigger. 317's
    -- table-level `REVOKE UPDATE` stands: nothing else on this row becomes
    -- writable, and `has_table_privilege(..., 'UPDATE')` stays FALSE, which is
    -- what the `_UPDATE_FREE_TABLES` pin reads.
    EXECUTE 'GRANT UPDATE (supersedes_version_id) '
            'ON app.context_relationship_versions TO connector';
END
$grants$;

COMMIT;
