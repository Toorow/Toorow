-- infra/nango/migrations/310_a_ventilated_share_names_the_volume_that_split_it.sql
--
-- Story 70.4 -- the prorata ventilation engine of Analytics Alignment gets the
-- row-by-row store its weights need to stay REVERSIBLE.
--
-- WHAT A WEIGHT IS, AND WHY IT IS STORED WHEN A DERIVABLE VALUE IS NOT. The
-- repository's rule is that a value which can be derived is not stored. A
-- ventilation weight looks derivable -- it is `volume / total` -- and it is not,
-- for the reason the FX provenance is not: the VOLUME IT WAS TAKEN OVER MOVES.
-- A day's clicks are re-fetched, corrected, superseded by a later sync. The
-- weight is therefore not a cache of a computation, it is the RECORD OF HOW A
-- PUBLISHED FIGURE WAS SPLIT, and without it a number a client was already shown
-- can no longer be re-derived once its basis has changed. That is the same
-- reason migration 309 pins `common_key_version_id` on a decision instead of
-- pointing at the key.
--
-- WHAT THIS TABLE IS NOT. It is not the ventilated figure. It carries no metric
-- and no amount: `volume_value`, `volume_total` and `weight` are the split, and
-- the observed figure they split is NEVER copied here -- `l'observe d'origine
-- n'est jamais ecrase` is the story's own sentence, and a second copy of it in
-- this table would be a second place for it to disagree with itself. The
-- ventilated amount is `observed x weight`, computed at read by
-- `core/analytics_ventilation.py`, exactly as `plan_actual_alignment` keeps
-- `spend x split_weight` an exact Decimal rather than a stored column.
--
-- The table is absent from `server/core/mirror_sync.py`'s explicit table list, so
-- NO dbt model can read it.
--
-- THE INVARIANT THIS TABLE CANNOT HOLD, STATED SO NOBODY LOOKS FOR IT HERE.
-- `SUM(weight) = 1` per `(alignment_key, activity_date)` is a constraint over a
-- SET of rows, which a CHECK cannot express and which a trigger could only
-- express by re-reading the group on every INSERT of a batch that is written row
-- by row -- refusing the first row of every legitimate run. So the column CHECKs
-- below hold what a single row can prove (a share is in [0, 1], a volume is not
-- negative, a share never exceeds its total), and the SUM is asserted in Python:
-- `assert_weights_sum_to_one` before the first INSERT of a run, and
-- `assert_stored_weights_conserve` over the rows read BACK, which is the check
-- that can see a run written half.
--
-- APPEND-ONLY? NO, and therefore no `app.rgpd_erasure` hatch of the kind
-- 099_rgpd_erasure_trigger_guards.sql adds. This table installs no immutability
-- trigger and no append-only journal. What makes the FIRST run of a
-- `(key, day, entity)` stand is the UNIQUE index below plus the writer's
-- ON CONFLICT DO NOTHING -- the shape migration 245 chose for
-- `app.plan_unmatched_spend_decisions` and migration 309 reused for the alignment
-- decisions -- so a second run over a re-fetched volume never rewrites a split a
-- client was already shown. The `connector` role holds no UPDATE on it either
-- (see the REVOKE below), so there is no privilege through which one could.
--
-- ERASURE. Every foreign key below is ON DELETE CASCADE, so it is POSTGRES that
-- erases these rows, from the `app.organizations` / `app.projects` statement the
-- purge already emits. It is NOT `core.org_purge` that reaches them: its
-- `_FK_GRAPH_SQL` walks `confdeltype IN ('a','r')` -- NO ACTION and RESTRICT
-- only -- so a CASCADE-only table is invisible to `plan_purge` and named in zero
-- of its statements, BY DESIGN. The same sentence is true of
-- `app.analytics_alignment_decisions`, whose own header (migration 309) states
-- the mechanism the other way round; this is the forward correction, in the words
-- migration 235 fixed.
--
-- Schema-Change-Checklist: additive and idempotent. It creates ONE table and
-- edits nothing. No migration below this number is touched. This is migration 310.
-- ============================================================================

BEGIN;

CREATE TABLE IF NOT EXISTS app.analytics_alignment_weights (
    id                    TEXT        PRIMARY KEY,   -- prefixed ULID: 'aaw_<ULID>'
    -- org_id is carried, not derived: migration 273's ratchet arms every
    -- org-scoped table from this column, and a table that omits it leaves the
    -- sweep in silence. The policy below is the 32-table form, word for word.
    org_id                TEXT        NOT NULL
                          REFERENCES app.organizations(id) ON DELETE CASCADE,
    project_id            TEXT        NOT NULL
                          REFERENCES app.projects(id) ON DELETE CASCADE,
    -- The ORDERED pair, as the cascade ran it: the left Datastream is the one
    -- whose rows are being aligned, and whose coarse key is being split.
    left_datastream_id    TEXT        NOT NULL
                          REFERENCES app.datastreams(id) ON DELETE CASCADE,
    right_datastream_id   TEXT        NOT NULL
                          REFERENCES app.datastreams(id) ON DELETE CASCADE,
    -- The EXACT common key version the pair was crossed on, for the reason
    -- migration 309 states: a key whose components changed is a different
    -- identity, and a split taken under the old one is not a split about the new.
    common_key_version_id TEXT        NOT NULL
                          REFERENCES app.mdm_common_key_versions(id) ON DELETE CASCADE,
    -- The COARSE key that was split, and the day it was split on. A DATE and
    -- never a timestamp: this product reports on a date grain everywhere, and an
    -- hour here would make one day's weights sum to twenty-four.
    alignment_key         TEXT        NOT NULL
                          CHECK (btrim(alignment_key) <> '' AND length(alignment_key) <= 400),
    activity_date         DATE        NOT NULL,
    -- The media entity that received this share.
    right_row_key         TEXT        NOT NULL
                          CHECK (btrim(right_row_key) <> '' AND length(right_row_key) <= 400),
    -- THE DECLARED VOLUME, BY NAME AND BY VERSION. Not decoration and not a log
    -- line: a ventilated figure whose basis is not written beside it cannot be
    -- re-derived by the person defending it, and `aucun volume par defaut
    -- implicite` is the story's refusal. Both are NOT NULL and both are refused
    -- empty, so a row can never say "split proportionally to something".
    volume_name           TEXT        NOT NULL
                          CHECK (btrim(volume_name) <> '' AND length(volume_name) <= 200),
    volume_version        TEXT        NOT NULL
                          CHECK (btrim(volume_version) <> '' AND length(volume_version) <= 200),
    -- This entity's own volume and the total it was taken over. Both travel, so a
    -- reader who has them can RECOMPUTE the weight; a reader who has only the
    -- weight can only believe it.
    volume_value          NUMERIC     NOT NULL CHECK (volume_value >= 0),
    volume_total          NUMERIC     NOT NULL CHECK (volume_total > 0),
    -- NUMERIC(19, 18): eighteen decimal places, which is the scale
    -- `analytics_ventilation.WEIGHT_SCALE` quantizes to. A column with a
    -- narrower scale would round a weight on its way in and the assertion would
    -- then be true in Python and false in the table.
    weight                NUMERIC(19, 18) NOT NULL CHECK (weight >= 0 AND weight <= 1),
    -- Which row absorbs the sub-quantum residual, so the weights of a key sum to
    -- exactly one. NAMED and not inferred: a share that is a hair away from its
    -- declared ratio and a share that is exactly its ratio are different facts,
    -- and a reader auditing a split has to be able to tell them apart.
    carries_residual      BOOLEAN     NOT NULL DEFAULT FALSE,
    computed_by           TEXT        NOT NULL CHECK (btrim(computed_by) <> ''),
    computed_at           TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- A share of a total cannot exceed the total. The one set-level fact a single
    -- row CAN prove, and it catches the volume/total pair being written crossed.
    CONSTRAINT analytics_alignment_weights_share_ck CHECK (volume_value <= volume_total),
    -- A Datastream aligned with itself is not a cross.
    CONSTRAINT analytics_alignment_weights_pair_ck CHECK (
        left_datastream_id <> right_datastream_id
    )
);

-- ONE share per entity, per key, per day, per pair, per key version -- AND THE
-- FIRST RUN STANDS. `record_ventilation_weights` inserts with
-- ON CONFLICT DO NOTHING, so a second run over a volume that has since been
-- re-fetched leaves the published split exactly as it was published.
CREATE UNIQUE INDEX IF NOT EXISTS analytics_alignment_weights_uq_share
    ON app.analytics_alignment_weights
       (project_id, left_datastream_id, right_datastream_id, common_key_version_id,
        alignment_key, activity_date, right_row_key);

-- The read path: every share of one pair under one key version, in one index
-- scan, so `assert_stored_weights_conserve` can re-check a whole run.
CREATE INDEX IF NOT EXISTS analytics_alignment_weights_pair
    ON app.analytics_alignment_weights
       (project_id, left_datastream_id, right_datastream_id, common_key_version_id,
        alignment_key, activity_date);

ALTER TABLE app.analytics_alignment_weights ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.analytics_alignment_weights FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS analytics_alignment_weights_epic36
    ON app.analytics_alignment_weights;
CREATE POLICY analytics_alignment_weights_epic36 ON app.analytics_alignment_weights
    USING (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id))
    WITH CHECK (current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
           OR app.epic36_has_resource_access(org_id, 'project', project_id));

-- SELECT and INSERT, and deliberately NO UPDATE: a published split is not a
-- mutable field. Re-splitting a key under a corrected volume is a gesture no
-- story has opened, and leaving the privilege for it lying about would let one
-- arrive without one.
--
-- AND THE GRANT ALONE DOES NOT SAY THAT -- MEASURED, 2026-08-25. Migration 207
-- declares `ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA app GRANT
-- SELECT, INSERT, UPDATE, DELETE ON TABLES TO connector`, so EVERY table created
-- after it arrives with all four privileges already granted, and a narrower
-- GRANT adds nothing it did not already have:
--
--     SELECT grantee, privilege_type FROM information_schema.role_table_grants
--      WHERE table_schema = 'app' AND table_name = 'analytics_alignment_weights';
--     -- connector: DELETE, INSERT, SELECT, UPDATE   (before the REVOKE below)
--
-- Which is why migration 207 section 4 exists at all: it RESTATES, verbatim, the
-- narrower postures earlier migrations had declared, because the blanket grant
-- ran after them. A migration that states a narrow posture and writes only a
-- GRANT states something the database does not do. The REVOKE is the sentence.
--
-- DELETE IS KEPT, AND THAT IS NOT AN OVERSIGHT -- IT IS THE ERASURE. Both tables
-- below are ON DELETE CASCADE children of `app.organizations`, which is scope (b)
-- of `server/tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py`:
-- PostgreSQL checks the TABLE PRIVILEGE before any trigger, so a role with no
-- DELETE turns a customer's right-to-erasure request into a 42501 on a statement
-- nothing gets to explain. Migration 198 found that on `app.org_plan_history` and
-- 277 stated the final posture. So the narrow posture that is actually available
-- to an org-scoped table is NO UPDATE, and never no DELETE -- revoking DELETE was
-- tried here first and the ratified guard refused it, which is the guard doing
-- its job.
GRANT SELECT, INSERT, DELETE ON app.analytics_alignment_weights TO connector;
REVOKE UPDATE ON app.analytics_alignment_weights FROM connector;

-- THE SAME REPAIR, FORWARD, FOR THE TABLE OF STORY 70.3. Migration 309 states in
-- its own header that `app.analytics_alignment_decisions` "grants no UPDATE and
-- no DELETE rather than leaving the privilege lying about for one to arrive
-- without one", and `docs/product-architecture/capabilities/analytics-alignment.md`
-- repeats it. Measured on a database carrying 309, the `connector` role holds
-- UPDATE and DELETE on it anyway, for the default-privileges reason above. The
-- half of that sentence that CAN be true is made true here -- 309 is applied and
-- cannot be edited -- and it changes no row: nothing in `server/core` issues an
-- UPDATE against that table. The other half is withdrawn rather than pursued,
-- for the erasure reason above, and the card says so.
REVOKE UPDATE ON app.analytics_alignment_decisions FROM connector;

COMMENT ON TABLE app.analytics_alignment_weights IS
    'Story 70.4: how one coarse Analytics Alignment key was split across the media entities '
    'that answer it on one day, in proportion to a DECLARED volume. One row per entity per '
    'key per day; the weights of a (key, day) sum to exactly 1, asserted in Python because a '
    'CHECK cannot span a set. Carries the split and never the figure it split -- the observed '
    'metric is never copied here and never overwritten. Absent from mirror_sync.py, so no dbt '
    'model can read it. The first run of a (key, day, entity) stands.';
COMMENT ON COLUMN app.analytics_alignment_weights.volume_name IS
    'The DECLARED volume the split rides on -- clicks per day in the reference case. There is '
    'no default and there is no equal-parts fallback: a key whose declared volume is absent or '
    'zero is NOT ventilated, stays unmatched or ambiguous, and is counted.';
COMMENT ON COLUMN app.analytics_alignment_weights.volume_version IS
    'The version at which that volume was declared. A declaration that changed is a different '
    'split, so a figure published under the old one is not a figure about the new one.';
COMMENT ON COLUMN app.analytics_alignment_weights.carries_residual IS
    'TRUE on the one row of a (key, day) that absorbs the sub-quantum residual so the weights '
    'sum to exactly one. The carrier is the entity with the largest volume, ties broken by the '
    'lowest row key -- deterministic, because a residual that moved between two runs would '
    'make two runs of one declaration disagree.';

COMMIT;
