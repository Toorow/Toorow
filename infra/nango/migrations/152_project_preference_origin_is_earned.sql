-- 152 — A Project default is born a `default`, not a `suggestion`.
--
-- Why (AI-77 / AI-81, review-epic-48.md C-3)
-- ------------------------------------------
-- Migration 131 declared:
--
--     canonical_currency_origin TEXT NOT NULL DEFAULT 'suggestion'
--     reporting_timezone_origin TEXT NOT NULL DEFAULT 'suggestion'
--
-- so any writer that omitted the column produced a row claiming a source had
-- suggested the value. Nothing had. Four Project-creation paths then made it
-- worse by passing the literal explicitly, and a test pinned the DDL default in
-- place as if it were the contract.
--
-- `server/core/project_provenance.py` is now the single decision point and every
-- creation path passes the origin explicitly, so this default no longer bites in
-- practice. It is corrected anyway, because a column default is a trap for the
-- next writer: the honest fallback for "nobody said anything" is `default`.
--
-- Migration 131 is applied and immutable, so this corrects it forward.
--
-- The CHECK is added last and deliberately: it is what makes an unearned
-- provenance impossible at the storage layer, not merely unlikely in Python.
-- `app.project_preferences` holds zero rows at the time of writing (verified on
-- 2026-07-31), so no backfill is required; the UPDATE below is written to be
-- correct if rows appear between authoring and application.

BEGIN;

ALTER TABLE app.project_preferences
    ALTER COLUMN canonical_currency_origin SET DEFAULT 'default';

ALTER TABLE app.project_preferences
    ALTER COLUMN reporting_timezone_origin SET DEFAULT 'default';

-- Any row still carrying the unearned label, and whose value is the platform
-- fallback, is restated as what it actually is. A row whose value differs from
-- the fallback was chosen by someone and is left alone -- relabelling it
-- `default` would destroy information, which is the opposite of the repair.
UPDATE app.project_preferences
   SET canonical_currency_origin = 'default'
 WHERE canonical_currency_origin = 'suggestion'
   AND canonical_currency = 'EUR';

UPDATE app.project_preferences
   SET reporting_timezone_origin = 'default'
 WHERE reporting_timezone_origin = 'suggestion'
   AND reporting_timezone = 'Europe/Paris';

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_currency_origin;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_currency_origin
    CHECK (canonical_currency_origin IN ('operator', 'suggestion', 'default'));

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS chk_project_preferences_timezone_origin;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT chk_project_preferences_timezone_origin
    CHECK (reporting_timezone_origin IN ('operator', 'suggestion', 'default'));

COMMENT ON COLUMN app.project_preferences.canonical_currency_origin IS
    'How this value was earned: operator (a human sent it), suggestion (a '
    'connected source declared it -- requires evidence), default (nobody said '
    'anything and the column is NOT NULL). Decided by core.project_provenance.';

COMMENT ON COLUMN app.project_preferences.reporting_timezone_origin IS
    'How this value was earned: operator, suggestion (evidence required) or '
    'default. Decided by core.project_provenance.';

COMMIT;
