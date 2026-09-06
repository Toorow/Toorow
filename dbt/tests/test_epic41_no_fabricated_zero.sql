-- T13 (Story 41.3, AC4 / E41-NFR02) -- NO FABRICATED ZERO.
--
-- REWRITTEN AFTER REVIEW (findings F2, F3, F12). The previous version's three headline
-- blocks -- incomplete_with_total, complete_without_total and gap_flag_disagreement --
-- all derived from the SINGLE expression `gap_codes_prefixed = ''`, so
-- `is_ladder_complete <> (gap_codes = '')` compared a value to itself. They are deleted
-- or labelled as tripwires, and replaced by checks over INDEPENDENT quantities.
--
-- The doctrine is unchanged: a 0 must ALWAYS mean "the composition ran and the answer is
-- zero", never "we could not compute it".
--
--   A. (F2) AN UNROUTABLE RULE MUST GAP, NOT CONTRIBUTE 0. Nothing validates
--      `category x form`, so (AGENCY_FEE, GROSS_UP) passes every CHECK in migration 119
--      and NO phase branch can execute it. Before the routing guard it fell to `ELSE 0`,
--      entered applied_rule_ids and left is_ladder_complete TRUE -- an UNDERSTATED
--      INVOICE REPORTED AS COMPLETE, the single worst output this engine can produce.
--      Story 41.8 hands rule creation to a governed LLM, so it is reachable without
--      anyone writing SQL.
--   B. (F12) A NEGATIVE COMPONENT IS NOT AUTOMATICALLY A BUG. Platform credits,
--      make-goods and refunds land as negative `cost`, so a negative net media makes
--      every rate-derived component legitimately negative. The old blanket "no component
--      may be negative" would have turned that DATA CONDITION into a RED BUILD -- and no
--      fixture carried one, so it was untested in both directions. Now: negative on a
--      NEGATIVE base passes and is ASSERTED to pass; negative on a NON-NEGATIVE base
--      fails.
--   C. net_media_micros is never NULL -- genuinely independent: it is a read of a real
--      fact, so a NULL means the canonical-series join broke, not that a rule sat out.
--   D. Two labelled structural tripwires, tautological today, kept for the day the
--      headline totals stop being gated by that one gap expression.
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
WITH ladder AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }}
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fee_tax_ladder_daily is empty -- nothing to assert'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM ladder) = 0
),

key AS (
    SELECT
        project_id || '|' || CAST(date AS STRING) || '|' || connector
            || '|' || breakdown_value AS subject,
        *
    FROM ladder
),

-- ---------------------------------- A. F2: unroutable must gap, not bill 0 --
unroutable_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no ladder row for feetax_dev_form_bad -- the unroutable'
        || ' (category, form) path is untested, and it is the one that produces an'
        || ' understated invoice under a green flag' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM ladder WHERE project_id = 'feetax_dev_form_bad') = 0
),

unroutable_breaks AS (
    SELECT
        k.subject,
        'UNROUTABLE_FAIL: (AGENCY_FEE, GROSS_UP) must raise RULE_FORM_UNROUTABLE with a'
        || ' NULL phase column and NULL totals -- never +0 micros under'
        || ' is_ladder_complete = TRUE. Got agency='
        || COALESCE(CAST(k.agency_fee_micros AS STRING), 'NULL')
        || ' gap_codes=' || k.gap_codes
        || ' applied=' || k.applied_rule_ids AS failure_reason
    FROM key k
    WHERE k.project_id = 'feetax_dev_form_bad'
      AND NOT (k.gap_rule_form_unroutable
               AND k.gap_codes = 'RULE_FORM_UNROUTABLE'
               AND k.agency_fee_micros IS NULL
               AND k.total_ttc_micros IS NULL
               AND k.applied_rule_ids = '')
),

-- ------------------------------ B. F12: negative is a data condition --------
negative_on_nonnegative_base AS (
    SELECT
        k.subject,
        'NEGATIVE_COMPONENT_FAIL: a component is negative on a NON-NEGATIVE net media --'
        || ' that is a real arithmetic bug, not a credit' AS failure_reason
    FROM key k
    WHERE COALESCE(k.net_media_micros, 0) >= 0
      AND (COALESCE(k.platform_fee_micros, 0)   < 0
        OR COALESCE(k.regulatory_tax_micros, 0) < 0
        OR COALESCE(k.wht_gross_up_micros, 0)   < 0
        OR COALESCE(k.agency_fee_micros, 0)     < 0
        OR COALESCE(k.sales_tax_micros, 0)      < 0)
),

credit_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no NEGATIVE-cost ladder row (feetax_dev_credit) -- the'
        || ' legitimate-credit direction of the sign rule is untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM ladder
           WHERE project_id = 'feetax_dev_credit' AND net_media_micros < 0) = 0
),

credit_breaks AS (
    SELECT
        k.subject,
        'CREDIT_FAIL: a platform credit (negative cost) must compose normally -- a'
        || ' negative component and a COMPLETE ladder, not a gap and not a red build.'
        || ' Got platform=' || COALESCE(CAST(k.platform_fee_micros AS STRING), 'NULL')
        || ' complete=' || CAST(k.is_ladder_complete AS STRING) AS failure_reason
    FROM key k
    WHERE k.project_id = 'feetax_dev_credit'
      AND NOT (k.net_media_micros < 0
               AND k.platform_fee_micros < 0
               AND k.is_ladder_complete
               AND k.total_ttc_micros < 0)
),

-- ------------------------------------------------- C. real: the fact read ---
null_net_media AS (
    SELECT
        k.subject,
        'NULL_NET_MEDIA_FAIL: net_media_micros is NULL -- it is a read of a real fact,'
        || ' so this means the canonical-series join broke' AS failure_reason
    FROM key k WHERE k.net_media_micros IS NULL
),

-- ------------------------- D. STRUCTURAL TRIPWIRES (tautological today) -----
structural_tripwires AS (
    SELECT k.subject,
           'STRUCTURAL_FAIL: an INCOMPLETE row carries a headline total -- gap_codes='
               || k.gap_codes AS failure_reason
    FROM key k
    WHERE NOT k.is_ladder_complete
      AND (k.subtotal_ht_micros IS NOT NULL OR k.total_ttc_micros IS NOT NULL)
    UNION ALL
    SELECT k.subject,
           'STRUCTURAL_FAIL: a COMPLETE row has no headline total'
    FROM key k
    WHERE k.is_ladder_complete
      AND (k.subtotal_ht_micros IS NULL OR k.total_ttc_micros IS NULL)
    UNION ALL
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) || '|' || r.rollup_kind
            || '|' || r.rollup_key,
        'STRUCTURAL_FAIL: an INCOMPLETE rollup key carries a headline total'
    FROM {{ ref('fee_tax_ladder_rollup') }} r
    WHERE NOT r.is_ladder_complete
      AND (r.subtotal_ht_micros IS NOT NULL OR r.total_ttc_micros IS NOT NULL)
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM unroutable_guard
UNION ALL SELECT subject, failure_reason FROM unroutable_breaks
UNION ALL SELECT subject, failure_reason FROM negative_on_nonnegative_base
UNION ALL SELECT subject, failure_reason FROM credit_guard
UNION ALL SELECT subject, failure_reason FROM credit_breaks
UNION ALL SELECT subject, failure_reason FROM null_net_media
UNION ALL SELECT subject, failure_reason FROM structural_tripwires
{%- endif -%}
