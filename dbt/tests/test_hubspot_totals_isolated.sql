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

-- CORRECTION 2026-08-04 : la clause exigeait `breakdown_dimension = 'date'` pour
-- TOUTES les lignes hubspot, alors que le bloc story 15.5 de fact_daily_kpi.sql
-- emet `deal_amount` en `breakdown_dimension = 'currency'` -- il est monetaire et
-- porte sa devise. L'assertion et le modele ont diverge des l'ecriture, et le test
-- n'a jamais tourne parce que la fixture locale ne construisait plus (AI-164).
-- Recalee SUR le modele, pas relachee : la regle est desormais nommee par metrique,
-- donc une quatrieme dimension, ou `deal_amount` qui perdrait sa devise, casse
-- toujours. (Le jumeau Python de cette assertion, dans
-- test_seed_to_mart_loop.py, portait exactement le meme defaut.)
SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'hubspot'
  AND (
        metric NOT IN (
            'new_contacts',
            'deals_created',
            'deals_closed',
            'deal_amount'
        )
     OR breakdown_value IS NULL
        -- deal_amount est monetaire : grain 'currency'.
     OR (metric = 'deal_amount' AND breakdown_dimension <> 'currency')
        -- les trois comptages CRM sont au grain journalier total.
     OR (metric <> 'deal_amount' AND breakdown_dimension <> 'date')
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
