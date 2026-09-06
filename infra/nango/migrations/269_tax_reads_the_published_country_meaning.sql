-- Epic 37 / Story 37.9: Tax stops reading a second geographic mapping.
--
-- THE MEASURED DIVERGENCE. `docs/product-architecture/capabilities/country.md`
-- is incomplete when "Analyze, Context Hub or Tax uses a second geographic
-- mapping", and today Tax does:
--
--   * Analyze reads the PUBLISHED hierarchy version through
--     `country_registry.load_projection` (`reports._load_geography_projection`),
--     and so does the Tax MCP proposal path
--     (`capability_compilers._tax_fee_preset_proposals`);
--   * the Tax CASCADE -- `dbt/models/marts/fee_tax_country_resolution.sql` and
--     its Python twin `server/core/fee_tax_geo_bridge.py` -- reads
--     `app.project_preferences.geographic_mode` / `local_markets`.
--
-- Those are not two views of one thing. `country_registry.py`'s own header says
-- the preference columns are what it "replaces outright", and the Country
-- capability confirmation (`project_settings` -> `country_activation`) derives a
-- posture IN MEMORY to compile plans and writes NOTHING back to
-- `project_preferences`. So a Project governed through the ratified Country
-- capability presents an EMPTY posture to the cascade, every spend row falls to
-- `no_binding_and_posture_global`, and the composed total is reported incomplete
-- for a Project whose geography is fully published. The failure is silent: the
-- gap reason names a posture the operator never chose.
--
-- WHAT THIS VIEW IS. `GeographyProjection.market_of_value` -- the exact structure
-- `load_projection` builds -- expressed once, in Postgres, so both engines read
-- the same rows rather than two shapes of the same intention. One row per
-- (project, assigned country) at the Project's CURRENT published hierarchy
-- version, effective TODAY.
--
-- WHY A VIEW AND NOT A MIRRORED TABLE PER PART. Migration 233's reason, and 119's
-- before it: the column list lives in Postgres so it cannot drift from the schema
-- inside `mirror_sync.py`. It also removes the ONE adapter branch of
-- `fee_tax_country_resolution.sql`: the market list arrived as
-- `project_preferences.local_markets` JSON, which needed `from_json` / `UNNEST`
-- and therefore made the whole model DuckDB-only and a deliberate no-op on
-- BigQuery. Flat scalars need no JSON, so that model becomes portable as a side
-- effect of reading the right source.
--
-- FAITHFUL TO `load_projection`, DELIBERATELY:
--
--   * the registry is selected on `object_kind = 'country'` and `project_id`
--     alone, with no lifecycle filter -- `fetch_country_registry` has none, and
--     adding one here would make the view disagree with the Python reader;
--   * `current_version_id` is the pin. No published version means NO ROW, which
--     the consumer must read as "this Project has published no Country meaning",
--     never as Global -- `load_projection` returns None for exactly this case and
--     its docstring forbids the same substitution;
--   * effective dating matches `build_projection`: a membership counts when
--     `effective_from <= today` and (`effective_to IS NULL` OR
--     `today < effective_to`). `CURRENT_DATE` is evaluated per query, not frozen
--     at creation;
--   * the market label prefers the VERSION's `node_labels` payload over the live
--     node label, because a rename after publication must not retroactively
--     change what a published version means;
--   * archived nodes are NOT excluded (unlike `master_data_nodes_dim_v`): a
--     version that contains an archived node still means what it meant, and an
--     old Result pinning it must stay reproducible.
--
-- `market_kind` TRAVELS UNRESOLVED, and that is load-bearing. A country assigned
-- to the Rest of World catch-all is resolved geographically but is NOT budgetable,
-- which is exactly the `binding_not_bindable` branch of the bridge. Collapsing the
-- kind here would make a synthetic grouping look like a declared market.
--
-- `Unknown` is absent by construction: it is not a node (see `country_registry.py`),
-- it is the evidence state of a value that did not resolve, and a country not
-- assigned to any market simply produces no row -- which the bridge already reads
-- as `country_outside_tracked_markets`.

BEGIN;

CREATE OR REPLACE VIEW app.country_market_projection_v AS
WITH published AS (
    SELECT r.project_id      AS project_id,
           r.id              AS registry_id,
           v.id              AS hierarchy_version_id,
           v.vocabulary_version_id AS vocabulary_version_id,
           v.content_hash    AS hierarchy_content_hash,
           v.payload         AS payload
    FROM app.master_data_registries r
    JOIN app.master_data_object_versions v
      ON v.id = r.current_version_id
     AND v.project_id = r.project_id
    WHERE r.object_kind = 'country'
      AND r.project_id IS NOT NULL
      AND r.current_version_id IS NOT NULL
),
-- Country -> owning node. `child_value` is the ISO code; `child_node_id` rows are
-- the node -> region edges, read separately below.
assigned AS (
    SELECT p.project_id            AS project_id,
           p.registry_id           AS registry_id,
           p.hierarchy_version_id  AS hierarchy_version_id,
           p.vocabulary_version_id AS vocabulary_version_id,
           p.hierarchy_content_hash AS hierarchy_content_hash,
           p.payload               AS payload,
           UPPER(BTRIM(m.child_value)) AS country_code,
           m.parent_node_id        AS market_id,
           m.display_order         AS display_order
    FROM published p
    JOIN app.master_data_memberships m
      ON m.project_id = p.project_id
     AND m.version_id = p.hierarchy_version_id
    WHERE m.child_value IS NOT NULL
      AND BTRIM(m.child_value) <> ''
      AND m.effective_from <= CURRENT_DATE
      AND (m.effective_to IS NULL OR CURRENT_DATE < m.effective_to)
),
-- Market -> region. One effective parent per node inside one version is enforced
-- upstream (migration 140's partial unique index), so this stays one row per node.
node_parent AS (
    SELECT p.project_id           AS project_id,
           p.hierarchy_version_id AS hierarchy_version_id,
           m.child_node_id        AS node_id,
           m.parent_node_id       AS parent_node_id
    FROM published p
    JOIN app.master_data_memberships m
      ON m.project_id = p.project_id
     AND m.version_id = p.hierarchy_version_id
    WHERE m.child_node_id IS NOT NULL
      AND m.effective_from <= CURRENT_DATE
      AND (m.effective_to IS NULL OR CURRENT_DATE < m.effective_to)
)
SELECT a.project_id                           AS project_id,
       a.registry_id                          AS registry_id,
       a.hierarchy_version_id                 AS hierarchy_version_id,
       a.vocabulary_version_id                AS vocabulary_version_id,
       a.hierarchy_content_hash               AS hierarchy_content_hash,
       a.country_code                         AS country_code,
       a.market_id                            AS market_id,
       COALESCE(a.payload -> 'node_labels' ->> a.market_id, mn.label)
                                              AS market_label,
       mn.node_kind                           AS market_kind,
       np.parent_node_id                      AS region_id,
       COALESCE(a.payload -> 'node_labels' ->> np.parent_node_id, rn.label)
                                              AS region_label,
       a.display_order                        AS display_order
FROM assigned a
LEFT JOIN app.master_data_nodes mn
  ON mn.id = a.market_id
 AND mn.project_id = a.project_id
LEFT JOIN node_parent np
  ON np.project_id = a.project_id
 AND np.hierarchy_version_id = a.hierarchy_version_id
 AND np.node_id = a.market_id
LEFT JOIN app.master_data_nodes rn
  ON rn.id = np.parent_node_id
 AND rn.project_id = a.project_id;

COMMENT ON VIEW app.country_market_projection_v IS
    'Story 37.9: the Project''s PUBLISHED country->market->region meaning, one row per assigned country at the current hierarchy version. The single geographic authority both engines read; project_preferences.local_markets is superseded and must not be read for geography.';

COMMIT;
