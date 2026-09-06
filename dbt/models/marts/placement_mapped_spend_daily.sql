-- placement_mapped_spend_daily: THE AGGREGATION PLACEMENT MAPPING ADDS ITS TWO
-- COLUMNS TO.
--
-- ============================ WHY THIS MODEL EXISTS ==========================
-- `docs/product-architecture/project-settings.md` ratified Placement Mapping an
-- AGGREGATION OPTION on 2026-08-07 -- "enabling it adds an id column and a name
-- column carrying the mapped plan line, in exactly the way enabling Country adds
-- a country column" -- and
-- `docs/product-architecture/capabilities/placement-mapping.md` named the two:
-- `plan_line_key` and `plan_line_label`. Measured 2026-08-30, before this model,
-- `grep -rn 'plan_line_key|plan_line_label' server dbt ui/admin/src` returned
-- exactly two lines, both COMMENTS in `server/core/analytics_alignment.py`
-- citing them as the precedent for its own columns. The columns an aggregation
-- was supposed to gain existed nowhere an aggregation could gain them.
--
-- This is the aggregation. One row per
--   (project_id, plan_id, plan_version_id, connector, campaign_ref, day, plan_line_key)
-- over the observed cost of a plan's perimeter, and the two columns are the last
-- of that key.
--
-- ===================== NULL IS THE MEASUREMENT ===============================
-- `placement-mapping.md`: *"Both columns are null on a row no plan line matches,
-- and that is a measurement rather than a gap [...] A `0`, an empty string or an
-- `Unmapped` sentinel would each make unplanned spend look planned. Rows
-- carrying null on both columns are exactly the fourth matching state, and they
-- are the ones that reveal spend nobody budgeted."*
-- So the model has TWO branches and no third: `covered`, whose rows carry a
-- line_key and its label, and `uncovered`, whose rows carry two NULLs and the
-- campaign's whole spend. Nothing composes a value out of an absence anywhere
-- below, which is what makes the sentinel unreachable rather than merely absent.
-- `test_placement_mapping_null_never_a_sentinel.sql` asserts it on the built
-- warehouse, and `test_placement_mapping_conservation.sql` asserts that the two
-- branches together re-sum to the observed spend they split.
--
-- ===================== IT ADDS NO SECOND VENTILATION =========================
-- The ventilation is `app.plan_line_mappings.split_weight`, under the invariant
-- SUM = 1.0 per (plan_id, connector, campaign_ref) the store enforces, and it is
-- the SAME one `plan_vs_actual_daily.sql` applies -- same weight, same
-- `fee_tax_pct_of_micros` macro, same day-by-day line-window bound. This model
-- states it at the CAMPAIGN grain instead of collapsing it to the line-day
-- grain, so the campaign a line ventilates stays nameable; it invents no weight
-- of its own and reads `app.plan_line_placement_mappings` not at all -- that
-- table carries no weight and `placement-mapping.md` requires that attaching a
-- placement change no monetary figure.
--
-- ===================== COVERAGE IS DECIDED DAY BY DAY ========================
-- The same rule `mediaplan_mapping.list_unmapped_actuals` already applies, and
-- for the reason its docstring gives (E1-F-1): a (connector, campaign_ref, day)
-- is COVERED iff at least one ACTIVE mapping of this plan reaches a line of the
-- active version whose [start_date, end_date] contains the day. Subtracting over
-- the plan's whole envelope instead would make a mapped campaign's spend on a
-- day outside its line's window vanish from BOTH branches -- present in neither
-- the ventilation nor the unmatched rows, which is money disappearing quietly.
--
-- ===================== A GAP IS NOT A ZERO ===================================
-- The guard `plan_vs_actual_daily.sql` carries, carried here for the same
-- reason: `fx_convert_at_read` yields NULL when no rate resolves and `SUM()`
-- skips NULLs, so a campaign-day with an unresolvable rate would otherwise
-- contribute 0 and read as a campaign that spent nothing.
-- `COUNT(*) <> COUNT(f.value)` withholds the amount instead, and
-- `money_gap_code` says why. A withheld row still carries its two added columns:
-- whether a plan line matched is a fact about the MAPPING, not about the money,
-- and losing the row would lose the match.
--
-- ===================== THE MIRROR MAY NOT BE HERE ============================
-- Same guard as `plan_vs_actual_daily.sql` (AI-314): `mirror_sync` defers its
-- BigQuery writes, so in production the `mirror` dataset does not exist and this
-- model builds EMPTY, naming the relations it did not find, rather than failing
-- every model downstream of it.
{{ config(materialized='view') }}

{%- set mirror_missing = toorow_absent_sources('mirror', [
      'media_plans',
      'media_plan_versions',
      'media_plan_lines',
      'plan_line_mappings',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['plan_id', 'string'],
    ['plan_version_id', 'string'],
    ['connector', 'string'],
    ['campaign_ref', 'string'],
    ['day', 'date'],
    ['plan_line_key', 'string'],
    ['plan_line_label', 'string'],
    ['split_weight', 'float'],
    ['spend', 'numeric'],
    ['spend_micros', 'bigint'],
    ['plan_currency', 'string'],
    ['money_gap_code', 'string'],
    ['pull_id', 'string'],
]) }}
{%- else %}

WITH active_version AS (
    -- The single active version per plan. Same deterministic window pick as
    -- `plan_vs_actual_daily`: a mirror lag that momentarily shows two actives
    -- must not fan this join out.
    SELECT plan_id, version_id, project_id, currency
    FROM (
        SELECT
            v.plan_id                          AS plan_id,
            v.id                               AS version_id,
            p.project_id                       AS project_id,
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
-- `line_key` is the STABLE cross-version identity and `label` is read at THIS
-- active version -- which is exactly what the two added columns are declared to
-- carry. `l.id`, the versioned row id, is deliberately not selected: a column
-- built on it would not survive a re-import of the plan.
lines AS (
    SELECT
        av.project_id                          AS project_id,
        av.plan_id                             AS plan_id,
        av.version_id                          AS plan_version_id,
        av.currency                            AS plan_currency,
        l.line_key                             AS line_key,
        l.label                                AS label,
        CAST(l.start_date AS DATE)             AS start_date,
        CAST(l.end_date AS DATE)               AS end_date
    FROM active_version av
    JOIN {{ source('mirror', 'media_plan_lines') }} l
        ON l.version_id = av.version_id
),

-- The plan's perimeter window: the envelope of its active version's lines, the
-- same one `list_unmapped_actuals` reads. Spend outside it is not this plan's
-- business at all and is neither matched nor reported unmatched here.
plan_window AS (
    SELECT
        project_id,
        plan_id,
        plan_version_id,
        plan_currency,
        MIN(start_date)                        AS window_start,
        MAX(end_date)                          AS window_end
    FROM lines
    GROUP BY project_id, plan_id, plan_version_id, plan_currency
),

-- ----------------------------------------------------------- ACTIVE MAPS ----
-- Only ACTIVE mappings ventilate. An `orphaned` match receives nothing, so a
-- campaign whose every match is orphaned lands in the NULL branch -- which is
-- the same predicate `plan_vs_actual_daily` and `line_matching_state` apply.
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

-- --------------------------------------------------- OBSERVED CAMPAIGN SPEND -
-- The campaign grain the fact exposes: `breakdown_dimension = 'campaign_id'`,
-- `breakdown_value` IS the campaign reference (fact_daily_kpi has no dedicated
-- campaign column). A day any of whose contributing rows could not be converted
-- states NO amount -- see the header.
campaign_spend AS (
    SELECT
        f.project_id                           AS project_id,
        f.connector                            AS connector,
        f.breakdown_value                      AS campaign_ref,
        CAST(f.date AS DATE)                   AS day,
        CASE WHEN COUNT(*) <> COUNT(f.value) THEN NULL
             ELSE SUM({{ fee_tax_to_micros('f.value') }}) END AS spend_micros,
        MIN(f.money_gap_code)                  AS money_gap_code,
        MAX(f.pull_id)                         AS pull_id
    FROM {{ ref('fact_daily_kpi') }} f
    WHERE f.metric = 'cost'
      AND f.breakdown_dimension = 'campaign_id'
    GROUP BY f.project_id, f.connector, f.breakdown_value, f.date
),

perimeter AS (
    SELECT
        pw.project_id                          AS project_id,
        pw.plan_id                             AS plan_id,
        pw.plan_version_id                     AS plan_version_id,
        pw.plan_currency                       AS plan_currency,
        cs.connector                           AS connector,
        cs.campaign_ref                        AS campaign_ref,
        cs.day                                 AS day,
        cs.spend_micros                        AS spend_micros,
        cs.money_gap_code                      AS money_gap_code,
        cs.pull_id                             AS pull_id
    FROM plan_window pw
    JOIN campaign_spend cs
        ON cs.project_id = pw.project_id
       AND cs.day       >= pw.window_start
       AND cs.day       <= pw.window_end
),

-- ---------------------------------------------------------------- COVERED ----
-- A plan line matched: the two columns carry its stable key and its label, and
-- the spend is this line's share of the campaign-day under the ONE ventilation.
covered AS (
    SELECT
        p.project_id                           AS project_id,
        p.plan_id                              AS plan_id,
        p.plan_version_id                      AS plan_version_id,
        p.plan_currency                        AS plan_currency,
        p.connector                            AS connector,
        p.campaign_ref                         AS campaign_ref,
        p.day                                  AS day,
        ln.line_key                            AS plan_line_key,
        ln.label                               AS plan_line_label,
        am.split_weight                        AS split_weight,
        {{ fee_tax_pct_of_micros('p.spend_micros', 'am.split_weight') }} AS spend_micros,
        p.money_gap_code                       AS money_gap_code,
        p.pull_id                              AS pull_id
    FROM perimeter p
    JOIN active_mappings am
        ON am.plan_id      = p.plan_id
       AND am.connector    = p.connector
       AND am.campaign_ref = p.campaign_ref
    JOIN lines ln
        ON ln.plan_id         = p.plan_id
       AND ln.plan_version_id = p.plan_version_id
       AND ln.line_key        = am.line_key
    WHERE p.day >= ln.start_date
      AND p.day <= ln.end_date
),

-- -------------------------------------------------------------- UNCOVERED ----
-- No plan line matched. BOTH columns are NULL and the campaign keeps its whole
-- spend: these are the rows that reveal a budget nobody wrote, and they are
-- listed rather than hidden -- « il est le travail, pas l'absence de travail ».
uncovered AS (
    SELECT
        p.project_id                           AS project_id,
        p.plan_id                              AS plan_id,
        p.plan_version_id                      AS plan_version_id,
        p.plan_currency                        AS plan_currency,
        p.connector                            AS connector,
        p.campaign_ref                         AS campaign_ref,
        p.day                                  AS day,
        CAST(NULL AS {{ dbt.type_string() }})  AS plan_line_key,
        CAST(NULL AS {{ dbt.type_string() }})  AS plan_line_label,
        CAST(NULL AS {{ toorow_float_type() }})                   AS split_weight,
        p.spend_micros                         AS spend_micros,
        p.money_gap_code                       AS money_gap_code,
        p.pull_id                              AS pull_id
    FROM perimeter p
    WHERE NOT EXISTS (
        SELECT 1
        FROM covered c
        WHERE c.plan_id      = p.plan_id
          AND c.connector    = p.connector
          AND c.campaign_ref = p.campaign_ref
          AND c.day          = p.day
    )
),

-- NOT NAMED `both`: `BOTH` is a reserved word in DuckDB (`TRIM(BOTH ...)`) and
-- the parser refuses it as a CTE alias. Measured by a build, not guessed.
projected AS (
    SELECT * FROM covered
    UNION ALL
    SELECT * FROM uncovered
)

SELECT
    project_id,
    plan_id,
    plan_version_id,
    connector,
    campaign_ref,
    day,
    -- THE TWO COLUMNS. Null together, or filled together; never a sentinel.
    plan_line_key,
    plan_line_label,
    split_weight,
    {{ fee_tax_from_micros('spend_micros') }}  AS spend,
    spend_micros,
    plan_currency,
    money_gap_code,
    pull_id
FROM projected
{%- endif -%}
