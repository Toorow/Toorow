-- Story 65.8: authenticated and anonymous feedback share one review identity,
-- while delivery eligibility and compatible classifications remain immutable.
BEGIN;

-- A Share feedback row needs a scoped identity before it can be referenced.
ALTER TABLE IF EXISTS app.feedback_review_subjects
    DROP CONSTRAINT IF EXISTS fk_feedback_review_subjects_share;

ALTER TABLE app.render_share_feedback
    ADD COLUMN IF NOT EXISTS observed_surface TEXT,
    ALTER COLUMN observed_surface DROP DEFAULT,
    ALTER COLUMN observed_surface DROP NOT NULL,
    DROP CONSTRAINT IF EXISTS uq_render_share_feedback_scope,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_observed_surface,
    ADD CONSTRAINT uq_render_share_feedback_scope UNIQUE (id, org_id, project_id),
    ADD CONSTRAINT ck_render_share_feedback_observed_surface
        CHECK (observed_surface IS NULL OR observed_surface = 'share');

-- Historical Share rows did not capture an observed surface and remain NULL.
-- Every new exact row gets the server-owned fixed Share surface before the
-- eligibility guard runs; no caller value is trusted and no old row is rewritten.
CREATE OR REPLACE FUNCTION app.set_exact_share_feedback_surface()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, app
AS $$
BEGIN
    NEW.observed_surface := 'share';
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_00_render_share_feedback_surface
    ON app.render_share_feedback;
CREATE TRIGGER trg_00_render_share_feedback_surface
BEFORE INSERT ON app.render_share_feedback
FOR EACH ROW EXECUTE FUNCTION app.set_exact_share_feedback_surface();

-- Existing Evaluation Cases remain historical/unmatched. New producers may pin
-- the exact Result-owned classification without moving verdicts out of their
-- existing tables.
ALTER TABLE app.evaluation_run_cases
    ADD COLUMN IF NOT EXISTS result_classification_hash TEXT,
    DROP CONSTRAINT IF EXISTS ck_evaluation_run_cases_classification_hash,
    ADD CONSTRAINT ck_evaluation_run_cases_classification_hash CHECK (
        result_classification_hash IS NULL
        OR result_classification_hash ~ '^[0-9a-f]{64}$'
    );

-- A rolling deployment has two exact-feedback writers for a short period. The
-- pre-251 writer cannot name the new eligibility contract; the current writer
-- does so explicitly. NULL remains a deliberate compatibility state for rows
-- created by the old binary and can be closed by a later migration after drain.
ALTER TABLE app.feedback_annotations
    ADD COLUMN IF NOT EXISTS eligibility_schema_version TEXT,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_eligibility_schema,
    ADD CONSTRAINT ck_feedback_annotations_eligibility_schema CHECK (
        eligibility_schema_version IS NULL
        OR (
            target_schema_version = 'exact-feedback.v1'
            AND eligibility_schema_version = 'feedback-eligibility.v1'
        )
    );

ALTER TABLE app.render_share_feedback
    ADD COLUMN IF NOT EXISTS eligibility_schema_version TEXT,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_eligibility_schema,
    ADD CONSTRAINT ck_render_share_feedback_eligibility_schema CHECK (
        eligibility_schema_version IS NULL
        OR (
            target_schema_version = 'exact-feedback.v1'
            AND eligibility_schema_version = 'feedback-eligibility.v1'
        )
    );

-- ---------------------------------------------------------------------------
-- One review subject, exactly one immutable source row.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.feedback_review_subjects (
    subject_id                 TEXT        NOT NULL,
    org_id                     TEXT        NOT NULL,
    project_id                 TEXT        NOT NULL,
    authenticated_feedback_id TEXT,
    share_feedback_id         TEXT,
    created_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_review_subjects PRIMARY KEY (subject_id),
    CONSTRAINT uq_feedback_review_subjects_scope
        UNIQUE (subject_id, org_id, project_id),
    CONSTRAINT uq_feedback_review_subjects_authenticated
        UNIQUE (authenticated_feedback_id, org_id, project_id),
    CONSTRAINT uq_feedback_review_subjects_share
        UNIQUE (share_feedback_id, org_id, project_id),
    CONSTRAINT ck_feedback_review_subjects_one_source CHECK (
        num_nonnulls(authenticated_feedback_id, share_feedback_id) = 1
    ),
    CONSTRAINT fk_feedback_review_subjects_authenticated
        FOREIGN KEY (authenticated_feedback_id, org_id, project_id)
        REFERENCES app.feedback_annotations (id, org_id, project_id),
    CONSTRAINT fk_feedback_review_subjects_share
        FOREIGN KEY (share_feedback_id, org_id, project_id)
        REFERENCES app.render_share_feedback (id, org_id, project_id)
);

-- CREATE TABLE IF NOT EXISTS leaves a pre-existing FK untouched, so restore
-- the dependency explicitly after repairing the Share scoped identity.
ALTER TABLE app.feedback_review_subjects
    DROP CONSTRAINT IF EXISTS fk_feedback_review_subjects_share,
    ADD CONSTRAINT fk_feedback_review_subjects_share
        FOREIGN KEY (share_feedback_id, org_id, project_id)
        REFERENCES app.render_share_feedback (id, org_id, project_id);

INSERT INTO app.feedback_review_subjects
    (subject_id, org_id, project_id, authenticated_feedback_id)
SELECT id, org_id, project_id, id
FROM app.feedback_annotations
ON CONFLICT (subject_id) DO NOTHING;

INSERT INTO app.feedback_review_subjects
    (subject_id, org_id, project_id, share_feedback_id)
SELECT id, org_id, project_id, id
FROM app.render_share_feedback
ON CONFLICT (subject_id) DO NOTHING;

ALTER TABLE app.feedback_reviews
    DROP CONSTRAINT IF EXISTS fk_feedback_reviews_annotation;
ALTER TABLE app.feedback_reviews
    DROP CONSTRAINT IF EXISTS fk_feedback_reviews_subject;
ALTER TABLE app.feedback_reviews
    ADD CONSTRAINT fk_feedback_reviews_subject
    FOREIGN KEY (feedback_id, org_id, project_id)
    REFERENCES app.feedback_review_subjects (subject_id, org_id, project_id);

-- A review head exists from the first moment for BOTH source stores. Existing
-- authenticated heads keep their ids and versions byte-for-byte.
INSERT INTO app.feedback_reviews
    (feedback_id, org_id, project_id, current_review_version_id, current_state, updated_at)
SELECT subject_id, org_id, project_id, NULL, 'unreviewed', created_at
FROM app.feedback_review_subjects
ON CONFLICT (feedback_id) DO NOTHING;

CREATE OR REPLACE FUNCTION app.seed_feedback_review_subject()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, app
AS $$
BEGIN
    IF TG_TABLE_NAME = 'feedback_annotations' THEN
        INSERT INTO app.feedback_review_subjects
            (subject_id, org_id, project_id, authenticated_feedback_id)
        VALUES (NEW.id, NEW.org_id, NEW.project_id, NEW.id);
    ELSIF TG_TABLE_NAME = 'render_share_feedback' THEN
        INSERT INTO app.feedback_review_subjects
            (subject_id, org_id, project_id, share_feedback_id)
        VALUES (NEW.id, NEW.org_id, NEW.project_id, NEW.id);
    ELSE
        RAISE EXCEPTION 'unsupported feedback subject source';
    END IF;
    INSERT INTO app.feedback_reviews
        (feedback_id, org_id, project_id, current_review_version_id,
         current_state, updated_at)
    VALUES (NEW.id, NEW.org_id, NEW.project_id, NULL, 'unreviewed', NOW());
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_feedback_annotations_seed_review_subject
    ON app.feedback_annotations;
CREATE TRIGGER trg_feedback_annotations_seed_review_subject
AFTER INSERT ON app.feedback_annotations
FOR EACH ROW EXECUTE FUNCTION app.seed_feedback_review_subject();

DROP TRIGGER IF EXISTS trg_render_share_feedback_seed_review_subject
    ON app.render_share_feedback;
CREATE TRIGGER trg_render_share_feedback_seed_review_subject
AFTER INSERT ON app.render_share_feedback
FOR EACH ROW EXECUTE FUNCTION app.seed_feedback_review_subject();

-- ---------------------------------------------------------------------------
-- Exact delivery observations. Result ids deliberately do not participate in
-- compatibility_key; they remain here only as the immutable evidence target.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.feedback_eligible_observations (
    id                            TEXT        NOT NULL,
    org_id                        TEXT        NOT NULL,
    project_id                    TEXT        NOT NULL,
    source                        TEXT        NOT NULL,
    observed_surface              TEXT        NOT NULL,
    interaction_ref               TEXT        NOT NULL,
    result_id                     TEXT        NOT NULL,
    result_content_hash           TEXT        NOT NULL,
    render_ref                    TEXT,
    visualization_spec_version_id TEXT,
    renderer_build_id             TEXT,
    runtime_build_id              TEXT,
    theme_version                 TEXT,
    formatter_version             TEXT,
    authority                     JSONB       NOT NULL,
    classification                JSONB       NOT NULL,
    classification_hash           TEXT        NOT NULL,
    compatibility_preimage        JSONB       NOT NULL,
    compatibility_key             TEXT        NOT NULL,
    observed_at                    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_eligible_observations PRIMARY KEY (id),
    CONSTRAINT ck_feedback_eligible_observations_id
        CHECK (id ~ '^fbe_[0-9A-HJKMNP-TV-Z]{26}$'),
    CONSTRAINT uq_feedback_eligible_observations_scope
        UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_feedback_eligible_observations_interaction
        UNIQUE (org_id, project_id, source, interaction_ref),
    CONSTRAINT fk_feedback_eligible_observations_result
        FOREIGN KEY (result_id, org_id, project_id, result_content_hash)
        REFERENCES app.query_results (id, org_id, project_id, content_hash),
    CONSTRAINT fk_feedback_eligible_observations_spec
        FOREIGN KEY (visualization_spec_version_id, org_id, project_id)
        REFERENCES app.visualization_spec_versions (id, org_id, project_id),
    CONSTRAINT fk_feedback_eligible_observations_render
        FOREIGN KEY (render_ref, org_id, project_id, result_id, result_content_hash,
                     visualization_spec_version_id, renderer_build_id, runtime_build_id,
                     theme_version, formatter_version)
        REFERENCES app.renders
            (id, org_id, project_id, result_id, result_content_hash,
             visualization_spec_version_id, renderer_build_id, runtime_build_id,
             theme_version, formatter_version),
    CONSTRAINT ck_feedback_eligible_observations_source
        CHECK (source IN ('authenticated', 'anonymous_share')),
    CONSTRAINT ck_feedback_eligible_observations_surface
        CHECK (observed_surface IN ('console', 'mcp_app', 'share')),
    CONSTRAINT ck_feedback_eligible_observations_source_surface CHECK (
        (source = 'anonymous_share' AND observed_surface = 'share')
        OR (source = 'authenticated' AND observed_surface IN ('console', 'mcp_app'))
    ),
    CONSTRAINT ck_feedback_eligible_observations_interaction
        CHECK (length(btrim(interaction_ref)) BETWEEN 1 AND 200),
    CONSTRAINT ck_feedback_eligible_observations_hashes CHECK (
        result_content_hash ~ '^[0-9a-f]{64}$'
        AND classification_hash ~ '^[0-9a-f]{64}$'
        AND compatibility_key ~ '^[0-9a-f]{64}$'
    ),
    CONSTRAINT ck_feedback_eligible_observations_documents CHECK (
        jsonb_typeof(authority) = 'object'
        AND classification->>'schema_version' = 'evaluation-classification.v1'
        AND classification->>'classification_hash' = classification_hash
        AND compatibility_preimage->>'schema_version' = 'feedback-compatibility.v1'
    ),
    CONSTRAINT ck_feedback_eligible_observations_render_pins CHECK (
        (visualization_spec_version_id IS NULL AND renderer_build_id IS NULL
         AND runtime_build_id IS NULL AND theme_version IS NULL AND formatter_version IS NULL)
        OR
        (visualization_spec_version_id IS NOT NULL AND renderer_build_id IS NOT NULL
         AND runtime_build_id IS NOT NULL AND theme_version IS NOT NULL
         AND formatter_version IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_feedback_eligible_observations_project_time
    ON app.feedback_eligible_observations
       (project_id, observed_at DESC, interaction_ref);
CREATE INDEX IF NOT EXISTS idx_feedback_eligible_observations_compatibility
    ON app.feedback_eligible_observations
       (project_id, compatibility_key, observed_at DESC);

-- A predecessor and an idempotency receipt must stay inside one subject's
-- lineage.  Separate subject/version foreign keys would permit cross-subject
-- chains that the service could neither replay nor repair.
ALTER TABLE IF EXISTS app.feedback_review_retries
    DROP CONSTRAINT IF EXISTS fk_feedback_review_retries_version;
ALTER TABLE app.feedback_reviews
    DROP CONSTRAINT IF EXISTS fk_feedback_reviews_current_version;

ALTER TABLE app.feedback_review_versions
    DROP CONSTRAINT IF EXISTS fk_feedback_review_versions_predecessor,
    DROP CONSTRAINT IF EXISTS uq_feedback_review_versions_subject_identity,
    ADD CONSTRAINT uq_feedback_review_versions_subject_identity
        UNIQUE (id, feedback_id, org_id, project_id),
    ADD CONSTRAINT fk_feedback_review_versions_predecessor
        FOREIGN KEY (predecessor_version_id, feedback_id, org_id, project_id)
        REFERENCES app.feedback_review_versions
            (id, feedback_id, org_id, project_id);

ALTER TABLE app.feedback_reviews
    ADD CONSTRAINT fk_feedback_reviews_current_version
        FOREIGN KEY (current_review_version_id, feedback_id, org_id, project_id)
        REFERENCES app.feedback_review_versions
            (id, feedback_id, org_id, project_id)
        DEFERRABLE INITIALLY DEFERRED;

-- Only the retry-key hash is retained. The immutable version row contains the
-- exact command fields and predecessor used to detect a changed replay.
CREATE TABLE IF NOT EXISTS app.feedback_review_retries (
    subject_id       TEXT        NOT NULL,
    org_id           TEXT        NOT NULL,
    project_id       TEXT        NOT NULL,
    reviewer         TEXT        NOT NULL,
    retry_key_hash   TEXT        NOT NULL,
    review_version_id TEXT       NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_feedback_review_retries
        PRIMARY KEY (subject_id, reviewer, retry_key_hash),
    CONSTRAINT uq_feedback_review_retries_scope
        UNIQUE (subject_id, reviewer, retry_key_hash, org_id, project_id),
    CONSTRAINT fk_feedback_review_retries_subject
        FOREIGN KEY (subject_id, org_id, project_id)
        REFERENCES app.feedback_review_subjects (subject_id, org_id, project_id),
    CONSTRAINT fk_feedback_review_retries_version
        FOREIGN KEY (review_version_id, subject_id, org_id, project_id)
        REFERENCES app.feedback_review_versions
            (id, feedback_id, org_id, project_id),
    CONSTRAINT ck_feedback_review_retries_reviewer
        CHECK (length(btrim(reviewer)) BETWEEN 1 AND 200),
    CONSTRAINT ck_feedback_review_retries_hash
        CHECK (retry_key_hash ~ '^[0-9a-f]{64}$')
);

-- CREATE TABLE IF NOT EXISTS does not repair a constraint on a pre-existing
-- table, so replace it explicitly after both referenced relations exist.
ALTER TABLE app.feedback_review_retries
    DROP CONSTRAINT IF EXISTS fk_feedback_review_retries_version,
    ADD CONSTRAINT fk_feedback_review_retries_version
        FOREIGN KEY (review_version_id, subject_id, org_id, project_id)
        REFERENCES app.feedback_review_versions
            (id, feedback_id, org_id, project_id);

-- A feedback insert without a previously delivered exact interaction would
-- make the aggregate denominator unknowable. Historical rows are untouched.
CREATE OR REPLACE FUNCTION app.require_feedback_eligible_observation()
RETURNS TRIGGER
LANGUAGE plpgsql
SET search_path = pg_catalog, app
AS $$
DECLARE
    v_source TEXT;
    v_surface TEXT;
    v_render TEXT;
    v_renderer TEXT;
    v_runtime TEXT;
BEGIN
    IF NEW.target_schema_version IS DISTINCT FROM 'exact-feedback.v1' THEN
        RETURN NEW;
    END IF;
    IF NEW.eligibility_schema_version IS NULL
       AND current_setting('app.feedback_eligibility_writer', TRUE)
            = 'feedback-eligibility.v1' THEN
        NEW.eligibility_schema_version := 'feedback-eligibility.v1';
    END IF;
    IF NEW.eligibility_schema_version IS DISTINCT FROM 'feedback-eligibility.v1' THEN
        -- Rolling-deploy compatibility for the drained pre-251 binary only.
        RETURN NEW;
    END IF;
    IF TG_TABLE_NAME = 'feedback_annotations' THEN
        v_source := 'authenticated';
        v_surface := NEW.observed_surface;
        v_render := NEW.render_ref;
        v_renderer := NEW.renderer_build_id;
        v_runtime := NEW.runtime_build_id;
    ELSE
        v_source := 'anonymous_share';
        v_surface := NEW.observed_surface;
        v_render := NEW.render_id;
        v_renderer := NEW.renderer_build;
        v_runtime := NEW.runtime_build;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM app.feedback_eligible_observations e
        WHERE e.org_id = NEW.org_id AND e.project_id = NEW.project_id
          AND e.source = v_source AND e.observed_surface = v_surface
          AND e.interaction_ref = NEW.interaction_ref
          AND e.result_id = NEW.result_id
          AND e.result_content_hash = NEW.result_content_hash
          AND e.render_ref IS NOT DISTINCT FROM v_render
          AND e.visualization_spec_version_id
              IS NOT DISTINCT FROM NEW.visualization_spec_version_id
          AND e.renderer_build_id IS NOT DISTINCT FROM v_renderer
          AND e.runtime_build_id IS NOT DISTINCT FROM v_runtime
          AND e.theme_version IS NOT DISTINCT FROM NEW.theme_version
          AND e.formatter_version IS NOT DISTINCT FROM NEW.formatter_version
    ) THEN
        RAISE EXCEPTION 'feedback interaction was not delivered as eligible'
            USING ERRCODE = '23503';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_feedback_annotations_require_eligibility
    ON app.feedback_annotations;
CREATE TRIGGER trg_feedback_annotations_require_eligibility
BEFORE INSERT ON app.feedback_annotations
FOR EACH ROW EXECUTE FUNCTION app.require_feedback_eligible_observation();

DROP TRIGGER IF EXISTS trg_render_share_feedback_require_eligibility
    ON app.render_share_feedback;
CREATE TRIGGER trg_render_share_feedback_require_eligibility
BEFORE INSERT ON app.render_share_feedback
FOR EACH ROW EXECUTE FUNCTION app.require_feedback_eligible_observation();

-- ---------------------------------------------------------------------------
-- Public Share delivery records eligibility through one fixed-search-path
-- definer. The public role receives EXECUTE and no table read.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.feedback_canonical_json(p_value JSONB)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
STRICT
SET search_path = pg_catalog
AS $$
    SELECT CASE jsonb_typeof(p_value)
        WHEN 'object' THEN '{' || COALESCE((
            SELECT string_agg(to_json(key)::TEXT || ':' || app.feedback_canonical_json(value),
                              ',' ORDER BY key)
            FROM jsonb_each(p_value)
        ), '') || '}'
        WHEN 'array' THEN '[' || COALESCE((
            SELECT string_agg(app.feedback_canonical_json(value), ',' ORDER BY ordinal)
            FROM jsonb_array_elements(p_value) WITH ORDINALITY AS item(value, ordinal)
        ), '') || ']'
        ELSE p_value::TEXT
    END
$$;

-- SECURITY DEFINER must be owned by the controlled migration principal, never
-- either runtime door. BYPASSRLS (or superuser in the disposable/local runner)
-- is required because the Share role itself intentionally has zero table read.
DO $$
DECLARE
    v_super BOOLEAN;
    v_bypass BOOLEAN;
BEGIN
    SELECT rolsuper, rolbypassrls INTO v_super, v_bypass
      FROM pg_roles WHERE rolname = current_user;
    IF current_user IN ('connector', 'toorow_share_reader')
       OR NOT (COALESCE(v_super, FALSE) OR COALESCE(v_bypass, FALSE)) THEN
        RAISE EXCEPTION 'Share eligibility definer requires controlled BYPASSRLS owner';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION app.record_render_share_feedback_eligibility_v1(
    p_session_hash TEXT,
    p_interaction_ref TEXT
)
RETURNS TABLE(status TEXT, interaction_ref TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
DECLARE
    v_session RECORD;
    v_classification JSONB;
    v_classification_hash TEXT;
    v_authority JSONB;
    v_preimage JSONB;
    v_compatibility_key TEXT;
    v_existing TEXT;
    v_allowed_fields TEXT[];
    v_path_ordinals JSONB;
BEGIN
    IF p_session_hash !~ '^[0-9a-f]{64}$'
       OR p_interaction_ref !~ '^afi_[0-9A-HJKMNP-TV-Z]{26}$' THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
    END IF;

    SELECT x.org_id, x.project_id, r.id AS render_id, r.result_id,
           r.result_content_hash, r.visualization_spec_version_id,
           r.renderer_build_id, r.runtime_build_id, r.theme_version,
           r.formatter_version, qr.query_spec_version_id, fp.result_manifest AS manifest,
           fp.result_schema, fp.rows_chunk, fp.ai_path_evidence, fp.spec_document
      INTO v_session
      FROM app.render_share_exchange_sessions x
      JOIN app.render_shares s
        ON s.id = x.share_id AND s.org_id = x.org_id AND s.project_id = x.project_id
      JOIN app.renders r
        ON r.id = s.render_id AND r.org_id = s.org_id AND r.project_id = s.project_id
      JOIN app.query_results qr
        ON qr.id = r.result_id AND qr.org_id = r.org_id AND qr.project_id = r.project_id
       AND qr.content_hash = r.result_content_hash
      JOIN app.render_frozen_payloads fp
        ON fp.render_id = r.id AND fp.org_id = r.org_id AND fp.project_id = r.project_id
     WHERE x.session_hash = p_session_hash
       AND x.expires_at > NOW() AND s.state = 'active' AND s.expires_at > NOW()
     FOR UPDATE OF x, s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = 'P0002';
    END IF;

    IF jsonb_array_length(COALESCE(v_session.rows_chunk, '[]'::jsonb)) > 1000 THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
    END IF;

    SELECT COALESCE(array_agg(DISTINCT field_name ORDER BY field_name), ARRAY[]::TEXT[])
      INTO v_allowed_fields
      FROM (
        SELECT sf->>'name' AS field_name
          FROM jsonb_array_elements(COALESCE(v_session.result_schema->'fields', '[]'::jsonb)) sf
         WHERE COALESCE((sf->>'hidden')::boolean, FALSE) IS FALSE
           AND COALESCE(sf->>'role', '') NOT IN ('internal', 'provenance')
           AND sf->>'name' NOT LIKE '\_%' ESCAPE '\'
           AND EXISTS (
                SELECT 1
                  FROM (
                    SELECT CASE
                        WHEN pv->>'member_id' = bound.member_id
                        THEN pv->>'source_field'
                        ELSE bound.member_id
                    END AS source_id
                      FROM (
                        SELECT bound_value #>> '{}' AS member_id
                          FROM jsonb_each(COALESCE(v_session.spec_document->'bindings', '{}'::jsonb)) b,
                               LATERAL jsonb_array_elements(
                                   CASE jsonb_typeof(b.value)
                                     WHEN 'array' THEN b.value
                                     ELSE jsonb_build_array(b.value)
                                   END
                               ) bound_value
                        UNION
                        SELECT datum_value #>> '{}'
                          FROM jsonb_array_elements(
                              COALESCE(v_session.spec_document->'evidence'->'datum_fields', '[]'::jsonb)
                          ) datum_value
                      ) bound
                      LEFT JOIN LATERAL jsonb_array_elements(
                          COALESCE(v_session.manifest->'provenance'->'values', '[]'::jsonb)
                      ) pv ON pv->>'member_id' = bound.member_id
                  ) mapped
                 WHERE mapped.source_id IN (sf->>'id', sf->>'name')
           )
      ) safe_fields;
    IF cardinality(v_allowed_fields) > 150 THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
    END IF;

    SELECT COALESCE(
        jsonb_agg((step->>'ordinal')::INTEGER ORDER BY (step->>'ordinal')::INTEGER),
        '[]'::jsonb
    )
      INTO v_path_ordinals
      FROM jsonb_array_elements(
          COALESCE(v_session.ai_path_evidence->'steps', '[]'::jsonb)
      ) step;
    IF jsonb_array_length(v_path_ordinals) > 200 THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
    END IF;

    v_classification := v_session.manifest->'evaluation_classification';
    IF v_classification->>'schema_version' IS DISTINCT FROM 'evaluation-classification.v1'
       OR v_classification->>'classification_hash' IS NULL
       OR v_classification->>'classification_hash' IS DISTINCT FROM encode(
            sha256(convert_to(app.feedback_canonical_json(
                v_classification - 'classification_hash'
            ), 'UTF8')), 'hex'
       ) THEN
        v_classification := jsonb_build_object(
            'schema_version', 'evaluation-classification.v1',
            'semantic_view', jsonb_build_object('state', 'unavailable',
                                                'reason', 'historical_result_unclassified'),
            'business_domains', jsonb_build_object('state', 'unavailable',
                                                   'reason', 'historical_result_unclassified'),
            'skills', jsonb_build_object('state', 'unavailable',
                                         'reason', 'historical_result_unclassified'),
            'capability', jsonb_build_object('state', 'unavailable',
                                             'reason', 'historical_result_unclassified'),
            'result_type', jsonb_build_object('state', 'unavailable',
                                              'reason', 'historical_result_unclassified')
        );
        v_classification_hash := encode(
            sha256(convert_to(app.feedback_canonical_json(v_classification), 'UTF8')), 'hex'
        );
        v_classification := v_classification
            || jsonb_build_object('classification_hash', v_classification_hash);
    ELSE
        v_classification_hash := v_classification->>'classification_hash';
    END IF;

    v_authority := jsonb_build_object(
        'delivered_rows', jsonb_build_object(
            'start', 0,
            'count', jsonb_array_length(COALESCE(v_session.rows_chunk, '[]'::jsonb)),
            'field_count', cardinality(v_allowed_fields),
            'fields_hash', encode(sha256(convert_to(
                array_to_json(v_allowed_fields)::TEXT, 'UTF8'
            )), 'hex')
        ),
        'ai_path_id', v_session.ai_path_evidence->>'path_id',
        'path_step_ordinals', v_path_ordinals,
        'target_kinds',
        to_jsonb(ARRAY['answer']::TEXT[])
            || CASE
                WHEN jsonb_array_length(COALESCE(v_session.rows_chunk, '[]'::jsonb)) > 0
                 AND cardinality(v_allowed_fields) > 0
                THEN to_jsonb(ARRAY['datum']::TEXT[])
                ELSE '[]'::jsonb
               END
            || CASE
                WHEN jsonb_array_length(v_path_ordinals) > 0
                THEN to_jsonb(ARRAY['path_step']::TEXT[])
                ELSE '[]'::jsonb
               END
    );
    v_preimage := jsonb_build_object(
        'schema_version', 'feedback-compatibility.v1',
        'target_schema_version', 'exact-feedback.v1',
        'source', 'anonymous_share',
        'surface', 'share',
        'query_spec_version_id', v_session.query_spec_version_id,
        'semantic_view', v_classification->'semantic_view',
        'business_domains', v_classification->'business_domains',
        'skills', v_classification->'skills',
        'capability', v_classification->'capability',
        'result_type', v_classification->'result_type',
        'render', jsonb_build_object(
            'visualization_spec_version_id', v_session.visualization_spec_version_id,
            'renderer_build_id', v_session.renderer_build_id,
            'runtime_build_id', v_session.runtime_build_id,
            'theme_version', v_session.theme_version,
            'formatter_version', v_session.formatter_version
        )
    );
    v_compatibility_key := encode(sha256(convert_to(
        app.feedback_canonical_json(v_preimage), 'UTF8'
    )), 'hex');

    INSERT INTO app.feedback_eligible_observations
        (id, org_id, project_id, source, observed_surface, interaction_ref,
         result_id, result_content_hash, render_ref,
         visualization_spec_version_id, renderer_build_id, runtime_build_id,
         theme_version, formatter_version, authority, classification,
         classification_hash, compatibility_preimage, compatibility_key)
    VALUES
        (regexp_replace(p_interaction_ref, '^afi_', 'fbe_'),
         v_session.org_id, v_session.project_id,
         'anonymous_share', 'share', p_interaction_ref,
         v_session.result_id, v_session.result_content_hash, v_session.render_id,
         v_session.visualization_spec_version_id, v_session.renderer_build_id,
         v_session.runtime_build_id, v_session.theme_version, v_session.formatter_version,
         v_authority, v_classification, v_classification_hash,
         v_preimage, v_compatibility_key)
    ON CONFLICT ON CONSTRAINT uq_feedback_eligible_observations_interaction
    DO NOTHING;

    SELECT e.id INTO v_existing
      FROM app.feedback_eligible_observations e
     WHERE e.org_id = v_session.org_id AND e.project_id = v_session.project_id
       AND e.source = 'anonymous_share' AND e.interaction_ref = p_interaction_ref
       AND e.result_id = v_session.result_id
       AND e.result_content_hash = v_session.result_content_hash
       AND e.render_ref = v_session.render_id;
    IF v_existing IS NULL THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '23505';
    END IF;
    RETURN QUERY SELECT 'recorded'::TEXT, p_interaction_ref;
END;
$$;

-- Keep the migration-249 Share call signature alive during a rolling deploy,
-- but interpose eligibility before its immutable insert. Renaming preserves the
-- already-reviewed target validation while the public name becomes the current
-- eligibility-backed writer. The transaction-local marker makes the BEFORE
-- trigger enforce the observation on the row written by the legacy body.
DO $$
BEGIN
    IF to_regprocedure(
        'app.record_render_share_feedback_legacy_v1(text,text,text,text,integer,text,integer,text,text,text,text)'
    ) IS NULL THEN
        ALTER FUNCTION app.record_render_share_feedback_v1(
            TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
        ) RENAME TO record_render_share_feedback_legacy_v1;
    END IF;
END $$;

CREATE OR REPLACE FUNCTION app.record_render_share_feedback_v1(
    p_session_hash TEXT,
    p_feedback_id TEXT,
    p_interaction_ref TEXT,
    p_target_kind TEXT,
    p_datum_row_index INTEGER,
    p_datum_field TEXT,
    p_path_step_ordinal INTEGER,
    p_polarity TEXT,
    p_comment TEXT,
    p_retry_key_hash TEXT,
    p_request_hash TEXT
)
RETURNS TABLE(status TEXT, feedback_id TEXT)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, app
AS $$
BEGIN
    PERFORM eligibility.status
      FROM app.record_render_share_feedback_eligibility_v1(
        p_session_hash, p_interaction_ref
      ) eligibility;
    PERFORM set_config(
        'app.feedback_eligibility_writer', 'feedback-eligibility.v1', TRUE
    );
    RETURN QUERY
    SELECT recorded.status, recorded.feedback_id
      FROM app.record_render_share_feedback_legacy_v1(
        p_session_hash, p_feedback_id, p_interaction_ref, p_target_kind,
        p_datum_row_index, p_datum_field, p_path_step_ordinal, p_polarity,
        p_comment, p_retry_key_hash, p_request_hash
      ) recorded;
END;
$$;

-- ---------------------------------------------------------------------------
-- Append-only evidence and tenant isolation.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.reject_feedback_review_exact_evidence_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'feedback review exact evidence is append-only'
        USING ERRCODE = '23000';
END;
$$;

DO $$
DECLARE
    target TEXT;
    guarded TEXT[] := ARRAY[
        'feedback_review_subjects',
        'feedback_eligible_observations',
        'feedback_review_retries'
    ];
BEGIN
    FOREACH target IN ARRAY guarded LOOP
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON app.%I',
                       'trg_' || target || '_immutable', target);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE UPDATE OR DELETE ON app.%I '
            'FOR EACH ROW WHEN (current_setting(''app.rgpd_erasure'', true) '
            'IS DISTINCT FROM ''on'') EXECUTE FUNCTION '
            'app.reject_feedback_review_exact_evidence_mutation()',
            'trg_' || target || '_immutable', target
        );
        EXECUTE format('DROP TRIGGER IF EXISTS %I ON app.%I',
                       'trg_' || target || '_block_truncate', target);
        EXECUTE format(
            'CREATE TRIGGER %I BEFORE TRUNCATE ON app.%I FOR EACH STATEMENT '
            'EXECUTE FUNCTION app.reject_feedback_review_exact_evidence_mutation()',
            'trg_' || target || '_block_truncate', target
        );
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', target || '_strict', target);
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

GRANT SELECT, INSERT ON app.feedback_review_subjects TO connector;
GRANT SELECT, INSERT ON app.feedback_eligible_observations TO connector;
GRANT SELECT, INSERT ON app.feedback_review_retries TO connector;

REVOKE ALL ON app.feedback_review_subjects FROM toorow_share_reader;
REVOKE ALL ON app.feedback_eligible_observations FROM toorow_share_reader;
REVOKE ALL ON app.feedback_review_retries FROM toorow_share_reader;
REVOKE ALL ON app.feedback_reviews FROM toorow_share_reader;
REVOKE ALL ON app.feedback_review_versions FROM toorow_share_reader;
REVOKE ALL ON app.evaluation_runs FROM toorow_share_reader;
REVOKE ALL ON app.evaluation_run_cases FROM toorow_share_reader;
REVOKE ALL ON app.evaluation_case_dimension_verdicts FROM toorow_share_reader;
REVOKE ALL ON FUNCTION app.record_render_share_feedback_eligibility_v1(TEXT, TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.record_render_share_feedback_eligibility_v1(TEXT, TEXT)
    TO toorow_share_reader;
REVOKE ALL ON FUNCTION app.record_render_share_feedback_legacy_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC, connector, toorow_share_reader;
REVOKE ALL ON FUNCTION app.record_render_share_feedback_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.record_render_share_feedback_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) TO toorow_share_reader;

COMMENT ON FUNCTION app.record_render_share_feedback_eligibility_v1(TEXT, TEXT) IS
    'Story 65.8 Share delivery door; controlled migration owner, fixed search path, EXECUTE only.';
COMMENT ON FUNCTION app.record_render_share_feedback_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) IS 'Story 65.8 rolling-compatible Share writer; eligibility is recorded before immutable feedback.';

COMMIT;
