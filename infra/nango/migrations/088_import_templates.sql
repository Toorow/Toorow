-- infra/nango/migrations/088_import_templates.sql
--
-- Story 38.6: Create an Inbound Datastream from a Versioned Template Contract.
--
-- Introduces `app.import_templates` -- the reference catalog of versioned,
-- immutable file-contract templates used when creating managed-feed Datastreams
-- via the inbound connector.
--
-- INVARIANTS:
--   AD-2  : zero provider/vendor vocabulary. Templates are identified only by
--            a stable template_code string. No brand name, adapter name, or
--            provider-specific vocabulary enters this table.
--   AC3   : template versions are IMMUTABLE. The `protect_import_template`
--            trigger blocks UPDATE and DELETE. A later version is a new row
--            (template_code, version+1) -- never an edit of an existing row.
--   AC4   : GENERIC_TABULAR_V1 (`is_generic = TRUE`) is the discover-first
--            fallback. Datastreams created against it are DRAFT (enabled=FALSE,
--            config.publishable=FALSE) until required canonical semantics are
--            confirmed in a later mapping story.
--   AC5   : this table is REFERENCE DATA only -- it is NOT a per-datastream
--            ledger. Per-datastream binding (template_code, template_version,
--            channels, publishable) lives in `app.datastreams.config` via the
--            Epic 12 create_datastream seam. No second datastream table exists.
--   ADDITIVE / IDEMPOTENT / REPLAYABLE: every statement uses IF NOT EXISTS /
--            OR REPLACE / DROP TRIGGER IF EXISTS. Re-application is a no-op.
--   SEED  : the six ratified v1 codes are seeded idempotently (ON CONFLICT DO
--            NOTHING). Re-applying this migration never overwrites existing rows.
--
-- Apply order: after 087 (highest on disk).
--
-- Apply with:
--   docker compose -f infra/nango/docker-compose.yml exec platform-db \
--     psql -U connector -d connector \
--     -f /migrations/088_import_templates.sql

BEGIN;

-- ---------------------------------------------------------------------------
-- app.import_templates -- reference catalog of versioned template contracts.
--
-- `template_code` : stable template identifier string (e.g. 'OFFLINE_TV_V1').
-- `version`       : integer version; starts at 1; bumped as new rows only.
-- `title`         : human-readable label (display only; not a business key).
-- `contract`      : JSONB contract (required_fields, optional_fields, aliases,
--                   field_types, grain, identity_keys, date_timezone,
--                   currency_unit, sensitive_classification, validation,
--                   canonical_bindings).
-- `is_generic`    : TRUE for the discover-first generic fallback. Datastreams
--                   created against a generic template are DRAFT and cannot
--                   publish until required canonical semantics are confirmed.
-- `created_at`    : immutable row-creation timestamp.
--
-- PRIMARY KEY (template_code, version): enforces one immutable row per version.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.import_templates (
    template_code  TEXT        NOT NULL,
    version        INT         NOT NULL DEFAULT 1,
    title          TEXT        NOT NULL,
    contract       JSONB       NOT NULL,
    is_generic     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT pk_import_templates
        PRIMARY KEY (template_code, version),
    CONSTRAINT ck_import_templates_code
        CHECK (template_code ~ '^[A-Z][A-Z0-9_]{1,63}$'),
    CONSTRAINT ck_import_templates_version
        CHECK (version >= 1)
);

-- Index: fast lookup by template_code (most common read path).
CREATE INDEX IF NOT EXISTS idx_import_templates_code
    ON app.import_templates (template_code);

-- ---------------------------------------------------------------------------
-- Immutability trigger: `protect_import_template`.
--
-- Blocks UPDATE and DELETE unconditionally. A later version of a template is
-- a new row; existing rows are frozen forever (AC3).
-- Mirrors the protect_connector_installation trigger style (migration 082).
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION app.protect_import_template()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'DELETE') THEN
        RAISE EXCEPTION
            'import_templates rows may not be deleted -- add a new version row instead'
            USING ERRCODE = '23000';
        RETURN OLD;
    END IF;
    IF (TG_OP = 'UPDATE') THEN
        RAISE EXCEPTION
            'import_templates rows are immutable -- add a new version row to release a change'
            USING ERRCODE = '23000';
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_import_template_protect
    ON app.import_templates;
CREATE TRIGGER trg_import_template_protect
    BEFORE UPDATE OR DELETE ON app.import_templates
    FOR EACH ROW EXECUTE FUNCTION app.protect_import_template();

-- ---------------------------------------------------------------------------
-- Seed: 6 ratified v1 template codes.
--
-- All inserts are idempotent (ON CONFLICT DO NOTHING). Re-applying this
-- migration is safe; existing rows are never updated (trigger + PK enforce it).
--
-- Shared required fields (all non-generic templates):
--   datastream_id, campaign_id_toorow, media_type, channel_or_network,
--   event_start_timestamp, media_date, net_cost_eur.
--
-- Shared optional fields:
--   sub_channel_or_face, event_end_timestamp, duration_seconds,
--   target_audience_code, grp_value, impressions_count, gross_cost_eur,
--   creative_id, geo_scope, raw_metadata.
-- ---------------------------------------------------------------------------

-- OFFLINE_TV_V1 --
INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
VALUES (
    'OFFLINE_TV_V1',
    1,
    'Offline TV broadcast spend',
    jsonb_build_object(
        'required_fields',  ARRAY['datastream_id','campaign_id_toorow','media_type',
                                  'channel_or_network','event_start_timestamp',
                                  'media_date','net_cost_eur'],
        'optional_fields',  ARRAY['duration_seconds','target_audience_code','grp_value',
                                  'impressions_count','gross_cost_eur','creative_id',
                                  'geo_scope','sub_channel_or_face',
                                  'event_end_timestamp','raw_metadata'],
        'aliases',          jsonb_build_object(
                                'campaign_id_toorow',  ARRAY['campaign_id','campaign','id_campagne'],
                                'channel_or_network',  ARRAY['station','network','channel']
                            ),
        'field_types',      jsonb_build_object(
                                'net_cost_eur',          'numeric',
                                'gross_cost_eur',        'numeric',
                                'grp_value',             'numeric',
                                'impressions_count',     'integer',
                                'duration_seconds',      'integer',
                                'event_start_timestamp', 'timestamp',
                                'media_date',            'date'
                            ),
        'grain',            'broadcast_spot',
        'identity_keys',    ARRAY['event_start_timestamp','channel_or_network',
                                  'creative_id','duration_seconds'],
        'date_timezone',    jsonb_build_object(
                                'media_date_cutoff_local_hour', 3,
                                'rule', 'media_date derived from event_start_timestamp at 03:00 local'
                            ),
        'currency_unit',    jsonb_build_object('cost_currency', 'EUR', 'unit', 'monetary'),
        'sensitive_classification', 'none',
        'validation',       jsonb_build_object(
                                'required_fields_must_be_present', true,
                                'missing_required_creates', 'PENDING_USER_MAPPING'
                            ),
        'canonical_bindings', jsonb_build_object(
                                'media_type_value', 'TV'
                              )
    ),
    FALSE
)
ON CONFLICT (template_code, version) DO NOTHING;

-- OFFLINE_OOH_V1 --
INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
VALUES (
    'OFFLINE_OOH_V1',
    1,
    'Offline out-of-home (OOH) panel spend',
    jsonb_build_object(
        'required_fields',  ARRAY['datastream_id','campaign_id_toorow','media_type',
                                  'channel_or_network','sub_channel_or_face',
                                  'event_start_timestamp','event_end_timestamp',
                                  'media_date','net_cost_eur'],
        'optional_fields',  ARRAY['impressions_count','geo_scope','creative_id',
                                  'gross_cost_eur','raw_metadata',
                                  'duration_seconds','target_audience_code',
                                  'grp_value'],
        'aliases',          jsonb_build_object(
                                'campaign_id_toorow', ARRAY['campaign_id','campaign','id_campagne'],
                                'channel_or_network', ARRAY['network','panel_network'],
                                'sub_channel_or_face', ARRAY['face','panel_face','panel_id']
                            ),
        'field_types',      jsonb_build_object(
                                'net_cost_eur',          'numeric',
                                'gross_cost_eur',        'numeric',
                                'impressions_count',     'integer',
                                'event_start_timestamp', 'timestamp',
                                'event_end_timestamp',   'timestamp',
                                'media_date',            'date'
                            ),
        'grain',            'panel_face_booking',
        'identity_keys',    ARRAY['campaign_id_toorow','channel_or_network',
                                  'sub_channel_or_face','event_start_timestamp',
                                  'event_end_timestamp'],
        'date_timezone',    jsonb_build_object(
                                'rule', 'cost and impressions evenly allocated across inclusive date range'
                            ),
        'currency_unit',    jsonb_build_object('cost_currency', 'EUR', 'unit', 'monetary'),
        'sensitive_classification', 'none',
        'validation',       jsonb_build_object(
                                'required_fields_must_be_present', true,
                                'missing_required_creates', 'PENDING_USER_MAPPING'
                            ),
        'canonical_bindings', jsonb_build_object(
                                'media_type_value', 'OOH',
                                'allocation_rule',  'inclusive_date_range_even'
                              )
    ),
    FALSE
)
ON CONFLICT (template_code, version) DO NOTHING;

-- OFFLINE_DOOH_V1 --
INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
VALUES (
    'OFFLINE_DOOH_V1',
    1,
    'Offline digital out-of-home (DOOH) screen spend',
    jsonb_build_object(
        'required_fields',  ARRAY['datastream_id','campaign_id_toorow','media_type',
                                  'channel_or_network','event_start_timestamp',
                                  'media_date','net_cost_eur'],
        'optional_fields',  ARRAY['event_end_timestamp','creative_id','impressions_count',
                                  'geo_scope','sub_channel_or_face',
                                  'gross_cost_eur','raw_metadata',
                                  'duration_seconds','target_audience_code','grp_value'],
        'aliases',          jsonb_build_object(
                                'campaign_id_toorow', ARRAY['campaign_id','campaign','id_campagne'],
                                'channel_or_network', ARRAY['operator','screen_operator','dooh_operator']
                            ),
        'field_types',      jsonb_build_object(
                                'net_cost_eur',          'numeric',
                                'gross_cost_eur',        'numeric',
                                'impressions_count',     'integer',
                                'event_start_timestamp', 'timestamp',
                                'event_end_timestamp',   'timestamp',
                                'media_date',            'date'
                            ),
        'grain',            'screen_play',
        'identity_keys',    ARRAY['event_start_timestamp','sub_channel_or_face',
                                  'creative_id','channel_or_network'],
        'date_timezone',    jsonb_build_object(
                                'rule', 'missing end_timestamp = instantaneous event; present end_timestamp allocated across range'
                            ),
        'currency_unit',    jsonb_build_object('cost_currency', 'EUR', 'unit', 'monetary'),
        'sensitive_classification', 'none',
        'validation',       jsonb_build_object(
                                'required_fields_must_be_present', true,
                                'missing_required_creates', 'PENDING_USER_MAPPING'
                            ),
        'canonical_bindings', jsonb_build_object(
                                'media_type_value', 'DOOH',
                                'allocation_rule',  'instantaneous_or_range'
                              )
    ),
    FALSE
)
ON CONFLICT (template_code, version) DO NOTHING;

-- OFFLINE_RADIO_V1 --
INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
VALUES (
    'OFFLINE_RADIO_V1',
    1,
    'Offline radio broadcast spend',
    jsonb_build_object(
        'required_fields',  ARRAY['datastream_id','campaign_id_toorow','media_type',
                                  'channel_or_network','event_start_timestamp',
                                  'media_date','net_cost_eur'],
        'optional_fields',  ARRAY['duration_seconds','target_audience_code','grp_value',
                                  'impressions_count','creative_id','sub_channel_or_face',
                                  'gross_cost_eur','event_end_timestamp','raw_metadata'],
        'aliases',          jsonb_build_object(
                                'campaign_id_toorow', ARRAY['campaign_id','campaign','id_campagne'],
                                'channel_or_network', ARRAY['station','radio_station']
                            ),
        'field_types',      jsonb_build_object(
                                'net_cost_eur',          'numeric',
                                'gross_cost_eur',        'numeric',
                                'grp_value',             'numeric',
                                'impressions_count',     'integer',
                                'duration_seconds',      'integer',
                                'event_start_timestamp', 'timestamp',
                                'media_date',            'date'
                            ),
        'grain',            'broadcast_spot',
        'identity_keys',    ARRAY['media_date','channel_or_network','creative_id'],
        'date_timezone',    jsonb_build_object(
                                'media_date_cutoff_local_hour', 5,
                                'rule', 'media_date derived from event_start_timestamp at 05:00 local'
                            ),
        'currency_unit',    jsonb_build_object('cost_currency', 'EUR', 'unit', 'monetary'),
        'sensitive_classification', 'none',
        'validation',       jsonb_build_object(
                                'required_fields_must_be_present', true,
                                'missing_required_creates', 'PENDING_USER_MAPPING'
                            ),
        'canonical_bindings', jsonb_build_object(
                                'media_type_value', 'RADIO'
                              )
    ),
    FALSE
)
ON CONFLICT (template_code, version) DO NOTHING;

-- OFFLINE_PRESS_V1 --
INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
VALUES (
    'OFFLINE_PRESS_V1',
    1,
    'Offline print press spend',
    jsonb_build_object(
        'required_fields',  ARRAY['datastream_id','campaign_id_toorow','media_type',
                                  'channel_or_network','event_start_timestamp',
                                  'event_end_timestamp','media_date','net_cost_eur'],
        'optional_fields',  ARRAY['impressions_count','geo_scope','creative_id',
                                  'sub_channel_or_face','gross_cost_eur',
                                  'raw_metadata','duration_seconds',
                                  'target_audience_code','grp_value'],
        'aliases',          jsonb_build_object(
                                'campaign_id_toorow', ARRAY['campaign_id','campaign','id_campagne'],
                                'channel_or_network', ARRAY['title','publication','press_title']
                            ),
        'field_types',      jsonb_build_object(
                                'net_cost_eur',          'numeric',
                                'gross_cost_eur',        'numeric',
                                'impressions_count',     'integer',
                                'event_start_timestamp', 'timestamp',
                                'event_end_timestamp',   'timestamp',
                                'media_date',            'date'
                            ),
        'grain',            'publication_insertion',
        'identity_keys',    ARRAY['channel_or_network','event_start_timestamp',
                                  'sub_channel_or_face','creative_id'],
        'date_timezone',    jsonb_build_object(
                                'rule', 'cost and impressions evenly allocated across inclusive date range'
                            ),
        'currency_unit',    jsonb_build_object('cost_currency', 'EUR', 'unit', 'monetary'),
        'sensitive_classification', 'none',
        'validation',       jsonb_build_object(
                                'required_fields_must_be_present', true,
                                'missing_required_creates', 'PENDING_USER_MAPPING'
                            ),
        'canonical_bindings', jsonb_build_object(
                                'media_type_value', 'PRESS',
                                'allocation_rule',  'inclusive_date_range_even'
                              )
    ),
    FALSE
)
ON CONFLICT (template_code, version) DO NOTHING;

-- GENERIC_TABULAR_V1 (discover-first fallback; is_generic = TRUE) --
INSERT INTO app.import_templates (template_code, version, title, contract, is_generic)
VALUES (
    'GENERIC_TABULAR_V1',
    1,
    'Generic tabular file (discover-first fallback)',
    jsonb_build_object(
        'required_fields',  ARRAY['datastream_id','campaign_id_toorow','media_type',
                                  'channel_or_network','event_start_timestamp',
                                  'media_date','net_cost_eur'],
        'optional_fields',  ARRAY['sub_channel_or_face','event_end_timestamp',
                                  'duration_seconds','target_audience_code',
                                  'grp_value','impressions_count','gross_cost_eur',
                                  'creative_id','geo_scope','raw_metadata'],
        'aliases',          jsonb_build_object(
                                'campaign_id_toorow', ARRAY['campaign_id','campaign','id_campagne']
                            ),
        'field_types',      jsonb_build_object(
                                'net_cost_eur',          'numeric',
                                'event_start_timestamp', 'timestamp',
                                'media_date',            'date'
                            ),
        'grain',            'unknown',
        'identity_keys',    ARRAY[]::TEXT[],
        'date_timezone',    jsonb_build_object(
                                'rule', 'discover-first: grain and identity confirmed during mapping'
                            ),
        'currency_unit',    jsonb_build_object('cost_currency', 'EUR', 'unit', 'monetary'),
        'sensitive_classification', 'unknown',
        'validation',       jsonb_build_object(
                                'required_fields_must_be_present', true,
                                'missing_required_creates', 'PENDING_USER_MAPPING',
                                'publish_gate', 'canonical_semantics_required'
                            ),
        'canonical_bindings', jsonb_build_object(
                                'publish_gate', 'canonical_semantics_required',
                                'note', 'Datastream created against this template is DRAFT (publishable=FALSE) until required canonical semantics are confirmed in the mapping flow.'
                              )
    ),
    TRUE
)
ON CONFLICT (template_code, version) DO NOTHING;

COMMIT;
