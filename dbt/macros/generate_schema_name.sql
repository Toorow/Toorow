{% macro generate_schema_name(custom_schema_name, node) -%}
{#
  toorow -- single dbt point of warehouse schema naming (Story 24.4, Epic 24).

  This macro is the dbt-side twin of core/warehouse_tenancy.py: it is the ONLY
  place in the whole dbt project where the ``org_<wslug>_<custom>`` composition
  lives (invariant epic-24: "aucun org_<slug> compose inline hors du point de
  nommage"). No model, no schema.yml ever concatenates an org schema name.

  AI-166 (2026-08-17) -- var('project'): the tenancy the LIVE deployment uses.
  The org zones (``org_<wslug>_*``) are provisioned shells holding zero tables;
  the rows sit in ``raw_<project_id>`` and every reader addresses
  ``marts_<project_id>`` (core/warehouse_tenancy resolvers, TOOROW_ORG_SCHEMAS
  OFF). So the nightly builder passes ``var('project')`` and this macro composes
  ``<custom>_<project_id>`` -- the exact twin of
  ``warehouse_tenancy.bigquery_marts_dataset`` / ``bigquery_staging_dataset``.
  ``var('org')`` is UNTOUCHED and still wins when both are passed, so a flip of
  TOOROW_ORG_SCHEMAS keeps its meaning.

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
    {%- set project = var('project', none) -%}
    {%- if node.resource_type == 'seed' or (org is none and project is none) -%}
        {{- dbt.generate_schema_name(custom_schema_name, node) -}}
    {%- elif org is not none -%}
        {%- if custom_schema_name is none -%}
            {{- ('org_' ~ org) | trim -}}
        {%- else -%}
            {{- ('org_' ~ org ~ '_' ~ custom_schema_name) | trim -}}
        {%- endif -%}
    {%- elif custom_schema_name is none -%}
        {#- No custom schema under the per-project tenancy: land in the project's
            RAW zone, which is where the rows already are (warehouse_write puts
            every connector landing there). The orchestrator always passes
            raw_schema; the literal below is only the parse-time default. -#}
        {{- var('raw_schema', 'main') | trim -}}
    {%- else -%}
        {{- (custom_schema_name ~ '_' ~ project) | trim -}}
    {%- endif -%}
{%- endmacro %}
