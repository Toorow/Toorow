-- infra/nango/migrations/122_derived_columns.sql
--
-- Derived columns: split one collected column into one or more new ones.
--
-- The need, stated by Jean on 2026-07-27: "tu as un champ nom du placement et tu
-- veux le decouper -- creer une colonne targeting et une colonne taille ou type
-- d'achat par extrait de regex", and "tu dois pouvoir masquer ou non la cellule
-- du report". Media placement names encode targeting, format and buy type in a
-- single string; every media team parses them, and today nothing in this product
-- can. app.datastream_mappings is a pure 1:1 rename (source_field ->
-- target_field, migration 023) with no room for a transformation, and
-- app.metric_definitions can express a ratio and nothing else -- there is no
-- expression column anywhere. So this is a new object rather than a widening.
--
-- WHAT ONE ROW IS
-- One rule: a source column, a regular expression, and the ordered list of
-- columns its capture groups produce. One rule can yield SEVERAL columns, which
-- is the point -- a single pass over "FR_DISPLAY_300x250_CPM_RETARGETING" gives
-- targeting, size and buy type together, instead of three rules each re-matching
-- the same string.
--
-- APPLIED AT READ, NOT AT INGESTION
-- Rules are evaluated when rows are served, never baked into the raw extract.
-- Three consequences, all deliberate:
--   * changing a regex costs nothing -- no refetch, no 16-month backfill because
--     a pattern was wrong the first time;
--   * the collected data stays exactly as the provider returned it (AD-7: raw is
--     append-only evidence, and a parsing opinion is not evidence);
--   * a rule can be deleted and the source column is still there.
--
-- NO MATCH IS A DECLARED OUTCOME
-- Placement naming conventions are never respected in practice: some rows will
-- not match. `on_no_match` forces that decision to be made and recorded rather
-- than defaulted to a silent NULL, because a rule that quietly nulls 40% of rows
-- looks exactly like a rule that works. 'null' emits empty derived columns and
-- leaves the row; 'flag' does the same but marks the row so the non-match rate is
-- measurable and surfaceable.
--
-- HIDING IS PRESENTATION, NEVER DELETION
-- `hide_source` affects what a report shows. The source column stays in the
-- extract and stays queryable -- hiding a column must never be a way to lose it.
--
-- ORG SCOPING / RGPD: org_id is carried and NOT NULL so migration 099's erasure
-- allowlist can reach these rows with the rest of the org tree; a rule is
-- tenant data, unlike app.target_fields which is platform-global.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, constraints added through a
-- guarded DO block that reads pg_constraint for the actual current state.

BEGIN;

CREATE TABLE IF NOT EXISTS app.datastream_derived_columns (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'dcol_'
    datastream_id   TEXT        NOT NULL
                    REFERENCES app.datastreams(id) ON DELETE CASCADE,
    project_id      TEXT        NOT NULL
                    REFERENCES app.projects(id) ON DELETE CASCADE,
    org_id          TEXT        NOT NULL
                    REFERENCES app.organizations(id),

    -- The collected column being parsed. Deliberately NOT an FK to
    -- datastream_mappings: a rule may read a column that was never mapped to a
    -- canonical target field, which is the common case for placement names.
    source_field    TEXT        NOT NULL,

    rule_kind       TEXT        NOT NULL DEFAULT 'regex'
                    CHECK (rule_kind IN ('regex')),
    -- The pattern. Length-bounded: a user-authored regular expression is
    -- attacker-adjacent input even from a trusted operator, and an unbounded one
    -- is the easy half of a catastrophic-backtracking incident. The other half
    -- (an execution timeout) belongs to the evaluator, not to the schema.
    pattern         TEXT        NOT NULL CHECK (length(pattern) BETWEEN 1 AND 500),

    -- Ordered outputs, one per capture group:
    --   [{"name": "targeting", "group": 1}, {"name": "size", "group": 2}]
    -- Kept as JSONB rather than a child table on purpose: outputs are meaningless
    -- apart from their rule, are always read together with it, and their order is
    -- the group order.
    outputs         JSONB       NOT NULL
                    CHECK (jsonb_typeof(outputs) = 'array' AND jsonb_array_length(outputs) >= 1),

    on_no_match     TEXT        NOT NULL DEFAULT 'null'
                    CHECK (on_no_match IN ('null', 'flag')),
    hide_source     BOOLEAN     NOT NULL DEFAULT FALSE,

    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE app.datastream_derived_columns IS
    'Read-time rules that split one collected column into one or more derived '
    'columns. Never materialised into the raw extract: the provider''s bytes stay '
    'as collected, and a rule change needs no refetch.';
COMMENT ON COLUMN app.datastream_derived_columns.on_no_match IS
    'What happens when the pattern does not match a row. null = empty derived '
    'columns; flag = the same, plus the row is marked so the non-match rate can be '
    'measured. Never a silent default: a rule that quietly nulls half the rows is '
    'indistinguishable from one that works.';
COMMENT ON COLUMN app.datastream_derived_columns.hide_source IS
    'Presentation only. The source column remains in the extract and remains '
    'queryable -- hiding a column must never be a way to lose it.';

-- One rule per (datastream, source column): several outputs come from one
-- pattern, so a second rule over the same column would mean two competing
-- parsings of the same string with no defined precedence.
CREATE UNIQUE INDEX IF NOT EXISTS uq_derived_columns_datastream_source
    ON app.datastream_derived_columns (datastream_id, source_field);

CREATE INDEX IF NOT EXISTS ix_derived_columns_project
    ON app.datastream_derived_columns (project_id);

-- Keep updated_at honest by reusing whichever generic touch function this
-- database actually has. Two exist in the corpus (app.touch_updated_at and
-- app.set_updated_at) and the schema is NOT linear, so the name is looked up in
-- pg_proc rather than assumed. No function, no trigger -- and updated_at simply
-- keeps its default; a missing helper must not fail the migration.
DO $$
DECLARE
    fn TEXT;
BEGIN
    SELECT p.proname INTO fn
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname = 'app'
       AND p.proname IN ('touch_updated_at', 'set_updated_at')
     ORDER BY p.proname = 'touch_updated_at' DESC
     LIMIT 1;

    IF fn IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_derived_columns_updated_at'
    ) THEN
        EXECUTE format(
            'CREATE TRIGGER trg_derived_columns_updated_at '
            'BEFORE UPDATE ON app.datastream_derived_columns '
            'FOR EACH ROW EXECUTE FUNCTION app.%I()', fn
        );
    END IF;
END
$$;

COMMIT;
