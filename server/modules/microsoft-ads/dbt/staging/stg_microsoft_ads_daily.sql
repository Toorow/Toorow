-- Staging: Microsoft Ads LONG-format daily landing (Story 26.4).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, data_level, account_id, campaign_id,
-- ad_group_id, ad_id, keyword_id, search_query, segments_json, metric).
-- The landing is LONG (metric, value_num) because catalog_daily can select
-- ANY of the 192 cataloged columns across the 8 covered report types -- wide
-- columns cannot hold an arbitrary selection. segments_json carries the
-- selected non-key SEGMENTING dimensions (age/gender, device, geo, ...) as
-- canonical sorted-key JSON so any catalog dimension participates in the
-- supersede grain without schema churn.
--
-- Story 26.6 Part A: attributes_json carries the DESCRIPTIVE-MUTABLE entity
-- status/type attributes (campaign_status, ad_status, campaign_type, ...).
-- These MUTATE across the refetch ladder re-pulls (a campaign Active on a day
-- can be re-pulled as Paused for the SAME day), so they are DELIBERATELY kept
-- OUT of the QUALIFY partition below (which is the supersede grain). If they
-- were in the partition, a status change would fork the grain and let the
-- stale row survive -> SUM(cost) would double for the whole re-pulled window
-- (finding F-1). attributes_json is latest-wins: it rides the surviving row
-- (ORDER BY pull_id DESC) so downstream reads the LAST KNOWN status/type.
--
-- data_level (CAMPAIGN | AD_GROUP | AD | KEYWORD | SEARCH_QUERY | ACCOUNT |
-- GEOGRAPHIC | AGE_GENDER -- one marker per whitelisted report type, story
-- 3.6 lesson): each report grain lands under its own marker and is part of
-- the grain key, so coexisting grains never double-count inside one series.
--
-- Restatement (story 26.4): Microsoft credits conversions/revenue to the
-- CLICK date with goal windows up to 90 days + invalid-traffic adjustments
-- (~1 week). The refetch ladder (3/30/90) re-pulls past days; AD-7
-- append-only + this QUALIFY makes every re-pull safe.
--
-- AD-4: ratio statistics selected via catalog_daily land RAW at day grain
-- (non-additive) -- NEVER SUM them downstream; recompute ratios at the
-- semantic layer from stored numerators/denominators.
--
-- Story 39.7 -- Report-timezone CAPTURE (generic time-context contract):
-- report_timezone is passed through UNCHANGED as immutable per-row provenance
-- (E39-AD2) -- the zone the daily rows were bucketed in. It is FIXED by
-- declaration (time_context.locus='fixed'): the connector pins ReportTimeZone
-- on every SubmitGenerateReport request. CAPTURE only: NOT used to
-- convert_timezone() at day grain (HG-4) and NEVER in the QUALIFY partition.
--
-- AI-270 -- FX PROVENANCE, so this staging can reach `fact_daily_kpi`.
--
-- This model was TERMINAL: nothing referenced it, so Microsoft Ads pulled,
-- landed, and produced no figure a person could see. Same repair as google-ads,
-- and deliberately the same SHAPE: `money_evidence_present` needs fx_rate /
-- fx_as_of_date / fx_source / fx_tier in scope, and a second way of joining them
-- would be a second answer to one question.
--
-- FX-AT-READ: `value_num` is NOT converted here. No parity fallback -- a Project
-- with no confirmed reporting currency joins no rate, the converted value is
-- NULL and `money_gap_code` says why.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT
        date,
        data_level,
        account_id,
        campaign_id,
        campaign_name,
        ad_group_id,
        ad_group_name,
        ad_id,
        keyword_id,
        keyword,
        search_query,
        segments_json,
        attributes_json,
        metric,
        value_num,
        cost_source_currency,
        pull_id,
        loaded_at,
        project_id,
        -- Story 39.7: immutable report-timezone provenance (CAPTURE only,
        -- no realign; HG-4). NEVER in the QUALIFY partition below.
        report_timezone
    FROM {{ source('raw_microsoft_ads', 'raw_microsoft_ads_daily') }}
    -- LA SUPERSEDE PORTE SUR LES LIGNES BRUTES : elle reste avant les jointures,
    -- sinon une ligne perdante survivrait en se distinguant par un TAUX et la
    -- supersede se ferait sur (grain x metrique x taux), ce qui n'est pas la regle.
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, data_level, account_id, campaign_id,
                     ad_group_id, ad_id, keyword_id, search_query,
                     COALESCE(segments_json, ''), metric
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.data_level,
    raw.account_id,
    raw.campaign_id,
    raw.campaign_name,
    raw.ad_group_id,
    raw.ad_group_name,
    raw.ad_id,
    raw.keyword_id,
    raw.keyword,
    raw.search_query,
    raw.segments_json,
    raw.attributes_json,
    raw.metric,
    raw.value_num,
    raw.cost_source_currency,
    -- Le montant source immuable, sous le nom que le contrat monetaire du mart
    -- attend : `value_num` est le chiffre, `cost_source_value` est la PREUVE qui
    -- survit a une conversion ratee.
    raw.value_num                              AS cost_source_value,
    -- FX evidence, emitted by ONE macro since the Story 67.13 cutover: the
    -- governed POSED rate is asked first and the seed below is the fallback,
    -- and a refusal (an unanswerable condition, a tie) serves no rate at all.
    -- `fx_as_of_date` is still the day the rate was QUOTED or DECLARED, never
    -- the day its window opens -- story 58.7's repair, carried into both arms.
    {{ toorow_fx_evidence_columns() }}
    raw.pull_id,
    raw.loaded_at,
    raw.project_id,
    -- Story 39.7: immutable report-timezone provenance (CAPTURE only, no
    -- realign; HG-4). Zone fixee par declaration (ReportTimeZone epingle).
    raw.report_timezone
FROM raw
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id    = raw.project_id
   AND fx_res.target_field  = 'cost'
   AND fx_res.source_module = 'microsoft-ads'
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
LEFT JOIN {{ toorow_fx_posed_resolution('microsoft-ads') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
