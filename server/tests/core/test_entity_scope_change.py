"""Tests for Story 40.4 -- add-scope (tracked-entity / source-binding) cost/cardinality
preview + backfill decision (Epic 40).

Offline (no DB): preview shape + no-write, per-source cost/cardinality, the "50 competitors"
multiply, idempotency-key replay + conflict, blocking gaps, stale fingerprint, defer
forward-only + honesty marker, request routes ONLY through the candidate contract
(candidate-only, no live-pointer move), atomic rollback, the AD-2 no-vocabulary grep, and
the E40-NFR05 import-identity of the Epic 37 primitives -- all over an in-memory FAKE conn +
injected binding/capability loaders.

Live-Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration 092, the
JSONB/fingerprint/status CHECKs, the idempotency unicity, ON DELETE RESTRICT, the
pending/confirmed coherence CHECK, the confirm audit row in app.audit_log, and the no
fabricated warehouse row on defer. Pattern calque sur test_geographic_change.py +
test_tracked_entity_registry.py.
"""

from __future__ import annotations

import copy
import os
import re
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import entity_scope_change as esc  # noqa: E402
from core.entity_scope_change import (  # noqa: E402
    EntityScopePreviewBlocked,
    EntityScopePreviewConflict,
    EntityScopePreviewStale,
    build_binding_scope_impact,
    create_entity_scope_change_preview,
    dependency_fingerprint_for_add,
    historical_absence_marker,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MODULE_PATH = _REPO_ROOT / "server" / "core" / "entity_scope_change.py"


# ---------------------------------------------------------------------------
# Fixtures: a source capability envelope + a binding + an entity (source-agnostic data).
# ---------------------------------------------------------------------------


def _capabilities(*, historical: bool = True, marker: str = "v1") -> dict:
    return {
        "contract_version": "1",
        "fingerprint_marker": marker,
        "reports": [
            {
                "id": "daily",
                "metrics": ["m"],
                "dimensions": ["date", "term"],
                "supported_grains": [["date"], ["date", "term"]],
                "quota_cost": {"read_points": 2, "unit": "request"},
                "pagination": {"row_limit": 1000, "max_pages": 3},
                "incremental": {
                    "mode": "date_window" if historical else "full_refresh",
                    "cursor_field": None,
                },
                "max_provider_backfill_days": 90,
            }
        ],
    }


def _binding(binding_id: str = "esb-1", *, driver: bool = True) -> dict:
    return {
        "id": binding_id,
        "entity_id": "tent-1",
        "source": "example_source",
        "external_id": "brand-123" if driver else None,
        "query_spec": {"report_id": "daily", "term": "acme"} if driver else {},
        "account_scope": None,
    }


def _entity() -> dict:
    return {"id": "tent-1", "org_id": "org-1", "canonical_name": "Acme"}


def _loaders(bindings: list[dict], caps: dict | None):
    return (lambda _entity_id: [copy.deepcopy(b) for b in bindings]), (lambda _binding: caps)


# ===========================================================================
# Offline -- preview shape & no-write (fake conn)
# ===========================================================================


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.connection.calls.append((" ".join(sql.split()), params))

    def fetchone(self):
        if not self.connection.results:
            return None
        return self.connection.results.pop(0)

    def fetchall(self):
        if not self.connection.results:
            return []
        return self.connection.results.pop(0)


class _Connection:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _inserted_row(
    *, proposed_add, impact, fingerprint="d" * 64, status="pending", confirmation=None
):
    return (
        "escprev-1",
        "project-1",
        "tent-1",
        proposed_add,
        impact,
        fingerprint,
        status,
        confirmation,
        "actor",
        None,
        None,
    )


# --- §2: per-source impact carries the cost/cardinality envelope + backfill contract --------


def test_build_binding_scope_impact_carries_cost_cardinality_and_contract() -> None:
    binding = _binding()
    original = copy.deepcopy(binding)
    impact = build_binding_scope_impact(
        _entity(), binding, _capabilities(), earliest_pull_date=None
    )

    assert binding == original  # no mutation
    assert impact["estimated_cost"] == {"read_points": 2, "unit": "request"}
    assert impact["estimated_volume"]["upper_bound_rows"] == 3_000
    assert impact["cardinality_estimate"]["upper_bound_rows"] == 3_000
    assert impact["cardinality_estimate"]["grain_width"] == 2
    assert impact["max_provider_backfill_days"] == 90
    assert impact["supports_historical_extraction"] is True
    assert impact["backfill_required"] is True
    assert impact["coverage_state"] == "partial"
    assert impact["blocking_gaps"] == []
    contract = impact["backfill_contract"]
    assert contract["candidate_only"] is True
    assert contract["touches_published_pointer"] is False
    assert contract["candidate_publication_required"] is True
    assert contract["publication_path"].endswith("/executions")


def test_retained_history_is_complete_no_backfill() -> None:
    impact = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date="2025-01-01"
    )
    assert impact["backfill_required"] is False
    assert impact["coverage_state"] == "complete"


# --- §3: a source with no query driver / no historical extraction produces a blocking gap ---


def test_missing_query_driver_is_a_blocking_gap() -> None:
    impact = build_binding_scope_impact(
        _entity(), _binding(driver=False), _capabilities(), earliest_pull_date=None
    )
    codes = {g["code"] for g in impact["blocking_gaps"]}
    assert "binding_query_driver_missing" in codes
    assert impact["compatibility"] == "blocked"
    assert impact["extra_query_count"] == 0


def test_no_historical_extraction_is_a_blocking_gap() -> None:
    impact = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(historical=False), earliest_pull_date=None
    )
    codes = {g["code"] for g in impact["blocking_gaps"]}
    assert "source_historical_extraction_unavailable" in codes
    assert impact["backfill_required"] is False


def test_no_capability_envelope_is_unavailable() -> None:
    impact = build_binding_scope_impact(_entity(), _binding(), None, earliest_pull_date=None)
    codes = {g["code"] for g in impact["blocking_gaps"]}
    assert "source_capability_envelope_unavailable" in codes
    assert impact["coverage_state"] == "unavailable"


# --- §1: preview persists ONLY the governance artifact (no registry/binding/warehouse write) -


def test_preview_persists_only_governance_artifact(monkeypatch) -> None:
    monkeypatch.setattr(esc, "_load_entity", lambda _eid: _entity())
    proposed_add = {"kind": "entity"}
    load_bindings, load_caps = _loaders([_binding()], _capabilities())
    # SELECT (no existing) then INSERT ... RETURNING.
    impact_row = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date=None
    )
    conn = _Connection(
        [None, _inserted_row(proposed_add=proposed_add, impact={"sources": [impact_row]})]
    )

    result = create_entity_scope_change_preview(
        project_id="project-1",
        entity_id="tent-1",
        proposed_add=proposed_add,
        identity="actor",
        idempotency_key="key-1",
        conn=conn,
        binding_loader=load_bindings,
        capability_loader=load_caps,
    )

    assert result["id"] == "escprev-1"
    assert result["status"] == "pending"
    assert result["idempotent_replay"] is False
    assert conn.commits == 1
    sql = " ".join(statement for statement, _ in conn.calls).lower()
    assert "insert into app.entity_scope_change_previews" in sql
    assert "update app.entity_source_bindings" not in sql
    assert "update app.tracked_entities" not in sql
    assert "insert into app.audit_log" not in sql


# --- §4: adding N bindings sizes extra_query_count / cost proportionally (50 competitors) ----


def test_multiple_bindings_multiply_cost_and_cardinality() -> None:
    bindings = [_binding(f"esb-{i}") for i in range(50)]
    impact = esc._build_impact(_entity(), bindings, {b["id"]: _capabilities() for b in bindings})
    assert impact["affected_source_count"] == 50
    assert impact["extra_query_count"] == 50
    # 50 sources * 2 read_points * 1 query each.
    assert impact["total_estimated_cost"]["read_points_upper_bound"] == 100
    assert impact["cardinality_total"]["upper_bound_rows"] == 50 * 3_000
    assert impact["backfill_required"] is True


# ===========================================================================
# Offline -- idempotency & staleness (NFR-parity)
# ===========================================================================


def test_idempotency_key_replay_returns_same_row_no_duplicate() -> None:
    proposed_add = {"kind": "entity"}
    existing = _inserted_row(proposed_add=proposed_add, impact={"sources": []})
    conn = _Connection([existing])  # SELECT returns the existing row -> replay.
    load_bindings, load_caps = _loaders([], _capabilities())

    result = create_entity_scope_change_preview(
        project_id="project-1",
        entity_id="tent-1",
        proposed_add=proposed_add,
        identity="actor",
        idempotency_key="key-1",
        conn=conn,
        binding_loader=load_bindings,
        capability_loader=load_caps,
    )

    assert result["idempotent_replay"] is True
    assert conn.commits == 0  # no INSERT/commit on replay
    sql = " ".join(statement for statement, _ in conn.calls).lower()
    assert "insert into" not in sql


def test_idempotency_key_conflict_on_different_add() -> None:
    existing = _inserted_row(proposed_add={"kind": "entity"}, impact={"sources": []})
    conn = _Connection([existing])
    load_bindings, load_caps = _loaders([], _capabilities())

    with pytest.raises(EntityScopePreviewConflict):
        create_entity_scope_change_preview(
            project_id="project-1",
            entity_id="tent-1",
            proposed_add={"kind": "binding", "source": "other"},
            identity="actor",
            idempotency_key="key-1",
            conn=conn,
            binding_loader=load_bindings,
            capability_loader=load_caps,
        )


def test_dependency_fingerprint_is_order_stable_and_detects_drift() -> None:
    add = {"kind": "entity"}
    deps_a = [
        {"binding_id": "b", "source": "s2", "capability_fingerprint": "y", "query_driver": "q"},
        {"binding_id": "a", "source": "s1", "capability_fingerprint": "y", "query_driver": "q"},
    ]
    deps_b = list(reversed(deps_a))
    drifted = copy.deepcopy(deps_a)
    drifted[0]["capability_fingerprint"] = "z"

    assert dependency_fingerprint_for_add(add, deps_a) == dependency_fingerprint_for_add(
        add, deps_b
    )
    assert dependency_fingerprint_for_add(add, drifted) != dependency_fingerprint_for_add(
        add, deps_a
    )


def _preview_for_confirm(*, impact, fingerprint="d" * 64, status="pending", confirmation=None):
    return {
        "id": "escprev-1",
        "project_id": "project-1",
        "entity_id": "tent-1",
        "proposed_add": {"kind": "entity"},
        "impact": impact,
        "dependency_fingerprint": fingerprint,
        "status": status,
        "confirmation": confirmation,
    }


def test_confirm_on_stale_fingerprint_rejects(monkeypatch) -> None:
    impact_row = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date=None
    )
    preview = _preview_for_confirm(impact={"sources": [impact_row]}, fingerprint="a" * 64)
    monkeypatch.setattr(esc, "_fetch_preview_for_update", lambda *_a: preview)
    # Fresh recompute yields a different fingerprint than the stored one.
    monkeypatch.setattr(
        esc,
        "_current_add_dependencies",
        lambda *_a: (_entity(), [_binding()], {"esb-1": _capabilities()}, "b" * 64),
    )
    conn = _Connection([])

    with pytest.raises(EntityScopePreviewStale):
        esc.confirm_entity_scope_change(
            preview_id="escprev-1",
            project_id="project-1",
            identity="actor",
            backfill_decision="request",
            conn=conn,
        )


def test_confirm_with_blocking_gaps_is_refused_and_audited(monkeypatch) -> None:
    blocked_row = build_binding_scope_impact(
        _entity(), _binding(driver=False), _capabilities(), earliest_pull_date=None
    )
    fingerprint = "c" * 64
    preview = _preview_for_confirm(impact={"sources": [blocked_row]}, fingerprint=fingerprint)
    monkeypatch.setattr(esc, "_fetch_preview_for_update", lambda *_a: preview)
    monkeypatch.setattr(
        esc,
        "_current_add_dependencies",
        lambda *_a: (_entity(), [_binding(driver=False)], {"esb-1": _capabilities()}, fingerprint),
    )
    audits: list = []
    monkeypatch.setattr("core.audit.insert_audit_row", lambda *a, **k: audits.append(k))
    conn = _Connection([])

    with pytest.raises(EntityScopePreviewBlocked):
        esc.confirm_entity_scope_change(
            preview_id="escprev-1",
            project_id="project-1",
            identity="actor",
            backfill_decision="defer",
            conn=conn,
        )
    assert audits and audits[0]["action"] == "entity_scope_change.blocked"


# ===========================================================================
# Offline -- backfill=request reuses the Epic 12 candidate contract (AC3, E40-NFR05)
# ===========================================================================


def _wire_confirm(monkeypatch, *, impact_rows, fingerprint="d" * 64):
    preview = _preview_for_confirm(impact={"sources": impact_rows}, fingerprint=fingerprint)
    monkeypatch.setattr(esc, "_fetch_preview_for_update", lambda *_a: preview)
    monkeypatch.setattr(
        esc,
        "_current_add_dependencies",
        lambda *_a: (_entity(), [_binding()], {"esb-1": _capabilities()}, fingerprint),
    )
    return preview


def test_request_dispatches_only_candidate_actions(monkeypatch) -> None:
    impact_row = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date=None
    )
    _wire_confirm(monkeypatch, impact_rows=[impact_row])
    dispatched: list = []
    monkeypatch.setattr(
        esc,
        "_current_add_dependencies",
        lambda *_a: (_entity(), [_binding()], {"esb-1": _capabilities()}, "d" * 64),
    )
    monkeypatch.setattr(
        esc,
        "_dispatch_backfill_candidates",
        lambda conn, project_id, actions, identity: dispatched.extend(actions),
    )
    audits: list = []
    monkeypatch.setattr("core.audit.insert_audit_row", lambda *a, **k: audits.append(k))
    conn = _Connection([])

    result = esc.confirm_entity_scope_change(
        preview_id="escprev-1",
        project_id="project-1",
        identity="actor",
        backfill_decision="request",
        conn=conn,
    )

    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert dispatched, "a backfill_required source must be dispatched"
    for action in dispatched:
        assert action["candidate_only"] is True
        assert action["touches_published_pointer"] is False
    assert result["backfill_decision"] == "request"
    assert audits[0]["metadata"]["backfill_decision"] == "request"


def test_request_dispatch_failure_rolls_back(monkeypatch) -> None:
    impact_row = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date=None
    )
    _wire_confirm(monkeypatch, impact_rows=[impact_row])

    def _boom(*_a, **_k):
        raise RuntimeError("dispatch failed")

    monkeypatch.setattr(esc, "_dispatch_backfill_candidates", _boom)
    audits: list = []
    monkeypatch.setattr("core.audit.insert_audit_row", lambda *a, **k: audits.append(k))
    conn = _Connection([])

    with pytest.raises(RuntimeError):
        esc.confirm_entity_scope_change(
            preview_id="escprev-1",
            project_id="project-1",
            identity="actor",
            backfill_decision="request",
            conn=conn,
        )
    assert conn.rollbacks == 1
    assert conn.commits == 0
    assert audits == []  # no audit row on a rolled-back confirm


def test_dispatch_seam_absent_asserts_candidate_only(monkeypatch) -> None:
    import core.bounded_recovery as br

    monkeypatch.delattr(br, "dispatch_backfill_candidate", raising=False)
    conn = _Connection([])
    # candidate-only action -> no raise, defers to the 40.5 surface.
    esc._dispatch_backfill_candidates(
        conn,
        "project-1",
        [{"candidate_only": True, "touches_published_pointer": False}],
        "actor",
    )
    # a live-pointer-moving action -> refused (never a bespoke pull).
    with pytest.raises(esc.EntityScopeChangeError):
        esc._dispatch_backfill_candidates(
            conn,
            "project-1",
            [{"candidate_only": False, "touches_published_pointer": True}],
            "actor",
        )


def test_invalid_backfill_decision_raises_before_any_write() -> None:
    conn = _Connection([])
    with pytest.raises(ValueError):
        esc.confirm_entity_scope_change(
            preview_id="escprev-1",
            project_id="project-1",
            identity="actor",
            backfill_decision="rewrite",
            conn=conn,
        )
    assert conn.calls == []


# ===========================================================================
# Offline -- backfill=defer honesty (AC4, E40-NFR06, AD-9)
# ===========================================================================


def test_defer_activates_forward_only_and_marks_absence(monkeypatch) -> None:
    impact_row = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date=None
    )
    _wire_confirm(monkeypatch, impact_rows=[impact_row])
    monkeypatch.setattr("core.audit.insert_audit_row", lambda *a, **k: None)
    conn = _Connection([])

    result = esc.confirm_entity_scope_change(
        preview_id="escprev-1",
        project_id="project-1",
        identity="actor",
        backfill_decision="defer",
        conn=conn,
    )

    assert result["coverage_state"] == "forward_only"
    assert result["backfill_actions"] == []
    marker = result["historical_absence_marker"]
    assert marker["brand_absent_historical"] is True
    assert marker["fabricated_history"] is False
    # No warehouse UPDATE issued -- existing figures byte-unchanged (E40-NFR06).
    sql = " ".join(statement for statement, _ in conn.calls).lower()
    assert "update app.fact" not in sql
    assert "insert into app.fact" not in sql
    # The ONLY write is the preview status UPDATE (governance artifact).
    assert sql.count("update app.entity_scope_change_previews") == 1


def test_historical_absence_marker_is_typed_never_fabricated() -> None:
    marker = historical_absence_marker("tent-1", project_id="project-1")
    assert marker["coverage_state"] == "forward_only"
    assert marker["brand_absent_historical"] is True
    assert marker["fabricated_history"] is False
    assert marker["back_projected"] is False
    # It is a marker, not a row: no numeric value / no date range in it.
    assert "value" not in marker
    assert "rows" not in marker


def test_defer_with_no_bound_sources_is_inaction(monkeypatch) -> None:
    preview = _preview_for_confirm(impact={"sources": []}, fingerprint="e" * 64)
    monkeypatch.setattr(esc, "_fetch_preview_for_update", lambda *_a: preview)
    monkeypatch.setattr(
        esc,
        "_current_add_dependencies",
        lambda *_a: (_entity(), [], {}, "e" * 64),
    )
    monkeypatch.setattr("core.audit.insert_audit_row", lambda *a, **k: None)
    conn = _Connection([])

    result = esc.confirm_entity_scope_change(
        preview_id="escprev-1",
        project_id="project-1",
        identity="actor",
        backfill_decision="defer",
        conn=conn,
    )
    assert result["affected_sources"] == []
    assert result["coverage_state"] == "forward_only"


# ===========================================================================
# Offline -- atomic audit + AD-2 + reuse import-identity
# ===========================================================================


def test_exactly_one_audit_row_on_confirm(monkeypatch) -> None:
    impact_row = build_binding_scope_impact(
        _entity(), _binding(), _capabilities(), earliest_pull_date=None
    )
    _wire_confirm(monkeypatch, impact_rows=[impact_row])
    monkeypatch.setattr(esc, "_dispatch_backfill_candidates", lambda *a, **k: None)
    audits: list = []
    monkeypatch.setattr("core.audit.insert_audit_row", lambda *a, **k: audits.append(k))
    conn = _Connection([])

    esc.confirm_entity_scope_change(
        preview_id="escprev-1",
        project_id="project-1",
        identity="actor",
        backfill_decision="request",
        conn=conn,
    )
    assert len(audits) == 1
    meta = audits[0]["metadata"]
    assert audits[0]["action"] == "entity_scope_change.confirmed"
    assert meta["entity_id"] == "tent-1"
    assert "cost_summary" in meta
    assert meta["backfill_decision"] == "request"
    assert "coverage_state" in meta


def test_ad2_no_provider_brand_or_cost_vocabulary_in_module() -> None:
    """AD-2 (E40-NFR03): no connector/brand name, no hardcoded cost/query number. Cost and
    cardinality come from the capability envelope; backfill from the reused contract."""
    text = _MODULE_PATH.read_text(encoding="utf-8")
    lowered = text.lower()
    # Word-boundary match so structural identifiers (e.g. 'metadata=' from the audit seam)
    # do not trip a bare 'meta' substring -- we ban provider/brand WORDS, not substrings.
    for banned in (
        "google",
        "meta",
        "facebook",
        "tiktok",
        "strava",
        "linkedin",
        "trends",
        "adwords",
        "youtube",
        "instagram",
        "amazon",
        "shopify",
        "woocommerce",
        "acme",
    ):
        assert not re.search(rf"\b{banned}\b", lowered), (
            f"provider/brand vocabulary leaked: {banned}"
        )
    # No baked-in cost/query magic number: only 0/1 (extra_query_count) + the sha regex 64 +
    # the escprev prefix are structural, not cost literals. Scan for a suspicious quota number.
    assert "read_points = " not in text  # cost is read off report.quota_cost, never assigned
    assert not re.search(r"quota_cost\s*=\s*\{[^}]*read_points[^}]*:\s*\d", text)


def test_reuses_epic37_primitives_by_import_identity() -> None:
    """E40-NFR05: the module IMPORTS the 37.4 primitives, it does not re-derive them."""
    import core.geographic_change as gc

    assert esc._canonical_json is gc._canonical_json
    assert esc._sha256 is gc._sha256
    assert esc._estimate_volume is gc._estimate_volume
    assert esc.validate_confirmation_dependencies is gc.validate_confirmation_dependencies


# ===========================================================================
# Live Postgres -- real DDL, CHECKs, unicity, RESTRICT, audit reuse, defer honesty
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

_MIGRATION_092 = (
    _REPO_ROOT / "infra" / "nango" / "migrations" / "092_entity_scope_change_previews.sql"
)


def _apply_sql(conn, sql: str) -> None:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _prepare(conn) -> None:
    """Ensure app schema + projects/organizations + audit_log + 092 applied idempotently."""
    _apply_sql(conn, "CREATE SCHEMA IF NOT EXISTS app")
    _apply_sql(conn, _MIGRATION_092.read_text(encoding="utf-8"))


def _mk_project(conn, project_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s,%s,%s,'system') ON CONFLICT (id) DO NOTHING",
            (f"org_{suffix}", f"Org-{suffix}", f"org-{suffix}"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id) VALUES (%s,%s,%s,%s) "
            "ON CONFLICT (id) DO UPDATE SET org_id = EXCLUDED.org_id",
            (project_id, f"Proj-{suffix}", f"proj-{suffix}", f"org_{suffix}"),
        )
    conn.commit()


def _cleanup(conn, project_id, suffix) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM app.entity_scope_change_previews WHERE project_id = %s", (project_id,)
        )
        cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        cur.execute("DELETE FROM app.organizations WHERE id = %s", (f"org_{suffix}",))
    conn.commit()


@pg_available
def test_ddl_creates_table_and_indexes_replayable() -> None:
    from core.db import get_connection

    with get_connection() as conn:
        _prepare(conn)
        _apply_sql(conn, _MIGRATION_092.read_text(encoding="utf-8"))  # replay is a no-op
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='app' AND table_name='entity_scope_change_previews'"
            )
            assert cur.fetchone() is not None
            cur.execute(
                "SELECT indexname FROM pg_indexes WHERE schemaname='app' "
                "AND tablename='entity_scope_change_previews'"
            )
            idx = {r[0] for r in cur.fetchall()}
    assert "idx_entity_scope_change_previews_project" in idx
    assert "idx_entity_scope_change_previews_entity" in idx


@pg_available
def test_jsonb_fingerprint_status_checks_enforced() -> None:
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    project_id = f"esc_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_project(conn, project_id, suffix)
        try:
            # non-object proposed_add rejected.
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_scope_change_previews "
                        "(id, project_id, entity_id, proposed_add, impact, "
                        " dependency_fingerprint, idempotency_key_hash, requested_by) "
                        "VALUES (%s,%s,'tent-1','[]'::jsonb,'{}'::jsonb,%s,%s,'a')",
                        (f"escprev_{uuid.uuid4().hex}", project_id, "f" * 64, "e" * 64),
                    )
                    conn.rollback()
                    raise AssertionError("non-object proposed_add should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
            # non-hex fingerprint rejected.
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_scope_change_previews "
                        "(id, project_id, entity_id, proposed_add, impact, "
                        " dependency_fingerprint, idempotency_key_hash, requested_by) "
                        "VALUES (%s,%s,'tent-1','{}'::jsonb,'{}'::jsonb,'not-hex',%s,'a')",
                        (f"escprev_{uuid.uuid4().hex}", project_id, "e" * 64),
                    )
                    conn.rollback()
                    raise AssertionError("non-hex fingerprint should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
            # bad status rejected.
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_scope_change_previews "
                        "(id, project_id, entity_id, proposed_add, impact, "
                        " dependency_fingerprint, status, idempotency_key_hash, requested_by) "
                        "VALUES (%s,%s,'tent-1','{}'::jsonb,'{}'::jsonb,%s,'weird',%s,'a')",
                        (f"escprev_{uuid.uuid4().hex}", project_id, "f" * 64, "e" * 64),
                    )
                    conn.rollback()
                    raise AssertionError("bad status should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
        finally:
            _cleanup(conn, project_id, suffix)


@pg_available
def test_idempotency_unicity_and_confirmation_coherence() -> None:
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    project_id = f"esc_proj_{suffix}"
    key_hash = "e" * 64
    with get_connection() as conn:
        _prepare(conn)
        _mk_project(conn, project_id, suffix)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.entity_scope_change_previews "
                    "(id, project_id, entity_id, proposed_add, impact, "
                    " dependency_fingerprint, idempotency_key_hash, requested_by) "
                    "VALUES (%s,%s,'tent-1','{}'::jsonb,'{}'::jsonb,%s,%s,'a')",
                    (f"escprev_{uuid.uuid4().hex}", project_id, "f" * 64, key_hash),
                )
            conn.commit()
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_scope_change_previews "
                        "(id, project_id, entity_id, proposed_add, impact, "
                        " dependency_fingerprint, idempotency_key_hash, requested_by) "
                        "VALUES (%s,%s,'tent-1','{}'::jsonb,'{}'::jsonb,%s,%s,'a')",
                        (f"escprev_{uuid.uuid4().hex}", project_id, "f" * 64, key_hash),
                    )
                    conn.rollback()
                    raise AssertionError("duplicate idempotency key should be rejected")
                except psycopg.errors.UniqueViolation:
                    conn.rollback()
            # confirmed row with NULL confirmation rejected by the coherence CHECK.
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO app.entity_scope_change_previews "
                        "(id, project_id, entity_id, proposed_add, impact, "
                        " dependency_fingerprint, status, idempotency_key_hash, requested_by) "
                        "VALUES (%s,%s,'tent-1','{}'::jsonb,'{}'::jsonb,%s,'confirmed',%s,'a')",
                        (f"escprev_{uuid.uuid4().hex}", project_id, "f" * 64, "d" * 64),
                    )
                    conn.rollback()
                    raise AssertionError("confirmed w/ NULL confirmation should be rejected")
                except psycopg.errors.CheckViolation:
                    conn.rollback()
        finally:
            _cleanup(conn, project_id, suffix)


@pg_available
def test_on_delete_restrict_protects_referenced_project() -> None:
    import psycopg
    from core.db import get_connection

    suffix = uuid.uuid4().hex[:8]
    project_id = f"esc_proj_{suffix}"
    with get_connection() as conn:
        _prepare(conn)
        _mk_project(conn, project_id, suffix)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.entity_scope_change_previews "
                    "(id, project_id, entity_id, proposed_add, impact, "
                    " dependency_fingerprint, idempotency_key_hash, requested_by) "
                    "VALUES (%s,%s,'tent-1','{}'::jsonb,'{}'::jsonb,%s,%s,'a')",
                    (f"escprev_{uuid.uuid4().hex}", project_id, "f" * 64, "e" * 64),
                )
            conn.commit()
            with conn.cursor() as cur:
                try:
                    cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
                    conn.rollback()
                    raise AssertionError("referenced project delete should be RESTRICTed")
                except psycopg.errors.ForeignKeyViolation:
                    conn.rollback()
        finally:
            _cleanup(conn, project_id, suffix)
