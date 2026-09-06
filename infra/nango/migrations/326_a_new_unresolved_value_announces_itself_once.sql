-- The delta memory of the `unresolved_values` monitor, and the finer identity
-- one alert row needs to carry it.
--
-- TARGET: `docs/product-architecture/unresolved-values.md`, section "Alerting --
-- the new value announces itself, the backlog does not", ratified 2026-08-12
-- (Jean) and decided « construire » on 2026-08-31.
--
-- ---------------------------------------------------------------------------
-- 1. WHY A STORE AT ALL, AND WHY THE PAGE SAYS "NOTHING IS STORED"
-- ---------------------------------------------------------------------------
-- The reading itself stores nothing: an unresolved set is REPLAYED from the
-- warehouse at every request (`core.unresolved_values.read_unresolved_set`), and
-- that stays true. What is stored here is not the reading, it is WHAT WAS
-- ALREADY ANNOUNCED -- which the alerting clause requires and the reading cannot
-- hold:
--
--     "It fires on the delta, never on the standing set. The condition is *a
--      value that was not in the previous evaluation's unresolved set*. [...] The
--      first evaluation of a (Datastream, dimension) seeds and fires nothing."
--
-- The completeness pass of that page (item 1) measured why the existing stores
-- cannot answer it. A value that carries a row in `app.dimension_value_mappings`
-- -- `proposed`, `confirmed` or `rejected` -- is indeed a value already reported.
-- But a value with NO candidate above the suggestion threshold produces no row at
-- all: the geography loop counts it as `unresolved` and forgets it, so it would
-- announce itself again every night. Hence "the smallest possible sighting
-- record".
--
-- ---------------------------------------------------------------------------
-- 2. TWO TABLES, AND THE SECOND ONE IS NOT A DUPLICATE OF THE FIRST
-- ---------------------------------------------------------------------------
-- `unresolved_value_watches`   one row per (Datastream, dimension). Its EXISTENCE
--                              is the arming. Without it, "this pair has never
--                              been evaluated" and "this pair was evaluated and
--                              its backlog was empty" are the same absence -- and
--                              the second one would swallow the first real new
--                              value as if it were backlog. That is the clause
--                              read backwards, and it costs one row.
--
-- `unresolved_value_sightings` one row per (Datastream, dimension, reason, value)
--                              ALREADY ANNOUNCED. Its foreign key onto the watch
--                              is what makes a sighting without an arming
--                              impossible rather than merely unlikely.
--
-- WHY THE REASON IS PART OF THE SIGHTING KEY. The firing's grain is
-- `(datastream, dimension, reason)`; a value that moves from `unmapped` to
-- `no_reference` is a different finding repaired by a different gesture, and the
-- page forbids merging the three ("it never merges the three reasons into one
-- number"). Keyed on the reason, such a value announces itself once per reason it
-- has actually taken, which is once per repair somebody owes.
--
-- ---------------------------------------------------------------------------
-- 3. THE VALUE IS STORED AS A FINGERPRINT, NEVER AS ITSELF -- and that is a
--    decision, not a shortcut.
-- ---------------------------------------------------------------------------
-- The memory needs EQUALITY and nothing else: "was this value announced before?".
-- A digest answers that exactly. Storing the source value instead would buy
-- nothing and cost three things:
--
--   (a) the same page (completeness pass, item 3) states that a dimension of
--       e-mails or user ids is MASKED by the reader's classification policy, and
--       that this surface "must not become the hole through which classified
--       values leave". A platform table holding the raw values would be that
--       hole, opened by the alerting half rather than by the panel;
--   (b) the firing's "top few by cost" are composed from TODAY's reading, which
--       already applied the classification. Nothing downstream ever needs to
--       render a value out of this memory;
--   (c) an org erasure would otherwise have to scrub client vocabulary out of a
--       second place.
--
-- The digest is SHA-256 of the UTF-8 source value, computed by
-- `core.unresolved_values_monitor.fingerprint`. It is not salted: a salt would
-- make the memory unreadable to the very sweep that writes it, and the digest is
-- keyed by Datastream and dimension already.
--
-- ---------------------------------------------------------------------------
-- 4. ORG ERASURE. It is NOT `core.org_purge` that names these rows.
-- ---------------------------------------------------------------------------
-- Both tables hang off `app.projects (org_id, id)` ON DELETE CASCADE, and
-- `plan_purge` walks the foreign-key graph over `confdeltype IN ('a','r')` --
-- NO ACTION and RESTRICT -- only. A CASCADE edge is invisible to it BY DESIGN,
-- because PostgreSQL already does the work. The parent statement that reaches
-- them is therefore the `DELETE FROM app.projects ...` the plan does emit, and
-- the cascade carries it down: watches first, then the sightings that hang off
-- the watches. Neither table carries a DELETE guard, so no `app.rgpd_erasure`
-- hatch is owed and none is written: there is nothing here an erasure must be
-- let past.
--
-- ---------------------------------------------------------------------------
-- 5. THE COST THRESHOLD IS A PROJECT PREFERENCE, NOT A CONSTANT.
-- ---------------------------------------------------------------------------
-- "The threshold is cost, not count. [...] One new value carrying three rows is
-- not a reason to wake anybody, and one carrying 12 % of the month's spend is."
-- `app.project_preferences` is where a governed per-project threshold already
-- lives (`max_projection_grain_cardinality`, migration 033), and
-- `datastream_projection.resolve_thresholds` is the shape that records
-- `threshold_source` so a documented default is never applied silently. The
-- column added below is read the same way.
--
-- ---------------------------------------------------------------------------
-- 6. `alert_firings.finding_key` -- migration 229's identity, widened by exactly
--    what the ninth monitor needs and by nothing else.
-- ---------------------------------------------------------------------------
-- Migration 229 dedups a DQ firing on `(project_id, type, datastream_id,
-- window_date)`: "one finding, one day, one Datastream, one alert". That identity
-- is right for the eight monitors that existed, whose finding IS the Datastream
-- (its volume, its lateness, its schema). It is too coarse for this one, whose
-- finding is `(datastream, dimension, reason)`: a Datastream with a new unmapped
-- video AND a new absent creative on the same night would have written one row
-- and lost the other to `ON CONFLICT DO NOTHING`, silently -- which is the same
-- collapse the page forbids on screen ("it never merges the three reasons into
-- one number").
--
-- The column is NULLABLE and the index reads `COALESCE(finding_key, '')`, so
-- every existing writer keeps migration 229's behaviour EXACTLY: they pass no
-- finding key, every row of a given `(project, type, datastream, day)` collapses
-- to `''`, and the identity is unchanged. Only a writer that STATES a finer
-- finding gets a finer identity. No row is rewritten; the column is NULL on all
-- of them.
--
-- Additive only. No table is dropped, no column removed, no row seeded.

BEGIN;

-- ---------------------------------------------------------------------------
-- The arming. One row per (Datastream, dimension) the monitor has ever swept.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.unresolved_value_watches (
    datastream_id       TEXT        NOT NULL,
    dimension           TEXT        NOT NULL,
    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,

    -- The moment this pair started being watched. A person reading the panel is
    -- owed "watched since", and the delta rule is unreadable without it.
    armed_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- How many unresolved values the backlog held at that moment. Recorded, not
    -- derived: the sightings of the backlog are indistinguishable afterwards from
    -- the values that arrived later and were announced, and "we swallowed 516 on
    -- the first night" is the one number that says the first arming behaved.
    baseline_values     INTEGER     NOT NULL DEFAULT 0
                        CHECK (baseline_values >= 0),

    last_evaluated_at   TIMESTAMPTZ,
    last_fired_at       TIMESTAMPTZ,

    CONSTRAINT pk_unresolved_value_watches
        PRIMARY KEY (datastream_id, dimension),
    CONSTRAINT ck_unresolved_value_watches_dimension
        CHECK (btrim(dimension) <> '' AND length(dimension) <= 200),
    -- A firing that never happened cannot be older than the arming.
    CONSTRAINT ck_unresolved_value_watches_fired_after_armed
        CHECK (last_fired_at IS NULL OR last_fired_at >= armed_at),

    CONSTRAINT fk_unresolved_value_watches_project
        FOREIGN KEY (org_id, project_id)
        REFERENCES app.projects (org_id, id) ON DELETE CASCADE,
    CONSTRAINT fk_unresolved_value_watches_datastream
        FOREIGN KEY (datastream_id)
        REFERENCES app.datastreams (id) ON DELETE CASCADE
);

COMMENT ON TABLE app.unresolved_value_watches IS
    'One row per (Datastream, dimension) the unresolved-values monitor has swept. '
    'Its EXISTENCE is the arming: the first evaluation records the backlog as the '
    'baseline and fires nothing (unresolved-values.md, Alerting). Without it, a '
    'pair never evaluated and a pair whose backlog was empty are the same absence, '
    'and the first real new value would be swallowed as backlog.';
COMMENT ON COLUMN app.unresolved_value_watches.baseline_values IS
    'How many unresolved values the backlog held at arming -- the number that says '
    'the first arming swallowed them instead of mailing them as new arrivals.';

CREATE INDEX IF NOT EXISTS idx_unresolved_value_watches_project
    ON app.unresolved_value_watches (project_id, datastream_id);

-- ---------------------------------------------------------------------------
-- The memory. One row per value ALREADY ANNOUNCED, as a digest.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS app.unresolved_value_sightings (
    datastream_id       TEXT        NOT NULL,
    dimension           TEXT        NOT NULL,
    -- The three reasons of `core.unresolved_values.REASONS`, and they never
    -- merge. Written as a CHECK rather than as a lookup table: a catalogue
    -- shipped with the product is code, not a row (CLAUDE.md).
    reason              TEXT        NOT NULL
                        CHECK (reason IN ('absent_at_source', 'unmapped',
                                          'no_reference')),
    -- SHA-256 of the UTF-8 source value. NEVER the value: see header, section 3.
    value_fingerprint   TEXT        NOT NULL
                        CHECK (value_fingerprint ~ '^[0-9a-f]{64}$'),

    org_id              TEXT        NOT NULL,
    project_id          TEXT        NOT NULL,

    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- TRUE when this value reached a firing, FALSE when it was recorded as
    -- backlog at arming. The two are different claims: the first says somebody
    -- was told, the second says nobody was and deliberately so.
    announced           BOOLEAN     NOT NULL DEFAULT TRUE,

    CONSTRAINT pk_unresolved_value_sightings
        PRIMARY KEY (datastream_id, dimension, reason, value_fingerprint),
    CONSTRAINT ck_unresolved_value_sightings_seen_order
        CHECK (last_seen_at >= first_seen_at),

    -- A sighting without an arming is impossible rather than unlikely: the delta
    -- rule is only meaningful relative to a moment the watch started.
    CONSTRAINT fk_unresolved_value_sightings_watch
        FOREIGN KEY (datastream_id, dimension)
        REFERENCES app.unresolved_value_watches (datastream_id, dimension)
        ON DELETE CASCADE,
    CONSTRAINT fk_unresolved_value_sightings_project
        FOREIGN KEY (org_id, project_id)
        REFERENCES app.projects (org_id, id) ON DELETE CASCADE
);

COMMENT ON TABLE app.unresolved_value_sightings IS
    'The delta memory of the unresolved-values monitor: one row per value already '
    'announced, or recorded as backlog at arming. The monitor fires only on values '
    'absent from here, so it never re-reports what it already reported '
    '(unresolved-values.md, "Incomplete if").';
COMMENT ON COLUMN app.unresolved_value_sightings.value_fingerprint IS
    'SHA-256 of the source value. The memory needs equality and nothing else, and '
    'a dimension of e-mails or user ids is masked by the reader classification -- '
    'storing the values here would be the hole through which classified values '
    'leave (unresolved-values.md, completeness pass item 3).';
COMMENT ON COLUMN app.unresolved_value_sightings.announced IS
    'TRUE: this value reached a firing. FALSE: it was the backlog the first arming '
    'deliberately did not mail.';

-- The read the sweep takes once per (Datastream, dimension): the whole memory of
-- one pair, to diff today's reading against.
CREATE INDEX IF NOT EXISTS idx_unresolved_value_sightings_pair
    ON app.unresolved_value_sightings (datastream_id, dimension, reason);

-- ---------------------------------------------------------------------------
-- The cost threshold, governed per project like every other one.
-- ---------------------------------------------------------------------------
ALTER TABLE app.project_preferences
    ADD COLUMN IF NOT EXISTS min_unresolved_new_value_row_share NUMERIC;

ALTER TABLE app.project_preferences
    DROP CONSTRAINT IF EXISTS ck_project_preferences_unresolved_share;
ALTER TABLE app.project_preferences
    ADD CONSTRAINT ck_project_preferences_unresolved_share
    CHECK (min_unresolved_new_value_row_share IS NULL
           OR (min_unresolved_new_value_row_share >= 0
               AND min_unresolved_new_value_row_share <= 1));

COMMENT ON COLUMN app.project_preferences.min_unresolved_new_value_row_share IS
    'The share of the window rows the NEW unresolved values of one '
    '(Datastream, dimension, reason) must carry before a firing is written. NULL '
    'means the documented default of core.unresolved_values_monitor, and the '
    'firing records which of the two was applied (threshold_source), so no '
    'platform-wide hardcode is applied silently.';

-- ---------------------------------------------------------------------------
-- One fact, one alert -- when the fact is finer than a Datastream-day.
-- ---------------------------------------------------------------------------
ALTER TABLE app.alert_firings
    ADD COLUMN IF NOT EXISTS finding_key TEXT;

COMMENT ON COLUMN app.alert_firings.finding_key IS
    'What this firing is ABOUT, when the Datastream and the day do not identify it '
    'on their own. NULL for every writer whose finding is the Datastream itself, '
    'and NULL on every row written before migration 326 -- the dedup index reads '
    'COALESCE(finding_key, ''''), so migration 229''s identity is unchanged for '
    'them. The unresolved-values monitor writes (dimension, reason) here, because '
    'two findings of one Datastream on one night are two alerts.';

DROP INDEX IF EXISTS app.alert_firings_dedup_dq;
CREATE UNIQUE INDEX IF NOT EXISTS alert_firings_dedup_dq
    ON app.alert_firings (project_id, type, datastream_id, window_date,
                          COALESCE(finding_key, ''))
    WHERE type LIKE 'dq\_%' AND datastream_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- RLS, the same posture as migration 317's project-scoped tables.
-- ---------------------------------------------------------------------------
DO $rls$
DECLARE
    target  TEXT;
    guarded TEXT[] := ARRAY['unresolved_value_watches', 'unresolved_value_sightings'];
BEGIN
    FOREACH target IN ARRAY guarded LOOP
        EXECUTE format('ALTER TABLE app.%I ENABLE ROW LEVEL SECURITY', target);
        EXECUTE format('ALTER TABLE app.%I FORCE ROW LEVEL SECURITY', target);
        EXECUTE format('DROP POLICY IF EXISTS %I ON app.%I', target || '_epic36', target);
        EXECUTE format(
            'CREATE POLICY %I ON app.%I USING ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id)) '
            'WITH CHECK ('
            'current_setting(''toorow.enforce_epic36'', true) IS DISTINCT FROM ''on'' '
            'OR app.epic36_has_resource_access(org_id, ''project'', project_id))',
            target || '_epic36', target
        );
    END LOOP;
END
$rls$;

-- ---------------------------------------------------------------------------
-- The privileges, written beside their REVOKE (migration 316's lesson): the
-- default privileges migration 207 set FOR ROLE postgres would otherwise widen
-- this silently, and a declarative narrow GRANT would add nothing.
-- ---------------------------------------------------------------------------
DO $grants$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'connector') THEN
        RAISE NOTICE
            '326: role `connector` does not exist on this cluster -- nothing to grant.';
        RETURN;
    END IF;

    -- Both tables are UPDATED in place by the sweep (`last_seen_at`,
    -- `last_evaluated_at`), so UPDATE is theirs by need and not by default.
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON app.unresolved_value_watches '
            'TO connector';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON app.unresolved_value_sightings '
            'TO connector';
END
$grants$;

COMMIT;
