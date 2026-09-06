-- Staging: Google Ad Manager raw report data -> canonical names
-- AD-7: QUALIFY supersede -- latest pull per grain wins (ULIDs are lex-monotonic).
-- MONEY: `value` stays in MICROS through staging AND the mart (decision 2026-07-22).
--   The /1e6 happens ONCE at read, with `currency`. Do NOT divide here.
-- AD-4: ratios (CTR, eCPM, CPM/CPC rate) are NOT stored -- reconstructed in the mart
--   from additive components. `currency` + `report_timezone` carry the money/date
--   context so revenue is never naked and currencies are never summed together.
-- TODO(catalogue port): confirm the breakdown grain against the ported report profiles.
--
-- AI-270 -- MICROS NORMALISED TO DECIMAL HERE, and FX provenance joined, so this
-- staging can reach `fact_daily_kpi`. It was terminal: nothing referenced it.
--
-- THE UNIT DECISION IS NOT MINE. `dbt/seeds/money_metric_units.csv` declares
-- `ad_revenue,decimal` and names this exact case in its own note: « GAM declares
-- native=micros on its field but is NOT mart-wired; when a micros source is
-- wired the per-source unit collision (adjust decimal vs GAM micros under the
-- SAME canonical name) must be resolved by adapter normalization AT STAGING so
-- fact_daily_kpi holds ONE unit ». adjust already emits `ad_revenue` in decimal.
-- Two units under one canonical name in one fact is a total nobody can read, so
-- the division happens here -- which is what that note prescribes.
--
-- The line above still stands for anything reading this model OUTSIDE the mart:
-- there is no such reader today, and if one appears it reads decimal, not micros.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT
        date,
        metric,
        value,
        currency,
        report_timezone,
        breakdown_dimension,
        breakdown_value,
        pull_id,
        loaded_at,
        project_id
    FROM {{ source('raw_google_ad_manager', 'raw_google_ad_manager_daily') }}
    -- La supersede porte sur les lignes brutes : avant les jointures, sinon une
    -- ligne perdante survivrait en se distinguant par un taux.
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, metric, breakdown_dimension, breakdown_value
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date,
    raw.metric,
    -- MONEY IN DECIMAL from here on (see the header). A non-money metric is
    -- untouched: dividing a click count by a million would be silent nonsense.
    CASE WHEN raw.metric = 'ad_revenue'
         THEN CAST(raw.value AS {{ toorow_exact_money_type() }}) / 1000000
         ELSE CAST(raw.value AS {{ toorow_exact_money_type() }})
    END                                        AS value,
    raw.currency,
    raw.currency                               AS cost_source_currency,
    CASE WHEN raw.metric = 'ad_revenue'
         THEN CAST(raw.value AS {{ toorow_exact_money_type() }}) / 1000000
         ELSE NULL
    END                                        AS cost_source_value,
    raw.report_timezone,
    raw.breakdown_dimension,
    raw.breakdown_value,
    {{ toorow_fx_evidence_columns() }}
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
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
   AND fx_res.target_field  = 'ad_revenue'
   AND fx_res.source_module = 'google-ad-manager'
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
LEFT JOIN {{ toorow_fx_posed_resolution('google-ad-manager') }} fxp
    ON  fxp.project_id     = raw.project_id
   AND fxp.base_currency  = COALESCE(fx_res.resolved_source_currency, raw.currency)
   AND fxp.quote_currency = dp.canonical_currency
   AND CAST(raw.date AS DATE) >= fxp.seg_from
   AND CAST(raw.date AS DATE) <  fxp.seg_until
LEFT JOIN {{ ref('fx_rates') }} fx
    ON fx.from_currency = COALESCE(fx_res.resolved_source_currency, raw.currency)
   AND fx.to_currency   = dp.canonical_currency
   AND CAST(raw.date AS DATE) BETWEEN fx.valid_from AND fx.valid_to
