-- Epic 51: the product evaluation evidence model -- Golden Questions, Evaluation
-- Runs, dimension verdicts, observed cohorts and reviewed feedback.
--
-- WHY THIS EXISTS. `docs/product-architecture/analyze-and-test.md` ratifies Test
-- as the product evaluation control plane: it owns versioned evaluation
-- definitions, immutable runs, explicitly approved baselines, per-dimension
-- verdicts and feedback review. Before this migration the repository had only
-- the Epic 14 benchmark rows -- `app.golden_questions` (a flat
-- question/topic/expected_citations record) and `app.eval_runs` (one
-- compensating `precision_pct`). Neither carries a Business Domain, a Semantic
-- View version, a tolerance, a provenance requirement or an expected AI Path,
-- so neither can be the product object the ratified contract describes.
--
-- WHY IT IS ONE MIGRATION AND NOT FIVE. Stories 51.1 to 51.5 describe one graph:
-- a Golden Question version is the subject a run case pins, a case is what a
-- dimension verdict and a path comparison hang off, a cohort member proposes a
-- Golden Question, and a feedback annotation classifies one of the same six
-- dimensions. Five concurrent migrations over that graph produce foreign keys
-- whose two sides never meet -- the exact defect Story 47.5 already cost this
-- repository. The five stories remain the owners of their code; the schema is
-- landed once so every edge is enforceable on the day it is written.
--
-- THE IDENTITY CHAINS this schema encodes, and refuses to let drift:
--
--   Business Domain version ┐
--   Semantic View version   ┼-> Golden Question / version -> reference paths
--                           ┘                    │
--                                                ├-> Evaluation Run case -> dimension verdicts
--                                                │                      └-> path comparison
--                                                └-> Gate Decision (via comparison)
--   AI Path (observed) -> Observed Cohort member -> Golden Question proposal
--   Result (immutable) -> Feedback annotation -> review versions
--
-- Every arrow is a COMPOSITE Project-scoped foreign key -- `(id, org_id,
-- project_id)`, never a bare id -- so a row cannot point across Projects even
-- when the application layer is wrong. Migration 151 established the pattern at
-- lines 87-98; this file follows it without exception.
--
-- WHAT IS IMMUTABLE, and why the database and not Python enforces it:
--
--   * a Golden Question VERSION is what "correct" meant at a moment. Editing it
--     would silently re-judge every past run against a definition that no longer
--     exists. Revision creates the next version with a `predecessor_version_id`.
--   * an EVALUATION RUN, its cases, its verdicts, its path comparisons, its
--     comparisons and its Gate Decisions are the evidence itself. `recording ->
--     finalized` is the only transition, exactly as `app.ai_paths` implements it
--     (migration 150).
--   * a BASELINE is an approval by a named actor, for one named run profile,
--     with a reason. No code path advances it: replacing one INSERTS a new row
--     and sets `superseded_by_baseline_id` on the previous one. That single
--     column change is the only UPDATE the trigger allows.
--   * an OBSERVED COHORT and its membership are frozen at `resolved_at`. A later
--     execution matching the same filters does not join a historical cohort;
--     re-resolving mints a new cohort with its own denominator.
--   * a FEEDBACK ANNOTATION and each REVIEW VERSION are insert-once. A human
--     wrote a judgement at a moment; rewriting it destroys the only evidence
--     that the judgement was ever different.
--
-- THREE ABSENCES ARE DECLARED HERE RATHER THAN FILLED.
--
--   1. The rendered-artifact object of Stories 50.4 / 50.5 / 50.7 does not
--      exist -- the rendering stack is not even installed. Four columns declare
--      the pin (`golden_question_versions.expected_render_ref`,
--      `evaluation_run_cases.render_ref`, `observed_cohort_members.render_ref`,
--      `feedback_annotations.render_ref`) and a CHECK holds each of them NULL,
--      so no session can quietly write a fabricated identifier. A NULL pin
--      yields the verdict `unverifiable` -- never `pass`, never `fail`. THIS
--      MIGRATION CREATES NO SUCH TABLE AND NO IDENTIFIER FORMAT FOR IT. Its
--      successor migration, owned by Story 50.4, drops the CHECKs and adds the
--      composite foreign keys. A migration is corrected by the next one, never
--      re-edited.
--   2. The evaluated MCP App behaviour of Story 50.6 does not exist. The
--      dimension is declared in the verdict vocabulary and a CHECK makes `pass`
--      unreachable on it while its pin is NULL.
--   3. A governed Golden Question SET object is not delivered by Story 51.1. A
--      run therefore pins its subject twice, honestly: each case names an exact
--      `golden_question_version_id` by composite foreign key, and the run
--      carries `question_set_fingerprint`, a sha256 DERIVED from that exact set.
--      The fingerprint is a comparison material, not an owner, and it names no
--      new object.
--
-- WHAT THIS IS NOT.
--
--   * There is NO aggregate score column anywhere in this file, and that absence
--     is load-bearing. `analyze-and-test.md:336-337` forbids an overall
--     percentage that hides a critical failure or pays for a correctness
--     regression with better feedback. A nullable `score` column is how such a
--     figure gets added later without anyone deciding to add it.
--   * There is NO column holding SQL text. `analyze-and-test.md:263-264`: "one
--     implementation-specific SQL string is never the definition of
--     correctness". Correctness is the typed assertion array; a reference query
--     is an approach, and a question may declare none, one or several.
--   * No table here stores an assessment of an observed AI Path. Migration 150
--     states the reason in its own header: a stored verdict makes a historical
--     path re-judge itself under today's policy. Assessment is derived at read
--     time from the snapshot pinned before the execution.
--   * Nothing here writes to a Governance or Context Hub table. Test emits a
--     Gate Decision that the owning workflow reads before its own transition
--     (`analyze-and-test.md:368-371`).
--   * `server/tests/evals/corpus.yaml` is test code. It is not read, imported,
--     mirrored or seeded by anything in this schema.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. One shared refusal: an unqualified `latest` is not a version pin.
--
--    `analyze-and-test.md:230` requires the Context Version Set to be "never an
--    unqualified `latest`". This is a function used by CHECK constraints rather
--    than a service convention, because the service is not the only writer a
--    repository ever grows. The console already encodes the same refusal for the
--    Explore address (`navigation.ts` `forbiddenValues`); this is its database
--    counterpart.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.is_exact_version_pin(candidate TEXT)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
    SELECT candidate IS NOT NULL
       AND btrim(candidate) <> ''
       AND lower(btrim(candidate)) NOT IN ('latest', 'current', 'head');
$$;

COMMENT ON FUNCTION app.is_exact_version_pin(TEXT) IS
    'Epic 51: TRUE only for an exact stored version identity. Refuses NULL, blank, latest, current and head.';

-- ---------------------------------------------------------------------------
-- 1. AI-81 point (1): one canonical name per concept.
--
--    `app.golden_questions` (migration 096) is the Epic 14 offline benchmark
--    record. Jean's recorded decision (2026-07-20) is that the harness is KEPT
--    but must stop being called "golden questions", because the ratified product
--    object claims that noun. This is a rename, not a deletion: the rows, the
--    seeds deleted by migration 120, the `expected_business_routes` column added
--    by migration 130 and the `app.eval_business_path_results.question_id`
--    foreign key all follow the table (PostgreSQL rewrites the dependency).
--
--    The constraints and indexes are renamed with it because their names are
--    schema-scoped: leaving `golden_questions_pkey` behind would collide with
--    the product table created below.
--
--    The ONE production reader is `server/core/admin_api.py` inside
--    `_list_golden_questions`. That file belongs to another session; the
--    single-token edit `FROM app.golden_questions` -> `FROM
--    app.eval_benchmark_questions` MUST land with this migration or the legacy
--    `/api/eval/golden-questions` route breaks.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF to_regclass('app.eval_benchmark_questions') IS NULL
       AND to_regclass('app.golden_questions') IS NOT NULL THEN

        ALTER TABLE app.golden_questions RENAME TO eval_benchmark_questions;

        ALTER TABLE app.eval_benchmark_questions
            RENAME CONSTRAINT golden_questions_pkey TO pk_eval_benchmark_questions;
        ALTER TABLE app.eval_benchmark_questions
            RENAME CONSTRAINT uq_golden_questions_project_question
            TO uq_eval_benchmark_questions_project_question;

        IF EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conrelid = 'app.eval_benchmark_questions'::regclass
              AND conname = 'golden_questions_expected_business_routes_check'
        ) THEN
            ALTER TABLE app.eval_benchmark_questions
                RENAME CONSTRAINT golden_questions_expected_business_routes_check
                TO ck_eval_benchmark_questions_business_routes;
        END IF;

        IF to_regclass('app.idx_golden_questions_project') IS NOT NULL THEN
            ALTER INDEX app.idx_golden_questions_project
                RENAME TO idx_eval_benchmark_questions_project;
        END IF;
    END IF;
END $$;

COMMENT ON TABLE app.eval_benchmark_questions IS
    'Epic 14 offline benchmark record, renamed by Epic 51 (AI-81 point 1). NOT the product Golden Question: it carries no Business Domain, Semantic View version, tolerance, provenance requirement or expected AI Path.';

-- ---------------------------------------------------------------------------
-- 2. Story 51.1: the product Golden Question -- a stable head and immutable
--    versions.
--
--    Field placement is a decision, so it is stated here rather than left to be
--    re-derived. On the VERSION: everything that changes what "correct" means --
--    the two governed pins, the question, the typed assertions, the provenance
--    requirements, the expected path pattern, the result type, the capability
--    tags and the severity. A run must be able to say which severity it judged
--    against. On the HEAD: stewardship only -- `owner` and `lifecycle` -- which
--    change without changing the meaning of any past verdict.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.golden_questions (
    id                  TEXT        NOT NULL,
    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    title               TEXT        NOT NULL,
    owner               TEXT        NOT NULL,
    lifecycle           TEXT        NOT NULL DEFAULT 'draft',
    current_version_id  TEXT,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_golden_questions PRIMARY KEY (id),
    CONSTRAINT ck_golden_questions_id CHECK (id ~ '^gq_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_golden_questions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_golden_questions_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_golden_questions_title
        CHECK (length(btrim(title)) BETWEEN 1 AND 200),
    CONSTRAINT ck_golden_questions_owner
        CHECK (length(btrim(owner)) BETWEEN 1 AND 200),
    CONSTRAINT ck_golden_questions_lifecycle
        CHECK (lifecycle IN ('draft', 'active', 'deprecated', 'archived'))
);

CREATE INDEX IF NOT EXISTS idx_golden_questions_project
    ON app.golden_questions (project_id, lifecycle, created_at DESC);

COMMENT ON TABLE app.golden_questions IS
    'Story 51.1: the product Golden Question head. Stewardship only; every field that defines correctness lives on the immutable version.';

CREATE TABLE IF NOT EXISTS app.golden_question_versions (
    id                              TEXT        NOT NULL,
    golden_question_id              TEXT        NOT NULL,
    org_id                          TEXT        NOT NULL,
    project_id                      TEXT        NOT NULL,
    version_number                  INTEGER     NOT NULL,

    -- Governed pin 1: the Business Domain. Domains are ORG-scoped while Golden
    -- Questions are PROJECT-scoped, so the version stores `org_id` and proves
    -- both halves: the exact (domain, version) pair exists, AND that domain
    -- belongs to this organization.
    business_domain_id              TEXT        NOT NULL,
    business_domain_version_number  INTEGER     NOT NULL,
    business_classification_id      TEXT,

    -- Governed pin 2: the Semantic View version, and whether this question
    -- exercises it as the baseline or as the candidate.
    semantic_view_id                TEXT        NOT NULL,
    semantic_view_version_id        TEXT        NOT NULL,
    semantic_view_version_role      TEXT        NOT NULL,

    question                        TEXT        NOT NULL,
    -- The declared fixed range / `as_of` / timezone boundary, or `{}` when the
    -- question declares none. An undeclared boundary is not a missing one.
    time_boundary                   JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Typed assertions. Structure is enforced by the trigger below, which names
    -- the offending index rather than refusing the whole document anonymously.
    expected_result                 JSONB       NOT NULL,
    required_provenance             JSONB       NOT NULL,
    expected_ai_path                JSONB       NOT NULL,

    result_type                     TEXT        NOT NULL,
    capability_tags                 TEXT[]      NOT NULL,
    severity                        TEXT        NOT NULL,

    -- Declared, and held NULL. See absence (1) in the header. No foreign key,
    -- no format check, no minting path: a NULL here yields `unverifiable`.
    expected_render_ref             TEXT,

    content_hash                    TEXT        NOT NULL,
    predecessor_version_id          TEXT,
    created_by                      TEXT        NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_golden_question_versions PRIMARY KEY (id),
    CONSTRAINT ck_golden_question_versions_id
        CHECK (id ~ '^gqv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_golden_question_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_golden_question_versions_number
        UNIQUE (golden_question_id, version_number),

    CONSTRAINT fk_golden_question_versions_head
        FOREIGN KEY (golden_question_id, org_id, project_id)
        REFERENCES app.golden_questions (id, org_id, project_id),
    CONSTRAINT fk_golden_question_versions_domain_version
        FOREIGN KEY (business_domain_id, business_domain_version_number)
        REFERENCES app.mdm_business_domain_versions (domain_id, version_number),
    CONSTRAINT fk_golden_question_versions_domain_org
        FOREIGN KEY (business_domain_id, org_id)
        REFERENCES app.mdm_business_domains (id, org_id),
    CONSTRAINT fk_golden_question_versions_classification
        FOREIGN KEY (business_classification_id, org_id, business_domain_id)
        REFERENCES app.mdm_business_classifications (id, org_id, domain_id),
    CONSTRAINT fk_golden_question_versions_semantic_scope
        FOREIGN KEY (semantic_view_version_id, semantic_view_id, project_id)
        REFERENCES app.semantic_view_versions (id, view_id, project_id),
    CONSTRAINT fk_golden_question_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.golden_question_versions (id, org_id, project_id),

    CONSTRAINT ck_golden_question_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_golden_question_versions_domain_version
        CHECK (business_domain_version_number >= 1),
    CONSTRAINT ck_golden_question_versions_view_role
        CHECK (semantic_view_version_role IN ('baseline', 'candidate')),
    CONSTRAINT ck_golden_question_versions_question
        CHECK (length(btrim(question)) BETWEEN 1 AND 4000),
    CONSTRAINT ck_golden_question_versions_time_boundary
        CHECK (jsonb_typeof(time_boundary) = 'object'),
    CONSTRAINT ck_golden_question_versions_expected_result
        CHECK (jsonb_typeof(expected_result) = 'array'
               AND jsonb_array_length(expected_result) >= 1),
    CONSTRAINT ck_golden_question_versions_provenance
        CHECK (jsonb_typeof(required_provenance) = 'array'
               AND jsonb_array_length(required_provenance) >= 1),
    CONSTRAINT ck_golden_question_versions_expected_path
        CHECK (jsonb_typeof(expected_ai_path) = 'object'),
    CONSTRAINT ck_golden_question_versions_result_type
        CHECK (result_type IN ('scalar', 'series', 'breakdown', 'comparison',
                               'table', 'narrative', 'refusal')),
    CONSTRAINT ck_golden_question_versions_capabilities
        CHECK (array_length(capability_tags, 1) >= 1),
    CONSTRAINT ck_golden_question_versions_severity
        CHECK (severity IN ('critical', 'major', 'minor')),
    -- Absence (1): declared, never populated by any story of Epic 51.
    CONSTRAINT ck_golden_question_versions_render_unpinned
        CHECK (expected_render_ref IS NULL),
    CONSTRAINT ck_golden_question_versions_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- A revision that loses its lineage is indistinguishable from a fresh
    -- intent, and a run that cited the old one could no longer be replayed.
    CONSTRAINT ck_golden_question_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_golden_question_versions_head
    ON app.golden_question_versions (golden_question_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_golden_question_versions_domain
    ON app.golden_question_versions (project_id, business_domain_id,
                                     business_domain_version_number);
CREATE INDEX IF NOT EXISTS idx_golden_question_versions_view
    ON app.golden_question_versions (semantic_view_version_id);

COMMENT ON COLUMN app.golden_question_versions.expected_render_ref IS
    'Declared pin held NULL: its owner (Stories 50.4 / 50.5 / 50.7) is not delivered. A NULL pin reports Unverifiable, never pass and never fail. The successor migration owned by Story 50.4 drops ck_golden_question_versions_render_unpinned and adds the composite foreign key.';

ALTER TABLE app.golden_questions
    DROP CONSTRAINT IF EXISTS fk_golden_questions_current_version;
ALTER TABLE app.golden_questions
    ADD CONSTRAINT fk_golden_questions_current_version
    FOREIGN KEY (current_version_id, org_id, project_id)
    REFERENCES app.golden_question_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- 0..N reference execution paths. Zero is valid: correctness is the typed
-- assertion array, not a reference query. Two or more is valid and is the
-- multi-approach question the ratified contract describes. NO COLUMN HERE, OR
-- ANYWHERE IN THIS FILE, STORES SQL TEXT.
CREATE TABLE IF NOT EXISTS app.golden_question_reference_paths (
    id                          TEXT        NOT NULL,
    golden_question_version_id  TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    ordinal                     INTEGER     NOT NULL,
    query_spec_version_id       TEXT        NOT NULL,
    role                        TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_golden_question_reference_paths PRIMARY KEY (id),
    CONSTRAINT ck_golden_question_reference_paths_id
        CHECK (id ~ '^gqr_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_golden_question_reference_paths_scope
        UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_golden_question_reference_paths_ordinal
        UNIQUE (golden_question_version_id, ordinal),
    CONSTRAINT fk_golden_question_reference_paths_version
        FOREIGN KEY (golden_question_version_id, org_id, project_id)
        REFERENCES app.golden_question_versions (id, org_id, project_id),
    CONSTRAINT fk_golden_question_reference_paths_spec
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT ck_golden_question_reference_paths_ordinal CHECK (ordinal >= 0),
    CONSTRAINT ck_golden_question_reference_paths_role
        CHECK (role IN ('canonical', 'alternative'))
);

CREATE INDEX IF NOT EXISTS idx_golden_question_reference_paths_version
    ON app.golden_question_reference_paths (golden_question_version_id, ordinal);

-- ---------------------------------------------------------------------------
-- 3. Story 51.4: the Observed Cohort -- a frozen selection of authorized real
--    executions.
--
--    There is deliberately NO `is_baseline`, `approved_by`, `approved_at`,
--    `gate_decision_id` or `blocking` column on either table below, and no route
--    will offer one. `analyze-and-test.md:357-358`: "Observed cohorts use
--    reference windows, not deterministic baselines." The prohibition is proved
--    from this side by the absence, and from the baseline side by the trigger in
--    section 10 which refuses to approve a run that is not `offline`.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.observed_cohorts (
    id                          TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    label                       TEXT,

    -- The TIME pin.
    window_start                TIMESTAMPTZ NOT NULL,
    window_end                  TIMESTAMPTZ NOT NULL,

    -- The SCOPE pins.
    surface                     TEXT,
    actor_class                 TEXT,

    -- The BUSINESS pin: both halves or neither.
    business_domain_id          TEXT,
    business_domain_version     INTEGER,

    -- The SUBJECT pins: both halves or neither.
    semantic_view_id            TEXT,
    semantic_view_version_id    TEXT,
    capability                  TEXT,
    result_type                 TEXT,

    -- The ENVIRONMENT pins.
    model_ref                   TEXT,
    host_profile                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tool_catalog_version        TEXT,
    context_version_set_hash    TEXT,

    -- Membership was frozen HERE. Everything computed over this cohort divides
    -- by `member_count`, which is stored rather than counted at read time so a
    -- later erasure cannot silently improve a historical percentage.
    resolved_at                 TIMESTAMPTZ NOT NULL,
    member_count                BIGINT      NOT NULL,

    filter_hash                 TEXT        NOT NULL,
    content_hash                TEXT        NOT NULL,
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_observed_cohorts PRIMARY KEY (id),
    CONSTRAINT ck_observed_cohorts_id
        CHECK (id ~ '^ocoh_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_observed_cohorts_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_observed_cohorts_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT fk_observed_cohorts_domain_version
        FOREIGN KEY (business_domain_id, business_domain_version)
        REFERENCES app.mdm_business_domain_versions (domain_id, version_number),
    CONSTRAINT fk_observed_cohorts_domain_org
        FOREIGN KEY (business_domain_id, org_id)
        REFERENCES app.mdm_business_domains (id, org_id),
    CONSTRAINT fk_observed_cohorts_semantic_scope
        FOREIGN KEY (semantic_view_version_id, semantic_view_id, project_id)
        REFERENCES app.semantic_view_versions (id, view_id, project_id),

    CONSTRAINT ck_observed_cohorts_label
        CHECK (label IS NULL OR length(btrim(label)) BETWEEN 1 AND 200),
    CONSTRAINT ck_observed_cohorts_window CHECK (window_end > window_start),
    CONSTRAINT ck_observed_cohorts_surface
        CHECK (surface IS NULL OR surface IN ('mcp-app', 'console', 'api')),
    CONSTRAINT ck_observed_cohorts_actor_class
        CHECK (actor_class IS NULL OR length(btrim(actor_class)) BETWEEN 1 AND 200),
    -- A domain id without its version number is not a pin, it is a hope.
    CONSTRAINT ck_observed_cohorts_business_pin
        CHECK ((business_domain_id IS NULL) = (business_domain_version IS NULL)),
    CONSTRAINT ck_observed_cohorts_subject_pin
        CHECK ((semantic_view_id IS NULL) = (semantic_view_version_id IS NULL)),
    -- `latest` is not a version. Where a pin is supplied it must be exact.
    CONSTRAINT ck_observed_cohorts_view_version_is_exact
        CHECK (semantic_view_version_id IS NULL
               OR app.is_exact_version_pin(semantic_view_version_id)),
    CONSTRAINT ck_observed_cohorts_model_is_exact
        CHECK (model_ref IS NULL OR app.is_exact_version_pin(model_ref)),
    CONSTRAINT ck_observed_cohorts_catalog_is_exact
        CHECK (tool_catalog_version IS NULL
               OR app.is_exact_version_pin(tool_catalog_version)),
    CONSTRAINT ck_observed_cohorts_capability
        CHECK (capability IS NULL OR length(btrim(capability)) BETWEEN 1 AND 200),
    CONSTRAINT ck_observed_cohorts_result_type
        CHECK (result_type IS NULL
               OR result_type IN ('scalar', 'series', 'breakdown', 'comparison',
                                  'table', 'narrative', 'refusal')),
    CONSTRAINT ck_observed_cohorts_host_profile
        CHECK (jsonb_typeof(host_profile) = 'object'),
    CONSTRAINT ck_observed_cohorts_context_hash
        CHECK (context_version_set_hash IS NULL
               OR context_version_set_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_observed_cohorts_member_count CHECK (member_count >= 0),
    CONSTRAINT ck_observed_cohorts_filter_hash CHECK (filter_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_observed_cohorts_content_hash CHECK (content_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_observed_cohorts_project
    ON app.observed_cohorts (project_id, resolved_at DESC);

COMMENT ON TABLE app.observed_cohorts IS
    'Story 51.4: a frozen, explicitly pinned selection of authorized real executions. A reference window, never a deterministic baseline: it carries no approval, gate or blocking column by construction.';

CREATE TABLE IF NOT EXISTS app.observed_cohort_members (
    id                      TEXT        NOT NULL,
    cohort_id               TEXT        NOT NULL,
    org_id                  TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    ai_path_id              TEXT        NOT NULL,
    query_result_id         TEXT,

    -- Declared, held NULL: see absence (1). `render_evidence_state` is the lens
    -- the read model shows; today only `unverifiable` is reachable.
    render_ref              TEXT,
    render_evidence_state   TEXT        NOT NULL DEFAULT 'unverifiable',

    -- A path with no steps, or one whose outcome is `unavailable`, counts in the
    -- denominator and never in the numerator. Dropping it would inflate coverage.
    path_evidence_state     TEXT        NOT NULL,
    observed_at             TIMESTAMPTZ NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_observed_cohort_members PRIMARY KEY (id),
    CONSTRAINT ck_observed_cohort_members_id
        CHECK (id ~ '^ocm_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_observed_cohort_members_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_observed_cohort_members_path UNIQUE (cohort_id, ai_path_id),
    CONSTRAINT fk_observed_cohort_members_cohort
        FOREIGN KEY (cohort_id, org_id, project_id)
        REFERENCES app.observed_cohorts (id, org_id, project_id),
    CONSTRAINT fk_observed_cohort_members_path
        FOREIGN KEY (ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id),
    CONSTRAINT fk_observed_cohort_members_result
        FOREIGN KEY (query_result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT ck_observed_cohort_members_render_state
        CHECK (render_evidence_state IN ('unverifiable', 'pinned')),
    CONSTRAINT ck_observed_cohort_members_render_pin
        CHECK ((render_ref IS NULL) = (render_evidence_state = 'unverifiable')),
    CONSTRAINT ck_observed_cohort_members_render_unpinned
        CHECK (render_ref IS NULL),
    CONSTRAINT ck_observed_cohort_members_path_state
        CHECK (path_evidence_state IN ('observed', 'unverifiable'))
);

CREATE INDEX IF NOT EXISTS idx_observed_cohort_members_cohort
    ON app.observed_cohort_members (cohort_id, observed_at DESC, id);

COMMENT ON COLUMN app.observed_cohort_members.render_ref IS
    'Declared pin held NULL by ck_observed_cohort_members_render_unpinned: its owner (Stories 50.4 / 50.5 / 50.7) is not delivered. Only the unverifiable evidence state is reachable today.';

-- A proposal SUGGESTS. It never creates, edits or versions a Golden Question:
-- Story 51.1 owns that object and its authoring path. `golden_question_id` is
-- populated only when someone accepted the proposal and authored the question
-- through that owner -- which is why the CHECK ties the two together instead of
-- leaving a nullable pointer whose meaning nobody can state.
CREATE TABLE IF NOT EXISTS app.golden_question_proposals (
    id                  TEXT        NOT NULL,
    cohort_id           TEXT        NOT NULL,
    member_id           TEXT        NOT NULL,
    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    reason_code         TEXT        NOT NULL,
    severity_hint       TEXT        NOT NULL,
    state               TEXT        NOT NULL DEFAULT 'proposed',
    golden_question_id  TEXT,
    decided_by          TEXT,
    decided_at          TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_golden_question_proposals PRIMARY KEY (id),
    CONSTRAINT ck_golden_question_proposals_id
        CHECK (id ~ '^gqp_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_golden_question_proposals_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_golden_question_proposals_member UNIQUE (member_id, reason_code),
    CONSTRAINT fk_golden_question_proposals_cohort
        FOREIGN KEY (cohort_id, org_id, project_id)
        REFERENCES app.observed_cohorts (id, org_id, project_id),
    CONSTRAINT fk_golden_question_proposals_member
        FOREIGN KEY (member_id, org_id, project_id)
        REFERENCES app.observed_cohort_members (id, org_id, project_id),
    CONSTRAINT fk_golden_question_proposals_question
        FOREIGN KEY (golden_question_id, org_id, project_id)
        REFERENCES app.golden_questions (id, org_id, project_id),
    CONSTRAINT ck_golden_question_proposals_reason CHECK (reason_code IN (
        'required_node_missing', 'forbidden_node_used', 'out_of_order',
        'version_mismatch', 'path_unverifiable', 'result_refused',
        'result_degraded', 'result_unavailable'
    )),
    CONSTRAINT ck_golden_question_proposals_severity
        CHECK (severity_hint IN ('low', 'medium', 'high', 'critical')),
    CONSTRAINT ck_golden_question_proposals_state
        CHECK (state IN ('proposed', 'accepted', 'declined')),
    CONSTRAINT ck_golden_question_proposals_accepted
        CHECK ((state = 'accepted') = (golden_question_id IS NOT NULL)),
    CONSTRAINT ck_golden_question_proposals_decision
        CHECK ((state = 'proposed') = (decided_at IS NULL)
               AND (decided_at IS NULL) = (decided_by IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_golden_question_proposals_cohort
    ON app.golden_question_proposals (cohort_id, state, created_at DESC);

-- ---------------------------------------------------------------------------
-- 4. Story 51.2: the run profile a baseline belongs to, and the frozen context.
--
--    `user_feedback` is deliberately absent from `evidence_mode`: an annotation
--    is a judgement on an observation, never a run (`analyze-and-test.md:216`).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evaluation_run_profiles (
    id              TEXT        NOT NULL,
    org_id          TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    name            TEXT        NOT NULL,
    description     TEXT        NOT NULL DEFAULT '',
    evidence_mode   TEXT        NOT NULL,
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_run_profiles PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_run_profiles_id
        CHECK (id ~ '^erp_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_run_profiles_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_run_profiles_name UNIQUE (project_id, name),
    CONSTRAINT fk_evaluation_run_profiles_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_evaluation_run_profiles_name
        CHECK (length(btrim(name)) BETWEEN 1 AND 200),
    CONSTRAINT ck_evaluation_run_profiles_mode
        CHECK (evidence_mode IN ('offline', 'observed_cohort'))
);

CREATE INDEX IF NOT EXISTS idx_evaluation_run_profiles_project
    ON app.evaluation_run_profiles (project_id, evidence_mode, name);

CREATE TABLE IF NOT EXISTS app.evaluation_context_version_sets (
    id              TEXT        NOT NULL,
    org_id          TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    content_hash    TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_context_version_sets PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_context_version_sets_id
        CHECK (id ~ '^ecvs_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_context_version_sets_scope
        UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_evaluation_context_version_sets_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_evaluation_context_version_sets_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$')
);

-- Where the `latest` refusal is enforced. The `owner_workspace` vocabulary is
-- copied verbatim from `app.ai_path_steps` (migration 150): one vocabulary for
-- an expected pin and an observed step, not two that drift apart.
--
-- A Skill has no table in this repository -- `skill_tool_catalog.py` is a
-- read-only projection of the live MCP catalog. A Skill is therefore pinned as
-- an entry here with `owner_object_type = 'skill'` and an exact
-- `owner_version_id`. When the caller cannot supply one, that is an entry in the
-- run's `unresolved_pins`, never a `latest`.
CREATE TABLE IF NOT EXISTS app.evaluation_context_version_set_entries (
    id                  TEXT        NOT NULL,
    set_id              TEXT        NOT NULL,
    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,
    ordinal             INTEGER     NOT NULL,
    owner_workspace     TEXT        NOT NULL,
    owner_object_type   TEXT        NOT NULL,
    owner_object_id     TEXT        NOT NULL,
    owner_version_id    TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_context_version_set_entries PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_cvs_entries_id
        CHECK (id ~ '^ecve_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_cvs_entries_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_cvs_entries_owner
        UNIQUE (set_id, owner_object_type, owner_object_id),
    CONSTRAINT uq_evaluation_cvs_entries_ordinal UNIQUE (set_id, ordinal),
    CONSTRAINT fk_evaluation_cvs_entries_set
        FOREIGN KEY (set_id, org_id, project_id)
        REFERENCES app.evaluation_context_version_sets (id, org_id, project_id),
    CONSTRAINT ck_evaluation_cvs_entries_ordinal CHECK (ordinal >= 0),
    CONSTRAINT ck_evaluation_cvs_entries_workspace
        CHECK (owner_workspace IN ('data', 'governance', 'analyze',
                                   'context-hub', 'test')),
    CONSTRAINT ck_evaluation_cvs_entries_object_type
        CHECK (owner_object_type ~ '^[a-z][a-z0-9-]{2,60}$'),
    CONSTRAINT ck_evaluation_cvs_entries_object_id
        CHECK (length(btrim(owner_object_id)) BETWEEN 1 AND 200),
    CONSTRAINT ck_evaluation_cvs_entries_version_is_exact
        CHECK (app.is_exact_version_pin(owner_version_id))
);

CREATE INDEX IF NOT EXISTS idx_evaluation_cvs_entries_set
    ON app.evaluation_context_version_set_entries (set_id, ordinal);

-- ---------------------------------------------------------------------------
-- 5. Story 51.2: the immutable Evaluation Run and its per-question cases.
--
--    `as_of` is a DATE, not a timestamp. The date-grain invariant is a product
--    invariant; a run whose `as_of` carried an hour would compare against a
--    baseline that did not.
--
--    `unresolved_pins` is what makes finalization honest. A run may finalize
--    only when every mandatory pin is set AND every pin that could not be
--    resolved has an entry naming its reason and its owner story. A run
--    finalized with neither a pin nor a recorded reason for its absence is
--    refused by the trigger in section 10.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evaluation_runs (
    id                          TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    run_profile_id              TEXT        NOT NULL,
    lifecycle                   TEXT        NOT NULL DEFAULT 'recording',
    evidence_mode               TEXT        NOT NULL,

    -- SUBJECT. Story 51.1 delivers a governed Golden Question and its versions,
    -- but no governed "question set" object. The subject is therefore pinned
    -- exactly where it can be: each case names its `golden_question_version_id`
    -- by composite foreign key, and this fingerprint is a sha256 DERIVED from
    -- that exact sorted set. It is comparison material, not an owner.
    question_set_fingerprint    TEXT,

    -- ENVIRONMENT.
    semantic_view_id            TEXT        NOT NULL,
    semantic_view_version_id    TEXT        NOT NULL,
    context_version_set_id      TEXT        NOT NULL,
    model_ref                   TEXT        NOT NULL,
    host_capability_profile     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    tool_catalog_version        TEXT        NOT NULL,
    data_snapshot_ref           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    data_snapshot_hash          TEXT        NOT NULL,
    as_of                       DATE        NOT NULL,

    -- Declared and held NULL: see absence (1).
    render_runtime_version      TEXT,

    -- The observed half. An `observed_cohort` run names the exact frozen cohort
    -- it describes; an `offline` run names none. A cohort can never be approved
    -- as a baseline -- see the trigger in section 10.
    observed_cohort_id          TEXT,

    unresolved_pins             JSONB       NOT NULL DEFAULT '[]'::jsonb,
    pin_fingerprints            JSONB       NOT NULL DEFAULT '{}'::jsonb,

    started_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ended_at                    TIMESTAMPTZ,
    created_by                  TEXT        NOT NULL,
    content_hash                TEXT,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_runs PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_runs_id
        CHECK (id ~ '^erun_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_runs_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_evaluation_runs_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT fk_evaluation_runs_profile
        FOREIGN KEY (run_profile_id, org_id, project_id)
        REFERENCES app.evaluation_run_profiles (id, org_id, project_id),
    CONSTRAINT fk_evaluation_runs_semantic_scope
        FOREIGN KEY (semantic_view_version_id, semantic_view_id, project_id)
        REFERENCES app.semantic_view_versions (id, view_id, project_id),
    CONSTRAINT fk_evaluation_runs_context_set
        FOREIGN KEY (context_version_set_id, org_id, project_id)
        REFERENCES app.evaluation_context_version_sets (id, org_id, project_id),
    CONSTRAINT fk_evaluation_runs_cohort
        FOREIGN KEY (observed_cohort_id, org_id, project_id)
        REFERENCES app.observed_cohorts (id, org_id, project_id),

    CONSTRAINT ck_evaluation_runs_lifecycle
        CHECK (lifecycle IN ('recording', 'finalized')),
    CONSTRAINT ck_evaluation_runs_mode
        CHECK (evidence_mode IN ('offline', 'observed_cohort')),
    CONSTRAINT ck_evaluation_runs_cohort_matches_mode
        CHECK ((observed_cohort_id IS NOT NULL) = (evidence_mode = 'observed_cohort')),
    CONSTRAINT ck_evaluation_runs_model_is_exact
        CHECK (app.is_exact_version_pin(model_ref)),
    CONSTRAINT ck_evaluation_runs_host_profile
        CHECK (jsonb_typeof(host_capability_profile) = 'object'),
    -- The catalog version is the sha256 `catalog_version` that
    -- `skill_tool_catalog.list_skill_tool_catalog()` already returns. Pinning
    -- that value rather than minting a numbering scheme is what keeps one
    -- catalog identity in the repository.
    CONSTRAINT ck_evaluation_runs_catalog_version
        CHECK (tool_catalog_version ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evaluation_runs_snapshot_ref
        CHECK (jsonb_typeof(data_snapshot_ref) = 'object'),
    CONSTRAINT ck_evaluation_runs_snapshot_hash
        CHECK (data_snapshot_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evaluation_runs_render_unpinned
        CHECK (render_runtime_version IS NULL),
    CONSTRAINT ck_evaluation_runs_unresolved_pins
        CHECK (jsonb_typeof(unresolved_pins) = 'array'),
    CONSTRAINT ck_evaluation_runs_pin_fingerprints
        CHECK (jsonb_typeof(pin_fingerprints) = 'object'),
    CONSTRAINT ck_evaluation_runs_question_set_fingerprint
        CHECK (question_set_fingerprint IS NULL
               OR question_set_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evaluation_runs_content_hash
        CHECK (content_hash IS NULL OR content_hash ~ '^[0-9a-f]{64}$'),
    -- A finalized run is a complete claim or it is not finalized. Half of one is
    -- what makes an evidence chain unreadable six months later.
    CONSTRAINT ck_evaluation_runs_finalized_is_complete CHECK (
        lifecycle <> 'finalized'
        OR (ended_at IS NOT NULL
            AND content_hash IS NOT NULL
            AND question_set_fingerprint IS NOT NULL)
    ),
    CONSTRAINT ck_evaluation_runs_ends_after_start
        CHECK (ended_at IS NULL OR ended_at >= started_at)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_runs_profile
    ON app.evaluation_runs (run_profile_id, started_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_project_mode
    ON app.evaluation_runs (project_id, evidence_mode, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_evaluation_runs_open
    ON app.evaluation_runs (project_id, started_at)
    WHERE lifecycle = 'recording';

COMMENT ON COLUMN app.evaluation_runs.render_runtime_version IS
    'Declared pin held NULL by ck_evaluation_runs_render_unpinned: its owner (Stories 50.4 / 50.5 / 50.7) is not delivered.';

-- The per-question subject pins.
--
-- The AI Path check has THREE legal states where migration 151 has two, and the
-- difference is deliberate. A Result must resolve its path or say `No AI path`.
-- An evaluation CASE has a third honest state: path evidence was expected and is
-- missing. That is an unresolved pin and `Unverifiable`. Collapsing it into
-- `No AI path` would assert that no AI was involved, which is a different claim.
CREATE TABLE IF NOT EXISTS app.evaluation_run_cases (
    id                              TEXT        NOT NULL,
    run_id                          TEXT        NOT NULL,
    org_id                          TEXT        NOT NULL,
    project_id                      TEXT        NOT NULL,
    golden_question_version_id      TEXT        NOT NULL,

    result_id                       TEXT,
    -- Declared, held NULL: see absence (1).
    render_ref                      TEXT,
    ai_path_id                      TEXT,
    ai_path_absent_literal          TEXT,

    business_domain_id              TEXT        NOT NULL,
    business_domain_version_number  INTEGER     NOT NULL,
    capability_key                  TEXT        NOT NULL,
    result_type                     TEXT        NOT NULL,

    unresolved_pins                 JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_run_cases PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_run_cases_id
        CHECK (id ~ '^ecase_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_run_cases_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_run_cases_question
        UNIQUE (run_id, golden_question_version_id),
    CONSTRAINT fk_evaluation_run_cases_run
        FOREIGN KEY (run_id, org_id, project_id)
        REFERENCES app.evaluation_runs (id, org_id, project_id),
    CONSTRAINT fk_evaluation_run_cases_question_version
        FOREIGN KEY (golden_question_version_id, org_id, project_id)
        REFERENCES app.golden_question_versions (id, org_id, project_id),
    CONSTRAINT fk_evaluation_run_cases_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT fk_evaluation_run_cases_ai_path
        FOREIGN KEY (ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id),
    CONSTRAINT fk_evaluation_run_cases_domain_version
        FOREIGN KEY (business_domain_id, business_domain_version_number)
        REFERENCES app.mdm_business_domain_versions (domain_id, version_number),
    CONSTRAINT fk_evaluation_run_cases_domain_org
        FOREIGN KEY (business_domain_id, org_id)
        REFERENCES app.mdm_business_domains (id, org_id),

    CONSTRAINT ck_evaluation_run_cases_render_unpinned
        CHECK (render_ref IS NULL),
    CONSTRAINT ck_evaluation_run_cases_ai_path_is_honest CHECK (
        (ai_path_id IS NOT NULL AND ai_path_absent_literal IS NULL)
        OR (ai_path_id IS NULL AND ai_path_absent_literal = 'No AI path')
        OR (ai_path_id IS NULL AND ai_path_absent_literal IS NULL)
    ),
    CONSTRAINT ck_evaluation_run_cases_domain_version
        CHECK (business_domain_version_number >= 1),
    -- The connector-name refusal of `analyze-and-test.md:237-238` needs the live
    -- connector registry and is enforced by the service; the database enforces
    -- the shape only.
    CONSTRAINT ck_evaluation_run_cases_capability
        CHECK (capability_key ~ '^[a-z][a-z0-9_.-]{1,120}$'),
    CONSTRAINT ck_evaluation_run_cases_result_type
        CHECK (result_type IN ('scalar', 'series', 'breakdown', 'comparison',
                               'table', 'narrative', 'refusal')),
    CONSTRAINT ck_evaluation_run_cases_unresolved_pins
        CHECK (jsonb_typeof(unresolved_pins) = 'array')
);

CREATE INDEX IF NOT EXISTS idx_evaluation_run_cases_run
    ON app.evaluation_run_cases (run_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_evaluation_run_cases_domain
    ON app.evaluation_run_cases (project_id, business_domain_id,
                                 business_domain_version_number);

COMMENT ON COLUMN app.evaluation_run_cases.render_ref IS
    'Declared pin held NULL by ck_evaluation_run_cases_render_unpinned: its owner (Stories 50.4 / 50.5 / 50.7) is not delivered. The successor migration owned by Story 50.4 drops the CHECK and adds the composite foreign key.';

-- ---------------------------------------------------------------------------
-- 6. Stories 51.2 and 51.3: six independent verdicts, and the path comparison.
--
--    The six dimensions are exactly the rows of `analyze-and-test.md:319-326`.
--    THERE IS NO SCORE COLUMN, and there is no place to add one without a
--    migration that has to explain itself. A dimension keeps its own verdict:
--    an overall percentage cannot hide a critical failure or pay for a
--    correctness regression with better feedback.
--
--    `unverifiable` is not a soft `fail` and `not_applicable` is not a silent
--    `pass`. Two rules make that structural rather than hopeful:
--      * `mcp_app_behavior` cannot be `pass` while its pin is NULL -- and its pin
--        is held NULL by CHECK, so today it is always `unverifiable`;
--      * `path_quality` cannot be `pass` unless a path comparison for the same
--        case resolved a real observed AI Path (trigger, section 10).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evaluation_case_dimension_verdicts (
    id              TEXT        NOT NULL,
    case_id         TEXT        NOT NULL,
    org_id          TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    dimension       TEXT        NOT NULL,
    verdict         TEXT        NOT NULL,
    reason_code     TEXT        NOT NULL,
    evidence_refs   JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Declared, held NULL: see absence (1). Present on the verdict row because
    -- the MCP App dimension is the one whose evidence it would be.
    render_ref      TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_case_dimension_verdicts PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_case_verdicts_id
        CHECK (id ~ '^edv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_case_verdicts_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_case_verdicts_dimension UNIQUE (case_id, dimension),
    CONSTRAINT fk_evaluation_case_verdicts_case
        FOREIGN KEY (case_id, org_id, project_id)
        REFERENCES app.evaluation_run_cases (id, org_id, project_id),
    CONSTRAINT ck_evaluation_case_verdicts_dimension CHECK (dimension IN (
        'semantic_correctness', 'provenance_correctness', 'context_adherence',
        'path_quality', 'dq_handling', 'mcp_app_behavior'
    )),
    CONSTRAINT ck_evaluation_case_verdicts_verdict
        CHECK (verdict IN ('pass', 'fail', 'unverifiable', 'not_applicable')),
    -- A verdict without a machine-readable reason is unusable six months later,
    -- and an `unverifiable` without one is indistinguishable from a bug.
    CONSTRAINT ck_evaluation_case_verdicts_reason
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{2,80}$'),
    CONSTRAINT ck_evaluation_case_verdicts_evidence
        CHECK (jsonb_typeof(evidence_refs) = 'object'),
    CONSTRAINT ck_evaluation_case_verdicts_render_unpinned
        CHECK (render_ref IS NULL),
    -- The durable rule, written so it survives the day Story 50.4 drops the
    -- CHECK above: this dimension cannot pass without its evidence.
    CONSTRAINT ck_evaluation_case_verdicts_mcp_needs_evidence CHECK (
        dimension <> 'mcp_app_behavior'
        OR verdict <> 'pass'
        OR render_ref IS NOT NULL
    )
);

CREATE INDEX IF NOT EXISTS idx_evaluation_case_verdicts_case
    ON app.evaluation_case_dimension_verdicts (case_id, dimension);
CREATE INDEX IF NOT EXISTS idx_evaluation_case_verdicts_project
    ON app.evaluation_case_dimension_verdicts (project_id, dimension, verdict);

COMMENT ON TABLE app.evaluation_case_dimension_verdicts IS
    'Stories 51.2/51.3: one verdict per (case, dimension). No aggregate score column exists here, and that absence is the enforcement of analyze-and-test.md:336-337.';

-- One row per case. The four violation families are SEPARATE fields because
-- `analyze-and-test.md:275` requires version mismatches, missing path evidence
-- and unavailable traces to be reported separately: a version mismatch is a
-- deviation (`fail`), missing evidence is an absence (`unverifiable`), and
-- merging them makes the second look like the first.
--
-- Nothing here stores or infers model reasoning. Migration 150 has no column for
-- it and this table adds none.
CREATE TABLE IF NOT EXISTS app.evaluation_path_comparisons (
    id                      TEXT        NOT NULL,
    case_id                 TEXT        NOT NULL,
    org_id                  TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    observed_ai_path_id     TEXT,
    expected_pattern_hash   TEXT        NOT NULL,
    evidence_state          TEXT        NOT NULL,
    path_verdict            TEXT        NOT NULL,
    required_missing        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    forbidden_present       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    order_violations        JSONB       NOT NULL DEFAULT '[]'::jsonb,
    version_mismatches      JSONB       NOT NULL DEFAULT '[]'::jsonb,
    matched_alternative_key TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_path_comparisons PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_path_comparisons_id
        CHECK (id ~ '^epc_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_path_comparisons_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_path_comparisons_case UNIQUE (case_id),
    CONSTRAINT fk_evaluation_path_comparisons_case
        FOREIGN KEY (case_id, org_id, project_id)
        REFERENCES app.evaluation_run_cases (id, org_id, project_id),
    CONSTRAINT fk_evaluation_path_comparisons_path
        FOREIGN KEY (observed_ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id),
    CONSTRAINT ck_evaluation_path_comparisons_pattern_hash
        CHECK (expected_pattern_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evaluation_path_comparisons_evidence_state
        CHECK (evidence_state IN ('observed', 'missing', 'unavailable')),
    CONSTRAINT ck_evaluation_path_comparisons_verdict
        CHECK (path_verdict IN ('pass', 'fail', 'unverifiable', 'not_applicable')),
    CONSTRAINT ck_evaluation_path_comparisons_state_matches_pin
        CHECK ((evidence_state = 'observed') = (observed_ai_path_id IS NOT NULL)),
    -- Missing server-owned path evidence is `Unverifiable`, never `pass`.
    CONSTRAINT ck_evaluation_path_comparisons_pass_needs_evidence
        CHECK (observed_ai_path_id IS NOT NULL OR path_verdict <> 'pass'),
    CONSTRAINT ck_evaluation_path_comparisons_required_missing
        CHECK (jsonb_typeof(required_missing) = 'array'),
    CONSTRAINT ck_evaluation_path_comparisons_forbidden
        CHECK (jsonb_typeof(forbidden_present) = 'array'),
    CONSTRAINT ck_evaluation_path_comparisons_order
        CHECK (jsonb_typeof(order_violations) = 'array'),
    CONSTRAINT ck_evaluation_path_comparisons_versions
        CHECK (jsonb_typeof(version_mismatches) = 'array'),
    CONSTRAINT ck_evaluation_path_comparisons_alternative
        CHECK (matched_alternative_key IS NULL
               OR length(btrim(matched_alternative_key)) BETWEEN 1 AND 200)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_path_comparisons_project
    ON app.evaluation_path_comparisons (project_id, evidence_state, created_at DESC);

-- ---------------------------------------------------------------------------
-- 7. Stories 51.2 and 51.3: baselines, comparisons and Gate Decisions.
--
--    A baseline is an APPROVAL, not a pointer that moves. Finalizing a run --
--    one run or twenty -- creates, moves and updates nothing here. The partial
--    unique index is what makes "at most one active baseline per profile" a fact
--    rather than a convention, and the trigger in section 10 permits exactly one
--    mutation: setting `superseded_by_baseline_id` from NULL to a value.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.evaluation_baselines (
    id                          TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    run_profile_id              TEXT        NOT NULL,
    run_id                      TEXT        NOT NULL,
    approved_by                 TEXT        NOT NULL,
    approval_reason             TEXT        NOT NULL,
    compared_versions           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    superseded_by_baseline_id   TEXT,
    approved_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_baselines PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_baselines_id
        CHECK (id ~ '^ebl_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_baselines_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_baselines_run UNIQUE (run_id),
    CONSTRAINT fk_evaluation_baselines_profile
        FOREIGN KEY (run_profile_id, org_id, project_id)
        REFERENCES app.evaluation_run_profiles (id, org_id, project_id),
    CONSTRAINT fk_evaluation_baselines_run
        FOREIGN KEY (run_id, org_id, project_id)
        REFERENCES app.evaluation_runs (id, org_id, project_id),
    CONSTRAINT fk_evaluation_baselines_superseded
        FOREIGN KEY (superseded_by_baseline_id, org_id, project_id)
        REFERENCES app.evaluation_baselines (id, org_id, project_id),
    CONSTRAINT ck_evaluation_baselines_actor
        CHECK (length(btrim(approved_by)) BETWEEN 1 AND 200),
    CONSTRAINT ck_evaluation_baselines_reason
        CHECK (length(btrim(approval_reason)) BETWEEN 1 AND 2000),
    CONSTRAINT ck_evaluation_baselines_compared_versions
        CHECK (jsonb_typeof(compared_versions) = 'object'),
    CONSTRAINT ck_evaluation_baselines_not_self
        CHECK (superseded_by_baseline_id IS NULL OR superseded_by_baseline_id <> id)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_evaluation_baseline_active
    ON app.evaluation_baselines (run_profile_id)
    WHERE superseded_by_baseline_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_evaluation_baselines_profile
    ON app.evaluation_baselines (run_profile_id, approved_at DESC);

-- A comparison holds everything constant but the intended candidate versions.
-- The service computes a fingerprint per pin family on both runs and refuses the
-- comparison unless the families whose fingerprints differ are exactly the
-- declared set; `held_constant_fingerprint` is the material that makes that
-- refusal auditable after the fact.
--
-- A model, host or tool-catalog qualification is a distinct `comparison_kind`
-- and its results are never pooled with a semantic or context comparison.
CREATE TABLE IF NOT EXISTS app.evaluation_comparisons (
    id                          TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    baseline_run_id             TEXT        NOT NULL,
    candidate_run_id            TEXT        NOT NULL,
    comparison_kind             TEXT        NOT NULL,
    changed_pin_families        JSONB       NOT NULL,
    held_constant_fingerprint   TEXT        NOT NULL,
    unverifiable_families       JSONB       NOT NULL DEFAULT '[]'::jsonb,
    created_by                  TEXT        NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_evaluation_comparisons PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_comparisons_id
        CHECK (id ~ '^ecmp_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_comparisons_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_evaluation_comparisons_baseline_run
        FOREIGN KEY (baseline_run_id, org_id, project_id)
        REFERENCES app.evaluation_runs (id, org_id, project_id),
    CONSTRAINT fk_evaluation_comparisons_candidate_run
        FOREIGN KEY (candidate_run_id, org_id, project_id)
        REFERENCES app.evaluation_runs (id, org_id, project_id),
    CONSTRAINT ck_evaluation_comparisons_kind
        CHECK (comparison_kind IN ('semantic', 'context', 'model', 'host',
                                   'tool_catalog')),
    CONSTRAINT ck_evaluation_comparisons_changed_families
        CHECK (jsonb_typeof(changed_pin_families) = 'array'
               AND jsonb_array_length(changed_pin_families) >= 1),
    CONSTRAINT ck_evaluation_comparisons_unverifiable_families
        CHECK (jsonb_typeof(unverifiable_families) = 'array'),
    CONSTRAINT ck_evaluation_comparisons_fingerprint
        CHECK (held_constant_fingerprint ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_evaluation_comparisons_distinct_runs
        CHECK (baseline_run_id <> candidate_run_id)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_comparisons_candidate
    ON app.evaluation_comparisons (candidate_run_id, created_at DESC);

-- The Gate Decision is EVIDENCE the owning Governance or Context Hub workflow
-- reads before its own publish/activate transition. It is not a lifecycle
-- transition, and nothing in this migration writes a Governance or Context Hub
-- table. Missing required coverage is `unverifiable`, never green.
CREATE TABLE IF NOT EXISTS app.evaluation_gate_decisions (
    id                          TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    comparison_id               TEXT        NOT NULL,
    decision                    TEXT        NOT NULL,
    candidate_owner_workspace   TEXT        NOT NULL,
    candidate_object_type       TEXT        NOT NULL,
    candidate_object_id         TEXT        NOT NULL,
    candidate_version_id        TEXT        NOT NULL,
    coverage                    JSONB       NOT NULL,
    failing_dimensions          JSONB       NOT NULL DEFAULT '[]'::jsonb,
    decision_reason             TEXT        NOT NULL,
    decided_by                  TEXT        NOT NULL,
    decided_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash                TEXT        NOT NULL,

    CONSTRAINT pk_evaluation_gate_decisions PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_gate_decisions_id
        CHECK (id ~ '^egd_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_gate_decisions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_evaluation_gate_decisions_comparison
        FOREIGN KEY (comparison_id, org_id, project_id)
        REFERENCES app.evaluation_comparisons (id, org_id, project_id),
    CONSTRAINT ck_evaluation_gate_decisions_decision
        CHECK (decision IN ('pass', 'block', 'unverifiable')),
    CONSTRAINT ck_evaluation_gate_decisions_workspace
        CHECK (candidate_owner_workspace IN ('governance', 'context-hub')),
    CONSTRAINT ck_evaluation_gate_decisions_object_type
        CHECK (candidate_object_type ~ '^[a-z][a-z0-9-]{2,60}$'),
    CONSTRAINT ck_evaluation_gate_decisions_object_id
        CHECK (length(btrim(candidate_object_id)) BETWEEN 1 AND 200),
    CONSTRAINT ck_evaluation_gate_decisions_version_is_exact
        CHECK (app.is_exact_version_pin(candidate_version_id)),
    -- Coverage always states its denominator. A percentage without one is a
    -- defect, not a rounding choice.
    CONSTRAINT ck_evaluation_gate_decisions_coverage
        CHECK (jsonb_typeof(coverage) = 'object'
               AND (coverage -> 'eligible') IS NOT NULL
               AND (coverage -> 'evaluated') IS NOT NULL
               AND (coverage -> 'missing') IS NOT NULL),
    CONSTRAINT ck_evaluation_gate_decisions_failing
        CHECK (jsonb_typeof(failing_dimensions) = 'array'),
    CONSTRAINT ck_evaluation_gate_decisions_reason
        CHECK (length(btrim(decision_reason)) BETWEEN 1 AND 2000),
    CONSTRAINT ck_evaluation_gate_decisions_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- A `pass` that lists a failing dimension is not a pass.
    CONSTRAINT ck_evaluation_gate_decisions_pass_is_clean
        CHECK (decision <> 'pass' OR jsonb_array_length(failing_dimensions) = 0)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_gate_decisions_candidate
    ON app.evaluation_gate_decisions (project_id, candidate_owner_workspace,
                                      candidate_object_id, decided_at DESC);

-- ---------------------------------------------------------------------------
-- 8. Story 51.5: the User Feedback evidence mode.
--
--    An annotation pins the exact immutable observation it judges. `result_id`
--    is NOT NULL because a reaction with no resolvable Result pins nothing --
--    which is precisely why `app.feedback` (migration 012) cannot be the owner:
--    it stores `module`, `report_ref` and `trace_id`, none of which is a Result,
--    a rendered artifact, an AI Path or an interaction. That table stays
--    readable and is never backfilled.
--
--    Note what is deliberately NOT a column in any of the three tables below: an
--    automated verdict, a computed score, a percentage, and a "resolved by"
--    Governance transition. Automated verdicts live on the case verdict rows of
--    section 6 and are returned under their own field name; no row and no
--    response merges them with a human judgement.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.feedback_annotations (
    id                              TEXT        NOT NULL,
    org_id                          TEXT        NOT NULL,
    project_id                      TEXT        NOT NULL,

    polarity                        TEXT        NOT NULL,
    comment                         TEXT,
    actor                           TEXT        NOT NULL,
    actor_source                    TEXT        NOT NULL,

    -- The interaction. `share` is Story 50.7's to add to this enum.
    observed_surface                TEXT        NOT NULL,
    interaction_ref                 TEXT,
    w3c_trace_id                    TEXT,

    result_id                       TEXT        NOT NULL,
    ai_path_id                      TEXT,
    ai_path_absent_literal          TEXT,

    -- Declared, held NULL: see absence (1).
    render_ref                      TEXT,
    datum_mark                      JSONB,

    business_domain_id              TEXT,
    business_domain_version_number  INTEGER,
    capability                      TEXT,
    result_type                     TEXT,

    -- What the person could actually SEE at that moment. A snapshot, never the
    -- authority for meaning: the authority stays the pinned owner rows. When the
    -- snapshot and the derived pins diverge, the read model discloses the
    -- divergence rather than reconciling it silently.
    visible_versions                JSONB       NOT NULL DEFAULT '{}'::jsonb,
    visible_versions_hash           TEXT        NOT NULL,

    observed_at                     TIMESTAMPTZ NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_annotations PRIMARY KEY (id),
    CONSTRAINT ck_feedback_annotations_id
        CHECK (id ~ '^fba_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_feedback_annotations_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_feedback_annotations_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT fk_feedback_annotations_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT fk_feedback_annotations_ai_path
        FOREIGN KEY (ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id),
    CONSTRAINT fk_feedback_annotations_domain_version
        FOREIGN KEY (business_domain_id, business_domain_version_number)
        REFERENCES app.mdm_business_domain_versions (domain_id, version_number),
    CONSTRAINT fk_feedback_annotations_domain_org
        FOREIGN KEY (business_domain_id, org_id)
        REFERENCES app.mdm_business_domains (id, org_id),

    CONSTRAINT ck_feedback_annotations_polarity
        CHECK (polarity IN ('positive', 'negative')),
    CONSTRAINT ck_feedback_annotations_comment
        CHECK (comment IS NULL OR length(comment) <= 4000),
    CONSTRAINT ck_feedback_annotations_actor
        CHECK (length(btrim(actor)) BETWEEN 1 AND 200),
    CONSTRAINT ck_feedback_annotations_actor_source
        CHECK (actor_source IN ('user', 'reviewer')),
    CONSTRAINT ck_feedback_annotations_surface
        CHECK (observed_surface IN ('console', 'mcp_app')),
    CONSTRAINT ck_feedback_annotations_interaction_ref
        CHECK (interaction_ref IS NULL
               OR length(btrim(interaction_ref)) BETWEEN 1 AND 200),
    -- The same rule as migration 150: 32 lower hex, never all-zero. A stored
    -- all-zero trace id would correlate every uninstrumented execution together.
    CONSTRAINT ck_feedback_annotations_trace_id CHECK (
        w3c_trace_id IS NULL
        OR (w3c_trace_id ~ '^[0-9a-f]{32}$' AND w3c_trace_id <> repeat('0', 32))
    ),
    -- The same two-state rule migration 151 applies to a Result: an exact path,
    -- or the exact literal. Never NULL, never `deferred`, never a rewording.
    --
    -- The `IS NOT NULL` clause in the second branch is load-bearing and is the
    -- reason this is not a verbatim copy. `NULL = 'No AI path'` evaluates to
    -- NULL, a CHECK passes on NULL, and a row with NEITHER a path NOR the
    -- literal would therefore be accepted -- which is exactly the silent
    -- "we'll fill it in later" state the rule exists to forbid.
    CONSTRAINT ck_feedback_annotations_ai_path_is_honest CHECK (
        (ai_path_id IS NOT NULL AND ai_path_absent_literal IS NULL)
        OR (ai_path_id IS NULL
            AND ai_path_absent_literal IS NOT NULL
            AND ai_path_absent_literal = 'No AI path')
    ),
    CONSTRAINT ck_feedback_annotations_render_unpinned
        CHECK (render_ref IS NULL),
    CONSTRAINT ck_feedback_annotations_datum_mark
        CHECK (datum_mark IS NULL),
    CONSTRAINT ck_feedback_annotations_business_pin
        CHECK ((business_domain_id IS NULL) = (business_domain_version_number IS NULL)),
    CONSTRAINT ck_feedback_annotations_capability
        CHECK (capability IS NULL OR length(btrim(capability)) BETWEEN 1 AND 200),
    CONSTRAINT ck_feedback_annotations_result_type
        CHECK (result_type IS NULL
               OR result_type IN ('scalar', 'series', 'breakdown', 'comparison',
                                  'table', 'narrative', 'refusal')),
    CONSTRAINT ck_feedback_annotations_visible_versions
        CHECK (jsonb_typeof(visible_versions) = 'object'),
    CONSTRAINT ck_feedback_annotations_visible_versions_hash
        CHECK (visible_versions_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_feedback_annotations_project
    ON app.feedback_annotations (project_id, observed_at DESC, id);
CREATE INDEX IF NOT EXISTS idx_feedback_annotations_result
    ON app.feedback_annotations (result_id);
CREATE INDEX IF NOT EXISTS idx_feedback_annotations_domain
    ON app.feedback_annotations (project_id, business_domain_id,
                                 business_domain_version_number);

COMMENT ON COLUMN app.feedback_annotations.render_ref IS
    'Declared pin held NULL by ck_feedback_annotations_render_unpinned: its owner (Stories 50.4 / 50.5 / 50.7) is not delivered, so the corresponding lens reports Unverifiable with reason render_owner_not_delivered.';
COMMENT ON COLUMN app.feedback_annotations.datum_mark IS
    'Declared and held NULL: the Visualization Spec datum/mark owner is Story 50.4. A datum claim without that owner is Unverifiable.';

CREATE TABLE IF NOT EXISTS app.feedback_reviews (
    feedback_id                 TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    current_review_version_id   TEXT,
    current_state               TEXT        NOT NULL DEFAULT 'unreviewed',
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_reviews PRIMARY KEY (feedback_id),
    CONSTRAINT uq_feedback_reviews_scope UNIQUE (feedback_id, org_id, project_id),
    CONSTRAINT fk_feedback_reviews_annotation
        FOREIGN KEY (feedback_id, org_id, project_id)
        REFERENCES app.feedback_annotations (id, org_id, project_id),
    CONSTRAINT ck_feedback_reviews_state CHECK (current_state IN (
        'unreviewed', 'triaged', 'accepted', 'rejected', 'duplicate', 'resolved'
    ))
);

CREATE TABLE IF NOT EXISTS app.feedback_review_versions (
    id                      TEXT        NOT NULL,
    feedback_id             TEXT        NOT NULL,
    org_id                  TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    version_number          INTEGER     NOT NULL,
    review_state            TEXT        NOT NULL,
    affected_dimension      TEXT        NOT NULL,
    human_verdict           TEXT        NOT NULL,
    severity                TEXT        NOT NULL,
    reason                  TEXT        NOT NULL,
    reviewer                TEXT        NOT NULL,

    -- An offline-evaluation seed is a REQUEST. Recording it writes this row and
    -- nothing else: no Golden Question, no Context Hub knowledge, no Skill, no
    -- Semantic View, no classification, no Governance lifecycle transition.
    seed_requested_at       TIMESTAMPTZ,
    seed_reason             TEXT,
    seed_target_kind        TEXT,
    seed_target_id          TEXT,

    predecessor_version_id  TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_review_versions PRIMARY KEY (id),
    CONSTRAINT ck_feedback_review_versions_id
        CHECK (id ~ '^fbrv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_feedback_review_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_feedback_review_versions_number
        UNIQUE (feedback_id, version_number),
    CONSTRAINT fk_feedback_review_versions_head
        FOREIGN KEY (feedback_id, org_id, project_id)
        REFERENCES app.feedback_reviews (feedback_id, org_id, project_id),
    CONSTRAINT fk_feedback_review_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.feedback_review_versions (id, org_id, project_id),
    CONSTRAINT fk_feedback_review_versions_seed_target
        FOREIGN KEY (seed_target_id, org_id, project_id)
        REFERENCES app.golden_questions (id, org_id, project_id),

    CONSTRAINT ck_feedback_review_versions_number CHECK (version_number >= 1),
    CONSTRAINT ck_feedback_review_versions_state CHECK (review_state IN (
        'unreviewed', 'triaged', 'accepted', 'rejected', 'duplicate', 'resolved'
    )),
    -- Exactly one of the ratified six, or `not_applicable` for an annotation
    -- that judges none of them. No seventh dimension, and none scored on tone,
    -- fluency, eloquence or layout taste.
    CONSTRAINT ck_feedback_review_versions_dimension CHECK (affected_dimension IN (
        'semantic_correctness', 'provenance_correctness', 'context_adherence',
        'path_quality', 'dq_handling', 'mcp_app_behavior', 'not_applicable'
    )),
    CONSTRAINT ck_feedback_review_versions_human_verdict
        CHECK (human_verdict IN ('pass', 'fail', 'unverifiable', 'not_applicable')),
    CONSTRAINT ck_feedback_review_versions_severity
        CHECK (severity IN ('critical', 'major', 'minor')),
    CONSTRAINT ck_feedback_review_versions_reason
        CHECK (length(btrim(reason)) BETWEEN 1 AND 2000),
    CONSTRAINT ck_feedback_review_versions_reviewer
        CHECK (length(btrim(reviewer)) BETWEEN 1 AND 200),
    CONSTRAINT ck_feedback_review_versions_seed_is_complete CHECK (
        (seed_requested_at IS NULL AND seed_reason IS NULL AND seed_target_kind IS NULL)
        OR (seed_requested_at IS NOT NULL AND seed_reason IS NOT NULL
            AND seed_target_kind IS NOT NULL)
    ),
    CONSTRAINT ck_feedback_review_versions_seed_kind
        CHECK (seed_target_kind IS NULL OR seed_target_kind = 'golden_question'),
    CONSTRAINT ck_feedback_review_versions_seed_target_needs_request
        CHECK (seed_target_id IS NULL OR seed_requested_at IS NOT NULL),
    CONSTRAINT ck_feedback_review_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_feedback_review_versions_head
    ON app.feedback_review_versions (feedback_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_review_versions_open_critical
    ON app.feedback_review_versions (project_id, severity, review_state)
    WHERE severity = 'critical';

ALTER TABLE app.feedback_reviews
    DROP CONSTRAINT IF EXISTS fk_feedback_reviews_current_version;
ALTER TABLE app.feedback_reviews
    ADD CONSTRAINT fk_feedback_reviews_current_version
    FOREIGN KEY (current_review_version_id, org_id, project_id)
    REFERENCES app.feedback_review_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 9. Structural validation of the Golden Question document.
--
--    Three of the seven mandatory fields are documents, so a column type cannot
--    prove them. The trigger below refuses the write and NAMES THE OFFENDING
--    INDEX, because a refusal that only says "invalid" makes the author guess.
--
--    The expected-path vocabulary is checked against the enumerations of
--    `app.ai_path_steps` (migration 150). An expected path that could describe a
--    step the observed record cannot express would make every comparison
--    `Unverifiable` by construction.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.assert_golden_question_version_structure()
RETURNS TRIGGER AS $$
DECLARE
    item        JSONB;
    node        JSONB;
    position    INTEGER;
    tag         TEXT;
BEGIN
    position := 0;
    FOR item IN SELECT * FROM jsonb_array_elements(NEW.expected_result) LOOP
        IF jsonb_typeof(item) <> 'object' THEN
            RAISE EXCEPTION 'expected_result[%] must be an object', position
                USING ERRCODE = '23514';
        END IF;
        IF NOT (item ? 'assertion_type')
           OR jsonb_typeof(item -> 'assertion_type') <> 'string'
           OR btrim(item ->> 'assertion_type') = '' THEN
            RAISE EXCEPTION 'expected_result[%] is missing a non-empty assertion_type', position
                USING ERRCODE = '23514';
        END IF;
        -- The key must be PRESENT. A declared `null` tolerance means exact; an
        -- absent tolerance key means nobody decided, and is refused.
        IF NOT (item ? 'tolerance') THEN
            RAISE EXCEPTION 'expected_result[%] is missing the tolerance key (declare null for exact)', position
                USING ERRCODE = '23514';
        END IF;
        position := position + 1;
    END LOOP;

    position := 0;
    FOR item IN SELECT * FROM jsonb_array_elements(NEW.required_provenance) LOOP
        IF jsonb_typeof(item) <> 'object' THEN
            RAISE EXCEPTION 'required_provenance[%] must be an object', position
                USING ERRCODE = '23514';
        END IF;
        IF (item ->> 'link_kind') IS NULL
           OR (item ->> 'link_kind') NOT IN ('source', 'pull', 'virtual_pull',
                                             'mapping', 'publication',
                                             'semantic_view', 'citation') THEN
            RAISE EXCEPTION 'required_provenance[%] has an unknown link_kind %',
                position, COALESCE(item ->> 'link_kind', 'null')
                USING ERRCODE = '23514';
        END IF;
        IF jsonb_typeof(item -> 'required') <> 'boolean' THEN
            RAISE EXCEPTION 'required_provenance[%] must declare a boolean required flag', position
                USING ERRCODE = '23514';
        END IF;
        position := position + 1;
    END LOOP;

    IF NEW.expected_ai_path ? 'required_nodes'
       AND jsonb_typeof(NEW.expected_ai_path -> 'required_nodes') <> 'array' THEN
        RAISE EXCEPTION 'expected_ai_path.required_nodes must be an array'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.expected_ai_path ? 'forbidden_tools'
       AND jsonb_typeof(NEW.expected_ai_path -> 'forbidden_tools') <> 'array' THEN
        RAISE EXCEPTION 'expected_ai_path.forbidden_tools must be an array'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.expected_ai_path ? 'order_constraints'
       AND jsonb_typeof(NEW.expected_ai_path -> 'order_constraints') <> 'array' THEN
        RAISE EXCEPTION 'expected_ai_path.order_constraints must be an array'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.expected_ai_path ? 'alternative_paths'
       AND jsonb_typeof(NEW.expected_ai_path -> 'alternative_paths') <> 'array' THEN
        RAISE EXCEPTION 'expected_ai_path.alternative_paths must be an array'
            USING ERRCODE = '23514';
    END IF;

    position := 0;
    FOR node IN
        SELECT * FROM jsonb_array_elements(
            COALESCE(NEW.expected_ai_path -> 'required_nodes', '[]'::jsonb))
    LOOP
        IF jsonb_typeof(node) <> 'object' THEN
            RAISE EXCEPTION 'expected_ai_path.required_nodes[%] must be an object', position
                USING ERRCODE = '23514';
        END IF;
        IF (node ->> 'step_kind') IS NULL
           OR (node ->> 'step_kind') NOT IN ('tool_call', 'knowledge_read',
                                             'skill_step', 'semantic_query',
                                             'data_read', 'handoff') THEN
            RAISE EXCEPTION 'expected_ai_path.required_nodes[%] uses step_kind % which app.ai_path_steps cannot record',
                position, COALESCE(node ->> 'step_kind', 'null')
                USING ERRCODE = '23514';
        END IF;
        IF (node ? 'owner_workspace')
           AND (node ->> 'owner_workspace') NOT IN ('data', 'governance',
                                                    'analyze', 'context-hub',
                                                    'test') THEN
            RAISE EXCEPTION 'expected_ai_path.required_nodes[%] uses owner_workspace % which app.ai_path_steps cannot record',
                position, COALESCE(node ->> 'owner_workspace', 'null')
                USING ERRCODE = '23514';
        END IF;
        -- Both or neither, exactly as ck_ai_path_steps_skill_pin requires: half
        -- a Skill pin resolves to the wrong step the first time a Skill is
        -- revised, so an expected path may not express one either.
        IF (node ? 'skill_version_id') <> (node ? 'skill_step_id') THEN
            RAISE EXCEPTION 'expected_ai_path.required_nodes[%] pins half a Skill step (skill_version_id and skill_step_id are both or neither)', position
                USING ERRCODE = '23514';
        END IF;
        position := position + 1;
    END LOOP;

    FOREACH tag IN ARRAY NEW.capability_tags LOOP
        IF btrim(COALESCE(tag, '')) = '' THEN
            RAISE EXCEPTION 'capability_tags contains an empty tag'
                USING ERRCODE = '23514';
        END IF;
    END LOOP;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_golden_question_versions_structure
    ON app.golden_question_versions;
CREATE TRIGGER trg_golden_question_versions_structure
BEFORE INSERT ON app.golden_question_versions
FOR EACH ROW EXECUTE FUNCTION app.assert_golden_question_version_structure();

-- ---------------------------------------------------------------------------
-- 10. Immutability, enforced where it cannot be argued with.
--
--     Every trigger carries the `app.rgpd_erasure` guard, the same hatch
--     migrations 098/099, 150 and 151 use. Erasure is the ONLY legitimate reason
--     these rows disappear, and it is explicit rather than implicit: without the
--     guard, `org_purge.py` would silently fail to erase any organization that
--     ever ran an evaluation.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_evaluation_evidence_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'evaluation evidence is immutable: % rows are insert-once (a revision creates a new row, it never rewrites one)',
        TG_TABLE_NAME
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_golden_question_versions_immutable
    ON app.golden_question_versions;
CREATE TRIGGER trg_golden_question_versions_immutable
BEFORE UPDATE OR DELETE ON app.golden_question_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_golden_question_reference_paths_immutable
    ON app.golden_question_reference_paths;
CREATE TRIGGER trg_golden_question_reference_paths_immutable
BEFORE UPDATE OR DELETE ON app.golden_question_reference_paths
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_observed_cohorts_immutable ON app.observed_cohorts;
CREATE TRIGGER trg_observed_cohorts_immutable
BEFORE UPDATE OR DELETE ON app.observed_cohorts
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_observed_cohort_members_immutable
    ON app.observed_cohort_members;
CREATE TRIGGER trg_observed_cohort_members_immutable
BEFORE UPDATE OR DELETE ON app.observed_cohort_members
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_context_version_sets_immutable
    ON app.evaluation_context_version_sets;
CREATE TRIGGER trg_evaluation_context_version_sets_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_context_version_sets
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_cvs_entries_immutable
    ON app.evaluation_context_version_set_entries;
CREATE TRIGGER trg_evaluation_cvs_entries_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_context_version_set_entries
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_run_cases_immutable ON app.evaluation_run_cases;
CREATE TRIGGER trg_evaluation_run_cases_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_run_cases
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_case_verdicts_immutable
    ON app.evaluation_case_dimension_verdicts;
CREATE TRIGGER trg_evaluation_case_verdicts_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_case_dimension_verdicts
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_path_comparisons_immutable
    ON app.evaluation_path_comparisons;
CREATE TRIGGER trg_evaluation_path_comparisons_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_path_comparisons
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_comparisons_immutable
    ON app.evaluation_comparisons;
CREATE TRIGGER trg_evaluation_comparisons_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_comparisons
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_evaluation_gate_decisions_immutable
    ON app.evaluation_gate_decisions;
CREATE TRIGGER trg_evaluation_gate_decisions_immutable
BEFORE UPDATE OR DELETE ON app.evaluation_gate_decisions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_feedback_annotations_immutable ON app.feedback_annotations;
CREATE TRIGGER trg_feedback_annotations_immutable
BEFORE UPDATE OR DELETE ON app.feedback_annotations
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

DROP TRIGGER IF EXISTS trg_feedback_review_versions_immutable
    ON app.feedback_review_versions;
CREATE TRIGGER trg_feedback_review_versions_immutable
BEFORE UPDATE OR DELETE ON app.feedback_review_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_evidence_mutation();

-- The heads ARE allowed to advance -- that is what a head is for -- but they may
-- never be re-identified, re-parented or deleted. A head whose Project could
-- change is a head that can walk its whole version history into another tenant.
CREATE OR REPLACE FUNCTION app.reject_test_head_identity_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a % head carries immutable versions and cannot be deleted',
            TG_TABLE_NAME
            USING ERRCODE = '23000';
    END IF;

    IF TG_TABLE_NAME = 'golden_questions' THEN
        IF NEW.id <> OLD.id
           OR NEW.org_id <> OLD.org_id
           OR NEW.project_id <> OLD.project_id
           OR NEW.created_by <> OLD.created_by
           OR NEW.created_at <> OLD.created_at THEN
            RAISE EXCEPTION 'a Golden Question head may not be re-identified or moved to another Project'
                USING ERRCODE = '23000';
        END IF;
    ELSE
        IF NEW.feedback_id <> OLD.feedback_id
           OR NEW.org_id <> OLD.org_id
           OR NEW.project_id <> OLD.project_id THEN
            RAISE EXCEPTION 'a feedback review head may not be re-identified or moved to another Project'
                USING ERRCODE = '23000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_golden_questions_head_identity ON app.golden_questions;
CREATE TRIGGER trg_golden_questions_head_identity
BEFORE UPDATE OR DELETE ON app.golden_questions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_test_head_identity_rewrite();

DROP TRIGGER IF EXISTS trg_feedback_reviews_head_identity ON app.feedback_reviews;
CREATE TRIGGER trg_feedback_reviews_head_identity
BEFORE UPDATE OR DELETE ON app.feedback_reviews
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_test_head_identity_rewrite();

-- A run profile may be renamed or re-described. It may NOT change evidence mode
-- or identity: every run and every baseline already approved under it was judged
-- as one kind of evidence, and switching the label would silently reclassify
-- them all.
CREATE OR REPLACE FUNCTION app.reject_run_profile_reclassification()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a run profile carries immutable runs and cannot be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.evidence_mode <> OLD.evidence_mode
       OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'a run profile may not be re-identified, moved, or reclassified between evidence modes'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evaluation_run_profiles_stable
    ON app.evaluation_run_profiles;
CREATE TRIGGER trg_evaluation_run_profiles_stable
BEFORE UPDATE OR DELETE ON app.evaluation_run_profiles
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_run_profile_reclassification();

-- The run head allows exactly one transition and freezes afterwards, the same
-- shape `app.ai_paths` uses. Everything else -- re-opening, re-pinning the
-- environment, moving the start time -- is refused at the row level.
--
-- A run may finalize only when nothing is silently missing: `unresolved_pins`
-- must carry an entry for every pin that could not be resolved. Today that is at
-- minimum the rendered-artifact pin, whose owner is not delivered.
CREATE OR REPLACE FUNCTION app.reject_evaluation_run_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'an Evaluation Run is immutable evidence and cannot be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.lifecycle = 'finalized' THEN
        RAISE EXCEPTION 'a finalized Evaluation Run cannot be modified'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.lifecycle <> 'finalized' THEN
        RAISE EXCEPTION 'the only legal Evaluation Run transition is recording -> finalized'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.run_profile_id <> OLD.run_profile_id
       OR NEW.evidence_mode <> OLD.evidence_mode
       OR NEW.semantic_view_id <> OLD.semantic_view_id
       OR NEW.semantic_view_version_id <> OLD.semantic_view_version_id
       OR NEW.context_version_set_id <> OLD.context_version_set_id
       OR NEW.model_ref <> OLD.model_ref
       OR NEW.tool_catalog_version <> OLD.tool_catalog_version
       OR NEW.data_snapshot_hash <> OLD.data_snapshot_hash
       OR NEW.as_of <> OLD.as_of
       OR NEW.observed_cohort_id IS DISTINCT FROM OLD.observed_cohort_id
       OR NEW.started_at <> OLD.started_at
    THEN
        RAISE EXCEPTION 'finalization may not rewrite the pinned subject or environment of an Evaluation Run'
            USING ERRCODE = '23000';
    END IF;

    -- Honest finalization, stated as the rule and not as its present-day shape.
    --
    -- The contract in section 4 is: every pin that could not be resolved carries
    -- an entry naming its reason and its owner. Refusing an EMPTY list expressed
    -- that only by accident -- it is true today solely because the rendered
    -- artifact pin is always unresolved. It also made the target state
    -- unreachable: on the day stories 50.4/50.5/50.7 land a Render and every pin
    -- resolves, the honest list IS empty, and this trigger would refuse every
    -- finalization until someone shipped a migration to unblock it. A guard that
    -- must be removed to let the product become correct is the wrong guard.
    --
    -- So the rule is tied to the pin it is actually about: while the rendered
    -- artifact cannot be pinned, its absence must be recorded. When it can be
    -- pinned, an empty list is the truth and is accepted.
    IF NEW.render_runtime_version IS NULL
       AND NOT EXISTS (
           SELECT 1
           FROM jsonb_array_elements(NEW.unresolved_pins) AS pin
           WHERE pin ->> 'pin_family' = 'render'
       )
    THEN
        RAISE EXCEPTION 'a run cannot finalize while the rendered-artifact pin is unresolved and unrecorded: add an unresolved_pins entry with pin_family=render, its reason_code and its owner story'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evaluation_runs_forward_only ON app.evaluation_runs;
CREATE TRIGGER trg_evaluation_runs_forward_only
BEFORE UPDATE OR DELETE ON app.evaluation_runs
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_evaluation_run_rewrite();

-- A baseline is an approval. Replacing one INSERTS a new row and marks the old
-- one superseded; that single column change is the only mutation permitted, and
-- it can only be made once.
CREATE OR REPLACE FUNCTION app.reject_baseline_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'an approved baseline is evidence and cannot be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.superseded_by_baseline_id IS NOT NULL THEN
        RAISE EXCEPTION 'a superseded baseline is frozen'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.superseded_by_baseline_id IS NULL THEN
        RAISE EXCEPTION 'the only legal baseline mutation is recording its successor'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.run_profile_id <> OLD.run_profile_id
       OR NEW.run_id <> OLD.run_id
       OR NEW.approved_by <> OLD.approved_by
       OR NEW.approval_reason <> OLD.approval_reason
       OR NEW.compared_versions IS DISTINCT FROM OLD.compared_versions
       OR NEW.approved_at <> OLD.approved_at
    THEN
        RAISE EXCEPTION 'a baseline approval may not be rewritten; approve a new one instead'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evaluation_baselines_supersede_only
    ON app.evaluation_baselines;
CREATE TRIGGER trg_evaluation_baselines_supersede_only
BEFORE UPDATE OR DELETE ON app.evaluation_baselines
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_baseline_rewrite();

-- The other half of "observed cohorts use reference windows, not deterministic
-- baselines". The cohort tables carry no approval column; this refuses the
-- approval from the baseline side, so neither route nor SQL can promote an
-- observation into a reproducible reference. A baseline also requires a
-- finalized run: approving a recording run would approve an unfinished claim.
CREATE OR REPLACE FUNCTION app.assert_baseline_run_is_offline()
RETURNS TRIGGER AS $$
DECLARE
    run_mode      TEXT;
    run_lifecycle TEXT;
BEGIN
    SELECT evidence_mode, lifecycle
      INTO run_mode, run_lifecycle
      FROM app.evaluation_runs
     WHERE id = NEW.run_id;

    IF run_lifecycle IS DISTINCT FROM 'finalized' THEN
        RAISE EXCEPTION 'a baseline may only approve a finalized Evaluation Run'
            USING ERRCODE = '23514';
    END IF;

    IF run_mode IS DISTINCT FROM 'offline' THEN
        RAISE EXCEPTION 'an observed cohort is a reference window and can never be approved as a baseline'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evaluation_baselines_offline_only
    ON app.evaluation_baselines;
CREATE TRIGGER trg_evaluation_baselines_offline_only
BEFORE INSERT ON app.evaluation_baselines
FOR EACH ROW EXECUTE FUNCTION app.assert_baseline_run_is_offline();

-- Path quality cannot be `pass` without server-owned path evidence. The CHECK on
-- the comparison row proves the comparison itself; this proves the VERDICT,
-- which is the row a caller actually reads.
CREATE OR REPLACE FUNCTION app.assert_path_verdict_has_evidence()
RETURNS TRIGGER AS $$
DECLARE
    observed TEXT;
BEGIN
    IF NEW.dimension <> 'path_quality' OR NEW.verdict <> 'pass' THEN
        RETURN NEW;
    END IF;

    SELECT observed_ai_path_id
      INTO observed
      FROM app.evaluation_path_comparisons
     WHERE case_id = NEW.case_id;

    IF observed IS NULL THEN
        RAISE EXCEPTION 'path_quality cannot pass without a resolved observed AI Path: missing path evidence is unverifiable'
            USING ERRCODE = '23514';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_evaluation_case_verdicts_path_evidence
    ON app.evaluation_case_dimension_verdicts;
CREATE TRIGGER trg_evaluation_case_verdicts_path_evidence
BEFORE INSERT ON app.evaluation_case_dimension_verdicts
FOR EACH ROW EXECUTE FUNCTION app.assert_path_verdict_has_evidence();

-- A proposal is the one row here that legitimately advances, and only along the
-- lifecycle it declares. It may never be re-identified, re-pointed at another
-- observation, given a different reason, walked back to `proposed`, or deleted:
-- an observed failure that was declined is itself evidence.
CREATE OR REPLACE FUNCTION app.reject_proposal_lifecycle_rewrite()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'a Golden Question proposal records an observed failure and cannot be deleted'
            USING ERRCODE = '23000';
    END IF;

    IF OLD.state <> 'proposed' THEN
        RAISE EXCEPTION 'a decided proposal is frozen'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.state = 'proposed' THEN
        RAISE EXCEPTION 'the only legal proposal transitions are proposed -> accepted and proposed -> declined'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id
       OR NEW.cohort_id <> OLD.cohort_id
       OR NEW.member_id <> OLD.member_id
       OR NEW.org_id <> OLD.org_id
       OR NEW.project_id <> OLD.project_id
       OR NEW.reason_code <> OLD.reason_code
       OR NEW.severity_hint <> OLD.severity_hint
       OR NEW.created_at <> OLD.created_at
    THEN
        RAISE EXCEPTION 'a proposal may not be re-identified or re-pointed at another observation'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_golden_question_proposals_forward_only
    ON app.golden_question_proposals;
CREATE TRIGGER trg_golden_question_proposals_forward_only
BEFORE UPDATE OR DELETE ON app.golden_question_proposals
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_proposal_lifecycle_rewrite();

CREATE OR REPLACE FUNCTION app.reject_evaluation_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'evaluation evidence cannot be truncated'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_golden_question_versions_block_truncate
    ON app.golden_question_versions;
CREATE TRIGGER trg_golden_question_versions_block_truncate
BEFORE TRUNCATE ON app.golden_question_versions
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_evaluation_runs_block_truncate ON app.evaluation_runs;
CREATE TRIGGER trg_evaluation_runs_block_truncate
BEFORE TRUNCATE ON app.evaluation_runs
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_evaluation_run_cases_block_truncate
    ON app.evaluation_run_cases;
CREATE TRIGGER trg_evaluation_run_cases_block_truncate
BEFORE TRUNCATE ON app.evaluation_run_cases
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_evaluation_case_verdicts_block_truncate
    ON app.evaluation_case_dimension_verdicts;
CREATE TRIGGER trg_evaluation_case_verdicts_block_truncate
BEFORE TRUNCATE ON app.evaluation_case_dimension_verdicts
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_evaluation_baselines_block_truncate
    ON app.evaluation_baselines;
CREATE TRIGGER trg_evaluation_baselines_block_truncate
BEFORE TRUNCATE ON app.evaluation_baselines
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_evaluation_gate_decisions_block_truncate
    ON app.evaluation_gate_decisions;
CREATE TRIGGER trg_evaluation_gate_decisions_block_truncate
BEFORE TRUNCATE ON app.evaluation_gate_decisions
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_observed_cohorts_block_truncate ON app.observed_cohorts;
CREATE TRIGGER trg_observed_cohorts_block_truncate
BEFORE TRUNCATE ON app.observed_cohorts
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_observed_cohort_members_block_truncate
    ON app.observed_cohort_members;
CREATE TRIGGER trg_observed_cohort_members_block_truncate
BEFORE TRUNCATE ON app.observed_cohort_members
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_feedback_annotations_block_truncate
    ON app.feedback_annotations;
CREATE TRIGGER trg_feedback_annotations_block_truncate
BEFORE TRUNCATE ON app.feedback_annotations
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

DROP TRIGGER IF EXISTS trg_feedback_review_versions_block_truncate
    ON app.feedback_review_versions;
CREATE TRIGGER trg_feedback_review_versions_block_truncate
BEFORE TRUNCATE ON app.feedback_review_versions
FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate();

-- The remaining evidence tables get the same guard. A guard on some of them
-- only would leave a documented shortcut to the ones it forgot.
DO $$
DECLARE
    target TEXT;
    protected TEXT[] := ARRAY[
        'golden_question_reference_paths',
        'golden_question_proposals',
        'evaluation_context_version_sets',
        'evaluation_context_version_set_entries',
        'evaluation_path_comparisons',
        'evaluation_comparisons'
    ];
BEGIN
    FOREACH target IN ARRAY protected LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON app.%I',
                       'trg_' || target || '_block_truncate', target);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON app.%I '
            'FOR EACH STATEMENT EXECUTE FUNCTION app.reject_evaluation_truncate()',
            'trg_' || target || '_block_truncate', target
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 11. Fail-closed Row Level Security.
--
--     The same contract as migrations 149, 150 and 151: RLS enabled with no
--     applicable policy exposes no rows, FORCE applies the policy to the table
--     owner too so a deployment connecting as owner is not silently exempt, and
--     the application authorization check still runs FIRST and answers before
--     any work is done. RLS is the floor, not the door.
--
--     Note for anyone writing a test against these tables: RLS is not applied to
--     superusers. An isolation assertion made on a superuser connection passes
--     while proving nothing.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    target TEXT;
    guarded TEXT[] := ARRAY[
        'golden_questions',
        'golden_question_versions',
        'golden_question_reference_paths',
        'observed_cohorts',
        'observed_cohort_members',
        'golden_question_proposals',
        'evaluation_run_profiles',
        'evaluation_context_version_sets',
        'evaluation_context_version_set_entries',
        'evaluation_runs',
        'evaluation_run_cases',
        'evaluation_case_dimension_verdicts',
        'evaluation_path_comparisons',
        'evaluation_baselines',
        'evaluation_comparisons',
        'evaluation_gate_decisions',
        'feedback_annotations',
        'feedback_reviews',
        'feedback_review_versions'
    ];
BEGIN
    FOREACH target IN ARRAY guarded LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', target || '_strict', target);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I '
            'USING ('
            '  current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'''
            '  OR app.epic36_has_resource_access(org_id, ''project'', project_id)'
            ') '
            'WITH CHECK ('
            '  current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'''
            '  OR app.epic36_has_resource_access(org_id, ''project'', project_id)'
            ')',
            target || '_strict', target
        );
    END LOOP;
END $$;

COMMIT;
