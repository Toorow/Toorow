-- The third level of the media-plan match: plan line -> campaign -> PLACEMENTS.
-- Story 61.1, arbitrage A4 (b).
--
-- WHY A CHILD TABLE AND NOT A THIRD COLUMN ON app.plan_line_mappings. Measured
-- before the choice, and both halves of the measurement refuse it:
--
--   * `uq_plan_line_mappings_identity` is UNIQUE on
--     (plan_id, line_key, connector, campaign_ref) -- migration 041. Adding a
--     placement column would either widen that key, which stops one row per
--     (line, campaign) from being one row, or leave it, which forbids a second
--     placement on the same campaign. « Une ligne de plan porte PLUSIEURS
--     placements (Feed + Marketplace + Search results) : c'est le cas normal »
--     (`docs/product-architecture/datastream-workbench-and-wizard.md`,
--     ratified amendment 4), so forbidding the second is forbidding the normal.
--   * `SUM(split_weight) = 1.0` per (plan_id, connector, campaign_ref) is an
--     AGGREGATE invariant enforced by `mediaplan_mapping.set_line_mappings`
--     under a per-plan `SELECT ... FOR UPDATE`. Three rows where there was one
--     would make that sum 3.0 and ventilate a campaign's spend three times. A
--     living invariant is not broken to house a third level.
--
-- WHAT THIS TABLE DOES NOT CARRY, so nobody looks for it: NO split_weight, and
-- therefore no second ventilation. Attaching a placement says WHICH placements
-- of an already-matched campaign the line bought; the money is still ventilated
-- at the campaign grain by the row this one hangs from, and this table is read
-- by no mart and by no dbt model. Nothing here changes a number.
--
-- WHAT A PLACEMENT IS HERE, and it is not a new vocabulary (arbitrage A3):
-- (breakdown_dimension, breakdown_value) of `fact_daily_kpi` -- the SAME shape
-- migration 041 already uses for `campaign_ref`, which it defines as
-- "fact_daily_kpi.breakdown_value WHERE breakdown_dimension='campaign_id'". The
-- dimension is stored beside the value rather than assumed, because it differs
-- per connector: measured over the 39 module manifests on 2026-08-09, exactly
-- TWO declare a placement dimension in `canonical_dimension_mapping` -- `cm360`
-- (`placement_id`) and `x-ads` (`placement`). The other 37 declare none, so
-- nothing can be attached on them and the surface says so instead of inventing
-- an identity.
--
-- WHAT IS NOT ENFORCED HERE, stated so no reader trusts a guard that does not
-- exist: this migration installs NO trigger and NO check that
-- `breakdown_dimension` is a dimension the connector declares. That check is a
-- read of the module manifests, which are files and not rows, so it lives in
-- `server/core/plan_line_placements.py` (`attach_placement`) and nowhere else.
-- The database enforces the parent, the uniqueness and the status vocabulary,
-- and claims nothing more.
--
-- NO ERASURE HATCH IS NEEDED, and no append-only guard either. This table
-- carries no immutability rule -- rows are deleted on detach -- so it needs
-- none of the kind `099_rgpd_erasure_trigger_guards.sql` adds. The only identity
-- it stores is `created_by`, exactly as `app.plan_line_mappings` does beside it.
--
-- THE EXACT MECHANISM THAT ERASES THESE ROWS, AND IT IS NOT `core.org_purge`.
-- Saying otherwise would send the next reader to the wrong file: `plan_purge`
-- walks the FK graph through `confdeltype IN ('a','r')` ONLY (`org_purge.py`,
-- `_FK_GRAPH_SQL`) -- NO ACTION and RESTRICT. The foreign key below is
-- ON DELETE CASCADE, so it is deliberately absent from that plan, because
-- Postgres already does the work. Measured on the disposable cluster before this
-- paragraph was written: `plan_purge(conn, 'org_EXAMPLE')` emits 2276
-- statements, ZERO of which name `plan_line_placement_mappings`.
--
-- What DOES reach here is that same plan's `DELETE FROM app.plan_line_mappings`
-- -- one statement, reached because the parent's own FK to `app.media_plans` is
-- ON DELETE RESTRICT and therefore IS in the graph -- and the cascade below
-- carries it down. Verified end to end on the disposable cluster, inside a
-- rolled-back transaction: one child row inserted, its parent mapping deleted,
-- child count back to 0. The FK is still what makes the erasure reach here (same
-- motive as 235's own note): an org-scoped table naming no parent is invisible
-- to BOTH mechanisms.

BEGIN;

CREATE TABLE IF NOT EXISTS app.plan_line_placement_mappings (
    id                  UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id             UUID        NOT NULL,
    line_key            TEXT        NOT NULL,
    connector           TEXT        NOT NULL,
    campaign_ref        TEXT        NOT NULL,
    breakdown_dimension TEXT        NOT NULL,
    breakdown_value     TEXT        NOT NULL,
    status              TEXT        NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'orphaned')),
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ON DELETE CASCADE, and it is the opposite choice from the parent's own
-- `ON DELETE RESTRICT` on app.media_plans. A plan must not vanish under its
-- mappings, which is why 041 restricts; but a placement attached to a
-- (line, campaign) pair that is being unmatched has nothing left to qualify,
-- and leaving it would be a row naming a campaign this line no longer buys.
-- `set_line_mappings` replaces a line's whole mapping set on every write, so
-- RESTRICT here would make the existing engine fail the moment one placement
-- existed -- a write path nobody could use.
--
-- The referenced columns are covered by `uq_plan_line_mappings_identity`, a
-- unique index rather than a named UNIQUE constraint. PostgreSQL accepts a
-- unique index as a foreign-key target; verified against this exact pair on the
-- disposable cluster before this file was written.
ALTER TABLE app.plan_line_placement_mappings
    DROP CONSTRAINT IF EXISTS fk_plan_line_placement_mappings_parent;
ALTER TABLE app.plan_line_placement_mappings
    ADD CONSTRAINT fk_plan_line_placement_mappings_parent
    FOREIGN KEY (plan_id, line_key, connector, campaign_ref)
    REFERENCES app.plan_line_mappings (plan_id, line_key, connector, campaign_ref)
    ON DELETE CASCADE;

-- One row per placement per matched campaign per line. The pair
-- (breakdown_dimension, breakdown_value) is the identity, not the value alone:
-- `placement` on one connector and `placement_id` on another are two different
-- vocabularies and their values must never collide in one key.
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_line_placement_mappings_identity
    ON app.plan_line_placement_mappings
       (plan_id, line_key, connector, campaign_ref, breakdown_dimension, breakdown_value);

-- The read path of the Placements tab: every placement of one plan for one
-- connector, in one index scan.
CREATE INDEX IF NOT EXISTS idx_plan_line_placement_mappings_plan_connector
    ON app.plan_line_placement_mappings (plan_id, connector);

COMMENT ON TABLE app.plan_line_placement_mappings IS
    'Story 61.1: the third level of the media-plan match -- which observed placements of an '
    'already-matched campaign a plan line bought. A CHILD of app.plan_line_mappings, never a '
    'column on it: a third column would break uq_plan_line_mappings_identity and the aggregate '
    'invariant SUM(split_weight)=1.0 per (plan_id, connector, campaign_ref). Carries NO weight '
    'and is read by no mart: the ventilation stays at the campaign grain.';
COMMENT ON COLUMN app.plan_line_placement_mappings.breakdown_dimension IS
    'fact_daily_kpi.breakdown_dimension of the connector''s declared placement dimension '
    '(cm360: placement_id, x-ads: placement -- the only two of the 39 manifests that declare one, '
    'measured 2026-08-09). Stored beside the value because it differs per connector. That it is a '
    'DECLARED dimension is checked in core/plan_line_placements.attach_placement, not here.';
COMMENT ON COLUMN app.plan_line_placement_mappings.breakdown_value IS
    'fact_daily_kpi.breakdown_value observed on that dimension -- the placement identity, in the '
    'same shape app.plan_line_mappings.campaign_ref already uses for a campaign.';
COMMENT ON COLUMN app.plan_line_placement_mappings.status IS
    'active | orphaned. The same vocabulary as app.plan_line_mappings.status, so a reader of the '
    'two tables reads one word. No recompute writes ''orphaned'' yet: the parent row cascades on '
    'delete, and no story has asked for a placement that outlives its observation.';

-- The application role is granted explicitly rather than left to the default
-- privileges migration 207 set FOR ROLE postgres: a migration applied by any
-- other owner would create this table outside that default and leave every
-- write refused with InsufficientPrivilege at runtime. Guarded, because the
-- role does not exist on every target.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE app.plan_line_placement_mappings TO connector;
    END IF;
END;
$$;

COMMIT;
