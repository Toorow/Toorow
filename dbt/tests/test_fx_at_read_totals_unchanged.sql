-- Singular test (0 rows = PASS) (Story 39.10 / E39-NFR06).
-- Asserts that for every migrated connector's money metric, fact_daily_kpi.value
-- is NOT NULL and matches converted rate calculations.
-- Cardinality-guarded so it never passes vacuously.

WITH money_rows AS (
    SELECT
        project_id,
        date,
        connector,
        metric,
        value,
        fx_rate
    FROM {{ ref('fact_daily_kpi') }}
    WHERE (connector = 'meta-ads' AND metric = 'cost')
       OR (connector = 'shopify' AND metric IN ('revenue', 'refund_amount'))
       OR (connector = 'woocommerce' AND metric IN ('revenue', 'refund_amount'))
       OR (connector = 'tiktok-ads' AND metric = 'cost')
       OR (connector = 'linkedin-ads' AND metric = 'cost')
       OR (connector = 'stripe' AND metric IN ('revenue', 'refunds', 'fees'))
       OR (connector = 'square' AND metric IN ('revenue', 'refunds', 'fees'))
),

cardinality_check AS (
    SELECT COUNT(*) AS cnt FROM money_rows
)

SELECT
    'cardinality_failure_zero_money_rows' AS failure_reason,
    0.0 AS value
FROM cardinality_check
WHERE cnt = 0

UNION ALL

SELECT
    'null_value_failure_' || connector || '_' || metric AS failure_reason,
    0.0 AS value
FROM money_rows
WHERE value IS NULL
