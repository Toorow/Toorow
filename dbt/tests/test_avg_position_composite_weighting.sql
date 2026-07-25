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

WITH fixture(country, device, average_position, impressions) AS (
    SELECT * FROM (VALUES
        ('fra', 'desktop', 7.3, 850),
        ('fra', 'desktop', 3.1, 200),
        ('gbr', 'mobile',  5.4, 180),
        ('fra', 'tablet',  12.0,  0)
    ) AS v(country, device, average_position, impressions)
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

expected(country, device, expected_position, expected_is_null) AS (
    SELECT * FROM (VALUES
        ('fra', 'desktop', 6.5,  FALSE),
        ('gbr', 'mobile',  5.4,  FALSE),
        ('fra', 'tablet',  CAST(NULL AS DOUBLE), TRUE)   -- zero impressions -> NULL
    ) AS e(country, device, expected_position, expected_is_null)
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
