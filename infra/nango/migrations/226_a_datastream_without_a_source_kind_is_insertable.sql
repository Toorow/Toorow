-- 226_a_datastream_without_a_source_kind_is_insertable.sql
--
-- EVERY INSERT INTO app.datastreams THAT OMITS `source_kind` FAILS TODAY, AND
-- THE PATH THAT OMITS IT IS THE ONE THE ORIGINAL MIGRATION DESCRIBES AS NORMAL.
--
-- Found 2026-08-07 by the dev of story 59.3, whose fixture could not insert a
-- Datastream, and confirmed by execution against the disposable base:
--
--     INSERT INTO app.datastreams (id, project_id, org_id, name, created_by) ...
--     -> NotNullViolation: null value in column "external_dispatch_excluded"
--
-- THE MECHANISM, and it is three lines of migration 076 disagreeing with each
-- other:
--
--   * `:161` adds `external_dispatch_excluded BOOLEAN NOT NULL DEFAULT FALSE`;
--   * `:175` sets it in a BEFORE INSERT trigger:
--         NEW.external_dispatch_excluded := (NEW.source_kind = 'external_bq');
--   * `app.datastreams.source_kind` is NULLABLE (`information_schema`, measured
--     today).
--
-- In SQL, `NULL = 'external_bq'` is NULL, not FALSE. So the trigger overwrites
-- the column's own DEFAULT with NULL on exactly the rows the DEFAULT was there
-- to serve, and the NOT NULL rejects the row. The DEFAULT was never reached: a
-- BEFORE trigger runs after defaults are applied and its assignment wins.
--
-- The original migration knew this shape and wrote it down at `:169-171`: "A
-- trigger, not a GENERATED column, because source_kind is set AFTER insert by
-- the intent path". A column set after insert is NULL at insert time. The
-- comment describes the failing case as the normal one.
--
-- WHY `COALESCE` AND NOT A NULLABLE COLUMN. `external_dispatch_excluded` is a
-- SCHEDULER GUARD -- `:158` shows the dispatch query filtering on
-- `AND ds.external_dispatch_excluded = FALSE`. A NULL there would make that
-- comparison NULL, so the row would silently leave the dispatch set: a stream
-- would stop collecting and nothing would say why. FALSE is the honest default
-- for a stream whose kind is not yet known -- it is not an external BigQuery
-- source until something says it is, and `UPDATE OF source_kind` fires this same
-- trigger the moment the intent path sets one.
--
-- WHY 076 IS NOT EDITED. It is applied, its checksum is in the ledger, and
-- CLAUDE.md forbids re-editing an applied migration. `CREATE OR REPLACE
-- FUNCTION` on the same name is the correction: the trigger keeps its name and
-- its binding, only the body changes.

CREATE OR REPLACE FUNCTION app.sync_external_dispatch_excluded()
RETURNS TRIGGER AS $$
BEGIN
    -- COALESCE, because `NULL = 'external_bq'` is NULL and the column is NOT
    -- NULL. A Datastream whose kind is not yet known is not an external
    -- BigQuery source, so it stays in the dispatch set until something says
    -- otherwise -- this same trigger fires again on `UPDATE OF source_kind`.
    NEW.external_dispatch_excluded := COALESCE(NEW.source_kind = 'external_bq', FALSE);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- The trigger itself is unchanged and is NOT recreated: `CREATE OR REPLACE
-- FUNCTION` above rebinds the body under the same name, so
-- `trg_datastreams_sync_external_dispatch` keeps its identity and its
-- `BEFORE INSERT OR UPDATE OF source_kind` binding from migration 076.

-- No backfill: the defect refused writes, it never wrote a wrong value. Every
-- row that exists went through the trigger with a non-NULL `source_kind` and
-- therefore already carries the right flag. Measured before applying:
--     SELECT count(*) FROM app.datastreams WHERE external_dispatch_excluded IS NULL;  -- 0
