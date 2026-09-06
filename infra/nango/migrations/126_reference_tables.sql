-- infra/nango/migrations/126_reference_tables.sql
--
-- Reference tables: match a collected value against a correspondence base.
--
-- THE CASE (Jean, 2026-07-27, "c'est le pire cas")
-- 500 placements with 500 names; classifying pages. No formula carries 500 cases:
-- nested =if() would be unreadable, unmaintainable, and would put a mapping
-- someone must maintain weekly inside a field nobody can diff. This is a JOIN
-- against data, and it wants to be data.
--
-- SCOPED TO THE PROJECT, NOT THE DATASTREAM
-- A page -> category mapping is worth exactly as much on the Search Console
-- Datastream as on the GA4 one. Binding it to a single Datastream would mean
-- maintaining the same 500 rows several times, and the copies would drift.
--
-- EXACT MATCH ONLY
-- Lookup resolves on the exact key. Fuzzy matching is a different problem with a
-- different governance answer -- this codebase already has one for brand aliases
-- (migration 090: confidence scoring, ambiguity band, human ratification of the
-- residue). Quietly approximating a category here would produce a plausible wrong
-- answer with no trace, which is worse than no answer.
--
-- A MISS IS A MISS
-- An unmatched key returns nothing. There is no policy enum, no fallback ladder,
-- no silent default -- the caller decides what an unknown value looks like, and
-- the miss RATE is measurable because the entries are queryable. 122's
-- `on_no_match` is dropped here for that reason: it was a machine for something
-- that only needed to be honest.
--
-- ONE KEY, SEVERAL ANSWERS
-- Entries carry a JSONB attribute map rather than one value column, so a single
-- pass over the page name can yield category AND owner AND funnel stage. The
-- expression names which attribute it wants.
--
-- ORG SCOPING / RGPD: org_id is NOT NULL so migration 099's erasure can reach
-- these rows with the rest of the org tree -- a client's own mapping is tenant
-- data, unlike the platform-global dictionary.
--
-- Idempotent throughout.

BEGIN;

-- 122's non-match policy, withdrawn. Set aside on Jean's advice: an unknown value
-- is simply unknown, and a four-way policy column is a decision nobody asked to
-- make on every rule.
ALTER TABLE app.datastream_derived_columns DROP COLUMN IF EXISTS on_no_match;

CREATE TABLE IF NOT EXISTS app.reference_tables (
    id          TEXT        PRIMARY KEY,              -- prefixed ULID: 'reft_'
    project_id  TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    org_id      TEXT        NOT NULL REFERENCES app.organizations(id),

    -- Referenced from an expression as =lookup("<name>", "<column>", "<attribute>"),
    -- so it obeys the same identifier rule as a derived column name: quoting
    -- problems are refused at the source, not escaped by every consumer.
    name        TEXT        NOT NULL
                CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$'),
    description TEXT,

    -- The attributes an entry may carry, declared once. A lookup for an attribute
    -- absent from this list is a save-time error naming the available ones,
    -- instead of a column that silently returns nothing on every row.
    attributes  JSONB       NOT NULL DEFAULT '[]'::jsonb
                CHECK (jsonb_typeof(attributes) = 'array'
                       AND NOT jsonb_path_exists(attributes, '$[*] ? (@.type() != "string")')),

    created_by  TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_reference_tables_project_name
    ON app.reference_tables (project_id, name);

CREATE TABLE IF NOT EXISTS app.reference_table_entries (
    table_id    TEXT        NOT NULL
                REFERENCES app.reference_tables(id) ON DELETE CASCADE,

    -- The value being matched, verbatim as collected. NOT normalised on the way
    -- in: two placements differing only by case are two different strings to the
    -- provider, and deciding they are the same is the operator's call, not the
    -- storage layer's.
    key_value   TEXT        NOT NULL CHECK (length(key_value) BETWEEN 1 AND 1000),

    -- {"category": "product", "owner": "growth"} -- one pass, several answers.
    attributes  JSONB       NOT NULL DEFAULT '{}'::jsonb
                CHECK (jsonb_typeof(attributes) = 'object'),

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- The lookup index as well as the uniqueness guarantee: one answer per key,
    -- so no read has to choose between two rows.
    CONSTRAINT pk_reference_table_entries PRIMARY KEY (table_id, key_value)
);

COMMENT ON TABLE app.reference_tables IS
    'Project-scoped correspondence bases matched from expressions via lookup(). '
    'Exact key match only -- approximate matching is a governed problem with its '
    'own model (migration 090), not a silent default here.';
COMMENT ON COLUMN app.reference_tables.attributes IS
    'Attribute names an entry may carry. A lookup for an attribute outside this '
    'list fails at save time, naming the available ones, rather than returning '
    'nothing on every row forever.';
COMMENT ON COLUMN app.reference_table_entries.key_value IS
    'Matched verbatim, exactly as collected. Deliberately not case-folded or '
    'trimmed on write: whether two spellings are the same value is the operator''s '
    'decision, and storage must not make it for them.';

DO $$
DECLARE fn TEXT;
BEGIN
    SELECT p.proname INTO fn
      FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'app' AND p.proname IN ('touch_updated_at', 'set_updated_at')
     ORDER BY p.proname = 'touch_updated_at' DESC LIMIT 1;
    IF fn IS NOT NULL THEN
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_reference_tables_updated_at') THEN
            EXECUTE format('CREATE TRIGGER trg_reference_tables_updated_at BEFORE UPDATE '
                           'ON app.reference_tables FOR EACH ROW EXECUTE FUNCTION app.%I()', fn);
        END IF;
        IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_reference_entries_updated_at') THEN
            EXECUTE format('CREATE TRIGGER trg_reference_entries_updated_at BEFORE UPDATE '
                           'ON app.reference_table_entries FOR EACH ROW EXECUTE FUNCTION app.%I()', fn);
        END IF;
    END IF;
END
$$;

COMMIT;
