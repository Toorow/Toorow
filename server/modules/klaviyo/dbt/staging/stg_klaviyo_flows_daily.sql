-- Staging Klaviyo flows -- Story 15.8 (Epic 15, connecteurs vague 1).
-- connector-requirements.md : famille email/SMS, profil flow_daily.
-- AD-4: metriques additives uniquement (sends, opens, clicks,
--        attributed_conversions, attributed_revenue).
-- AD-7: pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.8: staging module-owned (AI-06 decision Option A -- model-path externe).
--
-- GRAIN: une ligne par (project_id, date, flow_id).
-- Semantique de supersede (AD-7): quand plusieurs pulls couvrent le meme grain,
-- le PLUS RECENT gagne (QUALIFY + ORDER BY pull_id DESC).
--
-- Memes regles de non-agregation et de NULL honnete que stg_klaviyo_campaigns_daily.
-- Voir ce fichier pour la documentation complete des regles AD-4 / AD-9 / AI-53.
--
-- Les flows Klaviyo (sequences automatisees : abandon panier, welcome series, etc.)
-- partagent la meme structure de metriques que les campagnes. Ils sont stockes dans
-- la MEME table raw (raw_klaviyo_daily) avec un flow_id non-NULL et campaign_id NULL.
--
-- AI-53 VERIFIE le 2026-07-19 : endpoint POST /api/flow-series-reports/ (Reporting API).
-- Noms de champs exacts a confirmer en passe live (Phase B, AI-13).

WITH raw AS (
    SELECT *
    FROM {{ source('raw_klaviyo', 'raw_klaviyo_daily') }}
    WHERE flow_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, flow_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date                     AS date_source,
    raw.date                     AS date,
    raw.flow_id,
    raw.flow_name,
    -- NULL honnete (AD-9) : les metriques absentes restent NULL.
    raw.sends,
    raw.opens,
    raw.clicks,
    -- REGLE CRITIQUE : attributed_conversions = conversions REVENDIQUEES canal email.
    -- JAMAIS sommees avec les conversions des regies ni GA4 (AD-4).
    raw.attributed_conversions,
    -- REGLE CRITIQUE : attributed_revenue = revenue ATTRIBUE canal email.
    -- JAMAIS additionne au revenue Shopify/Stripe (AD-4).
    raw.attributed_revenue,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
