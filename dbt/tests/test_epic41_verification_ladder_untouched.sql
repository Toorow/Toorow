-- T3 (Story 41.4, AC3 / E41-AD4 / Epic 27 invariant 4) -- GUARD 3, THE DISCRIMINATOR:
-- the twin-project byte-identity differential.
--
-- feetax_dev_verif_twin_a and feetax_dev_verif_twin_b are identical in EVERY respect --
-- the same meta-ads spend (12 345.67 EUR on fvt_camp_1, 2026-05-01), the same three
-- ladder rules (platform 3 %, agency 10 %, VAT 20 %), the same day, the same campaign ref
-- -- EXCEPT that twin B additionally carries a confirmed VERIFICATION CPM rule AND IAS
-- measured_impressions facts. Their fee_tax_ladder_daily rows must therefore be equal
-- COLUMN FOR COLUMN at tolerance EXACTLY 0.
--
-- WHY THIS IS THE RIGHT DISCRIMINATOR, AND WHY AN IN-PROJECT ASSERTION IS NOT. Any
-- assertion made INSIDE a verification-bearing project has to know the right number in
-- advance, so it only ever catches the change it was told to expect. This differential
-- needs to know NOTHING: it fails the moment verification cost lands ANYWHERE in the
-- ladder -- in a phase column, in the running subtotal, in total_ttc_micros, or even as a
-- GAP that nulls a total. It is the device test_epic39_totals_bit_identical.sql uses,
-- applied to the one property this story exists to protect. T4 (the pinned twin total) is
-- the belt to its braces.
--
-- THE ANTI-VACUITY GUARD IS LOAD-BEARING HERE, more than anywhere else in this suite:
-- without it the twins could be identical simply because NOTHING WAS COMPUTED -- an empty
-- overlay makes this test pass perfectly while proving nothing at all. So twin B's
-- overlay must report exactly 5 000 000 000 micros of verification cost.
--
-- NULLs are compared as EQUAL (a gapped column is NULL on both sides or on neither).
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

{% set compared_columns = [
    'net_media_micros',
    'platform_fee_micros',
    'regulatory_tax_micros',
    'wht_gross_up_micros',
    'agency_fee_micros',
    'subtotal_ht_micros',
    'sales_tax_micros',
    'total_ttc_micros',
    'gap_codes',
    'applied_rule_ids',
    'is_ladder_complete'
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
WITH twin_a AS (
    SELECT *
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id = 'feetax_dev_verif_twin_a'
),

twin_b AS (
    SELECT *
    FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id = 'feetax_dev_verif_twin_b'
),

cardinality_guard AS (
    -- Both twins must exist AND produce the SAME NUMBER of ladder rows. A row that
    -- appeared or vanished on one side would slip past a join-based comparison entirely.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: twin_a has ' || CAST((SELECT COUNT(*) FROM twin_a) AS STRING)
        || ' ladder rows and twin_b has ' || CAST((SELECT COUNT(*) FROM twin_b) AS STRING)
        || ' -- the twins must be identical in cardinality, and both must be non-empty.'
        || ' Run dbt/seeds/feetax/seed_fee_tax_verification.py, then rebuild'
        || ' fact_daily_kpi BEFORE the ladder.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM twin_a) = 0
       OR (SELECT COUNT(*) FROM twin_a) <> (SELECT COUNT(*) FROM twin_b)
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: twin_b''s overlay reports '
        || COALESCE(CAST((
               SELECT SUM(verification_cost_micros)
               FROM {{ ref('fee_tax_verification_allocation') }}
               WHERE project_id = 'feetax_dev_verif_twin_b'
                 AND row_kind   = 'allocation'
           ) AS STRING), 'NULL')
        || ' micros of verification cost, expected 5000000000. Without a real,'
        || ' non-zero verification cost the twins are trivially identical and this test'
        || ' proves nothing.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE COALESCE((
        SELECT SUM(verification_cost_micros)
        FROM {{ ref('fee_tax_verification_allocation') }}
        WHERE project_id = 'feetax_dev_verif_twin_b'
          AND row_kind   = 'allocation'
    ), -1) <> 5000000000
),

unmatched_rows AS (
    -- A row present on one side only. FULL OUTER would be neater but a symmetric pair of
    -- anti-joins is portable and says which side is missing.
    SELECT
        'twin_a|' || CAST(a.date AS STRING) || '|' || a.breakdown_value AS subject,
        'LADDER_ROW_MISSING_FAIL: this twin_a ladder row has no twin_b counterpart --'
        || ' the presence of verification cost changed the ladder''s SHAPE' AS failure_reason
    FROM twin_a a
    LEFT JOIN twin_b b
        ON  b.date            = a.date
        AND b.breakdown_value = a.breakdown_value
    WHERE b.breakdown_value IS NULL
    UNION ALL
    SELECT
        'twin_b|' || CAST(b.date AS STRING) || '|' || b.breakdown_value AS subject,
        'LADDER_ROW_MISSING_FAIL: this twin_b ladder row has no twin_a counterpart --'
        || ' the presence of verification cost changed the ladder''s SHAPE' AS failure_reason
    FROM twin_b b
    LEFT JOIN twin_a a
        ON  a.date            = b.date
        AND a.breakdown_value = b.breakdown_value
    WHERE a.breakdown_value IS NULL
),

column_breaks AS (
    SELECT
        CAST(a.date AS STRING) || '|' || a.breakdown_value AS subject,
        'KEEP_SEPARATE_FAIL: Epic 27 invariant 4 -- VERIFICATION COST HAS ENTERED THE'
        || ' MEDIA COST LADDER. The two twins differ on this row although the ONLY'
        || ' difference between the projects is twin B''s VERIFICATION rule and its IAS'
        || ' facts. Verification cost is a KEEP_SEPARATE overlay and must appear in NO'
        || ' ladder column, in no running subtotal, and as no gap.'
        {%- for c in compared_columns %}
        || '  {{ c }}: a=' || COALESCE(CAST(a.{{ c }} AS STRING), 'NULL')
        || ' b=' || COALESCE(CAST(b.{{ c }} AS STRING), 'NULL')
        {%- endfor %}
            AS failure_reason
    FROM twin_a a
    JOIN twin_b b
        ON  b.date            = a.date
        AND b.breakdown_value = a.breakdown_value
    WHERE
        {%- for c in compared_columns %}
        {% if not loop.first %}OR {% endif %}
        {%- if c == 'applied_rule_ids' -%}
        -- A rule id is a global primary key, so the twins' ids CANNOT be equal by
        -- construction ('..._twin_a_agency' vs '..._twin_b_agency'). Normalising only the
        -- twin discriminator compares the SET of rules the ladder applied, which is what
        -- this column is here to police: if twin B applied an EXTRA rule (a VERIFICATION
        -- rule leaking into the cost cascade) its normalised id has no counterpart in A
        -- and this still fires. Comparing the raw strings would fail on every row forever
        -- and prove nothing.
        REPLACE(a.{{ c }}, '_twin_a_', '_twin_') IS DISTINCT FROM REPLACE(b.{{ c }}, '_twin_b_', '_twin_')
        {%- else -%}
        a.{{ c }} IS DISTINCT FROM b.{{ c }}
        {%- endif %}
        {%- endfor %}
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM unmatched_rows
UNION ALL SELECT subject, failure_reason FROM column_breaks
{%- endif -%}
