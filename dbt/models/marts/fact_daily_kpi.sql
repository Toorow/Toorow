{#-
  ONE PROJECT DOES NOT HAVE FIFTY-TWO CONNECTORS, and this fact used to require
  it: every branch below was UNIONed unconditionally, so the model could only be
  built by a Project that had landed EVERY connector. No real Project has.

  Measured 2026-08-18 on the only production Project carrying data (YouTube
  alone, 34 640 raw rows, 547 videos): the nightly's own command,
  `dbt build --vars '{project, raw_schema}'`, returned **53 errors**, each of
  them `Catalog Error: Table with name raw_<connector>_daily does not exist!`.
  So this fact had never been built there, every card that reads it answered
  `warehouse_not_ready`, and the connector's own mart -- `fact_youtube_daily`,
  which depends on YouTube alone -- built perfectly beside it.

  Each branch is now conditioned on the models it reads being in the warehouse
  (`toorow_model_present`, dbt/macros/relation_present.sql). `ns.emitted` carries
  the separator, because `UNION ALL` belongs BETWEEN two emitted branches and is
  therefore written by the second one -- a separator hard-coded on its own line
  is what made this file unconditional in the first place.

  THE GUARD IS HALF THE REPAIR. A staging model whose `raw_*` source is missing
  still errors, and dbt then SKIPS everything downstream, this model included.
  The runner excludes those staging models for the Project being built; this
  file drops their branches.
-#}
{#- `ref()` sous condition : dbt ne peut pas inferer la dependance
    statiquement, donc elle est declaree ici. La branche managed_feed (69.2) est
    la seule dont le `ref` vive dans un `{% if %}` que le parseur ne traverse
    pas -- les autres passent par `toorow_model_present`, qui rend True au parse
    et appelle `ref` a ce moment-la. -#}
-- depends_on: {{ ref('stg_managed_feed_facts') }}
{%- set ns = namespace(emitted=false) -%}
{% if toorow_model_present('stg_ga4_standard_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}
-- fact_daily_kpi: canonical day-grain fact table (AD-4, AD-7, AD-14)
-- Schema: (project_id, date, connector, metric, breakdown_dimension, breakdown_value, value, pull_id, loaded_at)
-- GRAIN (enforced by fact_daily_kpi_grain_unique): one row per
--   project_id × date × connector × metric × breakdown_dimension × breakdown_value.
-- Each breakdown is aggregated over the OTHER dimensions (e.g. sessions ×
-- device_category sums across countries) — review-1-4/F-05 exposed that the
-- previous unpivot materialised one row per source row and violated this grain.
-- AD-4: additive values only, so SUM is the only aggregation used here;
--       ratios (CTR, CVR) are NOT stored; computed at view time in semantic layer
-- AD-6: timezone normalization (Europe/Paris) deferred to Story 4.2
-- AD-7: pull_id preserved for provenance; after staging's supersede dedup all
--       rows of a grain share one batch, MAX() is a formality (documented for 3.4)
-- AD-14: project_id hardcoded to 'default' at P0; real resolution in Story 2.3

{% set metrics = ["sessions", "active_users", "conversions"] %}
{% set dimensions = ["device_category", "country"] %}

{% for metric in metrics %}
{% for dimension in dimensions %}
{#- Story 58.5, arbitrage 1: a row the source gave no country for keeps a NAMED
    bucket instead of a NULL `breakdown_value` the not_null test would break on.
    Every other dimension is untouched -- the macro fires on `country` alone. -#}
{%- set dimension_value = country_bucket('country', 'country_source') if dimension == 'country' else dimension -%}
SELECT
    -- Story 2.7 AC6 HG-4: project_id from staging (not hardcoded 'default').
    -- Seeded rows have project_id='default'; real pull rows carry their own project_id.
    project_id,
    date,
    'google-analytics'     AS connector,
    '{{ metric }}'         AS metric,
    '{{ dimension }}'      AS breakdown_dimension,
    {{ dimension_value }}  AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)           AS pull_id,
    MAX(loaded_at)         AS loaded_at
FROM {{ ref('stg_ga4_standard_daily') }}
GROUP BY project_id, date, {{ dimension_value }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_ga4_standard_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Story 8.11 (R5): composite sub-dimension split 'country>device'.
-- Emitted as ORDINARY long-format rows using a '>' path separator so no schema
-- change is needed (breakdown_dimension='country>device',
-- breakdown_value='<iso>>​<device>' e.g. 'FR>mobile'). Derived here from staging's
-- already-normalized country + device_category columns (raw carries both per row,
-- so no new extraction and no seed change).
--
-- DOUBLE-COUNT SAFETY (see story DESIGN §2): this composite is ONE MORE parallel
-- series that independently totals the day. SUM over 'country>device' equals SUM
-- over 'country' equals SUM over 'device_category' (proved by
-- test_composite_reconciliation.sql). Marts that need a day total pick a canonical
-- single dimension via MIN(breakdown_dimension): 'country' < 'country>device' <
-- 'device_category', so MIN never selects the composite — metric_baselines and
-- cross_source_conversions keep summing a single-dimension series (no double count).
--
-- AD-4: additive metrics only (sessions, active_users, conversions). Non-additive
-- metrics (e.g. average_position) are NEVER emitted as composites in v1
-- (test_composite_additive_only.sql guards this).
{% for metric in metrics %}
SELECT
    project_id,
    date,
    'google-analytics'                       AS connector,
    '{{ metric }}'                           AS metric,
    'country>device'                         AS breakdown_dimension,
    -- Story 58.5, arbitrage 1: THIS is the concatenation that broke. `NULL || '>' ||
    -- 'mobile'` is NULL in SQL, so a single row with no country turned
    -- `breakdown_value` NULL and took the nightly build down on the not_null test.
    -- The named bucket keeps the composite total equal to both single-dimension
    -- series (test_composite_reconciliation).
    {{ country_bucket('country', 'country_source') }} || '>' || device_category
                                             AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }}))        AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                             AS pull_id,
    MAX(loaded_at)                           AS loaded_at
FROM {{ ref('stg_ga4_standard_daily') }}
GROUP BY project_id, date, {{ country_bucket('country', 'country_source') }}, device_category
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_ga4_user_type_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Epic 10, story 10.1: user_type_daily profile (GA4 newVsReturning).
-- breakdown_dimension='user_type', values: new / returning / unknown.
-- ADDITIVE-ONLY (AD-4): appended AFTER the existing GA4 blocks; it does NOT modify
-- any existing block and does NOT change device_category or country totals -- it is
-- ONE MORE parallel series that independently totals the day (the 9.9 CRITICAL
-- scenario; reconciliation proven by test_ga4_user_type_reconciliation.py).
--
-- DOUBLE-COUNT SAFETY (same argument as the 8.11 composite block, DESIGN 2): marts
-- that need a day total pick a canonical single dimension via MIN(breakdown_dimension).
-- Lexicographic MIN order across the four GA4 dims:
--   'country' < 'country>device' < 'device_category' < 'user_type'
--   ('device_category' < 'user_type' because 'd' < 'u').
-- So MIN never selects 'user_type' -- metric_baselines and cross_source_conversions
-- keep summing 'country' (the canonical single dimension). No double count.
--
-- No composite sub-dimension for user_type: this profile has a SINGLE dimension, so
-- no '>'-path is possible (unlike country>device).
{% for metric in metrics %}
SELECT
    project_id,
    date,
    'google-analytics'                AS connector,
    '{{ metric }}'                    AS metric,
    'user_type'                       AS breakdown_dimension,
    user_type                         AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_user_type_daily') }}
GROUP BY project_id, date, user_type
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_ga4_landing_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Epic 10, story 10.3: pages_daily_landing profile (GA4 landingPage).
-- breakdown_dimension='landing_page', metric sessions, value = URL path.
-- ADDITIVE-ONLY (AD-4): appended AFTER the existing GA4 blocks; it does NOT modify
-- any existing block and does NOT change device_category / country / user_type
-- totals -- it is ONE MORE parallel series (the 9.9 CRITICAL scenario; reconciliation
-- proven by test_ga4_pages_reconciliation.py).
--
-- TOP-N BOUNDED PARTITION -- NOT a full reconciliation (Story 10.3 point dur 1):
-- UNLIKE the user_type block (whose 3 buckets sum to the connector daily total),
-- this landing_page partition is TOP-N bounded upstream (the pull_pages_daily_landing
-- pull applies orderBys sessions DESC + limit=page_top_n, default 50, clamped 10..200).
-- The long tail beyond page_top_n is DROPPED BY DESIGN, so SUM over 'landing_page'
-- does NOT equal the day total. This is intentional (no silent cap: page_top_n and the
-- dropped tail are documented here and logged by the pull). The reconciliation test
-- therefore asserts only (a) the OTHER partitions are unchanged, (b) MIN stays
-- 'country', (c) <= page_top_n distinct landing_page values per day -- it does NOT
-- assert this partition sums to the connector total (it does not).
--
-- DOUBLE-COUNT SAFETY (same argument as the user_type block, DESIGN 2): marts that
-- need a day total pick a canonical single dimension via MIN(breakdown_dimension).
-- Lexicographic MIN order across the GA4 dims:
--   'country' < 'country>device' < 'device_category' < 'landing_page' < 'page' < 'user_type'
--   ('device_category' < 'landing_page' because 'de' < 'la'; 'landing_page' < 'page'
--    because 'l' < 'p'; 'page' < 'user_type' because 'p' < 'u').
-- So MIN never selects 'landing_page' -- metric_baselines and cross_source_conversions
-- keep summing 'country'. No double count.
{% set page_metrics = ["sessions"] %}
{% for metric in page_metrics %}
SELECT
    project_id,
    date,
    'google-analytics'                AS connector,
    '{{ metric }}'                    AS metric,
    'landing_page'                    AS breakdown_dimension,
    landing_page                      AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_landing_daily') }}
GROUP BY project_id, date, landing_page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_ga4_paths_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Epic 10, story 10.3: pages_daily_paths profile (GA4 pagePath).
-- breakdown_dimension='page', metric screen_page_views, value = URL path.
-- ADDITIVE-ONLY (AD-4): appended AFTER the landing_page block; same TOP-N bounded
-- semantics (the tail beyond page_top_n is dropped by design -- SUM over 'page' does
-- NOT equal the day total; NOT asserted as a reconciliation). Parallel series; does
-- NOT change any existing partition total.
--
-- AI-53 HONESTY: screen_page_views comes from the GA4 screenPageViews metric, which
-- is DECLARED but NOT verified live in this story (Phase B gate, AC 11). The mart
-- block ships; the live confirmation is annotated in the Dev Agent Record.
--
-- CONNECTOR-KEYED DISTINCTNESS: GSC also lands breakdown_dimension='page' rows, but
-- those carry connector='gsc'; these carry connector='google-analytics'. fact_daily_kpi
-- keys on connector, so the two 'page' partitions never collide at the grain.
--
-- DOUBLE-COUNT SAFETY: MIN(breakdown_dimension) for GA4 stays 'country' (see the
-- landing_page block's lexicographic proof; 'page' sorts after 'device_category' and
-- 'landing_page'). No double count.
{% set page_view_metrics = ["screen_page_views"] %}
{% for metric in page_view_metrics %}
SELECT
    project_id,
    date,
    'google-analytics'                AS connector,
    '{{ metric }}'                    AS metric,
    'page'                            AS breakdown_dimension,
    page                              AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_paths_daily') }}
GROUP BY project_id, date, page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_ga4_acquisition_session') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Epic 16, story 16.1: acquisition_daily_session profile (GA4 last click).
-- TWO MARGINAL partitions derived from the SINGLE staging grain
-- (session_source_medium, session_campaign) x metrics {conversions, sessions}. Like
-- country/device_category are two marginals of the standard row -- NOT a cross-join
-- between attribution axes (the forbidden cross-join is session x first-user, which is
-- a SEPARATE profile / raw table).
-- ADDITIVE-ONLY (AD-4): appended AFTER the existing GA4 blocks; does NOT modify any
-- existing block and does NOT change device_category / country / user_type /
-- landing_page / page totals -- ONE MORE parallel series per axis (the 9.9 CRITICAL
-- scenario; reconciliation proven by test_ga4_acquisition_reconciliation.py).
--
-- TOP-N BOUNDED PARTITION -- NOT a full reconciliation (Story 16.1 point dur 1):
-- UNLIKE user_type (whose 3 buckets sum to the connector daily total), these
-- acquisition partitions are TOP-N bounded upstream (pull_acquisition_daily_session
-- applies orderBys conversions DESC + limit=top_n, default 50, clamped 10..200). The
-- long tail beyond top_n is DROPPED BY DESIGN, so SUM over each axis does NOT equal the
-- day total. Intentional (page_top_n + dropped tail documented + logged by the pull).
--
-- review-16-5 fix-11 SEMANTICS CLARIFICATION:
-- The `limit=top_n` in the GA4 Data API request applies to the ENTIRE DATE RANGE
-- (dateRanges spans date_from..date_to). GA4 returns the top-N rows over the FULL
-- window, NOT per day. The 'date' dimension multiplies rows per day -- a single
-- source_medium x campaign combo that appears on D days yields D rows, each counted
-- once. Therefore `n_distinct(source_medium) per day <= top_n` is valid (the full-
-- window top-N includes at most top_n combos per day), but the LIVE count is
-- typically LESS because most combos are only active on a subset of days. The
-- part/share expressed in the attribution card (16.3) is "share of the top-N
-- returned window", not share of the GA4 day total (long tail excluded, Phase B).
--
-- DOUBLE-COUNT SAFETY (same argument as the user_type / page blocks, DESIGN 2): marts
-- that need a day total pick a canonical single dimension via MIN(breakdown_dimension).
-- Lexicographic MIN order across ALL GA4 dims after this story:
--   'country' < 'country>device' < 'device_category' < 'first_user_source_medium'
--   < 'landing_page' < 'page' < 'session_campaign' < 'session_source_medium'
--   < 'user_type'
--   (all new dims start with 'f' or 's' > 'c'; 'country' < 'country>device' because
--    the bare 'country' prefix is shorter). So MIN never selects an acquisition dim --
--   metric_baselines and cross_source_conversions keep summing 'country'. No double
--   count. Proven programmatically in test_ga4_acquisition_reconciliation.py and by
--   test_ga4_acquisition_min_breakdown_stable.sql / _top_n_not_in_canonical.sql.
--
-- AI-53 HONESTY: session_source_medium / session_campaign come from GA4
-- sessionSourceMedium / sessionCampaignName, DECLARED but NOT verified live in this
-- story (Phase B gate, AC11). The mart block ships; the live confirmation is annotated
-- in the Dev Agent Record.
{% set acq_metrics = ["conversions", "sessions"] %}
{% for metric in acq_metrics %}
SELECT
    project_id,
    date,
    'google-analytics'                AS connector,
    '{{ metric }}'                    AS metric,
    'session_source_medium'           AS breakdown_dimension,
    session_source_medium             AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_acquisition_session') }}
GROUP BY project_id, date, session_source_medium
UNION ALL
SELECT
    project_id,
    date,
    'google-analytics'                AS connector,
    '{{ metric }}'                    AS metric,
    'session_campaign'                AS breakdown_dimension,
    session_campaign                  AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_acquisition_session') }}
GROUP BY project_id, date, session_campaign
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_ga4_acquisition_first_user') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Epic 16, story 16.1: acquisition_daily_first_user profile (GA4 first click).
-- breakdown_dimension='first_user_source_medium', metric conversions.
-- ADDITIVE-ONLY (AD-4): same TOP-N bounded semantics as the session block (tail beyond
-- top_n dropped by design -- SUM does NOT equal the day total; NOT asserted as
-- reconciliation). SEPARATE profile / raw table from the session axis (no session x
-- first-user cross-join). Parallel series; does NOT change any existing partition total.
--
-- DOUBLE-COUNT SAFETY: MIN(breakdown_dimension) for GA4 stays 'country' (see the session
-- block's lexicographic proof; 'first_user_source_medium' sorts after 'device_category').
-- No double count. AI-53: firstUserSourceMedium declared but not verified live (Phase B).
--
-- NEVER sum first_user + session axes together: each axis is an INDEPENDENT marginal
-- partition (last click vs first click). Summing them double-counts conversions across
-- attribution perspectives -- the mart keeps them as distinct breakdown_dimension rows
-- and the card (16.3) presents them side-by-side, never added (AD-9).
{% set acq_fu_metrics = ["conversions"] %}
{% for metric in acq_fu_metrics %}
SELECT
    project_id,
    date,
    'google-analytics'                AS connector,
    '{{ metric }}'                    AS metric,
    'first_user_source_medium'        AS breakdown_dimension,
    first_user_source_medium          AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_acquisition_first_user') }}
GROUP BY project_id, date, first_user_source_medium
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_meta_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Meta Ads (Story 3.6) -- the ONE permitted central dbt edit (HG-2, AC8).
-- Meta Ads has a different metric/dimension set than GA4, so it is an explicit
-- UNION ALL block. At day grain all Meta metrics (cost, impressions, clicks,
-- conversions) are additive (AD-4). Meta 'conversions' and GA4 'conversions' are
-- kept as SEPARATE rows distinguished by connector -- cross-source dedup is 3.7.
--
-- AI-51 (Epic 8): country>device composite sub-dimension split is DELIBERATELY
-- NOT emitted for meta-ads. stg_meta_ads_daily lands only campaign_id / adset_id /
-- ad_id -- it carries NEITHER country NOR device on any row (the Meta Insights pull
-- in this project is at the campaign hierarchy grain, not a geo/device breakdown).
-- The proven 8.11 composite is country>device; meta-ads lacks BOTH components, so
-- per the AI-51 constraint "if a module lacks one of the pair, SKIP it -- do not
-- invent data" there is no honest country>device composite to build here. When a
-- future Meta pull adds country/device breakdown columns to raw_meta_ads_daily,
-- graft a UNION block identical to the GA4/GSC composite blocks.
--
-- DOUBLE-COUNT SAFETY (review-15-9 F-1, data_level-scoped series -- EXACT tiktok F-1
-- pattern): the three meta breakdowns (campaign_id / adset_id / ad_id) are each ONE
-- parallel series, but each is built ONLY from the rows of ITS OWN report grain
-- (data_level): campaign_id reads data_level='CAMPAIGN', adset_id reads 'ADSET', ad_id
-- reads 'CREATIVE'. This fixes the latent double-count: BEFORE this filter, when both
-- campaign-grain and adset-grain rows coexisted the same day, the campaign_id series
-- summed BOTH the campaign-grain row AND the campaign_id carried by every adset/creative
-- -grain row -> the campaign was double-counted inside its own series. Now each series is
-- self-contained per data_level, so no grain bleeds into another. Marts needing a day
-- total pick a canonical single series via MIN(breakdown_dimension) ('ad_id' <
-- 'adset_id' < 'campaign_id'), never two grains summed together.
{% set meta_series = [
    ("campaign_id", "CAMPAIGN"),
    ("adset_id", "ADSET"),
    ("ad_id", "CREATIVE"),
] %}
{% set meta_metrics = ["cost", "impressions", "clicks", "conversions"] %}
{% for metric in meta_metrics %}
{% for dimension, data_level in meta_series %}
SELECT
    project_id,
    date,
    'meta-ads'          AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    {% if metric == 'cost' %}
    SUM({{ fx_convert_at_read('cost') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_meta_ads_daily') }}
-- review-15-9 F-1: this series reads ONLY its own report grain (data_level) so grains
-- never bleed. The IS NOT NULL guard still filters any stray NULL id defensively.
WHERE data_level = '{{ data_level }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_gsc_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- GSC Search Analytics (Story 6.2) — additive metrics: clicks, impressions.
-- AD-4: SUM is valid for additive metrics at day grain.
-- average_position is NOT in this mart AT ALL (review-6-2 fix): a non-additive
-- metric has no honest per-dimension mart row — semantic_avg_position computes
-- the impression-weighted mean directly from staging (page-grain) rows.
{% set gsc_additive_metrics = ["clicks", "impressions"] %}
{% set gsc_dimensions = ["page", "country", "device"] %}
{% for metric in gsc_additive_metrics %}
{% for dimension in gsc_dimensions %}
{#- Story 58.5, arbitrage 1: the SAME repair as the GA4 block above. GSC carries
    `country_source` too, so a row with no country signal is one bucket here as
    well -- a defect of one connector is a defect of the family. -#}
{%- set dimension_value = country_bucket('country', 'country_source') if dimension == 'country' else dimension -%}
SELECT
    project_id,
    date,
    'gsc'               AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension_value }} AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_gsc_daily') }}
GROUP BY project_id, date, {{ dimension_value }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_gsc_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- GSC composite sub-dimension split 'country>device' (AI-51, Epic 8) -- following
-- the proven GA4 8.11 pattern EXACTLY. stg_gsc_daily lands country AND device on the
-- SAME raw row (grain project_id x date x page x country x device), so both dims
-- co-occur and the composite is honestly derivable (no invented data).
--
-- DOUBLE-COUNT SAFETY (same argument as the GA4 block, DESIGN §2): this composite is
-- ONE MORE parallel series that independently totals the day. For additive metrics
-- SUM over 'country>device' == SUM over 'country' == SUM over 'device' (proved by
-- test_composite_reconciliation_gsc.sql). Marts needing a day total pick a canonical
-- single dimension via MIN(breakdown_dimension); for GSC the ordered dims are
-- 'country' < 'country>device' < 'device' < 'page', so MIN selects 'country' -- never
-- the composite. No double count.
--
-- AD-4: additive metrics ONLY (clicks, impressions). GSC's average_position is
-- non-additive and is NOT in this mart at all; its impression-weighted composite is
-- provided by semantic_avg_position_composite (AI-52), NEVER summed here.
-- test_composite_additive_only.sql guards that no average_position composite appears.
{% for metric in gsc_additive_metrics %}
SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    '{{ metric }}'                     AS metric,
    'country>device'                   AS breakdown_dimension,
    -- Story 58.5, arbitrage 1: the GSC twin of the GA4 concatenation.
    {{ country_bucket('country', 'country_source') }} || '>' || device
                                       AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }}))  AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_daily') }}
GROUP BY project_id, date, {{ country_bucket('country', 'country_source') }}, device
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_gsc_query_page_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Epic 10, story 10.5: GSC composite sub-dimension split 'query>page' (cannibalisation).
-- Follows the proven 8.11/AI-51 country>device composite pattern EXACTLY. Source is the
-- NEW query-grain staging model stg_gsc_query_page_daily (raw rows landed by the
-- query_page_daily pull profile: query AND page co-occur on the SAME raw row), so the
-- JOINT (query, page) grain is honestly derivable — the marginal 'query' and 'page'
-- rows above CANNOT express which page belongs to which query, which is exactly what
-- cannibalisation detection requires. This composite carries that join on one long-format
-- row (breakdown_dimension='query>page', breakdown_value='<query>>​<page>').
--
-- DOUBLE-COUNT SAFETY (same argument as the country>device block, DESIGN §2): this is ONE
-- MORE parallel series that independently totals the day. For additive metrics SUM over
-- 'query>page' == SUM over 'query' == SUM over 'page'. Marts needing a day total pick a
-- canonical single dimension via MIN(breakdown_dimension); across ALL GSC dims the order is
-- 'country' < 'country>device' < 'device' < 'page' < 'query' < 'query>page' (since 'c' is
-- the smallest first letter and 'p' < 'q'), so MIN still selects 'country' — NEVER the new
-- query composite. No double count; the existing page/country/device day totals are unchanged.
--
-- RECONCILIATION SCOPE (Story 10.5 F-4): the anti-double-count guard here rests SOLELY on
-- MIN(breakdown_dimension)='country' (proven by the grain-unique test + the existing
-- test_composite_reconciliation_gsc.sql for country>device). A SYMMETRIC reconciliation of
-- 'query>page' against its marginals (SUM over 'query>page' == SUM over 'query' == SUM over
-- 'page') is NOT applicable here: the GSC marginal block only emits 'page' (and country /
-- device) rows — it emits NO marginal 'query' rows into fact_daily_kpi — so there is no
-- 'query' total to reconcile against. Writing a query>page reconciliation dbt test would
-- require fabricating a marginal 'query' series that the mart does not produce; per AI-54 we
-- do NOT add a test we cannot back with real mart rows. The 'page'-side identity is already
-- covered by the page marginal totals staying unchanged (asserted above / by MIN ordering).
--
-- AD-4: additive metrics ONLY (clicks, impressions). GSC's average_position is non-additive
-- and is NEVER emitted here (test_composite_additive_only.sql guards it). The cannibalisation
-- resolver impression-weights position per page from the staging rows (semantic rule), never
-- from a summed mart value.
{% for metric in gsc_additive_metrics %}
SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    '{{ metric }}'                     AS metric,
    'query>page'                       AS breakdown_dimension,
    query || '>' || page               AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }}))  AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_query_page_daily') }}
GROUP BY project_id, date, query, page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_gsc_surface_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- GSC non-web search surfaces (GSC full coverage): Discover, Google News, image,
-- video, news — landed via the API 'type' parameter (the ONLY access path to those
-- surfaces) and staged in stg_gsc_surface_daily (grain project x date x search_type
-- x page; web is EXCLUDED there — web stays in the marginal blocks above).
--
-- DATA-UNIVERSE NOTE (unlike the parallel-series blocks above): these series do NOT
-- re-total the web-search day — a Discover impression is not a web impression. The
-- 'search_type' marginal totals ITS OWN surface per value; it must never be summed
-- with the page/country/device web series. MIN(breakdown_dimension) canonical-day
-- selection is UNAFFECTED: 'country' < 'search_type' (c < s) so the web day total
-- keeps its canonical dimension; these rows are additional, clearly-keyed data.
--
-- AD-4: additive metrics ONLY (clicks, impressions); average_position for surfaces
-- is read from staging (impression-weighted at the semantic/card layer), never here.
{% for metric in gsc_additive_metrics %}
SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    '{{ metric }}'                     AS metric,
    'search_type'                      AS breakdown_dimension,
    search_type                        AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }}))  AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_surface_daily') }}
GROUP BY project_id, date, search_type
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_gsc_surface_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- GSC surface composite 'search_type>page' (same pattern as country>device /
-- query>page): the joint (search_type, page) grain co-occurs on the SAME staging
-- row, so the composite is honestly derivable. Lets cards answer "top Discover
-- pages" without a second pull. Same data-universe caveat as the marginal above.
{% for metric in gsc_additive_metrics %}
SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    '{{ metric }}'                     AS metric,
    'search_type>page'                 AS breakdown_dimension,
    search_type || '>' || page         AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }}))  AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_surface_daily') }}
GROUP BY project_id, date, search_type, page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_gsc_search_appearance_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- GSC searchAppearance (GSC full coverage): rich-result / AMP / etc. appearance
-- buckets from stg_gsc_search_appearance_daily (grain project x date x
-- search_appearance, web type only, daily grain reconstructed by the per-day
-- pull loop — the API forbids combining searchAppearance with other dimensions).
--
-- DATA-UNIVERSE NOTE: appearance buckets OVERLAP web totals partially (only
-- impressions with a special appearance are counted; plain blue links are not),
-- so SUM over 'search_appearance' != the web day total BY DESIGN. Keyed under its
-- own breakdown_dimension; never part of canonical day-total selection
-- ('country' < 'search_appearance').
{% for metric in gsc_additive_metrics %}
SELECT
    project_id,
    date,
    'gsc'                              AS connector,
    '{{ metric }}'                     AS metric,
    'search_appearance'                AS breakdown_dimension,
    search_appearance                  AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }}))  AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_search_appearance_daily') }}
GROUP BY project_id, date, search_appearance
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_shopify_orders_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Shopify orders (Story 15.4, Epic 15) -- source de verite VENTES e-commerce.
-- metrics: revenue, refund_amount, orders_count (all ADDITIVE at day grain, AD-4).
-- The mart AGGREGATES orders to the DAY: breakdown_dimension = 'day_total' (a single
-- bare day-total series, no geo/device breakdown -- the Admin REST orders pull in this
-- project is at the order grain, not a country breakdown). transaction_id is a DETAIL
-- dimension of the raw/staging (the GA4 x Shopify join key, Epic 17) -- it is DELIBERATELY
-- NOT emitted as a mart partition (a per-transaction breakdown would explode the grain and
-- is not what any day-total mart consumer needs).
--
-- DOUBLE-COUNT SAFETY: Shopify contributes a SINGLE breakdown_dimension ('day_total') per
-- metric, so there is no intra-connector multi-partition double-count risk (unlike the GA4
-- country/device/user_type parallel series). It is ONE MORE parallel series keyed by
-- connector='shopify'; fact_daily_kpi keys on connector, so it never collides with any GA4/
-- Meta/GSC partition. MIN(breakdown_dimension) for shopify is trivially 'day_total'.
-- review-15-4 F-5 (future guard): a later shopify breakdown sorting BEFORE 'day_total'
-- lexicographically (e.g. 'country', 'channel') would move the dbt per-day MIN onto it --
-- that new partition must then NOT be top-N bounded, or the Python selector
-- (coverage-first; see rollup.py canonical_breakdown_per_connector caveat) would keep
-- pinning 'day_total' for the window and DIVERGE from the per-day dbt MIN.
--
-- REFUNDS (decision de story 15.4, REFERENCE pour Stripe 15.7): refund_amount is a
-- DEDICATED positive metric row -- it is NEVER subtracted silently from the revenue row.
-- A net figure (revenue - refund_amount) is computed EXPLICITLY at the semantic/card layer,
-- keeping both flows independently auditable (AD-9 no-black-box).
--
-- REVENUE PRIORITY (3.7 declarative rule): revenue is a NEW canonical metric here -- NO other
-- connector emits 'revenue' into fact_daily_kpi today (verified: semantic_roas references
-- 'revenue' only as a NULL placeholder; no mart block produces it). So the declarative
-- priority seed (metric_source_priority.csv) is NOT required at this stage. IT BECOMES
-- OBLIGATORY the moment Stripe (Story 15.7) also emits 'revenue': a Stripe payment settling a
-- Shopify order must count ONCE -- at that point add a metric_source_priority row for revenue
-- and a cross_source_revenue view mirroring cross_source_conversions (AD-4 / 3.7 discipline).
--
-- orders_count vs conversions: orders_count is DELIBERATELY NOT mapped onto the canonical
-- 'conversions' metric. A Shopify order is a completed SALE, not a régie-declared conversion;
-- mapping it onto 'conversions' would drag it into the GA4/Meta cross_source_conversions dedup
-- (Rule P, 3.7) where it does not belong. It stays a distinct 'orders_count' metric.
{% set shopify_metrics = ["revenue", "refund_amount", "orders_count"] %}
{% for metric in shopify_metrics %}
SELECT
    project_id,
    date,
    'shopify'           AS connector,
    '{{ metric }}'      AS metric,
    'day_total'         AS breakdown_dimension,
    'all'               AS breakdown_value,
    {% if metric in ["revenue", "refund_amount"] %}
    SUM({{ fx_convert_at_read(metric) }}) AS value,
    {{ money_evidence_present('revenue_source_value' if metric == 'revenue' else 'refund_source_value', 'revenue_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_shopify_orders_daily') }}
GROUP BY project_id, date
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_woocommerce_orders_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- woocommerce: BEGIN (epic-25) -- STRICTLY ADDITIVE block (do not reformat above).
-- Self-hosted commerce sales source of record, the sibling of the shopify block above.
-- Same shape: three additive day-grain metrics (revenue, refund_amount, orders_count)
-- on a single 'day_total' breakdown. Keyed by connector='woocommerce', so it NEVER
-- collides with the shopify / stripe / GA4 / Meta / GSC / TikTok / LinkedIn partitions.
-- MIN(breakdown_dimension) for woocommerce is trivially 'day_total'.
--
-- REFUNDS (decision REFERENCE shopify 15.4 / stripe 15.7): refund_amount is a DEDICATED
-- positive metric row (the connector already took abs() of WooCommerce's negative refund
-- totals). It is NEVER subtracted silently from the revenue row; the net is computed
-- explicitly at the semantic/card layer (AD-9 no-black-box).
--
-- REVENUE DEDUP (AD-4, Rule P 3.7): revenue is now emitted by shopify (15.4), stripe (15.7)
-- AND woocommerce. A store rarely runs both shopify and woocommerce, but the declarative
-- rule still applies: metric_source_priority.csv ranks revenue -> shopify=1, woocommerce=2,
-- stripe=3, adjust=4, square=5, and cross_source_revenue.sql picks ONE winning source per
-- (project_id, date). The per-source rows stay intact in fact_daily_kpi; they are never
-- summed into a single cross-source total without the dedup view.
--
-- orders_count vs conversions: like shopify, orders_count is DELIBERATELY NOT mapped onto
-- the canonical 'conversions' metric (a WooCommerce order is a completed SALE, not a
-- régie-declared conversion) -- it stays a distinct 'orders_count' metric outside the
-- GA4/Meta cross_source_conversions dedup.
{% set woocommerce_metrics = ["revenue", "refund_amount", "orders_count"] %}
{% for metric in woocommerce_metrics %}
SELECT
    project_id,
    date,
    'woocommerce'       AS connector,
    '{{ metric }}'      AS metric,
    'day_total'         AS breakdown_dimension,
    'all'               AS breakdown_value,
    {% if metric in ["revenue", "refund_amount"] %}
    SUM({{ fx_convert_at_read(metric) }}) AS value,
    {{ money_evidence_present('revenue_source_value' if metric == 'revenue' else 'refund_source_value', 'revenue_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_woocommerce_orders_daily') }}
GROUP BY project_id, date
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- woocommerce: END block.
{% endif %}
{% if toorow_model_present('stg_tiktok_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- tiktok-ads: BEGIN Story 15.2 (Epic 15) -- ADDITIVE-ONLY block (do not reformat above).
-- Third paid-social régie alongside GA4/Meta. TikTok has a different metric/dimension
-- set, so it is an explicit UNION ALL block (same shape as the meta-ads block). At day
-- grain all TikTok metrics (cost, impressions, clicks, conversions) are additive (AD-4).
--
-- CONVERSIONS DEDUP (AD-4, Rule P 3.7, leçon Epic 10 CRITICAL): TikTok 'conversions' are
-- CLAIMED by the channel (attributed by the account window, default 7D click / 1D view --
-- declared in tiktok-ads/manifest.json attribution_window). They are kept as SEPARATE rows
-- distinguished by connector='tiktok-ads' -- fact_daily_kpi keys on connector, so a TikTok
-- 'conversions' row NEVER collides with GA4 or Meta 'conversions'. They are NEVER summed
-- into a single cross-source total without the declarative dedup (cross_source_conversions,
-- Rule P). Same discipline as the meta-ads block above. Reconciliation "totaux GA4/Meta/GSC/
-- Shopify existants INCHANGES" is proven by test_tiktok_totals_isolated.sql +
-- test_tiktok_conversions_isolated_from_dedup.sql.
--
-- AI-51 (Epic 8): country>device composite is DELIBERATELY NOT emitted for tiktok-ads.
-- stg_tiktok_ads_daily lands only campaign_id / adgroup_id / ad_id -- it carries NEITHER
-- country NOR device on any row (the BASIC report pull in this project is at the campaign
-- hierarchy grain). Per AI-51 "if a module lacks one of the pair, SKIP it -- do not invent
-- data" there is no honest composite to build here (identical rationale to meta-ads).
--
-- DOUBLE-COUNT SAFETY (review-15-2 F-1, data_level-scoped series): the three tiktok
-- breakdowns (campaign_id / adgroup_id / ad_id) are each ONE parallel series, but each is
-- built ONLY from the rows of ITS OWN report grain (data_level): the campaign_id series
-- reads data_level='AUCTION_CAMPAIGN', adgroup_id reads 'AUCTION_ADGROUP', ad_id reads
-- 'AUCTION_AD'. This is the fix for the latent double-count: BEFORE this filter, when both
-- campaign-grain and adgroup-grain rows coexisted in raw the same day, the campaign_id
-- series summed BOTH the campaign-grain row AND the campaign_id carried by every adgroup-
-- grain row -> the campaign was double-counted inside its own series. Now each series is
-- self-contained per data_level, so no grain bleeds into another.
--   * Within a single series, the reconciliation "campaign total == sum of its adgroups ==
--     sum of its ads" is an INTER-SERIES identity (compare the campaign series total to the
--     adgroup series total for the same project/date/metric), asserted by
--     test_tiktok_grain_series_reconcile.sql -- it is NOT an intra-series sum here.
--   * Marts needing a day total pick a canonical single series via MIN(breakdown_dimension);
--     ordered dims 'ad_id' < 'adgroup_id' < 'campaign_id', so MIN selects the finest grain
--     PRESENT -- NEVER two grains summed together (proven by test_tiktok_min_breakdown_stable
--     .sql). No top-N-bounded partition is emitted here (each grain series is a FULL
--     reconciliation of the day total -- unlike the GA4 landing_page/acquisition top-N blocks).
{% set tiktok_series = [
    ("campaign_id", "AUCTION_CAMPAIGN"),
    ("adgroup_id", "AUCTION_ADGROUP"),
    ("ad_id", "AUCTION_AD"),
] %}
{% set tiktok_metrics = ["cost", "impressions", "clicks", "conversions"] %}
{% for metric in tiktok_metrics %}
{% for dimension, data_level in tiktok_series %}
SELECT
    project_id,
    date,
    'tiktok-ads'        AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    {% if metric == 'cost' %}
    SUM({{ fx_convert_at_read('cost') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_tiktok_ads_daily') }}
-- review-15-2 F-1: this series reads ONLY its own report grain (data_level) so grains
-- never bleed. The IS NOT NULL guard still filters any stray NULL id defensively.
WHERE data_level = '{{ data_level }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- tiktok-ads: END Story 15.2 block.
{% endif %}
{% if toorow_model_present('stg_klaviyo_campaigns_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- klaviyo: BEGIN Story 15.8 (Epic 15) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Email/SMS marketing -- source de metriques d'attribution canal email.
-- metrics: sends, opens, clicks, attributed_conversions, attributed_revenue (all ADDITIVE, AD-4).
-- DEUX sous-modeles de staging : campagnes (breakdown campaign_id) et flows (breakdown flow_id).
--
-- REGLE DE NON-AGREGATION (CRITIQUE, AD-4, Story 15.8) :
--   attributed_conversions et attributed_revenue sont des metriques REVENDIQUEES par le canal
--   Klaviyo (fenetre last-touch 5j clic/ouverture email par defaut, configurable par compte --
--   AI-53 verifie le 2026-07-19). ILS NE SONT JAMAIS SOMMES avec :
--     * les conversions des regies (connector='meta-ads' / 'tiktok-ads' / 'gsc' metric='conversions')
--       dans cross_source_conversions ou un total marketing agrege ;
--     * le revenue Shopify (connector='shopify' metric='revenue') ou Stripe dans un total croise.
--   Usage legitime : alimentation de la deduplication (Epic 17, contribution canal email) et
--   comparaison canal par canal (pas de somme marketing totale sans la regle AD-4).
--   Reconciliation "totaux GA4/Meta/TikTok/GSC/Shopify existants INCHANGES" prouvee par :
--   test_klaviyo_totals_isolated.sql + test_klaviyo_attributed_not_in_cross_source.sql.
--
-- DOUBLE-COUNT SAFETY (MIN(breakdown_dimension), DESIGN 2) :
--   Klaviyo emet DEUX breakdown_dimensions : 'campaign_id' et 'flow_id'. Ces deux series sont
--   MUTUELLEMENT EXCLUSIVES par construction (une ligne raw a soit campaign_id soit flow_id,
--   jamais les deux) : les campagnes ponctuelles et les flows automatises sont des entites
--   distinctes dans la Reporting API Klaviyo -- une ligne ne peut pas avoir a la fois un
--   campaign_id ET un flow_id. Il ne s'agit donc PAS de deux vues du meme total que l'on
--   additionne via MIN ; MIN(breakdown_dimension) selectionne 'campaign_id' (< 'flow_id'
--   lexicographiquement) comme dimension canonique POUR LA SERIE CAMPAGNE uniquement.
--   Un total canal Klaviyo COMPLET doit UNIONER les deux dimensions independamment
--   (ou filtrer par l'une ou l'autre) -- sommer 'campaign_id' seul sous-compterait les flows.
--   Le mart emet deux blocs UNION ALL distincts (stg_klaviyo_campaigns_daily et
--   stg_klaviyo_flows_daily) precisement parce qu'ils sont mutuellement exclusifs et
--   additifs sur le total canal email, jamais en double-count l'un par rapport a l'autre.
--   Les autres connecteurs ne sont pas affectes : connector='klaviyo' est une cle unique dans
--   fact_daily_kpi et ne touche pas les partitions GA4/Meta/TikTok/GSC/Shopify.
--
-- AI-53 VERIFIE le 2026-07-19 : endpoints POST /api/campaign-values-reports/ et
-- POST /api/flow-series-reports/ de la Reporting API Klaviyo. Noms de champs (campaign_id,
-- flow_id, sends, opens, clicks) et metriques attributees (conversions -> attributed_conversions,
-- revenue -> attributed_revenue) declares d'apres la doc officielle :
-- https://developers.klaviyo.com/en/reference/reporting_api_overview
-- A confirmer en passe live (Phase B, AI-13).
--
-- NULL HONNETE (AD-9) : les metriques absentes dans la reponse API Klaviyo restent NULL dans
-- le staging. Le mart n'emet PAS de ligne pour un agregat entierement NULL (HAVING ... IS NOT
-- NULL) : l'ABSENCE de ligne = "Klaviyo n'a pas retourne cette metrique" ; un zero reel reste
-- present comme 0.0. Contrat mart : fact_daily_kpi.value est NOT NULL (test schema central) --
-- jamais un NULL deguise en 0, jamais un 0 invente pour une metrique manquante.

{% set klaviyo_metrics = ["sends", "opens", "clicks", "attributed_conversions", "attributed_revenue"] %}

{% for metric in klaviyo_metrics %}
SELECT
    project_id,
    date,
    'klaviyo'           AS connector,
    '{{ metric }}'      AS metric,
    'campaign_id'       AS breakdown_dimension,
    campaign_id         AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_klaviyo_campaigns_daily') }}
WHERE campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL)
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_klaviyo_flows_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

{% for metric in klaviyo_metrics %}
SELECT
    project_id,
    date,
    'klaviyo'           AS connector,
    '{{ metric }}'      AS metric,
    'flow_id'           AS breakdown_dimension,
    flow_id             AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_klaviyo_flows_daily') }}
WHERE flow_id IS NOT NULL
GROUP BY project_id, date, flow_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL)
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- klaviyo: END Story 15.8 block.
{% endif %}
{% if toorow_model_present('stg_linkedin_ads_campaign_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- linkedin-ads: BEGIN Story 15.3 (Epic 15) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Acquisition B2B paid social -- source de metriques de campagne LinkedIn.
-- Deux grains distincts : campaign_daily (pivot CAMPAIGN) et campaign_group_daily
-- (pivot CAMPAIGN_GROUP). Chaque grain est une serie INDEPENDANTE dans le mart.
--
-- CONVERSIONS DEDUP (AD-4, Rule P 3.7, lecon review-15-2 F-1 CRITIQUE) :
--   Les conversions LinkedIn (externalWebsiteConversions, renommees 'conversions')
--   sont REVENDIQUEES par le canal (fenetre d'attribution 30j clic / 7j vue par
--   defaut -- AI-53 verifie le 2026-07-19). JAMAIS sommees avec GA4/Meta/TikTok/GSC
--   sans la regle de dedup declarative (cross_source_conversions, Rule P). Le mart
--   garde ces lignes SEPAREES distinguees par connector='linkedin-ads'.
--   Reconciliation "totaux GA4/Meta/TikTok/GSC/Shopify/Klaviyo existants INCHANGES"
--   prouvee par test_linkedin_totals_isolated.sql et
--   test_linkedin_conversions_isolated_from_dedup.sql.
--
-- LEADS : metrique DISTINCTE (leadGenerationMailContactInfoShares = Lead Gen Form
--   in-app LinkedIn). Elle est emise comme metrique 'leads' SEPAREE de 'conversions'.
--   Elle NE MAPPE PAS sur la metrique canonique 'conversions' et n'entre JAMAIS dans
--   cross_source_conversions. La relation leads <-> conversions est documentee dans
--   le manifest.
--
-- DOUBLE-COUNT SAFETY (lecon review-15-2 F-1, data_level-scoped series) :
--   Chaque serie ci-dessous lit UNIQUEMENT les lignes de son propre pivot (data_level) :
--   - la serie campaign_id lit UNIQUEMENT data_level='CAMPAIGN' (stg_linkedin_ads_campaign_daily)
--   - la serie campaign_group_id lit UNIQUEMENT data_level='CAMPAIGN_GROUP' (stg_linkedin...)
--   Sommer les deux series sur la meme date serait un double-compte (les campaign_group
--   sont des roll-ups de leurs campagnes). Le mart ne les somme JAMAIS ; chaque serie
--   est une vue independante de la hierarchie LinkedIn.
--   MIN(breakdown_dimension) pour linkedin-ads :
--     'campaign_group_id' < 'campaign_id' (alphabetiquement 'campaign_g' < 'campaign_i').
--   Donc MIN selectionne la serie campaign_group quand les deux coexistent. Cette
--   selection canonique est DOCUMENTEE ICI : le consommateur qui veut un total
--   journalier LinkedIn doit choisir UN grain (campaign OU campaign_group), pas les deux.
--   La serie 'leads' n'est emise qu'au grain campaign (campaign_group retourne NULL pour
--   leads, aucune ligne emise pour ce metric/grain si leads=NULL).
--
-- AI-51 (Epic 8) : country>device composite DELIBEREMENT non emis pour linkedin-ads.
--   stg_linkedin_ads_campaign_daily ne porte ni country ni device -- le pull adAnalytics
--   est au grain de la hierarchie de campagne, pas un breakdown geo/device. Per AI-51
--   "if a module lacks one of the pair, SKIP it -- do not invent data".

{% set linkedin_metrics = ["cost", "impressions", "clicks", "conversions"] %}

{% for metric in linkedin_metrics %}
SELECT
    project_id,
    date,
    'linkedin-ads'       AS connector,
    '{{ metric }}'       AS metric,
    'campaign_id'        AS breakdown_dimension,
    campaign_id          AS breakdown_value,
    {% if metric == 'cost' %}
    SUM({{ fx_convert_at_read('cost') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)         AS pull_id,
    MAX(loaded_at)       AS loaded_at
FROM {{ ref('stg_linkedin_ads_campaign_daily') }}
WHERE campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (valeur absente != zero reel).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_linkedin_ads_campaign_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Metrique 'leads' (Lead Gen Form) -- emise UNIQUEMENT au grain campaign.
SELECT
    project_id,
    date,
    'linkedin-ads'       AS connector,
    'leads'              AS metric,
    'campaign_id'        AS breakdown_dimension,
    campaign_id          AS breakdown_value,
    SUM(CAST(leads AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)         AS pull_id,
    MAX(loaded_at)       AS loaded_at
FROM {{ ref('stg_linkedin_ads_campaign_daily') }}
WHERE campaign_id IS NOT NULL
  AND leads IS NOT NULL
GROUP BY project_id, date, campaign_id
HAVING SUM(CAST(leads AS {{ toorow_float_type() }})) IS NOT NULL
{% endif %}
{% if toorow_model_present('stg_linkedin_ads_campaign_group_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Serie campaign_group : roll-up LinkedIn (data_level=CAMPAIGN_GROUP).
-- NOTE : leads non emis ici (NULL au grain campaign_group -- cf. note staging).
{% for metric in linkedin_metrics %}
SELECT
    project_id,
    date,
    'linkedin-ads'          AS connector,
    '{{ metric }}'          AS metric,
    'campaign_group_id'     AS breakdown_dimension,
    campaign_group_id       AS breakdown_value,
    {% if metric == 'cost' %}
    SUM({{ fx_convert_at_read('cost') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)            AS pull_id,
    MAX(loaded_at)          AS loaded_at
FROM {{ ref('stg_linkedin_ads_campaign_group_daily') }}
WHERE campaign_group_id IS NOT NULL
GROUP BY project_id, date, campaign_group_id
-- AD-9: agregat entierement NULL -> pas de ligne.
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- linkedin-ads: END Story 15.3 block.
{% endif %}
{% if toorow_model_present('stg_stripe_payments_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- stripe: BEGIN Story 15.7 (Epic 15) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Source de verite REVENUS SaaS/services (pendant de Shopify pour les business sans boutique).
-- metrics: revenue, refunds, fees, transaction_count, order_count (all ADDITIVE at day grain, AD-4).
-- Le mart AGGREGE les charges au JOUR : breakdown_dimension = 'day_total' (une seule serie
-- day-total, pas de breakdown geo/device -- le pull Charges est au grain charge, pas un breakdown).
-- charge_id / payment_intent_id / client_reference_id sont des dimensions de DETAIL du raw/staging
-- (identifiants de transaction + cle GA4-joignable QUAND presente, Epic 17) -- DELIBEREMENT PAS
-- emis comme partitions du mart (une partition par charge exploserait le grain et n'est ce dont
-- aucun consommateur day-total a besoin ; meme discipline que transaction_id de Shopify 15.4).
--
-- DOUBLE-COUNT SAFETY : Stripe contribue une SEULE breakdown_dimension ('day_total') par metrique,
-- donc aucun risque de double-compte multi-partition intra-connecteur (contrairement aux series
-- paralleles GA4 country/device). C'est UNE serie parallele de plus, cle par connector='stripe' ;
-- fact_daily_kpi cle sur connector, donc elle n'entre jamais en collision avec une partition GA4/
-- Meta/GSC/Shopify/TikTok/LinkedIn. MIN(breakdown_dimension) pour stripe est trivialement 'day_total'.
--
-- REFUNDS / FEES (decision de story 15.7, meme discipline que refund_amount de Shopify 15.4) :
-- refunds et fees sont des lignes-metriques DEDIEES positives -- JAMAIS soustraites silencieusement
-- de la ligne revenue. Le net (revenue - refunds - fees) se calcule EXPLICITEMENT au niveau
-- semantique/carte, gardant les trois flux independamment auditables (AD-9 no-black-box). Prouve par
-- test_stripe_refunds_fees_not_netted.sql.
--
-- REGLE DE DEDUP REVENUE (CRITIQUE, AD-4, Rule P 3.7) : revenue est desormais emis par DEUX
-- connecteurs (shopify 15.4 ET stripe 15.7). Un paiement Stripe qui regle une commande Shopify
-- mesure la MEME vente -- revenue Stripe et revenue Shopify ne sont JAMAIS sommes dans un total
-- croise. La regle DECLARATIVE (metric_source_priority.csv : revenue -> shopify priorite 1, stripe
-- priorite 2) + la vue cross_source_revenue.sql (miroir EXACT de cross_source_conversions 3.7)
-- choisissent UNE source gagnante par (projet, jour). Les lignes par source restent
-- independamment interrogeables dans fact_daily_kpi (WHERE metric='revenue', connector distinct).
-- JUSTIFICATION du choix priorite (vs non-agregation klaviyo) : revenue Stripe et revenue Shopify
-- sont la MEME grandeur canonique (des ventes en euros), PAS une vue d'attribution canal comme
-- attributed_revenue Klaviyo -- la bonne regle est donc la priorite de source, comme conversions
-- GA4/Meta/TikTok 3.7. Prouve non-tautologiquement par test_stripe_revenue_dedup_vs_shopify.sql
-- (les deux sources chevauchent le meme jour dans le seed correlate ; cross_source_revenue < somme).
--
-- transaction_count / order_count NE SONT PAS mappes sur 'conversions' : un paiement Stripe est une
-- VENTE, pas une conversion regie -- pas de collision avec la regle 3.7 GA4/Meta (comme orders_count
-- Shopify 15.4). Reconciliation "totaux GA4/Meta/GSC/Shopify/TikTok/Klaviyo/LinkedIn existants
-- INCHANGES" prouvee par test_stripe_totals_isolated.sql.
{% set stripe_metrics = ["revenue", "refunds", "fees", "transaction_count", "order_count"] %}
{% for metric in stripe_metrics %}
SELECT
    project_id,
    date,
    'stripe'            AS connector,
    '{{ metric }}'      AS metric,
    'day_total'         AS breakdown_dimension,
    'all'               AS breakdown_value,
    {% if metric in ["revenue", "refunds", "fees"] %}
    SUM({{ fx_convert_at_read(metric) }}) AS value,
    {{ money_evidence_present(metric ~ '_source_value', 'revenue_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_stripe_payments_daily') }}
GROUP BY project_id, date
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- stripe: END Story 15.7 block.
{% endif %}
{% if toorow_model_present('stg_square_payments_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- square: BEGIN block -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Source de verite REVENUS POS/omnichannel (pendant Square de Stripe 15.7 / Shopify 15.4).
-- metrics: revenue, refunds, fees, transaction_count, order_count (all ADDITIVE at day grain, AD-4).
-- Le mart AGGREGE les payments au JOUR : breakdown_dimension = 'day_total' (une seule serie
-- day-total, pas de breakdown geo/device -- le pull Payments est au grain payment, pas un breakdown).
-- payment_id / order_id / location_id sont des dimensions de DETAIL du raw/staging (identifiants de
-- transaction + cle Orders-API joignable + emplacement topologie) -- DELIBEREMENT PAS emis comme
-- partitions du mart (une partition par payment exploserait le grain ; meme discipline que
-- transaction_id de Shopify 15.4 et charge_id de Stripe 15.7).
--
-- DOUBLE-COUNT SAFETY : Square contribue une SEULE breakdown_dimension ('day_total') par metrique,
-- donc aucun risque de double-compte multi-partition intra-connecteur. C'est UNE serie parallele de
-- plus, cle par connector='square' ; fact_daily_kpi cle sur connector, donc elle n'entre jamais en
-- collision avec une partition GA4/Meta/GSC/Shopify/Stripe/TikTok/LinkedIn.
--
-- REFUNDS \ FEES (meme discipline que Stripe 15.7 / Shopify 15.4) : refunds et fees sont des
-- lignes-metriques DEDIEES positives -- JAMAIS soustraites silencieusement de la ligne revenue.
-- Le net (revenue - refunds - fees) se calcule EXPLICITEMENT au niveau semantique (AD-9 no-black-box).
--
-- REGLE DE DEDUP REVENUE (CRITIQUE, AD-4, Rule P 3.7) : revenue est desormais emis par shopify (15.4),
-- stripe (15.7), adjust ET square. Un paiement Square qui regle une commande e-commerce mesure la MEME
-- vente -- revenue Square et revenue Shopify/Stripe ne sont JAMAIS sommes dans un total croise. La regle
-- DECLARATIVE (metric_source_priority.csv : revenue -> shopify 1, stripe 2, adjust 3, square 4) + la vue
-- cross_source_revenue.sql (miroir de cross_source_conversions 3.7) choisissent UNE source gagnante par
-- (projet, jour). Les lignes par source restent independamment interrogeables (WHERE metric='revenue',
-- connector distinct). Square est place en dernier (defaut conservateur) ; un marchand POS Square-seul
-- reste toujours gagnant car source unique.
--
-- transaction_count \ order_count NE SONT PAS mappes sur 'conversions' : un paiement Square est une VENTE,
-- pas une conversion regie -- pas de collision avec la regle 3.7 GA4/Meta (comme orders_count Shopify 15.4).
{% set square_metrics = ["revenue", "refunds", "fees", "transaction_count", "order_count"] %}
{% for metric in square_metrics %}
SELECT
    project_id,
    date,
    'square'            AS connector,
    '{{ metric }}'      AS metric,
    'day_total'         AS breakdown_dimension,
    'all'               AS breakdown_value,
    {% if metric in ["revenue", "refunds", "fees"] %}
    SUM({{ fx_convert_at_read(metric) }}) AS value,
    {{ money_evidence_present(metric ~ '_source_value', 'revenue_source_currency') }}
    {% else %}
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_square_payments_daily') }}
GROUP BY project_id, date
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- square: END block.
{% endif %}
{% if toorow_model_present('stg_hubspot_contacts_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- hubspot: BEGIN Story 15.5 (Epic 15) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- CRM leads/pipelines -- source de reconciliation entre les leads declares par les regies
-- et les leads/deals CRM reels. DEUX grains distincts :
--   stg_hubspot_contacts_daily  -> new_contacts (contacts crees par jour)
--   stg_hubspot_deals_daily     -> deals_created, deals_closed, deal_amount
--
-- REGLE D'ISOLATION CRM (CRITIQUE, AD-4, Story 15.5) :
--   Les metriques HubSpot CRM (new_contacts, deals_created, deals_closed, deal_amount)
--   sont des indicateurs CRM PURS. ILS NE SONT JAMAIS SOMMES avec :
--     * les conversions des regies (connector='meta-ads'/'tiktok-ads'/'gsc'/'linkedin-ads'
--       metric='conversions') dans cross_source_conversions ou un total marketing ;
--     * le revenue Shopify/Stripe (connector='shopify'/'stripe' metric='revenue') ;
--     * les contacts Klaviyo email (connector='klaviyo' metric='attributed_conversions').
--   Ces metriques CRM ont des noms DISTINCTS des metriques marketing existantes :
--   new_contacts != conversions ; deal_amount != revenue ; deals_created != orders_count.
--   Cette distinction nominale est la protection AD-4 primaire (pas de collision possible
--   dans un SUM agrege). Le manifest declare la regle '_crm_isolation_rule'.
--   Reconciliation "totaux GA4/Meta/TikTok/GSC/Shopify/Klaviyo/LinkedIn/Stripe existants
--   INCHANGES" prouvee par test_hubspot_totals_isolated.sql.
--
-- DOUBLE-COUNT SAFETY :
--   Les deux grains (contacts et deals) sont des entites DISTINCTES (contacts != deals).
--   Il n'existe pas de risque de double-compte intra-connecteur : un contact cree n'est
--   pas un deal cree. Les deux blocs UNION ALL additionnent des series independantes.
--   MIN(breakdown_dimension) pour hubspot :
--     contacts -> breakdown_dimension='date' (single dim, day_total semantics)
--     deals    -> breakdown_dimension='date' (single dim, day_total semantics)
--   Les deux series utilisent le meme breakdown_dimension='date' mais des metriques
--   differentes (new_contacts vs deals_*) donc pas de collision grain.
--   connector='hubspot' est une cle unique dans fact_daily_kpi ; les lignes hubspot
--   ne touchent jamais les partitions GA4/Meta/TikTok/GSC/Shopify/Klaviyo/LinkedIn/Stripe.
--
-- NULL HONNETE (AD-9) :
--   deal_amount peut etre NULL dans le staging (aucun deal ferme ou sans montant).
--   HAVING ... IS NOT NULL filtre les agregats entierement NULL (contrat value NOT NULL).
--
-- AI-53 VERIFIE le 2026-07-19 : HubSpot CRM v3 ne fournit pas d'agregats daily natifs.
--   Les grains journaliers sont calcules par pagination du Search API
--   (POST /crm/v3/objects/contacts/search + /deals/search, filtre BETWEEN createdate/closedate).
--   Champs : new_contacts (count), deals_created (count), deals_closed (count), deal_amount (sum).
--   Sources : https://developers.hubspot.com/docs/api/crm/contacts
--             https://developers.hubspot.com/docs/api/crm/deals
--             https://developers.hubspot.com/docs/api/crm/search
--   A confirmer en passe live (Phase B, AI-13).

-- Contacts crees par jour
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'new_contacts'      AS metric,
    'date'              AS breakdown_dimension,
    date                AS breakdown_value,
    SUM(CAST(new_contacts AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_contacts_daily') }}
WHERE new_contacts IS NOT NULL
GROUP BY project_id, date
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST(new_contacts AS {{ toorow_float_type() }})) IS NOT NULL
{% endif %}
{% if toorow_model_present('stg_hubspot_deals_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Deals crees par jour
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'deals_created'     AS metric,
    'date'              AS breakdown_dimension,
    date                AS breakdown_value,
    SUM(CAST(deals_created AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_deals_daily') }}
WHERE deals_created IS NOT NULL
GROUP BY project_id, date
HAVING SUM(CAST(deals_created AS {{ toorow_float_type() }})) IS NOT NULL
{% endif %}
{% if toorow_model_present('stg_hubspot_deals_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Deals fermes/gagnes par jour
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'deals_closed'      AS metric,
    'date'              AS breakdown_dimension,
    date                AS breakdown_value,
    SUM(CAST(deals_closed AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_deals_daily') }}
WHERE deals_closed IS NOT NULL
GROUP BY project_id, date
HAVING SUM(CAST(deals_closed AS {{ toorow_float_type() }})) IS NOT NULL
{% endif %}
{% if toorow_model_present('stg_hubspot_deals_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Montant des deals fermes par jour (NULL honnete AD-9 : HAVING filtre les NULL)
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'deal_amount'       AS metric,
    'currency'          AS breakdown_dimension,
    currency            AS breakdown_value,
    SUM(CAST(deal_amount AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_deals_daily') }}
WHERE deal_amount IS NOT NULL AND currency IS NOT NULL
GROUP BY project_id, date, currency
-- AD-9: une journee sans deal ferme avec montant -> pas de ligne deal_amount.
HAVING SUM(CAST(deal_amount AS {{ toorow_float_type() }})) IS NOT NULL
-- hubspot: END Story 15.5 block.
{% endif %}
{% if toorow_model_present('stg_google_sheets_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- google-sheets: BEGIN Story 15.6 (Epic 15) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Source de saisie manuelle d'objectifs et de budgets declares dans Google Sheets.
-- metrics: budget_declared, target_revenue, target_conversions (all ADDITIVE at day grain, AD-4).
-- Le mart AGREGE par (date, sheet_row_id) : breakdown_dimension = 'sheet_row_id'.
-- sheet_row_id identifie la ligne de la feuille (valeur de la colonne row_id_column du
-- column_mapping, ou 'row_N' si non declare). Il distingue plusieurs canaux/campagnes
-- dans la meme feuille pour le meme jour.
--
-- CARACTERISTIQUE SHEETS (mapping declaratif) :
--   Le mapping colonnes->metriques est DECLARE PAR DATASTREAM (config), pas encode dans
--   le module. Le staging stg_google_sheets_daily materialise les colonnes canoniques
--   (budget_declared / target_revenue / target_conversions) depuis la feuille brute.
--   Seules les metriques configurees dans le column_mapping du datastream apparaissent
--   ici ; les colonnes non mappees ne sont jamais emises (zero invention AD-9).
--
-- CES METRIQUES SONT DES OBJECTIFS -- PAS DES REALISATIONS (ISOLATION IMPORTANTE) :
--   budget_declared / target_revenue / target_conversions sont des valeurs de PLAN
--   saisies manuellement. Elles ne participent PAS a cross_source_conversions (la regle
--   de deduplication des CONVERSIONS REVENDIQUEES des regies). Elles ne se sommant JAMAIS
--   avec les realisations GA4/Meta/TikTok/GSC. L'usage legitime est la comparaison
--   Plan vs Reel au niveau carte/semantic (un objectif budget se compare au spend reel,
--   pas au revenue reel -- semantiques distinctes).
--
-- DOUBLE-COUNT SAFETY :
--   Google Sheets contribue une SEULE dimension ('sheet_row_id') par metrique.
--   connector='google-sheets' est une cle unique dans fact_daily_kpi -- pas de collision
--   avec les partitions existantes. MIN(breakdown_dimension) pour google-sheets est
--   trivialement 'sheet_row_id'.
--
-- NULL HONNETE (AD-9) : les metriques absentes dans la feuille restent NULL dans
-- le staging. Le mart n'emet PAS de ligne pour un agregat entierement NULL (HAVING ...
-- IS NOT NULL) : l'ABSENCE de ligne = "aucun objectif declare pour ce grain" ;
-- un zero reel reste present comme 0.0. Contrat mart : fact_daily_kpi.value est NOT NULL.
--
-- AUTH (AD-21) : la connexion google-sheets utilise auth_path='google_direct' dans
-- app.connection_ref. La facade get_fresh_token route automatiquement sur token_service.
-- Le scope 'spreadsheets.readonly' fait partie du GOOGLE_STACK_SCOPES de l'Epic 18.
-- LIVE OAuth = BLOCKED Phase B (AI-08).
--
-- AI-53 VERIFIE le 2026-07-19 : Google Sheets API v4, quota 300 req/min par projet,
-- 60 req/min par utilisateur. Une extraction = 1 appel batchGet. Tres confortable pour
-- la saisie d'objectifs. Source : https://developers.google.com/sheets/api/limits.
-- Reconciliation "totaux GA4/Meta/TikTok/GSC/Shopify/LinkedIn/Klaviyo/Stripe/HubSpot
-- existants INCHANGES" garantie par la cle connector='google-sheets' unique dans le mart
-- et le test test_google_sheets_totals_isolated.sql.

{% set gsheets_metrics = ["budget_declared", "target_revenue", "target_conversions"] %}

{% for metric in gsheets_metrics %}
SELECT
    project_id,
    date,
    'google-sheets'     AS connector,
    '{{ metric }}'      AS metric,
    'sheet_row_id'      AS breakdown_dimension,
    sheet_row_id        AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_google_sheets_daily') }}
WHERE sheet_row_id IS NOT NULL
GROUP BY project_id, date, sheet_row_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- google-sheets: END Story 15.6 block.
{% endif %}
{% if toorow_model_present('stg_ias_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- ias: BEGIN Integral Ad Science block -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Ad verification / media-quality measurement (viewability, invalid traffic / IVT,
-- brand safety & suitability). ALL landed metrics are ADDITIVE impression/ad counts
-- (AD-4): measured/viewable/eligible impressions, invalid_traffic_ads, brand_safety
-- passed/failed ads. IAS RATES ARE NEVER STORED -- they are recomputed downstream
-- from these counts (a viewability rate is viewable_impressions / measured_impressions
-- at view time), never summed.
--
-- Grain: campaign_id per day (stg_ias_daily). breakdown_dimension='campaign_id'.
--
-- METRIC NAMESPACE ISOLATION (AD-4): IAS metric names are DISTINCT from every
-- régie metric -- measured_impressions / viewable_impressions / eligible_impressions
-- / invalid_traffic_ads / brand_safety_passed_ads / brand_safety_failed_ads collide
-- with NONE of the canonical 'impressions'/'clicks'/'conversions'/'cost' metrics.
-- They measure AD QUALITY, not delivery volume, so no cross-source dedup rule
-- applies and they never enter cross_source_conversions.
--
-- DOUBLE-COUNT SAFETY: IAS contributes a SINGLE breakdown_dimension ('campaign_id')
-- per metric, so there is no intra-connector multi-partition double-count risk.
-- connector='ias' is a unique key in fact_daily_kpi; these rows never collide with
-- any GA4/Meta/GSC/Shopify/TikTok/LinkedIn/Klaviyo/Stripe/HubSpot/Sheets partition.
-- MIN(breakdown_dimension) for ias is trivially 'campaign_id'.
--
-- NULL HONNETE (AD-9): HAVING ... IS NOT NULL drops an entirely-NULL aggregate
-- (absent metric != real zero; contract value NOT NULL).
--
-- LIVE (AI-13): public_catalog.verification stays 'blocked' until an IAS Signal
-- account connects; the request/field contract is proven by scripts/ratify_connector.py
-- (see server/modules/ias/ROLLOUT_NOTES.md).
{% set ias_metrics = ["measured_impressions", "viewable_impressions", "eligible_impressions", "invalid_traffic_ads", "brand_safety_passed_ads", "brand_safety_failed_ads"] %}
{% for metric in ias_metrics %}
SELECT
    project_id,
    date,
    'ias'               AS connector,
    '{{ metric }}'      AS metric,
    'campaign_id'       AS breakdown_dimension,
    campaign_id         AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_ias_daily') }}
WHERE campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- ias: END Integral Ad Science block.
{% endif %}
{% if toorow_model_present('stg_adjust_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- adjust: BEGIN (module adjust, kit epic-25) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Mobile measurement (Report Service API) -- grain staging (date, app_token, network, campaign_id).
-- metrics: cost, installs, clicks, impressions, sessions, revenue, ad_revenue, all_revenue
-- (all ADDITIVE at day grain, AD-4). Ratios (ctr, *_rate) ne sont JAMAIS stockes.
--
-- TROIS series paralleles par metrique (breakdown app_token / campaign_id / network), chacune
-- construite depuis les MEMES lignes staging (grain unique network_daily -- pas de data_level :
-- un seul grain de rapport existe, contrairement a meta/tiktok multi-grains, donc aucun risque
-- de bleed inter-grain). Chaque serie est une reconciliation COMPLETE du total jour (aucun
-- top-N) ; les marts qui veulent un total jour prennent MIN(breakdown_dimension) = 'app_token'
-- (ordre lexicographique 'app_token' < 'campaign_id' < 'network'), JAMAIS deux series sommees
-- ensemble (lecon 1.6).
--
-- REGLE AD-4 (revenue, miroir stripe 15.7) : le revenue Adjust (mesure SDK in-app) et le
-- revenue Shopify/Stripe peuvent mesurer des ventes liees. Ils ne sont JAMAIS sommes dans un
-- total croise : metric_source_priority.csv porte revenue -> shopify 1, stripe 2, adjust 3 ;
-- cross_source_revenue choisit UNE source gagnante par (projet, jour). Les lignes par source
-- restent interrogeables independamment (connector='adjust' est une cle unique du mart).
--
-- REGLE AD-4 (sessions) : 'sessions' Adjust (app, SDK) et 'sessions' GA4 (web analytics)
-- mesurent des proprietes DIFFERENTES ; fact_daily_kpi les separe par connector et elles ne
-- sont jamais sommees en un total croise (meme discipline que klaviyo attributed_*).
--
-- NULL HONNETE (AD-9) : agregat entierement NULL -> pas de ligne (HAVING ... IS NOT NULL),
-- jamais un faux 0 (contrat mart : fact_daily_kpi.value NOT NULL). Un zero reel reste 0.0.
-- Reconciliation "totaux des connecteurs existants INCHANGES" garantie par la cle
-- connector='adjust' unique dans le mart.
{% set adjust_series = ["app_token", "campaign_id", "network"] %}
{% set adjust_metrics = ["cost", "installs", "clicks", "impressions", "sessions", "revenue", "ad_revenue", "all_revenue"] %}
{% for metric in adjust_metrics %}
{% for dimension in adjust_series %}
SELECT
    project_id,
    date,
    'adjust'            AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_adjust_daily') }}
WHERE {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- adjust: END block.
{% endif %}
{% if toorow_model_present('stg_cm360_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- cm360: BEGIN Story 33-3 (Epic 33) -- BLOC STRICTEMENT ADDITIF (ne pas reformater au-dessus).
-- Campaign Manager 360 v5 display/video delivery -- source of record for managed display.
-- Additive physical metrics only (AD-4): impressions, clicks, cost, conversions,
-- conversion_value. Non-additive reach metrics (unique_reach, average_frequency) are
-- DELIBERATELY EXCLUDED via the WHERE non_additive = FALSE guard below.
--
-- GRAIN: stg_cm360_daily carries THREE report profiles (standard_daily, floodlight_daily,
-- reach) in one staging view, differentiated by the report_profile column. The non_additive
-- flag is set at landing for reach rows (unique_reach / average_frequency = TRUE); all
-- standard_daily and floodlight_daily rows have non_additive = FALSE. The WHERE guard
-- is therefore the exact selector for "additive-only delivery data" across all profiles
-- with no extra profile filter required.
--
-- CONVERSIONS DEDUP (AD-4, Rule P 3.7): CM360 'conversions' (dfa:totalConversions) are
-- CLAIMED by the channel (Floodlight attribution window, configurable per advertiser --
-- verification:blocked Phase B). They are kept as SEPARATE rows keyed by
-- connector='cm360'; they are NEVER summed with GA4/Meta/TikTok/GSC 'conversions' without
-- the declarative dedup (cross_source_conversions, Rule P).
--
-- NON-ADDITIVE GUARD (CRITICAL, F-GRAIN-2 twin): unique_reach and average_frequency
-- are declared non_additive=TRUE at landing (connector.py:358) and tested in
-- test_reach_and_frequency_are_declared_non_additive. The WHERE non_additive = FALSE
-- guard here is the mart-side enforcement. test_projection_additive_only.sql guards that
-- 'unique_reach' and 'average_frequency' never appear as stored metric names in this table
-- (F-GRAIN-2 fix adds them to that test's IN list).
--
-- BREAKDOWN: campaign_id is the finest available grain in stg_cm360_daily that is always
-- populated across profiles. Non-NULL guard filters the rare case of NULL campaign rows.
--
-- DOUBLE-COUNT SAFETY: CM360 emits a SINGLE breakdown_dimension ('campaign_id') per
-- metric. connector='cm360' is a unique key in fact_daily_kpi; these rows never collide
-- with any GA4/Meta/GSC/Shopify/TikTok/LinkedIn/Klaviyo/Stripe/HubSpot/IAS/Adjust
-- partition. MIN(breakdown_dimension) for cm360 is trivially 'campaign_id'.
--
-- NULL HONNETE (AD-9): agregat entierement NULL -> pas de ligne (HAVING ... IS NOT NULL),
-- jamais un faux 0 (contrat mart: fact_daily_kpi.value NOT NULL).
--
-- LIVE (AI-13): verification:blocked until a CM360 advertiser account connects (no test
-- account available 2026-07-21). The field contract is proven by test_connector.py.
{% set cm360_metrics = ["impressions", "clicks", "cost", "conversions", "conversion_value"] %}
{% for metric in cm360_metrics %}
SELECT
    project_id,
    date,
    'cm360'             AS connector,
    '{{ metric }}'      AS metric,
    'campaign_id'       AS breakdown_dimension,
    campaign_id         AS breakdown_value,
    -- BUGFIX 2026-07-27 (pre-existing, unrelated to the epic that surfaced it):
    -- stg_cm360_daily is LONG format (columns `metric` / `value`), so the previous
    -- `SUM(CAST({{ metric }} AS DOUBLE))` referenced columns that do not exist and
    -- raised `Binder Error: Referenced column "impressions" not found in FROM clause!`,
    -- which made the WHOLE fact_daily_kpi model unbuildable. It had never been caught
    -- because cm360 ships no seed fixtures, so stg_cm360_daily always failed earlier in
    -- the DAG and this block was never reached. Unpivot instead.
    SUM(CASE WHEN metric = '{{ metric }}' THEN CAST(value AS {{ toorow_float_type() }}) END) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_cm360_daily') }}
-- F-GRAIN-1 fix: non_additive = FALSE excludes unique_reach and average_frequency reach rows.
-- Only standard_daily and floodlight_daily additive rows are projected to the canonical fact.
WHERE non_additive = FALSE
  AND campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
-- With the unpivot this also means a metric absent for a campaign-day emits NO row,
-- rather than a fabricated 0 -- the same honesty contract as before.
HAVING SUM(CASE WHEN metric = '{{ metric }}' THEN CAST(value AS {{ toorow_float_type() }}) END) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- cm360: END Story 33-3 block.
{% endif %}
{% if toorow_model_present('stg_instagram_insights_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Instagram Insights: only additive account-day profile views enter the shared
-- daily fact. Unique reach and per-media cumulative engagement stay in the
-- dedicated fact_instagram_insights_snapshot model.
SELECT
    project_id,
    date,
    'instagram-insights' AS connector,
    'profile_views'      AS metric,
    'account_id'         AS breakdown_dimension,
    account_id           AS breakdown_value,
    SUM(CASE WHEN metric = 'profile_views' THEN CAST(value AS {{ toorow_float_type() }}) END) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)         AS pull_id,
    MAX(loaded_at)       AS loaded_at
FROM {{ ref('stg_instagram_insights_daily') }}
WHERE report_profile = 'account_daily'
  AND non_additive = FALSE
  AND account_id IS NOT NULL
GROUP BY project_id, date, account_id
HAVING SUM(CASE WHEN metric = 'profile_views' THEN CAST(value AS {{ toorow_float_type() }}) END) IS NOT NULL
{% endif %}
{% if toorow_model_present('stg_google_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Google Ads (AI-270) -- the connector was TERMINAL: nothing referenced
-- `stg_google_ads_daily`, so a Project could connect Google Ads, pull
-- successfully, and see no figure anywhere. Its own MCP read path already reads
-- this mart (`connector.py:10`) -- it was written for a table it never reached.
--
-- LONG LANDING, so each series filters `metric = '<name>'` and sums `value_num`.
-- The other ad connectors land WIDE (one column per metric); this one cannot,
-- because the catalog_daily profile may select any of 278 metrics and wide
-- columns cannot hold an arbitrary selection.
--
-- ONE SERIES PER REPORT GRAIN, exactly as meta-ads does and for the same
-- measured reason: before that filter, a campaign_id series summed BOTH the
-- campaign-grain row AND the campaign_id carried by every ad-group- and
-- ad-grain row, so a campaign was double-counted inside its own series. Each
-- series reads only its own `data_level`, so no grain bleeds into another.
-- A mart needing a day total picks a canonical single series via
-- MIN(breakdown_dimension) -- 'ad_group_id' < 'ad_id' < 'campaign_id' -- never
-- two grains summed together.
--
-- ADDITIVE METRICS ONLY (AD-4). The seven canonical names come from the
-- manifest's `canonical_metric_mapping`, which is what the connector's
-- transform() renames to before landing (`_canonical_metric_names`). Every one
-- of them is a count or an amount. Provider-computed RATIOS (ctr, average_cpc,
-- ...) also land through catalog_daily and are deliberately absent here: summing
-- a ratio over a day is the defect AD-4 names, and their honest form is
-- recomputed at the semantic layer from stored numerators and denominators.
--
-- KEYWORD and SEARCH_TERM data levels also land. They are not series here: their
-- breakdown is the keyword text, not an id, and neither the fact's
-- `breakdown_value` contract nor any surface asks for it today. Naming them and
-- leaving them out is the point -- a reader can see the decision instead of
-- wondering whether the grain was forgotten.
{% set gads_series = [
    ("campaign_id", "CAMPAIGN"),
    ("ad_group_id", "AD_GROUP"),
    ("ad_id", "AD"),
] %}
{% set gads_metrics = [
    "cost", "impressions", "clicks", "conversions",
    "conversions_value", "all_conversions", "view_through_conversions",
] %}
{#- The two monetary ones. `conversions_value` is revenue in the SAME account
    billing currency as cost -- `cost_source_currency` names that currency for
    the row whatever the metric, so both convert through it. -#}
{% set gads_money = ["cost", "conversions_value"] %}
{% for metric in gads_metrics %}
{% for dimension, data_level in gads_series %}
SELECT
    project_id,
    date,
    'google-ads'        AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    {% if metric in gads_money %}
    SUM({{ fx_convert_at_read('value_num') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST(value_num AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_google_ads_daily') }}
WHERE metric = '{{ metric }}'
  AND data_level = '{{ data_level }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_microsoft_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Microsoft Ads (AI-270) -- terminal for the same reason as google-ads, and
-- repaired the same way: nothing referenced `stg_microsoft_ads_daily`, so the
-- connector pulled and produced no figure a person could see.
--
-- LONG landing, so each series filters `metric` and sums `value_num`. ONE series
-- per report grain (`data_level`), so a campaign is never counted twice inside
-- its own series -- the measured defect meta-ads documents.
--
-- FIVE canonical metrics, all additive, from the manifest's
-- `canonical_metric_mapping`. `revenue` is monetary alongside `cost`: both are
-- amounts in the account billing currency that `cost_source_currency` names.
--
-- THE OTHER FIVE data levels land and are NOT series here: ACCOUNT has no
-- breakdown id of its own, KEYWORD and SEARCH_QUERY break down by text rather
-- than an id, and GEOGRAPHIC and AGE_GENDER are segment axes carried in
-- `segments_json`, not grain keys. Named so a reader sees the decision instead
-- of wondering whether the grain was forgotten.
{% set msads_series = [
    ("campaign_id", "CAMPAIGN"),
    ("ad_group_id", "AD_GROUP"),
    ("ad_id", "AD"),
] %}
{% set msads_metrics = ["cost", "revenue", "impressions", "clicks", "conversions"] %}
{% set msads_money = ["cost", "revenue"] %}
{% for metric in msads_metrics %}
{% for dimension, data_level in msads_series %}
SELECT
    project_id,
    date,
    'microsoft-ads'     AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    {% if metric in msads_money %}
    SUM({{ fx_convert_at_read('value_num') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST(value_num AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_microsoft_ads_daily') }}
WHERE metric = '{{ metric }}'
  AND data_level = '{{ data_level }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_doubleverify_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- DoubleVerify (AI-270) -- terminal, and the cheapest of the seventeen to
-- repair: its staging ALREADY carries the fact's own shape
-- (metric / breakdown_dimension / breakdown_value), so this block renames
-- nothing and invents no axis. It only sums and stamps the connector.
--
-- Ten metrics, all COUNTS of ads or impressions -- additive by construction.
-- Verification counters have no money at all, so every money column is NULL and
-- `money_gap_code` is NULL too: nothing to convert is not a gap.
{% set dv_metrics = [
    "monitored_ads", "measured_impressions", "eligible_impressions",
    "authentic_ads", "brand_suitable_ads", "brand_suitability_incidents",
    "fraud_sivt_free_ads", "fraud_sivt_incidents",
    "viewable_impressions", "video_viewable_impressions",
] %}
SELECT
    project_id,
    date,
    'doubleverify'      AS connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_doubleverify_daily') }}
WHERE metric IN ({% for m in dv_metrics %}'{{ m }}'{% if not loop.last %}, {% endif %}{% endfor %})
  AND breakdown_dimension IS NOT NULL
  AND breakdown_value IS NOT NULL
GROUP BY project_id, date, metric, breakdown_dimension, breakdown_value
{% endif %}
{% if toorow_model_present('stg_piano_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Piano Analytics (AI-270) -- terminal. LONG landing, broken down by site.
--
-- ONLY THE ADDITIVE HALF, and the staging says which: `visits`,
-- `unique_visitors` and `bounce_rate` carry `non_additive = TRUE` because they
-- are VISIT- or VISITOR-scoped -- a breakdown sum is NOT the site total, which
-- is Piano's own documented rule. Summing a visitor count across segments counts
-- the same person several times.
--
-- The filter is on the FLAG, never on a list of names kept here: a metric that
-- becomes non-additive upstream is then excluded without anyone remembering to
-- edit this file. `page_loads` and `events` are plain counts and pass it.
SELECT
    project_id,
    date,
    'piano'             AS connector,
    metric,
    'site_id'           AS breakdown_dimension,
    site_id             AS breakdown_value,
    SUM(CAST(value_num AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_piano_daily') }}
WHERE non_additive = FALSE
  AND site_id IS NOT NULL
GROUP BY project_id, date, metric, site_id
{% endif %}
{% if toorow_model_present('stg_google_ad_manager_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Google Ad Manager (AI-270) -- terminal. Its staging already carried the fact's
-- shape (metric / breakdown_dimension / breakdown_value), so this block sums and
-- stamps; the only work was the UNIT, done at staging where the seed prescribes.
--
-- `ad_revenue` is monetary and arrives DECIMAL here -- `stg_google_ad_manager_daily`
-- divides the GAM micros by 1e6, because `dbt/seeds/money_metric_units.csv`
-- declares `ad_revenue,decimal` and adjust already emits that canonical name in
-- decimal. Two units under one name in one fact is a total nobody can read.
--
-- `impressions` and `clicks` are counts: every money column is NULL for them.
{% set gam_money = ["ad_revenue"] %}
{% for metric in ["ad_revenue", "impressions", "clicks"] %}
SELECT
    project_id,
    date,
    'google-ad-manager' AS connector,
    '{{ metric }}'      AS metric,
    breakdown_dimension,
    breakdown_value,
    {% if metric in gam_money %}
    SUM({{ fx_convert_at_read('value') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_google_ad_manager_daily') }}
WHERE metric = '{{ metric }}'
  AND breakdown_dimension IS NOT NULL
  AND breakdown_value IS NOT NULL
GROUP BY project_id, date, breakdown_dimension, breakdown_value
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_amazon_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Amazon Ads (AI-270) -- terminal, and the ONE of the seventeen that must NOT
-- copy the google-ads template. Its `data_level` is not a report GRAIN, it is an
-- ad PROGRAM: spCampaigns / sbCampaigns / sdCampaigns -- Sponsored Products,
-- Brands and Display. All three are at CAMPAIGN grain and carry DISJOINT
-- campaigns, so summing across them is CORRECT here, the exact opposite of
-- google-ads where one data_level per series is what prevents a double count.
-- Filtering one program per series would have dropped two thirds of the spend in
-- silence.
--
-- The `IN` list is written out rather than left open, and that is the guard:
-- every profile amazon-ads exposes today is campaign-grain, but a future
-- ad-group profile would land under its own data_level and must NOT join the
-- campaign series by default.
--
-- THREE ATTRIBUTION WINDOWS travel as SEPARATE metrics -- `purchases` beside
-- `purchases_14d` and `purchases_30d`, `sales` beside its two. Each is its own
-- series and they are never added together: they measure the same conversions
-- counted over different lookbacks, so their sum means nothing.
{% set amz_levels = ["spCampaigns", "sbCampaigns", "sdCampaigns"] %}
{% set amz_metrics = [
    "cost", "sales", "sales_14d", "sales_30d",
    "impressions", "viewable_impressions", "clicks",
    "purchases", "purchases_14d", "purchases_30d", "units_sold",
] %}
{% set amz_money = ["cost", "sales", "sales_14d", "sales_30d"] %}
{% for metric in amz_metrics %}
SELECT
    project_id,
    date,
    'amazon-ads'        AS connector,
    '{{ metric }}'      AS metric,
    'campaign_id'       AS breakdown_dimension,
    campaign_id         AS breakdown_value,
    {% if metric in amz_money %}
    SUM({{ fx_convert_at_read('value_num') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST(value_num AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_amazon_ads_daily') }}
WHERE metric = '{{ metric }}'
  AND data_level IN ({% for lvl in amz_levels %}'{{ lvl }}'{% if not loop.last %}, {% endif %}{% endfor %})
  AND campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_pinterest_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Pinterest Ads (AI-270) -- terminal. LONG landing, one series per report grain
-- (`data_level`), same anti-double-count discipline as google-ads.
--
-- COUNTS ONLY, AND THE REASON IS STRUCTURAL. `cost` and `checkout_value` land
-- and are NOT emitted here: neither `raw_pinterest_ads_daily` nor its staging
-- carries a currency column -- measured 2026-08-16, the raw table has sixteen
-- columns and none of them names one. A money row whose currency can never be
-- known would publish an amount that no reporting-currency total may ever
-- include, and `money_gap_code` would say `native_currency_missing` on every row
-- forever. That is not a disclosure, it is noise.
--
-- The repair belongs at the LANDING (the pull must capture the ad account's
-- billing currency), exactly like brevo's canonical names. Named here so the
-- absence is a decision a reader can see, not a grain someone forgot.
{% set pin_series = [
    ("campaign_id", "CAMPAIGN"),
    ("ad_group_id", "AD_GROUP"),
    ("ad_id", "AD"),
] %}
{% set pin_metrics = ["impressions", "clicks", "engagements", "conversions", "checkouts"] %}
{% for metric in pin_metrics %}
{% for dimension, data_level in pin_series %}
SELECT
    project_id,
    date,
    'pinterest-ads'     AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    SUM(CAST(value_num AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_pinterest_ads_daily') }}
WHERE metric = '{{ metric }}'
  AND data_level = '{{ data_level }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_linkedin_company_pages_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- LinkedIn company pages (AI-270) -- terminal. Organic page analytics: no money
-- at all, so every money column is NULL.
--
-- THE DATE IS `interval_start`, because this connector reports over an INTERVAL
-- rather than a day. Only the daily grain belongs in a DAILY fact, so a row
-- whose interval spans more than one day is excluded rather than attributed to
-- its first day -- attributing a week's impressions to a Monday would be an
-- invented figure.
--
-- `lifetime = TRUE` rows are CUMULATIVE (total followers to date, not followers
-- gained). Summing them across days would add the same followers once per day.
-- They are excluded here, and that exclusion is the reason this connector cannot
-- simply publish everything it lands.
--
-- `non_additive` is honoured too: `engagement_rate` is a ratio and never sums.
SELECT
    project_id,
    CAST(interval_start AS DATE) AS date,
    'linkedin-company-pages' AS connector,
    metric,
    'organization_urn'  AS breakdown_dimension,
    organization_urn    AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_linkedin_company_pages_daily') }}
WHERE lifetime = FALSE
  AND non_additive = FALSE
  AND organization_urn IS NOT NULL
  AND CAST(interval_end AS DATE) <= CAST(interval_start AS DATE) + INTERVAL 1 DAY
GROUP BY project_id, CAST(interval_start AS DATE), metric, organization_urn
{% endif %}
{% if toorow_model_present('stg_thetradedesk_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- The Trade Desk (AI-270) -- terminal. LONG landing, no `data_level`: TTD
-- reports one grain per template, so a series per grain dimension is enough and
-- there is no grain to filter against.
--
-- THREE COST METRICS ARE USD BY CONTRACT -- `advertiser_cost_usd`,
-- `ttd_cost_usd`, `partner_cost_usd` -- and the staging states 'USD' for them
-- rather than joining a currency column, because that IS what they are. They
-- convert once at read like every other money series.
--
-- `advertiser_cost_adv_currency` IS DELIBERATELY ABSENT. It is denominated in
-- the advertiser's currency, and that currency is written NOWHERE in the
-- landing: publishing it would put an amount in the fact whose unit nobody can
-- name, and summing it with anything would be a category error. Its repair is at
-- the landing -- the pull must record the advertiser's currency -- not here.
{% set ttd_money = ["advertiser_cost_usd", "ttd_cost_usd", "partner_cost_usd"] %}
{% set ttd_metrics = [
    "advertiser_cost_usd", "ttd_cost_usd", "partner_cost_usd",
    "impressions", "clicks", "bids",
    "total_click_conversions", "total_view_through_conversions",
] %}
{% set ttd_series = ["campaign_id", "ad_group_id"] %}
{% for metric in ttd_metrics %}
{% for dimension in ttd_series %}
SELECT
    project_id,
    date,
    'thetradedesk'      AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    {% if metric in ttd_money %}
    SUM({{ fx_convert_at_read('value_num') }}) AS value,
    {{ money_evidence_present('cost_source_value', 'cost_source_currency') }}
    {% else %}
    SUM(CAST(value_num AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_thetradedesk_daily') }}
WHERE metric = '{{ metric }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_generic_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Generic KPI datastream (AI-270) -- terminal, and the one whose whole purpose
-- was to reach this fact: its staging header says « canonical long-format KPI
-- rows », `module_kind='kpi'`, « any day>measure tabular payload grafts on with
-- zero core change ». It grafted on and then went nowhere.
--
-- THE METRIC NAMES ARE THE CUSTOMER'S, not the platform dictionary's, and that
-- is BY DESIGN here rather than the brevo defect: a generic datastream carries
-- whatever measures its source has, and their aggregation semantics are declared
-- in `target_fields`. Nothing is renamed, because there is nothing to rename to.
--
-- No money: a generic payload states no currency, so every money column is NULL.
-- A generic source that carries an amount reaches money through a governed
-- mapping, not through a guess made here.
SELECT
    project_id,
    date,
    'generic'           AS connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_generic_daily') }}
WHERE breakdown_dimension IS NOT NULL
  AND breakdown_value IS NOT NULL
GROUP BY project_id, date, metric, breakdown_dimension, breakdown_value
{% endif %}
{% if toorow_model_present('stg_bigquery_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- External BigQuery (AI-270) -- terminal. A replicated customer table, folded
-- into long format at `transform()`: one row per numeric column per breakdown
-- value per day. Same shape as the generic datastream and the same reasoning --
-- the metric names are the CUSTOMER'S columns, and renaming them to a platform
-- dictionary they were never written against would be an invention.
--
-- No money: a replicated table states no currency.
SELECT
    project_id,
    date,
    'bigquery'          AS connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_bigquery_daily') }}
WHERE breakdown_dimension IS NOT NULL
  AND breakdown_value IS NOT NULL
GROUP BY project_id, date, metric, breakdown_dimension, breakdown_value
{% endif %}
{% if toorow_model_present('stg_amazon_dsp_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- The five that landed RAW metric names (AI-270). Their staging now maps through
-- `connector_metric_names`, so what arrives here is the dictionary name -- see
-- any of the five staging models for why the repair is there and not at the
-- landing.
--
-- `non_additive` is honoured on every one of them: sa360 ships
-- `click_through_rate` and `search_impression_share`, x-ads ships
-- `engagement_rate` and `average_frequency`, adobe ships bounce rates. Summing a
-- ratio over a day is the defect AD-4 names, and the flag is what says which.

-- Amazon DSP -- broken down by advertiser. `revenue` and `cost` land WITHOUT a
-- currency column anywhere (measured: the raw table names none), so they are
-- emitted as counts of nothing rather than money: every money column is NULL and
-- no total may treat them as an amount. Same structural gap as pinterest, same
-- repair -- at the landing.
SELECT
    project_id,
    CAST(date AS DATE)  AS date,
    'amazon-dsp'        AS connector,
    metric,
    'advertiser_id'     AS breakdown_dimension,
    advertiser_id       AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_amazon_dsp_daily') }}
WHERE non_additive = FALSE
  AND advertiser_id IS NOT NULL
GROUP BY project_id, CAST(date AS DATE), metric, advertiser_id
{% endif %}
{% if toorow_model_present('stg_sa360_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- SA360 -- broken down by campaign. It carries `currency_code`, and its money is
-- already DECIMAL at landing: `metrics.cost_micros` is a NAME, and the connector
-- divides by 1e6 in transform(). Verified on landed data rather than believed
-- from the name -- 1.2, not 1 200 000. Dividing again here would have made every
-- cost a millionth of itself.
{% set sa360_money = ["cost", "conversions_value"] %}
SELECT
    project_id,
    CAST(date AS DATE)  AS date,
    'sa360'             AS connector,
    metric,
    'campaign_id'       AS breakdown_dimension,
    campaign_id         AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_sa360_daily') }}
WHERE non_additive = FALSE
  AND campaign_id IS NOT NULL
  AND metric NOT IN ({% for m in sa360_money %}'{{ m }}'{% if not loop.last %}, {% endif %}{% endfor %})
GROUP BY project_id, CAST(date AS DATE), metric, campaign_id
{% endif %}
{% if toorow_model_present('stg_adobe_analytics_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Adobe Analytics -- broken down by the report's own dimension, which travels in
-- `dimension` with its value in `item_value`. No money.
SELECT
    project_id,
    CAST(date AS DATE)  AS date,
    'adobe-analytics'   AS connector,
    metric,
    dimension           AS breakdown_dimension,
    item_value          AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_adobe_analytics_daily') }}
WHERE non_additive = FALSE
  AND partial = FALSE
  AND dimension IS NOT NULL
  AND item_value IS NOT NULL
GROUP BY project_id, CAST(date AS DATE), metric, dimension, item_value
{% endif %}
{% if toorow_model_present('stg_brevo_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Brevo -- broken down by channel (email / sms). `protected_identifier` is in
-- the supersede grain and NEVER a breakdown: it identifies a PERSON, and a mart
-- row keyed by it would publish a recipient.
SELECT
    project_id,
    CAST(date AS DATE)  AS date,
    'brevo'             AS connector,
    metric,
    'channel'           AS breakdown_dimension,
    channel             AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_brevo_daily') }}
WHERE non_additive = FALSE
  AND channel IS NOT NULL
GROUP BY project_id, CAST(date AS DATE), metric, channel
{% endif %}
{% if toorow_model_present('stg_x_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- X Ads -- the date is `interval_start`: this connector reports over an INTERVAL.
-- Only the daily grain enters a DAILY fact, so a row spanning more than one day
-- is excluded rather than attributed to its first day.
--
-- It DOES carry `currency`, but its cost is emitted without money evidence for a
-- reason worth stating: `billed_charge_local_micro` is divided by 1e6 at
-- transform(), so the value is decimal -- but nothing states which currency the
-- `local` in its name refers to per row beyond that column, and the fx join
-- belongs at staging where the other four have it. Left as a count here, and
-- named, rather than wired half-way.
SELECT
    project_id,
    CAST(interval_start AS DATE) AS date,
    'x-ads'             AS connector,
    metric,
    'entity_id'         AS breakdown_dimension,
    entity_id           AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_x_ads_daily') }}
WHERE non_additive = FALSE
  AND entity_id IS NOT NULL
  AND CAST(interval_end AS DATE) <= CAST(interval_start AS DATE) + INTERVAL 1 DAY
GROUP BY project_id, CAST(interval_start AS DATE), metric, entity_id
{% endif %}
{% if toorow_model_present('stg_youtube_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- youtube-analytics (AI-270, chantier 67-27) -- STRICTLY ADDITIVE block.
-- It was NOT terminal: `stg_youtube_daily` reached `fact_youtube_daily`, a mart
-- OF ITS OWN, so the connector-level coverage guard filed it "another mart" and
-- nobody looked again. That mart's own header states the deferral in writing --
-- "wiring these into the cross-source fact_daily_kpi is a follow-up" -- and this
-- is that follow-up. Reaching a private mart is not reaching the fact: no card
-- and no cross-source comparison reads `fact_youtube_daily`.
--
-- THE TRAP THIS BLOCK EXISTS TO AVOID, and it is invisible from the staging
-- model alone. FOUR pull profiles land in `raw_youtube_daily`, not two, and they
-- are indistinguishable once landed -- same table, same columns, and the
-- `channel_snapshot` profile carries `video = ''` exactly like `channel_daily`:
--   * channel_daily   -> views, estimated_minutes_watched, likes, comments,
--                        shares, subscribers_gained, subscribers_lost   ADDITIVE
--   * video_daily     -> views, estimated_minutes_watched, likes, comments,
--                        shares                                          ADDITIVE
--   * channel_snapshot-> subscriber_count, lifetime_view_count, video_count
--                                                              NOT ADDITIVE
-- `subscriber_count` is a STOCK and `lifetime_view_count` a running total: the
-- manifest declares both `aggregation=latest`. Summing a stock across days
-- produces a number on no scale at all (AD-4), and summing `lifetime_view_count`
-- over a week multiplies the channel's whole history by seven. `stg_youtube_daily`
-- carries NO `non_additive` column to filter on -- unlike the five connectors
-- repaired through `connector_metric_names` -- so the additive set is named
-- EXPLICITLY below. A `SELECT metric` passthrough here, which is what the
-- generic/bigquery blocks do, would have silently summed all three stocks.
-- The three stocks keep their home in `fact_youtube_daily`, which stores them
-- unaggregated -- the same treatment strava's non-additive club levels get.
--
-- NO MONEY. YouTube Analytics states no currency on any of these metrics; every
-- money evidence column is NULL and no total may treat them as an amount.
-- Metric names are ALREADY canonical at landing: `transform()` renames the keys
-- through the manifest's `canonical_metric_mapping` (estimatedMinutesWatched ->
-- estimated_minutes_watched), so no `connector_metric_names` join is needed.
--
-- DOUBLE-COUNT SAFETY (the meta-ads / tiktok data_level discipline -- and since
-- AI-310 the column IS called data_level here too): the two series below read
-- DISJOINT row sets of the same relation -- `data_level = 'CHANNEL'` is the
-- channel-grain profile, `'VIDEO'` the per-video one. Without that split the
-- channel_id series would sum the channel-grain row AND the channel_id carried by
-- every video-grain row, double-counting the channel inside its own series. Marts
-- needing a day total pick a canonical single series via MIN(breakdown_dimension):
-- 'channel_id' < 'video' ('c' < 'v'), so MIN selects the channel roll-up -- never
-- the two grains summed together. Neither series is top-N bounded: each is a full
-- reconciliation of its own day.
--
-- ONE ROW SET CHANGES, and in the direction of not losing rows: `video = ''` is
-- FALSE for a NULL video, so a row landed without one fell out of BOTH series and
-- was counted nowhere. `data_level` resolves NULL to CHANNEL (the level such a row
-- actually is), so it now reaches the channel series instead of vanishing. Every
-- other row is unchanged, and no row can reach two series: the two levels are
-- exhaustive and disjoint by construction.
--
-- OTHERWISE THE ROWS ARE THE SAME ROWS; what changed is that the split says its name.
-- It used to test `video = ''` / `video <> ''`, a convention every reader of this
-- relation had to already know -- and the connector's own MCP tool did not know
-- it, which is how a production answer came back at exactly 2x (AI-310).
{% set youtube_additive_metrics = [
    "views", "estimated_minutes_watched", "likes",
    "comments", "shares", "subscribers_gained", "subscribers_lost",
] %}
{% set youtube_series = [("channel_id", "CHANNEL"), ("video", "VIDEO")] %}
{% for dimension, youtube_data_level in youtube_series %}
SELECT
    project_id,
    CAST(date AS DATE)  AS date,
    'youtube-analytics' AS connector,
    metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_youtube_daily') }}
-- The additive set, named rather than inferred: see the stock trap above.
WHERE metric IN ({% for m in youtube_additive_metrics %}'{{ m }}'{% if not loop.last %}, {% endif %}{% endfor %})
  AND data_level = '{{ youtube_data_level }}'
  AND {{ dimension }} IS NOT NULL
GROUP BY project_id, CAST(date AS DATE), metric, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('stg_youtube_breakdown') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- youtube-analytics BREAKDOWNS (AI-342) -- the other half of the same connector.
--
-- WHAT THIS CLOSES. The block above lands the channel and per-video series, and
-- for as long as it was the whole of YouTube in this fact, a published Semantic
-- View could bind `country`, `device_type`, `traffic_source_type` and five more
-- to a Datastream whose relation carried `video` and `channel_id` and nothing
-- else. The question « views by country » compiled, resolved, ran, and answered
-- ZERO ROWS -- not a refusal, an empty answer, which reads as "no views from
-- anywhere". Measured 2026-09-01 on the reference Project: 11 dimensions bound,
-- 2 materialised.
--
-- ALREADY LONG, SO NOTHING IS PIVOTED. `stg_youtube_breakdown` lands one row per
-- (date, channel, breakdown_dimension, breakdown_value, metric) -- the exact
-- slot pair this fact keys on -- so the branch is a SUM over the reported cells,
-- never a column-per-dimension widening. The sum is what turns the cells of a
-- two-dimension profile into that profile's marginals: `audience_device` reports
-- the device x operating-system cross, and summing over `breakdown_value` gives
-- `device_type=MOBILE` its whole day instead of one of its operating systems.
--
-- ONE DATE PER ROW. `stg_youtube_breakdown.date` is a day, the reports take no
-- hourly dimension, and `CAST(date AS DATE)` is the same coercion the daily
-- block makes -- so no row of this branch carries a time of day.
--
-- THE MEASUREMENT GRAIN IS THE BREAKDOWN, AND THE FACT SAYS IT ROW BY ROW.
-- `views` cut by country and `views` cut by device are the SAME measure at two
-- grains and are NOT summable together: adding them counts every view twice.
-- That is epic 71's point, and this fact expresses it the way it has always
-- expressed it -- `breakdown_dimension` on the row -- so a reader that sums
-- across dimensions is summing across declared grains and can be caught doing
-- it. Nothing here mixes two.
--
-- DOUBLE-COUNT SAFETY. Each dimension is one more PARALLEL series that
-- independently totals the day (proved on the fixture: geography, device,
-- traffic source, playback location and subscription each sum to the channel
-- roll-up of the same day, per metric). Marts needing a day total pick a
-- canonical single series via MIN(breakdown_dimension), and across the eight
-- YouTube dimensions 'channel_id' still sorts first -- 'ch' < 'co' < 'de' <
-- 'op' < 'pl' < 'su' < 'tr' < 'vi' -- so the canonical selection is UNCHANGED by
-- this branch. Guarded by `test_youtube_breakdown_reconciles.sql`.
--
-- THE ADDITIVE SET IS NAMED, for the same reason it is named in the block above:
-- `stg_youtube_breakdown` carries no `non_additive` column, and it carries a
-- SHARE. `audience_demographics` reports `viewer_percentage` alone -- the
-- manifest declares it `aggregation=latest`, `non_additive=true` -- so summing
-- the age/gender split would produce percentages adding to several hundred. The
-- two age/gender dimensions therefore reach NO row of this fact, and that is a
-- statement about the metric they carry, not about the dimensions: the day a
-- demographic report serves `views`, the same filter lets it in with no edit.
--
-- NO MONEY. YouTube Analytics states no currency on any of these metrics.
{% set youtube_breakdown_metrics = ["views", "estimated_minutes_watched"] %}
SELECT
    project_id,
    CAST(date AS DATE)  AS date,
    'youtube-analytics' AS connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_youtube_breakdown') }}
WHERE metric IN (
    {%- for m in youtube_breakdown_metrics %}'{{ m }}'{% if not loop.last %}, {% endif %}{% endfor -%}
)
  -- A cell the source reported under no value is not a breakdown of anything,
  -- and `breakdown_value` is NOT NULL on this fact. The country lookup is the
  -- one that can produce it: `normalize_dimension` answers NULL for a code the
  -- shipped vocabulary does not resolve, and the raw spelling stays readable in
  -- `country_source` for the data-quality evidence to name.
  AND breakdown_value IS NOT NULL
  AND breakdown_value <> ''
GROUP BY project_id, CAST(date AS DATE), metric, breakdown_dimension, breakdown_value
{% endif %}
{% if toorow_model_present('stg_gbp_location_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- google-business-profile (AI-270, chantier 67-27) -- STRICTLY ADDITIVE block.
-- The twin of the youtube case above, and filed the same way: `stg_gbp_location_daily`
-- reached `fact_gbp_location_daily`, a mart OF ITS OWN, so the connector-level
-- coverage guard said "another mart" and the connector looked covered while no
-- card could read it. That mart's header states the deferral in writing -- "wiring
-- these metrics into the cross-source long-format fact_daily_kpi is a follow-up,
-- deliberately deferred" -- and this is that follow-up.
--
-- WIDE -> LONG. Unlike youtube, this staging model is WIDE: one column per metric,
-- one row per (project_id, date, location_id). The fact is long-format, so the 11
-- columns are unpivoted here by the same Jinja loop the GA4 and shopify blocks use.
-- All 11 are ADDITIVE integer daily counts -- `source_capabilities.fields` declares
-- every one `aggregation: "sum"`, `non_additive: false` -- so SUM is honest at day
-- grain for the whole set, and no metric needs excluding (the youtube stock trap has
-- no equivalent here).
--
-- `total_impressions` IS DELIBERATELY NOT EMITTED. It is the sum of the four
-- `business_impressions_*` metrics and it exists as a column NOWHERE -- not in raw,
-- not in staging, not in the dedicated mart. Emitting it here would put a derived
-- figure beside its own components in the same additive fact, so any consumer
-- summing the connector's day would count those impressions twice. It stays a
-- downstream computation, which is what the dedicated mart's header already says.
--
-- NO MONEY. Google Business Profile states no currency and none of the 11 is an
-- amount (zero rows in `money_metric_units.csv`); every money evidence column is
-- NULL. Metric names are ALREADY canonical at landing -- `transform()` renames the
-- keys through the manifest's `canonical_metric_mapping` (BUSINESS_IMPRESSIONS_
-- DESKTOP_MAPS -> business_impressions_desktop_maps) -- so no
-- `connector_metric_names` join is needed.
--
-- DOUBLE-COUNT SAFETY: this connector contributes a SINGLE breakdown_dimension
-- ('location_id') per metric, so there is no intra-connector multi-partition risk
-- (unlike the GA4 country/device parallel series). It is ONE MORE parallel series
-- keyed by connector='google-business-profile'; the fact keys on connector, so it
-- never collides with any other partition. MIN(breakdown_dimension) is trivially
-- 'location_id'. No series here is top-N bounded: the API returns every location
-- asked for, so each day is a full reconciliation.
--
-- AD-9 NULL HONESTY: GBP omits a metric it has nothing to say about, and the
-- staging keeps that absence NULL rather than zero-filling. A fully-NULL aggregate
-- emits NO ROW (the HAVING below) -- absence stays distinguishable from a recorded
-- zero, and the fixture's real `business_food_orders = 0` still lands as 0.0.
{% set gbp_metrics = [
    "business_impressions_desktop_maps",
    "business_impressions_desktop_search",
    "business_impressions_mobile_maps",
    "business_impressions_mobile_search",
    "business_conversations",
    "business_direction_requests",
    "call_clicks",
    "website_clicks",
    "business_bookings",
    "business_food_orders",
    "business_food_menu_clicks",
] %}
{% for metric in gbp_metrics %}
SELECT
    project_id,
    CAST(date AS DATE)          AS date,
    'google-business-profile'   AS connector,
    '{{ metric }}'              AS metric,
    'location_id'               AS breakdown_dimension,
    location_id                 AS breakdown_value,
    SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id)                AS pull_id,
    MAX(loaded_at)              AS loaded_at
FROM {{ ref('stg_gbp_location_daily') }}
WHERE location_id IS NOT NULL
GROUP BY project_id, CAST(date AS DATE), location_id
-- AD-9: a wholly-NULL aggregate produces no row (never a fabricated 0).
HAVING SUM(CAST({{ metric }} AS {{ toorow_float_type() }})) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_model_present('int_country_daily_kpi') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Country capability partitions are isolated so their staging-to-mart contract
-- can be built and tested without provisioning every unrelated connector.
SELECT * FROM {{ ref('int_country_daily_kpi') }}
{% endif %}

{#- ------------------------------------------------------------------------
  MANAGED FEED (story 69.2) -- les faits qu'un fichier apporte entrent ici.

  CE QUE CETTE BRANCHE FERME. L'audit du 2026-08-20 (R1) : la donnee d'un
  managed feed atterrissait, se dedoublonnait (69.1) et s'arretait la. Aucun
  rapport, aucune alerte, aucun insight ne la voyait, parce que le mart
  canonique n'avait pas de branche pour elle. Les fichiers de l'operateur
  vivaient a cote du produit.

  POURQUOI ELLE EST GENEREE ET NON ECRITE. Les 52 autres branches connaissent
  leurs colonnes : elles viennent d'un connecteur dont le manifeste est dans le
  depot. Un managed feed n'a que la declaration de son operateur -- son mapping
  publie. La branche est donc COMPILEE a partir de `mirror.managed_feed_grain`
  (migrations 227 et 299), qui relaie cette declaration : quelle colonne est le
  jour, lesquelles sont des axes, lesquelles sont des mesures ADDITIVES. Rien
  n'est devine ici ; en particulier, << toute colonne numerique est une mesure >>
  ferait d'un identifiant de campagne numerique une somme.

  `connector` EST LE DATASTREAM. Le mart est unique sur
  (projet, jour, connector, metrique, axe, valeur d'axe). Deux fichiers d'un
  meme projet peuvent porter la meme metrique le meme jour : les ecrire tous
  deux sous `'managed_feed'` ferait echouer `fact_daily_kpi_grain_unique` -- et
  la faire passer en les additionnant serait pire, car deux fichiers ne sont pas
  deux moities d'un meme total. Le producteur d'une ligne de fichier EST son
  Datastream, et `datastreams_dim` le nomme deja pour la console.

  CE QUI EST NOMME PLUTOT QUE TU (AD-9) : un flux sans jour dans sa maille ne
  decrit pas des faits journaliers et est exclu EN LE DISANT ; une mesure
  declaree non additive est exclue EN LA NOMMANT (AD-4 : une somme de taux n'est
  pas un petit mensonge, c'en est un grand). Les deux passent par `log()`, comme
  les exclusions du staging 69.1.

  SANS AXE, `day_total`. Un fichier dont la maille est le seul jour rend une
  serie journaliere, avec la convention deja portee par les branches shopify et
  woocommerce (`breakdown_dimension = 'day_total'`, `breakdown_value = 'all'`) --
  jamais un NULL que le test `not_null` refuserait.
------------------------------------------------------------------------- -#}
{%- set mf_streams = [] -%}
{%- set mf_without_date = [] -%}
{%- set mf_without_measure = [] -%}
{%- set mf_non_additive = [] -%}
{%- if toorow_model_present('stg_managed_feed_facts') -%}
  {%- set mf_grain_relation = adapter.get_relation(
        database=source('mirror', 'managed_feed_grain').database,
        schema=source('mirror', 'managed_feed_grain').schema,
        identifier=source('mirror', 'managed_feed_grain').identifier) -%}
  {#- Le miroir peut dater d'AVANT la migration 299 : il porte alors la maille
      sans la classification des colonnes. On INTERROGE ses colonnes plutot que
      de les supposer -- une synchro pas encore refaite n'est pas une panne, et
      un build qui casse sur `column date_column does not exist` dirait
      << l'entrepot est mort >> a la place de << le miroir est en retard >>. -#}
  {#- ET LE MARQUEUR QUE LE NOCTURNE LIT (AI-314) : la branche managed_feed est
      OMISE quand le miroir n'existe pas dans cet entrepot, et un fait bati sans
      une de ses sources ne doit pas se lire comme un fait complet. -#}
  {%- if execute and mf_grain_relation is none -%}
    {{ toorow_log_source_absent('mirror', ['managed_feed_grain']) }}
  {%- endif -%}
  {%- set mf_mirror_columns = [] -%}
  {%- if mf_grain_relation is not none -%}
    {%- for column in adapter.get_columns_in_relation(mf_grain_relation) -%}
      {%- do mf_mirror_columns.append(column.name | string | replace('"', '') | lower) -%}
    {%- endfor -%}
  {%- endif -%}
  {%- set mf_classified = 'measure_columns' in mf_mirror_columns -%}
  {%- if execute and mf_grain_relation is not none and not mf_classified -%}
    {{ log("fact_daily_kpi: mirror.managed_feed_grain precede la migration 299 "
          "(pas de classification des colonnes) -- branche managed_feed omise, "
          "resynchroniser le miroir", info=true) }}
  {%- endif -%}
  {%- if execute and mf_grain_relation is not none and mf_classified -%}
    {%- set mf_rows = run_query(
          "SELECT datastream_id, has_grain, date_column, dimension_columns, "
          "measure_columns, non_additive_columns FROM " ~ mf_grain_relation
        ) -%}
    {%- for row in mf_rows.rows -%}
      {%- if row['has_grain'] -%}
        {%- set refused = fromjson(row['non_additive_columns'] | string) -%}
        {%- for name in refused -%}
          {%- do mf_non_additive.append(row['datastream_id'] ~ '.' ~ name) -%}
        {%- endfor -%}
        {%- set measures = fromjson(row['measure_columns'] | string) -%}
        {%- if not row['date_column'] -%}
          {%- do mf_without_date.append(row['datastream_id'] | string) -%}
        {%- elif measures | length == 0 -%}
          {%- do mf_without_measure.append(row['datastream_id'] | string) -%}
        {%- else -%}
          {%- do mf_streams.append({
                'datastream_id': row['datastream_id'] | string,
                'date': row['date_column'] | string,
                'dimensions': fromjson(row['dimension_columns'] | string),
                'measures': measures,
              }) -%}
        {%- endif -%}
      {%- endif -%}
    {%- endfor -%}
  {%- endif -%}
{%- endif -%}
{%- if mf_without_date | length > 0 -%}
  {{ log("fact_daily_kpi: managed feeds sans jour dans leur maille, exclus: "
        ~ (mf_without_date | join(', ')), info=true) }}
{%- endif -%}
{%- if mf_without_measure | length > 0 -%}
  {{ log("fact_daily_kpi: managed feeds sans mesure additive publiee, exclus: "
        ~ (mf_without_measure | join(', ')), info=true) }}
{%- endif -%}
{%- if mf_non_additive | length > 0 -%}
  {{ log("fact_daily_kpi: mesures non additives refusees (AD-4), jamais stockees: "
        ~ (mf_non_additive | join(', ')), info=true) }}
{%- endif -%}
{% if mf_streams | length > 0 %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}
{#- LE SEPARATEUR EST ECRIT PAR LA SECONDE BRANCHE, comme dans tout ce fichier
    (`ns.emitted`). Un `loop.last` sur trois boucles imbriquees demanderait
    `loop.parent`, que l'environnement Jinja de dbt n'expose pas -- et un
    separateur pose apres la derniere branche est du SQL invalide. Un drapeau
    local repond a la seule question qui compte : << a-t-on deja emis ? >>. -#}
{%- set mf = namespace(emitted=false) -%}
{%- for stream in mf_streams %}
{%- set axes = stream['dimensions'] if stream['dimensions'] | length > 0 else [none] %}
{%- for measure in stream['measures'] %}
{%- for axis in axes %}
{% if mf.emitted %}UNION ALL{% endif %}{% set mf.emitted = true %}
SELECT
    project_id,
    CAST({{ stream['date'] }} AS DATE)  AS date,
    '{{ stream['datastream_id'] }}'     AS connector,
    '{{ measure }}'                     AS metric,
    {%- if axis is none %}
    'day_total'                         AS breakdown_dimension,
    'all'                               AS breakdown_value,
    {%- else %}
    '{{ axis }}'                        AS breakdown_dimension,
    CAST({{ axis }} AS {{ toorow_string_type() }})         AS breakdown_value,
    {%- endif %}
    SUM(CAST({{ measure }} AS {{ toorow_float_type() }}))  AS value,
    {{ money_evidence_absent() }}
    -- AD-7 : la provenance d'un fichier est son EXECUTION, l'analogue exact du
    -- `pull_id` d'un connecteur (un dse_<ULID> monotone). Elle voyage dans la
    -- colonne `pull_id` parce que c'est la colonne de provenance du mart, et
    -- qu'une seconde colonne serait un second endroit ou la chercher.
    MAX(execution_id)                   AS pull_id,
    MAX(loaded_at)                      AS loaded_at
FROM {{ ref('stg_managed_feed_facts') }}
WHERE datastream_id = '{{ stream['datastream_id'] }}'
  AND {{ stream['date'] }} IS NOT NULL
  -- Le mart exige un `loaded_at` non nul sur TOUTES ses branches. Une ligne
  -- dont l'execution est inconnue du miroir (synchro en retard) ne peut pas
  -- dire quand elle a ete chargee : elle est exclue plutot que datee au
  -- hasard, et elle reviendra a la prochaine synchro. AD-9.
  AND loaded_at IS NOT NULL
GROUP BY project_id, CAST({{ stream['date'] }} AS DATE)
    {%- if axis is not none %}, CAST({{ axis }} AS {{ toorow_string_type() }}){% endif %}
-- AD-9 : un agregat entierement NULL ne produit pas de ligne (jamais un 0
-- fabrique).
HAVING SUM(CAST({{ measure }} AS {{ toorow_float_type() }})) IS NOT NULL
{%- endfor %}
{%- endfor %}
{%- endfor %}
{% endif %}

{% if not ns.emitted %}
{#- NOT DECORATION. With no branch emitted the body would be empty, and an empty
    model is a SQL ERROR rather than an empty table -- which would say "the
    warehouse is broken" about a Project whose only truth is "it has landed
    nothing yet". Those two must never be the same answer. -#}
SELECT
    CAST(NULL AS {{ dbt.type_string() }})   AS project_id,
    CAST(NULL AS DATE)      AS date,
    CAST(NULL AS {{ dbt.type_string() }})   AS connector,
    CAST(NULL AS {{ dbt.type_string() }})   AS metric,
    CAST(NULL AS {{ dbt.type_string() }})   AS breakdown_dimension,
    CAST(NULL AS {{ dbt.type_string() }})   AS breakdown_value,
    CAST(NULL AS {{ dbt.type_float() }})    AS value,
    {{ money_evidence_absent() }}
    CAST(NULL AS {{ dbt.type_string() }})   AS pull_id,
    CAST(NULL AS {{ dbt.type_timestamp() }}) AS loaded_at
-- PORTABLE EMPTY SET: `WHERE FALSE` needs something to filter. BigQuery refuses
-- a WHERE on a query with no FROM -- `Query without FROM clause cannot have a
-- WHERE clause` -- while DuckDB accepts it, so this branch built green locally
-- and could not compile on the engine production runs. Measured 2026-08-24 by
-- replaying the nightly build of a project whose sources are absent, which is
-- the only case that reaches this branch. `FROM (SELECT 1)` gives the filter a
-- row to reject, and both engines accept it.
FROM (SELECT 1) AS _empty
WHERE FALSE
{% endif %}
