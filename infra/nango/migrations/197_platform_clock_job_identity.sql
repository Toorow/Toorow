-- 197: a platform clock BINDS the Cloud Scheduler job it drives, and that
-- binding is stored in the registry -- not composed from the environment.
--
-- WHY THIS EXISTS. Migration 195 stores the canonical short name of a clock
-- ('dispatch-nightly') and its own comment on `clock_name` says the rest out
-- loud: "The GCP job id is composed at read time from this name and the
-- environment prefix". That composition lived in
-- `core/platform_clocks.job_id_for`, which read `PLATFORM_CLOCK_JOB_PREFIX` from
-- the process environment. Jean, 2026-08-02: "je comprends pas pourquoi tu dois
-- mettre les horloges dans le deploiement", then "retire pour que ca soit propre,
-- si on va pas s'en servir ca va creer de la dette".
--
-- He is right on two counts, and they are different:
--   * it is a TRUTH ABOUT THE PLATFORM STORED OUTSIDE THE PLATFORM, which is the
--     exact failure this registry was created to end. A row that cannot say which
--     job it drives without an environment variable is a row still half in GCP;
--   * it invents a free failure mode. Forget the variable in one deployment and
--     the SEVEN declared clocks read `missing_in_gcp` while the SEVEN real jobs
--     read `unmanaged_in_gcp` -- fourteen loud lines, all false, and the cause
--     appears on no screen. The variable was already removed from
--     `.github/workflows/deploy.yml` (b72449c7); this migration removes the need
--     for it.
--
-- ─────────────────────────────────────────────────────────────────────────────
-- THE POPULATION DECISION, AND WHAT IT COSTS. Written here because a later
-- reader will otherwise re-derive it, and because the alternative was real.
--
-- The seven rows already in production drive jobs whose ids carry this
-- deployment's prefix. That prefix is a deployment identifier: writing it as a
-- literal in this file would put a production identifier in a repository that
-- must stay shareable, and would hardcode ONE deployment's answer into a file
-- every deployment runs. So it is not written here, in any form.
--
-- CHOSEN: the column is NULLABLE, and this migration binds the existing rows
-- ONLY from a value handed to THIS RUN by the operator, as a session setting:
--
--     PGOPTIONS="-c toorow.platform_clock_job_prefix=<prefix>" \
--         uv run python scripts/apply_migrations.py
--
-- When that setting is absent the rows stay NULL -- UNBOUND -- and the module
-- answers `unknown` with the reason `clock_unbound`. That is the whole point of
-- choosing NULL over a fabricated default: "I do not know which job this clock
-- drives" is TRUE, while `missing_in_gcp` (which a default of `clock_name` would
-- produce) is FALSE -- the job exists, we simply pointed at the wrong id. A
-- registry that fabricates a binding is a registry that lies quietly; one that
-- reports `unbound` names its own gap.
--
-- REJECTED: "leave it NULL and let `reconcile` resolve it" -- i.e. let the
-- reconciliation adopt the GCP job whose id ends with the clock name. That is
-- adoption, and adoption is the one thing this registry refuses: 195 already
-- carries `ck_platform_clocks_never_declares_unmanaged` so that a job nobody
-- declared can never become a declaration by observation. A heuristic on job
-- names would adopt a stranger's cron whose id happens to end the same way, and
-- would do it silently. Binding is a named act, never an inference.
--
-- THE COST, stated rather than discovered:
--   1. an operator who applies this migration without the session setting gets
--      seven clocks reading `unknown / clock_unbound` and seven jobs reading
--      `unmanaged_in_gcp`. Noisy -- but every line is true, and the repair is one
--      statement against their own database, no redeploy and no restart:
--          UPDATE app.platform_clocks
--             SET scheduler_job_id = <prefix> || clock_name
--           WHERE scheduler_job_id IS NULL;
--      Under the environment variable the same mistake was invisible and could
--      only be repaired by a deploy.
--   2. `scheduler_job_id` is NULLABLE, so every reader must handle "unbound".
--      `core/platform_clocks.py` does, in one place (`bound_job_id`), and refuses
--      to observe, apply or fire an unbound clock.
--   3. a NEW clock declared through `core.platform_clocks.declare` binds itself
--      to its own name unless the caller names a job. That is a CHOICE made at
--      declaration time and stored, not a guess about an id that already exists
--      elsewhere -- `apply` then creates exactly that job.
-- ─────────────────────────────────────────────────────────────────────────────
--
-- Additive only: one nullable column, one CHECK, one UNIQUE, one comment. No
-- table is created or dropped, no column removed, no row deleted, and no
-- deployment identifier is written. The trigger and the RLS policy installed by
-- 195 are left exactly as they are: `scheduler_job_id` belongs to neither the
-- declared nor the observed set of that trigger's "written separately" rule, and
-- it must stay editable -- it is how a wrong binding gets repaired.

BEGIN;

DO $migration$
DECLARE
    -- Handed to THIS RUN, never stored in this file. NULL when the operator did
    -- not supply it, which is a legitimate outcome (see the header).
    v_prefix  TEXT;
    v_bound   INTEGER := 0;
    v_unbound INTEGER := 0;
    -- Built with explicit `||` and emitted through `EXECUTE` / `RAISE '%'`:
    -- neither COMMENT ON nor RAISE accepts an expression where a string constant
    -- is expected, and relying on adjacent-literal concatenation inside a plpgsql
    -- body is the ambiguity migration 196 already documented.
    v_comment TEXT :=
        'The Cloud Scheduler job id this clock drives, as chosen by this '
        || 'deployment. NULL means UNBOUND: the registry does not know which job '
        || 'the clock drives and says so (verdict `unknown`, reason '
        || '`clock_unbound`) rather than guessing. Never composed from an '
        || 'environment variable, and never inferred from a job found in GCP.';
BEGIN
    IF to_regclass('app.platform_clocks') IS NULL THEN
        RAISE NOTICE '197: skip -- app.platform_clocks does not exist';
        RETURN;
    END IF;

    -- 195 FORCES row level security here and its policy opens only for a session
    -- that has declared platform-operator context. Transaction-local, so it can
    -- never leak onto a pooled connection.
    PERFORM set_config('toorow.platform_operator', 'on', true);

    ALTER TABLE app.platform_clocks
        ADD COLUMN IF NOT EXISTS scheduler_job_id TEXT;

    v_prefix := NULLIF(btrim(COALESCE(
        current_setting('toorow.platform_clock_job_prefix', true), '')), '');

    IF v_prefix IS NOT NULL THEN
        -- Only rows that bind nothing. A binding already present was chosen by
        -- somebody; re-imposing this run's prefix on it would be the silent
        -- overwrite the registry forbids everywhere else.
        UPDATE app.platform_clocks
           SET scheduler_job_id = v_prefix || clock_name
         WHERE scheduler_job_id IS NULL;
        GET DIAGNOSTICS v_bound = ROW_COUNT;
    END IF;

    -- Both constraints are added AFTER the population on purpose: a malformed
    -- prefix or a collision then fails the migration loudly instead of being
    -- discovered by the first reconciliation.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'ck_platform_clocks_scheduler_job_id'
           AND conrelid = 'app.platform_clocks'::regclass
    ) THEN
        -- Cloud Scheduler's own rule for a job id: a letter, then up to 499
        -- letters, digits, hyphens or underscores. Refusing the shapes that are
        -- certainly not job ids beats discovering them as an opaque 400 from GCP.
        --
        -- The length is a SEPARATE clause, and that is not a style choice:
        -- Postgres caps a regex repetition count at 255, so `{0,499}` is an
        -- INVALID regular expression. It is also a silent one -- adding the
        -- constraint over rows whose `scheduler_job_id` is NULL succeeds, because
        -- `IS NULL` short-circuits and the pattern is never compiled against a
        -- value. The first write of a real binding is what would have failed,
        -- months later, with "invalid repetition count(s)". Measured on
        -- PostgreSQL 17.2, 2026-08-02.
        ALTER TABLE app.platform_clocks
            ADD CONSTRAINT ck_platform_clocks_scheduler_job_id
            CHECK (scheduler_job_id IS NULL
                   OR (scheduler_job_id ~ '^[A-Za-z][A-Za-z0-9_-]*$'
                       AND length(scheduler_job_id) <= 500));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'uq_platform_clocks_scheduler_job_id'
           AND conrelid = 'app.platform_clocks'::regclass
    ) THEN
        -- Two clocks cannot drive the same job. Without this, the second row to
        -- claim a job id makes both of them read `in_sync` against ONE job while
        -- the other job runs undeclared -- a drift that reports itself as health.
        -- NULLs are exempt by the SQL rule, so any number of clocks may be
        -- unbound at once, which is the state this migration may legitimately
        -- leave behind.
        ALTER TABLE app.platform_clocks
            ADD CONSTRAINT uq_platform_clocks_scheduler_job_id
            UNIQUE (scheduler_job_id);
    END IF;

    EXECUTE 'COMMENT ON COLUMN app.platform_clocks.scheduler_job_id IS '
            || quote_literal(v_comment);

    SELECT count(*) INTO v_unbound
      FROM app.platform_clocks
     WHERE scheduler_job_id IS NULL;

    RAISE NOTICE '%', '197: ' || v_bound || ' clock(s) bound from the session '
        || 'prefix, ' || v_unbound || ' left unbound';

    IF v_unbound > 0 THEN
        RAISE NOTICE '%',
            '197: an unbound clock reads `unknown` (clock_unbound), never '
            || '`missing_in_gcp` -- the registry does not guess. Bind them '
            || 'against this deployment''s own database with: '
            || 'UPDATE app.platform_clocks SET scheduler_job_id = '
            || '<prefix> || clock_name WHERE scheduler_job_id IS NULL;';
    END IF;
END
$migration$;

COMMIT;
