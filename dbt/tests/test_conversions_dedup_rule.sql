-- Reconciliation test (Story 3.7 AC6, review-3-7 strengthened):
-- cross_source_conversions.conversions_total must EQUAL the winning source's
-- single-dimension day total -- both over-count (summed sources / summed
-- parallel dimensions) AND under-count (single retained breakdown row) fail.
-- A dbt singular test returns rows when it FAILS (zero rows = pass).

WITH conv AS (
    SELECT project_id, date, connector, breakdown_dimension, value
    FROM {{ ref('fact_daily_kpi') }}
    WHERE metric = 'conversions'
),

canonical_dim AS (
    SELECT project_id, date, connector, MIN(breakdown_dimension) AS dim
    FROM conv
    GROUP BY project_id, date, connector
),

per_source_day AS (
    -- day total per connector on ONE breakdown dimension (the honest grain)
    SELECT c.project_id, c.date, c.connector, SUM(c.value) AS source_total
    FROM conv c
    JOIN canonical_dim d
        ON d.project_id = c.project_id AND d.date = c.date
       AND d.connector = c.connector AND d.dim = c.breakdown_dimension
    GROUP BY c.project_id, c.date, c.connector
),

expected AS (
    -- the winner's total per (project, date), by declared priority
    SELECT project_id, date, source_total AS expected_total
    FROM (
        SELECT
            ps.*,
            ROW_NUMBER() OVER (
                PARTITION BY ps.project_id, ps.date
                ORDER BY COALESCE(p.priority, 99), ps.connector
            ) AS rnk
        FROM per_source_day ps
        LEFT JOIN {{ ref('metric_source_priority') }} p
            ON p.metric = 'conversions' AND p.connector = ps.connector
    )
    WHERE rnk = 1
)

SELECT
    cs.project_id,
    cs.date,
    cs.conversions_total,
    e.expected_total
FROM {{ ref('cross_source_conversions') }} cs
JOIN expected e
    ON e.project_id = cs.project_id AND e.date = cs.date
WHERE ABS(cs.conversions_total - e.expected_total) > e.expected_total * 0.001
