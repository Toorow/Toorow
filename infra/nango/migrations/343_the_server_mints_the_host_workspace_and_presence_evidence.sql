-- 343 -- the server mints the host's workspace and presence evidence at preflight.
--
-- WHAT WAS MEASURED, 2026-09-04. `bind_host_connection` grants a high-risk MCP
-- profile only against a 64-hex `workspace_evidence_hash` on a catalogue entry
-- that requires workspace proof, and records the interactive-presence evidence
-- the same way -- and the product minted neither, anywhere: only the SHAPE was
-- read. So no host, customer or harness, could ever obtain `operations` through
-- a door of the product (`app.mcp_capability_contexts`: 0 rows in production
-- until that day), and the only way to bind one was to invent sixty-four
-- hexadecimal characters -- which is exactly what the ratified page forbids
-- ("never by a host claiming to be interactive").
--
-- THE REPAIR. The preflight -- the step a `manage` holder of the organization
-- performs -- mints both evidences for THIS preflight and keeps them on its row.
-- They are returned to that same authenticated holder (and to the installer at
-- handoff), and the bind accepts a proof only when it EQUALS the minted one. A
-- caller can no longer manufacture a proof: it can only present the one the
-- server issued for the preflight it is binding. Nullable: every preflight row
-- written before this migration carries none, and binds without proof exactly as
-- before -- Insights only.

ALTER TABLE app.host_preflights
    ADD COLUMN IF NOT EXISTS minted_workspace_evidence_hash TEXT,
    ADD COLUMN IF NOT EXISTS minted_presence_evidence_hash TEXT;

ALTER TABLE app.host_preflights
    DROP CONSTRAINT IF EXISTS ck_host_preflights_minted_evidence_hex;
ALTER TABLE app.host_preflights
    ADD CONSTRAINT ck_host_preflights_minted_evidence_hex CHECK (
        (minted_workspace_evidence_hash IS NULL OR minted_workspace_evidence_hash ~ '^[0-9a-f]{64}$')
        AND (minted_presence_evidence_hash IS NULL OR minted_presence_evidence_hash ~ '^[0-9a-f]{64}$')
    );

COMMENT ON COLUMN app.host_preflights.minted_workspace_evidence_hash IS
    'Migration 343 -- the workspace proof the server minted for this preflight; the bind accepts only this value.';
COMMENT ON COLUMN app.host_preflights.minted_presence_evidence_hash IS
    'Migration 343 -- the interactive-presence evidence the server minted for this preflight; the bind accepts only this value.';
