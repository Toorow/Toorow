-- Story 64.9 (AI-232): the governed node becomes joinable in the warehouse.
--
-- THE MEASURED GAP. `mirror_sync._DEFAULT_TABLES` carried 21 entries and not one
-- was `master_data_*`. A dbt model could therefore join the CLIENT's own
-- correspondence table (`reference_tables`, mirrored since migration 126) but not
-- the GOVERNED node that carries identity, SKOS-typed aliases and versions. Every
-- other link of the chain existed -- file template, datastream, mapping, the
-- `derived_columns_projection` macro, `normalize_dimension`, `days_between` -- so
-- this one absence is what kept a client-supplied dimension ungoverned.
--
-- WHY TWO VIEWS AND NOT TWO TABLES IN THE MIRROR. Same reason as migration 119:
-- what the warehouse sees is a DELIBERATE projection, not "whatever the table
-- happens to hold". The column list lives here, in Postgres, so it cannot drift
-- from the schema inside mirror_sync.py.
--
-- SCALARS ONLY. `master_data_aliases.evidence` is JSONB and is deliberately NOT
-- projected -- the fee/tax precedent mirrors relationally, never a raw object or
-- array. A consumer that needs the evidence reads Postgres, not the warehouse.
--
-- WHAT IS DELIBERATELY NOT DECIDED HERE. These views resolve nothing. Which
-- `relation` counts as a match, what an expired alias means and what happens to a
-- `contradiction` are Story 64.10's decisions, taken in the macro. Encoding them
-- in the view would freeze a policy where a build-time rule belongs -- and a view
-- is frozen at creation (the lesson of `119_fee_tax_alignment.sql:541`).

BEGIN;

-- ---------------------------------------------------------------------------
-- The identity. `archived_at IS NULL` because an archived node must not resolve
-- a value: nothing is deleted here (an old Result pins a version and must stay
-- reproducible), so the filter is the only thing that retires an identity.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.master_data_nodes_dim_v AS
SELECT org_id,
       project_id,
       id          AS node_id,
       registry_id,
       node_kind,
       label
FROM app.master_data_nodes
WHERE archived_at IS NULL;

COMMENT ON VIEW app.master_data_nodes_dim_v IS
    'Story 64.9: scalars-only projection of the live governed nodes for the warehouse mirror. Archived nodes are excluded; nothing is deleted upstream.';

-- ---------------------------------------------------------------------------
-- The words a source uses for that identity. `relation` travels UNRESOLVED: SKOS
-- says a close match is not transitive, and collapsing close into exact here is
-- exactly how two different companies become one row. `conflict_state` travels
-- too -- a contradiction is recorded, never resolved by write order.
--
-- `raw_value` is kept beside `normalized_value` so an unresolved value can be
-- shown to a person in the spelling their file actually used.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW app.master_data_aliases_dim_v AS
SELECT org_id,
       project_id,
       node_id,
       namespace,
       locale,
       raw_value,
       normalized_value,
       relation,
       confidence,
       effective_from,
       effective_to,
       provenance,
       conflict_state
FROM app.master_data_aliases
WHERE retired_at IS NULL;

COMMENT ON VIEW app.master_data_aliases_dim_v IS
    'Story 64.9: scalars-only projection of live SKOS-typed aliases. relation and conflict_state travel unresolved -- the matching policy belongs to Story 64.10, at build time, not to this view.';

COMMIT;
