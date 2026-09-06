-- Story 68.3: every occurrence of a bound key resolves to a node, WITH A VERDICT.
--
-- WHAT THIS TABLE IS. Story 68.2 lets a mapping column declare which entity type
-- its values are keys OF (`fields[].binding.designates_object_kind`). This table
-- is what makes that declaration honest at landing time: each DISTINCT value a
-- pull lands in such a column is resolved against the declared type's registry,
-- and the OUTCOME is persisted -- `resolved`, `unmatched` or `ambiguous`, the
-- taxonomy `dbt/macros/resolve_governed_node.sql` ratified, never a third
-- invention. An approximate join that happened silently would leave no row here;
-- the absence of a verdict row for a bound occurrence is the defect this story
-- exists to make visible.
--
-- WHY NOT `entity_match_decisions` (migration 147). That table is keyed by
-- competitor observed value -- one live decision per (org, project, value hash),
-- the scope of Competitor governance, and its CHECK carries the proposal
-- vocabulary, not the resolution taxonomy. The SHAPE is the model, the scope is
-- not: occurrence-verdicts key on (datastream, field, value, window), carry the
-- same append-only + supersession discipline, the same immutability trigger and
-- the same rgpd_erasure escape.
--
-- SUPERSESSION IS A NEW ROW. A replayed pull over the same window with the same
-- outcome writes NOTHING (idempotent -- the coverage fraction must not inflate).
-- The same occurrence resolving DIFFERENTLY -- an alias was recorded, a node was
-- declared -- is a new `current` row pointing at the one it supersedes, and the
-- old row walks to `superseded`. At most one `current` row per occurrence: the
-- partial unique index below is what makes "what does this value resolve to,
-- here, now" a single answer.
--
-- THE CHECK IS THE GOVERNANCE. `resolved` names its node, and nothing else may:
-- a machine never auto-picks among candidates. `ambiguous` persists the
-- candidate list it refused to choose from -- a person picks later, and the
-- refusal is the evidence.
--
-- RLS: project-scoped with `org_id` NOT NULL, so it takes the 294 first-form
-- predicate verbatim -- the AI-299 ratchet
-- (`test_rls_covers_every_org_scoped_table_pg.py`) walks information_schema and
-- would refuse the table otherwise.

BEGIN;

CREATE TABLE IF NOT EXISTS app.entity_key_match_verdicts (
    id TEXT PRIMARY KEY CHECK (id ~ '^ekmv_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    datastream_id TEXT NOT NULL,

    -- The occurrence: which pull landed it, in which window, in which column.
    -- The window, not the pull id, is the idempotence key -- a replayed pull
    -- gets a new pull id and must not mint a second verdict.
    execution_id TEXT,
    mapping_version_id TEXT,
    registry_id TEXT,
    -- The declared type the column designates. Opaque (AD-2): stored, never
    -- compared to a literal anywhere in core.
    object_kind TEXT NOT NULL CHECK (object_kind ~ '^[a-z][a-z0-9_]{1,39}$'),
    field_id TEXT NOT NULL CHECK (length(btrim(field_id)) BETWEEN 1 AND 120),
    -- The value is hashed because a provider value is untrusted input that may
    -- carry personal data; the normalized form is what resolution matched on
    -- (normalize_alias_value, the single authority).
    raw_value_hash TEXT NOT NULL CHECK (raw_value_hash ~ '^[0-9a-f]{64}$'),
    normalized_value TEXT NOT NULL CHECK (length(btrim(normalized_value)) BETWEEN 1 AND 400),
    occurrence_count INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    observed_from DATE NOT NULL,
    observed_to DATE NOT NULL CHECK (observed_to >= observed_from),

    -- The verdict: the ratified taxonomy, and nothing else.
    verdict TEXT NOT NULL CHECK (verdict IN ('resolved', 'unmatched', 'ambiguous')),
    -- The SKOS relation the resolution landed on. `none` for unmatched and for
    -- an ambiguity the server refuses to arbitrate.
    relation TEXT NOT NULL DEFAULT 'none'
        CHECK (relation IN ('exact', 'close', 'broader', 'narrower', 'related', 'negative', 'none')),
    node_id TEXT,
    -- The candidates, persisted even on a refusal to choose: what was NOT
    -- picked is part of the evidence (AC3).
    candidates JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(candidates) = 'array'),
    confidence NUMERIC(5, 4) CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
    reason_code TEXT NOT NULL
        CHECK (reason_code IN (
            'exact_lookup', 'ranked', 'no_candidate', 'below_threshold',
            'ambiguous', 'negative_alias', 'entity_type_unavailable'
        )),
    evidence JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(evidence) = 'object'),

    -- Supersession is a new row pointing back, never an edit.
    state TEXT NOT NULL DEFAULT 'current' CHECK (state IN ('current', 'superseded')),
    supersedes_id TEXT REFERENCES app.entity_key_match_verdicts(id) ON DELETE RESTRICT,

    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- A resolved verdict names its node; nothing else may. This CHECK is what
    -- makes "matching never auto-picks" a database fact rather than a service
    -- convention.
    CHECK ((verdict = 'resolved') = (node_id IS NOT NULL)),

    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, registry_id)
        REFERENCES app.master_data_registries(project_id, id) ON DELETE RESTRICT,
    FOREIGN KEY (project_id, node_id)
        REFERENCES app.master_data_nodes(project_id, id) ON DELETE RESTRICT
);

-- At most one CURRENT verdict per occurrence (datastream, field, value, window).
-- A second one would make "what does this value denote here?" ambiguous -- the
-- exact defect the `ambiguous` verdict exists to record, not to become.
CREATE UNIQUE INDEX IF NOT EXISTS uq_entity_key_match_verdict_live
    ON app.entity_key_match_verdicts
       (project_id, datastream_id, field_id, raw_value_hash, observed_from, observed_to)
    WHERE state = 'current';

-- The coverage read: N resolved of M occurrences per (datastream, entity type).
CREATE INDEX IF NOT EXISTS idx_entity_key_match_verdict_coverage
    ON app.entity_key_match_verdicts (project_id, state, verdict);
CREATE INDEX IF NOT EXISTS idx_entity_key_match_verdict_registry
    ON app.entity_key_match_verdicts (registry_id) WHERE state = 'current';

-- Append-only with supersession, modeled on app.protect_entity_match_decision
-- (migration 147): only the walk to `superseded` is allowed, and nothing else
-- moves with it. The rgpd_erasure escape in the WHEN clause is the doctrine of
-- migrations 200/209: an append-only table that forgets it blocks erasure.
CREATE OR REPLACE FUNCTION app.protect_entity_key_match_verdict()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'entity key match verdicts are append-only';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NEW.state <> 'superseded' THEN
        RAISE EXCEPTION 'a match verdict is superseded by a new row, never edited';
    END IF;
    IF (
           NEW.verdict IS DISTINCT FROM OLD.verdict
        OR NEW.relation IS DISTINCT FROM OLD.relation
        OR NEW.node_id IS DISTINCT FROM OLD.node_id
        OR NEW.raw_value_hash IS DISTINCT FROM OLD.raw_value_hash
        OR NEW.candidates IS DISTINCT FROM OLD.candidates
        OR NEW.reason_code IS DISTINCT FROM OLD.reason_code
    ) THEN
        RAISE EXCEPTION 'match verdict content is immutable';
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_entity_key_match_verdict_protect ON app.entity_key_match_verdicts;
CREATE TRIGGER trg_entity_key_match_verdict_protect
    BEFORE UPDATE OR DELETE ON app.entity_key_match_verdicts
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.protect_entity_key_match_verdict();
DROP TRIGGER IF EXISTS trg_entity_key_match_verdict_block_truncate ON app.entity_key_match_verdicts;
CREATE TRIGGER trg_entity_key_match_verdict_block_truncate
    BEFORE TRUNCATE ON app.entity_key_match_verdicts
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.protect_entity_key_match_verdict();

COMMENT ON TABLE app.entity_key_match_verdicts IS
    'Story 68.3: each occurrence of a designates_object_kind column resolves to an MDM node with a verdict -- resolved / unmatched / ambiguous, the ratified taxonomy. Append-only with supersession by new row; a machine never auto-picks (resolved names its node, nothing else may).';
COMMENT ON COLUMN app.entity_key_match_verdicts.candidates IS
    'The candidates the resolver ranked, persisted even on unmatched and ALWAYS on ambiguous: what was not chosen is the evidence a person repairs from.';

ALTER TABLE app.entity_key_match_verdicts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.entity_key_match_verdicts FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS entity_key_match_verdicts_epic36 ON app.entity_key_match_verdicts;
CREATE POLICY entity_key_match_verdicts_epic36 ON app.entity_key_match_verdicts
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id))
    WITH CHECK (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

GRANT SELECT, INSERT, UPDATE ON app.entity_key_match_verdicts TO connector;

COMMIT;
