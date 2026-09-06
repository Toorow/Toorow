-- 341 -- a Share can open a Dossier version: one grant, one sequence of Renders.
--
-- Story 73-2 (epic-73-dossier-partageable.md). The amendment of 2026-09-01 says
-- the Share mechanics EXTEND to a Dossier -- revocable grant, audit evidence,
-- the same runtime -- rather than a second sharing system growing beside the
-- first. So the grant row stays `app.render_shares`: the bearer, the
-- second-holder confirmation (323), the exchange sessions, the access events
-- and the revocation machinery all apply unchanged. What changes is the TARGET:
-- exactly one of `render_id` and `dossier_version_id`, said by a CHECK, never
-- by a comment.
--
-- The target pins a VERSION of the Dossier, never the head: a share whose
-- content could change after the second holder confirmed it would not be the
-- thing they confirmed. Every Render of the pinned version is frozen at share
-- time by the same `freeze_render_payload` path a single-Render share uses.

ALTER TABLE app.render_shares
    ALTER COLUMN render_id DROP NOT NULL;

ALTER TABLE app.render_shares
    ADD COLUMN IF NOT EXISTS dossier_version_id TEXT;

ALTER TABLE app.render_shares
    DROP CONSTRAINT IF EXISTS fk_render_shares_dossier_version;
ALTER TABLE app.render_shares
    ADD CONSTRAINT fk_render_shares_dossier_version
        FOREIGN KEY (dossier_version_id, org_id, project_id)
        REFERENCES app.analysis_dossier_versions (id, org_id, project_id);

ALTER TABLE app.render_shares
    DROP CONSTRAINT IF EXISTS ck_render_shares_one_target;
ALTER TABLE app.render_shares
    ADD CONSTRAINT ck_render_shares_one_target CHECK (
        (render_id IS NOT NULL)::int + (dossier_version_id IS NOT NULL)::int = 1
    );

CREATE INDEX IF NOT EXISTS idx_render_shares_dossier_version
    ON app.render_shares (dossier_version_id)
    WHERE dossier_version_id IS NOT NULL;
