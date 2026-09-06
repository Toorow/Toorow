-- Story 52.2: an Answerable Topic names the governed queries that answer it.
--
-- WHY THIS EXISTS. `glossary.md:205-209` defines the object as one "whose answer
-- legitimately takes ONE OR SEVERAL queries and approaches rather than mapping to
-- a single card". Until now a topic bound metrics and dimensions only -- so it
-- could describe what it needed, and could not name what actually answers it.
-- `glossary.md:219` records the consequence: verified queries, the question <->
-- vetted query pairing, "Not delivered".
--
-- The house it binds to already exists and is NOT rebuilt here:
-- `app.query_spec_versions` (migration 151, Story 50.1) is Project-scoped,
-- versioned and immutable, and it exposes `uq_query_spec_versions_scope
-- (id, org_id, project_id)` precisely so a reference like this one can be a
-- COMPOSITE foreign key rather than a bare id.
--
-- THE PIN IS EXACT, AND THAT IS ENFORCED, NOT INTENDED. Story 52.2's acceptance
-- says `latest` is unstorable. A foreign key already refuses anything that is not
-- a real version id, and the CHECK below refuses the two words that a caller
-- would most plausibly try to store as an intent ("give me whatever is current").
-- The same two words are refused in the URL layer by
-- `navigation.ts EXPLORE_QUERY.forbiddenValues`, for the same reason: a reference
-- that follows `latest` silently changes what it means.
--
-- `role` IS FREE TEXT, DELIBERATELY. The epic says each binding carries a role and
-- ratifies no vocabulary of roles. Shipping an enumeration here would invent a
-- taxonomy and force every project into it -- the same mistake migration 104
-- avoided for `binding_kind`. When a vocabulary is ratified, a CHECK can be added
-- forward; the reverse is not true.
--
-- `position` IS UNIQUE PER TOPIC. The order of the queries that answer a question
-- is the project's decision, not an accident of insertion time.
--
-- NO INSERT. A binding is authored, never seeded. And on the day this is applied
-- production holds ZERO Query Specs, so this table is empty by fact as well as by
-- design -- which is why Story 52.2 requires the two absences ("the store does not
-- exist" and "this project bound nothing") to stay distinguishable in the read
-- surface.

BEGIN;

CREATE TABLE IF NOT EXISTS app.answerable_topic_query_bindings (
    id                      TEXT PRIMARY KEY,
    answerable_topic_id     TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    query_spec_id           TEXT NOT NULL,
    query_spec_version_id   TEXT NOT NULL,
    role                    TEXT NOT NULL,
    position                INTEGER NOT NULL,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_atqb_scope UNIQUE (id, org_id, project_id),
    -- The project's declared order, and no two queries in the same slot.
    CONSTRAINT uq_atqb_position UNIQUE (answerable_topic_id, position),
    -- Binding the same exact version twice under two roles is legitimate (a query
    -- can be both the headline and a component); binding it twice under the SAME
    -- role is a duplicate, not an intent.
    CONSTRAINT uq_atqb_version_role UNIQUE (answerable_topic_id, query_spec_version_id, role),

    CONSTRAINT fk_atqb_topic
        FOREIGN KEY (answerable_topic_id, org_id, project_id)
        REFERENCES app.answerable_topics (id, org_id, project_id) ON DELETE CASCADE,
    -- Composite, never a bare id: a binding cannot point at another Project's
    -- query even if the application layer is wrong.
    CONSTRAINT fk_atqb_query_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_atqb_query_head
        FOREIGN KEY (query_spec_id, org_id, project_id)
        REFERENCES app.query_specs (id, org_id, project_id),

    CONSTRAINT ck_atqb_role_bounded
        CHECK (char_length(role) BETWEEN 1 AND 60),
    CONSTRAINT ck_atqb_position_positive CHECK (position >= 1),
    -- `latest` is unstorable, said by the storage layer rather than by a comment.
    CONSTRAINT ck_atqb_pin_is_exact
        CHECK (lower(query_spec_version_id) NOT IN ('latest', 'current'))
);

CREATE INDEX IF NOT EXISTS idx_atqb_topic
    ON app.answerable_topic_query_bindings (answerable_topic_id, position);
CREATE INDEX IF NOT EXISTS idx_atqb_query_version
    ON app.answerable_topic_query_bindings (query_spec_version_id);

-- The Epic-36 access floor, identical in shape to migration 170. The application
-- check still runs first; this is the floor under it.
ALTER TABLE app.answerable_topic_query_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.answerable_topic_query_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS answerable_topic_query_bindings_strict
    ON app.answerable_topic_query_bindings;
CREATE POLICY answerable_topic_query_bindings_strict
    ON app.answerable_topic_query_bindings
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

COMMIT;
