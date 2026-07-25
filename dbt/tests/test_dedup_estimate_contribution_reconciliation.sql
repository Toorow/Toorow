-- Story 17.2 formula reconciliation (AD-9, doc/synthese §2.2):
-- when verified_total is non-NULL and claimed_total > 0, the deduplicated
-- contributions across channels for a (project, date) MUST sum to verified_total.
--   deduplicated_contribution = claimed_channel * verified / SUM(claimed)
--   => SUM over channels = verified * SUM(claimed) / SUM(claimed) = verified.
-- This is the "Σ contributions dédupliquées = réel" invariant of the epic.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass). Float tolerance
-- 0.001 relative (rescaling is exact arithmetic; tolerance guards float rounding).

WITH per_day AS (
    SELECT
        project_id,
        date,
        MAX(verified_total)                AS verified_total,
        SUM(deduplicated_contribution)     AS sum_contribution
    FROM {{ ref('dedup_estimate') }}
    WHERE verified_total IS NOT NULL
      AND verified_total <> 0
    GROUP BY project_id, date
)

SELECT project_id, date, verified_total, sum_contribution
FROM per_day
WHERE ABS(sum_contribution - verified_total) > verified_total * 0.001
