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
        SUM(CAST({{ stg_col }} AS DOUBLE)) AS stg_total
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
