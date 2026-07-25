-- test_google_sheets_totals_isolated.sql -- Story 15.6 (Epic 15).
--
-- Prouve que le bloc google-sheets dans fact_daily_kpi respecte la forme attendue ET
-- que les metriques Google Sheets n'entrent pas en collision avec les totaux
-- cross-source existants (totaux GA4/Meta/TikTok/GSC/Shopify/LinkedIn/Klaviyo/Stripe/HubSpot
-- INCHANGES apres ajout du module Google Sheets).
--
-- Controles :
--   * connector='google-sheets' uniquement ;
--   * breakdown_dimension = 'sheet_row_id' (les objectifs sont au grain sheet_row_id) ;
--   * metric IN ('budget_declared', 'target_revenue', 'target_conversions') ;
--   * breakdown_value IS NOT NULL (garde review-15-3 F-3 : sheet_row_id NULL exclu) ;
--   * value IS NOT NULL (contrat fact_daily_kpi.value NOT NULL, HAVING au mart).
--
-- REGLE D'ISOLATION OBJECTIFS (AD-4, CRITIQUE, Story 15.6) :
--   Les metriques Google Sheets sont des OBJECTIFS declares (plan), pas des realisations.
--   Les noms 'conversions' et 'revenue' NE DOIVENT JAMAIS apparaitre sous
--   connector='google-sheets' (collision avec cross_source_conversions / cross_source_revenue).
--   Les noms canoniques (budget_declared, target_revenue, target_conversions) sont DISTINCTS
--   des metriques de realisation -- c'est la protection nominale AD-4 primaire.
--
-- Pattern : miroir de test_hubspot_totals_isolated.sql (Story 15.5).
-- Un test dbt singulier ECHOUE si retourne des lignes (zero lignes = SUCCES).

SELECT connector, metric, breakdown_dimension, breakdown_value, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'google-sheets'
  AND (
        -- breakdown_dimension doit etre 'sheet_row_id'
        breakdown_dimension <> 'sheet_row_id'
     OR metric NOT IN (
            'budget_declared',
            'target_revenue',
            'target_conversions'
        )
     OR breakdown_value IS NULL
     OR value IS NULL
      )
GROUP BY connector, metric, breakdown_dimension, breakdown_value
