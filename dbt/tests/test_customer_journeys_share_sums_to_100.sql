-- Story 16.2 share sanity (AC (c)): share_of_conversions sums to ~100 per
-- (project_id, date, attribution_model, breakdown_kind) group.
--
-- share_of_conversions is a row's conversions over its (project, date, model, kind) TOP-N
-- subtotal (a descriptive "Part" of the ranked top-N -- story 10.4 honesty, NOT a share
-- of the true GA4 day total). Within one such group the shares are parts of the SAME
-- subtotal, so they sum to 100 exactly up to floating-point rounding.
--
-- We do NOT group by (project, date, model) alone: last_click has TWO axes (channel and
-- campaign) that are marginals of the SAME conversions, so their shares would sum to ~200
-- -- pooling them is precisely the double-count the mart avoids. Grouping by
-- breakdown_kind gives one honest 100%-summing set per axis.
--
-- Groups whose subtotal is zero produce NULL shares (NULLIF guard) and no 100% target --
-- excluded via HAVING SUM(conversions) > 0.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Tolerance 0.5
-- absolute on the summed percentage (accumulated rounding across up to top_n rows).

SELECT
    project_id,
    date,
    attribution_model,
    breakdown_kind,
    SUM(share_of_conversions) AS share_sum,
    SUM(conversions)          AS conv_subtotal
FROM {{ ref('customer_journeys') }}
GROUP BY project_id, date, attribution_model, breakdown_kind
HAVING SUM(conversions) > 0
   AND ABS(SUM(share_of_conversions) - 100.0) > 0.5
