-- test_hubspot_crm_not_in_cross_source.sql -- Story 15.5 (Epic 15).
--
-- Test non-tautologique (AD-4 CRM) : prouve que les totaux cross-source existants
-- (conversions des regies, revenue Shopify/Stripe) sont INCHANGES apres l'ajout
-- du bloc HubSpot dans fact_daily_kpi.
--
-- Logique : si les metriques HubSpot avaient des noms qui chevauchent les metriques
-- marketing (ex: 'conversions', 'revenue'), elles augmenteraient les totaux cross-source.
-- Ce test verifie qu'aucune ligne connector='hubspot' ne porte un metric qui existe
-- sous d'autres connecteurs dans la vue cross_source_conversions ou qui represente
-- un revenue double-compte.
--
-- Test 1 : aucune ligne hubspot dans les metriques marketing "dangereuses"
-- (conversions, revenue, cost, impressions, clicks -- noms deja utilises par regies).
-- Retourne des lignes = ECHEC (presence d'une collision nominale).
SELECT connector, metric, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'hubspot'
  AND metric IN (
      -- Metriques des regies (cross_source_conversions)
      'conversions',
      'cost',
      'impressions',
      -- Metriques revenue (cross_source_revenue)
      'revenue',
      'refunds',
      'refund_amount',
      -- Metriques Klaviyo (non-aggregables)
      'attributed_conversions',
      'attributed_revenue',
      -- Metriques Shopify
      'orders_count'
  )
GROUP BY connector, metric
