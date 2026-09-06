-- Story 52.3: an Answerable Topic declares which governed knowledge it may read.
--
-- WHY THIS EXISTS. Until now a topic bound metrics, dimensions and (Story 52.2)
-- governed queries -- so it could COMPUTE and could not EXPLAIN. The half that
-- elaborates from the project's own written knowledge is the one that was never
-- wired, and `glossary.md:225-227` names it: "a topic today binds to metrics and
-- dimensions only. The half that elaborates from the project's governed knowledge
-- is the one that was never wired".
--
-- WHAT IT BINDS TO, AND WHY IT IS NOT A GUESS. Epic 52 declared this its ONLY
-- open design question and forbade settling it by writing a schema first. A spike
-- was run against the Context Hub as built on 2026-08-01; the schema below is its
-- conclusion, and the two findings that decide the shape are:
--
--   * a Skill IS a procedure here. `ContextHubRoute.tsx` renders `Procedures` for
--     the `skills-registry` section, and no `app.skills` table exists. So the
--     three candidates the epic listed -- Knowledge Library entry, business path,
--     Skill -- are TWO stores in the delivered system: `app.context_topics` and
--     `app.procedures`. A "skill_id" column would have been a column that could
--     never be filled.
--   * a knowledge version has NO id of its own: the primary keys are
--     `(topic_id, version_number)` and `(procedure_id, version_number)`. So an
--     exact pin is necessarily COMPOSITE, and cannot be one `*_version_id` column
--     the way Story 52.2 pinned a Query Spec version.
--
-- WHY THIS IS NOT `app.mdm_business_links`, so nobody re-proposes the merge.
-- That table already links business taxonomy to eight target types including
-- `topic` and `procedure`, and specializing an existing store rather than building
-- beside it is the standing lesson of Story 37.9 and Epic 40. It cannot serve here
-- for two reasons that are properties of the table, not preferences:
--
--   * its taxonomy side is closed to `business_domain` / `business_classification`
--     (`business_taxonomy.BUSINESS_TAXONOMY_TYPES`), and an Answerable Topic is
--     neither of those;
--   * it stores NO version. It means "this domain is about that object", which is
--     deliberately version-free. Adding a version column to it would change the
--     meaning of every link already written through it.
--
-- WHAT A PIN THAT CANNOT BE READ MEANS. Production holds 41 knowledge version rows
-- and ZERO knowledge heads (measured 2026-08-01) -- append-only versions survived a
-- scrub their heads did not. So a pin CAN legitimately point at a version whose head
-- is gone, and the read path must answer `context_missing` for it. That is not an
-- error path: `context-hub.md:59-60` makes it a completeness criterion that an
-- unavailable context store must never look like an empty one, "so a model reads
-- 'nothing is defined here' and answers from its own priors".
--
-- NO INSERT. A declaration is authored, never seeded.

BEGIN;

CREATE TABLE IF NOT EXISTS app.answerable_topic_knowledge_bindings (
    id                      TEXT PRIMARY KEY,
    answerable_topic_id     TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    knowledge_kind          TEXT NOT NULL,
    -- Exactly ONE of the two pairs below is filled, per `ck_atkb_exactly_one_kind`.
    -- Two nullable pairs rather than one polymorphic pair, because that is what
    -- buys a REAL foreign key to each version table: a single pair could not
    -- reference two tables, and an unenforced pin is how a citation ends up
    -- naming a version that never existed.
    context_topic_id        TEXT,
    context_topic_version   INTEGER,
    procedure_id            TEXT,
    procedure_version       INTEGER,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_atkb_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_atkb_topic
        FOREIGN KEY (answerable_topic_id, org_id, project_id)
        REFERENCES app.answerable_topics (id, org_id, project_id) ON DELETE CASCADE,
    CONSTRAINT fk_atkb_context_topic_version
        FOREIGN KEY (context_topic_id, context_topic_version)
        REFERENCES app.context_topics_versions (topic_id, version_number),
    CONSTRAINT fk_atkb_procedure_version
        FOREIGN KEY (procedure_id, procedure_version)
        REFERENCES app.procedures_versions (procedure_id, version_number),

    CONSTRAINT ck_atkb_kind CHECK (knowledge_kind IN ('topic', 'procedure')),
    -- Exactly one kind, and its pin complete. A half-filled pin would name a
    -- knowledge item without saying which version of it the topic may read.
    CONSTRAINT ck_atkb_exactly_one_kind CHECK (
        (knowledge_kind = 'topic'
            AND context_topic_id IS NOT NULL AND context_topic_version IS NOT NULL
            AND procedure_id IS NULL AND procedure_version IS NULL)
        OR
        (knowledge_kind = 'procedure'
            AND procedure_id IS NOT NULL AND procedure_version IS NOT NULL
            AND context_topic_id IS NULL AND context_topic_version IS NULL)
    ),
    CONSTRAINT ck_atkb_versions_positive CHECK (
        COALESCE(context_topic_version, 1) >= 1 AND COALESCE(procedure_version, 1) >= 1
    ),
    -- The same knowledge version declared twice for one topic is a duplicate, not
    -- an intent. Nulls make UNIQUE permissive, so each kind gets its own index.
    CONSTRAINT uq_atkb_topic_pin
        UNIQUE (answerable_topic_id, knowledge_kind, context_topic_id,
                context_topic_version, procedure_id, procedure_version)
);

CREATE INDEX IF NOT EXISTS idx_atkb_topic
    ON app.answerable_topic_knowledge_bindings (answerable_topic_id, knowledge_kind);

-- A topic may declare that it answers ONLY from governed knowledge. When its pins
-- cannot be read, the honest answer is a stated refusal -- not a card rendered
-- without the half that explains it. The flag lives on the topic head because it
-- is a property of the question, not of one pin.
ALTER TABLE app.answerable_topics
    ADD COLUMN IF NOT EXISTS requires_knowledge BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE app.answerable_topic_knowledge_bindings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.answerable_topic_knowledge_bindings FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS answerable_topic_knowledge_bindings_strict
    ON app.answerable_topic_knowledge_bindings;
CREATE POLICY answerable_topic_knowledge_bindings_strict
    ON app.answerable_topic_knowledge_bindings
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

COMMIT;
