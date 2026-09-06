-- Staging: Google Ads LONG-format daily landing (Story 26.2).
-- AD-7: QUALIFY supersede -- the latest pull per grain wins. ULIDs are
-- lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- GRAIN: one row per (project_id, date, data_level, customer_id, campaign_id,
-- ad_group_id, ad_id, criterion_id, search_term, segments_json, metric).
-- The landing is LONG (metric, value_num) because the catalog_daily profile can
-- select ANY of the 278 cataloged metrics -- wide columns cannot hold an
-- arbitrary selection. segments_json carries the selected ROW-BEARING
-- dimensions (GAQL segments: device, geo, ...) as canonical sorted-key JSON so
-- any catalog segment participates in the supersede grain without schema churn.
--
-- Story 26.6 Part A -- attributes_json vs the grain: DESCRIPTIVE-MUTABLE
-- attributes (status/type enums: campaign_status, ad_group_status,
-- ad_group_ad_status, campaign_advertising_channel_type, ... -- declared
-- descriptive_mutable in the catalog) land in a SEPARATE attributes_json column
-- and are DELIBERATELY EXCLUDED from the QUALIFY partition below. A campaign
-- ENABLED on the 1st then PAUSED on the 6th, re-pulled for the 1st by the 26.1
-- refetch ladder, comes back status=PAUSED; if the status were in segments_json
-- (a grain key) the changed value would fork the grain, the stale ENABLED row
-- would survive the QUALIFY and SUM(cost) would double for the whole re-pulled
-- window. Kept out of the grain, the re-pull keeps ONE row per grain, metrics
-- are never doubled, and attributes_json shows the LATEST-WINS status (the row
-- from the newest pull_id the QUALIFY selects). attributes_json NEVER enters
-- the partition.
--
-- data_level (CAMPAIGN | AD_GROUP | AD | KEYWORD | SEARCH_TERM -- story 3.6
-- lesson): each report grain lands under its own marker and is part of the
-- grain key, so a campaign-grain pull and an ad-grain pull for the same
-- campaign_id coexist without double-counting inside one series.
--
-- Micros rule (story 26.2): *_micros amounts are divided by 1e6 in the
-- connector's transform() BEFORE landing -- value_num is ALWAYS in currency
-- units for monetary metrics (cost_source_currency preserves the account
-- billing currency when customer_currency_code was selected).
--
-- AD-4: provider-computed ratio metrics selected via catalog_daily land RAW at
-- day grain (non-additive) -- NEVER SUM them downstream; recompute ratios at
-- the semantic layer from stored numerators/denominators.
--
-- Story 39.7 -- Report-timezone CAPTURE (generic time-context contract):
-- report_timezone is passed through UNCHANGED as immutable per-row provenance
-- (E39-AD2) -- the exact IANA zone the Google Ads account used to draw its
-- reporting-day boundaries (time_context.locus='account', customer.time_zone
-- selected at pull). CAPTURE only: it is NOT used to convert_timezone() at day
-- grain (HG-4) and NEVER enters the QUALIFY partition (a zone change at the
-- source must not fork the grain). Undetermined at pull => NULL here
-- (fail-closed -> read-time TIMEZONE_GAP, never a silent 'UTC').
-- AI-270 -- FX PROVENANCE, so this staging can reach `fact_daily_kpi`.
--
-- This model was TERMINAL: `grep -rl "ref('stg_google_ads_daily')"` returned
-- zero. Google Ads pulled, landed, and its figures reached no fact -- therefore
-- no card, no report, no number a person could see. The connector's own read
-- path already expects the opposite (`connector.py:10`, « the MCP server reads
-- the fact_daily_kpi mart only »): it was written for a mart it never reached.
--
-- The fact's monetary contract is `money_evidence_present`, and it requires
-- `fx_rate` / `fx_as_of_date` / `fx_source` / `fx_tier` IN SCOPE. They are
-- joined here rather than in the mart because the source currency is a property
-- of the landed row, and because that is where `stg_meta_ads_daily` puts them --
-- one shape for the same question, not a second.
--
-- FX-AT-READ, NOT AT STAGING: `value_num` is NOT converted here. The source
-- amount stays immutable and the conversion happens once, in the mart, through
-- `fx_convert_at_read`. A reporting-currency change then re-derives without
-- touching a source fact.
--
-- NO PARITY FALLBACK. A Project that has not confirmed a reporting currency
-- joins no rate; `fx_rate` stays NULL, the converted value is NULL and
-- `money_gap_code` says why. Coalescing to 1.0 would sum a dollar into a euro
-- total and record nothing -- the defect story 48.3 removed from the macro.
--
-- The join attaches a rate to EVERY row, monetary or not, because the landing is
-- LONG: one row per (grain x metric). The mart uses it only on the monetary
-- series, and a non-monetary row carrying an unused rate costs nothing and
-- avoids a second, metric-aware join here.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT
        date,
        data_level,
        customer_id,
        campaign_id,
        campaign_name,
        ad_group_id,
        ad_group_name,
        ad_id,
        criterion_id,
        keyword_text,
        search_term,
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
    FROM {{ source('raw_google_ads', 'raw_google_ads_daily') }}
    -- LA SUPERSEDE PORTE SUR LES LIGNES BRUTES, donc elle reste ICI, avant
    -- toute jointure (AI-270). Descendue apres le LEFT JOIN FX elle aurait
    -- deux defauts : ses colonnes deviennent ambigues, et surtout une ligne
    -- perdante pourrait survivre en se distinguant par un taux -- la supersede
    -- se ferait alors sur (grain x metrique x taux), ce qui n'est pas la regle.
    -- attributes_json est DELIBEREMENT absent de la partition (story 26.6 A) :
    -- un changement de statut ne doit jamais forker le grain.
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, data_level, customer_id, campaign_id,
                     ad_group_id, ad_id, criterion_id, search_term,
                     COALESCE(segments_json, ''), metric
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.data_level,
    raw.customer_id,
    raw.campaign_id,
    raw.campaign_name,
    raw.ad_group_id,
    raw.ad_group_name,
    raw.ad_id,
    raw.criterion_id,
    raw.keyword_text,
    raw.search_term,
    raw.segments_json,
    raw.attributes_json,
    raw.metric,
    raw.value_num,
    raw.cost_source_currency,
    -- The immutable source-currency amount, under the name the mart's money
    -- contract expects. Same column, said twice on purpose: `value_num` is the
    -- figure, `cost_source_value` is the EVIDENCE that survives a failed
    -- conversion.
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
    -- realign; HG-4). NULL when undetermined at pull.
    raw.report_timezone
FROM raw
-- The Project's confirmed reporting currency (AD-6/FR4). No 'EUR' fallback.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2 (AD-6): an operator's FX conflict resolution replaces the raw
-- source currency in the join below. RETROACTIVE, and the sentence that stood
-- here said the opposite: this mart is rebuilt in full on every run, so a
-- binding applies to every day in scope and figures already published DO move.
-- That is Wording A, decided 2026-08-22 in
-- docs/product-architecture/capabilities/currency-fx.md for the posed RATE, and
-- the binding beside it never had a second temporal authority of its own.
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id    = raw.project_id
   AND fx_res.target_field  = 'cost'
   AND fx_res.source_module = 'google-ads'
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
LEFT JOIN {{ toorow_fx_posed_resolution('google-ads') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
