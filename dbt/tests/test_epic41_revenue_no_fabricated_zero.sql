-- Story 41.5 AC5 (E41-NFR02) -- NO FABRICATED TOTAL, EVER.
--
-- The rule this whole epic turns on: a DERIVED figure that could not be computed is
-- NULL WITH A TYPED GAP CODE, never 0, never an assumed rate and never an assumed count.
-- A `0` must therefore ALWAYS mean "the computation ran and the answer is zero".
--
--   A. landed_revenue_micros is NEVER NULL -- it is a read of a real fact;
--   B. gap_codes and applied_rule_ids are '' rather than NULL when there is nothing to
--      say (a NULL string forces every consumer to write a COALESCE, and one of them
--      will forget);
--   C. net_revenue_ht_micros is NULL whenever SALES_TAX_RULE_ABSENT -- and, the other
--      way round, is NOT NULL when the row is complete on a typed basis. Both directions,
--      because "always NULL" would satisfy the first half alone;
--   D. no derived column is 0 on a row that carries the matching gap code -- the exact
--      shape of the fabricated total;
--   E. is_normalization_complete agrees with gap_codes being empty;
--   F. anti-vacuity: the SALES_TAX_RULE_ABSENT fixture (the DAY-ONE HOT PATH) must
--      exist, asserted POSITIVELY rather than as some other scenario's mirror image.

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

case_a AS (
    SELECT 'LANDED_REVENUE_IS_NULL' AS failure, project_id, connector || '/' || metric AS detail
    FROM normalized WHERE landed_revenue_micros IS NULL
),

case_b AS (
    SELECT 'EMPTY_STRING_COLUMN_IS_NULL' AS failure, project_id, connector AS detail
    FROM normalized WHERE gap_codes IS NULL OR applied_rule_ids IS NULL
),

case_c AS (
    SELECT 'HT_PRESENT_DESPITE_SALES_TAX_RULE_ABSENT' AS failure, project_id,
           CAST(net_revenue_ht_micros AS STRING) AS detail
    FROM normalized
    WHERE gap_sales_tax_rule_absent AND net_revenue_ht_micros IS NOT NULL
    UNION ALL
    SELECT 'HT_MISSING_ON_A_COMPLETE_TYPED_ROW' AS failure, project_id,
           landed_basis AS detail
    FROM normalized
    WHERE is_normalization_complete
      AND landed_basis <> 'UNRESOLVED'
      AND net_revenue_ht_micros IS NULL
),

case_d AS (
    SELECT 'DERIVED_ZERO_ON_A_GAPPED_ROW' AS failure, project_id, gap_codes AS detail
    FROM normalized
    WHERE (gap_vat_rate_invalid AND sales_tax_micros = 0)
       OR (gap_transaction_count_unavailable AND payment_fee_micros = 0)
       OR (gap_revenue_form_unsupported AND sales_tax_micros = 0)
       OR (gap_currency_mismatch AND payment_fee_micros = 0)
),

case_e AS (
    SELECT 'COMPLETENESS_DISAGREES_WITH_GAP_CODES' AS failure, project_id, gap_codes AS detail
    FROM normalized
    WHERE is_normalization_complete <> (gap_codes = '')
),

case_f AS (
    SELECT 'DAY_ONE_HOT_PATH_FIXTURE_MISSING' AS failure,
           'feetaxrev_dev_absent' AS project_id,
           'no row with a populated TTC view, a NULL HT view and SALES_TAX_RULE_ABSENT'
               AS detail
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (
        SELECT 1 FROM normalized
        WHERE project_id = 'feetaxrev_dev_absent'
          AND landed_revenue_micros IS NOT NULL
          AND gross_revenue_ttc_micros IS NOT NULL
          AND net_revenue_ht_micros IS NULL
          AND gap_codes = 'SALES_TAX_RULE_ABSENT'
    )
)

-- G. F2 DEFENCE IN DEPTH. `feetaxrev_dev_formbad` carries a SALES_TAX / SPEND_TIERS rule
-- over GROSS_REVENUE -- a pair migration 119's ck_fee_tax_rules_form_base_target now
-- makes UNREPRESENTABLE at declaration. The DuckDB mirror carries no CHECK, so this is
-- exactly the mirror-lag / pre-constraint row the model's own guard exists for. It must
-- be REFUSED AND TYPED, never silently worth 0 behind a green complete flag: CONSTRAINTS
-- PROTECT THE FUTURE, GUARDS PROTECT THE ROWS ALREADY WRITTEN.
, case_g AS (
    SELECT 'UNROUTABLE_REVENUE_FORM_WAS_NOT_REFUSED' AS failure, project_id,
           gap_codes AS detail
    FROM normalized
    WHERE project_id = 'feetaxrev_dev_formbad'
      AND (NOT gap_revenue_form_unsupported OR net_revenue_ht_micros IS NOT NULL)
    UNION ALL
    SELECT 'UNROUTABLE_FORM_FIXTURE_MISSING', 'feetaxrev_dev_formbad',
           'the silent-zero guard would pass by finding nothing'
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE NOT EXISTS (
        SELECT 1 FROM normalized WHERE project_id = 'feetaxrev_dev_formbad'
    )
)

SELECT * FROM case_a
UNION ALL SELECT * FROM case_b
UNION ALL SELECT * FROM case_c
UNION ALL SELECT * FROM case_d
UNION ALL SELECT * FROM case_e
UNION ALL SELECT * FROM case_f
UNION ALL SELECT * FROM case_g
{%- endif -%}
