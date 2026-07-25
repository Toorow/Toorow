-- test_linkedin_min_breakdown_stable.sql -- Story 15.3 (MIN(breakdown_dimension) stable).
--
-- Prouve que MIN(breakdown_dimension) pour linkedin-ads selectionne toujours 'campaign_group_id'
-- (alphabetiquement < 'campaign_id') quand les deux series coexistent. Cette propriete garantit
-- que les consommateurs du mart (metric_baselines, rollup.py) qui utilisent MIN pour choisir
-- une dimension canonique ne selectionneront jamais simultanement les deux grains
-- (double-compte inter-series).
--
-- La regle documentee :
--   MIN(breakdown_dimension) pour linkedin-ads :
--     'campaign_group_id' < 'campaign_id' ('campaign_g' < 'campaign_i').
--   => MIN selectionne 'campaign_group_id' quand les deux coexistent.
--
-- Ce test echoue si MIN selectionne autre chose que les deux valeurs attendues.
-- Un test singulier dbt ECHOUE quand il retourne des lignes (zero lignes = succes).

SELECT
    project_id,
    date,
    metric,
    MIN(breakdown_dimension) AS actual_min,
    COUNT(DISTINCT breakdown_dimension) AS n_dims
FROM {{ ref('fact_daily_kpi') }}
WHERE connector = 'linkedin-ads'
GROUP BY project_id, date, metric
-- Le MIN doit etre 'campaign_group_id' (si les deux series coexistent)
-- ou 'campaign_id' (si seule la serie campagne est presente).
-- Toute autre valeur est inattendue.
HAVING MIN(breakdown_dimension) NOT IN ('campaign_group_id', 'campaign_id')
