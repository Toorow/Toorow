-- review-16-2 F-1 (MEDIUM) : identite marginale du last_click.
--
-- Les deux axes last_click ('channel' = session_source_medium, 'campaign' =
-- session_campaign) sont DEUX MARGINALES DES MEMES conversions (le staging
-- acquisition_session porte les deux dimensions sur la MEME ligne raw ; le mart
-- fact_daily_kpi en derive deux partitions paralleles qui totalisent chacune la
-- meme somme de combos top-N).
--
-- Consequence -- LE PIEGE DU CONSOMMATEUR NAIF : SUM(conversions) sur les lignes
-- last_click de customer_journeys SANS filtrer breakdown_kind rend ~2x le vrai
-- sous-total (channel + campaign additionnes). Tout consommateur (16.3 carte
-- Attribution inclus) DOIT filtrer breakdown_kind. Ce test PROUVE l'identite
-- marginale -- si elle casse, soit une partition a perdu des lignes, soit les
-- deux axes ne derivent plus de la meme source (et la doc du piege devient
-- fausse).
--
-- Un test dbt singular FAIL quand il retourne des lignes (zero ligne = pass).

WITH channel_totals AS (
    SELECT project_id, date, SUM(conversions) AS channel_total
    FROM {{ ref('customer_journeys') }}
    WHERE attribution_model = 'last_click' AND breakdown_kind = 'channel'
    GROUP BY project_id, date
),

campaign_totals AS (
    SELECT project_id, date, SUM(conversions) AS campaign_total
    FROM {{ ref('customer_journeys') }}
    WHERE attribution_model = 'last_click' AND breakdown_kind = 'campaign'
    GROUP BY project_id, date
)

SELECT
    ch.project_id,
    ch.date,
    ch.channel_total,
    ca.campaign_total
FROM channel_totals ch
FULL OUTER JOIN campaign_totals ca
    ON ca.project_id = ch.project_id AND ca.date = ch.date
WHERE ch.project_id IS NULL
   OR ca.project_id IS NULL
   OR ABS(COALESCE(ch.channel_total, 0) - COALESCE(ca.campaign_total, 0)) > 0.001
