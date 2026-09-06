-- 345 -- an exploration calculation is PROPOSED before it is governed.
--
-- WHAT WAS MEASURED, 2026-09-05, before this file existed. The review rail of
-- the Context Hub carries exactly one typed payload:
--
--   server/core/context_review.py -> PROPOSAL_KINDS = ("business_link",)
--
-- and `app.context_review_requests` pins every remark to a node id AND a node
-- version, with `node_type` limited to `topic | procedure`. A calculation a
-- person or a model discovers while reading a Result names no Hub node, so
-- there was no row it could become: the authoring of a calculated field lived
-- only in the governance workbenches, and the exploration surfaces had no rail
-- back into the shared model at all.
--
-- WHY A NEIGHBOUR TABLE AND NOT A FOURTH `node_type`. Not a preference -- a
-- fact of the incumbent schema. `context_review_requests.node_version` is NOT
-- NULL and `uq_context_review_request_open` keys on (node_type, node_id,
-- node_version): filing a calculation there would mean inventing a node to
-- point at and a version for it. The VOCABULARY is reused verbatim instead --
-- `open | accepted | declined`, `human | agent`, `applied_ref` -- because two
-- words for one gesture would be two products.
--
-- WHAT `applied_ref` HOLDS HERE. The id of the semantic change-set that the
-- acceptance opened and PREPARED, never a published version. The rail's own
-- rule, written at `context_review.resolve_request`: accepting and applying are
-- not the same fact. A change-set left in `prepared` is a diff, an impact and a
-- Test-gate verdict waiting for a person; nothing is confirmed by a machine.
--
-- THE EVENTS TABLE IS APPEND-ONLY, WITH THE ERASURE HATCH. Migration 099's WHEN
-- clause, copied because it is the clause an org erasure needs -- and migration
-- 339 derives the same set from the FK closure of `app.organizations`, which
-- this table joins the day it is created. Without it the guard would refuse the
-- cascade an erasure produces, inside the very transaction that set
-- `app.rgpd_erasure` to `on`.
--
-- ERASURE, AND THE MECHANISM NAMED EXACTLY. Both tables hang off
-- `app.projects (org_id, id)` ON DELETE CASCADE. What erases them is therefore
-- NOT `core.org_purge` walking its FK graph: `plan_purge` admits
-- `confdeltype IN ('a','r')` -- NO ACTION and RESTRICT -- and a CASCADE edge is
-- invisible to it BY DESIGN. They are erased by POSTGRES, cascading from the
-- statement that deletes this organization's Projects, inside the transaction
-- where `org_purge` has already set `app.rgpd_erasure`. Five older headers say
-- the comfortable thing instead; `test_migration_erasure_claims` is the guard
-- that makes writing it cost a red line.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The proposal.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.calculated_field_proposals (
    id             TEXT        NOT NULL,
    org_id         TEXT        NOT NULL,
    project_id     TEXT        NOT NULL,

    -- The three states of the incumbent rail, and no fourth. A fourth is
    -- declared in `context_review.STATUSES` first, which is where the
    -- discussion belongs.
    status         TEXT        NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open', 'accepted', 'declined')),
    -- A proposal from a machine confused with a person's is worth less than
    -- nothing -- the reason `origin` has existed on the review queue since it
    -- was written.
    origin         TEXT        NOT NULL CHECK (origin IN ('human', 'agent')),

    -- The canonical machine name the promoted Concept would carry.
    name           TEXT        NOT NULL
                   CHECK (length(btrim(name)) BETWEEN 1 AND 120),
    description    TEXT        CHECK (description IS NULL OR length(description) <= 4000),

    -- The typed expression TREE of `semantic-expression.v1`. NEVER SQL: the
    -- object shape is asserted here, and the allowlist of operations plus the
    -- exactness of every reference is asserted by the validator, which is the
    -- only writer of this column.
    expression     JSONB       NOT NULL CHECK (jsonb_typeof(expression) = 'object'),

    -- INFERRED, never declared by the caller: what `validate_expression`
    -- returned for this tree. Stored so the review queue can be read without
    -- re-walking every formula.
    value_type     TEXT        NOT NULL CHECK (length(btrim(value_type)) > 0),
    unit           TEXT,
    currency       TEXT,
    -- The exact `(concept_id, version_id, role)` pins the walk collected. A
    -- dependency list is an array of objects, and the CHECK says so.
    dependencies   JSONB       NOT NULL DEFAULT '[]'::jsonb
                   CHECK (jsonb_typeof(dependencies) = 'array'),

    -- WHERE THE CALCULATION CAME FROM: `{origin: 'exploration', result_id,
    -- query_spec_version_id}`. A promotion that cannot say which exploration
    -- produced it is a guess with better manners, so this column is NOT NULL
    -- and the service verifies the rows it names belong to this Project.
    provenance     JSONB       NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),

    requested_by   TEXT        NOT NULL CHECK (length(btrim(requested_by)) > 0),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    resolved_by    TEXT,
    resolved_at    TIMESTAMPTZ,
    -- The change-set the acceptance opened and prepared. Never a version id.
    applied_ref    TEXT,

    CONSTRAINT pk_calculated_field_proposals PRIMARY KEY (id),
    CONSTRAINT ck_calculated_field_proposals_id
        CHECK (id ~ '^cfp_[0-9A-HJKMNP-TV-Z]{26}$'),
    -- An open proposal has no verdict; a closed one has both halves of it.
    -- Written as one CHECK so neither half can drift from the other.
    CONSTRAINT ck_calculated_field_proposals_resolution_is_whole
        CHECK (
            (status = 'open' AND resolved_by IS NULL AND resolved_at IS NULL)
            OR (status <> 'open' AND resolved_by IS NOT NULL AND resolved_at IS NOT NULL)
        ),
    -- `applied_ref` is what an ACCEPTANCE produced. A declined proposal that
    -- carried one would say a change-set exists for a verdict that refused.
    CONSTRAINT ck_calculated_field_proposals_applied_only_when_accepted
        CHECK (applied_ref IS NULL OR status = 'accepted'),
    -- The COUPLE, never project_id alone: it stops a proposal from carrying
    -- another organization's id and crossing the RLS by its own column.
    CONSTRAINT fk_calculated_field_proposals_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE
);

-- One OPEN proposal per name per Project. A second one for the same name is a
-- duplicate signal, not a second question; a resolved one frees the name, so a
-- declined calculation can be proposed again after the model has moved.
CREATE UNIQUE INDEX IF NOT EXISTS uq_calculated_field_proposals_open_name
    ON app.calculated_field_proposals (project_id, lower(btrim(name)))
    WHERE status = 'open';

-- The queue read: what is left to decide in this Project, newest first.
CREATE INDEX IF NOT EXISTS idx_calculated_field_proposals_queue
    ON app.calculated_field_proposals (org_id, project_id, status, created_at DESC);

COMMENT ON TABLE app.calculated_field_proposals IS
    'Migration 345 (story 75-1) -- a calculation found while exploring, offered to the '
    'governed model. Accepting it opens a PREPARED semantic change-set; it never publishes.';

-- ---------------------------------------------------------------------------
-- 2. The trace -- append-only, with the erasure hatch.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.calculated_field_proposal_events (
    id           TEXT        NOT NULL,
    proposal_id  TEXT        NOT NULL,
    org_id       TEXT        NOT NULL,
    project_id   TEXT        NOT NULL,
    -- What the act was. Each of the three is a ROW, never a rewrite.
    event        TEXT        NOT NULL
                 CHECK (event IN ('proposed', 'accepted', 'declined')),
    -- The typed fact of that act: ids and inferred values only, never a copied
    -- label and never a URL.
    fact         JSONB       NOT NULL CHECK (jsonb_typeof(fact) = 'object'),
    actor        TEXT        NOT NULL CHECK (length(btrim(actor)) > 0),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_calculated_field_proposal_events PRIMARY KEY (id),
    CONSTRAINT ck_calculated_field_proposal_events_id
        CHECK (id ~ '^cfpe_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT fk_calculated_field_proposal_events_proposal
        FOREIGN KEY (proposal_id) REFERENCES app.calculated_field_proposals (id)
        ON DELETE CASCADE,
    CONSTRAINT fk_calculated_field_proposal_events_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_calculated_field_proposal_events_proposal
    ON app.calculated_field_proposal_events (proposal_id, created_at);

CREATE OR REPLACE FUNCTION app.reject_calculated_field_proposal_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $fn$
BEGIN
    RAISE EXCEPTION 'calculated field proposal events are append-only'
        USING ERRCODE = '23000';
END;
$fn$;

DROP TRIGGER IF EXISTS trg_calculated_field_proposal_events_immutable
    ON app.calculated_field_proposal_events;
CREATE TRIGGER trg_calculated_field_proposal_events_immutable
    BEFORE UPDATE OR DELETE ON app.calculated_field_proposal_events
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_calculated_field_proposal_event_mutation();

DROP TRIGGER IF EXISTS trg_calculated_field_proposal_events_block_truncate
    ON app.calculated_field_proposal_events;
CREATE TRIGGER trg_calculated_field_proposal_events_block_truncate
    BEFORE TRUNCATE ON app.calculated_field_proposal_events
    FOR EACH STATEMENT
    EXECUTE FUNCTION app.reject_calculated_field_proposal_event_mutation();

COMMENT ON TABLE app.calculated_field_proposal_events IS
    'Migration 345 (story 75-1) -- append-only trace of every proposal write. Yields to '
    'a flagged org erasure (app.rgpd_erasure), exactly as migration 099 taught.';

-- ---------------------------------------------------------------------------
-- 3. The Epic-36 RLS floor on both, verbatim from migration 342.
--    RLS is the floor, not the door: the service checks first.
-- ---------------------------------------------------------------------------

DO $rls$
DECLARE
    t TEXT;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'calculated_field_proposals',
        'calculated_field_proposal_events'
    ] LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', t || '_strict', t);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I '
            'USING (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK (current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            '       OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            t || '_strict', t
        );
    END LOOP;
END
$rls$;

-- ---------------------------------------------------------------------------
-- 4. The privileges, WITH their REVOKE (migration 316's lesson).
-- ---------------------------------------------------------------------------

DO $grants$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '345: role `connector` does not exist on this cluster -- the '
            'append-only TRIGGER is the enforcement and is unaffected.';
        RETURN;
    END IF;

    -- The proposal carries a status and a verdict that move: UPDATE is its own.
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON app.calculated_field_proposals '
            'TO connector';

    -- The events are append-only. INSERT and SELECT are the posture; DELETE is
    -- the RGPD hatch and nothing else; UPDATE is revoked BECAUSE an earlier
    -- schema-wide grant would otherwise have said otherwise.
    EXECUTE 'GRANT SELECT, INSERT, DELETE ON app.calculated_field_proposal_events '
            'TO connector';
    EXECUTE 'REVOKE UPDATE ON app.calculated_field_proposal_events FROM connector';
END
$grants$;

COMMIT;
