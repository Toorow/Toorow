-- Story 59.6: an alert names the destination it leaves by.
--
-- THE MEASURED GAP. What is PERSISTED is never SENT, and what is SENT is never
-- PERSISTED. `app.alert_firings` holds every finding of the eight DQ monitors,
-- of the business, anomaly and mediaplan evaluators -- and nothing reads it but a
-- SELECT. The only object that ever reaches a notification channel is one of the
-- four `evaluate_alerts` signals (`infra_alerts.py:208`), which are computed in
-- memory, written nowhere, and carry no `project_id` at all.
--
-- TWO TABLES, AND WHY NEITHER IS A COLUMN.
--
--   * `app.alert_destinations` -- a LIST of destinations per project. Migration
--     044 extended `app.project_preferences` by COLUMNS (one column, one value),
--     which cannot hold a list with a constraint, an index or a per-row secret.
--   * `app.alert_deliveries`   -- the record that an alert was attempted, and how
--     it ended. Without it "an alert lost in silence" is not measurable: today
--     `notify_alert:585` swallows a channel failure into a `logger.warning` and
--     nothing survives the process.
--
-- THE PATTERN OF THE SECOND TABLE IS `app.operation_outbox` (`060:73-90`) --
-- `pending | processing | delivered`, `attempts`, `delivered_at`,
-- `last_error_class` -- because that shape is already drained by a scheduler
-- endpoint and already proven. WHAT IS DELIBERATELY NOT COPIED is its
-- `REFERENCES app.operations(id) ON DELETE RESTRICT`: a firing is not an
-- operation. Two consequences, both intended:
--
--   * `firing_id` carries NO foreign key. A delivery is the record of what left
--     the process; a RESTRICT would make a firing undeletable by the trace of its
--     own delivery, and a CASCADE would erase the evidence that it was sent. The
--     row keeps the id it was told, and a firing that is pruned later leaves its
--     delivery standing -- which is what evidence means.
--   * `destination_id` DOES cascade: a destination that no longer exists cannot
--     have a pending delivery to it, and its history is about a target the
--     project deliberately removed.
--
-- THE SECRET LIVES IN THE ROW, sealed by the project's tenant key, in the exact
-- shape `app.connection_ref` seals a Google token (`google_token_store.py`,
-- Fernet over `tenant_keys.get_or_create_key(project_id)`). A deployment-wide
-- `SMTP_PASSWORD` in Secret Manager cannot be per-project, and this table is
-- per-project by construction. An `email` destination carries NO secret at all --
-- it carries an address; only a webhook carries a key. That is a CHECK below,
-- not a convention.
--
-- WHAT THIS MIGRATION DOES NOT DECIDE. Which types a destination may name is
-- application vocabulary (`dq_monitor_registry.FIRING_ALERT_TYPES`), refused at
-- the route as `unknown_alert_type` and deliberately NOT frozen into a CHECK
-- here: a monitor added to the registry tomorrow would otherwise need a
-- migration to become routable.

BEGIN;

-- ---------------------------------------------------------------------------
-- Where an alert goes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.alert_destinations (
    id              TEXT        PRIMARY KEY,          -- prefixed ULID: 'adest_'
    -- THE FOREIGN KEY IS NOT DECORATION HERE. `core.org_purge` walks the FK
    -- GRAPH to dismantle a tenant (`plan_purge`, `_load_graph`), so a
    -- project-scoped table that names no parent is INVISIBLE to an RGPD org
    -- erasure -- and this one holds a sealed webhook key. `ON DELETE RESTRICT`
    -- is the family's rule, shared with `app.alert_definitions` and
    -- `app.alert_firings` (`018_projects.sql:143-151`).
    project_id      TEXT        NOT NULL
                    REFERENCES app.projects(id) ON DELETE RESTRICT,
    kind            TEXT        NOT NULL
                    CHECK (kind IN ('email', 'slack_webhook', 'webhook')),
    label           TEXT        NOT NULL CHECK (length(btrim(label)) BETWEEN 1 AND 160),
    -- The address or the URL. Rendered MASKED by every read route: a Slack
    -- incoming-webhook URL is itself a credential, so nothing here is echoed
    -- back verbatim.
    target          TEXT        NOT NULL CHECK (length(btrim(target)) BETWEEN 1 AND 2048),
    -- Fernet ciphertext, sealed by the project's tenant key. NULL for e-mail.
    secret_blob     BYTEA,
    -- The routing rule. EMPTY means "everything": a destination without a rule
    -- receives every persisted firing of its project, which is the only way the
    -- non-DQ emitters (business, anomaly, mediaplan) reach a destination at all.
    -- Routing is on `alert_firings.type`, never on `severity`: eight DQ monitors
    -- out of eight write `severity='warning'`.
    alert_types     TEXT[]      NOT NULL DEFAULT '{}',
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_by      TEXT        NOT NULL DEFAULT 'system',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- An e-mail destination carries an address and nothing else (story 59.6,
    -- arbitrage 9). This is what keeps the undeployed SMTP transport from
    -- becoming a reason to store a password per project.
    CONSTRAINT alert_destinations_email_has_no_secret
        CHECK (kind <> 'email' OR secret_blob IS NULL),
    -- Two destinations of the same project cannot answer to the same name: the
    -- confirmation of a deletion names the label, and two identical labels make
    -- that sentence a lie.
    CONSTRAINT uq_alert_destinations_label UNIQUE (project_id, label)
);

CREATE INDEX IF NOT EXISTS alert_destinations_project
    ON app.alert_destinations (project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS alert_destinations_enabled
    ON app.alert_destinations (project_id)
    WHERE enabled;

-- ---------------------------------------------------------------------------
-- That it went, or why it did not.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.alert_deliveries (
    id                  TEXT        PRIMARY KEY,      -- prefixed ULID: 'adlv_'
    destination_id      TEXT        NOT NULL
                        REFERENCES app.alert_destinations(id) ON DELETE CASCADE,
    -- `app.alert_firings.id`, WITHOUT a foreign key -- see the header.
    firing_id           TEXT        NOT NULL,
    alert_type          TEXT        NOT NULL,
    state               TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (state IN ('pending', 'processing', 'delivered')),
    attempts            INTEGER     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    delivered_at        TIMESTAMPTZ,
    last_error_class    TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- One firing reaches one destination once. The nightly step runs again every
    -- night over a lookback window, and without this a week of nights would send
    -- the same finding seven times -- the shape `UNIQUE (operation_id, event_type)`
    -- protects the outbox from.
    CONSTRAINT uq_alert_deliveries_pair UNIQUE (destination_id, firing_id)
);

CREATE INDEX IF NOT EXISTS alert_deliveries_pending
    ON app.alert_deliveries (created_at)
    WHERE state <> 'delivered';

-- What the screen reads to answer "when did this destination last receive
-- something", which is the ONLY honest answer to the plan's "ce que chacun
-- reçoit": before this table there was no such date, and the surface was
-- specified to say so rather than print a zero.
CREATE INDEX IF NOT EXISTS alert_deliveries_destination
    ON app.alert_deliveries (destination_id, delivered_at DESC);

COMMIT;
