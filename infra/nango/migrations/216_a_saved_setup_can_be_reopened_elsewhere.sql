-- 216_a_saved_setup_can_be_reopened_elsewhere.sql
--
-- A VALIDATED SETUP CAN BE REOPENED ELSEWHERE IN THE SAME ORGANIZATION.
-- Story 57.7. An operator who finished configuring a Datastream and needs its
-- twin for another source account retypes every selection today: nothing in
-- this schema holds a reusable operator input.
--
-- WHY A THIRD TABLE, MEASURED, AND WHY IT IS NOT THE THIRD REGISTRY THE
-- RATIFIED DOCUMENT REFUSES:
--
--   * `app.import_templates` (088:55-68) carries NO scope column at all -- no
--     org_id, no project_id -- and declares itself "REFERENCE DATA only, NOT a
--     per-datastream ledger" (088:20-23). A client configuration written there
--     would be readable by every organization of the deployment. That is a
--     tenancy breach, not a naming inconvenience.
--   * `app.file_source_templates` (097:43-91) is a FILE contract: its validator
--     demands `kind`, non-empty `required_fields`, `grain`, `class`,
--     `placement.metric`, `placement.period` (file_source_template.py:113-186),
--     and its normalizer DROPS every unknown key (:130-133). A
--     `connector_pull` operator input carries none of the six and would be
--     erased key by key.
--   * What the document refuses (datastream-workbench-and-wizard.md:303-305) is
--     a registry of MEANING -- a hand-written preset named "Acquisition". What
--     this table holds is a `normalized_operator_input`: references and
--     selections, already validated by `_validate_operator_union` and
--     re-validated against the applying project by `_validate_reference_scope`.
--     No definition, no hand-written report label, no canonical field.
--
-- WHAT IS DELIBERATELY NOT STORED. Three keys of an operator input are
-- addressed `(id, draft_id, project_id)` or are a position in one session, so
-- they mean nothing outside the draft that produced them:
-- `observation_ref` (datastream_setup_observations.py:515-520),
-- `source.staged_asset_ref` (:1460-1464) and `wizard_state`. The CHECK below
-- refuses a payload naming any of them -- a guard in the schema, not only in
-- the writer, because the writer is one door and the table outlives it.
--
-- `origin_contract_version_ref` IS PROVENANCE, NEVER A PIN.
-- `SELECT count(*) FROM app.connector_contract_versions` is 0 in this
-- deployment, so a template pinning a contract version would be born stale.
-- The contract is RE-RESOLVED from `source-options` when the template is
-- applied; this column only says what the original was resolved against.
--
-- THE UNIQUE KEYS ARE PARTIAL, ON PURPOSE. Retirement is logical
-- (`is_active=FALSE`) because the row carries a provenance and a content hash,
-- like its two neighbours. A total `UNIQUE (org_id, label)` would let a retired
-- row hold its name forever, so "retire it and save a corrected one under the
-- same name" -- the ordinary repair -- would be impossible.
--
-- NO IMMUTABILITY TRIGGER. `app.org_purge` walks the FK graph and DELETEs the
-- tenant tree; a BEFORE DELETE trigger like migration 137's would make this
-- table the one row an erasure cannot remove.

BEGIN;

-- The three draft-only keys, refused at the storage layer. Same shape and same
-- byte bound as `app.safe_preconfiguration_evidence` (134:20-25), plus the
-- sensitive-key refusal that function already carries, because a payload
-- reaching here has been through `_validate_inputs` and must stay through it.
CREATE OR REPLACE FUNCTION app.safe_setup_template_payload(value JSONB)
RETURNS BOOLEAN LANGUAGE sql IMMUTABLE AS $$
    SELECT jsonb_typeof(value) = 'object'
       AND octet_length(value::text) <= 8192
       AND lower(value::text) !~ '"(observation_ref|staged_asset_ref|wizard_state)"[[:space:]]*:'
       AND app.safe_preconfiguration_evidence(value);
$$;

CREATE TABLE IF NOT EXISTS app.datastream_setup_templates (
    id TEXT PRIMARY KEY CHECK (id ~ '^dst_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL REFERENCES app.organizations(id) ON DELETE RESTRICT,
    origin_project_id TEXT NOT NULL REFERENCES app.projects(id) ON DELETE RESTRICT,
    origin_datastream_id TEXT NOT NULL,
    -- The EXACT revision that was materialized, never the draft's current one.
    -- Revisions are append-only, so a draft's current revision can be LATER
    -- than what was validated and created; deriving from it would save a
    -- configuration nobody confirmed.
    origin_draft_revision_id TEXT NOT NULL
        REFERENCES app.datastream_setup_draft_revisions(id) ON DELETE RESTRICT,
    label TEXT NOT NULL CHECK (char_length(btrim(label)) BETWEEN 1 AND 80),
    mode TEXT NOT NULL CHECK (mode IN ('connector_pull','external_bq','managed_feed')),
    connector_ref TEXT,
    report_ref TEXT,
    origin_contract_version_ref TEXT,
    origin_source_account_ref TEXT,
    template_payload JSONB NOT NULL CHECK (app.safe_setup_template_payload(template_payload)),
    -- The references this payload does NOT carry, DECLARED. Derived from the
    -- measured scope of each source key, never hand-listed: a key whose scope
    -- does not reach across the organization is reopened, and says so before
    -- the click.
    open_variables JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(open_variables) = 'array'),
    content_hash TEXT NOT NULL CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash TEXT NOT NULL CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    created_by TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    retired_at TIMESTAMPTZ,
    retired_by TEXT,
    CHECK ((NOT is_active) = (retired_at IS NOT NULL)),
    FOREIGN KEY (origin_datastream_id, origin_project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_datastream_setup_template_label
    ON app.datastream_setup_templates(org_id, label) WHERE is_active;
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastream_setup_template_content
    ON app.datastream_setup_templates(org_id, content_hash) WHERE is_active;
-- The replay key is NOT partial: a retried POST must return the row it already
-- wrote even after that row has been retired, instead of writing a second one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_datastream_setup_template_idempotency
    ON app.datastream_setup_templates(org_id, idempotency_key_hash);
CREATE INDEX IF NOT EXISTS ix_datastream_setup_template_active
    ON app.datastream_setup_templates(org_id, created_at DESC) WHERE is_active;

COMMIT;
