-- Story 65.4: a Shared Render carries the bounded observed AI Path that was
-- authorized when its frozen payload was created.  Historical payloads predate
-- this column and remain readable as `evidence_not_frozen`; new payloads may not
-- silently create that legacy state.

BEGIN;

ALTER TABLE app.render_frozen_payloads
    ADD COLUMN IF NOT EXISTS ai_path_evidence JSONB;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
          FROM pg_constraint
         WHERE conrelid = 'app.render_frozen_payloads'::regclass
           AND conname = 'ck_render_frozen_payloads_ai_path_evidence'
    ) THEN
        ALTER TABLE app.render_frozen_payloads
            ADD CONSTRAINT ck_render_frozen_payloads_ai_path_evidence
            CHECK (
                ai_path_evidence IS NOT NULL
                AND jsonb_typeof(ai_path_evidence) = 'object'
            ) NOT VALID;
    END IF;
END
$$;

COMMENT ON COLUMN app.render_frozen_payloads.ai_path_evidence IS
    'Story 65.4: server-authored observed-ai-path.v1 frozen beside RenderInput. '
    'NULL identifies a payload created before migration 248; the NOT VALID CHECK '
    'keeps those rows readable while refusing NULL or non-object values on every '
    'new insert. The public share reader receives no additional table privilege.';

COMMIT;
