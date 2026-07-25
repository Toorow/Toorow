-- Story 11.1: Context layer tables, procedures, graph, schema_context, and append-only version tables (AD-17)
-- Additive after migrations 001-030.

BEGIN;

-- 1. context_topics
CREATE TABLE IF NOT EXISTS app.context_topics (
    id          TEXT        NOT NULL PRIMARY KEY, -- top_<ULID>
    project_id  TEXT        NULL,                -- NULL = platform scope, non-NULL = project scope (AD-5)
    title       TEXT        NOT NULL,
    body_md     TEXT        NOT NULL DEFAULT '',
    status      TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_by  TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_context_topics_project ON app.context_topics (project_id);

-- 2. procedures
CREATE TABLE IF NOT EXISTS app.procedures (
    id               TEXT        NOT NULL PRIMARY KEY, -- proc_<ULID>
    project_id       TEXT        NULL,                -- NULL = platform scope, non-NULL = project scope (AD-5)
    name             TEXT        NOT NULL,
    description      TEXT        NOT NULL DEFAULT '',
    frontmatter_yaml TEXT        NOT NULL,
    body_md          TEXT        NOT NULL DEFAULT '',
    status           TEXT        NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    created_by       TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_procedures_project ON app.procedures (project_id);

-- Partial unique indexes to handle NULL project_id platform scope vs project scope (active status only)
CREATE UNIQUE INDEX IF NOT EXISTS uq_procedures_name_platform ON app.procedures (name) WHERE project_id IS NULL AND status != 'archived';
CREATE UNIQUE INDEX IF NOT EXISTS uq_procedures_name_project ON app.procedures (project_id, name) WHERE project_id IS NOT NULL AND status != 'archived';

-- 3. context_graph
CREATE TABLE IF NOT EXISTS app.context_graph (
    id          TEXT        NOT NULL PRIMARY KEY, -- edge_<ULID>
    from_id     TEXT        NOT NULL,
    from_type   TEXT        NOT NULL CHECK (from_type IN ('topic', 'procedure', 'schema_doc')),
    to_id       TEXT        NOT NULL,
    to_type     TEXT        NOT NULL CHECK (to_type IN ('topic', 'procedure', 'schema_doc')),
    edge_type   TEXT        NOT NULL,
    project_id  TEXT        NULL,
    created_by  TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_context_graph_project ON app.context_graph (project_id);
CREATE INDEX IF NOT EXISTS idx_context_graph_from ON app.context_graph (from_id);
CREATE INDEX IF NOT EXISTS idx_context_graph_to ON app.context_graph (to_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_context_graph_edge ON app.context_graph (from_id, from_type, to_id, to_type, edge_type);

-- 4. schema_context (AD-17 / AD-8 single writer)
CREATE TABLE IF NOT EXISTS app.schema_context (
    id           TEXT        NOT NULL PRIMARY KEY, -- sctx_<ULID>
    project_id   TEXT        NOT NULL,
    relation     TEXT        NOT NULL,
    doc_kind     TEXT        NOT NULL CHECK (doc_kind IN ('columns', 'description', 'preview')),
    body_md      TEXT        NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

ALTER TABLE app.schema_context DROP CONSTRAINT IF EXISTS uq_schema_context_key;
ALTER TABLE app.schema_context ADD CONSTRAINT uq_schema_context_key UNIQUE (project_id, relation, doc_kind);

CREATE INDEX IF NOT EXISTS idx_schema_context_project ON app.schema_context (project_id);

-- 5. context_topics_versions (append-only history)
CREATE TABLE IF NOT EXISTS app.context_topics_versions (
    topic_id       TEXT        NOT NULL,
    project_id     TEXT        NULL,
    title          TEXT        NOT NULL,
    body_md        TEXT        NOT NULL DEFAULT '',
    status         TEXT        NOT NULL CHECK (status IN ('active', 'archived')),
    created_by     TEXT        NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL,
    updated_at     TIMESTAMPTZ NOT NULL,
    version_number INTEGER     NOT NULL CHECK (version_number >= 1),
    changed_by     TEXT        NOT NULL,
    changed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (topic_id, version_number)
);

-- 6. procedures_versions (append-only history)
CREATE TABLE IF NOT EXISTS app.procedures_versions (
    procedure_id     TEXT        NOT NULL,
    project_id       TEXT        NULL,
    name             TEXT        NOT NULL,
    description      TEXT        NOT NULL DEFAULT '',
    frontmatter_yaml TEXT        NOT NULL,
    body_md          TEXT        NOT NULL DEFAULT '',
    status           TEXT        NOT NULL CHECK (status IN ('active', 'archived')),
    created_by       TEXT        NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL,
    updated_at       TIMESTAMPTZ NOT NULL,
    version_number   INTEGER     NOT NULL CHECK (version_number >= 1),
    changed_by       TEXT        NOT NULL,
    changed_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (procedure_id, version_number)
);

-- 7. schema_context_versions (append-only history -- Story 11.2 fix pass, Jean 2026-07-20)
-- Historises every content change to app.schema_context. The generator appends the
-- PREVIOUS body (pre-update snapshot) so the version chain records what each doc looked
-- like before it was overwritten (see upsert_schema_context_doc in schema_context_gen.py).
-- Same shape/immutability pattern as context_topics_versions / procedures_versions.
CREATE TABLE IF NOT EXISTS app.schema_context_versions (
    schema_context_id TEXT        NOT NULL,           -- FK-ish to app.schema_context.id (sctx_<ULID>)
    project_id        TEXT        NOT NULL,
    relation          TEXT        NOT NULL,
    doc_kind          TEXT        NOT NULL CHECK (doc_kind IN ('columns', 'description', 'preview')),
    body_md           TEXT        NOT NULL,
    generated_at      TIMESTAMPTZ NOT NULL,
    version_number    INTEGER     NOT NULL CHECK (version_number >= 1),
    changed_by        TEXT        NOT NULL,
    changed_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (schema_context_id, version_number)
);

CREATE INDEX IF NOT EXISTS idx_schema_context_versions_key
    ON app.schema_context_versions (project_id, relation, doc_kind);

-- Triggers for append-only versions tables
CREATE OR REPLACE FUNCTION app.reject_context_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Context versions tables are append-only: % blocked', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS trg_context_topics_versions_immutable ON app.context_topics_versions;
CREATE TRIGGER trg_context_topics_versions_immutable
    BEFORE UPDATE OR DELETE ON app.context_topics_versions
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_context_version_mutation();

DROP TRIGGER IF EXISTS trg_procedures_versions_immutable ON app.procedures_versions;
CREATE TRIGGER trg_procedures_versions_immutable
    BEFORE UPDATE OR DELETE ON app.procedures_versions
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_context_version_mutation();

DROP TRIGGER IF EXISTS trg_schema_context_versions_immutable ON app.schema_context_versions;
CREATE TRIGGER trg_schema_context_versions_immutable
    BEFORE UPDATE OR DELETE ON app.schema_context_versions
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_context_version_mutation();

-- Statement-level BEFORE TRUNCATE triggers (mirror migration 004 pattern).
-- Row-level triggers above do NOT fire on TRUNCATE; the owner retains TRUNCATE
-- privilege regardless of REVOKE. A statement-level trigger closes this gap.

CREATE OR REPLACE FUNCTION app.reject_context_version_truncate()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'Context versions tables are append-only: TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS trg_context_topics_versions_no_truncate ON app.context_topics_versions;
CREATE TRIGGER trg_context_topics_versions_no_truncate
    BEFORE TRUNCATE ON app.context_topics_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_context_version_truncate();

DROP TRIGGER IF EXISTS trg_procedures_versions_no_truncate ON app.procedures_versions;
CREATE TRIGGER trg_procedures_versions_no_truncate
    BEFORE TRUNCATE ON app.procedures_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_context_version_truncate();

DROP TRIGGER IF EXISTS trg_schema_context_versions_no_truncate ON app.schema_context_versions;
CREATE TRIGGER trg_schema_context_versions_no_truncate
    BEFORE TRUNCATE ON app.schema_context_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_context_version_truncate();

COMMIT;
