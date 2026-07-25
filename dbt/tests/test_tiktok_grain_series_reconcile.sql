-- test_tiktok_grain_series_reconcile.sql — Story 15.2 (double-count reconciliation, AD-4).
--
-- INTER-SERIES identity (review-15-2 F-1): each TikTok breakdown grain (campaign_id /
-- adgroup_id / ad_id) is ONE parallel series, each built ONLY from its own data_level. When
-- the three grains COEXIST in raw, the series must still reconcile: SUM over 'campaign_id'
-- == SUM over 'adgroup_id' == SUM over 'ad_id' for a given (project_id, date, metric). This
-- is an INTER-SERIES comparison (campaign series total vs adgroup series total), NOT an
-- intra-series sum -- the data_level filter guarantees no grain bleeds into another before
-- we even compare. No TikTok partition is top-N bounded, so this is a FULL reconciliation.
-- It proves marts can pick ANY single grain via MIN(breakdown_dimension) without over- or
-- under-counting.
--
-- COEXISTENCE COVERAGE (F-1): the multi-grain seed (generate_multigrain_rows) lands all
-- three grains for the SAME campaigns/days -- exactly the case that double-counted the
-- campaign_id series before the data_level filter existed. This test would return rows (FAIL)
-- if the mart summed grains together (old schema: campaign series inflated by the campaign_id
-- carried on every adgroup/ad row). The campaign-only seed still passes trivially (single
-- grain per group). test_tiktok_no_grain_bleed.sql adds a direct upper-bound assertion.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.001 relative.

WITH per_grain AS (
    SELECT project_id, date, metric, breakdown_dimension, SUM(value) AS grain_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'tiktok-ads'
    GROUP BY project_id, date, metric, breakdown_dimension
),

canonical AS (
    -- The canonical grain total per (project, date, metric) = the MIN-dim series total.
    SELECT p.project_id, p.date, p.metric, p.grain_total AS canonical_total
    FROM per_grain p
    JOIN (
        SELECT project_id, date, metric, MIN(breakdown_dimension) AS dim
        FROM per_grain
        GROUP BY project_id, date, metric
    ) m
      ON m.project_id = p.project_id AND m.date = p.date
     AND m.metric = p.metric AND m.dim = p.breakdown_dimension
)

SELECT
    pg.project_id,
    pg.date,
    pg.metric,
    pg.breakdown_dimension,
    pg.grain_total,
    c.canonical_total
FROM per_grain pg
JOIN canonical c
    ON c.project_id = pg.project_id AND c.date = pg.date AND c.metric = pg.metric
WHERE ABS(pg.grain_total - c.canonical_total) > 0.001 * NULLIF(ABS(c.canonical_total), 0)
