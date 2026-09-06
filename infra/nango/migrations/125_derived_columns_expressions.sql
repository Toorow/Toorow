-- infra/nango/migrations/125_derived_columns_expressions.sql
--
-- Replaces the rule-kind model of 122/124 with a single expression per column.
--
-- WHY, AND WHY NOW
-- 122 modelled regex extraction because that was the example in front of me; 124
-- added a `constant` kind because a fixed value did not fit. Jean's next sentence
-- collapses both:
--
--     ="nomdelacolonne"                a column reference
--     =split("nomdelacolonne","_",3)   a function over one
--     =3                               a literal -- the "constant" kind was this
--     =if("true",0)else(1)             a conditional
--
-- There is no kind axis. There is an EXPRESSION, and constant / split /
-- conditional are values it can take. Enumerating kinds would mean a migration
-- per function forever, which is the same mistake 124 already corrected once:
-- a CHECK written around the single example in front of you is a guess about the
-- domain. The table has never held a row, so reshaping costs nothing today and a
-- data migration tomorrow.
--
-- ONE ROW = ONE COLUMN
-- 122 let one rule emit several columns from one pattern. An expression yields
-- ONE value, so a person wanting targeting, size and buy type writes three
-- columns with three expressions. It parses the same string three times, exactly
-- as a spreadsheet does, and in exchange each column is independently readable,
-- editable and removable -- worth far more than one saved pass over a string.
--
-- TWO REPRESENTATIONS, ON PURPOSE
--   expression  what the person typed, verbatim. The editable truth: it is shown
--               back unchanged, never a re-serialised approximation of itself.
--   ast         the parsed, validated form. What the evaluator reads.
-- Storing only the text would mean parsing on every row of every read; storing
-- only the AST would mean showing the operator a machine's paraphrase of their
-- own formula. Keeping both makes the pair a thing to verify -- the writer is the
-- single place allowed to produce `ast`, always from `expression`.
--
-- THE AST IS NOT CODE
-- No expression is ever eval()'d, in Python or anywhere else. `ast` is data: a
-- node tree over a closed, whitelisted function set, walked by an interpreter
-- that knows nothing else. A formula field that reaches a language runtime is
-- remote code execution wearing a spreadsheet costume, and the fact that only a
-- trusted operator can type it is not a control -- it is an assumption about
-- everyone who will ever hold that account.
--
-- Idempotent: columns are added IF NOT EXISTS, dropped IF EXISTS, constraints
-- dropped IF EXISTS before being re-added.

BEGIN;

DO $$
BEGIN
    IF to_regclass('app.datastream_derived_columns') IS NULL THEN
        RAISE EXCEPTION
            'app.datastream_derived_columns is missing -- apply migration 122 first';
    END IF;

    -- Refuse to reshape a table that already holds rules: silently dropping
    -- someone's parsing rules is exactly the class of quiet loss this file argues
    -- against. Empty in every environment at the time of writing.
    IF EXISTS (SELECT 1 FROM app.datastream_derived_columns) THEN
        RAISE EXCEPTION
            '125: app.datastream_derived_columns is not empty -- a rewrite of existing '
            'rules into expressions must be written deliberately, not assumed here';
    END IF;

    -- The new shape.
    ALTER TABLE app.datastream_derived_columns
        ADD COLUMN IF NOT EXISTS name        TEXT,
        ADD COLUMN IF NOT EXISTS expression  TEXT,
        ADD COLUMN IF NOT EXISTS ast         JSONB,
        ADD COLUMN IF NOT EXISTS result_type TEXT;

    -- The kind-era columns go: `outputs` (many per rule), `rule_kind`, `pattern`
    -- and `source_field` are all subsumed by the expression. `hide_source` becomes
    -- `hidden_sources`, a list: one expression can read several columns
    -- (=concat("a","b")) and hiding is per source column, not per rule.
    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_kind_shape,
        DROP CONSTRAINT IF EXISTS ck_derived_columns_outputs_named,
        DROP CONSTRAINT IF EXISTS datastream_derived_columns_rule_kind_check,
        DROP CONSTRAINT IF EXISTS datastream_derived_columns_pattern_check,
        DROP CONSTRAINT IF EXISTS datastream_derived_columns_outputs_check;

    ALTER TABLE app.datastream_derived_columns
        ADD COLUMN IF NOT EXISTS hidden_sources JSONB NOT NULL DEFAULT '[]'::jsonb;

    ALTER TABLE app.datastream_derived_columns
        DROP COLUMN IF EXISTS outputs,
        DROP COLUMN IF EXISTS rule_kind,
        DROP COLUMN IF EXISTS pattern,
        DROP COLUMN IF EXISTS source_field,
        DROP COLUMN IF EXISTS hide_source;

    ALTER TABLE app.datastream_derived_columns
        ALTER COLUMN name       SET NOT NULL,
        ALTER COLUMN expression SET NOT NULL,
        ALTER COLUMN ast        SET NOT NULL;
END
$$;

-- A column name a person can actually write in a report or a query: letters,
-- digits and underscores, not starting with a digit. Refused here rather than
-- escaped later -- a derived column called `2024 spend (€)` becomes a quoting
-- problem in every consumer downstream.
ALTER TABLE app.datastream_derived_columns
    DROP CONSTRAINT IF EXISTS ck_derived_columns_name;
ALTER TABLE app.datastream_derived_columns
    ADD CONSTRAINT ck_derived_columns_name
    CHECK (name ~ '^[A-Za-z_][A-Za-z0-9_]{0,62}$');

-- The leading '=' is the operator's own convention and is kept: it is what they
-- typed, and it is what tells a reader this cell is computed rather than stored.
ALTER TABLE app.datastream_derived_columns
    DROP CONSTRAINT IF EXISTS ck_derived_columns_expression;
ALTER TABLE app.datastream_derived_columns
    ADD CONSTRAINT ck_derived_columns_expression
    CHECK (expression LIKE '=%' AND length(expression) BETWEEN 2 AND 2000);

-- The AST is a node object, and every node carries its kind. Enough to keep
-- garbage out of the column; the closed function set and arity are the parser's
-- contract, where a rejection can name the function it did not recognise.
ALTER TABLE app.datastream_derived_columns
    DROP CONSTRAINT IF EXISTS ck_derived_columns_ast;
ALTER TABLE app.datastream_derived_columns
    ADD CONSTRAINT ck_derived_columns_ast
    CHECK (jsonb_typeof(ast) = 'object' AND ast ? 'kind');

ALTER TABLE app.datastream_derived_columns
    DROP CONSTRAINT IF EXISTS ck_derived_columns_result_type;
ALTER TABLE app.datastream_derived_columns
    ADD CONSTRAINT ck_derived_columns_result_type
    CHECK (result_type IS NULL
           OR result_type IN ('text', 'number', 'boolean', 'date'));

ALTER TABLE app.datastream_derived_columns
    DROP CONSTRAINT IF EXISTS ck_derived_columns_hidden_sources;
ALTER TABLE app.datastream_derived_columns
    ADD CONSTRAINT ck_derived_columns_hidden_sources
    CHECK (jsonb_typeof(hidden_sources) = 'array'
           AND NOT jsonb_path_exists(hidden_sources, '$[*] ? (@.type() != "string")'));

-- Two derived columns may not claim the same name on one Datastream: which one
-- wins would be undefined, and the loser would vanish without a word.
DROP INDEX IF EXISTS app.uq_derived_columns_datastream_source;
CREATE UNIQUE INDEX IF NOT EXISTS uq_derived_columns_datastream_name
    ON app.datastream_derived_columns (datastream_id, name);

COMMENT ON TABLE app.datastream_derived_columns IS
    'One derived column per row: a name and an expression, evaluated at read. '
    'Spreadsheet-shaped surface (=3, ="col", =split("col","_",3), =if(...)), '
    'stored parsed as a JSON node tree. Never materialised into the raw extract '
    'and never evaluated as code.';
COMMENT ON COLUMN app.datastream_derived_columns.expression IS
    'Verbatim, as typed. The editable truth -- shown back unchanged, never a '
    're-serialisation of the AST.';
COMMENT ON COLUMN app.datastream_derived_columns.ast IS
    'Parsed and validated node tree over a closed function set. DATA, walked by '
    'an interpreter -- never passed to eval() in any language.';
COMMENT ON COLUMN app.datastream_derived_columns.hidden_sources IS
    'Source columns to hide from reports once this column exists. Presentation '
    'only: the columns stay in the extract and stay queryable.';

COMMIT;
