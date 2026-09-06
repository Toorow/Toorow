-- Story 45.1: organization-owned business taxonomy foundation.
-- The seed set is editable data, never a closed business-domain enum.

BEGIN;

DO $$
BEGIN
    IF to_regclass('app.organizations') IS NULL THEN
        RAISE EXCEPTION 'app.organizations is missing -- apply migration 035 first';
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS app.mdm_business_domain_templates (
    slug            TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    display_order   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO app.mdm_business_domain_templates (slug, name, description, display_order) VALUES
    ('sales', 'Sales', 'Revenue generation, pipeline and commercial performance.', 10),
    ('marketing', 'Marketing', 'Demand, audience, channel and campaign performance.', 20),
    ('product', 'Product', 'Portfolio, product usage, adoption and value delivery.', 30),
    ('engineering', 'Engineering', 'Delivery, reliability and technical operations.', 40),
    ('finance', 'Finance', 'Financial performance, planning and cost governance.', 50),
    ('legal', 'Legal', 'Legal obligations, policy and compliance context.', 60)
ON CONFLICT (slug) DO UPDATE SET
    name = EXCLUDED.name,
    description = EXCLUDED.description,
    display_order = EXCLUDED.display_order;

CREATE TABLE IF NOT EXISTS app.mdm_business_domains (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    owner           TEXT,
    status          TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at     TIMESTAMPTZ,
    CONSTRAINT uq_mdm_business_domain_org_slug UNIQUE (org_id, slug),
    CONSTRAINT uq_mdm_business_domain_identity UNIQUE (id, org_id)
);
CREATE INDEX IF NOT EXISTS idx_mdm_business_domains_org_status
    ON app.mdm_business_domains (org_id, status, name);

CREATE TABLE IF NOT EXISTS app.mdm_business_classifications (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    domain_id           TEXT NOT NULL,
    parent_id           TEXT,
    classification_type TEXT NOT NULL,
    slug                TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    owner               TEXT,
    status              TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    archived_at         TIMESTAMPTZ,
    CONSTRAINT uq_mdm_business_classification_identity UNIQUE (id, org_id, domain_id),
    CONSTRAINT fk_mdm_classification_domain
        FOREIGN KEY (domain_id, org_id)
        REFERENCES app.mdm_business_domains(id, org_id) ON DELETE RESTRICT,
    CONSTRAINT fk_mdm_classification_parent
        FOREIGN KEY (parent_id, org_id, domain_id)
        REFERENCES app.mdm_business_classifications(id, org_id, domain_id) ON DELETE RESTRICT,
    CONSTRAINT ck_mdm_classification_not_self_parent CHECK (parent_id IS NULL OR parent_id <> id)
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_business_classification_root_slug
    ON app.mdm_business_classifications (org_id, domain_id, slug)
    WHERE parent_id IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_mdm_business_classification_child_slug
    ON app.mdm_business_classifications (org_id, domain_id, parent_id, slug)
    WHERE parent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_mdm_business_classifications_tree
    ON app.mdm_business_classifications (org_id, domain_id, parent_id, status, name);

CREATE TABLE IF NOT EXISTS app.mdm_business_domain_versions (
    domain_id       TEXT NOT NULL REFERENCES app.mdm_business_domains(id) ON DELETE RESTRICT,
    version_number  INTEGER NOT NULL CHECK (version_number >= 1),
    org_id          TEXT NOT NULL,
    slug            TEXT NOT NULL,
    name            TEXT NOT NULL,
    description     TEXT NOT NULL DEFAULT '',
    owner           TEXT,
    status          TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL,
    updated_at      TIMESTAMPTZ NOT NULL,
    archived_at     TIMESTAMPTZ,
    change_kind     TEXT NOT NULL CHECK (change_kind IN ('seeded', 'created', 'updated', 'archived', 'restored')),
    changed_by      TEXT NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (domain_id, version_number)
);

CREATE TABLE IF NOT EXISTS app.mdm_business_classification_versions (
    classification_id   TEXT NOT NULL REFERENCES app.mdm_business_classifications(id) ON DELETE RESTRICT,
    version_number      INTEGER NOT NULL CHECK (version_number >= 1),
    org_id              TEXT NOT NULL,
    domain_id           TEXT NOT NULL,
    parent_id           TEXT,
    classification_type TEXT NOT NULL,
    slug                TEXT NOT NULL,
    name                TEXT NOT NULL,
    description         TEXT NOT NULL DEFAULT '',
    owner               TEXT,
    status              TEXT NOT NULL,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL,
    archived_at         TIMESTAMPTZ,
    change_kind         TEXT NOT NULL CHECK (change_kind IN ('created', 'updated', 'archived', 'restored')),
    changed_by          TEXT NOT NULL,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (classification_id, version_number)
);

CREATE OR REPLACE FUNCTION app.reject_business_taxonomy_version_mutation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Business taxonomy history is append-only: % blocked', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS trg_mdm_business_domain_versions_immutable ON app.mdm_business_domain_versions;
CREATE TRIGGER trg_mdm_business_domain_versions_immutable
    BEFORE UPDATE OR DELETE ON app.mdm_business_domain_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_business_taxonomy_version_mutation();
DROP TRIGGER IF EXISTS trg_mdm_business_domain_versions_no_truncate ON app.mdm_business_domain_versions;
CREATE TRIGGER trg_mdm_business_domain_versions_no_truncate
    BEFORE TRUNCATE ON app.mdm_business_domain_versions
    FOR EACH STATEMENT EXECUTE FUNCTION app.reject_business_taxonomy_version_mutation();

DROP TRIGGER IF EXISTS trg_mdm_business_classification_versions_immutable ON app.mdm_business_classification_versions;
CREATE TRIGGER trg_mdm_business_classification_versions_immutable
    BEFORE UPDATE OR DELETE ON app.mdm_business_classification_versions
    FOR EACH ROW EXECUTE FUNCTION app.reject_business_taxonomy_version_mutation();
DROP TRIGGER IF EXISTS trg_mdm_business_classification_versions_no_truncate ON app.mdm_business_classification_versions;
CREATE TRIGGER trg_mdm_business_classification_versions_no_truncate
    BEFORE TRUNCATE ON app.mdm_business_classification_versions
    FOR EACH STATEMENT EXECUTE FUNCTION app.reject_business_taxonomy_version_mutation();

CREATE OR REPLACE FUNCTION app.reject_business_classification_cycle()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.parent_id IS NULL THEN
        RETURN NEW;
    END IF;
    IF NEW.parent_id = NEW.id OR EXISTS (
        WITH RECURSIVE descendants AS (
            SELECT id FROM app.mdm_business_classifications WHERE parent_id = NEW.id
            UNION ALL
            SELECT child.id
            FROM app.mdm_business_classifications child
            JOIN descendants d ON child.parent_id = d.id
        )
        SELECT 1 FROM descendants WHERE id = NEW.parent_id
    ) THEN
        RAISE EXCEPTION 'Business classification hierarchy cannot contain a cycle'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mdm_business_classifications_no_cycle ON app.mdm_business_classifications;
CREATE TRIGGER trg_mdm_business_classifications_no_cycle
    BEFORE INSERT OR UPDATE OF parent_id ON app.mdm_business_classifications
    FOR EACH ROW EXECUTE FUNCTION app.reject_business_classification_cycle();

CREATE OR REPLACE FUNCTION app.seed_business_domains_for_org(target_org_id TEXT, actor TEXT)
RETURNS VOID LANGUAGE plpgsql AS $$
BEGIN
    WITH inserted AS (
        INSERT INTO app.mdm_business_domains
            (id, org_id, slug, name, description, owner, status, created_by)
        SELECT
            'bdm_' || substr(md5(target_org_id || ':' || template.slug), 1, 26),
            target_org_id,
            template.slug,
            template.name,
            template.description,
            NULL,
            'active',
            actor
        FROM app.mdm_business_domain_templates template
        ON CONFLICT (org_id, slug) DO NOTHING
        RETURNING *
    )
    INSERT INTO app.mdm_business_domain_versions
        (domain_id, version_number, org_id, slug, name, description, owner, status,
         created_by, created_at, updated_at, archived_at, change_kind, changed_by)
    SELECT id, 1, org_id, slug, name, description, owner, status, created_by,
           created_at, updated_at, archived_at, 'seeded', actor
    FROM inserted;
END;
$$;

CREATE OR REPLACE FUNCTION app.seed_business_domains_on_org_create()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    PERFORM app.seed_business_domains_for_org(NEW.id, COALESCE(NEW.created_by, 'system'));
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_organizations_seed_business_domains ON app.organizations;
CREATE TRIGGER trg_organizations_seed_business_domains
    AFTER INSERT ON app.organizations
    FOR EACH ROW EXECUTE FUNCTION app.seed_business_domains_on_org_create();

DO $$
DECLARE org_row RECORD;
BEGIN
    FOR org_row IN SELECT id, created_by FROM app.organizations LOOP
        PERFORM app.seed_business_domains_for_org(org_row.id, COALESCE(org_row.created_by, 'system'));
    END LOOP;
END
$$;

CREATE TABLE IF NOT EXISTS app.mdm_business_links (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id      TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    taxonomy_type   TEXT NOT NULL CHECK (taxonomy_type IN ('business_domain', 'business_classification')),
    taxonomy_id     TEXT NOT NULL,
    target_type     TEXT NOT NULL CHECK (target_type IN ('topic', 'procedure', 'target_field', 'schema_doc', 'report_view')),
    target_id       TEXT NOT NULL,
    relation_type   TEXT NOT NULL,
    link_origin     TEXT NOT NULL DEFAULT 'direct' CHECK (link_origin = 'direct'),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_mdm_business_link UNIQUE
        (project_id, taxonomy_type, taxonomy_id, target_type, target_id, relation_type)
);
CREATE INDEX IF NOT EXISTS idx_mdm_business_links_taxonomy
    ON app.mdm_business_links (org_id, project_id, taxonomy_type, taxonomy_id);
CREATE INDEX IF NOT EXISTS idx_mdm_business_links_target
    ON app.mdm_business_links (project_id, target_type, target_id);

CREATE OR REPLACE FUNCTION app.validate_business_link_scope()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM app.projects p
        WHERE p.id = NEW.project_id AND p.org_id = NEW.org_id AND p.status = 'active'
    ) THEN
        RAISE EXCEPTION 'Business link project is not active in the organization'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.taxonomy_type = 'business_domain' AND NOT EXISTS (
        SELECT 1 FROM app.mdm_business_domains d
        WHERE d.id = NEW.taxonomy_id AND d.org_id = NEW.org_id AND d.status = 'active'
    ) THEN
        RAISE EXCEPTION 'Business domain link source is not active in the organization'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.taxonomy_type = 'business_classification' AND NOT EXISTS (
        SELECT 1 FROM app.mdm_business_classifications c
        WHERE c.id = NEW.taxonomy_id AND c.org_id = NEW.org_id AND c.status = 'active'
    ) THEN
        RAISE EXCEPTION 'Business classification link source is not active in the organization'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mdm_business_links_scope ON app.mdm_business_links;
CREATE TRIGGER trg_mdm_business_links_scope
    BEFORE INSERT OR UPDATE ON app.mdm_business_links
    FOR EACH ROW EXECUTE FUNCTION app.validate_business_link_scope();

CREATE TABLE IF NOT EXISTS app.context_path_resolutions (
    id              TEXT PRIMARY KEY,
    org_id          TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id      TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    path_key        TEXT NOT NULL,
    purpose         TEXT NOT NULL CHECK (purpose IN ('llm', 'evaluation', 'report')),
    trace_id        TEXT,
    target_type     TEXT NOT NULL CHECK (target_type IN ('topic', 'procedure', 'target_field', 'schema_doc', 'report_view')),
    target_id       TEXT NOT NULL,
    link_origin     TEXT NOT NULL CHECK (link_origin IN ('direct', 'derived')),
    ordered_path    JSONB NOT NULL CHECK (jsonb_typeof(ordered_path) = 'array'),
    source_versions JSONB NOT NULL CHECK (jsonb_typeof(source_versions) = 'array'),
    created_by      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_context_path_resolutions_project_key
    ON app.context_path_resolutions (project_id, path_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_context_path_resolutions_trace
    ON app.context_path_resolutions (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_context_path_resolutions_target
    ON app.context_path_resolutions (project_id, target_type, target_id, created_at DESC);

-- Golden questions declare semantic routes. Runs retain both aggregate evidence and
-- per-question outcomes so reporting can separate missing, wrong and drifted paths.
ALTER TABLE app.golden_questions
    ADD COLUMN IF NOT EXISTS expected_business_routes JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(expected_business_routes) = 'array');

ALTER TABLE app.eval_runs
    ADD COLUMN IF NOT EXISTS observed_path_keys JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(observed_path_keys) = 'array'),
    ADD COLUMN IF NOT EXISTS observed_trace_ids JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(observed_trace_ids) = 'array'),
    ADD COLUMN IF NOT EXISTS path_coverage_pct NUMERIC(5,2),
    ADD COLUMN IF NOT EXISTS missing_path_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS wrong_domain_count INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS path_version_drift_count INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS app.eval_business_path_results (
    id                       TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    run_id                   TEXT NOT NULL REFERENCES app.eval_runs(id) ON DELETE CASCADE,
    question_id              TEXT NOT NULL REFERENCES app.golden_questions(id) ON DELETE CASCADE,
    expected_route           JSONB NOT NULL CHECK (jsonb_typeof(expected_route) = 'object'),
    observed_path_key        TEXT,
    trace_id                 TEXT,
    outcome                  TEXT NOT NULL CHECK (
        outcome IN ('pass', 'missing_path', 'wrong_domain', 'path_version_drift')
    ),
    observed_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, question_id)
);
CREATE INDEX IF NOT EXISTS idx_eval_business_path_results_run
    ON app.eval_business_path_results (run_id, outcome);
COMMIT;
