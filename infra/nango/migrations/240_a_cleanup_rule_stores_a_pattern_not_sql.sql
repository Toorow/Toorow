-- Story 60.3: a cleanup rule stores a PATTERN, never SQL.
--
-- WHAT THIS IS NOT: A RESURRECTION OF `app.datastream_derived_columns`.
-- Migration 122 created that table with `rule_kind CHECK (rule_kind IN ('regex'))`
-- (122:66-67) and `pattern ... BETWEEN 1 AND 500` (122:72). Migration 125 DROPPED
-- both, together with `outputs`, `source_field` and `hide_source` (125:89-93),
-- after replacing them with a single SQL `expression` (125:69-72); 127 then
-- dropped `ast` (127:50). The living schema of that table therefore carries
-- `name, expression, result_type, hidden_sources, source_columns` and no pattern
-- of any kind. The regex object was REMOVED, not forgotten.
--
-- This migration does not revive it, and it does not touch 122, 125 or 127. The
-- two objects answer two different questions and keep two different stores:
--   * `app.datastream_derived_columns` PROJECTS a new column from a governed SQL
--     expression, compiled by `server/core/derived_columns.py` and projected by
--     `dbt/macros/derived_columns_projection.sql`. It reads columns; it never
--     removes a row. Story 60.6 owns the state of its wiring.
--   * `app.cleanup_rules` (here) REMOVES rows or strips a substring, from a
--     regular expression the client types. It stores that expression as a
--     PATTERN and nothing else -- no SQL, no AST, no compiled fragment.
--
-- WHY THE PATTERN AND NOT THE SQL. The pattern is dialect-free; the SQL is not.
-- The fixture chain of this repository is DuckDB and production is BigQuery, and
-- the repository already polices the mixture: `test_fee_tax_geo_bridge.py:1027`
-- refuses `regexp_full_match` (DuckDB) where BigQuery wants `REGEXP_CONTAINS`.
-- Storing a compiled fragment would freeze one of the two dialects into the row
-- and make the other unreachable. `server/core/cleanup_rules.py` emits both from
-- this one stored pattern, and `tests/conformance/test_cleanup_rule_dialects.py`
-- holds it to that.
--
-- APPLIED AT READ, NOT AT INGESTION -- the same sentence 122:23-30 wrote for its
-- own object, and for the same reason: "changing a regex costs nothing, no
-- refetch, no 16-month backfill". A row this rule removes was still collected,
-- still lives in the raw zone, and is still the append-only evidence of AD-7. A
-- wrong rule is corrected by editing it, never by re-collecting sixteen months.
--
-- THE LENGTH BOUND LIVES HERE, AND IT IS THE ONE 125 CARRIED AWAY. 122:69-72
-- bounded a pattern to 1..500 characters and lost the bound with the column.
-- `length(pattern) BETWEEN 1 AND 500` below is that bound, back in the schema
-- where a bound cannot be forgotten by a caller.
--
-- WHAT THIS SCHEMA DOES **NOT** GUARANTEE, stated so no reader infers it:
-- Postgres checks the LENGTH of the pattern and nothing else about it. It does
-- not parse the regular expression, does not know RE2, and does not know which
-- warehouse dialect will run it. Syntax, the RE2 subset and the string-literal
-- safety are refused by `core/cleanup_rules.py` BEFORE the INSERT, and the only
-- writer of this table is that module -- which is the whole reason the bound is
-- duplicated here rather than trusted to it.
--
-- ORG SCOPING / RGPD. `org_id` is NOT NULL and its foreign key is ON DELETE
-- CASCADE, so a tenant erasure takes these rows with the org row itself
-- (`admin_api.py:8516`, the DELETE that ends the erasure). As migration 235:54-61
-- measured for its own tables, it is NOT `core.org_purge` that reaches them:
-- `plan_purge` walks `confdeltype IN ('a','r')` only (`org_purge.py:97`), so a
-- CASCADE edge is deliberately absent from its plan. The foreign key is what
-- makes the erasure reach here -- an org-scoped table naming no parent would be
-- invisible to both mechanisms.
--
-- Idempotent: CREATE TABLE / INDEX IF NOT EXISTS, trigger creation guarded.

BEGIN;

CREATE TABLE IF NOT EXISTS app.cleanup_rules (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'crule_'
    org_id          TEXT        NOT NULL
                    REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id      TEXT        NOT NULL
                    REFERENCES app.projects(id) ON DELETE CASCADE,
    -- NULL means "every Datastream of this Project". It is a column and not an
    -- assignment table because a cleanup rule names ONE field and one condition:
    -- the fan-out it can have is "this Datastream" or "all of them", and both
    -- are COUNTABLE before a change (`cleanup_rules.assess_rule_impact`).
    datastream_id   TEXT        REFERENCES app.datastreams(id) ON DELETE CASCADE,
    name            TEXT        NOT NULL CHECK (length(btrim(name)) BETWEEN 1 AND 160),
    -- The collected column the rule reads. Deliberately NOT a foreign key to
    -- app.datastream_mappings, for the reason 122:62-64 states: a rule may read a
    -- column that was never mapped, and cleaning a placement name is exactly the
    -- case where nothing has mapped it yet.
    source_field    TEXT        NOT NULL CHECK (length(btrim(source_field)) BETWEEN 1 AND 320),
    -- The three kinds `core/cleanup_rules.RULE_KINDS` emits, and no others. Two
    -- remove rows at read; the third rewrites one field of the rows it keeps.
    rule_kind       TEXT        NOT NULL
                    CHECK (rule_kind IN ('exclude_row', 'keep_row', 'strip_match')),
    pattern         TEXT        NOT NULL CHECK (length(pattern) BETWEEN 1 AND 500),
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    -- What the warehouse answered when the compiled SELECT was submitted BEFORE
    -- this row was written. `passed` means an engine accepted it; `not_attempted`
    -- means no engine could be reached and the row says so rather than implying a
    -- validation nobody performed. A dry run that FAILS never reaches this table:
    -- the API refuses the write with the engine's own message.
    dry_run_state   TEXT        NOT NULL
                    CHECK (dry_run_state IN ('passed', 'not_attempted')),
    dry_run_detail  TEXT        CHECK (dry_run_detail IS NULL OR length(dry_run_detail) <= 2000),
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Two rules of the same reach cannot answer to the same name: every confirmation
-- sentence in the surface names the rule, and two identical names make that
-- sentence ambiguous. COALESCE is mandatory -- NULL <> NULL would let two
-- Project-wide rules through (the discipline of 235:92-97).
CREATE UNIQUE INDEX IF NOT EXISTS uq_cleanup_rules_name
    ON app.cleanup_rules (project_id, COALESCE(datastream_id, ''), lower(btrim(name)));

CREATE INDEX IF NOT EXISTS ix_cleanup_rules_project
    ON app.cleanup_rules (project_id);

CREATE INDEX IF NOT EXISTS ix_cleanup_rules_datastream
    ON app.cleanup_rules (datastream_id);

COMMENT ON TABLE app.cleanup_rules IS
    'Story 60.3: a CLEANUP RULE -- a named regular expression the client writes '
    'to drop rows or strip a substring AT READ. It is not app.datastream_derived '
    '_columns (122/125/127), which projects a SQL expression and removes no row. '
    'The word "filter" is deliberately absent: query_specs.py holds it for a read '
    'filter and datastream_intents.py for a collection filter.';

COMMENT ON COLUMN app.cleanup_rules.pattern IS
    'The regular expression AS THE CLIENT TYPED IT, never SQL. core/cleanup_rules '
    'emits REGEXP_CONTAINS for BigQuery and regexp_matches for DuckDB from this '
    'one string; storing a compiled fragment would freeze one dialect into the row.';

COMMENT ON COLUMN app.cleanup_rules.datastream_id IS
    'NULL means every Datastream of the Project. The count that number resolves '
    'to is READ before a change and shown in the confirmation; a read that fails '
    'is reported as unknown, never as zero.';

-- updated_at. The helper name is LOOKED UP rather than assumed: two exist in the
-- corpus (app.set_updated_at, app.touch_updated_at) and the schema is not linear,
-- so a missing helper must not fail the migration (235:166-169).
DO $$
DECLARE
    fn TEXT;
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

    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_cleanup_rules_updated_at') THEN
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE ON app.cleanup_rules '
            'FOR EACH ROW EXECUTE FUNCTION app.%I()',
            'trg_cleanup_rules_updated_at', fn
        );
    END IF;
END
$$;

COMMIT;
