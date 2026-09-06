-- T16 (Story 41.3, AC13 / decision D5 / decision C4) -- plan-version scope resolves for
-- real, and unmapped spend is KNOWN-FALSE, not a gap.
--
-- A plan_version-scoped rule resolves through
-- mirror.media_plan_versions -> mirror.plan_line_mappings (status='active') and applies
-- to the rows whose (connector, campaign_ref) is mapped to a line of that plan --
-- campaign_ref = fact_daily_kpi.breakdown_value WHERE breakdown_dimension='campaign_id',
-- the exact key plan_vs_actual_daily already uses.
--
--   (a) MAPPED   -> the rule fires with coverage weight 1.0 (the store enforces
--                   SUM(split_weight) = 1.0 per (plan, connector, campaign_ref); the cap
--                   in the model is a defensive guard, not a normal path);
--   (b) UNMAPPED -> component exactly 0, is_ladder_complete TRUE, gap_codes '' --
--                   D5 verbatim: the operator scoped the rule to a plan ON PURPOSE, so
--                   spend outside the plan legitimately is not covered. Turning that into
--                   a gap would null the totals of every project that scopes any rule to
--                   a plan;
--   (c) THE C4 REGRESSION GUARD -- feetax_dev_plan's connector emits all THREE meta-ads
--       cost series. Under the pre-C4 MIN(breakdown_dimension) pick the ladder ran at
--       'ad_id' grain, where the row carries no campaign key at all, so "not mapped" and
--       "cannot be evaluated" were indistinguishable and D5 could not be honoured
--       literally. C4 changed the canonical pick to prefer campaign_id, which removed the
--       mismatch at its root. There is no PLAN_SCOPE_GRAIN_MISMATCH state left to assert
--       -- so this asserts its ABSENCE, and that the rule fires.
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
WITH plan_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_plan'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 2 ladder rows for feetax_dev_plan (the mapped and the'
        || ' unmapped campaign), got ' || CAST((SELECT COUNT(*) FROM plan_rows) AS STRING)
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM plan_rows) <> 2
),

scope_state_guard AS (
    SELECT
        'ftr_dev_plan_platform' AS subject,
        'SCOPE_FAIL: the plan_version-scoped rule did not resolve to a plan_id -- check'
        || ' that mirror.media_plan_versions carries the seeded version' AS failure_reason
    FROM {{ ref('fee_tax_rules_effective') }} r
    WHERE r.rule_id = 'ftr_dev_plan_platform'
      AND (r.scope_state <> 'RESOLVED' OR r.scope_plan_id IS NULL)
),

mapped_break AS (
    SELECT
        p.project_id || '|' || p.breakdown_value AS subject,
        'PLAN_SCOPE_FAIL: the MAPPED campaign did not receive the plan-scoped fee --'
        || ' expected 2 % of 4 000 000 000 = 80 000 000, got '
        || COALESCE(CAST(p.platform_fee_micros AS STRING), 'NULL') AS failure_reason
    FROM plan_rows p
    WHERE p.breakdown_value = 'fdp_camp_mapped'
      AND NOT (p.platform_fee_micros = 80000000
               AND p.is_ladder_complete
               AND p.applied_rule_ids LIKE '%ftr_dev_plan_platform%')
),

unmapped_break AS (
    SELECT
        p.project_id || '|' || p.breakdown_value AS subject,
        'PLAN_SCOPE_FAIL: the UNMAPPED campaign must be KNOWN-FALSE -- component exactly'
        || ' 0, complete, gap_codes empty. Got platform='
        || COALESCE(CAST(p.platform_fee_micros AS STRING), 'NULL')
        || ' gap_codes=' || p.gap_codes AS failure_reason
    FROM plan_rows p
    WHERE p.breakdown_value = 'fdp_camp_unmapped'
      AND NOT (p.platform_fee_micros = 0
               AND p.is_ladder_complete
               AND p.gap_codes = ''
               AND p.applied_rule_ids = '')
),

c4_guard AS (
    -- The ladder must be at campaign_id grain on this three-series connector, and no
    -- grain-mismatch code may exist anywhere in the gap vocabulary any more.
    SELECT
        p.project_id || '|' || p.breakdown_dimension AS subject,
        'C4_REGRESSION_FAIL: the three-series connector did not collapse to campaign_id,'
        || ' so the plan mapping cannot resolve -- this is the pre-C4 behaviour'
            AS failure_reason
    FROM plan_rows p
    WHERE p.breakdown_dimension <> 'campaign_id'
    UNION ALL
    SELECT
        p.project_id || '|' || p.breakdown_value,
        'C4_REGRESSION_FAIL: a grain-mismatch gap code reappeared -- C4 deleted both'
        || ' PLAN_SCOPE_GRAIN_MISMATCH and PLAN_LINE_GRAIN_MISMATCH'
    FROM {{ ref('fee_tax_ladder_daily') }} p
    WHERE p.gap_codes LIKE '%GRAIN_MISMATCH%'
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM scope_state_guard
UNION ALL SELECT subject, failure_reason FROM mapped_break
UNION ALL SELECT subject, failure_reason FROM unmapped_break
UNION ALL SELECT subject, failure_reason FROM c4_guard
{%- endif -%}
