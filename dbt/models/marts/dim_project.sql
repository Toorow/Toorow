-- dim_project: project dimension materialised from the governed mirror.
-- AD-8: Postgres is the sole writer; dbt reads from mirror_* only.
-- Story 4.4 -- initially contains project_id and preference columns
--              (project_preferences as source).
-- Story 5.3 will add alert_definitions here via mirror.alert_definitions source.
-- Story 17.1: ajout des 3 colonnes source de vérification (nullable).
--   verification_source_type : 'ga4' | 'shopify' | 'stripe' | NULL (opt-in).
--   verification_source_id   : identifiant du datastream/profil source de vérité.
--   lead_event_name          : nom de l'événement lead GA4 (pertinent si type='ga4').
-- Ces colonnes sont propagées via mirror_sync.py (SELECT * FROM app.project_preferences)
-- sans modification du code mirror_sync. Le modèle 17.2 lit dim_project pour obtenir
-- la configuration de déduplication par projet.
-- NOTE 17.2 (corrigée review-17-2 F-2) : dedup_estimate JOINT ces colonnes pour
-- sélectionner la source de vérité et porter lead_event_name en PROVENANCE seulement —
-- le FILTRE par nom d'événement est BLOCKED (16.1 AC10 Phase B : fact_daily_kpi ne
-- porte pas les noms d'événements) ; verified GA4 v1 = conversions totales du connecteur.
--
-- ============ THE MIRROR MAY NOT HAVE BEEN WRITTEN HERE AT ALL (AI-314) ======
-- `mirror_sync` writes the mirror to DuckDB and journals
-- `bigquery target -- write for <table> deferred (Phase B)` for its 28
-- relations, so in production the `mirror` dataset DOES NOT EXIST: measured
-- 2026-08-24 by replaying the nightly build against BigQuery, which answered
-- *Not found: Dataset toorow:mirror was not found in location EU* on this very
-- model. Every mart that reads `mirror.*` failed with it, dbt skipped everything
-- downstream, and the whole nightly built NO mart for two of three Projects.
--
-- A Project whose governed mirror has not been written is not a broken Project:
-- it is a Project about which nothing governed is known. So this model is built
-- EMPTY and says so, exactly as the marts already do over an absent raw source
-- (`toorow_source_present`, dbt/macros/relation_present.sql). Every staging
-- model joins it LEFT, so an empty dimension leaves the facts standing and the
-- money columns state their own absence (`money_gap_code`) instead of a currency
-- nobody declared.
{{
  config(materialized='table')
}}
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_preferences']) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['canonical_currency', 'string'],
    ['reporting_timezone', 'string'],
    ['verification_source_type', 'string'],
    ['verification_source_id', 'string'],
    ['lead_event_name', 'string'],
    ['created_at', 'timestamp'],
    ['updated_at', 'timestamp'],
]) }}
{%- else %}
SELECT
    project_id,
    canonical_currency,
    reporting_timezone,
    -- Story 17.1: source de vérification (nullable — déduplication opt-in par projet).
    verification_source_type,
    verification_source_id,
    lead_event_name,
    created_at,
    updated_at
FROM {{ source('mirror', 'project_preferences') }}
{%- endif -%}
