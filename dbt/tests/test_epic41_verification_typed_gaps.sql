-- T5a (Story 41.4, AC4 / AC12 / E41-NFR02 / ruling Q4) -- typed gaps, the coverage state
-- that is NOT a gap, and honest NULLs. Never a fabricated 0, never a silent drop.
--
-- THE DISTINCTION THIS TEST EXISTS TO PROTECT, because it is the one a future reader will
-- try to "harmonise":
--   * A confirmed rule that DECLARED a computation which could not be carried out is a
--     GAP -- VERIFICATION_BASE_MISSING, CURRENCY_MISMATCH, and the rest.
--   * Real impressions with NO rule reaching them is a KNOWN fact, not an unresolvable
--     one. There is simply no cost to compute. It gets coverage_state NO_RULE_DECLARED,
--     contributes NO gap code, and leaves is_allocation_complete TRUE.
-- A client who runs IAS and is billed for it OUTSIDE the platform is an ordinary
-- configuration; under the rejected design every one of their rows would carry a gap flag
-- forever, and A FLAG THAT IS ALWAYS ON IS WORSE THAN NO FLAG. is_allocation_complete
-- must keep meaning exactly one thing.
--
-- The cost is NULL and never 0 in both cases, but for different reasons, and the
-- coverage_state is what tells them apart. A 0 would ASSERT "verification cost for this
-- campaign is zero", which we do not know.
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

no_rule_rows AS (
    SELECT * FROM overlay
    WHERE project_id = 'feetax_dev_verif_no_rule' AND row_kind = 'allocation'
),

no_base_rows AS (
    SELECT * FROM overlay
    WHERE project_id = 'feetax_dev_verif_no_base' AND row_kind = 'rule_without_base'
),

refused_rows AS (
    SELECT * FROM overlay
    WHERE project_id = 'feetax_dev_verif_refused' AND row_kind = 'allocation'
),

complete_reason_rows AS (
    -- Story 41.3's own project carries a confirmed VERIFICATION CPM rule and NO IAS
    -- facts, so it produces a reason row for free -- and that proves the mechanism fires
    -- for a project THIS story did not author.
    SELECT * FROM overlay
    WHERE project_id = 'feetax_dev_complete' AND row_kind = 'rule_without_base'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: expected 1 no_rule allocation row (got '
        || CAST((SELECT COUNT(*) FROM no_rule_rows) AS STRING)
        || '), 1 no_base reason row (got '
        || CAST((SELECT COUNT(*) FROM no_base_rows) AS STRING)
        || '), 1 refused allocation row (got '
        || CAST((SELECT COUNT(*) FROM refused_rows) AS STRING)
        || '), 1 feetax_dev_complete reason row (got '
        || CAST((SELECT COUNT(*) FROM complete_reason_rows) AS STRING)
        || '). Run both feetax seeders and rebuild fact_daily_kpi first.' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM no_rule_rows)         <> 1
       OR (SELECT COUNT(*) FROM no_base_rows)         <> 1
       OR (SELECT COUNT(*) FROM refused_rows)         <> 1
       OR (SELECT COUNT(*) FROM complete_reason_rows) <> 1
),

-- ------------------------------------ Q4: NO RULE DECLARED IS NOT A GAP ------
no_rule_break AS (
    SELECT
        'feetax_dev_verif_no_rule|' || r.breakdown_value AS subject,
        'COVERAGE_STATE_FAIL: real impressions with no rule must be'
        || ' coverage_state=NO_RULE_DECLARED, verification_cost_micros NULL (NOT 0),'
        || ' gap_codes='''' and is_allocation_complete TRUE -- "no rule declared" is a'
        || ' KNOWN fact, not an unresolvable one (ruling Q4). Got coverage_state='
        || r.coverage_state
        || ' cost=' || COALESCE(CAST(r.verification_cost_micros AS STRING), 'NULL')
        || ' impressions=' || COALESCE(CAST(r.measured_impressions AS STRING), 'NULL')
        || ' gap_codes=''' || r.gap_codes || ''''
        || ' complete=' || CAST(r.is_allocation_complete AS STRING) AS failure_reason
    FROM no_rule_rows r
    WHERE r.coverage_state           <> 'NO_RULE_DECLARED'
       OR r.verification_cost_micros IS NOT NULL
       OR r.measured_impressions     IS DISTINCT FROM 1000000
       OR r.gap_codes                <> ''
       OR NOT r.is_allocation_complete
),

no_rule_not_zero_break AS (
    -- Asserted EXPLICITLY and separately, because "NULL" and "0" are the two answers a
    -- careless implementation picks between and only one of them is honest.
    SELECT
        'feetax_dev_verif_no_rule|' || r.breakdown_value AS subject,
        'FABRICATED_ZERO_FAIL: the cost is 0, which ASSERTS that verification cost for'
        || ' this campaign is zero. We do not know that -- nobody declared a rate. It must'
        || ' be NULL.' AS failure_reason
    FROM no_rule_rows r
    WHERE r.verification_cost_micros = 0
),

-- ------------------------------------ THE rule_without_base REASON ROW -------
no_base_break AS (
    SELECT
        'feetax_dev_verif_no_base|' || n.breakdown_value AS subject,
        'RULE_WITHOUT_BASE_FAIL: a confirmed CPM rule that priced nothing must surface as a'
        || ' visible reason row -- date NULL, coverage_state=RULE_WITHOUT_BASE,'
        || ' breakdown_value = the rule id, gap_codes=VERIFICATION_BASE_MISSING,'
        || ' is_allocation_complete FALSE -- never a silent drop. Got date='
        || COALESCE(CAST(n.date AS STRING), 'NULL')
        || ' coverage_state=' || n.coverage_state
        || ' breakdown_value=' || n.breakdown_value
        || ' gap_codes=''' || n.gap_codes || ''''
        || ' complete=' || CAST(n.is_allocation_complete AS STRING) AS failure_reason
    FROM no_base_rows n
    WHERE n.date                     IS NOT NULL
       OR n.coverage_state           <> 'RULE_WITHOUT_BASE'
       OR n.breakdown_value          <> 'ftr_dev_verif_no_base_cpm'
       OR n.gap_codes                <> 'VERIFICATION_BASE_MISSING'
       OR n.is_allocation_complete
       OR n.verification_cost_micros IS NOT NULL
       OR n.measured_impressions     IS NOT NULL
       OR n.spend_link_state         <> 'NOT_ATTEMPTED'
       OR n.rule_effective_from      IS NULL
),

complete_reason_break AS (
    SELECT
        'feetax_dev_complete|' || c.breakdown_value AS subject,
        'RULE_WITHOUT_BASE_FAIL: Story 41.3''s feetax_dev_complete carries a confirmed'
        || ' VERIFICATION CPM rule and no IAS facts, so it must produce exactly one reason'
        || ' row for ftr_dev_complete_verification. Got breakdown_value='
        || c.breakdown_value || ' gap_codes=''' || c.gap_codes || '''' AS failure_reason
    FROM complete_reason_rows c
    WHERE c.breakdown_value <> 'ftr_dev_complete_verification'
       OR c.gap_codes       <> 'VERIFICATION_BASE_MISSING'
),

-- ------------------------------------ CROSS-CURRENCY: REFUSED, NOT CONVERTED -
refused_break AS (
    SELECT
        'feetax_dev_verif_refused|' || r.breakdown_value AS subject,
        'CURRENCY_MISMATCH_FAIL: a CPM rule declared in USD on an EUR project must be'
        || ' REFUSED -- verification_cost_micros NULL, gap_codes containing'
        || ' CURRENCY_MISMATCH, is_allocation_complete FALSE. NEVER summed and NEVER'
        || ' converted: converting here would apply FX a SECOND time outside the'
        || ' fx_convert_at_read locus (Epic 39.10). Got cost='
        || COALESCE(CAST(r.verification_cost_micros AS STRING), 'NULL')
        || ' gap_codes=''' || r.gap_codes || '''' AS failure_reason
    FROM refused_rows r
    WHERE r.verification_cost_micros IS NOT NULL
       OR r.gap_codes NOT LIKE '%CURRENCY_MISMATCH%'
       OR r.is_allocation_complete
       -- The honesty check: not the USD amount (100 000 x 2 500 = 250 000 000) and not
       -- any FX-converted variant of it. NULL is the only acceptable answer.
       OR r.measured_impressions IS DISTINCT FROM 100000
),

-- ------------------------------------ GLOBAL HONESTY INVARIANTS -------------
honesty_a AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'HONESTY_FAIL (a): gap_codes <> '''' but verification_cost_micros is NOT NULL --'
        || ' you may look at the parts, you may not read a total we could not compute.'
        || ' gap_codes=''' || v.gap_codes || '''' AS failure_reason
    FROM overlay v
    WHERE v.gap_codes <> '' AND v.verification_cost_micros IS NOT NULL
),

honesty_b AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'HONESTY_FAIL (b): coverage_state=NO_RULE_DECLARED must imply'
        || ' verification_cost_micros IS NULL AND gap_codes = '''' (ruling Q4)'
            AS failure_reason
    FROM overlay v
    WHERE v.coverage_state = 'NO_RULE_DECLARED'
      AND (v.verification_cost_micros IS NOT NULL OR v.gap_codes <> '')
),

honesty_c AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'HONESTY_FAIL (c): coverage_state=PRICED with no gap must yield a NUMBER --'
        || ' a rule reached the row, nothing refused it, so a NULL here means the'
        || ' component silently vanished' AS failure_reason
    FROM overlay v
    WHERE v.coverage_state = 'PRICED'
      AND v.gap_codes      = ''
      AND v.verification_cost_micros IS NULL
),

allocation_shape_break AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'ALLOCATION_SHAPE_FAIL: an allocation row must always carry a real date and a real'
        || ' measured_impressions -- both are reads of real facts, and neither may be NULL'
            AS failure_reason
    FROM overlay v
    WHERE v.row_kind = 'allocation'
      AND (v.date IS NULL OR v.measured_impressions IS NULL)
),

gap_codes_shape_break AS (
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'GAP_CODES_SHAPE_FAIL: gap_codes must never be NULL ('''' when clean), must never'
        || ' start or end with the ''|'' separator, and must never contain'
        || ' VERIFICATION_RULE_MISSING -- that code was RETIRED by ruling Q4 in favour of'
        || ' coverage_state=NO_RULE_DECLARED. Got ''' || COALESCE(v.gap_codes, 'NULL') || ''''
            AS failure_reason
    FROM overlay v
    WHERE v.gap_codes IS NULL
       OR v.gap_codes LIKE '|%'
       OR v.gap_codes LIKE '%|'
       OR v.gap_codes LIKE '%VERIFICATION_RULE_MISSING%'
),

gap_codes_sorted_break AS (
    -- The CASE chain is written alphabetically, so the result is SORTED BY CONSTRUCTION.
    -- This asserts the construction still holds rather than trusting it. Stated honestly:
    -- string_split is BANNED (§B.5), so this cannot be a general "is this list sorted?"
    -- check. It is an INVERSION DETECTOR -- for every ordered pair of codes in the
    -- vocabulary that can co-occur, the LATER code must never appear BEFORE the earlier
    -- one. That catches the realistic regression (someone reorders the CASE chain or
    -- appends a new code at the bottom) without pretending to be exhaustive.
    SELECT
        v.project_id || '|' || COALESCE(v.breakdown_value, '?') AS subject,
        'GAP_CODES_ORDER_FAIL: gap_codes must be alphabetically sorted by construction --'
        || ' the CASE chain in fee_tax_verification_allocation is written in alphabetical'
        || ' order precisely so no cross-engine aggregate ordering has to be trusted.'
        || ' Got ''' || v.gap_codes || '''' AS failure_reason
    FROM overlay v
    WHERE v.gap_codes <> ''
      AND (
          v.gap_codes LIKE '%COUNTRY_UNRESOLVED%CONDITION_KEY_UNKNOWN%'
       OR v.gap_codes LIKE '%CURRENCY_MISMATCH%CONDITION_KEY_UNKNOWN%'
       OR v.gap_codes LIKE '%CURRENCY_MISMATCH%COUNTRY_UNRESOLVED%'
       OR v.gap_codes LIKE '%DATASTREAM_SCOPE_AMBIGUOUS%CURRENCY_MISMATCH%'
       OR v.gap_codes LIKE '%MARKET_UNRESOLVED%DATASTREAM_SCOPE_AMBIGUOUS%'
       OR v.gap_codes LIKE '%PLACEMENT_TYPE_UNRESOLVED%MARKET_UNRESOLVED%'
       OR v.gap_codes LIKE '%RULE_SCOPE_UNRESOLVED%PLACEMENT_TYPE_UNRESOLVED%'
       OR v.gap_codes LIKE '%SOURCE_TYPE_UNRESOLVED%RULE_SCOPE_UNRESOLVED%'
       OR v.gap_codes LIKE '%TAX_CODE_UNRESOLVED%SOURCE_TYPE_UNRESOLVED%'
       OR v.gap_codes LIKE '%VERIFICATION_CPM_OUT_OF_RANGE%TAX_CODE_UNRESOLVED%'
       OR v.gap_codes LIKE '%VERIFICATION_PLAN_SCOPE_UNSUPPORTED%VERIFICATION_CPM_OUT_OF_RANGE%'
      )
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM no_rule_break
UNION ALL SELECT subject, failure_reason FROM no_rule_not_zero_break
UNION ALL SELECT subject, failure_reason FROM no_base_break
UNION ALL SELECT subject, failure_reason FROM complete_reason_break
UNION ALL SELECT subject, failure_reason FROM refused_break
UNION ALL SELECT subject, failure_reason FROM honesty_a
UNION ALL SELECT subject, failure_reason FROM honesty_b
UNION ALL SELECT subject, failure_reason FROM honesty_c
UNION ALL SELECT subject, failure_reason FROM allocation_shape_break
UNION ALL SELECT subject, failure_reason FROM gap_codes_shape_break
UNION ALL SELECT subject, failure_reason FROM gap_codes_sorted_break
{%- endif -%}
