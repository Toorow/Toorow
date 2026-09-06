-- A node version sits in the SCOPE its node sits in.
--
-- THE DEFECT, MEASURED 2026-08-31 (AI-328). `master_data.create_node_version`
-- wrote `project_id` as a literal NULL for every revision it minted. That is the
-- truth for an organization identity -- a Business Domain, a classification, a
-- tracked entity, all `project_id IS NULL` on the node too -- and a falsehood for
-- every PROJECT identity: a Product and an Activity are project nodes, minted by
-- `entity_reference_import.land_reference_rows` and by
-- `observed_entities.attach_observed_entity`, and their revisions were filed
-- under no project at all.
--
-- What that severed, and it is three things at once:
--
--   * `fk_master_data_versions_node (project_id, node_id)` is MATCH SIMPLE, so a
--     NULL in the leading column means the reference is never checked. The
--     revision named a node nothing verified it belonged to.
--   * `uq_master_data_versions_current_node (project_id, node_id) WHERE
--     status = 'current'` is a unique index, and NULLs are distinct in one. The
--     "exactly one current revision per identity" that migration 143 introduced
--     that index for guaranteed nothing for these rows.
--   * `governance_read_model._CLIENT_OBJECT_INSTANCES` joins the current
--     revision on `v.project_id = n.project_id`. It could never match, so the
--     Governance Overview reported EVERY Product and EVERY Activity as having no
--     version and no base -- while the rename lock ratified on 2026-08-30 is
--     preconditioned on exactly that base. The console sent the null it was
--     given, the authority read the revision that did exist, and the rename was
--     refused `version_conflict` with a reload that could never help.
--
-- The writer is repaired in the same change: it takes the scope from the NODE
-- row it already locks, so there is one answer to "which project is this
-- identity in" and no caller can hold a different one. This migration moves the
-- rows written before that.
--
-- WHY THE GUARD IS LIFTED FOR THE UPDATE, AND PUT BACK IN THE SAME TRANSACTION.
-- `app.protect_master_data_object_version` (migration 140) refuses any change to
-- `project_id` on a revision that has left draft, and that refusal is right: a
-- published revision does not migrate between projects. This is not a migration
-- between projects. It is the FIRST writing of a column that was never written,
-- from NULL to the project its own node has always been in, on rows whose
-- content, number, digest and status are untouched. It is performed here -- once,
-- by a migration -- rather than by weakening the trigger for everyone, which is
-- the shape migration 327 used for the same reason. `DISABLE TRIGGER` takes
-- ACCESS EXCLUSIVE, so no other session can write the table while it is off, and
-- a rollback restores it with the rest of the transaction.
--
-- ONLY NULL MOVES, AND ONLY TO ITS OWN NODE'S PROJECT. A revision that already
-- names a project is not touched, and the value written is read from
-- `app.master_data_nodes` by the node the revision already names -- so an
-- organization identity (node `project_id IS NULL`) keeps its NULL and the FK
-- that was inert becomes enforced with the value that satisfies it.

BEGIN;

ALTER TABLE app.master_data_object_versions
    DISABLE TRIGGER trg_master_data_versions_protect;

UPDATE app.master_data_object_versions AS version
   SET project_id = node.project_id
  FROM app.master_data_nodes AS node
 WHERE version.node_id = node.id
   AND version.project_id IS NULL
   AND node.project_id IS NOT NULL;

ALTER TABLE app.master_data_object_versions
    ENABLE TRIGGER trg_master_data_versions_protect;

-- The state this migration exists to reach, asserted rather than hoped for.
DO $$
DECLARE
    still_orphaned INTEGER;
BEGIN
    SELECT count(*) INTO still_orphaned
      FROM app.master_data_object_versions AS version
      JOIN app.master_data_nodes AS node ON node.id = version.node_id
     WHERE version.project_id IS DISTINCT FROM node.project_id;
    IF still_orphaned > 0 THEN
        RAISE EXCEPTION
            '% node revisions still sit in a scope their node does not',
            still_orphaned
            USING ERRCODE = '23503';
    END IF;

    -- And the guard is back on. A later edit that drops the re-enable would
    -- leave published revisions' scope writable forever, silently.
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
         WHERE tgrelid = 'app.master_data_object_versions'::regclass
           AND tgname = 'trg_master_data_versions_protect'
           AND tgenabled = 'O'
    ) THEN
        RAISE EXCEPTION
            'trg_master_data_versions_protect was left disabled by the rescoping'
            USING ERRCODE = '23000';
    END IF;
END $$;

COMMIT;
