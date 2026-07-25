-- test_hubspot_totals_isolated.sql -- Story 15.5 (Epic 15).
--
-- Prouve que le bloc hubspot dans fact_daily_kpi respecte la forme attendue ET
-- que les metriques HubSpot CRM ne contaminent pas les totaux cross-source existants.
--
-- Controles :
--   * connector='hubspot' uniquement ;
--   * breakdown_dimension = 'date' (les metriques CRM sont au grain journalier total,
--     pas par campagne/canal -- pas de partition geographique ni par entite CRM) ;
--   * metric IN ('new_contacts', 'deals_created', 'deals_closed', 'deal_amount') ;
--   * breakdown_value IS NOT NULL.
--
-- REGLE D'ISOLATION CRM (AD-4, CRITIQUE, Story 15.5) :
--   Les noms de metriques 'conversions' et 'revenue' NE DOIVENT JAMAIS apparaitre
--   sous connector='hubspot'. Un nom generique indiquerait une collision potentielle :
--   * 'conversions' -> collision avec cross_source_conversions (regies GA4/Meta/TikTok)
--   * 'revenue'     -> collision avec cross_source_revenue (Shopify 15.4 / Stripe 15.7)
--   Les noms canoniques HubSpot (new_contacts, deals_created, deals_closed, deal_amount)
--   sont DISTINCTS des metriques marketing -- c'est la protection nominale AD-4 primaire.
--
-- Pattern : miroir de test_stripe_totals_isolated.sql (Story 15.7) et
--           test_klaviyo_totals_isolated.sql (Story 15.8).
-- Un test dbt singulier ECHOUE si retourne des lignes (zero lignes = SUCCES).

SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'hubspot'
  AND (
        -- breakdown_dimension doit etre 'date' pour le bloc CRM day-total
        breakdown_dimension <> 'date'
     OR metric NOT IN (
            'new_contacts',
            'deals_created',
            'deals_closed',
            'deal_amount'
        )
     OR breakdown_value IS NULL
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
