-- T5c (Story 41.4, AC4 / AC12 / E41-NFR02) -- THE CPM RANGE REFUSAL, EXERCISED.
--
-- WHY THIS TEST EXISTS (review finding C, 2026-08-10). The overlay's header says of
-- VERIFICATION_CPM_OUT_OF_RANGE: "cpm_micros NULL, negative, or >= 1e10 [...] Fail
-- closed", and `row_cost` asserts "Every member of `fired` is in range, so this SUM can
-- never be NULL". Both were UNTESTED CLAIMS: every fixture CPM was 2 500 000 or
-- 2 500 500, so no build could emit the code, and the guard at
-- fee_tax_verification_allocation.sql:477-479 could be reduced to `cpm_micros IS NULL`
-- with `dbt build --select fee_tax_verification_allocation` still reporting
-- PASS=30 ERROR=0. A guard no test can fell is a guard that can be deleted in silence --
-- and this one stands between a bad rate and an invoice line.
--
-- The three boundaries are seeded one project each
-- (dbt/seeds/feetax/seed_fee_tax_verification.py), so a red names WHICH boundary broke:
--
--   feetax_dev_verif_cpm_negative   cpm_micros = -2 500 000
--   feetax_dev_verif_cpm_overflow   cpm_micros = 10 000 000 000  (exactly 1e10)
--   feetax_dev_verif_cpm_null       cpm_micros = NULL
--
-- Each carries 300 000 REAL measured impressions and a CONFIRMED, in-window,
-- project-scoped CPM rule in the project's own currency. Everything except the rate is
-- therefore healthy: the ONLY thing that can refuse the row is the range guard. That is
-- what makes this test able to fall.
--
-- WHAT IS ASSERTED, and why each clause is separate:
--   A. the code is EMITTED at all -- the anti-vacuity half. Without it the four
--      assertions below hold over zero rows.
--   B. the cost is NULL. NEVER 0: a 0 asserts "verification cost for this campaign is
--      zero", which is precisely what an unusable rate does not tell us.
--   C. is_allocation_complete is FALSE and the impressions are still visible. You may
--      look at the parts; you may not read a total we could not compute.
--   D. no OTHER gap contaminates the row. If CURRENCY_MISMATCH also fired, the NULL cost
--      would be explained by the wrong refusal and the guard could still be dead.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{% set cpm_range_projects = [
    'feetax_dev_verif_cpm_negative',
    'feetax_dev_verif_cpm_overflow',
    'feetax_dev_verif_cpm_null',
] %}

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
WITH overlay AS (
    SELECT * FROM {{ ref('fee_tax_verification_allocation') }}
),

range_rows AS (
    SELECT *
    FROM overlay
    WHERE row_kind = 'allocation'
      AND project_id IN (
          {%- for project in cpm_range_projects %}
          '{{ project }}'{{ "," if not loop.last }}
          {%- endfor %}
      )
),

-- A. THE ANTI-VACUITY HALF. One allocation row per range project, and each one must
-- carry the code. This is the clause that turns the reduced guard red.
cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected {{ cpm_range_projects | length }} allocation rows'
        || ' across the CPM range projects (got '
        || CAST((SELECT COUNT(*) FROM range_rows) AS STRING)
        || '). Run dbt/seeds/feetax/seed_fee_tax_verification.py and rebuild'
        || ' fact_daily_kpi.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM range_rows) <> {{ cpm_range_projects | length }}
),

code_never_emitted_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'RESERVED_VALUE_FAIL: not one row in fee_tax_verification_allocation carries'
        || ' VERIFICATION_CPM_OUT_OF_RANGE. The code is then a value declared in'
        || ' accepted_values that no build emits, and the range guard at'
        || ' fee_tax_verification_allocation.sql:477-479 can be deleted without a single'
        || ' line turning red.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*) FROM overlay WHERE gap_codes LIKE '%VERIFICATION_CPM_OUT_OF_RANGE%'
    ) = 0
),

missing_code AS (
    SELECT
        r.project_id AS subject,
        'CPM_RANGE_FAIL: an unusable cpm_micros (NULL, negative or >= 1e10) reached this'
        || ' row and the refusal was NOT typed. Got gap_codes=''' || r.gap_codes
        || ''' cost=' || COALESCE(CAST(r.verification_cost_micros AS STRING), 'NULL')
            AS failure_reason
    FROM range_rows r
    WHERE r.gap_codes NOT LIKE '%VERIFICATION_CPM_OUT_OF_RANGE%'
),

-- B + C. The refusal must be a REFUSAL, not a quiet number and not a fabricated zero.
refusal_shape AS (
    SELECT
        r.project_id AS subject,
        'CPM_RANGE_SHAPE_FAIL: a refused row must carry verification_cost_micros NULL'
        || ' (never 0 -- a 0 ASSERTS the cost is zero), is_allocation_complete FALSE, and'
        || ' the measured impressions still visible. Got cost='
        || COALESCE(CAST(r.verification_cost_micros AS STRING), 'NULL')
        || ' complete=' || CAST(r.is_allocation_complete AS STRING)
        || ' impressions='
        || COALESCE(CAST(r.measured_impressions AS STRING), 'NULL') AS failure_reason
    FROM range_rows r
    WHERE r.verification_cost_micros IS NOT NULL
       OR r.is_allocation_complete
       OR r.measured_impressions IS DISTINCT FROM 300000
),

-- D. The row must be refused for THIS reason and no other, or the assertion above could
-- be satisfied by an unrelated gap while the range guard stays dead.
contaminated AS (
    SELECT
        r.project_id AS subject,
        'CPM_RANGE_ISOLATION_FAIL: the row carries a gap code other than'
        || ' VERIFICATION_CPM_OUT_OF_RANGE, so its NULL cost no longer proves the range'
        || ' guard fired. Got gap_codes=''' || r.gap_codes || '''' AS failure_reason
    FROM range_rows r
    WHERE r.gap_codes <> 'VERIFICATION_CPM_OUT_OF_RANGE'
),

-- The rule really did REACH the row. A rule that never matched would also produce a NULL
-- cost -- via coverage_state = NO_RULE_DECLARED -- and prove nothing about the guard.
rule_did_not_reach AS (
    SELECT
        r.project_id AS subject,
        'CPM_RANGE_REACH_FAIL: the CPM rule did not reach this row, so the NULL cost is'
        || ' explained by "no rule declared" rather than by the range refusal. Got'
        || ' coverage_state=' || r.coverage_state AS failure_reason
    FROM range_rows r
    WHERE r.coverage_state = 'NO_RULE_DECLARED'
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM code_never_emitted_guard
UNION ALL SELECT subject, failure_reason FROM missing_code
UNION ALL SELECT subject, failure_reason FROM refusal_shape
UNION ALL SELECT subject, failure_reason FROM contaminated
UNION ALL SELECT subject, failure_reason FROM rule_did_not_reach
{%- endif -%}
