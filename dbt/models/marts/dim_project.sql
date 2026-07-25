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
{{
  config(materialized='table')
}}
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
