-- fee_tax_revenue_alignment_daily: HT revenue against HT cost, TTC revenue against TTC
-- cost -- ROAS HT, gross margin HT and a cash-cover TTC ratio at the ONE grain where
-- each is legitimate (Epic 41, Story 41.5 / E41-FR07 / E41-AD7).
--
-- ============================ THIS MART IS A READ ============================
-- It READS fee_tax_revenue_normalized_daily, fee_tax_ladder_rollup and the seed
-- metric_source_priority. It creates NO new fact row and edits NO existing model. It
-- does not even read fact_daily_kpi. With the module OFF both inputs are empty and this
-- view returns ZERO ROWS (E41-NFR01, by construction).
--
-- GRAIN (enforced by fee_tax_revenue_alignment_daily_grain_unique):
--   one row per (project_id, date, tax_basis, currency). AT MOST TWO ROWS PER
--   PROJECT-DAY-CURRENCY: one HT, one TTC.
--
-- ############################################################################
-- ##  TAX HOMOGENEITY IS STRUCTURAL, NOT DOCUMENTARY (E41-AD7)              ##
-- ############################################################################
-- Three layers, in increasing strength:
--   1. NO COLUMN IS CALLED `revenue` OR `cost` WITHOUT ITS BASIS. Upstream every money
--      column names its basis (gross_revenue_ttc_micros, net_revenue_ht_micros,
--      subtotal_ht_micros, total_ttc_micros). A basis-free money column cannot be
--      selected because none exists.
--   2. `tax_basis` IS A DISCRIMINATOR COLUMN on every row here.
--   3. THE BASIS IS THE JOIN KEY. Both sides are unpivoted into two basis rows and
--      joined ON project_id AND date AND currency AND TAX_BASIS. A MIXED PAIR HAS NO ROW
--      TO LIVE IN -- it is not filtered out, it is UNCONSTRUCTIBLE. And a normalized row
--      whose landed_basis is 'UNRESOLVED' produces NO unpivoted row at all, so it can
--      never join anything. That join IS AC2, and accepted_values on tax_basis is
--      ['HT','TTC'] with 'UNRESOLVED' deliberately absent.
--
-- ############################################################################
-- ##  THE GRAIN MISMATCH, SOLVED HONESTLY (AC4)                             ##
-- ############################################################################
-- The cost ladder's grain is (project, date, connector, breakdown_dimension,
-- breakdown_value) -- CAMPAIGN-level for meta / tiktok / linkedin / cm360. Commerce
-- revenue's grain is (project, date): WHOLE-BUSINESS, with NO CAMPAIGN DIMENSION IN THE
-- SOURCE DATA AT ALL. THERE IS NO HONEST FUNCTION FROM A DAY-TOTAL COMMERCE FIGURE TO A
-- CAMPAIGN. Any per-campaign split would be a FABRICATED ATTRIBUTION, which E41-NFR02
-- forbids -- and it is exactly the failure semantic_roas.sql already demonstrates: it
-- computes revenue / cost at a grain where revenue and cost NEVER CO-OCCUR, and its own
-- header concedes "this view returns NULL for all roas values". This model does not
-- repeat that, and does not fix it either (additive discipline: semantic_roas is
-- untouched, and this is its tax-homogeneous, grain-honest sibling).
--
-- THE SOLUTION IS TO COLLAPSE THE COST SIDE UP TO THE REVENUE SIDE'S GRAIN, NEVER THE
-- REVERSE. NO `connector`, NO `breakdown_dimension` and NO `breakdown_value` COLUMN
-- EXISTS ON THIS MODEL: AC4 is enforced by THE ABSENCE OF THE COLUMNS, not by a filter
-- someone can relax later.
--
--   grain                                     verdict
--   ---------------------------------------   ------------------------------------
--   (project, date)                           LEGITIMATE -- the only row emitted.
--   (project, date, connector) vs a day total REFUSED: the revenue is not
--                                             attributable to a connector.
--   (project, date, campaign)                 REFUSED: no campaign dimension exists on
--                                             the revenue side.
--   (project, period)                         NOT EMITTED. A consumer may sum the
--                                             NUMERATOR AND DENOMINATOR MICROS COLUMNS
--                                             across days and divide ONCE.
--                                             revenue_cost_ratio ITSELF MUST NEVER BE
--                                             AVERAGED -- it is a ratio, and AD-4's
--                                             reasoning applies to consumers too.
--
-- ############################################################################
-- ##  AN INCOMPLETE COST LADDER MAKES THE RATIO NULL. THIS IS THE POINT.    ##
-- ############################################################################
-- total_cost_ht_micros / total_invoice_ttc_micros are NULL whenever the rollup reports
-- is_ladder_complete = FALSE. The rollup already NULLs them and this model does NOT
-- substitute total_ttc_micros_complete_only. SUMMING ONLY THE COMPLETE ROWS WOULD
-- UNDERSTATE COST AND THEREFORE OVERSTATE ROAS -- a silent, flattering, wrong number,
-- which is the single most dangerous thing a performance metric can be. What a surface
-- shows instead is carried right here: cost_row_count / cost_complete_row_count let it
-- say "ROAS unavailable -- 8 of 11 cost rows composed".
--
-- ############################################################################
-- ##  ROAS HT AND CASH-COVER TTC ANSWER DIFFERENT QUESTIONS                 ##
-- ############################################################################
-- Both are revenue over cost, and naming the TTC one `roas` would invite a reader to
-- treat a CASH ratio as PERFORMANCE. So ratio_kind says which is which:
--   * 'roas_ht'        -- performance and margin. Net Revenue HT over Total Cost HT:
--                         the two figures a marketer's return is actually made of.
--   * 'cash_cover_ttc' -- treasury. Money IN including the customer's VAT over money OUT
--                         including the agency's VAT: TWO DIFFERENT TAXES ON TWO
--                         DIFFERENT FLOWS, which is why it is not a performance figure.
-- gross_margin_micros exists on the HT ROW ONLY and is NULL on the TTC row: a "margin"
-- computed from two different tax flows is not a margin.
--
-- HONESTY NOTE, carried deliberately: SAME-DAY REVENUE AGAINST SAME-DAY SPEND IS A
-- CONVENTION, NOT A CAUSAL ATTRIBUTION. Conversion lag means a day's sales are not that
-- day's spend's return. This model reports the convention EXACTLY and does not model
-- lag; ratio_kind and the day grain make that legible rather than hidden.
--
-- ############################################################################
-- ##  WHY A RATIO IS ALLOWED HERE AT ALL (AD-4)                             ##
-- ############################################################################
-- AD-4 forbids ratios as STORED ADDITIVE FACTS in fact_daily_kpi, because a stored ratio
-- double-counts or mis-averages at every downstream SUM;
-- dbt/tests/test_projection_additive_only.sql enforces it by returning any
-- fact_daily_kpi row whose metric is one of average_position / roas / ctr / cpa / cvr /
-- unique_reach / average_frequency. THIS STORY WRITES NO FACT ROW AT ALL, so that test
-- stays green UNTOUCHED. A ratio computed in a READ VIEW, sum-then-divide, at a declared
-- grain, is the sanctioned pattern and has a precedent in the tree (semantic_roas.sql:
-- "Ratio metrics are NOT stored in fact_daily_kpi (AD-4 enforcement) ... materialized as
-- a VIEW, never as a table (HG-3)"). The micros cancel in the division, so the ratio is
-- unit-free and needs no 1e6 step -- unlike semantic_roas, whose components are display
-- decimals.
--
-- ############################################################################
-- ##  ONE WINNING REVENUE SOURCE PER DAY (C.7)                              ##
-- ############################################################################
-- Summing shopify + stripe revenue DOUBLE-COUNTS THE SAME SALE: a Stripe payment that
-- settles a Shopify order measures ONE sale. This model mirrors cross_source_revenue's
-- two-step -- rank the day's SALES connectors by the declarative seed
-- metric_source_priority, keep the winner -- WITHOUT ref()ing that view (it returns a
-- float revenue_total, carries no currency and applies no tax normalisation) and without
-- modifying it. The join is p.metric = r.metric rather than a hard-coded 'revenue', so
-- an ATTRIBUTED metric that later gains a priority row works unchanged. NO CONNECTOR
-- NAME APPEARS IN THIS FILE (AD-2 / E41-NFR05).
--
-- AND THE WARNING cross_source_revenue CARRIES, REPEATED BECAUSE IT MATTERS AS MUCH
-- HERE: the winner CAN CHANGE DAY TO DAY, and the bases differ (orders CREATED vs
-- payments SETTLED). A day-over-day series that crosses a source change MIXES TWO
-- POPULATIONS. revenue_source is on every row precisely so that is visible.
--
-- ############################################################################
-- ##  EPIC 27 INVARIANT 4: CLAIMED REVENUE IS NEVER A ROAS NUMERATOR        ##
-- ############################################################################
-- Only revenue_role = 'SALES' rows enter. ATTRIBUTED metrics (attributed_revenue,
-- conversion_value, ad_revenue, all_revenue) are normalized and inspectable in
-- fee_tax_revenue_normalized_daily as an OVERLAY, and they never reach this numerator.
-- A PLAN metric (target_revenue) enters neither.

{{ config(materialized='view') }}

WITH revenue_all AS (
    SELECT * FROM {{ ref('fee_tax_revenue_normalized_daily') }}
),

sales_rows AS (
    SELECT * FROM revenue_all WHERE revenue_role = 'SALES'
),

-- Step 1 of the dedup: rank the day's SALES connectors. DISTINCT first so a connector
-- emitting two SALES metrics on one day is ranked ONCE.
source_candidates AS (
    SELECT DISTINCT project_id, date, connector, metric, currency
    FROM sales_rows
),

source_ranked AS (
    SELECT
        c.project_id, c.date, c.connector, c.currency,
        COALESCE(p.priority, 99)                                AS priority,
        ROW_NUMBER() OVER (
            PARTITION BY c.project_id, c.date
            -- An unranked connector sorts last, then alphabetically, so the pick is
            -- DETERMINISTIC even when the seed says nothing. No QUALIFY (not portable).
            ORDER BY COALESCE(p.priority, 99), c.connector
        )                                                       AS _rn
    FROM source_candidates c
    LEFT JOIN {{ ref('metric_source_priority') }} p
        ON  p.metric    = c.metric
        AND p.connector = c.connector
),

source_winner AS (
    SELECT project_id, date, connector, currency, priority
    FROM source_ranked
    WHERE _rn = 1
),

revenue_winner AS (
    SELECT
        s.project_id, s.date, s.currency,
        w.connector                                             AS revenue_source,
        w.priority                                              AS revenue_source_priority,
        -- Several metrics of ONE winning connector legitimately sum (they are different
        -- money, not the same sale twice). The completeness flag is ALL-or-nothing: a
        -- day is complete only if EVERY contributing row of the winner composed.
        CASE WHEN COUNT(*) = COUNT(s.gross_revenue_ttc_micros)
             THEN SUM(s.gross_revenue_ttc_micros) ELSE NULL END AS gross_revenue_ttc_micros,
        CASE WHEN COUNT(*) = COUNT(s.net_revenue_ht_micros)
             THEN SUM(s.net_revenue_ht_micros)    ELSE NULL END AS net_revenue_ht_micros,
        MIN(CASE WHEN s.is_normalization_complete THEN 1 ELSE 0 END) AS revenue_complete,
        -- Story 48.4: MAX, not MIN. The empty string is the SMALLEST string, so a
        -- winner with one clean row and one gapped row reported `''` -- a clean bill
        -- of health over a row that was short. MAX returns `''` only when EVERY
        -- contributing row is clean, which is the claim this column is read as. It
        -- still shows one row's string rather than the union; that is a provenance
        -- pointer, and `revenue_complete` beside it is the verdict.
        MAX(s.gap_codes)                                        AS revenue_gap_codes,
        -- Criterion [4]: an UNDECLARED tax posture must reach the surface where
        -- revenue meets spend, as ITSELF. It was visible in the normalized model and
        -- invisible here, so "we cannot say whether this figure includes tax" was
        -- indistinguishable from "this day was simply incomplete".
        MAX(CASE WHEN s.gap_revenue_tax_posture_undeclared THEN 1 ELSE 0 END)
                                                                AS revenue_posture_undeclared
    FROM sales_rows s
    JOIN source_winner w
        ON  w.project_id = s.project_id
        AND w.date       = s.date
        AND w.connector  = s.connector
        AND w.currency   = s.currency
    GROUP BY s.project_id, s.date, s.currency, w.connector, w.priority
),

-- ------------------------------------------------ THE TWO TYPED BASIS ROWS ---
-- A basis whose money column is NULL DOES still produce a row, so the day stays VISIBLE
-- with a NULL ratio and its gap codes rather than vanishing -- disappearing silently is
-- the failure this epic exists to prevent.
--
-- ⚠️ THIS COMMENT USED TO OPEN WITH "an UNRESOLVED basis produces NO ROW HERE, which is
-- what makes it structurally unable to join a cost side". THAT WAS NEVER TRUE OF THIS
-- CODE, and it contradicted the sentence right after it. `revenue_winner` aggregates
-- every SALES row of the winning connector, unresolved ones included; their money
-- columns come back NULL, and the two basis rows below are emitted anyway. Measured
-- 2026-08-04, the first day a fixture could reach the case at all (adjust's loader).
--
-- The code is the RIGHT behaviour and the comment was the wrong intent. Suppressing the
-- row would report "this project has no revenue" for a project that has revenue whose
-- tax basis nobody stated -- the silence completeness criterion [4] forbids. What the
-- row carries instead: NULL money, NULL ratio, `is_revenue_posture_undeclared` and
-- REVENUE_TAX_POSTURE_UNDECLARED. Refused, visible, and explained.
revenue_by_basis AS (
    SELECT
        project_id, date, 'HT' AS tax_basis, currency, revenue_source,
        revenue_source_priority,
        net_revenue_ht_micros AS revenue_micros,
        revenue_complete, revenue_gap_codes, revenue_posture_undeclared
    FROM revenue_winner
    UNION ALL
    SELECT
        project_id, date, 'TTC' AS tax_basis, currency, revenue_source,
        revenue_source_priority,
        gross_revenue_ttc_micros AS revenue_micros,
        revenue_complete, revenue_gap_codes, revenue_posture_undeclared
    FROM revenue_winner
),

-- The cost side is the rollup's GLOBAL row, which is already SUM() over every ladder row
-- of the project-day and already carries the ladder's completeness rules. Reusing it
-- means ZERO DUPLICATION OF THE CASCADE ARITHMETIC.
cost_global AS (
    SELECT
        project_id,
        date,
        currency,
        subtotal_ht_micros,
        total_ttc_micros,
        total_row_count,
        complete_row_count,
        is_ladder_complete,
        gap_codes
    FROM {{ ref('fee_tax_ladder_rollup') }}
    WHERE rollup_kind = 'global'
),

cost_by_basis AS (
    SELECT
        project_id, date, 'HT' AS tax_basis, currency,
        subtotal_ht_micros AS cost_micros,
        total_row_count, complete_row_count, is_ladder_complete, gap_codes
    FROM cost_global
    UNION ALL
    SELECT
        project_id, date, 'TTC' AS tax_basis, currency,
        total_ttc_micros AS cost_micros,
        total_row_count, complete_row_count, is_ladder_complete, gap_codes
    FROM cost_global
),

-- FULL OUTER JOIN on the basis: a project with paid media and no commerce revenue, and a
-- project with commerce revenue and no paid media, are BOTH legitimate states and both
-- must stay visible with a typed reason rather than being dropped.
joined AS (
    SELECT
        COALESCE(r.project_id, c.project_id)                     AS project_id,
        COALESCE(r.date, c.date)                                 AS date,
        COALESCE(r.tax_basis, c.tax_basis)                       AS tax_basis,
        COALESCE(r.currency, c.currency)                         AS currency,
        r.revenue_source                                         AS revenue_source,
        r.revenue_source_priority                                AS revenue_source_priority,
        r.revenue_micros                                         AS revenue_micros,
        r.revenue_complete                                       AS revenue_complete,
        r.revenue_gap_codes                                      AS revenue_gap_codes,
        c.cost_micros                                            AS cost_micros,
        c.total_row_count                                        AS cost_row_count,
        c.complete_row_count                                     AS cost_complete_row_count,
        c.is_ladder_complete                                     AS is_cost_complete,
        c.gap_codes                                              AS cost_gap_codes,
        COALESCE(r.revenue_posture_undeclared, 0)                AS revenue_posture_undeclared,
        CASE WHEN r.project_id IS NULL THEN 1 ELSE 0 END         AS revenue_absent,
        CASE WHEN c.project_id IS NULL THEN 1 ELSE 0 END         AS cost_absent
    FROM revenue_by_basis r
    FULL OUTER JOIN cost_by_basis c
        ON  c.project_id = r.project_id
        AND c.date       = r.date
        AND c.currency   = r.currency
        AND c.tax_basis  = r.tax_basis
),

coded AS (
    SELECT
        j.*,
        -- Alphabetical CASE chain, so the joined string is SORTED BY CONSTRUCTION.
        CASE WHEN COALESCE(j.is_cost_complete, TRUE) = FALSE
             THEN '|COST_LADDER_INCOMPLETE'    ELSE '' END
     || CASE WHEN j.cost_absent = 1
             THEN '|COST_SIDE_ABSENT'          ELSE '' END
     || CASE WHEN j.cost_micros = 0
             THEN '|RATIO_UNDEFINED_ZERO_COST' ELSE '' END
     || CASE WHEN COALESCE(j.revenue_complete, 1) = 0
             THEN '|REVENUE_NORMALIZATION_INCOMPLETE' ELSE '' END
     || CASE WHEN j.revenue_absent = 1
             THEN '|REVENUE_SIDE_ABSENT'       ELSE '' END
        -- Sorts last, and that is the alphabetical accident that keeps the chain
        -- sorted by construction: 'REVENUE_S' < 'REVENUE_T'.
     || CASE WHEN j.revenue_posture_undeclared = 1
             THEN '|REVENUE_TAX_POSTURE_UNDECLARED' ELSE '' END
            AS gap_codes_prefixed
    FROM joined j
)

SELECT
    c.project_id,
    c.date,
    c.tax_basis,
    c.currency,

    -- Which commerce source won the day, and at what declared priority. Carried on every
    -- row because a series that crosses a source change mixes two populations.
    c.revenue_source,
    c.revenue_source_priority,

    c.revenue_micros,
    c.cost_micros,

    -- SUM-THEN-DIVIDE, in a VIEW, at ONE grain, never re-summed. The micros cancel, so
    -- the ratio is unit-free. cost_micros = 0 yields NULL, never infinity, and the row
    -- says RATIO_UNDEFINED_ZERO_COST. An incomplete cost ladder yields a NULL cost and
    -- therefore a NULL ratio: understating cost would OVERSTATE ROAS.
    -- Story 48.4: computed in EXACT DECIMAL at one declared scale, not binary float.
    -- BIGINT / BIGINT returns DOUBLE on DuckDB and FLOAT64 on BigQuery, so the two
    -- warehouses could print different digits for the same two integers and the low
    -- bits moved with evaluation order. A ratio is not generally terminating, so the
    -- guarantee is a declared scale both adapters quantise at, never "no error".
    {{ fee_tax_exact_ratio('c.revenue_micros', 'c.cost_micros') }}
                                                                 AS revenue_cost_ratio,
    CASE c.tax_basis
        WHEN 'HT'  THEN 'roas_ht'
        ELSE            'cash_cover_ttc'
    END                                                          AS ratio_kind,

    -- HT ROW ONLY, and EXACT INTEGER SUBTRACTION -- no rounding, no division. A margin
    -- across two different tax flows is not a margin, so the TTC row carries NULL.
    CASE
        WHEN c.tax_basis = 'HT'
         AND c.revenue_micros IS NOT NULL
         AND c.cost_micros IS NOT NULL
            THEN c.revenue_micros - c.cost_micros
        ELSE NULL
    END                                                          AS gross_margin_micros,

    COALESCE(c.cost_row_count, 0)                                AS cost_row_count,
    COALESCE(c.cost_complete_row_count, 0)                       AS cost_complete_row_count,
    COALESCE(c.is_cost_complete, FALSE)                          AS is_cost_complete,
    COALESCE(c.revenue_complete, 0) = 1                          AS is_revenue_complete,

    -- Criterion [4], as a TYPED COLUMN and not only as a substring of gap_codes: a
    -- surface must be able to say "the HT/TTC comparison is refused because this
    -- source never declared whether its figure includes tax" WITHOUT parsing a
    -- '|'-joined string. TRUE here means the ratio is NULL for a reason a reader can
    -- act on -- ask the source, or declare the posture -- rather than for the generic
    -- reason that something upstream was incomplete.
    c.revenue_posture_undeclared = 1                             AS is_revenue_posture_undeclared,
    c.gap_codes_prefixed = ''                                    AS is_alignment_complete,
    CASE WHEN c.gap_codes_prefixed = '' THEN ''
         ELSE SUBSTR(c.gap_codes_prefixed, 2) END                AS gap_codes,

    -- The two sides' OWN gap strings, carried as provenance so a surface can say WHICH
    -- side is short without re-deriving anything.
    COALESCE(c.cost_gap_codes, '')                               AS cost_gap_codes,
    COALESCE(c.revenue_gap_codes, '')                            AS revenue_gap_codes
FROM coded c
