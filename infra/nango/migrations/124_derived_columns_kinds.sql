-- infra/nango/migrations/124_derived_columns_kinds.sql
--
-- Widens app.datastream_derived_columns beyond regex extraction.
--
-- WHY, IMMEDIATELY AFTER 122
-- 122 modelled the case that had been described to me -- splitting a placement
-- name into targeting / size / buy type by regular expression -- and made
-- `source_field` NOT NULL and `rule_kind` a one-value CHECK. Jean's next
-- sentence ("tu dois pouvoir ajouter des colonnes comme tu veux, meme mettre une
-- valeur fixe partout si tu en as besoin") breaks both: a constant column has NO
-- source column and NO pattern. Widening now, while the table is empty and
-- nothing reads it, costs nothing; widening after the first rule is written costs
-- a data migration and a compatibility story.
--
-- The lesson is the one this repository keeps re-learning: a CHECK written around
-- the single example in front of you is a guess about the domain, and
-- `rule_kind IN ('regex')` was that guess.
--
-- WHAT A RULE IS NOW
--   regex     one source column + a pattern -> one or more columns from its
--             capture groups. outputs: [{"name": ..., "group": n}, ...]
--   constant  no source, no pattern -> one or more columns holding a fixed value
--             on every row. outputs: [{"name": ..., "value": "..."}, ...]
--
-- The two are told apart by rule_kind and held to their own shape by
-- ck_derived_columns_kind_shape, so a constant carrying a pattern -- or a regex
-- carrying no source -- cannot be stored at all. More kinds (concat, lookup,
-- expression) will each add their own arm to that CHECK rather than loosening it.
--
-- hide_source is refused on a constant: there is no source cell to hide, and a
-- flag that silently does nothing is worse than one that is rejected.
--
-- Idempotent: nullability changes are DROP NOT NULL (no-ops when already
-- nullable), constraints are dropped IF EXISTS then re-added, and the unique
-- index is recreated as a partial one.

BEGIN;

DO $$
BEGIN
    IF to_regclass('app.datastream_derived_columns') IS NULL THEN
        RAISE EXCEPTION
            'app.datastream_derived_columns is missing -- apply migration 122 first';
    END IF;

    -- A constant has neither a source column nor a pattern.
    ALTER TABLE app.datastream_derived_columns ALTER COLUMN source_field DROP NOT NULL;
    ALTER TABLE app.datastream_derived_columns ALTER COLUMN pattern DROP NOT NULL;

    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS datastream_derived_columns_rule_kind_check;
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT datastream_derived_columns_rule_kind_check
        CHECK (rule_kind IN ('regex', 'constant'));

    -- The pattern length bound moves with the column becoming nullable: an
    -- absent pattern is legal, an unbounded one never is (a user-authored regular
    -- expression is the easy half of a catastrophic-backtracking incident; the
    -- execution timeout is the evaluator's half).
    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS datastream_derived_columns_pattern_check;
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT datastream_derived_columns_pattern_check
        CHECK (pattern IS NULL OR length(pattern) BETWEEN 1 AND 500);

    -- Each kind is held to its own shape. Not one permissive CHECK: a rule that
    -- is half regex and half constant has no defined evaluation.
    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_kind_shape;
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT ck_derived_columns_kind_shape
        CHECK (
            (rule_kind = 'regex'
                AND source_field IS NOT NULL
                AND pattern IS NOT NULL)
         OR (rule_kind = 'constant'
                AND source_field IS NULL
                AND pattern IS NULL
                AND hide_source = FALSE)
        );

    -- Every output carries a non-empty name, whatever the kind. The rest of the
    -- shape (group vs value) is the evaluator's contract, checked where it can be
    -- explained to the operator rather than as an unreadable JSONB assertion.
    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_outputs_named;
    -- Expressed as SQL/JSON path predicates, not as a subquery: Postgres refuses
    -- a subquery inside a CHECK ("cannot use subquery in check constraint"), so
    -- the jsonb_array_elements form this was first written with could not exist.
    -- Each predicate is proven against a fixture before being trusted here.
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT ck_derived_columns_outputs_named
        CHECK (
            NOT jsonb_path_exists(outputs, '$[*] ? (@.type() != "object")')
        AND NOT jsonb_path_exists(outputs, '$[*] ? (!exists(@.name))')
        AND NOT jsonb_path_exists(outputs, '$[*] ? (@.name == "")')
        );
END
$$;

-- One rule per source column still holds -- two patterns over the same string
-- would be two competing parsings with no defined precedence -- but constants
-- have no source, so the index becomes partial instead of forbidding a second
-- constant.
DROP INDEX IF EXISTS app.uq_derived_columns_datastream_source;
CREATE UNIQUE INDEX IF NOT EXISTS uq_derived_columns_datastream_source
    ON app.datastream_derived_columns (datastream_id, source_field)
    WHERE source_field IS NOT NULL;

COMMENT ON COLUMN app.datastream_derived_columns.rule_kind IS
    'regex: one source column split by a pattern into its capture groups. '
    'constant: a fixed value on every row, with no source and no pattern. Each '
    'kind is held to its own shape by ck_derived_columns_kind_shape; a new kind '
    'adds an arm there rather than loosening it.';

COMMIT;
