"""Story 40.6 -- THE load-bearing guardrail (Epic 40 acceptance gate, AC1 / E40-FR06 / E40-AD4).

Identity-aligned != metric-comparable. Aligning ONE entity (`__epic40_brand_x__`) across
FOUR incommensurable sources -- Share of Search (a share ratio), SOV (a spend/share figure),
Brand Lift (a recall %), Strava members (a headcount) -- through the 40.x registry does NOT
license summing or comparing those measures without an Epic 27 reconciliation rule. The
combination is a FAIL-CLOSED gap: an honest NULL + an invitation to define the rule, NEVER a
silent cross-source sum.

The proof is FOUR ways (Task 2 / AC1):
  (a) THE typed no-rule status: the REAL metric_reconciliation.resolve_route (with injectable
      no-rule collaborators) returns UNRULED_OVERLAP / NOT_COMBINABLE -- a typed
      ReconciliationReason, target_mart=None, NO numeric total (fail-closed proven by
      absence-of-a-number, the 39.9 discipline).
  (b) The CONTRAPOSITIVE: resolve_entity_for_project returns the aligned entity (identity IS
      conformed) WHILE resolve_reconciliation returns None (no rule at any cascade level).
  (c) The STORE-BOUNDARY: populating the registry (upsert entity + set roles + wire bindings)
      writes ZERO rows to overlap_groups / overlap_group_members / reconciliation_rules --
      proven statically (the 40.x modules import NO Epic-27 write function) and, pg-gated, by
      a row-count of the three Epic 27 tables unchanged after registry population.
  (d) The POSITIVE CONTROL: only an explicit KEEP_SEPARATE rule flips the decision to N
      series -- and even then it is STILL not a sum.

Offline (no DB) for the guardrail core: resolve_route is fully fakeable (every collaborator
injectable -- see its docstring), so the load-bearing assertion needs no DB and no running
server. The store-boundary Epic-27-tables-unchanged proof on REAL rows is pg-gated (throwaway
DB; migrations 049 -> 086 -> 089 applied idempotently, Supabase stays human-gated). Pattern
calque sur test_metric_reconciliation.py + test_tracked_entity_registry.py.
"""

from __future__ import annotations

import ast
import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import metric_reconciliation as mr  # noqa: E402
from core import metric_semantics as ms  # noqa: E402
from core import tracked_entity_registry as ter  # noqa: E402

# ---------------------------------------------------------------------------
# The guardrail spine: ONE entity reported by FOUR incommensurable canonical metrics.
# These are SYNTHETIC canonical names in the fixture -- the point is the machinery, not a
# specific ingested panel (MEMORY: no-connector-test-accounts). All keys are `__epic40_*__`
# namespaced so they can NEVER pollute a production mart/rollup (39.9 discipline).
# ---------------------------------------------------------------------------

ENTITY_KEY = "__epic40_brand_x__"

# The four incommensurable measures the guardrail must REFUSE to sum:
#   share_of_search -> a share ratio      (dimensionless proportion)
#   sov             -> a spend/share      (currency-or-share figure)
#   brand_lift      -> a recall %         (survey percentage)
#   strava_members  -> a headcount        (an integer count of people)
# A share ratio, a spend, a recall % and a headcount are INCOMMENSURABLE: sharing an ENTITY
# does not make them share a UNIT or a semantics (the audiences-GAM!=GA4 / AD-4 discipline).
GUARDRAIL_METRICS = (
    "__epic40_share_of_search__",
    "__epic40_sov__",
    "__epic40_brand_lift__",
    "__epic40_strava_members__",
)

# Two synthetic sources emit EACH guardrail metric for the aligned entity -> >=2 emitters, so
# the no-rule path is UNRULED_OVERLAP (the conditional gate). Provider names live ONLY here in
# FIXTURE data, never in core (AD-2). Fully synthetic `__epic40_*__` handles.
GUARDRAIL_EMITTERS = ("__epic40_source_a__", "__epic40_source_b__")


# ---------------------------------------------------------------------------
# Offline fakes -- injectable no-rule collaborators (mirror test_metric_reconciliation).
# The ONE thing that MUST NOT be mocked is resolve_route itself: we call the REAL resolver so
# the test proves the ACTUAL gate returns the fail-closed decision.
# ---------------------------------------------------------------------------


def _no_rule_resolver(project_id, metric):
    """The cascade fake for the guardrail: NO reconciliation rule at any level (the registry
    aligned the entity but created NO overlap_group / reconciliation_rules row)."""
    return None


def _two_emitters(metric):
    """>=2 synthetic sources emit the metric for the aligned entity (the overlap condition)."""
    return list(GUARDRAIL_EMITTERS)


def _one_emitter(metric):
    """A single synthetic source emits the metric (the non-additive single-emitter slice)."""
    return [GUARDRAIL_EMITTERS[0]]


def _non_additive_definition(project_id, metric):
    """A layer-1 definition marking the metric NON-ADDITIVE (a ratio / recall % / position)."""
    return {"canonical_name": metric, "additive": False}


def _keep_separate_rule(metric):
    """An explicit PLATFORM KEEP_SEPARATE rule (the positive control -- N series, never a sum).

    NOTE: this is deliberately KEEP_SEPARATE, NOT a SUM/PRIORITY rule: authoring a real rule
    that summed the four incommensurable measures would be semantically WRONG. The point is
    that the ONLY managed combination is a human-authored Epic 27 rule, and even KEEP_SEPARATE
    is 'series per source, NEVER add them' (D-5)."""
    return {
        "method": mr.METHOD_KEEP_SEPARATE,
        "scope_level": mr.SCOPE_PLATFORM,
        "overlap_group_id": "__epic40_ovg__",
        "priority_order": list(GUARDRAIL_EMITTERS),
        "target_mart": None,
        "join_key": None,
    }


def _decision_has_no_total(decision: mr.RouteDecision) -> bool:
    """A fail-closed decision carries NO numeric total: no target_mart, no DIRECT_SUM/SUM
    method, no priority_order that a consumer could read as a routed combination.

    Fail-closed is proven by ABSENCE-OF-A-NUMBER (the 39.9 discipline), not merely by the
    presence of a warning. UNRULED_OVERLAP / NOT_COMBINABLE both carry per-source series (or
    none) + a reason, and NEVER a target_mart nor a SUM method."""
    return (
        decision.target_mart is None
        and decision.method not in (mr.METHOD_SUM, mr.METHOD_PRIORITY,
                                    mr.METHOD_DEDUP_ID, mr.METHOD_ESTIMATE)
        and decision.priority_order == ()
    )


# ===========================================================================
# (a) THE typed no-rule status -- the negative (fail-closed) proof (AC1).
# ===========================================================================


@pytest.mark.parametrize("metric", GUARDRAIL_METRICS)
def test_aligned_entity_metric_is_unruled_overlap_not_a_sum(metric):
    """AC1 (a): each of the four incommensurable measures for the aligned entity, emitted by
    >=2 sources WITHOUT a rule, routes to UNRULED_OVERLAP -- a typed ReconciliationReason
    (code=UNRULED_OVERLAP, emitters=...), target_mart=None, NO total. The registry aligned the
    ENTITY; it did NOT make the metrics summable."""
    decision = mr.resolve_route(
        "__epic40_project_p__",
        metric,
        rule_resolver=_no_rule_resolver,          # no Epic 27 rule
        definition_resolver=lambda p, m: None,    # keep the resolver fully offline (no DB)
        emitters_source=_two_emitters,            # >=2 sources emit the metric
    )
    # The typed reason IS present ...
    assert decision.status == mr.RouteStatus.UNRULED_OVERLAP
    assert decision.reason is not None
    assert decision.reason.code == mr.CODE_UNRULED_OVERLAP
    assert set(decision.reason.emitters) == set(GUARDRAIL_EMITTERS)
    # ... AND no cross-source sum/total was produced (fail-closed by absence-of-a-number).
    assert _decision_has_no_total(decision), decision
    # The consumer gets N per-source series it MUST NOT add, never a combined figure.
    assert len(decision.series) == len(GUARDRAIL_EMITTERS)


@pytest.mark.parametrize("metric", GUARDRAIL_METRICS)
def test_non_additive_single_emitter_is_not_combinable(metric):
    """AC1 (a, second branch): a non-additive measure with a SINGLE emitter and no rule ->
    NOT_COMBINABLE (a ratio / recall % / position cannot be summed across sources without a
    rule). Still a typed reason + NO total."""
    decision = mr.resolve_route(
        "__epic40_project_p__",
        metric,
        rule_resolver=_no_rule_resolver,
        definition_resolver=_non_additive_definition,   # layer-1: additive=False
        emitters_source=_one_emitter,                    # a single emitter
    )
    assert decision.status == mr.RouteStatus.NOT_COMBINABLE
    assert decision.reason is not None
    assert decision.reason.code == mr.CODE_NON_ADDITIVE_NO_RULE
    assert _decision_has_no_total(decision), decision


def test_detect_unruled_overlaps_flags_every_guardrail_metric():
    """The conditional gate (detect_unruled_overlaps) surfaces EACH aligned metric as an
    unruled overlap -- the 'invitation to configure', never a block, never a sum. Proves the
    gate sees all four incommensurable measures as combination-unavailable."""
    index = {metric: set(GUARDRAIL_EMITTERS) for metric in GUARDRAIL_METRICS}
    reasons = mr.detect_unruled_overlaps(
        "__epic40_project_p__",
        rule_resolver=_no_rule_resolver,
        emitters_source=index,   # dict short-cut {metric -> connectors}
        members_reader=lambda: [],
    )
    flagged = {r.emitters and r.message for r in reasons}
    assert len(reasons) == len(GUARDRAIL_METRICS)
    for r in reasons:
        assert r.code == mr.CODE_UNRULED_OVERLAP
        assert set(r.emitters) == set(GUARDRAIL_EMITTERS)
    assert flagged  # every metric produced a reason (non-empty)


# ===========================================================================
# (b) The CONTRAPOSITIVE -- alignment happened, comparability did not (AC1).
# ===========================================================================


def test_contrapositive_entity_resolves_while_reconciliation_is_none(monkeypatch):
    """AC1 (b): the registry conformed the ENTITY (resolve_entity_for_project returns it with
    its role) WHILE resolve_reconciliation returns None for EACH metric (no rule at any cascade
    level). Alignment is NECESSARY-BUT-NOT-SUFFICIENT for a combined figure.

    resolve_entity_for_project is driven over a tiny fake store (the real 40.1 store call);
    resolve_reconciliation is driven with the real cascade shape but no covering group."""
    # -- resolve_entity_for_project over a minimal fake 40.1 store --
    from contextlib import contextmanager

    project_id = "__epic40_project_p__"
    org_id = "__epic40_org_a__"
    entity_row = {
        "id": "tent___epic40_brand_x__", "org_id": org_id, "scope_level": "ORG",
        "canonical_name": ENTITY_KEY, "display_name": None, "aliases": [],
        "entity_kind": "brand", "status": "approved", "created_by": "s",
        "approved_by": None, "approved_at": None, "created_at": None, "updated_at": None,
    }
    role_row = {
        "id": "epr___epic40__", "entity_id": entity_row["id"], "project_id": project_id,
        "role": "competitor", "created_by": "s", "created_at": None, "updated_at": None,
    }

    class _Cur:
        def __init__(self):
            self.description = None
            self._res = None

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, sql, params=None):
            s = " ".join(sql.split())
            if s.startswith("SELECT org_id FROM app.projects"):
                self._res = [(org_id,)] if params[0] == project_id else []
            elif "FROM app.tracked_entities" in s and " WHERE id = " in s:
                self.description = [(c.strip(),) for c in ter._ENTITY_COLUMNS.split(",")]
                self._res = ([tuple(entity_row[c[0]] for c in self.description)]
                             if params[0] == entity_row["id"] else [])
            elif "FROM app.entity_project_roles" in s and "entity_id = %s AND project_id" in s:
                self.description = [(c.strip(),) for c in ter._ROLE_COLUMNS.split(",")]
                self._res = ([tuple(role_row[c[0]] for c in self.description)]
                             if params[0] == entity_row["id"] else [])
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

    import core.db as db

    monkeypatch.setattr(db, "get_connection", _fake_conn)

    # Identity IS conformed: the entity resolves for the project, carrying its role.
    view = ter.resolve_entity_for_project(project_id, entity_row["id"])
    assert view is not None
    assert view["entity"]["canonical_name"] == ENTITY_KEY
    assert view["role"] == "competitor"

    # Comparability did NOT happen: no rule covers any of the four metrics (cascade -> None).
    # Drive the real resolve_reconciliation with a cascade that finds no covering group.
    monkeypatch.setattr(ms, "_project_org_id", lambda pid: org_id)
    monkeypatch.setattr(
        ms, "_load_reconciliation_rows",
        lambda *, org_id, project_id, metric: [],  # no group covers the metric at any level
    )
    for metric in GUARDRAIL_METRICS:
        assert ms.resolve_reconciliation(project_id, metric) is None


# ===========================================================================
# (c) The STORE-BOUNDARY -- the registry fabricates NO Epic 27 rule (AC1).
# ===========================================================================

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CORE_DIR = _REPO_ROOT / "server" / "core"

# The three Epic 27 tables the registry must NEVER write (the guardrail lives entirely in
# Epic 27; 40's registry conforms the ENTITY dimension only).
_EPIC27_TABLES = ("overlap_groups", "overlap_group_members", "reconciliation_rules")

# The 40.x registry modules 40.6 populates the fixture through.
_REGISTRY_MODULES = (
    "tracked_entity_registry.py",   # 40.1
    "entity_source_bindings.py",    # 40.2
    "tracked_entity_matching.py",   # 40.3
    "entity_scope_change.py",       # 40.4
    "brand_registry_mcp.py",        # 40.5
)


def test_registry_modules_never_write_epic27_tables():
    """AC1 (c, static): NO 40.x registry module contains an INSERT/UPDATE/DELETE against any
    Epic 27 overlap/reconciliation table. Aligning an entity conforms the ENTITY dimension; it
    creates NO overlap_group / reconciliation_rules row -- so resolve_route stays UNRULED_OVERLAP.

    Proven by scanning the source for a write verb adjacent to an Epic 27 table name. A read
    (SELECT, e.g. the emitter enumeration) is allowed; a WRITE is the boundary breach."""
    write_verbs = ("insert into", "update ", "delete from")
    breaches: list[str] = []
    for module in _REGISTRY_MODULES:
        src = (_CORE_DIR / module).read_text(encoding="utf-8").lower()
        for table in _EPIC27_TABLES:
            for verb in write_verbs:
                # a write verb followed (within the same statement window) by the table name
                needle = f"{verb}app.{table}"
                if needle.replace(" ", "") in src.replace(" ", ""):
                    breaches.append(f"{module}: {verb!r} -> app.{table}")
    assert not breaches, f"a 40.x module WRITES an Epic 27 table: {breaches}"


def test_registry_modules_import_no_epic27_write_function():
    """AC1 (c, import-boundary): NO 40.x registry module imports a metric_semantics /
    metric_reconciliation WRITE helper (upsert_reconciliation_rule / create_overlap_group /
    import_platform_defaults / add_overlap_member ...). The ONLY 27.x symbols a registry
    module may import are the SOCLE re-exports (validate_scope / _mint_id /
    _write_semantics_audit / SCOPE_* / InvalidScope) -- pure helpers that touch NO Epic 27
    overlap table. Proven by parsing every `from core.metric_semantics import` / `from
    core.metric_reconciliation import` and asserting each name is in the allow-list."""
    allowed_socle = {
        "SCOPE_ORG", "SCOPE_PROJECT", "SCOPE_PLATFORM", "InvalidScope",
        "_mint_id", "_write_semantics_audit", "validate_scope",
    }
    forbidden_hits: list[str] = []
    for module in _REGISTRY_MODULES:
        tree = ast.parse((_CORE_DIR / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in (
                "core.metric_semantics", "core.metric_reconciliation",
            ):
                for alias in node.names:
                    if alias.name not in allowed_socle:
                        forbidden_hits.append(f"{module}: from {node.module} import {alias.name}")
    assert not forbidden_hits, (
        f"a 40.x module imports a non-socle Epic-27 symbol (possible write path): {forbidden_hits}"
    )


# ===========================================================================
# (d) The POSITIVE CONTROL -- only an explicit rule flips it (still no sum) (AC1).
# ===========================================================================


@pytest.mark.parametrize("metric", GUARDRAIL_METRICS)
def test_positive_control_keep_separate_rule_yields_series_never_a_sum(metric):
    """AC1 (d): with an explicit human-authored PLATFORM KEEP_SEPARATE rule, resolve_route
    returns KEEP_SEPARATE -- N per-source series, target_mart=None, an explicit 'never add
    them' reason. This proves the ONLY path to a managed combination is a human-authored Epic
    27 rule (which the registry CANNOT create), and even then KEEP_SEPARATE is STILL not a sum.
    """
    decision = mr.resolve_route(
        "__epic40_project_p__",
        metric,
        rule_resolver=lambda pid, m: _keep_separate_rule(m),
        members_source=lambda ovg_id: list(GUARDRAIL_EMITTERS),
    )
    assert decision.status == mr.RouteStatus.KEEP_SEPARATE
    assert decision.method == mr.METHOD_KEEP_SEPARATE
    # STILL no combined total: N series + a 'never add them' reason, target_mart=None.
    assert decision.target_mart is None
    assert decision.reason is not None
    assert decision.reason.code == mr.CODE_KEEP_SEPARATE
    assert len(decision.series) == len(GUARDRAIL_EMITTERS)
    assert _decision_has_no_total(decision), decision


# ===========================================================================
# Pg-gated (throwaway DB) -- the Epic 27 tables are unchanged after registry population.
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
    _apply_migration(conn, _MIGRATION_049)  # Epic 27 tables + audit
    _apply_migration(conn, _MIGRATION_086)  # 40.1 registry
    _apply_migration(conn, _MIGRATION_089)  # 40.2 bindings


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


def _epic27_counts(conn) -> dict:
    counts: dict = {}
    with conn.cursor() as cur:
        for table in _EPIC27_TABLES:
            cur.execute(f"SELECT count(*) FROM app.{table}")  # noqa: S608 (fixed table names)
            counts[table] = cur.fetchone()[0]
    return counts


@pg_available
def test_registry_population_leaves_epic27_tables_unchanged():
    """AC1 (c, pg-gated): populating the registry over the guardrail fixture (upsert entity,
    role it in a project, wire per-source bindings for all four measures) changes the row-count
    of overlap_groups / overlap_group_members / reconciliation_rules by EXACTLY 0. The registry
    conforms the ENTITY dimension without ever touching the Epic 27 overlap layer, so
    resolve_route stays UNRULED_OVERLAP -- the guardrail cannot be bypassed by aligning a brand.
    """
    from core import entity_source_bindings as esb
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    org_id = f"__epic40_org_{suffix}__"
    proj_id = f"__epic40_proj_{suffix}__"
    with get_connection() as conn:
        _prepare(conn)
        _mk_org(conn, org_id, suffix)
        _mk_project(conn, proj_id, org_id, suffix)

    try:
        with get_connection() as conn:
            before = _epic27_counts(conn)

        # Populate the registry: the aligned entity + a role + a per-source binding per measure.
        entity = ter.upsert_tracked_entity(
            org_id=org_id, canonical_name=f"{ENTITY_KEY}_{suffix}", created_by="s",
            aliases=["__epic40_brand_x_alias__"],
        )
        ter.set_entity_project_role(
            entity_id=entity["id"], project_id=proj_id, role="competitor", created_by="s"
        )
        for metric in GUARDRAIL_METRICS:
            # source == the synthetic emitter; the query_spec pins the measure (opaque data).
            esb.upsert_source_binding(
                entity_id=entity["id"],
                source=f"__epic40_src_{metric}__",
                created_by="s",
                external_id=f"ext_{metric}",
                query_spec={"measure": metric},
            )

        with get_connection() as conn:
            after = _epic27_counts(conn)

        # The load-bearing store-boundary fact: the Epic 27 overlap layer is byte-untouched.
        assert after == before, (
            f"registry population changed an Epic 27 table (guardrail bypass!): "
            f"before={before} after={after}"
        )
    finally:
        _cleanup_org(org_id)


def _cleanup_org(org_id: str) -> None:
    from core.db import get_connection

    with get_connection() as clean:
        with clean.cursor() as cur:
            cur.execute("DELETE FROM app.organizations WHERE id = %s", (org_id,))
        clean.commit()
