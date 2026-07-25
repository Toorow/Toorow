-- test_google_sheets_objectives_not_in_cross_source.sql -- Story 15.6 (Epic 15).
--
-- Prouve que les metriques Google Sheets (objectifs/budgets declares) ne participent
-- JAMAIS aux totaux cross-source existants (cross_source_conversions, metric_baselines).
--
-- Controle : les metriques GA4/Meta/TikTok/GSC (conversions, sessions, cost) ne
-- doivent pas avoir change apres l'ajout du bloc google-sheets dans fact_daily_kpi.
--
-- Strategie : verifie que les noms de metriques 'conversions' et 'revenue' n'existent
-- PAS sous connector='google-sheets'. Si un bug de cablage ajoutait une ligne
-- google-sheets/conversions, ce test l'attraperait immediatement.
--
-- Un test dbt singulier ECHOUE si retourne des lignes (zero lignes = SUCCES).

SELECT connector, metric, COUNT(*) AS n
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'google-sheets'
  AND metric IN ('conversions', 'revenue', 'sessions', 'active_users', 'cost',
                 'impressions', 'clicks', 'sends', 'opens',
                 'attributed_conversions', 'attributed_revenue',
                 'orders_count', 'refund_amount', 'refunds', 'fees',
                 'new_contacts', 'deals_created', 'deals_closed', 'deal_amount',
                 'leads')
GROUP BY connector, metric
