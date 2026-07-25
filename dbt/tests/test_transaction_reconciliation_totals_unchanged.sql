-- Story 17.4 non-interference (Epic 17 invariant AD-4/AD-6, "totaux inchanges" discipline
-- of review-epic-10): transaction_reconciliation[_daily] is a READ. The transaction grain
-- must NEVER enter fact_daily_kpi -- it lives ONLY in stg_ga4_transactions_daily and the
-- reconciliation marts.
--
-- A dbt test cannot diff "fact_daily_kpi with vs without the reconciliation marts" (the
-- marts do not write into fact_daily_kpi -- that non-editing is the point, and is verified
-- structurally: transaction_reconciliation.sql only SELECTs from the two staging models,
-- never from or into fact_daily_kpi). What CAN and MUST hold as a hard guard: the GA4
-- transactions raw table's transaction grain has NOT leaked into fact_daily_kpi as a
-- breakdown partition. If a future edit ever added a 'transaction_id' breakdown_dimension
-- to fact_daily_kpi (the exact forbidden move), THIS test fails.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT
    project_id,
    date,
    connector,
    breakdown_dimension
FROM {{ ref('fact_daily_kpi') }}
WHERE breakdown_dimension = 'transaction_id'
   OR breakdown_dimension = 'transaction'
