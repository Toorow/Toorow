-- A Connector contract has NO installation, and no row exists until a binding.
-- AI-279, correcting migration 247.
--
-- WHAT 247 DID, AND WHY IT WAS THE WRONG HALF. It made
-- `connector_contract_versions.installation_id` NULLABLE so a pull Connector's
-- contract could stop hanging from an inbound `app.connector_installations`
-- row -- a lifecycle that is a domain to configure and a DNS route to verify,
-- which a pull Connector has neither of. But a wrong parent made optional is
-- still a wrong parent: it left two ways to identify the same object, a CHECK
-- to say a row must have one of them, and four uniques where two belong.
--
-- The identity of a contract is (connector_id, environment, version_number).
-- The installation is where an INBOUND CHANNEL is deployed, and an inbound
-- contract loses nothing here: `verification_run_id` still points at the run
-- that proved its domain, and that run names the installation.
--
-- WHAT WAS MEASURED, 2026-08-10, before this ran:
--   SELECT count(*) FILTER (WHERE installation_id IS NOT NULL) -> 0
-- No row has ever had this parent, in any deployment. Dropping the column
-- therefore removes a constraint, not data.
--
-- AND THE 39 ROWS GO. They were written on 2026-08-10 13:26:32, one per module
-- on disk, so that a screen reading this table would find something. Nothing
-- referenced them:
--   app.event_configuration_versions.connector_contract_version_id -> 0
--   app.datastream_setup_observations.connector_contract_version_ref -> 0
-- They were an answer pre-written for a question nobody had asked yet, and the
-- day a manifest changed they would have been stale with nothing to say so.
-- The catalogue now reads the module registry (`context_seed.registry_module_names`)
-- and a row appears when a Connector is actually BOUND, recording what the
-- module declared at that moment -- which is what makes a later manifest change
-- readable as `stale`.

-- The table is append-only by trigger, and deliberately so: a contract version
-- somebody's Datastream stands on must not be editable. Removing rows nothing
-- stands on is a one-off correction, and it is done with the guard visibly
-- lifted and restored rather than by weakening the guard.
ALTER TABLE app.connector_contract_versions
    DISABLE TRIGGER trg_connector_contract_versions_immutable;

DELETE FROM app.connector_contract_versions v
WHERE NOT EXISTS (
        SELECT 1 FROM app.event_configuration_versions e
        WHERE e.connector_contract_version_id = v.id
    )
  AND NOT EXISTS (
        SELECT 1 FROM app.datastream_setup_observations o
        WHERE o.connector_contract_version_ref = v.id
    );

ALTER TABLE app.connector_contract_versions
    ENABLE TRIGGER trg_connector_contract_versions_immutable;

-- The parent goes, and with it everything 247 added to work around it.
ALTER TABLE app.connector_contract_versions
    DROP CONSTRAINT IF EXISTS ck_connector_contract_versions_has_a_parent;
DROP INDEX IF EXISTS app.uq_connector_contract_fingerprint_by_module;
DROP INDEX IF EXISTS app.uq_connector_contract_version_number_by_module;

ALTER TABLE app.connector_contract_versions
    DROP CONSTRAINT IF EXISTS uq_connector_contract_version_number;
ALTER TABLE app.connector_contract_versions
    DROP CONSTRAINT IF EXISTS uq_connector_contract_fingerprint;

ALTER TABLE app.connector_contract_versions
    DROP COLUMN IF EXISTS installation_id;

-- ONE identity, stated once. Same two rules 133 wrote against the installation:
-- version numbers are a sequence, and the same manifest recorded twice is the
-- same contract -- now keyed on the module and the deployment it was read in.
ALTER TABLE app.connector_contract_versions
    ADD CONSTRAINT uq_connector_contract_version_number
    UNIQUE (connector_id, environment, version_number);

ALTER TABLE app.connector_contract_versions
    ADD CONSTRAINT uq_connector_contract_fingerprint
    UNIQUE (connector_id, environment, connector_fingerprint);
