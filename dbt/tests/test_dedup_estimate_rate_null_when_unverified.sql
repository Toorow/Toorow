-- Story 17.2 honest-degrade guard (AD-9, HONESTY 3): duplication_rate must be NULL
-- whenever verified_total is NULL or 0 -- NEVER a fabricated 0% duplication and NEVER
-- a masked division by zero. Symmetrically, deduplicated_contribution must be NULL when
-- verified_total is NULL (stripe v1 / absent truth) or claimed_total is 0.
--
-- This covers the "régies indisponibles" / stripe-not-delivered degrade: the source of
-- truth may be present but claimed=0 (no row emitted), or the truth may be NULL (stripe),
-- and in both cases the rate is NULL rather than a disguised number.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT project_id, date, channel_connector, verified_total, duplication_rate
FROM {{ ref('dedup_estimate') }}
WHERE (verified_total IS NULL OR verified_total = 0)
  AND duplication_rate IS NOT NULL
