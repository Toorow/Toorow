-- T12 (Story 41.3, AC1 / AC4 / §D.8) -- the rollups reconcile against the LADDER, and
-- the deleted reason row stays deleted.
--
-- REWRITTEN AFTER THE ROUND-1 AUDIT. The previous version compared
-- `SUM(rollup WHERE kind='connector')` to `rollup WHERE kind='global'` and asserted
-- `complete_row_count <= total_row_count`. Both are PARTLY TAUTOLOGICAL: the rollup
-- UNION ALLs the same ladder four times into one `members` CTE and then GROUPs it, so
-- the four kinds are four groupings of ONE input and SUM is linear -- the connector sum
-- equals the global row whatever the grouping does to the numbers, as long as it does
-- the same thing to both. A wholesale error (wrong grain, dropped member, fanned-out
-- join) moves BOTH sides together and the old test stayed green.
--
-- What replaces it re-aggregates FROM THE LADDER, independently of the rollup's own
-- grouping, and pins absolute literals:
--   A. LADDER -> global. An independent GROUP BY of fee_tax_ladder_daily must equal the
--      `global` row on every count and total, in BOTH directions (no missing key, no
--      extra key).
--   B. LADDER -> country. Same, at the country grain, including the '__unresolved__'
--      bucket. Catches a rollup that keys the country axis wrongly or drops the
--      unresolved rows, which the connector/global pair cannot see.
--   C. ABSOLUTE LITERALS. feetax_dev_complete's global row for 2026-05-01 is pinned at
--      net 13 345 670 000 and total_ttc_micros_complete_only 21 554 042 090
--      (19 938 983 266 + 1 615 058 824, the two campaigns' hand-derived totals). A
--      grouping error that moved every side consistently would still miss these.
--   D. PLAN_LINE IS PROJECT-SCOPED. The permanent regression guard for review finding
--      F4: mirror.plan_line_mappings has NO project_id, so joining a ladder row on
--      (connector, campaign_ref) alone attributed project B's spend to project A's plan
--      whenever an agency connected one ad account into two projects. Every plan_line
--      rollup_key must name a plan that the ROW'S OWN project owns. Nothing else in the
--      suite asserts this.
--   E. Retained real checks: the '__unresolved__' bucket must exist whenever an
--      unresolved-country ladder row does, and rollup_key='__unavailable__' (the D6
--      reason row C4 deleted) must appear nowhere.
--   F. One LABELLED tripwire: complete_row_count <= total_row_count is tautological
--      under the current implementation and is kept only as a refactor guard.
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
WITH rollup AS (
    SELECT * FROM {{ ref('fee_tax_ladder_rollup') }}
),

ladder AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }}
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: fee_tax_ladder_rollup is empty' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM rollup) = 0
),

-- ------------------------------------------------- A. LADDER -> global ------
ladder_global AS (
    SELECT
        project_id, date, currency,
        COUNT(*)                                            AS total_row_count,
        SUM(CASE WHEN is_ladder_complete THEN 1 ELSE 0 END) AS complete_row_count,
        SUM(net_media_micros)                               AS net_media_micros,
        SUM(CASE WHEN is_ladder_complete THEN total_ttc_micros ELSE 0 END)
                                                            AS ttc_complete_only
    FROM ladder
    GROUP BY project_id, date, currency
),

rollup_global AS (
    SELECT project_id, date, currency, total_row_count, complete_row_count,
           net_media_micros, total_ttc_micros_complete_only
    FROM rollup WHERE rollup_kind = 'global'
),

global_breaks AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) AS subject,
        'ROLLUP_FAIL: the global row does not equal an INDEPENDENT re-aggregation of the'
        || ' ladder. expected rows/complete/net/ttc = '
        || CAST(l.total_row_count AS STRING) || '/' || CAST(l.complete_row_count AS STRING)
        || '/' || CAST(l.net_media_micros AS STRING) || '/' || CAST(l.ttc_complete_only AS STRING)
        || '  got ' || CAST(r.total_row_count AS STRING) || '/'
        || CAST(r.complete_row_count AS STRING) || '/'
        || CAST(r.net_media_micros AS STRING) || '/'
        || CAST(r.total_ttc_micros_complete_only AS STRING) AS failure_reason
    FROM ladder_global l
    JOIN rollup_global r
        ON r.project_id = l.project_id AND r.date = l.date AND r.currency = l.currency
    WHERE r.total_row_count                <> l.total_row_count
       OR r.complete_row_count             <> l.complete_row_count
       OR r.net_media_micros               <> l.net_media_micros
       OR r.total_ttc_micros_complete_only <> l.ttc_complete_only
    UNION ALL
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING),
        'ROLLUP_FAIL: a ladder (project, date, currency) has NO global rollup row'
    FROM ladder_global l
    LEFT JOIN rollup_global r
        ON r.project_id = l.project_id AND r.date = l.date AND r.currency = l.currency
    WHERE r.project_id IS NULL
    UNION ALL
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING),
        'ROLLUP_FAIL: a global rollup row has NO ladder rows behind it'
    FROM rollup_global r
    LEFT JOIN ladder_global l
        ON r.project_id = l.project_id AND r.date = l.date AND r.currency = l.currency
    WHERE l.project_id IS NULL
),

-- ------------------------------------------------ B. LADDER -> country ------
ladder_country AS (
    SELECT
        project_id, date, currency,
        COALESCE(attr_country, '__unresolved__')            AS rollup_key,
        COUNT(*)                                            AS total_row_count,
        SUM(net_media_micros)                               AS net_media_micros
    FROM ladder
    GROUP BY project_id, date, currency, COALESCE(attr_country, '__unresolved__')
),

rollup_country AS (
    SELECT project_id, date, currency, rollup_key, total_row_count, net_media_micros
    FROM rollup WHERE rollup_kind = 'country'
),

country_breaks AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.rollup_key AS subject,
        'ROLLUP_FAIL: the country rollup does not equal an INDEPENDENT re-aggregation of'
        || ' the ladder at the country grain' AS failure_reason
    FROM ladder_country l
    JOIN rollup_country r
        ON r.project_id = l.project_id AND r.date = l.date
       AND r.currency = l.currency AND r.rollup_key = l.rollup_key
    WHERE r.total_row_count  <> l.total_row_count
       OR r.net_media_micros <> l.net_media_micros
    UNION ALL
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.rollup_key,
        'ROLLUP_FAIL: a country key present in the ladder is MISSING from the country'
        || ' rollup -- rows are being dropped or re-keyed'
    FROM ladder_country l
    LEFT JOIN rollup_country r
        ON r.project_id = l.project_id AND r.date = l.date
       AND r.currency = l.currency AND r.rollup_key = l.rollup_key
    WHERE r.rollup_key IS NULL
    UNION ALL
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) || '|' || r.rollup_key,
        'ROLLUP_FAIL: the country rollup invented a key the ladder does not carry'
    FROM rollup_country r
    LEFT JOIN ladder_country l
        ON r.project_id = l.project_id AND r.date = l.date
       AND r.currency = l.currency AND r.rollup_key = l.rollup_key
    WHERE l.rollup_key IS NULL
),

-- ----------------------------------------------- C. ABSOLUTE LITERALS -------
pinned_expected AS (
    SELECT * FROM (VALUES
        ('feetax_dev_complete', DATE '2026-05-01', 'global', 'all',
         13345670000, 21554042090, 2, 2),
        -- feetax_dev_auto resolves FR through the posture rung, so its country key is a
        -- REAL country, not the '__unresolved__' bucket. A country axis that stopped
        -- reading attr_country would key this 'FR' row as unresolved and fail here.
        ('feetax_dev_auto', DATE '2026-05-01', 'country', 'FR',
         10000000000, 12360000000, 1, 1)
    ) AS v(project_id, date, rollup_kind, rollup_key,
           net_media, ttc_complete_only, total_rows, complete_rows)
),

pinned_breaks AS (
    SELECT
        e.project_id || '|' || CAST(e.date AS STRING) || '|' || e.rollup_kind
            || '|' || e.rollup_key AS subject,
        'ROLLUP_PIN_FAIL: expected net/ttc_complete_only/rows/complete = '
        || CAST(e.net_media AS STRING) || '/' || CAST(e.ttc_complete_only AS STRING)
        || '/' || CAST(e.total_rows AS STRING) || '/' || CAST(e.complete_rows AS STRING)
        || '  got '
        || COALESCE(CAST(r.net_media_micros AS STRING), 'NO ROW') || '/'
        || COALESCE(CAST(r.total_ttc_micros_complete_only AS STRING), 'NO ROW') || '/'
        || COALESCE(CAST(r.total_row_count AS STRING), 'NO ROW') || '/'
        || COALESCE(CAST(r.complete_row_count AS STRING), 'NO ROW') AS failure_reason
    FROM pinned_expected e
    LEFT JOIN rollup r
        ON  r.project_id  = e.project_id
        AND r.date        = e.date
        AND r.rollup_kind = e.rollup_kind
        AND r.rollup_key  = e.rollup_key
    WHERE r.project_id IS NULL
       OR r.net_media_micros               <> e.net_media
       OR r.total_ttc_micros_complete_only <> e.ttc_complete_only
       OR r.total_row_count                <> e.total_rows
       OR r.complete_row_count             <> e.complete_rows
),

-- --------------------------------- D. F4: plan_line must be PROJECT-SCOPED --
plan_line_rows AS (
    SELECT project_id, date, rollup_key FROM rollup WHERE rollup_kind = 'plan_line'
),

plan_line_anti_vacuity AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: no plan_line rollup row exists, so the F4 cross-project'
        || ' scoping guard below is unfalsifiable. Run seed_fee_tax_mirror.py --'
        || ' feetax_dev_plan maps one campaign to an active line.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM plan_line_rows) = 0
),

plan_line_leaks AS (
    SELECT
        p.project_id || '|' || CAST(p.date AS STRING) || '|' || p.rollup_key AS subject,
        'CROSS_PROJECT_LEAK_FAIL: a plan_line rollup key names a plan this project does'
        || ' NOT own. mirror.plan_line_mappings carries no project_id, so a join on'
        || ' (connector, campaign_ref) alone attributes one project''s spend to another'
        || ' project''s plan whenever an agency connects one ad account into two'
        || ' projects. This is review finding F4 recurring.' AS failure_reason
    FROM plan_line_rows p
    LEFT JOIN {{ source('mirror', 'media_plans') }} mp
        ON  p.rollup_key LIKE mp.id || '|%'
        AND mp.project_id = p.project_id
    WHERE mp.id IS NULL
),

-- ----------------------------------------------- E. retained real checks ----
missing_unresolved_bucket AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) AS subject,
        'ROLLUP_FAIL: a ladder row has an unresolved country but the country rollup has'
        || ' no __unresolved__ bucket for that (project, date) -- an unresolved row must'
        || ' stay countable' AS failure_reason
    FROM (
        SELECT DISTINCT project_id, date, currency
        FROM ladder WHERE attr_country IS NULL
    ) l
    LEFT JOIN rollup r
        ON  r.project_id  = l.project_id
        AND r.date        = l.date
        AND r.currency    = l.currency
        AND r.rollup_kind = 'country'
        AND r.rollup_key  = '__unresolved__'
    WHERE r.rollup_key IS NULL
),

resurrected_reason_row AS (
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) || '|' || r.rollup_kind AS subject,
        'REGRESSION_FAIL: rollup_key __unavailable__ reappeared. C4 removed the grain'
        || ' mismatch at its root (the canonical pick prefers campaign_id); the D6 reason'
        || ' row was deleted and must not be reintroduced.' AS failure_reason
    FROM rollup r
    WHERE r.rollup_key = '__unavailable__'
),

-- ------------------------------- F. LABELLED TRIPWIRE (tautological today) --
-- complete_row_count is SUM(CASE WHEN is_ladder_complete ...) over the same members
-- COUNT(*) counts, so it cannot exceed it. Kept as a refactor guard, NOT as evidence.
count_tripwire AS (
    SELECT
        r.project_id || '|' || CAST(r.date AS STRING) || '|' || r.rollup_kind
            || '|' || r.rollup_key AS subject,
        'STRUCTURAL_FAIL: complete_row_count > total_row_count' AS failure_reason
    FROM rollup r
    WHERE r.complete_row_count > r.total_row_count
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM global_breaks
UNION ALL SELECT subject, failure_reason FROM country_breaks
UNION ALL SELECT subject, failure_reason FROM pinned_breaks
UNION ALL SELECT subject, failure_reason FROM plan_line_anti_vacuity
UNION ALL SELECT subject, failure_reason FROM plan_line_leaks
UNION ALL SELECT subject, failure_reason FROM missing_unresolved_bucket
UNION ALL SELECT subject, failure_reason FROM resurrected_reason_row
UNION ALL SELECT subject, failure_reason FROM count_tripwire
{%- endif -%}
