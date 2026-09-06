-- Country capability partitions retained independently from each connector's
-- canonical day-total series. Unknown provider spellings stay observable for
-- governed DQ.
--
-- STORY 58.5, ARBITRAGE 1 -- WHAT CHANGED, AND WHY THE OLD LINE WAS THE WORSE HALF
-- OF THE DEFECT. This model used to filter `AND COALESCE(country, country_source)
-- IS NOT NULL`, and its header claimed that as a virtue: "rows with no country
-- signal do not fabricate a NULL bucket". They did not fabricate one -- they
-- DISAPPEARED. The total by country then stopped equalling the total of the day,
-- and nothing said so: no test failed, no build broke, no screen changed. Measured
-- 2026-08-07 it had never fired (0 staging rows with no country), so it was latent,
-- not harmless -- the first provider row without a country would have taken money
-- out of a country report in silence.
--
-- The row is now KEPT under the declared bucket of `country_bucket()`, which is not
-- a country and is qualified by its own kind everywhere it is read. `retain_source_
-- value=true` because this model's contract is the one it always had: an unresolved
-- provider spelling stays visible for governed DQ rather than collapsing into the
-- absence -- "the provider wrote something we cannot read" and "the provider wrote
-- nothing" are two different repairs, on two different desks.

SELECT
    project_id,
    date,
    'cm360' AS connector,
    metric,
    'country' AS breakdown_dimension,
    {{ country_bucket('country', 'country_source', retain_source_value=true) }} AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id) AS pull_id,
    MAX(loaded_at) AS loaded_at
FROM {{ ref('stg_cm360_daily') }}
WHERE non_additive = FALSE
GROUP BY project_id, date, metric, {{ country_bucket('country', 'country_source', retain_source_value=true) }}
HAVING SUM(CAST(value AS {{ toorow_float_type() }})) IS NOT NULL

UNION ALL

SELECT
    project_id,
    date,
    'dv360' AS connector,
    metric,
    'country' AS breakdown_dimension,
    {{ country_bucket('country', 'country_source', retain_source_value=true) }} AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id) AS pull_id,
    MAX(loaded_at) AS loaded_at
FROM {{ ref('stg_dv360_daily') }}
WHERE non_additive = FALSE
GROUP BY project_id, date, metric, {{ country_bucket('country', 'country_source', retain_source_value=true) }}
HAVING SUM(CAST(value AS {{ toorow_float_type() }})) IS NOT NULL

UNION ALL

SELECT
    project_id,
    date,
    'taboola' AS connector,
    metric,
    'country' AS breakdown_dimension,
    {{ country_bucket('country', 'country_source', retain_source_value=true) }} AS breakdown_value,
    SUM(CAST(value AS {{ toorow_float_type() }})) AS value,
    {{ money_evidence_absent() }}
    MAX(pull_id) AS pull_id,
    MAX(loaded_at) AS loaded_at
FROM {{ ref('stg_taboola_daily') }}
WHERE non_additive = FALSE
GROUP BY project_id, date, metric, {{ country_bucket('country', 'country_source', retain_source_value=true) }}
HAVING SUM(CAST(value AS {{ toorow_float_type() }})) IS NOT NULL
