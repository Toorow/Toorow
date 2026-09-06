-- plan_vs_actual_daily: per-day plan allocation vs ventilated real spend
-- (Epic 22, Story 22.4 / CAP-26 / FR38 -- spike 22-0 §2/§3/§5).
--
-- ============================ THIS MART IS A READ ============================
-- plan_vs_actual_daily READS the plan mirror (media_plans / media_plan_versions /
-- media_plan_lines / plan_allocation_daily / plan_line_mappings) + fact_daily_kpi.
-- It creates NO new fact row and edits NO existing model. fact_daily_kpi /
-- cross_source_conversions / metric_baselines / dedup_estimate are STRICTLY
-- UNTOUCHED (Epic 22 règle d'or AD-4/AD-6; the "totaux inchangés" discipline of
-- review-epic-10/12.4/17.2). The plan is a JOIN layer, never a rewrite of facts.
--
-- GRAIN (enforced by plan_vs_actual_daily_grain_unique):
--   one row per (project_id, plan_id, plan_version_id, line_key, day).
--   day spans the UNION of the line's allocated days and the days it carries real
--   spend, so a day with allocation-but-no-spend and a day with spend-but-no-
--   allocation both surface (LEFT/RIGHT via FULL OUTER JOIN) -- neither is hidden.
--
-- ===================== ACTIVE VERSION ONLY (decision 3) ======================
-- Only the ACTIVE version's allocations participate (media_plan_versions.is_active).
-- Reprocess-complet (décision 3): the active version's allocation is materialised
-- over its WHOLE range, so pacing = real past vs the ACTIVE plan. "Ce qui était
-- attendu à l'époque" lives in the immutable versions, not here.
--
-- ===================== VENTILATION (anti-double-count, 22.3) =================
-- Real spend = fact_daily_kpi rows with metric='cost' AND
-- breakdown_dimension='campaign_id' (the campaign reference the fact grain exposes;
-- fact_daily_kpi has NO dedicated campaign column -- see 22.3 migration 041 and
-- warehouse.query_campaign_spend). A campaign's daily spend is split across the
-- plan lines that map it by the ACTIVE mappings' split_weight (SUM=1.0 per
-- (plan, connector, campaign_ref), enforced by the store). So:
--     ventilated_spend(line, day) =
--        SUM over mapped (connector, campaign_ref) of
--           fact_spend(connector, campaign_ref, day) * split_weight
-- The ventilated total of a campaign over the lines that claim it == the campaign's
-- original daily spend EXACTLY (Σ split_weight = 1.0) -- a campaign shared 0.5/0.5
-- between two lines contributes 50/50, never 100/100 (the discriminant that a naive
-- JOIN without weights would fail). Proven by test_plan_vs_actual_ventilation_sum.sql.
-- A campaign is NEVER counted in two lines at full weight; the ventilation is the
-- ONLY place spend is split, and it re-sums to the origin at the cent.
--
-- ===================== PLAN-ONLY LINES (decision 7, AD-9) ====================
-- is_plan_only lines (TV, OOH -- no actuals source) are PRESENT with their budget
-- and allocations, but actual_amount is NULL (never 0): there is no honest spend to
-- pace. They carry no mapping, so they never receive ventilated spend anyway; the
-- explicit is_plan_only flag makes downstream pacing skip them (never a 0% trap).
--
-- ===================== NO INTER-PLAN AGGREGATION ============================
-- The grain is keyed by plan_id; a campaign shared across two concurrent plans is
-- ventilated INDEPENDENTLY per plan (each plan has a self-coherent view, décision 5).
-- This model NEVER sums across plans -- that would need a dedicated dedup (Phase B).
--
-- ===================== PROVENANCE (AD-9) ====================================
-- plan_version_id on every row (which active version produced the allocation).
-- actual_pull_id = MAX(pull_id) of the fact rows that fed the ventilated spend of
-- the (line, day) -- the repo convention (customer_journeys / dedup_estimate carry
-- MAX(pull_id) per group, AD-7). NULL when there is no real spend that day
-- (allocation-only day / plan-only line). The by-line rollup exposes MIN/MAX/COUNT
-- DISTINCT over these so a line cites the full span + count of its provenance ids.
--
-- ============ STORY 61.4 (AI-266): THE TWO SIDES WERE NEVER IN THE SAME =======
-- ============                       CURRENCY, AND NOTHING SAID SO       =======
-- Measured 2026-08-09, and it was being SERVED -- by the pacing route, by the MCP
-- card `mediaplan_pacing`, by `mediaplan_alerts` and by the scheduler:
--
--   * the planned side is `media_plans.currency` (migration 040:49), one currency
--     per plan, chosen by whoever imported the plan;
--   * the real side is `fact_daily_kpi.value`, which is
--     `fx_convert_at_read('cost')` -- an amount ALREADY CONVERTED into the
--     currency the staging FX join targeted, `dim_project.canonical_currency`;
--   * and this model put `media_plans.currency` on the row as `currency`.
--
-- So a plan in USD compared with spend converted into EUR was rendered "USD". The
-- number was wrong AND its label was wrong, which is worse: a wrong number under
-- an honest label invites a check, a wrong number under a confident label does
-- not. It is `placement-mapping.md`'s own "Incomplete if" -- *a pacing figure is
-- composed across unconverted currencies* -- and it had been true since 22.4.
--
-- WHAT LABELS AN AMOUNT HERE NOW. The rule is one sentence: **a currency column
-- names the currency that PRODUCED the amount beside it, or it is NULL.**
--
--   `plan_currency`      labels `budget` and `allocated_amount`  (the plan's own)
--   `actual_currency`    labels `actual_amount`   (the FX target that produced it)
--   `reporting_currency` is the CONFIRMED Money Policy's (`money_policy.py`,
--                        `app.project_money_policy_v`, migration 148) -- the
--                        governed authority, carried with its version id
--   `currency`           is KEPT for the four readers that already select it, and
--                        it now means "the ONE currency this whole row is in":
--                        non-NULL only when plan_currency = actual_currency. It
--                        can be true or absent; it can no longer be wrong.
--
-- WHEN NO AMOUNT IS STATED AT ALL (`money_gap_code` says which):
--   `fx_rate_unavailable` / `native_currency_missing` -- a contributing fact row
--       could not be converted, so `fx_convert_at_read` yielded NULL. SUM() SKIPS
--       nulls, so before this story that day contributed **0** to the spend and
--       the line read as under-delivering. An absent rate is not a zero spend,
--       and it must never accuse anybody: the day makes the whole ventilated sum
--       NULL, which is what `mediaplan_alerts` already refuses to pace.
--   `reporting_currency_mismatch` -- the confirmed Money Policy names a currency
--       the conversion did not target. Neither label would be true, so no actual
--       is stated.
-- And `money_policy_unconfirmed` does NOT suppress: `canonical_currency` is an
-- operator-confirmed column (its `_origin` / `_confirmation_status` pair, and
-- Story 48.3 removed its 'EUR' DEFAULT), so the amount is real and correctly
-- labelled -- it is simply not GOVERNED yet, and the row says so instead of
-- pretending either way.
--
-- ===================== MONEY IS EXACT AGAIN (Story 48.3's rule) =============
-- `SUM(CAST(f.value AS DOUBLE))` and `CAST(l.budget AS DOUBLE)` put money back on
-- binary floats -- the very cast `fx_convert_at_read` was repaired to remove.
-- Every monetary quantity is now normalised to integer micros PER SOURCE ROW
-- (`fee_tax_to_micros`, the repo's one adapter, mirroring
-- `server/core/money.py::to_canonical_micros`) and only then summed, and the
-- ventilation multiplies in exact decimal (`fee_tax_pct_of_micros`). The display
-- columns are DERIVED from the micros ONCE, at the end, so `budget` /
-- `allocated_amount` / `actual_amount` keep their names and their values while
-- the authority underneath them is an exact integer.

-- ============ AND THE PLAN MIRROR MAY NOT BE IN THIS WAREHOUSE (AI-314) ======
-- The five plan relations above live in the mirror, and `mirror_sync` defers its
-- BigQuery writes (Phase B): in production the `mirror` dataset does not exist,
-- so this model could not be built at all and dbt skipped every model downstream
-- of it. A Project with no mirrored plan has no pacing to state -- that is an
-- ordinary state, not a failure -- so the model is built EMPTY and names the
-- relations it did not find. Same guard as the marts over an absent raw source
-- (`toorow_absent_sources`, dbt/macros/relation_present.sql).
{{ config(materialized='view') }}

{%- set mirror_missing = toorow_absent_sources('mirror', [
      'media_plans',
      'media_plan_versions',
      'media_plan_lines',
      'plan_allocation_daily',
      'plan_line_mappings',
      'project_money_policy',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['plan_id', 'string'],
    ['plan_version_id', 'string'],
    ['line_key', 'string'],
    ['day', 'date'],
    ['label', 'string'],
    ['channel', 'string'],
    ['currency', 'string'],
    ['plan_currency', 'string'],
    ['actual_currency', 'string'],
    ['reporting_currency', 'string'],
    ['money_policy_version_id', 'string'],
    ['money_gap_code', 'string'],
    ['budget', 'numeric'],
    ['budget_micros', 'bigint'],
    ['line_start_date', 'date'],
    ['line_end_date', 'date'],
    ['is_plan_only', 'boolean'],
    ['sort_order', 'bigint'],
    ['allocated_amount', 'numeric'],
    ['allocated_micros', 'bigint'],
    ['actual_amount', 'numeric'],
    ['actual_micros', 'bigint'],
    ['actual_withheld', 'boolean'],
    ['native_currency', 'string'],
    ['fx_as_of_date_min', 'date'],
    ['fx_as_of_date_max', 'date'],
    ['fx_source', 'string'],
    ['fx_tier', 'string'],
    ['fx_method', 'string'],
    ['actual_pull_id', 'string'],
]) }}
{%- else %}

WITH active_version AS (
    -- The single active version per plan (partial-unique in Postgres; we still
    -- guard with a window pick so a mirror lag that momentarily shows two actives
    -- cannot fan the join out -- deterministic: highest version_number wins).
    SELECT plan_id, version_id, project_id, plan_name, currency
    FROM (
        SELECT
            v.plan_id                          AS plan_id,
            v.id                               AS version_id,
            p.project_id                       AS project_id,
            p.name                             AS plan_name,
            p.currency                         AS currency,
            ROW_NUMBER() OVER (
                PARTITION BY v.plan_id
                ORDER BY v.version_number DESC
            )                                  AS _rn
        FROM {{ source('mirror', 'media_plan_versions') }} v
        JOIN {{ source('mirror', 'media_plans') }} p
            ON p.id = v.plan_id
        WHERE v.is_active
          AND p.archived_at IS NULL
    ) ranked
    WHERE _rn = 1
),

-- ------------------------------------------------------------------- LINES ---
-- The active version's lines (line_key is the stable identity the mapping uses).
lines AS (
    SELECT
        av.project_id                          AS project_id,
        av.plan_id                             AS plan_id,
        av.version_id                          AS plan_version_id,
        av.currency                            AS currency,
        l.id                                   AS line_id,
        l.line_key                             AS line_key,
        l.label                                AS label,
        l.channel                              AS channel,
        -- Mirror columns may surface as VARCHAR (mirror is a typed copy of
        -- Postgres but the dev seed path writes ISO strings): CAST explicitly.
        CAST(l.start_date AS DATE)             AS start_date,
        CAST(l.end_date AS DATE)               AS end_date,
        -- Money, in exact integer micros. `CAST(... AS DOUBLE)` was here.
        {{ fee_tax_to_micros('l.budget') }}    AS budget_micros,
        CAST(l.is_plan_only AS BOOLEAN)        AS is_plan_only,
        l.sort_order                           AS sort_order
    FROM active_version av
    JOIN {{ source('mirror', 'media_plan_lines') }} l
        ON l.version_id = av.version_id
),

-- ------------------------------------------------------ THE TWO CURRENCIES ---
-- `project_money` is the CONFIRMED Money Policy (`app.project_money_policy_v`,
-- migration 148, mirrored by `mirror_sync` since 61.4). One row per Project that
-- has PUBLISHED one; a Project with no row has not decided, which 48.3 keeps
-- deliberately distinct from "decided on the default".
--
-- `conversion_target` is the currency the staging FX join actually converted INTO
-- (`stg_*_daily`: `fx.to_currency = dim_project.canonical_currency`). It is what
-- `fact_daily_kpi.value` is expressed in, so it is what may label it -- and it is
-- read from `dim_project`, which reads `mirror.project_preferences` only, so this
-- adds no cycle.
project_money AS (
    SELECT
        project_id,
        reporting_currency                     AS reporting_currency,
        money_policy_version_id                AS money_policy_version_id
    FROM {{ source('mirror', 'project_money_policy') }}
),

conversion_target AS (
    SELECT
        project_id,
        canonical_currency                     AS actual_currency
    FROM {{ ref('dim_project') }}
),

-- ------------------------------------------------------ ALLOCATED PER DAY ----
-- The materialised daily spread of the active version, per line.
allocated AS (
    SELECT
        ln.project_id,
        ln.plan_id,
        ln.plan_version_id,
        ln.line_key,
        CAST(a.day AS DATE)                    AS day,
        {{ fee_tax_to_micros('a.amount') }}    AS allocated_micros
    FROM lines ln
    JOIN {{ source('mirror', 'plan_allocation_daily') }} a
        ON a.version_id = ln.plan_version_id
       AND a.line_id    = ln.line_id
),

-- ----------------------------------------------------------- ACTIVE MAPS ----
-- Only ACTIVE mappings ventilate spend (orphaned ones do not). Scoped per plan.
active_mappings AS (
    SELECT
        m.plan_id                              AS plan_id,
        m.line_key                             AS line_key,
        m.connector                            AS connector,
        m.campaign_ref                         AS campaign_ref,
        CAST(m.split_weight AS {{ toorow_float_type() }})         AS split_weight
    FROM {{ source('mirror', 'plan_line_mappings') }} m
    WHERE m.status = 'active'
),

-- --------------------------------------------------- REAL CAMPAIGN SPEND ----
-- fact_daily_kpi spend at the campaign grain the fact exposes. We scope to the
-- projects that actually have a plan (join on project_id below) -- fact totals are
-- READ, never mutated (this is a SELECT). Cost is additive (AD-4): SUM is valid.
--
-- A GAP IS NOT A ZERO, AND THIS IS WHERE THAT WAS LOST (Story 61.4 / AI-266).
-- `fx_convert_at_read` yields NULL when no rate was resolved -- deliberately, so
-- an unconvertible amount is excluded rather than added at parity. But `SUM()`
-- SKIPS nulls, so `SUM(CAST(f.value AS DOUBLE))` turned that honest NULL back
-- into a silent 0 one model later: a day with no FX rate read as a day with no
-- spend, `pace` went negative, and `mediaplan_alerts` fired a
-- `mediaplan_pace_underdelivery` accusing a campaign of under-delivering when all
-- that was missing was a rate. `COUNT(*) <> COUNT(f.value)` is the guard: if ANY
-- contributing row could not be converted, the day states no amount at all and
-- `money_gap_code` says which of the two reasons it was.
--
-- THE FX EVIDENCE COMES UP WITH IT, which is what `placement-mapping.md:217`
-- asks for ("Planned versus observed, with the reporting currency and its FX
-- provenance"). `fx_rate` itself is deliberately NOT carried: it varies day by
-- day, so a single MAX() rate over a to-date span would be one day's rate wearing
-- the whole period's clothes. The AS-OF SPAN, the source, the tier and the method
-- are the honest provenance of an aggregate.
campaign_spend AS (
    SELECT
        f.project_id                           AS project_id,
        f.connector                            AS connector,
        f.breakdown_value                      AS campaign_ref,
        CAST(f.date AS DATE)                   AS day,
        CASE WHEN COUNT(*) <> COUNT(f.value) THEN NULL
             ELSE SUM({{ fee_tax_to_micros('f.value') }}) END AS spend_micros,
        -- MIN() over a column that is NULL when the row converted: it returns the
        -- gap code as soon as ONE row carries one, and NULL when none does.
        MIN(f.money_gap_code)                  AS money_gap_code,
        MIN(f.native_currency)                 AS native_currency,
        MIN(f.fx_as_of_date)                   AS fx_as_of_date_min,
        MAX(f.fx_as_of_date)                   AS fx_as_of_date_max,
        MIN(f.fx_source)                       AS fx_source,
        MIN(f.fx_tier)                         AS fx_tier,
        MIN(f.fx_method)                       AS fx_method,
        MAX(f.pull_id)                         AS pull_id
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.metric = 'cost'
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.project_id, f.connector, f.breakdown_value, f.date
),

-- ------------------------------------------------- VENTILATED PER LINE/DAY --
-- Split each campaign's daily spend across the lines that map it, by weight.
-- Σ(split_weight)=1.0 per (plan, connector, campaign_ref) => Σ ventilated over the
-- claiming lines == the campaign's original daily spend EXACTLY. Provenance: the
-- pull_ids of the contributing fact rows, DISTINCT + sorted, '|'-joined.
ventilated AS (
    SELECT
        ln.project_id,
        ln.plan_id,
        ln.plan_version_id,
        ln.line_key,
        cs.day                                             AS day,
        -- Exact decimal multiplication, then an integer SUM -- and the SAME guard
        -- as above one level up: a line whose ventilation draws on a campaign-day
        -- that could not be converted states NO amount, because SUM would
        -- otherwise skip that campaign and understate the line.
        CASE WHEN COUNT(*) <> COUNT(cs.spend_micros) THEN NULL
             ELSE SUM({{ fee_tax_pct_of_micros('cs.spend_micros', 'am.split_weight') }})
        END                                                AS actual_micros,
        MIN(cs.money_gap_code)                             AS money_gap_code,
        MIN(cs.native_currency)                            AS native_currency,
        MIN(cs.fx_as_of_date_min)                          AS fx_as_of_date_min,
        MAX(cs.fx_as_of_date_max)                          AS fx_as_of_date_max,
        MIN(cs.fx_source)                                  AS fx_source,
        MIN(cs.fx_tier)                                    AS fx_tier,
        MIN(cs.fx_method)                                  AS fx_method,
        MAX(cs.pull_id)                                    AS actual_pull_id
    FROM lines ln
    JOIN active_mappings am
        ON am.plan_id  = ln.plan_id
       AND am.line_key = ln.line_key
    JOIN campaign_spend cs
        ON cs.project_id   = ln.project_id
       AND cs.connector    = am.connector
       AND cs.campaign_ref = am.campaign_ref
    -- Ventilated spend only counts inside the line's own flight window (a mapping
    -- does not resurrect spend outside the line's dates); allocation is already
    -- window-bounded by construction, so the FULL OUTER JOIN below aligns on day.
    WHERE cs.day >= ln.start_date
      AND cs.day <= ln.end_date
    GROUP BY ln.project_id, ln.plan_id, ln.plan_version_id, ln.line_key, cs.day
)

-- ---------------------------------------------------------------- OUTPUT -----
-- FULL OUTER JOIN so a day with allocation-but-no-spend (actual NULL) AND a day
-- with spend-but-no-allocation (allocated NULL) both surface. is_plan_only lines
-- have allocations but never a ventilated row -> actual_amount stays NULL (honest).
--
-- THE MONEY GAP IS DECIDED HERE, ONCE, and every downstream model reads the
-- decision rather than re-deriving it. Order matters and it is the order of
-- severity: a conversion that did not happen beats a label that cannot be
-- trusted, which beats two currencies that cannot be composed.
{% set money_gap_code %}
    CASE
        -- 1. A contributing fact row could not be converted at all. Whatever the
        --    currencies say, there is no amount to label.
        WHEN ve.money_gap_code IS NOT NULL          THEN ve.money_gap_code
        -- 2. Nothing names the currency the conversion targeted.
        WHEN ct.actual_currency IS NULL             THEN 'reporting_currency_unresolved'
        -- 3. The governed currency and the produced one disagree. Neither label
        --    would be true, so the actual is withheld (see the SELECT below).
        WHEN pm.reporting_currency IS NOT NULL
             AND pm.reporting_currency <> ct.actual_currency
                                                    THEN 'reporting_currency_mismatch'
        -- 4. The plan is in one currency and the spend in another. BOTH amounts
        --    are stated, each under its own currency; nothing COMPOSED of the two
        --    is (see plan_pacing_by_line).
        WHEN ln.currency <> ct.actual_currency      THEN 'plan_currency_mismatch'
        -- 5. Real, correctly labelled, not governed yet. Never a suppression.
        WHEN pm.reporting_currency IS NULL          THEN 'money_policy_unconfirmed'
        ELSE NULL
    END
{% endset %}
{% set actual_is_statable %}
    (ve.actual_micros IS NOT NULL
     AND ct.actual_currency IS NOT NULL
     AND (pm.reporting_currency IS NULL OR pm.reporting_currency = ct.actual_currency))
{% endset %}
SELECT
    COALESCE(al.project_id, ve.project_id)             AS project_id,
    COALESCE(al.plan_id, ve.plan_id)                   AS plan_id,
    COALESCE(al.plan_version_id, ve.plan_version_id)   AS plan_version_id,
    COALESCE(al.line_key, ve.line_key)                 AS line_key,
    COALESCE(al.day, ve.day)                           AS day,
    ln.label                                           AS label,
    ln.channel                                         AS channel,
    -- THE ONE CURRENCY THIS ROW IS IN -- or nothing. It used to be the plan's,
    -- unconditionally, which is how a EUR amount was served labelled "USD".
    -- A row that states NO actual holds only plan-currency amounts, so the plan's
    -- currency IS the row's one currency: withholding the label there would lose
    -- a true statement (a plan-only line's budget has a currency) to protect
    -- against a conflict that cannot arise.
    CASE WHEN NOT ({{ actual_is_statable }}) OR ln.currency = ct.actual_currency
         THEN ln.currency ELSE NULL END                AS currency,
    ln.currency                                        AS plan_currency,
    CASE WHEN {{ actual_is_statable }}
         THEN ct.actual_currency ELSE NULL END         AS actual_currency,
    pm.reporting_currency                              AS reporting_currency,
    pm.money_policy_version_id                         AS money_policy_version_id,
    {{ money_gap_code }}                               AS money_gap_code,
    {{ fee_tax_from_micros('ln.budget_micros') }}      AS budget,
    ln.budget_micros                                   AS budget_micros,
    ln.start_date                                      AS line_start_date,
    ln.end_date                                        AS line_end_date,
    ln.is_plan_only                                    AS is_plan_only,
    ln.sort_order                                      AS sort_order,
    {{ fee_tax_from_micros('al.allocated_micros') }}   AS allocated_amount,
    al.allocated_micros                                AS allocated_micros,
    -- plan-only lines never carry a ventilated row; actual stays NULL (never 0).
    -- So does a day whose FX rate was missing, and a row whose governed currency
    -- disagrees with the one that produced the number.
    CASE WHEN {{ actual_is_statable }}
         THEN {{ fee_tax_from_micros('ve.actual_micros') }} ELSE NULL END
                                                       AS actual_amount,
    CASE WHEN {{ actual_is_statable }}
         THEN ve.actual_micros ELSE NULL END           AS actual_micros,
    -- THE DIFFERENCE BETWEEN "no spend" AND "spend we cannot state", and the
    -- rollups above depend on it: `actual_amount` is NULL for both, but only the
    -- second must make a to-date sum refuse. `actual_pull_id` is MAX(pull_id) of
    -- the contributing fact rows and survives a NULL value, so its presence is
    -- exactly the evidence that spend WAS there.
    (ve.actual_pull_id IS NOT NULL AND NOT ({{ actual_is_statable }}))
                                                       AS actual_withheld,
    ve.native_currency                                 AS native_currency,
    ve.fx_as_of_date_min                               AS fx_as_of_date_min,
    ve.fx_as_of_date_max                               AS fx_as_of_date_max,
    ve.fx_source                                       AS fx_source,
    ve.fx_tier                                         AS fx_tier,
    ve.fx_method                                       AS fx_method,
    ve.actual_pull_id                                  AS actual_pull_id
FROM allocated al
FULL OUTER JOIN ventilated ve
    ON  ve.project_id      = al.project_id
    AND ve.plan_id         = al.plan_id
    AND ve.plan_version_id = al.plan_version_id
    AND ve.line_key        = al.line_key
    AND ve.day             = al.day
-- Re-attach the line descriptors (budget/channel/flag) regardless of which side of
-- the FULL OUTER JOIN produced the row.
JOIN lines ln
    ON  ln.project_id      = COALESCE(al.project_id, ve.project_id)
    AND ln.plan_id         = COALESCE(al.plan_id, ve.plan_id)
    AND ln.plan_version_id = COALESCE(al.plan_version_id, ve.plan_version_id)
    AND ln.line_key        = COALESCE(al.line_key, ve.line_key)
-- LEFT, both of them: a Project with no confirmed Money Policy and a Project whose
-- preferences row is absent must still produce their plan rows, carrying the
-- absence as evidence. An INNER JOIN here would make the pacing of such a Project
-- vanish silently, which is the failure mode this story exists to remove.
LEFT JOIN conversion_target ct
    ON ct.project_id = COALESCE(al.project_id, ve.project_id)
LEFT JOIN project_money pm
    ON pm.project_id = COALESCE(al.project_id, ve.project_id)
{%- endif -%}
