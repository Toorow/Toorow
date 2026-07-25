-- infra/nango/migrations/097_file_source_templates.sql
--
-- Story 22.11 (Epic 22 Phase B-1): Create and version a file-source template.
--
-- Introduces app.file_source_templates -- the ONE immutable, content-hashed,
-- versioned template artifact for the file-source ingestion framework (AD-2).
-- A file-source template declares the canonical TARGET a later import maps onto
-- (required canonical fields + grain + matrix class + placement coordinates),
-- independent of any file's shifting layout.
--
-- Design (mirrors 032/078/088 exactly):
--   * kind IN ('catalog','adaptation): a catalog template holds a declarative
--     JSONB contract; an adaptation template holds a locked .py + its hash
--     (Phase B-3). Both resolve to ONE producer interface (AD-2).
--   * One row per VERSION, append-only. version = MAX(version)+1 per
--     (project_id, template_code). A new content = a new version row; an edit is
--     NEVER an UPDATE of an existing row.
--   * content_hash = SHA-256 of the canonical (sorted-key) contract JSON; a
--     UNIQUE (project_id, template_code, content_hash) makes an IDENTICAL
--     re-creation return the existing version (idempotent), a DIFFERENT one
--     append a new version.
--   * idempotency_key_hash mirrors the AD-27 operation idempotency (the create
--     is written through operations.execute_operation).
--   * placement_class + grain are hoisted to NOT NULL columns: a template with
--     no declared class cannot exist (AD-6, defense-in-depth over the app gate).
--   * An immutability trigger freezes every identity field on BEFORE UPDATE OR
--     DELETE; only label + is_active (human annotations / lifecycle) may change.
--
-- Additive and idempotent (IF NOT EXISTS / OR REPLACE / DROP TRIGGER IF EXISTS).
-- Safe to re-apply after any preceding migration (highest on disk = 096).
--
-- Apply with (human-gated on Jean's authorization):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/097_file_source_templates.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.file_source_templates
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.file_source_templates (
    -- Identity (immutable after insert).
    id                    TEXT        NOT NULL PRIMARY KEY,   -- fst_<ULID>
    project_id            TEXT        NOT NULL,
    template_code         TEXT        NOT NULL,
    version               INT         NOT NULL,
    kind                  TEXT        NOT NULL
                              CHECK (kind IN ('catalog', 'adaptation')),
    content_hash          TEXT        NOT NULL
                              CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash  TEXT        NOT NULL
                              CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    contract              JSONB       NOT NULL,               -- normalised contract doc
    -- Hoisted from the contract for querying + the AD-6 no-class-refused guard.
    placement_class       TEXT        NOT NULL
                              CHECK (placement_class IN ('planned', 'actual', 'extrapolated')),
    grain                 TEXT        NOT NULL,
    created_by            TEXT        NOT NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Mutable (human annotations / lifecycle).
    label                 TEXT        NULL,
    is_active             BOOLEAN     NOT NULL DEFAULT TRUE,

    -- House-style ULID id (Crockford base32, 26 chars) -- cf. mdm_/cic_/dse_.
    CONSTRAINT ck_file_source_templates_id
        CHECK (id ~ '^fst_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT ck_file_source_templates_code
        CHECK (template_code ~ '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$'),
    CONSTRAINT ck_file_source_templates_version
        CHECK (version >= 1)
);

-- One immutable row per (project, code, version).
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_source_template_version
    ON app.file_source_templates (project_id, template_code, version);

-- Identical contract dedup: same content => same version, no duplicate rows.
CREATE UNIQUE INDEX IF NOT EXISTS uq_file_source_template_content
    ON app.file_source_templates (project_id, template_code, content_hash);

-- Fast lookup: all versions for a template, newest first.
CREATE INDEX IF NOT EXISTS ix_file_source_template_code
    ON app.file_source_templates (project_id, template_code, created_at DESC);

-- Fast lookup: only active templates.
CREATE INDEX IF NOT EXISTS ix_file_source_template_active
    ON app.file_source_templates (project_id, template_code)
    WHERE is_active = TRUE;

-- ---------------------------------------------------------------------------
-- Immutability trigger: freeze identity fields on UPDATE / DELETE.
-- Mirrors the pattern of 078 exactly (only label + is_active are mutable).
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_file_source_template_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'file_source_templates is append-only: '
            'DELETE of id=% is forbidden (immutable template version).',
            OLD.id;
    END IF;
    -- UPDATE: freeze the identity fields; allow only label + is_active changes.
    IF NEW.id                   IS DISTINCT FROM OLD.id                   OR
       NEW.project_id           IS DISTINCT FROM OLD.project_id           OR
       NEW.template_code        IS DISTINCT FROM OLD.template_code        OR
       NEW.version              IS DISTINCT FROM OLD.version              OR
       NEW.kind                 IS DISTINCT FROM OLD.kind                 OR
       NEW.content_hash         IS DISTINCT FROM OLD.content_hash         OR
       NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash OR
       NEW.contract             IS DISTINCT FROM OLD.contract             OR
       NEW.placement_class      IS DISTINCT FROM OLD.placement_class      OR
       NEW.grain                IS DISTINCT FROM OLD.grain                OR
       NEW.created_by           IS DISTINCT FROM OLD.created_by           OR
       NEW.created_at           IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION
            'file_source_templates: identity fields are immutable after insert '
            '(id=%). Only label and is_active may be updated.',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_file_source_template_immutable
    ON app.file_source_templates;

CREATE TRIGGER trg_file_source_template_immutable
    BEFORE UPDATE OR DELETE ON app.file_source_templates
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_file_source_template_mutation();

COMMIT;
