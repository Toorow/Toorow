-- Spend a media plan never covered, ACCEPTED as such by a named person.
-- Story 61.2, arbitrage A3 (a).
--
-- WHY A TABLE AT ALL, and it is the whole weight of story 61.2. « Permet : sur
-- une dépense hors plan, la rattacher OU l'accepter comme telle » (epic-61) had
-- nowhere to land. Measured before this file was written:
--
--   * `mediaplan_mapping.list_unmapped_actuals` DERIVES the out-of-plan spend
--     from the warehouse at every read -- there is no row, and therefore no
--     primary key a decision could decorate;
--   * `core/audit.py` declares 10 `ACTION_MEDIA_PLAN_*` actions (`created`,
--     `version.created`, `version.published`, `mapping.set`, `mapping.orphaned`,
--     `mapping.rebalanced`, `placement.attached`, `placement.detached`,
--     `import_contract.set`, `imported`) and NOT ONE of them is an acceptance.
--
-- So accepting is a WRITE that did not exist, and the only shape that survives a
-- re-read of the warehouse is a row keyed on the identity the derivation itself
-- emits: (plan_id, connector, campaign_ref).
--
-- THE GRAIN IS THE CAMPAIGN, NOT THE PLACEMENT -- arbitrage A4, and it is a
-- measurement rather than a preference. `list_unmapped_actuals` returns
-- {connector, campaign_ref, spend, reason} and nothing finer, and the placement
-- reading beside it (`query_breakdown_values`) carries a `row_count` -- NOT a
-- spend. A decision offered at the placement grain could not state the amount it
-- is about, and a decision that cannot name its amount is a decoration.
--
-- THERE IS NO `status` COLUMN, AND THAT ABSENCE IS THE DECISION. The row's
-- EXISTENCE is the acceptance: one value in a status column is what story 61.2
-- removed from the screen one table over (`plan_line_placement_mappings.status`
-- has only ever been `active`), and adding the same shape back here would teach
-- the next reader that a second value exists somewhere. A `declined` is not a
-- state nobody asked for: it is `unmatched`, which is the absence of a row.
--
-- WHAT THIS TABLE CHANGES ABOUT MONEY: NOTHING, and that is checkable rather
-- than promised. It carries no weight and no amount; `mirror_sync.py` syncs an
-- EXPLICIT list of tables (`plan_line_mappings` is in it, this one is not), so no
-- dbt model can reference it; and `dbt/models/marts/plan_vs_actual_daily.sql`
-- keeps ventilating on `mirror.plan_line_mappings WHERE status = 'active'` and on
-- that alone. An accepted campaign stays listed in the out-of-plan panel with its
-- spend: accepting says "this spend was not planned, and we know", never "this
-- spend was planned after all".
--
-- WHAT IS NOT ENFORCED HERE, stated so no reader trusts a guard that does not
-- exist: nothing below checks that `campaign_ref` really carries spend outside
-- this plan. That fact lives in the warehouse, which a CHECK cannot read, and a
-- decision about a campaign that stops being out of plan is simply a decision the
-- panel no longer shows. The database enforces the plan, the identity and the
-- presence of a reason and of an author, and claims nothing more.
--
-- ORG ERASURE REACHES THESE ROWS THROUGH `core.org_purge`, and the foreign key
-- below is what puts them on its plan. `plan_purge` walks the FK graph through
-- `confdeltype IN ('a','r')` only -- NO ACTION and RESTRICT -- so the choice of
-- ON DELETE RESTRICT (the same one migration 041 made for
-- `app.plan_line_mappings`, and the opposite of the CASCADE migration 244 chose)
-- is what makes this table visible to it. Measured on the disposable cluster
-- after applying this file: `plan_purge(conn, 'org_EXAMPLE')` emits 2277
-- statements, 1 of which names `app.plan_unmatched_spend_decisions`
-- (`DELETE FROM app.plan_unmatched_spend_decisions WHERE (plan_id) IN (SELECT id
-- FROM app.media_plans WHERE (project_id) IN …)`) -- against 2276 and 0 before it.

BEGIN;

CREATE TABLE IF NOT EXISTS app.plan_unmatched_spend_decisions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id      UUID        NOT NULL,
    connector    TEXT        NOT NULL,
    campaign_ref TEXT        NOT NULL,
    reason       TEXT        NOT NULL CHECK (btrim(reason) <> ''),
    decided_by   TEXT        NOT NULL CHECK (btrim(decided_by) <> ''),
    decided_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ON DELETE RESTRICT, on the plan ROOT and not on a version: an acceptance is a
-- statement about a plan, and a plan that vanished under its decisions would
-- leave a journal nobody can read. It is also the edge `org_purge` can see, so
-- the erasure of an org emits a DELETE for these rows by name instead of relying
-- on a cascade the graph never walks.
ALTER TABLE app.plan_unmatched_spend_decisions
    DROP CONSTRAINT IF EXISTS fk_plan_unmatched_spend_decisions_plan;
ALTER TABLE app.plan_unmatched_spend_decisions
    ADD CONSTRAINT fk_plan_unmatched_spend_decisions_plan
    FOREIGN KEY (plan_id) REFERENCES app.media_plans(id) ON DELETE RESTRICT;

-- One decision per (plan, connector, campaign). The FIRST acceptance is the one
-- that stands: `plan_spend_decisions.accept_unmatched_spend` inserts with
-- ON CONFLICT DO NOTHING and returns the row already there, so a second click --
-- or a second person -- never rewrites who decided and when.
CREATE UNIQUE INDEX IF NOT EXISTS uq_plan_unmatched_spend_decisions_identity
    ON app.plan_unmatched_spend_decisions (plan_id, connector, campaign_ref);

-- The read path of the `Placements` tab: every decision of one plan for one
-- connector, in one index scan, taken beside the derivation it annotates.
CREATE INDEX IF NOT EXISTS idx_plan_unmatched_spend_decisions_plan_connector
    ON app.plan_unmatched_spend_decisions (plan_id, connector);

COMMENT ON TABLE app.plan_unmatched_spend_decisions IS
    'Story 61.2: spend of one connector that no active plan line ventilates, ACCEPTED as '
    'unplanned by a named person on a dated row. Keyed on (plan_id, connector, campaign_ref), '
    'the identity mediaplan_mapping.list_unmapped_actuals derives -- the campaign grain, because '
    'that derivation carries a spend and the placement reading beside it carries only a row '
    'count. Carries no amount and no weight, is absent from mirror_sync.py, and is therefore '
    'readable by no dbt model: the ventilation stays on app.plan_line_mappings.status = active.';
COMMENT ON COLUMN app.plan_unmatched_spend_decisions.campaign_ref IS
    'fact_daily_kpi.breakdown_value WHERE breakdown_dimension=''campaign_id'', scoped by '
    'connector -- the same identity app.plan_line_mappings.campaign_ref carries, so a campaign '
    'that is accepted here and matched there is one campaign under one word.';
COMMENT ON COLUMN app.plan_unmatched_spend_decisions.reason IS
    'Why this spend is accepted as unplanned, in a person''s words. NOT NULL and non-blank: an '
    'acceptance with no reason is indistinguishable from a row somebody clicked past.';
COMMENT ON COLUMN app.plan_unmatched_spend_decisions.decided_by IS
    'The identity subject who accepted, as app.plan_line_mappings.created_by carries it. An '
    'acceptance is a dated human act, which is why it is stored rather than derived.';

-- The application role is granted explicitly rather than left to the default
-- privileges migration 207 set FOR ROLE postgres: a migration applied by any
-- other owner would create this table outside that default and leave every write
-- refused with InsufficientPrivilege at runtime. Guarded, because the role does
-- not exist on every target.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE app.plan_unmatched_spend_decisions TO connector;
    END IF;
END;
$$;

COMMIT;
