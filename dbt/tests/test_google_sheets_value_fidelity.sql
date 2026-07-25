-- test_google_sheets_value_fidelity.sql -- Story 15.6, fix F-3 (review-15-6).
--
-- AC epique : "les valeurs sheet sont STRICTEMENT egales aux valeurs importees
-- (aucune transformation implicite)".
--
-- APPROCHE RETENUE : jointure raw -> mart (variante "raw->mart", plus robuste).
--
-- Justification du choix :
--   Le generateur de seed depend de date.today() au runtime (end_date par defaut =
--   date.today()) -- les valeurs RNG changent donc a chaque execution. Coder les
--   valeurs attendues en dur dans le test les rendrait caduques des le lendemain.
--   La variante "jointure raw->mart" prouve "aucune transformation implicite"
--   independamment des valeurs absolues et des dates : elle verifie que pour chaque
--   (project_id, date, sheet_row_id), la value du mart est IDENTIQUE a la valeur
--   dans le raw (pas d'arrondi, de conversion, de normalisation ou d'agregation
--   parasite). Ce test casse des qu'une transformation est introduite.
--
-- Ce que le test valide :
--   Pour chaque (project_id, date, sheet_row_id) present dans le mart
--   connector='google-sheets', la value du mart == la valeur de la colonne source
--   correspondante dans raw_google_sheets_daily (apres deduplication staging QUALIFY).
--
-- Ce que le test ne valide PAS (covert par d'autres tests) :
--   - Qu'aucune ligne n'est perdue au staging (couvert par le test grain-unique).
--   - Que les metriques restent isolees des autres connecteurs (couvert par
--     test_google_sheets_totals_isolated.sql).
--
-- Convention dbt : un test singulier ECHOUE si la requete retourne des lignes
-- (zero lignes = SUCCES).

WITH raw_deduped AS (
    -- Reproduit le QUALIFY du staging stg_google_sheets_daily pour comparer
    -- apples-to-apples : la valeur "du raw" = la valeur issue du dernier pull
    -- pour ce grain (meme logique supersede que le staging).
    SELECT
        project_id,
        date,
        sheet_row_id,
        budget_declared,
        target_revenue,
        target_conversions
    FROM {{ source('raw_google_sheets', 'raw_google_sheets_daily') }}
    WHERE sheet_row_id IS NOT NULL
    QUALIFY ROW_NUMBER() OVER (
        PARTITION BY project_id, date, sheet_row_id
        ORDER BY pull_id DESC
    ) = 1
),

mart_gs AS (
    SELECT
        project_id,
        date,
        metric,
        breakdown_value   AS sheet_row_id,
        value             AS mart_value
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector = 'google-sheets'
      AND breakdown_dimension = 'sheet_row_id'
),

-- Pivot le mart pour comparer metrique par metrique avec le raw.
mart_pivoted AS (
    SELECT
        project_id,
        date,
        sheet_row_id,
        MAX(CASE WHEN metric = 'budget_declared'    THEN mart_value END) AS budget_declared_mart,
        MAX(CASE WHEN metric = 'target_revenue'     THEN mart_value END) AS target_revenue_mart,
        MAX(CASE WHEN metric = 'target_conversions' THEN mart_value END) AS target_conversions_mart
    FROM mart_gs
    GROUP BY project_id, date, sheet_row_id
),

violations AS (
    SELECT
        r.project_id,
        r.date,
        r.sheet_row_id,
        -- Signale toute divergence valeur raw vs valeur mart.
        -- NULL == NULL est considere comme egal (pas de divergence).
        CASE
            WHEN r.budget_declared IS DISTINCT FROM m.budget_declared_mart
            THEN 'budget_declared raw=' || COALESCE(CAST(r.budget_declared AS VARCHAR), 'NULL')
              || ' mart=' || COALESCE(CAST(m.budget_declared_mart AS VARCHAR), 'NULL')
        END AS budget_violation,
        CASE
            WHEN r.target_revenue IS DISTINCT FROM m.target_revenue_mart
            THEN 'target_revenue raw=' || COALESCE(CAST(r.target_revenue AS VARCHAR), 'NULL')
              || ' mart=' || COALESCE(CAST(m.target_revenue_mart AS VARCHAR), 'NULL')
        END AS revenue_violation,
        CASE
            WHEN r.target_conversions IS DISTINCT FROM m.target_conversions_mart
            THEN 'target_conversions raw='
              || COALESCE(CAST(r.target_conversions AS VARCHAR), 'NULL')
              || ' mart=' || COALESCE(CAST(m.target_conversions_mart AS VARCHAR), 'NULL')
        END AS conversions_violation
    FROM raw_deduped r
    LEFT JOIN mart_pivoted m
        ON r.project_id = m.project_id
       AND r.date = m.date
       AND r.sheet_row_id = m.sheet_row_id
)

-- Retourne les lignes en violation (non-NULL violation = transformation implicite detectee).
-- Zero lignes = toutes les valeurs mart sont strictement identiques aux valeurs raw.
SELECT
    project_id,
    date,
    sheet_row_id,
    budget_violation,
    revenue_violation,
    conversions_violation
FROM violations
WHERE budget_violation IS NOT NULL
   OR revenue_violation IS NOT NULL
   OR conversions_violation IS NOT NULL
