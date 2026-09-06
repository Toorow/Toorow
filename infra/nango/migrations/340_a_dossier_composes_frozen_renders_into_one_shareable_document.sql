-- 340 -- a Dossier composes frozen Renders into ONE shareable document.
--
-- Story 73-1 (epic-73-dossier-partageable.md), the object half of the amendment
-- << the shareable analysis dossier, and its PDF >> ratified 2026-09-01 in
-- docs/product-architecture/visualization-and-rendering.md: the deliverable of
-- an analysis is a dossier -- several sections, several figures, a narrative --
-- and the current chain stops one level below (a Share grants exactly ONE
-- Render, nothing composes several into a document).
--
-- WHAT A VERSION PINS, AND WHY THERE IS NO FOREIGN KEY TO app.renders. A
-- version's `blocks` is an ordered JSONB array of
--   {"kind": "render", "render_id": "rnd_..."} and
--   {"kind": "narrative", "text": "..."}
-- A jsonb element cannot carry a foreign key, so the render references are
-- validated at the door (core/dossiers.py) against app.renders of the SAME org
-- and project -- the same posture analysis_report_versions takes for
-- `presentation_version_id` (154's header says why). A Render is already
-- immutable and append-only, so a validated pin cannot rot: the row it names
-- can only still exist, unchanged.
--
-- IMMUTABLE PER VERSION, like everything else in this chain: versions reuse
-- migration 151's app.reject_analytical_evidence_mutation() with the
-- app.rgpd_erasure escape hatch -- the same trigger shape as
-- analysis_report_versions, so both raise the same SQLSTATE and sentence.
-- The head advances current_version_id and archived_at; nothing else moves.

CREATE TABLE IF NOT EXISTS app.analysis_dossiers (
    id                  TEXT PRIMARY KEY,
    org_id              TEXT NOT NULL,
    project_id          TEXT NOT NULL,
    label               TEXT NOT NULL,
    description         TEXT,
    current_version_id  TEXT,
    -- Archive, never delete: a Share of tomorrow (73-2) will point at versions.
    archived_at         TIMESTAMPTZ,
    created_by          TEXT NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_dossiers_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT fk_analysis_dossiers_project
        FOREIGN KEY (org_id, project_id) REFERENCES app.projects (org_id, id),
    CONSTRAINT ck_analysis_dossiers_label_bounded
        CHECK (char_length(label) BETWEEN 1 AND 200)
);

CREATE INDEX IF NOT EXISTS idx_analysis_dossiers_project
    ON app.analysis_dossiers (project_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_analysis_dossiers_live
    ON app.analysis_dossiers (project_id, updated_at DESC)
    WHERE archived_at IS NULL;

CREATE TABLE IF NOT EXISTS app.analysis_dossier_versions (
    id                      TEXT PRIMARY KEY,
    dossier_id              TEXT NOT NULL,
    org_id                  TEXT NOT NULL,
    project_id              TEXT NOT NULL,
    version_number          INTEGER NOT NULL,
    label                   TEXT NOT NULL,
    description             TEXT,
    blocks                  JSONB NOT NULL,
    content_hash            TEXT NOT NULL,
    predecessor_version_id  TEXT,
    created_by              TEXT NOT NULL,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_analysis_dossier_versions_scope UNIQUE (id, org_id, project_id),
    CONSTRAINT uq_analysis_dossier_version_number UNIQUE (dossier_id, version_number),
    CONSTRAINT fk_analysis_dossier_versions_head
        FOREIGN KEY (dossier_id, org_id, project_id)
        REFERENCES app.analysis_dossiers (id, org_id, project_id),
    CONSTRAINT fk_analysis_dossier_versions_predecessor
        FOREIGN KEY (predecessor_version_id, org_id, project_id)
        REFERENCES app.analysis_dossier_versions (id, org_id, project_id),
    CONSTRAINT ck_analysis_dossier_versions_number_positive CHECK (version_number >= 1),
    CONSTRAINT ck_analysis_dossier_versions_hash CHECK (content_hash ~ '^[0-9a-f]{64}$'),
    CONSTRAINT ck_analysis_dossier_versions_blocks_are_a_sequence
        CHECK (jsonb_typeof(blocks) = 'array' AND jsonb_array_length(blocks) >= 1),
    CONSTRAINT ck_analysis_dossier_versions_lineage CHECK (
        (version_number = 1 AND predecessor_version_id IS NULL)
        OR (version_number > 1 AND predecessor_version_id IS NOT NULL)
    ),
    CONSTRAINT ck_analysis_dossier_versions_label_bounded
        CHECK (char_length(label) BETWEEN 1 AND 200)
);

CREATE INDEX IF NOT EXISTS idx_analysis_dossier_versions_head
    ON app.analysis_dossier_versions (dossier_id, version_number DESC);

ALTER TABLE app.analysis_dossiers
    DROP CONSTRAINT IF EXISTS fk_analysis_dossiers_current_version;
ALTER TABLE app.analysis_dossiers
    ADD CONSTRAINT fk_analysis_dossiers_current_version
        FOREIGN KEY (current_version_id, org_id, project_id)
        REFERENCES app.analysis_dossier_versions (id, org_id, project_id);

DROP TRIGGER IF EXISTS trg_analysis_dossier_versions_immutable
    ON app.analysis_dossier_versions;
CREATE TRIGGER trg_analysis_dossier_versions_immutable
BEFORE UPDATE OR DELETE ON app.analysis_dossier_versions
FOR EACH ROW
WHEN (current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on')
EXECUTE FUNCTION app.reject_analytical_evidence_mutation();
