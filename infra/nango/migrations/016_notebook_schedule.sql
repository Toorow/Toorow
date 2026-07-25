BEGIN;

-- Story 6.6 (AC1, AC3, AC7): notebook scheduling and sharing columns.
-- Combined into one migration for simplicity (AC7 dev decision documented
-- in Dev Agent Record).

-- AC1 (AC7): scheduling columns
ALTER TABLE app.notebooks
    ADD COLUMN IF NOT EXISTS scheduled       BOOLEAN     NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS schedule_rule   TEXT
        CONSTRAINT schedule_rule_check CHECK (
            (scheduled = FALSE AND schedule_rule IS NULL)
            OR (scheduled = TRUE AND schedule_rule = 'nightly')
        );

-- AC3: share token columns
-- share_token: NULL = not shared; 32-char URL-safe token (192-bit entropy)
-- stored plain (P0 decision — see Dev Agent Record; hashing deferred to Epic 7)
ALTER TABLE app.notebooks
    ADD COLUMN IF NOT EXISTS share_token TEXT UNIQUE,    -- NULL = not shared
    ADD COLUMN IF NOT EXISTS shared_at   TIMESTAMPTZ;   -- when sharing was enabled

COMMIT;
