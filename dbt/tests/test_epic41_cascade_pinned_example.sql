-- T4 (Story 41.3, AC1 / AC2 / AC6 / AC7) -- the PINNED end-to-end arithmetic.
--
-- TWO HALVES, and review finding F3 is the reason the second one exists. The first half
-- replays scenarios S1 / S2 through the macros and pins the ROUNDING; on its own that
-- is NOT a test of the ladder -- it re-implements all six phases inside the test, so a
-- ladder that chained the phases wrongly (every phase reading net_media_micros instead
-- of the running subtotal) would still have passed it, and no other test pinned an
-- absolute cascade number. The second half fixes that by asserting the LIVE
-- fee_tax_ladder_daily row against hand-derived literals.
--
--   HALF A -- fixture replay: pins the two MACROS and the rounding mode.
--   HALF B -- live pin: pins the LADDER. One assertion covers the phase chaining, the
--             intra-phase parallelism (two platform fees at different slots both firing
--             on the SAME phase-entry base), the gross-up rounding boundary, and the
--             precedence pick -- because every one of those changes at least one of the
--             eight literals below.
--
-- S1 (12 345.67 EUR, platform 3 % / DST 3 % / WHT 15 % / agency 10 % / VAT 20 %):
--     12 345 670 000 -> +370 370 100 -> +381 481 203 -> +2 311 327 289
--                    -> +1 540 884 859 = 16 949 733 451 -> +3 389 946 690
--                    = 20 339 680 141
-- S2 is the ROUNDING-MODE DISCRIMINATOR: 12 716 040 300 x 0.055 = 699 382 216.5, an
-- exact half-micro. ROUND_HALF_UP gives 699_382_217; Python's banker's rounding in
-- server/core/money.py would give 699_382_216. The SQL side is the authority for the
-- ladder (arbitration B2) and this is where that divergence is PINNED rather than
-- discovered later. S2 is also AC2 + AC3: two of its three rules are KNOWN FALSE, each
-- contributes exactly +0, and the row stays COMPLETE.
--
-- CARDINALITY GUARD: both scenarios must be present in the fixture.
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
WITH fixture AS (
    SELECT *
    FROM {{ ref('epic41_cascade_fixture') }}
    WHERE scenario IN ('ladder_full_fr', 'known_false_de')
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: epic41_cascade_fixture is missing ladder_full_fr or'
        || ' known_false_de -- dbt seed not run or the fixture was edited' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(DISTINCT scenario) FROM fixture) < 2
),

-- Per-rule matching, replayed: a rule participates when it is unconditioned, or its
-- single declared condition matches the row's attribute.
rates AS (
    SELECT
        f.scenario                                          AS scenario,
        f.date                                              AS date,
        MAX(f.net_media_micros)                             AS net_media_micros,
        COALESCE(MAX(CASE WHEN f.rule_category = 'PLATFORM_FEE'   AND (f.cond_key IS NULL OR (f.cond_key = 'country' AND f.cond_value = f.attr_country) OR (f.cond_key = 'connector' AND f.cond_value = f.connector)) THEN f.rule_rate END), '0') AS rate_platform,
        COALESCE(MAX(CASE WHEN f.rule_category = 'REGULATORY_TAX' AND (f.cond_key IS NULL OR (f.cond_key = 'country' AND f.cond_value = f.attr_country) OR (f.cond_key = 'connector' AND f.cond_value = f.connector)) THEN f.rule_rate END), '0') AS rate_regulatory,
        COALESCE(MAX(CASE WHEN f.rule_category = 'WHT_GROSS_UP'   AND (f.cond_key IS NULL OR (f.cond_key = 'country' AND f.cond_value = f.attr_country) OR (f.cond_key = 'connector' AND f.cond_value = f.connector)) THEN f.rule_rate END), '0') AS rate_wht,
        COALESCE(MAX(CASE WHEN f.rule_category = 'AGENCY_FEE'     AND (f.cond_key IS NULL OR (f.cond_key = 'country' AND f.cond_value = f.attr_country) OR (f.cond_key = 'connector' AND f.cond_value = f.connector)) THEN f.rule_rate END), '0') AS rate_agency,
        COALESCE(MAX(CASE WHEN f.rule_category = 'SALES_TAX'      AND (f.cond_key IS NULL OR (f.cond_key = 'country' AND f.cond_value = f.attr_country) OR (f.cond_key = 'connector' AND f.cond_value = f.connector)) THEN f.rule_rate END), '0') AS rate_sales,
        MAX(f.expected_platform_fee_micros)                 AS e_platform,
        MAX(f.expected_regulatory_tax_micros)               AS e_regulatory,
        MAX(f.expected_wht_gross_up_micros)                 AS e_wht,
        MAX(f.expected_agency_fee_micros)                   AS e_agency,
        MAX(f.expected_subtotal_ht_micros)                  AS e_subtotal,
        MAX(f.expected_sales_tax_micros)                    AS e_sales,
        MAX(f.expected_total_ttc_micros)                    AS e_total
    FROM fixture f
    GROUP BY f.scenario, f.date
),

ph2 AS (
    SELECT
        r.*,
        {{ fee_tax_pct_of_micros('r.net_media_micros', 'r.rate_platform') }} AS c_platform
    FROM rates r
),

ph3 AS (
    SELECT
        p.*,
        p.net_media_micros + p.c_platform AS s2,
        {{ fee_tax_pct_of_micros('p.net_media_micros + p.c_platform', 'p.rate_regulatory') }} AS c_regulatory
    FROM ph2 p
),

ph4 AS (
    SELECT
        p.*,
        p.s2 + p.c_regulatory AS s3,
        {{ fee_tax_grossup_micros('p.s2 + p.c_regulatory', 'p.rate_wht') }}
            - (p.s2 + p.c_regulatory) AS c_wht
    FROM ph3 p
),

ph5 AS (
    SELECT
        p.*,
        p.s3 + p.c_wht AS s4,
        {{ fee_tax_pct_of_micros('p.s3 + p.c_wht', 'p.rate_agency') }} AS c_agency
    FROM ph4 p
),

ph6 AS (
    SELECT
        p.*,
        p.s4 + p.c_agency AS subtotal_ht,
        {{ fee_tax_pct_of_micros('p.s4 + p.c_agency', 'p.rate_sales') }} AS c_sales
    FROM ph5 p
),

computed AS (
    SELECT p.*, p.subtotal_ht + p.c_sales AS total_ttc FROM ph6 p
),

mismatches AS (
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING) AS subject,
        'PINNED_FAIL: platform expected ' || CAST(c.e_platform AS STRING)
            || ' got ' || CAST(c.c_platform AS STRING) AS failure_reason
    FROM computed c WHERE c.c_platform <> c.e_platform
    UNION ALL
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: regulatory expected ' || CAST(c.e_regulatory AS STRING)
            || ' got ' || CAST(c.c_regulatory AS STRING)
    FROM computed c WHERE c.c_regulatory <> c.e_regulatory
    UNION ALL
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: wht gross-up expected ' || CAST(c.e_wht AS STRING)
            || ' got ' || CAST(c.c_wht AS STRING)
    FROM computed c WHERE c.c_wht <> c.e_wht
    UNION ALL
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: agency expected ' || CAST(c.e_agency AS STRING)
            || ' got ' || CAST(c.c_agency AS STRING)
    FROM computed c WHERE c.c_agency <> c.e_agency
    UNION ALL
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: subtotal_ht expected ' || CAST(c.e_subtotal AS STRING)
            || ' got ' || CAST(c.subtotal_ht AS STRING)
    FROM computed c WHERE c.subtotal_ht <> c.e_subtotal
    UNION ALL
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: sales_tax expected ' || CAST(c.e_sales AS STRING)
            || ' got ' || CAST(c.c_sales AS STRING)
            || ' (S2 is the ROUND_HALF_UP discriminator: 699382217, not 699382216)'
    FROM computed c WHERE c.c_sales <> c.e_sales
    UNION ALL
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: total_ttc expected ' || CAST(c.e_total AS STRING)
            || ' got ' || CAST(c.total_ttc AS STRING)
    FROM computed c WHERE c.total_ttc <> c.e_total
    UNION ALL
    -- The waterfall identity on the replayed numbers, tolerance exactly 0.
    SELECT
        c.scenario || '|' || CAST(c.date AS STRING),
        'PINNED_FAIL: the replayed waterfall does not add up at tolerance 0'
    FROM computed c
    WHERE c.net_media_micros + c.c_platform + c.c_regulatory + c.c_wht + c.c_agency
              <> c.subtotal_ht
       OR c.subtotal_ht + c.c_sales <> c.total_ttc
),

-- ============================== HALF B: THE LIVE PIN =========================
-- feetax_dev_complete, campaign fdc_camp_1, net media 12 345.67 EUR, five rules:
--   platform 3 % of NET_MEDIA        + a datastream-scoped platform 1 % of NET_MEDIA
--   (phase 2, DIFFERENT slots -- both fire, BOTH on net media, not on each other)
--   WHT 15 % of the running subtotal   agency 10 %   VAT 20 %
-- Derived by hand, independently of the model:
--   12 345 670 000
--   +   370 370 100 (3 %)  +  123 456 700 (1 %)  =  493 826 800   -> 12 839 496 800
--   + phase 3: no rule                           =            0   -> 12 839 496 800
--   + WHT: ROUND(12 839 496 800 / 0.85) = 15 105 290 353
--          component 15 105 290 353 - 12 839 496 800 = 2 265 793 553
--                                                      -> 15 105 290 353
--   + agency ROUND(x 0.10) = 1 510 529 035           -> subtotal_ht 16 615 819 388
--   + VAT    ROUND(x 0.20) = 3 323 163 878           -> total_ttc   19 938 983 266
-- fdc_camp_2 (1 000.00 EUR) is pinned too: its roundings fall the other way
-- (183 529 412 / 122 352 941 / 269 176 471), so one magnitude cannot mask the other.
live_expected AS (
    SELECT * FROM (VALUES
        ('fdc_camp_1', 12345670000, 493826800, 0, 2265793553, 1510529035,
         16615819388, 3323163878, 19938983266),
        ('fdc_camp_2',  1000000000,  40000000, 0,  183529412,  122352941,
          1345882353,  269176471,  1615058824)
    ) AS v(breakdown_value, net_media, platform, regulatory, wht, agency,
           subtotal_ht, sales_tax, total_ttc)
),

live_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }}
    WHERE project_id = 'feetax_dev_complete'
),

live_cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 6 feetax_dev_complete ladder rows (2 campaigns x 3'
        || ' days), got ' || CAST((SELECT COUNT(*) FROM live_rows) AS STRING)
        || ' -- the live cascade pin cannot run' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM live_rows) <> 6
),

live_mismatches AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'LIVE_CASCADE_FAIL: the ladder does not match the hand-derived cascade.'
        || ' expected net/platform/regulatory/wht/agency/subtotal/vat/total = '
        || CAST(e.net_media AS STRING) || '/' || CAST(e.platform AS STRING) || '/'
        || CAST(e.regulatory AS STRING) || '/' || CAST(e.wht AS STRING) || '/'
        || CAST(e.agency AS STRING) || '/' || CAST(e.subtotal_ht AS STRING) || '/'
        || CAST(e.sales_tax AS STRING) || '/' || CAST(e.total_ttc AS STRING)
        || '  got '
        || CAST(l.net_media_micros AS STRING) || '/'
        || COALESCE(CAST(l.platform_fee_micros AS STRING), 'NULL') || '/'
        || COALESCE(CAST(l.regulatory_tax_micros AS STRING), 'NULL') || '/'
        || COALESCE(CAST(l.wht_gross_up_micros AS STRING), 'NULL') || '/'
        || COALESCE(CAST(l.agency_fee_micros AS STRING), 'NULL') || '/'
        || COALESCE(CAST(l.subtotal_ht_micros AS STRING), 'NULL') || '/'
        || COALESCE(CAST(l.sales_tax_micros AS STRING), 'NULL') || '/'
        || COALESCE(CAST(l.total_ttc_micros AS STRING), 'NULL') AS failure_reason
    FROM live_rows l
    JOIN live_expected e
        ON e.breakdown_value = l.breakdown_value
    WHERE l.net_media_micros      IS DISTINCT FROM e.net_media
       OR l.platform_fee_micros   IS DISTINCT FROM e.platform
       OR l.regulatory_tax_micros IS DISTINCT FROM e.regulatory
       OR l.wht_gross_up_micros   IS DISTINCT FROM e.wht
       OR l.agency_fee_micros     IS DISTINCT FROM e.agency
       OR l.subtotal_ht_micros    IS DISTINCT FROM e.subtotal_ht
       OR l.sales_tax_micros      IS DISTINCT FROM e.sales_tax
       OR l.total_ttc_micros      IS DISTINCT FROM e.total_ttc
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM mismatches
UNION ALL SELECT subject, failure_reason FROM live_cardinality_guard
UNION ALL SELECT subject, failure_reason FROM live_mismatches
{%- endif -%}
