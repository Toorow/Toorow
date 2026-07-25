-- Story 17.2 anti-double-count guard (HONESTY 2, review-1-6 lesson): a claimant
-- régie's claimed_conversions in dedup_estimate MUST equal the SUM of that régie's
-- 'conversions' on its ONE canonical breakdown dimension (declared in the
-- conversion_claimants seed) -- NEVER the sum across its parallel partitions
-- (meta-ads: campaign_id / adset_id / ad_id each total the day; summing all three
-- would triple-count).
--
-- We recompute the canonical-partition total straight from fact_daily_kpi using the
-- seed's declared dimension and assert equality per (project, date, channel).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.001.

WITH canonical AS (
    SELECT
        f.project_id,
        f.date,
        f.connector           AS channel_connector,
        SUM(f.value)          AS canonical_claimed
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN {{ ref('conversion_claimants') }} cl
        ON cl.connector = f.connector
       AND cl.canonical_breakdown_dimension = f.breakdown_dimension
    WHERE f.metric = 'conversions'
    GROUP BY f.project_id, f.date, f.connector
)

SELECT
    de.project_id,
    de.date,
    de.channel_connector,
    de.claimed_conversions,
    c.canonical_claimed
FROM {{ ref('dedup_estimate') }} de
JOIN canonical c
    ON  c.project_id        = de.project_id
    AND c.date              = de.date
    AND c.channel_connector = de.channel_connector
WHERE ABS(de.claimed_conversions - c.canonical_claimed) > 0.001
