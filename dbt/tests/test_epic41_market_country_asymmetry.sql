-- T18 (Story 41.3, ruling R4 / §D.3.1 trap) -- resolvability is PER RULE, never PER ROW.
--
-- THE SINGLE MOST LIKELY WRONG IMPLEMENTATION of this engine is to hoist "does this row
-- have an unresolved attribute?" to row level and then null everything. Story 41.2's
-- frozen contract explicitly allows attr_market to be NON-NULL while attr_country is
-- NULL, so on ONE AND THE SAME ROW a market-conditioned rule must evaluate MATCH while a
-- country-conditioned rule evaluates UNRESOLVED. Hoisting drags the market rule down with
-- the country rule and silently kills a rule that was perfectly evaluable.
--
-- The same failure has a second, more common shape, and it is asserted here too: the
-- bridge sets gap_code='COUNTRY_UNRESOLVED' on essentially EVERY paid-media row. If that
-- were propagated, every project on the platform would lose its totals -- including ones
-- whose rules constrain nothing but connector.
--
-- Three assertions the live warehouse can carry today:
--   A. PARTIAL-BUT-HONEST. feetax_dev_gapped has gapped phases 3 and 6 and a perfectly
--      evaluable UNCONDITIONED rule in phase 5. Phase 5 MUST stay populated. A row-level
--      hoist makes it NULL, and this is the discriminator.
--      CORRECTION TO MY OWN ROUND-1 AUDIT: I listed this block as "near-tautological".
--      That was WRONG and the audit is corrected here. It is REAL -- grouping
--      `rule_state` by row_key instead of (row_key, rule_id), i.e. the exact hoist this
--      header warns about, turns it red. What it is NOT is UNIQUE: T17's anti-vacuity
--      guard catches the same mutation from the other side. Two witnesses to the single
--      most likely wrong implementation of this model is deliberate -- but it is ONE
--      piece of evidence, not two.
--   A2. SCOPE GAPS ARE ATTRIBUTED TO THEIR OWN RULE'S PHASE -- the block that IS unique.
--      On feetax_dev_scope the DATASTREAM_SCOPE_AMBIGUOUS gap comes from a PLATFORM_FEE
--      rule and the RULE_SCOPE_UNRESOLVED gap from a REGULATORY_TAX rule, so phases 2
--      and 3 must be NULL while phases 4, 5 and 6 -- which carry NO rule at all -- must
--      report a REAL ZERO. Mis-attributing a scope gap to another phase, or letting any
--      gap null every phase, passes T15 (which checks only the flags and the headline
--      totals), passes T17 (feetax_dev_scope has no country gap at all) and passes T13's
--      tripwires. This is the only assertion in the suite that pins scope-gap phase
--      attribution.
--   B. NO BRIDGE PROPAGATION. feetax_dev_complete's rules constrain nothing unresolvable,
--      so every one of its rows must be COMPLETE regardless of what the bridge reports.
--   C. THE FLAGS MOVE INDEPENDENTLY. gap_market_unresolved must never be set merely
--      because gap_country_unresolved is, and vice versa: a gap flag may only be raised
--      by a rule that ACTUALLY CONSTRAINS that attribute. On feetax_dev_gapped only
--      `country` is constrained, so gap_market_unresolved must be FALSE on every row even
--      though gap_country_unresolved is TRUE on all of them.
--
-- Plus the fixture-side statement of the ideal scenario S13 (a row where attr_market
-- resolves and attr_country does not), kept as the pinned expectation for the day the
-- Epic-37 binding rung is populated and the case becomes reproducible live.
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
WITH gapped AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_gapped'
),

complete_project AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_complete'
),

cardinality_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: the asymmetry discriminator needs both feetax_dev_gapped and'
        || ' feetax_dev_complete rows' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM gapped) = 0
       OR (SELECT COUNT(*) FROM complete_project) = 0
),

-- A. a gapped row must still carry the components of the rules that WERE evaluable.
hoisted_to_row_level AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.breakdown_value AS subject,
        'HOIST_FAIL: an UNCONDITIONED agency rule was nulled on a row whose only gap comes'
        || ' from a COUNTRY-conditioned rule in another phase. Resolvability must be'
        || ' rolled up per (row, rule), never per row.' AS failure_reason
    FROM gapped g
    WHERE g.gap_country_unresolved
      AND g.agency_fee_micros IS NULL
),

-- A2. scope gaps land on THEIR OWN rule's phase; phases with no rule stay a real 0.
scope_rows AS (
    SELECT * FROM {{ ref('fee_tax_ladder_daily') }} WHERE project_id = 'feetax_dev_scope'
),

scope_attribution_guard AS (
    SELECT
        CAST(NULL AS STRING) AS subject,
        'CARDINALITY_FAIL: no ladder row for feetax_dev_scope -- scope-gap phase'
        || ' attribution is untested, and no other test covers it' AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM scope_rows) = 0
),

scope_attribution_breaks AS (
    SELECT
        s.project_id || '|' || CAST(s.date AS STRING) || '|' || s.breakdown_value AS subject,
        'SCOPE_ATTRIBUTION_FAIL: the DATASTREAM_SCOPE_AMBIGUOUS gap comes from a'
        || ' PLATFORM_FEE rule and RULE_SCOPE_UNRESOLVED from a REGULATORY_TAX rule, so'
        || ' phases 2 and 3 must be NULL and phases 4/5/6 -- which carry NO rule -- must'
        || ' be a REAL 0. Got platform='
        || COALESCE(CAST(s.platform_fee_micros AS STRING), 'NULL')
        || ' regulatory=' || COALESCE(CAST(s.regulatory_tax_micros AS STRING), 'NULL')
        || ' wht=' || COALESCE(CAST(s.wht_gross_up_micros AS STRING), 'NULL')
        || ' agency=' || COALESCE(CAST(s.agency_fee_micros AS STRING), 'NULL')
        || ' sales=' || COALESCE(CAST(s.sales_tax_micros AS STRING), 'NULL')
            AS failure_reason
    FROM scope_rows s
    WHERE NOT (s.platform_fee_micros   IS NULL
               AND s.regulatory_tax_micros IS NULL
               AND s.wht_gross_up_micros   = 0
               AND s.agency_fee_micros     = 0
               AND s.sales_tax_micros      = 0)
),

-- B. the bridge's own gap_code must not drive is_ladder_complete.
bridge_propagated AS (
    SELECT
        c.project_id || '|' || CAST(c.date AS STRING) || '|' || c.breakdown_value AS subject,
        'BRIDGE_GAP_LEAK_FAIL: a project whose rules constrain nothing unresolvable lost'
        || ' its totals (gap_codes=' || c.gap_codes || '). The bridge gap_code is'
        || ' provenance only.' AS failure_reason
    FROM complete_project c
    WHERE NOT c.is_ladder_complete
),

-- C. the country and market flags move independently.
flags_coupled AS (
    SELECT
        g.project_id || '|' || CAST(g.date AS STRING) || '|' || g.breakdown_value AS subject,
        'FLAG_COUPLING_FAIL: gap_market_unresolved is set on a project whose rules'
        || ' constrain only `country` -- a gap may only be raised by a rule that ACTUALLY'
        || ' constrains that attribute' AS failure_reason
    FROM gapped g
    WHERE g.gap_market_unresolved
),

spurious_flags AS (
    SELECT
        l.project_id || '|' || CAST(l.date AS STRING) || '|' || l.breakdown_value AS subject,
        'SPURIOUS_GAP_FAIL: a placement_type / tax_code / source_type gap was raised on a'
        || ' row whose rules declare no such condition' AS failure_reason
    FROM {{ ref('fee_tax_ladder_daily') }} l
    WHERE l.project_id IN ('feetax_dev_complete', 'feetax_dev_gapped')
      AND (l.gap_placement_type_unresolved
           OR l.gap_tax_code_unresolved
           OR l.gap_source_type_unresolved
           OR l.gap_condition_key_unknown)
),

-- The pinned S13 expectations, asserted on the fixture so an edit that erased the
-- asymmetry case is caught even while the live case is unreachable.
fixture_asymmetry AS (
    SELECT
        'market_vs_country_asymmetry' AS subject,
        'FIXTURE_FAIL: scenario market_vs_country_asymmetry must contain BOTH directions'
        || ' -- one row with attr_market resolved and attr_country NULL expecting'
        || ' COUNTRY_UNRESOLVED, and its mirror image expecting MARKET_UNRESOLVED'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (
        SELECT COUNT(DISTINCT COALESCE(expected_gap_codes, ''))
        FROM {{ ref('epic41_cascade_fixture') }}
        WHERE scenario = 'market_vs_country_asymmetry'
    ) < 2
    UNION ALL
    SELECT
        'market_vs_country_asymmetry',
        'FIXTURE_FAIL: the market-conditioned rule must still carry a NON-NULL expected'
        || ' component on the row whose country is unresolved -- that is the whole point'
        || ' of R4'
    FROM {{ ref('epic41_cascade_fixture') }} f
    WHERE f.scenario = 'market_vs_country_asymmetry'
      AND f.cond_key = 'market'
      AND f.expected_platform_fee_micros IS NULL
)

SELECT subject, failure_reason FROM cardinality_guard
UNION ALL SELECT subject, failure_reason FROM hoisted_to_row_level
UNION ALL SELECT subject, failure_reason FROM scope_attribution_guard
UNION ALL SELECT subject, failure_reason FROM scope_attribution_breaks
UNION ALL SELECT subject, failure_reason FROM bridge_propagated
UNION ALL SELECT subject, failure_reason FROM flags_coupled
UNION ALL SELECT subject, failure_reason FROM spurious_flags
UNION ALL SELECT subject, failure_reason FROM fixture_asymmetry
{%- endif -%}
