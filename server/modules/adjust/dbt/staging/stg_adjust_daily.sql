-- Staging Adjust -- mobile measurement (Report Service API), profil network_daily.
-- AD-4: metriques additives uniquement (cost, installs, clicks, impressions,
--        sessions, revenue, ad_revenue, all_revenue). Les ratios (ctr, *_rate)
--        ne sont JAMAIS stockes (drop dans transform()).
-- AD-7: QUALIFY supersede -- la derniere pull par grain gagne. Les ULIDs sont
--        lexicographiquement monotones, ORDER BY pull_id DESC = plus recent.
-- Staging module-owned (AI-06 Option A -- model-path externe).
--
-- GRAIN: une ligne par (project_id, date, app_token, network, campaign_id).
-- campaign_name est le libelle du campaign_id (meme grain), app celui de
-- app_token, cost_source_currency est fonctionnellement dependante de l'app.
--
-- NULL HONNETE (AD-9) : une metrique absente dans la reponse API reste NULL.
--   Elle n'est PAS remplacee par 0 (distinguer "donnee manquante" de "zero").
--
-- REGLE AD-4 (revenue) : le revenue Adjust (mesure SDK in-app) n'est JAMAIS
--   somme avec le revenue Shopify/Stripe dans un total croise. Regle
--   declarative metric_source_priority.csv (shopify 1, stripe 2, adjust 3) +
--   vue cross_source_revenue.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_adjust', 'raw_adjust_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, app_token, network, campaign_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.app_token,
    raw.app,
    raw.network,
    raw.campaign_id,
    raw.campaign_name,
    -- NULL honnete (AD-9) : les metriques absentes restent NULL.
    raw.cost,
    raw.installs,
    raw.clicks,
    raw.impressions,
    raw.sessions,
    -- REGLE AD-4 : revenue Adjust = revenu mesure par le SDK ; jamais somme
    -- avec shopify/stripe (metric_source_priority + cross_source_revenue).
    raw.revenue,
    raw.ad_revenue,
    raw.all_revenue,
    raw.cost_source_currency,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
