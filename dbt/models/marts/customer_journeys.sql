-- customer_journeys: v1 attribution mart (Epic 16, story 16.2).
-- Two attribution perspectives SIDE BY SIDE derived from the story-16.1 acquisition
-- partitions in fact_daily_kpi: last click (session_*) and first click (first_user_*).
--
-- GRAIN (enforced by customer_journeys_grain_unique):
--   one row per project_id x date x attribution_model x breakdown_kind
--            x channel x campaign
--   where attribution_model in ('last_click','first_click')
--     and breakdown_kind    in ('channel','campaign').
-- A 'channel' row carries channel = source/medium and campaign = NULL.
-- A 'campaign' row carries campaign = campaign name and channel = NULL.
-- (channel, campaign) are NEVER jointly populated on one row -- see MARGINAL AXES below.
--
-- ============================ THIS MART IS A READ ============================
-- customer_journeys READS fact_daily_kpi only. It creates NO new fact rows and
-- edits NO existing model. fact_daily_kpi / cross_source_conversions /
-- metric_baselines are UNTOUCHED (story 16.2 constraint (b); proven by
-- test_customer_journeys_totals_unchanged.sql, which checks the acquisition
-- partition totals in fact_daily_kpi are exactly what this mart re-exposes).
--
-- MARGINAL AXES -- NOT a joint (channel x campaign) grid ----------------------
-- fact_daily_kpi carries the acquisition axes as SEPARATE MARGINAL partitions
-- ('session_source_medium', 'session_campaign', 'first_user_source_medium'), never
-- a joint (source_medium x campaign) partition -- a joint would be a cross-join with
-- a different, higher-cardinality grain that story 16.1 deliberately did NOT land
-- (point dur: session x campaign explosion). So v1 exposes the SAME marginals:
--   * a 'channel' row per source/medium (campaign NULL), and
--   * a 'campaign' row per campaign (channel NULL).
-- We do NOT invent a (channel, campaign) pair that the source data cannot support.
--
-- FIRST-CLICK HAS NO CAMPAIGN AXIS -------------------------------------------
-- Story 16.1 landed the first-user axis as 'first_user_source_medium' ONLY (no
-- firstUserCampaignName partition -- see fact_daily_kpi.sql first_user block). So the
-- first_click model emits ONLY 'channel' rows; campaign is ALWAYS NULL for first_click.
-- We do NOT fabricate a first-click campaign the source never produced.
--
-- METRICS PER MODEL ----------------------------------------------------------
--   last_click : conversions AND sessions (both landed on the session axis).
--   first_click: conversions ONLY (the first-user axis never carried sessions -- 16.1).
-- sessions is therefore NULL on every first_click row (honest, not zero).
--
-- SHARE_OF_CONVERSIONS -- a PART OF THE TOP-N (same honesty as the "Part" in 10.4) --
-- share_of_conversions is computed at VIEW TIME as this row's conversions over the
-- SUBTOTAL of its (project_id, date, attribution_model, breakdown_kind) group. Because
-- the acquisition axes are TOP-N BOUNDED upstream (story 16.1: the long tail beyond
-- top_n is DROPPED by design), that subtotal is the TOP-N subtotal, NOT the true GA4
-- day total. So share_of_conversions is a share of the RANKED TOP-N, not of all
-- conversions -- exactly the descriptive "Part" semantics of story 10.4. The shares of
-- a (project, date, model, breakdown_kind) group sum to ~100 (rounding). We do NOT
-- pool channel + campaign rows into one denominator (that would double-count the model
-- subtotal, since channel and campaign are two marginals of the SAME conversions).
--
-- AD-9 (no invented causation): this mart exposes DESCRIPTIVE shares of ranked
-- conversions. There is NO attributed_revenue column and NO causal claim -- "channel X
-- carried N last-click conversions" is a data observation, never "channel X caused the
-- sale". The card (16.3) presents last vs first side by side, NEVER summed.
--
-- AD-7 provenance: pull_id / loaded_at propagated as MAX per group (same convention as
-- fact_daily_kpi -- after supersede dedup a group shares one batch, MAX is the formality).
--
-- Phase B note: multi-touch chains (user_pseudo_id) are the GATED spike 16.4, NOT here.

{{ config(materialized='view') }}

WITH acq AS (
    -- Read ONLY the acquisition partitions from the fact table. Everything below is a
    -- projection of these rows -- no new aggregation grain beyond the marginals 16.1 landed.
    SELECT
        project_id,
        date,
        breakdown_dimension,
        breakdown_value,
        metric,
        value,
        pull_id,
        loaded_at
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-analytics'
      AND breakdown_dimension IN (
          'session_source_medium', 'session_campaign', 'first_user_source_medium'
      )
),

-- Pivot conversions + sessions onto one row per source axis value, tagging each row
-- with its attribution_model and breakdown_kind (the grain axis-type).
axis_rows AS (
    -- last_click / channel: session_source_medium
    SELECT
        project_id,
        date,
        'last_click'                   AS attribution_model,
        'channel'                      AS breakdown_kind,
        breakdown_value                AS channel,
        CAST(NULL AS VARCHAR)          AS campaign,
        SUM(CASE WHEN metric = 'conversions' THEN value END) AS conversions,
        SUM(CASE WHEN metric = 'sessions'    THEN value END) AS sessions,
        MAX(pull_id)                   AS pull_id,
        MAX(loaded_at)                 AS loaded_at
    FROM acq
    WHERE breakdown_dimension = 'session_source_medium'
    GROUP BY project_id, date, breakdown_value

    UNION ALL

    -- last_click / campaign: session_campaign
    SELECT
        project_id,
        date,
        'last_click'                   AS attribution_model,
        'campaign'                     AS breakdown_kind,
        CAST(NULL AS VARCHAR)          AS channel,
        breakdown_value                AS campaign,
        SUM(CASE WHEN metric = 'conversions' THEN value END) AS conversions,
        SUM(CASE WHEN metric = 'sessions'    THEN value END) AS sessions,
        MAX(pull_id)                   AS pull_id,
        MAX(loaded_at)                 AS loaded_at
    FROM acq
    WHERE breakdown_dimension = 'session_campaign'
    GROUP BY project_id, date, breakdown_value

    UNION ALL

    -- first_click / channel: first_user_source_medium (conversions only; sessions NULL;
    -- campaign NULL -- 16.1 landed no first-user campaign axis).
    SELECT
        project_id,
        date,
        'first_click'                  AS attribution_model,
        'channel'                      AS breakdown_kind,
        breakdown_value                AS channel,
        CAST(NULL AS VARCHAR)          AS campaign,
        SUM(CASE WHEN metric = 'conversions' THEN value END) AS conversions,
        CAST(NULL AS DOUBLE)           AS sessions,
        MAX(pull_id)                   AS pull_id,
        MAX(loaded_at)                 AS loaded_at
    FROM acq
    WHERE breakdown_dimension = 'first_user_source_medium'
    GROUP BY project_id, date, breakdown_value
)

SELECT
    project_id,
    date,
    attribution_model,
    breakdown_kind,
    channel,
    campaign,
    conversions,
    sessions,
    -- Share of the TOP-N subtotal of this (project, date, model, breakdown_kind) group.
    -- Descriptive "Part" (10.4 semantics), NOT a share of the true GA4 day total.
    -- NULLIF guards a zero/empty subtotal (honest NULL, never a divide error).
    conversions
        / NULLIF(SUM(conversions) OVER (
              PARTITION BY project_id, date, attribution_model, breakdown_kind
          ), 0) * 100.0 AS share_of_conversions,
    pull_id,
    loaded_at
FROM axis_rows
