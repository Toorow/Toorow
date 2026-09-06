-- T3 (Story 41.3, AC1 / AC7) -- the waterfall adds up, ACROSS RELATIONS, tolerance 0.
--
-- REWRITTEN AFTER REVIEW FINDING F3. The previous version asserted
--     net + platform + regulatory + wht + agency = subtotal_ht
-- on fee_tax_ladder_daily and claimed it would catch rounding the gross-up COMPONENT
-- instead of the grossed TOTAL. It would not, and the reviewer proved it: phases 2..6
-- build `subtotal_micros` by ADDING the very component they publish, so the identity is
-- true by construction whatever the components are. Swap in ROUND(base * w/(1-w)) and
-- the old test still passed. That is a tautology dressed as a proof -- lesson L-3 / 17.2
-- recurring.
--
-- What replaces it compares TWO DIFFERENT RELATIONS, which no single view's internal
-- consistency can make true for free:
--   A. LADDER vs ROLLUP. Re-aggregate fee_tax_ladder_daily by (project, date, connector,
--      currency) and require it to equal fee_tax_ladder_rollup's `connector` row
--      exactly. Catches a rollup that silently sums NULL-bearing members, drops rows
--      through a bad join, or fans out.
--   B. NULL PROPAGATION. A rollup phase column must be NULL if and ONLY if at least one
--      member ladder row's is NULL -- the difference between "8 of 11 rows" and a short
--      total presented as a full one.
--   C. A LABELLED structural tripwire. The in-view identity is kept, but honestly
--      described: it is TAUTOLOGICAL under the current implementation and exists only
--      for the day the totals stop being built by accumulation. The numbers themselves
--      are pinned by HALF B of test_epic41_cascade_pinned_example.sql, against
--      hand-derived literals.
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
WITH complete_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE is_ladder_complete
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fee_tax_ladder_daily has no COMPLETE row -- either the dev'
        || ' mirror seeder did not run or every rule is gapping' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM complete_rows) = 0
),

-- ------------------------------------------------- A. LADDER vs ROLLUP ------
ladder_by_connector AS (
    SELECT
        project_id, date, connector, currency,
        COUNT(*)                                            AS n_rows,
        SUM(net_media_micros)                               AS net_media_micros,
        SUM(CASE WHEN is_ladder_complete THEN total_ttc_micros ELSE 0 END)
                                                            AS ttc_complete_only,
        SUM(CASE WHEN is_ladder_complete THEN 1 ELSE 0 END) AS complete_rows,
        COUNT(platform_fee_micros)                          AS n_platform_present,
        SUM(platform_fee_micros)                            AS platform_sum
    FROM {{ ref('fee_tax_ladder_daily') }}
    GROUP BY project_id, date, connector, currency
),

rollup_connector AS (
    SELECT
        project_id, date, rollup_key AS connector, currency,
        net_media_micros, total_ttc_micros_complete_only, total_row_count,
        complete_row_count, platform_fee_micros
    FROM {{ ref('fee_tax_ladder_rollup') }}
    WHERE rollup_kind = 'connector'
),

cross_relation_breaks AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.connector AS subject,
        'CROSS_RELATION_FAIL: the connector rollup does not equal a re-aggregation of'
        || ' the ladder rows it covers' AS failure_reason
    FROM ladder_by_connector l
    JOIN rollup_connector r
        ON  r.project_id = l.project_id
        AND r.date       = l.date
        AND r.connector  = l.connector
        AND r.currency   = l.currency
    WHERE r.net_media_micros               <> l.net_media_micros
       OR r.total_ttc_micros_complete_only <> l.ttc_complete_only
       OR r.total_row_count                <> l.n_rows
       OR r.complete_row_count             <> l.complete_rows
    UNION ALL
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.connector,
        'CROSS_RELATION_FAIL: a ladder (project, date, connector) has no matching'
        || ' connector rollup row -- rows are being dropped'
    FROM ladder_by_connector l
    LEFT JOIN rollup_connector r
        ON  r.project_id = l.project_id
        AND r.date       = l.date
        AND r.connector  = l.connector
        AND r.currency   = l.currency
    WHERE r.connector IS NULL
),

-- ----------------------------------------------- B. NULL PROPAGATION --------
null_propagation_breaks AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.connector AS subject,
        'NULL_PROPAGATION_FAIL: the rollup published a platform_fee_micros total while a'
        || ' member ladder row carries NULL (a short total presented as a full one), or'
        || ' NULLed one where every member is present' AS failure_reason
    FROM ladder_by_connector l
    JOIN rollup_connector r
        ON  r.project_id = l.project_id
        AND r.date       = l.date
        AND r.connector  = l.connector
        AND r.currency   = l.currency
    WHERE (l.n_platform_present = l.n_rows
           AND r.platform_fee_micros IS DISTINCT FROM l.platform_sum)
       OR (l.n_platform_present < l.n_rows
           AND r.platform_fee_micros IS NOT NULL)
),

-- --------------------- C. STRUCTURAL REFACTOR TRIPWIRE (tautological today) -
structural_tripwire AS (
    SELECT
        c.project_id || '|' || CAST(c.date AS STRING) || '|' || c.breakdown_value AS subject,
        'STRUCTURAL_FAIL: the published components no longer sum to the published totals'
        || ' -- the ladder stopped building its totals by accumulation' AS failure_reason
    FROM complete_rows c
    WHERE c.net_media_micros + c.platform_fee_micros + c.regulatory_tax_micros
          + c.wht_gross_up_micros + c.agency_fee_micros <> c.subtotal_ht_micros
       OR c.subtotal_ht_micros + c.sales_tax_micros <> c.total_ttc_micros
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM cross_relation_breaks
UNION ALL SELECT subject, failure_reason FROM null_propagation_breaks
UNION ALL SELECT subject, failure_reason FROM structural_tripwire
{%- endif -%}
