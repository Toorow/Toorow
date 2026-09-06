-- 279 -- a rate a person POSED is a method of its own, not a borrowed `direct`
--
-- RATIFIED 2026-08-17 (amendment at the end of
-- docs/product-architecture/alignment-register.md): "a fixed FX value is a
-- first-class method, not a stopgap". A user must be able to set the conversion
-- rate as a VALUE -- a fixed rate for a currency pair over a period -- and that
-- is a governed, visible method with its OWN label. It is explicitly not to be
-- disguised as `fx_method='direct'`.
--
-- WHY THE CHECK HAD TO MOVE. Migration 144 wrote the four methods of an
-- INGESTED quotation:
--
--     CHECK (method IN ('direct', 'triangulated', 'carry_forward', 'identity'))
--
-- Those four describe how an OBSERVATION was derived: quoted directly, derived
-- through a pivot, carried from an earlier day, or the same-currency identity.
-- A rate a human types has none of those provenances -- it was not observed at
-- all -- so under this constraint the only way to store one was to call it
-- `direct`, which is exactly what the warehouse half was doing to the dev seed
-- (`dbt/macros/money_evidence.sql`, repaired in the same change). A vocabulary
-- that forces a lie is the defect, not the writer.
--
-- WHAT `fixed` MEANS, PRECISELY. The rate was declared by an identified actor
-- for a pair and a period; no provider was read and none is claimed. Its
-- provenance is the version row above it (`app.fx_rate_set_versions`: `method`,
-- which already admits `manual_entry`, plus `created_by` and `retrieved_at`),
-- and `derivation` on the observation carries whatever condition the actor
-- attached. A reader can therefore always tell a posed number from a measured
-- one, which is the whole point of the amendment.
--
-- NO ROW IS RELABELLED, AND THAT IS MEASURED, NOT ASSUMED.
-- `app.fx_rate_observations` holds zero rows: the ingestion engine
-- (`core.fx_rate_sets.ingest_rate_batch`) has never had a caller, which the audit
-- of 2026-08-17 states and `SELECT count(*)` on the rebuilt disposable base
-- confirms (0). The mislabelled `direct` lives in the WAREHOUSE, on a dbt seed,
-- and a SQL migration cannot reach it -- it is corrected at its source, in
-- `dbt/seeds/fx_rates.csv`, which now carries its own `fx_method` column.
--
-- WIDENING ONLY. Every value that was legal before is legal after: no existing
-- row can violate the new constraint, so it is validated immediately rather than
-- added NOT VALID. The identity guard is untouched and still holds -- `fixed` is
-- not exempt from it, and it does not need to be: it only constrains `identity`.

BEGIN;

ALTER TABLE app.fx_rate_observations
    DROP CONSTRAINT IF EXISTS fx_rate_observations_method_check;

ALTER TABLE app.fx_rate_observations
    ADD CONSTRAINT fx_rate_observations_method_check
    CHECK (method IN ('fixed', 'direct', 'triangulated', 'carry_forward', 'identity'));

COMMENT ON COLUMN app.fx_rate_observations.method IS
    'How this rate was obtained. `fixed` was POSED by an identified actor for a '
    'pair and a period (governed, first-class, ratified 2026-08-17) -- no provider '
    'was read. The other four describe an ingested quotation: `direct` observed, '
    '`triangulated` through a pivot, `carry_forward` carried from an earlier day, '
    '`identity` the same-currency evidence. `identity` is evidence, never a '
    'COALESCE default.';

COMMIT;
