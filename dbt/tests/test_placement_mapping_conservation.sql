-- « Conservation of spend across the ventilation » -- the Test row of
-- `docs/product-architecture/capabilities/placement-mapping.md`, asserted on the
-- aggregation that carries the two added columns.
--
-- A dbt singular test FAILS when it returns rows; zero rows is a pass.
--
-- TWO INVARIANTS, and neither is a tautology of the model's own SQL:
--
--   1. NO CENT IS CREATED. For one (project, plan, connector, campaign, day),
--      the rows this model emits -- the matched shares and, when nothing
--      matched, the whole -- must never sum to MORE than the campaign's observed
--      spend that day. This is the discriminant a naive line<->fact join without
--      split weights FAILS: a campaign shared 0.5/0.5 between two lines would
--      come out at 100/100 instead of 50/50.
--   2. NO CENT IS LOST ON THE UNMATCHED SIDE, which is the side criterion [3] is
--      about. A campaign-day no active plan line matched must state its spend
--      ENTIRE on its null-plan-line row. A partial figure there would understate
--      the spend nobody budgeted, which is the one number these rows exist to
--      reveal.
--
-- WITHHELD DAYS ARE EXCLUDED FROM BOTH SIDES, and that is the repair story 61.4
-- made to `test_plan_vs_actual_ventilation_sum.sql` rather than a tolerance:
-- `SUM()` skips NULLs, so a campaign-day whose rate could not be resolved would
-- otherwise count as zero cents on the expected side -- the exact substitution
-- (a gap read as a zero) the money rules of this capability exist to refuse.

{%- set mirror_missing = toorow_absent_sources('mirror', ['media_plans', 'plan_line_mappings']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH observed AS (
    -- The origin, read from the fact and untouched -- the same read the model
    -- makes, but summed WITHOUT any weight, which is what makes the comparison
    -- meaningful rather than circular.
    SELECT
        f.project_id                           AS project_id,
        f.connector                            AS connector,
        f.breakdown_value                      AS campaign_ref,
        CAST(f.date AS DATE)                   AS day,
        CASE WHEN COUNT(*) <> COUNT(f.value) THEN NULL
             ELSE SUM({{ fee_tax_to_micros('f.value') }}) END AS observed_micros
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.metric = 'cost'
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.project_id, f.connector, f.breakdown_value, f.date
),

emitted AS (
    SELECT
        project_id,
        plan_id,
        connector,
        campaign_ref,
        day,
        SUM(spend_micros)                      AS emitted_micros,
        -- A campaign-day nothing matched emits ONE row, and its plan line is
        -- null. `MAX` over a boolean-as-integer: 1 as soon as any row of the
        -- group carries a plan line.
        MAX(CASE WHEN plan_line_key IS NULL THEN 0 ELSE 1 END) AS any_line_matched
    FROM {{ ref('placement_mapped_spend_daily') }}
    -- A withheld amount is NULL, and a group with any NULL member cannot be
    -- compared: excluded here and, symmetrically, by the NOT NULL on the origin.
    WHERE spend_micros IS NOT NULL
    GROUP BY project_id, plan_id, connector, campaign_ref, day
)

SELECT
    CASE WHEN e.emitted_micros > o.observed_micros THEN 'cent_created'
         ELSE 'unmatched_spend_understated' END AS defect,
    e.project_id,
    e.plan_id,
    e.connector,
    e.campaign_ref,
    e.day,
    e.emitted_micros,
    o.observed_micros
FROM emitted e
JOIN observed o
    ON o.project_id   = e.project_id
   AND o.connector    = e.connector
   AND o.campaign_ref = e.campaign_ref
   AND o.day          = e.day
WHERE o.observed_micros IS NOT NULL
  AND (
        -- 1. never more than the origin, matched or not.
        e.emitted_micros > o.observed_micros
        -- 2. and exactly the origin when nothing matched.
     OR (e.any_line_matched = 0 AND e.emitted_micros <> o.observed_micros)
      )
{%- endif -%}
