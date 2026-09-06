-- 298_the_last_ungoverned_dq_baseline_is_retired.sql
--
-- `app.dq_baselines` LEAVES. Story 49.4, criterion 3 of
-- docs/product-architecture/governance.md -- "reconciliation, DQ or capability
-- rules create a parallel semantic or evidence store".
--
-- WHAT WAS RATIFIED AND NEVER IMPLEMENTED. On 2026-08-17 governance.md wrote a
-- settled order for this store: a published DQ Monitor version wins, and
-- `app.dq_baselines` answers only "for a Datastream no published monitor
-- covers". Measured 2026-08-21, NO CODE ANYWHERE ASKED WHETHER A PUBLISHED
-- MONITOR COVERED A DATASTREAM. `dq_monitors._run_monitors_for_datastream`
-- passed `version=None` for every enabled Datastream, unconditionally, so the
-- nightly sweep read and wrote this table for every stream -- including the ones
-- a published monitor already covered. The ratified sentence was true of the
-- manual route only, and the manual route is not what runs at night.
--
-- WHY THAT MAKES IT A PARALLEL STORE AND NOT A LAYER BELOW. Migration 145
-- REFUSES to apply while this table holds a single row:
--
--     'Controls & Quality migration refused: % mutable DQ baseline(s) exist.
--      They carry no monitor identity, no version and no decision date, so
--      adopting them would invent the governed identity they never had.'
--
-- The sweep then re-created exactly those rows, every night, on the evaluation
-- path. A store one migration declares un-adoptable and the runtime keeps
-- refilling is a second opinion, whatever layer it is called. And a check that
-- WRITES while it reads is not a read: `_write_dq_baseline` was called from
-- inside `_check_schema`.
--
-- WHERE THE BASELINE LIVES NOW. In the `baseline.columns` of the Datastream's
-- published `schema` DQ Monitor version -- an immutable row with a version
-- number, a date and an actor. `schema` was the LAST check still off the
-- governed store: `volume`, `arrival_timeliness`, `null_rate` and `zero_rows`
-- have frozen their reference inside a published version since story 59.5,
-- through `dq_monitor_bridge`. Counted in production on 2026-08-21:
-- 31 published monitor versions (null_rate 11, volume 10, zero_rows 10) and
-- ZERO of profile `schema` -- the check that kept its own table is exactly the
-- check that never appeared in the governed registry.
--
-- WHY THIS DROPS RATHER THAN MIGRATES. There is nothing to carry over, and
-- migration 145 already said why: a row keyed on `datastream_id` alone names no
-- monitor and no decision, so adopting it would mint a governed identity nobody
-- ever decided. The first sweep after this migration freezes a first
-- observation as a published version, with `system` as its actor and the date it
-- happened -- which is the honest identity these rows never had.
--
-- IT REFUSES RATHER THAN DESTROYS. Production held 0 rows when this was written
-- (SELECT count(*) FROM app.dq_baselines -> 0), but a deployment that has been
-- running the old sweep may not. Dropping evidence silently is worse than a
-- migration that stops and says what to do, so a non-empty table raises here.

BEGIN;

DO $$
DECLARE
    remaining BIGINT;
BEGIN
    IF to_regclass('app.dq_baselines') IS NULL THEN
        RAISE NOTICE 'app.dq_baselines is already absent; nothing to retire.';
        RETURN;
    END IF;

    SELECT COUNT(*) INTO remaining FROM app.dq_baselines;
    IF remaining > 0 THEN
        RAISE EXCEPTION
            'DQ baseline retirement refused: % mutable baseline(s) are still stored. '
            'Each names a Datastream and nothing else -- no monitor, no version, no '
            'decision date -- so this migration will not decide for them and will not '
            'delete them. Let the sweep freeze each Datastream first observation as a '
            'published `schema` DQ Monitor version, then apply this migration.',
            remaining
            USING ERRCODE = '23000';
    END IF;

    DROP INDEX IF EXISTS app.idx_dq_baselines_datastream_id;
    DROP TABLE app.dq_baselines;
    RAISE NOTICE 'app.dq_baselines retired: 0 rows, no reader, no writer.';
END $$;

COMMIT;
