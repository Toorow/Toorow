-- fee_tax_rules_effective: the CONFIRMED fee & tax rule set of every project whose
-- fee & tax alignment module is ON, with its scope resolved (Epic 41, Story 41.3 /
-- E41-FR03 / arbitration B2 / decisions D2, D3, D5, D9).
--
-- ============================ THIS MART IS A READ ============================
-- fee_tax_rules_effective READS the governed rule mirror (mirror.fee_tax_rules,
-- mirror.fee_tax_rule_conditions, mirror.datastreams_dim, mirror.media_plan_versions)
-- and mirror.project_preferences. It creates NO new fact row and edits NO existing
-- model. fact_daily_kpi / cross_source_conversions / cross_source_revenue /
-- metric_baselines / dedup_estimate / plan_vs_actual_daily are STRICTLY UNTOUCHED --
-- this model does not even read fact_daily_kpi. Epic 41 is an ADDITIVE overlay
-- (arbitration B2): with the module OFF, this view returns ZERO rows, which is what
-- makes E41-NFR01 ("module OFF => byte-identical") true BY CONSTRUCTION.
--
-- GRAIN (enforced by fee_tax_rules_effective_grain_unique):
--   one row per rule_id. DELIBERATELY NOT per (rule x day): this view is the
--   GOVERNANCE projection -- 41.7's Global Rule Matrix and 41.8's MCP tools read it
--   and the epic requires the rule set to be inspectable AS A RULE SET. Fanning it
--   out by the calendar would multiply it for no benefit and couple governance to
--   facts. The PER-DAY effective-window test therefore lives in the ladder, because
--   only the ladder knows the day.
--
-- ===================== ACTIVATION (E41-FR01 / C.1) ==========================
-- THE GATE READ HERE IS mirror.project_tax_fee_activation.tax_fees_active -- see the
-- `module_on` CTE below, which is the executable statement of this paragraph. It is
-- NOT project_preferences.fee_tax_alignment_enabled: that column was the gate when
-- 41.3 landed, and Story 48.4 / migration 146 moved the authority because the two
-- could disagree (146:11-15 -- "a Project whose capability was disabled through the
-- Change Set kept producing Tax rows"). Since 146 the old column is a PROJECTION of
-- the new authority in production, so the sentence "read the preferences flag" is
-- true of neither the code nor the schema.
--
-- FALSE, or an absent activation row, means OFF -- and an OFF project contributes no
-- rule, hence no ladder row anywhere downstream. Because the fixture universe used
-- to DERIVE tax_fees_active from the preferences flag, that divergence was
-- unfalsifiable locally; dbt/tests/test_epic41_module_off_zero_rows.sql now carries a
-- DIVERGENCE guard that refuses a fixture in which the two columns always agree.
--
-- ===================== STATUS (C.2, human-in-the-loop) ======================
-- status = 'confirmed' ONLY -- AND THIS FILTER IS EMPTY IN PRODUCTION TODAY. Migration
-- 146:508 makes `status` the CONSTANT 'confirmed' in app.fee_tax_rules_dim_v, which is
-- the relation the mirror copies, so no mirrored row can carry 'proposed' or
-- 'disabled' and this WHERE clause removes nothing. It is kept because the mirrored
-- COLUMN still exists and a true statement is cheaper to keep than to re-derive.
--
-- WHAT ACTUALLY HOLDS "review, confirm, or override" TODAY, and where: publication of
-- a rule-set VERSION. app.tax_fee_rule_set_v (146:369-387) admits a project only when
-- `governance_rule_set_versions.status = 'published'` AND that version is the rule
-- set's `current_version_id`; app.tax_fee_published_ladder_v (146:473) then flattens
-- only that version's ordered_rules. An unconfirmed rate is therefore not a row with a
-- weaker status -- it is a row that is NOT IN THE PUBLISHED VERSION AT ALL, so it
-- never reaches the mirror, and this model cannot see it.
--
-- THE CONSEQUENCE FOR 41.2, STATED SO IT IS NOT REDISCOVERED AS A BUG: auto-population
-- writes status='proposed' into app.fee_tax_rules, and since 146 that table is no
-- longer what the flattening views read. An auto-proposal has no path into the cascade
-- until it is carried into a published rule-set version. That wiring is not built.
--
-- ===================== EFFECTIVE WINDOW =====================================
-- effective_from / effective_to are normalised to typed DATE here (effective_to
-- NULL = open-ended) and an INVERTED window is DROPPED as an integrity guard.
-- The per-day test (`day BETWEEN effective_from AND COALESCE(effective_to, day)`)
-- is applied in fee_tax_ladder_daily. A rule out of window for a day is simply
-- ABSENT for that day -- not NO_MATCH, not a gap.
--
-- ===================== SCOPE RESOLUTION (D2 + D5) ===========================
-- This is RULE-SCOPE resolution -- mapping a rule's scope_ref to the rows it
-- governs. It is NOT attribute resolution: country / market / source_type belong
-- entirely to Story 41.2's fee_tax_country_resolution view (ruling R3), and no line
-- of Epic 41.3 resolves any of them.
--
--   scope_kind='project'       -> every row of the project.            RESOLVED
--   scope_kind='datastream'    -> mirror.datastreams_dim on
--                                 (project_id, datastream_id = scope_ref) gives the
--                                 CONNECTOR (fact_daily_kpi has no datastream_id).
--                                 Then count the project's datastreams for that
--                                 connector: exactly 1 -> RESOLVED; >= 2 ->
--                                 AMBIGUOUS (the connector's rows mix both and
--                                 there is no honest way to attribute them, so the
--                                 ladder raises DATASTREAM_SCOPE_AMBIGUOUS and
--                                 ONE IS NEVER PICKED); scope_ref absent from the
--                                 dim -> UNRESOLVED (we do not even know the
--                                 connector, so the ladder's blast radius is the
--                                 whole project).
--                                 Rows with connector IS NULL are SKIPPED: those are
--                                 managed_feed / external_bq datastreams (module_name
--                                 is NULLABLE since migration 030) and they can never
--                                 match a fact row.
--   scope_kind='plan_version'  -> mirror.media_plan_versions on id = scope_ref gives
--                                 the plan_id; the ladder then joins
--                                 mirror.plan_line_mappings (status='active') on
--                                 (connector, campaign_ref) to decide, PER ROW,
--                                 whether the row is covered. A version id absent
--                                 from the mirror -> UNRESOLVED (a governance
--                                 problem, not a grain problem).
--
-- ============ SCOPE PRECEDENCE IS EMITTED HERE, APPLIED IN THE LADDER =======
-- THE TRAP: a datastream-scoped rule must override a project-scoped rule ONLY FOR
-- THAT DATASTREAM'S ROWS. Applying precedence at rule grain would delete the
-- project-scoped rule for EVERY row -- a silent, hard-to-see wrong answer. So this
-- view emits only the RANK (datastream 3 > plan_version 2 > project 1) and
-- fee_tax_ladder_daily applies it per matched (row x rule) pair with
-- ROW_NUMBER() + WHERE _rn = 1 (the plan_vs_actual_daily idiom; no QUALIFY, which
-- is not portable).
--
-- ===================== D3: NO SLOT UNIQUENESS EXISTS ========================
-- Migration 119 deliberately ships NO UNIQUE (project_id, cascade_phase,
-- sequence_order): every rule inside a cascade phase reads the subtotal AS AT PHASE
-- ENTRY, so intra-phase rules do not compound and sequence_order is a
-- display/override key. Two rules may legitimately share a slot -- the ladder's
-- tiebreak (effective_from DESC, rule_id ASC) makes the pick DETERMINISTIC, and
-- test_epic41_rules_effective_governance.sql asserts that determinism rather than
-- assuming a uniqueness the schema does not provide.
--
-- ===================== COLUMN-NAME NOTE (grounded, not paraphrased) =========
-- The mirrored relation's primary key is `id`, not `rule_id`: migration 119's
-- app.fee_tax_rules_dim_v projects `id` (22 scalar columns) and mirror_sync copies
-- it verbatim. This view aliases it to rule_id, which is the name
-- mirror.fee_tax_rule_conditions / mirror.fee_tax_rule_tiers use for their foreign
-- key, so everything downstream of this model joins on one name.
-- `currency` is exposed as `rule_currency` so it can never be confused with the
-- ladder row's own (project canonical) currency, which is a different thing (§D.6).

-- ============ THE MIRROR MAY NOT BE IN THIS WAREHOUSE AT ALL (AI-314) =======
-- Every relation this model reads is mirrored, and `mirror_sync` defers its
-- BigQuery writes (Phase B): in production the `mirror` dataset does not exist,
-- so this model answered *Not found: Dataset toorow:mirror was not found in
-- location EU* and dbt skipped the whole Tax ladder behind it (measured
-- 2026-08-24). Without the activation relation NO Project can be active -- which
-- is the same thing this model already says when `tax_fees_active` is false --
-- so it is built EMPTY and names what it did not find, instead of failing the
-- build of a Project that never turned the module on.
{{ config(materialized='view') }}

{%- set mirror_missing = toorow_absent_sources('mirror', [
      'project_tax_fee_activation',
      'fee_tax_rules',
      'fee_tax_rule_conditions',
      'datastreams_dim',
      'media_plans',
      'media_plan_versions',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['rule_id', 'string'],
    ['project_id', 'string'],
    ['scope_kind', 'string'],
    ['scope_ref', 'string'],
    ['category', 'string'],
    ['form', 'string'],
    ['rate', 'float'],
    ['amount_micros', 'bigint'],
    ['cpm_micros', 'bigint'],
    ['rule_currency', 'string'],
    ['base_target', 'string'],
    ['cascade_phase', 'bigint'],
    ['sequence_order', 'bigint'],
    ['effective_from', 'date'],
    ['effective_to', 'date'],
    ['status', 'string'],
    ['origin', 'string'],
    ['label', 'string'],
    ['scope_precedence', 'int'],
    ['scope_connector', 'string'],
    ['scope_plan_id', 'string'],
    ['scope_state', 'string'],
    ['has_conditions', 'boolean'],
    ['condition_row_count', 'bigint'],
]) }}
{%- else %}

WITH module_on AS (
    -- E41-FR01 / C.1. COALESCE(..., FALSE) covers both an explicit FALSE and the
    -- mirror landing the column as NULL. CAST because the mirror writer types an
    -- EMPTY mirrored table's columns as strings.
    -- Story 48.4: the ONE activation authority. This used to read
    -- project_preferences.fee_tax_alignment_enabled, a second boolean beside
    -- app.project_capabilities that could disagree with it -- and this was the one
    -- every Tax model believed, so a capability disabled through a Project Change Set
    -- kept producing Tax-derived rows. mirror.project_tax_fee_activation projects
    -- app.project_tax_fee_activation_v, where active requires all three of: the
    -- capability is not disabled, its pin names the Project's CURRENT active
    -- configuration version, and a Rule Ladder version is published.
    --
    -- reporting_currency comes from the confirmed Money Policy (Story 48.3), not from
    -- project_preferences.canonical_currency, whose DEFAULT 'EUR' made every Project
    -- look decided from birth.
    SELECT
        project_id,
        reporting_currency AS canonical_currency
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

rules_confirmed AS (
    SELECT
        r.id                                AS rule_id,
        r.project_id                        AS project_id,
        r.scope_kind                        AS scope_kind,
        r.scope_ref                         AS scope_ref,
        r.category                          AS category,
        r.form                              AS form,
        -- rate stays in its mirrored exact-decimal type: every rounding boundary in
        -- this engine lives inside the two fee_tax_* macros, never in a model.
        r.rate                              AS rate,
        CAST(r.amount_micros AS BIGINT)     AS amount_micros,
        CAST(r.cpm_micros    AS BIGINT)     AS cpm_micros,
        r.currency                          AS rule_currency,
        r.base_target                       AS base_target,
        CAST(r.cascade_phase  AS BIGINT)    AS cascade_phase,
        CAST(r.sequence_order AS BIGINT)    AS sequence_order,
        CAST(r.effective_from AS DATE)      AS effective_from,
        CAST(r.effective_to   AS DATE)      AS effective_to,
        r.status                            AS status,
        r.origin                            AS origin,
        r.label                             AS label
    FROM {{ source('mirror', 'fee_tax_rules') }} r
    JOIN module_on m
        ON m.project_id = r.project_id
    WHERE r.status = 'confirmed'
),

rules_windowed AS (
    -- Integrity guard: an inverted window is not a rule, it is a data defect. Drop
    -- it rather than letting it silently never fire (or, worse, always fire).
    SELECT *
    FROM rules_confirmed
    WHERE effective_from IS NOT NULL
      AND (effective_to IS NULL OR effective_to >= effective_from)
),

condition_counts AS (
    -- The empty-conditions FAST PATH made explicit: a rule with NO row in
    -- mirror.fee_tax_rule_conditions is UNCONSTRAINED and matches every row (an
    -- empty `conditions` object and an empty `source_type_scope` both flatten to
    -- zero rows -- absence is never "no match").
    SELECT
        rule_id,
        COUNT(*) AS n_conditions
    FROM {{ source('mirror', 'fee_tax_rule_conditions') }}
    GROUP BY rule_id
),

live_datastreams AS (
    -- C1: `connector` is app.datastreams.module_name ALIASED, because everything in
    -- the warehouse joins on fact_daily_kpi.connector. NULL connector = managed_feed
    -- / external_bq: mirrored on purpose (41.2 needs to see it), useless here.
    SELECT
        project_id,
        datastream_id,
        connector
    FROM {{ source('mirror', 'datastreams_dim') }}
    WHERE connector IS NOT NULL
),

datastreams_per_connector AS (
    SELECT
        project_id,
        connector,
        COUNT(*) AS n_datastreams
    FROM live_datastreams
    GROUP BY project_id, connector
),

scope_datastream AS (
    -- datastream_id is the PK of the dim, so this join can never fan out.
    SELECT
        r.rule_id                       AS rule_id,
        d.connector                     AS scope_connector,
        COALESCE(c.n_datastreams, 0)    AS n_datastreams_on_connector
    FROM rules_windowed r
    JOIN live_datastreams d
        ON  d.project_id    = r.project_id
        AND d.datastream_id = r.scope_ref
    LEFT JOIN datastreams_per_connector c
        ON  c.project_id = r.project_id
        AND c.connector  = d.connector
    WHERE r.scope_kind = 'datastream'
),

scope_plan AS (
    -- media_plan_versions.id is the PK, so this join can never fan out either.
    --
    -- PROJECT-SCOPED (review finding F4, same class as the rollup leak). Neither
    -- mirror.media_plan_versions nor mirror.plan_line_mappings carries a project_id,
    -- so the owning project has to be recovered through mirror.media_plans. Without
    -- `p.project_id = r.project_id` a rule could resolve against ANOTHER PROJECT'S
    -- plan -- and since an agency routinely connects one Meta account into two
    -- projects, the campaign refs collide and the rule would silently start billing
    -- on the wrong tenant's spend. A version id that does not belong to the rule's
    -- own project is therefore UNRESOLVED, which is loud, rather than resolved
    -- against a stranger's plan, which is silent.
    SELECT
        r.rule_id   AS rule_id,
        v.plan_id   AS scope_plan_id
    FROM rules_windowed r
    JOIN {{ source('mirror', 'media_plan_versions') }} v
        ON v.id = r.scope_ref
    JOIN {{ source('mirror', 'media_plans') }} p
        ON  p.id         = v.plan_id
        AND p.project_id = r.project_id
    WHERE r.scope_kind = 'plan_version'
)

SELECT
    r.rule_id,
    r.project_id,
    r.scope_kind,
    r.scope_ref,
    r.category,
    r.form,
    r.rate,
    r.amount_micros,
    r.cpm_micros,
    r.rule_currency,
    r.base_target,
    r.cascade_phase,
    r.sequence_order,
    r.effective_from,
    r.effective_to,
    r.status,
    r.origin,
    r.label,
    -- The RANK only. Applying it here would be the §D.5 trap.
    CASE r.scope_kind
        WHEN 'datastream'   THEN 3
        WHEN 'plan_version' THEN 2
        ELSE 1
    END                                             AS scope_precedence,
    sd.scope_connector                              AS scope_connector,
    sp.scope_plan_id                                AS scope_plan_id,
    CASE
        WHEN r.scope_kind = 'datastream'   AND sd.scope_connector IS NULL          THEN 'UNRESOLVED'
        WHEN r.scope_kind = 'datastream'   AND sd.n_datastreams_on_connector >= 2  THEN 'AMBIGUOUS'
        WHEN r.scope_kind = 'datastream'                                           THEN 'RESOLVED'
        WHEN r.scope_kind = 'plan_version' AND sp.scope_plan_id IS NULL            THEN 'UNRESOLVED'
        WHEN r.scope_kind = 'plan_version'                                         THEN 'RESOLVED'
        ELSE 'RESOLVED'
    END                                             AS scope_state,
    COALESCE(cc.n_conditions, 0) > 0                AS has_conditions,
    COALESCE(cc.n_conditions, 0)                    AS condition_row_count
FROM rules_windowed r
LEFT JOIN condition_counts cc ON cc.rule_id = r.rule_id
LEFT JOIN scope_datastream sd ON sd.rule_id = r.rule_id
LEFT JOIN scope_plan       sp ON sp.rule_id = r.rule_id
{%- endif -%}
