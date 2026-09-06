-- A pull Connector's contract hangs from its MODULE, not from an inbound installation.
-- AI-279.
--
-- WHAT WAS MEASURED. `app.connector_contract_versions` held 0 rows against 39
-- Connector modules on disk, in every deployment, since the table existed. The
-- Datastream wizard offers a Connector only when this table holds its contract,
-- so step 1 offered nothing and said so correctly: "No Connector contract is
-- persisted in this deployment".
--
-- WHY IT COULD NOT HOLD ONE. `installation_id` is NOT NULL against
-- `app.connector_installations`, whose trigger `app.protect_connector_installation`
-- admits an INSERT only in state `DOMAIN_PENDING`, under a
-- `connector.install.applied` platform operation, carrying
-- `blocking_cause = 'domain_configuration_pending'` -- then reaches READY only
-- through DOMAIN_PENDING -> VERIFYING -> READY, each transition under its own
-- operation. That lifecycle is an INBOUND CHANNEL's: a domain to configure, a
-- DNS route to verify. A `connector_pull` Connector has no domain, no webhook
-- and no DNS. The only way to give it an installation was to declare it blocked
-- on a domain configuration that will never exist -- fabricated evidence.
--
-- So the parent was wrong, not the writer. `connector_id` -- the module id --
-- is ALREADY on this table and is the true identity of a pull contract.
--
-- WHAT THIS CHANGES, AND WHAT IT DOES NOT. `installation_id` becomes nullable.
-- Every existing row keeps its installation and its constraints unchanged: the
-- two uniques below stay exactly as they were for installation-rooted rows,
-- because a UNIQUE over a NULL column does not constrain NULL rows, and the two
-- new partial uniques cover only the rows the old ones stopped covering. An
-- inbound connector still cannot skip its domain verification -- nothing here
-- touches `protect_connector_installation` or the verification tables.

ALTER TABLE app.connector_contract_versions
    ALTER COLUMN installation_id DROP NOT NULL;

-- A module-rooted contract is identified by (connector_id, fingerprint): the
-- same manifest recorded twice is the same contract, exactly as
-- `uq_connector_contract_fingerprint` says for an installation-rooted one.
CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_contract_fingerprint_by_module
    ON app.connector_contract_versions (connector_id, connector_fingerprint)
    WHERE installation_id IS NULL;

-- And its version numbers are a sequence per module, mirroring
-- `uq_connector_contract_version_number`.
CREATE UNIQUE INDEX IF NOT EXISTS uq_connector_contract_version_number_by_module
    ON app.connector_contract_versions (connector_id, version_number)
    WHERE installation_id IS NULL;

-- A row must hang from SOMETHING. Without this, dropping NOT NULL would allow a
-- contract that belongs to no installation and names no module -- an orphan the
-- wizard would read and no operator could trace.
ALTER TABLE app.connector_contract_versions
    ADD CONSTRAINT ck_connector_contract_versions_has_a_parent
    CHECK (installation_id IS NOT NULL OR connector_id IS NOT NULL);
