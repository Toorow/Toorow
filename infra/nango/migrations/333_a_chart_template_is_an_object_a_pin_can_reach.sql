-- Story 72.1 -- the Chart Template is an object, and the pin that names one
-- finally resolves.
--
-- WHY THIS EXISTS. `docs/product-architecture/visualization-and-rendering.md`
-- § *Amendment, 2026-08-31 -- Chart Template, the ratified target* ratifies an
-- object that had no table anywhere in this repository. Measured before this file
-- was written:
--
--     grep -rln "visualization_template" infra/nango/migrations/
--     #   154_reports_notebooks_renders.sql
--     #   155_honest_literals_are_null_proof.sql
--
-- Neither CREATEs anything. They carry the string `'visualization_template_version'`
-- as an accepted value of `presentation_kind` (154:181, 154:345, 155:40, 155:61),
-- so the word named a vocabulary and nothing else.
--
-- THE DEFECT THAT MADE IT URGENT, and it is the AI-338 defect on another table.
-- `app.analysis_report_versions` (154:143-190) carries a foreign key to
-- `query_spec_versions`, one to `query_specs` and one to its own predecessor --
-- and NONE on `presentation_version_id`. `app.analysis_notebook_version_blocks`
-- (154:292-293) carries the same two columns and the same absence. The only guard
-- was `app.is_exact_pin` (154:70-75), which refuses six placeholder words and
-- accepts every other string. So a Report version or a Notebook block could store
-- a presentation pin -- of EITHER kind -- that no read would ever resolve. That is
-- the fabricated pin `resolve_presentation`'s own docstring says it refuses.
--
-- WHAT WAS MEASURED IN PRODUCTION BEFORE ADDING THE KEYS (read-only, 2026-08-31,
-- ledger head 332):
--
--     app.analysis_report_versions                            = 0 rows
--     app.analysis_notebook_version_blocks                    = 0 rows
--     rows of either carrying a presentation pin              = 0
--     app.visualization_spec_versions                          = 1 row
--     to_regclass('app.visualization_template_versions')       = NULL
--
-- Zero stored pins is what makes the keys below free to add and safe to VALIDATE.
-- They are added plainly, never `NOT VALID`: a key nobody validated would let this
-- migration pass while the defect stayed, which is AI-338's own words.
--
-- ---------------------------------------------------------------------------
-- THE FORM OF THE KEY, AND THE TWO THAT WERE REFUSED
-- ---------------------------------------------------------------------------
-- The pin is MULTI-KIND: `presentation_kind` decides which table
-- `presentation_version_id` names. Two kinds are declared today
-- (`visualization_spec_version`, `visualization_template_version`), by a CHECK
-- migration 154 already wrote.
--
-- (a) TWO NULLABLE COLUMNS, ONE PER KIND, EACH WITH ITS OWN PLAIN KEY. Refused:
--     it re-shapes a ratified schema -- `presentation_kind` /
--     `presentation_version_id` are what `analyze_artifacts.py`, both carriers and
--     migration 155's honest-literal CHECK all speak -- and a migration that
--     rewrites the columns of an applied one is exactly what this repository does
--     not do. It also multiplies by the number of kinds, and 72.x adds none but
--     the next surface might.
--
-- (b) A PARTIAL FOREIGN KEY -- `FOREIGN KEY (...) WHERE presentation_kind = ...`.
--     Not available: PostgreSQL has no partial or conditional foreign key. A
--     trigger can imitate one, and that is the third option below.
--
-- (c) WHAT THIS MIGRATION DOES -- one derived registry holding exactly what a
--     foreign key needs, and the two carriers keyed onto it. It is the same shape
--     migration 330 ratified yesterday for the same reason (AI-338: a validator
--     judging a union while five keys judged one ledger), and the same shape
--     `app.context_graph` uses (context-hub.md, amendment of 2026-08-28). Its cost
--     is stated rather than hidden: it STORES A DERIVABLE FACT, which CLAUDE.md
--     tells us not to do, and the reason is that PostgreSQL references a table --
--     never a view, never a union, never a condition. The assertion at the bottom
--     is what keeps the derivation honest.
--
-- A TRIGGER WAS THE OTHER LIVE CANDIDATE and it is weaker in one measurable way:
-- a trigger checks the row it is handed, and nothing checks the row it points at
-- when that row goes away. A foreign key is the only thing that makes the target
-- undeletable while the pin stands, and `core.org_purge` reads its ordering FROM
-- `pg_constraint` (org_purge.py:105) -- so a key is also what makes an erasure
-- delete these tables in the right order without anybody naming them in a list.
--
-- WHAT THE REGISTRY DOES NOT DECIDE. It answers ONE question -- *is this (kind,
-- version id) a version some Project of this deployment holds?* -- which is the
-- question a foreign key asks and the only one it asks. It holds no document, no
-- family and no content hash, so nothing here asserts what a version SAYS. That
-- stays in the two version tables.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The stable head.
--
--    On the exact pattern of `app.analysis_reports` (154:98-127), including its
--    seed columns, because a connector expert pack SEEDS a Chart Template and
--    never owns it -- the sentence at 154:95-97, applied to the other object.
--
--    `seed_origin` carries THREE origins, and they are the three the ratified
--    amendment names in its provenance row ("platform, connector expert pack, or
--    Project"): `project`, `platform_seed`, `connector_seed`. The Report's fourth
--    value, `explore`, has no meaning here -- a template is not born from a
--    Result -- and inventing a fifth would be inventing a decision.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.visualization_templates (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    label               TEXT NOT NULL,
    description         TEXT,
    seed_origin         TEXT NOT NULL DEFAULT 'project',
    seed_module_name    TEXT,
    seed_template_id    TEXT,
    current_version_id  TEXT,
    -- Archive, never delete. Every Report version that pinned a version of this
    -- head is evidence, and evidence does not disappear because a template went
    -- out of use.
    archived_at         TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_visualization_templates_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_visualization_templates_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_visualization_templates_label_bounded
        CHECK (char_length(label) BETWEEN 1 AND 200),
    CONSTRAINT ck_visualization_templates_seed_origin
        CHECK (seed_origin IN ('project', 'platform_seed', 'connector_seed')),
    -- AC2, and it is `ck_analysis_reports_seed_pair` (154:118-123) character for
    -- character on the other object: a connector seed names BOTH coordinates or
    -- neither. Half a seed reference is how a template ends up claiming a
    -- provenance nobody can resolve.
    CONSTRAINT ck_visualization_templates_seed_pair CHECK (
        (seed_origin = 'connector_seed'
            AND seed_module_name IS NOT NULL AND seed_template_id IS NOT NULL)
        OR (seed_origin <> 'connector_seed'
            AND seed_module_name IS NULL AND seed_template_id IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_visualization_templates_project
    ON app.visualization_templates (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_visualization_templates_live
    ON app.visualization_templates (project_id, updated_at DESC)
    WHERE archived_at IS NULL;

COMMENT ON TABLE app.visualization_templates IS
    'Chart Template: the stable identity of a validated, UNBOUND presentation '
    'starting point (visualization-and-rendering.md, amendment 2026-08-31). It '
    'names no data. The wire token of its pin stays visualization_template_version '
    'because migration 154 wrote it into a CHECK; the product noun is Chart '
    'Template.';

-- ---------------------------------------------------------------------------
-- 2. The immutable versions.
--
--    The column list is story 72.1's, and no more of it than that. The document
--    lives in `document` as one JSONB value; its GRAMMAR is story 72.2's, and the
--    CHECKs below are only the outer shape -- the same division migration 156
--    states for the Spec ("the outer shape of the contract belongs there").
--
--    WHAT IS DELIBERATELY NOT PINNED HERE. `spec_contract_version` is bounded and
--    made to agree with the document, but its LITERAL is not fixed to a value:
--    story 72.2 owns the grammar and therefore owns the name of its contract.
--    Fixing a literal now would be this migration inventing a decision, and the
--    widening would have to be undone by another migration. It is named rather
--    than silently omitted.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.visualization_template_versions (
    id                      TEXT PRIMARY KEY,
    template_id             TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    version_number          INTEGER NOT NULL,
    family                  TEXT NOT NULL,
    spec_contract_version   TEXT NOT NULL,
    schema_version          INTEGER NOT NULL,
    document                JSONB NOT NULL,
    content_hash            TEXT NOT NULL,
    predecessor_version_id  TEXT,
    -- Evidence, never a permission -- `visualization_spec_versions.proposed_by`
    -- (156:104-105) exists for the same reason and is spelled the same way. A
    -- model's proposal takes the same route, the same validator and the same
    -- refusal as a human hand; only the record differs.
    proposed_by             TEXT NOT NULL DEFAULT 'person',
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_visualization_template_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_visualization_template_version_number
        UNIQUE (template_id, version_number),
    CONSTRAINT fk_visualization_template_versions_head
        FOREIGN KEY (template_id, org_id, project_id)
        REFERENCES app.visualization_templates (id, org_id, project_id),
    CONSTRAINT fk_visualization_template_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.visualization_template_versions (id, org_id, project_id),

    CONSTRAINT ck_visualization_template_versions_number_positive
        CHECK (version_number >= 1),
    CONSTRAINT ck_visualization_template_versions_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- The closed family enum, mirrored from `SPEC_SELECTABLE_FAMILY_IDS`
    -- (`server/core/visualization_families.py:533-535`) exactly as migration 156
    -- mirrored it for the Spec and migration 191 widened it to eight. It WIDENS BY
    -- MIGRATION and is never re-edited. A template may not name a family the
    -- shipped registry cannot draw: the ten `KNOWN_UNBUILT_FAMILIES` are refused
    -- here by their absence, and by their name in the service.
    CONSTRAINT ck_visualization_template_versions_family CHECK (
        family IN (
            'table', 'kpi', 'line', 'area', 'bar', 'stacked_bar', 'scatter',
            'waterfall'
        )
    ),
    CONSTRAINT ck_visualization_template_versions_contract_bounded
        CHECK (char_length(spec_contract_version) BETWEEN 1 AND 64),
    CONSTRAINT ck_visualization_template_versions_schema_version
        CHECK (schema_version = 1),
    CONSTRAINT ck_visualization_template_versions_proposed_by
        CHECK (proposed_by IN ('person', 'model')),
    CONSTRAINT ck_visualization_template_versions_document_is_object
        CHECK (jsonb_typeof(document) = 'object'),
    -- The document restates its own identity, and the columns and the document may
    -- not disagree -- migration 156's `ck_visualization_spec_versions_document_pins`
    -- on the other object, for the same reason: a psql session meets the same
    -- contract as a request.
    CONSTRAINT ck_visualization_template_versions_document_pins CHECK (
        document ->> 'spec_contract_version' = spec_contract_version
        AND (document -> 'schema_version') = to_jsonb(schema_version)
        AND document ->> 'family' = family
    ),
    -- THE ONE LINE THAT MAKES IT A TEMPLATE AND NOT A VISUALIZATION. A Spec binds
    -- roles to concrete members under `bindings`
    -- (`visualization_specs.py:385`); a template declares `requires` and binds
    -- nothing. The ratified criterion is "a Chart Template carries a member_id, a
    -- query_spec_version_id, a result_id or a data value: that is no longer a
    -- starting point, it is a Visualization". Story 72.2 owns the walker that
    -- refuses each of those by name with a JSON pointer; this is the outer shape
    -- of it, in the layer that cannot be bypassed.
    CONSTRAINT ck_visualization_template_versions_is_unbound CHECK (
        NOT (document ? 'bindings')
        AND NOT (document ? 'query_spec_version_id')
        AND NOT (document ? 'result_id')
    ),
    -- The size cap, mirroring `MAX_SPEC_BYTES` (`visualization_specs.py:123`) and
    -- migration 156's `pg_column_size` CHECK. The cap is the layer that holds when
    -- the walker is bypassed.
    CONSTRAINT ck_visualization_template_versions_size
        CHECK (pg_column_size(document) <= 32768),
    -- Version 1 has no predecessor; every later version names one. AC1: an edit
    -- produces a NEW version carrying its predecessor, never a rewrite.
    CONSTRAINT ck_visualization_template_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_visualization_template_versions_head
    ON app.visualization_template_versions (template_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_visualization_template_versions_hash
    ON app.visualization_template_versions (project_id, content_hash);

COMMENT ON TABLE app.visualization_template_versions IS
    'Immutable versions of a Chart Template. Two writes of the same document '
    'produce the same content_hash; an edit appends a version naming its '
    'predecessor. Insert-once, enforced by trigger rather than by a service.';

ALTER TABLE app.visualization_templates
    DROP CONSTRAINT IF EXISTS fk_visualization_templates_current_version;
ALTER TABLE app.visualization_templates
    ADD CONSTRAINT fk_visualization_templates_current_version
    FOREIGN KEY (current_version_id, org_id, project_id)
    REFERENCES app.visualization_template_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 3. Immutability and the erasure hatch, in the same migration that creates the
--    table (AC4, the rule of `099_rgpd_erasure_trigger_guards.sql`).
--
--    The function is migration 151's, reused rather than redeclared -- two
--    immutability functions are two policies and the second one drifts.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_visualization_template_versions_immutable
    ON app.visualization_template_versions;
CREATE TRIGGER trg_visualization_template_versions_immutable
BEFORE UPDATE OR DELETE ON app.visualization_template_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- TRUNCATE is statement-level: a row trigger never sees it, and a table whose rows
-- are insert-once but whose contents can be emptied in one statement is not
-- immutable.
DROP TRIGGER IF EXISTS trg_visualization_template_versions_no_truncate
    ON app.visualization_template_versions;
CREATE TRIGGER trg_visualization_template_versions_no_truncate
BEFORE TRUNCATE ON app.visualization_template_versions
FOR EACH STATEMENT
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- The head is the one mutable row: it advances its pointer and it is archived. The
-- guard is migration 154's `app.reject_stable_head_rebind`, EXTENDED rather than
-- copied -- a second head policy is a second authority, and the second one drifts.
-- The two existing branches are preserved word for word; only a third is added, so
-- `analysis_reports` and `analysis_notebooks` behave exactly as they did.
CREATE OR REPLACE FUNCTION app.reject_stable_head_rebind()
RETURNS TRIGGER AS $$
DECLARE
    old_number INTEGER;
    new_number INTEGER;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'archive % rather than deleting it: its versions and runs are evidence',
            TG_TABLE_NAME USING ERRCODE = '23000';
    END IF;

    IF NEW.id <> OLD.id OR NEW.org_id <> OLD.org_id OR NEW.project_id <> OLD.project_id THEN
        RAISE EXCEPTION 'a % may not change identity or Project', TG_TABLE_NAME
            USING ERRCODE = '23000';
    END IF;

    IF NEW.current_version_id IS DISTINCT FROM OLD.current_version_id
       AND OLD.current_version_id IS NOT NULL THEN
        IF TG_TABLE_NAME = 'analysis_reports' THEN
            SELECT version_number INTO old_number FROM app.analysis_report_versions
                WHERE id = OLD.current_version_id;
            SELECT version_number INTO new_number FROM app.analysis_report_versions
                WHERE id = NEW.current_version_id;
        ELSIF TG_TABLE_NAME = 'visualization_templates' THEN
            SELECT version_number INTO old_number FROM app.visualization_template_versions
                WHERE id = OLD.current_version_id;
            SELECT version_number INTO new_number FROM app.visualization_template_versions
                WHERE id = NEW.current_version_id;
        ELSE
            SELECT version_number INTO old_number FROM app.analysis_notebook_versions
                WHERE id = OLD.current_version_id;
            SELECT version_number INTO new_number FROM app.analysis_notebook_versions
                WHERE id = NEW.current_version_id;
        END IF;
        IF new_number IS NULL OR old_number IS NULL OR new_number <= old_number THEN
            RAISE EXCEPTION
                'a stable head advances only to a NEWER version (% -> %)', old_number, new_number
                USING ERRCODE = '23000';
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_visualization_templates_forward_only
    ON app.visualization_templates;
CREATE TRIGGER trg_visualization_templates_forward_only
BEFORE UPDATE OR DELETE ON app.visualization_templates
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_stable_head_rebind();

-- ---------------------------------------------------------------------------
-- 4. The pin registry -- what a foreign key can hold when the target depends on a
--    kind column. See the header for the two forms that were refused.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.presentation_version_registry (
    presentation_kind       TEXT NOT NULL,
    presentation_version_id TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    registered_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_presentation_version_registry
        PRIMARY KEY (presentation_kind, presentation_version_id),
    -- What the two carriers key onto. The Project is part of the KEY, not checked
    -- afterwards: a Report of one Project cannot pin a presentation version of
    -- another even when the application layer is wrong -- the composite-key rule
    -- migration 154 states for every one of its arrows (AC13 there, AC3 here).
    CONSTRAINT uq_presentation_version_registry_scope
        UNIQUE (presentation_kind, presentation_version_id, org_id, project_id),
    -- The edge `core.org_purge` walks. Without it an erasure would stop on a table
    -- nobody named -- migration 330's finding, on its own registry.
    CONSTRAINT fk_presentation_version_registry_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    -- The same two kinds migration 154's CHECK admits, and no third. A kind added
    -- to one list and not the other is a pin the validator accepts and the key
    -- cannot hold.
    CONSTRAINT ck_presentation_version_registry_kind CHECK (
        presentation_kind IN ('visualization_template_version', 'visualization_spec_version')
    ),
    CONSTRAINT ck_presentation_version_registry_pin
        CHECK (app.is_exact_pin(presentation_version_id))
);

CREATE INDEX IF NOT EXISTS idx_presentation_version_registry_project
    ON app.presentation_version_registry (org_id, project_id);

COMMENT ON TABLE app.presentation_version_registry IS
    'Which (presentation kind, version id) pairs a Project of this deployment '
    'actually holds. Derived, never authored: it exists because a foreign key '
    'references a table and a presentation pin names one of two tables depending '
    'on its kind. It holds no document and decides no content.';

GRANT SELECT, INSERT, DELETE ON app.presentation_version_registry TO connector;
-- Migration 207's ALTER DEFAULT PRIVILEGES already hands UPDATE on every future
-- table in `app`, so the GRANT above is declarative and the REVOKE is the sentence
-- (migration 316's finding). Nothing UPDATEs a registered pair -- a pair is
-- registered or it is not -- and the one path that DELETEs is the org purge.
REVOKE UPDATE ON app.presentation_version_registry FROM connector;

-- AND THE VERSION LEDGER KEEPS ITS UPDATE, DELIBERATELY. The same REVOKE was
-- written here first -- it is append-only by trigger, so the privilege looked
-- purely declarative -- and it broke the RGPD erasure, measured:
--
--     core/org_purge.py:357
--     UPDATE app.visualization_template_versions SET predecessor_version_id = NULL
--       WHERE (predecessor_version_id, org_id, project_id) IN (...)
--     InsufficientPrivilege: permission denied for table visualization_template_versions
--
-- `core.org_purge` breaks a SELF-REFERENCING blocking edge by NULLing it before
-- it deletes the rows, and `predecessor_version_id` is exactly such an edge --
-- which is why `app.visualization_spec_versions` (migration 156) carries no such
-- REVOKE either. Append-onlyness here is the trigger's job, and the trigger yields
-- to the erasure precisely so this statement can run. Named rather than left as an
-- omission, so the next reader does not "tidy up" the missing REVOKE.

-- ---------------------------------------------------------------------------
-- 5. The two writers. Triggers rather than application code, for migration 330's
--    reason: these ledgers have writers this repository does not route through one
--    module -- the services, a repair script, and every pg-gated fixture that
--    INSERTs directly. A Python writer would be true for the paths it was added to
--    and silently false for the others, which is the shape of the defect above.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.register_presentation_version()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO app.presentation_version_registry
        (presentation_kind, presentation_version_id, org_id, project_id)
    VALUES (TG_ARGV[0], NEW.id, NEW.org_id, NEW.project_id)
    ON CONFLICT DO NOTHING;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_visualization_spec_versions_register
    ON app.visualization_spec_versions;
CREATE TRIGGER trg_visualization_spec_versions_register
    AFTER INSERT ON app.visualization_spec_versions
    FOR EACH ROW EXECUTE FUNCTION
        app.register_presentation_version('visualization_spec_version');

DROP TRIGGER IF EXISTS trg_visualization_template_versions_register
    ON app.visualization_template_versions;
CREATE TRIGGER trg_visualization_template_versions_register
    AFTER INSERT ON app.visualization_template_versions
    FOR EACH ROW EXECUTE FUNCTION
        app.register_presentation_version('visualization_template_version');

-- ---------------------------------------------------------------------------
-- 6. The backfill, BEFORE the keys move, so the ALTERs below validate the pins
--    that already exist instead of refusing them. The Chart Template ledger is
--    created empty in this same transaction and has nothing to backfill.
-- ---------------------------------------------------------------------------
INSERT INTO app.presentation_version_registry
    (presentation_kind, presentation_version_id, org_id, project_id)
SELECT 'visualization_spec_version', v.id, v.org_id, v.project_id
  FROM app.visualization_spec_versions v
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- 7. The two keys the pin never had.
--
--    MATCH SIMPLE (the default) is what makes the honest-absence row pass without
--    a second constraint: a version with no presentation carries NULL in both pin
--    columns -- migration 154's `..._presentation_is_honest` CHECK admits no other
--    shape -- and a composite key with a NULL member is satisfied. So "no accepted
--    presentation contract" stays writable, and a NAMED pin must resolve.
-- ---------------------------------------------------------------------------
ALTER TABLE app.analysis_report_versions
    DROP CONSTRAINT IF EXISTS fk_analysis_report_versions_presentation;
ALTER TABLE app.analysis_report_versions
    ADD CONSTRAINT fk_analysis_report_versions_presentation
    FOREIGN KEY (presentation_kind, presentation_version_id, org_id, project_id)
    REFERENCES app.presentation_version_registry
        (presentation_kind, presentation_version_id, org_id, project_id);

ALTER TABLE app.analysis_notebook_version_blocks
    DROP CONSTRAINT IF EXISTS fk_analysis_notebook_version_blocks_presentation;
ALTER TABLE app.analysis_notebook_version_blocks
    ADD CONSTRAINT fk_analysis_notebook_version_blocks_presentation
    FOREIGN KEY (presentation_kind, presentation_version_id, org_id, project_id)
    REFERENCES app.presentation_version_registry
        (presentation_kind, presentation_version_id, org_id, project_id);

-- ---------------------------------------------------------------------------
-- 8. Row level security.
--
--    Same contract as migrations 149, 150, 151 and 156: RLS enabled with no
--    applicable policy exposes no rows, FORCE applies it to the table owner too,
--    and the application authorization check still runs first. RLS is the floor,
--    not the door. Referential integrity checks bypass row security, so a pin
--    never fails to see the pair it names; the policy governs who may READ.
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualization_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.visualization_templates FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS visualization_templates_strict ON app.visualization_templates;
CREATE POLICY visualization_templates_strict ON app.visualization_templates
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.visualization_template_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.visualization_template_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS visualization_template_versions_strict
    ON app.visualization_template_versions;
CREATE POLICY visualization_template_versions_strict
    ON app.visualization_template_versions
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.presentation_version_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.presentation_version_registry FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS presentation_version_registry_strict
    ON app.presentation_version_registry;
CREATE POLICY presentation_version_registry_strict
    ON app.presentation_version_registry
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- ---------------------------------------------------------------------------
-- 9. The state this migration exists to reach, asserted rather than hoped for.
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    missing INTEGER;
    strays  INTEGER;
    keys    INTEGER;
BEGIN
    -- (a) The registry holds every version the two ledgers number. A backfill that
    --     silently dropped one would move the defect rather than close it.
    SELECT count(*) INTO missing FROM (
        SELECT 'visualization_spec_version' AS kind, v.id, v.org_id, v.project_id
          FROM app.visualization_spec_versions v
        UNION ALL
        SELECT 'visualization_template_version', t.id, t.org_id, t.project_id
          FROM app.visualization_template_versions t
    ) AS numbered
    WHERE NOT EXISTS (
        SELECT 1 FROM app.presentation_version_registry r
         WHERE r.presentation_kind = numbered.kind
           AND r.presentation_version_id = numbered.id
           AND r.org_id = numbered.org_id
           AND r.project_id = numbered.project_id
    );
    IF missing > 0 THEN
        RAISE EXCEPTION
            '% presentation version(s) are absent from the registry the two keys '
            'now judge -- a pin the validator accepts would still be refused',
            missing USING ERRCODE = '23503';
    END IF;

    -- (b) And it holds NOTHING ELSE. A registry wider than the union would let a
    --     key hold a pin naming a version no table carries, which is the same
    --     disagreement pointing the other way.
    SELECT count(*) INTO strays
      FROM app.presentation_version_registry r
     WHERE NOT EXISTS (
            SELECT 1 FROM app.visualization_spec_versions v
             WHERE r.presentation_kind = 'visualization_spec_version'
               AND v.id = r.presentation_version_id
               AND v.org_id = r.org_id AND v.project_id = r.project_id)
       AND NOT EXISTS (
            SELECT 1 FROM app.visualization_template_versions t
             WHERE r.presentation_kind = 'visualization_template_version'
               AND t.id = r.presentation_version_id
               AND t.org_id = r.org_id AND t.project_id = r.project_id);
    IF strays > 0 THEN
        RAISE EXCEPTION
            '% registered pair(s) name a version neither ledger carries', strays
            USING ERRCODE = '23514';
    END IF;

    -- (c) Two keys judge the registry, and the pin is no longer unkeyed.
    SELECT count(*) INTO keys
      FROM pg_constraint c
     WHERE c.contype = 'f'
       AND c.confrelid = 'app.presentation_version_registry'::regclass;
    IF keys <> 2 THEN
        RAISE EXCEPTION
            '% foreign key(s) point at the presentation pin registry, expected 2',
            keys USING ERRCODE = '23000';
    END IF;

    -- (d) The erasure hatch is on every append-only table this migration created,
    --     in this migration (AC4, the rule of 099). Asserted from pg_trigger, not
    --     from the text above.
    SELECT count(*) INTO keys
      FROM pg_trigger t
     WHERE t.tgrelid = 'app.visualization_template_versions'::regclass
       AND NOT t.tgisinternal
       AND pg_get_triggerdef(t.oid) ILIKE '%rgpd_erasure%';
    IF keys < 2 THEN
        RAISE EXCEPTION
            'the Chart Template version ledger carries % guard(s) yielding to the '
            'erasure, expected the row guard and the truncate guard', keys
            USING ERRCODE = '23000';
    END IF;
END $$;

COMMIT;
