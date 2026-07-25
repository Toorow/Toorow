-- Generic dbt test: mart pull_id must be well-formed (AD-7), source-agnostic.
--
-- Story 3.6 (FR2): the previous integrity gate was a `relationships` test to a
-- single raw table (raw_ga4_standard_daily) and therefore broke as soon as a
-- second connector (with a different raw table) landed rows in the mart. A
-- per-module central edit would be required for every new source.
--
-- Provenance is already guaranteed structurally: each module's staging model
-- selects pull_id straight from its own raw table (source()), and the mart only
-- MAX()es that value -- a mart pull_id can only originate from some module's raw
-- table. This source-agnostic well-formedness check (prefix + non-null) preserves
-- the AD-7 provenance intent without coupling the central mart to any one raw
-- table. Adding module N+1 needs NO edit here.
{% test pull_id_well_formed(model, column_name) %}
SELECT *
FROM {{ model }}
WHERE {{ column_name }} IS NULL
   OR {{ column_name }} NOT LIKE 'pull\_%' ESCAPE '\'
{% endtest %}
