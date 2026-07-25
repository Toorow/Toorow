-- Story 17.2 opt-in guard (Epic 17: dedup is opt-in per project) + "totaux inchangés"
-- discipline for a READ mart (review-epic-10): dedup_estimate must NOT emit ANY row
-- for a project that did NOT designate a source of truth
-- (dim_project.verification_source_type IS NULL). If it ever leaked a row for an
-- opt-out project, this fails loudly.
--
-- This is the READ-mart form of the "totaux existants inchangés" test: dedup_estimate
-- writes into NO existing model (it only SELECTs from fact_daily_kpi + dim_project --
-- verifiable structurally), and it adds rows ONLY for opt-in projects. An opt-out
-- project's totals are therefore untouched AND unshadowed.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

SELECT de.project_id, de.date, de.channel_connector
FROM {{ ref('dedup_estimate') }} de
JOIN {{ ref('dim_project') }} p
    ON p.project_id = de.project_id
WHERE p.verification_source_type IS NULL
