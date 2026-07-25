-- Staging Klaviyo campagnes -- Story 15.8 (Epic 15, connecteurs vague 1).
-- connector-requirements.md : famille email/SMS, profil campaign_daily.
-- AD-4: metriques additives uniquement (sends, opens, clicks,
--        attributed_conversions, attributed_revenue).
-- AD-7: pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.8: staging module-owned (AI-06 decision Option A -- model-path externe).
--
-- GRAIN: une ligne par (project_id, date, campaign_id).
-- Semantique de supersede (AD-7): quand plusieurs pulls couvrent le meme grain
-- (project_id, date, campaign_id), le PLUS RECENT gagne.
-- Les ULIDs sont lexicographiquement monotones, ORDER BY pull_id DESC = le plus
-- recent en premier.
--
-- REGLE DE NON-AGREGATION (CRITIQUE, AD-4, Story 15.8) :
--   attributed_conversions et attributed_revenue sont des metriques REVENDIQUEES
--   par le canal Klaviyo (fenetre last-touch 5j clic/ouverture email par defaut,
--   configurable par compte -- AI-53 verifie le 2026-07-19).
--   Ces colonnes ne doivent JAMAIS etre additionnees dans un total croise avec :
--     * les conversions des regies (Meta, TikTok, Google Ads) ;
--     * le revenue Shopify/Stripe.
--   Elles alimentent la deduplication (Epic 17) et sont comparees CANAL PAR CANAL
--   uniquement. Toute agregation croisee requiert la regle AD-4 / cross_source.
--
-- NULL HONNETE (AD-9) : une metrique absente dans la reponse API reste NULL.
--   Elle n'est PAS remplacee par 0. Cela permet de distinguer "donnee manquante"
--   de "zero enregistre". Le staging propage les NULL du raw.
--
-- AI-53 VERIFIE le 2026-07-19 : noms de champs bases sur la doc Reporting API
-- Klaviyo (https://developers.klaviyo.com/en/reference/reporting_api_overview).
-- Les noms exacts (campaign_id, campaign_name, conversion/revenue mappings) sont
-- a confirmer en passe live (Phase B, AI-13).

WITH raw AS (
    SELECT *
    FROM {{ source('raw_klaviyo', 'raw_klaviyo_daily') }}
    WHERE campaign_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, campaign_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date                     AS date_source,
    raw.date                     AS date,
    raw.campaign_id,
    raw.campaign_name,
    -- NULL honnete (AD-9) : les metriques absentes restent NULL.
    raw.sends,
    raw.opens,
    raw.clicks,
    -- REGLE CRITIQUE : attributed_conversions = conversions REVENDIQUEES canal email.
    -- JAMAIS sommees avec les conversions des regies (Meta/TikTok/Google) ni GA4.
    raw.attributed_conversions,
    -- REGLE CRITIQUE : attributed_revenue = revenue ATTRIBUE canal email.
    -- JAMAIS additionne au revenue Shopify/Stripe dans un total croise (AD-4).
    raw.attributed_revenue,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
