-- test_tiktok_min_breakdown_stable.sql — Story 15.2 double-count guard (AD-4, leçon Epic 10).
--
-- The canonical single-dimension partition per (project_id, date, metric) for tiktok-ads is
-- picked by MIN(breakdown_dimension). It MUST always resolve to one of the TikTok hierarchy
-- dims 'ad_id' / 'adgroup_id' / 'campaign_id' (lexical order: 'ad_id' < 'adgroup_id' <
-- 'campaign_id'). It must NEVER be a foreign dimension (no 'country', 'day_total', etc. can
-- leak under connector='tiktok-ads'), which would signal a cross-connector partition bleed
-- and a potential double count when a mart sums the canonical series.
--
-- This is the tiktok analogue of test_ga4_acquisition_min_breakdown_stable.sql: it PROVES on
-- the built mart (not by analysis) that adding the tiktok block did not perturb the canonical
-- selection with an unexpected breakdown_dimension.
--
-- NB (documented, tested here indirectly): when only the campaign grain is pulled (the seed
-- case), MIN = 'campaign_id'. When the adgroup/ad grains are ALSO pulled, MIN slides to the
-- finer dim -- that is INTENTIONAL and correct: each grain series independently totals the
-- day, and the canonical selector picks exactly ONE, never summing two grains together (that
-- full-coverage identity is asserted by test_tiktok_grain_series_reconcile.sql).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). We return any
-- (project_id, date, metric) whose tiktok-ads MIN(breakdown_dimension) is NOT a hierarchy dim.

SELECT
    project_id,
    date,
    metric,
    MIN(breakdown_dimension) AS min_dim
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'tiktok-ads'
GROUP BY project_id, date, metric
HAVING MIN(breakdown_dimension) NOT IN ('ad_id', 'adgroup_id', 'campaign_id')
