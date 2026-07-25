-- test_klaviyo_attributed_not_in_cross_source.sql -- Story 15.8 (Epic 15).
--
-- Prouve la regle de non-agregation (AD-4) : attributed_revenue et
-- attributed_conversions Klaviyo n'apparaissent dans AUCUN total cross-source
-- existant (GA4 conversions, Meta conversions, Shopify revenue).
--
-- Guards :
--   1. Les metriques 'attributed_conversions' et 'attributed_revenue' n'existent
--      dans fact_daily_kpi QUE pour connector='klaviyo'.
--   2. Le total GA4 'conversions' est inchange par l'ajout de Klaviyo :
--      SUM(value WHERE connector='google-analytics' AND metric='conversions') ne
--      contient aucune valeur Klaviyo.
--   3. Le total Shopify 'revenue' est inchange :
--      SUM(value WHERE connector='shopify' AND metric='revenue') ne contient
--      aucune valeur Klaviyo.
--
-- Un test dbt singulier ECHOUE si retourne des lignes (zero lignes = SUCCES).
-- Sources : AD-4, Epic 15 Story 15.8 AC, declaration de non-agregation manifest.

-- Guard 1 : metriques attributed_* uniquement pour connector='klaviyo'.
SELECT
    'attributed_metric_outside_klaviyo' AS check_type,
    connector,
    metric,
    COUNT(*) AS violation_count
FROM {{ ref('fact_daily_kpi') }}
WHERE metric IN ('attributed_conversions', 'attributed_revenue')
  AND connector != 'klaviyo'
GROUP BY connector, metric

UNION ALL

-- Guard 2 : les 'conversions' GA4 ne changent pas (hors Klaviyo).
-- Si un bug ajoutait des lignes Klaviyo avec metric='conversions', ce guard le detecte.
SELECT
    'conversions_metric_in_klaviyo' AS check_type,
    connector,
    metric,
    COUNT(*) AS violation_count
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'klaviyo'
  AND metric = 'conversions'
GROUP BY connector, metric

UNION ALL

-- Guard 3 : les 'revenue' Shopify ne changent pas (hors Klaviyo).
SELECT
    'revenue_metric_in_klaviyo' AS check_type,
    connector,
    metric,
    COUNT(*) AS violation_count
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'klaviyo'
  AND metric = 'revenue'
GROUP BY connector, metric
