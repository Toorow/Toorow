{% macro raw_source_schema() -%}
{#
  toorow -- resolves the SCHEMA of a raw_* dbt source (Story 24.4, AC2).

  NAMING-POINT ROLE: this macro IS the single dbt naming point for raw source
  schemas (twin of core/warehouse_tenancy.OrgSchemas.raw on the Python side).
  The T1 naming guard allows 'org_' ~ composition ONLY here and in
  generate_schema_name.sql.

  USAGE IN SCHEMA.YML (source schema: fields):
  dbt 1.11 does not evaluate custom macros in source ``schema:`` fields at parse
  time (it only resolves built-in var() calls). Therefore module schema.yml files
  use the pre-computed variable injected by the orchestrator:

    schema: "{{ var('raw_schema', 'main') }}"

  The orchestrator (scheduler.run_dbt_per_project) passes:
    --vars '{"project": "<project_id>", "raw_schema": "raw_<project_id>"}'
  where ``raw_schema`` is taken from ``warehouse_tenancy.ProjectZones.raw`` --
  the single Python naming point, itself resolved by ``bigquery_raw_dataset``,
  the function every reader calls. No schema.yml composes a raw schema itself.

  AI-166 (2026-08-17) retargeted that orchestrator from the ORG zones to the
  PROJECT zones, because the org datasets are provisioned shells holding zero
  tables while the rows sit in ``raw_<project_id>``. Under TOOROW_ORG_SCHEMAS=1
  the same resolver returns ``org_<wslug>_raw`` again, so this macro's contract
  is unchanged: it takes the value it is given.

  THIS MACRO is still the canonical definition of the contract and can be
  called from SQL models or other macros where a full Jinja context is available.

  Contract:
    * var('raw_schema') absent (default) -> ``main`` : EXACT legacy behaviour,
      staging reads ``main.raw_*``. A compile without ``--vars`` is bit-identical
      to pre-24.4.
    * var('raw_schema') present -> returned as-is (e.g. ``org_acme_raw``).
      The value is pre-sanitised by warehouse_tenancy (AD-2: no re-sanitisation).

  The ``mirror`` source is NOT routed by this macro (AD-8): it keeps its literal
  ``schema: mirror`` -- the mirror is the central control-plane mirror, shared by
  every org, never exposed per-org. Only raw_* sources use this routing.

  BigQuery parity: ``org_<wslug>_raw`` is a valid BQ dataset id (charset-safe),
  matching warehouse_tenancy on the Python side.
#}
    {%- set raw_schema = var('raw_schema', none) -%}
    {%- if raw_schema is none -%}
        {{- 'main' -}}
    {%- else -%}
        {{- raw_schema -}}
    {%- endif -%}
{%- endmacro %}
