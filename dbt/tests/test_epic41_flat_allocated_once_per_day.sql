-- T20 (Story 41.3, review finding F8 -- orchestrator ruling) -- a FLAT fee is charged
-- ONCE PER DAY, then allocated; it is NOT multiplied by the row count.
--
-- WHAT WENT WRONG AND WHY NOTHING CAUGHT IT. The ladder grain is
-- (project, date, connector, breakdown_value), and §D.4.1 said only
-- "FLAT -> amount_micros" -- so a project-scoped FLAT of 500 EUR fired ONCE PER CAMPAIGN
-- PER DAY: 40 campaigns x 30 days = 600 000 EUR of "flat" fee. No test noticed, because
-- across all eleven fixture projects there was exactly ONE FLAT rule and its component
-- was currency-REFUSED, so the FLAT branch never once produced a number. A branch that
-- never produces a number is a branch with no coverage, whatever the test count says.
--
-- THE RULING: a FLAT amount applies ONCE per (project_id, date, currency) and is
-- ALLOCATED across that day's ladder rows pro-rata by net media, largest-remainder, so
-- the day's components sum to EXACTLY amount_micros -- the cent-exact discipline
-- mediaplan_store.compute_spread already uses for budgets.
--
-- Asserted here:
--   A. EXACT DAY TOTAL. Per (project, date, currency), the FLAT rule's contribution sums
--      to exactly 500 000 000 micros -- not 500 000 000 x the number of campaigns. This
--      is the assertion the ruling is about.
--   B. IT IS ACTUALLY SPLIT. Both campaigns receive a non-zero share and the two shares
--      DIFFER (1 000.00 vs 2 333.33 of net media), so an even split or an
--      all-to-one-row degenerate would fail. The pinned shares are 150 000 150 and
--      349 999 850 -- deliberately not round, so the largest-remainder path is exercised
--      rather than a clean division.
--   C. ANTI-VACUITY. At least one FLAT rule must actually FIRE somewhere -- i.e. appear
--      in applied_rule_ids with a non-NULL component. This is the guard whose absence
--      let the whole branch go untested.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{#- UNE PREMISSE ABSENTE N EST PAS UN DEFAUT (AI-314, 2026-08-24).
    Ce test epingle un exemple SEME : il mesure ce que la fixture locale porte, et
    la fixture vit dans le MIROIR. `mirror_sync` differe ses ecritures BigQuery
    (Phase B), donc dans un entrepot ou le miroir n a pas ete ecrit -- toute la
    production aujourd hui -- ce test ne trouve rien a mesurer et rend son
    CARDINALITY_FAIL : un rouge qui accuse le calcul d un defaut dont la cause est
    qu il n y a rien a calculer. Un test rouge est un code de sortie, et un code
    de sortie est un projet sans marts.
    Il DECLINE donc de juger, EN LE DISANT : `TOOROW_SOURCE_ABSENT` remonte au
    nocturne, qui refuse alors le mot << ok >> pour ce projet. La ou le miroir EST
    -- la boucle locale, la CI -- rien ne bouge et l assertion reste entiere. -#}
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_tax_fee_activation']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH flat_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_flat'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 4 ladder rows for feetax_dev_flat (2 campaigns x 2'
        || ' days), got ' || CAST((SELECT COUNT(*) FROM flat_rows) AS STRING)
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM flat_rows) <> 4
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no FLAT rule anywhere in the warehouse actually FIRES with a'
        || ' non-NULL component. The FLAT branch is producing no number at all, which is'
        || ' exactly the blind spot that hid the once-per-row multiplication.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ ref('fee_tax_ladder_daily') }} l
        JOIN {{ ref('fee_tax_rules_effective') }} r
            ON  r.project_id = l.project_id
            AND l.applied_rule_ids LIKE '%' || r.rule_id || '%'
        WHERE r.form = 'FLAT'
          AND l.platform_fee_micros IS NOT NULL
    ) = 0
),

-- ------------------------------------------------ A. the exact day total ----
day_totals AS (
    SELECT project_id, date, currency, SUM(platform_fee_micros) AS flat_total_micros
    FROM flat_rows
    GROUP BY project_id, date, currency
),

day_total_breaks AS (
    SELECT
        d.project_id || '|' || CAST(d.date AS STRING) AS subject,
        'FLAT_DAY_TOTAL_FAIL: a FLAT fee must be charged ONCE per (project, date,'
        || ' currency). Expected exactly 500000000 micros for the day, got '
        || COALESCE(CAST(d.flat_total_micros AS STRING), 'NULL')
        || ' -- a multiple of 500000000 means it is still being billed per row'
            AS failure_reason
    FROM day_totals d
    WHERE d.flat_total_micros IS DISTINCT FROM 500000000
),

-- ------------------------------------------- B. it is genuinely allocated ---
allocation_breaks AS (
    SELECT
        f.project_id || '|' || CAST(f.date AS STRING) || '|' || f.breakdown_value AS subject,
        'FLAT_ALLOCATION_FAIL: expected the pro-rata largest-remainder shares'
        || ' 150000150 (ffl_camp_a, 1 000.00 net) and 349999850 (ffl_camp_b, 2 333.33'
        || ' net), got ' || COALESCE(CAST(f.platform_fee_micros AS STRING), 'NULL')
            AS failure_reason
    FROM flat_rows f
    WHERE (f.breakdown_value = 'ffl_camp_a' AND f.platform_fee_micros IS DISTINCT FROM 150000150)
       OR (f.breakdown_value = 'ffl_camp_b' AND f.platform_fee_micros IS DISTINCT FROM 349999850)
),

degenerate_split_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the two FLAT shares are equal, so an even split and a'
        || ' pro-rata split are indistinguishable in this fixture' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(DISTINCT platform_fee_micros) FROM flat_rows) < 2
),

completeness_breaks AS (
    SELECT
        f.project_id || '|' || CAST(f.date AS STRING) || '|' || f.breakdown_value AS subject,
        'FLAT_FAIL: an allocated FLAT row must be COMPLETE -- allocation is arithmetic,'
        || ' not a gap. Got gap_codes=' || f.gap_codes AS failure_reason
    FROM flat_rows f
    WHERE NOT f.is_ladder_complete
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM day_total_breaks
UNION ALL SELECT subject, failure_reason FROM allocation_breaks
UNION ALL SELECT subject, failure_reason FROM degenerate_split_guard
UNION ALL SELECT subject, failure_reason FROM completeness_breaks
{%- endif -%}
