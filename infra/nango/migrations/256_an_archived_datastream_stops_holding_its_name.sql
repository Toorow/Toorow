-- An archived Datastream kept its name reserved forever.
--
-- `uq_datastreams_project_name` spans every row, archived ones included. A
-- Datastream that carries history cannot be deleted -- it is archived, which is
-- correct -- so its name stayed taken and the same feed could never be built
-- again under the name it deserves. Measured 2026-08-12: after archiving a
-- failed attempt, rebuilding the same report answered 409 `name_taken`, which
-- pushes anyone into "the same thing v2, v3, v4" instead of a clean rebuild.
--
-- Uniqueness is what the operator sees, and they do not see archived rows. It
-- becomes partial. Two archived rows may share a name -- they are history, and
-- history repeats.

ALTER TABLE app.datastreams DROP CONSTRAINT IF EXISTS uq_datastreams_project_name;

CREATE UNIQUE INDEX IF NOT EXISTS uq_datastreams_project_name_live
    ON app.datastreams (project_id, name)
    WHERE archived_at IS NULL;
