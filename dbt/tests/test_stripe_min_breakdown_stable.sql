-- test_stripe_min_breakdown_stable.sql -- Story 15.7 (MIN(breakdown_dimension) stable).
--
-- Prouve que MIN(breakdown_dimension) pour stripe est TOUJOURS 'day_total' (unique breakdown emis).
-- Cette propriete garantit que les consommateurs du mart qui utilisent MIN pour choisir une
-- dimension canonique (metric_baselines, cross_source_revenue, rollup.py) ne selectionneront jamais
-- une partition inattendue -- stripe n'a qu'une seule serie day-total (pas de series paralleles
-- geo/device), donc pas de risque de double-compte intra-connecteur.
--
-- Un test singulier dbt ECHOUE quand il retourne des lignes (zero lignes = succes).

SELECT
    project_id,
    date,
    metric,
    MIN(breakdown_dimension) AS actual_min,
    COUNT(DISTINCT breakdown_dimension) AS n_dims
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'stripe'
GROUP BY project_id, date, metric
HAVING MIN(breakdown_dimension) <> 'day_total'
    OR COUNT(DISTINCT breakdown_dimension) <> 1
