-- T6 (Story 41.4, AC7 / E41-FR01 / E41-NFR01) -- OFF => INERT for the overlay too.
--
-- Every Epic-41 surface must return ZERO rows for a project that did not switch the
-- module on. Story 41.3 asserts this for the bridge, the rule view and the two ladder
-- models; the overlay is a fourth surface and gets the same anti-join, in the shape
-- test_dedup_estimate_opt_in_only.sql established.
--
-- WHAT MAKES THIS NON-VACUOUS. Story 41.3's `feetax_dev_off` project carries REAL SPEND
-- with the flag FALSE (its review finding F5), which is what turns "OFF => zero rows" into
-- a claim an anti-join can actually FALSIFY. This test reads that existing project rather
-- than creating a second OFF project -- a second one would only make the anti-join harder
-- to read without making it stronger.
--
-- THE GATE THIS TEST READS -- AND WHY IT MOVED (repair of the 2026-08-10 review,
-- finding A). It enumerated OFF from `project_preferences.fee_tax_alignment_enabled`;
-- the overlay gates on `mirror.project_tax_fee_activation.tax_fees_active`
-- (fee_tax_verification_allocation.sql:251) since Story 48.4 / migration 146. Both sets
-- are now read from the relation the model reads, and `divergence_guard` refuses a
-- fixture in which the two columns cannot disagree.
--
-- THE HONEST NOTE THAT USED TO BE HERE IS RETIRED, because the fixture it described was
-- repaired rather than documented. It said: `feetax_dev_off` carries cost rows but no
-- measured_impressions, so the overlay would emit nothing for it even if it ignored the
-- flag -- i.e. `leak_off` could not bite for THIS model. `feetax_dev_governed_off`
-- (dbt/seeds/feetax/seed_tax_fee_activation_mirror.py) carries measured_impressions AND
-- a confirmed VERIFICATION CPM rule, so an overlay that read the wrong gate would emit a
-- priced row for it. `divergent_has_base_guard` keeps that true.
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
WITH off_projects AS (
    SELECT project_id
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE NOT COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

on_projects AS (
    SELECT project_id
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

-- The projects the legacy flag calls ON and the governed authority calls OFF. This is
-- migration 146's stated defect (146:11-15) and the only state in which "which column
-- does the model read" is answerable.
divergent_projects AS (
    SELECT p.project_id AS project_id
    FROM {{ source('mirror', 'project_preferences') }} p
    JOIN {{ source('mirror', 'project_tax_fee_activation') }} a
      ON a.project_id = p.project_id
    WHERE COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
      AND NOT COALESCE(CAST(a.tax_fees_active AS BOOLEAN), FALSE)
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no project has tax_fees_active FALSE, so the OFF'
        || ' anti-join below is vacuous. Run dbt/seeds/feetax/seed_fee_tax_mirror.py.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM off_projects) = 0
),

divergence_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'DIVERGENCE_FAIL: no project has fee_tax_alignment_enabled TRUE while'
        || ' tax_fees_active is FALSE, so the fixture derives one gate from the other'
        || ' and this anti-join holds whichever column the overlay reads.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM divergent_projects) = 0
),

divergent_has_base_guard AS (
    -- The divergent project must carry BOTH a measured_impressions fact and a confirmed
    -- CPM rule, or the overlay emits nothing for it whichever gate it reads -- which is
    -- exactly the weakness this test used to declare instead of removing.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the preferences-ON / governed-OFF project has no'
        || ' measured_impressions fact and confirmed CPM rule pair, so leak_off cannot'
        || ' fail for the overlay' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM divergent_projects d
        JOIN {{ ref('fact_daily_kpi') }} f
          ON f.project_id = d.project_id AND f.metric = 'measured_impressions'
        JOIN {{ source('mirror', 'fee_tax_rules') }} r
          ON r.project_id = d.project_id
         AND r.form = 'CPM'
         AND r.status = 'confirmed'
    ) = 0
),

anti_vacuity_guard AS (
    -- The overlay must be producing SOMETHING for the ON projects, or "zero rows for OFF"
    -- is indistinguishable from "zero rows for everyone".
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: fee_tax_verification_allocation is empty for every project, so'
        || ' "OFF => zero rows" proves only that the test ran. Run'
        || ' dbt/seeds/feetax/seed_fee_tax_verification.py and rebuild fact_daily_kpi.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM {{ ref('fee_tax_verification_allocation') }}) = 0
),

leak_off AS (
    SELECT
        v.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_verification_allocation emitted a row for a project whose'
        || ' tax_fees_active is FALSE -- every Epic-41 surface must be inert when'
        || ' the module is off' AS failure_reason
    FROM {{ ref('fee_tax_verification_allocation') }} v
    JOIN off_projects o ON o.project_id = v.project_id
),

leak_unknown_project AS (
    -- An ABSENT preferences row is also OFF (C.1). Such a project cannot be enumerated
    -- from the preferences table, so assert the complement instead. This is the assertion
    -- with real teeth for THIS model (see the header).
    SELECT
        v.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_verification_allocation emitted a row for a project with no'
        || ' module-ON activation row -- an absent row means OFF' AS failure_reason
    FROM {{ ref('fee_tax_verification_allocation') }} v
    LEFT JOIN on_projects p ON p.project_id = v.project_id
    WHERE p.project_id IS NULL
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM divergence_guard
UNION ALL SELECT subject, failure_reason FROM divergent_has_base_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM leak_off
UNION ALL SELECT subject, failure_reason FROM leak_unknown_project
{%- endif -%}
