-- Staging: maps raw Meta Ads source fields to canonical names
-- connector-requirements.md Meta Ads profile
-- AD-4: additive metrics only (cost, impressions, clicks, conversions)
-- AD-7: pull_id propagated from raw for provenance chain
-- Story 3.6: module-owned staging (AI-06 decision Option A -- external model-path).
--
-- GRAIN: one row per (project_id, date, data_level, campaign_id, adset_id, ad_id).
-- review-15-9 F-1: data_level (CAMPAIGN | ADSET | CREATIVE) records WHICH report grain
-- landed each row. It is part of the grain key AND the supersede partition so a
-- campaign-grain pull and an adset-grain pull for the same campaign_id coexist without
-- colliding; downstream, EACH mart breakdown series reads ONLY the rows of its own
-- data_level (the campaign_id series NEVER sums the campaign_id carried by adset/creative
-- -grain rows). Higher-grain profiles land rows whose adset_id / ad_id are NULL.
--
-- LEGACY NULL data_level (documented COALESCE choice, review-15-9 F-1): rows landed
-- before the data_level column existed (NULL after the additive ALTER) are treated as
-- CAMPAIGN grain via COALESCE(data_level, 'CAMPAIGN'). Justification: the historical
-- Meta pull was campaign-level ONLY (a single bare pull() at level=campaign) -- every
-- pre-migration row IS a campaign-grain row, so COALESCE to 'CAMPAIGN' is honest, not
-- an invention. This keeps the campaign_id series correct across the migration boundary.
--
-- Supersede semantics (AD-7): when several pulls cover the same grain, the LATEST pull
-- wins. ULIDs are lexicographically monotonic, so ORDER BY pull_id DESC = newest first.
--
-- Story 4.2 (AC3, AC6): Cost currency normalization + timezone policy.
--
-- Timezone: Meta Ads Insights API dates are already bucketed in the account's
-- reporting timezone (set in Business Manager). At day grain, we re-label the date
-- to the project timezone when they differ; intraday precision is not available.
-- Source date preserved as 'date_source'. No actual time shift is performed at day grain.
-- Intraday re-labeling (account tz → Europe/Paris) would only matter for spend
-- occurring between account-tz midnight boundaries. Since the Meta API already buckets
-- by its own timezone, re-labeling at day grain means accepting the source tz boundary.
-- We store this as a policy decision (HG-4: do NOT add convert_timezone() at day grain).
-- Story 6.x or a future Epic 4 story can revisit if hourly Meta data is added.
--
-- Story 39.7 -- Report-timezone CAPTURE (generic time-context contract):
-- report_timezone is passed through UNCHANGED as immutable per-row provenance
-- (E39-AD2) -- the exact IANA zone the ad account used to draw its reporting-day
-- boundaries (time_context.locus='account'; the account's timezone_name captured
-- at pull). CAPTURE only: it is NOT used to convert_timezone() at day grain
-- (HG-4 above stands) and NEVER enters the QUALIFY partition. Undetermined at
-- pull => NULL here (fail-closed -> read-time TIMEZONE_GAP, never a silent 'UTC').
--
-- Currency normalization (AD-6, HG-2, HG-3):
--   - cost_source_value: raw spend in the account's billing currency (preserved for reconciliation).
--   - cost_source_currency: billing currency from raw column (default 'USD' for seed data).
--   - cost: the SAME source-currency amount, converted at read, never here.
--   NOTHING IS NORMALIZED HERE, and the two lines above that said otherwise were
--   read as fact for one epic. Story 39.10 moved the conversion to the READ
--   ([[fx-locus-read-not-staging]]): this model preserves the source-currency
--   amount and CARRIES the rate provenance beside it; `fx_convert_at_read` in the
--   mart is what converts, once, and it can be re-derived after a policy change.

WITH raw AS (
    SELECT
        *,
        -- review-15-9 F-1: legacy rows (pre-migration) carry data_level NULL -> treat
        -- them as CAMPAIGN grain (the only grain the historical pull produced).
        COALESCE(data_level, 'CAMPAIGN') AS data_level_resolved
    FROM {{ source('raw_meta', 'raw_meta_ads_daily') }}
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, COALESCE(data_level, 'CAMPAIGN'),
                     campaign_id, adset_id, ad_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    -- Timezone policy (AC6): date_source = raw date; date = re-labeled project date.
    -- At day grain these are identical (no intraday conversion performed — HG-4).
    raw.date     AS date_source,
    raw.date     AS date,
    -- review-15-9 F-1: propagate the report grain so each mart series filters to its own.
    raw.data_level_resolved AS data_level,
    raw.campaign_id,
    raw.campaign_name,
    raw.adset_id,
    raw.adset_name,
    raw.ad_id,
    raw.creative_id,
    -- Cost normalization (Story 39.10 FX-at-read [[fx-locus-read-not-staging]]):
    -- Staging preserves immutable source-currency amount; conversion happens ONCE at read.
    raw.spend                                  AS cost_source_value,
    raw.cost_source_currency                   AS cost_source_currency,
    raw.spend                                  AS cost,
    -- FX evidence, emitted by ONE macro since the Story 67.13 cutover: the
    -- governed POSED rate is asked first and the seed below is the fallback,
    -- and a refusal (an unanswerable condition, a tie) serves no rate at all.
    -- `fx_as_of_date` is still the day the rate was QUOTED or DECLARED, never
    -- the day its window opens -- story 58.7's repair, carried into both arms.
    {{ toorow_fx_evidence_columns() }}
    raw.impressions,
    raw.clicks,
    raw.conversions,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id,
    -- Story 39.7: immutable report-timezone provenance (CAPTURE only, no
    -- realign; HG-4). NULL when undetermined at pull.
    raw.report_timezone
FROM raw
-- G-05: JOIN dim_project to obtain canonical_currency per project (AD-6/FR4).
-- Story 48.3: NO 'EUR' fallback. A Project that has not confirmed a reporting
-- currency joins no rate, so fx_rate stays NULL, fx_convert_at_read yields NULL and
-- fx_gap_code says why -- instead of a source-currency amount being summed into a
-- EUR total as though a dollar were a euro.
-- dim_project reads from source('mirror', 'project_preferences') only -- no cycle.
LEFT JOIN {{ ref('dim_project') }} dp
    ON dp.project_id = raw.project_id
-- Story 13.2: FX conflict resolution override (AD-6). When a resolution exists for
-- (project_id, 'cost', 'meta-ads'), the resolved_source_currency replaces the raw
-- column in the FX JOIN below. RETROACTIVE, and the sentence that stood here
-- said the opposite ("applies from the next dbt run"): this mart is rebuilt in
-- full on every run, so a binding applies to every day in scope. Wording A,
-- decided 2026-08-22 in docs/product-architecture/capabilities/currency-fx.md.
LEFT JOIN {{ toorow_source_or_empty('mirror', 'fx_source_currency_bindings', [
        ['project_id', 'string'],
        ['target_field', 'string'],
        ['source_module', 'string'],
        ['resolved_source_currency', 'string'],
    ]) }} fx_res
    ON fx_res.project_id   = raw.project_id
   AND fx_res.target_field = 'cost'
   AND fx_res.source_module = 'meta-ads'
-- FX validity window: JOIN with raw.date BETWEEN valid_from AND valid_to.
-- raw.date is a VARCHAR ISO string (F-06 convention) -- cast for the DATE seed columns.
-- COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency): prefer resolution.
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
LEFT JOIN {{ toorow_fx_posed_resolution('meta-ads') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.cost_source_currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
