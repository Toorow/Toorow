-- Story 50.4: the Visualization -- a stable presentation identity with immutable
-- Visualization Spec versions.
--
-- WHY THIS EXISTS. Epic 50's governed path is
--
--     Query Spec -> immutable Result -> Visualization Spec version -> shared runtime
--
-- and the third arrow had no schema. Migration 151 landed the first two; migration
-- 154 landed Reports, Notebooks and Renders and refuses to write a Render because
-- `app.visualization_spec_versions` -- named there by
-- `server/core/analyze_artifacts.py:65` -- did not exist. This migration is that
-- table, under exactly that name, so the pin resolves rather than being worked
-- around.
--
-- THE NUMBER. This file was drafted as 154 when 153 was the head. By the time it
-- was written, migration 154 (Reports/Notebooks/Renders) and 155 were on disk and
-- applied. 156 is the first free number, verified with
-- `scripts/check_migration_catalog.py` against the ledger, not against a plan.
--
-- WHAT IS MUTABLE, AND WHAT IS NOT.
--
--   * `app.visualizations` is the stable head: the saved presentation identity.
--     It is the ONLY mutable row here, and it advances only through the authorized
--     service transaction.
--   * `app.visualization_spec_versions` is insert-once. Changing how an answer
--     looks appends a version; it never rewrites one. The trigger reuses
--     `app.reject_analytical_evidence_mutation()` from migration 151 rather than
--     declaring a second function, because two immutability functions is two
--     policies and the second one drifts.
--
-- WHY THE CHECKS DUPLICATE PYTHON. `server/core/visualization_specs.py` refuses a
-- document that carries an undeclared key, a renderer option or a colour literal.
-- That protects every caller that goes through the service. The CHECKs below
-- protect the ones that do not: a psql session, a future migration, a repair
-- script. The database is the layer that cannot be argued with, and the outer
-- shape of the contract belongs there -- the same reasoning as
-- `ck_query_results_ai_path_is_honest` (`151_query_specs_and_results.sql:239-242`).
--
-- WHAT THIS IS NOT. It is not a cached analytical answer. There is deliberately no
-- `result_id` column and no row/cardinality count: a Visualization pins the QUERY
-- SPEC VERSION, and volume is evaluated against one exact Result at validate time
-- and returned as a disclosure (`visualization-and-rendering.md:43`). A spec that
-- froze a row count would need a new version every time the data moved.

-- ---------------------------------------------------------------------------
-- 1. The stable head.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.visualizations (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    -- The Query Spec whose shape this presentation is built for. Composite and
    -- Project-scoped: a Visualization cannot point at another Project's query
    -- even if the application layer is wrong.
    query_spec_id       TEXT NOT NULL,
    name                TEXT,
    current_version_id  TEXT,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_visualizations_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_visualizations_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT fk_visualizations_query_spec
        FOREIGN KEY (query_spec_id, org_id, project_id)
        REFERENCES app.query_specs (id, org_id, project_id),
    CONSTRAINT ck_visualizations_name_bounded
        CHECK (name IS NULL OR char_length(name) <= 200)
);

CREATE INDEX IF NOT EXISTS idx_visualizations_project
    ON app.visualizations (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_visualizations_query_spec
    ON app.visualizations (query_spec_id);

-- ---------------------------------------------------------------------------
-- 2. The immutable versions.
--
--    `spec` is the whole validated grammar. The individual pins are lifted into
--    columns ONLY where the database must enforce a scoped foreign key, an index
--    or a CHECK. Everything else lives inside `spec` and is covered by
--    `content_hash`.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.visualization_spec_versions (
    id                      TEXT PRIMARY KEY,
    visualization_id        TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    version_number          INTEGER NOT NULL,
    query_spec_id           TEXT NOT NULL,
    -- The pin that makes AC5 enforceable. A presentation-only change keeps this
    -- value; a query change is a NEW Query Spec version, and re-pinning to it is
    -- a visible, revalidated act rather than a silent one.
    query_spec_version_id   TEXT NOT NULL,
    -- Both version keys, and they do different jobs (decision D1). The literal is
    -- what THIS CHECK can pin distinctively; the integer is what Story 50.5's
    -- renderer registry range-compares.
    spec_contract_version   TEXT NOT NULL,
    schema_version          INTEGER NOT NULL,
    family                  TEXT NOT NULL,
    spec                    JSONB NOT NULL,
    content_hash            TEXT NOT NULL,
    predecessor_version_id  TEXT,
    -- Evidence, never a permission. A model proposal travels the same route, the
    -- same validator and the same refusal envelope as a human edit; the only
    -- difference is recorded here so a reader can tell them apart afterwards.
    proposed_by             TEXT NOT NULL DEFAULT 'person',
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_visualization_spec_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_visualization_spec_version_number
        UNIQUE (visualization_id, version_number),
    CONSTRAINT fk_visualization_spec_versions_head
        FOREIGN KEY (visualization_id, org_id, project_id)
        REFERENCES app.visualizations (id, org_id, project_id),
    CONSTRAINT fk_visualization_spec_versions_query_spec
        FOREIGN KEY (query_spec_id, org_id, project_id)
        REFERENCES app.query_specs (id, org_id, project_id),
    -- `uq_query_spec_versions_scope` (151:85) exists for exactly this.
    CONSTRAINT fk_visualization_spec_versions_query_spec_version
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_visualization_spec_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.visualization_spec_versions (id, org_id, project_id),

    CONSTRAINT ck_visualization_spec_versions_number_positive
        CHECK (version_number >= 1),
    CONSTRAINT ck_visualization_spec_versions_hash
        CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    -- The literal, pinned. A document written by a future contract cannot land in
    -- this table pretending to be a v1 spec.
    CONSTRAINT ck_visualization_spec_versions_contract
        CHECK (spec_contract_version = 'visualization-spec.v1'),
    CONSTRAINT ck_visualization_spec_versions_schema_version
        CHECK (schema_version = 1),
    -- The closed family enum, mirrored from
    -- `server/core/visualization_families.py`. A direct SQL insert cannot invent
    -- an eighth family either.
    CONSTRAINT ck_visualization_spec_versions_family CHECK (
        family IN ('table', 'kpi', 'line', 'area', 'bar', 'stacked_bar', 'scatter')
    ),
    CONSTRAINT ck_visualization_spec_versions_proposed_by
        CHECK (proposed_by IN ('person', 'model')),
    CONSTRAINT ck_visualization_spec_versions_spec_is_object
        CHECK (jsonb_typeof(spec) = 'object'),
    -- The outer shape of the grammar, mirrored so psql meets the same contract as
    -- a request: the two version keys must be present INSIDE the document and must
    -- agree with the columns, the family must agree, and the accessible table
    -- fallback has no off switch (AC8).
    CONSTRAINT ck_visualization_spec_versions_document_pins CHECK (
        spec ->> 'spec_contract_version' = spec_contract_version
        AND (spec -> 'schema_version') = to_jsonb(schema_version)
        AND spec ->> 'family' = family
        AND spec #>> '{accessibility,table_fallback}' = 'required'
    ),
    -- The size cap. There is no free-text leaf in the grammar, so a document that
    -- approaches this ceiling is carrying something the walker should have refused;
    -- the cap is the layer that holds when the walker is bypassed.
    CONSTRAINT ck_visualization_spec_versions_size
        CHECK (pg_column_size(spec) <= 32768),
    -- Version 1 has no predecessor; every later version names one. A revision that
    -- loses its lineage is indistinguishable from a fresh presentation.
    CONSTRAINT ck_visualization_spec_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_visualization_spec_versions_head
    ON app.visualization_spec_versions (visualization_id, version_number DESC);
CREATE INDEX IF NOT EXISTS idx_visualization_spec_versions_query_spec_version
    ON app.visualization_spec_versions (query_spec_version_id);
CREATE INDEX IF NOT EXISTS idx_visualization_spec_versions_hash
    ON app.visualization_spec_versions (project_id, content_hash);

ALTER TABLE app.visualizations
    DROP CONSTRAINT IF EXISTS fk_visualizations_current_version;
ALTER TABLE app.visualizations
    ADD CONSTRAINT fk_visualizations_current_version
    FOREIGN KEY (current_version_id, org_id, project_id)
    REFERENCES app.visualization_spec_versions (id, org_id, project_id)
    DEFERRABLE INITIALLY DEFERRED;

-- ---------------------------------------------------------------------------
-- 3. Immutability, enforced where it cannot be argued with.
--
--    The function is migration 151's, reused rather than redeclared. The
--    `app.rgpd_erasure` escape hatch is the single exception, exactly as 151 does:
--    erasure is the ONLY legitimate reason these rows disappear, and it is
--    explicit rather than implicit.
-- ---------------------------------------------------------------------------
DROP TRIGGER IF EXISTS trg_visualization_spec_versions_immutable
    ON app.visualization_spec_versions;
CREATE TRIGGER trg_visualization_spec_versions_immutable
BEFORE UPDATE OR DELETE ON app.visualization_spec_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- TRUNCATE is a statement-level operation: a row trigger never sees it, and a
-- table whose rows are insert-once but whose contents can be emptied in one
-- statement is not immutable.
DROP TRIGGER IF EXISTS trg_visualization_spec_versions_no_truncate
    ON app.visualization_spec_versions;
CREATE TRIGGER trg_visualization_spec_versions_no_truncate
BEFORE TRUNCATE ON app.visualization_spec_versions
FOR EACH STATEMENT
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();

-- ---------------------------------------------------------------------------
-- 4. Row level security.
--
--    Same contract as migrations 149, 150 and 151: RLS enabled with no applicable
--    policy exposes no rows, FORCE applies it to the table owner too, and the
--    application authorization check still runs first. RLS is the floor, not the
--    door.
-- ---------------------------------------------------------------------------
ALTER TABLE app.visualizations ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.visualizations FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS visualizations_strict ON app.visualizations;
CREATE POLICY visualizations_strict ON app.visualizations
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

ALTER TABLE app.visualization_spec_versions ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.visualization_spec_versions FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS visualization_spec_versions_strict ON app.visualization_spec_versions;
CREATE POLICY visualization_spec_versions_strict ON app.visualization_spec_versions
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );
