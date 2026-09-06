-- 346 -- AI settings are ONE object per scope, and a cleared override is a version.
--
-- Story 75-4 (epic-75-couche-semantique-amelioree-par-usage.md), the object half
-- of the amendment ratified 2026-09-05 in docs/product-architecture/context-hub.md
-- << AI settings: one object per scope, one cascade, served in the agent envelope >>.
--
-- WHAT WAS MEASURED, 2026-09-05, BEFORE ANY CODE. The guidance an agent is meant
-- to obey exists, and none of it is an object: `ai_context` sits per metric on
-- app.metric_definitions, Skills carry procedure, Answerable Topics carry reach,
-- context events carry facts. No table anywhere carries behaviour rules, a fiscal
-- calendar, a query scope or a narrative language for an organization or a
-- project. So two agents reading the same Project answer in two registers and
-- count the year from two different Januaries, and nobody has a place to correct
-- either.
--
-- THE SCOPE IS THE TRIPLET, NOT A BARE `scope_id`, AND THAT IS DELIBERATE. The
-- story spoke of `scope_id`; this file writes `scope_level` + `org_id` +
-- `project_id`, byte for byte the shape app.metric_definitions carries since
-- migration 049 -- the very cascade `metric_semantics._SCOPE_RANK` reads. A bare
-- `scope_id` would be a text column pointing at two different tables: it can
-- carry no foreign key, so an org deletion would leave these rows behind, the
-- FK closure app.organizations -> ... that migration 339 derives the erasure
-- hatch from would not reach them, and the Epic-36 policy would have nothing to
-- test. The triplet gives all three for free, and the python layer still answers
-- `scope_id` to its callers -- a derived value, which is where a derived value
-- belongs.
--
-- ONE ROW PER SCOPE. The COALESCE(...,'') in the unique index is MANDATORY and
-- 049 says why in the same words: without it two PLATFORM rows
-- (PLATFORM, NULL, NULL) are both accepted, because NULL <> NULL in a plain
-- composite unique index, and "one settings object per scope" stops being true
-- at the second INSERT.
--
-- CLEARING IS A VERSION, AND THERE IS NO DELETE IN THIS OBJECT. Returning a
-- scope to its parent appends a row with `cleared = true` and every payload
-- column NULL; the resolver then reads that scope as TRANSPARENT and falls
-- through. A DELETE would destroy the sentence an audit needs -- "the project
-- overrode the language on the 3rd and gave it back on the 5th" -- and would
-- also make the head's current_version_id dangle. The CHECK below makes the two
-- halves inseparable: a cleared version that still states a field, or a stated
-- field on a cleared version, is refused by the database rather than by a
-- convention.
--
-- APPEND-ONLY. Versions reuse migration 151's
-- app.reject_analytical_evidence_mutation() with the app.rgpd_erasure escape
-- hatch in the trigger's WHEN clause -- the same shape as
-- app.analysis_dossier_versions (340) and app.query_spec_versions (151), so all
-- three raise the same SQLSTATE and the same sentence. Writing the hatch HERE
-- rather than leaving it to a later repair is what migration 339 asks of every
-- new DELETE guard inside the org tree: it derives the set, and a guard written
-- without the clause is a guard that will one day refuse an erasure.
--
-- RLS FLOOR. Both tables carry `org_id`, so the AI-299 ratchet
-- (server/tests/core/test_rls_covers_every_org_scoped_table_pg.py) demands a
-- policy on each. The predicate is the THREE-BRANCH one of migration 274, chosen
-- on the SHAPE of the table -- org_id nullable, project_id nullable:
--   org_id IS NULL           -> a PLATFORM row belongs to no tenant, so there is
--                               nothing to isolate and hiding it protects nobody
--                               (274's whole subject);
--   project_id IS NOT NULL   -> project scope, epic36_has_resource_access;
--   project_id IS NULL       -> org scope, epic36_is_org_member.
-- ENABLE *and* FORCE, because ENABLE alone does not apply to the table owner and
-- an isolation that believes itself posed is worse than none.
--
-- ERASURE, AND THE MECHANISM NAMED IS THE ONE THAT ACTS. Both tables hold
-- tenant rows and both are erased when their organization is -- but it is
-- NOT `core.org_purge` that reaches them. Every tenant edge here is
-- ON DELETE CASCADE (org_id -> app.organizations, (org_id, project_id) ->
-- app.projects, ai_settings_id -> app.ai_settings), and `plan_purge` walks the
-- FK graph through NO ACTION and RESTRICT edges ONLY (`_FK_GRAPH_SQL`:
-- confdeltype IN ('a','r')) precisely because Postgres already does the CASCADE
-- work. MEASURED 2026-09-05 on the disposable base carrying this migration:
-- `plan_purge(conn, 'org_EXAMPLE')` emits 3430 statements and ZERO of them names
-- `ai_settings` or `ai_setting_versions`. The erasure is real; the agent is the
-- database. `test_ai_settings_pg.py::test_erasing_the_organization_takes_its_ai_settings_with_it`
-- deletes an organization and reads both tables back empty, so the sentence
-- above is a measurement rather than a belief.
--
-- The only DELETE guard this file adds carries the RGPD hatch in its WHEN
-- clause, so the append-only trigger cannot refuse that cascade.
--
-- Schema-Change-Checklist:
--   [x] Additive: creates two tables, alters none
--   [x] Replayable (IF NOT EXISTS / DROP ... IF EXISTS on trigger and policies)
--   [x] No column altered, no existing row read or written
--   [x] No table dropped

BEGIN;

CREATE TABLE IF NOT EXISTS app.ai_settings (
    id                  TEXT PRIMARY KEY,          -- prefixed ULID: 'aiset_'
    scope_level         TEXT NOT NULL,
    org_id              TEXT REFERENCES app.organizations (id) ON DELETE CASCADE,
    project_id          TEXT,
    current_version_id  TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_ai_settings_scope_level
        CHECK (scope_level IN ('PLATFORM', 'ORG', 'PROJECT')),
    -- The scope columns and the scope word say the same thing, or the row is
    -- refused: 049's ck_metric_definitions_scope_cols, verbatim in intent.
    CONSTRAINT ck_ai_settings_scope_cols CHECK (
        (scope_level = 'PLATFORM' AND org_id IS NULL     AND project_id IS NULL)
     OR (scope_level = 'ORG'      AND org_id IS NOT NULL AND project_id IS NULL)
     OR (scope_level = 'PROJECT'  AND org_id IS NOT NULL AND project_id IS NOT NULL)
    ),
    -- THE PROJECT IS NAMED WITH ITS OWNER, NOT BESIDE IT. A single-column FK to
    -- app.projects would accept a PROJECT row whose org_id is a DIFFERENT
    -- organization -- a settings object filed under a tenant that does not own
    -- the project, which is the one row that must never exist here. The
    -- composite FK to uq_projects_org_id refuses it; MATCH SIMPLE means it is
    -- simply not enforced for the ORG and PLATFORM rows, whose project_id is
    -- NULL, so one constraint covers the three scopes.
    CONSTRAINT fk_ai_settings_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE
);

-- ONE settings object per scope. COALESCE, not a bare composite: see the header.
CREATE UNIQUE INDEX IF NOT EXISTS uq_ai_settings_scope
    ON app.ai_settings (scope_level, COALESCE(org_id, ''), COALESCE(project_id, ''));

CREATE TABLE IF NOT EXISTS app.ai_setting_versions (
    id                  TEXT PRIMARY KEY,          -- prefixed ULID: 'aisetv_'
    ai_settings_id      TEXT NOT NULL REFERENCES app.ai_settings (id) ON DELETE CASCADE,
    -- Mirrored from the head so the Epic-36 floor has something to test on the
    -- version itself: a policy that has to join to be evaluated is a policy that
    -- is skipped.
    org_id              TEXT REFERENCES app.organizations (id) ON DELETE CASCADE,
    project_id          TEXT,
    version_number      INTEGER NOT NULL,
    -- TRUE: this scope states nothing and is TRANSPARENT to the cascade. It is
    -- how an override is given back to the parent -- never a DELETE.
    cleared             BOOLEAN NOT NULL DEFAULT FALSE,
    rules_always        JSONB,
    rules_never         JSONB,
    query_scope         TEXT,
    fiscal_calendar     JSONB,
    narrative_language  TEXT,
    narrative_register  TEXT,
    note                TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT fk_ai_setting_versions_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE,
    CONSTRAINT uq_ai_setting_versions_number UNIQUE (ai_settings_id, version_number),
    CONSTRAINT ck_ai_setting_versions_number_positive CHECK (version_number >= 1),
    -- Three values, narrowest first. The names are flagged "to arbitrate (Jean)"
    -- in the amendment; the CHECK exists so an unknown fourth cannot be stored
    -- while that arbitration is open.
    CONSTRAINT ck_ai_setting_versions_query_scope CHECK (
        query_scope IS NULL
        OR query_scope IN ('governed_views_only', 'any_published_view', 'any_field')
    ),
    CONSTRAINT ck_ai_setting_versions_register CHECK (
        narrative_register IS NULL
        OR narrative_register IN ('plain', 'executive', 'technical')
    ),
    CONSTRAINT ck_ai_setting_versions_rules_are_lists CHECK (
        (rules_always IS NULL OR jsonb_typeof(rules_always) = 'array')
        AND (rules_never IS NULL OR jsonb_typeof(rules_never) = 'array')
    ),
    CONSTRAINT ck_ai_setting_versions_calendar_is_an_object CHECK (
        fiscal_calendar IS NULL OR jsonb_typeof(fiscal_calendar) = 'object'
    ),
    -- The two halves of "clearing is a version", inseparable at the schema level.
    CONSTRAINT ck_ai_setting_versions_cleared_states_nothing CHECK (
        cleared IS FALSE
        OR (rules_always IS NULL AND rules_never IS NULL AND query_scope IS NULL
            AND fiscal_calendar IS NULL AND narrative_language IS NULL
            AND narrative_register IS NULL)
    ),
    -- A version that is not a clearing states at least one field. Otherwise
    -- "set" and "clear" would be the same row with two different words on it.
    CONSTRAINT ck_ai_setting_versions_set_states_something CHECK (
        cleared IS TRUE
        OR (rules_always IS NOT NULL OR rules_never IS NOT NULL OR query_scope IS NOT NULL
            OR fiscal_calendar IS NOT NULL OR narrative_language IS NOT NULL
            OR narrative_register IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_ai_setting_versions_head
    ON app.ai_setting_versions (ai_settings_id, version_number DESC);

ALTER TABLE app.ai_settings
    DROP CONSTRAINT IF EXISTS fk_ai_settings_current_version;
ALTER TABLE app.ai_settings
    ADD CONSTRAINT fk_ai_settings_current_version
        FOREIGN KEY (current_version_id) REFERENCES app.ai_setting_versions (id);

-- APPEND-ONLY, with the erasure hatch written here rather than repaired later.
DROP TRIGGER IF EXISTS trg_ai_setting_versions_immutable ON app.ai_setting_versions;
CREATE TRIGGER trg_ai_setting_versions_immutable
BEFORE UPDATE OR DELETE ON app.ai_setting_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- ---------------------------------------------------------------------------
-- The Epic-36 floor. Three branches, chosen on the shape of the table (274).
-- ---------------------------------------------------------------------------
ALTER TABLE app.ai_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.ai_settings FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ai_settings_epic36 ON app.ai_settings;
CREATE POLICY ai_settings_epic36 ON app.ai_settings
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)))
    WITH CHECK (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

ALTER TABLE app.ai_setting_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.ai_setting_versions FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS ai_setting_versions_epic36 ON app.ai_setting_versions;
CREATE POLICY ai_setting_versions_epic36 ON app.ai_setting_versions
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)))
    WITH CHECK (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR org_id IS NULL
           OR (project_id IS NOT NULL AND app.epic36_has_resource_access(org_id, 'project', project_id))
           OR (project_id IS NULL AND app.epic36_is_org_member(org_id)));

-- ---------------------------------------------------------------------------
-- Grants. The head moves (its pointer and its clock); a version is written once
-- and never again -- so the platform role is granted no UPDATE and no DELETE on
-- it, and the trigger above is not the only sentence that says so.
-- Migration 316's warning holds: under the default privileges of 207 this is
-- DECLARATIVE for a table created after it. It is written anyway, because the
-- posture must be readable in the schema and because a deployment that tightens
-- the defaults must not then break these two tables.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON app.ai_settings TO connector';
        EXECUTE 'GRANT SELECT, INSERT ON app.ai_setting_versions TO connector';
    END IF;
END $$;

COMMENT ON TABLE app.ai_settings IS
    'Story 75-4. The head of the AI settings object at ONE scope (PLATFORM/ORG/PROJECT); '
    'one row per scope, and it holds no content -- only the pointer to the current version. '
    'The shipped defaults are CODE (core/ai_settings.py), never a row here: a PLATFORM row '
    'exists only when a deployment overrides them.';
COMMENT ON TABLE app.ai_setting_versions IS
    'Story 75-4. Append-only content of the AI settings at one scope. A version states the '
    'fields that scope overrides and NOTHING else; `cleared = true` states nothing at all and '
    'makes the scope transparent to the PLATFORM > ORG > PROJECT cascade -- that is how an '
    'override is returned to its parent, and there is no DELETE in this object.';
COMMENT ON COLUMN app.ai_setting_versions.cleared IS
    'TRUE: this scope gives its override back to the parent. Every payload column is NULL '
    '(ck_ai_setting_versions_cleared_states_nothing), and the resolver falls through.';
COMMENT ON COLUMN app.ai_setting_versions.query_scope IS
    'How far a model may reach: governed_views_only | any_published_view | any_field. '
    'The equivalent of Omni''s query_all_views_and_fields. Names to arbitrate (Jean) -- '
    'context-hub.md, amendment of 2026-09-05.';

COMMIT;
