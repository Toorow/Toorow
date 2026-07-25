"""Story 40.6 -- isolation / confidentiality suite (Epic 40 acceptance gate, AC2 / E40-NFR01).

Org-anchor / project-role isolation + the confidentiality boundary, driven on the MULTI-PROJECT
multi-source fixture matrix (Dev Notes A):
  * orgA holds two SIBLING projects P and Q; orgB holds a project R.
  * the SAME entity `__epic40_brand_x__` is roled `own` in P and `competitor` in Q.
  * a distinct entity is roled only by R (orgB) for the cross-org slice.

The invariants (a leak is CRITICAL and blocks the epic):
  (a) same-org confidentiality: list_project_roles(P) returns ONLY P's roled entities -- Q's
      and R's are ABSENT even though P and Q share an org; NO cross-project enumerator exists.
  (b) existence-hiding: assert_role_visible_to_project(roleQ, P) and
      assert_entity_in_org(entityOrgR, orgA) each raise EntityNotFound -- a sibling/foreign id
      is INDISTINGUISHABLE from absent (404-semantic, NOT a 403 that leaks existence).
  (c) cross-project role mutation: set_entity_project_role(entityOrgP, projectOrgR) ->
      CrossProjectDenied; no row written.
  (d) the 40.5 governance-surface cross-project mutation refusal (seam re-exercise -- 40.6
      adds NO new endpoint).

Offline (fake-store, extends 40.1's harness) + a pg-gated slice on REAL rows. Any cross-project
enumeration, existence disclosure, or a successful cross-project read/write is a CRITICAL
finding. Pattern calque sur test_tracked_entity_registry.py.
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

# Reuse the LANDED 40.1 fake-store cursor/conn verbatim (single source of truth -- do NOT fork
# the SQL doubles). Importing the test module gives us _FakeConn / _FakeCursor / _FakeTS.
from tests.core.test_tracked_entity_registry import (  # noqa: E402
    _FakeConn,
)

# ---------------------------------------------------------------------------
# The multi-project multi-source fixture matrix (Dev Notes A). Namespaced `__epic40_*__` so it
# can NEVER pollute a production mart/rollup (39.9 discipline). Fully synthetic -- NO connector
# test accounts (MEMORY: no-connector-test-accounts).
# ---------------------------------------------------------------------------

ORG_A = "__epic40_orgA__"      # holds sibling projects P and Q
ORG_B = "__epic40_orgB__"      # holds project R
PROJ_P = "__epic40_projP__"    # sibling of Q (orgA) -- roles the entity `own`
PROJ_Q = "__epic40_projQ__"    # sibling of P (orgA) -- roles the SAME entity `competitor`
PROJ_R = "__epic40_projR__"    # a different org (orgB) -- the cross-org slice

BRAND_X = "__epic40_brand_x__"      # roled own in P AND competitor in Q (same entity)
BRAND_R = "__epic40_brand_r__"      # roled only by R (orgB) -- cross-org disjointness


@pytest.fixture
def matrix(monkeypatch):
    """Populate the P/Q/R fixture matrix over the 40.1 fake store and return the handles.

    Returns a dict with the org/project ids + the two entities + the P/Q role ids so the tests
    can drive the confidentiality assertions by name."""
    store = {
        "entities": {}, "entities_by_key": {},
        "roles": {}, "roles_by_key": {},
        "projects": {ORG_A: None}, "audit": [],  # placeholder; projects filled below
    }
    # projects map is project_id -> org_id (mirrors the fake cursor's projects lookup).
    store["projects"] = {PROJ_P: ORG_A, PROJ_Q: ORG_A, PROJ_R: ORG_B}
    conn = _FakeConn(store)

    @contextmanager
    def _fake_get_connection():
        yield conn

    import core.db as db

    monkeypatch.setattr(db, "get_connection", _fake_get_connection)

    # orgA's shared entity, roled own in P and competitor in Q (E40-AD1: same entity, 2 roles).
    ex = ter.upsert_tracked_entity(org_id=ORG_A, canonical_name=BRAND_X, created_by="s")
    role_p = ter.set_entity_project_role(
        entity_id=ex["id"], project_id=PROJ_P, role="own", created_by="s"
    )
    role_q = ter.set_entity_project_role(
        entity_id=ex["id"], project_id=PROJ_Q, role="competitor", created_by="s"
    )
    # orgB's disjoint entity, roled only by R.
    er = ter.upsert_tracked_entity(org_id=ORG_B, canonical_name=BRAND_R, created_by="s")
    role_r = ter.set_entity_project_role(
        entity_id=er["id"], project_id=PROJ_R, role="own", created_by="s"
    )
    return {
        "store": store,
        "entity_x": ex, "entity_r": er,
        "role_p": role_p, "role_q": role_q, "role_r": role_r,
    }


# ===========================================================================
# (a) Same-org confidentiality -- P vs Q (a leak here is CRITICAL).
# ===========================================================================


def test_list_project_roles_p_excludes_sibling_q_and_foreign_r(matrix):
    """AC2 (a) -- CRITICAL: list_project_roles(P) returns ONLY P's roled entities. Q's role of
    the SAME entity and R's foreign-org role are ABSENT, even though P and Q share orgA. A leak
    of a sibling project's tracked brand is CRITICAL and blocks the epic."""
    rows_p = ter.list_project_roles(PROJ_P)
    assert {r["entity_id"] for r in rows_p} == {matrix["entity_x"]["id"]}
    assert all(r["project_id"] == PROJ_P for r in rows_p)
    assert all(r["role"] == "own" for r in rows_p)  # P's view, not Q's 'competitor'

    rows_q = ter.list_project_roles(PROJ_Q)
    assert {r["entity_id"] for r in rows_q} == {matrix["entity_x"]["id"]}
    assert all(r["role"] == "competitor" for r in rows_q)  # Q's view, distinct from P

    # R (orgB) never surfaces the orgB entity to an orgA project and vice-versa.
    rows_r = ter.list_project_roles(PROJ_R)
    assert {r["entity_id"] for r in rows_r} == {matrix["entity_r"]["id"]}
    assert matrix["entity_r"]["id"] not in {r["entity_id"] for r in rows_p}


def test_no_cross_project_enumerator_exists():
    """AC2 (c) -- CRITICAL: the module exposes NO project-facing 'list_projects_tracking_entity'
    twin. Confidentiality is enforced BY CONSTRUCTION: there is provably no store function that
    returns 'entities another project tracks'. (Extends 40.1's §15 to the epic gate.)"""
    public = {name for name in dir(ter) if not name.startswith("_")}
    forbidden = ("projects_tracking", "tracking_entity", "siblings", "all_projects",
                 "cross_project", "by_entity")
    leaks = [n for n in public if any(f in n.lower() for f in forbidden)]
    assert not leaks, f"a cross-project enumerator leaked: {leaks}"


# ===========================================================================
# (b) Existence-hiding -- a sibling/foreign id is indistinguishable from absent (CRITICAL).
# ===========================================================================


def test_role_visible_existence_hiding_sibling_project(matrix):
    """AC2 (b) -- CRITICAL: assert_role_visible_to_project(roleQ, P) raises EntityNotFound -- a
    sibling project's role id is INDISTINGUISHABLE from absent (P cannot probe which entities Q
    tracks by guessing role ids). The SAME error as a truly-absent id (404-semantic, not a 403
    that leaks existence -- lesson IDOR 27.2 F-1)."""
    role_q_id = matrix["role_q"]["id"]
    # Visible to its OWN project ...
    assert ter.assert_role_visible_to_project(role_q_id, PROJ_Q)["id"] == role_q_id
    # ... hidden from the sibling P (existence-hiding) ...
    with pytest.raises(ter.EntityNotFound):
        ter.assert_role_visible_to_project(role_q_id, PROJ_P)
    # ... and an absent id raises the SAME typed error (indistinguishable).
    with pytest.raises(ter.EntityNotFound):
        ter.assert_role_visible_to_project("epr___epic40_ghost__", PROJ_P)


def test_entity_in_org_existence_hiding_cross_org(matrix):
    """AC2 (b) -- CRITICAL: assert_entity_in_org(entityOfOrgR, orgA) raises EntityNotFound -- a
    foreign-org entity is INDISTINGUISHABLE from absent. orgA cannot probe orgB's brands."""
    entity_r_id = matrix["entity_r"]["id"]
    # Visible to its OWN org ...
    assert ter.assert_entity_in_org(entity_r_id, ORG_B)["id"] == entity_r_id
    # ... hidden from orgA (foreign-org -> existence-hiding) ...
    with pytest.raises(ter.EntityNotFound):
        ter.assert_entity_in_org(entity_r_id, ORG_A)
    # ... same typed error as an absent id.
    with pytest.raises(ter.EntityNotFound):
        ter.assert_entity_in_org("tent___epic40_ghost__", ORG_A)


def test_resolve_entity_for_project_hides_foreign_org_entity(matrix):
    """AC2 (b) -- CRITICAL: resolve_entity_for_project(P, entityOfOrgR) returns None (invisible),
    never a leak of orgB's entity or its role. A project's VIEW never crosses the org anchor."""
    # P resolves its OWN entity with its role ...
    view = ter.resolve_entity_for_project(PROJ_P, matrix["entity_x"]["id"])
    assert view is not None and view["role"] == "own"
    # ... but the orgB entity is invisible to an orgA project (no cross-org leak).
    assert ter.resolve_entity_for_project(PROJ_P, matrix["entity_r"]["id"]) is None


# ===========================================================================
# (c) Cross-project mutation -- CrossProjectDenied, no row written (CRITICAL).
# ===========================================================================


def test_cross_project_role_mutation_denied_no_write(matrix):
    """AC2 (d) -- CRITICAL: set_entity_project_role(entityOfOrgA, projectOfOrgR) ->
    CrossProjectDenied (a project can only role an entity of ITS own org); no row is written."""
    roles_before = dict(matrix["store"]["roles"])
    with pytest.raises(ter.CrossProjectDenied):
        ter.set_entity_project_role(
            entity_id=matrix["entity_x"]["id"],   # orgA entity
            project_id=PROJ_R,                     # orgB project
            role="competitor", created_by="s",
        )
    # No row written (the store is unchanged).
    assert matrix["store"]["roles"] == roles_before


# ===========================================================================
# (d) The 40.5 governance-surface cross-project mutation refusal (seam RE-EXERCISE).
#     40.6 adds NO new endpoint -- this re-exercises the delivered 40.5 tool's guard path.
#     Skip-guarded on the presence of the 40.5 delivered surface.
# ===========================================================================


def _brand_registry_mcp_available() -> bool:
    try:
        import core.brand_registry_mcp  # noqa: F401
        return True
    except Exception:
        return False


brand_mcp_available = pytest.mark.skipif(
    not _brand_registry_mcp_available(),
    reason="40.5 brand_registry_mcp surface not landed (skip-guard)",
)


@brand_mcp_available
def test_governance_surface_cross_project_role_set_denied(monkeypatch):
    """AC2 (d) -- CRITICAL: the 40.5 registry_role_set tool surfaces the store's
    CrossProjectDenied as a typed cross_project_denied refusal (no leak of the sibling project).
    A RE-EXERCISE of the 40.5 seam -- 40.6 adds NO new endpoint. The store guard is the same one
    the offline test above proves; here we prove the SURFACE maps it to a typed refusal."""
    import json

    from core import brand_registry_mcp as brm

    # Capture the registered handlers (mirror test_epic40_brand_registry_mcp._register_all).
    from core import mcp_profiles
    from core import tracked_entity_registry as reg

    handlers: dict = {}
    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        handlers[handler.__name__] = handler
        return real(mcp, handler, **kwargs)

    monkeypatch.setattr(mcp_profiles, "register_profiled", spy)
    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()

    class _Recorder:
        def __init__(self):
            self.tool = None

    from unittest.mock import MagicMock

    rec = _Recorder()
    rec.tool = MagicMock()
    brm.register(rec)

    # Guards pass (authorised org/project) -- the CrossProjectDenied comes from the STORE.
    monkeypatch.setattr(brm, "_identity", lambda: "user-1")
    monkeypatch.setattr(brm, "_guard_org_manage", lambda org, ident: None)
    monkeypatch.setattr(brm, "_assert_project_in_org", lambda project, org: None)

    def raise_cpd(**kwargs):
        raise reg.CrossProjectDenied("entity org != project org")

    # The entity IS in ORG_A (org-of-record), so the new existence-hiding guard passes; the
    # refusal comes from the STORE because PROJ_R belongs to another org (cross-PROJECT, not a
    # foreign entity). A genuinely foreign entity is existence-hidden to not_found upstream.
    monkeypatch.setattr(reg, "assert_entity_in_org", lambda e, o: None)
    monkeypatch.setattr(reg, "set_entity_project_role", raise_cpd)

    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        handlers["registry_role_set"](matrix_entity := "tent___epic40_brand_x__",
                                      PROJ_R, ORG_A, "competitor")
    assert json.loads(str(exc.value))["code"] == "cross_project_denied"
    _ = matrix_entity  # silence unused-name lint on the walrus binding
    mcp_profiles.reset_registry_for_tests()


# ===========================================================================
# Pg-gated (throwaway DB) -- the confidentiality boundary on REAL rows.
# Migrations 049 -> 086 applied idempotently; Supabase stays human-gated.
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

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION_049 = _REPO_ROOT / "infra" / "nango" / "migrations" / "049_metric_semantics.sql"
_MIGRATION_086 = _REPO_ROOT / "infra" / "nango" / "migrations" / "086_tracked_entity_registry.sql"


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


@pg_available
def test_confidentiality_and_isolation_live_multiproject():
    """AC2 (a)/(b)/(c)/(d) on REAL rows -- CRITICAL. The P/Q/R matrix with real DDL:
      (a) list_project_roles(P) never returns Q's or R's rows;
      (b) assert_role_visible_to_project(roleQ, P) + assert_entity_in_org(entityOrgR, orgA)
          each raise EntityNotFound (existence-hiding);
      (c) set_entity_project_role(entityOrgA, projectOrgB) -> CrossProjectDenied, no row.
    """
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_a, org_b = f"__epic40_orgA_{suffix}__", f"__epic40_orgB_{suffix}__"
    proj_p, proj_q, proj_r = (
        f"__epic40_projP_{suffix}__", f"__epic40_projQ_{suffix}__", f"__epic40_projR_{suffix}__",
    )
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_a, f"a{suffix}")
        _mk_org(conn, org_b, f"b{suffix}")
        _mk_project(conn, proj_p, org_a, f"p{suffix}")
        _mk_project(conn, proj_q, org_a, f"q{suffix}")
        _mk_project(conn, proj_r, org_b, f"r{suffix}")
    try:
        ex = ter.upsert_tracked_entity(org_id=org_a, canonical_name=f"{BRAND_X}_{suffix}",
                                       created_by="s")
        er = ter.upsert_tracked_entity(org_id=org_b, canonical_name=f"{BRAND_R}_{suffix}",
                                       created_by="s")
        role_p = ter.set_entity_project_role(
            entity_id=ex["id"], project_id=proj_p, role="own", created_by="s"
        )
        role_q = ter.set_entity_project_role(
            entity_id=ex["id"], project_id=proj_q, role="competitor", created_by="s"
        )
        ter.set_entity_project_role(
            entity_id=er["id"], project_id=proj_r, role="own", created_by="s"
        )

        # (a) same-org confidentiality: P sees only its own row.
        rows_p = ter.list_project_roles(proj_p)
        assert {r["entity_id"] for r in rows_p} == {ex["id"]}
        assert role_q["id"] not in {r["id"] for r in rows_p}
        assert er["id"] not in {r["entity_id"] for r in rows_p}

        # (b) existence-hiding: sibling role id + foreign-org entity both 404-semantic.
        assert ter.assert_role_visible_to_project(role_p["id"], proj_p)["id"] == role_p["id"]
        with pytest.raises(ter.EntityNotFound):
            ter.assert_role_visible_to_project(role_q["id"], proj_p)  # sibling -> hidden
        with pytest.raises(ter.EntityNotFound):
            ter.assert_entity_in_org(er["id"], org_a)                 # cross-org -> hidden

        # (c) cross-project mutation refused, no row written.
        with pytest.raises(ter.CrossProjectDenied):
            ter.set_entity_project_role(
                entity_id=ex["id"], project_id=proj_r, role="competitor", created_by="s"
            )
        assert ter.get_entity_project_role(ex["id"], proj_r) is None
    finally:
        _cleanup_org(org_a)
        _cleanup_org(org_b)
