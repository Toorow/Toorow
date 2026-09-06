-- T1 (Story 41.3, AC9 / E41-FR01 / E41-NFR01) -- OFF => INERT.
--
-- The opt-in guard, in the shape test_dedup_estimate_opt_in_only.sql established: a
-- project that did not switch the module on must not receive a single row from ANY of
-- the three Epic-41 views. Three anti-joins plus the structural check that no ladder
-- row exists for a project with no activation row at all (absent row = OFF, C.1).
--
-- CARDINALITY GUARD -- AND THE FIX FOR REVIEW FINDING F5. An OFF project must exist AND
-- IT MUST CARRY REAL FACT ROWS. The previous fixture's OFF project had ZERO spend, which
-- made leak_ladder / leak_rollup STRUCTURALLY UNABLE TO FAIL: with nothing in
-- fact_daily_kpi the ladder emits nothing whether or not the activation flag is read, so
-- the whole test survived on leak_unknown_project alone. `feetax_dev_off` now carries two
-- days of spend, so "OFF => zero rows" is a claim these anti-joins can actually falsify.
--
-- Story 41.2's fee_tax_country_resolution is included in the anti-join (its own module
-- gate is an INNER join on the enabled projects), because a bridge that leaked rows for
-- an OFF project would be an Epic-41 surface that is not inert.
--
-- THE GATE THIS TEST READS -- AND WHY IT MOVED (repair of the 2026-08-10 review,
-- finding A). Until this revision OFF and ON were enumerated from
-- `mirror.project_preferences.fee_tax_alignment_enabled`, while ALL FOUR models gate on
-- `mirror.project_tax_fee_activation.tax_fees_active` (fee_tax_rules_effective.sql:146,
-- fee_tax_ladder_daily.sql:328, fee_tax_country_resolution.sql:188,
-- fee_tax_verification_allocation.sql:251). So this test asserted a boolean NOTHING
-- READS.
--
-- That was survivable only because the fixture DERIVED one column from the other
-- (dbt/seeds/feetax/seed_tax_fee_activation_mirror.py), which made the two columns
-- identical by construction:
--
--     select pref, governed, count(*)  ->  (False, False, 4)   (True, True, 39)
--
-- and migration 146 exists BECAUSE THEY DIVERGE (146:11-15: "a Project whose capability
-- was disabled through the Change Set kept producing Tax rows"). A fixture that cannot
-- express the defect cannot test the repair.
--
-- Both halves are fixed. OFF/ON below come from the relation the models read, and
-- `divergence_guard` REFUSES a fixture in which no project is preferences-ON /
-- governed-OFF -- so reverting the seeder to a pure derivation turns this test red
-- instead of turning it vacuous.
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

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no project has tax_fees_active FALSE -- the OFF'
        || ' anti-joins are vacuous. Run dbt/seeds/feetax/seed_fee_tax_mirror.py.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM off_projects) = 0
),

-- THE GUARD THAT REFUSES A DERIVED FIXTURE. At least one project must be ON by the
-- legacy preferences column and OFF by the governed one: that single state is the whole
-- content of migration 146, and it is the only state in which "which column does the
-- model read" is an answerable question. Without it, a seeder that derives one from the
-- other makes every anti-join below pass for the wrong reason.
divergence_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'DIVERGENCE_FAIL: no project has fee_tax_alignment_enabled TRUE while'
        || ' tax_fees_active is FALSE, so the fixture derives one gate from the other'
        || ' and CANNOT express the defect migration 146 was written for (146:11-15).'
        || ' Every OFF assertion below then holds whichever column the models read.'
        AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ source('mirror', 'project_preferences') }} p
        JOIN {{ source('mirror', 'project_tax_fee_activation') }} a
          ON a.project_id = p.project_id
        WHERE COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
          AND NOT COALESCE(CAST(a.tax_fees_active AS BOOLEAN), FALSE)
    ) = 0
),

-- AND THE DIVERGENT PROJECT MUST CARRY A CONFIRMED RULE, or `leak_rules` cannot fail
-- for it: a governed-OFF project with no rule emits nothing from
-- fee_tax_rules_effective whichever gate that model reads.
divergent_has_rule_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the preferences-ON / governed-OFF project carries no'
        || ' confirmed fee_tax_rules row, so "the governed gate is the one that is'
        || ' read" is unfalsifiable through fee_tax_rules_effective' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ source('mirror', 'project_preferences') }} p
        JOIN {{ source('mirror', 'project_tax_fee_activation') }} a
          ON a.project_id = p.project_id
        JOIN {{ source('mirror', 'fee_tax_rules') }} r
          ON r.project_id = p.project_id
        WHERE COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
          AND NOT COALESCE(CAST(a.tax_fees_active AS BOOLEAN), FALSE)
          AND r.status = 'confirmed'
    ) = 0
),

-- F5: the OFF project must have SPEND, or the anti-joins below cannot fail.
off_has_facts_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no OFF project has a single fact_daily_kpi cost row, so'
        || ' "OFF => zero ladder rows" is structurally unfalsifiable -- the ladder would'
        || ' emit nothing whether or not it read the flag' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ ref('fact_daily_kpi') }} f
        JOIN off_projects o ON o.project_id = f.project_id
        WHERE f.metric = 'cost'
    ) = 0
),

-- The same anti-vacuity clause, narrowed to the DIVERGENT project. The one above is
-- satisfied by a project that is OFF on BOTH columns, which proves nothing about which
-- column is read; only spend on a preferences-ON / governed-OFF project does.
divergent_has_facts_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the preferences-ON / governed-OFF project has no'
        || ' fact_daily_kpi cost row, so the ladder and the bridge would emit nothing'
        || ' for it whichever gate they read' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(*)
        FROM {{ ref('fact_daily_kpi') }} f
        JOIN {{ source('mirror', 'project_preferences') }} p
          ON p.project_id = f.project_id
        JOIN {{ source('mirror', 'project_tax_fee_activation') }} a
          ON a.project_id = f.project_id
        WHERE f.metric = 'cost'
          AND COALESCE(CAST(p.fee_tax_alignment_enabled AS BOOLEAN), FALSE)
          AND NOT COALESCE(CAST(a.tax_fees_active AS BOOLEAN), FALSE)
    ) = 0
),

leak_bridge AS (
    SELECT
        b.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_country_resolution emitted a row for an OFF project --'
        || ' every Epic-41 surface must be inert when the module is off' AS failure_reason
    FROM {{ ref('fee_tax_country_resolution') }} b
    JOIN off_projects o ON o.project_id = b.project_id
),

leak_rules AS (
    SELECT
        r.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_rules_effective emitted a rule for an OFF project' AS failure_reason
    FROM {{ ref('fee_tax_rules_effective') }} r
    JOIN off_projects o ON o.project_id = r.project_id
),

leak_ladder AS (
    SELECT
        l.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_ladder_daily emitted a row for an OFF project' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    JOIN off_projects o ON o.project_id = l.project_id
),

leak_rollup AS (
    SELECT
        g.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_ladder_rollup emitted a row for an OFF project' AS failure_reason
    FROM {{ ref('fee_tax_ladder_rollup') }} g
    JOIN off_projects o ON o.project_id = g.project_id
),

-- An ABSENT activation row is also OFF (C.1). Such a project cannot be enumerated
-- from the preferences table, so assert the complement instead.
leak_unknown_project AS (
    SELECT
        l.project_id AS subject,
        'OFF_LEAK_FAIL: fee_tax_ladder_daily emitted a row for a project with no'
        || ' activation row -- an absent row means OFF' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    LEFT JOIN on_projects p ON p.project_id = l.project_id
    WHERE p.project_id IS NULL
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM divergence_guard
UNION ALL SELECT subject, failure_reason FROM divergent_has_rule_guard
UNION ALL SELECT subject, failure_reason FROM off_has_facts_guard
UNION ALL SELECT subject, failure_reason FROM divergent_has_facts_guard
UNION ALL SELECT subject, failure_reason FROM leak_bridge
UNION ALL SELECT subject, failure_reason FROM leak_rules
UNION ALL SELECT subject, failure_reason FROM leak_ladder
UNION ALL SELECT subject, failure_reason FROM leak_rollup
UNION ALL SELECT subject, failure_reason FROM leak_unknown_project
{%- endif -%}
