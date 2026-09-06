-- 305 -- a posed rate reaches the relation that CONVERTS
--
-- Step 1 of the cutover ratified in
-- docs/product-architecture/capabilities/currency-fx.md, section
-- "Arbitration, 2026-08-21 -- the read path". Migrations 279 and 288 built the
-- write door: a rate a person POSES for a pair over a period, optionally
-- qualified by a condition, versioned, windowed, ranked by specificity, refusing
-- a tie. What no migration built is the half that makes a posed rate MEAN
-- something -- the read. Since 2026-08-17 the live conversion authority has been
-- `dbt/seeds/fx_rates.csv`, a two-line git-committed CSV, and `rate_freshness`
-- has been answering `state: current` over a store no converted figure reads.
--
-- WHY TWO VIEWS AND NOT ONE TABLE ACROSS THE MIRROR.
-- `core.mirror_sync` goes psycopg -> pandas -> DuckDB. A JSONB column lands as
-- whatever pandas infers from the rows it happens to see, and that inference
-- DIFFERS between a populated sync and an empty one -- so a warehouse built the
-- day before the first posed rate and a warehouse built the day after would not
-- have the same column type. The precedent is migration 119's
-- `app.fee_tax_rule_conditions_v`: flatten in Postgres, where the jsonb
-- semantics are exact, and let only scalars cross. Same shape here, same reason.
--
-- WHAT THE FIRST VIEW ALSO DOES, AND WHY IT BELONGS HERE.
-- `app.fx_posed_rates_v` does not project `app.fx_rate_observations` raw: it
-- resolves the ACTIVE VERSION first, exactly as
-- `core.fx_rate_sets._active_rate_set_version` does -- current version, else
-- last-known-good, status in ('validated', 'superseded'), newest as_of_date
-- wins. If the warehouse re-derived that selection it would be a THIRD
-- implementation of the rule, free to disagree with the application over which
-- version is live. One row per project per posed observation, already scoped to
-- the version that governs.
--
-- WHAT IT DELIBERATELY DOES NOT DO. No INSERT of any kind, no default rate, no
-- rate set. A project with no posed rate simply produces no row, and the
-- warehouse falls back to the seed -- which is what "the seed is the fallback"
-- means in step 3 of the cutover.
--
-- `method = 'fixed'` is the filter that keeps the two paths apart, and it is the
-- same predicate `core.fx_rate_sets._observation` uses in the negative
-- (`method <> 'fixed'`). An INGESTED quotation is not published here: it speaks
-- for its `effective_date` under a staleness policy this view carries no way to
-- evaluate, and serving one through the posed door would restore migration 279's
-- defect at the moment somebody read the number.
--
-- Numbering: 001..304 present on disk with no gaps; 305 is the first free
-- identifier. Regenerate the manifest with
--   python scripts/check_migration_catalog.py infra/nango/migrations --write-manifest

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The posed rates of the version that governs, scalars only.
--
-- Frozen contract (the mirror column list, and the dbt source declaration that
-- names it): project_id, observation_id, base_currency, quote_currency, rate,
-- effective_date, valid_from, valid_to, condition_key_count, provider,
-- rate_set_version_id.
--
-- `condition_key_count` is the SPECIFICITY of the rule, computed where the jsonb
-- lives. It is the whole precedence rule of this capability -- "the most
-- specific matching rule wins, specificity being the number of condition keys a
-- rule declares" -- and it is a derived integer, so it crosses the mirror as an
-- integer rather than as an object the warehouse would have to count.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW app.fx_posed_rates_v AS
WITH active AS (
    -- The version that governs, per project. DISTINCT ON is the set-returning
    -- form of core.fx_rate_sets._active_rate_set_version's
    -- `ORDER BY v.as_of_date DESC LIMIT 1`, asked for every project at once.
    SELECT DISTINCT ON (s.project_id)
           s.project_id,
           COALESCE(s.current_version_id, s.last_known_good_version_id) AS version_id,
           v.provider
    FROM app.fx_rate_sets s
    JOIN app.fx_rate_set_versions v
      ON v.id = COALESCE(s.current_version_id, s.last_known_good_version_id)
     AND v.project_id = s.project_id
    WHERE v.status IN ('validated', 'superseded')
    ORDER BY s.project_id, v.as_of_date DESC
)
SELECT a.project_id                                        AS project_id,
       o.id                                                AS observation_id,
       o.base_currency                                     AS base_currency,
       o.quote_currency                                    AS quote_currency,
       o.rate                                              AS rate,
       o.effective_date                                    AS effective_date,
       o.valid_from                                        AS valid_from,
       o.valid_to                                          AS valid_to,
       (SELECT count(*)::INTEGER
          FROM jsonb_object_keys(o.conditions) AS k)       AS condition_key_count,
       a.provider                                          AS provider,
       a.version_id                                        AS rate_set_version_id
FROM active a
JOIN app.fx_rate_observations o
  ON o.rate_set_version_id = a.version_id
 AND o.project_id = a.project_id
WHERE o.method = 'fixed'
  AND o.validation_status = 'validated'
  -- Both ends or neither: ck_fx_rate_observation_window already guarantees it,
  -- and naming both here keeps the view honest if that constraint is ever
  -- relaxed. A posed rate without a window is not a candidate -- it is the
  -- open-ended rate the governed method exists to replace.
  AND o.valid_from IS NOT NULL
  AND o.valid_to IS NOT NULL;

COMMENT ON VIEW app.fx_posed_rates_v IS
    'Story 67.13 (migration 305): the POSED rates of the version that governs '
    'each project, scalars only, for core.mirror_sync. One row per observation. '
    'condition_key_count is the specificity the warehouse ranks by; the values of '
    'the condition live in app.fx_posed_rate_conditions_v. Ingested quotations '
    'are excluded by method = ''fixed'' -- they carry a staleness policy this '
    'projection cannot evaluate.';

-- ---------------------------------------------------------------------------
-- 2. The condition, flattened one row per key x value.
--
-- Frozen contract: (observation_id, condition_key, condition_value).
--
-- Verbatim the shape of app.fee_tax_rule_conditions_v (migration 119), because
-- it is verbatim the same condition model: `{key: [values]}`, conjunction ACROSS
-- keys, disjunction ACROSS the values of one key. An observation whose
-- `conditions` is `{}` produces NO ROW here, which is what makes the
-- unconditional rate the fallback rather than a rival: the warehouse's join to
-- this relation is a LEFT JOIN, and no row means "unconstrained".
--
-- `jsonb_array_elements_text` cannot raise: `core.fx_fixed_rates` refuses a
-- condition whose value is not a non-empty list before the row is written, and
-- ck_fx_rate_observation_conditions holds the object shape.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE VIEW app.fx_posed_rate_conditions_v AS
SELECT o.id      AS observation_id,
       cond.key  AS condition_key,
       val.value AS condition_value
FROM app.fx_rate_observations o
CROSS JOIN LATERAL jsonb_each(o.conditions)              AS cond(key, value)
CROSS JOIN LATERAL jsonb_array_elements_text(cond.value) AS val(value)
WHERE o.method = 'fixed';

COMMENT ON VIEW app.fx_posed_rate_conditions_v IS
    'Story 67.13 (migration 305): the condition of a posed FX rate, one row per '
    'key x value -- the migration-119 shape, for the migration-119 reason (no '
    'jsonb crosses the mirror). An unconditional rate produces no row at all.';

COMMIT;
