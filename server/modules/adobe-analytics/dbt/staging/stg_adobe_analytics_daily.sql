--
-- AI-270 -- LE NOM DE METRIQUE DEVIENT CANONIQUE ICI.
--
-- Cet atterrissage est LONG : le nom de metrique est une VALEUR de colonne. Le
-- `transform()` du connecteur renomme les CLES de ligne, donc il ne peut pas
-- l'atteindre -- ce connecteur posait un nom de fournisseur la ou le fait attend
-- le nom du dictionnaire, et le brancher tel quel aurait fait lire DEUX
-- metriques la ou il y en a une.
--
-- La correspondance vient de `connector_metric_names`, une seed DERIVEE des
-- manifestes par scripts/generate_metric_name_map.py et tenue par
-- test_metric_name_map_is_current -- jamais une seconde table ecrite a la main.
--
-- ICI et pas a l'atterrissage, parce que les lignes DEJA landees gardent leur
-- nom brut : une reparation au seul point d'atterrissage couperait chaque total
-- en deux a la date de sa livraison.
--
-- LEFT JOIN + COALESCE : une metrique absente de la carte garde son nom, ce qui
-- est exactement juste pour les 24 connecteurs qui landent deja canonique.
{{ config(materialized='view') }}

WITH raw AS (
    SELECT * FROM {{ source('raw_adobe_analytics', 'raw_adobe_analytics_daily') }}
    QUALIFY ROW_NUMBER() OVER (
      PARTITION BY project_id, report_profile, global_company_id, rsid, date,
        dimension, item_id, parent_item_id, segment_ids, metric
      ORDER BY pull_id DESC
    ) = 1
)

SELECT
    {{ toorow_star_except('raw', ['metric']) }},
    COALESCE(nm.canonical_metric, raw.metric) AS metric
FROM raw
LEFT JOIN {{ ref('connector_metric_names') }} nm
    ON nm.connector = 'adobe-analytics'
   AND nm.landed_metric = raw.metric
