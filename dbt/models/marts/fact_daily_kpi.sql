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
SELECT
    -- Story 2.7 AC6 HG-4: project_id from staging (not hardcoded 'default').
    -- Seeded rows have project_id='default'; real pull rows carry their own project_id.
    project_id,
    date,
    'google-analytics'     AS connector,
    '{{ metric }}'         AS metric,
    '{{ dimension }}'      AS breakdown_dimension,
    {{ dimension }}        AS breakdown_value,
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)           AS pull_id,
    MAX(loaded_at)         AS loaded_at
FROM {{ ref('stg_ga4_standard_daily') }}
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    country || '>' || device_category        AS breakdown_value,
    SUM(CAST({{ metric }} AS DOUBLE))        AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                             AS pull_id,
    MAX(loaded_at)                           AS loaded_at
FROM {{ ref('stg_ga4_standard_daily') }}
GROUP BY project_id, date, country, device_category
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_user_type_daily') }}
GROUP BY project_id, date, user_type
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_landing_daily') }}
GROUP BY project_id, date, landing_page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_paths_daily') }}
GROUP BY project_id, date, page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_acquisition_session') }}
GROUP BY project_id, date, session_campaign
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                      AS pull_id,
    MAX(loaded_at)                    AS loaded_at
FROM {{ ref('stg_ga4_acquisition_first_user') }}
GROUP BY project_id, date, first_user_source_medium
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
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

UNION ALL

-- GSC Search Analytics (Story 6.2) — additive metrics: clicks, impressions.
-- AD-4: SUM is valid for additive metrics at day grain.
-- average_position is NOT in this mart AT ALL (review-6-2 fix): a non-additive
-- metric has no honest per-dimension mart row — semantic_avg_position computes
-- the impression-weighted mean directly from staging (page-grain) rows.
{% set gsc_additive_metrics = ["clicks", "impressions"] %}
{% set gsc_dimensions = ["page", "country", "device"] %}
{% for metric in gsc_additive_metrics %}
{% for dimension in gsc_dimensions %}
SELECT
    project_id,
    date,
    'gsc'               AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_gsc_daily') }}
GROUP BY project_id, date, {{ dimension }}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    country || '>' || device           AS breakdown_value,
    SUM(CAST({{ metric }} AS DOUBLE))  AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_daily') }}
GROUP BY project_id, date, country, device
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE))  AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_query_page_daily') }}
GROUP BY project_id, date, query, page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE))  AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_surface_daily') }}
GROUP BY project_id, date, search_type
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE))  AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_surface_daily') }}
GROUP BY project_id, date, search_type, page
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE))  AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)                       AS pull_id,
    MAX(loaded_at)                     AS loaded_at
FROM {{ ref('stg_gsc_search_appearance_daily') }}
GROUP BY project_id, date, search_appearance
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_shopify_orders_daily') }}
GROUP BY project_id, date
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_woocommerce_orders_daily') }}
GROUP BY project_id, date
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- woocommerce: END block.

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
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

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_klaviyo_campaigns_daily') }}
WHERE campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL)
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

{% for metric in klaviyo_metrics %}
SELECT
    project_id,
    date,
    'klaviyo'           AS connector,
    '{{ metric }}'      AS metric,
    'flow_id'           AS breakdown_dimension,
    flow_id             AS breakdown_value,
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_klaviyo_flows_daily') }}
WHERE flow_id IS NOT NULL
GROUP BY project_id, date, flow_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL)
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- klaviyo: END Story 15.8 block.

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    {% endif %}
    MAX(pull_id)         AS pull_id,
    MAX(loaded_at)       AS loaded_at
FROM {{ ref('stg_linkedin_ads_campaign_daily') }}
WHERE campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (valeur absente != zero reel).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

-- Metrique 'leads' (Lead Gen Form) -- emise UNIQUEMENT au grain campaign.
SELECT
    project_id,
    date,
    'linkedin-ads'       AS connector,
    'leads'              AS metric,
    'campaign_id'        AS breakdown_dimension,
    campaign_id          AS breakdown_value,
    SUM(CAST(leads AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)         AS pull_id,
    MAX(loaded_at)       AS loaded_at
FROM {{ ref('stg_linkedin_ads_campaign_daily') }}
WHERE campaign_id IS NOT NULL
  AND leads IS NOT NULL
GROUP BY project_id, date, campaign_id
HAVING SUM(CAST(leads AS DOUBLE)) IS NOT NULL

UNION ALL

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
    MAX(fx_rate)            AS fx_rate,
    MAX(fx_as_of_date)      AS fx_as_of_date,
    MAX(fx_source)          AS fx_source,
    MAX(fx_tier)            AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE            AS fx_rate,
    NULL::DATE              AS fx_as_of_date,
    NULL::VARCHAR           AS fx_source,
    NULL::VARCHAR           AS fx_tier,
    {% endif %}
    MAX(pull_id)            AS pull_id,
    MAX(loaded_at)          AS loaded_at
FROM {{ ref('stg_linkedin_ads_campaign_group_daily') }}
WHERE campaign_group_id IS NOT NULL
GROUP BY project_id, date, campaign_group_id
-- AD-9: agregat entierement NULL -> pas de ligne.
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- linkedin-ads: END Story 15.3 block.

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_stripe_payments_daily') }}
GROUP BY project_id, date
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- stripe: END Story 15.7 block.

UNION ALL

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
    MAX(fx_rate)        AS fx_rate,
    MAX(fx_as_of_date)  AS fx_as_of_date,
    MAX(fx_source)      AS fx_source,
    MAX(fx_tier)        AS fx_tier,
    {% else %}
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    {% endif %}
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_square_payments_daily') }}
GROUP BY project_id, date
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- square: END block.

UNION ALL

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
    SUM(CAST(new_contacts AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_contacts_daily') }}
WHERE new_contacts IS NOT NULL
GROUP BY project_id, date
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST(new_contacts AS DOUBLE)) IS NOT NULL

UNION ALL

-- Deals crees par jour
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'deals_created'     AS metric,
    'date'              AS breakdown_dimension,
    date                AS breakdown_value,
    SUM(CAST(deals_created AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_deals_daily') }}
WHERE deals_created IS NOT NULL
GROUP BY project_id, date
HAVING SUM(CAST(deals_created AS DOUBLE)) IS NOT NULL

UNION ALL

-- Deals fermes/gagnes par jour
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'deals_closed'      AS metric,
    'date'              AS breakdown_dimension,
    date                AS breakdown_value,
    SUM(CAST(deals_closed AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_deals_daily') }}
WHERE deals_closed IS NOT NULL
GROUP BY project_id, date
HAVING SUM(CAST(deals_closed AS DOUBLE)) IS NOT NULL

UNION ALL

-- Montant des deals fermes par jour (NULL honnete AD-9 : HAVING filtre les NULL)
SELECT
    project_id,
    date,
    'hubspot'           AS connector,
    'deal_amount'       AS metric,
    'currency'          AS breakdown_dimension,
    currency            AS breakdown_value,
    SUM(CAST(deal_amount AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_hubspot_deals_daily') }}
WHERE deal_amount IS NOT NULL AND currency IS NOT NULL
GROUP BY project_id, date, currency
-- AD-9: une journee sans deal ferme avec montant -> pas de ligne deal_amount.
HAVING SUM(CAST(deal_amount AS DOUBLE)) IS NOT NULL
-- hubspot: END Story 15.5 block.

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_google_sheets_daily') }}
WHERE sheet_row_id IS NOT NULL
GROUP BY project_id, date, sheet_row_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- google-sheets: END Story 15.6 block.

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_ias_daily') }}
WHERE campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- ias: END Integral Ad Science block.

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_adjust_daily') }}
WHERE {{ dimension }} IS NOT NULL
GROUP BY project_id, date, {{ dimension }}
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- adjust: END block.

UNION ALL

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
    SUM(CAST({{ metric }} AS DOUBLE)) AS value,
    NULL::DOUBLE        AS fx_rate,
    NULL::DATE          AS fx_as_of_date,
    NULL::VARCHAR       AS fx_source,
    NULL::VARCHAR       AS fx_tier,
    MAX(pull_id)        AS pull_id,
    MAX(loaded_at)      AS loaded_at
FROM {{ ref('stg_cm360_daily') }}
-- F-GRAIN-1 fix: non_additive = FALSE excludes unique_reach and average_frequency reach rows.
-- Only standard_daily and floodlight_daily additive rows are projected to the canonical fact.
WHERE non_additive = FALSE
  AND campaign_id IS NOT NULL
GROUP BY project_id, date, campaign_id
-- AD-9: agregat entierement NULL -> pas de ligne (jamais un faux 0, contrat value NOT NULL).
HAVING SUM(CAST({{ metric }} AS DOUBLE)) IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
-- cm360: END Story 33-3 block.
