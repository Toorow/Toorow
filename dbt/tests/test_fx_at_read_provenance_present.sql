-- Singular test (0 rows = PASS) (Story 39.10 / AC3).
-- Asserts every converted money row in fact_daily_kpi carries non-null FX provenance.
-- Cardinality-guarded so it never passes vacuously.

WITH converted_rows AS (
    SELECT
        connector,
        metric,
        value,
        fx_rate,
        fx_as_of_date,
        fx_source,
        fx_tier
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
    SELECT COUNT(*) AS cnt FROM converted_rows
)

SELECT
    'cardinality_failure_zero_converted_rows' AS failure_reason,
    NULL AS connector,
    NULL AS metric
FROM cardinality_check
WHERE cnt = 0

UNION ALL

SELECT
    'missing_provenance' AS failure_reason,
    connector,
    metric
FROM converted_rows
WHERE fx_source IS NULL OR fx_tier IS NULL
