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

{{ config(materialized='view') }}

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
        CAST(l.budget AS DOUBLE)               AS budget,
        CAST(l.is_plan_only AS BOOLEAN)        AS is_plan_only,
        l.sort_order                           AS sort_order
    FROM active_version av
    JOIN {{ source('mirror', 'media_plan_lines') }} l
        ON l.version_id = av.version_id
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
        CAST(a.amount AS DOUBLE)               AS allocated_amount
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
        CAST(m.split_weight AS DOUBLE)         AS split_weight
    FROM {{ source('mirror', 'plan_line_mappings') }} m
    WHERE m.status = 'active'
),

-- --------------------------------------------------- REAL CAMPAIGN SPEND ----
-- fact_daily_kpi spend at the campaign grain the fact exposes. We scope to the
-- projects that actually have a plan (join on project_id below) -- fact totals are
-- READ, never mutated (this is a SELECT). Cost is additive (AD-4): SUM is valid.
campaign_spend AS (
    SELECT
        f.project_id                           AS project_id,
        f.connector                            AS connector,
        f.breakdown_value                      AS campaign_ref,
        CAST(f.date AS DATE)                   AS day,
        SUM(CAST(f.value AS DOUBLE))           AS spend,
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
        SUM(cs.spend * am.split_weight)                    AS actual_amount,
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
SELECT
    COALESCE(al.project_id, ve.project_id)             AS project_id,
    COALESCE(al.plan_id, ve.plan_id)                   AS plan_id,
    COALESCE(al.plan_version_id, ve.plan_version_id)   AS plan_version_id,
    COALESCE(al.line_key, ve.line_key)                 AS line_key,
    COALESCE(al.day, ve.day)                           AS day,
    ln.label                                           AS label,
    ln.channel                                         AS channel,
    ln.currency                                        AS currency,
    ln.budget                                          AS budget,
    ln.start_date                                      AS line_start_date,
    ln.end_date                                        AS line_end_date,
    ln.is_plan_only                                    AS is_plan_only,
    ln.sort_order                                      AS sort_order,
    al.allocated_amount                                AS allocated_amount,
    -- plan-only lines never carry a ventilated row; actual stays NULL (never 0).
    ve.actual_amount                                   AS actual_amount,
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
