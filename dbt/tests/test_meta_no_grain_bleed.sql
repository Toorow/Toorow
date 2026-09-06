-- test_meta_no_grain_bleed.sql — review-15-9 F-1 (anti-regression, EXACT tiktok pattern).
--
-- Direct proof that each Meta mart breakdown series is built ONLY from its own report
-- grain (data_level). This is the test that FAILS under the OLD schema (grains mixed in
-- one raw table with no data_level column, so `WHERE campaign_id IS NOT NULL` swept up the
-- campaign_id carried by adset/creative rows and inflated the campaign_id series).
--
-- For each (project_id, date, metric), the mart value of a breakdown series MUST equal the
-- staging total of the MATCHING data_level only:
--   campaign_id series  == SUM(stg WHERE data_level='CAMPAIGN')
--   adset_id    series  == SUM(stg WHERE data_level='ADSET')
--   ad_id       series  == SUM(stg WHERE data_level='CREATIVE')
-- Under the old (bleeding) mart the campaign_id series would be STRICTLY GREATER than the
-- CAMPAIGN staging total whenever adset/creative grains coexisted -> this test returns
-- rows (FAIL). With the data_level filter the two match exactly (zero rows = pass).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.001 relative.

{% set series = [
    ("campaign_id", "CAMPAIGN"),
    ("adset_id", "ADSET"),
    ("ad_id", "CREATIVE"),
] %}
{% set metrics = [
    ("cost", "cost"),
    ("impressions", "impressions"),
    ("clicks", "clicks"),
    ("conversions", "conversions"),
] %}

WITH mart_series AS (
    SELECT project_id, date, metric, breakdown_dimension, SUM(value) AS mart_total
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'meta-ads'
    GROUP BY project_id, date, metric, breakdown_dimension
),

stg_series AS (
    {% for dimension, data_level in series %}
    {% for mart_metric, stg_col in metrics %}
    SELECT
        project_id,
        date,
        '{{ mart_metric }}'  AS metric,
        '{{ dimension }}'    AS breakdown_dimension,
        -- The mart applies `fx_convert_at_read` to `cost` (Story 48.3); this side
        -- must apply the SAME expression or the comparison measures the exchange
        -- rate instead of the grain. Measured 2026-08-04 before the repair:
        -- mart 339.6272 vs staging 369.16 on 90 rows -- a ratio of exactly 0.92,
        -- the USD->EUR seed rate. The grain filter was never at fault; the test
        -- was comparing a converted total to a native one and calling it a bleed.
        -- Using the macro on both sides also matches its NULL propagation, so an
        -- unconvertible row is excluded from BOTH totals rather than one.
        {%- if mart_metric == 'cost' %}
        SUM({{ fx_convert_at_read(stg_col) }}) AS stg_total
        {%- else %}
        SUM(CAST({{ stg_col }} AS {{ toorow_float_type() }})) AS stg_total
        {%- endif %}
    FROM {{ ref('stg_meta_ads_daily') }}
    WHERE data_level = '{{ data_level }}'
      AND {{ dimension }} IS NOT NULL
    GROUP BY project_id, date
    {% if not loop.last %}UNION ALL{% endif %}
    {% endfor %}
    {% if not loop.last %}UNION ALL{% endif %}
    {% endfor %}
)

SELECT
    m.project_id,
    m.date,
    m.metric,
    m.breakdown_dimension,
    m.mart_total,
    s.stg_total
FROM mart_series m
JOIN stg_series s
    ON s.project_id = m.project_id AND s.date = m.date
   AND s.metric = m.metric AND s.breakdown_dimension = m.breakdown_dimension
WHERE ABS(m.mart_total - s.stg_total) > 0.001 * NULLIF(ABS(s.stg_total), 0)
