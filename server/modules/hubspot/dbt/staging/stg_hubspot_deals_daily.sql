-- Staging HubSpot deals -- Story 15.5 (Epic 15, connecteurs vague 1).
-- connector-requirements.md : famille CRM & leads B2B, profil deals_daily.
-- AD-4: metriques additives uniquement (deals_created, deals_closed, deal_amount).
-- AD-7: pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.5: staging module-owned (AI-06 decision Option A -- model-path externe).
--
-- GRAIN: une ligne par (project_id, date).
-- Le pull HubSpot effectue deux queries Search par jour :
--   (1) Deals crees (filtre createdate BETWEEN) -> deals_created
--   (2) Deals fermes/won (filtre closedate BETWEEN + dealstage=closedwon) -> deals_closed
-- La table raw stocke une ligne par (project_id, date, pull_id).
-- Le QUALIFY garde le pull le plus recent par (project_id, date).
--
-- Semantique de supersede (AD-7) : ORDER BY pull_id DESC (ULIDs monotones).
--
-- REGLE CRM (CRITIQUE, AD-4, Story 15.5) :
--   deals_created, deals_closed, deal_amount sont des metriques CRM PURES.
--   deals_created != conversions regies (un deal CRM cree par un commercial != une
--   conversion pixel declaree par Meta/Google).
--   deal_amount != revenue Shopify/Stripe (c'est un montant de pipeline estime,
--   pas un paiement encaisse). Ces metriques n'entrent JAMAIS dans un total croise
--   marketing sans une regle declarative explicite (AD-4).
--
-- NULL HONNETE (AD-9) :
--   deal_amount peut etre NULL si aucun deal ferme n'avait de montant renseigne,
--   ou si deals_closed = 0. Le staging propage le NULL.
--   Le mart filtre les agregats NULL via HAVING (contrat value NOT NULL).
--
-- AI-53 VERIFIE le 2026-07-19 :
--   Proprietes deals : amount, dealstage, pipeline, createdate, closedate.
--   Stage "closedwon" par defaut -- a confirmer en passe live (Phase B, AI-13)
--   car la valeur exacte depend du pipeline configure par le compte HubSpot.
--   Source : https://developers.hubspot.com/docs/api/crm/deals

WITH raw AS (
    SELECT *
    FROM {{ source('raw_hubspot', 'raw_hubspot_deals_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date                     AS date_source,
    raw.date                     AS date,
    -- REGLE CRM : deals_created != conversions regies (AD-4).
    raw.deals_created,
    -- REGLE CRM : deals_closed = deals gagnes (won) dans la journee.
    raw.deals_closed,
    -- NULL honnete (AD-9) : deal_amount NULL si aucun deal ferme ou sans montant.
    -- deal_amount != revenue Shopify/Stripe : c'est un montant de pipeline estime.
    raw.deal_amount,
    -- Company currency resolved from account metadata; never infer from process env.
    raw.currency,
    -- Complete recursive splitting makes truncation a diagnostic legacy column only.
    -- F-4 (AD-9) : truncated=TRUE si le pull a atteint le cap 10 000 (50 pages API).
    -- Dans ce cas deals_created ou deals_closed sont des PLANCHERS, pas des valeurs exactes.
    -- Defaut FALSE (seed synthetique ou pull sous le cap).
    COALESCE(raw.truncated, FALSE)  AS truncated,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
WHERE raw.date IS NOT NULL
