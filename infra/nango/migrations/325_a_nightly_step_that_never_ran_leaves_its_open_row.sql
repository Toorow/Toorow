-- execution-substrate `Incomplete if` 2, THIRD LOCUS: the nightly STEPS.
--
-- "A scheduled run that does not happen leaves no record that it did not
-- happen." Two of the three loci were closed before this migration:
--
--   (a) a Datastream occurrence that was stepped over is CHARGED
--       (`scheduler._advance_next_run`, `app.datastream_schedule_state`);
--   (b) the platform clock that did not fire is OBSERVED
--       (`app.platform_clocks.observed_last_attempt_status`, migration 195,
--       written by the hourly reconcile-clocks job).
--
-- The third was open, and it is the one INSIDE the run. `run_nightly_steps`
-- executes a sequence of named steps through `_run_isolated_step`, which
-- deliberately never re-raises so that one failure cannot take the night down.
-- Everything it knows it says to a log line and, on failure, to a `meta_alert`.
-- So a step that SILENTLY NEVER RUNS -- an exception thrown before its call
-- site, an early return, a step name added to the sequence whose call site is
-- dead, a container killed mid-night -- leaves NOTHING behind: no row, no
-- firing, and a log stream that is only evidence of what did happen. Reading
-- "no meta_alert" as "the night was fine" is the same shape as reading
-- `missed_run_count = 0` as health, which is the sentence migration 195 was
-- written about.
--
-- THE SHAPE THIS TABLE TAKES, AND WHY IT IS THE ONE THE CODE SUPPORTS.
-- The whole declared step LIST is written AT DISPATCH, before any step runs,
-- one row per step with `started_at` NULL. Each step then stamps `started_at`
-- immediately BEFORE it is executed and closes the row AFTER, each in its own
-- committed transaction. Three states, and no fourth is inventable:
--
--   declared    started_at IS NULL              the step never began. THE
--                                               SILENCE THE CLAUSE FORBIDS,
--                                               and now it is a row.
--   started     started_at set, ended_at NULL   the step began and never
--                                               finished. A crash between the
--                                               two writes IS this row, and the
--                                               open row is the record.
--   closed      ended_at + outcome              succeeded, or failed with the
--                                               class of what was raised.
--
-- The alternative -- derive the silence by diffing a declared list against the
-- night's rows -- was rejected because the declared list would then live only
-- in the reader, and a reader is exactly what is missing at 3am. Writing the
-- list first makes the absence a ROW, which a person can see, sort and count
-- without knowing what the list was supposed to contain.
--
-- WHY THERE ARE ONLY TWO OUTCOMES, and no `skipped`. Several nightly steps are
-- guarded by an environment flag (`DBT_NIGHTLY_ENABLED`, `TOOROW_CACHE_ENABLED`,
-- `SCHEMA_CONTEXT_ENABLED`) and return normally when the flag is off. From the
-- step boundary that is indistinguishable from work done, so a `skipped`
-- written there would be a claim nothing supports -- and a vocabulary value
-- nothing writes is a value a later reader will guess the meaning of. The two
-- outcomes are the two the boundary can actually observe.
--
-- DURATION IS NOT A COLUMN. It is `ended_at - started_at`, and a derivable
-- value is not stored.
--
-- THE PAYLOAD IS NEVER STORED. `error_class` takes the exception CLASS NAME and
-- the CHECK below enforces that shape (an identifier, no whitespace, 120 chars):
-- a step's exception message can carry a connector response, a row of customer
-- data or a credential fragment, and a platform-scoped table with no org is
-- exactly where none of that may land. The message keeps going to the log and
-- to `app.alert_firings`, which is where it already was.
--
-- SCOPE: PLATFORM, and therefore NO RGPD ERASURE HATCH IS OWED.
-- This table has no `org_id`, no `project_id` and no foreign key of any kind.
-- `core.org_purge.plan_purge` builds its statements by walking the foreign-key
-- graph out of `app.organizations` over NO ACTION / RESTRICT edges, so it never
-- reaches a table with no edge at all; nor is this a CASCADE child of
-- `app.organizations`. Both scopes of
-- `tests/conformance/test_the_erasure_hatch_is_a_privilege_too.py` and of
-- `test_immutability_triggers_yield_to_erasure.py` are therefore empty here,
-- and the DELETE guard below deliberately carries NO `rgpd_erasure` WHEN
-- clause: an org erasure has no business deleting a row that says the platform
-- ran, or did not run, a maintenance step. This is NOT `core.org_purge`.
--
-- WHAT THE DELETE GUARD COSTS, said with the arithmetic rather than assumed.
-- The declared sequence is ELEVEN steps -- counted, not assumed, by reading the
-- call sites out of `run_nightly_steps` itself -- at one night per day: 4 015
-- rows a year. Blocking DELETE for good is affordable at that rate, and it is
-- the only shape under which "there is no record" cannot be produced by a
-- statement.
--
-- Additive only. No table is dropped, no column removed, no row seeded -- in
-- particular no step name is written here: the sequence is code
-- (`core.scheduler.NIGHTLY_STEPS`), and a catalogue shipped with the product is
-- not read from a table.

BEGIN;

CREATE TABLE IF NOT EXISTS app.nightly_step_runs (
    -- ── identity: one night, one step ───────────────────────────────────────
    -- `run_id` is the `nrun_<ULID>` that `run_nightly_steps` already mints for
    -- briefing provenance. Reusing it rather than minting a second id is what
    -- lets a briefing and the step that built it be joined by eye.
    run_id          TEXT        NOT NULL,
    as_of_date      DATE        NOT NULL,
    step_name       TEXT        NOT NULL,
    -- Position in the declared sequence. Stored because it is NOT derivable
    -- from this table: the order is a property of the code that dispatched the
    -- night, and a night whose sequence changed must still read in the order it
    -- actually ran.
    step_ordinal    SMALLINT    NOT NULL,

    -- ── the three states ────────────────────────────────────────────────────
    declared_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    ended_at        TIMESTAMPTZ,
    outcome         TEXT,
    error_class     TEXT,

    CONSTRAINT pk_nightly_step_runs PRIMARY KEY (run_id, step_name),

    CONSTRAINT ck_nightly_step_runs_run_id
        CHECK (run_id ~ '^nrun_[0-9A-Za-z]{10,64}$'),
    CONSTRAINT ck_nightly_step_runs_step_name
        CHECK (step_name ~ '^[a-z][a-z0-9_]{2,60}$'),
    CONSTRAINT ck_nightly_step_runs_ordinal
        CHECK (step_ordinal BETWEEN 0 AND 200),

    CONSTRAINT ck_nightly_step_runs_outcome
        CHECK (outcome IS NULL OR outcome IN ('succeeded', 'failed')),
    -- A row is closed by BOTH or by neither. An `ended_at` with no outcome is a
    -- step that finished into no verdict; an outcome with no `ended_at` is a
    -- verdict with no moment.
    CONSTRAINT ck_nightly_step_runs_closed_pair
        CHECK ((ended_at IS NULL) = (outcome IS NULL)),
    -- A step cannot end without having begun. Without this, an UPDATE that
    -- skipped the `started_at` write would produce a row that reads like a step
    -- which finished instantly, which is the collapse the table exists to stop.
    CONSTRAINT ck_nightly_step_runs_ended_implies_started
        CHECK (ended_at IS NULL OR started_at IS NOT NULL),
    CONSTRAINT ck_nightly_step_runs_ends_after_it_starts
        CHECK (ended_at IS NULL OR ended_at >= started_at),
    -- 'failed' must NAME what was raised, or it cannot be acted on -- the same
    -- rule migration 195 applies to a `drifted` verdict with no detail.
    CONSTRAINT ck_nightly_step_runs_failure_names_its_class
        CHECK (outcome IS DISTINCT FROM 'failed'
               OR (error_class IS NOT NULL AND length(btrim(error_class)) > 0)),
    -- 'succeeded' is the strongest claim in the table: it carries no error.
    CONSTRAINT ck_nightly_step_runs_success_is_clean
        CHECK (outcome IS DISTINCT FROM 'succeeded' OR error_class IS NULL),
    -- THE PAYLOAD GUARD. A class name is an identifier. Anything with
    -- whitespace, punctuation or a quote in it is a message, and a message is
    -- exactly what must never land here.
    CONSTRAINT ck_nightly_step_runs_error_class_is_a_class
        CHECK (error_class IS NULL
               OR error_class ~ '^[A-Za-z_][A-Za-z0-9_.]{0,120}$')
);

COMMENT ON TABLE app.nightly_step_runs IS
    'One row per declared nightly step per night. The whole step list is '
    'written at dispatch, so a step that never began is an OPEN row rather '
    'than an absence. Platform-scoped: no org, no project, no payload.';

COMMENT ON COLUMN app.nightly_step_runs.started_at IS
    'NULL means the step never began -- the silence execution-substrate '
    '"Incomplete if" 2 forbids, recorded as a row.';

COMMENT ON COLUMN app.nightly_step_runs.error_class IS
    'The exception CLASS name only. The message goes to the log and to '
    'app.alert_firings; it never lands here.';

-- The reader's index: the last night, in the order the steps ran.
CREATE INDEX IF NOT EXISTS idx_nightly_step_runs_by_night
    ON app.nightly_step_runs (as_of_date DESC, run_id, step_ordinal);

-- The question the operator actually asks: what did not close?
CREATE INDEX IF NOT EXISTS idx_nightly_step_runs_unfinished
    ON app.nightly_step_runs (as_of_date DESC, step_ordinal)
    WHERE ended_at IS NULL;

-- ---------------------------------------------------------------------------
-- The guard. Four refusals, each because the alternative is a silence.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_nightly_step_run()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $body$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- Deleting the row is how a step that did not run becomes invisible
        -- again -- the exact failure this table ends. NO `rgpd_erasure` hatch:
        -- see the header. There is no org here to erase.
        RAISE EXCEPTION
            'a nightly step run is recorded, never deleted (step %, night %)',
            OLD.step_name, OLD.as_of_date
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        -- A step is declared before it is judged. A row born closed would let a
        -- caller assert an outcome for work nobody watched begin.
        IF NEW.ended_at IS NOT NULL OR NEW.outcome IS NOT NULL THEN
            RAISE EXCEPTION
                'a nightly step is declared before it is judged (step %)',
                NEW.step_name
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.run_id IS DISTINCT FROM OLD.run_id
       OR NEW.step_name IS DISTINCT FROM OLD.step_name
       OR NEW.as_of_date IS DISTINCT FROM OLD.as_of_date
       OR NEW.step_ordinal IS DISTINCT FROM OLD.step_ordinal
       OR NEW.declared_at IS DISTINCT FROM OLD.declared_at THEN
        RAISE EXCEPTION 'a nightly step run identity is immutable (step %)',
            OLD.step_name
            USING ERRCODE = '23000';
    END IF;

    -- A closed row is frozen WHOLE. Re-opening one, or rewriting its verdict,
    -- would let a later pass over the same night replace a failure with a
    -- success -- and the point of the row is that it survives the thing that
    -- would rather it did not.
    IF OLD.ended_at IS NOT NULL THEN
        RAISE EXCEPTION
            'a closed nightly step run is frozen (step %, night %)',
            OLD.step_name, OLD.as_of_date
            USING ERRCODE = '23000';
    END IF;

    -- `started_at` is the moment the step began; there is only one of those.
    IF OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'a nightly step begins once (step %)', OLD.step_name
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS trg_nightly_step_run_protect ON app.nightly_step_runs;
CREATE TRIGGER trg_nightly_step_run_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.nightly_step_runs
    FOR EACH ROW EXECUTE FUNCTION app.protect_nightly_step_run();

-- ---------------------------------------------------------------------------
-- RLS -- the same posture as `app.platform_clocks`, for the same reason.
-- There is no org in scope, so `app.epic36_has_resource_access` has nothing to
-- check. Under strict enforcement the table opens only for a session that has
-- declared platform-operator context
-- (`core.platform_clocks.arm_platform_clock_access`). Deny by default: a
-- session that forgets to arm it sees zero rows rather than the platform's.
-- ---------------------------------------------------------------------------
ALTER TABLE app.nightly_step_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.nightly_step_runs FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS nightly_step_runs_strict ON app.nightly_step_runs;
CREATE POLICY nightly_step_runs_strict ON app.nightly_step_runs
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR current_setting('toorow.platform_operator', true) = 'on'
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR current_setting('toorow.platform_operator', true) = 'on'
    );

COMMIT;
