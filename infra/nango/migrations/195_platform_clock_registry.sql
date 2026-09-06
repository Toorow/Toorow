-- AI-117: the platform clock registry -- what this deployment DECLARES its
-- clocks to be, next to what Cloud Scheduler was last OBSERVED to hold.
--
-- WHY THIS EXISTS. Five Cloud Scheduler jobs drive the whole platform
-- (dispatch-nightly, dispatch-hourly, reconcile-queues, poll-health,
-- drain-outbox). They exist ONLY in GCP: they were created by
-- `infra/gcp/provision_ad36_substrate.sh` and nothing in this repository, in the
-- database, or on any screen knows they exist. The consequences are all silent:
--   * nobody can see the cadence without opening the Google console;
--   * nobody can pause, edit or fire one from the product;
--   * a job edited by hand in GCP -- or never created in a new environment --
--     produces no signal at all. A clock that does not fire leaves NOTHING
--     behind, which is why `missed_run_count` stays at 0 and reads like health.
-- Jean's requirement, literal: "on doit avoir une sync entre l'interface et les
-- cloud schedule". This table is the half of that sync which survives a restart.
--
-- WHAT THIS IS NOT. It is NOT the cadence of a Datastream. That is a product
-- setting, it already exists (`app.datastream_schedule_state`: `next_run_at`,
-- watermark, `retry_count`, `missed_run_count`), it is already editable
-- (`core/schedule_mcp.py` -> `set_schedule`), and it is per-Datastream. The rows
-- below are the PLATFORM heartbeat: one set per deployment, invisible to an end
-- user, and they exist whether a Datastream exists or not. Nothing in this
-- migration touches, references or duplicates the Datastream schedule.
--
-- THE SCOPE DECISION, written here rather than left to be re-derived.
-- A platform clock has NO `org_id` and NO `project_id`, and that absence is
-- deliberate:
--   * one clock serves every organization at once -- `dispatch-nightly` walks
--     the whole ledger. Attributing it to an org would be a lie, and a nullable
--     `org_id` is how that lie gets written later by accident;
--   * these rows are deployment infrastructure. Their real owner is the GCP
--     project named by `CLOUD_SCHEDULER_PROJECT` / `CLOUD_TASKS_PROJECT`, which
--     is an environment fact, not a tenant fact.
-- Because there is no org, the Epic-36 org-membership gate cannot decide access
-- here: `app.epic36_has_resource_access` needs a target org and there is none.
-- The policy below therefore refuses EVERY org-scoped reader once strict
-- enforcement is armed, and opens only for a session that has explicitly
-- declared platform-operator context (`toorow.platform_operator = 'on'`, set by
-- `core.platform_clocks.arm_platform_clock_access`). Deny-by-default: a session
-- that forgets to arm it sees zero rows rather than another tenant's.
--
-- DECLARED vs OBSERVED ARE TWO DISTINCT COLUMN SETS, and that is the entire
-- point of the table. A drift is the DIFFERENCE between them, and it must be
-- READABLE. Nothing here corrects anything: an observation is written by
-- `reconcile()`, which never calls a mutating Cloud Scheduler method, and a
-- correction is `apply()`, which acts on ONE explicitly named clock. Silently
-- re-imposing the declared value would destroy the only evidence that somebody
-- changed a clock by hand.
--
-- Additive only. No table is dropped, no column is removed, no row is seeded --
-- in particular no job name, project or region is written here: those are
-- environment facts read from the environment, never literals in the repository.

BEGIN;

CREATE TABLE IF NOT EXISTS app.platform_clocks (
    -- ── identity ────────────────────────────────────────────────────────────
    -- The canonical SHORT name ('dispatch-nightly'), never the GCP job id. The
    -- deployment's job id is composed at read time from that name and the
    -- environment's prefix, so the same registry row is valid in every
    -- environment and no deployment identifier is stored in the repository.
    clock_name                          TEXT        NOT NULL,
    registry_policy_version             TEXT        NOT NULL
                                        DEFAULT 'platform-clock-v1',

    -- ── DECLARED: what the platform says this clock must be ─────────────────
    declared_schedule                   TEXT        NOT NULL,
    declared_timezone                   TEXT        NOT NULL,
    declared_target_path                TEXT        NOT NULL,
    declared_http_method                TEXT        NOT NULL DEFAULT 'POST',
    declared_attempt_deadline_seconds   INTEGER     NOT NULL DEFAULT 600,
    -- 'retired' means: this clock MUST NOT exist in GCP any more. It is a third
    -- declared state rather than a DELETE because deleting the row is how a
    -- clock becomes invisible again -- the exact failure this table exists to
    -- end. A retired clock still present in GCP is a drift, and says so.
    desired_state                       TEXT        NOT NULL DEFAULT 'enabled',
    -- Why this clock exists, in one sentence, for the person who finds it
    -- paused at 3am. A clock nobody can explain is a clock somebody deletes.
    purpose                             TEXT        NOT NULL,

    -- ── OBSERVED: what Cloud Scheduler held at the last reconciliation ──────
    -- All NULL until the first observation. NEVER written by hand, never
    -- written by `apply()`: only `reconcile()` writes this half.
    observed_at                         TIMESTAMPTZ,
    observed_state                      TEXT,
    observed_schedule                   TEXT,
    observed_timezone                   TEXT,
    observed_target_uri                 TEXT,
    observed_http_method                TEXT,
    observed_attempt_deadline_seconds   INTEGER,
    observed_last_attempt_at            TIMESTAMPTZ,
    observed_last_attempt_status        TEXT,

    -- ── the verdict, which is a comparison and not a state ──────────────────
    drift_verdict                       TEXT,
    -- {"schedule": {"declared": "...", "observed": "..."}, ...} -- the exact
    -- fields that differ. A verdict of 'drifted' with an empty detail is a
    -- verdict nobody can act on, so the CHECK below forbids it.
    drift_detail                        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    -- Why the verdict is 'unknown'. An uncertainty at an authorization or
    -- execution seam is never a yes: it is recorded, with its reason.
    observation_error                   TEXT,

    created_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                          TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    CONSTRAINT pk_platform_clocks PRIMARY KEY (clock_name),
    CONSTRAINT ck_platform_clocks_name
        CHECK (clock_name ~ '^[a-z][a-z0-9-]{2,60}$'),
    CONSTRAINT ck_platform_clocks_policy_version
        CHECK (registry_policy_version = 'platform-clock-v1'),
    -- Five whitespace-separated fields. This repository does not reimplement a
    -- cron parser in SQL; it refuses the shapes that are certainly not one.
    CONSTRAINT ck_platform_clocks_declared_schedule
        CHECK (declared_schedule ~ '^[^[:space:]]+([[:space:]]+[^[:space:]]+){4}$'),
    CONSTRAINT ck_platform_clocks_declared_timezone
        CHECK (declared_timezone ~ '^[A-Za-z][A-Za-z0-9_+-]*(/[A-Za-z0-9_+-]+)*$'),
    -- Every platform clock targets an internal endpoint. A clock pointed at a
    -- tenant-facing route would be an unauthenticated cron against the product.
    CONSTRAINT ck_platform_clocks_declared_path
        CHECK (declared_target_path ~ '^/internal/[A-Za-z0-9/_-]{1,200}$'),
    CONSTRAINT ck_platform_clocks_declared_method
        CHECK (declared_http_method IN ('POST', 'GET', 'PUT', 'PATCH', 'HEAD')),
    CONSTRAINT ck_platform_clocks_declared_deadline
        CHECK (declared_attempt_deadline_seconds BETWEEN 15 AND 1800),
    CONSTRAINT ck_platform_clocks_desired_state
        CHECK (desired_state IN ('enabled', 'paused', 'retired')),
    CONSTRAINT ck_platform_clocks_purpose
        CHECK (length(btrim(purpose)) BETWEEN 1 AND 500),

    -- UPDATE_FAILED is a real Cloud Scheduler Job.State and is kept verbatim:
    -- collapsing it into 'disabled' would hide the one state that says the last
    -- write to that job did not take.
    CONSTRAINT ck_platform_clocks_observed_state
        CHECK (observed_state IS NULL OR observed_state IN (
            'enabled', 'paused', 'disabled', 'update_failed', 'absent', 'unknown'
        )),
    CONSTRAINT ck_platform_clocks_observed_deadline
        CHECK (observed_attempt_deadline_seconds IS NULL
               OR observed_attempt_deadline_seconds >= 0),
    CONSTRAINT ck_platform_clocks_verdict
        CHECK (drift_verdict IS NULL OR drift_verdict IN (
            'in_sync', 'drifted', 'missing_in_gcp', 'unmanaged_in_gcp', 'unknown'
        )),
    CONSTRAINT ck_platform_clocks_drift_detail
        CHECK (jsonb_typeof(drift_detail) = 'object'),

    -- A verdict without the observation that produced it is a claim with no
    -- evidence; an observation with no verdict is evidence nobody read.
    CONSTRAINT ck_platform_clocks_observation_pair
        CHECK ((drift_verdict IS NULL) = (observed_at IS NULL)),
    -- 'drifted' must NAME what differs, or it cannot be acted on.
    CONSTRAINT ck_platform_clocks_drifted_is_detailed
        CHECK (drift_verdict IS DISTINCT FROM 'drifted'
               OR drift_detail <> '{}'::jsonb),
    -- 'in_sync' is the strongest claim in the table: it must carry no
    -- difference and no error.
    CONSTRAINT ck_platform_clocks_in_sync_is_clean
        CHECK (drift_verdict IS DISTINCT FROM 'in_sync'
               OR (drift_detail = '{}'::jsonb AND observation_error IS NULL)),
    -- 'unknown' must say WHY. Fail-closed is only useful when it is legible.
    CONSTRAINT ck_platform_clocks_unknown_is_explained
        CHECK (drift_verdict IS DISTINCT FROM 'unknown'
               OR observation_error IS NOT NULL),
    -- The registry DECLARES; it never absorbs what it did not declare. A job
    -- found in GCP with no row here is reported as 'unmanaged_in_gcp' by the
    -- reconciliation and is NOT inserted -- writing it would silently turn an
    -- undeclared job into a declaration.
    CONSTRAINT ck_platform_clocks_never_declares_unmanaged
        CHECK (drift_verdict IS DISTINCT FROM 'unmanaged_in_gcp')
);

-- This comment deliberately does not name the OTHER schedule table: a reader who
-- greps for that name must land on its owner, never on this one.
COMMENT ON TABLE app.platform_clocks IS
    'AI-117: the platform heartbeat registry. DECLARED cadence next to the '
    'Cloud Scheduler state last OBSERVED. Platform-scoped: no org, no project. '
    'One set per deployment; NOT the per-flux product cadence, which is owned '
    'by its own table and its own MCP tool.';

COMMENT ON COLUMN app.platform_clocks.clock_name IS
    'Canonical short name (e.g. dispatch-nightly). The GCP job id is composed '
    'at read time from this name and the environment prefix, so no deployment '
    'identifier is stored here.';

COMMENT ON COLUMN app.platform_clocks.drift_verdict IS
    'in_sync | drifted | missing_in_gcp | unknown. unmanaged_in_gcp is a '
    'verdict about a job with NO row here, so it is reported and never stored.';

CREATE INDEX IF NOT EXISTS idx_platform_clocks_drifted
    ON app.platform_clocks (drift_verdict, observed_at DESC)
    WHERE drift_verdict IS DISTINCT FROM 'in_sync';

-- ---------------------------------------------------------------------------
-- The guard. Three things it refuses, each because the alternative is silent.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_platform_clock()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $body$
BEGIN
    IF TG_OP = 'DELETE' THEN
        -- Deleting the declaration is how a clock becomes invisible again --
        -- precisely the failure this table ends. Retire it instead
        -- (desired_state='retired'), which keeps the drift readable.
        RAISE EXCEPTION 'a platform clock is retired, never deleted'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        -- A declaration is born unobserved. Inserting a row that already claims
        -- a verdict would let a caller assert a GCP state nobody looked at.
        IF NEW.observed_at IS NOT NULL OR NEW.drift_verdict IS NOT NULL THEN
            RAISE EXCEPTION 'a platform clock is declared before it is observed'
                USING ERRCODE = '23000';
        END IF;
        RETURN NEW;
    END IF;

    IF NEW.clock_name IS DISTINCT FROM OLD.clock_name
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'platform clock identity is immutable'
            USING ERRCODE = '23000';
    END IF;

    -- An observation is never written in the same statement as a declaration.
    -- Allowing both would let a writer "fix" the drift by moving the declared
    -- value onto the observed one, which is the exact silence forbidden above.
    IF (NEW.declared_schedule IS DISTINCT FROM OLD.declared_schedule
        OR NEW.declared_timezone IS DISTINCT FROM OLD.declared_timezone
        OR NEW.declared_target_path IS DISTINCT FROM OLD.declared_target_path
        OR NEW.declared_http_method IS DISTINCT FROM OLD.declared_http_method
        OR NEW.declared_attempt_deadline_seconds
           IS DISTINCT FROM OLD.declared_attempt_deadline_seconds
        OR NEW.desired_state IS DISTINCT FROM OLD.desired_state)
       AND (NEW.observed_at IS DISTINCT FROM OLD.observed_at
            OR NEW.drift_verdict IS DISTINCT FROM OLD.drift_verdict
            OR NEW.observed_state IS DISTINCT FROM OLD.observed_state
            OR NEW.observed_schedule IS DISTINCT FROM OLD.observed_schedule) THEN
        RAISE EXCEPTION
            'a platform clock declaration and its observation are written separately'
            USING ERRCODE = '23000';
    END IF;

    NEW.updated_at := NOW();
    RETURN NEW;
END;
$body$;

DROP TRIGGER IF EXISTS trg_platform_clock_protect ON app.platform_clocks;
CREATE TRIGGER trg_platform_clock_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.platform_clocks
    FOR EACH ROW EXECUTE FUNCTION app.protect_platform_clock();

-- ---------------------------------------------------------------------------
-- RLS. See the scope decision in the header: there is no org to check, so the
-- Epic-36 helper cannot decide here. Under strict enforcement the table opens
-- only for a session that declared platform-operator context.
-- ---------------------------------------------------------------------------
ALTER TABLE app.platform_clocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE app.platform_clocks FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS platform_clocks_strict ON app.platform_clocks;
CREATE POLICY platform_clocks_strict ON app.platform_clocks
    USING (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR current_setting('toorow.platform_operator', true) = 'on'
    )
    WITH CHECK (
        current_setting('toorow.enforce_epic36', true) IS DISTINCT FROM 'on'
        OR current_setting('toorow.platform_operator', true) = 'on'
    );

COMMIT;
