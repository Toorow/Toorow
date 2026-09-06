-- Story 65.9: reviewed exact feedback becomes immutable regression evidence.
BEGIN;

ALTER TABLE app.golden_question_versions
    ADD COLUMN IF NOT EXISTS contract_version TEXT NOT NULL DEFAULT 'golden-question.v1';
ALTER TABLE app.golden_question_versions
    DROP CONSTRAINT IF EXISTS ck_golden_question_versions_contract,
    ADD CONSTRAINT ck_golden_question_versions_contract
        CHECK (contract_version IN ('golden-question.v1', 'golden-question.v2'));

CREATE OR REPLACE FUNCTION app.validate_golden_question_v2()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, app
AS $$
DECLARE
    assertion JSONB;
BEGIN
    IF NEW.contract_version <> 'golden-question.v2' THEN
        RETURN NEW;
    END IF;
    IF jsonb_array_length(NEW.expected_result) NOT BETWEEN 1 AND 32
       OR jsonb_array_length(NEW.required_provenance) < 1
       OR octet_length(jsonb_build_object(
            'contract_version', NEW.contract_version,
            'expected_result', NEW.expected_result,
            'required_provenance', NEW.required_provenance,
            'expected_ai_path', NEW.expected_ai_path
          )::TEXT) > 65536 THEN
        RAISE EXCEPTION 'golden-question.v2 exceeds a structural budget'
            USING ERRCODE = '23514';
    END IF;
    FOR assertion IN SELECT value FROM jsonb_array_elements(NEW.expected_result) LOOP
        IF jsonb_typeof(assertion) <> 'object'
           OR assertion->>'assertion_type' NOT IN
                ('value', 'row_set', 'ordering', 'cardinality', 'invariant',
                 'empty', 'degraded', 'refused')
           OR jsonb_typeof(assertion->'selectors') <> 'array'
           OR jsonb_array_length(assertion->'selectors') > 8
           OR NOT assertion ? 'tolerance' THEN
            RAISE EXCEPTION 'invalid golden-question.v2 assertion'
                USING ERRCODE = '23514';
        END IF;
    END LOOP;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_golden_question_versions_validate_v2
    ON app.golden_question_versions;
CREATE TRIGGER trg_golden_question_versions_validate_v2
BEFORE INSERT ON app.golden_question_versions
FOR EACH ROW EXECUTE FUNCTION app.validate_golden_question_v2();

ALTER TABLE app.evaluation_case_dimension_verdicts
    ADD COLUMN IF NOT EXISTS producer TEXT NOT NULL DEFAULT 'caller-declared.v1',
    ADD COLUMN IF NOT EXISTS producer_contract_hash TEXT;
ALTER TABLE app.evaluation_case_dimension_verdicts
    DROP CONSTRAINT IF EXISTS ck_evaluation_case_verdicts_producer,
    ADD CONSTRAINT ck_evaluation_case_verdicts_producer CHECK (
        (producer = 'caller-declared.v1' AND producer_contract_hash IS NULL)
        OR
        (producer = 'result-case-evaluator.v1'
         AND producer_contract_hash =
             'd8e8a0a4ccdcb2e61c25c2206a60d15ad7da306ab229c83ad1a76eaeb74b4add')
    );

CREATE TABLE IF NOT EXISTS app.feedback_regression_cases (
    id                              TEXT        NOT NULL,
    feedback_id                     TEXT        NOT NULL,
    org_id                          TEXT        NOT NULL,
    project_id                      TEXT        NOT NULL,
    review_version_id               TEXT        NOT NULL,
    golden_question_id              TEXT        NOT NULL,
    golden_question_version_id      TEXT        NOT NULL,
    result_id                       TEXT        NOT NULL,
    result_content_hash             TEXT        NOT NULL,
    query_spec_version_id           TEXT        NOT NULL,
    result_classification_hash      TEXT        NOT NULL,
    semantic_view_id                TEXT        NOT NULL,
    semantic_view_version_id        TEXT        NOT NULL,
    business_domain_id              TEXT        NOT NULL,
    business_domain_version_number  INTEGER     NOT NULL,
    capability_key                  TEXT        NOT NULL,
    capability_version_id           TEXT        NOT NULL,
    result_type                     TEXT        NOT NULL,
    eligible_target                 JSONB       NOT NULL,
    render_ref                      TEXT,
    ai_path_id                      TEXT,
    ai_path_absent_literal          TEXT,
    reproduction_reason             TEXT        NOT NULL,
    retry_key_hash                  TEXT        NOT NULL,
    request_hash                    TEXT        NOT NULL,
    created_by                      TEXT        NOT NULL,
    created_at                      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_regression_cases PRIMARY KEY (id),
    CONSTRAINT ck_feedback_regression_cases_id
        CHECK (id ~ '^frc_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_feedback_regression_cases_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_feedback_regression_cases_version
        UNIQUE (golden_question_version_id, org_id, project_id),
    CONSTRAINT uq_feedback_regression_cases_retry
        UNIQUE (feedback_id, created_by, retry_key_hash, org_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_subject
        FOREIGN KEY (feedback_id, org_id, project_id)
        REFERENCES app.feedback_review_subjects (subject_id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_review
        FOREIGN KEY (review_version_id, feedback_id, org_id, project_id)
        REFERENCES app.feedback_review_versions (id, feedback_id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_question
        FOREIGN KEY (golden_question_id, org_id, project_id)
        REFERENCES app.golden_questions (id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_question_version
        FOREIGN KEY (golden_question_version_id, org_id, project_id)
        REFERENCES app.golden_question_versions (id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_result
        FOREIGN KEY (result_id, org_id, project_id, result_content_hash)
        REFERENCES app.query_results (id, org_id, project_id, content_hash),
    CONSTRAINT fk_feedback_regression_cases_spec
        FOREIGN KEY (query_spec_version_id, org_id, project_id)
        REFERENCES app.query_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_semantic
        FOREIGN KEY (semantic_view_version_id, semantic_view_id, project_id)
        REFERENCES app.semantic_view_versions (id, view_id, project_id),
    CONSTRAINT fk_feedback_regression_cases_domain
        FOREIGN KEY (business_domain_id, business_domain_version_number)
        REFERENCES app.mdm_business_domain_versions (domain_id, version_number),
    CONSTRAINT fk_feedback_regression_cases_domain_org
        FOREIGN KEY (business_domain_id, org_id)
        REFERENCES app.mdm_business_domains (id, org_id),
    CONSTRAINT fk_feedback_regression_cases_capability
        FOREIGN KEY (project_id, capability_version_id)
        REFERENCES app.project_configuration_versions (project_id, id),
    CONSTRAINT fk_feedback_regression_cases_ai_path
        FOREIGN KEY (ai_path_id, org_id, project_id)
        REFERENCES app.ai_paths (id, org_id, project_id),
    CONSTRAINT ck_feedback_regression_cases_hashes CHECK (
        result_content_hash ~ '^[0-9a-f]{64}$'
        AND result_classification_hash ~ '^[0-9a-f]{64}$'
        AND retry_key_hash ~ '^[0-9a-f]{64}$'
        AND request_hash ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_feedback_regression_cases_target
        CHECK (jsonb_typeof(eligible_target) = 'object'),
    CONSTRAINT ck_feedback_regression_cases_path CHECK (
        (ai_path_id IS NOT NULL AND ai_path_absent_literal IS NULL)
        OR (ai_path_id IS NULL AND ai_path_absent_literal = 'No AI path')
    ),
    CONSTRAINT ck_feedback_regression_cases_reason
        CHECK (length(btrim(reproduction_reason)) BETWEEN 1 AND 2000)
);

CREATE TABLE IF NOT EXISTS app.evaluation_assertion_results (
    id                      TEXT        NOT NULL,
    case_id                 TEXT        NOT NULL,
    org_id                  TEXT        NOT NULL,
    project_id              TEXT        NOT NULL,
    assertion_ordinal       INTEGER     NOT NULL,
    assertion_type          TEXT        NOT NULL,
    verdict                 TEXT        NOT NULL,
    reason_code             TEXT        NOT NULL,
    expected_hash           TEXT        NOT NULL,
    observed_hash           TEXT,
    evidence_refs           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    producer                TEXT        NOT NULL,
    producer_contract_hash  TEXT        NOT NULL,
    retry_key_hash          TEXT        NOT NULL,
    request_hash            TEXT        NOT NULL,
    evaluated_by            TEXT        NOT NULL,
    evaluated_at            TIMESTAMPTZ NOT NULL,

    CONSTRAINT pk_evaluation_assertion_results PRIMARY KEY (id),
    CONSTRAINT ck_evaluation_assertion_results_id
        CHECK (id ~ '^ear_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_evaluation_assertion_results_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_evaluation_assertion_results_ordinal UNIQUE (case_id, assertion_ordinal),
    CONSTRAINT fk_evaluation_assertion_results_case
        FOREIGN KEY (case_id, org_id, project_id)
        REFERENCES app.evaluation_run_cases (id, org_id, project_id),
    CONSTRAINT ck_evaluation_assertion_results_type CHECK (assertion_type IN (
        'value', 'row_set', 'ordering', 'cardinality', 'invariant',
        'empty', 'degraded', 'refused'
    )),
    CONSTRAINT ck_evaluation_assertion_results_verdict
        CHECK (verdict IN ('pass', 'fail', 'unverifiable')),
    CONSTRAINT ck_evaluation_assertion_results_reason
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{2,80}$'),
    CONSTRAINT ck_evaluation_assertion_results_hashes CHECK (
        expected_hash ~ '^[0-9a-f]{64}$'
        AND (observed_hash IS NULL OR observed_hash ~ '^[0-9a-f]{64}$')
        AND retry_key_hash ~ '^[0-9a-f]{64}$'
        AND request_hash ~ '^[0-9a-f]{64}$'
        AND producer = 'result-case-evaluator.v1'
        AND producer_contract_hash =
            'd8e8a0a4ccdcb2e61c25c2206a60d15ad7da306ab229c83ad1a76eaeb74b4add'
    ),
    CONSTRAINT ck_evaluation_assertion_results_evidence
        CHECK (jsonb_typeof(evidence_refs) = 'object')
);

CREATE TABLE IF NOT EXISTS app.feedback_regression_resolutions (
    id                          TEXT        NOT NULL,
    regression_case_id          TEXT        NOT NULL,
    feedback_id                 TEXT        NOT NULL,
    org_id                      TEXT        NOT NULL,
    project_id                  TEXT        NOT NULL,
    review_version_id           TEXT        NOT NULL,
    golden_question_version_id  TEXT        NOT NULL,
    evaluation_run_id           TEXT        NOT NULL,
    evaluation_case_id          TEXT        NOT NULL,
    verdict_id                  TEXT        NOT NULL,
    affected_dimension          TEXT        NOT NULL,
    result_id                   TEXT        NOT NULL,
    result_content_hash         TEXT        NOT NULL,
    result_classification_hash  TEXT        NOT NULL,
    resolved_by                 TEXT        NOT NULL,
    resolved_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_regression_resolutions PRIMARY KEY (id),
    CONSTRAINT ck_feedback_regression_resolutions_id
        CHECK (id ~ '^frr_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_feedback_regression_resolutions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_feedback_regression_resolutions_case UNIQUE (regression_case_id),
    CONSTRAINT fk_feedback_regression_resolutions_promotion
        FOREIGN KEY (regression_case_id, org_id, project_id)
        REFERENCES app.feedback_regression_cases (id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_resolutions_review
        FOREIGN KEY (review_version_id, feedback_id, org_id, project_id)
        REFERENCES app.feedback_review_versions (id, feedback_id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_resolutions_evaluation
        FOREIGN KEY (evaluation_case_id, org_id, project_id)
        REFERENCES app.evaluation_run_cases (id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_resolutions_verdict
        FOREIGN KEY (verdict_id, org_id, project_id)
        REFERENCES app.evaluation_case_dimension_verdicts (id, org_id, project_id),
    CONSTRAINT fk_feedback_regression_resolutions_result
        FOREIGN KEY (result_id, org_id, project_id, result_content_hash)
        REFERENCES app.query_results (id, org_id, project_id, content_hash),
    CONSTRAINT ck_feedback_regression_resolutions_hash
        CHECK (result_classification_hash ~ '^[0-9a-f]{64}$')
);

CREATE INDEX IF NOT EXISTS idx_feedback_regression_cases_feedback
    ON app.feedback_regression_cases (project_id, feedback_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_evaluation_assertion_results_case
    ON app.evaluation_assertion_results (case_id, assertion_ordinal);

CREATE OR REPLACE FUNCTION app.reject_feedback_regression_evidence_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'feedback regression evidence is append-only'
        USING ERRCODE = '23000';
END;
$$;

DO $$
DECLARE
    target TEXT;
    guarded TEXT[] := ARRAY[
        'feedback_regression_cases',
        'evaluation_assertion_results',
        'feedback_regression_resolutions'
    ];
BEGIN
    FOREACH target IN ARRAY guarded LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON app.%I',
                       'trg_' || target || '_immutable', target);
        EXECUTE format('CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON app.%I '
                       'FOR EACH ROW WHEN (current_setting(''app.rgpd_erasure'', true) '
                       'IS DISTINCT FROM ''on'') EXECUTE FUNCTION '
                       'app.reject_feedback_regression_evidence_mutation()',
                       'trg_' || target || '_immutable', target);
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON app.%I',
                       'trg_' || target || '_block_truncate', target);
        EXECUTE format('CREATE TRIGGER %I BEFORE TRUNCATE ON app.%I FOR EACH STATEMENT '
                       'EXECUTE FUNCTION app.reject_feedback_regression_evidence_mutation()',
                       'trg_' || target || '_block_truncate', target);
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I',
                       target || '_strict', target);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I USING ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            target || '_strict', target
        );
    END LOOP;
END $$;

GRANT SELECT, INSERT ON app.feedback_regression_cases TO connector;
GRANT SELECT, INSERT ON app.evaluation_assertion_results TO connector;
GRANT SELECT, INSERT ON app.feedback_regression_resolutions TO connector;

REVOKE ALL ON app.feedback_regression_cases FROM PUBLIC, toorow_share_reader;
REVOKE ALL ON app.evaluation_assertion_results FROM PUBLIC, toorow_share_reader;
REVOKE ALL ON app.feedback_regression_resolutions FROM PUBLIC, toorow_share_reader;

COMMIT;
