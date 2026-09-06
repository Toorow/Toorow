-- Story 50.7 repair (review finding F1/F2): the Render-payload table AC6.2 already
-- names, and without which a Share can open nothing.
--
-- WHAT WAS WRONG, measured rather than argued. `/api/render-shares/session/render`
-- assigned its envelope to `window.__TOOROW_FROZEN_RENDER__`, which
-- `ui/cards/shell/src/viz/entries/share.tsx` casts to `RenderInput` -- a five-field
-- contract (`result`, `spec`, `pins`, `profile`, `display`). The envelope carried
-- none of them: its keys were `content_hash, datum_evidence_keys, display_state,
-- evidence_manifest, formatter_version, render_id, ...`. So the runtime's validator
-- refused, and the recipient met a field-by-field refusal panel: not a chart, not
-- the accessible table fallback, not an honest unavailable state -- a page that
-- reads like a bug.
--
-- Underneath it, there was nothing to send. `app.renders` (migration 154) pins the
-- Result's IDENTITY and its content hash; the row data itself lives in
-- `app.query_result_payloads` (migration 151), and AC6.2 grants
-- `toorow_share_reader` NO privilege on that table -- deliberately, because a Share
-- that could reach back into a Result would be the browsing grant AD-20 forbids by
-- name. Migration 162 wrote that refusal down as an explicit REVOKE.
--
-- AC6.2 ALREADY NAMED THE ANSWER. It enumerates what the reader role may SELECT:
-- "the Share, Render, RENDER-PAYLOAD and access-event tables". The Render-payload
-- table was never created. This is it.
--
-- WHY A COPY AND NOT A GRANT. Two different objects with two different lifetimes:
--
--   * `app.query_result_payloads` belongs to the Project. It is reachable by every
--     Query Spec, every re-execution and every Result of that Project, and the
--     recipient of a Share must be able to reach exactly one frozen answer -- not a
--     table from which any Result of the Project can be selected. A grant is a door
--     into the Project's evidence; a copy is one frozen artifact.
--   * `app.render_frozen_payloads` belongs to the Render. It is insert-once, and
--     its content hash must equal the hash the Render already pinned, so the copy
--     can PROVE it is the same bytes rather than assert it.
--
-- WHEN IT IS WRITTEN. At Share creation, by `core.render_shares.freeze_render_payload`,
-- inside the same transaction as the grant and its audit row. Freezing at share
-- time rather than at render time changes nothing about WHAT is frozen: both
-- `app.query_results` and `app.query_result_payloads` are insert-once and refuse
-- UPDATE and DELETE by trigger (migration 151), and the copy is refused unless its
-- `content_hash` equals `app.renders.result_content_hash`. If the payload were ever
-- to differ from the hash the Render pinned, the Share is refused at creation -- the
-- console operator learns immediately, instead of a recipient meeting an empty page.
--
-- WHAT IT DOES NOT DO. It grants nothing new to `toorow_share_reader` beyond SELECT
-- on this one table. Every REVOKE migration 162 issued stays exactly as it was, and
-- `test_the_share_reader_role_is_refused_on_project_data` still asserts 42501 on all
-- ten sensitive relations.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The frozen payload. One row per Render, insert-once, Project-scoped.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.render_frozen_payloads (
    render_id                       TEXT PRIMARY KEY,
    org_id                          TEXT NOT NULL,
    project_id                      TEXT NOT NULL,

    -- The Result this is a copy OF, and the hash that proves it is the same
    -- bytes the Render was drawn from. Both are compared against the Render's
    -- own pins by the writer, and the CHECK below refuses a malformed hash.
    result_id                       TEXT NOT NULL,
    result_content_hash             TEXT NOT NULL,

    -- The Result, exactly as `app.query_result_payloads` held it, plus the three
    -- facts that live on `app.query_results` and that the runtime's `VizResult`
    -- requires: the outcome, the row count and the truncation flag. An outcome
    -- resolved at read time would let a frozen artifact change its own honesty.
    outcome                         TEXT NOT NULL,
    result_schema                   JSONB NOT NULL,
    result_manifest                 JSONB NOT NULL,
    rows_chunk                      JSONB NOT NULL,
    row_count                       BIGINT NOT NULL,
    truncated                       BOOLEAN NOT NULL,

    -- The Visualization Spec version the Render pinned, and its document. The
    -- document is copied for the same reason the rows are: the reader role has no
    -- privilege on `app.visualization_spec_versions`, and giving it one would open
    -- every Visualization of the Project to a public session.
    visualization_spec_version_id   TEXT NOT NULL,
    spec_contract_version           TEXT NOT NULL,
    schema_version                  INTEGER NOT NULL,
    family                          TEXT NOT NULL,
    spec_document                   JSONB NOT NULL,

    frozen_at                       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    frozen_by                       TEXT NOT NULL,

    CONSTRAINT uq_render_frozen_payloads_scope UNIQUE (render_id, org_id, project_id),
    -- Composite and Project-scoped, like every child key migration 151 introduced:
    -- a frozen payload cannot point at another Project's Render even when the
    -- application layer is wrong.
    CONSTRAINT fk_render_frozen_payloads_render
        FOREIGN KEY (render_id, org_id, project_id)
        REFERENCES app.renders (id, org_id, project_id)
        ON DELETE RESTRICT,
    CONSTRAINT fk_render_frozen_payloads_result
        FOREIGN KEY (result_id, org_id, project_id)
        REFERENCES app.query_results (id, org_id, project_id),
    CONSTRAINT fk_render_frozen_payloads_spec_version
        FOREIGN KEY (visualization_spec_version_id, org_id, project_id)
        REFERENCES app.visualization_spec_versions (id, org_id, project_id),
    CONSTRAINT ck_render_frozen_payloads_hash
        CHECK (result_content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_render_frozen_payloads_outcome
        CHECK (outcome IN ('success', 'empty', 'degraded', 'refused', 'unavailable')),
    CONSTRAINT ck_render_frozen_payloads_schema_is_object
        CHECK (jsonb_typeof(result_schema) = 'object'),
    CONSTRAINT ck_render_frozen_payloads_manifest_is_object
        CHECK (jsonb_typeof(result_manifest) = 'object'),
    CONSTRAINT ck_render_frozen_payloads_rows_is_array
        CHECK (jsonb_typeof(rows_chunk) = 'array'),
    CONSTRAINT ck_render_frozen_payloads_spec_is_object
        CHECK (jsonb_typeof(spec_document) = 'object'),
    CONSTRAINT ck_render_frozen_payloads_counts_nonnegative
        CHECK (row_count >= 0),
    -- The same honesty rule `app.query_results` enforces: only a `success` or a
    -- `degraded` answer may carry rows. A frozen copy that reported rows under
    -- `empty` would be a dishonest state preserved forever.
    CONSTRAINT ck_render_frozen_payloads_rows_match_outcome CHECK (
        outcome IN ('success', 'degraded') OR jsonb_array_length(rows_chunk) = 0
    ),
    -- The contract literal is what the runtime compares for EQUALITY, and the
    -- integer is the only key it ranges over. Both are pinned here so the public
    -- read never has to consult the Visualization the recipient cannot reach.
    CONSTRAINT ck_render_frozen_payloads_contract_version
        CHECK (spec_contract_version = 'visualization-spec.v1'),
    CONSTRAINT ck_render_frozen_payloads_schema_version
        CHECK (schema_version >= 1)
);

COMMENT ON TABLE app.render_frozen_payloads IS
    'Story 50.7: the frozen bytes ONE Render was drawn from -- the Result slice, '
    'its schema and manifest, and the pinned Visualization Spec document. It is '
    'the Render-payload table AC6.2 enumerates among the four relations '
    'toorow_share_reader may SELECT. It exists so a public Share can compose the '
    'runtime input WITHOUT any privilege on app.query_result_payloads or '
    'app.visualization_spec_versions, which stay refused to the reader role.';

COMMENT ON COLUMN app.render_frozen_payloads.result_content_hash IS
    'Equal to app.renders.result_content_hash by construction: the writer refuses '
    'the Share when the retained payload no longer matches the hash the Render '
    'pinned, so this copy can PROVE it is the same bytes rather than assert it.';

CREATE INDEX IF NOT EXISTS ix_render_frozen_payloads_project
    ON app.render_frozen_payloads (org_id, project_id);

-- ---------------------------------------------------------------------------
-- 2. Insert-once. A frozen payload that could be rewritten is not frozen.
--
--    The `app.rgpd_erasure` escape hatch is the same one migrations 098/099, 150
--    and 151 use: erasure is the only legitimate reason these rows disappear, and
--    it is explicit rather than implicit.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_render_frozen_payload_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION
        'Story 50.7: app.render_frozen_payloads is insert-once. A frozen payload '
        'that could be rewritten would change what an already-delivered link '
        'shows, for a recipient with no way to notice.'
        USING ERRCODE = '23000';
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_render_frozen_payloads_insert_once
    ON app.render_frozen_payloads;
CREATE TRIGGER trg_render_frozen_payloads_insert_once
    BEFORE UPDATE OR DELETE ON app.render_frozen_payloads
    FOR EACH ROW
    WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
    EXECUTE FUNCTION app.reject_render_frozen_payload_mutation();

DROP TRIGGER IF EXISTS trg_render_frozen_payloads_block_truncate
    ON app.render_frozen_payloads;
CREATE TRIGGER trg_render_frozen_payloads_block_truncate
    BEFORE TRUNCATE ON app.render_frozen_payloads
    EXECUTE FUNCTION app.reject_render_share_truncate();

-- ---------------------------------------------------------------------------
-- 3. RLS, on the Epic-36 floor, exactly as migration 162 armed its four tables.
-- ---------------------------------------------------------------------------
ALTER TABLE app.render_frozen_payloads ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.render_frozen_payloads FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS render_frozen_payloads_strict ON app.render_frozen_payloads;
CREATE POLICY render_frozen_payloads_strict ON app.render_frozen_payloads
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'project', project_id)
    );

-- ---------------------------------------------------------------------------
-- 4. The ONE new grant. SELECT, on one table, and nothing else.
--
--    Enumerated, never `GRANT ... ON ALL TABLES IN SCHEMA app`. The public reader
--    gains the ability to read the frozen bytes of a Render it already holds a
--    session for; it gains no path to a Result, a Query Spec, a Visualization or a
--    Project. The REVOKEs migration 162 issued are re-stated below so that this
--    file cannot be read as loosening them.
-- ---------------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'toorow_share_reader') THEN
        RAISE EXCEPTION
            'Story 50.7: role toorow_share_reader is missing. It is a CLUSTER '
            'prerequisite, created once as a superuser: '
            'CREATE ROLE toorow_share_reader NOLOGIN; '
            'GRANT toorow_share_reader TO %I;', current_user;
    END IF;
END
$$;

GRANT SELECT ON app.render_frozen_payloads TO toorow_share_reader;

REVOKE ALL ON app.query_result_payloads      FROM toorow_share_reader;
REVOKE ALL ON app.visualization_spec_versions FROM toorow_share_reader;
REVOKE ALL ON app.visualizations              FROM toorow_share_reader;

COMMIT;
