-- Story 60.1: a client value table names the streams it serves.
--
-- WHAT WAS MISSING, MEASURED. `app.dimension_value_mappings` (052) already holds
-- `source_value -> canonical_value` and is read at render time by the geography
-- and language paths. What it cannot hold is the OBJECT: it is a flat store keyed
-- by `(canonical_dimension, connector)` (052:92-94) with no name a client gives,
-- no description, and no way to say "this table applies to those six Datastreams,
-- on that column". `doc/datastream/03-transformations-and-custom-fields.md:43`
-- promises exactly that (« affectables a plusieurs Datastreams ») and nothing in
-- the schema carried it.
--
-- WHY A SECOND STORE RATHER THAN A `table_id` COLUMN ON 052 -- AND THE DEBT IT
-- CREATES, WRITTEN DOWN RATHER THAN DISCOVERED LATER.
--   The parent-object variant was preferred first: it keeps ONE authority over
--   "what does this source value become" and leaves `conform_value` untouched.
--   The measurement refuses it. `dimension_value_mappings.connector` is NOT NULL
--   and part of the unique key (052:92-94, 052:128-131), while a table the client
--   assigns to Datastreams of DIFFERENT connectors cannot carry a single
--   connector. Making that column nullable would rewrite an index the live
--   geography and language read paths depend on.
--   So there are now TWO stores answering "what does this source value become",
--   and that is a real cost, stated here rather than hidden:
--     * 052 holds the GOVERNED, semi-automatic suggestions of a conformed
--       dimension (proposed -> confirmed -> rejected, AD-9);
--     * this migration holds the CLIENT's own vocabulary, written by hand or
--       imported, and it is not read by any render path yet.
--   Story 60.1 wires NOTHING into the read: `conform_value` is unchanged and this
--   store is not consulted at render time. **WHICH OF THE TWO WINS WHEN BOTH
--   ANSWER IS DECIDED BY STORY 60.5** (action item AI-238), and until it is
--   decided nothing here may be applied to a report.
--
-- NO `status` COLUMN ON AN ENTRY, ON PURPOSE. 052 carries
-- proposed/confirmed/rejected because its rows are SUGGESTED by a machine and
-- `conform_value` resolves confirmed ones only (052:83-88). An entry here is
-- typed or imported BY the client: it is alive the moment it is written, and
-- asking someone to confirm what they just typed is friction with no subject. A
-- CSV imported as 'proposed' would apply to nothing at all. The proposal regime
-- stays where the proposals are made, and this migration does not touch 052.
--
-- THE ASSIGNED FIELD IS A RAW SOURCE COLUMN, NOT A CONFORMED CONCEPT. Same choice
-- and same reason as `app.datastream_derived_columns` (122:62-64): "a rule may
-- read a column that was never mapped", which is the common case for placement
-- names. A value table exists precisely to normalise a column BEFORE anything
-- maps it, so a foreign key to `app.datastream_mappings` would make it unusable
-- on its main case. The calculated fields of story 60.2 carry the opposite rule
-- (concepts only) -- two objects, two rules.
--
-- ORG SCOPING / RGPD, AND THE EXACT MECHANISM THAT ERASES THESE ROWS. `org_id`
-- is NOT NULL on the table and on the assignment, and both foreign keys are
-- ON DELETE CASCADE, so a tenant erasure takes them with the org row itself
-- (`admin_api.py:8515`, the DELETE at the end of the erasure). Entries hang off
-- their table and are reached through it, by the same cascade.
--
-- It is NOT `core.org_purge` that reaches them, and saying so would send the
-- next reader to the wrong file: `plan_purge` walks the FK graph through
-- `confdeltype IN ('a','r')` only (`org_purge.py:97`) -- NO ACTION and RESTRICT.
-- A CASCADE edge is deliberately absent from that plan because Postgres already
-- does the work, and measuring it says so: `plan_purge` emits 0 statements
-- naming `value_mapping_*` out of 2239. The FK is still what makes the erasure
-- reach here (same motive as 122:43-46) -- an org-scoped table that names no
-- parent is invisible to BOTH mechanisms.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, trigger creation guarded.

BEGIN;

-- ---------------------------------------------------------------------------
-- The object the client names.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.value_mapping_tables (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'vmt_'
    org_id          TEXT        NOT NULL
                    REFERENCES app.organizations(id) ON DELETE CASCADE,
    -- NULL for an ORG-scoped table: it is visible to every Project of the org.
    project_id      TEXT        REFERENCES app.projects(id) ON DELETE CASCADE,
    -- PLATFORM is absent from this CHECK and that is the schema half of the
    -- API rule: seeds are the platform authority (dimension_lineage_api.py:14-16),
    -- so no client write can ever reach a platform-wide vocabulary.
    scope_level     TEXT        NOT NULL CHECK (scope_level IN ('ORG', 'PROJECT')),
    name            TEXT        NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 160),
    description     TEXT        CHECK (description IS NULL OR length(description) <= 2000),
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT ck_value_mapping_tables_scope CHECK (
        (scope_level = 'ORG'     AND project_id IS NULL)
     OR (scope_level = 'PROJECT' AND project_id IS NOT NULL)
    )
);

-- Two tables of the same scope cannot answer to the same name: every
-- confirmation sentence in the surface names the table, and two identical names
-- make that sentence a lie. COALESCE is mandatory -- NULL <> NULL would let two
-- ORG rows through (the discipline of 052:125-127).
CREATE UNIQUE INDEX IF NOT EXISTS uq_value_mapping_tables_name
    ON app.value_mapping_tables (org_id, COALESCE(project_id, ''), lower(btrim(name)));

CREATE INDEX IF NOT EXISTS ix_value_mapping_tables_project
    ON app.value_mapping_tables (project_id);

COMMENT ON TABLE app.value_mapping_tables IS
    'Story 60.1: a NAMED lookup table the client writes for its own vocabulary '
    '(50 campaign names -> 3 product lines). Distinct from '
    'app.dimension_value_mappings (052), which holds the governed semi-automatic '
    'suggestions of a conformed dimension: this one is not read by any render '
    'path, and which store wins when both answer is decided by story 60.5.';

-- ---------------------------------------------------------------------------
-- The pairs. No status -- see the header.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.value_mapping_entries (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'vment_'
    table_id        TEXT        NOT NULL
                    REFERENCES app.value_mapping_tables(id) ON DELETE CASCADE,
    source_value    TEXT        NOT NULL CHECK (length(source_value) BETWEEN 1 AND 1024),
    canonical_value TEXT        NOT NULL CHECK (length(canonical_value) BETWEEN 1 AND 1024),
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One live pair per source value per table. A second pair for the same source
-- value would be two answers to one question with no defined precedence -- the
-- shape 122:105-109 protects a derived column from.
CREATE UNIQUE INDEX IF NOT EXISTS uq_value_mapping_entries_source
    ON app.value_mapping_entries (table_id, source_value);

COMMENT ON COLUMN app.value_mapping_entries.source_value IS
    'The value as the source emits it. There is deliberately NO status column: '
    'an entry the client typed or imported is live at write time, unlike the '
    'proposed/confirmed/rejected suggestions of migration 052.';

-- ---------------------------------------------------------------------------
-- Where a table applies. The triplet, unique.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.value_mapping_assignments (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'vmasg_'
    table_id        TEXT        NOT NULL
                    REFERENCES app.value_mapping_tables(id) ON DELETE CASCADE,
    datastream_id   TEXT        NOT NULL
                    REFERENCES app.datastreams(id) ON DELETE CASCADE,
    org_id          TEXT        NOT NULL
                    REFERENCES app.organizations(id) ON DELETE CASCADE,
    -- The collected column the table applies to. Deliberately NOT a foreign key
    -- to app.datastream_mappings, for the reason 122:62-64 states.
    source_field    TEXT        NOT NULL CHECK (length(btrim(source_field)) BETWEEN 1 AND 320),
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_value_mapping_assignments_triplet
        UNIQUE (table_id, datastream_id, source_field)
);

CREATE INDEX IF NOT EXISTS ix_value_mapping_assignments_datastream
    ON app.value_mapping_assignments (datastream_id);

COMMENT ON TABLE app.value_mapping_assignments IS
    'Story 60.1: which Datastream and which raw column one value table applies '
    'to. Read BEFORE a modification so the count of affected Datastreams is '
    'stated before the confirmation, never after -- and a read that fails is '
    'reported as unknown, never as zero.';

-- ---------------------------------------------------------------------------
-- updated_at. The helper name is LOOKED UP rather than assumed: two exist in the
-- corpus (app.set_updated_at, app.touch_updated_at) and the schema is not
-- linear, so a missing helper must not fail the migration (122:114-118).
-- ---------------------------------------------------------------------------
DO $$
DECLARE
    fn TEXT;
    tbl TEXT;
BEGIN
    SELECT p.proname INTO fn
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'app'
       AND p.proname IN ('set_updated_at', 'touch_updated_at')
     ORDER BY p.proname = 'set_updated_at' DESC
     LIMIT 1;

    IF fn IS NULL THEN
        RETURN;
    END IF;

    FOREACH tbl IN ARRAY ARRAY['value_mapping_tables', 'value_mapping_entries'] LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_trigger WHERE tgname = 'trg_' || tbl || '_updated_at'
        ) THEN
            EXECUTE format(
                'CREATE TRIGGER %I BEFORE UPDATE ON app.%I '
                'FOR EACH ROW EXECUTE FUNCTION app.%I()',
                'trg_' || tbl || '_updated_at', tbl, fn
            );
        END IF;
    END LOOP;
END
$$;

COMMIT;
