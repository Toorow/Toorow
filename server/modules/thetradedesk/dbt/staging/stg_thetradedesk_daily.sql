-- Staging: The Trade Desk LONG-format daily landing (Story 28.2).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, report_template, partner_id,
-- advertiser_id, campaign_id, ad_group_id, creative_id, segments_json,
-- metric). The landing is LONG (metric, value_num) because catalog_daily can
-- select ANY exposed column of the 263-column surface -- wide columns cannot
-- hold an arbitrary selection. segments_json carries the selected non-key
-- dimensions as canonical sorted-key JSON so any catalog column participates
-- in the supersede grain without schema churn.
--
-- report_template = the managed ReportTemplate that produced this row
-- (daily_performance | conversions | creative | geo_platform | video_player);
-- it is part of the grain key so coexisting report grains never double-count
-- inside one series. advertiser_id is the MyReports reporting anchor.
--
-- Attribution restatement (AD-7 + refetch ladder 3/14/45): TTD restates
-- conversion families (click/view-through/touch/time-weighted-decay, pixels
-- 01-06) as attribution windows close (last ~3-7 days provisional). Re-pulls
-- land as new pull_ids and this QUALIFY keeps the most recent value per grain
-- -- restatements are absorbed, never double-counted.
--
-- AD-4: provider-computed ratio columns (Cpm/Cpc/Ctr/Cpa*) selected via
-- catalog_daily land RAW at day grain (NON-ADDITIVE) -- NEVER SUM them
-- downstream; recompute ratios at the semantic layer from stored
-- numerators/denominators. PlayerRewind is an ADDITIVE count (not a ratio).
--
-- AI-270 -- FX PROVENANCE for the USD-denominated cost metrics, so this staging
-- can reach `fact_daily_kpi`.
--
-- THE CURRENCY IS IN THE METRIC NAME here, which is why this connector needs no
-- currency COLUMN and gets a different treatment from google-ads. TTD publishes
-- `AdvertiserCostUSD`, `TtdCostUsd` and `PartnerCostUsd` -- denominated by
-- contract -- alongside `AdvertiserCostAdvCurrency`, whose currency is the
-- advertiser's and is stated NOWHERE in the landing.
--
-- So the join below is on the literal 'USD': it is the currency those three
-- metrics ARE, not a guess. The fourth is deliberately left out of the mart, in
-- the fact model, with its reason.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT
        date,
        report_template,
        partner_id,
        advertiser_id,
        campaign_id,
        campaign_name,
        ad_group_id,
        ad_group_name,
        creative_id,
        segments_json,
        metric,
        value_num,
        pull_id,
        loaded_at,
        project_id
    FROM {{ source('raw_thetradedesk', 'raw_thetradedesk_daily') }}
    -- La supersede porte sur les lignes BRUTES, avant les jointures.
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, report_template, partner_id, advertiser_id,
                     campaign_id, ad_group_id, creative_id,
                     COALESCE(segments_json, ''), metric
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.report_template,
    raw.partner_id,
    raw.advertiser_id,
    raw.campaign_id,
    raw.campaign_name,
    raw.ad_group_id,
    raw.ad_group_name,
    raw.creative_id,
    raw.segments_json,
    raw.metric,
    raw.value_num,
    -- La devise EST connue pour les trois metriques `*_usd` : elle est dans leur
    -- nom. Elle reste NULL pour toute autre metrique -- y compris
    -- `advertiser_cost_adv_currency`, dont la devise n'est ecrite nulle part.
    CASE WHEN raw.metric IN ('advertiser_cost_usd', 'ttd_cost_usd', 'partner_cost_usd')
         THEN 'USD' END                        AS cost_source_currency,
    CASE WHEN raw.metric IN ('advertiser_cost_usd', 'ttd_cost_usd', 'partner_cost_usd')
         THEN CAST(raw.value_num AS {{ toorow_exact_money_type() }}) END AS cost_source_value,
    {{ toorow_fx_evidence_columns() }}
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
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
LEFT JOIN {{ toorow_fx_posed_resolution('thetradedesk') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = 'USD'
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = 'USD'
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
