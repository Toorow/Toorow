-- Story 37.8: a tracked market becomes a stable id + label + one or more ISO codes.
-- Additive, idempotent, and backward compatible: projects already configured with
-- flat country codes are backfilled as the equivalent SINGLE-COUNTRY markets, with
-- id AND label equal to the ISO code, so their reporting output is unchanged.
--
-- The platform holds no opinion on market composition: this migration never adds,
-- merges, or suggests a territory, and seeds no preset. It only re-expresses what
-- the operator already chose in the richer shape.

BEGIN;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS local_markets JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Compatibility backfill: one market per already-tracked code.
UPDATE app.project_preferences
SET local_markets = COALESCE(
        (
            SELECT jsonb_agg(
                       jsonb_build_object(
                           'id', code,
                           'label', code,
                           'country_codes', jsonb_build_array(code)
                       )
                       ORDER BY code
                   )
            FROM unnest(local_market_country_codes) AS code
        ),
        '[]'::jsonb
    )
WHERE geographic_mode = 'local_markets'
  AND jsonb_array_length(local_markets) = 0
  AND cardinality(local_market_country_codes) > 0;

-- CHECK constraints cannot contain subqueries, so the market invariants live in
-- an IMMUTABLE helper. Only the invariants the platform legitimately imposes are
-- encoded: shape, stable unique ids, and disjunction. Composition is never
-- constrained -- any member list satisfying these is accepted without judgement.
CREATE OR REPLACE FUNCTION app.geographic_markets_valid(markets JSONB)
RETURNS BOOLEAN
LANGUAGE sql
IMMUTABLE
AS $$
    SELECT
        jsonb_typeof(markets) = 'array'
        -- Shape: non-empty id, non-empty label, at least one country code.
        AND NOT EXISTS (
            SELECT 1
            FROM jsonb_array_elements(markets) AS market
            WHERE jsonb_typeof(market) <> 'object'
               OR COALESCE(market->>'id', '') = ''
               OR COALESCE(market->>'label', '') = ''
               OR jsonb_typeof(market->'country_codes') <> 'array'
               OR jsonb_array_length(market->'country_codes') = 0
        )
        -- Stable identity: market ids are unique inside a project.
        AND (
            SELECT COUNT(DISTINCT lower(market->>'id'))
            FROM jsonb_array_elements(markets) AS market
        ) = jsonb_array_length(markets)
        -- Disjunction: a country code belongs to at most one market. Without
        -- this, additive reconciliation double-counts.
        AND (
            SELECT COUNT(DISTINCT upper(code))
            FROM jsonb_array_elements(markets) AS market,
                 jsonb_array_elements_text(market->'country_codes') AS code
        ) = (
            SELECT COUNT(*)
            FROM jsonb_array_elements(markets) AS market,
                 jsonb_array_elements_text(market->'country_codes') AS code
        );
$$;

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_local_markets;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_local_markets
    CHECK (app.geographic_markets_valid(local_markets));

-- Aggregate invariants: non-emptiness on each side of the mode.
ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_geographic_aggregate;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_geographic_aggregate
    CHECK (
        (
            geographic_mode = 'global'
            AND cardinality(local_market_country_codes) = 0
            AND jsonb_array_length(local_markets) = 0
        )
        OR
        (
            geographic_mode = 'local_markets'
            AND cardinality(local_market_country_codes) > 0
            AND jsonb_array_length(local_markets) > 0
        )
    );

COMMIT;
