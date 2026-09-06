-- A Semantic View could be created once and never revised.
--
-- `app.reject_semantic_version_mutation()` guards immutability for BOTH version
-- tables. It reads `NEW.concept_id` unconditionally, and a view version has no
-- such column: it carries `view_id`. So the moment a second version had to
-- supersede the first -- adding a dimension, correcting a definition -- the
-- update raised `UndefinedColumn: record "new" has no field "concept_id"`, which
-- the API answered as a flat 503. Creating the FIRST version worked, because
-- nothing had to be superseded; every edit after it died.
--
-- Measured 2026-08-12 in production: adding eleven analysis axes to a published
-- View answered `publishable: true` at prepare and 503 at confirm.
--
-- The guard now reads the owning object through `to_jsonb`, so it does not have
-- to know which column each table calls its owner -- which is exactly what made
-- it wrong. Nothing about the immutability itself changes: the same four things
-- stay frozen on a published version.

CREATE OR REPLACE FUNCTION app.reject_semantic_version_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    owner_new text;
    owner_old text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF OLD.status IN ('published', 'superseded', 'archived') THEN
            RAISE EXCEPTION 'published semantic versions are immutable'
                USING ERRCODE = '23000';
        END IF;
        RETURN OLD;
    END IF;
    IF OLD.status = 'published' AND NEW.status NOT IN ('superseded', 'archived') THEN
        RAISE EXCEPTION 'published semantic versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    IF OLD.status IN ('superseded', 'archived') THEN
        RAISE EXCEPTION 'published semantic versions are immutable'
            USING ERRCODE = '23000';
    END IF;

    -- The object this version belongs to, whatever its table calls it.
    owner_new := COALESCE(to_jsonb(NEW) ->> 'concept_id', to_jsonb(NEW) ->> 'view_id');
    owner_old := COALESCE(to_jsonb(OLD) ->> 'concept_id', to_jsonb(OLD) ->> 'view_id');

    IF OLD.status = 'published' AND (
           NEW.id <> OLD.id
        OR owner_new IS DISTINCT FROM owner_old
        OR NEW.content_hash <> OLD.content_hash
        OR NEW.version_number <> OLD.version_number
    ) THEN
        RAISE EXCEPTION 'published semantic versions are immutable'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$function$;
