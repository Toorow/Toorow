-- 260 -- one published Semantic View relationship approves one exact source pair.
--
-- The MDM key is reusable across the Project, but approval to cross two Outputs
-- is not global permission for every Datastream implementing that identity.
-- Existing relationships remain readable; their NULL endpoints make them
-- unavailable to the Epic-66 match compiler until a new version declares the
-- exact pair.

BEGIN;

ALTER TABLE app.semantic_view_version_relationships
    ADD COLUMN IF NOT EXISTS left_datastream_id TEXT,
    ADD COLUMN IF NOT EXISTS right_datastream_id TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_datastreams_id_project
    ON app.datastreams (id, project_id);

ALTER TABLE app.semantic_view_version_relationships
    DROP CONSTRAINT IF EXISTS ck_semantic_relationship_datastream_pair;
ALTER TABLE app.semantic_view_version_relationships
    ADD CONSTRAINT ck_semantic_relationship_datastream_pair CHECK (
        (left_datastream_id IS NULL AND right_datastream_id IS NULL)
        OR (
            left_datastream_id IS NOT NULL
            AND right_datastream_id IS NOT NULL
            AND left_datastream_id <> right_datastream_id
        )
    );

ALTER TABLE app.semantic_view_version_relationships
    DROP CONSTRAINT IF EXISTS fk_semantic_relationship_left_datastream;
ALTER TABLE app.semantic_view_version_relationships
    ADD CONSTRAINT fk_semantic_relationship_left_datastream
    FOREIGN KEY (left_datastream_id, project_id)
    REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT;

ALTER TABLE app.semantic_view_version_relationships
    DROP CONSTRAINT IF EXISTS fk_semantic_relationship_right_datastream;
ALTER TABLE app.semantic_view_version_relationships
    ADD CONSTRAINT fk_semantic_relationship_right_datastream
    FOREIGN KEY (right_datastream_id, project_id)
    REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT;

CREATE INDEX IF NOT EXISTS idx_semantic_relationship_datastream_pair
    ON app.semantic_view_version_relationships
       (project_id, left_datastream_id, right_datastream_id)
    WHERE left_datastream_id IS NOT NULL;

COMMENT ON COLUMN app.semantic_view_version_relationships.left_datastream_id IS
    'First exact Datastream this immutable relationship approves. NULL is historical.';
COMMENT ON COLUMN app.semantic_view_version_relationships.right_datastream_id IS
    'Second exact Datastream this immutable relationship approves. NULL is historical.';

COMMIT;
