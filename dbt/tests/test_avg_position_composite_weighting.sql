-- AI-52 (Epic 8) fixture test: semantic_avg_position_composite weighting is correct
-- AND zero-division safe. Self-contained hand-computed fixture (inline VALUES) so the
-- assertion is deterministic and reviewable, independent of the 90-day sinusoidal GSC
-- seed. It replicates the view's EXACT arithmetic
--   SUM(position * impressions) / NULLIF(SUM(impressions), 0)  GROUP BY country, device
-- on known numbers and asserts the expected weighted value per composite cell.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).
--
-- HAND-COMPUTED EXPECTED VALUES
-- Cell fra>desktop (2 fixture pages):
--   position 7.3 @ 850 impressions, position 3.1 @ 200 impressions
--   weighted = (7.3*850 + 3.1*200) / (850+200)
--            = (6205 + 620) / 1050 = 6825 / 1050 = 6.5   (Story 6.2 canonical proof)
--   NAIVE avg would be (7.3+3.1)/2 = 5.2 -- proving the weighting matters.
-- Cell gbr>mobile (1 fixture page):
--   position 5.4 @ 180 impressions -> weighted = 5.4 (single row, trivially itself).
-- Cell fra>tablet (zero-impressions guard):
--   position 12.0 @ 0 impressions -> SUM(impressions)=0 -> NULLIF -> NULL (no divide error).

{#- LES DEUX FORMES QUI NE TRAVERSENT PAS (AI-314, 2026-08-24) : une CTE qui
    nomme ses colonnes -- `WITH fixture(a, b) AS` -- et `VALUES` comme
    constructeur de table autonome. BigQuery refuse les deux (*Expected ")" but
    got identifier "country"*), donc ce test etait REFUSE chaque nuit sur le
    moteur de la production. Mesure par un dry run a 0 octet. Les valeurs n ont
    pas bouge d un chiffre : seule leur ECRITURE change, en UNION de SELECT
    d une ligne, que les deux moteurs portent. #}
{#- LES LITTERAUX RESTENT NUS, et ce n est pas un detail : `CAST(5.4 AS float)`
    rend un flottant SIMPLE precision en DuckDB (5.400000095367432) et la
    comparaison a 1e-9 dessous echoue -- vu au premier tir de cette reecriture.
    Un litteral decimal nu garde en DuckDB le type que `VALUES` lui donnait, et
    BigQuery le lit en FLOAT64 : les deux moteurs, une seule ecriture. #}
WITH fixture AS (
    SELECT 'fra' AS country, 'desktop' AS device, 7.3 AS average_position, 850 AS impressions
    UNION ALL SELECT 'fra', 'desktop',  3.1,  200
    UNION ALL SELECT 'gbr', 'mobile',   5.4,  180
    UNION ALL SELECT 'fra', 'tablet',  12.0,    0
),

weighted AS (
    SELECT
        country,
        device,
        SUM(average_position * impressions) / NULLIF(SUM(impressions), 0) AS avg_position,
        SUM(impressions) AS impressions_weight
    FROM fixture
    GROUP BY country, device
),

expected AS (
    SELECT 'fra' AS country, 'desktop' AS device, 6.5 AS expected_position, FALSE AS expected_is_null
    UNION ALL SELECT 'gbr', 'mobile', 5.4,  FALSE
    -- zero impressions -> NULL
    UNION ALL SELECT 'fra', 'tablet', NULL, TRUE
)

SELECT
    w.country,
    w.device,
    w.avg_position,
    e.expected_position,
    e.expected_is_null
FROM weighted w
JOIN expected e
    ON e.country = w.country AND e.device = w.device
WHERE
    -- zero-impressions cell must be NULL
    (e.expected_is_null AND w.avg_position IS NOT NULL)
    -- weighted cells must match the hand-computed value (tolerance 1e-9)
    OR (NOT e.expected_is_null AND (
            w.avg_position IS NULL
         OR ABS(w.avg_position - e.expected_position) > 0.000000001
        ))
