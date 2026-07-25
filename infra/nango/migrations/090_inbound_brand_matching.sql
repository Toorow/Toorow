-- infra/nango/migrations/090_inbound_brand_matching.sql
--
-- Story 40.3: Inbound alias matching + governed new-entity alert.
--
-- The INBOUND HALF of Epic 40 (competitor / brand registry, E40-AD2). It SPECIALISES
-- Epic 27's value-conformance resolver (Story 27.4, app.dimension_value_mappings: exact ->
-- normalized -> similarity via difflib) onto BRAND matching, reuses the 40.1 entity master
-- (app.tracked_entities + aliases, migration 086) as the alias corpus, and routes every
-- new-entity proposal through the Epic 13 draft->approved governance. It does NOT refork the
-- resolver and it declares NO second approval engine. Three tables and nothing more:
--
--   1. app.entity_negative_aliases       -- the homonym-suppression list (E40-FR09): a
--      string that must NOT resolve to a given tracked_entity ("Orange" the telco vs the
--      fruit). The matcher EXCLUDES the entity from that string's candidate set.
--   2. app.inbound_brand_match_decisions -- the APPEND-ONLY provenance ledger (E40-NFR04):
--      one row per matched string per import, carrying the drillable provenance of AD-9
--      (matched entity + alias + method + confidence on a resolve; reason + candidates on an
--      alert). Immutable via a protect_*() BEFORE-UPDATE/DELETE trigger (077 pattern).
--   3. app.tracked_entity_alerts         -- the governed new-entity alert (E40-FR05): a
--      below-threshold / no-match / ambiguous string opens ONE alert per unknown normalized
--      string per org; nothing enters the master 'approved' without a recorded human
--      decision (E40-AD5). The AI (host LLM, via a 40.5 MCP tool) DRAFTS the proposal; a
--      human ratifies through approve_new_entity_alert.
--
-- LLM PLACEMENT (hard architectural fact): toorow has NO server-side LLM SDK. The LLM lives
-- in the MCP host (Claude), not the backend -- exactly as Epic 35's daily insights ship a
-- recipe the host executes and Epic 12/13 mapping corrections flow through a governed
-- proposal. Core (server/core/tracked_entity_matching.py) does DETERMINISTIC ranking + a
-- governed proposal surface; the host LLM adjudicates the ambiguous residue and DRAFTS
-- proposals via 40.5 tools; a human ratifies. No model secret / no LLM client in the schema
-- or in core.
--
-- AUDIT: NO new audit table. The append-only registry app.metric_semantics_audit (migration
-- 049, ALREADY APPLIED to Supabase) has a FREE entity_type TEXT column (no enum CHECK). 40.3
-- writes there with entity_type in {'tracked_entity_alert', 'entity_negative_alias'} via the
-- shared _write_semantics_audit helper. The match-decision ledger is its OWN append-only
-- provenance table (a distinct concern from the mutation audit). This migration has a SOFT
-- dependency on 049 (its audit table must exist) and 086 (app.tracked_entities); neither is
-- re-declared here.
--
-- ID prefixes (prefixed-ULID pattern): 'nena_' (entity_negative_aliases), 'ibm_'
-- (inbound_brand_match_decisions), 'tea_' (tracked_entity_alerts). Natural extension of
-- 'tent_'/'epr_' (40.1). Audit rows keep 049's 'msaudit_' prefix (shared table).
--
-- Apply with (human-gated on Jean's authorization -- Supabase only):
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector -f /migrations/090_inbound_brand_matching.sql
--
-- Schema-Change-Checklist (CONTRIBUTING.md):
--   [x] Additive & idempotent (IF NOT EXISTS everywhere; the whole file is replayable)
--   [x] New columns are NULL-able or have defaults
--   [x] No destructive DROP/ALTER on populated columns
-- Verified at dev time: the highest migration present is 089 (entity_source_bindings, Story
-- 40.2, landed by a parallel session); 40.2 took 089, so 40.3 takes the next free number 090
-- (noted in the story Completion Notes). 086 (40.1) + 049 (audit) are SOFT dependencies,
-- not re-declared.
--
-- PENDING -- NOT applied to Supabase (human gate: explicit go from Jean, like 052/086 before
-- their go). The orchestrator may apply it to a throwaway test DB. 049 is ALREADY applied in
-- prod, so its audit table exists and 090 does NOT recreate it.
-- ============================================================================

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

-- ---------------------------------------------------------------------------
-- A.2  app.entity_negative_aliases -- homonym suppression (E40-FR09, AC5).
--
-- One row per (entity, normalized_string): "this string must NOT match this entity". The
-- matcher (match_brand) EXCLUDES the entity from that string's candidate set FIRST; if no
-- OTHER entity clears the threshold the string becomes a governed new-entity alert
-- (fail-closed, E40-NFR02) -- never a silent join to the suppressed entity, never a silent
-- drop. Curated ONLY by an explicit human correction (correct_match / add_negative_alias),
-- never an autonomous AI write (E40-AD5). Mutations audited in app.metric_semantics_audit
-- (049) with entity_type='entity_negative_alias'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.entity_negative_aliases (
    id                TEXT        PRIMARY KEY,                 -- prefixed ULID: 'nena_'
    entity_id         TEXT        NOT NULL
                      REFERENCES app.tracked_entities(id) ON DELETE CASCADE,
    raw_string        TEXT        NOT NULL,                    -- the verbatim false-friend
    normalized_string TEXT        NOT NULL,                    -- normalize_value(raw_string) -- the match key
    created_by        TEXT        NOT NULL,                    -- identity subject (AD-14)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One suppression per (entity, normalized string).
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_negative_aliases_key
    ON app.entity_negative_aliases (entity_id, normalized_string);

-- Load-by-entity for the matcher corpus (joined through tracked_entities.org_id).
CREATE INDEX IF NOT EXISTS ix_entity_negative_aliases_entity
    ON app.entity_negative_aliases (entity_id);

COMMENT ON TABLE app.entity_negative_aliases IS
    'Story 40.3: negative aliases -- a string that must NOT resolve to a given tracked_entity (homonym / recorded false-friend, e.g. a telco vs a fruit). The matcher EXCLUDES the entity from that string''s candidate set (E40-FR09, E40-NFR02); if no other entity clears the threshold the string becomes a governed new-entity alert (fail-closed). Curated only by an explicit human correction -- never an autonomous AI write (E40-AD5). Mutations audited in app.metric_semantics_audit (049), entity_type=entity_negative_alias.';

-- ---------------------------------------------------------------------------
-- A.3  app.inbound_brand_match_decisions -- append-only provenance ledger (E40-NFR04, AC1).
--
-- One row per matched string per import; the provenance drill of AD-9. Copies the 077
-- append-only-ledger shape (immutable, protect_*() BEFORE-UPDATE/DELETE trigger + REVOKE
-- UPDATE, DELETE). import_ref is an OPAQUE TEXT correlation (no FK -- 38.6+ owns the
-- parsed-file table; this keeps 40.3 landable before it). candidates JSONB holds the ranked
-- rejected candidates on an alert. NEVER a silent join/drop: an alert row is written
-- explicitly (outcome='alert') so nothing is dropped and nothing is silently joined.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.inbound_brand_match_decisions (
    id                TEXT        PRIMARY KEY,                 -- prefixed ULID: 'ibm_'
    project_id        TEXT        NOT NULL REFERENCES app.projects(id)      ON DELETE CASCADE,
    org_id            TEXT        NOT NULL REFERENCES app.organizations(id) ON DELETE CASCADE,
    -- Opaque correlation to the inbound file/execution (TEXT, no FK -- 38.6+ owns rows).
    import_ref        TEXT        NOT NULL,
    raw_string        TEXT        NOT NULL,                    -- the verbatim inbound brand string
    normalized_string TEXT        NOT NULL,                    -- normalize_value(raw_string)
    outcome           TEXT        NOT NULL
                      CHECK (outcome IN ('resolved', 'alert')),
    -- Resolved provenance (all NULL on an alert):
    matched_entity_id TEXT        REFERENCES app.tracked_entities(id) ON DELETE SET NULL,
    matched_alias     TEXT,                                    -- the corpus string that won
    method            TEXT        CHECK (method IN ('exact', 'normalized', 'similarity')),
    confidence        REAL        CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    -- Alert provenance (NULL on a resolve):
    alert_reason      TEXT        CHECK (alert_reason IN ('no_match', 'below_threshold', 'ambiguous')),
    candidates        JSONB       NOT NULL DEFAULT '[]'::jsonb -- ranked rejected candidates + scores
                      CHECK (jsonb_typeof(candidates) = 'array'),
    created_by        TEXT        NOT NULL,                    -- identity subject (AD-14)
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- Shape guards mirroring the store: a resolve carries match fields; an alert carries a
    -- reason. Fail-closed by construction (a resolve without an entity, or an alert without a
    -- reason, is rejected at the DB level as well as in the store).
    CONSTRAINT ck_ibm_resolved_shape CHECK (
        outcome <> 'resolved'
        OR (matched_entity_id IS NOT NULL AND method IS NOT NULL AND alert_reason IS NULL)
    ),
    CONSTRAINT ck_ibm_alert_shape CHECK (
        outcome <> 'alert'
        OR (alert_reason IS NOT NULL AND matched_entity_id IS NULL
            AND method IS NULL AND confidence IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS ix_inbound_brand_match_decisions_project
    ON app.inbound_brand_match_decisions (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS ix_inbound_brand_match_decisions_org_outcome
    ON app.inbound_brand_match_decisions (org_id, outcome);

CREATE INDEX IF NOT EXISTS ix_inbound_brand_match_decisions_import_ref
    ON app.inbound_brand_match_decisions (import_ref);

-- Append-only: a decision row is immutable provenance. UPDATE and DELETE are rejected by the
-- protect_*() trigger (077 pattern) AND by the REVOKE below (defence in depth).
CREATE OR REPLACE FUNCTION app.protect_inbound_brand_match_decisions()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'inbound_brand_match_decisions is append-only (no UPDATE/DELETE)'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_inbound_brand_match_decisions_protect
    ON app.inbound_brand_match_decisions;
CREATE TRIGGER trg_inbound_brand_match_decisions_protect
    BEFORE UPDATE OR DELETE ON app.inbound_brand_match_decisions
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_brand_match_decisions();

COMMENT ON TABLE app.inbound_brand_match_decisions IS
    'Story 40.3: append-only provenance ledger of inbound brand matching (E40-NFR04, AD-9). One row per matched string per import: on a resolve, the matched entity + alias + method + confidence; on an alert, the reason + ranked rejected candidates. import_ref is an opaque correlation (38.6+ owns the parsed-file table). Immutable (protect_*() trigger + REVOKE UPDATE, DELETE). Never a silent join/drop -- an alert is recorded explicitly (fail-closed, E40-NFR02).';

-- ---------------------------------------------------------------------------
-- A.4  app.tracked_entity_alerts -- the governed new-entity alert (E40-FR05, AC2).
--
-- ONE OPEN alert per unknown normalized string per org (a re-import of the same unknown
-- reuses the open alert, no duplicate spam). proposed_entity_id FKs the 'draft' entity the
-- proposal minted (NULL until drafted; the entity becomes 'approved' only on the human
-- approve). Routed through Epic 13 draft->approved: nothing enters the master approved
-- without a recorded human decision (E40-AD5). Mutations audited in app.metric_semantics_audit
-- (049) with entity_type='tracked_entity_alert'.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.tracked_entity_alerts (
    id                 TEXT        PRIMARY KEY,                -- prefixed ULID: 'tea_'
    org_id             TEXT        NOT NULL
                       REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id         TEXT        NOT NULL
                       REFERENCES app.projects(id) ON DELETE CASCADE,  -- who saw it
    raw_string         TEXT        NOT NULL,                   -- the verbatim unknown string
    normalized_string  TEXT        NOT NULL,                   -- normalize_value(raw_string) -- the open-unicity key
    -- The 'draft' entity the proposal minted (NULL until a proposal drafts one). Set NULL on
    -- entity delete so the alert survives its proposed entity being removed.
    proposed_entity_id TEXT        REFERENCES app.tracked_entities(id) ON DELETE SET NULL,
    status             TEXT        NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open', 'approved', 'dismissed')),
    reason             TEXT        NOT NULL
                       CHECK (reason IN ('no_match', 'below_threshold', 'ambiguous')),
    candidates         JSONB       NOT NULL DEFAULT '[]'::jsonb
                       CHECK (jsonb_typeof(candidates) = 'array'),
    created_by         TEXT        NOT NULL,                   -- identity subject (AD-14)
    resolved_by        TEXT,                                   -- who approved/dismissed (NULL while open)
    resolved_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ONE open alert per (org, normalized string): a partial unique index on status='open' so a
-- re-import of the same unknown hits the existing open alert (idempotent, AC2) while an
-- approved/dismissed alert for the same string does not block a fresh open one later.
CREATE UNIQUE INDEX IF NOT EXISTS uq_tracked_entity_alerts_open
    ON app.tracked_entity_alerts (org_id, normalized_string)
    WHERE status = 'open';

-- Listing surfaces: by org (owner/admin triage) and by project (who saw it).
CREATE INDEX IF NOT EXISTS ix_tracked_entity_alerts_org_status
    ON app.tracked_entity_alerts (org_id, status);

CREATE INDEX IF NOT EXISTS ix_tracked_entity_alerts_project_status
    ON app.tracked_entity_alerts (project_id, status);

DROP TRIGGER IF EXISTS trg_tracked_entity_alerts_updated_at ON app.tracked_entity_alerts;
CREATE TRIGGER trg_tracked_entity_alerts_updated_at
    BEFORE UPDATE ON app.tracked_entity_alerts
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMENT ON TABLE app.tracked_entity_alerts IS
    'Story 40.3: governed new-entity alert -- a below-threshold / no-match / ambiguous inbound brand string. ONE open alert per unknown normalized string per org (a re-import reuses it). Routed through Epic 13 draft->approved: nothing enters the master approved without a recorded human decision (E40-AD5). The AI (host LLM, via a 40.5 MCP tool) drafts the proposal; approve_new_entity_alert mints the entity status=approved on the human call. Mutations audited in app.metric_semantics_audit (049), entity_type=tracked_entity_alert.';

-- Defence-in-depth append-only on the ledger (mirrors 049/077 REVOKE; the trigger is the
-- primary guard so this is idempotent and safe even if the role lacks GRANT authority).
DO $$
BEGIN
    BEGIN
        REVOKE UPDATE, DELETE ON app.inbound_brand_match_decisions FROM PUBLIC;
    EXCEPTION WHEN OTHERS THEN
        -- REVOKE is best-effort (the trigger enforces immutability regardless).
        NULL;
    END;
END $$;

COMMIT;
