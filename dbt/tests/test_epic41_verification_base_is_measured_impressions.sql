-- T8 (Story 41.4, AC9 / AD-2 / E41-NFR05) -- the allocation base is
-- `measured_impressions` AND NOTHING ELSE.
--
-- THE FAILURE THIS PREVENTS. `impressions` is DELIVERY volume; `measured_impressions` is
-- the subset a verification vendor actually measured. They are different numbers, and a
-- verification invoice is priced on the second. An engine that priced `impressions` would
-- inflate verification cost by the whole UNMEASURED TAIL -- silently, plausibly, and in
-- the wrong direction for the client. `viewable_impressions`, `eligible_impressions` and
-- `monitored_ads` are wrong for the same reason.
--
-- WHAT MAKES IT DISCRIMINATING RATHER THAN DECORATIVE: feetax_dev_verif_mixed_metric emits
-- BOTH metrics on the SAME campaign ref and day -- meta-ads lands impressions = 1000 while
-- ias lands measured_impressions = 250 000. If the model read the wrong metric, or read
-- both, the number moves and this test names it. The anti-vacuity guard asserts the
-- project genuinely emits both, because without that the whole test is an empty gesture.
--
-- 250 000 x 2 500 000 / 1000 = 625 000 000 micros.
--
-- The second half is the AD-2 half: no overlay row may exist for a connector that emits no
-- measured_impressions at all. That is what proves the selection is by METRIC NAME rather
-- than by a connector allow-list -- which is also why DoubleVerify will be supported the
-- day its fact_daily_kpi block lands, with no change to the model.
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
WITH module_on AS (
    SELECT project_id
    -- Story 48.4 / migration 146: the ONE activation authority. This used to read
    -- the legacy preferences flag, which NO Epic-41 model reads any more.
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

mixed_rows AS (
    SELECT *
    FROM {{ ref('fee_tax_verification_allocation') }}
    WHERE project_id = 'feetax_dev_verif_mixed_metric'
      AND row_kind   = 'allocation'
),

mixed_facts AS (
    SELECT
        f.metric        AS metric,
        f.connector     AS connector,
        SUM(f.value)    AS total_value
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.project_id = 'feetax_dev_verif_mixed_metric'
      AND f.metric IN ('impressions', 'measured_impressions')
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.metric, f.connector
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected exactly one feetax_dev_verif_mixed_metric allocation'
        || ' row, got ' || CAST((SELECT COUNT(*) FROM mixed_rows) AS STRING)
        || '. Run dbt/seeds/feetax/seed_fee_tax_verification.py and rebuild'
        || ' fact_daily_kpi.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM mixed_rows) <> 1
),

anti_vacuity_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: feetax_dev_verif_mixed_metric must emit BOTH `impressions`'
        || ' (meta-ads) AND `measured_impressions` (ias) at campaign grain, or "only'
        || ' measured_impressions is priced" is untested -- there would be nothing wrong'
        || ' for the model to pick.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM mixed_facts WHERE metric = 'impressions') = 0
       OR (SELECT COUNT(*) FROM mixed_facts WHERE metric = 'measured_impressions') = 0
),

base_break AS (
    SELECT
        'feetax_dev_verif_mixed_metric|' || m.breakdown_value AS subject,
        'BASE_METRIC_FAIL: the allocation base must be measured_impressions = 250000 and'
        || ' the priced cost 625000000 micros. Got base='
        || COALESCE(CAST(m.measured_impressions AS STRING), 'NULL')
        || ' cost=' || COALESCE(CAST(m.verification_cost_micros AS STRING), 'NULL')
        || '. A base of 1000 would mean `impressions` was priced -- delivery volume, not'
        || ' measured volume.' AS failure_reason
    FROM mixed_rows m
    WHERE m.measured_impressions     IS DISTINCT FROM 250000
       OR m.verification_cost_micros IS DISTINCT FROM 625000000
),

wrong_connector_break AS (
    SELECT
        'feetax_dev_verif_mixed_metric|' || m.connector AS subject,
        'BASE_CONNECTOR_FAIL: an overlay row exists for connector ' || m.connector
        || ', which emits no measured_impressions at all. The base is selected by METRIC'
        || ' NAME, never by connector.' AS failure_reason
    FROM mixed_rows m
    WHERE m.connector <> 'ias'
),

-- The general form of the same claim, across every project: no allocation row may exist
-- for a (project, date, connector) that emits no measured_impressions.
unmeasured_connector_break AS (
    SELECT
        v.project_id || '|' || CAST(v.date AS STRING) || '|' || v.connector AS subject,
        'BASE_CONNECTOR_FAIL: an allocation row exists for a (project, date, connector)'
        || ' with no measured_impressions rows in fact_daily_kpi -- the overlay must price'
        || ' a real measured base or emit nothing' AS failure_reason
    FROM {{ ref('fee_tax_verification_allocation') }} v
    WHERE v.row_kind = 'allocation'
      AND NOT EXISTS (
          SELECT 1
          FROM {{ ref('fact_daily_kpi') }} f
          JOIN module_on m ON m.project_id = f.project_id
          WHERE f.project_id       = v.project_id
            AND CAST(f.date AS DATE) = v.date
            AND f.connector        = v.connector
            AND f.metric           = 'measured_impressions'
      )
),

-- The other never-priced metrics, asserted explicitly so a future edit that widens the
-- predicate to `metric LIKE '%impressions%'` fails loudly.
forbidden_metric_priced AS (
    SELECT
        v.project_id || '|' || v.connector AS subject,
        'BASE_METRIC_FAIL: the overlay''s base for this (project, date, connector) matches'
        || ' the total of a metric OTHER than measured_impressions, which suggests the'
        || ' wrong metric (impressions / viewable_impressions / eligible_impressions /'
        || ' monitored_ads) reached the base' AS failure_reason
    FROM {{ ref('fee_tax_verification_allocation') }} v
    JOIN (
        SELECT
            f.project_id            AS project_id,
            CAST(f.date AS DATE)    AS date,
            f.connector             AS connector,
            f.breakdown_value       AS breakdown_value,
            SUM(f.value)            AS measured_total
        FROM {{ ref('fact_daily_kpi') }} f
        WHERE f.metric = 'measured_impressions'
        GROUP BY f.project_id, CAST(f.date AS DATE), f.connector, f.breakdown_value
    ) k
        ON  k.project_id      = v.project_id
        AND k.date            = v.date
        AND k.connector       = v.connector
        AND k.breakdown_value = v.breakdown_value
    WHERE v.row_kind = 'allocation'
      AND CAST(ROUND(k.measured_total) AS BIGINT) <> v.measured_impressions
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM base_break
UNION ALL SELECT subject, failure_reason FROM wrong_connector_break
UNION ALL SELECT subject, failure_reason FROM unmeasured_connector_break
UNION ALL SELECT subject, failure_reason FROM forbidden_metric_priced
{%- endif -%}
