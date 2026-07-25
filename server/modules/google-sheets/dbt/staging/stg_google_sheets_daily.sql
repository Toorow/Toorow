-- Staging Google Sheets -- grain (project_id, date, sheet_row_id) daily.
-- connector-requirements.md Google Sheets profile (Story 15.6, Epic 15).
-- AD-4 : metriques additives uniquement (budget_declared, target_revenue, target_conversions).
-- AD-7 : pull_id propage depuis raw pour la chaine de provenance.
-- Story 15.6 : staging module-owned (AI-06 option A -- model-path externe).
--
-- GRAIN : une ligne par (project_id, date, sheet_row_id).
-- sheet_row_id : identifiant de la ligne dans la feuille (valeur de la colonne row_id_column
--   du column_mapping, ou 'row_N' si non declare). Il distingue les lignes pour le meme jour
--   (ex : plusieurs canaux ou campagnes dans la meme feuille).
--
-- Supersede semantics (AD-7) : quand plusieurs pulls couvrent le meme grain, le
-- DERNIER pull gagne. Les ULIDs sont monotoniquement croissants, ORDER BY pull_id
-- DESC = le plus recent en premier.
--
-- METRIQUES (AD-9 NULL HONNETE) :
--   budget_declared : budget declare dans la feuille (ex: budget mensuel)
--   target_revenue  : objectif de CA declare dans la feuille
--   target_conversions : objectif de conversions declare dans la feuille
-- Ces trois metriques sont des OBJECTIFS saisis manuellement -- pas des realisations.
-- Une cellule vide = NULL (la colonne etait absente de la ligne) ; un zero reel = 0.0.
-- Le HAVING IS NOT NULL au mart evite d'emettre des lignes sans valeur pour les
-- consommateurs (AD-9 : l'absence de ligne = "aucun objectif declare pour ce grain").
--
-- DOUBLE-COMPTE SAFETY (DESIGN 2) :
--   Google Sheets contribue une SEULE dimension ('sheet_row_id') par metrique.
--   Les metriques budget_declared / target_revenue / target_conversions sont des
--   OBJECTIFS declares -- elles ne participent PAS a cross_source_conversions (pas de
--   dedup requis : elles ne sont jamais sommees avec les realisations des regies).
--   connector='google-sheets' est une cle unique dans fact_daily_kpi ; elle n'entre
--   jamais en collision avec une partition GA4/Meta/TikTok/GSC/Shopify/LinkedIn.
--   MIN(breakdown_dimension) pour google-sheets est trivialement 'sheet_row_id'.
--
-- NULL GARDE (review-15-3 F-3 : lecon grain-unique) :
--   Les lignes avec sheet_row_id IS NULL sont exclues. Un sheet_row_id NULL rendrait
--   la cle du test grain-unique NULL (ignoree par `unique`) -- des doublons passeraient
--   silencieusement.

WITH raw AS (
    SELECT *
    FROM {{ source('raw_google_sheets', 'raw_google_sheets_daily') }}
    -- review-15-3 F-3 pattern : sheet_row_id NULL -> exclusion explicite.
    WHERE sheet_row_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, sheet_row_id
        ORDER BY pull_id DESC
    ) = 1
)

SELECT
    raw.date     AS date_source,
    raw.date     AS date,
    raw.sheet_row_id,
    raw.spreadsheet_id,
    raw.sheet_name,
    -- Metriques objectifs (ADDITIVE au grain date x sheet_row_id, AD-4).
    -- NULL HONNETE (AD-9) : cellule vide = NULL, zero reel = 0.0.
    raw.budget_declared,
    raw.target_revenue,
    raw.target_conversions,
    raw.pull_id,
    raw.loaded_at,
    raw.project_id
FROM raw
