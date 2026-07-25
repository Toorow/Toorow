-- test_linkedin_conversions_isolated_from_dedup.sql -- Story 15.3 (AD-4 dedup discipline).
--
-- Prouve que les conversions LinkedIn ne sont JAMAIS incluses dans la vue
-- cross_source_conversions (Rule P, 3.7) sans la regle de dedup declarative.
--
-- La vue cross_source_conversions filtre par metric_source_priority.csv pour n'inclure
-- que le connecteur gagnant par date. Si linkedin-ads est en priorite 4 et qu'un
-- connecteur de priorite plus haute (GA4=1, Meta=2, TikTok=3) a des donnees pour la
-- meme date, les conversions linkedin ne doivent PAS apparaitre dans cross_source_conversions.
-- Si linkedin-ads est le seul connecteur conversions actif pour une date (tous les autres
-- sont absents), ses conversions peuvent legalement apparaitre.
--
-- Ce test verifie que les conversions 'linkedin-ads' dans cross_source_conversions sont
-- UNIQUEMENT presentes quand aucun connecteur de priorite plus haute n'a de donnees.
--
-- Un test singulier dbt ECHOUE quand il retourne des lignes (zero lignes = succes).

WITH linkedin_in_dedup AS (
    -- La vue expose le connecteur gagnant sous 'attribution_source' (Rule P).
    SELECT date, project_id
    FROM {{ ref('cross_source_conversions') }}
    WHERE attribution_source = 'linkedin-ads'
),

higher_priority_connectors AS (
    -- Connecteurs de priorite superieure a linkedin-ads (priorite < 4)
    SELECT date, project_id, connector
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector IN ('google-analytics', 'meta-ads', 'tiktok-ads')
      AND metric = 'conversions'
)

-- S'il y a une date ou linkedin-ads est dans cross_source_conversions ET
-- un connecteur de priorite superieure a aussi des donnees ce jour-la,
-- c'est une violation du dedup (linkedin ne devrait pas etre selectionne).
SELECT
    l.date,
    l.project_id,
    h.connector AS higher_priority_connector_present
FROM linkedin_in_dedup l
JOIN higher_priority_connectors h
    ON h.date = l.date AND h.project_id = l.project_id
