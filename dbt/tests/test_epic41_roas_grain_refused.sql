-- Story 41.5 AC4 -- ROAS EXISTS AT EXACTLY ONE GRAIN AND IS REFUSED AT EVERY OTHER.
--
-- Two halves, and the first is the load-bearing one:
--   A. THE COLUMNS DO NOT EXIST. `connector`, `breakdown_dimension` and
--      `breakdown_value` are absent from fee_tax_revenue_alignment_daily, so a
--      campaign-grain or connector-grain ratio is UNCONSTRUCTIBLE rather than filtered
--      out. A filter can be relaxed by anyone; a missing column cannot be selected.
--      (`revenue_source` is NOT a grain column -- it is provenance naming which commerce
--      source won the day, and it does not partition the row.)
--   B. AT MOST TWO ROWS per (project, date, currency) -- one per basis.
-- Read from information_schema so the assertion is about the SHIPPED RELATION, not about
-- the model text someone could edit in one place and not the other.

WITH forbidden_columns AS (
    SELECT
        'FORBIDDEN_GRAIN_COLUMN_PRESENT' AS failure,
        column_name                      AS project_id,
        'fee_tax_revenue_alignment_daily must carry no connector / breakdown grain'
                                         AS detail
    FROM information_schema.columns
    WHERE table_name = 'fee_tax_revenue_alignment_daily'
      AND column_name IN ('connector', 'breakdown_dimension', 'breakdown_value')
),

too_many_rows AS (
    SELECT
        'MORE_THAN_TWO_BASIS_ROWS' AS failure,
        a.project_id               AS project_id,
        CAST(a.date AS STRING) || ' rows=' || CAST(COUNT(*) AS STRING) AS detail
    FROM {{ ref('fee_tax_revenue_alignment_daily') }} a
    GROUP BY a.project_id, a.date, a.currency
    HAVING COUNT(*) > 2
)

SELECT * FROM forbidden_columns
UNION ALL SELECT * FROM too_many_rows
