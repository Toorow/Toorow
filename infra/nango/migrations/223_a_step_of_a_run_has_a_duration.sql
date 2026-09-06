-- 223_a_step_of_a_run_has_a_duration.sql
--
-- THE ACTIVITY TRACK SHOWS THE TIME EACH STEP TOOK, SO A STEP NEEDS TWO ENDS.
-- Story 58.10, epic 58: `Collect 2 min` -> `Map 2 s` -> `Check 1 s` ->
-- `Publish 1 min`.
--
-- MEASURED 2026-08-07, and it says the substrate is absent rather than partial:
--
--   * `app.datastream_executions.step` (migration 218) is CHECK-constrained to
--     the ratified four -- `('Collect','Map','Check','Publish')` -- and is
--     exactly ONE value: where the run is NOW. Migration 218 chose that on
--     purpose and wrote why in its own header: "Progress is a CURRENT STATE, not
--     a history". Four durations are a history, which is the case 218 excluded.
--   * `app.datastream_execution_phase_evidence` (138) DOES carry
--     `interval_start`/`interval_end`, but on the SEVEN-phase vocabulary, and
--     they are written 0 times out of 202 rows. The only production writer is
--     `datastream_activation.py:777`, always `phase="publication"`.
--
-- WHY THE SEVEN PHASES ARE NOT REUSED, AND IT IS NOT A PREFERENCE.
-- `docs/product-architecture/datastream-workbench-and-wizard.md:1579-1583` is
-- ratified and explicit: "The four steps `Collect`/`Map`/`Check`/`Publish` and
-- the seven phases of `app.datastream_execution_phase_evidence` are two
-- vocabularies, and they stay two." Deriving a step span by folding phases onto
-- steps at read time would fuse them in the read model, which is the same fusion
-- wearing a different coat. So the four steps get their own spans, in the
-- shape their sibling already proved.
--
-- WHY A TABLE AND NOT EIGHT COLUMNS. Eight nullable timestamps on
-- `app.datastream_executions` cannot be constrained pairwise without eight more
-- CHECKs, cannot be indexed for "which step is slow across the fleet", and grow
-- by two every time the vocabulary does. One row per (run, step) is the shape
-- `phase_evidence` already uses for the same question at a different grain.
--
-- WHY IT IS NOT APPEND-ONLY, SAID OUT LOUD. A step is written twice: once when
-- the run enters it (`started_at`, `ended_at` NULL) and once when it leaves
-- (`ended_at` set). That is an UPSERT, so this table carries NO append-only
-- trigger and therefore needs NO organization-erasure hatch -- the rule that
-- every append-only table must carry the escape in its trigger (migration 200)
-- does not reach a mutable one. Erasure works through `project_id`, whose
-- composite foreign keys below root every row in a Project.
--
-- `ended_at >= started_at`, NOT `>`. `phase_evidence` (`138:75-78`) uses a
-- strict `<` and it is wrong for this grain: `Check` is measured in
-- milliseconds and a step that opens and closes inside one clock tick is a real
-- step, not a corrupt row. Refusing it would make the fast path the invalid one.

CREATE TABLE IF NOT EXISTS app.datastream_execution_step_evidence (
    id TEXT PRIMARY KEY CHECK (id ~ '^dsse_[0-9A-HJKMNP-TV-Z]{26}$'),
    org_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    datastream_id TEXT NOT NULL,
    execution_id TEXT NOT NULL,
    -- The product's own vocabulary, mirrored from migration 218's CHECK on
    -- `app.datastream_executions.step`. Two places, one list: a step this table
    -- accepts and that column refuses would be a run stuck between them.
    step TEXT NOT NULL CHECK (step IN ('Collect', 'Map', 'Check', 'Publish')),
    started_at TIMESTAMPTZ NOT NULL,
    -- NULL means the step is STILL RUNNING. It never means "took no time":
    -- a reader that cannot tell those apart prints `0 s` under a step that is
    -- still working, which is the fabricated-zero this epic exists against.
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (execution_id, step),
    CHECK (ended_at IS NULL OR ended_at >= started_at),
    FOREIGN KEY (execution_id, datastream_id, project_id)
        REFERENCES app.datastream_executions(id, datastream_id, project_id) ON DELETE RESTRICT,
    FOREIGN KEY (datastream_id, project_id)
        REFERENCES app.datastreams(id, project_id) ON DELETE RESTRICT
);

-- The track's own read: the four steps of ONE run, already in order.
CREATE INDEX IF NOT EXISTS idx_datastream_step_evidence_track
    ON app.datastream_execution_step_evidence(project_id, datastream_id, execution_id, started_at);

COMMENT ON TABLE app.datastream_execution_step_evidence IS
'Story 58.10: one row per (run, ratified step) carrying the two ends of its span. Distinct from datastream_execution_phase_evidence, whose seven phases are a separate vocabulary that stays separate (datastream-workbench-and-wizard.md:1579-1583).';

COMMENT ON COLUMN app.datastream_execution_step_evidence.ended_at IS
'NULL means the step is still running -- never that it took no time.';
