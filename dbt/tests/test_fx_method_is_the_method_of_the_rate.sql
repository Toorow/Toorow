-- Singular test (0 rows = PASS).
--
-- A POSED RATE MAY NOT WEAR THE WORD OF AN OBSERVED ONE.
--
-- `dbt/seeds/fx_rates.csv` holds a rate a human wrote by hand (USD->EUR at 0.92,
-- open from 2020 to 2099). Until the amendment of 2026-08-17 to
-- docs/product-architecture/alignment-register.md, `money_evidence.sql` stamped
-- `fx_method = 'direct'` on it -- `direct` being the word core.fx_rate_sets
-- reserves for an OBSERVED quotation. The number was disclosed (`fx_source='seed'`)
-- and the method label was not true of it, which is worse than saying nothing: a
-- plausible figure under a method that lies reads as measured.
--
-- The amendment makes the fixed value a first-class method with its OWN label, so
-- this test states the two halves that keep it honest:
--
--   1. no row sourced from the seed claims `direct`, `triangulated` or
--      `carry_forward` -- the three words that assert an observation this row
--      never had;
--   2. every converted row names a method at all, and it is one of the governed
--      words (core.fx_rate_sets.METHOD_*), never a NULL and never an invention.
--
-- Cardinality-guarded on both sides: without a converted seed row, and without a
-- converted row at all, every assertion below is a zero-row pass.

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
    ['failure_reason', 'string'],
    ['connector', 'string'],
    ['metric', 'string'],
    ['fx_method', 'string'],
]) }}
{%- else %}

WITH converted_rows AS (
    SELECT
        connector,
        metric,
        fx_source,
        fx_method
    FROM {{ ref('fact_daily_kpi') }}
    WHERE fx_rate IS NOT NULL
),

seed_rows AS (
    SELECT * FROM converted_rows WHERE fx_source = 'seed'
),

cardinality_check AS (
    SELECT
        (SELECT COUNT(*) FROM converted_rows) AS converted_count,
        (SELECT COUNT(*) FROM seed_rows)      AS seed_count
)

SELECT
    'cardinality_failure_no_converted_row' AS failure_reason,
    NULL AS connector,
    NULL AS metric,
    NULL AS fx_method
FROM cardinality_check
WHERE converted_count = 0

UNION ALL

SELECT
    'cardinality_failure_no_seed_sourced_row' AS failure_reason,
    NULL AS connector,
    NULL AS metric,
    NULL AS fx_method
FROM cardinality_check
WHERE seed_count = 0

UNION ALL

-- 1. The seed cannot claim an observation.
SELECT
    'seed_rate_claims_an_observed_method' AS failure_reason,
    connector,
    metric,
    fx_method
FROM seed_rows
WHERE fx_method IN ('direct', 'triangulated', 'carry_forward')

UNION ALL

-- 2. Every converted row names a governed method.
SELECT
    'converted_row_names_no_governed_method' AS failure_reason,
    connector,
    metric,
    fx_method
FROM converted_rows
WHERE fx_method IS NULL
   OR fx_method NOT IN ('fixed', 'direct', 'triangulated', 'carry_forward', 'identity')
{%- endif -%}
