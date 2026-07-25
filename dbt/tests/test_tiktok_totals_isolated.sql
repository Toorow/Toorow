-- test_tiktok_totals_isolated.sql — Story 15.2 (reconciliation: existing totals unchanged).
--
-- The TikTok mart block is ADDITIVE-ONLY and connector-keyed: it must emit rows ONLY under
-- connector='tiktok-ads', with breakdown_dimension in {campaign_id, adgroup_id, ad_id} and
-- metrics in {cost, impressions, clicks, conversions}. This proves the "totaux GA4/Meta/GSC/
-- Shopify existants inchangés" reconciliation at the mart grain: TikTok adds a brand-new
-- connector partition and touches no existing connector's rows (fact_daily_kpi keys on
-- connector, so a tiktok-ads row can never collide with a GA4/Meta/GSC/Shopify row).
--
-- It also guards the intended shape:
--   * every tiktok row carries a hierarchy breakdown_dimension (campaign/adgroup/ad) --
--     NO 'day_total', NO geo/device partition leaked in;
--   * tiktok metrics are exactly {cost, impressions, clicks, conversions} -- spend was
--     renamed to the canonical 'cost' and no stray source name (spend) reached the mart;
--   * breakdown_value is never NULL (the NULL-id campaign-grain rows are filtered by the
--     mart's WHERE ... IS NOT NULL, so no empty partition is produced).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'tiktok-ads'
  AND (
        breakdown_dimension NOT IN ('campaign_id', 'adgroup_id', 'ad_id')
     OR metric NOT IN ('cost', 'impressions', 'clicks', 'conversions')
     OR breakdown_value IS NULL
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
