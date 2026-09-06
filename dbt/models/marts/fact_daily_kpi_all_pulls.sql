{#-
  THE SAME CONDITION ITS SUPERSEDED TWIN NOW CARRIES. This model reads the raw
  landing zone directly -- it is the un-deduplicated view of every pull -- and it
  UNIONed three connectors unconditionally, so a Project that landed none of them
  could not build it. Measured 2026-08-18: `Catalog Error: Table with name
  raw_ga4_standard_daily does not exist!` on the one production Project with
  data, which reads YouTube alone.

  Guarded on the SOURCE rather than on a staging model, because that is what this
  one reads (`toorow_source_present`, dbt/macros/relation_present.sql).
-#}
{%- set ns = namespace(emitted=false) -%}
{#-
  AGGREGATED TO THE SAME GRAIN AS ITS SUPERSEDED TWIN -- added 2026-08-23 (AI-312).

  This model's own header promises "column selection matches fact_daily_kpi
  schema for query compatibility", and the columns did match while the GRAIN did
  not. Each branch below reads a RAW row, and a raw GA4 row is one
  (date, device_category, country) cell: emitting it once per requested
  breakdown produced FIFTEEN rows where `fact_daily_kpi` -- which groups -- has
  five, for the same day and the same country dimension.

  The as-of reader (`warehouse._build_asof_query`) then keeps ONE row per
  (date, connector, metric, breakdown_dimension, breakdown_value): of the three
  rows a country had, two were dropped, and the answer was a FRACTION of the
  truth. Measured on the seeded warehouse: 2026-06-01 GA4 sessions by country =
  8065 in `fact_daily_kpi` and in this relation's total, 4186 through the as-of
  path. Nothing in the repository could see it -- the as-of predicate itself
  could not execute until today, so this query had never returned a row.

  `pull_id` and `loaded_at` STAY in the grain: this is the all-pulls relation,
  and collapsing two pulls of one cell into a sum is the exact thing it exists
  not to do. What is summed is only what the raw row already split.
-#}
WITH all_pulls AS (
{% if toorow_source_present('raw_ga4', 'raw_ga4_standard_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}
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
    CAST({{ metric }} AS {{ toorow_float_type() }}) AS value,
    pull_id,
    loaded_at
FROM {{ source('raw_ga4', 'raw_ga4_standard_daily') }}
WHERE {{ metric }} IS NOT NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_source_present('raw_meta', 'raw_meta_ads_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

-- Meta Ads: all pull versions, all metrics, all breakdown dimensions.
{% set meta_metrics = ["cost", "impressions", "clicks", "conversions"] %}
-- DATA_LEVEL-SCOPED, exactly as `fact_daily_kpi` does it (its own DOUBLE-COUNT
-- SAFETY note). Meta lands three report grains in one relation; a series built
-- from ALL of them sums the campaign row AND the campaign_id carried by every
-- adset and creative row -- the campaign counted three times inside its own
-- series. This relation listed the three dimensions and no level, so the as-of
-- answer was exactly 3x the truth (measured 2026-08-23 on
-- `expert_report_meta_conversion_performance`: 7488 against a pinned 2496).
-- The pairs are the same three `fact_daily_kpi` declares, not a second opinion.
{% set meta_series = [
    ("campaign_id", "CAMPAIGN"),
    ("adset_id", "ADSET"),
    ("ad_id", "CREATIVE"),
] %}

{% for metric in meta_metrics %}
{% for dimension, data_level in meta_series %}
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
    CAST(spend AS {{ toorow_float_type() }}) AS value,
    {% else %}
    CAST({{ metric }} AS {{ toorow_float_type() }}) AS value,
    {% endif %}
    pull_id,
    loaded_at
FROM {{ source('raw_meta', 'raw_meta_ads_daily') }}
{% if metric == "cost" %}
WHERE spend IS NOT NULL
{% else %}
WHERE {{ metric }} IS NOT NULL
{% endif %}
  AND data_level = '{{ data_level }}'
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}
{% if toorow_source_present('raw_gsc', 'raw_gsc_daily') %}
{% if ns.emitted %}UNION ALL{% endif %}{% set ns.emitted = true %}

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
    CAST({{ metric }} AS {{ toorow_float_type() }}) AS value,
    pull_id,
    loaded_at
FROM {{ source('raw_gsc', 'raw_gsc_daily') }}
WHERE {{ metric }} IS NOT NULL
-- THE SAME ROW SELECTION `stg_gsc_daily` MAKES, and for its stated reasons --
-- reading the raw relation is what makes this model the all-pulls twin, not a
-- licence to count rows the twin excludes. Its three clauses, quoted from that
-- model: `query IS NULL` excludes the query_page_daily rows (several queries per
-- page, so a page is counted once per query); `search_type` web-or-null excludes
-- discover/news/image/video; `hour IS NULL` excludes ad-hoc hourly pulls, which
-- are partial days. Without them the as-of answer was 163253 impressions against
-- a pinned 141253 (measured 2026-08-23, `as_of_gsc_impressions_replay`).
  AND query IS NULL
  AND (search_type IS NULL OR search_type = 'web')
  AND hour IS NULL
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% if not loop.last %}UNION ALL{% endif %}
{% endfor %}
{% endif %}

{% if not ns.emitted %}
{#- No pull of any guarded connector landed here. An empty body is a SQL error,
    which would say "broken" about a Project whose truth is "nothing yet". -#}
SELECT
    CAST(NULL AS {{ dbt.type_string() }})   AS project_id,
    CAST(NULL AS DATE)      AS date,
    CAST(NULL AS {{ dbt.type_string() }})   AS connector,
    CAST(NULL AS {{ dbt.type_string() }})   AS metric,
    CAST(NULL AS {{ dbt.type_string() }})   AS breakdown_dimension,
    CAST(NULL AS {{ dbt.type_string() }})   AS breakdown_value,
    CAST(NULL AS {{ dbt.type_float() }})    AS value,
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
)

SELECT
    project_id,
    date,
    connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    SUM(value) AS value,
    pull_id,
    loaded_at
FROM all_pulls
GROUP BY
    project_id, date, connector, metric,
    breakdown_dimension, breakdown_value, pull_id, loaded_at
