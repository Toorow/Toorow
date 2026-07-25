-- Staging HubSpot contacts -- Story 15.5 (Epic 15, connecteurs vague 1).
-- connector-requirements.md : famille CRM & leads B2B, profil contacts_daily.
-- AD-4: metriques additives uniquement (new_contacts -- compte de contacts crees).
-- AD-7: pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.5: staging module-owned (AI-06 decision Option A -- model-path externe).
--
-- GRAIN: une ligne par (project_id, date).
-- Le grain est le JOUR -- HubSpot CRM v3 n'expose pas d'agregat daily natif
-- (AI-53 verifie 2026-07-19) ; le pull compte les contacts crees dans la journee
-- via le Search API (filtre BETWEEN sur createdate). La table raw contient une
-- ligne par (project_id, date, pull_id) -- le QUALIFY garde le pull le plus recent.
--
-- Semantique de supersede (AD-7) : quand plusieurs pulls couvrent le meme grain
-- (project_id, date), le PLUS RECENT gagne.
-- Les ULIDs sont lexicographiquement monotones, ORDER BY pull_id DESC = le plus
-- recent en premier.
--
-- REGLE CRM (CRITIQUE, AD-4, Story 15.5) :
--   new_contacts est une metrique CRM PURE -- elle mesure la realite du pipeline
--   commercial. JAMAIS sommee avec les conversions des regies (Meta, TikTok,
--   Google Ads / GA4) ni le revenue Shopify/Stripe dans un total croise marketing.
--   Usage legitime : reconciliation leads CRM vs leads declares par les regies,
--   carte pipeline commerciale distincte.
--
-- NULL HONNETE (AD-9) : new_contacts peut theoriquement etre NULL si le pull
-- n'a pas abouti -- la colonne raw est INTEGER NULLable. Le staging propage le NULL.
-- Un zero est un vrai zero (aucun contact cree ce jour-la, pas une donnee manquante).
--
-- AI-53 VERIFIE le 2026-07-19 : noms de champs bases sur la documentation
-- officielle HubSpot CRM v3 (https://developers.hubspot.com/docs/api/crm/contacts).
-- A confirmer en passe live (Phase B, AI-13).

WITH raw AS (
    SELECT *
    FROM {{ source('raw_hubspot', 'raw_hubspot_contacts_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date                     AS date_source,
    raw.date                     AS date,
    -- NULL honnete (AD-9) : new_contacts NULL = donnee de pull absente.
    -- Zero = aucun contact cree ce jour-la (valeur reelle, pas une imputation).
    raw.new_contacts,
    -- F-4 (AD-9) : truncated=TRUE si le pull a atteint le cap 10 000 (50 pages API).
    -- Dans ce cas new_contacts est un PLANCHER, pas une valeur exacte.
    -- Defaut FALSE (seed synthetique ou pull sous le cap).
    COALESCE(raw.truncated, FALSE)  AS truncated,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
-- Garde NULL : on garde les lignes meme si new_contacts est NULL (donnee absente vs zero).
-- Le mart filtre les agregats NULL via HAVING (AD-9, contrat value NOT NULL).
WHERE raw.date IS NOT NULL
