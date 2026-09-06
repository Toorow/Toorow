-- Story 65.5: exact Analytics feedback is append-only evidence about one delivered view.
-- Existing annotations remain historical NULL-schema rows. Every future insert must be v1.
BEGIN;

ALTER TABLE app.feedback_annotations
    ADD COLUMN IF NOT EXISTS target_schema_version TEXT,
    ADD COLUMN IF NOT EXISTS result_content_hash TEXT,
    ADD COLUMN IF NOT EXISTS visualization_spec_version_id TEXT,
    ADD COLUMN IF NOT EXISTS renderer_build_id TEXT,
    ADD COLUMN IF NOT EXISTS runtime_build_id TEXT,
    ADD COLUMN IF NOT EXISTS theme_version TEXT,
    ADD COLUMN IF NOT EXISTS formatter_version TEXT,
    ADD COLUMN IF NOT EXISTS target_kind TEXT,
    ADD COLUMN IF NOT EXISTS datum_row_index INTEGER,
    ADD COLUMN IF NOT EXISTS datum_field TEXT,
    ADD COLUMN IF NOT EXISTS path_step_ordinal INTEGER,
    ADD COLUMN IF NOT EXISTS retry_key_hash TEXT,
    ADD COLUMN IF NOT EXISTS request_hash TEXT;

ALTER TABLE app.feedback_annotations
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_datum_mark,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_comment,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_result_identity,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_spec,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_render_identity,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_path_step;

ALTER TABLE app.query_results
    DROP CONSTRAINT IF EXISTS uq_query_results_feedback_identity,
    ADD CONSTRAINT uq_query_results_feedback_identity
    UNIQUE (id, org_id, project_id, content_hash);

ALTER TABLE app.renders
    DROP CONSTRAINT IF EXISTS uq_renders_feedback_identity,
    ADD CONSTRAINT uq_renders_feedback_identity
    UNIQUE (id, org_id, project_id, result_id, result_content_hash,
            visualization_spec_version_id, renderer_build_id, runtime_build_id,
            theme_version, formatter_version);

ALTER TABLE app.feedback_annotations
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_result_identity,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_spec,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_render_identity,
    DROP CONSTRAINT IF EXISTS fk_feedback_annotations_path_step,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_target_schema,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_result_hash,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_retry_hash,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_request_hash,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_render_pins_all_or_none,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_render_needs_pins,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_target,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_v1_required,
    DROP CONSTRAINT IF EXISTS ck_feedback_annotations_exact_render_pins,
    ADD CONSTRAINT ck_feedback_annotations_datum_mark CHECK (datum_mark IS NULL),
    ADD CONSTRAINT fk_feedback_annotations_result_identity
        FOREIGN KEY (result_id, org_id, project_id, result_content_hash)
        REFERENCES app.query_results (id, org_id, project_id, content_hash),
    ADD CONSTRAINT fk_feedback_annotations_spec
        FOREIGN KEY (visualization_spec_version_id, org_id, project_id)
        REFERENCES app.visualization_spec_versions (id, org_id, project_id),
    ADD CONSTRAINT fk_feedback_annotations_render_identity
        FOREIGN KEY (render_ref, org_id, project_id, result_id, result_content_hash,
                     visualization_spec_version_id, renderer_build_id, runtime_build_id,
                     theme_version, formatter_version)
        REFERENCES app.renders
            (id, org_id, project_id, result_id, result_content_hash,
             visualization_spec_version_id, renderer_build_id, runtime_build_id,
             theme_version, formatter_version),
    ADD CONSTRAINT fk_feedback_annotations_path_step
        FOREIGN KEY (ai_path_id, path_step_ordinal)
        REFERENCES app.ai_path_steps (path_id, ordinal),
    ADD CONSTRAINT ck_feedback_annotations_comment
        CHECK (comment IS NULL OR length(comment) <= CASE
            WHEN target_schema_version = 'exact-feedback.v1' THEN 2000 ELSE 4000 END),
    ADD CONSTRAINT ck_feedback_annotations_target_schema
        CHECK (target_schema_version IS NULL OR target_schema_version = 'exact-feedback.v1'),
    ADD CONSTRAINT ck_feedback_annotations_result_hash
        CHECK (result_content_hash IS NULL OR result_content_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_feedback_annotations_retry_hash
        CHECK (retry_key_hash IS NULL OR retry_key_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_feedback_annotations_request_hash
        CHECK (request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'),
    ADD CONSTRAINT ck_feedback_annotations_render_pins_all_or_none CHECK (
        (visualization_spec_version_id IS NULL AND renderer_build_id IS NULL
         AND runtime_build_id IS NULL AND theme_version IS NULL AND formatter_version IS NULL)
        OR
        (visualization_spec_version_id IS NOT NULL AND renderer_build_id IS NOT NULL
         AND runtime_build_id IS NOT NULL AND theme_version IS NOT NULL
         AND formatter_version IS NOT NULL)
    ),
    ADD CONSTRAINT ck_feedback_annotations_exact_render_pins CHECK (
        (visualization_spec_version_id IS NULL
         OR app.is_exact_pin(visualization_spec_version_id))
        AND (renderer_build_id IS NULL OR app.is_exact_pin(renderer_build_id))
        AND (runtime_build_id IS NULL OR app.is_exact_pin(runtime_build_id))
        AND (theme_version IS NULL OR app.is_exact_pin(theme_version))
        AND (formatter_version IS NULL OR app.is_exact_pin(formatter_version))
    ),
    ADD CONSTRAINT ck_feedback_annotations_render_needs_pins CHECK (
        target_schema_version IS NULL
        OR render_ref IS NULL
        OR visualization_spec_version_id IS NOT NULL
    ),
    ADD CONSTRAINT ck_feedback_annotations_target CHECK (
        target_schema_version IS NULL
        OR (
            target_kind = 'answer'
            AND datum_row_index IS NULL AND datum_field IS NULL AND path_step_ordinal IS NULL
        )
        OR (
            target_kind = 'datum'
            AND datum_row_index >= 0 AND datum_field IS NOT NULL
            AND length(btrim(datum_field)) BETWEEN 1 AND 200
            AND path_step_ordinal IS NULL
        )
        OR (
            target_kind = 'path_step'
            AND datum_row_index IS NULL AND datum_field IS NULL
            AND path_step_ordinal IS NOT NULL AND ai_path_id IS NOT NULL
        )
    ),
    ADD CONSTRAINT ck_feedback_annotations_v1_required CHECK (
        target_schema_version IS NULL
        OR (result_content_hash IS NOT NULL AND target_kind IS NOT NULL
            AND retry_key_hash IS NOT NULL AND request_hash IS NOT NULL
            AND interaction_ref IS NOT NULL)
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_feedback_annotations_retry_v1
    ON app.feedback_annotations
       (org_id, project_id, actor, observed_surface, retry_key_hash)
    WHERE target_schema_version = 'exact-feedback.v1';

CREATE OR REPLACE FUNCTION app.require_exact_feedback_v1()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.target_schema_version IS DISTINCT FROM 'exact-feedback.v1' THEN
        RAISE EXCEPTION 'new Analytics feedback requires exact-feedback.v1';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_feedback_annotations_require_v1
    ON app.feedback_annotations;
CREATE TRIGGER trg_feedback_annotations_require_v1
BEFORE INSERT ON app.feedback_annotations
FOR EACH ROW EXECUTE FUNCTION app.require_exact_feedback_v1();

COMMENT ON COLUMN app.feedback_annotations.target_schema_version IS
    'NULL only on immutable historical rows; every future insert is exact-feedback.v1.';
COMMENT ON COLUMN app.feedback_annotations.datum_field IS
    'Server-attested displayed Result field; for waterfall marks this is running_total_micros, not series.measureId.';

-- Frozen Share feedback remains in its anonymous append-only store.  Historical
-- Story 50.7 rows keep every new column NULL; only exact-feedback.v1 rows use the
-- closed target algebra and replay hashes below.
ALTER TABLE app.render_share_feedback
    ADD COLUMN IF NOT EXISTS target_schema_version TEXT,
    ADD COLUMN IF NOT EXISTS interaction_ref TEXT,
    ADD COLUMN IF NOT EXISTS result_content_hash TEXT,
    ADD COLUMN IF NOT EXISTS ai_path_id TEXT,
    ADD COLUMN IF NOT EXISTS target_kind TEXT,
    ADD COLUMN IF NOT EXISTS datum_row_index INTEGER,
    ADD COLUMN IF NOT EXISTS datum_field TEXT,
    ADD COLUMN IF NOT EXISTS path_step_ordinal INTEGER,
    ADD COLUMN IF NOT EXISTS retry_key_hash TEXT,
    ADD COLUMN IF NOT EXISTS request_hash TEXT;

ALTER TABLE app.render_share_feedback
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_target_schema,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_result_hash,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_retry_hash,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_request_hash,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_target,
    DROP CONSTRAINT IF EXISTS ck_render_share_feedback_exact_required,
    ADD CONSTRAINT ck_render_share_feedback_exact_target_schema CHECK (
        target_schema_version IS NULL OR target_schema_version = 'exact-feedback.v1'
    ),
    ADD CONSTRAINT ck_render_share_feedback_exact_result_hash CHECK (
        result_content_hash IS NULL OR result_content_hash ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT ck_render_share_feedback_exact_retry_hash CHECK (
        retry_key_hash IS NULL OR retry_key_hash ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT ck_render_share_feedback_exact_request_hash CHECK (
        request_hash IS NULL OR request_hash ~ '^[0-9a-f]{64}$'
    ),
    ADD CONSTRAINT ck_render_share_feedback_exact_target CHECK (
        target_schema_version IS NULL
        OR (target_kind = 'answer' AND datum_row_index IS NULL
            AND datum_field IS NULL AND path_step_ordinal IS NULL)
        OR (target_kind = 'datum' AND datum_row_index >= 0 AND datum_field IS NOT NULL
            AND length(btrim(datum_field)) BETWEEN 1 AND 200
            AND path_step_ordinal IS NULL)
        OR (target_kind = 'path_step' AND datum_row_index IS NULL
            AND datum_field IS NULL AND path_step_ordinal IS NOT NULL
            AND path_step_ordinal >= 0 AND ai_path_id IS NOT NULL)
    ),
    ADD CONSTRAINT ck_render_share_feedback_exact_required CHECK (
        target_schema_version IS NULL
        OR (interaction_ref IS NOT NULL AND result_content_hash IS NOT NULL
            AND target_kind IS NOT NULL AND retry_key_hash IS NOT NULL
            AND request_hash IS NOT NULL)
    );

CREATE UNIQUE INDEX IF NOT EXISTS uq_render_share_feedback_retry_v1
    ON app.render_share_feedback (share_id, retry_key_hash)
    WHERE target_schema_version = 'exact-feedback.v1';

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
DECLARE
    v_share_id TEXT;
    v_org_id TEXT;
    v_project_id TEXT;
    v_render_id TEXT;
    v_result_id TEXT;
    v_result_hash TEXT;
    v_spec_id TEXT;
    v_renderer TEXT;
    v_runtime TEXT;
    v_theme TEXT;
    v_formatter TEXT;
    v_manifest JSONB;
    v_schema JSONB;
    v_rows JSONB;
    v_result_manifest JSONB;
    v_spec JSONB;
    v_path JSONB;
    v_ai_path_id TEXT;
    v_allowed_fields TEXT[];
    v_existing_id TEXT;
    v_existing_hash TEXT;
BEGIN
    IF p_feedback_id !~ '^rsfb_[0-9A-HJKMNP-TV-Z]{26}$'
       OR p_interaction_ref IS NULL OR length(btrim(p_interaction_ref)) NOT BETWEEN 1 AND 200
       OR p_retry_key_hash !~ '^[0-9a-f]{64}$'
       OR p_request_hash !~ '^[0-9a-f]{64}$'
       OR p_polarity NOT IN ('positive', 'negative')
       OR (p_comment IS NOT NULL AND char_length(p_comment) > 2000) THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
    END IF;

    SELECT x.share_id, x.org_id, x.project_id, s.render_id,
           r.result_id, r.result_content_hash, r.visualization_spec_version_id,
           r.renderer_build_id, r.runtime_build_id, r.theme_version,
           r.formatter_version, r.evidence_manifest,
           fp.result_schema, fp.rows_chunk, fp.result_manifest,
           fp.spec_document, fp.ai_path_evidence
      INTO v_share_id, v_org_id, v_project_id, v_render_id,
           v_result_id, v_result_hash, v_spec_id, v_renderer, v_runtime,
           v_theme, v_formatter, v_manifest, v_schema, v_rows,
           v_result_manifest, v_spec, v_path
      FROM app.render_share_exchange_sessions x
      JOIN app.render_shares s
        ON s.id = x.share_id AND s.org_id = x.org_id AND s.project_id = x.project_id
      JOIN app.renders r
        ON r.id = s.render_id AND r.org_id = s.org_id AND r.project_id = s.project_id
      JOIN app.render_frozen_payloads fp
        ON fp.render_id = r.id AND fp.org_id = r.org_id AND fp.project_id = r.project_id
     WHERE x.session_hash = p_session_hash
       AND x.expires_at > NOW() AND s.state = 'active' AND s.expires_at > NOW()
     FOR UPDATE OF x, s;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = 'P0002';
    END IF;

    SELECT COALESCE(array_agg(DISTINCT field_name), ARRAY[]::TEXT[])
      INTO v_allowed_fields
      FROM (
        SELECT sf->>'name' AS field_name
          FROM jsonb_array_elements(COALESCE(v_schema->'fields', '[]'::jsonb)) sf
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
                          FROM jsonb_each(COALESCE(v_spec->'bindings', '{}'::jsonb)) b,
                               LATERAL jsonb_array_elements(
                                   CASE jsonb_typeof(b.value)
                                     WHEN 'array' THEN b.value
                                     ELSE jsonb_build_array(b.value)
                                   END
                               ) bound_value
                        UNION
                        SELECT datum_value #>> '{}'
                          FROM jsonb_array_elements(
                              COALESCE(v_spec->'evidence'->'datum_fields', '[]'::jsonb)
                          ) datum_value
                      ) bound
                      LEFT JOIN LATERAL jsonb_array_elements(
                          COALESCE(v_result_manifest->'provenance'->'values', '[]'::jsonb)
                      ) pv ON pv->>'member_id' = bound.member_id
                  ) mapped
                 WHERE mapped.source_id IN (sf->>'id', sf->>'name')
           )
      ) safe_fields;

    IF v_path->>'lifecycle' = 'finalized'
       AND v_path->>'state' IN ('completed', 'failed') THEN
        v_ai_path_id := v_path->>'path_id';
    END IF;

    IF p_target_kind = 'answer' THEN
        IF p_datum_row_index IS NOT NULL OR p_datum_field IS NOT NULL
           OR p_path_step_ordinal IS NOT NULL THEN
            RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
        END IF;
    ELSIF p_target_kind = 'datum' THEN
        IF p_datum_row_index IS NULL OR p_datum_row_index < 0
           OR p_datum_row_index >= jsonb_array_length(COALESCE(v_rows, '[]'::jsonb))
           OR NOT (p_datum_field = ANY(v_allowed_fields))
           OR NOT ((COALESCE(v_rows, '[]'::jsonb)->p_datum_row_index) ? p_datum_field)
           OR p_path_step_ordinal IS NOT NULL THEN
            RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
        END IF;
    ELSIF p_target_kind = 'path_step' THEN
        IF v_path->>'lifecycle' <> 'finalized'
           OR v_path->>'state' NOT IN ('completed', 'failed')
           OR p_datum_row_index IS NOT NULL OR p_datum_field IS NOT NULL
           OR p_path_step_ordinal IS NULL
           OR NOT EXISTS (
                SELECT 1 FROM jsonb_array_elements(COALESCE(v_path->'steps', '[]'::jsonb)) step
                 WHERE (step->>'ordinal')::integer = p_path_step_ordinal
           ) THEN
            RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
        END IF;
    ELSE
        RAISE EXCEPTION 'share_unavailable' USING ERRCODE = '22023';
    END IF;

    PERFORM pg_advisory_xact_lock(hashtextextended(v_share_id || ':' || p_retry_key_hash, 0));
    SELECT id, request_hash INTO v_existing_id, v_existing_hash
      FROM app.render_share_feedback
     WHERE share_id = v_share_id AND retry_key_hash = p_retry_key_hash
       AND target_schema_version = 'exact-feedback.v1';
    IF FOUND THEN
        IF v_existing_hash <> p_request_hash THEN
            RETURN QUERY SELECT 'conflict'::TEXT, v_existing_id;
            RETURN;
        END IF;
        RETURN QUERY SELECT 'replayed'::TEXT, v_existing_id;
        RETURN;
    END IF;

    INSERT INTO app.render_share_feedback
        (id, share_id, org_id, project_id, render_id, result_id,
         visualization_spec_version_id, renderer_build, runtime_build,
         theme_version, formatter_version, responsive_profile,
         evidence_manifest_hash, polarity, comment, target_schema_version,
         interaction_ref, result_content_hash, ai_path_id, target_kind,
         datum_row_index, datum_field, path_step_ordinal, retry_key_hash, request_hash)
    VALUES
        (p_feedback_id, v_share_id, v_org_id, v_project_id, v_render_id, v_result_id,
         v_spec_id, v_renderer, v_runtime, v_theme, v_formatter, 'share',
         encode(sha256(convert_to(COALESCE(v_manifest, '{}'::jsonb)::text, 'UTF8')), 'hex'),
         CASE p_polarity WHEN 'positive' THEN 'helpful' ELSE 'not_helpful' END,
         p_comment, 'exact-feedback.v1', p_interaction_ref, v_result_hash,
         v_ai_path_id, p_target_kind, p_datum_row_index, p_datum_field,
         p_path_step_ordinal, p_retry_key_hash, p_request_hash);
    RETURN QUERY SELECT 'recorded'::TEXT, p_feedback_id;
END;
$$;

DO $$
DECLARE
    v_owner TEXT;
BEGIN
    SELECT r.rolname INTO v_owner
      FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
      JOIN pg_roles r ON r.oid = p.proowner
     WHERE n.nspname = 'app' AND p.proname = 'record_render_share_feedback_v1'
       AND pg_get_function_identity_arguments(p.oid) =
           'p_session_hash text, p_feedback_id text, p_interaction_ref text, '
           'p_target_kind text, p_datum_row_index integer, p_datum_field text, '
           'p_path_step_ordinal integer, p_polarity text, p_comment text, '
           'p_retry_key_hash text, p_request_hash text';
    IF v_owner IS NULL OR v_owner IN ('connector', 'toorow_share_reader') THEN
        RAISE EXCEPTION 'record_render_share_feedback_v1 requires a controlled definer owner';
    END IF;
END;
$$;

REVOKE INSERT ON app.render_share_feedback FROM toorow_share_reader;
REVOKE ALL ON FUNCTION app.record_render_share_feedback_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION app.record_render_share_feedback_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) TO toorow_share_reader;

COMMENT ON FUNCTION app.record_render_share_feedback_v1(
    TEXT, TEXT, TEXT, TEXT, INTEGER, TEXT, INTEGER, TEXT, TEXT, TEXT, TEXT
) IS 'Story 65.5 frozen Share writer; owner is the controlled migration role, never runtime/share.';

COMMIT;
