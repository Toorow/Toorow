-- Story 22.4 THE invariance / no-double-count test (règle d'or AD-4/AD-6, pattern
-- 12.4/17.2, discriminant type 17.3).
--
-- The plan is a READ: fact_daily_kpi is byte-identical with or without a plan (the
-- mart never writes to fact_daily_kpi -- a fact-only sum is trivially invariant, so
-- that alone would be tautological). The REAL, DISCRIMINANT proof -- the one a naive
-- JOIN without split weights would FAIL: the mart's OWN ventilated actual, summed
-- over a plan's lines for a day, must equal the ORIGINAL fact spend of the campaigns
-- that plan maps ACTIVE on that day (inside the claiming lines' flight windows) --
-- no cent created, no cent lost. A campaign shared 0.5/0.5 between two lines
-- contributes 50/50 (its origin spend once), NOT 100/100 (which a naive line<->fact
-- JOIN would double-count).
--
-- We compare PER PLAN (décision 5: a campaign shared across two concurrent plans is
-- ventilated independently in each; each plan re-sums to the origin, never summed
-- across plans).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Float tolerance
-- 1e-6 (ventilation is exact arithmetic; the tolerance only guards float rounding).
--
-- AMENDED story 61.4 -- THIS TEST CARRIED THE DEFECT IT WAS WATCHING FOR. `origin`
-- sums `f.value` with SUM(), which SKIPS a NULL, so a campaign-day that could not
-- be converted counted as ZERO on the EXPECTED side. The invariant then read "no
-- cent created, no cent lost, and a cent nobody could convert is zero cents" --
-- which is the very substitution (a gap read as a zero) that made a missing
-- exchange rate fire an under-delivery alert. Now that the mart WITHHOLDS such a
-- day instead of understating it, the two sides disagreed and this test was the
-- first to say so.
-- The repair is not a tolerance: the (plan, day) pairs the mart withheld are
-- EXCLUDED from both sides, and what happens on them is asserted in full by
-- test_plan_pacing_currency_is_declared.sql (a withheld line states no actual, no
-- pace, no consumed share, and its channel and plan rollups state none either).

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
{%- set mirror_missing = toorow_absent_sources('mirror', ['media_plans', 'plan_line_mappings']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH origin AS (
    -- Original per-campaign daily spend from fact_daily_kpi (the source of truth,
    -- untouched). campaign_ref = breakdown_value @ breakdown_dimension='campaign_id'.
    SELECT
        f.project_id,
        f.connector,
        f.breakdown_value AS campaign_ref,
        CAST(f.date AS DATE) AS day,
        SUM(CAST(f.value AS {{ toorow_float_type() }})) AS origin_spend
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.metric = 'cost'
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.project_id, f.connector, f.breakdown_value, f.date
),

-- The EXPECTED ventilated total per (plan, day): the origin spend of every campaign
-- the plan maps ACTIVE, counted ONCE per campaign, but ONLY on days that fall inside
-- at least one claiming line's flight window (the mart's own WHERE clause). This is
-- the honest denominator the mart must reproduce -- summing origin_spend per campaign
-- ONCE (not per line) is what forbids the 100/100 double-count.
mapped_campaign_days AS (
    SELECT DISTINCT
        p.id                                   AS plan_id,
        o.project_id,
        o.connector,
        o.campaign_ref,
        o.day,
        o.origin_spend
    FROM origin o
    JOIN {{ source('mirror', 'plan_line_mappings') }} m
        ON m.connector    = o.connector
       AND m.campaign_ref = o.campaign_ref
       AND m.status       = 'active'
    JOIN {{ source('mirror', 'media_plans') }} p
        ON p.id = m.plan_id AND p.project_id = o.project_id
    -- the campaign day must fall inside at least one claiming line's flight window
    -- (same bound as plan_vs_actual_daily.ventilated) -- else the mart ventilates 0.
    JOIN {{ source('mirror', 'media_plan_versions') }} v
        ON v.plan_id = p.id AND v.is_active
    JOIN {{ source('mirror', 'media_plan_lines') }} l
        ON l.version_id = v.id AND l.line_key = m.line_key
       AND o.day >= CAST(l.start_date AS DATE) AND o.day <= CAST(l.end_date AS DATE)
),

-- The (plan, project, day) triples on which the mart stated no actual BECAUSE it
-- could not -- not because there was no spend. Story 61.4: `actual_withheld` is the
-- mart's own word for it, so this test reads the decision rather than re-deriving it.
withheld AS (
    SELECT DISTINCT plan_id, project_id, day
    FROM {{ ref('plan_vs_actual_daily') }}
    WHERE actual_withheld
),

expected AS (
    SELECT m.plan_id, m.project_id, m.day, SUM(m.origin_spend) AS expected_spend
    FROM mapped_campaign_days m
    WHERE NOT EXISTS (
        SELECT 1 FROM withheld w
        WHERE w.plan_id = m.plan_id AND w.project_id = m.project_id AND w.day = m.day
    )
    GROUP BY m.plan_id, m.project_id, m.day
),

-- The mart's OWN ventilated total per (plan, day) -- summing every line's actual.
actual AS (
    SELECT
        p.plan_id,
        p.project_id,
        p.day,
        SUM(p.actual_amount) AS mart_spend
    FROM {{ ref('plan_vs_actual_daily') }} p
    WHERE p.actual_amount IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM withheld w
          WHERE w.plan_id = p.plan_id AND w.project_id = p.project_id AND w.day = p.day
      )
    GROUP BY p.plan_id, p.project_id, p.day
)

SELECT
    e.plan_id,
    e.project_id,
    e.day,
    e.expected_spend,
    a.mart_spend
FROM expected e
FULL OUTER JOIN actual a
    ON a.plan_id = e.plan_id AND a.project_id = e.project_id AND a.day = e.day
WHERE ABS(COALESCE(a.mart_spend, 0) - COALESCE(e.expected_spend, 0)) > 1e-6
{%- endif -%}
