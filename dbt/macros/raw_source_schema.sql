{% macro raw_source_schema() -%}
{#
  toorow -- resolves the SCHEMA of a raw_* dbt source (Story 24.4, AC2).

  dbt sources do NOT go through generate_schema_name (that macro only names the
  schemas dbt WRITES). A source's ``schema:`` field is rendered as Jinja though,
  so every module's raw_* source declares ``schema: "{{ raw_source_schema() }}"``
  and the org routing lives HERE -- the single dbt point of naming, twin of
  core/warehouse_tenancy.OrgSchemas.raw. No schema.yml composes ``org_..._raw``
  itself (invariant epic-24; keeps the T1 naming guard's dbt allowlist == this
  file only).

  Contract:
    * var('org') ABSENT (default) -> ``main`` : EXACT legacy behaviour, the raw
      seeds land in DuckDB's ``main`` schema, staging reads ``main.raw_*``. A
      compile without ``--vars`` is bit-identical to pre-24.4.
    * var('org') PRESENT          -> ``org_<wslug>_raw`` where <wslug> = var('org')
      (already sanitised by warehouse_tenancy, AD-2: never re-sanitised here).

  The ``mirror`` source is NOT routed by this macro (AD-8): it keeps its literal
  ``schema: mirror`` -- the mirror is the central control-plane mirror, shared by
  every org, never exposed per-org. Only raw_* sources call raw_source_schema().

  BigQuery parity: the same resolution yields the ``org_<wslug>_raw`` dataset id
  (charset-safe), matching warehouse_tenancy on the Python side.
#}
    {%- set org = var('org', none) -%}
    {%- if org is none -%}
        {{- 'main' -}}
    {%- else -%}
        {{- 'org_' ~ org ~ '_raw' -}}
    {%- endif -%}
{%- endmacro %}
