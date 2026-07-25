-- Staging LinkedIn Ads -- grain CAMPAIGN_GROUP daily.
-- connector-requirements.md LinkedIn Ads profile (Story 15.3, Epic 15).
-- AD-4 : metriques additives uniquement (cost, impressions, clicks, conversions).
-- AD-7 : pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.3 : staging module-owned (AI-06 option A -- model-path externe).
--
-- GRAIN : une ligne par (project_id, date, campaign_group_id).
-- data_level = 'CAMPAIGN_GROUP' : ce modele lit UNIQUEMENT les lignes du pivot
-- CAMPAIGN_GROUP. Lecon review-15-2 F-1 (TikTok pattern) : le filtre data_level
-- empeche les lignes CAMPAIGN de contaminer la serie CAMPAIGN_GROUP.
--
-- Note semantique : les lignes CAMPAIGN_GROUP sont des ROLL-UPS de campagnes d'un
-- meme groupe (SUM(campaigns) == campaign_group total par date+metrique). Il ne
-- faut JAMAIS sommer les deux series en meme temps sur la meme date : ce serait
-- un double-compte. Le mart emet deux blocs UNION ALL distincts (un par data_level)
-- et chaque serie est independante.
--
-- Supersede semantics (AD-7) : ORDER BY pull_id DESC = le pull le plus recent gagne.
--
-- CONVERSIONS (AD-4) : meme discipline que stg_linkedin_ads_campaign_daily.
-- Les conversions CAMPAIGN_GROUP sont elles aussi REVENDIQUEES par le canal.
--
-- leads (Lead Gen Form) non expose a ce grain (CAMPAIGN_GROUP pivot ne retourne
-- pas leadGenerationMailContactInfoShares dans la doc officielle -- a confirmer
-- en passe live Phase B). Le modele garde la colonne NULL par honnete.

WITH raw AS (
    SELECT *
    FROM {{ source('raw_linkedin', 'raw_linkedin_ads_daily') }}
    WHERE data_level = 'CAMPAIGN_GROUP'
      -- review-15-3 F-3 : cle NULL = doublons invisibles au test grain-unique.
      AND campaign_group_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, campaign_group_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date     AS date_source,
    raw.date     AS date,
    raw.data_level,
    raw.campaign_group_id,
    -- Cost normalization (Story 39.10 FX-at-read [[fx-locus-read-not-staging]]):
    -- Staging preserves immutable source-currency amount; conversion happens ONCE at read.
    raw.costInLocalCurrency                                AS cost_source_value,
    raw.cost_source_currency                               AS cost_source_currency,
    raw.costInLocalCurrency                                AS cost,
    -- FX provenance columns for read-time conversion
    fx.rate                                                AS fx_rate,
    fx.valid_from                                          AS fx_as_of_date,
    'seed'                                                 AS fx_source,
    'fixed'                                                AS fx_tier,
    raw.impressions,
    raw.clicks,
    raw.externalWebsiteConversions                         AS conversions,
    -- Leads au grain campaign_group : non fourni par l'API LinkedIn (NULL honnete).
    -- AI-53 : a confirmer en passe live (Phase B) si ce champ est disponible
    -- au niveau CAMPAIGN_GROUP.
    NULL::INTEGER                                          AS leads,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = raw.cost_source_currency
   AND fx.to_currency   = COALESCE(dp.canonical_currency, 'EUR')
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
