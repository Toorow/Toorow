-- fee_tax_country_resolution: the Epic 37 country/market bridge + the AD-8 source_type,
-- resolved once per fact row (Epic 41, Story 41.2 / contract B1 + C.4 + C5).
--
-- ============================ THIS MODEL IS A READ ============================
-- It READS fact_daily_kpi + four governed mirror relations and RESOLVES attributes.
-- It creates NO new fact row, edits NO existing model, and captures NO country:
-- Epic 41 owns no country column, pulls no country from any connector and emits no
-- breakdown_dimension. fact_daily_kpi is STRICTLY UNTOUCHED (the plan_vs_actual_daily
-- overlay precedent, AD-4/AD-6). Story 41.3 LEFT JOINs this view ONCE on the six join
-- keys and contains zero country-resolution logic of its own.
--
-- ===================== FROZEN OUTPUT CONTRACT (14 columns) ====================
-- project_id, date, connector, metric, breakdown_dimension, breakdown_value,
-- attr_country, attr_market, attr_source_type, resolution_source,
-- country_gap_reason, market_gap_reason, source_type_gap_reason, gap_code.
-- Story 41.3's dev codes against these names sight unseen: they may not change.
-- The four accepted resolution_source values are 'row_dimension', 'declared_binding',
-- 'project_posture' and '' (a gap) -- see schema_fee_tax_bridge.yml.
--
-- ============================== THE LADDER ====================================
-- 1. row_dimension    -- the row's own breakdown_dimension = <country partition> value.
--                        Real data always outranks any inference, so when Story 37.6
--                        lands a paid-media country fanout those rows resolve HERE with
--                        no change to this model. There is NO connector name anywhere in
--                        this file, by design (AD-2 / E41-NFR05 / AC5).
-- 2. declared_binding -- the datastream's DECLARED Epic 37 market binding, read through
--                        mirror.datastream_country_binding_dim. >= 2 distinct markets on
--                        one connector is a CONTRADICTION: both axes gap, we never pick
--                        one, and rung 3 does not rescue it. Same for a binding to the
--                        governed Rest of World catch-all, which is not budgetable.
-- 3. project_posture  -- a DECLARED INFERENCE (C5 / AC14): a project whose PUBLISHED
--                        Country hierarchy tracks EXACTLY ONE country resolves every row
--                        to it. Reached only from a SILENCE (nothing declared), never
--                        after a contradiction, and never over a row that carries its own
--                        country. It needs NO migration 104, which is why a
--                        correctly-configured single-country client is not staring at
--                        blank totals on day one.
--
--                        THE LITERAL 'project_posture' IS FROZEN (see the contract note
--                        below) and Story 37.9 deliberately did NOT rename it: the rung
--                        means what it always meant -- an inference from the Project's
--                        own single tracked country -- only its authority moved from a
--                        mutable preference to a published, versioned hierarchy.
-- 4. a typed GAP      -- attr_country IS NULL (never '' and never a placeholder),
--                        resolution_source is '', and a *_gap_reason says why.
--
-- The single-country test is the DISTINCT COUNT OF COUNTRY CODES ACROSS EVERY TRACKED
-- MARKET -- the union, matching `GeographyProjection.market_of_value` -- and NOT the
-- number of markets. One market may hold several countries (a market is a named group of
-- countries); counting markets would resolve a multi-country project to an arbitrary
-- code, which is the exact failure AC14 forbids.
--
-- ======================== D3: COUNTRY AND MARKET ARE INDEPENDENT ==============
-- A binding to a market that holds >= 2 countries resolves attr_market (a rule
-- conditioned on `market` fires) while attr_country stays NULL with
-- country_gap_reason = 'market_not_single_country' (a rule conditioned on `country` is a
-- typed gap). attr_market may be non-NULL while attr_country is NULL. That asymmetry is
-- the point.
--
-- resolution_source is the provenance OF THE COUNTRY resolution: it is non-empty if and
-- only if attr_country is non-NULL. In the D3 case it is therefore '' even though the
-- market did resolve.
--
-- ===================== KNOWN FALSE != UNRESOLVABLE (E41-AD9 / NFR02) ==========
-- This model resolves ATTRIBUTES; it evaluates no rule and fabricates no zero. A
-- condition that is known false is 41.3's +0 micros; an attribute that is unresolvable is
-- the typed gap this model reports. Never interchange them.
--
-- ===================== TWO HONEST LIMITS OF THE SQL SIDE ======================
-- (a) "cannot read the registry" vs "nothing declared". The Python bridge
--     (server/core/fee_tax_geo_bridge.py) probes to_regclass('app.market_bindings') and
--     can report 'binding_registry_unavailable'. The mirror cannot: mirror_sync falls
--     back to a SHAPE-STABLE EMPTY projection while migration 104 is unapplied, which is
--     indistinguishable here from "no binding declared". This model therefore emits the
--     rung-4 reasons instead.
--
--     Story 37.9 does NOT extend that limit to the country meaning:
--     `mirror.country_market_projection` is a GUARDED relation, not a shape-stable empty,
--     so an unapplied migration 269 fails the build loudly at the declared deployment
--     order rather than reporting `no_binding_and_country_model_absent` for a Project
--     whose Country model is published. An empty relation here means exactly one thing.
--
--     THIS PARAGRAPH SAID "the SQL gap-reason vocabulary is a strict SUBSET of the
--     Python one". Measured 2026-08-09, it is not, and this same file falsifies it two
--     paragraphs below: 'source_type_not_declared' is introduced HERE, and
--     'signals_disagree' is reserved HERE. With 'no_datastream_for_connector' that is
--     three reasons absent from every file of server/core.
--
--     The true relation is the other way round, and it is not a defect: the warehouse
--     SEES things the runtime bridge cannot -- a connector with no datastream, a
--     source_type nobody declared -- so it EXTENDS the vocabulary rather than narrowing
--     it. What stays true, and is the half worth keeping: no reason means anything
--     different in the two engines. Nothing checks that today (AI-254).
-- (b) the source_type DERIVATION half of contract C.4 cannot run in the warehouse: it
--     needs the module manifest's public_catalog.category, which lives on disk and is
--     not mirrored. This model resolves source_type from the DECLARED table only. An
--     undeclared datastream is therefore a GAP (attr_source_type NULL), never a fabricated
--     'UNKNOWN' -- emitting 'UNKNOWN' would silently make every paid-media row known-false
--     against the auto-populated rules and the cascade would compute nothing at all.
--     That gap is labelled 'source_type_not_declared', which is what actually happened:
--     no signal disagreed, none was offered. 'signals_disagree' is RESERVED for the case
--     it names -- a declaration and a derivation that genuinely conflict -- which this
--     model cannot currently observe and therefore never emits. Telling an operator to go
--     reconcile a conflict that does not exist would be a lie that propagates straight
--     into the 41.7 rule matrix.
--
-- ===================== MODULE GATE (contract C.1) =============================
-- OFF => ZERO ROWS. A project resolves here only when
-- mirror.project_tax_fee_activation.tax_fees_active is TRUE -- see the `enabled_projects`
-- CTE below, which is the executable statement of this paragraph. It is NOT
-- project_preferences.fee_tax_alignment_enabled: that was the gate when 41.2 landed, and
-- Story 48.4 / migration 146 moved the authority because the two could disagree
-- (146:11-15). project_preferences is NO LONGER READ HERE AT ALL -- see GEOGRAPHIC
-- AUTHORITY below.
--
-- ===================== GEOGRAPHIC AUTHORITY (Story 37.9) =====================
-- This model read `project_preferences.geographic_mode` / `local_markets` until
-- 2026-08-17. Those are the columns `server/core/country_registry.py` says it "replaces
-- outright", and the ratified Country capability confirmation
-- (`project_settings` -> `country_activation`) derives a posture IN MEMORY to compile
-- Datastream plans and writes NOTHING back to them. Meanwhile Analyze
-- (`reports._load_geography_projection`) and the Tax MCP proposal path
-- (`capability_compilers._tax_fee_preset_proposals`) both read the PUBLISHED hierarchy
-- version through `country_registry.load_projection`.
--
-- So Tax was the second geographic mapping that
-- `docs/product-architecture/capabilities/country.md` forbids, and the failure was
-- SILENT: a Project governed through the ratified capability showed this model an empty
-- posture, every spend row came back `no_binding_and_posture_global`, and the composed
-- total was reported incomplete while its geography was fully published.
--
-- The input is now `mirror.country_market_projection` (migration 269), which is
-- `GeographyProjection.market_of_value` expressed in Postgres so both engines read the
-- same rows. NO ROW FOR A PROJECT IS "no governed Country meaning" -- read as "cannot
-- decide", NEVER as Global, exactly as `load_projection`'s docstring requires of every
-- caller.
--
-- The tracked set is `market_kind = 'market'`. Rest of World is a governed catch-all
-- over countries NOT assigned to an explicit reporting market (country.md), so its
-- members are resolved geographically but are not a tracked market: a row landing there
-- says `country_outside_tracked_markets`, and a binding naming it is `not bindable`.
-- That is the same boundary the retired posture drew with its synthetic groupings, now
-- drawn by a governed node kind instead of by a hard-coded id.
--
-- An absent activation row and a FALSE flag both mean OFF, and OFF means this model
-- returns nothing for that project -- not a row with NULL attributes. Without the gate
-- the view emitted one row per fact row
-- for EVERY project on the platform (24 553 for a project that has no preferences row at
-- all), which contradicts C.1 outright and ran the not_null + 6-key unique schema tests
-- over every org's entire fact table on every build.
--
-- ===================== ROW COUNT AND JOIN SAFETY ==============================
-- Exactly one row per fact_daily_kpi row OF AN ENABLED PROJECT (the gate above is the only
-- filter; it never drops a row of an enabled project). Every joined relation is
-- pre-aggregated to at most one row per join key: the published-meaning flag per project,
-- the binding per (project_id, connector), the datastream roll-up per
-- (project_id, connector), and the governed country index per (project_id, country_code)
-- -- unique because the Country hierarchy enforces one effective home per value inside one
-- version (migration 140's uq_master_data_membership_open_value).
-- Asserted by the combination-of-columns test in schema_fee_tax_bridge.yml.
--
-- ===================== ADAPTER PORTABILITY ====================================
-- Every construct here is accepted by both DuckDB and BigQuery: no postfix ::, no DOUBLE,
-- no VARCHAR, no TEXT, no parameterised DECIMAL / NUMERIC, no QUALIFY, no regexp_* and no
-- CONCAT_WS (which BigQuery does not have). The two-letter check is LENGTH + STRPOS +
-- SUBSTR and the gap_code join is || + two-argument TRIM, both portable.
--
-- THERE IS NO LONGER AN ADAPTER BRANCH, and that is a consequence of Story 37.9 rather
-- than a separate cleanup. The model carried ONE `target.type == 'duckdb'` exception
-- because Epic 37's market list arrived as `project_preferences.local_markets` JSON and
-- needed `from_json` / `UNNEST`, which is a hard parse failure on BigQuery; the whole
-- model was therefore a DELIBERATE NO-OP outside DuckDB and had never been executed
-- anywhere else. `mirror.country_market_projection` lands flat scalars, so there is no
-- JSON to unnest and the model now compiles and means the same thing on both adapters.
-- C.8 decision 1's claim that flattening "deletes ALL JSON portability risk from the dbt
-- engine" is true of this model again.
--
-- AI-63: the per-org dbt run (Story 24.4) MUST include this model, otherwise 41.3's
-- LEFT JOIN resolves against a missing relation for that org.
--
-- Deployment order (C.8/11): migrations 119 and 269 -> mirror_sync -> dbt build.

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
      'country_market_projection',
      'datastream_country_binding_dim',
      'datastream_source_types',
      'datastreams_dim',
      'project_tax_fee_activation',
    ]) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['date', 'date'],
    ['connector', 'string'],
    ['metric', 'string'],
    ['breakdown_dimension', 'string'],
    ['breakdown_value', 'string'],
    ['attr_country', 'string'],
    ['attr_market', 'string'],
    ['attr_source_type', 'string'],
    ['resolution_source', 'string'],
    ['country_gap_reason', 'string'],
    ['market_gap_reason', 'string'],
    ['source_type_gap_reason', 'string'],
    ['gap_code', 'string'],
]) }}
{%- else %}

-- The canonical country partition. Overridable exactly like the Python side's
-- TOOROW_COUNTRY_DIMENSION, so a deployment that renames its country partition renames it
-- in both engines. This is a DIMENSION NAME, not a country.
{% set country_partition = var('country_partition', 'country') %}

WITH

-- ------------------------------------------------- GOVERNED COUNTRY MEANING ---
-- Story 37.9: the Project's PUBLISHED country -> market meaning, the same rows
-- `country_registry.load_projection` builds for Analyze. See "GEOGRAPHIC AUTHORITY"
-- in the header for why this replaced project_preferences outright.
--
-- The tracked set is `market_kind = 'market'`. Rest of World members are governed and
-- resolved, but they are NOT a tracked reporting market: excluding them here is what
-- makes a row landing in the catch-all say `country_outside_tracked_markets` and a
-- binding naming the catch-all say `binding_not_bindable` -- the same boundary the
-- retired posture drew with its synthetic ids, now drawn by a governed node kind.
tracked_countries AS (
    SELECT
        cp.project_id                            AS project_id,
        cp.market_id                             AS market_id,
        UPPER(TRIM(CAST(cp.country_code AS STRING))) AS country_code
    FROM {{ source('mirror', 'country_market_projection') }} cp
    WHERE cp.market_kind = 'market'
      AND cp.market_id IS NOT NULL
      AND cp.country_code IS NOT NULL
      AND TRIM(CAST(cp.country_code AS STRING)) <> ''
),

-- "This Project has published a Country meaning we can infer from." Its ABSENCE is
-- never Global: it is "cannot decide", and rung 4 says so in its own words.
governed_projects AS (
    SELECT DISTINCT tc.project_id AS project_id
    FROM tracked_countries tc
),

-- ------------------------------------------------------------- MODULE GATE ---
-- Contract C.1: OFF => zero rows. An absent preferences row is OFF too, so this is an
-- INNER join below, never a LEFT one with a COALESCE.
enabled_projects AS (
    -- Story 48.4: one activation authority. See the note in fee_tax_rules_effective:
    -- project_preferences.fee_tax_alignment_enabled was a second boolean, and it is
    -- now a projection that cannot be written directly.
    SELECT a.project_id AS project_id
    FROM {{ source('mirror', 'project_tax_fee_activation') }} a
    WHERE COALESCE(CAST(a.tax_fees_active AS BOOLEAN), FALSE)
),

-- How many countries the project tracks IN TOTAL (the union across markets, never the
-- market count). One market may hold several countries, and counting markets would
-- resolve a multi-country project to an arbitrary code.
governed_country_count AS (
    SELECT
        tc.project_id                        AS project_id,
        COUNT(DISTINCT tc.country_code)      AS tracked_country_count
    FROM tracked_countries tc
    GROUP BY tc.project_id
),

-- Rung 3's input: the single tracked country and its owning market, and NOTHING when the
-- project tracks zero or two-or-more. We never guess between two countries.
governed_single_country AS (
    SELECT
        tc.project_id            AS project_id,
        MIN(tc.country_code)     AS country_code,
        MIN(tc.market_id)        AS market_id
    FROM tracked_countries tc
    JOIN governed_country_count n
      ON n.project_id = tc.project_id
     AND n.tracked_country_count = 1
    GROUP BY tc.project_id
),

-- Members per declared market: 1 => the binding resolves a country too, >= 2 => D3.
-- A binding naming an id absent from this relation -- a Rest of World node, an archived
-- market, a grouping this version no longer knows -- has NO member count, which is the
-- `binding_not_bindable` branch below. That is why no synthetic market id is named
-- anywhere in this model any more.
market_member_counts AS (
    SELECT
        tc.project_id                       AS project_id,
        tc.market_id                        AS market_id,
        COUNT(DISTINCT tc.country_code)     AS member_count,
        MIN(tc.country_code)                AS single_country_code
    FROM tracked_countries tc
    GROUP BY tc.project_id, tc.market_id
),

-- ---------------------------------------------------------- DECLARED BINDINGS ---
-- EMPTY IS NOT BROKEN: migration 104 is unapplied and nothing writes a
-- binding_kind='datastream' row yet, so this relation is legitimately empty today.
declared_bindings AS (
    SELECT
        b.project_id                        AS project_id,
        b.connector                         AS connector,
        COUNT(DISTINCT b.market_id)         AS bound_market_count,
        MIN(b.market_id)                    AS bound_market_id
    FROM {{ source('mirror', 'datastream_country_binding_dim') }} b
    WHERE b.market_id IS NOT NULL
      AND b.connector IS NOT NULL
    GROUP BY b.project_id, b.connector
),

binding_resolution AS (
    SELECT
        db.project_id                       AS project_id,
        db.connector                        AS connector,
        db.bound_market_count               AS bound_market_count,
        db.bound_market_id                  AS bound_market_id,
        mmc.member_count                    AS bound_member_count,
        mmc.single_country_code             AS bound_single_country
    FROM declared_bindings db
    LEFT JOIN market_member_counts mmc
      ON mmc.project_id = db.project_id
     AND mmc.market_id  = db.bound_market_id
),

-- ----------------------------------------------------------------- SOURCE TYPE ---
-- fact_daily_kpi carries no datastream_id, so the connector is mapped to its
-- datastream(s) through mirror.datastreams_dim. Two datastreams of one connector that do
-- not resolve to the SAME source type is an AMBIGUITY -- a gap, never a pick.
connector_source_types AS (
    SELECT
        d.project_id                             AS project_id,
        d.connector                              AS connector,
        COUNT(*)                                 AS datastream_count,
        COUNT(st.source_type)                    AS declared_count,
        COUNT(DISTINCT st.source_type)           AS distinct_source_type_count,
        MIN(st.source_type)                      AS single_source_type
    FROM {{ source('mirror', 'datastreams_dim') }} d
    LEFT JOIN {{ source('mirror', 'datastream_source_types') }} st
      ON st.datastream_id = d.datastream_id
    WHERE d.connector IS NOT NULL
    GROUP BY d.project_id, d.connector
),

-- ------------------------------------------------------------------ FACT ROWS ---
-- The module gate (C.1) is this INNER JOIN: a project with the flag FALSE, or with no
-- preferences row at all, contributes NO row to this model.
fact_rows AS (
    SELECT
        f.project_id                                                AS project_id,
        CAST(f.date AS DATE)                                        AS date,
        f.connector                                                 AS connector,
        f.metric                                                    AS metric,
        f.breakdown_dimension                                       AS breakdown_dimension,
        f.breakdown_value                                           AS breakdown_value,
        -- EXACT match on the canonical partition: a composite sub-dimension such as
        -- '<country partition>>device' is NOT a country row, and reading its value as a
        -- code would invent data.
        CASE
            WHEN f.breakdown_dimension = '{{ country_partition }}'
            THEN UPPER(TRIM(CAST(f.breakdown_value AS STRING)))
        END                                                         AS row_country_candidate
    FROM {{ ref('fact_daily_kpi') }} f
    JOIN enabled_projects e
      ON e.project_id = f.project_id
),

-- The canonical code set is the same governed seed the Python bridge reads.  A shape
-- check accepted `ZZ` as a country even though it is not assigned; membership in this
-- relation is the vocabulary boundary.  Client-specific aliases remain governed by the
-- 37.9 MDM layer and are intentionally not guessed here.
country_vocabulary AS (
    SELECT DISTINCT UPPER(TRIM(CAST(iso_code AS STRING))) AS iso_code
    FROM {{ ref('dim_country') }}
),

row_attributes AS (
    SELECT
        fr.project_id                       AS project_id,
        fr.date                             AS date,
        fr.connector                        AS connector,
        fr.metric                           AS metric,
        fr.breakdown_dimension              AS breakdown_dimension,
        fr.breakdown_value                  AS breakdown_value,
        CASE
            WHEN cv.iso_code IS NOT NULL
            THEN fr.row_country_candidate
        END                                 AS row_country,
        CASE
            WHEN fr.row_country_candidate IS NULL
            THEN FALSE
            -- Story 58.5: the declared no-country bucket is NOT an unreadable value.
            -- It reaches here uppercased by rung 1's TRIM/UPPER, and without this
            -- branch it falls into `row_country_unmapped` and the row is told to go
            -- and MAP a value -- on a surface where there is nothing to map, because
            -- the provider reported nothing. Same gap, wrong instruction.
            WHEN fr.row_country_candidate = UPPER({{ country_absent_sentinel() }})
            THEN FALSE
            WHEN cv.iso_code IS NOT NULL
            THEN FALSE
            ELSE TRUE
        END                                 AS row_country_unmapped,
        -- Its own flag, because it earns its own rung and its own word.
        CASE
            WHEN fr.row_country_candidate = UPPER({{ country_absent_sentinel() }})
            THEN TRUE
            ELSE FALSE
        END                                 AS row_country_absent
    FROM fact_rows fr
    LEFT JOIN country_vocabulary cv
      ON cv.iso_code = fr.row_country_candidate
),

-- --------------------------------------------------------------- LADDER INPUTS ---
ladder AS (
    SELECT
        ra.project_id                                   AS project_id,
        ra.date                                         AS date,
        ra.connector                                    AS connector,
        ra.metric                                       AS metric,
        ra.breakdown_dimension                          AS breakdown_dimension,
        ra.breakdown_value                              AS breakdown_value,
        ra.row_country                                  AS row_country,
        ra.row_country_unmapped                         AS row_country_unmapped,
        ra.row_country_absent                           AS row_country_absent,
        rm.market_id                                    AS row_market_id,
        -- The Project has a governed Country meaning to reason from. Its absence is
        -- "cannot decide", never Global -- which is why this is a flag and not a mode.
        CASE WHEN gp.project_id IS NULL THEN FALSE ELSE TRUE END
                                                        AS country_governed,
        COALESCE(br.bound_market_count, 0)              AS bound_market_count,
        br.bound_market_id                              AS bound_market_id,
        br.bound_member_count                           AS bound_member_count,
        br.bound_single_country                         AS bound_single_country,
        gs.country_code                                 AS posture_country,
        gs.market_id                                    AS posture_market_id,
        cst.datastream_count                            AS datastream_count,
        cst.declared_count                              AS declared_source_type_count,
        cst.distinct_source_type_count                  AS distinct_source_type_count,
        cst.single_source_type                          AS single_source_type
    FROM row_attributes ra
    LEFT JOIN governed_projects gp
      ON gp.project_id = ra.project_id
    LEFT JOIN tracked_countries rm
      ON rm.project_id   = ra.project_id
     AND rm.country_code = ra.row_country
    LEFT JOIN binding_resolution br
      ON br.project_id = ra.project_id
     AND br.connector  = ra.connector
    LEFT JOIN governed_single_country gs
      ON gs.project_id = ra.project_id
    LEFT JOIN connector_source_types cst
      ON cst.project_id = ra.project_id
     AND cst.connector  = ra.connector
),

-- ------------------------------------------------------------------- VERDICTS ---
-- One rung per row, in ladder order. The two CONTRADICTION rungs (an ambiguous binding
-- and a binding to a non-budgetable grouping) sit ABOVE the inference rung on purpose:
-- an inference may fill a silence, it may never overrule a contradiction.
--
-- Story 37.9: the two synthetic ids '__other_markets__' and '__unknown_market__' are
-- GONE from this model. They were the retired posture's computed buckets; a binding to
-- a governed Rest of World node, to an archived market or to an id this version no
-- longer knows is now caught by `bound_member_count IS NULL` alone -- because
-- `tracked_countries` carries markets only. One test instead of a hard-coded name list,
-- and no bucket id in the SQL (AD-2 / E41-NFR05).
classified AS (
    SELECT
        l.*,
        CASE
            WHEN l.row_country IS NOT NULL
                THEN 'row_dimension'
            WHEN l.country_governed AND l.bound_market_count >= 2
                THEN 'gap_binding_ambiguous'
            WHEN l.country_governed AND l.bound_market_count = 1
             AND l.bound_member_count IS NULL
                THEN 'gap_binding_not_bindable'
            WHEN l.country_governed AND l.bound_market_count = 1
             AND l.bound_member_count = 1
                THEN 'declared_binding'
            WHEN l.country_governed AND l.bound_market_count = 1
                THEN 'gap_market_not_single_country'
            -- Story 58.5: BEFORE the posture rung. A row whose source reported no
            -- country must not inherit the project's single tracked country -- that
            -- would attribute rows the provider explicitly did not attribute, which
            -- is the fabrication the whole ladder exists to avoid. It is a gap, and
            -- it is the gap that says the provider is the door, not conformance.
            WHEN l.row_country_absent
                THEN 'gap_country_absent_at_source'
            WHEN NOT l.row_country_unmapped AND l.posture_country IS NOT NULL
                THEN 'project_posture'
            WHEN l.row_country_unmapped
                THEN 'gap_country_value_unmapped'
            -- Story 37.9: NOT "the posture is Global". Either the Project published no
            -- Country hierarchy version, or the one it published defines no tracked
            -- market -- and in both cases the repair is the Country model, not a
            -- preference nobody can edit any more.
            WHEN NOT l.country_governed
                THEN 'gap_country_model_absent'
            ELSE 'gap_multiple_countries_governed'
        END                                     AS rung,
        CASE
            WHEN l.datastream_count IS NULL
                THEN 'gap_no_datastream'
            WHEN l.distinct_source_type_count >= 2
                THEN 'gap_source_type_ambiguous'
            WHEN l.declared_source_type_count = 0
                THEN 'gap_source_type_not_declared'
            WHEN l.declared_source_type_count < l.datastream_count
                THEN 'gap_source_type_ambiguous'
            ELSE 'source_type_declared'
        END                                     AS source_type_rung
    FROM ladder l
),

-- --------------------------------------------------------------- RESOLUTIONS ----
resolved AS (
    SELECT
        c.project_id                            AS project_id,
        c.date                                  AS date,
        c.connector                             AS connector,
        c.metric                                AS metric,
        c.breakdown_dimension                   AS breakdown_dimension,
        c.breakdown_value                       AS breakdown_value,
        -- attr_country is NULL whenever the country did not resolve. NEVER '' and never a
        -- placeholder: a blank would read as a value.
        CASE c.rung
            WHEN 'row_dimension'     THEN c.row_country
            WHEN 'declared_binding'  THEN c.bound_single_country
            WHEN 'project_posture'   THEN c.posture_country
        END                                     AS attr_country,
        -- attr_market may be non-NULL while attr_country is NULL (the D3 case below).
        CASE c.rung
            WHEN 'row_dimension'                  THEN c.row_market_id
            WHEN 'declared_binding'               THEN c.bound_market_id
            WHEN 'gap_market_not_single_country'  THEN c.bound_market_id
            WHEN 'project_posture'                THEN c.posture_market_id
        END                                     AS attr_market,
        CASE
            WHEN c.source_type_rung = 'source_type_declared' THEN c.single_source_type
        END                                     AS attr_source_type,
        -- Load-bearing provenance (AD-9 / AC14): non-empty IF AND ONLY IF the country
        -- resolved. A drill-down uses it to answer "how was this country known".
        CASE
            WHEN c.rung IN ('row_dimension', 'declared_binding', 'project_posture')
            THEN c.rung
            ELSE ''
        END                                     AS resolution_source,
        CASE c.rung
            WHEN 'gap_binding_ambiguous'          THEN 'binding_ambiguous_for_connector'
            WHEN 'gap_binding_not_bindable'       THEN 'binding_not_bindable'
            WHEN 'gap_market_not_single_country'  THEN 'market_not_single_country'
            WHEN 'gap_country_value_unmapped'     THEN 'country_value_unmapped'
            -- Story 58.5: the provider reported no country. There is nothing to map,
            -- so the word must not send anyone to the conformance surface.
            WHEN 'gap_country_absent_at_source'    THEN 'country_absent_at_source'
            WHEN 'gap_country_model_absent'       THEN 'no_binding_and_country_model_absent'
            WHEN 'gap_multiple_countries_governed'
                THEN 'no_binding_and_multiple_countries_governed'
            ELSE ''
        END                                     AS country_gap_reason,
        CASE
            -- rung 1 resolved a country no tracked market owns: the COUNTRY is resolved,
            -- only the market is not.
            WHEN c.rung = 'row_dimension' AND c.row_market_id IS NULL
                THEN 'country_outside_tracked_markets'
            WHEN c.rung IN ('row_dimension', 'declared_binding', 'project_posture',
                            'gap_market_not_single_country')
                THEN ''
            WHEN c.rung = 'gap_binding_ambiguous'
                THEN 'binding_ambiguous_for_connector'
            WHEN c.rung = 'gap_binding_not_bindable'
                THEN 'binding_not_bindable'
            WHEN c.rung = 'gap_country_value_unmapped'
                THEN 'country_value_unmapped'
            WHEN c.rung = 'gap_country_absent_at_source'
                THEN 'country_absent_at_source'
            WHEN c.rung = 'gap_country_model_absent'
                THEN 'no_binding_and_country_model_absent'
            ELSE 'no_binding_and_multiple_countries_governed'
        END                                     AS market_gap_reason,
        -- 'signals_disagree' is RESERVED for a declaration and a derivation that genuinely
        -- conflict. This model cannot observe that (no derivation in the warehouse), so it
        -- never emits it: the undeclared case says 'source_type_not_declared', because that
        -- is what actually happened.
        CASE c.source_type_rung
            WHEN 'gap_no_datastream'            THEN 'no_datastream_for_connector'
            WHEN 'gap_source_type_ambiguous'    THEN 'source_type_ambiguous_for_connector'
            WHEN 'gap_source_type_not_declared' THEN 'source_type_not_declared'
            ELSE ''
        END                                     AS source_type_gap_reason
    FROM classified c
)

-- ----------------------------------------------------------------- OUTPUT -------
-- The FROZEN 14-column contract of section D.2. gap_code is the '|'-joined, sorted,
-- distinct set of the gap codes raised on this row (the repo's '|'-join convention);
-- '' when the row raised none. Alphabetical order is guaranteed by construction:
-- COUNTRY_UNRESOLVED < MARKET_UNRESOLVED < SOURCE_TYPE_UNRESOLVED.
-- Built with || and a two-argument TRIM rather than CONCAT_WS, which BigQuery does not
-- have. The trailing separator each present code carries is what TRIM removes, so one
-- code alone, any pair, all three, and none all render correctly.
SELECT
    r.project_id                                            AS project_id,
    r.date                                                  AS date,
    r.connector                                             AS connector,
    r.metric                                                AS metric,
    r.breakdown_dimension                                   AS breakdown_dimension,
    r.breakdown_value                                       AS breakdown_value,
    r.attr_country                                          AS attr_country,
    r.attr_market                                           AS attr_market,
    r.attr_source_type                                      AS attr_source_type,
    r.resolution_source                                     AS resolution_source,
    r.country_gap_reason                                    AS country_gap_reason,
    r.market_gap_reason                                     AS market_gap_reason,
    r.source_type_gap_reason                                AS source_type_gap_reason,
    TRIM(
        CASE WHEN r.country_gap_reason <> '' THEN 'COUNTRY_UNRESOLVED|' ELSE '' END
     || CASE WHEN r.market_gap_reason <> '' THEN 'MARKET_UNRESOLVED|' ELSE '' END
     || CASE WHEN r.source_type_gap_reason <> '' THEN 'SOURCE_TYPE_UNRESOLVED' ELSE '' END,
        '|'
    )                                                       AS gap_code
FROM resolved r
{%- endif -%}
