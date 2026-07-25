-- Story 37.1: project-level Global versus Local markets posture (CAP-27).
-- Additive and idempotent. Postgres owns preferences (AD-8).

BEGIN;

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS geographic_mode TEXT NOT NULL DEFAULT 'global';

ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS local_market_country_codes TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[];

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_geographic_mode;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_geographic_mode
    CHECK (geographic_mode IN ('global', 'local_markets'));

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_geographic_aggregate;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_geographic_aggregate
    CHECK (
        (geographic_mode = 'global' AND cardinality(local_market_country_codes) = 0)
        OR
        (
            geographic_mode = 'local_markets'
            AND cardinality(local_market_country_codes) > 0
        )
    );

COMMIT;
