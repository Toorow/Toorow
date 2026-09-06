-- Story 41.5 -- the VAT arithmetic, pinned, AND the identity that must never bend.
--
-- A dbt singular test FAILS when it returns rows.
--
-- PART A pins the three worked examples (R1 / R2 / R3) against the dev fixture, so a
-- change to the macro's widths, its rounding or its guard shape shows up as a WRONG
-- NUMBER rather than as a plausible one.
-- PART B asserts `net + tax == gross` ON EVERY ROW OF THE MODEL at a tolerance of
-- EXACTLY ZERO. That is not a strict choice, it is the only correct one: the tax is
-- derived by EXACT INTEGER SUBTRACTION of the single rounded quotient, so the identity
-- holds by construction regardless of the division's last bit. IF THIS EVER FAILS, THE
-- FIX IS IN THE MODEL, NEVER IN THE TOLERANCE. A loosened identity test silently stops
-- catching the bug it exists for.
-- PART C is the anti-vacuity guard: if the fixture rows are missing, PART A would pass
-- by finding nothing, so their absence is itself a failure.

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
WITH normalized AS (
    SELECT * FROM {{ ref('fee_tax_revenue_normalized_daily') }}
),

expected AS (
    -- project_id, gross, net, tax
    SELECT 'feetaxrev_dev_vat'  AS project_id,
           12345670000 AS gross, 10288058333 AS net, 2057611667 AS tax
    UNION ALL
    -- R2, the ROUND_HALF_UP-vs-banker's pin: 12 000 000 003 / 1.2 is 10 000 000 002.5
    -- ALGEBRAICALLY. SQL rounds half away from zero.
    SELECT 'feetaxrev_dev_half', 12000000003, 10000000003, 2000000000
    UNION ALL
    -- A REAL zero: the computation ran and the answer is zero.
    SELECT 'feetaxrev_dev_zero',  5000000000,  5000000000,          0
),

part_a AS (
    SELECT
        'PINNED_VALUE_DIVERGED' AS failure,
        n.project_id            AS project_id,
        CAST(n.gross_revenue_ttc_micros AS STRING) || '/'
            || CAST(n.net_revenue_ht_micros AS STRING) || '/'
            || CAST(n.sales_tax_micros AS STRING)      AS detail
    FROM normalized n
    JOIN expected e ON e.project_id = n.project_id
    WHERE n.gross_revenue_ttc_micros IS DISTINCT FROM e.gross
       OR n.net_revenue_ht_micros    IS DISTINCT FROM e.net
       OR n.sales_tax_micros         IS DISTINCT FROM e.tax
),

part_b AS (
    SELECT
        'IDENTITY_BROKEN_net_plus_tax_ne_gross' AS failure,
        n.project_id                            AS project_id,
        CAST(n.net_revenue_ht_micros + n.sales_tax_micros - n.gross_revenue_ttc_micros
             AS STRING)                         AS detail
    FROM normalized n
    WHERE n.net_revenue_ht_micros IS NOT NULL
      AND n.sales_tax_micros      IS NOT NULL
      AND n.gross_revenue_ttc_micros IS NOT NULL
      -- TOLERANCE EXACTLY ZERO. Do not relax this.
      AND n.net_revenue_ht_micros + n.sales_tax_micros <> n.gross_revenue_ttc_micros
),

part_c AS (
    SELECT
        'PINNED_FIXTURE_MISSING' AS failure,
        e.project_id             AS project_id,
        'no normalized row for this fixture project' AS detail
    FROM expected e
    WHERE NOT EXISTS (
        SELECT 1 FROM normalized n WHERE n.project_id = e.project_id
    )
)

SELECT * FROM part_a
UNION ALL SELECT * FROM part_b
UNION ALL SELECT * FROM part_c
{%- endif -%}
