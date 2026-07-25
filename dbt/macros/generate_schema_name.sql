{% macro generate_schema_name(custom_schema_name, node) -%}
{#
  toorow -- single dbt point of warehouse schema naming (Story 24.4, Epic 24).

  This macro is the dbt-side twin of core/warehouse_tenancy.py: it is the ONLY
  place in the whole dbt project where the ``org_<wslug>_<custom>`` composition
  lives (invariant epic-24: "aucun org_<slug> compose inline hors du point de
  nommage"). No model, no schema.yml ever concatenates an org schema name.

  Contract (AC1):
    * var('org') ABSENT (default)  -> EXACT legacy behaviour. We delegate to the
      builtin dbt.generate_schema_name so a ``dbt run`` without ``--vars`` is
      bit-identical to pre-24.4 (dbt-duckdb: main / main_staging / main_marts;
      BigQuery: target dataset / <dataset>_staging etc.). NOTHING changes.
    * var('org') PRESENT           -> ``org_<wslug>_<custom>`` where <wslug> is
      the value of var('org') passed by the orchestrator, ALREADY sanitised
      ([A-Za-z0-9_]) by warehouse_tenancy.sanitize_warehouse_slug. This macro
      does NOT re-sanitise and does NOT know any module/provider (AD-2).
      A node with NO custom schema (custom_schema_name is none) keeps the org
      RAW/shared default: it lands in ``org_<wslug>`` (bare), mirroring the
      legacy ``main`` default. Seeds are excluded below.

  Org value convention (Dev Notes piege n2): the orchestrator passes the BARE
  warehouse_slug (e.g. ``acme_corp``); the ``org_..._<custom>`` prefix/suffix is
  composed HERE only -- symmetric to warehouse_tenancy.OrgSchemas.

  Seeds stay central (piege n8): dim_* / metric_source_priority are shared
  reference tables, never per-org. dbt applies generate_schema_name to seeds too,
  so we force seeds back to the legacy (builtin) resolution regardless of var('org').
#}
    {%- set org = var('org', none) -%}
    {%- if org is none or node.resource_type == 'seed' -%}
        {{- dbt.generate_schema_name(custom_schema_name, node) -}}
    {%- elif custom_schema_name is none -%}
        {{- ('org_' ~ org) | trim -}}
    {%- else -%}
        {{- ('org_' ~ org ~ '_' ~ custom_schema_name) | trim -}}
    {%- endif -%}
{%- endmacro %}
