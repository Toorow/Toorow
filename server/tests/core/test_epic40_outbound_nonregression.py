"""Story 40.6 -- outbound binding + add-scope + non-regression (Epic 40 gate, AC4 / E40-NFR03,
NFR06). Drift on an untracked datastream = CRITICAL.

  (a) the per-source binding (40.2) is stored/read SOURCE-AGNOSTICALLY -- the AD-2 grep over
      the 40.2/40.4 core modules stays green (zero provider/brand vocabulary; provider names
      appear only in FIXTURE data);
  (b) adding an entity / wiring a binding surfaces the 40.4 cost/cardinality preview + an
      explicit backfill decision, and a confirmed add with NO backfill leaves existing figures
      provably unchanged with past periods HONESTLY showing the brand absent (not fabricated);
  (c) NON-REGRESSION (E40-NFR06, drift = CRITICAL): a datastream with NO tracked-brand
      alignment behaves EXACTLY as before -- declaring a binding wires NO new join onto an
      existing datastream. The store-level "registry population writes nothing onto existing
      joins" proof (40.1's satisfied-by-inaction, generalized to the 40.2 binding path).

No warehouse-shaped untracked datastream is in the fixture (Epic 40 is store + matching +
governance, not warehouse math), so AC4's non-regression is the STORE-LEVEL proof; a real
`dbt build` + `mirror_sync` is NOT owed here (recorded in Completion Notes). Offline + pg-gated;
skip-guarded on the 40.2/40.4 symbols. Pattern calque sur test_entity_source_bindings.py.
"""

from __future__ import annotations

import ast
import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import tracked_entity_registry as ter  # noqa: E402

# Skip-guard the 40.2 (binding) + 40.4 (add-scope) surfaces (contexted-in-parallel).
esb = pytest.importorskip("core.entity_source_bindings")
esc = pytest.importorskip("core.entity_scope_change")

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_DIR = _REPO_ROOT / "server" / "core"


# ===========================================================================
# (a) The binding is source-agnostic -- the AD-2 grep stays green (E40-NFR03, AD-2).
# ===========================================================================

# Provider/brand names must NEVER appear in the 40.2/40.4 core modules -- they are FIXTURE data
# only. (Mirror the forbidden lists in the 40.1/40.5 module tests.)
_FORBIDDEN_VOCAB = (
    "google-analytics", "google_analytics", "meta-ads", "meta_ads",
    "google-ads", "google_ads", "tiktok", "linkedin", "shopify", "stripe",
    "facebook", "pinterest", "amazon", "snapchat", "doubleverify", "strava",
    # brand names must be org/project DATA, never code:
    "peugeot", "renault", "citroen", "nike", "adidas", "coca", "orange",
)

_OUTBOUND_MODULES = (
    "entity_source_bindings.py",   # 40.2
    "entity_scope_change.py",      # 40.4
)


@pytest.mark.parametrize("module", _OUTBOUND_MODULES)
def test_no_provider_or_brand_vocabulary_in_outbound_core(module):
    """AC4 (a) -- AD-2: the 40.2/40.4 core module hard-codes NO provider name and NO brand name.
    source / external_id / query_spec are opaque org DATA; the cost/cardinality comes from the
    source's declared capability envelope. Any provider literal in core is a source-agnostic
    breach (E40-NFR03)."""
    src = (_CORE_DIR / module).read_text(encoding="utf-8").lower()
    hits = [name for name in _FORBIDDEN_VOCAB if name in src]
    assert not hits, f"{module} hard-codes provider/brand vocabulary: {hits}"


def test_binding_round_trips_opaque_source_and_query_spec():
    """AC4 (a) -- the binding stores/reads the source handle + external_id + query_spec VERBATIM
    (opaque round-trip). Core never interprets them: a fully synthetic `__epic40_*__` source and
    an arbitrary JSONB query_spec come back byte-identical. Driven over a tiny fake store so the
    proof is DB-free."""
    store = {"entities": {}, "bindings": {}, "bindings_by_key": {}, "audit": []}
    entity_row = {
        "id": "tent___epic40_brand_x__", "org_id": "__epic40_orgA__",
        "canonical_name": "__epic40_brand_x__",
    }
    store["entities"][entity_row["id"]] = entity_row

    class _Cur:
        def __init__(self):
            self.description = None
            self._res = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            params = params or []
            s = " ".join(sql.split())
            self._res = None
            self.description = None
            if s.startswith("INSERT INTO app.entity_source_bindings"):
                self.description = [(c.strip(),) for c in esb._BINDING_COLUMNS.split(",")]
                import json
                (bid, entity_id, org_id, source, account_scope, external_id, spec_json,
                 status, created_by) = params
                row = {
                    "id": bid, "entity_id": entity_id, "org_id": org_id, "scope_level": "ORG",
                    "source": source, "account_scope": account_scope, "external_id": external_id,
                    "query_spec": json.loads(spec_json), "status": status,
                    "created_by": created_by, "created_at": None, "updated_at": None,
                }
                store["bindings"][bid] = row
                store["bindings_by_key"][(entity_id, source, account_scope or "")] = bid
                self._res = [tuple(row[c[0]] for c in self.description)]
            elif "FROM app.entity_source_bindings" in s and s.startswith("SELECT"):
                self.description = [(c.strip(),) for c in esb._BINDING_COLUMNS.split(",")]
                out = []
                if "WHERE id = " in s:
                    out = [r for r in store["bindings"].values() if r["id"] == params[0]]
                else:
                    # _select_binding: entity_id + source + COALESCE(account_scope,'')
                    entity_id, source, acct = params
                    key = (entity_id, source, acct or "")
                    bid = store["bindings_by_key"].get(key)
                    out = [store["bindings"][bid]] if bid else []
                self._res = [tuple(r[c[0]] for c in self.description) for r in out]
            elif s.startswith("INSERT INTO app.metric_semantics_audit"):
                store["audit"].append({"composed": params[2]})
            else:
                self._res = []

        def fetchone(self):
            return self._res[0] if self._res else None

        def fetchall(self):
            return list(self._res or [])

    class _Conn:
        def cursor(self):
            return _Cur()

        def commit(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _fake_conn():
        yield _Conn()

    from unittest.mock import patch

    import core.db as db

    spec = {"measure": "__epic40_share_of_search__", "window": {"grain": "day"}}
    with patch.object(db, "get_connection", _fake_conn), \
         patch.object(esb, "get_tracked_entity", lambda eid: entity_row):
        created = esb.upsert_source_binding(
            entity_id=entity_row["id"], source="__epic40_opaque_source__", created_by="s",
            external_id="__epic40_ext_id__", query_spec=spec,
        )
        fetched = esb.resolve_source_binding(entity_row["id"], "__epic40_opaque_source__")

    assert created["source"] == "__epic40_opaque_source__"
    assert created["external_id"] == "__epic40_ext_id__"
    assert created["query_spec"] == spec           # opaque JSONB round-trip, verbatim
    assert fetched["query_spec"] == spec
    # org integrity: the binding's org is DERIVED from the entity (never a caller-supplied org).
    assert created["org_id"] == entity_row["org_id"]


# ===========================================================================
# (b) Add-scope preview + no-backfill honesty (40.4) (E40-NFR06 / AD-9).
# ===========================================================================


def _fake_capabilities():
    """A synthetic source capability envelope (a windowed report so history CAN be pulled)."""
    return {
        "reports": [
            {
                "id": "__epic40_report__",
                "quota_cost": {"read_points": 5},
                "incremental": {"mode": "date_window"},
                "supported_grains": [["date"], ["date", "__epic40_dim__"]],
            }
        ]
    }


def test_add_scope_preview_surfaces_cost_cardinality_and_backfill_flag():
    """AC4 (b) -- the 40.4 impact builder surfaces the per-source add-scope cost (quota),
    cardinality estimate, and a backfill flag from the source's declared envelope. Adding a
    brand previews the extra query cost BEFORE activation (never a silent quota multiply)."""
    entity = {"id": "tent___epic40_brand_x__"}
    binding = {
        "id": "esb___epic40__", "entity_id": entity["id"],
        "source": "__epic40_opaque_source__",
        "external_id": "__epic40_ext__",
        "query_spec": {"report_id": "__epic40_report__"},
    }
    impact = esc.build_binding_scope_impact(
        entity, binding, _fake_capabilities(), earliest_pull_date=None
    )
    # The preview surfaces cost + cardinality + a backfill decision, all from the envelope.
    assert impact["estimated_cost"] == {"read_points": 5}
    assert impact["cardinality_estimate"]["grain_width"] == 2
    assert impact["extra_query_count"] == 1          # one outbound driver per bound source
    # A windowed source with no retained history -> backfill is REQUIRED (an explicit decision).
    assert impact["backfill_required"] is True
    assert impact["coverage_state"] == esc.COVERAGE_PARTIAL
    assert impact["blocking_gaps"] == []             # a compatible add (has driver + envelope)


def test_no_backfill_marker_is_honest_absence_never_fabricated_history():
    """AC4 (b) -- the no-backfill honesty seam (E40-NFR06 / AD-9): a `defer` add marks past
    periods with an EXPLICIT typed absence (coverage_state=forward_only, brand_absent_historical
    =True) -- and NEVER a fabricated zero, a back-projected row, or a silent join. The marker is
    a MARKER, not a fact: the warehouse is not read or written."""
    marker = esc.historical_absence_marker("tent___epic40_brand_x__",
                                           project_id="__epic40_projP__")
    assert marker["coverage_state"] == esc.COVERAGE_FORWARD_ONLY
    assert marker["brand_absent_historical"] is True
    # The load-bearing honesty flags: no fabricated / back-projected history.
    assert marker["fabricated_history"] is False
    assert marker["back_projected"] is False


# ===========================================================================
# (c) NON-REGRESSION (E40-NFR06, drift = CRITICAL): registry population wires no new join.
#     Store-level "no warehouse/join I/O" proof (40.1's satisfied-by-inaction, generalized to
#     the 40.2 binding path). The offline slice proves the store touches NO fact/mart table.
# ===========================================================================

# The fact/mart tables the registry must NEVER touch (an untracked datastream reads/writes
# these; if the registry wired a join onto them, an already-correct single-source figure would
# drift). The registry conforms the ENTITY dimension only -- it must be BYTE-INERT on these.
_WAREHOUSE_TABLES = (
    "fact_daily_kpi", "fact_", "mart_", "cross_source_", "rollup",
    "dim_project", "main_marts",
)

_REGISTRY_MODULES = (
    "tracked_entity_registry.py",   # 40.1
    "entity_source_bindings.py",    # 40.2
    "tracked_entity_matching.py",   # 40.3
    "entity_scope_change.py",       # 40.4
    "brand_registry_mcp.py",        # 40.5
)


@pytest.mark.parametrize("module", _REGISTRY_MODULES)
def test_registry_modules_touch_no_fact_or_mart_table(module):
    """AC4 (c) -- CRITICAL: NO 40.x registry module references a fact/mart/rollup warehouse
    table. Declaring a brand / a binding wires NO new join onto an existing datastream -- a
    datastream with NO tracked-brand alignment behaves EXACTLY as before (drift threshold = 0).
    Any warehouse-table reference in a registry module is a potential drift path (CRITICAL)."""
    src = (_CORE_DIR / module).read_text(encoding="utf-8").lower()
    # entity_scope_change carries backfill CONTRACT PATHS (REST route strings) + reuses Epic 12
    # candidate publication by NAME, but must not itself read/write a fact/mart table.
    hits = [t for t in _WAREHOUSE_TABLES if t in src]
    assert not hits, (
        f"{module} references a fact/mart/rollup table {hits} -- a potential untracked-"
        f"datastream drift path (E40-NFR06 CRITICAL)"
    )


def test_registry_stores_import_no_warehouse_or_duckdb_module():
    """AC4 (c) -- CRITICAL: the 40.x stores import NO warehouse/DuckDB/rollup/mart module. The
    registry's only I/O is core.db (Postgres app.* rows) + the socle audit writer. It never
    opens a warehouse cursor, so an untracked datastream's joins are provably untouched
    (E40-NFR06 satisfied by inaction, generalized from 40.1 to the 40.2 binding path)."""
    forbidden_imports = ("core.warehouse", "core.rollup", "core.duckdb", "duckdb",
                         "core.cache_warehouse", "core.marts")
    breaches: list[str] = []
    for module in _REGISTRY_MODULES:
        tree = ast.parse((_CORE_DIR / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            mods: list[str] = []
            if isinstance(node, ast.ImportFrom) and node.module:
                mods.append(node.module)
            elif isinstance(node, ast.Import):
                mods.extend(a.name for a in node.names)
            for m in mods:
                if any(m == f or m.startswith(f + ".") for f in forbidden_imports):
                    breaches.append(f"{module}: import {m}")
    assert not breaches, f"a 40.x store imports a warehouse module (drift path): {breaches}"


# ===========================================================================
# Pg-gated (throwaway DB) -- creating entities/bindings changes no EXISTING count.
# Migrations 049 -> 086 -> 089 applied idempotently; Supabase stays human-gated.
# ===========================================================================


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")

_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"
_MIGRATION_086 = _REPO_ROOT / "infra" / "nango" / "migrations" / "086_tracked_entity_registry.sql"
_MIGRATION_089 = _REPO_ROOT / "infra" / "nango" / "migrations" / "089_entity_source_bindings.sql"


def _apply_migration(conn, path) -> None:
    with conn.cursor() as cur:
        cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _ensure_set_updated_at(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("CREATE SCHEMA IF NOT EXISTS app")
        cur.execute(
            """
            CREATE OR REPLACE FUNCTION app.set_updated_at() RETURNS trigger AS $$
            BEGIN NEW.updated_at = now(); RETURN NEW; END;
            $$ LANGUAGE plpgsql
            """
        )
    conn.commit()


def _prepare(conn) -> None:
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_086)
    _apply_migration(conn, _MIGRATION_089)


def _mk_org(conn, org_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s,%s,%s,'system')",
            (org_id, f"Org-{suffix}", f"org-{suffix}"),
        )
    conn.commit()


def _mk_project(conn, project_id, org_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET org_id = EXCLUDED.org_id",
            (project_id, f"Proj-{suffix}", f"proj-{suffix}", org_id),
        )
    conn.commit()


def _cleanup_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        with clean.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        clean.commit()


# The EXISTING (non-registry) app tables an untracked datastream depends on. Registry
# population must change NONE of their counts (drift threshold exactly 0).
_EXISTING_TABLES = ("app.projects", "app.organizations", "app.metric_definitions",
                    "app.overlap_groups", "app.reconciliation_rules")


@pg_available
def test_registry_population_changes_no_existing_table_count():
    """AC4 (c) -- CRITICAL, pg-gated: creating an entity + a role + per-source bindings changes
    the row-count of the EXISTING (non-registry) tables by EXACTLY 0 (drift threshold = 0). The
    registry writes ONLY to its own tables (tracked_entities / entity_project_roles /
    entity_source_bindings) + the shared append-only audit -- never onto an existing datastream's
    tables. Any non-zero delta is a CRITICAL drift finding that blocks the epic (E40-NFR06)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"__epic40_org_{suffix}__"
    proj_id = f"__epic40_proj_{suffix}__"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)

    def _counts(conn) -> dict:
        out: dict = {}
        with conn.cursor() as cur:
            for table in _EXISTING_TABLES:
                cur.execute(f"SELECT count(*) FROM {table}")  # noqa: S608 (fixed table names)
                out[table] = cur.fetchone()[0]
        return out

    try:
        with get_connection() as conn:
            before = _counts(conn)

        entity = ter.upsert_tracked_entity(
            org_id=org_id, canonical_name=f"__epic40_brand_x_{suffix}__", created_by="s"
        )
        ter.set_entity_project_role(
            entity_id=entity["id"], project_id=proj_id, role="own", created_by="s"
        )
        esb.upsert_source_binding(
            entity_id=entity["id"], source="__epic40_opaque_source__", created_by="s",
            external_id="__epic40_ext__", query_spec={"measure": "__epic40_share_of_search__"},
        )

        with get_connection() as conn:
            after = _counts(conn)

        # NOTE the org/project counts include the two rows THIS test inserted BEFORE the
        # baseline snapshot, so before==after holds: registry population added nothing to them.
        assert after == before, (
            f"registry population drifted an existing table count (E40-NFR06 CRITICAL): "
            f"before={before} after={after}"
        )
    finally:
        _cleanup_org(org_id)
