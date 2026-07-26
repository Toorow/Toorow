-- infra/nango/migrations/107_target_fields_versions.sql
--
-- Story 44.7: target_fields versioning -- "who changed what, when" for the
-- data dictionary. Copies the version-append pattern already used by
-- app.context_topics_versions / app.procedures_versions (server/core/
-- context_store.py): one row per mutation, full snapshot of the parent row
-- plus change_kind + diff + changed_by/changed_at, PK (name, version_number).
--
-- R44-NFR01: the Supabase schema is NOT linear (043 was warehouse-only; 095/097
-- applied but 102/104/105 not, per the schema drift memory). This migration
-- asserts app.target_fields' ACTUAL current columns and its ACTUAL current
-- status CHECK constraint via information_schema / pg_constraint before
-- touching anything -- it does not assume migrations 023/050 ran in that
-- exact numeric order.
--
-- app.target_fields columns as they exist today (023_datastreams.sql +
-- 050_target_fields_governance.sql):
--   name, display_name, data_type, field_kind, measure, description,
--   created_by, is_default, created_at, status, approved_at, approved_by,
--   updated_at.
-- target_fields_versions snapshots every one of those columns, plus the
-- versioning columns (version_number, change_kind, diff, changed_by, changed_at).
--
-- ORG SCOPING / RGPD (099 allowlist) -- deliberately NOT added:
-- app.target_fields is PLATFORM-GLOBAL (no org_id / project_id column at all --
-- confirmed by core.datamodel.get_target_field's docstring and by org_purge.py,
-- which never references target_fields). Exactly like app.target_field_approvals
-- and app.context_topics_versions, target_fields_versions belongs to no tenant,
-- so no org erasure has any business deleting it -- the 099 allowlist comment
-- block explicitly lists that family as "global reference data ... absent".
-- Nothing to add there; see server/tests/core/test_target_fields_versioning.py
-- for the behavioural side of this story instead.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, ADD CONSTRAINT guarded by a DO block
-- that reads pg_constraint for the actual current definition (never assumes
-- a constraint name), CREATE INDEX IF NOT EXISTS.

BEGIN;

-- ---------------------------------------------------------------------------
-- 0. R44-NFR01 guard: assert migration 050's governance columns are actually
-- present before touching anything. The schema is NOT linear (memory:
-- Supabase-migrations-applied-094 -- 095/097 applied but 102/104/105 not; 106
-- applied out of sequence), so "050 has a lower number than 107" proves
-- nothing -- check information_schema.columns for the real column set.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF to_regclass('app.target_fields') IS NULL THEN
        -- Do NOT pretend to skip: the CREATE TABLE below carries a FK
        -- REFERENCES app.target_fields(name) and would abort anyway with a
        -- less actionable error. Fail loud and early instead.
        RAISE EXCEPTION 'app.target_fields is missing -- apply migration 023 first (107 cannot proceed)';
    END IF;

    IF NOT (
        SELECT count(*) = 4
        FROM information_schema.columns
        WHERE table_schema = 'app'
          AND table_name = 'target_fields'
          AND column_name IN ('status', 'approved_at', 'approved_by', 'updated_at')
    ) THEN
        RAISE EXCEPTION
            'app.target_fields is missing one of status/approved_at/approved_by/'
            'updated_at -- apply migration 050 first';
    END IF;
END
$$;

-- ---------------------------------------------------------------------------
-- 1. Extend app.target_fields.status CHECK to allow 'deleted' (soft delete).
--
-- The constraint was created inline by an ADD COLUMN ... CHECK in migration
-- 050 (auto-named by Postgres, e.g. target_fields_status_check) -- per
-- R44-NFR01 we do not assume that name or that migration ran. Instead: find
-- the CHECK constraint on app.target_fields that actually encodes the
-- draft/approved status enum (defensively narrowed: must mention 'status'
-- AND both 'draft' and 'approved' literals -- a bare '%status%' ILIKE would
-- also match an unrelated CHECK that merely references the column, e.g. a
-- future cross-column constraint), and only touch it if 'deleted' is not
-- already allowed. More than one match means our assumption about this
-- table's constraints no longer holds -- fail loudly instead of guessing.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    con RECORD;
    match_count INTEGER;
BEGIN
    IF to_regclass('app.target_fields') IS NULL THEN
        RAISE NOTICE 'app.target_fields does not exist -- skipping status CHECK extension';
        RETURN;
    END IF;

    SELECT count(*) INTO match_count
    FROM pg_constraint c
    WHERE c.conrelid = 'app.target_fields'::regclass
      AND c.contype = 'c'
      AND pg_get_constraintdef(c.oid) ILIKE '%status%'
      AND pg_get_constraintdef(c.oid) ILIKE '%draft%'
      AND pg_get_constraintdef(c.oid) ILIKE '%approved%';

    IF match_count > 1 THEN
        RAISE EXCEPTION
            'app.target_fields has % CHECK constraints matching status/draft/'
            'approved -- expected at most 1, refusing to guess which to rewrite',
            match_count;
    END IF;

    FOR con IN
        SELECT c.conname, pg_get_constraintdef(c.oid) AS def
        FROM pg_constraint c
        WHERE c.conrelid = 'app.target_fields'::regclass
          AND c.contype = 'c'
          AND pg_get_constraintdef(c.oid) ILIKE '%status%'
          AND pg_get_constraintdef(c.oid) ILIKE '%draft%'
          AND pg_get_constraintdef(c.oid) ILIKE '%approved%'
    LOOP
        IF con.def ILIKE '%''deleted''%' THEN
            RAISE NOTICE 'skip %: already allows deleted', con.conname;
            CONTINUE;
        END IF;

        EXECUTE format('ALTER TABLE app.target_fields DROP CONSTRAINT %I', con.conname);
        EXECUTE format(
            'ALTER TABLE app.target_fields ADD CONSTRAINT %I '
            'CHECK (status IN (''draft'', ''approved'', ''deleted''))',
            con.conname
        );
        RAISE NOTICE 'extended % to allow deleted', con.conname;
    END LOOP;
END
$$;

-- ---------------------------------------------------------------------------
-- 2. app.target_fields_versions -- append-only version history.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.target_fields_versions (
    name            TEXT        NOT NULL
                    REFERENCES app.target_fields(name) ON DELETE CASCADE,
    version_number  INTEGER     NOT NULL,

    -- Full snapshot of app.target_fields at the time of this version.
    display_name    TEXT,
    data_type       TEXT,
    field_kind      TEXT,
    measure         TEXT,
    description     TEXT,
    created_by      TEXT,
    is_default      BOOLEAN,
    created_at      TIMESTAMPTZ,
    status          TEXT,
    approved_at     TIMESTAMPTZ,
    approved_by     TEXT,
    updated_at      TIMESTAMPTZ,

    -- Versioning metadata.
    change_kind     TEXT        NOT NULL
                    CHECK (change_kind IN ('created', 'updated', 'approved',
                                           'deleted', 'restored')),
    diff            JSONB,
    changed_by      TEXT        NOT NULL,
    changed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT pk_target_fields_versions PRIMARY KEY (name, version_number)
);

CREATE INDEX IF NOT EXISTS idx_target_fields_versions_name_changed_at
    ON app.target_fields_versions (name, changed_at DESC);

COMMENT ON TABLE app.target_fields_versions IS
    'Story 44.7 -- one row per app.target_fields mutation (create/patch/approve/'
    'soft-delete/restore), full column snapshot + diff + real changed_by identity. '
    'Append-only enforced by trigger (see trg_target_fields_versions_immutable '
    'below) -- convention alone was not enough of a guarantee.';

-- ---------------------------------------------------------------------------
-- 3. Append-only guard trigger (ORCHESTRATOR DECISION, finding 44.7#10):
-- same reject-mutation pattern as app.context_topics_versions /
-- app.procedures_versions in migration 031 (BEFORE UPDATE OR DELETE row
-- trigger + BEFORE TRUNCATE statement trigger). Dedicated functions (not a
-- reuse of app.reject_context_version_mutation) so the error message names
-- the actual table.
--
-- ORG SCOPING / RGPD NOTE: app.target_fields_versions has no org_id/
-- project_id column at all -- app.target_fields is PLATFORM-GLOBAL (see the
-- header comment above and core.datamodel.get_target_field's docstring).
-- Org-purge (org_purge.py) never references target_fields or
-- target_fields_versions, and the migration 099 RGPD-erasure allowlist does
-- NOT apply here for the same reason it does not apply to
-- context_topics_versions/procedures_versions: there is no tenant to purge
-- this table for. Do NOT add this table to the 099 allowlist.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_target_fields_version_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'app.target_fields_versions is append-only: % blocked', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_target_fields_versions_immutable
    ON app.target_fields_versions;
CREATE TRIGGER trg_target_fields_versions_immutable
    BEFORE UPDATE OR DELETE ON app.target_fields_versions
    FOR EACH ROW
    EXECUTE FUNCTION app.reject_target_fields_version_mutation();

CREATE OR REPLACE FUNCTION app.reject_target_fields_version_truncate()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'app.target_fields_versions is append-only: TRUNCATE blocked'
        USING ERRCODE = 'raise_exception';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_target_fields_versions_no_truncate
    ON app.target_fields_versions;
CREATE TRIGGER trg_target_fields_versions_no_truncate
    BEFORE TRUNCATE ON app.target_fields_versions
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_target_fields_version_truncate();

COMMIT;
