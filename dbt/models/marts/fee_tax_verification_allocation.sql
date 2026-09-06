-- fee_tax_verification_allocation: IAS / DoubleVerify CPM verification cost allocated to
-- campaigns BY MEASURED IMPRESSIONS -- a KEEP_SEPARATE overlay, never inside a native
-- total (Epic 41, Story 41.4 / E41-FR04 / E41-AD4 / Epic 27 invariant 4).
--
-- ============================ THIS MART IS A READ ============================
-- It READS fact_daily_kpi (metric='measured_impressions' for the base, metric='cost' for
-- the spend-link KEYS ONLY), fee_tax_rules_effective, Story 41.2's
-- fee_tax_country_resolution, mirror.project_preferences and
-- mirror.fee_tax_rule_conditions. It creates NO new fact row and edits NO existing model.
-- fact_daily_kpi / cross_source_conversions / cross_source_revenue / metric_baselines /
-- dedup_estimate / plan_vs_actual_daily / candidate_full_grain are STRICTLY UNTOUCHED.
-- Nothing pre-existing depends on this view, so the overlay CANNOT change a pre-existing
-- number -- E41-NFR01 is true BY CONSTRUCTION and asserted by
-- server/tests/conformance/test_epic41_verification_overlay_additive.py.
--
-- ===================== WHY THIS MODEL EXISTS AT ALL ==========================
-- IAS and DoubleVerify EMIT NO COST METRIC. server/modules/ias/connector.py's entire raw
-- table is impression/ad COUNTS (measured / viewable / eligible impressions,
-- invalid_traffic_ads, brand_safety_passed_ads, brand_safety_failed_ads) -- no cost, no
-- spend, no amount, no currency. doubleverify likewise emits monitored_ads /
-- measured_impressions / viewable_impressions and nothing monetary. So the money in a
-- verification invoice is NOT OBSERVABLE in the platform: the CPM RATE has to be
-- DECLARED (a confirmed FeeTaxRule, category='VERIFICATION', form='CPM',
-- base_target='MEASURED_IMPRESSIONS'), and the ALLOCATION BASE is a REAL additive metric
-- read from fact_daily_kpi. Any other design would be inventing money.
--
-- ===================== WHAT 41.3 ALREADY PROVED, AND WHAT THIS ADDS ==========
-- dbt/tests/test_epic41_verification_keep_separate.sql (Story 41.3) proves exactly ONE
-- thing: a VERIFICATION rule is EXCLUDED from the cost ladder's rule set -- it never
-- reaches applied_rule_ids and it raises no gap. Until this model landed, "kept separate"
-- was therefore indistinguishable from "not implemented": there was no
-- verification_cost_micros anywhere in the repository. This model makes it PRESENT,
-- EXACT and STRUCTURALLY UNMERGEABLE (§D.7's three guards).
--
-- ===================== doubleverify DOES NOT REACH fact_daily_kpi ============
-- Stated plainly rather than glossed: server/modules/doubleverify/dbt/staging/
-- stg_doubleverify_daily.sql EXISTS (long format, QUALIFY-superseded, grain-tested by its
-- own schema.yml) but NOTHING UNIONs it into fact_daily_kpi. So this overlay finds ZERO
-- DoubleVerify rows today, and that is CORRECT OUTPUT, NOT A BUG.
-- It is also exactly why the base is selected BY METRIC NAME and never by connector name
-- (see below): the day someone adds a doubleverify block emitting
-- metric='measured_impressions', this model prices it WITH NO CHANGE AT ALL.
--
-- ===================== THE BASE IS A METRIC NAME, NOT A CONNECTOR (AD-2) =====
--     WHERE f.metric = 'measured_impressions'
-- That single predicate is the whole source selection, so there is ZERO connector
-- vocabulary in this model (AD-2 / E41-NFR05 -- the posture
-- fee_tax_country_resolution.sql takes at its lines 24-25). Consequences worth stating:
--   * `impressions` (which meta-ads / tiktok-ads / linkedin-ads / adjust DO emit) is
--     NEVER priced. It is DELIVERY volume, not MEASURED volume; pricing it would inflate
--     verification cost by the whole unmeasured tail.
--   * `viewable_impressions`, `eligible_impressions`, `monitored_ads` are never priced
--     either. base_target='MEASURED_IMPRESSIONS' in the rule and
--     metric='measured_impressions' in the fact are the two ends of ONE contract, and
--     this model is the only place they meet.
--
-- ===================== THREE ORTHOGONAL STATE COLUMNS -- DO NOT CONFLATE =====
-- This is the single most likely misreading of the model:
--   coverage_state                        -- is there a RATE to apply at all?
--                                            PRICED | NO_RULE_DECLARED | RULE_WITHOUT_BASE
--   gap_codes + is_allocation_complete    -- did a DECLARED computation fail to run?
--   spend_link_state                      -- can we NAME the spend row this sits beside?
-- is_allocation_complete means EXACTLY ONE THING: "a declared computation could not be
-- carried out". "No rule was declared" is a KNOWN fact, not an unresolvable one (the
-- epic's B1 / E41-AD9 known-false vs unresolvable distinction), so it is a coverage_state
-- and NEVER a gap. A client who runs IAS and is billed for it OUTSIDE the platform is an
-- ordinary configuration; flagging every one of their rows forever would train people to
-- ignore the flag -- A FLAG THAT IS ALWAYS ON IS WORSE THAN NO FLAG.
-- Hence NULL, not 0, when no rule was declared: a 0 would ASSERT "verification cost for
-- this campaign is zero", which we do not know. NULL + NO_RULE_DECLARED says the true
-- thing -- "nobody told us the rate" -- while measured_impressions stays FULLY VISIBLE.
-- VERIFICATION_RULE_MISSING is deliberately NOT in the gap vocabulary; it was retired.
--
-- ===================== THE ONE LEGITIMATE ZERO ===============================
-- measured_impressions = 0 yields verification_cost_micros = 0, coverage_state='PRICED',
-- is_allocation_complete = TRUE. A real zero base is a real zero cost; that is the only 0
-- this model may ever emit (the ladder's "a 0 always means the composition ran").
--
-- ===================== ALLOCATION IS PER-ROW, NEVER "TOTAL THEN SPLIT" =======
-- The component is computed PER (row x rule) by fee_tax_cpm_micros -- ONE rounding
-- boundary each, ROUND_HALF_UP -- and then SUMmed as exact BIGINTs across rules. Never
-- ROUND(SUM(impressions) * rate) at a coarser grain and never a proportional re-split:
-- that would put the single rounding boundary at the wrong grain AND create an allocation
-- residue that has to be dumped on some arbitrary campaign. Because CPM is LINEAR, the
-- per-campaign formula IS the allocation by measured impressions, and the day total is
-- simply the integer SUM of the campaign figures -- ZERO ALLOCATION RESIDUE.
--
-- ===================== NO FX IS APPLIED AND NONE IS CARRIED ==================
-- Three independent reasons:
--   1. THE BASE IS A COUNT, NOT MONEY. IAS rows land fx_rate / fx_as_of_date / fx_source
--      / fx_tier as NULL in fact_daily_kpi because there is nothing to convert.
--   2. THE RATE IS A RULE-DECLARED AMOUNT whose currency must already equal the project's
--      canonical currency, or the component is REFUSED (below).
--   3. Carrying an all-NULL FX block would invite a downstream reader to believe a
--      conversion happened. State the absence instead.
-- So this model projects NO fx_* COLUMN AT ALL -- a deliberate, documented divergence
-- from fee_tax_ladder_daily, which does carry them because its base IS money.
--
-- ===================== REFUSALS: NULL + A CODE, NEVER 0, NEVER CONVERTED =====
--   CURRENCY_MISMATCH                    -- 41.3's spelling exactly. The row's currency
--     is the project's canonical_currency; migration 119 REQUIRES a CPM rule to declare
--     its own. When they differ the component is refused -- NOT SUMMED, NOT CONVERTED.
--     Converting would apply FX a SECOND time outside the fx_convert_at_read locus, which
--     Epic 39.10 forbids. The comparison is the maximally-safe form the ladder uses: a
--     rule declaring no currency is treated as declaring the row's own, the only reading
--     that cannot fabricate a mismatch.
--   VERIFICATION_CPM_OUT_OF_RANGE        -- cpm_micros NULL, negative, or >= 1e10 (the
--     macro's exact-arithmetic capacity, above which the CAST would RAISE). Fail closed.
--   VERIFICATION_PLAN_SCOPE_UNSUPPORTED  -- see the next block.
--   DATASTREAM_SCOPE_AMBIGUOUS / RULE_SCOPE_UNRESOLVED -- reused verbatim from 41.3.
--   The six condition gaps -- emitted by the SHARED MACRO (below).
--
-- ===================== plan_version SCOPE IS REFUSED ON PURPOSE ==============
-- 41.3 resolves plan-scoped rules through mirror.plan_line_mappings, weighting the rate
-- by split_weight. THIS MODEL DOES NOT, AND MUST NOT:
--   * split_weight is defined as a share of a CAMPAIGN'S SPEND mapped to a media-plan
--     LINE. There is no such thing as a share of a campaign's MEASURED IMPRESSIONS in
--     that table; applying the spend weight to an impression count would INVENT an
--     allocation.
--   * Worse, it would fail SILENTLY IN THE WRONG DIRECTION. The mapping keys on
--     (connector, campaign_ref) OF THE SPEND CONNECTOR, while a verification row carries
--     the VERIFICATION VENDOR's connector and campaign ref. It would essentially never
--     match, the ladder's SCOPE_NOT_APPLICABLE branch would classify every row as
--     known-false +0, and the project would report ZERO VERIFICATION COST UNDER A CLEAN,
--     COMPLETE-LOOKING OVERLAY. That is the worst available failure mode.
-- So such a rule yields verification_cost_micros IS NULL +
-- VERIFICATION_PLAN_SCOPE_UNSUPPORTED on every row it would have reached -- which also
-- keeps mirror.plan_line_mappings OUT of this model's dependency set entirely.
--
-- ===================== THE MATCHER LIVES IN A SHARED MACRO (Q3) ==============
-- row_attributes_long / cond_eval / rule_state / cond_gap are NOT written here. They have
-- exactly ONE definition site, dbt/macros/fee_tax_condition_matcher.sql, shared with
-- fee_tax_ladder_daily (and, read-only, with 41.5). That is what guarantees the six
-- condition gap codes have ONE spelling and cannot drift between surfaces. A shared macro
-- is COMPILED TEXT, NOT A DATA EDGE, so Guard 1's DAG disjointness is unaffected.
-- If you find yourself typing `attr_unresolved` in this file, you are in the wrong file.
--
-- ===================== ZERO RESOLUTION LOGIC LIVES HERE (R3) =================
-- Country / market / source_type enter through EXACTLY ONE LEFT JOIN on
-- ref('fee_tax_country_resolution') -- 41.2's frozen 14-column contract, which covers
-- EVERY fact_daily_kpi row of a module-ON project and therefore already carries
-- metric='measured_impressions'. No dim_country, no market_bindings, no
-- datastream_country_binding_dim, no datastream_source_types, no derivation ladder. The
-- attributes are carried as OPAQUE PROVENANCE and are never branched on.
-- Note that ias / doubleverify resolve to source_type = UNKNOWN, and THAT IS CORRECT --
-- a verification module sits in the paid_media category but its data_role is neither
-- Spend nor Performance, so fee_tax_source_types' agreement table has no pair for it and
-- COST_CASCADE_SOURCE_TYPES excludes UNKNOWN. That exclusion is precisely what keeps a
-- verification feed OUT of net media. Widening the agreement table so `ias` resolved to
-- PAID_MEDIA would make a verification datastream eligible for the COST cascade -- the
-- exact failure Epic 27 invariant 4 forbids. It is not a defect and was not "fixed".
--
-- ===================== D.1 ANTI-DOUBLE-COUNT (a no-op TODAY, on purpose) =====
-- `ias` emits a SINGLE breakdown_dimension ('campaign_id'), so the canonical collapse
-- changes nothing today. It ships anyway because stg_doubleverify_daily is LONG-FORMAT --
-- its breakdown_dimension comes straight from the raw feed, so a DoubleVerify mart block
-- could easily land two or three parallel series each totalling the day, and without the
-- collapse EVERY VERIFICATION FEE WOULD MULTIPLY. Same rule as the ladder: prefer
-- 'campaign_id', else MIN(breakdown_dimension), per (project, date, connector).
--
-- ===================== THE SPEND LINK IS ANNOTATION, NEVER COMPUTATION =======
-- Verification arrives from a DIFFERENT connector than the spend it annotates. `ias`
-- lands breakdown_value = the IAS Signal API's OWN campaign id; `meta-ads` lands META's.
-- These are different namespaces, coinciding only when the advertiser configured the
-- vendor with the DSP's ids. THERE IS NO CONFORMED CAMPAIGN DIMENSION IN THIS REPOSITORY
-- (Epic 27's conformed dimensions cover country / language / device / event, and the only
-- (connector, campaign_ref) table that exists is a MEDIA-PLAN mapping, not a
-- cross-connector identity).
-- This does not damage the allocation AT ALL, and that is the design point:
-- cost = impressions/1000 x CPM is computed ENTIRELY ON THE VERIFICATION ROW, which IS
-- the campaign it verifies in the vendor's own id space. The allocation is exact and
-- complete WITHOUT EVER TOUCHING SPEND. Matching to spend is annotation (which bar of
-- 41.6's waterfall this sits beside), not computation.
-- Hard rules, all three tested:
--   1. It is a LEFT JOIN and IT NEVER DROPS A ROW. An INNER JOIN here would silently
--      delete exactly the verification cost that is hardest to see -- the unlinked kind --
--      the single most likely wrong implementation of this model.
--   2. It NEVER TOUCHES MONEY. Spend rows are read for their KEYS ONLY; no cost value is
--      ever projected. linked_connector / linked_breakdown_value are keys, so 41.6 can
--      join onward to the ladder itself.
--   3. It NEVER AFFECTS verification_cost_micros, gap_codes, OR is_allocation_complete.
--      An unlinked verification row carries its FULL, EXACT cost and is COMPLETE: the
--      cost is real and owed whether or not we can name the spend row beside it. So
--      spend_link_state is not in the gap vocabulary at all.
-- LINKED_VIA_PLAN_LINE is RESERVED IN accepted_values AND NEVER EMITTED: it would need
-- both sides' (connector, campaign_ref) to map to the same (plan_id, line_key), and
-- nobody has declared such a mapping, so wiring it now is speculative coupling. Adding
-- the rung later is purely additive.
-- READ THIS BEFORE FILING A BUG: with that rung out, linking depends entirely on vendor
-- and DSP sharing an id space -- usually FALSE. So UNLINKED_NO_SPEND_MATCH IS THE
-- EXPECTED MAJORITY OUTCOME, exactly like COUNTRY_UNRESOLVED in the ladder. THAT IS
-- SPECIFIED BEHAVIOUR, NOT A BROKEN BUILD, and it is harmless PRECISELY BECAUSE the
-- overlay is KEEP_SEPARATE: an unlinked row is still a complete, exact, reportable
-- verification cost at campaign grain in the vendor's own id space. Note the converse
-- too, because it is the honest reading of Epic 27 invariant 4: YOU COULD NOT MERGE THIS
-- INTO THE NATIVE TOTAL EVEN IF YOU WANTED TO -- you cannot reliably join to it. The
-- posture and the plumbing agree.
--
-- ===================== KEEP_SEPARATE IS STRUCTURAL, NOT A COMMENT ============
-- Three INDEPENDENT guards, so defeating one is not enough:
--   Guard 1 -- DAG isolation, BIDIRECTIONAL: this model does not ref() either ladder and
--     neither ladder refs it. There is therefore NO DATA PATH from the overlay into any
--     ladder total, whatever anyone writes in either file. That is why the spend link
--     reads fact_daily_kpi rather than the ladder: it keeps the two subgraphs DISJOINT,
--     not merely acyclic.
--   Guard 2 -- the money-column ban: the ONLY *_micros column here is
--     verification_cost_micros, and the executable SQL names none of the ladder's money
--     columns. To sum verification into a total, someone must first CREATE a column to
--     sum it into -- and that fails conformance before it can be wrong.
--   Guard 3 -- the twin-project byte-identity differential
--     (test_epic41_verification_ladder_untouched.sql): two fixture projects identical in
--     every respect except that one also carries a VERIFICATION rule AND IAS facts; their
--     fee_tax_ladder_daily rows must be equal column-for-column at tolerance exactly 0.
-- keep_separate = TRUE and reconciliation_status = 'KEEP_SEPARATE' are SELF-DESCRIBING
-- COLUMNS so a consumer reads the contract off the data (the same token as
-- metric_reconciliation.RouteStatus.KEEP_SEPARATE, whose _keep_separate_decision returns
-- target_mart=None -- there is no place to put a combined number). They are
-- DOCUMENTATION, NOT A GUARD: a column value protects nothing.
--
-- GRAIN (enforced by fee_tax_verification_allocation_grain_unique):
--   project_id | row_kind | COALESCE(CAST(date AS STRING),'__no_date__') | connector
--              | breakdown_dimension | breakdown_value | currency
-- The COALESCE is REQUIRED because a row_kind='rule_without_base' row legitimately
-- carries a NULL date -- a rule that priced nothing has no day, and inventing one
-- (effective_from, say) would be a fabricated value. Hence `date` carries no not_null
-- test; test_epic41_verification_typed_gaps.sql asserts instead that date IS NOT NULL on
-- every row_kind='allocation' row.

-- ============ THE MIRROR MAY NOT BE IN THIS WAREHOUSE AT ALL (AI-314) =======
-- `mirror_sync` writes the mirror to DuckDB and journals `write for <table>
-- deferred (Phase B)` for its BigQuery target, so in production the `mirror`
-- dataset DOES NOT EXIST and every model reading it answered *Not found: Dataset
-- toorow:mirror was not found in location EU* -- which failed the build and made
-- dbt skip every model behind it (measured 2026-08-24; two Projects of three
-- built no mart at all). Without the activation relation no Project can have the
-- module ON, which is the state this model already renders as zero rows, so it
-- is built EMPTY and NAMES what it did not find rather than failing the nightly
-- of a Project that never turned Tax & Fees on.
{{ config(materialized='view') }}

{%- set mirror_missing = toorow_absent_sources('mirror', [
      'fee_tax_rule_conditions',
      'project_tax_fee_activation',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['row_kind', 'string'],
    ['project_id', 'string'],
    ['date', 'date'],
    ['connector', 'string'],
    ['breakdown_dimension', 'string'],
    ['breakdown_value', 'string'],
    ['currency', 'string'],
    ['measured_impressions', 'bigint'],
    ['verification_cost_micros', 'bigint'],
    ['coverage_state', 'string'],
    ['applied_rule_ids', 'string'],
    ['applied_rule_count', 'bigint'],
    ['rule_effective_from', 'date'],
    ['rule_effective_to', 'date'],
    ['spend_link_state', 'string'],
    ['spend_link_source', 'string'],
    ['linked_connector', 'string'],
    ['linked_breakdown_value', 'string'],
    ['attr_country', 'string'],
    ['attr_market', 'string'],
    ['attr_source_type', 'string'],
    ['resolution_source', 'string'],
    ['gap_condition_key_unknown', 'boolean'],
    ['gap_country_unresolved', 'boolean'],
    ['gap_currency_mismatch', 'boolean'],
    ['gap_datastream_scope_ambiguous', 'boolean'],
    ['gap_market_unresolved', 'boolean'],
    ['gap_placement_type_unresolved', 'boolean'],
    ['gap_rule_scope_unresolved', 'boolean'],
    ['gap_source_type_unresolved', 'boolean'],
    ['gap_tax_code_unresolved', 'boolean'],
    ['gap_verification_base_missing', 'boolean'],
    ['gap_verification_cpm_out_of_range', 'boolean'],
    ['gap_verification_plan_scope_unsupported', 'boolean'],
    ['gap_codes', 'string'],
    ['is_allocation_complete', 'boolean'],
    ['keep_separate', 'boolean'],
    ['reconciliation_status', 'string'],
    ['pull_id', 'string'],
]) }}
{%- else %}

WITH module_on AS (
    -- E41-FR01 / C.1. COALESCE(..., FALSE) covers both an explicit FALSE and the mirror
    -- landing the column as NULL; the CAST covers the mirror writer typing an EMPTY
    -- mirrored table's columns as strings. FALSE, or an absent row, means OFF -- and an
    -- OFF project contributes no row anywhere below.
    -- Story 48.4: the ONE activation authority. This used to read
    -- project_preferences.fee_tax_alignment_enabled, a second boolean beside
    -- app.project_capabilities that could disagree with it -- and this was the one
    -- every Tax model believed, so a capability disabled through a Project Change Set
    -- kept producing Tax-derived rows. mirror.project_tax_fee_activation projects
    -- app.project_tax_fee_activation_v, where active requires all three of: the
    -- capability is not disabled, its pin names the Project's CURRENT active
    -- configuration version, and a Rule Ladder version is published.
    --
    -- reporting_currency comes from the confirmed Money Policy (Story 48.3), not from
    -- project_preferences.canonical_currency, whose DEFAULT 'EUR' made every Project
    -- look decided from birth.
    SELECT
        project_id,
        reporting_currency AS canonical_currency
    FROM {{ source('mirror', 'project_tax_fee_activation') }}
    WHERE COALESCE(CAST(tax_fees_active AS BOOLEAN), FALSE)
),

-- --------------------------------------------- D.1 CANONICAL SINGLE SERIES ---
canonical_dim AS (
    SELECT
        f.project_id                                    AS project_id,
        f.date                                          AS date,
        f.connector                                     AS connector,
        CASE
            WHEN MAX(CASE WHEN f.breakdown_dimension = 'campaign_id' THEN 1 ELSE 0 END) = 1
                THEN 'campaign_id'
            ELSE MIN(f.breakdown_dimension)
        END                                             AS dim
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    WHERE f.metric = 'measured_impressions'
    GROUP BY f.project_id, f.date, f.connector
),

-- ------------------------------------------------- THE BASE: A REAL METRIC ---
impression_base AS (
    -- measured_impressions is an integer COUNT, so there is no micros boundary and no
    -- money normalisation here. ROUND before the CAST purely because fact_daily_kpi's
    -- `value` column is a floating type for every connector; impression magnitudes are
    -- far below 2^53 so the aggregate is exact.
    SELECT
        f.project_id                                    AS project_id,
        CAST(f.date AS DATE)                            AS date,
        f.connector                                     AS connector,
        f.breakdown_dimension                           AS breakdown_dimension,
        f.breakdown_value                               AS breakdown_value,
        m.canonical_currency                            AS currency,
        CAST(ROUND(SUM(f.value)) AS BIGINT)             AS measured_impressions,
        -- Per-group provenance convention (AD-7). NO fx_* columns: see the header.
        MAX(f.pull_id)                                  AS pull_id
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    JOIN canonical_dim cd
        ON  cd.project_id = f.project_id
        AND cd.date       = f.date
        AND cd.connector  = f.connector
        AND cd.dim        = f.breakdown_dimension
    WHERE f.metric = 'measured_impressions'
    GROUP BY
        f.project_id, CAST(f.date AS DATE), f.connector,
        f.breakdown_dimension, f.breakdown_value, m.canonical_currency
),

base_rows AS (
    SELECT
        b.project_id || '|' || CAST(b.date AS STRING) || '|' || b.connector
            || '|' || b.breakdown_dimension || '|' || b.breakdown_value  AS row_key,
        b.project_id,
        b.date,
        b.connector,
        b.breakdown_dimension,
        b.breakdown_value,
        b.currency,
        b.measured_impressions,
        b.pull_id
    FROM impression_base b
),

-- ------------------------- ROW ATTRIBUTES: ONE JOIN, ZERO RESOLUTION (R3) ----
-- The seven columns here ARE fee_tax_condition_matcher's input contract.
row_attributes AS (
    SELECT
        x.row_key                       AS row_key,
        x.connector                     AS attr_connector,
        r.attr_country                  AS attr_country,
        r.attr_market                   AS attr_market,
        r.attr_source_type              AS attr_source_type,
        CAST(NULL AS STRING)            AS attr_placement_type,   -- structurally absent
        CAST(NULL AS STRING)            AS attr_tax_code,         -- no emitter, no column
        r.resolution_source             AS resolution_source      -- opaque provenance
    FROM base_rows x
    LEFT JOIN {{ ref('fee_tax_country_resolution') }} r
        ON  r.project_id          = x.project_id
        AND r.date                = x.date
        AND r.connector           = x.connector
        AND r.metric              = 'measured_impressions'
        AND r.breakdown_dimension = x.breakdown_dimension
        AND r.breakdown_value     = x.breakdown_value
),

-- ------------------------------------------------------------- RULE SIDE -----
-- The mirror image of the ladder's own skip rule: everything that is not
-- (VERIFICATION, CPM, MEASURED_IMPRESSIONS) belongs to 41.3 or 41.5 and is SKIPPED WITH
-- NO GAP, because such a rule is not addressed to this model at all. Two cases worth
-- naming so a reviewer does not file them as bugs:
--   * a VERIFICATION rule with form='PERCENTAGE' -- a percentage of WHAT? there is no
--     verification base to take a percentage of. Skipped silently; gapping it would
--     pollute every row of the project.
--   * a CPM rule with base_target='NET_MEDIA' or 'RUNNING_SUBTOTAL' -- 41.3 already
--     treats that as an UNEXECUTABLE rule and says so with RULE_FORM_UNROUTABLE. This
--     model honours the same boundary from the other side and does not adopt it.
verification_rules AS (
    SELECT
        rule_id, project_id, scope_kind, category, form, cpm_micros, rule_currency,
        base_target, cascade_phase, sequence_order, effective_from, effective_to,
        scope_precedence, scope_connector, scope_state
    FROM {{ ref('fee_tax_rules_effective') }}
    WHERE category    = 'VERIFICATION'
      AND form        = 'CPM'
      AND base_target = 'MEASURED_IMPRESSIONS'
),

-- --------------------------------------------------------- CANDIDATE PAIRS ---
-- (impression row x rule) pairs the rule COULD govern: same project, day inside the
-- rule's effective window, scope reaching the row. The per-day window test lives HERE
-- because only this model knows the day; a rule out of window is simply ABSENT for that
-- day -- not NO_MATCH, not a gap. The scope branch is the ladder's, MINUS the plan
-- coverage join, which the header refuses on purpose.
candidate_pairs AS (
    SELECT
        x.row_key                       AS row_key,
        x.project_id                    AS project_id,
        x.date                          AS date,
        x.connector                     AS connector,
        x.currency                      AS currency,
        x.measured_impressions          AS measured_impressions,
        r.rule_id                       AS rule_id,
        r.category                      AS category,
        r.cpm_micros                    AS cpm_micros,
        r.rule_currency                 AS rule_currency,
        r.cascade_phase                 AS cascade_phase,
        r.sequence_order                AS sequence_order,
        r.scope_precedence              AS scope_precedence,
        r.effective_from                AS effective_from,
        CASE
            WHEN r.scope_state = 'UNRESOLVED'  THEN 'SCOPE_UNRESOLVED'
            WHEN r.scope_state = 'AMBIGUOUS'   THEN 'SCOPE_AMBIGUOUS'
            -- Refused, never silently applied and never silently zero (see the header).
            WHEN r.scope_kind = 'plan_version' THEN 'SCOPE_PLAN_UNSUPPORTED'
            ELSE 'SCOPE_APPLIES'
        END                             AS scope_verdict
    FROM base_rows x
    JOIN verification_rules r
        ON  r.project_id = x.project_id
        AND x.date >= r.effective_from
        AND (r.effective_to IS NULL OR x.date <= r.effective_to)
        AND (
                r.scope_kind = 'project'
                -- scope_ref absent from the dim -> we do not even know the connector, so
                -- the blast radius is the whole project.
             OR (r.scope_kind = 'datastream' AND r.scope_state = 'UNRESOLVED')
                -- RESOLVED and AMBIGUOUS both know the connector; AMBIGUOUS gaps that
                -- connector's rows and ONE IS NEVER PICKED.
             OR (r.scope_kind = 'datastream' AND r.scope_connector = x.connector)
             OR  r.scope_kind = 'plan_version'
            )
),

-- ------------------------------- THE SHARED MATCHER: ONE DEFINITION SITE -----
-- Emits row_attributes_long / cond_eval / rule_state / cond_gap. NO MATCHING LOGIC OF
-- THIS MODEL'S OWN. conditions_relation is passed from here so this model keeps its own
-- explicit depends_on edge on mirror.fee_tax_rule_conditions.
{{ fee_tax_condition_matcher('row_attributes', 'candidate_pairs', source('mirror', 'fee_tax_rule_conditions')) }}

evaluated AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.currency,
        p.measured_impressions, p.rule_id, p.category, p.cpm_micros, p.rule_currency,
        p.cascade_phase, p.sequence_order, p.scope_precedence, p.effective_from,
        p.scope_verdict,
        -- COALESCE(..., 0) IS the empty-conditions case: unconstrained == MATCH. An INNER
        -- JOIN here would silently delete every unconditional rule, i.e. most of them.
        CASE
            WHEN p.scope_verdict = 'SCOPE_UNRESOLVED'      THEN 'SCOPE_UNRESOLVED'
            WHEN p.scope_verdict = 'SCOPE_AMBIGUOUS'       THEN 'SCOPE_AMBIGUOUS'
            WHEN p.scope_verdict = 'SCOPE_PLAN_UNSUPPORTED' THEN 'SCOPE_PLAN_UNSUPPORTED'
            WHEN COALESCE(rs.state_rank, 0) = 2            THEN 'UNRESOLVED'
            WHEN COALESCE(rs.state_rank, 0) = 1            THEN 'NO_MATCH'
            ELSE 'MATCH'
        END                             AS verdict
    FROM candidate_pairs p
    LEFT JOIN rule_state rs
        ON  rs.row_key = p.row_key
        AND rs.rule_id = p.rule_id
),

-- Did ANY rule REACH this row (fired, refused, gapped or unresolved)? A row every
-- candidate rule evaluated KNOWN-FALSE on is a row no rule reaches, which is
-- NO_RULE_DECLARED and NOT a gap -- the operator scoped those rules elsewhere on purpose.
row_reach AS (
    SELECT
        row_key,
        MAX(CASE WHEN verdict <> 'NO_MATCH' THEN 1 ELSE 0 END) AS reached
    FROM evaluated
    GROUP BY row_key
),

-- --------------------------------------------------- SLOT PRECEDENCE (D.5) ---
-- The override handle is the SLOT (category, cascade_phase, sequence_order), and two
-- same-scope rules can legitimately share one because migration 119 ships no slot
-- uniqueness -- effective_from DESC, rule_id ASC makes the pick DETERMINISTIC (the
-- plan_vs_actual_daily discipline). No QUALIFY: it is not portable.
matched_ranked AS (
    SELECT
        e.*,
        ROW_NUMBER() OVER (
            PARTITION BY e.row_key, e.category, e.cascade_phase, e.sequence_order
            ORDER BY e.scope_precedence DESC, e.effective_from DESC, e.rule_id ASC
        ) AS _rn
    FROM evaluated e
    WHERE e.verdict = 'MATCH'
),

matched AS (
    SELECT
        m.row_key, m.project_id, m.date, m.currency, m.measured_impressions,
        m.rule_id, m.cpm_micros,
        -- IS DISTINCT FROM is avoided in favour of the maximally safe form the ladder
        -- sanctions: a rule that declares no currency is treated as declaring the row's
        -- own, the only reading that cannot fabricate a mismatch.
        CASE
            WHEN COALESCE(m.rule_currency, m.currency) <> m.currency THEN 1
            ELSE 0
        END AS currency_mismatch,
        -- The same predicate the macro guards on, surfaced so the refusal is TYPED rather
        -- than an unexplained NULL inside a SUM.
        CASE
            WHEN m.cpm_micros IS NULL
              OR m.cpm_micros < 0
              OR m.cpm_micros >= 10000000000 THEN 1
            ELSE 0
        END AS cpm_out_of_range
    FROM matched_ranked m
    WHERE m._rn = 1
),

fired AS (
    SELECT *
    FROM matched
    WHERE currency_mismatch = 0
      AND cpm_out_of_range  = 0
),

row_cost AS (
    -- ONE rounding per (row x rule) inside the macro, then an EXACT INTEGER SUM across
    -- rules. Every member of `fired` is in range, so this SUM can never be NULL.
    SELECT
        f.row_key                                                                   AS row_key,
        SUM({{ fee_tax_cpm_micros('f.measured_impressions', 'f.cpm_micros') }})     AS cost_known_micros
    FROM fired f
    GROUP BY f.row_key
),

applied AS (
    -- AD-9 provenance.
    SELECT
        f.row_key                                     AS row_key,
        STRING_AGG(f.rule_id, '|' ORDER BY f.rule_id) AS applied_rule_ids,
        COUNT(*)                                      AS applied_rule_count
    FROM fired f
    GROUP BY f.row_key
),

-- ------------------------------------------------------------ GAP LEDGER -----
-- A gap is raised ONLY when a confirmed, in-window rule ACTUALLY CONSTRAINS the
-- unresolvable attribute, or is itself unexecutable. A project whose verification rule
-- constrains nothing composes cleanly with no gap at all.
all_gaps AS (
    SELECT row_key, 'DATASTREAM_SCOPE_AMBIGUOUS' AS gap_code
    FROM evaluated WHERE verdict = 'SCOPE_AMBIGUOUS'
    UNION ALL
    SELECT row_key, 'RULE_SCOPE_UNRESOLVED' AS gap_code
    FROM evaluated WHERE verdict = 'SCOPE_UNRESOLVED'
    UNION ALL
    SELECT row_key, 'VERIFICATION_PLAN_SCOPE_UNSUPPORTED' AS gap_code
    FROM evaluated WHERE verdict = 'SCOPE_PLAN_UNSUPPORTED'
    UNION ALL
    SELECT e.row_key, cg.gap_code AS gap_code
    FROM evaluated e
    JOIN cond_gap cg
        ON  cg.row_key = e.row_key
        AND cg.rule_id = e.rule_id
    WHERE e.verdict = 'UNRESOLVED'
      AND cg.gap_code IS NOT NULL
    UNION ALL
    SELECT row_key, 'CURRENCY_MISMATCH' AS gap_code
    FROM matched WHERE currency_mismatch = 1
    UNION ALL
    SELECT row_key, 'VERIFICATION_CPM_OUT_OF_RANGE' AS gap_code
    FROM matched WHERE cpm_out_of_range = 1
),

row_gaps AS (
    SELECT
        row_key,
        MAX(CASE WHEN gap_code = 'CONDITION_KEY_UNKNOWN'      THEN 1 ELSE 0 END) AS g_key_unknown,
        MAX(CASE WHEN gap_code = 'COUNTRY_UNRESOLVED'         THEN 1 ELSE 0 END) AS g_country,
        MAX(CASE WHEN gap_code = 'CURRENCY_MISMATCH'          THEN 1 ELSE 0 END) AS g_currency,
        MAX(CASE WHEN gap_code = 'DATASTREAM_SCOPE_AMBIGUOUS' THEN 1 ELSE 0 END) AS g_ds_ambiguous,
        MAX(CASE WHEN gap_code = 'MARKET_UNRESOLVED'          THEN 1 ELSE 0 END) AS g_market,
        MAX(CASE WHEN gap_code = 'PLACEMENT_TYPE_UNRESOLVED'  THEN 1 ELSE 0 END) AS g_placement,
        MAX(CASE WHEN gap_code = 'RULE_SCOPE_UNRESOLVED'      THEN 1 ELSE 0 END) AS g_scope,
        MAX(CASE WHEN gap_code = 'SOURCE_TYPE_UNRESOLVED'     THEN 1 ELSE 0 END) AS g_source_type,
        MAX(CASE WHEN gap_code = 'TAX_CODE_UNRESOLVED'        THEN 1 ELSE 0 END) AS g_tax_code,
        MAX(CASE WHEN gap_code = 'VERIFICATION_CPM_OUT_OF_RANGE'       THEN 1 ELSE 0 END) AS g_cpm_range,
        MAX(CASE WHEN gap_code = 'VERIFICATION_PLAN_SCOPE_UNSUPPORTED' THEN 1 ELSE 0 END) AS g_plan_scope
    FROM all_gaps
    GROUP BY row_key
),

-- ------------------------------------------- THE rule_without_base REASON ROW
-- A confirmed VERIFICATION CPM rule that priced NOTHING cannot be represented at the
-- impression grain -- there is no impression row to attach it to. Rather than let it
-- VANISH, emit one reason row per (project_id, rule_id), following the '__unresolved__'
-- reason-bucket precedent of fee_tax_ladder_rollup.
-- IT FIRES ONLY WHEN the rule reached zero rows AND the project has zero
-- measured_impression rows inside the rule's effective window -- i.e. THE FEED IS NOT
-- CONNECTED. If impression rows DO exist and the rule simply did not reach them, that is
-- either a known-false condition (the operator scoped it on purpose -> no reason row) or
-- an UNRESOLVED one (which already raised its own per-row gap). Emitting a reason row in
-- those cases would DOUBLE-REPORT. This distinction is the kind a reviewer will otherwise
-- call a bug in one direction or the other, so it is stated rather than implied.
-- And this one IS a gap, by the epic's own criterion: the operator DECLARED a computation
-- and it could not be carried out. Contrast NO_RULE_DECLARED in the header -- the pair is
-- the known-false / unresolvable distinction applied cleanly, and stating them together
-- is what stops a future reader from "harmonising" them.
rule_pair_reach AS (
    SELECT rule_id, COUNT(*) AS n_pairs
    FROM candidate_pairs
    GROUP BY rule_id
),

rule_window_base AS (
    SELECT
        r.rule_id           AS rule_id,
        COUNT(x.row_key)    AS n_base_rows
    FROM verification_rules r
    LEFT JOIN base_rows x
        ON  x.project_id = r.project_id
        AND x.date >= r.effective_from
        AND (r.effective_to IS NULL OR x.date <= r.effective_to)
    GROUP BY r.rule_id
),

rules_without_base AS (
    SELECT
        r.rule_id           AS rule_id,
        r.project_id        AS project_id,
        r.effective_from    AS effective_from,
        r.effective_to      AS effective_to
    FROM verification_rules r
    LEFT JOIN rule_pair_reach   pr ON pr.rule_id = r.rule_id
    LEFT JOIN rule_window_base  wb ON wb.rule_id = r.rule_id
    WHERE COALESCE(pr.n_pairs, 0)     = 0
      AND COALESCE(wb.n_base_rows, 0) = 0
),

-- ---------------------------------------------- THE SPEND LINK: KEYS ONLY ----
-- No cost VALUE is projected anywhere below (Guard 2). The canonical collapse is applied
-- to the spend side too, so an id collision is counted once per connector rather than
-- once per parallel series.
spend_canonical_dim AS (
    SELECT
        f.project_id                                    AS project_id,
        f.date                                          AS date,
        f.connector                                     AS connector,
        CASE
            WHEN MAX(CASE WHEN f.breakdown_dimension = 'campaign_id' THEN 1 ELSE 0 END) = 1
                THEN 'campaign_id'
            ELSE MIN(f.breakdown_dimension)
        END                                             AS dim
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    WHERE f.metric = 'cost'
    GROUP BY f.project_id, f.date, f.connector
),

spend_keys AS (
    SELECT DISTINCT
        f.project_id                                    AS project_id,
        CAST(f.date AS DATE)                            AS date,
        f.connector                                     AS connector,
        f.breakdown_value                               AS breakdown_value
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    JOIN spend_canonical_dim cd
        ON  cd.project_id = f.project_id
        AND cd.date       = f.date
        AND cd.connector  = f.connector
        AND cd.dim        = f.breakdown_dimension
    WHERE f.metric = 'cost'
),

link_candidates AS (
    -- Rung 1 only: LITERAL cross-connector breakdown_value equality inside one
    -- (project, day). Weaker provenance, labelled as such, never presented as governed
    -- truth. >= 2 distinct spend connectors is an id COLLISION and ONE IS NEVER PICKED.
    SELECT
        x.row_key                       AS row_key,
        COUNT(DISTINCT s.connector)     AS n_link_connectors,
        MIN(s.connector)                AS link_connector,
        MIN(s.breakdown_value)          AS link_breakdown_value
    FROM base_rows x
    JOIN spend_keys s
        ON  s.project_id      = x.project_id
        AND s.date            = x.date
        AND s.breakdown_value = x.breakdown_value
        AND s.connector      <> x.connector
    GROUP BY x.row_key
),

-- ---------------------------------------------------------------- ASSEMBLE ---
assembled AS (
    SELECT
        x.project_id                        AS project_id,
        x.date                              AS date,
        x.connector                         AS connector,
        x.breakdown_dimension               AS breakdown_dimension,
        x.breakdown_value                   AS breakdown_value,
        x.currency                          AS currency,
        x.measured_impressions              AS measured_impressions,
        x.pull_id                           AS pull_id,
        rc.cost_known_micros                AS cost_known_micros,
        COALESCE(a.applied_rule_ids, '')    AS applied_rule_ids,
        CAST(COALESCE(a.applied_rule_count, 0) AS BIGINT) AS applied_rule_count,
        CASE WHEN COALESCE(rr.reached, 0) = 1 THEN 'PRICED' ELSE 'NO_RULE_DECLARED' END
                                            AS coverage_state,
        COALESCE(g.g_key_unknown, 0)        AS g_key_unknown,
        COALESCE(g.g_country, 0)            AS g_country,
        COALESCE(g.g_currency, 0)           AS g_currency,
        COALESCE(g.g_ds_ambiguous, 0)       AS g_ds_ambiguous,
        COALESCE(g.g_market, 0)             AS g_market,
        COALESCE(g.g_placement, 0)          AS g_placement,
        COALESCE(g.g_scope, 0)              AS g_scope,
        COALESCE(g.g_source_type, 0)        AS g_source_type,
        COALESCE(g.g_tax_code, 0)           AS g_tax_code,
        COALESCE(g.g_cpm_range, 0)          AS g_cpm_range,
        COALESCE(g.g_plan_scope, 0)         AS g_plan_scope,
        ra.attr_country                     AS attr_country,
        ra.attr_market                      AS attr_market,
        ra.attr_source_type                 AS attr_source_type,
        ra.resolution_source                AS resolution_source,
        lc.n_link_connectors                AS n_link_connectors,
        lc.link_connector                   AS link_connector,
        lc.link_breakdown_value             AS link_breakdown_value
    FROM base_rows x
    LEFT JOIN row_cost        rc ON rc.row_key = x.row_key
    LEFT JOIN applied         a  ON a.row_key  = x.row_key
    LEFT JOIN row_gaps        g  ON g.row_key  = x.row_key
    LEFT JOIN row_reach       rr ON rr.row_key = x.row_key
    LEFT JOIN row_attributes  ra ON ra.row_key = x.row_key
    LEFT JOIN link_candidates lc ON lc.row_key = x.row_key
),

coded AS (
    SELECT
        s.*,
        -- '|'-joined, SORTED, distinct gap codes; '' when none. The CASE chain is written
        -- in ALPHABETICAL ORDER, so the result is sorted BY CONSTRUCTION -- no aggregate
        -- ordering to trust across engines. (VERIFICATION_BASE_MISSING belongs to the
        -- reason rows only and so is absent from this chain by design.)
        CASE WHEN s.g_key_unknown  = 1 THEN '|CONDITION_KEY_UNKNOWN'               ELSE '' END
     || CASE WHEN s.g_country      = 1 THEN '|COUNTRY_UNRESOLVED'                  ELSE '' END
     || CASE WHEN s.g_currency     = 1 THEN '|CURRENCY_MISMATCH'                   ELSE '' END
     || CASE WHEN s.g_ds_ambiguous = 1 THEN '|DATASTREAM_SCOPE_AMBIGUOUS'          ELSE '' END
     || CASE WHEN s.g_market       = 1 THEN '|MARKET_UNRESOLVED'                   ELSE '' END
     || CASE WHEN s.g_placement    = 1 THEN '|PLACEMENT_TYPE_UNRESOLVED'           ELSE '' END
     || CASE WHEN s.g_scope        = 1 THEN '|RULE_SCOPE_UNRESOLVED'               ELSE '' END
     || CASE WHEN s.g_source_type  = 1 THEN '|SOURCE_TYPE_UNRESOLVED'              ELSE '' END
     || CASE WHEN s.g_tax_code     = 1 THEN '|TAX_CODE_UNRESOLVED'                 ELSE '' END
     || CASE WHEN s.g_cpm_range    = 1 THEN '|VERIFICATION_CPM_OUT_OF_RANGE'       ELSE '' END
     || CASE WHEN s.g_plan_scope   = 1 THEN '|VERIFICATION_PLAN_SCOPE_UNSUPPORTED' ELSE '' END
                                            AS gap_codes_prefixed,
        CASE
            WHEN s.n_link_connectors IS NULL  THEN 'UNLINKED_NO_SPEND_MATCH'
            WHEN s.n_link_connectors >= 2     THEN 'UNLINKED_AMBIGUOUS_SPEND_MATCH'
            ELSE 'LINKED_VIA_CAMPAIGN_REF'
        END                                     AS spend_link_state
    FROM assembled s
),

allocation_rows AS (
    SELECT
        'allocation'                        AS row_kind,
        c.project_id                        AS project_id,
        c.date                              AS date,
        c.connector                         AS connector,
        c.breakdown_dimension               AS breakdown_dimension,
        c.breakdown_value                   AS breakdown_value,
        c.currency                          AS currency,

        -- ALWAYS POPULATED on an allocation row: it is a read of a real fact.
        c.measured_impressions              AS measured_impressions,

        -- The ONE money column in this model. NULL when a declared computation could not
        -- be carried out, and NULL when no rate was declared at all -- never a fabricated
        -- 0 in either case. "You may look at the parts; you may not read a total we could
        -- not compute", plus "there is no cost to compute".
        CASE
            WHEN c.gap_codes_prefixed <> ''                THEN NULL
            WHEN c.coverage_state = 'NO_RULE_DECLARED'     THEN NULL
            ELSE c.cost_known_micros
        END                                 AS verification_cost_micros,

        c.coverage_state                    AS coverage_state,
        c.applied_rule_ids                  AS applied_rule_ids,
        c.applied_rule_count                AS applied_rule_count,
        -- Rule-window provenance belongs to the reason rows; an allocation row's day IS
        -- its date, so inventing a window here would be noise.
        CAST(NULL AS DATE)                  AS rule_effective_from,
        CAST(NULL AS DATE)                  AS rule_effective_to,

        c.spend_link_state                  AS spend_link_state,
        CASE
            WHEN c.spend_link_state = 'LINKED_VIA_CAMPAIGN_REF'
                THEN 'campaign_ref_equality'
            ELSE ''
        END                                 AS spend_link_source,
        CASE
            WHEN c.spend_link_state = 'LINKED_VIA_CAMPAIGN_REF' THEN c.link_connector
        END                                 AS linked_connector,
        CASE
            WHEN c.spend_link_state = 'LINKED_VIA_CAMPAIGN_REF' THEN c.link_breakdown_value
        END                                 AS linked_breakdown_value,

        c.attr_country                      AS attr_country,
        c.attr_market                       AS attr_market,
        c.attr_source_type                  AS attr_source_type,
        c.resolution_source                 AS resolution_source,

        c.g_key_unknown  = 1                AS gap_condition_key_unknown,
        c.g_country      = 1                AS gap_country_unresolved,
        c.g_currency     = 1                AS gap_currency_mismatch,
        c.g_ds_ambiguous = 1                AS gap_datastream_scope_ambiguous,
        c.g_market       = 1                AS gap_market_unresolved,
        c.g_placement    = 1                AS gap_placement_type_unresolved,
        c.g_scope        = 1                AS gap_rule_scope_unresolved,
        c.g_source_type  = 1                AS gap_source_type_unresolved,
        c.g_tax_code     = 1                AS gap_tax_code_unresolved,
        FALSE                               AS gap_verification_base_missing,
        c.g_cpm_range    = 1                AS gap_verification_cpm_out_of_range,
        c.g_plan_scope   = 1                AS gap_verification_plan_scope_unsupported,
        CASE WHEN c.gap_codes_prefixed = '' THEN ''
             ELSE SUBSTR(c.gap_codes_prefixed, 2) END AS gap_codes,
        c.gap_codes_prefixed = ''           AS is_allocation_complete,

        TRUE                                AS keep_separate,
        'KEEP_SEPARATE'                     AS reconciliation_status,
        c.pull_id                           AS pull_id
    FROM coded c
),

reason_rows AS (
    SELECT
        'rule_without_base'                 AS row_kind,
        r.project_id                        AS project_id,
        -- HONEST: it priced nothing on any day, so it has no day.
        CAST(NULL AS DATE)                  AS date,
        ''                                  AS connector,
        '__rule_without_base__'             AS breakdown_dimension,
        r.rule_id                           AS breakdown_value,
        m.canonical_currency                AS currency,
        CAST(NULL AS BIGINT)                AS measured_impressions,
        CAST(NULL AS BIGINT)                AS verification_cost_micros,
        'RULE_WITHOUT_BASE'                 AS coverage_state,
        ''                                  AS applied_rule_ids,
        CAST(0 AS BIGINT)                   AS applied_rule_count,
        -- The declared window, so a surface can say "declared from X, priced nothing".
        r.effective_from                    AS rule_effective_from,
        r.effective_to                      AS rule_effective_to,
        'NOT_ATTEMPTED'                     AS spend_link_state,
        ''                                  AS spend_link_source,
        CAST(NULL AS STRING)                AS linked_connector,
        CAST(NULL AS STRING)                AS linked_breakdown_value,
        CAST(NULL AS STRING)                AS attr_country,
        CAST(NULL AS STRING)                AS attr_market,
        CAST(NULL AS STRING)                AS attr_source_type,
        CAST(NULL AS STRING)                AS resolution_source,
        FALSE                               AS gap_condition_key_unknown,
        FALSE                               AS gap_country_unresolved,
        FALSE                               AS gap_currency_mismatch,
        FALSE                               AS gap_datastream_scope_ambiguous,
        FALSE                               AS gap_market_unresolved,
        FALSE                               AS gap_placement_type_unresolved,
        FALSE                               AS gap_rule_scope_unresolved,
        FALSE                               AS gap_source_type_unresolved,
        FALSE                               AS gap_tax_code_unresolved,
        TRUE                                AS gap_verification_base_missing,
        FALSE                               AS gap_verification_cpm_out_of_range,
        FALSE                               AS gap_verification_plan_scope_unsupported,
        'VERIFICATION_BASE_MISSING'         AS gap_codes,
        FALSE                               AS is_allocation_complete,
        TRUE                                AS keep_separate,
        'KEEP_SEPARATE'                     AS reconciliation_status,
        CAST(NULL AS STRING)                AS pull_id
    FROM rules_without_base r
    JOIN module_on m
        ON m.project_id = r.project_id
)

SELECT * FROM allocation_rows
UNION ALL
SELECT * FROM reason_rows
{%- endif -%}
