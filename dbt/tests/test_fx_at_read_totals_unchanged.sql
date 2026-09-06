-- Singular test (0 rows = PASS) (Story 39.10 / E39-NFR06, REPAIRED 2026-08-04).
-- Asserts that every migrated connector's money metric converted, OR says why not.
-- Cardinality-guarded so it never passes vacuously ON A PROJECT THAT OWES MONEY.
--
-- WHAT CHANGED. This test used to assert `value IS NOT NULL`, full stop. Story
-- 48.3 then rewrote `fx_convert_at_read` so that an unconvertible row yields NULL
-- rather than being converted AT PARITY and summed -- "a USD figure landed in a
-- EUR total as though a dollar were a euro, and no column anywhere recorded that
-- it had happened". The row is deliberately KEPT, carrying `native_value`,
-- `native_currency` and a typed `money_gap_code`.
--
-- So a NULL value is no longer a failure BY ITSELF: it is the honest outcome the
-- repair introduced, and demanding non-NULL here would push the codebase back
-- toward the parity conversion 48.3 removed. What IS a failure -- and what this
-- test now checks -- is a NULL that nothing EXPLAINS. Measured on the local
-- fixture: 3 square rows in JPY, a currency the fx_rates seed has no pair for,
-- each correctly carrying `money_gap_code = 'fx_rate_unavailable'`.
--
-- (The same invariant is enforced mart-wide by the generic test
-- `fact_kpi_value_null_iff_money_gap`; this test keeps it on the NAMED list of
-- migrated money metrics, so a connector dropping off that list is still caught.)
--
-- ======================================================================
-- VACUITY IS OWED ONLY WHERE MONEY IS DECLARED (2026-08-31).
-- ======================================================================
-- The anti-vacuity guard below was unconditional, and on the first real
-- per-project production run it made an HONEST project permanently red:
-- proj_01KZ7ANYMN89GFPEZ05KTVEDRT has no money-capable connector at all, so
-- `money_rows` is empty for the same reason a project with no YouTube channel
-- has no YouTube rows. `Got 1 result` was the nightly reporting a hole where
-- there is nothing to fill (revision mcp-server-00226).
--
-- The doctrine the guard came from targets FIXTURES: a fixture whose seed did
-- not run answers zero rows and every assertion over it passes having proved
-- nothing. That is a real defect, and it is still refused here. What it never
-- meant is that a production project must ingest money.
--
-- WHAT DECLARES MONEY, AND WHY IT IS THIS. The declaration is read from the
-- warehouse this test can actually see: `fact_daily_kpi` carries a `connector`
-- column, and the seven connectors named below are exactly the ones whose
-- catalogues declare a money metric -- the same list the pair predicate uses,
-- so the two cannot drift. A project with ANY row from one of them is a project
-- that owes money rows: shopify without revenue, meta-ads without cost, is a
-- hole worth reddening. A project with NO row from any of them owes nothing,
-- and this test emits nothing.
--
-- IT IS NOT CIRCULAR, and that is the whole design of it: the declaration counts
-- rows from those connectors REGARDLESS of metric, while the guard counts the
-- money METRICS. On the local fixture the two sets differ by construction --
-- every money connector also lands a non-money metric (meta-ads clicks 587,
-- shopify orders_count 105, stripe order_count 92, square order_count 3,
-- woocommerce orders_count 3, linkedin-ads clicks 321, tiktok-ads clicks 840,
-- measured 2026-08-31) -- so deleting every money row from the fixture leaves
-- the declaration standing and the guard FIRES, which is the behaviour the
-- doctrine exists for and which is proved by doing exactly that.
--
-- The mirror was the other candidate source and it is refused: `mirror.*` is not
-- in the production warehouse at all (AI-314, mirror_sync defers its BigQuery
-- writes), so a guard reading it would decline on every project for a reason
-- that has nothing to do with money.

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

WITH money_rows AS (
    SELECT
        project_id,
        date,
        connector,
        metric,
        value,
        fx_rate,
        money_gap_code
    FROM {{ ref('fact_daily_kpi') }}
    WHERE {{ money_pairs }}
),

-- ONE scan answers both questions, so the two counts can never be taken from
-- two different states of the mart: how many money rows there are, and whether
-- any money-capable connector landed anything at all.
cardinality_check AS (
    SELECT
        SUM(CASE WHEN {{ money_pairs }} THEN 1 ELSE 0 END) AS money_row_count,
        COUNT(*) AS declared_row_count
    FROM {{ ref('fact_daily_kpi') }}
    WHERE connector IN ({{ money_connectors }})
)

SELECT
    'cardinality_failure_zero_money_rows' AS failure_reason,
    0.0 AS value
FROM cardinality_check
WHERE declared_row_count > 0
  AND COALESCE(money_row_count, 0) = 0

UNION ALL

SELECT
    'unexplained_null_value_' || connector || '_' || metric AS failure_reason,
    0.0 AS value
FROM money_rows
WHERE value IS NULL
  AND money_gap_code IS NULL

UNION ALL

-- The mirror failure, which the old `IS NOT NULL` shape could not express: a row
-- that reports a converted figure AND a reason it could not convert. One of the
-- two is false and the reader cannot tell which.
SELECT
    'converted_value_carries_gap_code_' || connector || '_' || metric AS failure_reason,
    0.0 AS value
FROM money_rows
WHERE value IS NOT NULL
  AND money_gap_code IS NOT NULL
