-- T11 (Story 41.3, AC1 / AC14 / C.2 / decision D3) -- the governance projection is
-- governed.
--
-- Asserts on fee_tax_rules_effective:
--   * every row is status='confirmed' -- 'proposed' is what 41.2's auto-population
--     lands and it must be INERT until a human confirms it (the epic's
--     human-in-the-loop, made structural). The dev seeder deliberately plants a
--     status='proposed' 50 % platform fee: if it ever appears here, or in any ladder
--     row's applied_rule_ids, the confirmation gate is broken;
--   * every row's project has the module ON;
--   * no inverted effective window survived;
--   * scope_precedence is one of 1/2/3 and scope_state one of the three vocabulary
--     values;
--   * and -- because decision D3 REMOVED the UNIQUE (project_id, cascade_phase,
--     sequence_order) constraint, so two rules may legitimately share a slot -- that
--     the ladder's per-slot pick is DETERMINISTIC: no (row, category, cascade_phase,
--     sequence_order) yields two winners. This asserts the determinism rather than
--     assuming a uniqueness the schema does not provide.
--
-- CARDINALITY GUARD: the view must not be empty, or every assertion is vacuous.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{#- UNE PREMISSE ABSENTE N EST PAS UN DEFAUT (AI-314, 2026-08-24).
    Ce test epingle un exemple SEME : il mesure ce que la fixture locale porte, et
    la fixture vit dans le MIROIR. `mirror_sync` differe ses ecritures BigQuery
    (Phase B), donc dans un entrepot ou le miroir n a pas ete ecrit -- toute la
    production aujourd hui -- ce test ne trouve rien a mesurer et rend son
    CARDINALITY_FAIL : un rouge qui accuse le calcul d un defaut dont la cause est
    qu il n y a rien a calculer. Un test rouge est un code de sortie, et un code
    de sortie est un projet sans marts.
    Il DECLINE donc de juger, EN LE DISANT : `TOOROW_SOURCE_ABSENT` remonte au
    nocturne, qui refuse alors le mot << ok >> pour ce projet. La ou le miroir EST
    -- la boucle locale, la CI -- rien ne bouge et l assertion reste entiere. -#}
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_tax_fee_activation']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH rules AS (
    SELECT * FROM {{ ref('fee_tax_rules_effective') }}
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fee_tax_rules_effective is empty -- run'
        || ' seed_fee_tax_mirror.py (and mirror_sync) before dbt build' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM rules) = 0
),

not_confirmed AS (
    SELECT r.rule_id AS subject,
           'GOVERNANCE_FAIL: status=' || r.status || ' entered the effective rule set --'
           || ' only confirmed rules may reach the cascade' AS failure_reason
    FROM rules r WHERE r.status <> 'confirmed'
),

proposed_leaked AS (
    SELECT 'ftr_dev_complete_proposed' AS subject,
           'GOVERNANCE_FAIL: the seeded status=proposed rule reached the ladder --'
           || ' the human-confirmation gate is broken' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.applied_rule_ids LIKE '%ftr_dev_complete_proposed%'
),

module_off_rule AS (
    SELECT r.rule_id AS subject,
           'GOVERNANCE_FAIL: a rule survived for a project whose module is OFF'
               AS failure_reason
    FROM rules r
    LEFT JOIN (
        -- Story 48.4 / migration 146: the ONE activation authority.
        SELECT project_id FROM {{ source('mirror', 'project_tax_fee_activation') }}
        WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
    ) p ON p.project_id = r.project_id
    WHERE p.project_id IS NULL
),

inverted_window AS (
    SELECT r.rule_id AS subject,
           'GOVERNANCE_FAIL: effective_to < effective_from survived the integrity guard'
               AS failure_reason
    FROM rules r
    WHERE r.effective_to IS NOT NULL AND r.effective_to < r.effective_from
),

bad_vocabulary AS (
    SELECT r.rule_id AS subject,
           'GOVERNANCE_FAIL: scope_precedence or scope_state is outside its vocabulary'
               AS failure_reason
    FROM rules r
    WHERE r.scope_precedence NOT IN (1, 2, 3)
       OR r.scope_state NOT IN ('RESOLVED', 'AMBIGUOUS', 'UNRESOLVED')
),

-- ============ PRECEDENCE: WHAT IS REALLY TESTED, AND WHAT IS NOT =============
-- The round-1 audit flagged the old `nondeterministic_slot` block as near-tautological,
-- and it was: the ladder applies ROW_NUMBER() ... WHERE _rn = 1 per
-- (row_key, category, cascade_phase, sequence_order), so at most one rule per slot can
-- reach applied_rule_ids BY CONSTRUCTION and the HAVING COUNT(DISTINCT) > 1 could not
-- fire. It is kept below, LABELLED as a tripwire, and two blocks with real
-- discriminating content are added.
--
-- HONEST COVERAGE GAP, recorded rather than papered over: NO fixture currently has two
-- rules CONTENDING FOR THE SAME SLOT, so the precedence CONTEST itself -- which of two
-- rules at (category, phase, sequence_order) wins under
-- `scope_precedence DESC, effective_from DESC, rule_id ASC` -- is untested end to end.
-- Closing it needs a seeder fixture (a project-scoped and a datastream-scoped rule at the
-- SAME slot, asserting the datastream one wins), which is outside this pass's
-- test-files-only remit. Flagged to the orchestrator as a follow-up.

precedence_rank_mapping AS (
    -- REAL. scope_precedence is emitted here and consumed by the ladder's ORDER BY --
    -- but with no slot contention anywhere, an inverted or constant rank changes NOTHING
    -- observable in the ladder, so no other test in the suite can see it. It is also
    -- read directly by 41.7's Global Rule Matrix. This is the only assertion that pins
    -- the mapping itself.
    SELECT
        r.rule_id AS subject,
        'PRECEDENCE_MAPPING_FAIL: scope_kind=' || r.scope_kind || ' carries'
        || ' scope_precedence=' || CAST(r.scope_precedence AS STRING)
        || ', expected datastream=3 > plan_version=2 > project=1' AS failure_reason
    FROM rules r
    WHERE r.scope_precedence <> CASE r.scope_kind
                                    WHEN 'datastream'   THEN 3
                                    WHEN 'plan_version' THEN 2
                                    ELSE 1
                                END
),

same_phase_slots AS (
    -- Two UNCONDITIONED, RESOLVED rules of the same category and phase at DIFFERENT
    -- sequence_order values. feetax_dev_complete has exactly this: platform 3 % at slot
    -- (PLATFORM_FEE, 2, 1) and the datastream-scoped 1 % at (PLATFORM_FEE, 2, 2).
    SELECT project_id, category, cascade_phase, COUNT(DISTINCT sequence_order) AS n_slots,
           COUNT(*) AS n_rules
    FROM rules
    WHERE NOT has_conditions
      AND scope_state = 'RESOLVED'
      AND category IN ('PLATFORM_FEE', 'REGULATORY_TAX', 'WHT_GROSS_UP',
                       'AGENCY_FEE', 'SALES_TAX')
    GROUP BY project_id, category, cascade_phase
    HAVING COUNT(DISTINCT sequence_order) > 1
),

slot_anti_vacuity AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no project has two unconditioned rules of the SAME category'
        || ' and phase at DIFFERENT sequence_order, so "the slot is the override handle,'
        || ' not the phase" is untested -- and dropping sequence_order from the'
        || ' precedence partition would silently delete one of a phase''s legitimate'
        || ' fees' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM same_phase_slots) = 0
),

slot_coexistence AS (
    -- REAL, and it is the assertion the story's §D.5 rationale demands: "a phase can
    -- legitimately host several distinct fees ... without a slot-level key you would
    -- either double-apply VAT or silently drop a legitimate second platform fee."
    -- Every rule occupying its own slot inside a shared (category, phase) must fire on
    -- every COMPLETE row of its project. Drop sequence_order from the ladder's
    -- PARTITION BY and exactly one of the two survives, so this fails.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value
            || '|' || r.category AS subject,
        'SLOT_FAIL: rule ' || r.rule_id || ' occupies its own slot ('
        || r.category || ', phase ' || CAST(r.cascade_phase AS STRING) || ', seq '
        || CAST(r.sequence_order AS STRING) || ') but did not fire. Two fees sharing a'
        || ' PHASE at different SLOTS must BOTH apply -- the slot is the override handle,'
        || ' not the phase. applied=' || l.applied_rule_ids AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    JOIN same_phase_slots s
        ON s.project_id = l.project_id
    JOIN rules r
        ON  r.project_id    = s.project_id
        AND r.category      = s.category
        AND r.cascade_phase = s.cascade_phase
        AND NOT r.has_conditions
        AND r.scope_state = 'RESOLVED'
    WHERE l.is_ladder_complete
      AND l.applied_rule_ids NOT LIKE '%' || r.rule_id || '%'
),

nondeterministic_slot_tripwire AS (
    -- LABELLED TRIPWIRE, not evidence. Cannot fail while the ladder picks a slot winner
    -- with ROW_NUMBER() + WHERE _rn = 1; earns its keep only if that pick is ever
    -- replaced by something that could emit two winners.
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.connector
            || '|' || l.breakdown_value || '|' || r.category || '|'
            || CAST(r.cascade_phase AS STRING) || '|' || CAST(r.sequence_order AS STRING)
            AS subject,
        'STRUCTURAL_FAIL: two rules won the SAME (row, category, phase, sequence_order)'
        || ' slot -- the precedence pick stopped being a single-winner pick'
            AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    JOIN rules r
        ON  r.project_id = l.project_id
        AND l.applied_rule_ids LIKE '%' || r.rule_id || '%'
    GROUP BY l.project_id, l.date, l.connector, l.breakdown_value,
             r.category, r.cascade_phase, r.sequence_order
    HAVING COUNT(DISTINCT r.rule_id) > 1
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM not_confirmed
UNION ALL SELECT subject, failure_reason FROM proposed_leaked
UNION ALL SELECT subject, failure_reason FROM module_off_rule
UNION ALL SELECT subject, failure_reason FROM inverted_window
UNION ALL SELECT subject, failure_reason FROM bad_vocabulary
UNION ALL SELECT subject, failure_reason FROM precedence_rank_mapping
UNION ALL SELECT subject, failure_reason FROM slot_anti_vacuity
UNION ALL SELECT subject, failure_reason FROM slot_coexistence
UNION ALL SELECT subject, failure_reason FROM nondeterministic_slot_tripwire
{%- endif -%}
