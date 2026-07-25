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

WITH origin AS (
    -- Original per-campaign daily spend from fact_daily_kpi (the source of truth,
    -- untouched). campaign_ref = breakdown_value @ breakdown_dimension='campaign_id'.
    SELECT
        f.project_id,
        f.connector,
        f.breakdown_value AS campaign_ref,
        CAST(f.date AS DATE) AS day,
        SUM(CAST(f.value AS DOUBLE)) AS origin_spend
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

expected AS (
    SELECT plan_id, project_id, day, SUM(origin_spend) AS expected_spend
    FROM mapped_campaign_days
    GROUP BY plan_id, project_id, day
),

-- The mart's OWN ventilated total per (plan, day) -- summing every line's actual.
actual AS (
    SELECT
        plan_id,
        project_id,
        day,
        SUM(actual_amount) AS mart_spend
    FROM {{ ref('plan_vs_actual_daily') }}
    WHERE actual_amount IS NOT NULL
    GROUP BY plan_id, project_id, day
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
