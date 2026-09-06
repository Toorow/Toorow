-- 351 -- the « only while recording » trigger reads the path under FOR KEY SHARE.
--
-- Migration 150's BEFORE INSERT trigger read `lifecycle` with a plain SELECT: an
-- MVCC snapshot, no lock. `ai_paths.finalize_path` takes `SELECT ... FOR UPDATE`
-- on the path, but nothing in the inserter waited for it, so this interleaving
-- stayed open (Opus review, round 3): the finalizer locks and hashes; a step's
-- trigger reads `recording` from its snapshot and passes; the finalizer commits
-- `finalized`; the step's AFTER-row FK check waits, then only verifies the key
-- still exists -- and a step lands on a finalized path whose content hash does
-- not describe it. FOR KEY SHARE conflicts with FOR UPDATE (and with nothing
-- weaker): the trigger now WAITS for a finalizer holding the path, re-reads the
-- committed lifecycle, and refuses; and a finalizer arriving second waits for the
-- appending transaction, then hashes the step it just let in. 150 is applied and
-- immutable; the function body is replaced here, by the next migration.
CREATE OR REPLACE FUNCTION app.reject_step_on_finalized_path()
RETURNS TRIGGER AS $$
DECLARE
    path_lifecycle TEXT;
BEGIN
    SELECT lifecycle INTO path_lifecycle FROM app.ai_paths WHERE id = NEW.path_id FOR KEY SHARE;
    IF path_lifecycle IS DISTINCT FROM 'recording' THEN
        RAISE EXCEPTION 'cannot append a step to a finalized AI Path'
            USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
