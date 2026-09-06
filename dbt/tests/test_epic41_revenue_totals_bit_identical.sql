-- Story 41.5 AC8 -- THE ADDITIVE CLAIM, NUMERICALLY.
--
-- The structural half is server/tests/conformance/test_epic41_revenue_additive_only.py
-- (no pre-existing node DEPENDS on a 41.5 node). This is the half a graph cannot prove:
-- that the story's own fixture keys never reach a pre-existing mart.
--
-- PART A -- the seed namespace (__epic41_revenue__ / __epic41_shop_a__ / _b / _sdk_a)
-- must appear in NO pre-existing mart. fact_daily_kpi UNIONs module STAGING and never
-- seeds, so this is true by construction -- and asserted anyway, because "by
-- construction" is only worth saying when a machine checks it.
--
-- PART B -- the dev fixture projects DO land real facts on purpose (they must, or every
-- ON/OFF assertion is vacuous), so they are NOT asserted absent from fact_daily_kpi.
-- What IS asserted is that they never reach the pre-existing SEMANTIC marts that Epic 41
-- must not perturb: cross_source_revenue and semantic_roas are computed from
-- fact_daily_kpi with no knowledge of Epic 41, so a 41.5 row appearing there would mean
-- the overlay had leaked into the incumbent read path.
-- Note the honest limit of PART B: those marts legitimately DO see the dev projects'
-- shopify / stripe rows, exactly as they would see any customer's. The assertion is
-- therefore about the SEED namespace, which nothing outside this story may ever carry.

WITH seed_keys AS (
    SELECT '__epic41_revenue__' AS key_value
    UNION ALL SELECT '__epic41_shop_a__'
    UNION ALL SELECT '__epic41_shop_b__'
    UNION ALL SELECT '__epic41_sdk_a__'
),

fact_leak AS (
    SELECT 'SEED_KEY_REACHED_fact_daily_kpi' AS failure,
           f.project_id AS project_id, f.connector AS detail
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN seed_keys k ON k.key_value = f.project_id OR k.key_value = f.connector
),

cross_source_leak AS (
    SELECT 'SEED_KEY_REACHED_cross_source_revenue' AS failure,
           c.project_id AS project_id, 'cross_source_revenue' AS detail
    FROM {{ ref('cross_source_revenue') }} c
    JOIN seed_keys k ON k.key_value = c.project_id
),

semantic_roas_leak AS (
    SELECT 'SEED_KEY_REACHED_semantic_roas' AS failure,
           s.project_id AS project_id, 'semantic_roas' AS detail
    FROM {{ ref('semantic_roas') }} s
    JOIN seed_keys k ON k.key_value = s.project_id
),

ladder_leak AS (
    SELECT 'SEED_KEY_REACHED_fee_tax_ladder_daily' AS failure,
           l.project_id AS project_id, 'fee_tax_ladder_daily' AS detail
    FROM {{ ref('fee_tax_ladder_daily') }} l
    JOIN seed_keys k ON k.key_value = l.project_id OR k.key_value = l.connector
),

rollup_leak AS (
    SELECT 'SEED_KEY_REACHED_fee_tax_ladder_rollup' AS failure,
           r.project_id AS project_id, 'fee_tax_ladder_rollup' AS detail
    FROM {{ ref('fee_tax_ladder_rollup') }} r
    JOIN seed_keys k ON k.key_value = r.project_id
),

-- The fixture must actually be LOADED, or every assertion above is about an empty set.
fixture_present AS (
    SELECT 'REVENUE_FIXTURE_SEED_IS_EMPTY' AS failure, 'n/a' AS project_id,
           'epic41_revenue_fixture has no rows, so the isolation proof is vacuous'
               AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM {{ ref('epic41_revenue_fixture') }})
)

SELECT * FROM fact_leak
UNION ALL SELECT * FROM cross_source_leak
UNION ALL SELECT * FROM semantic_roas_leak
UNION ALL SELECT * FROM ladder_leak
UNION ALL SELECT * FROM rollup_leak
UNION ALL SELECT * FROM fixture_present
