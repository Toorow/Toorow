-- Story 43.2: canonical application persons.
--
-- Identity is stable only on the inbound provider's (issuer, subject) pair.
-- A verified email is retained as a non-unique claim and MUST NOT be used to
-- merge persons or transfer access.

BEGIN;

CREATE SCHEMA IF NOT EXISTS app;

CREATE TABLE IF NOT EXISTS app.persons (
    id          TEXT        PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS app.person_identities (
    id                TEXT        PRIMARY KEY,
    person_id         TEXT        NOT NULL
                      REFERENCES app.persons(id) ON DELETE CASCADE,
    issuer            TEXT        NOT NULL
                      CHECK (issuer = BTRIM(issuer) AND length(issuer) BETWEEN 1 AND 2048),
    subject           TEXT        NOT NULL
                      CHECK (subject = BTRIM(subject) AND length(subject) BETWEEN 1 AND 2048),
    verified_email    TEXT
                      CHECK (
                          verified_email IS NULL OR (
                              verified_email = LOWER(BTRIM(verified_email))
                              AND length(verified_email) BETWEEN 3 AND 320
                              AND position('@' IN verified_email) > 1
                          )
                      ),
    verified_email_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (issuer, subject),
    CHECK ((verified_email IS NULL) = (verified_email_at IS NULL))
);

CREATE INDEX IF NOT EXISTS person_identities_person_id
    ON app.person_identities (person_id);

-- Deliberately non-unique: equal emails never imply equal persons.
CREATE INDEX IF NOT EXISTS person_identities_verified_email
    ON app.person_identities (verified_email)
    WHERE verified_email IS NOT NULL;

CREATE OR REPLACE FUNCTION app.protect_person_identity_binding()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.person_id IS DISTINCT FROM OLD.person_id
       OR NEW.issuer IS DISTINCT FROM OLD.issuer
       OR NEW.subject IS DISTINCT FROM OLD.subject
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'person identity binding is immutable';
    END IF;
    IF OLD.verified_email IS NOT NULL
       AND (
           NEW.verified_email IS DISTINCT FROM OLD.verified_email
           OR NEW.verified_email_at IS DISTINCT FROM OLD.verified_email_at
       ) THEN
        RAISE EXCEPTION 'verified email claim change requires explicit account review';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_person_identity_binding_immutable ON app.person_identities;
CREATE TRIGGER trg_person_identity_binding_immutable
    BEFORE UPDATE ON app.person_identities
    FOR EACH ROW EXECUTE FUNCTION app.protect_person_identity_binding();

DROP TRIGGER IF EXISTS trg_persons_updated_at ON app.persons;
CREATE TRIGGER trg_persons_updated_at
    BEFORE UPDATE ON app.persons
    FOR EACH ROW EXECUTE FUNCTION app.set_updated_at();

COMMIT;