-- cross_source_conversions: single-source day totals for conversions (Rule P).
-- AD-4-conversions decision record: _bmad-output/planning-artifacts/architecture/AD-4-conversions.md
-- Priority is DECLARATIVE: dbt/seeds/metric_source_priority.csv (no names here).
--
-- review-3-7 F-1/F-2 fix: two-step logic --
--   1. pick the winning CONNECTOR per (project_id, date) from the priority seed;
--   2. total = SUM over ALL rows of that connector on ONE breakdown dimension
--      (the mart carries parallel per-dimension series that EACH total the day:
--      summing across dimensions would double-count (review-1-6 lesson); keeping
--      a single row under-counts (review-3-7 lesson)).
-- Per-source rows remain untouched in fact_daily_kpi (query WHERE metric='conversions').
-- Story: 3.7 -- No metric counted twice

WITH conv AS (
    SELECT project_id, date, connector, breakdown_dimension, value, pull_id
    FROM {{ ref('fact_daily_kpi') }}
    WHERE metric = 'conversions'
),

winners AS (
    -- one winning connector per (project_id, date), by declared priority
    SELECT project_id, date, connector
    FROM (
        SELECT
            c.project_id,
            c.date,
            c.connector,
            ROW_NUMBER() OVER (
                PARTITION BY c.project_id, c.date
                ORDER BY COALESCE(p.priority, 99), c.connector
            ) AS rnk
        FROM (SELECT DISTINCT project_id, date, connector FROM conv) c
        LEFT JOIN {{ ref('metric_source_priority') }} p
            ON p.metric = 'conversions' AND p.connector = c.connector
    )
    WHERE rnk = 1
),

canonical_dim AS (
    -- one breakdown dimension per (project_id, date, connector): each dimension
    -- series independently totals the day; pick one deterministically.
    -- Partitioned by date so a dimension appearing mid-history does not drop
    -- earlier dates silently (review-global-gaps canonical_dim fix).
    SELECT project_id, date, connector, MIN(breakdown_dimension) AS dim
    FROM conv
    GROUP BY project_id, date, connector
)

SELECT
    f.project_id,
    f.date,
    SUM(f.value)     AS conversions_total,
    MAX(f.connector) AS attribution_source,
    MAX(f.pull_id)   AS pull_id
FROM conv f
JOIN winners w
    ON w.project_id = f.project_id AND w.date = f.date AND w.connector = f.connector
JOIN canonical_dim d
    ON d.project_id = f.project_id AND d.date = f.date
   AND d.connector = f.connector AND d.dim = f.breakdown_dimension
GROUP BY f.project_id, f.date
