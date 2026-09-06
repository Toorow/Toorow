-- Staging LinkedIn Ads -- grain CAMPAIGN daily.
-- connector-requirements.md LinkedIn Ads profile (Story 15.3, Epic 15).
-- AD-4 : metriques additives uniquement (cost, impressions, clicks, conversions, leads).
-- AD-7 : pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.3 : staging module-owned (AI-06 option A -- model-path externe).
--
-- GRAIN : une ligne par (project_id, date, campaign_id).
-- data_level = 'CAMPAIGN' : ce modele lit UNIQUEMENT les lignes du pivot CAMPAIGN.
-- Lecon review-15-2 F-1 (TikTok pattern, CRITIQUE) : data_level distingue le grain
-- du pivot LinkedIn qui a atterri chaque ligne raw. Sans ce filtre, un CAMPAIGN_GROUP
-- qui porte le meme campaign_id que ses campagnes ferait doubler le total de la serie
-- campagne. Chaque serie mart lit UNIQUEMENT son propre data_level.
--
-- Supersede semantics (AD-7) : quand plusieurs pulls couvrent le meme grain, le
-- DERNIER pull gagne. Les ULIDs sont monotoniquement croissants, ORDER BY pull_id
-- DESC = le plus recent en premier.
--
-- CONVERSIONS (AD-4) : les conversions LinkedIn (externalWebsiteConversions, renommees
-- 'conversions' dans le mart) sont REVENDIQUEES par le canal (fenetre d'attribution par
-- defaut 30j clic / 7j vue, verifiee AI-53 2026-07-19). JAMAIS sommees avec GA4/Meta/
-- TikTok sans la regle de dedup declarative (Rule P, 3.7). Le mart garde ces lignes
-- SEPAREES distinguees par connector='linkedin-ads'.
--
-- LEADS (AD-4) : leadGenerationMailContactInfoShares (Lead Gen Form in-app LinkedIn)
-- renomme 'leads' -- metrique DISTINCTE qui ne mappe PAS sur 'conversions'. Les leads
-- in-app sont des soumissions de formulaire dans la plateforme LinkedIn, pas des
-- conversions web. JAMAIS additionnes aux conversions pixel sans regle explicite.
--
-- Normalisation monnaie (AD-6, pattern 4.2) :
--   - cost_source_value : depense brute dans la devise du compte.
--   - cost_source_currency : devise source (EUR par defaut seed ; a confirmer live).
--   - cost : normalise en devise canonique du projet via fx_rates.
--
-- NULL HONNETE (AD-9) : costInLocalCurrency est une STRING dans l'API LinkedIn
-- (ex: "19.91"). Le CAST DOUBLE peut produire NULL si la valeur est absente ou
-- malformee -- on le laisse NULL plutot que de substituer 0 (zero reel reste 0.0).

WITH raw AS (
    SELECT *
    FROM {{ source('raw_linkedin', 'raw_linkedin_ads_daily') }}
    WHERE data_level = 'CAMPAIGN'
      -- review-15-3 F-3 : un campaign_id NULL rendrait la cle du test grain-unique
      -- NULL (ignoree par `unique`) -- des doublons passeraient silencieusement.
      AND campaign_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, campaign_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date     AS date_source,
    raw.date     AS date,
    -- data_level propage pour traçabilite (chaque serie mart filtre par son propre pivot).
    raw.data_level,
    raw.campaign_id,
    -- Cost normalization (Story 39.10 FX-at-read [[fx-locus-read-not-staging]]):
    -- Staging preserves immutable source-currency amount; conversion happens ONCE at read.
    raw.costInLocalCurrency                                AS cost_source_value,
    raw.cost_source_currency                               AS cost_source_currency,
    raw.costInLocalCurrency                                AS cost,
    -- FX evidence, emitted by ONE macro since the Story 67.13 cutover: the
    -- governed POSED rate is asked first and the seed below is the fallback,
    -- and a refusal (an unanswerable condition, a tie) serves no rate at all.
    -- `fx_as_of_date` is still the day the rate was QUOTED or DECLARED, never
    -- the day its window opens -- story 58.7's repair, carried into both arms.
    {{ toorow_fx_evidence_columns() }}
    raw.impressions,
    raw.clicks,
    -- Rename canonique : externalWebsiteConversions -> conversions (manifest mapping).
    raw.externalWebsiteConversions                         AS conversions,
    -- Leads : metrique DISTINCTE Lead Gen Form (ne mappe PAS sur 'conversions').
    raw.leadGenerationMailContactInfoShares                AS leads,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). RETROACTIVE -- this mart
-- is rebuilt in full on every run, so a binding applies to every day in scope.
-- Wording A, decided 2026-08-22 in
-- docs/product-architecture/capabilities/currency-fx.md.
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'cost'
   AND fx_res.source_module = 'linkedin-ads'
-- ==========================================================================
-- Story 67.13 -- THE GOVERNED RATE IS ASKED FIRST; the seed below is the
-- FALLBACK. Step 4 of the cutover ratified in
-- docs/product-architecture/capabilities/currency-fx.md ("Arbitration,
-- 2026-08-21 -- the read path"), applied to all thirteen staging models in one
-- change because a partial cutover would leave one Project reading the governed
-- store for one connector and the seed for another: two rate authorities inside
-- one total, which is worse than the one wrong authority it replaces.
--
-- The conversion LOCUS does not move. The source currency still stays here and
-- `fx_convert_at_read` still converts once, in the mart. What moves is only
-- where the RATE comes from.
--
-- AT MOST ONE ROW: `toorow_fx_posed_resolution` returns disjoint half-open
-- segments per (project, pair), the winner already chosen by specificity, a tie
-- already REFUSED and an unanswerable condition already named. No QUALIFY and no
-- grain key are needed here, and a plain LEFT JOIN cannot pick a row where the
-- application engine refuses.
--
-- WORDING A (decided 2026-08-22): the declared window governs, retroactively.
-- Nothing below reads when a rate was posted.
LEFT JOIN {{ toorow_fx_posed_resolution('linkedin-ads') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
