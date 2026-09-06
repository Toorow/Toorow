-- A retracted daily insight is frozen WHOLE, and no row is born retracted.
--
-- WHY A SECOND MIGRATION. The adversarial review of ae60c22a (2026-08-30) proved
-- two gaps in 321's guard, both unreachable through the product write path and
-- both real at the SQL layer:
--
--   (1) BORN RETRACTED. 321 guards UPDATE only, so an INSERT already carrying
--       the retraction triple would file a withdrawal nobody performed -- an
--       audit-free retraction, forged at birth. `record_run`'s INSERT never
--       names these columns; this makes that a constraint instead of a habit.
--
--   (2) PROVENANCE SWAP. 321 froze `payload`/`payload_hash` under a filed
--       retraction, but an idempotent re-upsert with an IDENTICAL payload
--       passed the trigger and could rewrite `run_id`, `identity`, `result_id`
--       -- a withdrawn claim silently re-attributed. The honest rule is total:
--       after the retraction is filed, the row IS the record; nothing on it
--       changes, ever. (`NEW IS DISTINCT FROM OLD` compares every column, so
--       a column added later is frozen with the rest by construction.)
--
-- The RGPD erasure path DELETEs (CASCADE from app.projects) and this trigger
-- carries no DELETE bit, so org erasure is untouched -- the same shape 321
-- deliberately chose, pinned by test_the_erasure_hatch_is_a_privilege_too.
--
-- 321 is APPLIED and is not re-edited (the checksum ledger forbids it); this
-- migration REPLACES the function and re-creates the trigger, which is the
-- ratified repair path: fix by the next one.

BEGIN;

CREATE OR REPLACE FUNCTION app.freeze_daily_insight_retraction()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        -- The write that FILES a retraction is an UPDATE on a standing row,
        -- audited beside it. A row born retracted is a forged withdrawal.
        IF NEW.retracted_at IS NOT NULL
           OR NEW.retracted_by IS NOT NULL
           OR NEW.retracted_reason IS NOT NULL THEN
            RAISE EXCEPTION
                'a daily insight is never born retracted: publish the claim, then '
                'retract it through the retraction door so the withdrawal is audited'
                USING ERRCODE = '23514';
        END IF;
        RETURN NEW;
    END IF;

    IF OLD.retracted_at IS NOT NULL THEN
        -- After the withdrawal is filed, the whole row is the record of it:
        -- the claim it judged, the run and identity it was published under,
        -- the lineage it cited. Nothing on it changes, ever.
        IF NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION
                'insight % was retracted at % by %: a withdrawn row is frozen whole. '
                'Publish the new reading on a free slot of the same day',
                OLD.id, OLD.retracted_at, OLD.retracted_by
                USING ERRCODE = '23514';
        END IF;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_daily_insights_freeze_retraction ON app.daily_insights;
CREATE TRIGGER trg_daily_insights_freeze_retraction
    BEFORE INSERT OR UPDATE ON app.daily_insights
    FOR EACH ROW EXECUTE FUNCTION app.freeze_daily_insight_retraction();

COMMIT;
