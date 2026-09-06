-- T5b (Story 41.4, AC5) -- the spend link is ANNOTATION, NEVER COMPUTATION.
--
-- Verification arrives from a DIFFERENT connector than the spend it annotates: `ias`
-- lands the IAS Signal API's own campaign id, `meta-ads` lands Meta's. There is NO
-- CONFORMED CAMPAIGN DIMENSION in this repository, so the link is a best-effort literal
-- equality and nothing more. What this test protects is that the WEAKNESS OF THE LINK
-- NEVER CONTAMINATES THE ALLOCATION:
--
--   1. It is a LEFT JOIN and IT NEVER DROPS A ROW. An INNER JOIN would silently delete
--      exactly the verification cost that is hardest to see -- the unlinked kind -- which
--      is the single most likely wrong implementation of this model. Assertion 4 below is
--      the anti-INNER-JOIN check, in BOTH directions.
--   2. An ambiguous match NEVER PICKS ONE. Two spend connectors carrying the same campaign
--      ref is an id COLLISION, and choosing between them would be inventing provenance.
--   3. The link state affects verification_cost_micros, gap_codes and
--      is_allocation_complete in NO WAY. An unlinked row carries its FULL, EXACT cost and
--      is COMPLETE -- the cost is real and owed whether or not we can name the spend row
--      beside it. That is why spend_link_state is not in the gap vocabulary at all.
--
-- AND THE THING NOT TO FILE AS A BUG: UNLINKED_NO_SPEND_MATCH IS THE EXPECTED MAJORITY
-- OUTCOME, because vendor and DSP id spaces usually differ. It is specified behaviour.
-- LINKED_VIA_PLAN_LINE is an ACCEPTED value that must NEVER be emitted (ruling Q2).
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
WITH overlay AS (
    SELECT * FROM {{ ref('fee_tax_verification_allocation') }}
),

alloc AS (
    SELECT * FROM overlay WHERE row_kind = 'allocation'
),

module_on AS (
    SELECT project_id
    -- Story 48.4 / migration 146: the ONE activation authority. This used to read
    -- the legacy preferences flag, which NO Epic-41 model reads any more.
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

-- The canonical measured-impression series, i.e. exactly what the model claims to cover.
canonical_dim AS (
    SELECT
        f.project_id AS project_id,
        f.date       AS date,
        f.connector  AS connector,
        CASE
            WHEN MAX(CASE WHEN f.breakdown_dimension = 'campaign_id' THEN 1 ELSE 0 END) = 1
                THEN 'campaign_id'
            ELSE MIN(f.breakdown_dimension)
        END          AS dim
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m ON m.project_id = f.project_id
    WHERE f.metric = 'measured_impressions'
    GROUP BY f.project_id, f.date, f.connector
),

fact_keys AS (
    SELECT DISTINCT
        f.project_id                    AS project_id,
        CAST(f.date AS DATE)            AS date,
        f.connector                     AS connector,
        f.breakdown_value               AS breakdown_value
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m ON m.project_id = f.project_id
    JOIN canonical_dim cd
        ON  cd.project_id = f.project_id
        AND cd.date       = f.date
        AND cd.connector  = f.connector
        AND cd.dim        = f.breakdown_dimension
    WHERE f.metric = 'measured_impressions'
),

all_fact_rows AS (
    SELECT DISTINCT
        f.project_id, CAST(f.date AS DATE) AS date, f.connector, f.breakdown_value
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m ON m.project_id = f.project_id
    WHERE f.metric = 'measured_impressions'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no allocation rows at all -- run'
        || ' dbt/seeds/feetax/seed_fee_tax_verification.py and rebuild fact_daily_kpi'
        || ' BEFORE the overlay' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM alloc) = 0
),

anti_vacuity_guard AS (
    -- All THREE emitted link states must occur somewhere, or the corresponding branch of
    -- the ladder is untested.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'ANTI_VACUITY_FAIL: the overlay does not exhibit all three emitted spend_link'
        || ' states (LINKED_VIA_CAMPAIGN_REF / UNLINKED_NO_SPEND_MATCH /'
        || ' UNLINKED_AMBIGUOUS_SPEND_MATCH), so at least one rung of the link ladder is'
        || ' untested' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(DISTINCT spend_link_state)
        FROM alloc
        WHERE spend_link_state IN (
            'LINKED_VIA_CAMPAIGN_REF',
            'UNLINKED_NO_SPEND_MATCH',
            'UNLINKED_AMBIGUOUS_SPEND_MATCH'
        )
    ) < 3
),

canonical_series_guard AS (
    -- Honesty about assertion 4's scope: it compares the model against the CANONICAL
    -- series, not against every measured_impressions row. Today those two sets are equal
    -- (ias emits a single breakdown_dimension), and this guard says so out loud -- so if a
    -- future connector lands parallel series, this guard fires and a human decides what
    -- the right comparison is, instead of assertion 4 silently narrowing.
    SELECT
        CAST(NULL AS STRING) AS subject,
        'SCOPE_CHANGED: a verification connector now emits MORE THAN ONE'
        || ' breakdown_dimension for measured_impressions, so "every fact row appears'
        || ' exactly once" is no longer the same claim as "every canonical-series row'
        || ' appears exactly once". Re-read the coverage assertion below before trusting'
        || ' it.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM all_fact_rows) <> (SELECT COUNT(*) FROM fact_keys)
),

-- ------------------------------------------------------- 1. THE RUNG FIRES ---
linked_break AS (
    SELECT
        'feetax_dev_verif_linked|' || a.breakdown_value AS subject,
        'SPEND_LINK_FAIL: ias and meta-ads share the campaign ref shared_camp_1, so rung 1'
        || ' must fire: spend_link_state=LINKED_VIA_CAMPAIGN_REF, spend_link_source'
        || ' non-empty, linked_connector=meta-ads, linked_breakdown_value=shared_camp_1.'
        || ' Got state=' || a.spend_link_state
        || ' source=''' || a.spend_link_source || ''''
        || ' connector=' || COALESCE(a.linked_connector, 'NULL')
        || ' value=' || COALESCE(a.linked_breakdown_value, 'NULL') AS failure_reason
    FROM alloc a
    WHERE a.project_id = 'feetax_dev_verif_linked'
      AND (a.spend_link_state       <> 'LINKED_VIA_CAMPAIGN_REF'
        OR a.spend_link_source      =  ''
        OR a.linked_connector       IS DISTINCT FROM 'meta-ads'
        OR a.linked_breakdown_value IS DISTINCT FROM 'shared_camp_1')
),

unlinked_break AS (
    SELECT
        'feetax_dev_verif_twin_b|' || a.breakdown_value AS subject,
        'SPEND_LINK_FAIL: twin B''s IAS campaign ids (camp_x / camp_y) deliberately differ'
        || ' from its meta id (fvt_camp_1), so the expected -- and correct -- state is'
        || ' UNLINKED_NO_SPEND_MATCH with an EMPTY source and a NULL linked_connector.'
        || ' Got state=' || a.spend_link_state
        || ' source=''' || a.spend_link_source || ''''
        || ' connector=' || COALESCE(a.linked_connector, 'NULL') AS failure_reason
    FROM alloc a
    WHERE a.project_id = 'feetax_dev_verif_twin_b'
      AND (a.spend_link_state  <> 'UNLINKED_NO_SPEND_MATCH'
        OR a.spend_link_source <> ''
        OR a.linked_connector  IS NOT NULL)
),

-- ------------------------------------------------ 2. ONE IS NEVER PICKED -----
ambiguous_break AS (
    SELECT
        'feetax_dev_verif_ambiguous|' || a.breakdown_value AS subject,
        'SPEND_LINK_AMBIGUITY_FAIL: dup_camp_1 exists on BOTH meta-ads and linkedin-ads,'
        || ' which is an id COLLISION. The state must be UNLINKED_AMBIGUOUS_SPEND_MATCH'
        || ' with linked_connector NULL -- ONE IS NEVER PICKED. Got state='
        || a.spend_link_state || ' connector='
        || COALESCE(a.linked_connector, 'NULL') AS failure_reason
    FROM alloc a
    WHERE a.project_id = 'feetax_dev_verif_ambiguous'
      AND (a.spend_link_state       <> 'UNLINKED_AMBIGUOUS_SPEND_MATCH'
        OR a.linked_connector       IS NOT NULL
        OR a.linked_breakdown_value IS NOT NULL
        OR a.spend_link_source      <> '')
),

reserved_state_emitted AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'RESERVED_STATE_EMITTED: LINKED_VIA_PLAN_LINE is RESERVED in accepted_values but'
        || ' must NEVER be emitted (ruling Q2) -- mirror.plan_line_mappings is a media-plan'
        || ' mapping, not a governed cross-connector campaign identity, and it is'
        || ' deliberately not in this model''s dependency set' AS failure_reason
    FROM overlay v
    WHERE v.spend_link_state = 'LINKED_VIA_PLAN_LINE'
),

source_iff_linked_break AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'SPEND_LINK_SOURCE_FAIL: spend_link_source must be non-empty IF AND ONLY IF the'
        || ' row is linked (Story 41.2''s resolution_source convention). Got state='
        || v.spend_link_state || ' source=''' || v.spend_link_source || ''''
            AS failure_reason
    FROM overlay v
    WHERE (v.spend_link_state LIKE 'LINKED%') <> (v.spend_link_source <> '')
),

-- ------------------------------------------- 3. THE LINK AFFECTS NOTHING -----
link_contaminates_break AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'LINK_CONTAMINATION_FAIL: a spend_link state has leaked into the gap vocabulary.'
        || ' The link is ANNOTATION, never COMPUTATION: it may not appear in gap_codes.'
        || ' Got gap_codes=''' || v.gap_codes || '''' AS failure_reason
    FROM overlay v
    WHERE v.gap_codes LIKE '%LINK%'
       OR v.gap_codes LIKE '%SPEND_MATCH%'
),

unlinked_still_complete_break AS (
    SELECT
        'feetax_dev_verif_twin_b|' || a.breakdown_value AS subject,
        'LINK_CONTAMINATION_FAIL: twin B''s UNLINKED rows must still be COMPLETE and carry'
        || ' a POSITIVE cost -- the cost is real and owed whether or not we can name the'
        || ' spend row beside it. Got complete='
        || CAST(a.is_allocation_complete AS STRING)
        || ' cost=' || COALESCE(CAST(a.verification_cost_micros AS STRING), 'NULL')
            AS failure_reason
    FROM alloc a
    WHERE a.project_id = 'feetax_dev_verif_twin_b'
      AND (NOT a.is_allocation_complete
        OR COALESCE(a.verification_cost_micros, 0) <= 0
        OR a.gap_codes <> '')
),

-- --------------------------------- 4. THE ANTI-INNER-JOIN ASSERTION ----------
dropped_row_break AS (
    SELECT
        k.project_id || '|' || CAST(k.date AS STRING) || '|' || k.connector
            || '|' || k.breakdown_value AS subject,
        'ROW_DROPPED_FAIL: this canonical measured_impressions row of a module-ON project'
        || ' produced NO allocation row. The spend link must be a LEFT JOIN: an INNER JOIN'
        || ' here would silently delete exactly the verification cost that is hardest to'
        || ' see.' AS failure_reason
    FROM fact_keys k
    LEFT JOIN alloc a
        ON  a.project_id      = k.project_id
        AND a.date            = k.date
        AND a.connector       = k.connector
        AND a.breakdown_value = k.breakdown_value
    WHERE a.breakdown_value IS NULL
),

invented_row_break AS (
    -- The other direction: an allocation row with no fact behind it would be invented data.
    SELECT
        a.project_id || '|' || CAST(a.date AS STRING) || '|' || a.connector
            || '|' || a.breakdown_value AS subject,
        'ROW_INVENTED_FAIL: this allocation row has no corresponding canonical'
        || ' measured_impressions fact row -- the overlay must READ facts, never mint them'
            AS failure_reason
    FROM alloc a
    LEFT JOIN fact_keys k
        ON  k.project_id      = a.project_id
        AND k.date            = a.date
        AND k.connector       = a.connector
        AND k.breakdown_value = a.breakdown_value
    WHERE k.breakdown_value IS NULL
),

duplicated_row_break AS (
    SELECT
        a.project_id || '|' || CAST(a.date AS STRING) || '|' || a.connector
            || '|' || a.breakdown_value AS subject,
        'ROW_DUPLICATED_FAIL: this key produced ' || CAST(COUNT(*) AS STRING)
        || ' allocation rows; the spend link must never fan a row out' AS failure_reason
    FROM alloc a
    GROUP BY a.project_id, a.date, a.connector, a.breakdown_value
    HAVING COUNT(*) > 1
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM anti_vacuity_guard
UNION ALL SELECT subject, failure_reason FROM canonical_series_guard
UNION ALL SELECT subject, failure_reason FROM linked_break
UNION ALL SELECT subject, failure_reason FROM unlinked_break
UNION ALL SELECT subject, failure_reason FROM ambiguous_break
UNION ALL SELECT subject, failure_reason FROM reserved_state_emitted
UNION ALL SELECT subject, failure_reason FROM source_iff_linked_break
UNION ALL SELECT subject, failure_reason FROM link_contaminates_break
UNION ALL SELECT subject, failure_reason FROM unlinked_still_complete_break
UNION ALL SELECT subject, failure_reason FROM dropped_row_break
UNION ALL SELECT subject, failure_reason FROM invented_row_break
UNION ALL SELECT subject, failure_reason FROM duplicated_row_break
{%- endif -%}
