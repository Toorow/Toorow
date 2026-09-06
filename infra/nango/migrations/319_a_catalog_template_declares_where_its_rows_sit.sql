-- 319 -- a catalog Template declares WHERE its rows sit in the matrix.
--
-- THE RULE, RATIFIED (`docs/product-architecture/file-source-ingestion.md`
-- § Placement): "The class is declared per Template, never guessed per row,
-- and an import with no declared class is refused before landing." The code
-- enforces it in `file_source_producer.stamp_placement` (`no_placement_class`,
-- AD-6), and a client-saved Template cannot be created without one
-- (`file_source_template.py:227`).
--
-- THE DEFECT: migration 088 seeded the six catalog Templates WITHOUT a
-- `class`. Measured 2026-08-29 on production (`SELECT contract->>'class' FROM
-- app.import_templates` -> NULL x6) and on the deployed QA walk (G9 T07: the
-- template confirm recomputed a full preview and refused it,
-- `reason: no_placement_class`). So no Datastream bound to a catalog Template
-- has ever been able to pass the landing gate: the catalog offered contracts
-- the gate refuses by construction.
--
-- THE REPAIR IS A NEW VERSION, NOT AN EDIT. `app.protect_import_template`
-- (migration 088) refuses UPDATE and DELETE: "add a new version row to
-- release a change". So each of the five OFFLINE Templates gains VERSION 2,
-- its contract being version 1's plus `class: actual` -- they carry MEASURED
-- spend after the fact (`net_cost_eur`, `impressions_count`, `media_date`).
-- Readers that resolve a catalog code take its latest version
-- (`import_templates.latest_version`, the wizard's binding); a Datastream that
-- pinned version 1 explicitly keeps a contract the gate refuses, exactly as it
-- did before -- nothing it could land changes.
--
-- `GENERIC_TABULAR_V1` is deliberately left without a class: a generic tabular
-- file has no place in the matrix until a Template says which, and the doc's
-- sentence is the refusal it should keep receiving.
--
-- REPLAYABLE. `ON CONFLICT (template_code, version) DO NOTHING`.

BEGIN;

INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
SELECT template_code,
       2,
       title,
       contract || '{"class": "actual"}'::jsonb,
       is_generic
  FROM app.import_templates
 WHERE version = 1
   AND template_code IN (
        'OFFLINE_OOH_V1', 'OFFLINE_DOOH_V1', 'OFFLINE_PRESS_V1',
        'OFFLINE_RADIO_V1', 'OFFLINE_TV_V1'
       )
ON CONFLICT (template_code, version) DO NOTHING;

COMMIT;
