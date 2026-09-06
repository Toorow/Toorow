-- fee_tax_ladder_daily: the six-phase fee & tax cascade, in exact integer micros
-- (Epic 41, Story 41.3 / E41-FR03 / E41-AD9 / arbitration B1 + B2).
--
-- ============================ THIS MART IS A READ ============================
-- fee_tax_ladder_daily READS fact_daily_kpi (metric='cost'), the governed rule
-- mirror (via fee_tax_rules_effective + mirror.fee_tax_rule_conditions +
-- mirror.fee_tax_rule_tiers), the plan mirror (mirror.plan_line_mappings) and
-- Story 41.2's fee_tax_country_resolution. It creates NO new fact row and edits NO
-- existing model. fact_daily_kpi / cross_source_conversions / cross_source_revenue /
-- metric_baselines / dedup_estimate / plan_vs_actual_daily / candidate_full_grain are
-- STRICTLY UNTOUCHED. Nothing pre-existing depends on this view, so the overlay
-- CANNOT change a pre-existing number -- E41-NFR01 ("module OFF => byte-identical")
-- is true BY CONSTRUCTION, and asserted by test_epic41_totals_bit_identical.sql +
-- server/tests/conformance/test_epic41_additive_only.py.
--
-- GRAIN (enforced by fee_tax_ladder_daily_grain_unique):
--   one row per (project_id, date, connector, breakdown_dimension, breakdown_value,
--   currency). breakdown_dimension is in the key even though the model filters to a
--   single canonical dimension per (project, date, connector) -- belt and braces if
--   a later story relaxes the filter. The filter itself is asserted by
--   test_epic41_no_double_count_single_breakdown.sql, which is the assertion that
--   actually prevents triple-counting.
--
-- ####################################################################
-- ## READ THIS BEFORE FILING A BUG: MOSTLY-GAP OUTPUT IS SPECIFIED. ##
-- ####################################################################
-- On cost-cascade sources the country ladder's first two rungs do not fire today:
--   * rung 1 (breakdown_dimension='country' on a cost row) never fires -- only
--     google-analytics and gsc emit a country breakdown and NEITHER emits cost
--     (amendment A.2 / AI-51: "if a module lacks one of the pair, SKIP it -- do not
--     invent data"); meta-ads / tiktok-ads / linkedin-ads land only
--     campaign_id / adset_id / ad_id;
--   * rung 2 (the declared datastream->market binding) is UNPOPULATED: migration 104
--     (app.market_bindings) is not applied and nothing writes binding_kind='datastream'.
-- Story 41.2's third rung (project posture: `local_markets` with EXACTLY ONE tracked
-- country) resolves the single-market projects. Everything else -- `global` posture,
-- or two or more tracked countries -- keeps attr_country NULL, and because 41.2's
-- auto-population produces COUNTRY-CONDITIONED rules by construction (DST GB 2 %,
-- DST FR 3 %, the standard VAT rows), those projects legitimately get
-- COUNTRY_UNRESOLVED and NULL headline totals on most rows.
-- THAT IS THE ARBITRATED ANSWER (B1: "the composed total is flagged incomplete, and
-- no zero is fabricated"), NOT A BROKEN BUILD. What keeps it honest rather than
-- useless: auto-populated rules land status='proposed' and are inert until a human
-- confirms them; net_media_micros and every resolvable component stay POPULATED on
-- the row; and fee_tax_ladder_rollup exposes complete_row_count / total_row_count /
-- total_ttc_micros_complete_only so a surface can say "composed for 0 of 240 rows --
-- country unresolved" instead of showing a blank or a lie.
--
-- ============ NULL DISCIPLINE (the plan_vs_actual_daily posture, AC4) ========
-- plan_vs_actual_daily documents that is_plan_only lines carry actual_amount NULL,
-- never 0: "there is no honest spend to pace", "never a 0% trap". This model carries
-- the same discipline:
--   * net_media_micros is ALWAYS populated -- it is a read of a real fact.
--   * A phase column is the EXACT SUM of the components that fired in that phase,
--     and is NULL when a rule routed to that phase could not be evaluated. A 0 in a
--     phase column therefore always means "the composition ran and the answer is
--     zero", never "we could not compute it".
--   * The RUNNING SUBTOTAL still advances with the KNOWN part of a partly-gapped
--     phase, which is why S12's unconditioned agency fee is populated even though
--     the DST and VAT phases above it are NULL.
--   * subtotal_ht_micros and total_ttc_micros are NULL whenever is_ladder_complete
--     is FALSE. You may look at the parts; you may not read a total we could not
--     compute. (test_epic41_no_fabricated_zero.sql.)
--   * A NEGATIVE component is NOT automatically a bug (F12). Platform credits,
--     make-goods and refunds land as negative `cost`, so a negative net media makes
--     every rate-derived component legitimately negative -- that is the fee being
--     credited back, and it must pass through untouched. What IS a bug is a negative
--     component on a NON-NEGATIVE base, and that is what the test asserts. A data
--     condition must never take the build down.
--
-- ===================== D.1 ANTI-DOUBLE-COUNT (CRITICAL) =====================
-- meta-ads and tiktok-ads each emit THREE parallel `cost` series (campaign_id /
-- adset_id / ad_id, one per data_level) and EACH ONE TOTALS THE DAY INDEPENDENTLY.
-- Running the ladder over all three would TRIPLE every fee on every invoice. The
-- repo idiom is a canonical single dimension (cross_source_conversions.sql:44,
-- cross_source_revenue.sql:62, dedup_estimate.sql:134). Decision C4 refines it:
-- PREFER 'campaign_id' when the connector emits it, else MIN(breakdown_dimension).
-- Preferring campaign_id is exactly as safe as MIN: meta-ads and tiktok-ads build
-- their campaign_id series from ONE report grain only (data_level='CAMPAIGN' /
-- 'AUCTION_CAMPAIGN', fact_daily_kpi.sql 364-368 and 788-792, the review-15-9 /
-- review-15-2 F-1 fix), so it is one self-contained series that totals the day
-- exactly like ad_id does. It is NOT a top-N-bounded partition -- the top-N blocks
-- in the mart are GA4's landing_page / page / session_campaign /
-- first_user_source_medium, none of which is named campaign_id and none of which
-- emits cost. And it is what makes plan-scoped rules (§D.5) and the plan-line
-- rollup (§D.8) resolve for the two connectors that matter most, which is why the
-- pre-C4 PLAN_SCOPE_GRAIN_MISMATCH / PLAN_LINE_GRAIN_MISMATCH gap codes no longer
-- exist -- the mismatch was removed at its root, it was not dropped.
--
-- ===================== D.2 THE MICROS BOUNDARY ==============================
-- `cost` is DECIMAL at the mart (dbt/seeds/money_metric_units.csv line 5), so the
-- ladder normalises AT ITS OWN BOUNDARY and never re-divides (amendment A.4 / C.6).
-- Normalise PER SOURCE ROW with fee_tax_to_micros, THEN SUM exact BIGINTs --
-- ROUND(SUM(value)*1e6) would put the single rounding boundary after the float drift
-- instead of before it. ROUND_HALF_UP, one boundary per component. The divergence
-- from money.py's banker's rounding is documented in the macro and pinned by S2.
--
-- ===================== FX IS CARRIED, NEVER RE-APPLIED ======================
-- fact_daily_kpi already converted `cost` to the project's canonical currency at
-- read (fx_convert_at_read, Epic 39.10). There is NO currency column on the fact:
-- a ladder row's currency IS mirror.project_preferences.canonical_currency, and
-- fx_rate / fx_as_of_date / fx_source / fx_tier are carried as PROVENANCE only
-- (AD-9 / C.6). The ladder never calls fx_convert_at_read and never multiplies by
-- a rate. Converting a rule-declared amount here would apply FX a second time,
-- outside the read locus -- which Epic 39.10 forbids; see §D.6 below.
--
-- ===================== D.3.2 THE MATCHER: THREE-VALUED, PER RULE ============
-- THE MATCHER NO LONGER LIVES IN THIS FILE. Story 41.4 (ruling Q3) extracted
-- row_attributes_long / cond_eval / rule_state / cond_gap into
-- dbt/macros/fee_tax_condition_matcher.sql -- ONE definition site, called below where
-- the CTEs used to be, emitting the same four names so nothing downstream moved. The
-- semantics summarised here are unchanged and are documented in full in that macro; the
-- extraction was gated on 41.3's singular tests staying byte-identical. E41-NFR01
-- protects the INCUMBENT warehouse, not Epic 41's own models, so this edit is in scope.
-- Vocabulary, shared with 41.2: MATCH (rank 0) . NO_MATCH (rank 1, the epic's
-- "known false") . UNRESOLVED (rank 2). Precedence UNRESOLVED > NO_MATCH > MATCH.
-- WHY UNRESOLVED BEATS NO_MATCH (or a reviewer will call it a bug): if one clause
-- cannot be evaluated, the conjunction is UNKNOWN, not false. With country unknown,
-- `country IN ('FR') AND connector = 'meta-ads'` is unknown even when the connector
-- clause is false -- asserting "false" would let us silently skip a rule that might
-- have applied.
-- Semantics: conjunction ACROSS condition_keys, disjunction ACROSS the values of one
-- key. A rule with NO condition row is UNCONSTRAINED and matches every row -- which
-- is why the condition join is a LEFT JOIN with COALESCE(state, 0): an INNER JOIN
-- would silently delete every unconditional rule, i.e. most of them.
-- The six recognised condition_keys are connector / country / market / source_type /
-- placement_type / tax_code. ANYTHING ELSE is CONDITION_KEY_UNKNOWN -> rank 2, never
-- ignored. placement_type and tax_code are ALWAYS NULL today: no dimension named
-- `placement` exists anywhere in fact_daily_kpi and no connector emits a tax code,
-- so a rule conditioned on either is always UNRESOLVED. That is fail-honest, not a
-- defect.
--
-- ===================== R4: THE MARKET / COUNTRY ASYMMETRY ===================
-- 41.2's frozen contract states that attr_market MAY BE NON-NULL WHILE attr_country
-- IS NULL. So on one and the same ladder row, a rule conditioned ('market','emea')
-- evaluates MATCH (or NO_MATCH) normally while a rule conditioned ('country','FR')
-- evaluates UNRESOLVED and raises its gap. This falls out for free BECAUSE THE STATE
-- IS ROLLED UP PER (row_key, rule_id) AND NEVER PER ROW. Do not hoist an "is this row
-- resolvable?" flag to row level: that would drag the market rule down with the
-- country rule and silently kill a rule that was perfectly evaluable. This is the
-- single most likely wrong implementation of this model;
-- test_epic41_market_country_asymmetry.sql pins it in both directions.
--
-- ===================== THE BRIDGE gap_code IS NOT PROPAGATED ================
-- fee_tax_country_resolution emits gap_code='COUNTRY_UNRESOLVED' on essentially
-- EVERY paid-media row, whether or not any rule cares about country. Copying it onto
-- the ladder row would NULL the totals of every project in the platform, including
-- ones whose rules constrain nothing but connector. This model raises a gap ONLY
-- WHEN A CONFIRMED, IN-WINDOW RULE ACTUALLY CONSTRAINS THE UNRESOLVED ATTRIBUTE.
-- bridge_gap_code and the three *_gap_reason columns are carried as PROVENANCE DETAIL
-- ONLY, so an operator can see WHY an attribute was unavailable; they never drive
-- is_ladder_complete. resolution_source is likewise OPAQUE PROVENANCE: this model
-- never branches on it, which is exactly why 41.2 could add a whole new resolution
-- rung (C5) at zero cost here, and why the next one will cost nothing either.
--
-- ===================== D.4.2 PHASE CHAINING (D3, CONFIRMED) =================
-- Six explicit chained CTEs, no recursion. Phase n's subtotal at entry = phase n-1's
-- at exit. WITHIN a phase, EVERY rule resolves its base from the running subtotal AS
-- AT PHASE ENTRY (base_target='NET_MEDIA' reads net_media_micros instead). Intra-phase
-- rules are therefore PARALLEL and ORDER-INDEPENDENT, and sequence_order is a
-- display/override key, not a compounding order -- which is why migration 119 carries
-- no UNIQUE (project_id, cascade_phase, sequence_order). Rationale: E41-FR03 defines
-- the PHASE as the compounding unit; intra-phase compounding would make the result
-- depend on an operator-editable integer and be non-associative -- reordering two
-- rules would silently change the invoice. Fee-on-fee is expressed by putting the
-- second fee in a LATER phase.
-- Routing is by `category`; cascade_phase is carried as provenance only, which
-- removes an entire class of category/phase-disagreement ambiguity:
--     1 net media (a read)   2 PLATFORM_FEE   3 REGULATORY_TAX
--     4 WHT_GROSS_UP         5 AGENCY_FEE     6 SALES_TAX
-- VERIFICATION (KEEP_SEPARATE, Epic 27 invariant 4 -- Story 41.4's overlay) and
-- PAYMENT_FEE (the revenue side, Story 41.5) are admitted by NO phase here, and are
-- excluded BEFORE condition evaluation so they can never raise a gap either.
-- base_target MEASURED_IMPRESSIONS / NET_REVENUE / GROSS_REVENUE belong to 41.4/41.5:
-- a rule declaring one is skipped with no gap, because such a rule is not addressed to
-- this ladder at all.
--
-- ============ EVERY (category, form) PAIR IS EITHER ROUTED OR GAPPED (F2) ====
-- A rule that reaches this ladder and that NO phase branch can execute must NOT fall
-- through to `ELSE 0`. Nothing in migration 119 validates `category x form`, so
-- (AGENCY_FEE, GROSS_UP, RUNNING_SUBTOTAL, phase 5) passes every CHECK -- and phase 5
-- has no GROSS_UP branch, so before this guard it contributed +0 micros, entered
-- applied_rule_ids, and left is_ladder_complete TRUE. An UNDERSTATED invoice reported
-- as complete is the worst failure this engine can produce, and Story 41.8 hands rule
-- creation to a governed LLM, so it is reachable without anyone writing SQL.
-- Routable pairs, positively enumerated:
--     WHT_GROSS_UP        -> GROSS_UP | PERCENTAGE | FLAT | SPEND_TIERS
--     every other phase   -> PERCENTAGE | FLAT | SPEND_TIERS
-- Anything else -> RULE_FORM_UNROUTABLE, the rule does not fire, the phase column goes
-- NULL. That includes form='CPM': its only meaningful base is MEASURED_IMPRESSIONS
-- (Story 41.4's), so a CPM rule declaring NET_MEDIA or RUNNING_SUBTOTAL is not "a 41.4
-- rule passing through", it is an unexecutable rule and it says so. A declaration-side
-- CHECK is landing in parallel; this is defence in depth, deliberately duplicated.
--
-- ==================== FLAT APPLIES ONCE PER DAY, THEN IS ALLOCATED (F8) =====
-- ORCHESTRATOR RULING, and it changes a spec-level semantic. §D.4.1 said only
-- "FLAT -> amount_micros", and because the ladder grain is
-- (project, date, connector, breakdown_value) that meant a project-scoped FLAT of
-- 500 EUR fired ONCE PER CAMPAIGN PER DAY: 40 campaigns x 30 days = 600 000 EUR of
-- "flat" fee. Nothing in the spec stated the multiplication and no test would have
-- noticed it.
-- A FLAT amount now applies ONCE per (project_id, date, currency) and is ALLOCATED
-- across that day's ladder rows PRO-RATA BY NET MEDIA, largest-remainder, so the day's
-- components sum to EXACTLY amount_micros -- the cent-exact discipline
-- mediaplan_store.compute_spread already uses for budgets, expressed as a window.
-- The allocation group is per RULE as well as per day, so a datastream-scoped FLAT
-- spreads only over the rows it actually governs. A day whose net media is entirely
-- zero falls back to an ordinal spread, so the fee is still allocated exactly and
-- never silently dropped.
--
-- ==================== PLAN COVERAGE APPLIES TO PERCENTAGE ONLY (F8) =========
-- The coverage weight (§D.5) is a fraction of a row's spend that a plan claims. It is
-- meaningful only for a DIMENSIONLESS RATE. Before this guard it was folded into the
-- rate for PERCENTAGE only, which meant a plan-scoped FLAT or SPEND_TIERS rule billed
-- 100 % on a row the plan covers 30 % of. The fix is NOT to scale those forms:
--   * a FLAT amount is a per-period charge, not a per-row rate -- "30 % of a flat fee
--     for this campaign" has no agreed meaning;
--   * a SPEND_TIERS ladder is contracted on the platform's own volume, so scaling a
--     tranche by one campaign's plan coverage misprices the band;
--   * a GROSS_UP is NOT linear in its rate, so rate x coverage is simply wrong.
-- So: coverage <> 1.0 on any form other than PERCENTAGE is REFUSED with
-- COVERAGE_NOT_APPLICABLE -- loud, typed, and never a silent full bill. Coverage
-- exactly 1.0 (what the store enforces) leaves every form working normally.
--
-- ===================== D.4.3 SPEND_TIERS: BOTH MODES, VIEW TIME =============
-- The cumulative base is computed AT VIEW TIME with SUM() OVER (...): no table is
-- mutated and no state is persisted (E41-NFR03 is satisfied STRUCTURALLY by
-- materialized: view, asserted by the conformance test, not by SQL).
--   * Partition = (rule_id, project_id, connector). Narrowing by rule_id is what
--     makes the tier ladder reset when the CONTRACT does -- a rule row IS one
--     contractual period, so no extra "reset" column is needed. A tier is a platform
--     contract: per project, per platform, never global across connectors.
--   * The cumulative base is NET MEDIA, never the running subtotal: a volume tier is
--     contracted on media spend, and using an inflated subtotal would make the tier
--     depend on the fees it determines.
--   * Frame = ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW, INCLUSIVE of the
--     current day, so the day that CROSSES the threshold is already at the new rate.
--     `ORDER BY date` alone would default to RANGE ... CURRENT ROW, which ties on
--     equal dates; ROWS is explicit and deterministic. Both engines support it
--     verbatim. Fixture S6 pins the frame: cliff day 3 = 210_000_000, marginal day 3
--     = 270_000_000, exclusive-frame cliff = 350_000_000 -- three different answers,
--     so mode and frame are pinned INDEPENDENTLY rather than assumed.
--   * `mode` is RULE DATA, not a platform choice (D4/R5): it is denormalised onto
--     every band row by migration 119, defaults to 'cliff', and ANY value outside
--     {cliff, marginal} is gap_tiers_malformed -- never a silent cliff.
--
-- ===================== D.4.4 WHT GROSS-UP: THE NON-ADDITIVE STEP ============
-- grossed_total = ROUND(base / (1 - w)) with ONE rounding, and the component is
-- grossed_total - base by EXACT INTEGER SUBTRACTION, so base + component ==
-- grossed_total to the micro and the waterfall test holds at tolerance exactly 0.
-- w outside [0,1) yields a NULL component + gap_wht_rate_invalid; the division is
-- CASE-guarded inside the macro so the engine never evaluates base/0. A w INSIDE
-- [0,1) but close enough to 1 that base/(1-w) would exceed INT64 (migration 119
-- admits 0.999999) previously ABORTED THE BUILD with a DuckDB conversion error rather
-- than producing a gap; the macro's magnitude guard now returns NULL and phase4_guard
-- turns it into gap_wht_base_overflow (F10). A typed gap, never a red build.
--
-- ===================== D.6 CROSS-CURRENCY: REFUSED, NOT SUMMED ==============
-- The only place two currencies can meet is a RULE-DECLARED amount: FLAT.amount_micros
-- and CPM.cpm_micros, which C.2 requires to carry their own currency. When it differs
-- from the row's currency the component is REFUSED -- not summed, NOT CONVERTED --
-- gap_currency_mismatch is TRUE and the headline totals are NULL. Converting here
-- would apply FX a second time outside the fx_convert_at_read locus (Epic 39.10).
-- PERCENTAGE, SPEND_TIERS and GROSS_UP are DIMENSIONLESS (a rate) and are therefore
-- never subject to this check -- stated explicitly so a reviewer does not ask why VAT
-- is exempt.
--
-- ===================== ZERO RESOLUTION LOGIC LIVES HERE (R3) ================
-- Country / market / source_type enter this model through EXACTLY ONE LEFT JOIN on
-- ref('fee_tax_country_resolution') -- Story 41.2's standalone view with a FROZEN
-- 14-column contract. There is no dim_country, no market_bindings, no
-- datastream_country_binding_dim, no datastream_source_types, no derivation ladder
-- and no fallback anywhere in Epic 41.3. If a line of this file resolves a country,
-- a market or a source type, it is wrong. 41.2 guarantees exactly one resolution row
-- per fact row (a unique combination-of-columns test on the six join keys), so the
-- join cannot fan out; it is a LEFT join so a missing bridge row degrades to all-NULL
-- attributes, never to a dropped ladder row.

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
      'fee_tax_rule_tiers',
      'plan_line_mappings',
      'project_tax_fee_activation',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['date', 'date'],
    ['connector', 'string'],
    ['breakdown_dimension', 'string'],
    ['breakdown_value', 'string'],
    ['currency', 'string'],
    ['net_media_micros', 'bigint'],
    ['platform_fee_micros', 'bigint'],
    ['regulatory_tax_micros', 'bigint'],
    ['wht_gross_up_micros', 'bigint'],
    ['agency_fee_micros', 'bigint'],
    ['subtotal_ht_micros', 'bigint'],
    ['sales_tax_micros', 'bigint'],
    ['total_ttc_micros', 'bigint'],
    ['fx_rate', 'numeric'],
    ['fx_as_of_date', 'date'],
    ['fx_source', 'string'],
    ['fx_tier', 'string'],
    ['pull_id', 'string'],
    ['applied_rule_ids', 'string'],
    ['applied_rule_count', 'bigint'],
    ['gap_country_unresolved', 'boolean'],
    ['gap_market_unresolved', 'boolean'],
    ['gap_placement_type_unresolved', 'boolean'],
    ['gap_tax_code_unresolved', 'boolean'],
    ['gap_source_type_unresolved', 'boolean'],
    ['gap_condition_key_unknown', 'boolean'],
    ['gap_currency_mismatch', 'boolean'],
    ['gap_datastream_scope_ambiguous', 'boolean'],
    ['gap_rule_scope_unresolved', 'boolean'],
    ['gap_wht_rate_invalid', 'boolean'],
    ['gap_wht_base_overflow', 'boolean'],
    ['gap_tiers_malformed', 'boolean'],
    ['gap_rule_form_unroutable', 'boolean'],
    ['gap_coverage_not_applicable', 'boolean'],
    ['gap_codes', 'string'],
    ['is_ladder_complete', 'boolean'],
    ['attr_country', 'string'],
    ['attr_market', 'string'],
    ['attr_source_type', 'string'],
    ['resolution_source', 'string'],
    ['country_gap_reason', 'string'],
    ['market_gap_reason', 'string'],
    ['source_type_gap_reason', 'string'],
    ['bridge_gap_code', 'string'],
]) }}
{%- else %}

{#- Shared SQL fragments. Kept as Jinja variables rather than repeated inline so the
    five phase CTEs cannot drift apart, and so the ONE definition of "the base a rule
    reads" (D3: the running subtotal AS AT PHASE ENTRY, or net media when the rule
    declares base_target='NET_MEDIA') is written exactly once and reviewable in one
    place. They are plain SQL strings; every rounding still happens inside the two
    fee_tax_* macros. -#}
{%- set phase_base = "CASE WHEN f.base_target = 'NET_MEDIA' THEN p.net_media_micros ELSE p.subtotal_micros END" -%}
{#- D.5 / Story 48.4: the plan coverage weight, with ONE rounding on BOTH branches.

    The earlier form folded the weight into the rate OPERAND -- `f.rate *
    f.coverage_micros / 1000000` -- and that expression's division is DOUBLE. The
    percentage macro then re-quantised the result to DECIMAL(10,6), so the covered
    path rounded the RATE in binary floating point and then rounded the COMPONENT: two
    boundaries, on a number that reaches an invoice. fee_tax_pct_of_micros' own
    docstring recorded this as finding F11 and it stayed open.

    The equality fast path is UNCHANGED and still bit-exact: the store enforces
    SUM(split_weight) = 1.0 per (plan, connector, campaign_ref), so the overwhelmingly
    common case passes `f.rate` untouched through the original macro. Only the
    defensive mirror-lag branch changes, and it now folds the weight into the SAME
    exact-decimal product as the rate, with one division and one ROUND. -#}
{%- set pct_component %}
CASE
    WHEN f.coverage_micros IS NULL OR f.coverage_micros = 1000000
        THEN {{ fee_tax_pct_of_micros(phase_base, "f.rate") }}
    ELSE {{ fee_tax_pct_of_micros_weighted(phase_base, "f.rate", "f.coverage_micros") }}
END
{%- endset %}
{%- set tranche_incl = "GREATEST(0, LEAST(rc.row_cum_incl_micros, COALESCE(b.next_threshold_micros, rc.row_cum_incl_micros)) - b.threshold_micros)" -%}
{%- set tranche_excl = "GREATEST(0, LEAST(rc.row_cum_incl_micros - rc.net_media_micros, COALESCE(b.next_threshold_micros, rc.row_cum_incl_micros - rc.net_media_micros)) - b.threshold_micros)" -%}

WITH module_on AS (
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

-- ------------------------------------------------- D.1 CANONICAL SINGLE SERIES
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
    WHERE f.metric = 'cost'
    GROUP BY f.project_id, f.date, f.connector
),

-- ------------------------------------------------------- PHASE 1: NET MEDIA --
spend_base AS (
    SELECT
        f.project_id                                    AS project_id,
        CAST(f.date AS DATE)                            AS date,
        f.connector                                     AS connector,
        f.breakdown_dimension                           AS breakdown_dimension,
        f.breakdown_value                               AS breakdown_value,
        m.canonical_currency                            AS currency,
        -- Per-source-row normalisation, THEN an exact integer SUM (§D.2).
        SUM({{ fee_tax_to_micros('f.value') }})         AS net_media_micros,
        -- Per-group provenance convention (AD-7 / plan_vs_actual_daily line 171).
        MAX(f.fx_rate)                                  AS fx_rate,
        MAX(f.fx_as_of_date)                            AS fx_as_of_date,
        MAX(f.fx_source)                                AS fx_source,
        MAX(f.fx_tier)                                  AS fx_tier,
        MAX(f.pull_id)                                  AS pull_id
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    JOIN canonical_dim cd
        ON  cd.project_id = f.project_id
        AND cd.date       = f.date
        AND cd.connector  = f.connector
        AND cd.dim        = f.breakdown_dimension
    WHERE f.metric = 'cost'
    GROUP BY
        f.project_id, CAST(f.date AS DATE), f.connector,
        f.breakdown_dimension, f.breakdown_value, m.canonical_currency
),

ladder_rows AS (
    SELECT
        s.project_id || '|' || CAST(s.date AS STRING) || '|' || s.connector
            || '|' || s.breakdown_dimension || '|' || s.breakdown_value  AS row_key,
        s.project_id,
        s.date,
        s.connector,
        s.breakdown_dimension,
        s.breakdown_value,
        s.currency,
        s.net_media_micros,
        s.fx_rate,
        s.fx_as_of_date,
        s.fx_source,
        s.fx_tier,
        s.pull_id
    FROM spend_base s
),

-- ------------------------- D.3.1 ROW ATTRIBUTES: ONE JOIN, ZERO RESOLUTION ---
row_attributes AS (
    SELECT
        x.row_key                       AS row_key,
        x.connector                     AS attr_connector,
        r.attr_country                  AS attr_country,
        r.attr_market                   AS attr_market,
        r.attr_source_type              AS attr_source_type,
        CAST(NULL AS STRING)            AS attr_placement_type,   -- structurally absent
        CAST(NULL AS STRING)            AS attr_tax_code,         -- no emitter, no column
        r.resolution_source             AS resolution_source,     -- opaque provenance
        r.country_gap_reason            AS country_gap_reason,
        r.market_gap_reason             AS market_gap_reason,
        r.source_type_gap_reason        AS source_type_gap_reason,
        r.gap_code                      AS bridge_gap_code        -- provenance ONLY
    FROM ladder_rows x
    LEFT JOIN {{ ref('fee_tax_country_resolution') }} r
        ON  r.project_id          = x.project_id
        AND r.date                = x.date
        AND r.connector           = x.connector
        AND r.metric              = 'cost'
        AND r.breakdown_dimension = x.breakdown_dimension
        AND r.breakdown_value     = x.breakdown_value
),

-- ------------------------------------------------------------- RULE SIDE -----
rules_ladder AS (
    -- AC11 / Epic 27 invariant 4: VERIFICATION is excluded HERE, before condition
    -- evaluation, so it can neither contribute nor gap. PAYMENT_FEE likewise (41.5).
    -- The three revenue/impression base_targets belong to 41.4/41.5 and are skipped
    -- with no gap.
    SELECT
        rule_id, project_id, scope_kind, scope_ref, category, form, rate,
        amount_micros, rule_currency, base_target, cascade_phase, sequence_order,
        effective_from, effective_to, scope_precedence, scope_connector,
        scope_plan_id, scope_state
    FROM {{ ref('fee_tax_rules_effective') }}
    WHERE category IN ('PLATFORM_FEE', 'REGULATORY_TAX', 'WHT_GROSS_UP',
                       'AGENCY_FEE', 'SALES_TAX')
      AND base_target IN ('NET_MEDIA', 'RUNNING_SUBTOTAL')
),

-- D.5: a plan-scoped rule covers the rows whose (connector, campaign_ref) is mapped
-- to a line of that plan by an ACTIVE mapping. The store enforces
-- SUM(split_weight) = 1.0 per (plan_id, connector, campaign_ref); the cap is a
-- DEFENSIVE guard so a mirror-lag double-claim can never inflate a fee above 100 %.
-- The weight is expressed in integer micros (1.0 = 1000000) so the cap comparison is
-- exact and needs no dialect-specific decimal cast in the model.
plan_coverage AS (
    SELECT
        pm.plan_id                                          AS plan_id,
        pm.connector                                        AS connector,
        pm.campaign_ref                                     AS campaign_ref,
        CASE
            WHEN SUM({{ fee_tax_to_micros('pm.split_weight') }}) > 1000000 THEN 1000000
            ELSE SUM({{ fee_tax_to_micros('pm.split_weight') }})
        END                                                 AS coverage_micros
    FROM {{ source('mirror', 'plan_line_mappings') }} pm
    WHERE pm.status = 'active'
    GROUP BY pm.plan_id, pm.connector, pm.campaign_ref
),

-- --------------------------------------------------------- CANDIDATE PAIRS ---
-- (ladder row x rule) pairs the rule COULD govern: same project, day inside the
-- rule's effective window, scope reaching the row. The per-day window test lives
-- here because only the ladder knows the day; a rule out of window is simply ABSENT
-- for that day -- not NO_MATCH, not a gap.
candidate_pairs AS (
    SELECT
        x.row_key                       AS row_key,
        x.project_id                    AS project_id,
        x.date                          AS date,
        x.connector                     AS connector,
        x.breakdown_dimension           AS breakdown_dimension,
        x.breakdown_value               AS breakdown_value,
        x.currency                      AS currency,
        x.net_media_micros              AS net_media_micros,
        r.rule_id                       AS rule_id,
        r.category                      AS category,
        r.form                          AS form,
        r.rate                          AS rate,
        r.amount_micros                 AS amount_micros,
        r.rule_currency                 AS rule_currency,
        r.base_target                   AS base_target,
        r.cascade_phase                 AS cascade_phase,
        r.sequence_order                AS sequence_order,
        r.scope_precedence              AS scope_precedence,
        r.effective_from                AS effective_from,
        r.scope_kind                    AS scope_kind,
        r.scope_state                   AS scope_state,
        CASE
            WHEN r.scope_state = 'UNRESOLVED'                       THEN 'SCOPE_UNRESOLVED'
            WHEN r.scope_state = 'AMBIGUOUS'                        THEN 'SCOPE_AMBIGUOUS'
            -- D5 verbatim: the operator scoped the rule to a plan, so spend outside
            -- the plan legitimately is not covered -> KNOWN-FALSE, +0 micros, NOT a gap.
            WHEN r.scope_kind = 'plan_version' AND pc.plan_id IS NULL THEN 'SCOPE_NOT_APPLICABLE'
            ELSE 'SCOPE_APPLIES'
        END                             AS scope_verdict,
        pc.coverage_micros              AS coverage_micros
    FROM ladder_rows x
    JOIN rules_ladder r
        ON  r.project_id = x.project_id
        AND x.date >= r.effective_from
        AND (r.effective_to IS NULL OR x.date <= r.effective_to)
        AND (
                r.scope_kind = 'project'
                -- D2: scope_ref absent from the dim -> we do not even know the
                -- connector, so the blast radius is the whole project.
             OR (r.scope_kind = 'datastream' AND r.scope_state = 'UNRESOLVED')
                -- RESOLVED and AMBIGUOUS both know the connector; AMBIGUOUS gaps
                -- that connector's rows and ONE IS NEVER PICKED.
             OR (r.scope_kind = 'datastream' AND r.scope_connector = x.connector)
             OR  r.scope_kind = 'plan_version'
            )
    LEFT JOIN plan_coverage pc
        ON  r.scope_kind    = 'plan_version'
        AND pc.plan_id      = r.scope_plan_id
        AND pc.connector    = x.connector
        AND pc.campaign_ref = x.breakdown_value
),

-- ----------------------------------------- D.3.2 RELATIONAL CONDITION MATCH --
-- EXTRACTED BY STORY 41.4 (ruling Q3). row_attributes_long / cond_eval / rule_state /
-- cond_gap used to be written out here; they now have exactly ONE definition site,
-- dbt/macros/fee_tax_condition_matcher.sql, which emits them under the SAME FOUR NAMES
-- so `evaluated` and `all_gaps` below are untouched by the move. The reason for the
-- extraction: ~70 lines of MONEY condition-matching were about to exist in three copies
-- (this ladder, 41.4's verification overlay, 41.5's revenue model), and drift between
-- copies means one surface computing a different total than another from the same rules
-- -- the exact failure this epic exists to prevent, arriving by maintenance rather than
-- by bug. The extraction is behaviour-preserving and the gate is numeric, not
-- argumentative: 41.3's singular tests must stay green with byte-identical pinned
-- integers (§A.3 of story 41.4).
-- conditions_relation is passed FROM HERE rather than called inside the macro, so this
-- model keeps its own explicit, greppable depends_on edge on
-- mirror.fee_tax_rule_conditions and the conformance test can assert the dependency set
-- per model. A shared macro is COMPILED TEXT, not a data edge: this model and the 41.4
-- overlay stay disjoint in the DAG.
{{ fee_tax_condition_matcher('row_attributes', 'candidate_pairs', source('mirror', 'fee_tax_rule_conditions')) }}

evaluated AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.breakdown_dimension,
        p.breakdown_value, p.currency, p.net_media_micros, p.rule_id, p.category,
        p.form, p.rate, p.amount_micros, p.rule_currency, p.base_target,
        p.cascade_phase, p.sequence_order, p.scope_precedence, p.effective_from,
        p.scope_kind, p.scope_verdict, p.coverage_micros,
        -- COALESCE(..., 0) IS the empty-conditions case: unconstrained == MATCH.
        COALESCE(rs.state_rank, 0)      AS state_rank,
        CASE
            WHEN p.scope_verdict = 'SCOPE_UNRESOLVED'     THEN 'SCOPE_UNRESOLVED'
            WHEN p.scope_verdict = 'SCOPE_AMBIGUOUS'      THEN 'SCOPE_AMBIGUOUS'
            WHEN p.scope_verdict = 'SCOPE_NOT_APPLICABLE' THEN 'NO_MATCH'
            WHEN COALESCE(rs.state_rank, 0) = 2           THEN 'UNRESOLVED'
            WHEN COALESCE(rs.state_rank, 0) = 1           THEN 'NO_MATCH'
            ELSE 'MATCH'
        END                             AS verdict
    FROM candidate_pairs p
    LEFT JOIN rule_state rs
        ON  rs.row_key = p.row_key
        AND rs.rule_id = p.rule_id
),

-- ------------------------------- D.5 SCOPE PRECEDENCE, APPLIED PER MATCHED ROW
-- A phase can legitimately host several distinct fees (a DSP fee AND an ad-serving
-- fee both sit in phase 2 and both must fire), so the override handle is the SLOT
-- (category, cascade_phase, sequence_order), not the phase. Without a slot-level key
-- you would either double-apply VAT or silently drop a legitimate second platform
-- fee. Because D3 removed the slot uniqueness constraint, two same-scope rules can
-- share a slot -- effective_from DESC, rule_id ASC makes the pick DETERMINISTIC (the
-- plan_vs_actual_daily discipline). No QUALIFY: it is not portable (§B.5).
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
        m.row_key, m.project_id, m.date, m.connector, m.breakdown_dimension,
        m.breakdown_value, m.currency, m.net_media_micros, m.rule_id, m.category,
        m.form, m.rate, m.amount_micros, m.rule_currency, m.base_target,
        m.cascade_phase, m.sequence_order, m.coverage_micros,
        -- §D.6. IS DISTINCT FROM is avoided in favour of the maximally safe form the
        -- story sanctions: a rule that declares no currency is treated as declaring
        -- the row's own, which is the only reading that cannot fabricate a mismatch.
        CASE
            WHEN m.form IN ('FLAT', 'CPM')
             AND COALESCE(m.rule_currency, m.currency) <> m.currency THEN 1
            ELSE 0
        END AS currency_mismatch,
        CASE
            WHEN m.form = 'GROSS_UP'
             AND (m.rate IS NULL OR m.rate >= 1 OR m.rate < 0) THEN 1
            ELSE 0
        END AS wht_invalid,
        -- F2: a (category, form) pair NO phase branch can execute must gap, never
        -- fall through to ELSE 0. Routable pairs enumerated POSITIVELY so a new form
        -- is unroutable until someone routes it, rather than silently free.
        CASE
            WHEN m.category = 'WHT_GROSS_UP'
                 AND m.form IN ('GROSS_UP', 'PERCENTAGE', 'FLAT', 'SPEND_TIERS') THEN 0
            WHEN m.category <> 'WHT_GROSS_UP'
                 AND m.form IN ('PERCENTAGE', 'FLAT', 'SPEND_TIERS') THEN 0
            ELSE 1
        END AS form_unroutable,
        -- F8: the coverage weight is a fraction of a row's spend, so it is meaningful
        -- only for a dimensionless RATE. Anything else at coverage <> 1.0 is refused
        -- rather than silently billed at 100 %.
        CASE
            WHEN m.form <> 'PERCENTAGE'
             AND m.coverage_micros IS NOT NULL
             AND m.coverage_micros <> 1000000 THEN 1
            ELSE 0
        END AS coverage_not_applicable
    FROM matched_ranked m
    WHERE m._rn = 1
),

-- ------------------------------------------------ D.4.3 SPEND_TIERS SUPPORT --
tier_rules AS (
    SELECT
        m.row_key, m.rule_id, m.project_id, m.connector, m.date,
        m.breakdown_dimension, m.breakdown_value, m.net_media_micros
    FROM matched m
    WHERE m.form = 'SPEND_TIERS'
),

tier_day_net AS (
    SELECT
        rule_id, project_id, connector, date,
        SUM(net_media_micros) AS day_net_micros
    FROM tier_rules
    GROUP BY rule_id, project_id, connector, date
),

tier_day_cum AS (
    -- Day-level inclusive cumulative: this is what selects the CLIFF band, so the
    -- whole of the crossing day is already at the new rate.
    SELECT
        rule_id, project_id, connector, date, day_net_micros,
        SUM(day_net_micros) OVER (
            PARTITION BY rule_id, project_id, connector
            ORDER BY date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS cum_incl_micros
    FROM tier_day_net
),

tier_row_cum AS (
    -- Row-level extension of the SAME frame, used by the MARGINAL mode only.
    -- The ladder grain is finer than the day (several campaigns per connector-day),
    -- so the day-level marginal fee has no exact allocation back to rows. Ordering
    -- rows by (date, breakdown_dimension, breakdown_value) keeps every day's rows
    -- contiguous, and because consecutive rows SHARE their boundary cumulative the
    -- per-row differences TELESCOPE EXACTLY: the rows of a day sum to that day's
    -- marginal fee, and the days sum to the period's, with no allocation residue.
    SELECT
        t.row_key, t.rule_id, t.net_media_micros,
        SUM(t.net_media_micros) OVER (
            PARTITION BY t.rule_id, t.project_id, t.connector
            ORDER BY t.date, t.breakdown_dimension, t.breakdown_value
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ) AS row_cum_incl_micros
    FROM tier_rules t
),

bands AS (
    SELECT
        CAST(t.rule_id AS STRING)             AS rule_id,
        CAST(t.tier_index AS BIGINT)          AS tier_index,
        CAST(t.threshold_micros AS BIGINT)    AS threshold_micros,
        t.rate                                AS band_rate,
        COALESCE(t.mode, 'cliff')             AS mode,
        LEAD(CAST(t.threshold_micros AS BIGINT)) OVER (
            PARTITION BY t.rule_id ORDER BY t.tier_index
        )                                     AS next_threshold_micros
    FROM {{ source('mirror', 'fee_tax_rule_tiers') }} t
),

band_meta AS (
    -- `mode` is denormalised onto every band row by migration 119 and a CHECK
    -- constraint forbids a bare array, so it is constant per rule and MIN() is just a
    -- way to read it. Aliased tier_mode (never `mode`) because `mode` is also a DuckDB
    -- aggregate function name and an unqualified reference is ambiguous to read.
    SELECT
        b.rule_id                AS rule_id,
        COUNT(*)                 AS n_bands,
        MIN(b.mode)              AS tier_mode,
        MIN(b.threshold_micros)  AS min_threshold_micros,
        MAX(CASE WHEN b.next_threshold_micros IS NOT NULL
                  AND b.next_threshold_micros <= b.threshold_micros THEN 1 ELSE 0 END) AS not_ascending,
        MAX(CASE WHEN b.mode NOT IN ('cliff', 'marginal') THEN 1 ELSE 0 END)           AS bad_mode
    FROM bands b
    GROUP BY b.rule_id
),

cliff_pick AS (
    -- The LAST band whose floor is at or below the inclusive cumulative.
    SELECT row_key, rule_id, band_rate
    FROM (
        SELECT
            tr.row_key      AS row_key,
            tr.rule_id      AS rule_id,
            b.band_rate     AS band_rate,
            ROW_NUMBER() OVER (
                PARTITION BY tr.row_key, tr.rule_id
                ORDER BY b.threshold_micros DESC
            ) AS _rn
        FROM tier_rules tr
        JOIN tier_day_cum dc
            ON  dc.rule_id    = tr.rule_id
            AND dc.project_id = tr.project_id
            AND dc.connector  = tr.connector
            AND dc.date       = tr.date
        JOIN bands b
            ON b.rule_id = tr.rule_id
        WHERE b.threshold_micros <= dc.cum_incl_micros
    ) ranked_bands
    WHERE _rn = 1
),

marginal_fee AS (
    -- Cumulative fee at two cumulative levels; the component is their difference.
    -- tranche(C, band) = GREATEST(0, LEAST(C, COALESCE(next_threshold, C)) - threshold).
    -- One rounding per (band, level) rather than one per level: the ban on a
    -- parameterised DECIMAL(p,s) outside a macro, plus the two-macro budget, means
    -- the exact-decimal SUM has to be expressed as a SUM of exactly-rounded per-band
    -- fees. The telescoping identity is unaffected -- consecutive rows share their
    -- boundary level, so the roundings cancel pairwise and the period total is exact.
    SELECT
        rc.row_key  AS row_key,
        rc.rule_id  AS rule_id,
        SUM({{ fee_tax_pct_of_micros(tranche_incl, 'b.band_rate') }}) AS fee_incl_micros,
        SUM({{ fee_tax_pct_of_micros(tranche_excl, 'b.band_rate') }}) AS fee_excl_micros
    FROM tier_row_cum rc
    JOIN bands b
        ON b.rule_id = rc.rule_id
    GROUP BY rc.row_key, rc.rule_id
),

tier_component AS (
    SELECT
        tr.row_key      AS row_key,
        tr.rule_id      AS rule_id,
        CASE
            -- No band at all (a SPEND_TIERS rule with an empty ladder).
            WHEN bm.rule_id IS NULL OR bm.n_bands = 0                          THEN 1
            -- Non-ascending floors, or a mode outside {cliff, marginal} -- never a
            -- silent fallback to cliff.
            WHEN bm.not_ascending = 1 OR bm.bad_mode = 1                       THEN 1
            -- No band at or below the cumulative base (i.e. no 0 floor): the first
            -- tranche of spend would be unpriced. Malformed, never a silent 0.
            WHEN bm.tier_mode = 'cliff' AND cp.band_rate IS NULL               THEN 1
            WHEN bm.tier_mode = 'marginal'
                 AND (mf.row_key IS NULL OR bm.min_threshold_micros > 0)       THEN 1
            ELSE 0
        END AS tiers_malformed,
        CASE
            WHEN bm.rule_id IS NULL OR bm.n_bands = 0                          THEN NULL
            WHEN bm.not_ascending = 1 OR bm.bad_mode = 1                       THEN NULL
            WHEN bm.tier_mode = 'marginal'
                 THEN CASE WHEN bm.min_threshold_micros > 0 THEN NULL
                           ELSE mf.fee_incl_micros - mf.fee_excl_micros END
            WHEN cp.band_rate IS NULL                                          THEN NULL
            ELSE {{ fee_tax_pct_of_micros('tr.net_media_micros', 'cp.band_rate') }}
        END AS component_micros
    FROM tier_rules tr
    LEFT JOIN band_meta    bm ON bm.rule_id = tr.rule_id
    LEFT JOIN cliff_pick   cp ON cp.row_key = tr.row_key AND cp.rule_id = tr.rule_id
    LEFT JOIN marginal_fee mf ON mf.row_key = tr.row_key AND mf.rule_id = tr.rule_id
),

-- ------------------------------------------------------------- MATCHED_OK ---
-- Matched, precedence-picked, and free of every form-level refusal: cross-currency,
-- invalid WHT rate, malformed tier ladder, unroutable (category, form) pair, and a
-- plan coverage that does not apply to this form. Each refusal ALSO raises its own
-- typed gap below -- a refused rule never silently contributes 0.
matched_ok AS (
    SELECT
        m.row_key, m.project_id, m.date, m.currency, m.rule_id, m.category, m.form,
        m.base_target, m.rate, m.amount_micros, m.coverage_micros, m.net_media_micros,
        tc.component_micros AS tier_component_micros
    FROM matched m
    LEFT JOIN tier_component tc
        ON  tc.row_key = m.row_key
        AND tc.rule_id = m.rule_id
    WHERE m.currency_mismatch = 0
      AND m.wht_invalid = 0
      AND m.form_unroutable = 0
      AND m.coverage_not_applicable = 0
      AND COALESCE(tc.tiers_malformed, 0) = 0
),

-- ------------------------------ F8: FLAT APPLIES ONCE PER DAY, THEN ALLOCATED
-- The cumulative-share form of largest-remainder: a row's allocation is
-- floor_share(cumulative through it) - floor_share(cumulative before it), which
-- TELESCOPES to exactly amount_micros over the group because the last row's
-- cumulative equals the total and the macro short-circuits that boundary to the
-- amount itself. Ordering by row_key makes the split deterministic.
-- The group is (rule_id, project_id, date, currency): per DAY as the ruling requires,
-- and per RULE so a datastream-scoped FLAT spreads only over the rows it governs.
flat_rows AS (
    SELECT
        row_key, rule_id, project_id, date, currency, amount_micros,
        -- A negative net media (a credit) must not invert the pro-rata weights, and a
        -- mixed-sign day has no honest pro-rata at all: weight by the ABSOLUTE net so
        -- the split stays monotone, and fall back to an ordinal spread when the day
        -- carries no net media at all.
        ABS(COALESCE(net_media_micros, 0)) AS weight_micros
    FROM matched_ok
    WHERE form = 'FLAT'
),

flat_cum AS (
    SELECT
        row_key, rule_id, amount_micros, weight_micros,
        SUM(weight_micros) OVER (
            PARTITION BY rule_id, project_id, date, currency
            ORDER BY row_key
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        )                                                       AS cum_weight_micros,
        SUM(weight_micros) OVER (
            PARTITION BY rule_id, project_id, date, currency
        )                                                       AS total_weight_micros,
        ROW_NUMBER() OVER (
            PARTITION BY rule_id, project_id, date, currency ORDER BY row_key
        )                                                       AS row_ordinal,
        COUNT(*) OVER (
            PARTITION BY rule_id, project_id, date, currency
        )                                                       AS group_row_count
    FROM flat_rows
),

flat_alloc AS (
    SELECT
        row_key,
        rule_id,
        CASE
            WHEN total_weight_micros > 0 THEN
                {{ fee_tax_alloc_floor_micros('amount_micros', 'cum_weight_micros', 'total_weight_micros') }}
                - {{ fee_tax_alloc_floor_micros('amount_micros', 'cum_weight_micros - weight_micros', 'total_weight_micros') }}
            ELSE
                {{ fee_tax_alloc_floor_micros('amount_micros', 'row_ordinal', 'group_row_count') }}
                - {{ fee_tax_alloc_floor_micros('amount_micros', 'row_ordinal - 1', 'group_row_count') }}
        END AS component_micros
    FROM flat_cum
),

-- ----------------------------------------------------------------- FIRED -----
fired AS (
    SELECT
        m.row_key           AS row_key,
        m.rule_id           AS rule_id,
        m.category          AS category,
        m.form              AS form,
        m.base_target       AS base_target,
        m.rate              AS rate,
        m.coverage_micros   AS coverage_micros,
        m.tier_component_micros AS tier_component_micros,
        fa.component_micros AS flat_component_micros
    FROM matched_ok m
    LEFT JOIN flat_alloc fa
        ON  fa.row_key = m.row_key
        AND fa.rule_id = m.rule_id
),

-- ------------------------------------------------------------ GAP LEDGER -----
-- A gap is raised ONLY when a confirmed, in-window rule ACTUALLY CONSTRAINS the
-- unresolvable attribute (or is itself unexecutable). A project whose rules
-- constrain nothing but connector composes cleanly with no gap at all.
all_gaps AS (
    SELECT row_key, 'DATASTREAM_SCOPE_AMBIGUOUS' AS gap_code, category
    FROM evaluated WHERE verdict = 'SCOPE_AMBIGUOUS'
    UNION ALL
    SELECT row_key, 'RULE_SCOPE_UNRESOLVED' AS gap_code, category
    FROM evaluated WHERE verdict = 'SCOPE_UNRESOLVED'
    UNION ALL
    SELECT e.row_key, cg.gap_code, e.category
    FROM evaluated e
    JOIN cond_gap cg
        ON  cg.row_key = e.row_key
        AND cg.rule_id = e.rule_id
    WHERE e.verdict = 'UNRESOLVED'
      AND cg.gap_code IS NOT NULL
    UNION ALL
    SELECT row_key, 'CURRENCY_MISMATCH' AS gap_code, category
    FROM matched WHERE currency_mismatch = 1
    UNION ALL
    SELECT row_key, 'WHT_RATE_INVALID' AS gap_code, category
    FROM matched WHERE wht_invalid = 1
    UNION ALL
    SELECT row_key, 'RULE_FORM_UNROUTABLE' AS gap_code, category
    FROM matched WHERE form_unroutable = 1
    UNION ALL
    SELECT row_key, 'COVERAGE_NOT_APPLICABLE' AS gap_code, category
    FROM matched WHERE coverage_not_applicable = 1
    UNION ALL
    SELECT m.row_key, 'TIERS_MALFORMED' AS gap_code, m.category
    FROM matched m
    JOIN tier_component tc
        ON  tc.row_key = m.row_key
        AND tc.rule_id = m.rule_id
    WHERE tc.tiers_malformed = 1
),

row_gaps AS (
    SELECT
        row_key,
        MAX(CASE WHEN gap_code = 'COUNTRY_UNRESOLVED'         THEN 1 ELSE 0 END) AS g_country,
        MAX(CASE WHEN gap_code = 'MARKET_UNRESOLVED'          THEN 1 ELSE 0 END) AS g_market,
        MAX(CASE WHEN gap_code = 'PLACEMENT_TYPE_UNRESOLVED'  THEN 1 ELSE 0 END) AS g_placement,
        MAX(CASE WHEN gap_code = 'TAX_CODE_UNRESOLVED'        THEN 1 ELSE 0 END) AS g_tax_code,
        MAX(CASE WHEN gap_code = 'SOURCE_TYPE_UNRESOLVED'     THEN 1 ELSE 0 END) AS g_source_type,
        MAX(CASE WHEN gap_code = 'CONDITION_KEY_UNKNOWN'      THEN 1 ELSE 0 END) AS g_key_unknown,
        MAX(CASE WHEN gap_code = 'CURRENCY_MISMATCH'          THEN 1 ELSE 0 END) AS g_currency,
        MAX(CASE WHEN gap_code = 'DATASTREAM_SCOPE_AMBIGUOUS' THEN 1 ELSE 0 END) AS g_ds_ambiguous,
        MAX(CASE WHEN gap_code = 'RULE_SCOPE_UNRESOLVED'      THEN 1 ELSE 0 END) AS g_scope,
        MAX(CASE WHEN gap_code = 'WHT_RATE_INVALID'           THEN 1 ELSE 0 END) AS g_wht,
        MAX(CASE WHEN gap_code = 'TIERS_MALFORMED'            THEN 1 ELSE 0 END) AS g_tiers,
        MAX(CASE WHEN gap_code = 'RULE_FORM_UNROUTABLE'       THEN 1 ELSE 0 END) AS g_form,
        MAX(CASE WHEN gap_code = 'COVERAGE_NOT_APPLICABLE'    THEN 1 ELSE 0 END) AS g_coverage,
        -- Which PHASE could not be fully composed (so its column goes NULL rather
        -- than showing a fabricated 0).
        MAX(CASE WHEN category = 'PLATFORM_FEE'   THEN 1 ELSE 0 END) AS gap_phase_platform,
        MAX(CASE WHEN category = 'REGULATORY_TAX' THEN 1 ELSE 0 END) AS gap_phase_regulatory,
        MAX(CASE WHEN category = 'WHT_GROSS_UP'   THEN 1 ELSE 0 END) AS gap_phase_wht,
        MAX(CASE WHEN category = 'AGENCY_FEE'     THEN 1 ELSE 0 END) AS gap_phase_agency,
        MAX(CASE WHEN category = 'SALES_TAX'      THEN 1 ELSE 0 END) AS gap_phase_sales
    FROM all_gaps
    GROUP BY row_key
),

-- ############################ THE SIX PHASES ################################
phase1_net AS (
    SELECT
        x.row_key, x.project_id, x.date, x.connector, x.breakdown_dimension,
        x.breakdown_value, x.currency, x.net_media_micros,
        x.fx_rate, x.fx_as_of_date, x.fx_source, x.fx_tier, x.pull_id,
        x.net_media_micros AS subtotal_micros
    FROM ladder_rows x
),

phase2_calc AS (
    SELECT
        p.row_key AS row_key,
        SUM(CASE
                WHEN f.form = 'PERCENTAGE' THEN {{ pct_component }}
                WHEN f.form = 'FLAT'        THEN f.flat_component_micros
                WHEN f.form = 'SPEND_TIERS' THEN f.tier_component_micros
                ELSE 0
            END) AS component_micros
    FROM phase1_net p
    JOIN fired f
        ON  f.row_key  = p.row_key
        AND f.category = 'PLATFORM_FEE'
    GROUP BY p.row_key
),

phase2_platform AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.breakdown_dimension,
        p.breakdown_value, p.currency, p.net_media_micros,
        p.fx_rate, p.fx_as_of_date, p.fx_source, p.fx_tier, p.pull_id,
        COALESCE(c.component_micros, 0)                     AS platform_fee_known_micros,
        p.subtotal_micros + COALESCE(c.component_micros, 0) AS subtotal_micros
    FROM phase1_net p
    LEFT JOIN phase2_calc c ON c.row_key = p.row_key
),

phase3_calc AS (
    SELECT
        p.row_key AS row_key,
        SUM(CASE
                WHEN f.form = 'PERCENTAGE' THEN {{ pct_component }}
                WHEN f.form = 'FLAT'        THEN f.flat_component_micros
                WHEN f.form = 'SPEND_TIERS' THEN f.tier_component_micros
                ELSE 0
            END) AS component_micros
    FROM phase2_platform p
    JOIN fired f
        ON  f.row_key  = p.row_key
        AND f.category = 'REGULATORY_TAX'
    GROUP BY p.row_key
),

phase3_regulatory AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.breakdown_dimension,
        p.breakdown_value, p.currency, p.net_media_micros,
        p.fx_rate, p.fx_as_of_date, p.fx_source, p.fx_tier, p.pull_id,
        p.platform_fee_known_micros,
        COALESCE(c.component_micros, 0)                     AS regulatory_tax_known_micros,
        p.subtotal_micros + COALESCE(c.component_micros, 0) AS subtotal_micros
    FROM phase2_platform p
    LEFT JOIN phase3_calc c ON c.row_key = p.row_key
),

phase4_guard AS (
    -- F10. The magnitude guard can only be evaluated once the PHASE-ENTRY base is
    -- known, which is here and not in `matched`. When the macro returns NULL for an
    -- in-range rate whose base would overflow INT64, SQL's SUM() silently SKIPS that
    -- term -- so without this flag the rule would contribute nothing under a green
    -- complete flag, which is exactly the F2 failure in another disguise.
    -- SUM() skipping the NULL is the RIGHT arithmetic (the phase still advances with
    -- the rules that DID resolve); the flag is what makes the phase column NULL and
    -- the row incomplete.
    SELECT
        p.row_key AS row_key,
        MAX(CASE
                WHEN f.form = 'GROSS_UP'
                 AND ({{ fee_tax_grossup_micros(phase_base, 'f.rate') }}) IS NULL THEN 1
                ELSE 0
            END) AS wht_base_overflow
    FROM phase3_regulatory p
    JOIN fired f
        ON  f.row_key  = p.row_key
        AND f.category = 'WHT_GROSS_UP'
    GROUP BY p.row_key
),

applied AS (
    -- AD-9 provenance. Defined HERE, after phase4_guard, and not next to `fired`,
    -- because a GROSS_UP rule whose base overflows is in `fired` (its RATE is valid --
    -- only the magnitude is not) yet contributes nothing. Listing it as applied while
    -- its phase reads NULL would be provenance that contradicts the number beside it.
    SELECT
        f.row_key,
        STRING_AGG(f.rule_id, '|' ORDER BY f.rule_id) AS applied_rule_ids,
        COUNT(*)                                      AS applied_rule_count
    FROM fired f
    LEFT JOIN phase4_guard w
        ON w.row_key = f.row_key
    WHERE NOT (f.form = 'GROSS_UP' AND COALESCE(w.wht_base_overflow, 0) = 1)
    GROUP BY f.row_key
),

phase4_calc AS (
    -- The one non-additive phase. The grossed TOTAL is rounded once; the component is
    -- exact integer subtraction, so base + component == grossed_total to the micro.
    SELECT
        p.row_key AS row_key,
        SUM(CASE
                WHEN f.form = 'GROSS_UP' THEN
                    {{ fee_tax_grossup_micros(phase_base, 'f.rate') }}
                    - ({{ phase_base }})
                WHEN f.form = 'PERCENTAGE' THEN {{ pct_component }}
                WHEN f.form = 'FLAT'        THEN f.flat_component_micros
                WHEN f.form = 'SPEND_TIERS' THEN f.tier_component_micros
                ELSE 0
            END) AS component_micros
    FROM phase3_regulatory p
    JOIN fired f
        ON  f.row_key  = p.row_key
        AND f.category = 'WHT_GROSS_UP'
    GROUP BY p.row_key
),

phase4_wht AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.breakdown_dimension,
        p.breakdown_value, p.currency, p.net_media_micros,
        p.fx_rate, p.fx_as_of_date, p.fx_source, p.fx_tier, p.pull_id,
        p.platform_fee_known_micros,
        p.regulatory_tax_known_micros,
        COALESCE(c.component_micros, 0)                     AS wht_gross_up_known_micros,
        p.subtotal_micros + COALESCE(c.component_micros, 0) AS subtotal_micros
    FROM phase3_regulatory p
    LEFT JOIN phase4_calc c ON c.row_key = p.row_key
),

phase5_calc AS (
    SELECT
        p.row_key AS row_key,
        SUM(CASE
                WHEN f.form = 'PERCENTAGE' THEN {{ pct_component }}
                WHEN f.form = 'FLAT'        THEN f.flat_component_micros
                WHEN f.form = 'SPEND_TIERS' THEN f.tier_component_micros
                ELSE 0
            END) AS component_micros
    FROM phase4_wht p
    JOIN fired f
        ON  f.row_key  = p.row_key
        AND f.category = 'AGENCY_FEE'
    GROUP BY p.row_key
),

phase5_agency AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.breakdown_dimension,
        p.breakdown_value, p.currency, p.net_media_micros,
        p.fx_rate, p.fx_as_of_date, p.fx_source, p.fx_tier, p.pull_id,
        p.platform_fee_known_micros,
        p.regulatory_tax_known_micros,
        p.wht_gross_up_known_micros,
        COALESCE(c.component_micros, 0)                     AS agency_fee_known_micros,
        p.subtotal_micros + COALESCE(c.component_micros, 0) AS subtotal_micros
    FROM phase4_wht p
    LEFT JOIN phase5_calc c ON c.row_key = p.row_key
),

phase6_calc AS (
    SELECT
        p.row_key AS row_key,
        SUM(CASE
                WHEN f.form = 'PERCENTAGE' THEN {{ pct_component }}
                WHEN f.form = 'FLAT'        THEN f.flat_component_micros
                WHEN f.form = 'SPEND_TIERS' THEN f.tier_component_micros
                ELSE 0
            END) AS component_micros
    FROM phase5_agency p
    JOIN fired f
        ON  f.row_key  = p.row_key
        AND f.category = 'SALES_TAX'
    GROUP BY p.row_key
),

phase6_vat AS (
    SELECT
        p.row_key, p.project_id, p.date, p.connector, p.breakdown_dimension,
        p.breakdown_value, p.currency, p.net_media_micros,
        p.fx_rate, p.fx_as_of_date, p.fx_source, p.fx_tier, p.pull_id,
        p.platform_fee_known_micros,
        p.regulatory_tax_known_micros,
        p.wht_gross_up_known_micros,
        p.agency_fee_known_micros,
        p.subtotal_micros                                   AS subtotal_ht_known_micros,
        COALESCE(c.component_micros, 0)                     AS sales_tax_known_micros,
        p.subtotal_micros + COALESCE(c.component_micros, 0) AS total_ttc_known_micros
    FROM phase5_agency p
    LEFT JOIN phase6_calc c ON c.row_key = p.row_key
),

assembled AS (
    SELECT
        p.row_key,
        p.project_id,
        p.date,
        p.connector,
        p.breakdown_dimension,
        p.breakdown_value,
        p.currency,
        p.net_media_micros,
        p.platform_fee_known_micros,
        p.regulatory_tax_known_micros,
        p.wht_gross_up_known_micros,
        p.agency_fee_known_micros,
        p.subtotal_ht_known_micros,
        p.sales_tax_known_micros,
        p.total_ttc_known_micros,
        p.fx_rate, p.fx_as_of_date, p.fx_source, p.fx_tier, p.pull_id,
        COALESCE(a.applied_rule_ids, '')    AS applied_rule_ids,
        COALESCE(a.applied_rule_count, 0)   AS applied_rule_count,
        COALESCE(g.g_country, 0)            AS g_country,
        COALESCE(g.g_market, 0)             AS g_market,
        COALESCE(g.g_placement, 0)          AS g_placement,
        COALESCE(g.g_tax_code, 0)           AS g_tax_code,
        COALESCE(g.g_source_type, 0)        AS g_source_type,
        COALESCE(g.g_key_unknown, 0)        AS g_key_unknown,
        COALESCE(g.g_currency, 0)           AS g_currency,
        COALESCE(g.g_ds_ambiguous, 0)       AS g_ds_ambiguous,
        COALESCE(g.g_scope, 0)              AS g_scope,
        COALESCE(g.g_wht, 0)                AS g_wht,
        COALESCE(g.g_tiers, 0)              AS g_tiers,
        COALESCE(g.g_form, 0)               AS g_form,
        COALESCE(g.g_coverage, 0)           AS g_coverage,
        COALESCE(w.wht_base_overflow, 0)    AS g_wht_overflow,
        COALESCE(g.gap_phase_platform, 0)   AS gap_phase_platform,
        COALESCE(g.gap_phase_regulatory, 0) AS gap_phase_regulatory,
        -- The overflow guard is a PHASE-4 gap by construction, so it also nulls the
        -- phase-4 column even when the pre-phase gap ledger found nothing.
        CASE WHEN COALESCE(g.gap_phase_wht, 0) = 1
               OR COALESCE(w.wht_base_overflow, 0) = 1 THEN 1 ELSE 0
        END                                 AS gap_phase_wht,
        COALESCE(g.gap_phase_agency, 0)     AS gap_phase_agency,
        COALESCE(g.gap_phase_sales, 0)      AS gap_phase_sales,
        ra.attr_country,
        ra.attr_market,
        ra.attr_source_type,
        ra.resolution_source,
        ra.country_gap_reason,
        ra.market_gap_reason,
        ra.source_type_gap_reason,
        ra.bridge_gap_code
    FROM phase6_vat p
    LEFT JOIN applied        a  ON a.row_key  = p.row_key
    LEFT JOIN row_gaps       g  ON g.row_key  = p.row_key
    LEFT JOIN phase4_guard   w  ON w.row_key  = p.row_key
    LEFT JOIN row_attributes ra ON ra.row_key = p.row_key
),

coded AS (
    SELECT
        s.*,
        -- '|'-joined, SORTED, distinct gap codes; '' when none. The CASE chain is
        -- written in alphabetical order, so the result is sorted BY CONSTRUCTION --
        -- no aggregate ordering to trust across engines.
        CASE WHEN s.g_key_unknown   = 1 THEN '|CONDITION_KEY_UNKNOWN'      ELSE '' END
     || CASE WHEN s.g_country       = 1 THEN '|COUNTRY_UNRESOLVED'         ELSE '' END
     || CASE WHEN s.g_coverage      = 1 THEN '|COVERAGE_NOT_APPLICABLE'    ELSE '' END
     || CASE WHEN s.g_currency      = 1 THEN '|CURRENCY_MISMATCH'          ELSE '' END
     || CASE WHEN s.g_ds_ambiguous  = 1 THEN '|DATASTREAM_SCOPE_AMBIGUOUS' ELSE '' END
     || CASE WHEN s.g_market        = 1 THEN '|MARKET_UNRESOLVED'          ELSE '' END
     || CASE WHEN s.g_placement     = 1 THEN '|PLACEMENT_TYPE_UNRESOLVED'  ELSE '' END
     || CASE WHEN s.g_form          = 1 THEN '|RULE_FORM_UNROUTABLE'       ELSE '' END
     || CASE WHEN s.g_scope         = 1 THEN '|RULE_SCOPE_UNRESOLVED'      ELSE '' END
     || CASE WHEN s.g_source_type   = 1 THEN '|SOURCE_TYPE_UNRESOLVED'     ELSE '' END
     || CASE WHEN s.g_tax_code      = 1 THEN '|TAX_CODE_UNRESOLVED'        ELSE '' END
     || CASE WHEN s.g_tiers         = 1 THEN '|TIERS_MALFORMED'            ELSE '' END
     || CASE WHEN s.g_wht_overflow  = 1 THEN '|WHT_BASE_OVERFLOW'          ELSE '' END
     || CASE WHEN s.g_wht           = 1 THEN '|WHT_RATE_INVALID'           ELSE '' END
            AS gap_codes_prefixed
    FROM assembled s
)

SELECT
    c.project_id,
    c.date,
    c.connector,
    c.breakdown_dimension,
    c.breakdown_value,
    c.currency,

    -- Phase 1 is a READ of a real fact: never NULL, never derived.
    c.net_media_micros                                                       AS net_media_micros,

    -- A phase column is NULL when a rule routed to that phase could not be
    -- evaluated -- so a 0 always means a real zero (AC3 / T13).
    CASE WHEN c.gap_phase_platform   = 1 THEN NULL ELSE c.platform_fee_known_micros    END AS platform_fee_micros,
    CASE WHEN c.gap_phase_regulatory = 1 THEN NULL ELSE c.regulatory_tax_known_micros  END AS regulatory_tax_micros,
    CASE WHEN c.gap_phase_wht        = 1 THEN NULL ELSE c.wht_gross_up_known_micros    END AS wht_gross_up_micros,
    CASE WHEN c.gap_phase_agency     = 1 THEN NULL ELSE c.agency_fee_known_micros      END AS agency_fee_micros,

    -- Headline totals: NULL whenever the ladder is incomplete. You may look at the
    -- parts; you may not read a total we could not compute (AC4, arbitration B1).
    CASE WHEN c.gap_codes_prefixed = '' THEN c.subtotal_ht_known_micros ELSE NULL END AS subtotal_ht_micros,
    CASE WHEN c.gap_phase_sales    = 1  THEN NULL ELSE c.sales_tax_known_micros     END AS sales_tax_micros,
    CASE WHEN c.gap_codes_prefixed = '' THEN c.total_ttc_known_micros   ELSE NULL END AS total_ttc_micros,

    -- FX carried, never re-applied (C.6 / Epic 39.10) + pull provenance (AD-7).
    c.fx_rate,
    c.fx_as_of_date,
    c.fx_source,
    c.fx_tier,
    c.pull_id,

    -- AD-9 provenance.
    c.applied_rule_ids,
    c.applied_rule_count,

    -- Typed gaps (§D.3.3). NOT copies of the bridge's gap_code.
    c.g_country      = 1 AS gap_country_unresolved,
    c.g_market       = 1 AS gap_market_unresolved,
    c.g_placement    = 1 AS gap_placement_type_unresolved,
    c.g_tax_code     = 1 AS gap_tax_code_unresolved,
    c.g_source_type  = 1 AS gap_source_type_unresolved,
    c.g_key_unknown  = 1 AS gap_condition_key_unknown,
    c.g_currency     = 1 AS gap_currency_mismatch,
    c.g_ds_ambiguous = 1 AS gap_datastream_scope_ambiguous,
    c.g_scope        = 1 AS gap_rule_scope_unresolved,
    c.g_wht          = 1 AS gap_wht_rate_invalid,
    c.g_wht_overflow = 1 AS gap_wht_base_overflow,
    c.g_tiers        = 1 AS gap_tiers_malformed,
    c.g_form         = 1 AS gap_rule_form_unroutable,
    c.g_coverage     = 1 AS gap_coverage_not_applicable,
    CASE WHEN c.gap_codes_prefixed = '' THEN '' ELSE SUBSTR(c.gap_codes_prefixed, 2) END AS gap_codes,
    c.gap_codes_prefixed = ''                                                AS is_ladder_complete,

    -- 41.2's attributes and its own provenance, carried for drill-down. NEVER
    -- branched on, and bridge_gap_code NEVER drives is_ladder_complete.
    c.attr_country,
    c.attr_market,
    c.attr_source_type,
    c.resolution_source,
    c.country_gap_reason,
    c.market_gap_reason,
    c.source_type_gap_reason,
    c.bridge_gap_code
FROM coded c
{%- endif -%}
