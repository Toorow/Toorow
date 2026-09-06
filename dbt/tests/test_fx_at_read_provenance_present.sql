-- Singular test (0 rows = PASS) (Story 39.10 / AC3).
-- Asserts every converted money row in fact_daily_kpi carries non-null FX provenance.
-- Cardinality-guarded so it never passes vacuously ON A PROJECT THAT OWES MONEY.
--
-- VACUITY IS OWED ONLY WHERE MONEY IS DECLARED (2026-08-31). The full reasoning
-- is written once, in `test_fx_at_read_totals_unchanged.sql`, beside its twin of
-- this guard; both fired together on proj_01KZ7ANYMN89GFPEZ05KTVEDRT
-- (`cardinality_failure_zero_converted_rows`, revision mcp-server-00226) and both
-- decline for the same reason. In one line: a project with no row from any of
-- the seven money-capable connectors owes no money rows, so an empty
-- `converted_rows` is the truth and this test emits nothing; a project that HAS
-- landed rows from one of them and converted none of them still gets the
-- vacuity row, because that is a hole.

{% set money_connectors = "'meta-ads', 'shopify', 'woocommerce', 'tiktok-ads', 'linkedin-ads', 'stripe', 'square'" %}
{% set money_pairs %}
       (connector = 'meta-ads' AND metric = 'cost')
    OR (connector = 'shopify' AND metric IN ('revenue', 'refund_amount'))
    OR (connector = 'woocommerce' AND metric IN ('revenue', 'refund_amount'))
    OR (connector = 'tiktok-ads' AND metric = 'cost')
    OR (connector = 'linkedin-ads' AND metric = 'cost')
    OR (connector = 'stripe' AND metric IN ('revenue', 'refunds', 'fees'))
    OR (connector = 'square' AND metric IN ('revenue', 'refunds', 'fees'))
{% endset %}

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
    WHERE {{ money_pairs }}
),

cardinality_check AS (
    SELECT
        SUM(CASE WHEN {{ money_pairs }} THEN 1 ELSE 0 END) AS converted_row_count,
        COUNT(*) AS declared_row_count
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector IN ({{ money_connectors }})
)

SELECT
    'cardinality_failure_zero_converted_rows' AS failure_reason,
    NULL AS connector,
    NULL AS metric
FROM cardinality_check
WHERE declared_row_count > 0
  AND COALESCE(converted_row_count, 0) = 0

UNION ALL

SELECT
    'missing_provenance' AS failure_reason,
    connector,
    metric
FROM converted_rows
WHERE fx_source IS NULL OR fx_tier IS NULL
