{#
  fact_daily_kpi_asof(as_of_ts) — as-of query macro (Story 4.6, AC2).

  Returns the fact_daily_kpi rows that were current at as_of_ts:
    - Only rows WHERE loaded_at <= as_of_ts (pulls known at that time)
    - Latest pull within that window wins (ROW_NUMBER() ORDER BY loaded_at DESC)

  This is the "QUALIFY latest-wins" logic from the current mart, but scoped to
  loaded_at <= as_of_ts instead of at "now".

  Source: fact_daily_kpi_all_pulls — the ALL-PULLS model that retains every
  historical pull version (raw, no QUALIFY dedup). This is required because:
    - stg_ga4_standard_daily and stg_meta_ads_daily apply QUALIFY at materialisation
      (they only retain the current-view row per grain).
    - fact_daily_kpi mart uses GROUP BY + MAX(loaded_at) — only current rows.
  See dbt/models/marts/fact_daily_kpi_all_pulls.sql for the source model.

  PARTITION BY must include ALL grain columns — missing one causes wrong results
  (epic-3-retrospective L-1 lesson: grain incompleteness bug).

  NOTE — HG-8 (do NOT invoke from Python):
  This macro is for documentation and dbt-level reproducibility only.
  Runtime as-of queries use server/core/warehouse.py get_daily_report_asof(),
  which implements the same SQL for DuckDB and BigQuery with parameterized bindings.
  Jinja string interpolation of as_of_ts here would be a SQL injection risk if
  called at runtime — the Python function uses parameterized ? placeholders.
#}
{%- macro fact_daily_kpi_asof(as_of_ts) -%}
SELECT
    project_id,
    date,
    connector,
    metric,
    breakdown_dimension,
    breakdown_value,
    value,
    pull_id,
    loaded_at
FROM (
    SELECT
        *,
        ROW_NUMBER() OVER (
            PARTITION BY project_id, date, connector, metric,
                         breakdown_dimension, breakdown_value
            ORDER BY loaded_at DESC  -- latest load within the as-of window wins
        ) AS _rn
    FROM {{ ref('fact_daily_kpi_all_pulls') }}
    WHERE loaded_at <= CAST('{{ as_of_ts }}' AS TIMESTAMP)
) kpi_snapshot
WHERE _rn = 1
{%- endmacro -%}
