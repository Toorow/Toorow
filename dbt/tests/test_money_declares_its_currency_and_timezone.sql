-- Story 48.3 -- MONEY WITHOUT A DECLARED CURRENCY IS THE FAILURE, NOT A NULL.
--
-- Migration 144 dropped the `'EUR'` and `'Europe/Paris'` column DEFAULTS on
-- app.project_preferences, and their NOT NULL with them. That was the point of
-- the story: a reporting currency must be CHOSEN, and a silent 'EUR' is one
-- client's currency applied to every other client's totals.
--
-- The consequence reached dbt and nobody saw it, because the build was not run:
-- `dim_project` carried `not_null` on both columns and started failing the
-- moment a Project existed that had not been configured yet. Restoring the
-- `not_null` would re-assert exactly the invariant the migration removed.
--
-- So the contract is restated as what it actually is. A NULL is legal and means
-- "not configured yet". What is forbidden is a Project that carries MONEY facts
-- while declaring neither the currency those amounts are in nor the timezone
-- their dates were labelled with -- because that row was computed against a
-- default that no longer exists, and nothing downstream can say in what
-- currency the number is.
--
-- A dbt singular test FAILS when it returns rows (zero rows = pass).

--
-- UN VERDICT NE SE DEPOSE PAS SUR UN COMPTE QUI N A PAS PU ETRE PRIS (AI-314,
-- 2026-08-24). La garde d anti-vacuite de ce test tire quand le MIROIR n est pas
-- dans cet entrepot : `mirror_sync` differe ses ecritures BigQuery (Phase B),
-- donc en production `dim_project` est bati VIDE, aucune ligne ne se convertit,
-- et le test rend son `CARDINALITY_FAIL` -- un rouge qui accuse le staging d un
-- defaut dont la cause est une PREMISSE ABSENTE. Un test rouge est un code de
-- sortie, et un code de sortie est un projet sans marts.
--
-- Il DECLINE donc de juger, en le DISANT (`TOOROW_SOURCE_ABSENT`, que le
-- nocturne lit et qui empeche le projet de rendre << ok >>). La ou le miroir EST
-- -- la boucle locale, la CI -- rien ne bouge et la garde reste armee.
{%- set mirror_missing = toorow_absent_sources('mirror', ['project_preferences']) -%}
{%- if mirror_missing | length > 0 -%}
{{ toorow_absent_source_stub('mirror', mirror_missing, [
    ['project_id', 'string'],
    ['failure_reason', 'string'],
]) }}
{%- else %}

WITH unconfigured AS (
    SELECT
        project_id,
        canonical_currency,
        reporting_timezone
    FROM {{ ref('dim_project') }}
    WHERE canonical_currency IS NULL
       OR reporting_timezone IS NULL
),

-- ANTI-VACUITY. If dim_project holds no Project at all, the anti-join below
-- passes by finding nothing and this test would report health it never
-- measured. Emptiness is reported as its own failure, in the shape every
-- Epic-41 test in this directory already uses.
cardinality_guard AS (
    SELECT
        CAST(NULL AS {{ toorow_string_type() }}) AS project_id,
        'CARDINALITY_FAIL: dim_project is empty, so the money-without-currency'
        || ' anti-join cannot fail. Seed the mirror before trusting this test.'
            AS failure_reason
    -- BigQuery refuses a WHERE with no FROM; DuckDB allows it. One constant row,
    -- accepted by both, keeps this guard a guard on either engine.
    FROM (SELECT 1) AS one_row
    WHERE (SELECT COUNT(*) FROM {{ ref('dim_project') }}) = 0
),

money_without_a_declaration AS (
    SELECT
        u.project_id,
        'UNDECLARED_MONEY_FAIL: project carries ' || CAST(COUNT(*) AS {{ toorow_string_type() }})
        || ' money fact rows while canonical_currency='
        || COALESCE(u.canonical_currency, 'NULL')
        || ' and reporting_timezone='
        || COALESCE(u.reporting_timezone, 'NULL')
        || ' -- the amounts were computed against a default that migration 144 removed'
            AS failure_reason
    FROM unconfigured u
    JOIN {{ ref('fact_daily_kpi') }} f
        ON f.project_id = u.project_id
    WHERE f.metric IN ('cost', 'revenue')
    GROUP BY u.project_id, u.canonical_currency, u.reporting_timezone
)

SELECT project_id, failure_reason FROM cardinality_guard
UNION ALL
SELECT project_id, failure_reason FROM money_without_a_declaration
{%- endif -%}
