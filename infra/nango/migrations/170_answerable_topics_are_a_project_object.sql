-- Story 52.1: the Answerable Topic catalog stops being a Python list.
--
-- WHY THIS EXISTS. `server/core/cards.py:224` declares `CARD_TEMPLATES`, a
-- module-level list of nine business questions. Every project on the platform
-- reads the same nine, and no project can add, reword or retire one without a
-- deploy. `docs/product-architecture/glossary.md:223-224` names that exactly:
-- "the nine topics are a platform-wide hardcode (CARD_TEMPLATES), which the
-- per-project preference rule forbids: defaults yes, hardcodes no".
--
-- WHY THE TABLES ARE NAMED `answerable_topic*` AND NOT `topics`. `app.context_topics`
-- (migration 108) already owns the noun "topic" in this schema -- it is the Context
-- Hub's knowledge entry, written by `server/core/context_store.py`. The glossary's
-- opening rule is one noun, one object; the ratified noun for THIS object is
-- "Answerable Topic" (glossary.md:205). Two objects sharing `topics` would be the
-- collision that rule exists to prevent, and it would be found later, in a join.
--
-- WHY THESE TABLES ARE EMPTY, AND STAY EMPTY. There is no INSERT below, and that
-- is the design, not an omission. The nine questions remain the platform DEFAULT
-- SET in `server/core/cards.py`; a project that has configured nothing resolves to
-- them and stores no row. Seeding nine rows per project would:
--   * fabricate the content of a screen the operator never authored, which this
--     repository has already paid for once (demo seeds in migrations 095/096);
--   * destroy the only distinction that matters here -- an inherited default is
--     not a project decision. `origin` carries that distinction explicitly, on
--     the doctrine migration 152 wrote for `app.project_preferences`: "the honest
--     fallback for nobody said anything is `default`".
--
-- WHAT IS IMMUTABLE, and why it is enforced here rather than in Python. A topic
-- VERSION is the wording a project committed to. Rewording appends the next
-- version with a `predecessor_version_id`; nothing relabels a prior one. That is
-- a property no UI can be trusted to preserve, so the trigger below refuses
-- UPDATE and DELETE outright -- the same shape, and the same RGPD escape hatch,
-- as `app.reject_analytical_evidence_mutation` in migration 151.
--
-- RETIRING IS NOT DELETING. `lifecycle_state = 'retired'` removes a topic from
-- the resolved catalog and keeps the row. What a project stopped answering is
-- inventory (docs/project-context.md, rule 9), not litter.
--
-- WHAT THIS IS NOT. No query binding (Story 52.2), no knowledge binding
-- (Story 52.3 -- the only open design question of the epic, and it is not settled
-- by writing a column here), no answer contract and no composition of its own
-- (Story 52.4). A project-authored topic names an existing registered composition
-- through `base_template_id`, because a widget bundle is built and shipped
-- (AD-11) and cannot be minted from a form.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The stable head. The only mutable row here: `current_version_id` advances
--    in the same transaction as the version insert, and `lifecycle_state` moves
--    between exactly two values.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.answerable_topics (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    -- Stable, human-meaningful key inside a project. It is what a caller passes
    -- as `template=` today, so a project-authored topic is addressable exactly
    -- like a default one and no caller learns a second addressing scheme.
    topic_key           TEXT NOT NULL,
    lifecycle_state     TEXT NOT NULL DEFAULT 'active',
    -- The registered composition this topic renders through. NULL is not allowed:
    -- a topic that renders nothing is not an answer, and Story 52.4 owns the day
    -- a project can author a composition of its own.
    base_template_id    TEXT NOT NULL,
    origin              TEXT NOT NULL DEFAULT 'project',
    current_version_id  TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_answerable_topics_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_answerable_topics_key UNIQUE (org_id, project_id, topic_key),
    CONSTRAINT fk_answerable_topics_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_answerable_topics_lifecycle
        CHECK (lifecycle_state IN ('active', 'retired')),
    -- `default` exists so a row can record "this project accepted the platform
    -- default deliberately" the day a surface needs to say it. It is never
    -- written by seeding: see the header.
    CONSTRAINT ck_answerable_topics_origin
        CHECK (origin IN ('project', 'default')),
    CONSTRAINT ck_answerable_topics_key_shape
        CHECK (topic_key ~ '^[a-z0-9][a-z0-9_-]{0,63}$')
);

CREATE INDEX IF NOT EXISTS idx_answerable_topics_project
    ON app.answerable_topics (project_id, lifecycle_state, created_at DESC);

-- ---------------------------------------------------------------------------
-- 2. The immutable versions. Every field a catalog entry exposes is pinned here,
--    so reading version N reproduces exactly what the catalog said at version N.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.answerable_topic_versions (
    id                      TEXT PRIMARY KEY,
    answerable_topic_id     TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    version_number          INTEGER NOT NULL,
    title                   TEXT NOT NULL,
    answers_question        TEXT NOT NULL,
    -- `kpi` (fact-backed, suggestible) or `context` (app-level inventory,
    -- explicit-only). Mirrors CARD_KIND_KPI / CARD_KIND_CONTEXT in cards.py; the
    -- CHECK is what stops a third kind appearing in storage before it exists in
    -- the selection contract.
    kind                    TEXT NOT NULL DEFAULT 'kpi',
    fallback_rank           INTEGER NOT NULL DEFAULT 0,
    required_metrics        JSONB NOT NULL DEFAULT '[]'::jsonb,
    required_dimensions     JSONB NOT NULL DEFAULT '[]'::jsonb,
    optional_metrics        JSONB NOT NULL DEFAULT '[]'::jsonb,
    optional_dimensions     JSONB NOT NULL DEFAULT '[]'::jsonb,
    base_template_id        TEXT NOT NULL,
    content_hash            TEXT NOT NULL,
    predecessor_version_id  TEXT,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_answerable_topic_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_answerable_topic_versions_number
        UNIQUE (answerable_topic_id, version_number),
    -- Composite, never a bare id: a version cannot point at a head in another
    -- Project even if the application layer is wrong.
    CONSTRAINT fk_answerable_topic_versions_head
        FOREIGN KEY (answerable_topic_id, org_id, project_id)
        REFERENCES app.answerable_topics (id, org_id, project_id) ON DELETE CASCADE,
    CONSTRAINT fk_answerable_topic_versions_predecessor
        FOREIGN KEY (predecessor_version_id) REFERENCES app.answerable_topic_versions (id),
    CONSTRAINT ck_answerable_topic_versions_kind
        CHECK (kind IN ('kpi', 'context')),
    CONSTRAINT ck_answerable_topic_versions_number
        CHECK (version_number >= 1),
    CONSTRAINT ck_answerable_topic_versions_title
        CHECK (char_length(title) BETWEEN 1 AND 200),
    CONSTRAINT ck_answerable_topic_versions_question
        CHECK (char_length(answers_question) BETWEEN 1 AND 500),
    CONSTRAINT ck_answerable_topic_versions_arrays
        CHECK (
            jsonb_typeof(required_metrics) = 'array'
            AND jsonb_typeof(required_dimensions) = 'array'
            AND jsonb_typeof(optional_metrics) = 'array'
            AND jsonb_typeof(optional_dimensions) = 'array'
        )
);

CREATE INDEX IF NOT EXISTS idx_answerable_topic_versions_head
    ON app.answerable_topic_versions (answerable_topic_id, version_number DESC);

ALTER TABLE app.answerable_topics
    DROP CONSTRAINT IF EXISTS fk_answerable_topics_current_version;
ALTER TABLE app.answerable_topics
    ADD CONSTRAINT fk_answerable_topics_current_version
    FOREIGN KEY (current_version_id) REFERENCES app.answerable_topic_versions (id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 3. Immutability, enforced where a UI cannot undo it.
--
--    The RGPD escape hatch is the same `app.rgpd_erasure` setting migrations
--    098/099/150/151 use: erasure is the only legitimate reason these rows
--    disappear, and it is explicit rather than implicit.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_answerable_topic_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'an Answerable Topic version is immutable: rewording appends the next version'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_answerable_topic_versions_immutable
    ON app.answerable_topic_versions;
CREATE TRIGGER trg_answerable_topic_versions_immutable
BEFORE UPDATE OR DELETE ON app.answerable_topic_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_answerable_topic_version_mutation();

-- ---------------------------------------------------------------------------
-- 4. The Epic-36 access floor. The application check still runs first; this is
--    the floor under it, so a query written one day without its scope predicate
--    is refused by the database instead of returning another organization's rows.
-- ---------------------------------------------------------------------------
ALTER TABLE app.answerable_topics ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.answerable_topics FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS answerable_topics_strict ON app.answerable_topics;
CREATE POLICY answerable_topics_strict ON app.answerable_topics
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.answerable_topic_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.answerable_topic_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS answerable_topic_versions_strict ON app.answerable_topic_versions;
CREATE POLICY answerable_topic_versions_strict ON app.answerable_topic_versions
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

COMMIT;
