-- Story 41.5 (R12) -- MEASUREMENT BEATS DERIVATION, AND THE TWO NEVER SUM.
--
-- stripe and square land a REAL `fees` fact. Where that measurement exists it WINS and
-- every declared PAYMENT_FEE rule for the row is SUPPRESSED. Applying both would
-- double-count the same fee, and the total would look entirely reasonable.
--
--   A. no row is provenance='measured_gateway_fee' while ALSO listing a PAYMENT_FEE rule
--      in applied_rule_ids -- provenance that contradicts the number beside it;
--   B. where a measurement exists AND a rule was declared, payment_fee_rule_superseded
--      must be TRUE (silently dropping the rule without saying so is not honesty);
--   C. the fee equals the MEASURED figure, not the derived one -- the discriminating
--      half, because a 1.4 % rule on the same row would give a different, plausible
--      number;
--   D. anti-vacuity: such a row must exist, or A/B/C prove nothing.

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

measured AS (
    SELECT * FROM normalized WHERE payment_fee_provenance = 'measured_gateway_fee'
),

case_a AS (
    SELECT
        'MEASURED_FEE_ALSO_APPLIED_A_RULE' AS failure,
        m.project_id                       AS project_id,
        m.applied_rule_ids                 AS detail
    FROM measured m
    WHERE m.applied_rule_ids LIKE '%_fee%'
),

case_b AS (
    SELECT
        'SUPPRESSION_NOT_DECLARED' AS failure,
        m.project_id               AS project_id,
        'a rule was declared for this row but payment_fee_rule_superseded is FALSE'
                                   AS detail
    FROM measured m
    WHERE m.project_id = 'feetaxrev_dev_measured'
      AND m.payment_fee_rule_superseded = FALSE
),

case_c AS (
    SELECT
        'MEASURED_FEE_VALUE_DIVERGED' AS failure,
        m.project_id                  AS project_id,
        CAST(m.payment_fee_micros AS STRING) AS detail
    FROM measured m
    WHERE m.project_id = 'feetaxrev_dev_measured'
      -- The seeded measurement is 173.00 EUR. The declared 1.4 % rule on the same row
      -- would have given 172 839 380, which is close enough to be believed and wrong.
      AND m.payment_fee_micros <> 173000000
),

case_d AS (
    SELECT
        'NO_MEASURED_FEE_ROW_IN_FIXTURE' AS failure,
        'n/a'                            AS project_id,
        'the measurement-wins assertions would pass by finding nothing' AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (SELECT 1 FROM measured)
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
UNION ALL SELECT * FROM case_d
{%- endif -%}
