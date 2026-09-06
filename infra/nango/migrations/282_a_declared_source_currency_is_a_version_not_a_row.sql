-- 282: a declared source currency becomes a governed version, and the store
-- migration 145 dethroned stops receiving writes.
--
-- WHAT WAS TRUE UNTIL THIS MIGRATION.
-- `app.fx_conflict_resolutions` (migration 053, Story 13.2) is one of the seven
-- stores migration 145 listed under "They stop being AUTHORITIES", with the DROP
-- explicitly left to "the commit that removes the last reader" (145:24,34-40).
-- Two years of readers and one writer stayed:
--
--   * the console WROTE it through `ON CONFLICT (project_id, target_field,
--     source_module) DO UPDATE SET ... decided_by = EXCLUDED.decided_by,
--     decided_at = NOW()`. Re-declaring a currency overwrote WHO decided the
--     previous one and WHEN, in place, with no version anywhere. 053 called that
--     "append-only par convention"; a convention with an UPDATE in it is not a
--     ledger. Story 60.5 removed exactly this defect from the two rule families;
--     this migration removes it from the third.
--   * nine dbt staging models JOINed `mirror.fx_conflict_resolutions`, so the
--     dethroned store was the runtime authority for the source currency of every
--     converted cost in the product.
--
-- WHAT IS AUTHORITATIVE NOW.
-- The store 144 built and 145 adopted: `app.governance_rule_sets` /
-- `app.governance_rule_set_versions`, family-typed, published versions frozen by
-- trigger. `core.source_currency_bindings` registers the `source_currency` family
-- on it -- a fifth family beside money_policy, fx_ingestion, timezone_policy and
-- tax_fee. Each declaration is one entry of the version's `ordered_rules`, and
-- carries its own `declared_by` / `declared_at`, carried forward untouched when a
-- neighbouring declaration changes.
--
-- WHAT dbt READS. Section 2 creates `app.fx_source_currency_bindings_v`, which
-- projects the PUBLISHED version back into the exact flat shape the staging
-- models already join on. This is not a new pattern: `app.project_money_policy_v`
-- (migration 148) projects the Money Policy family the same way, and mirror_sync
-- already carries three such family views. A family view naming its own family in
-- SQL is the family declaring itself; the prohibition 144 states is on the
-- GENERIC lifecycle branching on `family`, and nothing here changes that.
--
-- WHAT IS MEASURED, NOT ASSUMED (section 1).
-- 145 measured `app.fx_conflict_resolutions` at 0 rows on 2026-07-30. This
-- migration re-measures at APPLY time rather than trusting that number, because a
-- row written since then would carry a decision this migration would otherwise
-- silence. If any row exists, the migration ABORTS and names the exact rows: a
-- declaration has an org, a version and a content hash, and inventing those in
-- SQL would be guessing at the one thing the whole change exists to stop. The
-- refusal is the deliverable; zero rows is the measurement.
--
-- WHAT IS NOT DONE HERE. The DROP of `app.fx_conflict_resolutions`. Section 3
-- makes it REFUSE every write, which is what "stops receiving writes" means and
-- what a checksum-frozen migration can guarantee; the table itself stays until a
-- commit that has measured production can remove it. Deleting it now would delete
-- the only trace of where the declarations came from.
--
-- Idempotent: IF NOT EXISTS / OR REPLACE throughout.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. Measure the dethroned store. Refuse rather than guess.
-- ---------------------------------------------------------------------------

DO $$
DECLARE
    leftover  BIGINT;
    sample    TEXT;
BEGIN
    IF to_regclass('app.fx_conflict_resolutions') IS NULL THEN
        RETURN;
    END IF;

    EXECUTE 'SELECT count(*) FROM app.fx_conflict_resolutions' INTO leftover;
    IF leftover > 0 THEN
        EXECUTE $q$
            SELECT string_agg(
                       format('%s/%s/%s=%s (decided_by %s)',
                              project_id, target_field, source_module,
                              resolved_source_currency, decided_by),
                       '; ' ORDER BY project_id, target_field, source_module)
            FROM (SELECT * FROM app.fx_conflict_resolutions LIMIT 20) s
        $q$ INTO sample;
        RAISE EXCEPTION
            'app.fx_conflict_resolutions still holds % declaration(s); each one '
            'must be re-declared through core.source_currency_bindings.declare_binding '
            '(which mints a governed version) before this migration can seal the '
            'table. Rows: %',
            leftover, sample
            USING ERRCODE = '23514';
    END IF;
END $$;

-- ---------------------------------------------------------------------------
-- 2. The projection dbt reads. Same columns, same names, same meaning as the
--    relation the staging models joined -- so the change to each model is the
--    relation NAME and nothing else.
--
--    `resolved_source_currency` keeps its old name on purpose: it is the column
--    nine `COALESCE(res.resolved_source_currency, raw.cost_source_currency)`
--    expressions read, and renaming it would turn a store convergence into nine
--    silent arithmetic edits.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW app.fx_source_currency_bindings_v AS
SELECT s.project_id,
       binding->>'target_field'                     AS target_field,
       binding->>'source_module'                    AS source_module,
       binding->>'source_currency'                  AS resolved_source_currency,
       binding->>'declared_by'                      AS decided_by,
       (binding->>'declared_at')::TIMESTAMPTZ       AS decided_at,
       binding->>'note'                             AS note,
       s.id                                         AS rule_set_id,
       v.id                                         AS rule_set_version_id,
       v.content_hash                               AS rule_set_content_hash
FROM app.governance_rule_sets s
JOIN app.governance_rule_set_versions v
  ON v.id = s.current_version_id
 AND v.rule_set_id = s.id
 AND v.project_id = s.project_id
CROSS JOIN LATERAL jsonb_array_elements(v.ordered_rules) AS binding
WHERE s.family = 'source_currency'
  AND s.lifecycle_status <> 'archived'
  AND v.status = 'published';

COMMENT ON VIEW app.fx_source_currency_bindings_v IS
    'Story 67.20: the PUBLISHED source-currency declarations of each Project, '
    'projected from the governance_rule_sets family `source_currency` into the '
    'flat shape dbt staging joins on. Replaces app.fx_conflict_resolutions, which '
    'migration 145 dethroned and section 3 of migration 282 sealed.';

-- ---------------------------------------------------------------------------
-- 3. The dethroned store stops receiving writes.
--
--    A trigger rather than a REVOKE: the write path and the readers run as the
--    same role, so a grant cannot separate "may no longer write this" from "may
--    still read it while the DROP waits". The message names the door that works,
--    because an error that names only the cause leaves the caller nowhere to go.
--
--    TWO triggers, not one, and the reason is the erasure hatch. Migrations 277
--    and 278 establish that every DELETE guard inside the org tree must yield to
--    a transaction that flagged itself `app.rgpd_erasure = 'on'`, or a tenant
--    erasure is refused by a guard nobody remembered. INSERT and UPDATE are
--    refused unconditionally -- there is no legitimate writer left. DELETE is
--    refused too, EXCEPT under a flagged erasure: purging a tenant's rows from a
--    sealed table is exactly the deletion that must still pass.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION app.reject_fx_conflict_resolution_write()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'app.fx_conflict_resolutions was dethroned by migration 145 and no longer '
        'accepts writes. Declare the source currency through the governed '
        '`source_currency` Rule Set family instead '
        '(core.source_currency_bindings.declare_binding), which mints an immutable '
        'version and keeps who decided it. An RGPD erasure that flags itself with '
        'app.rgpd_erasure may still delete from it.'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_fx_conflict_resolutions_sealed
    ON app.fx_conflict_resolutions;
CREATE TRIGGER trg_fx_conflict_resolutions_sealed
    BEFORE INSERT OR UPDATE ON app.fx_conflict_resolutions
    FOR EACH ROW EXECUTE FUNCTION app.reject_fx_conflict_resolution_write();

DROP TRIGGER IF EXISTS trg_fx_conflict_resolutions_sealed_delete
    ON app.fx_conflict_resolutions;
CREATE TRIGGER trg_fx_conflict_resolutions_sealed_delete
    BEFORE DELETE ON app.fx_conflict_resolutions
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_fx_conflict_resolution_write();

COMMIT;
