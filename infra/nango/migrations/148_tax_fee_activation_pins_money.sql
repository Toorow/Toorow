-- Story 48.4, follow-up to 146: the activation projection pins the Money Policy.
-- (Numbered 148 rather than 147: a parallel session claimed 147 while this was
-- being written. The catalog check is what caught it -- see CLAUDE.md section 8.)
--
-- WHY A SECOND MIGRATION AND NOT AN EDIT TO 146. 146 is applied. An applied
-- migration is never re-edited -- its checksum is in the ledger, and rewriting it
-- makes every deployment that already ran it indistinguishable from one that ran
-- something else. Corrections go forward.
--
-- WHAT 146 LEFT OPEN. Its `project_tax_fee_activation_v` answered "is Tax & Fees
-- on?" and stopped there. Two things were missing and both matter to the dbt
-- models that read it:
--
--   * The reporting currency. Every Tax model's module gate selected
--     `project_preferences.canonical_currency` beside the activation flag. That
--     column carries `DEFAULT 'EUR'` from migration 008 -- the exact defect Story
--     48.3 removed for Currency & FX -- so a Project that had never chosen a
--     reporting currency still composed its invoice totals in euros. The confirmed
--     Money Policy is the authority, and it is projected here so the models read
--     ONE relation for the whole gate rather than joining a second one.
--   * The dependency itself. AC1: "Currency & FX is required." A ladder composes
--     exact money; with no confirmed Money Policy there is no currency to state a
--     total in, so `tax_fees_active` must be FALSE rather than TRUE-with-no-currency.
--     146's predicate did not test it.
--
-- Idempotent: CREATE OR REPLACE only. No table, no row, no rate.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The confirmed Money Policy, projected.
--
--    One row per Project that has PUBLISHED one. A Project with no row has not
--    decided, which is a different state from "decided on the default" -- and
--    keeping them apart is the whole point of Story 48.3's lifecycle.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.project_money_policy_v AS
SELECT s.project_id,
       s.id                                              AS money_policy_rule_set_id,
       v.id                                              AS money_policy_version_id,
       v.content_hash                                    AS money_policy_content_hash,
       v.payload->>'reporting_currency'                  AS reporting_currency,
       (v.payload->>'reporting_currency_minor_unit')::INTEGER AS reporting_currency_minor_unit,
       COALESCE(v.payload->>'rounding', 'half_even')     AS money_rounding
FROM app.governance_rule_sets s
JOIN app.governance_rule_set_versions v
  ON v.id = s.current_version_id
 AND v.rule_set_id = s.id
 AND v.project_id = s.project_id
WHERE s.family = 'money_policy'
  AND s.lifecycle_status <> 'archived'
  AND v.status = 'published';


-- ---------------------------------------------------------------------------
-- 2. The activation projection, with its money dependency made explicit.
--
--    Column order is APPEND-ONLY relative to 146: the mirror reads SELECT *, and a
--    reordering would silently re-map a dbt column reference.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.project_tax_fee_activation_v AS
SELECT p.id                                        AS project_id,
       p.active_configuration_version_id           AS project_configuration_version_id,
       COALESCE(c.state, 'disabled')               AS capability_state,
       (COALESCE(c.state, 'disabled') <> 'disabled'
        AND c.active_version_id IS NOT NULL
        AND c.active_version_id = p.active_configuration_version_id
        AND rs.rule_set_version_id IS NOT NULL
        -- AC1: Currency & FX is a hard dependency, not a warning.
        AND mp.money_policy_version_id IS NOT NULL) AS tax_fees_active,
       rs.rule_set_id,
       rs.rule_set_version_id,
       rs.rule_set_content_hash,
       rs.rounding,
       rs.default_money_basis,
       COALESCE(rs.rule_count, 0)                  AS rule_count,
       mp.reporting_currency,
       mp.money_policy_version_id,
       mp.money_policy_content_hash
FROM app.projects p
LEFT JOIN app.project_capabilities c
       ON c.project_id = p.id AND c.capability_key = 'tax_fees'
LEFT JOIN app.tax_fee_rule_set_v rs
       ON rs.project_id = p.id
LEFT JOIN app.project_money_policy_v mp
       ON mp.project_id = p.id;

COMMIT;
