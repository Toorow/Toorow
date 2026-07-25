-- fact_daily_kpi_all_pulls: ALL pull versions retained (no QUALIFY / no dedup).
-- Used as the source for as-of queries (Story 4.6, AC2b / T2.3).
--
-- WHY THIS MODEL EXISTS:
--   The staging models (stg_ga4_standard_daily, stg_meta_ads_daily) apply QUALIFY
--   ROW_NUMBER() = 1 at materialisation time, discarding superseded rows.
--   fact_daily_kpi also discards via GROUP BY + MAX(). Neither is usable for
--   as-of queries (they only have current-view rows).
--
--   This model reads directly from the RAW tables (raw_ga4_standard_daily,
--   raw_meta_ads_daily) WITHOUT any QUALIFY, retaining every pull version.
--   The as-of logic (loaded_at <= as_of_ts, ROW_NUMBER re-computed as-of) is
--   applied at QUERY TIME by server/core/warehouse.py get_daily_report_asof().
--
-- AD-12: This model reads raw_* tables intentionally — the as-of use case
--   requires the full history. It is materialized as table (not view) so that
--   DuckDB can execute the as-of window query efficiently over the full history.
--
-- HG-8: The Python runtime as-of query targets this table's name directly
--   (fact_daily_kpi_all_pulls). The dbt macro (fact_daily_kpi_asof.sql) is for
--   documentation only (see macros/fact_daily_kpi_asof.sql).
--
-- Story 4.6 — Dev Agent Record note:
--   confirmed source table for as-of query = fact_daily_kpi_all_pulls (this model).
--   Staging models + mart discard superseded rows; raw tables retain all pulls.

-- GA4: all pull versions, all metrics, both breakdown dimensions.
-- Column selection matches fact_daily_kpi schema for query compatibility.
{% set metrics = ["sessions", "active_users", "conversions"] %}
{% set dimensions = ["device_category", "country"] %}

{% for metric in metrics %}
{% for dimension in dimensions %}
SELECT
    project_id,
    date,
    'google-analytics'     AS connector,
    '{{ metric }}'         AS metric,
    '{{ dimension }}'      AS breakdown_dimension,
    {{ dimension }}        AS breakdown_value,
    CAST({{ metric }} AS DOUBLE) AS value,
    pull_id,
    loaded_at
FROM {{ source('raw_ga4', 'raw_ga4_standard_daily') }}
WHERE {{ metric }} IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

-- Meta Ads: all pull versions, all metrics, all breakdown dimensions.
{% set meta_metrics = ["cost", "impressions", "clicks", "conversions"] %}
{% set meta_dimensions = ["campaign_id", "adset_id", "ad_id"] %}

{% for metric in meta_metrics %}
{% for dimension in meta_dimensions %}
SELECT
    project_id,
    date,
    'meta-ads'          AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    {% if metric == "cost" %}
    -- cost: use raw spend (source value). As-of queries get raw spend;
    -- currency normalization is a staging concern and deferred for as-of path.
    -- TODO(AI-28): apply FX normalization via JOIN on fx_rates when the live ECB feed replaces the static seed (Phase B). Until then this model carries raw source-currency spend (USD for some connectors) while fact_daily_kpi is EUR-normalized; the 'couts en devise source' caveat in build_daily_report_summary must remain.
    CAST(spend AS DOUBLE) AS value,
    {% else %}
    CAST({{ metric }} AS DOUBLE) AS value,
    {% endif %}
    pull_id,
    loaded_at
FROM {{ source('raw_meta', 'raw_meta_ads_daily') }}
{% if metric == "cost" %}
WHERE spend IS NOT NULL
{% else %}
WHERE {{ metric }} IS NOT NULL
{% endif %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}

UNION ALL

-- GSC: all pull versions, additive metrics only (clicks, impressions).
-- AD-4: average_position is NOT additive -- excluded from this model.
-- Match fact_daily_kpi GSC block: breakdown dims page/country/device.
{% set gsc_metrics = ["clicks", "impressions"] %}
{% set gsc_dimensions = ["page", "country", "device"] %}

{% for metric in gsc_metrics %}
{% for dimension in gsc_dimensions %}
SELECT
    project_id,
    date,
    'gsc'               AS connector,
    '{{ metric }}'      AS metric,
    '{{ dimension }}'   AS breakdown_dimension,
    {{ dimension }}     AS breakdown_value,
    CAST({{ metric }} AS DOUBLE) AS value,
    pull_id,
    loaded_at
FROM {{ source('raw_gsc', 'raw_gsc_daily') }}
WHERE {{ metric }} IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
