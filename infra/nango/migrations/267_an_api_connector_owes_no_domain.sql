-- 267 -- An installation that owes no domain may start at VERIFYING (AI-206).
--
-- WHAT WAS MEASURED, 2026-08-16. `apply_installation` works and is routed, so
-- the action item's « rien ne permet de l'enregistrer » was false. What is true
-- is worse: every connector landed in DOMAIN_PENDING carrying the next action
-- « platform_admin: configure domain routing to advance to VERIFYING », and for
-- a module connector that gesture DOES NOT EXIST. It could never leave.
--
--   apply_installation(connector_name='google-ads')
--     -> state            DOMAIN_PENDING
--        blocking_cause   domain_configuration_pending
--        safe_next_action platform_admin: configure domain routing ...
--
-- `app.connector_domain_configs` carries `domain`, `provider_adapter`,
-- `webhook_endpoint_version`, `signing_secret_ref` and `dns_evidence_*`. Those
-- are inbound-transport notions -- an email domain and its DNS proof. Measured
-- across the 39 module manifests in `server/modules`: NOT ONE declares a domain,
-- a receipt adapter or a transport. 18 declare an `auth` block, 21 go through
-- Nango, and none of them has a domain to configure.
--
-- The domain step belongs to the inbound family, whose single member is
-- `managed_feed` -- and `managed_feed` is not a module. So the second step of
-- this lifecycle was written for one thing and required of everything.
--
-- WHY A MIGRATION AND NOT ONLY PYTHON. The rule is in the schema too: migration
-- 178's INSERT branch pins `state = 'DOMAIN_PENDING'` AND
-- `blocking_cause = 'domain_configuration_pending'`. Repairing only the caller
-- would have produced a row the trigger refuses -- which is how this was found,
-- by driving `apply_installation` against a disposable database rather than
-- reading it.
--
-- WHAT CHANGES, AND NOTHING ELSE. The INSERT branch admits a second opening
-- state, `VERIFYING`, and only with `blocking_cause IS NULL`. Everything else of
-- 178 is reproduced verbatim: append-only, immutable identity, the operation
-- binding, the transition graph, the closed metadata vocabulary.
--
-- NO CAUSE, rather than a new code. A connector in VERIFYING is not blocked, it
-- is in progress: a blocking cause names something that must be RESOLVED, and
-- waiting for an automated check is not that. Inventing `verification_pending`
-- would make a normal step read as an obstacle on every screen that lists
-- blocked connectors.
--
-- STILL REFUSED, deliberately: READY on insert. The initial apply never jumps to
-- READY -- that was AC5 of story 38.2 and it is untouched. Verification has to
-- run and leave its evidence.
--
-- `NOT_INSTALLED -> VERIFYING` joins the transition graph for the same reason: a
-- state that can be INSERTED but not REACHED is an inconsistency the next reader
-- trips on.
--
-- Schema-Change-Checklist:
--   [x] Additive and replayable (CREATE OR REPLACE + trigger recreation)
--   [x] No populated column is rewritten, no existing row is touched
--   [x] Every rule of 178 is preserved; one opening state is added
--   [x] Source-agnostic: no provider name appears in this function
--
-- ERASURE: creates no table, so `core.org_purge` has nothing here to reach.
-- `app.connector_installations` is platform-scope and carries no org column.

BEGIN;

CREATE OR REPLACE FUNCTION app.protect_connector_installation()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    lifecycle_changed BOOLEAN;
    operation_command TEXT;
    operation_org TEXT;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'connector_installations is append-only (no DELETE)'
            USING ERRCODE = '23000';
    END IF;

    IF TG_OP = 'INSERT' THEN
        -- AI-206: two openings, not one. DOMAIN_PENDING for a transport that owes
        -- a routing contract, VERIFYING for an installation that owes none.
        IF NEW.state NOT IN ('DOMAIN_PENDING', 'VERIFYING')
           OR NEW.operation_id IS NULL
        THEN
            RAISE EXCEPTION
                'connector installation inserts require DOMAIN_PENDING or VERIFYING'
                ' and an operation'
                USING ERRCODE = '23000';
        END IF;

        SELECT command_type, effective_org_id
          INTO operation_command, operation_org
          FROM app.operations
         WHERE id = NEW.operation_id;

        IF operation_command IS DISTINCT FROM 'connector.install.applied'
           OR operation_org IS NOT NULL
        THEN
            RAISE EXCEPTION
                'connector installation insert requires a platform install operation'
                USING ERRCODE = '23000';
        END IF;

        IF NEW.responsible_actor IS NULL
           OR NEW.responsible_actor NOT IN (
            'automated', 'platform_admin', 'platform_support'
        )
           OR NEW.last_verified_at IS NOT NULL
           -- The cause must MATCH the opening state. A DOMAIN_PENDING row says
           -- which gesture it waits on; a VERIFYING row waits on no gesture at
           -- all, and a cause there would name an obstacle that does not exist.
           OR (NEW.state = 'DOMAIN_PENDING'
               AND NEW.blocking_cause IS DISTINCT FROM 'domain_configuration_pending')
           OR (NEW.state = 'VERIFYING' AND NEW.blocking_cause IS NOT NULL)
        THEN
            RAISE EXCEPTION
                'connector installation insert metadata is not a safe classification'
                USING ERRCODE = '23000';
        END IF;

        RETURN NEW;
    END IF;

    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.environment IS DISTINCT FROM OLD.environment
       OR NEW.connector_name IS DISTINCT FROM OLD.connector_name
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
    THEN
        RAISE EXCEPTION 'connector_installations identity columns are immutable'
            USING ERRCODE = '23000';
    END IF;

    lifecycle_changed :=
        NEW.state IS DISTINCT FROM OLD.state
        OR NEW.blocking_cause IS DISTINCT FROM OLD.blocking_cause
        OR NEW.responsible_actor IS DISTINCT FROM OLD.responsible_actor
        OR NEW.last_verified_at IS DISTINCT FROM OLD.last_verified_at;

    IF lifecycle_changed THEN
        IF NEW.operation_id IS NULL
           OR NEW.operation_id IS NOT DISTINCT FROM OLD.operation_id
        THEN
            RAISE EXCEPTION
                'connector installation lifecycle changes require a fresh operation'
                USING ERRCODE = '23000';
        END IF;

        SELECT command_type, effective_org_id
          INTO operation_command, operation_org
          FROM app.operations
         WHERE id = NEW.operation_id;

        IF operation_command IS DISTINCT FROM 'connector.state.changed'
           OR operation_org IS NOT NULL
        THEN
            RAISE EXCEPTION
                'connector installation transition requires a platform state operation'
                USING ERRCODE = '23000';
        END IF;

        IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
            -- AI-206 adds NOT_INSTALLED -> VERIFYING: a state that can be
            -- inserted must also be reachable, or the graph contradicts the
            -- insert branch above.
            (OLD.state = 'NOT_INSTALLED' AND NEW.state IN ('DOMAIN_PENDING', 'VERIFYING', 'DISABLED'))
            OR (OLD.state = 'DOMAIN_PENDING' AND NEW.state IN ('VERIFYING', 'DISABLED'))
            OR (OLD.state = 'VERIFYING' AND NEW.state IN ('READY', 'DEGRADED', 'DISABLED'))
            OR (OLD.state = 'READY' AND NEW.state IN ('DEGRADED', 'DISABLED'))
            OR (OLD.state = 'DEGRADED' AND NEW.state IN ('READY', 'VERIFYING', 'DISABLED'))
        ) THEN
            RAISE EXCEPTION 'illegal connector installation state transition'
                USING ERRCODE = '23000';
        END IF;

        IF NEW.responsible_actor IS NULL
           OR NEW.responsible_actor NOT IN (
            'automated', 'platform_admin', 'platform_support'
        )
           OR (
               NEW.blocking_cause IS NOT NULL
               AND NEW.blocking_cause NOT IN (
                   'connector_disabled',
                   'dependency_unavailable',
                   'domain_configuration_pending',
                   'domain_route_misconfigured',
                   'installation_not_applied',
                   'synthetic_verification_failed',
                   'verification_failed'
               )
           )
           OR (NEW.state = 'READY' AND (
               NEW.blocking_cause IS NOT NULL OR NEW.last_verified_at IS NULL
           ))
           OR (NEW.state = 'DEGRADED' AND NEW.blocking_cause IS NULL)
        THEN
            RAISE EXCEPTION
                'connector installation transition metadata is inconsistent'
                USING ERRCODE = '23000';
        END IF;
    ELSIF NEW.operation_id IS DISTINCT FROM OLD.operation_id THEN
        RAISE EXCEPTION
            'connector installation operation cannot change without lifecycle evidence'
            USING ERRCODE = '23000';
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_connector_installation_protect
    ON app.connector_installations;
CREATE TRIGGER trg_connector_installation_protect
    BEFORE INSERT OR UPDATE OR DELETE ON app.connector_installations
    FOR EACH ROW EXECUTE FUNCTION app.protect_connector_installation();

COMMENT ON FUNCTION app.protect_connector_installation() IS
    'Story 38.2 corrective guard, widened by AI-206: immutable identity, legal '
    'six-state transitions, closed safe metadata, a fresh canonical platform '
    'operation for every lifecycle write, and TWO opening states -- '
    'DOMAIN_PENDING for a transport that owes a routing contract, VERIFYING for '
    'an installation that owes none.';

COMMIT;
