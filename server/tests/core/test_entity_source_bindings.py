"""Tests for Story 40.2 -- per-source binding, the OUTBOUND collection (Epic 40).

Offline (no DB): binding CRUD, source-agnostic opaque round-trip (external_id + JSONB
query_spec), idempotency, audit shape, the query driver (resolve_source_binding /
list_bindings_for_entity / list_bindings_for_source -- derive the queries), existence-hiding
on a foreign/absent binding id and a foreign/absent entity, org integrity
(binding.org_id == entity.org_id), and the AD-2 "no provider/source/brand name in the module"
grep -- all over an in-memory FAKE store (core.db.get_connection monkeypatched).

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 089, the
scope/status CHECKs, the COALESCE unicity (NULL-safe), FK CASCADE from tracked_entities/org,
the audit reuse of app.metric_semantics_audit (049), the JSONB round-trip, cross-org
existence-hiding and the org-scoping seam on real rows. Migrations 049 then 086 then 089 are
applied idempotently to a throwaway DB (Supabase stays human-gated). Pattern calque sur
test_tracked_entity_registry.py.
"""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import entity_source_bindings as esb  # noqa: E402
from core import tracked_entity_registry as ter  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calque sur test_tracked_entity_registry.py)
# ---------------------------------------------------------------------------


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

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"
_MIGRATION_086 = _REPO_ROOT / "infra" / "nango" / "migrations" / "086_tracked_entity_registry.sql"
_MIGRATION_089 = _REPO_ROOT / "infra" / "nango" / "migrations" / "089_entity_source_bindings.sql"


# ===========================================================================
# Offline -- in-memory FAKE store (no DB)
# ===========================================================================


class _FakeTS:
    """A tiny stand-in for a Postgres TIMESTAMPTZ (has .isoformat() like a datetime)."""

    def __init__(self, label):
        self._label = label

    def isoformat(self):
        return self._label

    def __eq__(self, other):
        return isinstance(other, _FakeTS) and other._label == self._label

    def __hash__(self):
        return hash(self._label)


class _FakeCursor:
    """A minimal cursor over the fake store; supports the exact SQL shapes used by 40.2
    (plus the entity SELECT-by-id used by the imported 40.1 get_tracked_entity)."""

    def __init__(self, store):
        self._store = store
        self._result = None
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):  # noqa: C901 - test double
        params = params or []
        s = " ".join(sql.split())  # collapse whitespace for matching
        self._result = None
        self.description = None

        # -- entity SELECT by id (40.1 get_tracked_entity, imported) --
        if "FROM app.tracked_entities" in s and s.startswith("SELECT"):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            eid = params[0]
            out = [r for r in self._store["entities"].values() if r["id"] == eid]
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- binding SELECTs --
        if "FROM app.entity_source_bindings" in s and s.startswith("SELECT"):
            self.description = [(c.strip(),) for c in esb._BINDING_COLUMNS.split(",")]
            rows = self._store["bindings"]
            out = []
            if " WHERE id = " in s:
                bid = params[0]
                out = [r for r in rows.values() if r["id"] == bid]
            elif "entity_id = %s AND source = %s" in s:
                entity_id, source, account_scope = params
                acc = account_scope or ""
                out = [
                    r for r in rows.values()
                    if r["entity_id"] == entity_id and r["source"] == source
                    and (r["account_scope"] or "") == acc
                ]
            elif "WHERE entity_id = %s" in s:
                entity_id = params[0]
                out = [r for r in rows.values() if r["entity_id"] == entity_id]
                out.sort(key=lambda r: (r["source"], r["account_scope"] or ""))
            else:
                # list_bindings_for_source: WHERE org_id = %s AND source = %s [AND status]
                org_id, source = params[0], params[1]
                status = params[2] if "status = %s" in s else None
                out = [
                    r for r in rows.values()
                    if r["org_id"] == org_id and r["source"] == source
                    and (status is None or r["status"] == status)
                ]
                out.sort(key=lambda r: (r["entity_id"], r["account_scope"] or ""))
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- binding UPSERT --
        if s.startswith("INSERT INTO app.entity_source_bindings"):
            self.description = [(c.strip(),) for c in esb._BINDING_COLUMNS.split(",")]
            import json
            (bid, entity_id, org_id, source, account_scope, external_id, spec_json,
             status, created_by) = params
            query_spec = json.loads(spec_json)
            key = (entity_id, source, account_scope or "")
            existing = self._store["bindings_by_key"].get(key)
            if existing is not None:
                row = self._store["bindings"][existing]
                row.update({
                    "external_id": external_id, "query_spec": query_spec,
                    "status": status, "updated_at": _FakeTS("t1"),
                })
            else:
                row = {
                    "id": bid, "entity_id": entity_id, "org_id": org_id,
                    "scope_level": "ORG", "source": source,
                    "account_scope": account_scope, "external_id": external_id,
                    "query_spec": query_spec, "status": status,
                    "created_by": created_by,
                    "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
                }
                self._store["bindings"][bid] = row
                self._store["bindings_by_key"][key] = bid
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- shared audit --
        if s.startswith("INSERT INTO app.metric_semantics_audit"):
            composed = params[2]
            verb = composed.split(".", 1)[1] if "." in composed else composed
            self._store["audit"].append({
                "action": verb, "composed": composed, "entity_type": params[3],
                "entity_id": params[4], "scope_level": params[5],
                "org_id": params[6], "project_id": params[7],
                "before": params[8], "after": params[9],
            })
            return

        # -- delete --
        if s.startswith("DELETE FROM app.entity_source_bindings"):
            bid = params[0]
            row = self._store["bindings"].pop(bid, None)
            if row is not None:
                self._store["bindings_by_key"].pop(
                    (row["entity_id"], row["source"], row["account_scope"] or ""), None
                )
            return

        raise AssertionError(f"unhandled SQL in fake cursor: {s[:80]}")

    def fetchone(self):
        return self._result[0] if self._result else None

    def fetchall(self):
        return list(self._result or [])


class _FakeConn:
    def __init__(self, store):
        self._store = store

    def cursor(self):
        return _FakeCursor(self._store)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


@pytest.fixture
def fake_db(monkeypatch):
    """Monkeypatch core.db.get_connection to a shared in-memory store."""
    store = {
        "entities": {},
        "bindings": {}, "bindings_by_key": {},
        "audit": [],
    }
    conn = _FakeConn(store)

    @contextmanager
    def _fake_get_connection():
        yield conn

    import core.db as db

    monkeypatch.setattr(db, "get_connection", _fake_get_connection)
    return store


def _seed_entity(store, *, entity_id, org_id, canonical_name="Brand"):
    """Insert a tracked_entity row directly into the fake store (the 40.1 master is a fixture
    here -- 40.2 only reads it via the imported get_tracked_entity)."""
    store["entities"][entity_id] = {
        "id": entity_id, "org_id": org_id, "scope_level": "ORG",
        "canonical_name": canonical_name, "display_name": None, "aliases": [],
        "entity_kind": "brand", "status": "approved", "created_by": "seed",
        "approved_by": None, "approved_at": None,
        "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
    }
    return entity_id


# ===========================================================================
# Offline -- pure helpers
# ===========================================================================


def test_normalize_source_strips_and_guards():
    """_normalize_source is a SHAPE guard (strip + non-empty), never an allow-list."""
    assert esb._normalize_source("  ga4  ") == "ga4"
    with pytest.raises(esb.InvalidSource):
        esb._normalize_source("   ")
    with pytest.raises(esb.InvalidSource):
        esb._normalize_source("")
    with pytest.raises(esb.InvalidSource):
        esb._normalize_source(None)


def test_binding_changed_ignores_volatile_and_stable_jsonb():
    """_binding_changed excludes id/timestamps; an identical query_spec dict is stable."""
    before = {"id": "esb_1", "source": "s", "external_id": "x",
              "query_spec": {"a": 1, "b": 2}, "status": "active",
              "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0")}
    after_same = dict(before, id="esb_2", query_spec={"b": 2, "a": 1},
                      created_at=_FakeTS("t9"), updated_at=_FakeTS("t9"))
    assert esb._binding_changed(before, after_same) is False
    assert esb._binding_changed(None, after_same) is True
    after_diff = dict(before, external_id="y")
    assert esb._binding_changed(before, after_diff) is True


# ===========================================================================
# Offline -- binding CRUD + source-agnostic round-trip (fake store)
# ===========================================================================


def test_upsert_binding_external_id_org_from_entity(fake_db):
    """§1: binding stores org_id FROM the entity, scope_level='ORG', opaque source/external_id
    verbatim; returns an esb_ id + a 'created' audit."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    row = esb.upsert_source_binding(
        entity_id="tent_1", source="some_source", external_id="club-42", created_by="alice"
    )
    assert row["id"].startswith("esb_")
    assert row["org_id"] == "org_1"  # derived from the entity
    assert row["scope_level"] == "ORG"
    assert row["source"] == "some_source"
    assert row["external_id"] == "club-42"
    assert row["query_spec"] == {}
    assert "project_id" not in row
    assert fake_db["audit"][-1]["entity_type"] == "entity_source_binding"
    assert fake_db["audit"][-1]["action"] == "created"


def test_upsert_binding_query_spec_round_trip(fake_db):
    """§2: a free-form JSONB query_spec is stored verbatim and returned byte-for-byte
    (a term / keyword set / handle list all round-trip -- source-agnostic)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    spec = {"terms": ["alpha", "beta"], "handles": {"x": "@brand"}, "n": 3}
    row = esb.upsert_source_binding(
        entity_id="tent_1", source="another_source", query_spec=spec, created_by="alice"
    )
    assert row["query_spec"] == spec
    assert row["external_id"] is None  # query_spec is the driver here


def test_upsert_binding_empty_source_rejected(fake_db):
    """§3: source empty/whitespace -> InvalidSource (offline, before any DB read)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    with pytest.raises(esb.InvalidSource):
        esb.upsert_source_binding(entity_id="tent_1", source="   ", created_by="a")


def test_upsert_binding_bad_status_rejected(fake_db):
    """§3: status not in _STATUSES -> InvalidSource (mirrors the CHECK)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    with pytest.raises(esb.InvalidSource):
        esb.upsert_source_binding(
            entity_id="tent_1", source="s", status="paused", created_by="a"
        )


def test_upsert_binding_unknown_entity_existence_hiding(fake_db):
    """§4: an unknown/foreign entity_id -> EntityNotFound (existence-hiding, imported 40.1)."""
    with pytest.raises(ter.EntityNotFound):
        esb.upsert_source_binding(entity_id="tent_ghost", source="s", created_by="a")
    # EntityNotFound is the SAME symbol re-exported from 40.1.
    assert esb.EntityNotFound is ter.EntityNotFound


def test_upsert_binding_idempotent_no_second_audit(fake_db):
    """§5: re-upsert same (entity, source, account_scope) -> SAME row, no duplicate, and an
    identical re-declare emits NO 'upserted' audit (idempotency; JSONB round-trip stable)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    spec = {"terms": ["alpha"], "k": {"nested": 1}}
    a = esb.upsert_source_binding(
        entity_id="tent_1", source="s", query_spec=spec, created_by="a"
    )
    n = len(fake_db["audit"])
    b = esb.upsert_source_binding(
        entity_id="tent_1", source="s", query_spec=spec, created_by="a"
    )
    assert a["id"] == b["id"]
    assert len(fake_db["bindings"]) == 1
    assert len(fake_db["audit"]) == n  # idempotency: no 'upserted' audit


def test_upsert_binding_change_emits_upserted(fake_db):
    """§6: a changed external_id or query_spec on re-upsert emits exactly one 'upserted'."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", external_id="a", created_by="u"
    )
    n = len(fake_db["audit"])
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", external_id="b", created_by="u"
    )
    assert len(fake_db["audit"]) == n + 1
    assert fake_db["audit"][-1]["action"] == "upserted"


def test_binding_distinct_account_scope_and_source(fake_db):
    """§7: same entity + same source, distinct account_scope -> TWO rows; same entity +
    different source -> TWO rows."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", account_scope="acct_A", created_by="u"
    )
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", account_scope="acct_B", created_by="u"
    )
    esb.upsert_source_binding(entity_id="tent_1", source="other", created_by="u")
    assert len(fake_db["bindings"]) == 3


def test_upsert_binding_org_always_from_entity(fake_db):
    """§14: binding.org_id is set FROM the entity's org, never caller-supplied -- there is no
    org parameter to disagree, so the invariant binding.org_id == entity.org_id holds."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_REAL")
    row = esb.upsert_source_binding(entity_id="tent_1", source="s", created_by="u")
    assert row["org_id"] == "org_REAL"
    assert row["org_id"] == fake_db["entities"]["tent_1"]["org_id"]


# ===========================================================================
# Offline -- the query driver / derive-the-queries (fake store)
# ===========================================================================


def test_resolve_source_binding(fake_db):
    """§8: resolve returns the single opaque spec; an unbound (entity, source) -> None."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", external_id="X", created_by="u"
    )
    got = esb.resolve_source_binding("tent_1", "s")
    assert got["external_id"] == "X"
    assert esb.resolve_source_binding("tent_1", "unbound") is None
    # account_scope discriminates:
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", account_scope="A", external_id="Y", created_by="u"
    )
    assert esb.resolve_source_binding("tent_1", "s", "A")["external_id"] == "Y"


def test_list_bindings_for_entity(fake_db):
    """§9: one brand's binding across all its sources (ordered), and only that entity's."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    _seed_entity(fake_db, entity_id="tent_2", org_id="org_1")
    esb.upsert_source_binding(entity_id="tent_1", source="zeta", created_by="u")
    esb.upsert_source_binding(entity_id="tent_1", source="alpha", created_by="u")
    esb.upsert_source_binding(entity_id="tent_2", source="alpha", created_by="u")
    rows = esb.list_bindings_for_entity("tent_1")
    assert [r["source"] for r in rows] == ["alpha", "zeta"]  # ordered, tent_2 absent
    assert all(r["entity_id"] == "tent_1" for r in rows)


def test_list_bindings_for_source_scoped_to_org(fake_db):
    """§10: list_bindings_for_source(orgA, s) returns every entity's binding for that source
    of org A (the AC2 derive-the-queries surface); org B's bindings are ABSENT."""
    _seed_entity(fake_db, entity_id="tent_A1", org_id="org_A")
    _seed_entity(fake_db, entity_id="tent_A2", org_id="org_A")
    _seed_entity(fake_db, entity_id="tent_B1", org_id="org_B")
    esb.upsert_source_binding(entity_id="tent_A1", source="s", created_by="u")
    esb.upsert_source_binding(entity_id="tent_A2", source="s", created_by="u")
    esb.upsert_source_binding(entity_id="tent_B1", source="s", created_by="u")
    rows = esb.list_bindings_for_source("org_A", "s")
    ids = {r["entity_id"] for r in rows}
    assert ids == {"tent_A1", "tent_A2"}
    assert "tent_B1" not in ids  # scoped to one org


def test_ac2_declare_once_wires_the_query(fake_db):
    """§11 (AC2): declaring one entity once + one binding makes it appear in
    list_bindings_for_source without any per-datastream re-typing (the registry drives it)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", query_spec={"term": "opaque"}, created_by="u"
    )
    driven = esb.list_bindings_for_source("org_1", "s")
    assert len(driven) == 1
    assert driven[0]["query_spec"] == {"term": "opaque"}  # the query, derived from the registry


def test_list_bindings_for_source_status_filter(fake_db):
    """list_bindings_for_source can filter on status (the 40.4 activation seam substrate)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    _seed_entity(fake_db, entity_id="tent_2", org_id="org_1")
    esb.upsert_source_binding(
        entity_id="tent_1", source="s", status="active", created_by="u"
    )
    esb.upsert_source_binding(
        entity_id="tent_2", source="s", status="disabled", created_by="u"
    )
    active = esb.list_bindings_for_source("org_1", "s", status="active")
    assert {r["entity_id"] for r in active} == {"tent_1"}


# ===========================================================================
# Offline -- existence-hiding + org integrity (AC4)
# ===========================================================================


def test_assert_binding_in_org_existence_hiding(fake_db):
    """§12/§13: assert_binding_in_org returns the owning-org row; a foreign org AND an absent
    id both raise EntityNotFound -- INDISTINGUISHABLE (existence-hiding, S-2)."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_owner")
    b = esb.upsert_source_binding(entity_id="tent_1", source="s", created_by="u")
    assert esb.assert_binding_in_org(b["id"], "org_owner")["id"] == b["id"]
    with pytest.raises(ter.EntityNotFound):
        esb.assert_binding_in_org(b["id"], "org_other")
    with pytest.raises(ter.EntityNotFound):
        esb.assert_binding_in_org("esb_does_not_exist", "org_owner")


def test_get_source_binding(fake_db):
    """get_source_binding returns the row by id, or None."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    b = esb.upsert_source_binding(entity_id="tent_1", source="s", created_by="u")
    assert esb.get_source_binding(b["id"])["id"] == b["id"]
    assert esb.get_source_binding("esb_absent") is None


# ===========================================================================
# Offline -- delete + audit shape + AD-2
# ===========================================================================


def test_delete_binding_audits_after_none(fake_db):
    """delete_source_binding audits ('deleted', after=None); a second delete is False."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    b = esb.upsert_source_binding(entity_id="tent_1", source="s", created_by="u")
    assert esb.delete_source_binding(b["id"], identity="u") is True
    assert fake_db["audit"][-1]["action"] == "deleted"
    assert fake_db["audit"][-1]["after"] is None
    assert fake_db["audit"][-1]["before"] is not None
    assert esb.delete_source_binding(b["id"], identity="u") is False  # already gone


def test_audit_shape_scope_and_verbs(fake_db):
    """§15/§16: every mutation emits ONE audit row with entity_type='entity_source_binding',
    action='entity_source_binding.<verb>', scope_level='ORG', org_id=<entity's org>,
    project_id None; create -> before=None; delete -> after=None."""
    _seed_entity(fake_db, entity_id="tent_1", org_id="org_1")
    b = esb.upsert_source_binding(entity_id="tent_1", source="s", created_by="u")
    created = fake_db["audit"][-1]
    assert created["entity_type"] == "entity_source_binding"
    assert created["composed"] == "entity_source_binding.created"
    assert created["scope_level"] == "ORG"
    assert created["org_id"] == "org_1"
    assert created["project_id"] is None
    assert created["before"] is None and created["after"] is not None

    esb.delete_source_binding(b["id"], identity="u")
    deleted = fake_db["audit"][-1]
    assert deleted["composed"] == "entity_source_binding.deleted"
    assert deleted["after"] is None and deleted["before"] is not None


def test_no_provider_or_source_name_in_module():
    """§17 (AD-2): entity_source_bindings.py hard-codes no provider/source/brand name and no
    query-shape keyword outside the STATUS_* constants / CHECK mirror."""
    source = Path(esb.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat",
        # concrete source names the story uses only as prose examples must NOT be baked
        # into the module (they are opaque org DATA, never code):
        "strava", "trends",
        # brand names must be org/project DATA, never code:
        "peugeot", "renault", "citroen", "nike", "adidas", "coca",
    ]
    hits = [name for name in forbidden if name in lowered]
    assert not hits, f"provider/source/brand name(s) hard-coded: {hits}"


def test_status_literals_only_via_constants():
    """AD-2: the status vocabulary appears ONLY through the STATUS_* named constants + the
    frozenset (the CHECK mirror)."""
    assert esb._STATUSES == {esb.STATUS_ACTIVE, esb.STATUS_DISABLED}
    assert esb.STATUS_ACTIVE == "active"
    assert esb.STATUS_DISABLED == "disabled"


def test_socle_symbols_imported_not_forked():
    """§(E40-NFR05/AD3): the socle helpers + the 40.1 entity read/verbs are the SAME symbols
    (imported, not copied) -- a fork would be a distinct object."""
    from core import metric_semantics as ms

    assert esb.validate_scope is ms.validate_scope
    assert esb._mint_id is ms._mint_id
    assert esb._write_semantics_audit is ms._write_semantics_audit
    assert esb.SCOPE_ORG is ms.SCOPE_ORG
    assert esb.get_tracked_entity is ter.get_tracked_entity
    assert esb.ACTION_CREATED is ter.ACTION_CREATED


# ===========================================================================
# Live Postgres -- real DDL, CHECKs, unicity, CASCADE, audit reuse, isolation
# ===========================================================================


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
    """Ensure set_updated_at + 049 (audit) + 086 (entity) + 089 (binding) applied idempotently."""
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


@pg_available
def test_ddl_creates_table_replayable():
    """§18: the table + indexes/trigger exist after 089; replay is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _apply_migration(conn, _MIGRATION_089)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='app' AND table_name='entity_source_bindings'"
            )
            tables = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='app' "
                "AND tablename='entity_source_bindings'"
            )
            idx = {r[0] for r in cur.fetchall()}
    assert tables == {"entity_source_bindings"}
    assert "uq_entity_source_bindings_entity_source" in idx
    assert "ix_entity_source_bindings_org_source" in idx
    assert "ix_entity_source_bindings_query_spec" in idx


@pg_available
def test_scope_and_status_checks():
    """§19: CHECK (scope_level='ORG') and CHECK (status IN ('active','disabled')) rejected."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"esb_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_source_bindings "
                        "(id, entity_id, org_id, scope_level, source, created_by) "
                        "VALUES (%s,%s,%s,'PROJECT','s','system')",
                        (f"esb_{uuid.uuid4().hex}", e["id"], org_id),
                    )
                    conn.rollback()
                    raise AssertionError("scope_level=PROJECT should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_source_bindings "
                        "(id, entity_id, org_id, source, status, created_by) "
                        "VALUES (%s,%s,%s,'s','paused','system')",
                        (f"esb_{uuid.uuid4().hex}", e["id"], org_id),
                    )
                    conn.rollback()
                    raise AssertionError("bad status should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_unicity_coalesce_null_safe():
    """§20: a second (entity, source, NULL account_scope) is rejected (COALESCE NULL-safety);
    a distinct account_scope is accepted."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"esb_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
    try:
        esb.upsert_source_binding(entity_id=e["id"], source="s", created_by="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_source_bindings "
                        "(id, entity_id, org_id, source, created_by) "
                        "VALUES (%s,%s,%s,'s','system')",  # NULL account_scope again
                        (f"esb_{uuid.uuid4().hex}", e["id"], org_id),
                    )
                    conn.rollback()
                    raise AssertionError("duplicate (entity, source, NULL) should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
        # A distinct account_scope is accepted.
        second = esb.upsert_source_binding(
            entity_id=e["id"], source="s", account_scope="acct_X", created_by="s"
        )
        assert second["account_scope"] == "acct_X"
    finally:
        _cleanup_org(org_id)


@pg_available
def test_fk_cascade_entity_and_org():
    """§21: deleting the tracked_entity removes its bindings; deleting the org removes them
    too -- verified by counts."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"esb_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        b = esb.upsert_source_binding(entity_id=e["id"], source="s", created_by="s")
        # Deleting the entity cascades its bindings.
        ter.delete_tracked_entity(e["id"], identity="s")
        assert esb.get_source_binding(b["id"]) is None
        # Deleting the org cascades everything.
        e2 = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"F{suffix}", created_by="s")
        b2 = esb.upsert_source_binding(entity_id=e2["id"], source="s", created_by="s")
        _cleanup_org(org_id)
        assert esb.get_source_binding(b2["id"]) is None
    finally:
        _cleanup_org(org_id)


@pg_available
def test_query_spec_jsonb_round_trip_live():
    """§22: a nested query_spec upserted then re-read is byte-equal, and a re-upsert of the
    identical spec writes NO second audit (the round-trip is idempotent)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"esb_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        spec = {"terms": ["a", "b"], "nested": {"k": [1, 2, {"z": True}]}}
        b = esb.upsert_source_binding(
            entity_id=e["id"], source="s", query_spec=spec, created_by="s"
        )
        assert esb.get_source_binding(b["id"])["query_spec"] == spec
        # Idempotent re-upsert: no second audit row.
        esb.upsert_source_binding(
            entity_id=e["id"], source="s", query_spec=spec, created_by="s"
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.metric_semantics_audit "
                    "WHERE entity_type='entity_source_binding' AND entity_id=%s",
                    (b["id"],),
                )
                assert len(cur.fetchall()) == 1  # one 'created', no idempotent 'upserted'
    finally:
        _cleanup_org(org_id)


@pg_available
def test_audit_reuse_append_only_live():
    """§23: mutations write to app.metric_semantics_audit (049) with
    entity_type='entity_source_binding'; the append-only trigger/REVOKE from 049 still blocks
    UPDATE on those rows (no new audit table)."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"esb_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        b = esb.upsert_source_binding(entity_id=e["id"], source="s", created_by="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.metric_semantics_audit "
                    "WHERE entity_type='entity_source_binding' AND entity_id=%s",
                    (b["id"],),
                )
                audit_ids = [r[0] for r in cur.fetchall()]
                assert len(audit_ids) == 1
                try:
                    cur.execute(
                        "UPDATE app.metric_semantics_audit SET action='x' WHERE id=%s",
                        (audit_ids[0],),
                    )
                    conn.rollback()
                    raise AssertionError("audit UPDATE should be blocked (append-only)")
                except (psycopg.errors.InsufficientPrivilege, psycopg.errors.RaiseException):
                    conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_cross_org_existence_hiding_live():
    """§24 (AC4): assert_binding_in_org(bindingOfOrgA, orgB) raises EntityNotFound, same as an
    absent id (both indistinguishable)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_a, org_b = f"esb_orgA_{suffix}", f"esb_orgB_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_a, f"a{suffix}")
        _mk_org(conn, org_b, f"b{suffix}")
    try:
        e = ter.upsert_tracked_entity(org_id=org_a, canonical_name=f"E{suffix}", created_by="s")
        b = esb.upsert_source_binding(entity_id=e["id"], source="s", created_by="s")
        assert esb.assert_binding_in_org(b["id"], org_a)["id"] == b["id"]
        with pytest.raises(ter.EntityNotFound):
            esb.assert_binding_in_org(b["id"], org_b)
        with pytest.raises(ter.EntityNotFound):
            esb.assert_binding_in_org("esb_absent", org_a)
    finally:
        _cleanup_org(org_a)
        _cleanup_org(org_b)


@pg_available
def test_list_for_source_org_scoped_live():
    """§25: list_bindings_for_source(orgA, s) never returns an org-B binding (org-scoping
    seam on real rows)."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_a, org_b = f"esb_orgA_{suffix}", f"esb_orgB_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_a, f"a{suffix}")
        _mk_org(conn, org_b, f"b{suffix}")
    try:
        ea = ter.upsert_tracked_entity(org_id=org_a, canonical_name=f"A{suffix}", created_by="s")
        eb = ter.upsert_tracked_entity(org_id=org_b, canonical_name=f"B{suffix}", created_by="s")
        esb.upsert_source_binding(entity_id=ea["id"], source="s", created_by="s")
        esb.upsert_source_binding(entity_id=eb["id"], source="s", created_by="s")
        rows_a = esb.list_bindings_for_source(org_a, "s")
        assert {r["entity_id"] for r in rows_a} == {ea["id"]}
        assert eb["id"] not in {r["entity_id"] for r in rows_a}
    finally:
        _cleanup_org(org_a)
        _cleanup_org(org_b)


def _cleanup_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        with clean.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        clean.commit()
