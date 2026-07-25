-- infra/nango/migrations/036_org_branding_user_profiles.sql
--
-- Story 21.2: Org branding + global user profiles (Epic 21, FR37/CAP-25).
--
-- Adds a small brand identity to app.organizations (3 colors + a logo) and a
-- global per-identity profile table (display name, email, avatar). The profile
-- is keyed on the AD-14 identity subject -- NOT on a Google connection: the
-- platform's Google OAuth is a DATA-access flow that does not request identity
-- scopes (google_oauth.py: "authorizes DATA access, not identity"), so avatars
-- come from the identity provider / self-service, never from the data callback.
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/036_org_branding_user_profiles.sql
--
-- Schema-Change-Checklist: additive & idempotent; new columns NULLable;
-- DROP CONSTRAINT IF EXISTS before ADD (no IF NOT EXISTS form for ADD CONSTRAINT).
-- Verified: previous migration in this branch is 035 (organizations). This is 036.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- 1. Org branding columns (hex #RRGGBB, validated app-side; DB CHECK is
--    defence-in-depth). logo_url is a free URL/asset ref (no format CHECK).
-- ---------------------------------------------------------------------------
ALTER TABLE app.organizations ADD COLUMN IF NOT EXISTS brand_primary   TEXT;
ALTER TABLE app.organizations ADD COLUMN IF NOT EXISTS brand_secondary TEXT;
ALTER TABLE app.organizations ADD COLUMN IF NOT EXISTS brand_accent    TEXT;
ALTER TABLE app.organizations ADD COLUMN IF NOT EXISTS logo_url        TEXT;

ALTER TABLE app.organizations DROP CONSTRAINT IF EXISTS organizations_brand_primary_hex;
ALTER TABLE app.organizations ADD CONSTRAINT organizations_brand_primary_hex
    CHECK (brand_primary IS NULL OR brand_primary ~ '^#[0-9A-Fa-f]{6}$');

ALTER TABLE app.organizations DROP CONSTRAINT IF EXISTS organizations_brand_secondary_hex;
ALTER TABLE app.organizations ADD CONSTRAINT organizations_brand_secondary_hex
    CHECK (brand_secondary IS NULL OR brand_secondary ~ '^#[0-9A-Fa-f]{6}$');

ALTER TABLE app.organizations DROP CONSTRAINT IF EXISTS organizations_brand_accent_hex;
ALTER TABLE app.organizations ADD CONSTRAINT organizations_brand_accent_hex
    CHECK (brand_accent IS NULL OR brand_accent ~ '^#[0-9A-Fa-f]{6}$');

-- ---------------------------------------------------------------------------
-- 2. app.user_profiles -- global per-identity profile (AD-14 subject as PK).
--    One row per person; the same avatar/name follows them across all orgs.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.user_profiles (
    identity      TEXT        PRIMARY KEY,           -- opaque subject (AD-14)
    display_name  TEXT,
    email         TEXT,
    avatar_url    TEXT,
    avatar_source TEXT,                              -- e.g. 'self', 'google', 'gravatar'
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

DROP TRIGGER IF EXISTS trg_user_profiles_updated_at ON app.user_profiles;
CREATE TRIGGER trg_user_profiles_updated_at
    BEFORE UPDATE ON app.user_profiles
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMIT;
