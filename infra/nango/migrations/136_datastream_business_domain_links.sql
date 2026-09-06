-- Story 47.4: Datastreams are canonical Business Domain link targets.

ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS mdm_business_links_target_type_check;
ALTER TABLE app.mdm_business_links
    DROP CONSTRAINT IF EXISTS ck_mdm_business_links_target_type;
ALTER TABLE app.mdm_business_links
    ADD CONSTRAINT ck_mdm_business_links_target_type
    CHECK (target_type IN (
        'topic', 'procedure', 'target_field', 'schema_doc', 'report_view', 'datastream'
    ));

CREATE OR REPLACE FUNCTION app.validate_business_link_scope()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM app.projects p
        WHERE p.id = NEW.project_id AND p.org_id = NEW.org_id AND p.status = 'active'
    ) THEN
        RAISE EXCEPTION 'Business link project is not active in the organization'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.taxonomy_type = 'business_domain' AND NOT EXISTS (
        SELECT 1 FROM app.mdm_business_domains d
        WHERE d.id = NEW.taxonomy_id AND d.org_id = NEW.org_id AND d.status = 'active'
    ) THEN
        RAISE EXCEPTION 'Business domain link source is not active in the organization'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.taxonomy_type = 'business_classification' AND NOT EXISTS (
        SELECT 1 FROM app.mdm_business_classifications c
        WHERE c.id = NEW.taxonomy_id AND c.org_id = NEW.org_id AND c.status = 'active'
    ) THEN
        RAISE EXCEPTION 'Business classification link source is not active in the organization'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.target_type = 'datastream' AND NOT EXISTS (
        SELECT 1 FROM app.datastreams d
        WHERE d.id = NEW.target_id
          AND d.project_id = NEW.project_id
          AND d.org_id = NEW.org_id
    ) THEN
        RAISE EXCEPTION 'Datastream link target is outside the organization or Project'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END;
$$;
