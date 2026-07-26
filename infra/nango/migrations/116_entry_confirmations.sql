-- Epic 43 review patch: server-issued, payload-bound first-scope confirmations.
BEGIN;

CREATE TABLE IF NOT EXISTS app.entry_confirmations (
    id                       TEXT PRIMARY KEY,
    command_type             TEXT NOT NULL
                              CHECK (command_type IN (
                                  'hosted.entry_scope.create',
                                  'instance.claim'
                              )),
    actor_person_id          TEXT NOT NULL
                              REFERENCES app.persons(id) ON DELETE RESTRICT,
    payload_hash             TEXT NOT NULL CHECK (payload_hash ~ '^[0-9a-f]{64}$'),
    idempotency_key_hash     TEXT NOT NULL
                              CHECK (idempotency_key_hash ~ '^[0-9a-f]{64}$'),
    context_reference_hash   TEXT NOT NULL
                              CHECK (context_reference_hash ~ '^[0-9a-f]{64}$'),
    confirmation_secret_hash TEXT NOT NULL UNIQUE
                              CHECK (confirmation_secret_hash ~ '^[0-9a-f]{64}$'),
    expires_at               TIMESTAMPTZ NOT NULL,
    consumed_at              TIMESTAMPTZ,
    operation_id             TEXT
                              REFERENCES app.operations(id) ON DELETE RESTRICT,
    created_at               TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (expires_at > created_at),
    CHECK (operation_id IS NULL OR consumed_at IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS entry_confirmations_pending
    ON app.entry_confirmations (actor_person_id, command_type, expires_at)
    WHERE consumed_at IS NULL;

CREATE OR REPLACE FUNCTION app.protect_entry_confirmation_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.command_type IS DISTINCT FROM OLD.command_type
       OR NEW.actor_person_id IS DISTINCT FROM OLD.actor_person_id
       OR NEW.payload_hash IS DISTINCT FROM OLD.payload_hash
       OR NEW.idempotency_key_hash IS DISTINCT FROM OLD.idempotency_key_hash
       OR NEW.context_reference_hash IS DISTINCT FROM OLD.context_reference_hash
       OR NEW.confirmation_secret_hash IS DISTINCT FROM OLD.confirmation_secret_hash
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR (OLD.consumed_at IS NOT NULL AND NEW.consumed_at IS DISTINCT FROM OLD.consumed_at)
       OR (OLD.operation_id IS NOT NULL AND NEW.operation_id IS DISTINCT FROM OLD.operation_id)
    THEN
        RAISE EXCEPTION 'entry confirmation binding is immutable';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_entry_confirmation_binding
    ON app.entry_confirmations;
CREATE TRIGGER trg_entry_confirmation_binding
    BEFORE UPDATE ON app.entry_confirmations
    FOR EACH ROW EXECUTE FUNCTION app.protect_entry_confirmation_binding();

COMMIT;
