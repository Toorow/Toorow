-- Story 38.10: durable AD-36 scan jobs, bounded attempts and recovery.
BEGIN;

CREATE TABLE IF NOT EXISTS app.inbound_scan_jobs (
    id TEXT PRIMARY KEY,
    job_policy_version TEXT NOT NULL,
    org_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL REFERENCES app.datastreams(id) ON DELETE RESTRICT,
    receipt_id TEXT NOT NULL REFERENCES app.inbound_receipts(id) ON DELETE RESTRICT,
    attachment_ordinal INT NOT NULL CHECK (attachment_ordinal >= 0),
    raw_import_id TEXT REFERENCES app.inbound_raw_imports(id) ON DELETE RESTRICT,
    manifest_uri TEXT NOT NULL,
    state TEXT NOT NULL,
    attempt_count INT NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts INT NOT NULL DEFAULT 5 CHECK (max_attempts BETWEEN 1 AND 20),
    task_name TEXT,
    dispatch_count INT NOT NULL DEFAULT 0 CHECK (dispatch_count >= 0),
    trace_id TEXT,
    error_code TEXT,
    outcome_status TEXT,
    recovery_evidence JSONB,
    recovery_count INT NOT NULL DEFAULT 0 CHECK (recovery_count >= 0),
    recovered_by TEXT,
    recovery_trace_id TEXT,
    queued_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    dispatched_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (receipt_id, attachment_ordinal),
    CONSTRAINT ck_inbound_scan_job_policy
        CHECK (job_policy_version = 'inbound-scan-job-v1'),
    CONSTRAINT ck_inbound_scan_job_state
        CHECK (state IN (
            'QUEUED','RUNNING','RETRY_WAIT','SUCCEEDED','REJECTED','DEAD_LETTER'
        )),
    CONSTRAINT ck_inbound_scan_job_outcome CHECK (
        outcome_status IS NULL OR outcome_status IN (
            'landed','observed','rejected','failed','duplicate'
        )),
    CONSTRAINT ck_inbound_scan_job_terminal_evidence CHECK (
        (state IN ('SUCCEEDED','REJECTED','DEAD_LETTER')
         AND finished_at IS NOT NULL)
        OR (state NOT IN ('SUCCEEDED','REJECTED','DEAD_LETTER'))
    ),
    CONSTRAINT ck_inbound_scan_job_dead_letter_evidence CHECK (
        state <> 'DEAD_LETTER'
        OR (
            attempt_count = max_attempts
            AND error_code IS NOT NULL
            AND recovery_evidence IS NOT NULL
        )
    )
);

CREATE TABLE IF NOT EXISTS app.inbound_scan_job_attempts (
    job_id TEXT NOT NULL REFERENCES app.inbound_scan_jobs(id) ON DELETE RESTRICT,
    recovery_count INT NOT NULL CHECK (recovery_count >= 0),
    attempt_no INT NOT NULL CHECK (attempt_no >= 1),
    state TEXT NOT NULL CHECK (state IN (
        'RUNNING','SUCCEEDED','REJECTED','RETRY_WAIT','DEAD_LETTER'
    )),
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    PRIMARY KEY (job_id, recovery_count, attempt_no),
    CONSTRAINT ck_inbound_scan_attempt_finished CHECK (
        (state='RUNNING' AND finished_at IS NULL)
        OR (state<>'RUNNING' AND finished_at IS NOT NULL)
    )
);

CREATE TABLE IF NOT EXISTS app.inbound_scan_job_recoveries (
    job_id TEXT NOT NULL REFERENCES app.inbound_scan_jobs(id) ON DELETE RESTRICT,
    recovery_no INT NOT NULL CHECK (recovery_no >= 1),
    actor TEXT NOT NULL CHECK (length(actor) BETWEEN 1 AND 200),
    trace_id TEXT NOT NULL CHECK (trace_id ~ '^[0-9a-f]{32}$'),
    prior_error_code TEXT NOT NULL,
    prior_recovery_evidence JSONB NOT NULL,
    recovered_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (job_id, recovery_no)
);

CREATE INDEX IF NOT EXISTS idx_inbound_scan_jobs_dispatch
    ON app.inbound_scan_jobs (state, queued_at)
    WHERE state IN ('QUEUED','RETRY_WAIT');
CREATE INDEX IF NOT EXISTS idx_inbound_scan_jobs_datastream
    ON app.inbound_scan_jobs (datastream_id, queued_at DESC);

CREATE OR REPLACE FUNCTION app.protect_inbound_scan_job()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $body$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'inbound scan job evidence may not be deleted'
            USING ERRCODE = '23000';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'QUEUED' OR NEW.attempt_count <> 0 THEN
            RAISE EXCEPTION 'inbound scan job must start queued'
                USING ERRCODE = '23000';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM app.datastreams d
            WHERE d.id=NEW.datastream_id AND d.org_id=NEW.org_id
        ) THEN
            RAISE EXCEPTION 'inbound scan job scope is inconsistent'
                USING ERRCODE = '23000';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM app.inbound_receipts r
            WHERE r.id=NEW.receipt_id AND r.datastream_id=NEW.datastream_id
        ) THEN
            RAISE EXCEPTION 'inbound scan job receipt scope is inconsistent'
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.datastream_id IS DISTINCT FROM OLD.datastream_id
       OR NEW.receipt_id IS DISTINCT FROM OLD.receipt_id
       OR NEW.attachment_ordinal IS DISTINCT FROM OLD.attachment_ordinal
       OR NEW.manifest_uri IS DISTINCT FROM OLD.manifest_uri
       OR NEW.job_policy_version IS DISTINCT FROM OLD.job_policy_version
       OR NEW.max_attempts IS DISTINCT FROM OLD.max_attempts
       OR NEW.queued_at IS DISTINCT FROM OLD.queued_at
          AND NOT (OLD.state='DEAD_LETTER' AND NEW.state='QUEUED') THEN
        RAISE EXCEPTION 'inbound scan job identity is immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.raw_import_id IS NOT NULL
       AND NEW.raw_import_id IS DISTINCT FROM OLD.raw_import_id THEN
        RAISE EXCEPTION 'inbound scan job raw evidence is immutable'
            USING ERRCODE = '23000';
    END IF;
    IF NEW.raw_import_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM app.inbound_raw_imports r
        WHERE r.id=NEW.raw_import_id
          AND r.receipt_id=NEW.receipt_id
          AND r.datastream_id=NEW.datastream_id
    ) THEN
        RAISE EXCEPTION 'inbound scan job raw scope is inconsistent'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.state IN ('SUCCEEDED','REJECTED') THEN
        RAISE EXCEPTION 'terminal scan job evidence is immutable'
            USING ERRCODE = '23000';
    END IF;
    IF NOT (
        (OLD.state='QUEUED' AND NEW.state IN ('QUEUED','RUNNING'))
        OR (OLD.state='RUNNING' AND NEW.state IN (
            'SUCCEEDED','REJECTED','RETRY_WAIT','DEAD_LETTER'
        ))
        OR (OLD.state='RETRY_WAIT' AND NEW.state IN ('RETRY_WAIT','RUNNING'))
        OR (OLD.state='DEAD_LETTER' AND NEW.state='QUEUED')
    ) THEN
        RAISE EXCEPTION 'illegal inbound scan job transition'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS trg_inbound_scan_job_protect ON app.inbound_scan_jobs;
CREATE TRIGGER trg_inbound_scan_job_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.inbound_scan_jobs
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_scan_job();

ALTER TABLE app.inbound_scan_jobs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbound_scan_jobs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_scan_jobs_strict ON app.inbound_scan_jobs;
CREATE POLICY inbound_scan_jobs_strict ON app.inbound_scan_jobs
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'flux', datastream_id)
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR app.epic36_has_resource_access(org_id, 'flux', datastream_id)
    );


CREATE OR REPLACE FUNCTION app.protect_inbound_scan_attempt()
RETURNS TRIGGER LANGUAGE plpgsql AS $body$
BEGIN
    IF TG_OP='DELETE' THEN
        RAISE EXCEPTION 'inbound scan attempt evidence may not be deleted'
            USING ERRCODE='23000';
    END IF;
    IF TG_OP='INSERT' THEN
        IF NOT EXISTS (
            SELECT 1 FROM app.inbound_scan_jobs j
            WHERE j.id=NEW.job_id
              AND j.recovery_count=NEW.recovery_count
              AND j.attempt_count=NEW.attempt_no
              AND j.state='RUNNING'
        ) THEN
            RAISE EXCEPTION 'inbound scan attempt scope is inconsistent'
                USING ERRCODE='23000';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.recovery_count IS DISTINCT FROM OLD.recovery_count
       OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no
       OR NEW.started_at IS DISTINCT FROM OLD.started_at
       OR OLD.state <> 'RUNNING'
       OR NEW.state NOT IN ('SUCCEEDED','REJECTED','RETRY_WAIT','DEAD_LETTER') THEN
        RAISE EXCEPTION 'inbound scan attempt evidence is immutable'
            USING ERRCODE='23000';
    END IF;
    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS trg_inbound_scan_attempt_protect
    ON app.inbound_scan_job_attempts;
CREATE TRIGGER trg_inbound_scan_attempt_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.inbound_scan_job_attempts
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_scan_attempt();

CREATE OR REPLACE FUNCTION app.protect_inbound_scan_recovery()
RETURNS TRIGGER LANGUAGE plpgsql AS $body$
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        RAISE EXCEPTION 'inbound scan recovery evidence is append-only'
            USING ERRCODE='23000';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM app.inbound_scan_jobs j
        WHERE j.id=NEW.job_id AND j.state='DEAD_LETTER'
          AND NEW.recovery_no=j.recovery_count+1
    ) THEN
        RAISE EXCEPTION 'inbound scan recovery scope is inconsistent'
            USING ERRCODE='23000';
    END IF;
    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS trg_inbound_scan_recovery_protect
    ON app.inbound_scan_job_recoveries;
CREATE TRIGGER trg_inbound_scan_recovery_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.inbound_scan_job_recoveries
    FOR EACH ROW EXECUTE FUNCTION app.protect_inbound_scan_recovery();

ALTER TABLE app.inbound_scan_job_attempts ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbound_scan_job_attempts FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_scan_job_attempts_strict
    ON app.inbound_scan_job_attempts;
CREATE POLICY inbound_scan_job_attempts_strict
    ON app.inbound_scan_job_attempts
    USING (EXISTS (
        SELECT 1 FROM app.inbound_scan_jobs j WHERE j.id=job_id
          AND (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
               OR app.epic36_has_resource_access(j.org_id, 'flux', j.datastream_id))
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM app.inbound_scan_jobs j WHERE j.id=job_id
          AND (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
               OR app.epic36_has_resource_access(j.org_id, 'flux', j.datastream_id))
    ));

ALTER TABLE app.inbound_scan_job_recoveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.inbound_scan_job_recoveries FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS inbound_scan_job_recoveries_strict
    ON app.inbound_scan_job_recoveries;
CREATE POLICY inbound_scan_job_recoveries_strict
    ON app.inbound_scan_job_recoveries
    USING (EXISTS (
        SELECT 1 FROM app.inbound_scan_jobs j WHERE j.id=job_id
          AND (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
               OR app.epic36_has_resource_access(j.org_id, 'flux', j.datastream_id))
    ))
    WITH CHECK (EXISTS (
        SELECT 1 FROM app.inbound_scan_jobs j WHERE j.id=job_id
          AND (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
               OR app.epic36_has_resource_access(j.org_id, 'flux', j.datastream_id))
    ));
COMMIT;
