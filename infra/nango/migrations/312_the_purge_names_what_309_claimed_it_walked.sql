-- ============================================================================
-- 312 — the purge plan names what 309 claimed it already walked
-- ============================================================================
--
-- Migration 309's header says erasure reaches app.analytics_alignment_decisions
-- "through the ON DELETE CASCADE below, which core/org_purge.py already walks".
-- Measured 2026-08-25 by tests/conformance/test_migration_erasure_claims.py:
-- `plan_purge` filters foreign keys to confdeltype IN ('a','r'), so a CASCADE
-- edge is exactly what its graph excludes — the same fault migration 119 froze.
-- 309 is applied and checksum-frozen, so the sentence cannot be corrected in
-- place, and the FROZEN_CLAIMS inventory is deliberately closed to migrations
-- below 200. The guard leaves exactly two ways out; this takes the first:
-- give the table a foreign key the graph CAN see.
--
-- The org_id edge becomes ON DELETE NO ACTION. `plan_purge` then emits an
-- explicit DELETE for this table, in dependency order, before the organization
-- row — which makes 309's sentence true and the erasure auditable in the plan
-- instead of implicit in a cascade. Nothing blocks: by the time the
-- organizations row is deleted, the purge's own statement (or the project_id
-- CASCADE, which stays) has already removed every referencing row. The DELETE
-- privilege the erasure needs is untouched — migration 310 revoked UPDATE only.
--
-- Schema-Change-Checklist: idempotent (the DO block re-runs to the same state),
-- no data touched, no privilege changed. No migration below this number is
-- edited. This is migration 312.
-- ============================================================================

BEGIN;

DO $$
DECLARE
    _constraint text;
BEGIN
    SELECT conname INTO _constraint
    FROM pg_constraint
    WHERE conrelid = 'app.analytics_alignment_decisions'::regclass
      AND contype = 'f'
      AND confrelid = 'app.organizations'::regclass
      AND confdeltype = 'c';

    IF _constraint IS NOT NULL THEN
        EXECUTE format(
            'ALTER TABLE app.analytics_alignment_decisions DROP CONSTRAINT %I',
            _constraint
        );
        ALTER TABLE app.analytics_alignment_decisions
            ADD CONSTRAINT analytics_alignment_decisions_org_id_fkey
            FOREIGN KEY (org_id) REFERENCES app.organizations(id)
            ON DELETE NO ACTION;
    END IF;
END $$;

COMMIT;
