-- ============================================================================
-- 313 — the dataset CHECK refuses every slate token, in every position
-- ============================================================================
--
-- Story 62.3 promised the slate refusal "in code AND in the CHECK". Measured
-- 2026-08-25: the 308 CHECK caught `sandbox`/`scratch` as substrings and `tmp`/
-- `temp` only BETWEEN underscores, and knew nothing of `wip`/`throwaway`. So
-- `marts_proj_tmp`, `marts_proj_wip` and `marts_proj_throwaway` -- a slate token
-- in the FINAL position -- passed the constraint while
-- `warehouse_tenancy.slate_dataset_reason` refused them. The code was the net;
-- the CHECK had holes.
--
-- 308 is applied and checksum-frozen, so its constraint cannot be edited in
-- place. This drops and re-adds `dataset_access_grants_dataset_ck` with the slate
-- tokens matched as WHOLE tokens anywhere -- `(^|_)(token)(_|$)` -- the same
-- word list `_SLATE_TOKENS` carries: sandbox, scratch, tmp, temp, throwaway, wip.
-- The exact-name and raw/mirror/staging clauses of 308 are preserved verbatim;
-- only the slate half widens.
--
-- Schema-Change-Checklist: additive and idempotent (drop-if-exists then add).
-- No migration below this number is edited. This is migration 313.
-- ============================================================================

BEGIN;

ALTER TABLE app.dataset_access_grants
    DROP CONSTRAINT IF EXISTS dataset_access_grants_dataset_ck;

ALTER TABLE app.dataset_access_grants
    ADD CONSTRAINT dataset_access_grants_dataset_ck CHECK (
        dataset_id IS NULL
        OR (
            (
                (project_id IS NULL AND dataset_id LIKE 'org\_%\_marts' ESCAPE '\')
                OR (project_id IS NOT NULL AND dataset_id = 'marts_' || project_id)
            )
            AND dataset_id NOT LIKE '%\_raw%' ESCAPE '\'
            AND dataset_id NOT LIKE 'mirror\_%' ESCAPE '\'
            AND dataset_id NOT LIKE '%staging%'
            -- Every slate token, as a WHOLE token, in any position -- the word
            -- list of `warehouse_tenancy._SLATE_TOKENS`. `(^|_)…(_|$)` is what
            -- makes `marts_proj_tmp` (token at the end) refuse like
            -- `marts_tmp_proj` (token in the middle) already did.
            AND lower(dataset_id) !~ '(^|_)(sandbox|scratch|tmp|temp|throwaway|wip)(_|$)'
            AND dataset_id !~ '_(19|20)[0-9]{2}([_-]?(0[1-9]|1[0-2])([_-]?(0[1-9]|[12][0-9]|3[01]))?)$'
        )
    );

COMMIT;
