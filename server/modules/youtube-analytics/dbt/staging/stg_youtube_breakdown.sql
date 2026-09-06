-- Staging YouTube Analytics -- les repartitions (audience, geo, appareil,
-- source de trafic, lieu de lecture, statut d'abonnement) et le releve d'audience.
--
-- POURQUOI UNE SEULE RELATION POUR TOUTES LES COMBINAISONS. Une repartition
-- demographique porte `age_group` et `gender` ; une geographique porte `country`.
-- Elargir la table par combinaison ajouterait une colonne par dimension que
-- quelqu'un demande un jour. Le couple (breakdown_dimension, breakdown_value)
-- est la reponse que la plateforme donne deja partout ailleurs, et il tient
-- toutes les combinaisons que l'API sert.
--
-- AD-4 : ce qui atterrit ici n'est pas toujours sommable, et la colonne
--        `metric` le dit. `viewer_percentage` est une REPARTITION et
--        `subscriber_count` un STOCK -- le manifeste les declare
--        `aggregation=latest`, `non_additive=true`, et le compilateur de
--        projection refuse de les sommer. Rien n'est agrege ici.
-- AD-7 : QUALIFY supersede -- le dernier pull par serie gagne (ULID lex-monotone).
-- AD-9 : NULL honnete -- une metrique absente reste NULL, jamais zero.
--
-- GRAIN : une ligne par CELLULE rapportee -- (project_id, date, channel_id,
--         breakdown_dimension, breakdown_value, metric) N'EST PAS unique, et
--         AI-342 est ce que le croire a coute. Un profil qui declare DEUX
--         dimensions -- `audience_device` : `device_type` et `operating_system` --
--         rapporte le CROISEMENT, et le landing ecrit chaque cellule une fois
--         par dimension : (MOBILE, ANDROID) et (MOBILE, IOS) donnent deux lignes
--         `breakdown_dimension='device_type', breakdown_value='MOBILE'` DANS LE
--         MEME PULL. La marginale mobile est leur SOMME.
--
--         Le `ROW_NUMBER() ... = 1` d'avant partitionnait sur `breakdown_value`
--         et en gardait UNE : mesure sur la fixture locale, `device_type=MOBILE`
--         rendait 500 vues la ou la source en rapporte 800, et rien ne le disait
--         -- le pull etait le meme, donc `ORDER BY pull_id DESC` choisissait au
--         hasard du moteur. Les cinq autres profils ne portent qu'une dimension
--         hors grain et n'ont jamais pu tomber sur ce cas, ce qui est exactement
--         pourquoi il a survecu.
--
--         La supersede garde donc TOUT le dernier pull d'une serie
--         (project_id, date, channel_id, breakdown_dimension, metric) : un
--         re-pull remplace la serie entiere, et l'agregation des cellules
--         appartient au lecteur -- `fact_daily_kpi` les somme par
--         (breakdown_dimension, breakdown_value).
{{ config(materialized='view') }}

WITH raw AS (
    SELECT *
    FROM {{ source('raw_youtube', 'raw_youtube_breakdown') }}
    -- DENSE_RANK et non ROW_NUMBER : le rang porte sur le PULL, pas sur la
    -- ligne, donc toutes les cellules du dernier pull d'une serie sont gardees.
    -- Meme forme d'expression fenetree dans le QUALIFY que les autres stagings
    -- du depot, donc meme portabilite DuckDB / BigQuery.
    QUALIFY DENSE_RANK() OVER (
        PARTITION BY project_id, date, channel_id, breakdown_dimension, metric
        ORDER BY pull_id DESC
    ) = 1
)

-- A country is a VOCABULARY, not a free string: YouTube answers ISO 3166-1
-- alpha-2, and a cross-source reconciliation only holds if every source speaks
-- the same one. Normalisation touches ONLY the rows whose dimension IS the
-- country; every other breakdown keeps the provider's own value.
--
-- AND THE RAW VALUE IS KEPT beside it: a value the vocabulary does not resolve
-- has to be nameable, or the data-quality evidence says "unresolved" about
-- nothing anybody can act on.
--
-- THE LOOKUP IS THE MACRO'S, AND THAT IS WHERE BOTH ITS PRODUCTION REFUSALS WERE
-- REPAIRED. This relation is the one production keeps naming -- `list_contains`
-- took the whole view down on revision mcp-server-00225, and once the view
-- compiled its `not_null` tests died on *Correlated subqueries that reference
-- other tables are not supported* (mcp-server-00226, project
-- proj_01KZGCRSV2XACWRP3RSVNWWGBK). Neither defect was written here: both were
-- in `dbt/macros/normalize_dimension.sql`, and both are fixed there, for the six
-- stagings that call it. Nothing about this file was the instance.
SELECT
    raw.date,
    raw.channel_id,
    raw.breakdown_dimension,
    CASE
        WHEN raw.breakdown_dimension = 'country'
        THEN {{ normalize_dimension('upper(raw.breakdown_value)', ref('dim_country'), 'aliases', 'iso_code') }}
        ELSE raw.breakdown_value
    END AS breakdown_value,
    CASE
        WHEN raw.breakdown_dimension = 'country' THEN raw.breakdown_value
    END AS country_source,
    raw.metric,
    raw.value,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
