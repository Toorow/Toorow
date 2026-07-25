-- test_klaviyo_totals_isolated.sql -- Story 15.8 (Epic 15).
--
-- Prouve que le bloc klaviyo dans fact_daily_kpi respecte la forme attendue :
--   * connector='klaviyo' uniquement ;
--   * breakdown_dimension IN ('campaign_id', 'flow_id') ;
--   * metric IN ('sends', 'opens', 'clicks', 'attributed_conversions', 'attributed_revenue') ;
--   * breakdown_value IS NOT NULL (les lignes avec id NULL sont filtrees par le mart).
--
-- Regle de non-agregation (AD-4, CRITIQUE) : les metriques 'conversions' et 'revenue'
-- generiques ne doivent JAMAIS apparaitre sous connector='klaviyo'. Seuls les noms
-- canoniques 'attributed_conversions' et 'attributed_revenue' sont autorises.
-- Un nom generique indiquerait une collision potentielle avec les regies
-- (connector='meta-ads', 'tiktok-ads', 'gsc') et le revenue Shopify.
--
-- Pattern : miroir de test_tiktok_totals_isolated.sql (Story 15.2).
-- Un test dbt singulier ECHOUE si retourne des lignes (zero lignes = SUCCES).

SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'klaviyo'
  AND (
        breakdown_dimension NOT IN ('campaign_id', 'flow_id')
     OR metric NOT IN (
            'sends', 'opens', 'clicks',
            'attributed_conversions', 'attributed_revenue'
        )
     OR breakdown_value IS NULL
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
