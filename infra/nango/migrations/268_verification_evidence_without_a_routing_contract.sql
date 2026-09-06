-- 268 -- Verification evidence for an installation that owes no domain (AI-206).
--
-- The companion of 267, and the FOURTH wall of the same chain. 267 let a module
-- connector open at VERIFYING with no domain configuration; this one lets it
-- record the evidence that gets it to READY.
--
-- WHAT WAS MEASURED, by driving the chain against a disposable database rather
-- than reading it. Each wall was invisible until the one before it fell:
--
--   1. `apply_installation` sent every connector to DOMAIN_PENDING, carrying the
--      next action « configure domain routing » -- a gesture that does not exist
--      for a connector with no domain.                          (fixed: caller)
--   2. `app.protect_connector_installation` pinned `state = 'DOMAIN_PENDING'` on
--      INSERT, so repairing the caller alone produced a refused row.
--                                                               (fixed: 267)
--   3. `run_verification` refused without an active domain config.
--                                                               (fixed: caller)
--   4. `app.validate_connector_verification_run_write` refuses a run row whose
--      `domain_config_id` does not match an ACTIVE config -- and a NULL matches
--      nothing.                                                 (fixed: HERE)
--
--     connector verification requires the active matching domain config
--
-- WHAT CHANGES, AND NOTHING ELSE. The domain-config identity block is skipped
-- when `domain_config_id IS NULL` on a run that is NOT a synthetic delivery.
-- Every other rule of 180 is reproduced verbatim: the platform operation
-- provenance, the installation identity match, the bounded evidence
-- classification, the TTL window, the safe blocking-reason vocabulary and the
-- outcome metadata consistency.
--
-- A SYNTHETIC DELIVERY STILL REQUIRES ONE, and that asymmetry is the point. A
-- synthetic delivery IS the routing contract being exercised -- it sends
-- something through the domain -- so without a config it has nothing to send
-- through and its evidence would describe nothing. An `auth_check` proves the
-- authorization, which exists whether or not anything is routed.
--
-- `app.connector_verification_runs.domain_config_id` was ALREADY nullable
-- (migration 085): the column always allowed this, and only the trigger did not.
-- So no column is altered here, and no historical row is touched -- a run
-- written with a config keeps being checked against it exactly as before.
--
-- WHAT THIS DOES NOT DELIVER. `connector_verification_api._build_checks` still
-- returns `[]`, so a real deployment still evaluates zero checks and advances no
-- state -- the core reads that as `not_configured`, which is honest. Supplying a
-- REAL `auth_check` is not wiring: an installation is PLATFORM-scope
-- (environment + connector_name) while every authorization in this product is a
-- customer's own OAuth consent, held per project. There is no platform-level
-- credential to check with, and inventing one would contradict the 2026-08-11
-- rule that what is Google goes through the person's consent. That question is
-- named in the action item, not answered here.
--
-- Schema-Change-Checklist:
--   [x] Additive and replayable (CREATE OR REPLACE + trigger recreation)
--   [x] No column altered, no populated row rewritten
--   [x] Every rule of 180 preserved; one identity check made conditional
--   [x] Source-agnostic: no provider name appears in this function
--
-- ERASURE: creates no table. `app.connector_verification_runs` is
-- platform-scope and carries no org column, so `core.org_purge` has nothing here
-- to reach.

BEGIN;

CREATE OR REPLACE FUNCTION app.validate_connector_verification_run_write()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    operation_command TEXT;
    operation_org TEXT;
    installation_environment TEXT;
    installation_connector TEXT;
    config_installation TEXT;
    config_environment TEXT;
    config_connector TEXT;
    config_superseded_by TEXT;
BEGIN
    SELECT command_type, effective_org_id
      INTO operation_command, operation_org
      FROM app.operations
     WHERE id = NEW.operation_id;

    IF operation_command IS DISTINCT FROM 'connector.verification.ran'
       OR operation_org IS NOT NULL
    THEN
        RAISE EXCEPTION
            'connector verification requires a platform verification operation'
            USING ERRCODE = '23000';
    END IF;

    SELECT environment, connector_name
      INTO installation_environment, installation_connector
      FROM app.connector_installations
     WHERE id = NEW.installation_id;

    IF installation_environment IS DISTINCT FROM NEW.environment
       OR installation_connector IS DISTINCT FROM NEW.connector_name
    THEN
        RAISE EXCEPTION
            'connector verification does not match its installation'
            USING ERRCODE = '23000';
    END IF;

    -- AI-206: an installation that owes no routing contract records evidence
    -- without one. A synthetic delivery is exempt from the exemption -- it is
    -- the routing contract being exercised.
    IF NEW.domain_config_id IS NOT NULL OR NEW.synthetic_delivery THEN
        SELECT installation_id, environment, connector_name, superseded_by
          INTO config_installation, config_environment, config_connector,
               config_superseded_by
          FROM app.connector_domain_configs
         WHERE id = NEW.domain_config_id;

        IF config_installation IS DISTINCT FROM NEW.installation_id
           OR config_environment IS DISTINCT FROM NEW.environment
           OR config_connector IS DISTINCT FROM NEW.connector_name
           OR config_superseded_by IS NOT NULL
        THEN
            RAISE EXCEPTION
                'connector verification requires the active matching domain config'
                USING ERRCODE = '23000';
        END IF;
    END IF;

    IF NEW.evidence_hash IS NULL
       OR NEW.evidence_hash !~ '^[0-9a-f]{64}$'
       OR NEW.ttl_seconds IS NULL
       OR NEW.ttl_seconds NOT BETWEEN 1 AND 86400
       OR (
           NEW.synthetic_delivery
           AND (
               NEW.evidence_class IS DISTINCT FROM 'synthetic_delivery_auth'
               OR NEW.outcome = 'degraded'
           )
       )
       OR (
           NOT NEW.synthetic_delivery
           AND NEW.evidence_class NOT IN ('routing_check', 'auth_check')
       )
       -- A run with no routing contract cannot carry ROUTING evidence: there is
       -- no route for it to describe. The evidence class and the presence of a
       -- domain config have to agree, or the ledger would hold a routing verdict
       -- about nothing.
       OR (
           NEW.domain_config_id IS NULL
           AND NOT NEW.synthetic_delivery
           AND NEW.evidence_class IS DISTINCT FROM 'auth_check'
       )
    THEN
        RAISE EXCEPTION
            'connector verification evidence classification is invalid'
            USING ERRCODE = '23000';
    END IF;

    IF NEW.blocking_reason IS NOT NULL
       AND NEW.blocking_reason NOT IN (
           'connector_disabled',
           'dependency_unavailable',
           'domain_configuration_pending',
           'domain_route_misconfigured',
           'installation_not_applied',
           'synthetic_verification_failed',
           'verification_failed'
       )
    THEN
        RAISE EXCEPTION
            'connector verification blocking reason is not a safe code'
            USING ERRCODE = '23000';
    END IF;

    IF (NEW.outcome = 'passed' AND (
            NEW.blocking_reason IS NOT NULL OR NEW.first_seen_at IS NOT NULL
        ))
       OR (NEW.outcome = 'failed' AND (
            NEW.blocking_reason IS NULL OR NEW.first_seen_at IS NOT NULL
        ))
       OR (NEW.outcome = 'degraded' AND (
            NEW.blocking_reason IS NULL OR NEW.first_seen_at IS NULL
        ))
    THEN
        RAISE EXCEPTION
            'connector verification outcome metadata is inconsistent'
            USING ERRCODE = '23000';
    END IF;

    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_verification_run_validate_write
    ON app.connector_verification_runs;
CREATE CONSTRAINT TRIGGER trg_connector_verification_run_validate_write
    AFTER INSERT ON app.connector_verification_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION app.validate_connector_verification_run_write();

COMMENT ON FUNCTION app.validate_connector_verification_run_write() IS
    'Story 38.4 corrective guard, widened by AI-206: operation provenance, '
    'active installation identity, bounded safe evidence, TTL and outcome '
    'metadata -- and a domain contract required only of a run that has one or '
    'that exercises routing, so an auth_check can stand alone.';

COMMIT;
