"""Tests for Story 40.1 -- tracked-brand entity master + project role (Epic 40).

Offline (no DB): entity/role CRUD, role-per-project, own-brand-as-entity, idempotency,
audit shape, the confidentiality boundary (no cross-project enumerator, existence-hiding),
alias helpers, and the AD-2 "no provider/brand name in the module" grep -- all over an
in-memory FAKE store (core.db.get_connection monkeypatched).

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 086,
the scope/role CHECKs, the COALESCE/plain unicity, FK CASCADE, the audit reuse of
app.metric_semantics_audit (049), cross-org existence-hiding, cross-project rejection and
the confidentiality boundary on real rows. Pattern calque sur test_dimension_conformance.py.
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

from core import tracked_entity_registry as ter  # noqa: E402

# ---------------------------------------------------------------------------
# Postgres availability check (calque sur test_dimension_conformance.py)
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
    """A minimal cursor over the fake store; supports the exact SQL shapes used."""

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

        # -- projects.org_id lookup (cross-org guard + resolution) --
        if s.startswith("SELECT org_id FROM app.projects"):
            org = self._store["projects"].get(params[0])
            self._result = [(org,)] if org is not None else []
            return

        # -- entity SELECTs --
        if "FROM app.tracked_entities" in s and s.startswith("SELECT"):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            rows = self._store["entities"]
            out = []
            if " WHERE id = " in s:
                eid = params[0]
                out = [r for r in rows.values() if r["id"] == eid]
            elif "org_id = %s AND canonical_name = %s" in s:
                org_id, name = params
                out = [
                    r for r in rows.values()
                    if r["org_id"] == org_id and r["canonical_name"] == name
                ]
            else:
                # list_tracked_entities_by_org: WHERE org_id = %s [AND status = %s]
                org_id = params[0]
                status = params[1] if "status = %s" in s else None
                out = [
                    r for r in rows.values()
                    if r["org_id"] == org_id and (status is None or r["status"] == status)
                ]
                out.sort(key=lambda r: r["canonical_name"])
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- role SELECT with the confidentiality JOIN (list_project_roles) --
        if "FROM app.entity_project_roles r" in s and "JOIN app.tracked_entities e" in s:
            self.description = [
                ("id",), ("entity_id",), ("project_id",), ("role",), ("created_by",),
                ("created_at",), ("updated_at",), ("entity_canonical_name",),
                ("entity_display_name",), ("entity_aliases",), ("entity_status",),
            ]
            project_id = params[0]
            out = []
            for r in self._store["roles"].values():
                if r["project_id"] != project_id:
                    continue
                e = self._store["entities"].get(r["entity_id"], {})
                out.append((
                    r["id"], r["entity_id"], r["project_id"], r["role"], r["created_by"],
                    r["created_at"], r["updated_at"], e.get("canonical_name"),
                    e.get("display_name"), e.get("aliases"), e.get("status"),
                ))
            out.sort(key=lambda t: (t[7] or ""))  # entity_canonical_name
            self._result = out
            return

        # -- plain role SELECTs --
        if "FROM app.entity_project_roles" in s and s.startswith("SELECT"):
            self.description = [(c.strip(),) for c in ter._ROLE_COLUMNS.split(",")]
            rows = self._store["roles"]
            out = []
            if " WHERE id = " in s:
                rid = params[0]
                out = [r for r in rows.values() if r["id"] == rid]
            elif "entity_id = %s AND project_id = %s" in s:
                entity_id, project_id = params
                out = [
                    r for r in rows.values()
                    if r["entity_id"] == entity_id and r["project_id"] == project_id
                ]
            self._result = [tuple(r[c[0]] for c in self.description) for r in out]
            return

        # -- entity UPSERT --
        if s.startswith("INSERT INTO app.tracked_entities"):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            (eid, org_id, canonical_name, display_name, aliases_json, status,
             created_by) = params
            import json
            aliases = json.loads(aliases_json)
            key = (org_id, canonical_name)
            existing = self._store["entities_by_key"].get(key)
            if existing is not None:
                row = self._store["entities"][existing]
                row.update({
                    "display_name": display_name, "aliases": aliases,
                    "status": status, "updated_at": _FakeTS("t1"),
                })
            else:
                row = {
                    "id": eid, "org_id": org_id, "scope_level": "ORG",
                    "canonical_name": canonical_name, "display_name": display_name,
                    "aliases": aliases, "entity_kind": "brand", "status": status,
                    "created_by": created_by, "approved_by": None, "approved_at": None,
                    "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
                }
                self._store["entities"][eid] = row
                self._store["entities_by_key"][key] = eid
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- entity alias UPDATE --
        if s.startswith("UPDATE app.tracked_entities"):
            self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
            import json
            aliases_json, eid = params
            row = self._store["entities"][eid]
            row.update({"aliases": json.loads(aliases_json), "updated_at": _FakeTS("t1")})
            self._result = [tuple(row[c[0]] for c in self.description)]
            return

        # -- role UPSERT --
        if s.startswith("INSERT INTO app.entity_project_roles"):
            self.description = [(c.strip(),) for c in ter._ROLE_COLUMNS.split(",")]
            rid, entity_id, project_id, role, created_by = params
            key = (entity_id, project_id)
            existing = self._store["roles_by_key"].get(key)
            if existing is not None:
                row = self._store["roles"][existing]
                row.update({"role": role, "updated_at": _FakeTS("t1")})
            else:
                row = {
                    "id": rid, "entity_id": entity_id, "project_id": project_id,
                    "role": role, "created_by": created_by,
                    "created_at": _FakeTS("t0"), "updated_at": _FakeTS("t0"),
                }
                self._store["roles"][rid] = row
                self._store["roles_by_key"][key] = rid
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

        # -- deletes --
        if s.startswith("DELETE FROM app.tracked_entities"):
            eid = params[0]
            row = self._store["entities"].pop(eid, None)
            if row is not None:
                self._store["entities_by_key"].pop((row["org_id"], row["canonical_name"]), None)
                # CASCADE: drop the entity's roles.
                for rid in [r for r, rr in self._store["roles"].items()
                            if rr["entity_id"] == eid]:
                    rr = self._store["roles"].pop(rid)
                    self._store["roles_by_key"].pop((rr["entity_id"], rr["project_id"]), None)
            return

        if s.startswith("DELETE FROM app.entity_project_roles"):
            rid = params[0]
            row = self._store["roles"].pop(rid, None)
            if row is not None:
                self._store["roles_by_key"].pop((row["entity_id"], row["project_id"]), None)
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
        "entities": {}, "entities_by_key": {},
        "roles": {}, "roles_by_key": {},
        "projects": {}, "audit": [],
    }
    conn = _FakeConn(store)

    @contextmanager
    def _fake_get_connection():
        yield conn

    import core.db as db

    monkeypatch.setattr(db, "get_connection", _fake_get_connection)
    return store


# ===========================================================================
# Offline -- alias helpers (pure)
# ===========================================================================


def test_normalize_alias_reuses_dimension_conformance():
    """normalize_alias delegates to dimension_conformance.normalize_value (27.4 reuse)."""
    from core.dimension_conformance import normalize_value

    assert ter.normalize_alias("Peugeot") == normalize_value("Peugeot")
    assert ter.normalize_alias("PEUGEOT") == ter.normalize_alias("peugeot")


def test_dedupe_aliases_on_normalized_form():
    """De-dupe on the normalized form, order-stable, keeping the verbatim first string."""
    out = ter._dedupe_aliases(["Peugeot", "peugeot", "PEUGEOT", "Renault"])
    assert out == ["Peugeot", "Renault"]


def test_dedupe_aliases_drops_empty():
    """Whitespace-only aliases (normalize to empty) are dropped."""
    assert ter._dedupe_aliases(["   ", "", "Brand"]) == ["Brand"]


# ===========================================================================
# Offline -- entity master (fake store)
# ===========================================================================


def test_upsert_entity_org_scoped(fake_db):
    """§1: entity stored at scope ORG, org_id NOT NULL, no project_id, tent_ id."""
    row = ter.upsert_tracked_entity(
        org_id="org_1", canonical_name="Peugeot", created_by="alice"
    )
    assert row["id"].startswith("tent_")
    assert row["org_id"] == "org_1"
    assert row["scope_level"] == "ORG"
    assert "project_id" not in row  # entity has no project scope
    assert fake_db["audit"][-1]["entity_type"] == "tracked_entity"
    assert fake_db["audit"][-1]["action"] == "created"


def test_upsert_entity_null_org_rejected(fake_db):
    """§2: validate_scope(SCOPE_ORG, None, None) path -> InvalidScope (org required)."""
    with pytest.raises(ter.InvalidScope):
        ter.upsert_tracked_entity(org_id=None, canonical_name="X", created_by="alice")


def test_upsert_entity_idempotent_same_row_no_second_audit(fake_db):
    """§3: re-upsert same (org, canonical_name) -> SAME row, no duplicate, no extra audit."""
    a = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="alice")
    audit_after_first = len(fake_db["audit"])
    b = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="alice")
    assert a["id"] == b["id"]
    assert len(fake_db["entities"]) == 1
    assert len(fake_db["audit"]) == audit_after_first  # idempotency: no 'upserted' audit


def test_upsert_entity_change_emits_upserted(fake_db):
    """A content change (display_name) on the same key emits an 'upserted' audit."""
    ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="alice")
    n = len(fake_db["audit"])
    ter.upsert_tracked_entity(
        org_id="org_1", canonical_name="Peugeot", created_by="alice", display_name="Peugeot SA"
    )
    assert len(fake_db["audit"]) == n + 1
    assert fake_db["audit"][-1]["action"] == "upserted"


def test_add_alias_appends_and_dedupes(fake_db):
    """§4: add_alias appends + de-dupes on normalized form; re-add is a no-op (no audit)."""
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="alice")
    r1 = ter.add_alias(e["id"], "PSA", identity="alice")
    assert r1["aliases"] == ["PSA"]
    n = len(fake_db["audit"])
    r2 = ter.add_alias(e["id"], "psa", identity="alice")  # same normalized form
    assert r2["aliases"] == ["PSA"]  # no duplicate
    assert len(fake_db["audit"]) == n  # no-op: no audit


def test_remove_alias(fake_db):
    """§5: remove_alias removes + audits; removing an absent alias is a no-op."""
    e = ter.upsert_tracked_entity(
        org_id="org_1", canonical_name="Peugeot", created_by="alice", aliases=["PSA", "Lion"]
    )
    r = ter.remove_alias(e["id"], "psa", identity="alice")  # matched on normalized form
    assert r["aliases"] == ["Lion"]
    n = len(fake_db["audit"])
    r2 = ter.remove_alias(e["id"], "absent", identity="alice")
    assert r2["aliases"] == ["Lion"]
    assert len(fake_db["audit"]) == n  # no-op


def test_list_entities_by_org_scoped_and_ordered(fake_db):
    """§6: list returns only that org's entities, ordered by canonical_name."""
    ter.upsert_tracked_entity(org_id="org_1", canonical_name="Renault", created_by="a")
    ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    ter.upsert_tracked_entity(org_id="org_2", canonical_name="Citroen", created_by="a")
    rows = ter.list_tracked_entities_by_org("org_1")
    names = [r["canonical_name"] for r in rows]
    assert names == ["Peugeot", "Renault"]  # ordered, org_2 absent


def test_assert_entity_in_org_existence_hiding(fake_db):
    """§7: assert_entity_in_org returns the owning-org row; foreign org AND absent id both
    raise EntityNotFound -- INDISTINGUISHABLE (existence-hiding, S-2)."""
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="alice")
    assert ter.assert_entity_in_org(e["id"], "org_1")["id"] == e["id"]
    with pytest.raises(ter.EntityNotFound):
        ter.assert_entity_in_org(e["id"], "org_OTHER")
    with pytest.raises(ter.EntityNotFound):
        ter.assert_entity_in_org("tent_does_not_exist", "org_1")


def test_delete_entity_audits_and_cascades_roles(fake_db):
    """delete_tracked_entity audits ('deleted', after=None) and CASCADE-drops its roles."""
    fake_db["projects"]["proj_1"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_1", role="own", created_by="a"
    )
    assert ter.delete_tracked_entity(e["id"], identity="a") is True
    assert fake_db["audit"][-1]["action"] == "deleted"
    assert fake_db["audit"][-1]["after"] is None
    assert fake_db["roles"] == {}  # CASCADE
    assert ter.delete_tracked_entity(e["id"], identity="a") is False  # already gone


# ===========================================================================
# Offline -- project role + own-brand-first-class (fake store)
# ===========================================================================


def test_role_per_project_same_entity_different_role(fake_db):
    """§8 (AC2): the SAME entity is 'own' for project P and 'competitor' for project Q --
    TWO rows, distinct project_id."""
    fake_db["projects"]["proj_P"] = "org_1"
    fake_db["projects"]["proj_Q"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    rp = ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_P", role="own", created_by="a"
    )
    rq = ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_Q", role="competitor", created_by="a"
    )
    assert rp["id"] != rq["id"]
    assert rp["role"] == "own" and rq["role"] == "competitor"
    assert rp["id"].startswith("epr_")


def test_own_brand_is_first_class_entity(fake_db):
    """§9 (AC2): the client's OWN brand is an ordinary entity + an 'own' role -- no special
    table, no special flag."""
    fake_db["projects"]["proj_1"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="MyBrand", created_by="a")
    r = ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_1", role="own", created_by="a"
    )
    # It is the very same tracked_entity row, only distinguished by the role value.
    assert ter.get_tracked_entity(e["id"])["canonical_name"] == "MyBrand"
    assert r["role"] == "own"


def test_reset_role_upserts_and_idempotent(fake_db):
    """§10: re-set (entity, project) to a NEW role UPSERTs (one 'upserted' audit); re-set
    to the SAME role emits no audit (idempotency)."""
    fake_db["projects"]["proj_1"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_1", role="competitor", created_by="a"
    )
    n = len(fake_db["audit"])
    ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_1", role="reference", created_by="a"
    )
    assert len(fake_db["audit"]) == n + 1
    assert fake_db["audit"][-1]["action"] == "upserted"
    assert len(fake_db["roles"]) == 1  # same row
    m = len(fake_db["audit"])
    ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_1", role="reference", created_by="a"
    )
    assert len(fake_db["audit"]) == m  # idempotent: no audit


def test_set_role_invalid_literal_before_db(fake_db):
    """§11: an unknown role literal -> InvalidRole (offline, before any DB access)."""
    with pytest.raises(ter.InvalidRole):
        ter.set_entity_project_role(
            entity_id="tent_x", project_id="proj_1", role="frenemy", created_by="a"
        )


def test_set_role_cross_org_denied(fake_db):
    """§12 (AC4): entity.org_id != project.org_id -> CrossProjectDenied; no row written."""
    fake_db["projects"]["proj_B"] = "org_B"
    e = ter.upsert_tracked_entity(org_id="org_A", canonical_name="Peugeot", created_by="a")
    with pytest.raises(ter.CrossProjectDenied):
        ter.set_entity_project_role(
            entity_id=e["id"], project_id="proj_B", role="competitor", created_by="a"
        )
    assert fake_db["roles"] == {}


def test_set_role_unknown_entity_existence_hiding(fake_db):
    """§13: an unknown entity -> EntityNotFound (existence-hiding)."""
    fake_db["projects"]["proj_1"] = "org_1"
    with pytest.raises(ter.EntityNotFound):
        ter.set_entity_project_role(
            entity_id="tent_ghost", project_id="proj_1", role="own", created_by="a"
        )


def test_set_role_unknown_project_existence_hiding(fake_db):
    """An unknown project -> EntityNotFound (existence-hiding, never leaks existence)."""
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    with pytest.raises(ter.EntityNotFound):
        ter.set_entity_project_role(
            entity_id=e["id"], project_id="proj_ghost", role="own", created_by="a"
        )


# ===========================================================================
# Offline -- confidentiality boundary (AC3, E40-NFR01)
# ===========================================================================


def test_list_project_roles_only_own_rows(fake_db):
    """§14 (AC3): list_project_roles(P) returns ONLY P's roled entities; Q's are ABSENT
    even though both projects belong to the SAME org."""
    fake_db["projects"]["proj_P"] = "org_1"
    fake_db["projects"]["proj_Q"] = "org_1"
    ep = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    eq = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Renault", created_by="a")
    ter.set_entity_project_role(
        entity_id=ep["id"], project_id="proj_P", role="own", created_by="a"
    )
    ter.set_entity_project_role(
        entity_id=eq["id"], project_id="proj_Q", role="competitor", created_by="a"
    )
    rows_p = ter.list_project_roles("proj_P")
    names_p = [r["entity_canonical_name"] for r in rows_p]
    assert names_p == ["Peugeot"]
    assert "Renault" not in names_p  # sibling project's brand is confidential


def test_no_cross_project_enumerator_exists():
    """§15: the module exposes NO 'list_projects_tracking_entity'-style project-facing twin
    (confidentiality by construction). The reverse index is org-internal only."""
    public = {name for name in dir(ter) if not name.startswith("_")}
    forbidden_substrings = ("projects_tracking", "tracking_entity", "siblings", "all_projects")
    leaks = [n for n in public if any(f in n.lower() for f in forbidden_substrings)]
    assert not leaks, f"a cross-project enumerator leaked: {leaks}"


def test_assert_role_visible_to_project_existence_hiding(fake_db):
    """§16: assert_role_visible_to_project(roleQ, P) -> EntityNotFound (a sibling's role id
    is indistinguishable from absent -- no probing by guessing ids)."""
    fake_db["projects"]["proj_P"] = "org_1"
    fake_db["projects"]["proj_Q"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    rq = ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_Q", role="competitor", created_by="a"
    )
    assert ter.assert_role_visible_to_project(rq["id"], "proj_Q")["id"] == rq["id"]
    with pytest.raises(ter.EntityNotFound):
        ter.assert_role_visible_to_project(rq["id"], "proj_P")  # sibling -> hidden
    with pytest.raises(ter.EntityNotFound):
        ter.assert_role_visible_to_project("epr_ghost", "proj_P")  # absent -> same error


def test_resolve_entity_for_project_view(fake_db):
    """§17: resolve returns P's role view; a foreign-org entity -> None (invisible)."""
    fake_db["projects"]["proj_P"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_P", role="competitor", created_by="a"
    )
    view = ter.resolve_entity_for_project("proj_P", e["id"])
    assert view["role"] == "competitor"
    assert view["entity"]["canonical_name"] == "Peugeot"
    # A foreign-org entity is invisible to the project (no leak).
    foreign = ter.upsert_tracked_entity(org_id="org_2", canonical_name="Citroen", created_by="a")
    assert ter.resolve_entity_for_project("proj_P", foreign["id"]) is None


def test_resolve_entity_visible_but_unroled(fake_db):
    """An org entity the project has NOT roled resolves with role=None (visible-but-unroled)."""
    fake_db["projects"]["proj_P"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    view = ter.resolve_entity_for_project("proj_P", e["id"])
    assert view is not None
    assert view["role"] is None


def test_resolve_entity_unknown_project_fail_soft(fake_db):
    """Fail-soft on an unknown project -> None (never a crash)."""
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    assert ter.resolve_entity_for_project("proj_ghost", e["id"]) is None


# ===========================================================================
# Offline -- audit shape + AD-2
# ===========================================================================


def test_audit_shape_create_delete(fake_db):
    """§18/§19: every mutation emits ONE audit row; create -> before=None; delete ->
    after=None; the entity_type is one of the two new values; action = '<verb>'."""
    fake_db["projects"]["proj_1"] = "org_1"
    e = ter.upsert_tracked_entity(org_id="org_1", canonical_name="Peugeot", created_by="a")
    created = fake_db["audit"][-1]
    assert created["entity_type"] == "tracked_entity"
    assert created["composed"] == "tracked_entity.created"
    assert created["before"] is None and created["after"] is not None

    ter.set_entity_project_role(
        entity_id=e["id"], project_id="proj_1", role="own", created_by="a"
    )
    role_audit = fake_db["audit"][-1]
    assert role_audit["entity_type"] == "entity_project_role"
    assert role_audit["composed"] == "entity_project_role.created"

    ter.delete_tracked_entity(e["id"], identity="a")
    deleted = fake_db["audit"][-1]
    assert deleted["action"] == "deleted"
    assert deleted["after"] is None and deleted["before"] is not None


def test_no_provider_or_brand_name_in_module():
    """§20 (AD-2): tracked_entity_registry.py hard-codes no provider/brand name and no role
    literal outside the ROLE_* constants / CHECK mirror."""
    source = Path(ter.__file__).read_text(encoding="utf-8")
    lowered = source.lower()
    forbidden = [
        "google-analytics", "google_analytics", "meta-ads", "meta_ads",
        "tiktok", "linkedin", "shopify", "stripe", "adjust",
        "facebook", "pinterest", "amazon", "microsoft", "snapchat",
        # brand names must be org/project DATA, never code:
        "peugeot", "renault", "citroen", "nike", "adidas", "coca",
    ]
    hits = [name for name in forbidden if name in lowered]
    assert not hits, f"provider/brand name(s) hard-coded: {hits}"


def test_role_literals_only_via_constants():
    """AD-2: the role vocabulary appears ONLY through the ROLE_* named constants + the
    frozenset (the CHECK mirror), never as scattered bare literals in logic."""
    assert ter._ROLES == {ter.ROLE_OWN, ter.ROLE_COMPETITOR, ter.ROLE_REFERENCE}
    assert ter.ROLE_OWN == "own"
    assert ter.ROLE_COMPETITOR == "competitor"
    assert ter.ROLE_REFERENCE == "reference"


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
    """Ensure set_updated_at + 049 (audit) + 086 applied idempotently."""
    _ensure_set_updated_at(conn)
    _apply_migration(conn, _MIGRATION_049)
    _apply_migration(conn, _MIGRATION_086)


def _mk_org(conn, org_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s,%s,%s,'system')",
            (org_id, f"Org-{suffix}", f"org-{suffix}"),
        )
    conn.commit()


def _mk_project(conn, project_id, org_id, suffix) -> None:
    # app.projects predates 035; org_id was added there. Insert the minimum + org_id.
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET org_id = EXCLUDED.org_id",
            (project_id, f"Proj-{suffix}", f"proj-{suffix}", org_id),
        )
    conn.commit()


@pg_available
def test_ddl_creates_tables_replayable():
    """§21: both tables + indexes/triggers exist after 086; replay is a no-op."""
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _apply_migration(conn, _MIGRATION_086)  # replay must not error
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='app' AND table_name IN "
                "('tracked_entities','entity_project_roles')"
            )
            tables = {r[0] for r in cur.fetchall()}
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='app' "
                "AND tablename IN ('tracked_entities','entity_project_roles')"
            )
            idx = {r[0] for r in cur.fetchall()}
    assert tables == {"tracked_entities", "entity_project_roles"}
    assert "uq_tracked_entities_org_name" in idx
    assert "uq_entity_project_roles_entity_project" in idx


@pg_available
def test_scope_check_org_only():
    """§22: CHECK (scope_level='ORG') rejects a non-ORG scope_level on tracked_entities."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"te_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        try:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.tracked_entities "
                        "(id, org_id, scope_level, canonical_name, created_by) "
                        "VALUES (%s,%s,'PROJECT','X','system')",
                        (f"tent_{uuid.uuid4().hex}", org_id),
                    )
                    conn.rollback()
                    raise AssertionError("scope_level=PROJECT should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
        finally:
            _cleanup_org(org_id)


@pg_available
def test_role_check_enforced():
    """§23: CHECK (role IN ('own','competitor','reference')) rejects a bad role."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"te_org_{suffix}", f"te_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_project_roles "
                        "(id, entity_id, project_id, role, created_by) "
                        "VALUES (%s,%s,%s,'frenemy','system')",
                        (f"epr_{uuid.uuid4().hex}", e["id"], proj_id),
                    )
                    conn.rollback()
                    raise AssertionError("bad role should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_unicity_entity_and_role():
    """§24: a second (org, canonical_name) and a second (entity, project) are rejected."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"te_org_{suffix}", f"te_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        # A raw duplicate entity insert (same org, canonical_name) is rejected.
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.tracked_entities "
                        "(id, org_id, canonical_name, created_by) VALUES (%s,%s,%s,'system')",
                        (f"tent_{uuid.uuid4().hex}", org_id, f"E{suffix}"),
                    )
                    conn.rollback()
                    raise AssertionError("duplicate entity should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
        # A duplicate role (entity, project) is rejected.
        ter.set_entity_project_role(
            entity_id=e["id"], project_id=proj_id, role="own", created_by="s"
        )
        with get_connection() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_project_roles "
                        "(id, entity_id, project_id, role, created_by) "
                        "VALUES (%s,%s,%s,'competitor','system')",
                        (f"epr_{uuid.uuid4().hex}", e["id"], proj_id),
                    )
                    conn.rollback()
                    raise AssertionError("duplicate role should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
    finally:
        _cleanup_org(org_id)


@pg_available
def test_fk_cascade():
    """§25: deleting an entity drops its roles; deleting an org drops its entities."""
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id, proj_id = f"te_org_{suffix}", f"te_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        ter.set_entity_project_role(
            entity_id=e["id"], project_id=proj_id, role="own", created_by="s"
        )
        # Deleting the entity cascades its roles.
        ter.delete_tracked_entity(e["id"], identity="s")
        assert ter.get_entity_project_role(e["id"], proj_id) is None
        # Deleting the org cascades its entities.
        e2 = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"F{suffix}", created_by="s")
        _cleanup_org(org_id)
        assert ter.get_tracked_entity(e2["id"]) is None
    finally:
        _cleanup_org(org_id)


@pg_available
def test_cross_org_existence_hiding_live():
    """§26 (AC4): assert_entity_in_org(entityA, orgB) raises EntityNotFound, same as absent."""
    suffix = uuid.uuid4().hex[:8]
    org_a, org_b = f"te_orgA_{suffix}", f"te_orgB_{suffix}"
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_a, f"a{suffix}")
        _mk_org(conn, org_b, f"b{suffix}")
    try:
        e = ter.upsert_tracked_entity(org_id=org_a, canonical_name=f"E{suffix}", created_by="s")
        assert ter.assert_entity_in_org(e["id"], org_a)["id"] == e["id"]
        with pytest.raises(ter.EntityNotFound):
            ter.assert_entity_in_org(e["id"], org_b)
        with pytest.raises(ter.EntityNotFound):
            ter.assert_entity_in_org("tent_absent", org_a)
    finally:
        _cleanup_org(org_a)
        _cleanup_org(org_b)


@pg_available
def test_cross_project_rejection_live():
    """§27 (AC4): set role for (entityOfOrgA, projectOfOrgB) -> CrossProjectDenied; no row."""
    suffix = uuid.uuid4().hex[:8]
    org_a, org_b = f"te_orgA_{suffix}", f"te_orgB_{suffix}"
    proj_b = f"te_projB_{suffix}"
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_a, f"a{suffix}")
        _mk_org(conn, org_b, f"b{suffix}")
        _mk_project(conn, proj_b, org_b, f"b{suffix}")
    try:
        e = ter.upsert_tracked_entity(org_id=org_a, canonical_name=f"E{suffix}", created_by="s")
        with pytest.raises(ter.CrossProjectDenied):
            ter.set_entity_project_role(
                entity_id=e["id"], project_id=proj_b, role="competitor", created_by="s"
            )
        assert ter.get_entity_project_role(e["id"], proj_b) is None
    finally:
        _cleanup_org(org_a)
        _cleanup_org(org_b)


@pg_available
def test_confidentiality_live():
    """§28 (E40-NFR01): list_project_roles(P) never returns a row of Q (same org)."""
    suffix = uuid.uuid4().hex[:8]
    org_id = f"te_org_{suffix}"
    proj_p, proj_q = f"te_projP_{suffix}", f"te_projQ_{suffix}"
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_p, org_id, f"p{suffix}")
        _mk_project(conn, proj_q, org_id, f"q{suffix}")
    try:
        ep = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"P{suffix}", created_by="s")
        eq = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"Q{suffix}", created_by="s")
        ter.set_entity_project_role(
            entity_id=ep["id"], project_id=proj_p, role="own", created_by="s"
        )
        ter.set_entity_project_role(
            entity_id=eq["id"], project_id=proj_q, role="competitor", created_by="s"
        )
        rows_p = ter.list_project_roles(proj_p)
        assert {r["entity_id"] for r in rows_p} == {ep["id"]}
        assert eq["id"] not in {r["entity_id"] for r in rows_p}
    finally:
        _cleanup_org(org_id)


@pg_available
def test_audit_reuse_live():
    """§29: mutations write to app.metric_semantics_audit (049) with the two new
    entity_types; the append-only trigger/REVOKE still blocks UPDATE/DELETE on those rows.
    §30: re-upsert identical entity -> stable count, no extra audit."""
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"te_org_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
    try:
        e = ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        # Idempotent re-upsert: no extra audit, stable row count.
        ter.upsert_tracked_entity(org_id=org_id, canonical_name=f"E{suffix}", created_by="s")
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.metric_semantics_audit "
                    "WHERE entity_type='tracked_entity' AND entity_id=%s",
                    (e["id"],),
                )
                audit_ids = [r[0] for r in cur.fetchall()]
                assert len(audit_ids) == 1  # one 'created', no idempotent 'upserted'
                # Append-only: UPDATE/DELETE on the audit row is blocked (049 REVOKE/trigger).
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


def _cleanup_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        with clean.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        clean.commit()
