-- T2 (Story 41.3, AC9 / AC14 / E41-NFR01) -- the BYTE-IDENTITY half.
-- Modelled on test_epic39_totals_bit_identical.sql (namespaced isolation PART A +
-- drift-threshold-exactly-0 PART B).
--
-- PART A -- ISOLATION. No __epic41_* fixture key may appear in fact_daily_kpi,
-- cross_source_conversions, cross_source_revenue or plan_vs_actual_daily. The fixture
-- is a SEED and those marts UNION module STAGING, never seeds, so this must hold by
-- construction; asserting it is what keeps the construction honest if a later story
-- adds a seed-fed branch. NOTE the deliberate scope: the DEV MIRROR SEEDER's projects
-- are named feetax_dev_* precisely so they are NOT covered here -- they are mirror +
-- raw landings that are SUPPOSED to reach the marts, exactly like seed_plan_mirror.py's
-- rows, and they are additive new project keys that cannot move an existing project's
-- total.
--
-- PART B -- DRIFT EXACTLY 0, re-run in the SAME build that materialises the three
-- fee-tax views. Two identities that the Epic-41 overlay must not have touched:
--   B1: the meta-ads campaign_id cost series in fact_daily_kpi still equals its
--       staging sum at data_level='CAMPAIGN', per (project, date). meta-ads is the
--       connector the dev seeder lands rows into, so this is where a regression would
--       actually show.
--   B2: plan_vs_actual_daily's ventilation still re-sums to origin -- the sum of a
--       campaign's ventilated spend over the lines that claim it equals the campaign's
--       own daily spend.
-- Threshold is exactly 0, not an epsilon.
--
-- AMENDED story 61.4, and for the same reason as
-- test_plan_vs_actual_ventilation_sum: B2's `mapped_origin` sums `f.value` with
-- SUM(), which skips a NULL, so a campaign-day with no resolvable exchange rate
-- counted as ZERO on the origin side. The mart now withholds such a day rather
-- than understating it -- an absent rate is not a zero spend -- so those (plan,
-- day) pairs are excluded from B2 and asserted in full by
-- test_plan_pacing_currency_is_declared.sql. B1 needs no change: both its sides
-- skip the same NULLs, so its identity is unaffected.
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
WITH fixture_keys AS (
    SELECT DISTINCT project_id, connector FROM {{ ref('epic41_cascade_fixture') }}
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: epic41_cascade_fixture is empty -- dbt seed not run, so the'
        || ' isolation assertion is vacuous' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM fixture_keys) = 0
),

leak_fact AS (
    SELECT
        f.project_id || '|' || f.connector AS subject,
        'ISOLATION_FAIL: an __epic41_* fixture key reached fact_daily_kpi' AS failure_reason
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN fixture_keys k
        ON k.project_id = f.project_id OR k.connector = f.connector
),

leak_conversions AS (
    SELECT
        c.project_id AS subject,
        'ISOLATION_FAIL: an __epic41_* fixture key reached cross_source_conversions' AS failure_reason
    FROM {{ ref('cross_source_conversions') }} c
    JOIN fixture_keys k ON k.project_id = c.project_id
),

leak_revenue AS (
    SELECT
        c.project_id AS subject,
        'ISOLATION_FAIL: an __epic41_* fixture key reached cross_source_revenue' AS failure_reason
    FROM {{ ref('cross_source_revenue') }} c
    JOIN fixture_keys k ON k.project_id = c.project_id
),

leak_plan AS (
    SELECT
        p.project_id AS subject,
        'ISOLATION_FAIL: an __epic41_* fixture key reached plan_vs_actual_daily' AS failure_reason
    FROM {{ ref('plan_vs_actual_daily') }} p
    JOIN fixture_keys k ON k.project_id = p.project_id
),

-- ---------------------------------------------------------------- PART B1 ----
staging_cost AS (
    SELECT
        s.project_id                                AS project_id,
        CAST(s.date AS DATE)                        AS date,
        SUM({{ fx_convert_at_read('cost') }})       AS staging_cost
    FROM {{ ref('stg_meta_ads_daily') }} s
    WHERE s.data_level = 'CAMPAIGN'
      AND s.campaign_id IS NOT NULL
    GROUP BY s.project_id, CAST(s.date AS DATE)
),

fact_cost AS (
    SELECT
        f.project_id            AS project_id,
        CAST(f.date AS DATE)    AS date,
        SUM(f.value)            AS fact_cost
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.connector = 'meta-ads'
      AND f.metric = 'cost'
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.project_id, CAST(f.date AS DATE)
),

fact_drift AS (
    SELECT
        s.project_id || '|' || CAST(s.date AS STRING) AS subject,
        'DRIFT_FAIL: meta-ads campaign_id cost in fact_daily_kpi no longer equals its'
        || ' staging sum -- the Epic-41 overlay must never move a pre-existing total' AS failure_reason
    FROM staging_cost s
    JOIN fact_cost f
        ON  f.project_id = s.project_id
        AND f.date       = s.date
    WHERE ABS(f.fact_cost - s.staging_cost) > 0
),

-- ---------------------------------------------------------------- PART B2 ----
campaign_origin AS (
    SELECT
        f.project_id            AS project_id,
        f.connector             AS connector,
        f.breakdown_value       AS campaign_ref,
        CAST(f.date AS DATE)    AS day,
        SUM(f.value)            AS origin_spend
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.metric = 'cost'
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.project_id, f.connector, f.breakdown_value, CAST(f.date AS DATE)
),

-- Story 61.4: the (plan, day) pairs on which the mart stated no actual because it
-- could not convert one, as opposed to because nothing was spent.
withheld_days AS (
    SELECT DISTINCT project_id, plan_id, day
    FROM {{ ref('plan_vs_actual_daily') }}
    WHERE actual_withheld
),

ventilated_total AS (
    SELECT
        p.project_id            AS project_id,
        p.plan_id               AS plan_id,
        p.day                   AS day,
        SUM(p.actual_amount)    AS ventilated
    FROM {{ ref('plan_vs_actual_daily') }} p
    WHERE p.actual_amount IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM withheld_days w
          WHERE w.project_id = p.project_id AND w.plan_id = p.plan_id AND w.day = p.day
      )
    GROUP BY p.project_id, p.plan_id, p.day
),

mapped_origin AS (
    SELECT
        m.plan_id               AS plan_id,
        co.project_id           AS project_id,
        co.day                  AS day,
        SUM(co.origin_spend)    AS origin
    FROM campaign_origin co
    JOIN (
        SELECT DISTINCT plan_id, connector, campaign_ref
        FROM {{ source('mirror', 'plan_line_mappings') }}
        WHERE status = 'active'
    ) m
        ON  m.connector    = co.connector
        AND m.campaign_ref = co.campaign_ref
    GROUP BY m.plan_id, co.project_id, co.day
),

ventilation_drift AS (
    SELECT
        v.project_id || '|' || v.plan_id || '|' || CAST(v.day AS STRING) AS subject,
        'DRIFT_FAIL: plan_vs_actual_daily ventilation no longer re-sums to origin'
            AS failure_reason
    FROM ventilated_total v
    JOIN mapped_origin o
        ON  o.plan_id    = v.plan_id
        AND o.project_id = v.project_id
        AND o.day        = v.day
    WHERE ABS(v.ventilated - o.origin) > 0
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM leak_fact
UNION ALL SELECT subject, failure_reason FROM leak_conversions
UNION ALL SELECT subject, failure_reason FROM leak_revenue
UNION ALL SELECT subject, failure_reason FROM leak_plan
UNION ALL SELECT subject, failure_reason FROM fact_drift
UNION ALL SELECT subject, failure_reason FROM ventilation_drift
{%- endif -%}
