-- 261 -- A measure declares which Datastream holds its total, and what each
-- breakdown of it sums to.
--
-- WHY THIS IS NOT DERIVABLE, and therefore why it is stored at all.
--
-- Which Datastreams CARRY a concept is a fact of the published mappings: a
-- Datastream carries `views` when its active mapping version binds a field to the
-- canonical target `views`. That is read, never written -- `metric_grain.carriers_of`
-- is its one reader and no table here holds a copy of it.
--
-- Two facts are not in any mapping and belong to the client:
--
--   1. WHICH carrier is authoritative for the TOTAL. Measured on
--      proj_01KZGCRSV2XACWRP3RSVNWWGBK on 2026-08-14: eight of the ten Datastreams
--      with an active mapping carry `views`, and the Semantic View pinned exactly
--      one of them. That pin was a decision -- nothing recorded that one had been
--      made, so "views by country" was refused as a cross-source question while the
--      country Datastream carried the measure and the dimension by itself.
--
--   2. WHAT a breakdown sums to against that total. A channel total is not the sum
--      of a per-video breakdown: a share of subscriber changes happens away from a
--      watch page. That sentence lives in a connector manifest as prose today; here
--      it becomes carried by the metric, so a reader of a figure gets it without
--      having read the manifest.
--
-- `unknown` is deliberately NOT a value: it is the ABSENCE of a breakdown row. A
-- gap nobody has explained must read as unexplained, and a third enum value would
-- let "we never looked" be written down as if it were an answer.
--
-- Contract: docs/product-architecture/analyze-and-test.md, "Amendment, chantier B".
-- Audit: app.metric_semantics_audit (migration 049) -- the same registry as the
-- cross-source reconciliation rules, because this is the same question one scope
-- down: 27.1 reconciles two SOURCES, this reconciles two GRAINS of one source.

BEGIN;

-- ---------------------------------------------------------------------------
-- 1. The declaration: one per (Project, concept).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.metric_grain_declarations (
    id                  TEXT        PRIMARY KEY,        -- prefixed ULID: 'mgd_'
    project_id          TEXT        NOT NULL
                        REFERENCES app.projects(id) ON DELETE CASCADE,
    concept_id          TEXT        NOT NULL
                        REFERENCES app.semantic_concepts(id) ON DELETE CASCADE,
    -- The carrier that answers for the total. A composite FK, not a comment: a
    -- declaration may not name a Datastream of another Project.
    total_datastream_id TEXT        NOT NULL,
    -- Free note from whoever declared it. Never a substitute for a breakdown reason.
    note                TEXT,
    created_by          TEXT        NOT NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_metric_grain_declarations UNIQUE (project_id, concept_id),
    -- Carried so a breakdown row cannot belong to a declaration of another Project.
    CONSTRAINT uq_metric_grain_declarations_id_project UNIQUE (id, project_id),
    CONSTRAINT fk_metric_grain_declaration_total_scope
        FOREIGN KEY (total_datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS ix_metric_grain_declarations_project
    ON app.metric_grain_declarations (project_id);

DROP TRIGGER IF EXISTS trg_metric_grain_declarations_updated_at
    ON app.metric_grain_declarations;
CREATE TRIGGER trg_metric_grain_declarations_updated_at
    BEFORE UPDATE ON app.metric_grain_declarations
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 2. What each breakdown sums to. One row per (declaration, breakdown carrier).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.metric_grain_breakdowns (
    id              TEXT        PRIMARY KEY,            -- prefixed ULID: 'mgb_'
    declaration_id  TEXT        NOT NULL,
    datastream_id   TEXT        NOT NULL,
    project_id      TEXT        NOT NULL,
    sums_to         TEXT        NOT NULL
                    CHECK (sums_to IN ('equals', 'partial_by_design')),
    -- What the breakdown does NOT contain, in the client's words. It is what makes
    -- `partial_by_design` an explanation instead of a label, so it is required.
    reason          TEXT,
    -- The gap a declared `equals` may still show before it counts as unexplained
    -- (rounding, late-arriving rows). NULL means zero tolerance.
    tolerance_ratio NUMERIC(6, 5) CHECK (tolerance_ratio IS NULL
                                         OR (tolerance_ratio >= 0 AND tolerance_ratio <= 1)),
    created_by      TEXT        NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_metric_grain_breakdowns UNIQUE (declaration_id, datastream_id),
    CONSTRAINT fk_metric_grain_breakdown_declaration
        FOREIGN KEY (declaration_id, project_id)
        REFERENCES app.metric_grain_declarations (id, project_id) ON DELETE CASCADE,
    CONSTRAINT fk_metric_grain_breakdown_scope
        FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT,
    -- A reason of twenty characters is the same floor story 53.7 puts on a Test
    -- gate override: a blank explanation is indistinguishable from no explanation.
    CONSTRAINT ck_metric_grain_breakdown_reason CHECK (
        sums_to <> 'partial_by_design'
        OR (reason IS NOT NULL AND length(btrim(reason)) >= 20)
    )
);

CREATE INDEX IF NOT EXISTS ix_metric_grain_breakdowns_declaration
    ON app.metric_grain_breakdowns (declaration_id);

DROP TRIGGER IF EXISTS trg_metric_grain_breakdowns_updated_at
    ON app.metric_grain_breakdowns;
CREATE TRIGGER trg_metric_grain_breakdowns_updated_at
    BEFORE UPDATE ON app.metric_grain_breakdowns
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

-- ---------------------------------------------------------------------------
-- 3. Grants -- the application role, like every other governance table.
-- ---------------------------------------------------------------------------

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        GRANT SELECT, INSERT, UPDATE, DELETE
            ON app.metric_grain_declarations, app.metric_grain_breakdowns TO connector;
    END IF;
END
$$;

COMMIT;
