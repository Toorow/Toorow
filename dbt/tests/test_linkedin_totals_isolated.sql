-- test_linkedin_totals_isolated.sql -- Story 15.3 (reconciliation: totaux existants inchanges).
--
-- Le bloc LinkedIn du mart est ADDITIF-UNIQUEMENT et cle par connector : il ne doit emettre
-- des lignes QUE sous connector='linkedin-ads', avec breakdown_dimension dans
-- {campaign_id, campaign_group_id} et metrics dans {cost, impressions, clicks, conversions,
-- leads}. Cela prouve la reconciliation "totaux GA4/Meta/TikTok/GSC/Shopify/Klaviyo
-- existants INCHANGES" : LinkedIn ajoute une nouvelle partition connector et ne touche
-- jamais les lignes des connecteurs existants (fact_daily_kpi est cle par connector,
-- donc une ligne linkedin-ads ne peut jamais entrer en collision avec une ligne GA4/Meta).
--
-- Le test verifie aussi la forme attendue :
--   * chaque ligne linkedin porte un breakdown_dimension dans {campaign_id, campaign_group_id}
--     -- PAS de 'day_total', PAS de geo/device infere ;
--   * les metriques linkedin sont exactement {cost, impressions, clicks, conversions, leads} --
--     costInLocalCurrency renomme en 'cost' (pas de nom source qui aurait fuite) ;
--   * breakdown_value n'est jamais NULL (filtre WHERE ... IS NOT NULL dans le mart).
--
-- Un test singulier dbt ECHOUE quand il retourne des lignes (zero lignes = succes).

SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'linkedin-ads'
  AND (
        breakdown_dimension NOT IN ('campaign_id', 'campaign_group_id')
     OR metric NOT IN ('cost', 'impressions', 'clicks', 'conversions', 'leads')
     OR breakdown_value IS NULL
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
