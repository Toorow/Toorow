-- Story 37.9: the used-by guard on markets.
--
-- A market is project MASTER DATA. Budgets, objectives and saved reports bind to
-- a market ID (never to a raw country code -- that is what makes a relabel
-- non-breaking, cf. resolve_market_binding delivered by Story 37.8). Deleting a
-- market, or moving a country out of one, therefore changes the meaning of
-- figures that were already published under that id.
--
-- This table is the REGISTRY the guard reads: whoever binds a market id declares
-- it here, and the governed change flow (app.geographic_change_previews ->
-- confirm) must STATE what depends on a market before the change is accepted.
--
-- AD-2: binding_kind is free TEXT written by the caller. The platform ships NO
-- enumeration of binding kinds and NO market composition, preset or alias --
-- Story 37.9's anti-hardcode rule applies to this migration too: it seeds
-- NOTHING.
--
-- The market diff itself is audited in the existing append-only registry
-- (app.audit_log, action project.geographic_change.confirmed) -- no second audit
-- table is created here, for the same reason 27.4 reused 049's.
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the whole file replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
--
-- Numbering verified: 101, 102 and 103 are taken; 104 is the first free.

BEGIN;

CREATE TABLE IF NOT EXISTS app.market_bindings (
    id             TEXT        PRIMARY KEY,
    project_id     TEXT        NOT NULL REFERENCES app.projects(id) ON DELETE CASCADE,
    market_id      TEXT        NOT NULL,
    -- Free text on purpose (AD-2). Examples of what a caller may write are
    -- deliberately NOT enumerated here: an enum would become a platform opinion
    -- on which surfaces are allowed to bind a market.
    binding_kind   TEXT        NOT NULL,
    binding_id     TEXT        NOT NULL,
    binding_label  TEXT,
    created_by     TEXT        NOT NULL DEFAULT 'system',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_market_bindings_market_id  CHECK (btrim(market_id) <> ''),
    CONSTRAINT chk_market_bindings_kind       CHECK (btrim(binding_kind) <> ''),
    CONSTRAINT chk_market_bindings_binding_id CHECK (btrim(binding_id) <> '')
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_market_bindings_identity
    ON app.market_bindings (project_id, binding_kind, binding_id, market_id);

CREATE INDEX IF NOT EXISTS ix_market_bindings_market
    ON app.market_bindings (project_id, market_id);

COMMENT ON TABLE app.market_bindings IS
    'Story 37.9: registry of what binds a project market id (budgets, objectives, saved reports...). Read by the used-by guard so deleting a market or moving a country out of it STATES its dependents before the governed change is accepted. Seeds nothing; binding_kind is caller data (AD-2).';

COMMIT;
