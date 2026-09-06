-- fee_tax_ladder_rollup: connector / country / plan-line / global rollups of the
-- six-phase cascade (Epic 41, Story 41.3 / E41-FR03 / §D.8).
--
-- ============================ THIS MART IS A READ ============================
-- fee_tax_ladder_rollup READS fee_tax_ladder_daily and mirror.plan_line_mappings.
-- It creates NO new fact row and edits NO existing model. fact_daily_kpi /
-- cross_source_conversions / cross_source_revenue / metric_baselines /
-- dedup_estimate / plan_vs_actual_daily are STRICTLY UNTOUCHED -- this model does
-- not even read fact_daily_kpi. With the module OFF, fee_tax_ladder_daily is empty
-- and so is this view: zero rows, by construction (E41-NFR01).
--
-- GRAIN (enforced by fee_tax_ladder_rollup_grain_unique):
--   one row per (project_id, date, rollup_kind, rollup_key, currency).
--
--   rollup_kind   rollup_key
--   -----------   ----------------------------------------------------------------
--   connector     the connector
--   country       attr_country, or the literal '__unresolved__' for rows whose
--                 country is NULL. NEVER dropped, NEVER bucketed into a real country
--                 -- an unresolved row must stay visible AS unresolved.
--   plan_line     plan_id || '|' || line_key from mirror.plan_line_mappings
--                 (status='active')
--   global        the literal 'all'
--
-- ===================== HONEST GAP PROPAGATION (arbitration B1) ==============
-- Aggregating a partly-incomplete set is where a rollup is most tempted to lie. The
-- rules here, all three of them deliberate:
--   * every micros column is an integer SUM();
--   * a PHASE column is NULL as soon as ANY member row's is NULL -- SUM() would
--     silently skip the NULLs and present a short total as a full one;
--   * subtotal_ht_micros / total_ttc_micros are NULL when ANY member is incomplete;
--   * total_ttc_micros_complete_only + complete_row_count / total_row_count give a
--     surface something true to show instead of either lying or showing nothing:
--     "20 339.68 EUR over 8 of 11 rows".
-- A ladder row whose campaign is mapped to NO active line contributes to no
-- plan_line rollup key; it is still counted in the connector / country / global
-- rollups, so it is visible as coverage and never silently dropped.
--
-- ===================== C4: TWO REASON ROWS WERE DELETED =====================
-- D6 approved a visible rollup_key='__unavailable__' reason row because
-- plan_line_mappings keys on campaign_id while the ladder ran at
-- MIN(breakdown_dimension) = 'ad_id' for meta-ads / tiktok-ads. C4 removed the
-- mismatch AT ITS ROOT: fee_tax_ladder_daily's canonical pick now PREFERS
-- campaign_id, so the plan-line rollup simply resolves and the reason row can never
-- fire. DO NOT REINTRODUCE IT, and do NOT re-read fact_daily_kpi at campaign grain
-- to "fix" anything -- the canonical pick already put the ladder there and a second
-- read would double-count against it. test_epic41_rollup_reconciliation.sql asserts
-- the ABSENCE of '__unavailable__', so a silent reintroduction fails the build.
--
-- ===================== THE plan_line ROLLUP IS NOT VENTILATED ===============
-- A campaign mapped to two lines contributes its FULL ladder row to BOTH line keys.
-- This is a GROUPING for navigation ("show me this line's composed cost"), not a
-- ventilated total: splitting by split_weight is Story 22.4's ventilation, and doing
-- it again here would produce a second, competing ventilation of the same spend.
-- Consequence, stated so nobody discovers it in a dashboard: plan_line rows MUST NOT
-- be summed across lines. The connector / country / global kinds are partitions of
-- the ladder and DO sum -- test_epic41_rollup_reconciliation.sql pins
-- SUM(connector) == global exactly, and deliberately does not pin plan_line.

-- ============ THE MIRROR MAY NOT BE IN THIS WAREHOUSE AT ALL (AI-314) =======
-- `mirror_sync` writes the mirror to DuckDB and journals `write for <table>
-- deferred (Phase B)` for its BigQuery target, so in production the `mirror`
-- dataset DOES NOT EXIST and every model reading it answered *Not found: Dataset
-- toorow:mirror was not found in location EU* -- which failed the build and made
-- dbt skip every model behind it (measured 2026-08-24; two Projects of three
-- built no mart at all). This rollup reads the plan mirror to scope its
-- `plan_line` rows; with no mirror there is no ladder to roll up either (its
-- upstream is guarded the same way), so it is built EMPTY and NAMES what it did
-- not find rather than failing the nightly of a Project that has no plan.
{{ config(materialized='view') }}

{%- set mirror_missing = toorow_absent_sources('mirror', [
      'media_plans',
      'plan_line_mappings',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['date', 'date'],
    ['rollup_kind', 'string'],
    ['rollup_key', 'string'],
    ['currency', 'string'],
    ['net_media_micros', 'bigint'],
    ['platform_fee_micros', 'bigint'],
    ['regulatory_tax_micros', 'bigint'],
    ['wht_gross_up_micros', 'bigint'],
    ['agency_fee_micros', 'bigint'],
    ['sales_tax_micros', 'bigint'],
    ['subtotal_ht_micros', 'bigint'],
    ['total_ttc_micros', 'bigint'],
    ['total_ttc_micros_complete_only', 'bigint'],
    ['total_row_count', 'bigint'],
    ['complete_row_count', 'bigint'],
    ['is_ladder_complete', 'boolean'],
    ['gap_codes', 'string'],
]) }}
{%- else %}

WITH ladder AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }}
),

-- PROJECT-SCOPED (review finding F4 -- a cross-project leak, now closed).
-- mirror.plan_line_mappings has NO project_id: it keys on (plan_id, line_key,
-- connector, campaign_ref). Joining a ladder row on (connector, campaign_ref) alone
-- means that when an agency connects ONE Meta account into TWO projects -- the routine
-- per-brand split -- project B's spend gets attributed to project A's plan and line,
-- and project B emits a rollup row keyed by a plan it cannot see.
-- plan_vs_actual_daily.sql:141-143 handles exactly this and says so; this now does too,
-- by recovering the owning project through mirror.media_plans.
active_mappings AS (
    SELECT
        mp.project_id   AS project_id,
        pm.connector    AS connector,
        pm.campaign_ref AS campaign_ref,
        pm.plan_id      AS plan_id,
        pm.line_key     AS line_key
    FROM {{ source('mirror', 'plan_line_mappings') }} pm
    JOIN {{ source('mirror', 'media_plans') }} mp
        ON mp.id = pm.plan_id
    WHERE pm.status = 'active'
),

members AS (
    SELECT 'connector' AS rollup_kind, l.connector AS rollup_key, l.*
    FROM ladder l

    UNION ALL

    -- '__unresolved__' is a REASON BUCKET, not a country. It exists so an unresolved
    -- row stays countable instead of vanishing from the country view.
    SELECT 'country' AS rollup_kind,
           COALESCE(l.attr_country, '__unresolved__') AS rollup_key,
           l.*
    FROM ladder l

    UNION ALL

    SELECT 'plan_line' AS rollup_kind,
           am.plan_id || '|' || am.line_key AS rollup_key,
           l.*
    FROM ladder l
    JOIN active_mappings am
        ON  am.project_id   = l.project_id
        AND am.connector    = l.connector
        AND am.campaign_ref = l.breakdown_value

    UNION ALL

    SELECT 'global' AS rollup_kind, 'all' AS rollup_key, l.*
    FROM ladder l
),

agg AS (
    SELECT
        m.project_id                            AS project_id,
        m.date                                  AS date,
        m.rollup_kind                           AS rollup_kind,
        m.rollup_key                            AS rollup_key,
        m.currency                              AS currency,

        SUM(m.net_media_micros)                 AS net_media_micros,

        CASE WHEN COUNT(*) = COUNT(m.platform_fee_micros)
             THEN SUM(m.platform_fee_micros)   ELSE NULL END AS platform_fee_micros,
        CASE WHEN COUNT(*) = COUNT(m.regulatory_tax_micros)
             THEN SUM(m.regulatory_tax_micros) ELSE NULL END AS regulatory_tax_micros,
        CASE WHEN COUNT(*) = COUNT(m.wht_gross_up_micros)
             THEN SUM(m.wht_gross_up_micros)   ELSE NULL END AS wht_gross_up_micros,
        CASE WHEN COUNT(*) = COUNT(m.agency_fee_micros)
             THEN SUM(m.agency_fee_micros)     ELSE NULL END AS agency_fee_micros,
        CASE WHEN COUNT(*) = COUNT(m.sales_tax_micros)
             THEN SUM(m.sales_tax_micros)      ELSE NULL END AS sales_tax_micros,

        MIN(CASE WHEN m.is_ladder_complete THEN 1 ELSE 0 END) AS all_complete,
        COUNT(*)                                              AS total_row_count,
        SUM(CASE WHEN m.is_ladder_complete THEN 1 ELSE 0 END) AS complete_row_count,

        SUM(m.subtotal_ht_micros)               AS subtotal_ht_sum_micros,
        SUM(m.total_ttc_micros)                 AS total_ttc_sum_micros,
        SUM(CASE WHEN m.is_ladder_complete THEN m.total_ttc_micros ELSE 0 END)
                                                AS total_ttc_micros_complete_only,

        MAX(CASE WHEN m.gap_condition_key_unknown      THEN 1 ELSE 0 END) AS g_key_unknown,
        MAX(CASE WHEN m.gap_country_unresolved         THEN 1 ELSE 0 END) AS g_country,
        MAX(CASE WHEN m.gap_currency_mismatch          THEN 1 ELSE 0 END) AS g_currency,
        MAX(CASE WHEN m.gap_datastream_scope_ambiguous THEN 1 ELSE 0 END) AS g_ds_ambiguous,
        MAX(CASE WHEN m.gap_market_unresolved          THEN 1 ELSE 0 END) AS g_market,
        MAX(CASE WHEN m.gap_placement_type_unresolved  THEN 1 ELSE 0 END) AS g_placement,
        MAX(CASE WHEN m.gap_rule_scope_unresolved      THEN 1 ELSE 0 END) AS g_scope,
        MAX(CASE WHEN m.gap_source_type_unresolved     THEN 1 ELSE 0 END) AS g_source_type,
        MAX(CASE WHEN m.gap_tax_code_unresolved        THEN 1 ELSE 0 END) AS g_tax_code,
        MAX(CASE WHEN m.gap_tiers_malformed            THEN 1 ELSE 0 END) AS g_tiers,
        MAX(CASE WHEN m.gap_wht_rate_invalid           THEN 1 ELSE 0 END) AS g_wht,
        MAX(CASE WHEN m.gap_coverage_not_applicable    THEN 1 ELSE 0 END) AS g_coverage,
        MAX(CASE WHEN m.gap_rule_form_unroutable       THEN 1 ELSE 0 END) AS g_form,
        MAX(CASE WHEN m.gap_wht_base_overflow          THEN 1 ELSE 0 END) AS g_wht_overflow
    FROM members m
    GROUP BY m.project_id, m.date, m.rollup_kind, m.rollup_key, m.currency
),

coded AS (
    -- Same alphabetical CASE chain as fee_tax_ladder_daily, built ONCE in its own CTE
    -- (it used to be written out twice -- in the emptiness test and again inside
    -- SUBSTR -- which is two places to forget a new gap code).
    SELECT
        a.*,
        CASE WHEN a.g_key_unknown   = 1 THEN '|CONDITION_KEY_UNKNOWN'      ELSE '' END
     || CASE WHEN a.g_country       = 1 THEN '|COUNTRY_UNRESOLVED'         ELSE '' END
     || CASE WHEN a.g_coverage      = 1 THEN '|COVERAGE_NOT_APPLICABLE'    ELSE '' END
     || CASE WHEN a.g_currency      = 1 THEN '|CURRENCY_MISMATCH'          ELSE '' END
     || CASE WHEN a.g_ds_ambiguous  = 1 THEN '|DATASTREAM_SCOPE_AMBIGUOUS' ELSE '' END
     || CASE WHEN a.g_market        = 1 THEN '|MARKET_UNRESOLVED'          ELSE '' END
     || CASE WHEN a.g_placement     = 1 THEN '|PLACEMENT_TYPE_UNRESOLVED'  ELSE '' END
     || CASE WHEN a.g_form          = 1 THEN '|RULE_FORM_UNROUTABLE'       ELSE '' END
     || CASE WHEN a.g_scope         = 1 THEN '|RULE_SCOPE_UNRESOLVED'      ELSE '' END
     || CASE WHEN a.g_source_type   = 1 THEN '|SOURCE_TYPE_UNRESOLVED'     ELSE '' END
     || CASE WHEN a.g_tax_code      = 1 THEN '|TAX_CODE_UNRESOLVED'        ELSE '' END
     || CASE WHEN a.g_tiers         = 1 THEN '|TIERS_MALFORMED'            ELSE '' END
     || CASE WHEN a.g_wht_overflow  = 1 THEN '|WHT_BASE_OVERFLOW'          ELSE '' END
     || CASE WHEN a.g_wht           = 1 THEN '|WHT_RATE_INVALID'           ELSE '' END
            AS gap_codes_prefixed
    FROM agg a
)

SELECT
    a.project_id,
    a.date,
    a.rollup_kind,
    a.rollup_key,
    a.currency,
    a.net_media_micros,
    a.platform_fee_micros,
    a.regulatory_tax_micros,
    a.wht_gross_up_micros,
    a.agency_fee_micros,
    a.sales_tax_micros,
    CASE WHEN a.all_complete = 1 THEN a.subtotal_ht_sum_micros ELSE NULL END AS subtotal_ht_micros,
    CASE WHEN a.all_complete = 1 THEN a.total_ttc_sum_micros   ELSE NULL END AS total_ttc_micros,
    a.total_ttc_micros_complete_only,
    a.total_row_count,
    a.complete_row_count,
    a.all_complete = 1                                                        AS is_ladder_complete,
    CASE WHEN a.gap_codes_prefixed = '' THEN ''
         ELSE SUBSTR(a.gap_codes_prefixed, 2) END                             AS gap_codes
FROM coded a
{%- endif -%}
