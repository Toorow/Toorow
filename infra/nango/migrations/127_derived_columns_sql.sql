-- infra/nango/migrations/127_derived_columns_sql.sql
--
-- Realigns app.datastream_derived_columns onto SQL expressions.
--
-- 125 stored a node tree (`ast`) for a bespoke `=`-prefixed grammar. That grammar
-- is withdrawn: the data lives in BigQuery and is modelled by dbt, so a rule that
-- is SQL runs where the data already is, and SQL is the form an agent writes most
-- reliably -- which matters because authoring a column is exactly what the agent
-- is meant to do for its user (decision Jean, 2026-07-27).
--
-- ONE DIALECT: BIGQUERY (ruling, same day)
-- `core/warehouse.py` has two backends, DuckDB and BigQuery. Expressions target
-- BIGQUERY and nothing else. Local DuckDB is allowed to skip a rule whose
-- functions it does not implement, because the datasets that matter are remote
-- and nobody edits transformation rules against a local copy. Restricting the
-- vocabulary to the intersection of two dialects would buy a local convenience
-- nobody uses, at the cost of SAFE_DIVIDE, SAFE_OFFSET and the rest of the
-- surface that makes these rules worth writing.
--
-- WHAT IS STORED, AND WHAT IS NOT
--   expression      the SQL the person wrote, verbatim, with {column} references
--   source_columns  the columns it reads, resolved at save time
-- The compiled SQL is NOT stored. It is a regex substitution away from
-- `expression`, and a stored copy is a second truth that can drift from the first
-- -- the same reason the migration ledger checks a checksum instead of trusting a
-- filename.
--
-- `source_columns` IS stored, because it answers a question the expression cannot
-- answer cheaply at scale: which rules break when a collected column disappears.
--
-- Idempotent. Refuses to run on a non-empty table rather than silently discarding
-- rules written under the previous shape.

BEGIN;

DO $$
BEGIN
    IF to_regclass('app.datastream_derived_columns') IS NULL THEN
        RAISE EXCEPTION 'app.datastream_derived_columns is missing -- apply 122 first';
    END IF;
    IF EXISTS (SELECT 1 FROM app.datastream_derived_columns) THEN
        RAISE EXCEPTION
            '127: the table is not empty -- rewriting existing rules from the '
            'previous grammar into SQL must be done deliberately, not assumed here';
    END IF;

    ALTER TABLE app.datastream_derived_columns
        ADD COLUMN IF NOT EXISTS source_columns JSONB NOT NULL DEFAULT '[]'::jsonb;

    ALTER TABLE app.datastream_derived_columns DROP COLUMN IF EXISTS ast;

    -- The '=' prefix belonged to the withdrawn grammar. A SQL expression starts
    -- with whatever SQL starts with.
    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_expression;
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT ck_derived_columns_expression
        CHECK (btrim(expression) <> '' AND length(expression) BETWEEN 1 AND 2000);

    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_ast;

    -- Refused in the schema as well as in code. These are the shapes that would
    -- escape a projected column and reach the generated model itself; the server
    -- rejects them with an explanation, and the column refuses them even if a
    -- future writer forgets to ask.
    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_expression_shape;
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT ck_derived_columns_expression_shape
        CHECK (
            position(';' IN expression) = 0
        AND position('--' IN expression) = 0
        AND position('/*' IN expression) = 0
        AND expression !~* '\ySELECT\y'
        AND expression !~* '\y(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\y'
        );

    ALTER TABLE app.datastream_derived_columns
        DROP CONSTRAINT IF EXISTS ck_derived_columns_source_columns;
    ALTER TABLE app.datastream_derived_columns
        ADD CONSTRAINT ck_derived_columns_source_columns
        CHECK (jsonb_typeof(source_columns) = 'array'
               AND NOT jsonb_path_exists(source_columns, '$[*] ? (@.type() != "string")'));
END
$$;

COMMENT ON TABLE app.datastream_derived_columns IS
    'One derived column per row: a name and a BigQuery SQL expression over the '
    'collected row, with columns written {like_this}. Evaluated by dbt where the '
    'data lives -- never materialised into the raw extract, never evaluated in the '
    'server. Reference tables are reached with MAP(table, {key}, attribute).';
COMMENT ON COLUMN app.datastream_derived_columns.expression IS
    'BigQuery SQL, verbatim as written. The editable truth; the compiled form is '
    'derived on demand and deliberately not stored beside it.';
COMMENT ON COLUMN app.datastream_derived_columns.source_columns IS
    'Collected columns the expression reads, resolved at save time. Stored so the '
    'question "which rules break if this column disappears" is answerable without '
    'reparsing every expression.';

COMMIT;
