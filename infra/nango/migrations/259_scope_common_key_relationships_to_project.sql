-- 259 -- a relationship may only pin a common-key version of its own Project.
--
-- Migration 258 added the semantic identity pin, but its single-column FK could
-- accept a valid key version owned by another Project. RLS hides the foreign row
-- from normal reads; it does not make a cross-project foreign key truthful. The
-- Project id below is derived from the immutable Semantic View version and both
-- foreign keys include it, so direct SQL and every adapter share the same rule.

BEGIN;

ALTER TABLE app.semantic_view_version_relationships
    ADD COLUMN IF NOT EXISTS project_id TEXT;

UPDATE app.semantic_view_version_relationships AS relationship
   SET project_id = version.project_id
  FROM app.semantic_view_versions AS version
 WHERE version.id = relationship.view_version_id
   AND relationship.project_id IS NULL;

ALTER TABLE app.semantic_view_version_relationships
    ALTER COLUMN project_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uq_semantic_view_versions_id_project
    ON app.semantic_view_versions (id, project_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_common_key_versions_id_project
    ON app.mdm_common_key_versions (id, project_id);

ALTER TABLE app.semantic_view_version_relationships
    DROP CONSTRAINT IF EXISTS fk_semantic_view_relationship_version_scope;
ALTER TABLE app.semantic_view_version_relationships
    ADD CONSTRAINT fk_semantic_view_relationship_version_scope
    FOREIGN KEY (view_version_id, project_id)
    REFERENCES app.semantic_view_versions (id, project_id) ON DELETE CASCADE;

ALTER TABLE app.semantic_view_version_relationships
    DROP CONSTRAINT IF EXISTS fk_semantic_view_relationship_common_key;
ALTER TABLE app.semantic_view_version_relationships
    ADD CONSTRAINT fk_semantic_view_relationship_common_key
    FOREIGN KEY (mdm_common_key_version_id, project_id)
    REFERENCES app.mdm_common_key_versions (id, project_id) ON DELETE RESTRICT;

CREATE OR REPLACE FUNCTION app.assign_semantic_relationship_project()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, app
AS $$
DECLARE
    owner_project TEXT;
BEGIN
    SELECT version.project_id
      INTO owner_project
      FROM app.semantic_view_versions AS version
     WHERE version.id = NEW.view_version_id;
    IF owner_project IS NULL THEN
        RAISE EXCEPTION 'semantic view version is unavailable' USING ERRCODE = '23503';
    END IF;
    IF NEW.project_id IS NOT NULL AND NEW.project_id <> owner_project THEN
        RAISE EXCEPTION 'semantic relationship project mismatch' USING ERRCODE = '23503';
    END IF;
    NEW.project_id := owner_project;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_semantic_relationship_project
    ON app.semantic_view_version_relationships;
CREATE TRIGGER trg_semantic_relationship_project
    BEFORE INSERT OR UPDATE OF view_version_id, project_id
    ON app.semantic_view_version_relationships
    FOR EACH ROW EXECUTE FUNCTION app.assign_semantic_relationship_project();

COMMENT ON COLUMN app.semantic_view_version_relationships.project_id IS
    'Server-derived Project scope shared by the Semantic View version and the exact '
    'MDM common-key version. Never supplied as analytical authority by a client.';

COMMIT;
