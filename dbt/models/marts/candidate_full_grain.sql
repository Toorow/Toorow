-- candidate_full_grain: the governed FULL-GRAIN relation (Story 12.4, AC1/AC3).
--
-- WHAT IT IS: a relation that preserves EVERY selected dimension at the declared
-- JOINT grain, as the analysis-side companion to fact_daily_kpi's canonical
-- projection ("two relations, one truth"). Each dimension is kept as its OWN
-- typed column -- NEVER collapsed into a fact_daily_kpi-style long-format
-- breakdown_dimension/breakdown_value pair. Every source measure is kept
-- alongside the grain (not aggregated away). A deterministic grain_key makes the
-- grain identity queryable (AI-05) and provenance columns (execution_id, pull_id,
-- mapping_version_id, plan_version_id, loaded_at) are queryable on the relation.
--
-- WHAT IT IS NOT: it is a DISTINCT artifact from fact_daily_kpi. Materializing it
-- writes NOTHING to fact_daily_kpi, mutates no existing mart total, and does not
-- touch fact_daily_kpi's grain-uniqueness test. The safe projection derives the
-- canonical fact FROM the full grain / staging via the compile-time gates in
-- server/core/datastream_projection.py (additive measures + ONE governed
-- dimension) -- it never projects the full grain directly into fact_daily_kpi
-- (that single mistake silently inflates every downstream total).
--
-- GRAIN (enforced by candidate_full_grain_grain_unique in schema.yml):
--   one row per grain_key = project_id | date | device_category | country
--   (the GA4 standard joint grain). No two rows share a grain_key: the source
--   staging is already deduplicated to this grain (QUALIFY newest pull wins), so
--   the joint grain is unique by construction -- an explicit grain-identity
--   column, never a grain-bleed (the tiktok/meta data_level lesson: multiple
--   grains in one series without a grain column = double-count).
--
-- CONCRETE INSTANTIATION (v1): this story wires the GA4 standard joint grain
-- (date x device_category x country) as the reference full-grain relation -- it
-- is derived from the SAME stg_ga4_standard_daily rows fact_daily_kpi aggregates,
-- so the two relations provably share one truth (the isolation test proves the
-- full grain adds NO fact_daily_kpi row and changes no existing total). The
-- provenance columns match server/core/warehouse.py's fact-row shape so
-- downstream rollup/provenance keeps working; execution_id is NULL here (the
-- execution/candidate registry linkage is 12.5's atomic-publication concern --
-- Open Question 1/2) and is carried as an explicit, honest NULL column, never a
-- fabricated value (AD-9).
--
-- AD-8: dbt is the ONLY writer of this mart (the core compiler validates only).
-- AD-2: source-agnostic mart vocabulary; a NEW joint grain grafts a UNION block
--       identical in shape (grain columns + grain_key + source fields +
--       provenance), never a per-provider central edit here.

-- INJECTION HARDENING (FIX 5): the three provenance vars are interpolated into
-- SQL as string literals ("'" ~ var(...) ~ "'"). A stray quote in a var could
-- break the query or inject SQL. Before interpolation each var is validated
-- against its ULID-prefixed shape (prefix + 26 Crockford base32 chars, excluding
-- I/L/O/U). A var that is ABSENT -> honest NULL (the existing behavior). A var
-- that is PRESENT but does NOT match the strict shape is ALSO coerced to NULL (it
-- can never be interpolated, so a quote cannot break or inject) -- the mismatch is
-- surfaced as a compile-time warning so a malformed provenance value is visible,
-- never silently trusted.
{%- set _ulid_body = '[0-9A-HJKMNP-TV-Z]{26}' -%}
{#- Validate each provenance var against its strict ULID-prefixed shape and
    resolve it to a bare SQL literal ('<value>') or NULL. A present-but-malformed
    var resolves to NULL (never interpolated -> a quote can't break/inject) and is
    logged so the mismatch is visible. -#}
{%- set _exec_raw = var('execution_id', none) -%}
{%- if _exec_raw is none -%}
    {%- set _execution_id_literal = 'NULL' -%}
{%- elif modules.re.match('^dse_' ~ _ulid_body ~ '$', _exec_raw | string) -%}
    {%- set _execution_id_literal = "'" ~ (_exec_raw | string) ~ "'" -%}
{%- else -%}
    {{- log("candidate_full_grain: var 'execution_id' does not match dse_<ULID>; emitting NULL (injection guard)", info=true) -}}
    {%- set _execution_id_literal = 'NULL' -%}
{%- endif -%}
{%- set _map_raw = var('mapping_version_id', none) -%}
{%- if _map_raw is none -%}
    {%- set _mapping_version_id_literal = 'NULL' -%}
{%- elif modules.re.match('^dmap_' ~ _ulid_body ~ '$', _map_raw | string) -%}
    {%- set _mapping_version_id_literal = "'" ~ (_map_raw | string) ~ "'" -%}
{%- else -%}
    {{- log("candidate_full_grain: var 'mapping_version_id' does not match dmap_<ULID>; emitting NULL (injection guard)", info=true) -}}
    {%- set _mapping_version_id_literal = 'NULL' -%}
{%- endif -%}
{%- set _plan_raw = var('plan_version_id', none) -%}
{%- if _plan_raw is none -%}
    {%- set _plan_version_id_literal = 'NULL' -%}
{%- elif modules.re.match('^dsp_' ~ _ulid_body ~ '$', _plan_raw | string) -%}
    {%- set _plan_version_id_literal = "'" ~ (_plan_raw | string) ~ "'" -%}
{%- else -%}
    {{- log("candidate_full_grain: var 'plan_version_id' does not match dsp_<ULID>; emitting NULL (injection guard)", info=true) -}}
    {%- set _plan_version_id_literal = 'NULL' -%}
{%- endif %}
SELECT
    project_id,
    'google-analytics'                            AS connector,
    -- Ordered typed grain columns (one per selected dimension), preserved as
    -- their own columns -- NOT a breakdown_dimension/breakdown_value pair.
    date,
    device_category,
    country,
    -- Deterministic grain_key: canonical '|'-joined ordered grain values. This is
    -- the AI-05 uniqueness key (candidate_full_grain_grain_unique) -- no two rows
    -- share it.
    project_id || '|' || date || '|' || device_category || '|' || country
                                                  AS grain_key,
    -- Every selected source measure kept at full grain (not aggregated away).
    CAST(sessions AS DOUBLE)                       AS sessions,
    CAST(active_users AS DOUBLE)                   AS active_users,
    CAST(conversions AS DOUBLE)                    AS conversions,
    -- Provenance columns queryable on the relation (AC1). Story 12.5 backfill:
    -- execution_id, mapping_version_id, and plan_version_id are read from dbt
    -- VARIABLES set by the publication-triggered build (a dse_<ULID> execution
    -- triggers `dbt build --select candidate_full_grain --vars '{...}'` so the
    -- three columns carry REAL provenance). Absent the variables (pure-dbt v1 / CI
    -- without a live publication context) they default to NULL -- an explicit,
    -- HONEST NULL column (present + queryable per AC1), never a fabricated value
    -- (AD-9). This is NOT a recomputation: the SAME rows are re-emitted with the
    -- provenance columns filled in; the content hash covers grain + measures ONLY,
    -- so backfilling provenance does not change it. Only pull_id and loaded_at were
    -- always real (carried from the raw load batch via stg_ga4_standard_daily).
    CAST({{ _execution_id_literal }} AS VARCHAR)
                                                  AS execution_id,
    pull_id,
    CAST({{ _mapping_version_id_literal }} AS VARCHAR)
                                                  AS mapping_version_id,
    CAST({{ _plan_version_id_literal }} AS VARCHAR)
                                                  AS plan_version_id,
    loaded_at
FROM {{ ref('stg_ga4_standard_daily') }}
