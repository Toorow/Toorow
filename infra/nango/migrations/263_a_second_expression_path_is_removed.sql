-- 263 -- The second expression path goes, table included (AI-252).
--
-- WHAT IS REMOVED, AND WHY IT IS NOT A LOSS. `app.datastream_derived_columns`
-- backed `server/core/derived_columns.py` and the dbt macro
-- `derived_columns_projection`. Measured 2026-08-15, all four facts together:
--
--   the module              426 lines, ZERO callers outside its own tests
--   the macro               ZERO invocations from any dbt model
--   the table               0 rows, on the disposable base and on preprod
--   the dialect             BigQuery-only
--
-- Story 60.6 deprecated the path and 60.3 forbade its shape outright by
-- requiring every pattern to compile in BOTH dialects. What kept it alive was
-- two classes -- `ExpressionError` and `PreviewResult` -- that `cleanup_rules`
-- imported; they now live in `cleanup_rules` itself, which is their only user.
--
-- WHY REMOVING IT IS THE POINT AND NOT HOUSEKEEPING. `semantic_expressions`
-- (story 60.2) is the expression path. A dormant second one, with its own
-- storage and its own dialect, is a shape the next author reaches for -- and a
-- rule that compiles for one warehouse only is exactly what 60.3 exists to
-- refuse. Leaving it "in case" is how a product grows two vocabularies for one
-- question.
--
-- THE EARLIER MIGRATIONS ARE NOT RE-EDITED. 122 created the table, 125 and 127
-- reshaped it; all three are applied and immutable, so the table leaves by this
-- migration and not by a rewrite of theirs.
--
-- ERASURE. NOT `core.org_purge`: it walks NO ACTION and RESTRICT edges only, and
-- this table hangs off `app.datastreams` by ON DELETE CASCADE. Postgres already
-- erased its rows with the Datastream -- which is moot at zero rows, and stated
-- so the header is right rather than silent (the class `test_migration_erasure_claims`
-- guards).

BEGIN;

DROP TABLE IF EXISTS app.datastream_derived_columns;

COMMIT;
