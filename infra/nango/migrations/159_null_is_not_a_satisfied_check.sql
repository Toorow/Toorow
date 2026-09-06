-- Corrective sweep: five CHECK constraints that a NULL expression defeats.
--
-- WHY THIS FILE EXISTS AT ALL. Migration 155 repaired exactly this defect on the
-- four constraints Story 50.3 had just written, and stopped there. CLAUDE.md §4
-- says a defect found on one screen holds for every screen and on one route for
-- its whole family; a constraint family is no different. So the whole schema was
-- swept before a line of this file was written, and the same hole was open in
-- four more places -- one of them in migration 156, which had landed hours
-- earlier and therefore reintroduced the very defect 155 had just closed.
--
-- THE DEFECT, restated once. A CHECK is satisfied when its expression is TRUE
-- *or NULL*, and only rejected on FALSE. Any disjunction of branches that each
-- compare a NULLABLE column therefore has a hole: make every branch NULL and the
-- row is accepted while satisfying none of them. That is precisely the
-- "fill it in later" state these constraints were written to make impossible.
--
-- HOW THE FIVE WERE FOUND, and why it is five and not seventy-two. All 1209 CHECK
-- constraints in schema `app` were enumerated from `pg_constraint` and each one
-- evaluated against real candidate rows -- NULL for every nullable column, the
-- literals the expression itself names for the rest, and NEVER a NULL in a
-- NOT NULL column, because nulling a NOT NULL column manufactures a false
-- positive. Twelve came back NULL-satisfiable on a legal row.
--
-- Seven of those twelve are NOT defects and are deliberately left alone. They are
-- the ordinary nullable-enumeration idiom -- a lone `col = ANY (ARRAY[...])` on a
-- nullable column, where NULL means "not stated" and is meant to be allowed.
-- `app.target_fields.target_fields_measure_check` even writes `NULL::text` into
-- its own array to say so out loud. Wrapping those in COALESCE would silently
-- promote them to NOT NULL and reject existing legitimate rows: that would be a
-- destructive change dressed as a repair, which is the opposite of the point.
--
-- The remaining five are multi-branch contracts, and all five are corrected here.
-- Each was proved before this file was written by an INSERT into the real table,
-- with the real foreign-key chain, that PostgreSQL 17.2 accepted:
--
--     app.query_results               ai_path_id NULL and literal NULL   ACCEPTED
--     app.notebooks                   scheduled=true, rule NULL          ACCEPTED
--     app.semantic_concept_versions   metric, no aggregation, no class   ACCEPTED
--     app.source_delegations          challenge set, method NULL         ACCEPTED
--     app.visualization_spec_versions spec '{}' pinning nothing          ACCEPTED
--
-- THE FIX is 155's, unchanged: wrap the expression in COALESCE(..., FALSE) so it
-- is never NULL. Nothing legitimate changes -- a row that satisfied the old
-- constraint by being TRUE still satisfies this one. Verified before writing:
-- zero existing rows in this database violate any of the five tightened forms.
--
-- A REGRESSION TEST accompanies this migration
-- (`server/tests/core/test_check_constraints_null_proof_pg.py`) and walks
-- `pg_constraint` the same way, so the next migration cannot quietly reopen the
-- class the way 156 did.

BEGIN;

-- 1. Story 50.1, migration 151:239-241. A Result names its AI Path, or states in
--    exact words that there is none. Both NULL was the third, unnamed state.
ALTER TABLE app.query_results
    DROP CONSTRAINT IF EXISTS ck_query_results_ai_path_is_honest;
ALTER TABLE app.query_results
    ADD CONSTRAINT ck_query_results_ai_path_is_honest CHECK (
        COALESCE(
            (ai_path_id IS NOT NULL AND ai_path_absent_literal IS NULL)
            OR (ai_path_id IS NULL AND ai_path_absent_literal = 'No AI path'),
            FALSE
        )
    );

-- 2. Story 50.4, migration 156. The document must PIN the contract it claims to
--    honour. With `spec` = '{}' every `->>` yields NULL, so every conjunct was
--    NULL and a spec version pinning nothing was accepted -- the exact promise
--    the constraint exists to keep, defeated by an empty object.
ALTER TABLE app.visualization_spec_versions
    DROP CONSTRAINT IF EXISTS ck_visualization_spec_versions_document_pins;
ALTER TABLE app.visualization_spec_versions
    ADD CONSTRAINT ck_visualization_spec_versions_document_pins CHECK (
        COALESCE(
            (spec ->> 'spec_contract_version') = spec_contract_version
            AND (spec -> 'schema_version') = to_jsonb(schema_version)
            AND (spec ->> 'family') = family
            AND (spec #>> '{accessibility,table_fallback}') = 'required',
            FALSE
        )
    );

-- 3. A Notebook is scheduled and names its rule, or is not scheduled and names
--    none. `scheduled = true` with a NULL rule is a Notebook that claims a
--    schedule nothing can ever run.
ALTER TABLE app.notebooks
    DROP CONSTRAINT IF EXISTS schedule_rule_check;
ALTER TABLE app.notebooks
    ADD CONSTRAINT schedule_rule_check CHECK (
        COALESCE(
            (scheduled = false AND schedule_rule IS NULL)
            OR (scheduled = true AND schedule_rule = 'nightly'),
            FALSE
        )
    );

-- 4. A metric states how it aggregates, or declares itself non-additive. With
--    both NULL the row was a metric that cannot be summed and does not say why --
--    which is how a total gets computed from something that has no total.
ALTER TABLE app.semantic_concept_versions
    DROP CONSTRAINT IF EXISTS ck_semantic_concept_versions_metric;
ALTER TABLE app.semantic_concept_versions
    ADD CONSTRAINT ck_semantic_concept_versions_metric CHECK (
        COALESCE(
            kind <> 'metric'
            OR (
                expression IS NOT NULL
                AND (aggregation IS NOT NULL OR additivity_class = 'non_additive')
            ),
            FALSE
        )
    );

-- 5. If a PKCE challenge was issued, the method that verifies it must be named.
--    A challenge with a NULL method is a delegation whose proof key cannot be
--    checked at redemption.
ALTER TABLE app.source_delegations
    DROP CONSTRAINT IF EXISTS source_delegations_pkce_present;
ALTER TABLE app.source_delegations
    ADD CONSTRAINT source_delegations_pkce_present CHECK (
        COALESCE(pkce_challenge IS NULL OR pkce_method = 'S256', FALSE)
    );

COMMIT;
