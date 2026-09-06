-- 348 -- a presentation rides the scope cascade WITHOUT duplicating the definition.
--
-- Story 75-6 (epic-75-couche-semantique-amelioree-par-usage.md), object half of
-- the amendment << presentation extends: a display block rides the scope cascade
-- without duplicating the definition >> ratified 2026-09-05 in
-- docs/product-architecture/governance.md.
--
-- WHAT WAS MEASURED, BEFORE ANY CODE. The cascade PLATFORM > ORG > PROJECT
-- (`server/core/metric_semantics.py:66`) carries DEFINITIONS. Exactly one
-- client-owned presentation value rides beside it today -- the client label,
-- `app.dimension_labels` (migration 106). Colour, default filter and number
-- format have no rail at all, so an organization that wants its own currency
-- display on a governed metric has one move available: copy the definition into
-- its own scope. That forks the MEANING in order to change the LOOK, and these
-- two tables exist to make it unnecessary.
--
-- WHAT AN OVERRIDE MAY CARRY. A `display` block and nothing else: format, colour
-- and a default filter a builder PRE-FILLS. No expression, no aggregation, no
-- grain, no unit, no predicate applied behind a served number. The definition row
-- is never read-modified-written and never copied; the reading path merges the
-- definition's own baseline with these rows leaf by leaf and says, for each leaf,
-- which scope it came from.
--
-- TWO SCOPES, NEVER THREE. PLATFORM is the definition's own scope and stays it:
-- the CHECK below admits only ORG and PROJECT, so a PLATFORM override cannot be
-- written even by direct SQL. Same line migration 106 drew for the client label.
--
-- TWO OBJECT TYPES, AND EACH BECAUSE ITS DEFINITION ALREADY CARRIES A FORMAT.
--   semantic_concept -> app.semantic_concepts.id ; baseline read off the current
--                       version's `display` JSONB, then its `format` text.
--   metric           -> app.metric_definitions.canonical_name ; baseline read off
--                       the PLATFORM row's `format` text.
-- `dimension` is deliberately NOT admitted: a dimension's client-owned
-- presentation is its NAME, which already has its rail (app.dimension_labels).
-- Two places to name one dimension is the defect, not the feature.
--
-- NO FOREIGN KEY TO THE OBJECT, AND THAT IS THE SAME POSTURE AS THE DOSSIER
-- (migration 340's header). `object_id` names one of two tables depending on
-- `object_type`, and a column cannot carry a foreign key that changes target. The
-- reference is validated at the door (`core/presentation_extends.py`), against the
-- SAME org and project the row is scoped to.
--
-- CLEARING IS A VERSION, NEVER A DELETE. `presentation_override_versions` is
-- append-only by trigger -- migration 151's app.reject_analytical_evidence_mutation()
-- with the app.rgpd_erasure escape hatch, the same shape analysis_dossier_versions
-- carries -- and a cleared scope is a version with `cleared = true` and an empty
-- `display`. Resolution then behaves as though that scope had never spoken, and
-- the history still says who cleared it and when.
--
-- ERASURE. Both tables carry a NO ACTION foreign key (org_id -> app.organizations),
-- so `core.org_purge`'s foreign-key graph reaches them: it walks NO ACTION and
-- RESTRICT edges only, and the append-only trigger yields to the transaction that
-- sets `app.rgpd_erasure`.
--
-- RLS floor: the AI-299 shape for a table carrying org_id with a NULLABLE
-- project_id -- project row -> resource access, org row -> membership. Copied from
-- `app.context_review_requests` in migration 273, which has exactly this shape.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on a populated column

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. app.presentation_overrides -- the HEAD: one per (scope, object).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.presentation_overrides (
    id                  TEXT PRIMARY KEY,           -- prefixed ULID: 'pxo_'
    org_id              TEXT NOT NULL,
    scope_level         TEXT NOT NULL,
    project_id          TEXT,
    object_type         TEXT NOT NULL,
    object_id           TEXT NOT NULL,
    current_version_id  TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_presentation_overrides_id
        CHECK (id ~ '^pxo_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- PLATFORM is the definition's scope and is refused HERE, not only at the door.
    CONSTRAINT ck_presentation_overrides_scope
        CHECK (scope_level IN ('ORG', 'PROJECT')),
    CONSTRAINT ck_presentation_overrides_scope_cols CHECK (
        (scope_level = 'ORG'     AND project_id IS NULL)
     OR (scope_level = 'PROJECT' AND project_id IS NOT NULL)
    ),
    CONSTRAINT ck_presentation_overrides_object_type
        CHECK (object_type IN ('semantic_concept', 'metric')),
    CONSTRAINT ck_presentation_overrides_object_id_bounded
        CHECK (char_length(btrim(object_id)) BETWEEN 1 AND 200),
    CONSTRAINT fk_presentation_overrides_org
        FOREIGN KEY (org_id) REFERENCES app.organizations (id),
    -- MATCH SIMPLE: an ORG-scoped row leaves project_id NULL and this constraint
    -- is satisfied without a lookup. A PROJECT-scoped row must name a project of
    -- the SAME organization -- the composite is what makes that true.
    CONSTRAINT fk_presentation_overrides_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
);

-- UNICITY: one override per (scope, object). COALESCE is MANDATORY -- without it
-- two ORG rows for one object would both be accepted (NULL <> NULL). Same
-- discipline as migrations 049 and 106.
CREATE UNIQUE INDEX IF NOT EXISTS uq_presentation_overrides_scope_object
    ON app.presentation_overrides
    (scope_level, org_id, COALESCE(project_id, ''), object_type, object_id);

-- The resolution path: every scope that could speak for one object, in one read.
CREATE INDEX IF NOT EXISTS idx_presentation_overrides_resolve
    ON app.presentation_overrides (object_type, object_id, org_id);

DROP TRIGGER IF EXISTS trg_presentation_overrides_updated_at ON app.presentation_overrides;
CREATE TRIGGER trg_presentation_overrides_updated_at
    BEFORE UPDATE ON app.presentation_overrides
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. app.presentation_override_versions -- append-only. A clear is a version.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.presentation_override_versions (
    id                      TEXT PRIMARY KEY,       -- prefixed ULID: 'pxv_'
    override_id             TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT,
    version_number          INTEGER NOT NULL,
    cleared                 BOOLEAN NOT NULL DEFAULT FALSE,
    display                 JSONB NOT NULL DEFAULT '{}'::jsonb,
    note                    TEXT,
    predecessor_version_id  TEXT,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT ck_presentation_override_versions_id
        CHECK (id ~ '^pxv_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_presentation_override_versions_number_positive
        CHECK (version_number >= 1),
    CONSTRAINT ck_presentation_override_versions_display_is_object
        CHECK (jsonb_typeof(display) = 'object'),
    -- A cleared version carries NOTHING. A cleared scope that still held a value
    -- would be a scope that speaks after saying it does not.
    CONSTRAINT ck_presentation_override_versions_cleared_is_empty
        CHECK (cleared = FALSE OR display = '{}'::jsonb),
    -- A living version carries SOMETHING. An empty display that is not a clear is
    -- a version nobody can read a decision from.
    CONSTRAINT ck_presentation_override_versions_live_is_not_empty
        CHECK (cleared = TRUE OR display <> '{}'::jsonb),
    CONSTRAINT ck_presentation_override_versions_note_bounded
        CHECK (note IS NULL OR char_length(note) <= 500),
    CONSTRAINT ck_presentation_override_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
     OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    ),
    CONSTRAINT uq_presentation_override_version_number
        UNIQUE (override_id, version_number),
    CONSTRAINT fk_presentation_override_versions_head
        FOREIGN KEY (override_id) REFERENCES app.presentation_overrides (id),
    CONSTRAINT fk_presentation_override_versions_predecessor
        FOREIGN KEY (predecessor_version_id)
        REFERENCES app.presentation_override_versions (id),
    CONSTRAINT fk_presentation_override_versions_org
        FOREIGN KEY (org_id) REFERENCES app.organizations (id),
    CONSTRAINT fk_presentation_override_versions_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
);

CREATE INDEX IF NOT EXISTS idx_presentation_override_versions_head
    ON app.presentation_override_versions (override_id, version_number DESC);

-- The head points at the version it serves. Added after both tables exist,
-- exactly as migration 340 does for the Dossier.
ALTER TABLE app.presentation_overrides
    DROP CONSTRAINT IF EXISTS fk_presentation_overrides_current_version;
ALTER TABLE app.presentation_overrides
    ADD CONSTRAINT fk_presentation_overrides_current_version
        FOREIGN KEY (current_version_id)
        REFERENCES app.presentation_override_versions (id);

-- IMMUTABLE PER VERSION. Migration 151's function, migration 099's escape hatch.
DROP TRIGGER IF EXISTS trg_presentation_override_versions_immutable
    ON app.presentation_override_versions;
CREATE TRIGGER trg_presentation_override_versions_immutable
BEFORE UPDATE OR DELETE ON app.presentation_override_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- ---------------------------------------------------------------------------
-- 3. The AI-299 floor. Both tables carry org_id with a NULLABLE project_id, so
--    both take the two-branch predicate: a PROJECT row asks resource access, an
--    ORG row asks membership. Without the second branch every ORG-scoped
--    override would vanish for a non-owner member.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'presentation_overrides',
        'presentation_override_versions'
    ] LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', t || '_epic36', t);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I '
            'USING (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR (project_id IS NOT NULL '
            '           AND app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            '       OR (project_id IS NULL AND app.epic36_is_org_member(org_id))) '
            'WITH CHECK (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR (project_id IS NOT NULL '
            '           AND app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            '       OR (project_id IS NULL AND app.epic36_is_org_member(org_id)))',
            t || '_epic36', t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- 4. Grants. Migration 207's ALTER DEFAULT PRIVILEGES already hands SELECT,
--    INSERT, UPDATE, DELETE on every future table in `app` to `connector`, so
--    these GRANTs are declarative and the REVOKE below is the sentence
--    (migration 316's finding, repeated by 333).
--
--    The HEAD keeps UPDATE: advancing `current_version_id` is what a head is for.
--    The VERSION ledger keeps UPDATE and DELETE too, DELIBERATELY: it is
--    append-only by TRIGGER, and revoking the privilege would break the RGPD
--    erasure -- the exact defect migration 333's header records having made once.
-- ---------------------------------------------------------------------------
GRANT SELECT, INSERT, UPDATE, DELETE ON app.presentation_overrides TO connector;
GRANT SELECT, INSERT, UPDATE, DELETE ON app.presentation_override_versions TO connector;

COMMENT ON TABLE app.presentation_overrides IS
    'Story 75-6: the HEAD of a presentation extends -- one per (scope, object). '
    'Scope is ORG or PROJECT only; PLATFORM is the definition''s own scope and is '
    'refused by CHECK. Carries no display itself: the served block is the version '
    'the head points at. An override never changes a definition.';

COMMENT ON TABLE app.presentation_override_versions IS
    'Story 75-6: append-only versions of a presentation extends. `display` carries '
    'format / color / default_filter and nothing that changes what a number means. '
    'Clearing is a version with cleared = true and an empty display, never a DELETE: '
    'resolution then falls back to the parent scope and the history still says who '
    'cleared it. Immutable by trigger, with the app.rgpd_erasure escape hatch.';

COMMIT;
