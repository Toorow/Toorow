"""Governed Postgres -> DuckDB/BigQuery mirror sync (AD-8, Story 4.4).

Syncs a configurable list of Postgres tables to the DuckDB mirror schema
(local analog of BigQuery mirror_* dataset). Postgres is the sole writer;
DuckDB/BigQuery is read-only from the analytics perspective.

AD-8: this is the ONLY Postgres->BigQuery path. No other code path may write
Postgres data to BigQuery/DuckDB directly.

This is the ONLY file that may write to the mirror.* schema.

Story 5.3 extension: to add alert_definitions to the mirror, add it to
MIRROR_SYNC_TABLES env var or extend the default list below.
No code change in this file is required (HG-8).

Story 11.3 extension: context_topics, procedures, context_graph, schema_context
added to _ALLOWED_TABLES and _DEFAULT_TABLES. Single Postgres->warehouse path
(AD-8) unchanged; DuckDB-first; BigQuery Phase B unaffected.

Story 22.1 extension: media_plans, media_plan_versions, media_plan_lines,
plan_allocation_daily added to the mirror (FR38/CAP-26). The plan-vs-actual mart
(22.4) reads these; media_plan_versions carries is_active so the mart filters the
active version. AD-8 path unchanged.

Story 22.3 extension: plan_line_mappings added to the mirror (FR38/CAP-26). The
22.4 ventilation joins active mappings against fact_daily_kpi to split each
campaign's spend across the plan lines that claim it. AD-8 path unchanged.

Story 13.2 extension: fx_conflict_resolutions added to the mirror (AD-6). The
dbt staging models (stg_*_daily.sql) JOIN on this table to prefer the declared
source currency over raw.cost_source_currency when a CURRENCY_CONFLICT resolution
exists. AD-6 path unchanged: no Python conversion, only dbt staging reads this.

Story 41.1 extension (Epic 41, fee & tax alignment): six entries added at once --
fee_tax_rules, fee_tax_rule_conditions, fee_tax_rule_tiers, datastream_source_types,
datastreams_dim, datastream_country_binding_dim. Story 41.1 owns EVERY Epic-41 edit
to this file (decision C.8/2): a later Epic-41 story touching mirror_sync.py is a
signal that something was missed, not routine work.

  * The fee/tax rules are mirrored RELATIONALLY, never as raw JSONB or arrays.
    Correction of an earlier justification (review finding F6): it is NOT true that
    "no mirrored table has ever carried a JSONB column" -- project_preferences has
    been mirrored by SELECT * since Story 4.4 and carries local_markets (JSONB) and
    local_market_country_codes (TEXT[]). The real reason is that those columns have
    no PINNED warehouse type and no dbt model reads them as structured values: this
    module goes psycopg -> pandas -> DuckDB, so a column of Python dicts/lists lands
    as whatever pandas and DuckDB infer, and the empty-table branch of
    _write_to_duckdb declares EVERY column VARCHAR -- so the same column can differ
    in type between a populated and an empty sync, and again on BigQuery. The 41.3
    engine must not build arithmetic on that. Migration 119 therefore flattens
    conditions, tiers AND source_type_scope into relational Postgres views
    (app.fee_tax_rule_conditions_v, app.fee_tax_rule_tiers_v) plus a scalars-only
    projection (app.fee_tax_rules_dim_v), where the types are exact. The mirror
    entry names stay clean (no _v suffix leaking into dbt), which is why each one is
    a one-line curated entry rather than the generic SELECT *.
  * Entries whose relation may not exist yet are GUARDED at CALL time, two ways:
      - _GUARDED_RELATIONS -> the entry is SKIPPED and the sync CONTINUES. Used for
        everything migration 119 creates. Migrations here are applied BY HAND and
        119 is human-gated, so this code can legitimately be deployed first; without
        the guard the first fee/tax table would raise UndefinedTable, sync_tables
        would return the error shape, every table after it would silently stop
        syncing, and the health endpoint would report mirror_sync: null -- which is
        indistinguishable from "never synced", so the lag alert could not fire. The
        skip is counted, logged and returned in the result, never silent.
      - _CURATED_SQL_FACTORY -> the entry produces a SHAPE-STABLE EMPTY relation.
        Used only for datastream_country_binding_dim, because Story 41.3 will
        source() it while migration 104 is still unapplied, and a dbt model cannot
        compile against a relation that does not exist. Its four column names are
        the only ones duplicated in this file; every other projection keeps its
        column list in a Postgres view where it cannot drift.
    Both re-evaluate on every sync, so applying the missing migration later simply
    starts producing rows with no code change.
  * This module must NEVER call db.set_local_access_context / set
    toorow.enforce_epic36. app.datastreams is under FORCE ROW LEVEL SECURITY with a
    default-open policy (migration 059): opting into enforcement would make
    datastreams_dim mirror ZERO rows silently -- RLS filters, it does not raise.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level last sync result (AC9)
# Health endpoint reads this to surface lag metric.
# ---------------------------------------------------------------------------
_last_sync_result: dict | None = None

# ---------------------------------------------------------------------------
# connection_ref_dim column selection (AC2)
# HARDCODED intentionally -- only project-dimension columns, no tokens (AD-3).
# Adding columns requires an explicit code change here to prevent accidental
# token leakage. Do NOT make this dynamic.
#
# NOTE: The actual Postgres schema uses 'provider' for the connector identifier
# (not 'connector_name'). We alias it as 'connector_name' in the mirror so dbt
# models use a consistent name. 'display_name' does not exist in connection_ref
# at this schema version; it is aliased as NULL for forward compatibility with
# Story 5.3 which may add it. This column list is the authoritative definition
# of what the mirror exposes -- no token columns (nango_connection_id excluded).
# ---------------------------------------------------------------------------
_CONNECTION_REF_DIM_SQL = """
    SELECT project_id, provider AS connector_name, NULL::VARCHAR AS display_name
    FROM app.connection_ref
"""

# ---------------------------------------------------------------------------
# Story 41.1 curated projections (Epic 41, migration 119).
#
# The mirror entry names are the CLEAN names (no _v suffix leaking into dbt),
# while the Postgres relations behind them are the flattening views created by
# migration 119. Each is therefore a one-line curated SELECT: the COLUMN LIST
# lives in the view, in Postgres, so it cannot drift from the schema here.
#
# These relations exist ONLY as views for the same reason connection_ref_dim exists
# as a curated SELECT: what the warehouse sees is a deliberate projection, not
# "whatever the table happens to hold". datastream_source_types needs no entry here
# -- it is a real table whose mirror name already matches, so it rides the generic
# SELECT * branch.
# ---------------------------------------------------------------------------
_FEE_TAX_RULES_DIM_SQL = "SELECT * FROM app.fee_tax_rules_dim_v"
_FEE_TAX_RULE_CONDITIONS_SQL = "SELECT * FROM app.fee_tax_rule_conditions_v"
_FEE_TAX_RULE_TIERS_SQL = "SELECT * FROM app.fee_tax_rule_tiers_v"
_DATASTREAMS_DIM_SQL = "SELECT * FROM app.datastreams_dim_v"

# ---------------------------------------------------------------------------
# Story 48.4. Two entries change what they READ without changing what they are
# CALLED, so every dbt model keeps compiling while the authority underneath moves.
#
#   * The three fee/tax views are redefined by migration 146 over the PUBLISHED
#     `tax_fee` Rule Set version instead of the mutable `app.fee_tax_rules` rows.
#     Same frozen scalar columns, plus appended evidence columns a Result row pins.
#   * `datastream_source_types` now reads `app.datastream_source_types_v` -- the
#     LATEST observed evidence per Datastream -- because the table it used to read
#     had no production writer, so the mirror carried an empty relation and every
#     source-type-scoped rule matched everything.
#
# `project_tax_fee_activation` is new: it is the ONE activation authority, so the
# dbt models stop gating on `project_preferences.fee_tax_alignment_enabled`, which
# was a second boolean meaning the same thing.
# ---------------------------------------------------------------------------
_DATASTREAM_SOURCE_TYPES_SQL = "SELECT * FROM app.datastream_source_types_v"
#: La maille publiee d'un managed feed, et la table ou ses lignes atterrissent.
#: Sans elle, l'entrepot ne sait pas sur QUOI dedoublonner un fichier renvoye --
#: la cle vit dans le mapping, cote plateforme, et rien ne la mirroitait.
_MANAGED_FEED_GRAIN_SQL = "SELECT * FROM app.managed_feed_grain_v"

#: QUAND un chargement fichier a eu lieu. Le mart exige un `loaded_at` non
#: nul et le chemin fichier n'en apposait pas sur ses lignes -- la valeur
#: existait pourtant depuis toujours dans `datastream_executions`, elle
#: n'etait pas mirroitee (story 69.2). Rien n'est fabrique : le staging la
#: joint sur `execution_id`.
_MANAGED_FEED_EXECUTION_SQL = "SELECT * FROM app.managed_feed_execution_v"

#: Les attributs d'un noeud AVEC LA FENETRE ou chacun faisait foi. La vue 241
#: rend la version courante -- la mauvaise reponse a << que savait-on le 3 aout
#: >>. Story 69.3 : une ligne d'un fait se croise a SA date.
_MDM_ATTRIBUTES_ASOF_SQL = "SELECT * FROM app.master_data_node_attributes_asof_v"

#: Les classifications que les REGLES de l'utilisateur derivent (story 68.6),
#: estampillees de la version de regle qui les a produites. Sans ce miroir,
#: l'entrepot ne peut pas croiser par une classification de l'utilisateur --
#: exactement ce que l'epic 68 existe pour rendre possible.
_MDM_DERIVED_ATTRIBUTES_SQL = (
    "SELECT * FROM app.master_data_derived_attributes_dim_v"
)

#: Le verdict de rattachement d'une cle designee (story 68.3). Mirroite pour que
#: la lecture croisee LISE ce rattachement au lieu d'en ecrire un second.
_ENTITY_KEY_VERDICTS_SQL = "SELECT * FROM app.entity_key_match_verdicts_dim_v"
_TAX_FEE_ACTIVATION_SQL = "SELECT * FROM app.project_tax_fee_activation_v"

# ---------------------------------------------------------------------------
# Story 61.4 (AI-266). The CONFIRMED reporting currency reaches the warehouse on
# its own, instead of only through the Tax & Fees door.
#
# `app.project_money_policy_v` has existed since migration 148 and exactly one
# relation mirrored it -- `project_tax_fee_activation`, which carries
# `reporting_currency` as one column of an ACTIVATION projection. So a model that
# needed the reporting currency and had nothing to do with Tax & Fees had two
# choices: read the tax activation relation (borrowing an authority that means
# something else) or read `project_preferences.canonical_currency` (the column
# Story 48.3 removed the 'EUR' default from precisely because it was answering a
# question nobody had asked it).
#
# `plan_vs_actual_daily` needed it for the third reason, and it is the sharpest:
# it was labelling a converted amount with the PLAN's currency. A label needs an
# authority, and this is the one the product declares -- `money_policy.py`,
# `resolve_money_policy`, the same rule set a Change Set publishes.
#
# GUARDED like its ten neighbours: 148 is applied by hand, and a deployment that
# has not run it must sync everything else rather than abort on the first fetch.
_PROJECT_MONEY_POLICY_SQL = "SELECT * FROM app.project_money_policy_v"

# Story 67.20 (migration 282). The DECLARED source currency, read from the family
# that owns it instead of from the table migration 145 dethroned.
#
# `app.fx_conflict_resolutions` was mirrored here since Story 13.2 and nine staging
# models joined it, which made a store nobody was allowed to trust the runtime
# authority for every converted cost. The view projects the PUBLISHED version of
# the `source_currency` Rule Set family into the same flat shape, so the change to
# each staging model is the relation NAME and nothing else. Same reason as
# migration 119's fee/tax views: the COLUMN LIST lives in Postgres, where it
# cannot drift from the schema.
_FX_SOURCE_CURRENCY_BINDINGS_SQL = (
    "SELECT project_id, target_field, source_module, resolved_source_currency, "
    "decided_by, decided_at, note, rule_set_id, rule_set_version_id "
    "FROM app.fx_source_currency_bindings_v"
)

# ---------------------------------------------------------------------------
# Story 67.13 (migration 305). Step 2 of the FX cutover ratified in
# docs/product-architecture/capabilities/currency-fx.md.
#
# Measured before this pair existed: `grep -n "fx_rate_observations"
# server/core/mirror_sync.py` -> no hit. The governed rate store had a write door
# (`set_fixed_fx_rate`) and a freshness reader that answers `state: current`, and
# NOTHING carried a posed rate to the relation that converts. Every monetary
# figure in every mart was computed from `dbt/seeds/fx_rates.csv`, two lines,
# git-committed, whose one edit on 2026-08-17 silently restated every USD figure
# the warehouse had ever held.
#
# TWO relations for one store, and the split is migration 119's: scalars here,
# the condition flattened beside it. No jsonb crosses this seam -- pandas infers
# a JSONB column's dtype from the rows it happens to see, so an empty sync and a
# populated one would not produce the same warehouse type.
#
# GUARDED, like their eleven neighbours: 305 is applied by hand, and a deployment
# that has not run it must sync everything else rather than abort here.
_FX_POSED_RATES_SQL = (
    "SELECT project_id, observation_id, base_currency, quote_currency, rate, "
    "effective_date, valid_from, valid_to, condition_key_count, provider, "
    "rate_set_version_id "
    "FROM app.fx_posed_rates_v"
)
_FX_POSED_RATE_CONDITIONS_SQL = (
    "SELECT observation_id, condition_key, condition_value "
    "FROM app.fx_posed_rate_conditions_v"
)

# ---------------------------------------------------------------------------
# Story 64.9 (AI-232). The governed node joins the warehouse.
#
# Measured before: this list carried 21 entries and not one was `master_data_*`,
# so a dbt model could join the CLIENT's correspondence table (`reference_tables`,
# mirrored since 126) but not the GOVERNED node behind it. Every other link of the
# chain already existed -- template, datastream, mapping, the
# `derived_columns_projection` macro, `normalize_dimension`, `days_between` -- and
# this single absence is what kept a client-supplied dimension ungoverned.
#
# Both read a view created by migration 233, for migration 119's reason: the
# COLUMN LIST lives in Postgres so it cannot drift from the schema here. Scalars
# only -- `master_data_aliases.evidence` is JSONB and is deliberately not
# projected.
#
# They are GUARDED, not shaped-empty: nothing source()s them yet (Story 64.10
# writes the first consumer), so an absent relation harms no model, and 233 is
# applied by hand like every other migration here.
# ---------------------------------------------------------------------------
_MASTER_DATA_NODES_DIM_SQL = "SELECT * FROM app.master_data_nodes_dim_v"
_MASTER_DATA_ALIASES_DIM_SQL = "SELECT * FROM app.master_data_aliases_dim_v"
#: Story 64.2: the client's own properties, one row per (node, attribute). EAV and
#: not a column per attribute, because the attributes are the CLIENT's and unknown
#: at build time -- a column each would need a migration per client, the opposite
#: of a generic object model. Still scalars only: the view does the flattening in
#: Postgres, where the column list cannot drift from the schema.
_MASTER_DATA_NODE_ATTRIBUTES_SQL = "SELECT * FROM app.master_data_node_attributes_dim_v"

# ---------------------------------------------------------------------------
# Story 37.9 (migration 269). The PUBLISHED country -> market -> region meaning,
# so the Tax cascade stops reading `project_preferences.local_markets`.
#
# The two were a genuine divergence, not two views of one thing: Analyze and the
# Tax MCP proposal path already read the published hierarchy version through
# `country_registry.load_projection`, while the cascade read the preference
# columns that `country_registry.py`'s header calls what it "replaces outright"
# -- and which the Country capability confirmation never writes back. A Project
# governed through the ratified capability therefore showed the cascade an EMPTY
# posture, and every spend row came back unresolvable while its geography was
# fully published.
#
# Flat scalars on purpose. The market list arrived as JSON before, which is why
# its consumer carried a `target.type == 'duckdb'` branch and was a deliberate
# no-op on BigQuery; there is no JSON to unnest here.
#
# GUARDED, not shaped-empty -- the same choice migration 119 made for
# `project_tax_fee_activation`, which the same consumer already source()s. An
# absent relation makes the build fail LOUDLY at the declared deployment order
# (migration 269 -> mirror_sync -> dbt build) rather than emit rows whose gap
# reason would describe a Country meaning nobody could read. A shape-stable empty
# would be indistinguishable from "this Project published no Country version",
# which is a different and much more common answer.
# ---------------------------------------------------------------------------
_COUNTRY_MARKET_PROJECTION_SQL = "SELECT * FROM app.country_market_projection_v"

# ---------------------------------------------------------------------------
# datastream_country_binding_dim -- guarded at CALL time (Story 41.1).
#
# app.market_bindings is created by migration 104, which is NOT applied. An
# unguarded SELECT would raise UndefinedTable, and sync_tables aborts the ENTIRE
# sync on the first fetch error -- taking the media-plan, knowledge and
# project_preferences mirrors down in the same nightly run.
#
# This is NOT a view for a reason: a view is frozen at creation. Migration 119 may
# well be applied before 104 (the applied set is not linear), and a view created
# against the "absent" branch would stay empty FOREVER. The runtime guard
# re-evaluates on every sync, so applying 104 later just starts producing rows.
#
# app.market_bindings has NO `connector` and NO `datastream_id` column: a
# datastream binding is expressed as binding_kind='datastream',
# binding_id=<datastream id>, so the dim JOINs app.datastreams to obtain the
# connector. The INNER JOIN makes this a PROJECTION, not a faithful mirror of
# market_bindings -- a binding whose binding_id is not a live datastream is
# dropped. Do not use it to audit bindings.
#
# binding_kind is free TEXT by design (AD-2, migration 104 header): 'datastream'
# is a convention Story 41.1 introduces and NOTHING writes it today, so an EMPTY
# dim is the expected state, not a bug.
# ---------------------------------------------------------------------------
_BINDING_DIM_COLUMNS = ("project_id", "connector", "datastream_id", "market_id")

_BINDING_DIM_EMPTY_SQL = """
    SELECT NULL::TEXT AS project_id, NULL::TEXT AS connector,
           NULL::TEXT AS datastream_id, NULL::TEXT AS market_id
    WHERE FALSE
"""

# The join is on (binding_id, project_id), NOT binding_id alone (review finding F5).
# binding_id is free TEXT with no foreign key, so a binding row written under project A
# that names project B's datastream id would otherwise emit (project_B, market_of_A)
# into a warehouse dimension -- a cross-project leak in a client-facing figure. The
# extra predicate makes a mis-scoped binding produce no row instead of a wrong one.
_BINDING_DIM_SQL = """
    SELECT d.project_id,
           d.module_name  AS connector,
           mb.binding_id  AS datastream_id,
           mb.market_id
    FROM app.market_bindings mb
    JOIN app.datastreams d
      ON d.id = mb.binding_id
     AND d.project_id = mb.project_id
    WHERE mb.binding_kind = 'datastream'
"""


def _relation_exists(cur: Any, relation: str) -> bool:
    """True when *relation* ('app.foo') is present in the live schema."""
    cur.execute("SELECT to_regclass(%s) IS NOT NULL", (relation,))
    return bool(cur.fetchone()[0])


def _datastream_country_binding_sql(cur: Any) -> str:
    """Return the real JOIN when app.market_bindings exists, else a SHAPE-STABLE empty.

    Both branches project the same four column aliases in the same order, so
    mirror.datastream_country_binding_dim ALWAYS exists with the same shape --
    zero rows when migration 104 is absent, real rows the day it lands. Unlike the
    _GUARDED_RELATIONS entries this one may NOT simply skip: Story 41.3 source()s it
    while 104 is still unapplied, and a dbt model cannot compile against a relation
    that does not exist.
    """
    if _relation_exists(cur, "app.market_bindings"):
        return _BINDING_DIM_SQL
    return _BINDING_DIM_EMPTY_SQL


# Curated projections: an entry here is NOT `SELECT * FROM app.<name>`. Keys are
# mirror table names; values are the exact SQL.
_CURATED_SQL: dict[str, str] = {
    "connection_ref_dim": _CONNECTION_REF_DIM_SQL,
    "fee_tax_rules": _FEE_TAX_RULES_DIM_SQL,  # Story 41.1: scalars only, no JSONB
    "fee_tax_rule_conditions": _FEE_TAX_RULE_CONDITIONS_SQL,  # Story 41.1
    "fee_tax_rule_tiers": _FEE_TAX_RULE_TIERS_SQL,  # Story 41.1
    "datastreams_dim": _DATASTREAMS_DIM_SQL,  # Story 41.1: 41.2 source_type resolution
    "datastream_source_types": _DATASTREAM_SOURCE_TYPES_SQL,  # Story 48.4: observed
    "project_tax_fee_activation": _TAX_FEE_ACTIVATION_SQL,  # Story 48.4: one authority
    "project_money_policy": _PROJECT_MONEY_POLICY_SQL,  # Story 61.4: the reporting currency
    # Story 67.20: the governed replacement for `fx_conflict_resolutions`.
    "fx_source_currency_bindings": _FX_SOURCE_CURRENCY_BINDINGS_SQL,
    # Story 67.13 (migration 305): the posed rate, and the case it holds for.
    "fx_posed_rates": _FX_POSED_RATES_SQL,
    "fx_posed_rate_conditions": _FX_POSED_RATE_CONDITIONS_SQL,
    "managed_feed_grain": _MANAGED_FEED_GRAIN_SQL,  # AI-227: la cle qui supersede un fichier
    # Story 69.2: l'heure du chargement, que la ligne de landing ne porte pas.
    "managed_feed_execution": _MANAGED_FEED_EXECUTION_SQL,
    # Story 69.3: l'attribut a la date de la ligne, et la classification derivee.
    "master_data_node_attributes_asof": _MDM_ATTRIBUTES_ASOF_SQL,
    "master_data_derived_attributes_dim": _MDM_DERIVED_ATTRIBUTES_SQL,
    "entity_key_match_verdicts_dim": _ENTITY_KEY_VERDICTS_SQL,
    "master_data_nodes_dim": _MASTER_DATA_NODES_DIM_SQL,  # Story 64.9: l'identite gouvernee
    "master_data_aliases_dim": _MASTER_DATA_ALIASES_DIM_SQL,  # Story 64.9: les mots des sources
    # Story 64.2: les proprietes du client.
    "master_data_node_attributes_dim": _MASTER_DATA_NODE_ATTRIBUTES_SQL,
    # Story 37.9: la geographie PUBLIEE, celle que Analyze lit deja.
    "country_market_projection": _COUNTRY_MARKET_PROJECTION_SQL,
}

# Curated projections whose SQL depends on the LIVE schema (a table may not exist
# yet). Resolved on every sync so applying the missing migration later just starts
# producing rows.
_CURATED_SQL_FACTORY: dict[str, Callable[[Any], str]] = {
    "datastream_country_binding_dim": _datastream_country_binding_sql,
}

# ---------------------------------------------------------------------------
# Mirror entries that require a relation a migration may not have created yet.
# When the relation is absent the entry is SKIPPED and the sync CONTINUES; it is
# counted in the "skipped" key of the result and logged at WARNING.
#
# This exists because migrations in this repo are applied BY HAND (make
# apply-migrations; the deploy workflow has no migration step) and 119 is
# human-gated. Deploying this module before 119 is applied is therefore a REAL
# state, not a hypothetical -- and without the guard it is a silent one:
# _fetch_from_postgres would raise UndefinedTable on the first fee/tax entry,
# sync_tables would return the {"error": ...} shape, every table after it would
# stop syncing, and because _last_sync_result then carries no synced_at the health
# endpoint reports mirror_sync: null -- indistinguishable from "never synced", so
# the mirror-lag alert cannot fire. Every night, with no signal.
#
# Skipping (rather than emitting a shaped-empty relation) is deliberate: faking the
# relation would mean duplicating five column lists here, which is exactly the
# drift we moved into Postgres views to avoid. Nothing source()s these five yet, so
# an absent DuckDB relation harms no model.
# ---------------------------------------------------------------------------
_GUARDED_RELATIONS: dict[str, str] = {
    "fee_tax_rules": "app.fee_tax_rules_dim_v",
    "fee_tax_rule_conditions": "app.fee_tax_rule_conditions_v",
    "fee_tax_rule_tiers": "app.fee_tax_rule_tiers_v",
    "datastream_source_types": "app.datastream_source_types_v",
    "datastreams_dim": "app.datastreams_dim_v",
    "project_tax_fee_activation": "app.project_tax_fee_activation_v",
    "project_money_policy": "app.project_money_policy_v",  # Story 61.4, migration 148
    # Story 67.20: migration 282, applied by hand like the rest. Absent until it is
    # applied, so the sync SKIPS rather than failing every entry after it.
    "fx_source_currency_bindings": "app.fx_source_currency_bindings_v",
    # Story 67.13: migration 305, applied by hand like the rest. Absent until it
    # is applied, and a warehouse without it converts at the seed exactly as it
    # did before -- which is why the two dbt sources are read through
    # `toorow_source_or_empty` and not through a bare `source()`.
    "fx_posed_rates": "app.fx_posed_rates_v",
    "fx_posed_rate_conditions": "app.fx_posed_rate_conditions_v",
    "managed_feed_grain": "app.managed_feed_grain_v",
    # Story 69.2: migration 299, applied by hand like the rest.
    "managed_feed_execution": "app.managed_feed_execution_v",
    # Story 69.3: migration 300, applied by hand like the rest.
    "master_data_node_attributes_asof": "app.master_data_node_attributes_asof_v",
    "master_data_derived_attributes_dim": "app.master_data_derived_attributes_dim_v",
    "entity_key_match_verdicts_dim": "app.entity_key_match_verdicts_dim_v",
    # Story 64.9: migration 233, applied by hand like the rest.
    "master_data_nodes_dim": "app.master_data_nodes_dim_v",
    "master_data_aliases_dim": "app.master_data_aliases_dim_v",
    "master_data_node_attributes_dim": "app.master_data_node_attributes_dim_v",
    # Story 37.9: migration 269, applied by hand like the rest.
    "country_market_projection": "app.country_market_projection_v",
}

_DEFAULT_TABLES = [
    "context_events",
    "project_preferences",
    "connection_ref_dim",
    "alert_definitions",  # Story 5.3: added with zero code change (HG-8)
    "context_topics",  # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "procedures",  # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "context_graph",  # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "schema_context",  # Story 11.3: knowledge warehouse mirror (AD-8, DuckDB-first)
    "media_plans",  # Story 22.1: media plan mirror (AD-8) -- FR38/CAP-26
    "media_plan_versions",  # Story 22.1: includes is_active so 22.4 filters the active version
    "media_plan_lines",  # Story 22.1: version-scoped plan lines (line_key stable identity)
    "plan_allocation_daily",  # Story 22.1: materialised daily spread for the plan-vs-actual join
    "plan_line_mappings",  # Story 22.3: N:M line<->campaign mapping + splits (22.4 ventilation)
    # Story 67.20: replaces the Story 13.2 entry `fx_conflict_resolutions`, whose
    # table migration 145 dethroned and migration 282 sealed. Same columns, same
    # meaning, published version instead of an overwritten row.
    "fx_source_currency_bindings",
    "fee_tax_rules",  # Story 41.1: app.fee_tax_rules_dim_v -- scalars only, no JSONB
    "fee_tax_rule_conditions",  # Story 41.1: one row per condition key x value
    "fee_tax_rule_tiers",  # Story 41.1: one row per tier band, mode denormalised
    # Story 48.4: app.datastream_source_types_v -- the latest OBSERVED evidence per
    # Datastream. The table it replaced had no writer, so an empty relation was
    # being read as "no declared type", which matched every source-scoped rule.
    "datastream_source_types",
    # AI-227 : la maille publiee d'un managed feed. Sans elle, le staging du
    # chemin fichier ne sait pas sur quoi superseder un fichier renvoye et laisse
    # les jours en double.
    "managed_feed_grain",
    # Story 69.2 : l'heure de chargement d'un fichier. Le mart la veut non nulle
    # sur TOUTES ses branches ; la ligne de landing ne la porte pas, l'execution
    # si.
    "managed_feed_execution",
    # Story 69.3 : l'attribut a la date de la ligne, et la classification que
    # les regles de l'utilisateur derivent. Sans elles, un croisement lirait la
    # valeur d'aujourd'hui sur une ligne d'aout.
    "master_data_node_attributes_asof",
    "master_data_derived_attributes_dim",
    # Story 68.3 / 69.3 : le verdict de rattachement d'une cle designee.
    "entity_key_match_verdicts_dim",
    # Story 48.4: the ONE Tax & Fees activation authority. Replaces
    # project_preferences.fee_tax_alignment_enabled as the dbt gate.
    "project_tax_fee_activation",
    # Story 61.4: the confirmed Money Policy, on its own rather than as one column
    # of the Tax & Fees activation. `plan_vs_actual_daily` reads it to label a
    # converted amount with an authority instead of with the plan's own currency.
    "project_money_policy",
    # Story 67.13 (migration 305): the posed rate reaches the relation that
    # converts. Until these two, `app.fx_rate_observations` was absent from this
    # list and from _ALLOWED_TABLES, so a rate posted through `set_fixed_fx_rate`
    # was governed, versioned, readable -- and was not what any mart converted at.
    "fx_posed_rates",
    "fx_posed_rate_conditions",
    "datastreams_dim",  # Story 41.1: app.datastreams_dim_v -- minimal projection
    "datastream_country_binding_dim",  # Story 41.1: guarded, empty until 104 lands
    # `datastream_derived_columns` stood here and is GONE (AI-252, 2026-08-15).
    # It was mirrored so a dbt macro could project extra columns at build time --
    # except no model ever invoked that macro, the table held zero rows, and its
    # dialect was BigQuery-only. Story 60.6 deprecated the path and 60.3 forbade
    # its shape outright by requiring a pattern to compile in BOTH dialects.
    # The correspondence bases MAP() resolves against. The 500-row placement
    # case is a JOIN in the warehouse, so the rows must be IN the warehouse.
    "reference_tables",
    "reference_table_entries",
    # Story 64.9 (AI-232): the governed identity and the words each source uses
    # for it. Until these two, dbt could join the CLIENT's correspondence table
    # but not the GOVERNED node -- the one missing link of an otherwise complete
    # chain. Views from migration 233, guarded because nothing source()s them yet.
    "master_data_nodes_dim",
    "master_data_aliases_dim",
    # Story 64.2: sans elle les proprietes existent en base et sont invisibles
    # a l analyse -- le point qui decide si "injecter son contexte" sert.
    "master_data_node_attributes_dim",
    # Story 37.9: la geographie PUBLIEE. Sans elle, la cascade Tax lit
    # project_preferences.local_markets -- que la capacite Country ne reecrit
    # jamais -- et rend incomplet un total dont la geographie est publiee.
    "country_market_projection",
]

# ---------------------------------------------------------------------------
# Allowlist of table names that may be synced (mirror_sync hardening)
# Any name returned by _get_tables() that is NOT in this set is ignored with
# a WARNING log.  Add new tables here when the migration + dbt model land.
# AD-2: no provider-specific strings -- only governance / analytics tables.
# ---------------------------------------------------------------------------
_ALLOWED_TABLES: frozenset[str] = frozenset(_DEFAULT_TABLES)

# ---------------------------------------------------------------------------
# Process-level DuckDB write lock (mirror_sync hardening)
# DuckDB allows only one writer at a time per file within a process.
# This lock serialises our own concurrent write calls so two nightly runs
# (e.g. triggered manually + scheduled) cannot race on the DuckDB file.
# Cross-process safety: DuckDB's own file-level lock handles that; if a
# concurrent reader holds the file we retry once after a short sleep.
# ---------------------------------------------------------------------------
_duckdb_write_lock = threading.Lock()


def _get_tables(tables: list[str] | None) -> list[str]:
    """Resolve and validate the list of tables to sync.

    Priority:
    1. Explicit ``tables`` argument (caller-supplied).
    2. MIRROR_SYNC_TABLES env var (comma-separated).
    3. Default list: context_events, project_preferences, connection_ref_dim.

    Validation: every resolved name is checked against _ALLOWED_TABLES.
    Unknown names are logged at WARNING and silently dropped so a typo in
    MIRROR_SYNC_TABLES cannot cause data corruption or SQL injection
    (table names are used in f-string SQL -- must be allowlisted).

    AD-2: table list is externally configured -- no module-specific strings.
    HG-8: adding alert_definitions requires only an env-var change, not code.
    """
    if tables is not None:
        raw = tables
    else:
        env_val = os.environ.get("MIRROR_SYNC_TABLES", "").strip()
        if env_val:
            raw = [t.strip() for t in env_val.split(",") if t.strip()]
        else:
            raw = list(_DEFAULT_TABLES)

    validated: list[str] = []
    for name in raw:
        if name in _ALLOWED_TABLES:
            validated.append(name)
        else:
            logger.warning(
                "mirror_sync: unknown_table_ignored table=%s"
                " (not in _ALLOWED_TABLES -- add it to mirror_sync.py to enable)",
                name,
            )
    return validated


def _fetch_from_postgres(
    table: str,
) -> tuple[list[str], list[dict], dict[str, str]] | None:
    """Fetch all rows from app.<table> in Postgres.

    Dispatch (Story 41.1 refactor of the former single `if table == ...` branch):
      * _GUARDED_RELATIONS  -- checked FIRST. When the required relation is absent
        (its migration is not applied yet) this returns None, meaning "skip this
        entry, the sync continues";
      * _CURATED_SQL_FACTORY -- SQL resolved at CALL time against the live schema
        (datastream_country_binding_dim, guarded on migration 104);
      * _CURATED_SQL        -- a fixed curated projection (connection_ref_dim keeps
        its AD-3 column list byte-identical; the fee/tax entries read the
        migration-119 views so their column list lives in Postgres);
      * otherwise           -- SELECT * from app.<table>, so a new column on an
        already-mirrored table propagates with zero code change (HG-8).

    This function opens a plain core.db connection and deliberately NEVER installs
    an access context: see the module docstring (FORCE RLS on app.datastreams).

    Returns (col_names, rows_as_list_of_dicts), or None when the entry is skipped
    because its relation does not exist yet.
    """
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            required = _GUARDED_RELATIONS.get(table)
            if required is not None and not _relation_exists(cur, required):
                logger.warning(
                    "mirror_sync: relation_absent_skipped table=%s relation=%s"
                    " -- apply the migration that creates it, then re-run the sync",
                    table,
                    required,
                )
                return None

            if table in _CURATED_SQL_FACTORY:
                cur.execute(_CURATED_SQL_FACTORY[table](cur))
            elif table in _CURATED_SQL:
                cur.execute(_CURATED_SQL[table])
            else:
                # Table name comes from the _ALLOWED_TABLES allowlist only.
                cur.execute(f"SELECT * FROM app.{table}")  # noqa: S608

            cols = [desc[0] for desc in cur.description]
            # The DECLARED type, taken from the cursor rather than guessed from
            # the values. See `_duckdb_type` for why guessing is a defect.
            types = {desc[0]: _duckdb_type(desc) for desc in cur.description}
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    return cols, rows, types


#: Postgres type name -> DuckDB type. Only the families this mirror actually
#: carries; anything unlisted falls back to VARCHAR, which is lossless for a
#: mirror whose readers cast explicitly.
_PG_TO_DUCKDB = {
    "bool": "BOOLEAN",
    "int2": "SMALLINT",
    "int4": "INTEGER",
    "int8": "BIGINT",
    "float4": "FLOAT",
    "float8": "DOUBLE",
    "numeric": "DECIMAL(38,9)",
    "date": "DATE",
    "timestamp": "TIMESTAMP",
    "timestamptz": "TIMESTAMP WITH TIME ZONE",
    "json": "JSON",
    "jsonb": "JSON",
    "uuid": "UUID",
}


def _duckdb_type(description) -> str:
    """The DuckDB type for one Postgres column, from its DECLARED type.

    This exists because the previous write path inferred types from the VALUES,
    via ``pd.DataFrame(rows)``. That is silently wrong for any nullable column
    that happens to hold only NULLs in a given tenant: pandas types it as a
    float column and DuckDB materialises it as INTEGER, so a TEXT column in
    Postgres becomes a numeric column in the warehouse.

    It is not hypothetical. Story 48.3 dropped the ``'EUR'`` and
    ``'Europe/Paris'`` defaults on ``app.project_preferences`` -- deliberately,
    because a reporting currency must be chosen rather than defaulted -- and a
    Project that had not chosen yet made ``canonical_currency`` and
    ``reporting_timezone`` all-NULL. The mirror then declared both INTEGER, and
    every fixture and model that writes or reads a currency code against them
    fails with "Could not convert string 'EUR' to INT32".

    The class is larger than those two columns: ANY nullable column empty for a
    tenant had the same fate, and the failure appears in the warehouse rather
    than at the sync, which is the hardest place to trace it back from.
    """
    try:
        import psycopg  # noqa: PLC0415

        name = psycopg.postgres.types.get(description[1]).name
    except Exception:  # noqa: BLE001 -- an unknown OID is VARCHAR, never a guess
        return "VARCHAR"
    if name.startswith("_"):
        # An array type. DuckDB LISTs need the element type; VARCHAR keeps the
        # value legible and is what sources_mirror.yml already documents.
        return "VARCHAR"
    return _PG_TO_DUCKDB.get(name, "VARCHAR")


def _write_to_duckdb(
    table: str,
    col_names: list[str],
    rows: list[dict],
    db_path: str,
    col_types: dict[str, str] | None = None,
) -> None:
    """Write rows to mirror.<table> in DuckDB (full replace, CREATE OR REPLACE).

    Uses pandas DataFrame as an in-memory intermediary so DuckDB can infer
    column types from Python objects cleanly. When rows are empty, creates an
    empty table with the correct schema (col_names) so the table exists and
    _fetch_context_events can detect it as "synced but empty" vs "not synced".

    Thread-safety: acquires _duckdb_write_lock before opening the DuckDB
    connection so that concurrent calls within the same process are serialised.
    Retries once on duckdb.IOException (e.g. a concurrent reader from another
    process briefly holds the file) with a 0.5-second sleep.
    """
    import duckdb  # noqa: PLC0415
    import pandas as pd  # noqa: PLC0415

    def _do_write() -> None:
        conn = duckdb.connect(db_path)
        try:
            conn.execute("CREATE SCHEMA IF NOT EXISTS mirror")
            if rows:
                df = pd.DataFrame(rows)
                conn.register("_mirror_sync_df", df)
                if col_types:
                    # The DECLARED schema, then a cast per column. Still one
                    # statement per table and still atomic from a reader's point
                    # of view, because the CREATE OR REPLACE is what publishes it.
                    projection = ", ".join(
                        f'CAST("{name}" AS {col_types.get(name, "VARCHAR")}) AS "{name}"'
                        for name in col_names
                    )
                    conn.execute(
                        f"CREATE OR REPLACE TABLE mirror.{table} AS "
                        f"SELECT {projection} FROM _mirror_sync_df"  # noqa: S608
                    )
                else:
                    conn.execute(  # review-epic-4 F-2: atomic, no CatalogException window
                        f"CREATE OR REPLACE TABLE mirror.{table} AS SELECT * FROM _mirror_sync_df"
                    )
                conn.unregister("_mirror_sync_df")
            else:
                # No rows but we know the schema from col_names -- create an empty
                # table with correct column structure so the table exists in DuckDB.
                # This distinguishes "synced but empty" from "not yet synced" (which
                # would raise CatalogException in _fetch_context_events -> graceful []).
                cols_ddl = ", ".join(f"{c} VARCHAR" for c in col_names)
                conn.execute(f"CREATE OR REPLACE TABLE mirror.{table} ({cols_ddl})")
        finally:
            conn.close()

    with _duckdb_write_lock:
        try:
            _do_write()
        except duckdb.IOException as exc:
            # Retry once: a concurrent reader in another process may hold the file.
            logger.warning("mirror_sync: duckdb_io_error table=%s -- retrying once: %s", table, exc)
            time.sleep(0.5)
            _do_write()  # raises if it fails a second time


def sync_tables(
    tables: list[str] | None = None,
    target_backend: str | None = None,
) -> dict:
    """Sync Postgres governance tables to the DuckDB/BigQuery mirror schema.

    AD-8: the ONLY Postgres->DuckDB write path. No other module may call this.

    Args:
        tables: List of table names to sync. Defaults to MIRROR_SYNC_TABLES env
                var or ["context_events", "project_preferences", "connection_ref_dim"].
                For connection_ref_dim, only safe columns are selected (AD-3).
        target_backend: "duckdb" | "bigquery". Defaults to TOOROW_DB_MODE env
                        var (same dual-backend pattern as warehouse.py).

    Returns:
        {
            "synced": {"context_events": N, "project_preferences": N, ...},
            "lag_seconds": float,     # wall-clock from Postgres read to DuckDB write
            "synced_at": str,         # ISO-8601 UTC timestamp
            "skipped": [str, ...],    # entries whose relation does not exist yet
                                      # (unapplied migration) -- always present,
                                      # usually empty. A skip does NOT fail the run.
        }
        On Postgres error:
        {
            "error": str,
            "synced": {},
            "lag_seconds": 0.0,
        }
    """
    global _last_sync_result  # noqa: PLW0603

    table_list = _get_tables(tables)
    backend = target_backend or os.environ.get("TOOROW_DB_MODE", "duckdb")
    db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")

    t0 = time.perf_counter()
    synced: dict[str, int] = {}
    skipped: list[str] = []

    for table in table_list:
        try:
            fetched = _fetch_from_postgres(table)
        except Exception as exc:
            logger.warning("mirror_sync: postgres_fetch_error table=%s: %s", table, exc)
            result: dict = {
                "error": str(exc),
                "synced": synced,
                "lag_seconds": 0.0,
            }
            _last_sync_result = result
            return result

        if fetched is None:
            # The entry's relation does not exist yet (its migration is unapplied).
            # Skip it and KEEP GOING: the remaining tables still sync and the run
            # still records synced_at, so mirror-lag monitoring never goes dark.
            skipped.append(table)
            continue
        col_names, rows, col_types = fetched

        if backend == "duckdb":
            if not db_path:
                logger.warning("mirror_sync: TOOROW_DUCKDB_PATH not set -- skipping DuckDB write")
                synced[table] = len(rows)
                continue
            try:
                _write_to_duckdb(table, col_names, rows, db_path, col_types)
                synced[table] = len(rows)
            except Exception as exc:
                logger.warning("mirror_sync: duckdb_write_error table=%s: %s", table, exc)
                result = {"error": str(exc), "synced": synced, "lag_seconds": 0.0}
                _last_sync_result = result
                return result

        elif backend == "bigquery":
            # BigQuery write path — deferred to Phase B.
            # Same pattern: create or replace mirror_* dataset table.
            # Not implemented at P4-dev (local only).
            logger.info("mirror_sync: bigquery target -- write for %s deferred (Phase B)", table)
            synced[table] = len(rows)

        else:
            raise ValueError(f"Unknown TOOROW_DB_MODE for mirror sync: {backend!r}")

    lag_seconds = round(time.perf_counter() - t0, 4)
    synced_at = datetime.now(tz=timezone.utc).isoformat()

    logger.info(
        "mirror_sync: synced %s in %.2fs%s",
        " ".join(f"{k}={v}" for k, v in synced.items()),
        lag_seconds,
        f" (skipped: {', '.join(skipped)})" if skipped else "",
    )

    result = {
        "synced": synced,
        "lag_seconds": lag_seconds,
        "synced_at": synced_at,
        "tables": synced,  # alias for AC9 health shape compatibility
        # Entries whose relation does not exist yet (unapplied migration). Always
        # present so a reader never has to distinguish "no key" from "none skipped".
        "skipped": skipped,
    }
    _last_sync_result = result
    return result
