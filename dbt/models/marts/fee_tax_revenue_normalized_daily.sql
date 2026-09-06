-- fee_tax_revenue_normalized_daily: TWO TAX VIEWS OF THE SAME MONEY -- Gross Revenue
-- TTC and Net Revenue HT -- per (project, date, connector, metric, currency)
-- (Epic 41, Story 41.5 / E41-FR07 / E41-AD7 / amendment C.5).
--
-- ============================ THIS MART IS A READ ============================
-- It READS fact_daily_kpi, fee_tax_rules_effective (41.3), fee_tax_country_resolution
-- (41.2), mirror.project_preferences, mirror.fee_tax_rule_conditions and the governed
-- seed fee_tax_revenue_scope. It creates NO new fact row and edits NO existing model.
-- fact_daily_kpi / cross_source_revenue / cross_source_conversions / semantic_roas /
-- plan_vs_actual_daily / dedup_estimate / metric_baselines and every 41.2 / 41.3 model
-- are STRICTLY UNTOUCHED. With the module OFF this view returns ZERO ROWS, which is
-- what makes E41-NFR01 true BY CONSTRUCTION rather than by proof (arbitration B2).
--
-- GRAIN (enforced by fee_tax_revenue_normalized_daily_grain_unique):
--   one row per (project_id, date, connector, metric, currency).
--   connector and metric stay IN THE GRAIN deliberately: different commerce sources
--   carry different VAT postures and different gateway economics, and Epic 27's
--   KEEP_SEPARATE discipline requires a CLAIMED metric to stay independently
--   inspectable. Deduplicating to one winning source is a CONSUMER-side decision and
--   happens ONCE, in fee_tax_revenue_alignment_daily.
--
-- ############################################################################
-- ##  TWO THINGS A FUTURE READER MUST NOT DISCOVER BY SURPRISE              ##
-- ############################################################################
--
-- 1. `conversions_value` IS DECLARED WITH NO EMITTER. It exists in
--    dbt/seeds/dim_metric.csv line 17 and dbt/seeds/money_metric_units.csv line 9, and
--    NO BLOCK OF fact_daily_kpi.sql PRODUCES A SINGLE ROW OF IT. So its absence from
--    fee_tax_revenue_scope.csv is DELIBERATE, not an oversight -- a scope row for a
--    metric that never lands would be dead configuration that READS AS COVERAGE. The
--    day a connector wires it, ONE SEED ROW makes it work with no model change.
--    cm360 emits `conversion_value` (SINGULAR), a different canonical name, itself
--    absent from money_metric_units.csv. That mismatch is PRE-EXISTING and NOT this
--    story's to fix (fixing it would edit a shared seed). Recorded, not patched.
--
-- 2. `ad_revenue` / `all_revenue` ARE adjust-ONLY, ON THREE PARALLEL SERIES -- the
--    revenue-side twin of the meta/tiktok triple-count trap (C.8 decision 10). adjust
--    crosses 8 metrics with ["app_token", "campaign_id", "network"] and EACH SERIES
--    TOTALS THE DAY INDEPENDENTLY. `revenue` itself is among those 8. A revenue model
--    that reads adjust without the canonical-dimension collapse TRIPLES revenue,
--    TRIPLES every fee derived from it and TRIPLES ROAS -- silently, because each
--    series is individually plausible. The `canonical_dim` CTE below applies the
--    ladder's C4 rule VERBATIM (the same expression, not a "simpler" second one) so the
--    two models cannot drift apart.
--
-- ############################################################################
-- ##  THE LANDED COMMERCE FIGURE IS TTC, AND THERE IS NO REAL PER-ORDER TAX ##
-- ############################################################################
--
-- Proven from the manifests, not assumed: shopify maps total_price -> revenue ("the sum
-- of all line item prices, discounts, shipping, TAXES and tips"), woocommerce maps
-- total -> revenue, stripe maps amount -> revenue (what the customer actually paid) and
-- square maps amount -> revenue. ALL FOUR LAND A TAX-INCLUSIVE FIGURE. adjust's revenue
-- is in-app SDK revenue whose store-net-vs-gross posture is NOT KNOWABLE from the feed:
-- it is UNDECLARED, which is a TYPED GAP, not a guess.
--
-- AND THE REAL TAX PAID PER ORDER IS NOT AVAILABLE ANYWHERE. Shopify's API catalogue
-- defines total_tax / current_total_tax and THE CONNECTOR NEVER PULLS THEM; no staging
-- model in the repo emits a tax field at all. So VAT MUST BE DERIVED FROM A DECLARED
-- RATE, and every derived figure is LABELLED AS SUCH through sales_tax_provenance. The
-- value 'measured_order_tax' is DEFINED AND RESERVED and THIS MODEL NEVER EMITS IT --
-- the same discipline 41.2 applied to `signals_disagree`: telling an operator to
-- reconcile a measurement that does not exist would be a lie. The day a connector lands
-- total_tax, the reserved branch activates and the derivation is superseded by the
-- measurement, exactly as the measured gateway fee already supersedes a declared rule.
--
-- THE GATEWAY FEE IS THE OPPOSITE CASE, AND THE ASYMMETRY IS CORRECT. stripe and square
-- both land a `fees` metric mapped from fee_amount: that IS a measured payment-gateway
-- fee. So where a measurement exists THE MEASUREMENT WINS AND THE DECLARED RULE IS
-- SUPPRESSED (payment_fee_rule_superseded = TRUE), which is the only reading under
-- which the two can never double-count.
--
-- ===================== STEADY STATE TODAY -- SPECIFIED, NOT BROKEN ==========
-- Two things keep HT unavailable on day one for most projects:
--   (1) no confirmed SALES_TAX / *_REVENUE rule exists until an operator confirms one
--       (41.2's auto-population lands status='proposed', inert by design); and
--   (2) an auto-populated VAT rule is country- and/or source_type-conditioned, and both
--       attributes are usually UNRESOLVABLE today -- nothing writes
--       app.datastream_source_types, so attr_source_type is NULL on EVERY row.
-- SO THE DAY-ONE HONEST END STATE IS "TTC ONLY, HT NULL, SALES_TAX_RULE_ABSENT or
-- SOURCE_TYPE_UNRESOLVED". That is the arbitrated answer (B1), not a broken build.
-- What keeps it useful: landed_revenue_micros and gross_revenue_ttc_micros stay
-- populated, the TTC row of the alignment view still composes, and the gap codes say
-- exactly which rule to confirm.
--
-- ===================== NULL DISCIPLINE (plan_vs_actual_daily / 41.3) ========
-- landed_revenue_micros is ALWAYS populated: it is a read of a real fact. Every DERIVED
-- column is NULL when it could not be computed, so a 0 ALWAYS MEANS "the computation
-- ran and the answer is zero". You may look at the parts; you may not read a total we
-- could not compute (E41-NFR02).
--
-- ===================== E41-AD9: KNOWN-FALSE vs UNRESOLVABLE =================
-- A condition that is KNOWN FALSE contributes exactly +0 micros and does NOT mark the
-- row incomplete (the operator scoped the rule elsewhere on purpose). An attribute that
-- is UNRESOLVABLE -- INCLUDING attr_source_type IS NULL -- is a TYPED GAP and the rule
-- does not fire. Reading NULL source_type as known-false would compute NOTHING while
-- reporting a complete, trustworthy total: the worst available failure mode.
--
-- ===================== ROUTABILITY IS ENUMERATED POSITIVELY (F2) ============
-- The revenue side routes EXACTLY: SALES_TAX -> PERCENTAGE; PAYMENT_FEE -> PERCENTAGE,
-- FLAT, PER_TRANSACTION. Anything else -> REVENUE_FORM_UNSUPPORTED, the rule does NOT
-- fire, and the derived columns go NULL. It is NEVER silently skipped: a confirmed
-- tiered gateway contract contributing 0 behind a green complete flag is the same
-- failure as reading a NULL attribute as known-false. Migration 119's
-- ck_fee_tax_rules_category_form / ck_fee_tax_rules_form_base_target make most of this
-- unrepresentable at declaration; this guard protects the rows already written (a
-- mirror-lag or pre-constraint row). CONSTRAINTS PROTECT THE FUTURE; GUARDS PROTECT THE
-- ROWS ALREADY WRITTEN.
--
-- ===================== MONEY, FX AND CROSS-CURRENCY =========================
-- Amounts compose as EXACT INTEGER MICROS, normalised PER SOURCE ROW with
-- fee_tax_to_micros and only then SUM()ed as BIGINTs -- never ROUND(SUM(value) * 1e6),
-- which would put the single rounding boundary after the float drift instead of before
-- it. ROUND_HALF_UP, ONE rounding boundary per component.
-- FX is CARRIED, NEVER RE-APPLIED: fact_daily_kpi already converted revenue to the
-- project canonical currency at read (fx_convert_at_read, Epic 39.10), and there is no
-- currency column on the fact -- the row's currency IS
-- mirror.project_preferences.canonical_currency. A rule-declared FLAT / PER_TRANSACTION
-- amount in another currency is REFUSED (CURRENCY_MISMATCH), not summed and NOT
-- CONVERTED: converting would apply FX a second time outside the read locus. PERCENTAGE
-- rates are dimensionless and are never subject to the check.
--
-- ===================== RULING R3: ZERO RESOLUTION LOGIC =====================
-- country / market / source_type enter through EXACTLY ONE LEFT JOIN on
-- fee_tax_country_resolution's six frozen keys. No dim_country, no market_bindings, no
-- datastream_country_binding_dim, no datastream_source_types anywhere in this file.
-- The three-valued matcher itself is the SHARED MACRO fee_tax_condition_matcher (41.4's
-- extraction), not a third copy -- drift between copies would mean one surface
-- computing a different total than another from the same rules.
--
-- ===================== NO CONNECTOR NAME IN THIS FILE =======================
-- Which metric of which connector is revenue, on what tax basis, and where its count
-- and its measured fee live, is DECLARATIVE DATA in dbt/seeds/fee_tax_revenue_scope.csv
-- (AD-2 / E41-NFR05), exactly as metric_source_priority.csv holds the priorities for
-- cross_source_revenue.sql.

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
    ['project_id', 'string'],
    ['date', 'date'],
    ['connector', 'string'],
    ['metric', 'string'],
    ['currency', 'string'],
    ['revenue_role', 'string'],
    ['landed_tax_posture', 'string'],
    ['landed_revenue_micros', 'bigint'],
    ['landed_basis', 'string'],
    ['gross_revenue_ttc_micros', 'bigint'],
    ['net_revenue_ht_micros', 'bigint'],
    ['sales_tax_micros', 'bigint'],
    ['sales_tax_rate', 'float'],
    ['sales_tax_provenance', 'string'],
    ['payment_fee_micros', 'bigint'],
    ['payment_fee_provenance', 'string'],
    ['payment_fee_rule_superseded', 'boolean'],
    ['transaction_count', 'bigint'],
    ['net_cash_received_ttc_micros', 'bigint'],
    ['applied_rule_ids', 'string'],
    ['applied_rule_count', 'bigint'],
    ['fx_rate', 'numeric'],
    ['fx_as_of_date', 'date'],
    ['fx_source', 'string'],
    ['fx_tier', 'string'],
    ['pull_id', 'string'],
    ['gap_country_unresolved', 'boolean'],
    ['gap_market_unresolved', 'boolean'],
    ['gap_placement_type_unresolved', 'boolean'],
    ['gap_tax_code_unresolved', 'boolean'],
    ['gap_source_type_unresolved', 'boolean'],
    ['gap_condition_key_unknown', 'boolean'],
    ['gap_currency_mismatch', 'boolean'],
    ['gap_datastream_scope_ambiguous', 'boolean'],
    ['gap_rule_scope_unresolved', 'boolean'],
    ['gap_sales_tax_rule_absent', 'boolean'],
    ['gap_vat_rate_invalid', 'boolean'],
    ['gap_revenue_tax_posture_undeclared', 'boolean'],
    ['gap_revenue_form_unsupported', 'boolean'],
    ['gap_transaction_count_unavailable', 'boolean'],
    ['gap_codes', 'string'],
    ['is_normalization_complete', 'boolean'],
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

WITH module_on AS (
    -- E41-FR01 / C.1. COALESCE(..., FALSE) covers both an explicit FALSE and the mirror
    -- landing the column as NULL. The CAST matters: the mirror writer types an EMPTY
    -- mirrored table's columns as strings.
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

scope AS (
    SELECT
        canonical_metric,
        connector,
        revenue_role,
        landed_tax_posture,
        COALESCE(transaction_count_metric, '') AS transaction_count_metric,
        COALESCE(measured_fee_metric, '')      AS measured_fee_metric
    FROM {{ ref('fee_tax_revenue_scope') }}
),

-- Every metric this model must READ, tagged by the role it plays. An empty cell in the
-- seed means "this connector offers none", which becomes a TYPED GAP the day a rule
-- needs it -- never a default and never an assumed value.
-- DISTINCT so two seed rows naming the same count (or fee) metric for one connector
-- cannot fan the fact join out and DOUBLE the aggregated value.
scope_metrics AS (
    SELECT DISTINCT connector, metric, metric_role
    FROM (
        SELECT connector, canonical_metric AS metric, 'REVENUE' AS metric_role FROM scope
        UNION ALL
        SELECT connector, transaction_count_metric AS metric, 'COUNT' AS metric_role
        FROM scope WHERE transaction_count_metric <> ''
        UNION ALL
        SELECT connector, measured_fee_metric AS metric, 'FEE' AS metric_role
        FROM scope WHERE measured_fee_metric <> ''
    ) u
),

-- ------------------------------------------------- C4 CANONICAL SINGLE SERIES
-- The ladder's rule, VERBATIM, per (project, date, connector, metric). For the four
-- commerce connectors this trivially picks 'day_total' (their only dimension); for an
-- adjust-shaped emitter it picks ONE self-contained series that totals the day instead
-- of summing three parallel ones.
canonical_dim AS (
    SELECT
        f.project_id                                    AS project_id,
        f.date                                          AS date,
        f.connector                                     AS connector,
        f.metric                                        AS metric,
        CASE
            WHEN MAX(CASE WHEN f.breakdown_dimension = 'campaign_id' THEN 1 ELSE 0 END) = 1
                THEN 'campaign_id'
            ELSE MIN(f.breakdown_dimension)
        END                                             AS dim
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    JOIN scope_metrics sm
        ON  sm.connector = f.connector
        AND sm.metric    = f.metric
    GROUP BY f.project_id, f.date, f.connector, f.metric
),

-- Per-source-row micros normalisation, THEN an exact integer SUM. `value_count` is the
-- same aggregation for a COUNT metric: a count is NOT money and must NOT pass through
-- fee_tax_to_micros, or a 412-order day would read as 412 000 000.
fact_scoped AS (
    SELECT
        f.project_id                                    AS project_id,
        CAST(f.date AS DATE)                            AS date,
        f.connector                                     AS connector,
        f.metric                                        AS metric,
        sm.metric_role                                  AS metric_role,
        m.canonical_currency                            AS currency,
        SUM({{ fee_tax_to_micros('f.value') }})         AS value_micros,
        SUM(CAST(ROUND(f.value) AS BIGINT))             AS value_count,
        MAX(f.fx_rate)                                  AS fx_rate,
        MAX(f.fx_as_of_date)                            AS fx_as_of_date,
        MAX(f.fx_source)                                AS fx_source,
        MAX(f.fx_tier)                                  AS fx_tier,
        MAX(f.pull_id)                                  AS pull_id
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN module_on m
        ON m.project_id = f.project_id
    JOIN scope_metrics sm
        ON  sm.connector = f.connector
        AND sm.metric    = f.metric
    JOIN canonical_dim cd
        ON  cd.project_id = f.project_id
        AND cd.date       = f.date
        AND cd.connector  = f.connector
        AND cd.metric     = f.metric
        AND cd.dim        = f.breakdown_dimension
    GROUP BY
        f.project_id, CAST(f.date AS DATE), f.connector, f.metric,
        sm.metric_role, m.canonical_currency
),

-- --------------------------------------------------------- THE REVENUE ROWS --
revenue_rows AS (
    SELECT
        fs.project_id || '|' || CAST(fs.date AS STRING) || '|' || fs.connector
            || '|' || fs.metric                         AS row_key,
        fs.project_id                                   AS project_id,
        fs.date                                         AS date,
        fs.connector                                    AS connector,
        fs.metric                                       AS metric,
        fs.currency                                     AS currency,
        s.revenue_role                                  AS revenue_role,
        s.landed_tax_posture                            AS landed_tax_posture,
        s.transaction_count_metric                      AS transaction_count_metric,
        s.measured_fee_metric                           AS measured_fee_metric,
        fs.value_micros                                 AS landed_revenue_micros,
        CASE s.landed_tax_posture
            WHEN 'TAX_INCLUSIVE' THEN 'TTC'
            WHEN 'TAX_EXCLUSIVE' THEN 'HT'
            ELSE 'UNRESOLVED'
        END                                             AS landed_basis,
        fs.fx_rate                                      AS fx_rate,
        fs.fx_as_of_date                                AS fx_as_of_date,
        fs.fx_source                                    AS fx_source,
        fs.fx_tier                                      AS fx_tier,
        fs.pull_id                                      AS pull_id
    FROM fact_scoped fs
    JOIN scope s
        ON  s.connector        = fs.connector
        AND s.canonical_metric = fs.metric
    WHERE fs.metric_role = 'REVENUE'
),

-- The transaction count actually available for a connector-day. Read BY NAME from the
-- seed, never inferred. A revenue row whose connector declares no count metric, or
-- whose declared count metric landed no fact for that day, simply finds nothing here --
-- and a PER_TRANSACTION rule then raises TRANSACTION_COUNT_UNAVAILABLE.
counts AS (
    SELECT
        fs.project_id, fs.date, fs.connector, fs.metric,
        fs.value_count AS transaction_count
    FROM fact_scoped fs
    WHERE fs.metric_role = 'COUNT'
),

-- The MEASURED gateway fee. Where this exists it BEATS a declared PAYMENT_FEE rule and
-- suppresses it, so the two can never double-count (measurement beats derivation).
measured_fees AS (
    SELECT
        fs.project_id, fs.date, fs.connector, fs.metric,
        fs.value_micros AS measured_fee_micros
    FROM fact_scoped fs
    WHERE fs.metric_role = 'FEE'
),

-- ------------------------- R3: ROW ATTRIBUTES -- ONE JOIN, ZERO RESOLUTION ---
-- 41.2's bridge is keyed at (project, date, connector, metric, breakdown_dimension,
-- breakdown_value) while this model's grain is the connector-day, so the bridge rows of
-- a group are COLLAPSED here. An attribute survives the collapse ONLY IF EVERY MEMBER
-- ROW RESOLVED IT AND THEY ALL AGREE: `COUNT(*) = COUNT(attr)` rejects a group where
-- some rows are unresolved, and `MIN = MAX` rejects a group whose rows disagree.
-- Anything else stays NULL, i.e. UNRESOLVED -- which is exactly right, because "some of
-- this day is French" is not an answer a rule can be evaluated against, and picking one
-- of the values would fabricate an attribution. For the four commerce connectors the
-- group is a SINGLE (day_total, all) row and this degenerates to reading it directly.
-- No dimension name is hard-coded: an adjust-shaped multi-value series collapses by the
-- same rule instead of by a literal that would silently stop matching.
bridge_rolled AS (
    SELECT
        r.project_id                    AS project_id,
        r.date                          AS date,
        r.connector                     AS connector,
        r.metric                        AS metric,
        CASE WHEN COUNT(*) = COUNT(r.attr_country)
              AND MIN(r.attr_country) = MAX(r.attr_country)
             THEN MIN(r.attr_country) END                       AS attr_country,
        CASE WHEN COUNT(*) = COUNT(r.attr_market)
              AND MIN(r.attr_market) = MAX(r.attr_market)
             THEN MIN(r.attr_market) END                        AS attr_market,
        CASE WHEN COUNT(*) = COUNT(r.attr_source_type)
              AND MIN(r.attr_source_type) = MAX(r.attr_source_type)
             THEN MIN(r.attr_source_type) END                   AS attr_source_type,
        -- Provenance only, never branched on: MIN() is a deterministic sample of the
        -- group's reasons, not a claim that they were unanimous.
        MIN(r.resolution_source)                                AS resolution_source,
        MIN(r.country_gap_reason)                               AS country_gap_reason,
        MIN(r.market_gap_reason)                                AS market_gap_reason,
        MIN(r.source_type_gap_reason)                           AS source_type_gap_reason,
        MIN(r.gap_code)                                         AS bridge_gap_code
    FROM {{ ref('fee_tax_country_resolution') }} r
    GROUP BY r.project_id, r.date, r.connector, r.metric
),

row_attributes AS (
    SELECT
        x.row_key                       AS row_key,
        x.connector                     AS attr_connector,
        b.attr_country                  AS attr_country,
        b.attr_market                   AS attr_market,
        b.attr_source_type              AS attr_source_type,
        CAST(NULL AS STRING)            AS attr_placement_type,   -- structurally absent
        CAST(NULL AS STRING)            AS attr_tax_code,         -- no emitter, no column
        b.resolution_source             AS resolution_source,
        b.country_gap_reason            AS country_gap_reason,
        b.market_gap_reason             AS market_gap_reason,
        b.source_type_gap_reason        AS source_type_gap_reason,
        b.bridge_gap_code               AS bridge_gap_code        -- provenance ONLY
    FROM revenue_rows x
    LEFT JOIN bridge_rolled b
        ON  b.project_id = x.project_id
        AND b.date       = x.date
        AND b.connector  = x.connector
        AND b.metric     = x.metric
),

-- ------------------------------------------------------------- RULE SIDE -----
-- The exact mirror of the ladder's line-422 skip, in the other direction: this model
-- admits ONLY the revenue categories over the revenue base targets. Everything else is
-- skipped WITH NO GAP, because such a rule is not addressed to this model at all.
rules_revenue AS (
    SELECT
        rule_id, project_id, scope_kind, scope_ref, category, form, rate,
        amount_micros, rule_currency, base_target, cascade_phase, sequence_order,
        effective_from, effective_to, scope_precedence, scope_connector, scope_state
    FROM {{ ref('fee_tax_rules_effective') }}
    WHERE category    IN ('SALES_TAX', 'PAYMENT_FEE')
      AND base_target IN ('NET_REVENUE', 'GROSS_REVENUE')
),

candidate_pairs AS (
    SELECT
        x.row_key                       AS row_key,
        x.project_id                    AS project_id,
        x.currency                      AS currency,
        r.rule_id                       AS rule_id,
        r.category                      AS category,
        r.form                          AS form,
        r.rate                          AS rate,
        r.amount_micros                 AS amount_micros,
        r.rule_currency                 AS rule_currency,
        r.cascade_phase                 AS cascade_phase,
        r.sequence_order                AS sequence_order,
        r.scope_precedence              AS scope_precedence,
        r.effective_from                AS effective_from,
        CASE
            WHEN r.scope_state = 'UNRESOLVED' THEN 'SCOPE_UNRESOLVED'
            WHEN r.scope_state = 'AMBIGUOUS'  THEN 'SCOPE_AMBIGUOUS'
            ELSE 'SCOPE_APPLIES'
        END                             AS scope_verdict
    FROM revenue_rows x
    JOIN rules_revenue r
        ON  r.project_id = x.project_id
        AND x.date >= r.effective_from
        AND (r.effective_to IS NULL OR x.date <= r.effective_to)
        AND (
                r.scope_kind = 'project'
             OR (r.scope_kind = 'datastream' AND r.scope_state = 'UNRESOLVED')
             OR (r.scope_kind = 'datastream' AND r.scope_connector = x.connector)
                -- A plan_version-scoped rule cannot reach a commerce revenue row: a
                -- media plan maps CAMPAIGN spend, and there is no campaign dimension on
                -- the revenue side at all. Such a rule is simply not a candidate here.
            )
),

-- The shared three-valued matcher (MATCH / NO_MATCH / UNRESOLVED, UNRESOLVED winning),
-- called rather than copied: 41.4 extracted it precisely so 41.5 would not become its
-- third copy. conditions_relation is passed FROM HERE so this model keeps its own
-- explicit, greppable depends_on edge on mirror.fee_tax_rule_conditions.
{{ fee_tax_condition_matcher('row_attributes', 'candidate_pairs', source('mirror', 'fee_tax_rule_conditions')) }}

evaluated AS (
    SELECT
        p.row_key, p.project_id, p.currency, p.rule_id, p.category, p.form, p.rate,
        p.amount_micros, p.rule_currency, p.cascade_phase, p.sequence_order,
        p.scope_precedence, p.effective_from, p.scope_verdict,
        -- COALESCE(..., 0) IS the empty-conditions case: unconstrained == MATCH. An
        -- INNER JOIN here would silently delete every unconditional rule.
        COALESCE(rs.state_rank, 0)      AS state_rank,
        CASE
            WHEN p.scope_verdict = 'SCOPE_UNRESOLVED' THEN 'SCOPE_UNRESOLVED'
            WHEN p.scope_verdict = 'SCOPE_AMBIGUOUS'  THEN 'SCOPE_AMBIGUOUS'
            WHEN COALESCE(rs.state_rank, 0) = 2       THEN 'UNRESOLVED'
            WHEN COALESCE(rs.state_rank, 0) = 1       THEN 'NO_MATCH'
            ELSE 'MATCH'
        END                             AS verdict
    FROM candidate_pairs p
    LEFT JOIN rule_state rs
        ON  rs.row_key = p.row_key
        AND rs.rule_id = p.rule_id
),

-- ---------------------------------------------------- THE VAT RULE: EXACTLY ONE
-- A DELIBERATE DIVERGENCE from the ladder's slot idiom, and the reason matters. On the
-- cost side a phase can legitimately host several distinct fees that all fire and sum.
-- On the revenue side a SECOND VAT RATE ON THE SAME ROW IS NOT A SECOND FEE, IT IS A
-- CONTRADICTION: summing two declared rates would produce a rate nobody declared. So
-- exactly ONE SALES_TAX rule wins per row, by the same deterministic precedence the
-- ladder uses (scope precedence, then the most recent window, then the id).
vat_ranked AS (
    SELECT
        e.*,
        ROW_NUMBER() OVER (
            PARTITION BY e.row_key
            ORDER BY e.scope_precedence DESC, e.effective_from DESC, e.rule_id ASC
        ) AS _rn
    FROM evaluated e
    WHERE e.verdict = 'MATCH'
      AND e.category = 'SALES_TAX'
),

vat_pick AS (
    SELECT
        v.row_key, v.rule_id, v.form, v.rate,
        -- Positive routability (F2): the revenue side executes a SALES_TAX rule as a
        -- PERCENTAGE and in no other shape. A FLAT sales tax on a day-total revenue row
        -- has no evaluator, so it is REFUSED and typed, never silently worth 0.
        CASE WHEN v.form = 'PERCENTAGE' THEN 0 ELSE 1 END       AS form_unsupported,
        CASE
            WHEN v.form = 'PERCENTAGE' AND (v.rate IS NULL OR v.rate < 0) THEN 1
            ELSE 0
        END                                                     AS rate_invalid
    FROM vat_ranked v
    WHERE v._rn = 1
),

-- -------------------------------------------------- THE PAYMENT FEE RULES ----
-- Several MAY fire and they SUM: "Stripe 1.4 % + 0.25 EUR" is one PERCENTAGE rule and
-- one PER_TRANSACTION rule. The ladder's slot idiom (category, cascade_phase,
-- sequence_order) is what lets two distinct fees coexist while still de-duplicating an
-- overridden rule.
fee_ranked AS (
    SELECT
        e.*,
        ROW_NUMBER() OVER (
            PARTITION BY e.row_key, e.category, e.cascade_phase, e.sequence_order
            ORDER BY e.scope_precedence DESC, e.effective_from DESC, e.rule_id ASC
        ) AS _rn
    FROM evaluated e
    WHERE e.verdict = 'MATCH'
      AND e.category = 'PAYMENT_FEE'
),

fee_matched AS (
    SELECT
        f.row_key, f.rule_id, f.form, f.rate, f.amount_micros, f.rule_currency,
        f.currency,
        CASE WHEN f.form IN ('PERCENTAGE', 'FLAT', 'PER_TRANSACTION') THEN 0 ELSE 1 END
                                                                AS form_unsupported,
        -- A rule that declares no currency is treated as declaring the row's own, which
        -- is the only reading that cannot FABRICATE a mismatch. IS DISTINCT FROM is
        -- avoided for portability.
        CASE
            WHEN f.form IN ('FLAT', 'PER_TRANSACTION')
             AND COALESCE(f.rule_currency, f.currency) <> f.currency THEN 1
            ELSE 0
        END                                                     AS currency_mismatch
    FROM fee_ranked f
    WHERE f._rn = 1
),

-- ------------------------------------------------------------- VAT MATHS -----
vat_calc AS (
    SELECT
        r.row_key                                               AS row_key,
        v.rule_id                                               AS vat_rule_id,
        v.rate                                                  AS sales_tax_rate,
        -- TAX_INCLUSIVE: the landed figure IS the gross. ONE rounding, on the derived
        -- NET total; the tax is then EXACT INTEGER SUBTRACTION, so net + tax == gross
        -- to the micro, always.
        CASE
            WHEN r.landed_basis = 'TTC' THEN r.landed_revenue_micros
            WHEN r.landed_basis = 'HT'  THEN
                CASE
                    WHEN v.rule_id IS NULL OR v.form_unsupported = 1 OR v.rate_invalid = 1
                        THEN NULL
                    ELSE r.landed_revenue_micros
                         + ({{ fee_tax_pct_of_micros('r.landed_revenue_micros', 'v.rate') }})
                END
            ELSE NULL
        END                                                     AS gross_revenue_ttc_micros,
        -- TAX_EXCLUSIVE: the landed figure IS the net. ONE rounding, on the COMPONENT
        -- this time; the gross is then exact by construction. The asymmetry is
        -- deliberate: round the DERIVED quantity once, obtain the other side exactly.
        CASE
            WHEN r.landed_basis = 'HT' THEN r.landed_revenue_micros
            WHEN r.landed_basis = 'TTC' THEN
                CASE
                    WHEN v.rule_id IS NULL OR v.form_unsupported = 1 OR v.rate_invalid = 1
                        THEN NULL
                    ELSE {{ fee_tax_vat_net_of_micros('r.landed_revenue_micros', 'v.rate') }}
                END
            ELSE NULL
        END                                                     AS net_revenue_ht_micros,
        CASE WHEN v.rule_id IS NULL     THEN 0 ELSE 1 END       AS vat_rule_present,
        COALESCE(v.form_unsupported, 0)                         AS vat_form_unsupported,
        COALESCE(v.rate_invalid, 0)                             AS vat_rate_invalid
    FROM revenue_rows r
    LEFT JOIN vat_pick v ON v.row_key = r.row_key
),

-- --------------------------------------------------------- PAYMENT FEE MATHS -
-- Precedence: a MEASURED fee wins outright and SUPPRESSES every declared rule for the
-- row (applying both would double-count the same fee). Otherwise the declared
-- components sum, and if ANY of them is NULL the whole fee is NULL -- you cannot
-- report a partial fee as a complete one.
fee_components AS (
    SELECT
        fm.row_key                                              AS row_key,
        fm.rule_id                                              AS rule_id,
        CASE
            WHEN fm.form_unsupported = 1 OR fm.currency_mismatch = 1 THEN NULL
            WHEN fm.form = 'PERCENTAGE' THEN
                CASE
                    WHEN v.gross_revenue_ttc_micros IS NULL THEN NULL
                    ELSE {{ fee_tax_pct_of_micros('v.gross_revenue_ttc_micros', 'fm.rate') }}
                END
            -- FLAT means THIS AMOUNT, ONCE FOR THE ROW, unchanged from the cost side's
            -- meaning. The grain here IS the connector-day, so "once for the row" is
            -- once per day and no allocation is needed (the ladder allocates only
            -- because its grain is finer than the day).
            WHEN fm.form = 'FLAT' THEN fm.amount_micros
            -- PER_TRANSACTION: an EXACT INTEGER MULTIPLICATION with NO ROUNDING AT ALL
            -- (the amount is already a whole micro value). NEVER assume a count of 1 and
            -- NEVER fall back to treating it as a day-level flat: a per-transaction fee
            -- applied once to a day of 4 000 orders is off by 4 000x, and SILENTLY.
            WHEN fm.form = 'PER_TRANSACTION' THEN
                CASE
                    WHEN c.transaction_count IS NULL THEN NULL
                    ELSE fm.amount_micros * c.transaction_count
                END
            ELSE NULL
        END                                                     AS component_micros,
        CASE
            WHEN fm.form = 'PER_TRANSACTION' AND c.transaction_count IS NULL THEN 1
            ELSE 0
        END                                                     AS count_unavailable,
        fm.form_unsupported                                     AS form_unsupported,
        fm.currency_mismatch                                    AS currency_mismatch
    FROM fee_matched fm
    JOIN revenue_rows r ON r.row_key = fm.row_key
    LEFT JOIN vat_calc v ON v.row_key = fm.row_key
    LEFT JOIN counts c
        ON  c.project_id = r.project_id
        AND c.date       = r.date
        AND c.connector  = r.connector
        AND c.metric     = r.transaction_count_metric
),

fee_rolled AS (
    SELECT
        row_key,
        COUNT(*)                                                AS rule_count,
        -- NULL-propagating on purpose: SUM() skips NULLs, so compare the counts.
        CASE WHEN COUNT(*) = COUNT(component_micros)
             THEN SUM(component_micros) ELSE NULL END           AS derived_fee_micros,
        MAX(count_unavailable)                                  AS count_unavailable,
        MAX(form_unsupported)                                   AS fee_form_unsupported,
        MAX(currency_mismatch)                                  AS fee_currency_mismatch
    FROM fee_components
    GROUP BY row_key
),

payment_fee AS (
    SELECT
        r.row_key                                               AS row_key,
        mf.measured_fee_micros                                  AS measured_fee_micros,
        CASE
            WHEN mf.measured_fee_micros IS NOT NULL THEN mf.measured_fee_micros
            ELSE fr.derived_fee_micros
        END                                                     AS payment_fee_micros,
        CASE
            WHEN mf.measured_fee_micros IS NOT NULL          THEN 'measured_gateway_fee'
            WHEN COALESCE(fr.rule_count, 0) > 0              THEN 'derived_rule'
            ELSE 'none'
        END                                                     AS payment_fee_provenance,
        CASE
            WHEN mf.measured_fee_micros IS NOT NULL
             AND COALESCE(fr.rule_count, 0) > 0 THEN 1 ELSE 0
        END                                                     AS rule_superseded,
        -- A refusal raised by a SUPPRESSED rule is not a gap: the measurement already
        -- answered the question the rule was trying to answer.
        CASE WHEN mf.measured_fee_micros IS NOT NULL THEN 0
             ELSE COALESCE(fr.count_unavailable, 0) END         AS count_unavailable,
        CASE WHEN mf.measured_fee_micros IS NOT NULL THEN 0
             ELSE COALESCE(fr.fee_form_unsupported, 0) END      AS fee_form_unsupported,
        CASE WHEN mf.measured_fee_micros IS NOT NULL THEN 0
             ELSE COALESCE(fr.fee_currency_mismatch, 0) END     AS fee_currency_mismatch
    FROM revenue_rows r
    LEFT JOIN fee_rolled fr ON fr.row_key = r.row_key
    LEFT JOIN measured_fees mf
        ON  mf.project_id = r.project_id
        AND mf.date       = r.date
        AND mf.connector  = r.connector
        AND mf.metric     = r.measured_fee_metric
),

-- --------------------------------------------------------- AD-9 PROVENANCE ---
-- Only rules that ACTUALLY CONTRIBUTED are listed. A suppressed PAYMENT_FEE rule and a
-- refused form are deliberately absent: provenance that contradicts the number beside
-- it is worse than no provenance.
applied AS (
    SELECT row_key, rule_id FROM vat_pick
    WHERE form_unsupported = 0 AND rate_invalid = 0
    UNION ALL
    SELECT fc.row_key, fc.rule_id
    FROM fee_components fc
    JOIN payment_fee pf ON pf.row_key = fc.row_key
    WHERE fc.form_unsupported = 0
      AND fc.currency_mismatch = 0
      AND pf.payment_fee_provenance = 'derived_rule'
),

applied_rolled AS (
    SELECT
        row_key,
        STRING_AGG(rule_id, '|' ORDER BY rule_id)               AS applied_rule_ids,
        COUNT(*)                                                AS applied_rule_count
    FROM applied
    GROUP BY row_key
),

-- ------------------------------------------------------------ GAP LEDGER -----
-- A gap is raised ONLY when a confirmed, in-window rule ACTUALLY CONSTRAINS the
-- unresolvable attribute (or is itself unexecutable). A project whose revenue rules
-- constrain nothing composes cleanly with no gap at all.
all_gaps AS (
    SELECT row_key, 'DATASTREAM_SCOPE_AMBIGUOUS' AS gap_code
    FROM evaluated WHERE verdict = 'SCOPE_AMBIGUOUS'
    UNION ALL
    SELECT row_key, 'RULE_SCOPE_UNRESOLVED' AS gap_code
    FROM evaluated WHERE verdict = 'SCOPE_UNRESOLVED'
    UNION ALL
    SELECT e.row_key, cg.gap_code
    FROM evaluated e
    JOIN cond_gap cg
        ON  cg.row_key = e.row_key
        AND cg.rule_id = e.rule_id
    WHERE e.verdict = 'UNRESOLVED'
      AND cg.gap_code IS NOT NULL
    UNION ALL
    SELECT row_key, 'REVENUE_FORM_UNSUPPORTED' AS gap_code
    FROM vat_pick WHERE form_unsupported = 1
    UNION ALL
    SELECT row_key, 'VAT_RATE_INVALID' AS gap_code
    FROM vat_pick WHERE rate_invalid = 1
    UNION ALL
    SELECT row_key, 'REVENUE_FORM_UNSUPPORTED' AS gap_code
    FROM payment_fee WHERE fee_form_unsupported = 1
    UNION ALL
    SELECT row_key, 'CURRENCY_MISMATCH' AS gap_code
    FROM payment_fee WHERE fee_currency_mismatch = 1
    UNION ALL
    SELECT row_key, 'TRANSACTION_COUNT_UNAVAILABLE' AS gap_code
    FROM payment_fee WHERE count_unavailable = 1
    UNION ALL
    -- The day-one hot path, and the reason a surface can say WHICH rule to confirm.
    SELECT v.row_key, 'SALES_TAX_RULE_ABSENT' AS gap_code
    FROM vat_calc v
    JOIN revenue_rows r ON r.row_key = v.row_key
    WHERE v.vat_rule_present = 0
      AND r.landed_basis <> 'UNRESOLVED'
    UNION ALL
    -- We cannot even say whether the landed figure is TTC, so BOTH derived columns are
    -- NULL and the row can never reach the alignment view.
    SELECT row_key, 'REVENUE_TAX_POSTURE_UNDECLARED' AS gap_code
    FROM revenue_rows WHERE landed_basis = 'UNRESOLVED'
),

row_gaps AS (
    SELECT
        row_key,
        MAX(CASE WHEN gap_code = 'CONDITION_KEY_UNKNOWN'          THEN 1 ELSE 0 END) AS g_key_unknown,
        MAX(CASE WHEN gap_code = 'COUNTRY_UNRESOLVED'             THEN 1 ELSE 0 END) AS g_country,
        MAX(CASE WHEN gap_code = 'CURRENCY_MISMATCH'              THEN 1 ELSE 0 END) AS g_currency,
        MAX(CASE WHEN gap_code = 'DATASTREAM_SCOPE_AMBIGUOUS'     THEN 1 ELSE 0 END) AS g_ds_ambiguous,
        MAX(CASE WHEN gap_code = 'MARKET_UNRESOLVED'              THEN 1 ELSE 0 END) AS g_market,
        MAX(CASE WHEN gap_code = 'PLACEMENT_TYPE_UNRESOLVED'      THEN 1 ELSE 0 END) AS g_placement,
        MAX(CASE WHEN gap_code = 'REVENUE_FORM_UNSUPPORTED'       THEN 1 ELSE 0 END) AS g_form,
        MAX(CASE WHEN gap_code = 'REVENUE_TAX_POSTURE_UNDECLARED' THEN 1 ELSE 0 END) AS g_posture,
        MAX(CASE WHEN gap_code = 'RULE_SCOPE_UNRESOLVED'          THEN 1 ELSE 0 END) AS g_scope,
        MAX(CASE WHEN gap_code = 'SALES_TAX_RULE_ABSENT'          THEN 1 ELSE 0 END) AS g_vat_absent,
        MAX(CASE WHEN gap_code = 'SOURCE_TYPE_UNRESOLVED'         THEN 1 ELSE 0 END) AS g_source_type,
        MAX(CASE WHEN gap_code = 'TAX_CODE_UNRESOLVED'            THEN 1 ELSE 0 END) AS g_tax_code,
        MAX(CASE WHEN gap_code = 'TRANSACTION_COUNT_UNAVAILABLE'  THEN 1 ELSE 0 END) AS g_count,
        MAX(CASE WHEN gap_code = 'VAT_RATE_INVALID'               THEN 1 ELSE 0 END) AS g_vat_rate
    FROM all_gaps
    GROUP BY row_key
),

assembled AS (
    SELECT
        r.row_key,
        r.project_id,
        r.date,
        r.connector,
        r.metric,
        r.currency,
        r.revenue_role,
        r.landed_tax_posture,
        r.landed_revenue_micros,
        r.landed_basis,
        r.fx_rate,
        r.fx_as_of_date,
        r.fx_source,
        r.fx_tier,
        r.pull_id,
        v.gross_revenue_ttc_micros,
        v.net_revenue_ht_micros,
        v.sales_tax_rate,
        v.vat_rule_present,
        pf.payment_fee_micros,
        pf.payment_fee_provenance,
        pf.rule_superseded,
        c.transaction_count,
        COALESCE(a.applied_rule_ids, '')                        AS applied_rule_ids,
        COALESCE(a.applied_rule_count, 0)                       AS applied_rule_count,
        COALESCE(g.g_key_unknown, 0)  AS g_key_unknown,
        COALESCE(g.g_country, 0)      AS g_country,
        COALESCE(g.g_currency, 0)     AS g_currency,
        COALESCE(g.g_ds_ambiguous, 0) AS g_ds_ambiguous,
        COALESCE(g.g_market, 0)       AS g_market,
        COALESCE(g.g_placement, 0)    AS g_placement,
        COALESCE(g.g_form, 0)         AS g_form,
        COALESCE(g.g_posture, 0)      AS g_posture,
        COALESCE(g.g_scope, 0)        AS g_scope,
        COALESCE(g.g_vat_absent, 0)   AS g_vat_absent,
        COALESCE(g.g_source_type, 0)  AS g_source_type,
        COALESCE(g.g_tax_code, 0)     AS g_tax_code,
        COALESCE(g.g_count, 0)        AS g_count,
        COALESCE(g.g_vat_rate, 0)     AS g_vat_rate,
        ra.attr_country,
        ra.attr_market,
        ra.attr_source_type,
        ra.resolution_source,
        ra.country_gap_reason,
        ra.market_gap_reason,
        ra.source_type_gap_reason,
        ra.bridge_gap_code
    FROM revenue_rows r
    LEFT JOIN vat_calc       v  ON v.row_key  = r.row_key
    LEFT JOIN payment_fee    pf ON pf.row_key = r.row_key
    LEFT JOIN applied_rolled a  ON a.row_key  = r.row_key
    LEFT JOIN row_gaps       g  ON g.row_key  = r.row_key
    LEFT JOIN row_attributes ra ON ra.row_key = r.row_key
    LEFT JOIN counts c
        ON  c.project_id = r.project_id
        AND c.date       = r.date
        AND c.connector  = r.connector
        AND c.metric     = r.transaction_count_metric
),

coded AS (
    SELECT
        s.*,
        -- '|'-joined, SORTED, distinct gap codes; '' when none. The CASE chain is
        -- written in ALPHABETICAL ORDER, so the result is sorted BY CONSTRUCTION -- no
        -- aggregate ordering to trust across engines.
        CASE WHEN s.g_key_unknown  = 1 THEN '|CONDITION_KEY_UNKNOWN'          ELSE '' END
     || CASE WHEN s.g_country      = 1 THEN '|COUNTRY_UNRESOLVED'             ELSE '' END
     || CASE WHEN s.g_currency     = 1 THEN '|CURRENCY_MISMATCH'              ELSE '' END
     || CASE WHEN s.g_ds_ambiguous = 1 THEN '|DATASTREAM_SCOPE_AMBIGUOUS'     ELSE '' END
     || CASE WHEN s.g_market       = 1 THEN '|MARKET_UNRESOLVED'              ELSE '' END
     || CASE WHEN s.g_placement    = 1 THEN '|PLACEMENT_TYPE_UNRESOLVED'      ELSE '' END
     || CASE WHEN s.g_form         = 1 THEN '|REVENUE_FORM_UNSUPPORTED'       ELSE '' END
     || CASE WHEN s.g_posture      = 1 THEN '|REVENUE_TAX_POSTURE_UNDECLARED' ELSE '' END
     || CASE WHEN s.g_scope        = 1 THEN '|RULE_SCOPE_UNRESOLVED'          ELSE '' END
     || CASE WHEN s.g_vat_absent   = 1 THEN '|SALES_TAX_RULE_ABSENT'          ELSE '' END
     || CASE WHEN s.g_source_type  = 1 THEN '|SOURCE_TYPE_UNRESOLVED'         ELSE '' END
     || CASE WHEN s.g_tax_code     = 1 THEN '|TAX_CODE_UNRESOLVED'            ELSE '' END
     || CASE WHEN s.g_count        = 1 THEN '|TRANSACTION_COUNT_UNAVAILABLE'  ELSE '' END
     || CASE WHEN s.g_vat_rate     = 1 THEN '|VAT_RATE_INVALID'               ELSE '' END
            AS gap_codes_prefixed
    FROM assembled s
)

SELECT
    c.project_id,
    c.date,
    c.connector,
    c.metric,
    c.currency,
    c.revenue_role,
    c.landed_tax_posture,

    -- A READ of a real fact: NEVER NULL, never derived. The twin of net_media_micros.
    c.landed_revenue_micros,
    c.landed_basis,

    -- The two views of the same money. NULL when the derivation was impossible.
    c.gross_revenue_ttc_micros,
    c.net_revenue_ht_micros,
    -- EXACT INTEGER SUBTRACTION in both directions, which is what makes
    -- net + tax == gross hold TO THE MICRO and lets the identity test assert a
    -- tolerance of exactly ZERO. NULL iff either view is NULL.
    CASE
        WHEN c.gross_revenue_ttc_micros IS NULL OR c.net_revenue_ht_micros IS NULL
            THEN NULL
        ELSE c.gross_revenue_ttc_micros - c.net_revenue_ht_micros
    END                                                             AS sales_tax_micros,
    c.sales_tax_rate,
    CASE
        WHEN c.gross_revenue_ttc_micros IS NULL OR c.net_revenue_ht_micros IS NULL
            THEN 'none'
        -- 'measured_order_tax' is DEFINED AND RESERVED and is NEVER emitted today: no
        -- connector lands a per-order tax (see the header). The day one does, that
        -- branch activates HERE and the measurement supersedes the derivation.
        ELSE 'derived_declared_rate'
    END                                                             AS sales_tax_provenance,

    -- A KEEP_SEPARATE overlay: NEVER deducted from either revenue column. NULL with
    -- provenance 'none' means nothing was declared and nothing was measured -- which is
    -- not the same claim as "the fee is zero", and this model does not make claims it
    -- cannot support.
    c.payment_fee_micros,
    c.payment_fee_provenance,
    c.rule_superseded = 1                                           AS payment_fee_rule_superseded,
    c.transaction_count,
    CASE
        WHEN c.gross_revenue_ttc_micros IS NULL OR c.payment_fee_micros IS NULL
            THEN NULL
        ELSE c.gross_revenue_ttc_micros - c.payment_fee_micros
    END                                                             AS net_cash_received_ttc_micros,

    -- AD-9 provenance.
    c.applied_rule_ids,
    c.applied_rule_count,

    -- FX carried, NEVER re-applied (Epic 39.10) + pull provenance (AD-7).
    c.fx_rate,
    c.fx_as_of_date,
    c.fx_source,
    c.fx_tier,
    c.pull_id,

    -- Typed gaps. NOT copies of the bridge's gap_code.
    c.g_country      = 1 AS gap_country_unresolved,
    c.g_market       = 1 AS gap_market_unresolved,
    c.g_placement    = 1 AS gap_placement_type_unresolved,
    c.g_tax_code     = 1 AS gap_tax_code_unresolved,
    c.g_source_type  = 1 AS gap_source_type_unresolved,
    c.g_key_unknown  = 1 AS gap_condition_key_unknown,
    c.g_currency     = 1 AS gap_currency_mismatch,
    c.g_ds_ambiguous = 1 AS gap_datastream_scope_ambiguous,
    c.g_scope        = 1 AS gap_rule_scope_unresolved,
    c.g_vat_absent   = 1 AS gap_sales_tax_rule_absent,
    c.g_vat_rate     = 1 AS gap_vat_rate_invalid,
    c.g_posture      = 1 AS gap_revenue_tax_posture_undeclared,
    c.g_form         = 1 AS gap_revenue_form_unsupported,
    c.g_count        = 1 AS gap_transaction_count_unavailable,
    CASE WHEN c.gap_codes_prefixed = '' THEN '' ELSE SUBSTR(c.gap_codes_prefixed, 2) END AS gap_codes,
    c.gap_codes_prefixed = ''                                       AS is_normalization_complete,

    -- 41.2's attributes and its own provenance, carried for drill-down. NEVER branched
    -- on, and bridge_gap_code NEVER drives is_normalization_complete.
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
