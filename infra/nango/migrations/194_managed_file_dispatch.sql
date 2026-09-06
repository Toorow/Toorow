-- Story 38.13: durable managed-file dispatch intent and reconciliation.
BEGIN;

ALTER TABLE app.inbound_receipts
    DROP CONSTRAINT IF EXISTS inbound_receipts_channel_check;
ALTER TABLE app.inbound_receipts
    ADD CONSTRAINT inbound_receipts_channel_check
    CHECK (channel IN ('email', 'webhook', 'upload'));

ALTER TABLE app.inbound_raw_imports
    ADD COLUMN IF NOT EXISTS dispatch_bundle JSONB,
    ADD COLUMN IF NOT EXISTS dispatch_bundle_fingerprint TEXT;
ALTER TABLE app.inbound_raw_imports
    ADD CONSTRAINT ck_inbraw_dispatch_bundle_pair CHECK (
      (dispatch_bundle IS NULL AND dispatch_bundle_fingerprint IS NULL)
      OR (jsonb_typeof(dispatch_bundle) = 'object'
          AND dispatch_bundle_fingerprint ~ '^[0-9a-f]{64}$')
    );

CREATE OR REPLACE FUNCTION app.protect_inbound_raw_dispatch_bundle()
RETURNS TRIGGER AS $$
BEGIN
    IF OLD.dispatch_bundle IS NOT NULL AND (
       NEW.dispatch_bundle IS DISTINCT FROM OLD.dispatch_bundle
       OR NEW.dispatch_bundle_fingerprint IS DISTINCT FROM OLD.dispatch_bundle_fingerprint
    ) THEN
        RAISE EXCEPTION 'raw-import governed dispatch bundle is immutable'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS trg_inbraw_dispatch_bundle_protect ON app.inbound_raw_imports;
CREATE TRIGGER trg_inbraw_dispatch_bundle_protect
    BEFORE UPDATE ON app.inbound_raw_imports
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_raw_dispatch_bundle();

CREATE TABLE IF NOT EXISTS app.managed_file_dispatches (
    id TEXT PRIMARY KEY CHECK (id ~ '^mfd_[0-9A-HJKMNP-TV-Z]{26}$'),
    raw_import_id TEXT NOT NULL REFERENCES app.inbound_raw_imports(id) ON DELETE RESTRICT,
    ledger_id TEXT NOT NULL UNIQUE,
    execution_id TEXT NOT NULL UNIQUE,
    datastream_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    bundle JSONB NOT NULL CHECK (jsonb_typeof(bundle) = 'object'),
    bundle_fingerprint TEXT NOT NULL CHECK (bundle_fingerprint ~ '^[0-9a-f]{64}$'),
    state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN (
      'pending', 'landing', 'landed', 'validating', 'ready', 'promoting', 'published',
      'reconcile_required', 'failed', 'rejected', 'reconciled'
    )),
    candidate_content_fingerprint TEXT CHECK (
      candidate_content_fingerprint IS NULL
      OR candidate_content_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    candidate_schema_fingerprint TEXT CHECK (
      candidate_schema_fingerprint IS NULL
      OR candidate_schema_fingerprint ~ '^[0-9a-f]{64}$'
    ),
    landing_relation TEXT,
    row_count BIGINT CHECK (row_count IS NULL OR row_count >= 0),
    dq_evidence JSONB CHECK (
      dq_evidence IS NULL OR (
        jsonb_typeof(dq_evidence) = 'object'
        AND dq_evidence->>'status' = 'passed'
      )
    ),
    error_code TEXT,
    reconciliation_evidence JSONB NOT NULL DEFAULT '{}'::jsonb
      CHECK (jsonb_typeof(reconciliation_evidence) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_managed_file_dispatch_raw_execution
      UNIQUE (raw_import_id, execution_id),
    CONSTRAINT fk_managed_file_dispatch_ledger
      FOREIGN KEY (ledger_id, datastream_id, project_id)
      REFERENCES app.managed_feed_import_ledger(id, datastream_id, project_id)
      ON DELETE RESTRICT,
    CONSTRAINT fk_managed_file_dispatch_execution
      FOREIGN KEY (execution_id, datastream_id, project_id)
      REFERENCES app.datastream_executions(id, datastream_id, project_id)
      ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_managed_file_dispatch_reconcile
    ON app.managed_file_dispatches(project_id, state, updated_at)
    WHERE state IN (
      'pending', 'landing', 'landed', 'validating', 'ready', 'promoting',
      'reconcile_required'
    );

CREATE OR REPLACE FUNCTION app.protect_managed_file_dispatch()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'managed_file_dispatches is append-only'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.raw_import_id IS DISTINCT FROM OLD.raw_import_id
       OR NEW.ledger_id IS DISTINCT FROM OLD.ledger_id
       OR NEW.execution_id IS DISTINCT FROM OLD.execution_id
       OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.bundle IS DISTINCT FROM OLD.bundle
       OR NEW.bundle_fingerprint IS DISTINCT FROM OLD.bundle_fingerprint
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'managed-file dispatch identity and bundle are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.candidate_content_fingerprint IS NOT NULL AND (
       NEW.candidate_content_fingerprint IS DISTINCT FROM OLD.candidate_content_fingerprint
       OR NEW.candidate_schema_fingerprint IS DISTINCT FROM OLD.candidate_schema_fingerprint
       OR NEW.landing_relation IS DISTINCT FROM OLD.landing_relation
       OR NEW.row_count IS DISTINCT FROM OLD.row_count
    ) THEN
        RAISE EXCEPTION 'managed-file candidate evidence is immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.dq_evidence IS NOT NULL
       AND NEW.dq_evidence IS DISTINCT FROM OLD.dq_evidence THEN
        RAISE EXCEPTION 'managed-file positive DQ evidence is immutable'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
         (OLD.state = 'pending' AND NEW.state IN ('landing', 'reconcile_required', 'failed', 'rejected'))
      OR (OLD.state = 'landing' AND NEW.state IN ('landed', 'reconcile_required', 'failed', 'rejected'))
      OR (OLD.state = 'landed' AND NEW.state IN ('validating', 'reconcile_required', 'failed', 'rejected'))
      OR (OLD.state = 'validating' AND NEW.state IN ('ready', 'reconcile_required', 'failed', 'rejected'))
      OR (OLD.state = 'ready' AND NEW.state IN ('promoting', 'reconcile_required', 'failed', 'rejected'))
      OR (OLD.state = 'promoting' AND NEW.state IN ('published', 'reconcile_required', 'failed'))
      OR (OLD.state = 'reconcile_required' AND NEW.state IN ('published', 'reconciled', 'failed', 'rejected'))
    ) THEN
        RAISE EXCEPTION 'invalid managed-file dispatch transition: % -> %', OLD.state, NEW.state
            USING ERRCODE = '23000';
    END IF;
    IF NEW.state IN ('landed', 'validating', 'ready', 'promoting', 'published', 'reconciled')
       AND (NEW.candidate_content_fingerprint IS NULL
            OR NEW.candidate_schema_fingerprint IS NULL
            OR NEW.landing_relation IS NULL
            OR NEW.row_count IS NULL) THEN
        RAISE EXCEPTION 'managed-file dispatch candidate evidence is incomplete'
            USING ERRCODE = '23514';
    END IF;
    IF NEW.state IN ('ready', 'promoting', 'published', 'reconciled')
       AND (NEW.dq_evidence IS NULL OR NEW.dq_evidence->>'status' <> 'passed') THEN
        RAISE EXCEPTION 'managed-file dispatch positive DQ evidence is missing'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_managed_file_dispatch_protect ON app.managed_file_dispatches;
CREATE TRIGGER trg_managed_file_dispatch_protect
    BEFORE UPDATE OR DELETE ON app.managed_file_dispatches
    FOR EACH ROW EXECUTE FUNCTION app.protect_managed_file_dispatch();

CREATE OR REPLACE FUNCTION app.validate_managed_file_dispatch_scope()
RETURNS TRIGGER AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM app.inbound_raw_imports r
        JOIN app.datastreams d ON d.id = r.datastream_id
        WHERE r.id = NEW.raw_import_id
          AND r.datastream_id = NEW.datastream_id
          AND d.project_id = NEW.project_id
    ) THEN
        RAISE EXCEPTION 'managed-file dispatch raw-import scope mismatch'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_managed_file_dispatch_scope ON app.managed_file_dispatches;
CREATE CONSTRAINT TRIGGER trg_managed_file_dispatch_scope
    AFTER INSERT ON app.managed_file_dispatches
    DEFERRABLE INITIALLY IMMEDIATE
    FOR EACH ROW EXECUTE FUNCTION app.validate_managed_file_dispatch_scope();

ALTER TABLE app.managed_file_dispatches ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.managed_file_dispatches FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS managed_file_dispatches_strict ON app.managed_file_dispatches;
CREATE POLICY managed_file_dispatches_strict ON app.managed_file_dispatches
    USING (
      current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
      OR EXISTS (
        SELECT 1 FROM app.datastreams d
        WHERE d.id = managed_file_dispatches.datastream_id
          AND d.project_id = managed_file_dispatches.project_id
          AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
      )
    )
    WITH CHECK (
      current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
      OR EXISTS (
        SELECT 1 FROM app.datastreams d
        WHERE d.id = managed_file_dispatches.datastream_id
          AND d.project_id = managed_file_dispatches.project_id
          AND app.epic36_has_resource_access(d.org_id, 'flux', d.id)
      )
    );

COMMIT;
