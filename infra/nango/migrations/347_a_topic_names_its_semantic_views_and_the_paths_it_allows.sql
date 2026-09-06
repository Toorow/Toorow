-- Story 75-5: an Answerable Topic names its Semantic Views and the join paths
-- it allows.
--
-- WHY THIS EXISTS. The topic already binds governed queries (migration 172) and
-- governed knowledge (migration 173). It has never bound a Semantic View, so a
-- model handed a topic knows the question and the pre-authored queries and has
-- to GUESS the join the moment the question needs a cross no bound query already
-- performs. Guessing a join is how a fan-out silently multiplies a measure --
-- which is precisely what `semantic_view_version_relationships.fan_out_policy`
-- (migration 142) exists to make unstorable.
--
-- THE THIRD BINDING FAMILY, SHAPED LIKE THE FIRST. Every decision below is
-- migration 172's, taken again rather than re-argued: composite foreign keys so
-- a binding cannot name another Project's object even if the application layer
-- is wrong; an exact version pin whose two most plausible wrong values are
-- refused by a CHECK; the Epic-36 access floor armed on the table; no INSERT,
-- because a binding is authored and never seeded.
--
-- `allowed_paths` HOLDS RELATION IDENTITIES AND NOTHING ELSE. A path is an
-- ordered list of relation NAMES declared by the pinned Semantic View version
-- (`app.semantic_view_version_relationships.name` -- the identity
-- `semantic_model.load_view_relationships` and the change-set export already
-- key a relationship by). The endpoints, the cardinality and the fan-out policy
-- of each relation are DERIVED at read time from those rows. Copying them here
-- would create a second truth able to disagree with the compiler's, and a
-- derivable value is not stored (CLAUDE.md, "ce qui est du code reste du code").
--
-- WHY NO `unbound_at`. Unbinding DELETEs, exactly as `unbind_query` does. The
-- trace of the declaration lives in `app.operations` and `app.audit_log`, which
-- commit in the same transaction as the change through
-- `core.operations.execute_operation`; a second lifecycle column here would be a
-- second, weaker record of the same fact.
--
-- ERASURE. `fk_atvb_topic` is ON DELETE CASCADE and `fk_atvb_view_version` is
-- NO ACTION, so the foreign-key graph `core.org_purge` walks reaches this table
-- through the Semantic View version as well as being cascaded from the topic.

BEGIN;

CREATE TABLE IF NOT EXISTS app.answerable_topic_view_bindings (
    id                          TEXT NOT NULL,
    answerable_topic_id         TEXT NOT NULL,
    org_id                      TEXT NOT NULL,
    project_id                  TEXT NOT NULL,
    semantic_view_id            TEXT NOT NULL,
    semantic_view_version_id    TEXT NOT NULL,
    -- `[{"relation_ids": ["campaign_to_account", ...]}, ...]`. An EMPTY array is
    -- legitimate and says one thing precisely: this topic may read this View and
    -- may cross nothing.
    allowed_paths               JSONB NOT NULL DEFAULT '[]'::jsonb,
    note                        TEXT,
    created_by                  TEXT NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_atvb PRIMARY KEY (id),
    CONSTRAINT uq_atvb_scope UNIQUE (id, org_id, project_id),
    -- One declaration per exact version. Binding the same View twice at two
    -- versions is legitimate (a topic may be migrating); binding the same
    -- version twice is a duplicate, not an intent.
    CONSTRAINT uq_atvb_version UNIQUE (answerable_topic_id, semantic_view_version_id),

    CONSTRAINT fk_atvb_topic
        FOREIGN KEY (answerable_topic_id, org_id, project_id)
        REFERENCES app.answerable_topics (id, org_id, project_id) ON DELETE CASCADE,
    -- Composite, never a bare id. `app.semantic_views` carries no `org_id` -- it
    -- is Project-scoped by construction (migration 142) -- so the Project half of
    -- this binding's scope is what the two keys below enforce, and the org half
    -- is enforced by `fk_atvb_topic` above. Neither key alone is enough; both
    -- together make a cross-Project or cross-org binding unstorable.
    CONSTRAINT fk_atvb_view_head
        FOREIGN KEY (semantic_view_id, project_id)
        REFERENCES app.semantic_views (id, project_id),
    CONSTRAINT fk_atvb_view_version
        FOREIGN KEY (semantic_view_version_id, project_id)
        REFERENCES app.semantic_view_versions (id, project_id),

    -- `latest` is unstorable, said by the storage layer rather than by a
    -- comment -- migration 172's sentence, and it holds for the same reason: a
    -- reference that follows `latest` silently changes what it means.
    CONSTRAINT ck_atvb_pin_is_exact
        CHECK (lower(semantic_view_version_id) NOT IN ('latest', 'current')),
    CONSTRAINT ck_atvb_paths_is_array
        CHECK (jsonb_typeof(allowed_paths) = 'array'),
    CONSTRAINT ck_atvb_note_bounded
        CHECK (note IS NULL OR char_length(note) BETWEEN 1 AND 500)
);

CREATE INDEX IF NOT EXISTS idx_atvb_topic
    ON app.answerable_topic_view_bindings (answerable_topic_id);
CREATE INDEX IF NOT EXISTS idx_atvb_view_version
    ON app.answerable_topic_view_bindings (semantic_view_version_id);
CREATE INDEX IF NOT EXISTS idx_atvb_project
    ON app.answerable_topic_view_bindings (org_id, project_id);

COMMENT ON TABLE app.answerable_topic_view_bindings IS
    'Which Semantic View versions an Answerable Topic may read, and which '
    'governed join paths it may cross. A path names relations the model already '
    'ratified; nothing derivable from those relations is stored here.';
COMMENT ON COLUMN app.answerable_topic_view_bindings.allowed_paths IS
    'Array of {"relation_ids": [...]} -- ordered names of relationships declared '
    'by the pinned Semantic View version. Endpoints, cardinality and fan-out '
    'policy are read from the model, never copied here.';

-- The Epic-36 access floor, identical in shape to migrations 170, 172 and 173.
-- The application check still runs first; this is the floor under it, and it is
-- armed on the connection by `answerable_topics_api.topic_connection`.
ALTER TABLE app.answerable_topic_view_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.answerable_topic_view_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS answerable_topic_view_bindings_strict
    ON app.answerable_topic_view_bindings;
CREATE POLICY answerable_topic_view_bindings_strict
    ON app.answerable_topic_view_bindings
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- Migration 207's ALTER DEFAULT PRIVILEGES already hands SELECT, INSERT, UPDATE,
-- DELETE to `connector` on every future table in `app`, so the GRANT below is
-- declarative and the REVOKE is the sentence (migration 316's finding). A
-- binding is declared or withdrawn -- there is no path that rewrites one, and
-- an `allowed_paths` correction is an unbind followed by a bind, so the audit
-- shows both gestures instead of one silent overwrite.
GRANT SELECT, INSERT, DELETE ON app.answerable_topic_view_bindings TO connector;
REVOKE UPDATE ON app.answerable_topic_view_bindings FROM connector;

COMMIT;
