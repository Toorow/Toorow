-- 303 -- A media plan names the Datastream that carries it.
--
-- RATIFIED 2026-08-24 (commit a379ec50), `file-source-ingestion.md`, amendment
-- "a project carries one or several media plans, each on its own carrier
-- Datastream": a project may hold ONE OR SEVERAL plans; each lives on its OWN
-- carrier Datastream, created with the template profile particular to that plan;
-- provisioning is the existing file-source Datastream creation gesture, never an
-- auto-provisioning on first import; and a revision is a DATED import into the
-- same carrier.
--
-- WHY THE RELATION IS STORED AND NOT DERIVED. `import_runner` stamps a landed
-- plan version with the landing relation `plan_store:<plan id>`, so after the
-- first import the pair (plan, datastream) IS readable from
-- `app.managed_feed_import_ledger`. It is stored all the same, for two reasons a
-- derivation cannot serve:
--
--   * a plan is CREATED before anything is imported into it. The decision puts
--     that creation in the carrier's Workbench, so the carrier is known at the
--     moment of creation and losing it until the first file lands would make the
--     screen unable to say which plan it hosts;
--   * the carrier is a CHOICE a person made, not a trace of what happened. A
--     derived link would silently name whichever Datastream happened to import
--     last, which is exactly the auto-provisioning the decision forbids.
--
-- WHAT IT DOES NOT DO. It adds no plan and provisions nothing: the column is
-- NULLABLE, and every plan written before today keeps a NULL carrier and stays
-- readable exactly as it was. A plan with no carrier is not broken -- it is a
-- plan whose file arrived by the older path, and the console says so rather than
-- inventing a Datastream for it.
--
-- THE UNIQUENESS IS PER CARRIER, NEVER PER PROJECT. "Each plan on its own
-- carrier" means one live plan per Datastream; it does NOT mean one plan per
-- project. A partial unique index on the carrier alone is the exact shape of
-- that sentence: a second plan in the same project is refused only if it tries
-- to sit on the SAME carrier, which is the case the decision names as two
-- agencies' spreadsheets forced through one template. `archived_at IS NULL`
-- keeps an archived plan from holding a carrier hostage, mirroring
-- `uq_media_plans_project_name` (migration 040).

ALTER TABLE app.media_plans
    ADD COLUMN IF NOT EXISTS carrier_datastream_id TEXT;

-- The composite reference is what makes the carrier belong to the plan's OWN
-- project: a FK on `id` alone would admit a Datastream of another project, and
-- the console would then offer a door across a scope boundary. `(id, project_id)`
-- is already unique on `app.datastreams` (migration 030).
ALTER TABLE app.media_plans
    DROP CONSTRAINT IF EXISTS fk_media_plans_carrier_datastream;
ALTER TABLE app.media_plans
    ADD CONSTRAINT fk_media_plans_carrier_datastream
    FOREIGN KEY (carrier_datastream_id, project_id)
    REFERENCES app.datastreams (id, project_id) ON DELETE RESTRICT;

CREATE UNIQUE INDEX IF NOT EXISTS uq_media_plans_carrier_datastream
    ON app.media_plans (carrier_datastream_id)
    WHERE carrier_datastream_id IS NOT NULL AND archived_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_media_plans_carrier_datastream
    ON app.media_plans (carrier_datastream_id);

COMMENT ON COLUMN app.media_plans.carrier_datastream_id IS
    'Ratified 2026-08-24 (file-source-ingestion.md): the file-source Datastream this plan is carried by. Its Workbench is where the plan is created and where each dated revision is imported. NULL for a plan that predates the decision or arrived through the older import path.';
