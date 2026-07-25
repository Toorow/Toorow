-- Story 12.11: Bounded synchronize / reload / reprocess (the three named verbs).
--
-- Additive, idempotent and replayable. Zero provider/source vocabulary (AD-2).
--
-- Story 12.11 promotes THREE bounded-scope recovery verbs to first-class,
-- audited operations: `synchronize` (on-demand incremental sync), `reload`
-- (a half-open [from, to) range re-pull that supersedes ONLY that scope) and
-- `reprocess` (reapply a chosen mapping + Ossie version to RETAINED raw/source
-- data WITHOUT calling the provider). They REUSE the Story 36.13 AD-27
-- prepare-confirm-commit substrate wholesale: the immutable proposal store is
-- `app.operation_preparations` (migration 070) and the single durable operation
-- is `app.operations` (migration 060). This migration adds NO new table and NO
-- new engine -- it ONLY widens the migration-070 `kind` CHECK constraint so the
-- three named verbs are representable in the SAME immutable-proposal store the
-- Operations profile already writes.
--
-- Before 080 the CHECK admitted only {retry, refetch, reconcile}. After 080 it
-- ALSO admits {synchronize, reload, reprocess}. This is purely additive: every
-- previously-valid row stays valid; the wider set is a superset. Publication /
-- live-pointer authority is STILL NOT expressible here (Governance owns that,
-- Story 36.18) -- the three new verbs create candidate executions only and never
-- move `current_published_execution_id`.
--
-- The immutability trigger (`trg_operation_preparation_immutable`, migration 070)
-- is unchanged and continues to freeze every substantive proposal field; only the
-- admissible `kind` domain grows.
--
-- Apply order: after 079 (highest applied). Re-application is a no-op (the DO
-- block only swaps the constraint when its current definition differs).

BEGIN;

-- Widen app.operation_preparations.kind to admit the three Story 12.11 named
-- verbs alongside the three Story 36.13 verbs. Idempotent: drop-then-add the
-- named CHECK constraint only when it is not already the widened form.
DO $$
DECLARE
    current_def TEXT;
    wanted_tokens CONSTANT TEXT[] := ARRAY[
        'retry', 'refetch', 'reconcile', 'synchronize', 'reload', 'reprocess'
    ];
    tok TEXT;
    all_present BOOLEAN := TRUE;
BEGIN
    SELECT pg_get_constraintdef(c.oid)
      INTO current_def
      FROM pg_constraint c
      JOIN pg_class t ON t.oid = c.conrelid
      JOIN pg_namespace n ON n.oid = t.relnamespace
     WHERE n.nspname = 'app'
       AND t.relname = 'operation_preparations'
       AND c.conname = 'operation_preparations_kind_check';

    IF current_def IS NOT NULL THEN
        FOREACH tok IN ARRAY wanted_tokens LOOP
            IF position('''' || tok || '''' IN current_def) = 0 THEN
                all_present := FALSE;
            END IF;
        END LOOP;
    ELSE
        all_present := FALSE;
    END IF;

    IF NOT all_present THEN
        -- Drop the inline/named CHECK (whatever its current name) then add the
        -- widened, explicitly-named constraint. The default inline CHECK created
        -- by migration 070 is named `operation_preparations_kind_check` by
        -- Postgres convention; guard the DROP so a re-run never errors.
        IF EXISTS (
            SELECT 1 FROM pg_constraint c
              JOIN pg_class t ON t.oid = c.conrelid
              JOIN pg_namespace n ON n.oid = t.relnamespace
             WHERE n.nspname = 'app'
               AND t.relname = 'operation_preparations'
               AND c.conname = 'operation_preparations_kind_check'
        ) THEN
            ALTER TABLE app.operation_preparations
                DROP CONSTRAINT operation_preparations_kind_check;
        END IF;

        ALTER TABLE app.operation_preparations
            ADD CONSTRAINT operation_preparations_kind_check
            CHECK (kind IN (
                'retry', 'refetch', 'reconcile',
                'synchronize', 'reload', 'reprocess'
            ));
    END IF;
END $$;

COMMIT;
