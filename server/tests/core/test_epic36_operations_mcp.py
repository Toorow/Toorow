"""Story 36.13 Operations-profile MCP tests (inspect + bounded recovery).

Fully offline (no live Postgres): the pure serializers are exercised directly, the
DB is a MagicMock cursor, and the capability middleware is driven with lightweight
stand-in tool/context objects. Mirrors test_epic36_operations.py /
test_epic36_capability_catalogs.py.

Coverage:
  (a) Operations tools register under profile="operations" and validate_catalog
      accepts them (no insights/write or read/confirmation contradiction).
  (b) discovery HIDES them without an operations opt-in (visible_profiles /
      middleware); an evidence-backed opt-in reveals them.
  (c) prepare produces an immutable proposal carrying every required field.
  (d) confirm routes EXACTLY ONE execute_operation; a duplicate confirm / timeout
      returns the ORIGINAL operation (replayed, no duplicate).
  (e) stale-version / forbidden-interval / quota-violation / lock-conflict each
      REFUSE dispatch (no execute_operation call).
  (f) the tools NEVER call commit_publication / mutate current_published_execution_id.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    # High-risk profiles are fail-closed OFF by default (review C1); this suite
    # exercises the Operations profile, so it opts in like a deployment that has
    # wired server-side host/workspace verification.
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


class _Recorder:
    """A stand-in mcp that records register_profiled handlers by name."""

    def __init__(self):
        self.handlers = {}
        self.tool = MagicMock()

    def capture(self, handler):
        self.handlers[handler.__name__] = handler
        return handler


def _register_all(recorder):
    """Run operations_mcp.register against a recorder; return the handler map."""
    from core import mcp_profiles, operations_mcp

    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.capture(handler)
        return real(mcp, handler, **kwargs)

    mcp_profiles.register_profiled = spy  # type: ignore[assignment]
    try:
        operations_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real  # type: ignore[assignment]
    return recorder.handlers


# ---------------------------------------------------------------------------
# (a) registration + validate_catalog acceptance (no contradiction).
# ---------------------------------------------------------------------------


def test_operations_tools_register_and_validate_catalog_accepts_them():
    from core import mcp_profiles

    recorder = _Recorder()
    _register_all(recorder)

    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert set(decls) == {
        "list_datastream_runs",
        "get_datastream_readiness",
        "prepare_datastream_recovery",
        "confirm_datastream_recovery",
        # Story 38.2: connector installation status read.
        "get_connector_installation_status",
        # Story 38.3: connector domain config read.
        "get_connector_domain_config",
        # Story 38.4: connector verification status read.
        "get_connector_verification_status",
        # Story 38.5: connector activation status read.
        "get_connector_activation_status",
        # Story 38.6: import template catalog read.
        "list_inbound_templates",
        # Story 38.7: inbound delivery credential status read (no secret, AC2).
        "get_inbound_credential_status",
    }
    # All under the operations profile.
    assert all(d.profile == "operations" for d in decls.values())
    # The consequential recovery dispatch is a human-confirmed write.
    confirm = decls["confirm_datastream_recovery"]
    assert confirm.effect == "write"
    assert confirm.confirmation_mode == "human"
    # prepare persists an immutable proposal row -> it is a preparation WRITE (review
    # H2), but needs no confirmation itself (the consequential confirm is separate).
    prepare = decls["prepare_datastream_recovery"]
    assert prepare.effect == "write"
    assert prepare.confirmation_mode == "none"
    # Pure reads are effect=read + confirmation none (no contradiction).
    for name in ("list_datastream_runs", "get_datastream_readiness",
                 "get_connector_installation_status", "get_connector_domain_config",
                 "get_connector_verification_status", "get_connector_activation_status",
                 "list_inbound_templates", "get_inbound_credential_status"):
        assert decls[name].effect == "read"
        assert decls[name].confirmation_mode == "none"
    # First real consumer of the profile system: boot validation must accept it.
    validated = {d.name for d in mcp_profiles.validate_catalog()}
    assert validated == set(decls)


def test_no_operations_tool_is_an_insights_write_or_read_confirmation_contradiction():
    from core import mcp_profiles

    recorder = _Recorder()
    _register_all(recorder)
    for d in mcp_profiles.registered_declarations():
        # No insights/write contradiction (none is insights here anyway).
        assert not (d.profile == "insights" and d.effect == "write")
        # No read tool demands confirmation.
        assert not (d.effect == "read" and d.confirmation_mode != "none")


# ---------------------------------------------------------------------------
# (b) discovery hides operations tools without an evidence-backed opt-in.
# ---------------------------------------------------------------------------


def _tool(name, profile):
    return SimpleNamespace(name=name, meta={"profile": profile}, tags={f"profile:{profile}"})


def test_operations_tools_hidden_without_opt_in(monkeypatch):
    from core import mcp_profiles

    recorder = _Recorder()
    _register_all(recorder)
    mw = mcp_profiles.build_middleware()

    tools = [
        _tool("report_read", "insights"),
        _tool("list_datastream_runs", "operations"),
        _tool("confirm_datastream_recovery", "operations"),
    ]

    async def call_next(_context):
        return tools

    # Authenticated but no operations opt-in / no workspace evidence -> insights only.
    monkeypatch.setattr(
        mcp_profiles, "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == ["report_read"]


def test_operations_tools_visible_with_evidence_backed_opt_in(monkeypatch):
    from core import mcp_profiles

    recorder = _Recorder()
    _register_all(recorder)
    mw = mcp_profiles.build_middleware()

    tools = [
        _tool("report_read", "insights"),
        _tool("list_datastream_runs", "operations"),
        _tool("confirm_datastream_recovery", "operations"),
    ]

    async def call_next(_context):
        return tools

    grants = {
        "enabled_profiles": ["insights", "operations"],
        "endpoint_binding": "admin-endpoint",
        "workspace_evidence_hash": "a" * 64,
    }
    monkeypatch.setattr(
        mcp_profiles, "_capability_context", lambda: ("user-1", {"host": "opaque"}, grants)
    )
    result = asyncio.run(mw.on_list_tools(SimpleNamespace(), call_next))
    assert [t.name for t in result] == [
        "report_read",
        "list_datastream_runs",
        "confirm_datastream_recovery",
    ]


def test_direct_call_to_confirm_denied_without_opt_in(monkeypatch):
    from core import mcp_profiles

    recorder = _Recorder()
    _register_all(recorder)
    mw = mcp_profiles.build_middleware()
    call_next = MagicMock()

    monkeypatch.setattr(
        mcp_profiles, "_capability_context",
        lambda: ("user-1", {}, {"enabled_profiles": ["insights"]}),
    )
    context = SimpleNamespace(message=SimpleNamespace(name="confirm_datastream_recovery"))
    with pytest.raises(Exception) as exc:
        asyncio.run(mw.on_call_tool(context, call_next))
    assert "introuvable" in str(exc.value)
    call_next.assert_not_called()


# ---------------------------------------------------------------------------
# Pure-helper guards (offline).
# ---------------------------------------------------------------------------


def test_bounded_interval_refuses_forbidden_windows():
    from core import operations_mcp

    ok = operations_mcp._bounded_interval("2026-07-01", "2026-07-10")
    assert ok == {"from": "2026-07-01", "to": "2026-07-10"}

    for bad in (("2026-07-10", "2026-07-01"), ("2026-01-01", "2026-12-31"), ("x", "y")):
        with pytest.raises(Exception) as exc:
            operations_mcp._bounded_interval(*bad)
        assert "forbidden_interval" in str(exc.value)


def test_versions_match_detects_drift():
    from core import operations_mcp

    pinned = {"plan_version_id": "p1", "mapping_version_id": "m1"}
    same = {"plan_version_id": "p1", "mapping_version_id": "m1"}
    plan_drift = {"plan_version_id": "p2", "mapping_version_id": "m1"}
    mapping_drift = {"plan_version_id": "p1", "mapping_version_id": "m9"}
    assert operations_mcp._versions_match(pinned, same)
    assert not operations_mcp._versions_match(pinned, plan_drift)
    assert not operations_mcp._versions_match(pinned, mapping_drift)


# ---------------------------------------------------------------------------
# (c) prepare produces an immutable proposal with all required fields.
# ---------------------------------------------------------------------------


def _guarded(monkeypatch, org_id="org-1"):
    """Patch the strict access guard to allow with an edit decision."""
    from core import operations_mcp
    from core.project_access import AccessDecision

    monkeypatch.setattr(
        operations_mcp, "_guard_datastream",
        lambda ds, ident, *, minimum_capability: AccessDecision(
            True, "explicit_grant", "edit", org_id, (f"organization:{org_id}", f"flux:{ds}")
        ),
    )


def _fake_conn():
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    return conn, cur


def test_prepare_produces_immutable_proposal_with_all_fields(monkeypatch):
    from core import operations_mcp

    handlers = _register_all(_Recorder())
    _guarded(monkeypatch)
    monkeypatch.setattr(operations_mcp, "_identity", lambda: "user-1")
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(
        operations_mcp, "_load_target",
        lambda conn, ds: {
            "datastream_id": ds, "project_id": "proj-1", "org_id": "org-1",
            "plan_version_id": "p1", "mapping_version_id": "m1",
            "current_published_execution_id": "dse_good", "platform": "modx",
            "active_execution_id": None,
        },
    )
    monkeypatch.setattr(
        operations_mcp, "_quota_estimate",
        lambda platform, points: {"platform_known": True, "estimated_points": points,
                                  "verdict": "ok", "can_proceed": True},
    )
    monkeypatch.setattr(operations_mcp, "_insert_preparation", lambda conn, proposal, key: "prep_1")

    conn, _cur = _fake_conn()
    # Patch get_connection via the module's lazy import point.
    import core.db as db
    monkeypatch.setattr(db, "get_connection", lambda: _ctx(conn))

    result = handlers["prepare_datastream_recovery"](
        "ds-1", "refetch", date_from="2026-07-01", date_to="2026-07-05", estimated_points=10
    )
    data = result.structured_content["data"]
    assert data["preparation_id"] == "prep_1"
    # Every AD-27 field is visible in the proposal.
    for field in ("target", "kind", "target_versions", "interval", "impact",
                  "quota", "lock_ref", "rollback_ref", "expires_in_seconds"):
        assert field in data, field
    assert data["kind"] == "refetch"
    assert data["interval"] == {"from": "2026-07-01", "to": "2026-07-05"}
    assert data["target_versions"] == {"plan_version_id": "p1", "mapping_version_id": "m1",
                                       "policy_version": "pol-1"}
    assert data["impact"]["touches_published_pointer"] is False
    assert data["rollback_ref"] == "dse_good"


def test_prepare_refetch_rejects_forbidden_interval(monkeypatch):
    from core import operations_mcp

    handlers = _register_all(_Recorder())
    _guarded(monkeypatch)
    monkeypatch.setattr(operations_mcp, "_identity", lambda: "user-1")
    with pytest.raises(Exception) as exc:
        handlers["prepare_datastream_recovery"](
            "ds-1", "refetch", date_from="2026-01-01", date_to="2026-12-31"
        )
    assert "forbidden_interval" in str(exc.value)


class _ctx:
    """A trivial context-manager wrapper around a prebuilt connection."""

    def __init__(self, conn):
        self._conn = conn

    def __enter__(self):
        return self._conn

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# (d) confirm routes exactly ONE execute_operation; duplicate returns original.
# ---------------------------------------------------------------------------


def _prep_row(**over):
    base = {
        "id": "prep_1", "org_id": "org-1", "project_id": "proj-1",
        "datastream_id": "ds-1", "kind": "retry", "actor": "user-1",
        "target_versions": {"plan_version_id": "p1", "mapping_version_id": "m1",
                            "policy_version": "pol-1"},
        "interval": None, "impact": {}, "quota": {"estimated_points": 5},
        "lock_ref": None, "rollback_ref": "dse_good",
        "idempotency_key_hash": None, "state": "prepared", "operation_id": None,
        "expired": False,
    }
    base.update(over)
    return base


def _live_target(**over):
    base = {
        "datastream_id": "ds-1", "project_id": "proj-1", "org_id": "org-1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_good", "platform": "modx",
        "active_execution_id": None,
    }
    base.update(over)
    return base


def _confirm_env(monkeypatch, prep, target, exec_return):
    """Wire operations_mcp for a confirm call; return (handler, captured, conn)."""
    from core import operations, operations_mcp

    handlers = _register_all(_Recorder())
    _guarded(monkeypatch)
    monkeypatch.setattr(operations_mcp, "_identity", lambda: "user-1")
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(operations_mcp, "_load_preparation", lambda conn, pid: prep)
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: target)
    monkeypatch.setattr(
        operations_mcp, "_quota_estimate",
        lambda platform, points: {"can_proceed": True, "verdict": "ok",
                                  "estimated_points": points, "platform_known": True},
    )
    marked = {}
    monkeypatch.setattr(
        operations_mcp, "_mark_preparation_confirmed",
        lambda conn, pid, op_id: marked.setdefault("op_id", op_id),
    )
    # Pin the idempotency hash so the tamper guard passes.
    from core.operations import _sha256

    fp = operations_mcp._proposal_fingerprint(
        {"datastream_id": prep["datastream_id"], "kind": prep["kind"],
         "interval": prep["interval"], "target_versions": prep["target_versions"]}
    )
    key = operations_mcp._recovery_idempotency_key(
        prep["org_id"], prep["datastream_id"], prep["kind"], fp
    )
    prep["idempotency_key_hash"] = _sha256(key)

    captured = {"calls": 0}

    def fake_execute(conn, spec, *, mutation):
        captured["calls"] += 1
        captured["spec"] = spec
        return exec_return

    monkeypatch.setattr(operations, "execute_operation", fake_execute)

    conn, _cur = _fake_conn()
    import core.db as db
    monkeypatch.setattr(db, "get_connection", lambda: _ctx(conn))
    return handlers["confirm_datastream_recovery"], captured, marked


def test_confirm_routes_exactly_one_execute_operation(monkeypatch):
    from core import operations

    op = operations.OperationResult(
        "op-1", "succeeded", {"kind": "retry"}, "audit-1", "opout-1", False
    )
    handler, captured, marked = _confirm_env(monkeypatch, _prep_row(), _live_target(), op)

    result = handler("prep_1")
    assert captured["calls"] == 1
    assert captured["spec"].command_type == "operations.recovery.retry"
    assert captured["spec"].confirmation_mode == "human"
    data = result.structured_content["data"]
    assert data["operation_id"] == "op-1"
    assert data["replayed"] is False
    assert marked["op_id"] == "op-1"


def test_duplicate_confirm_returns_original_operation_no_duplicate(monkeypatch):
    from core import operations

    # execute_operation replays the ORIGINAL durable operation (idempotent).
    replay = operations.OperationResult(
        "op-1", "succeeded", {"kind": "retry"}, "audit-1", "opout-1", True
    )
    handler, captured, _marked = _confirm_env(monkeypatch, _prep_row(), _live_target(), replay)

    result = handler("prep_1")
    assert captured["calls"] == 1  # still ONE call; execute_operation itself dedups.
    data = result.structured_content["data"]
    assert data["operation_id"] == "op-1"
    assert data["replayed"] is True  # original returned, never a duplicate.


def test_timeout_outcome_unknown_never_creates_duplicate(monkeypatch):
    from core import operations

    unknown = operations.OperationResult("op-1", "outcome_unknown", {}, "audit-1", "opout-1", True)
    handler, captured, _marked = _confirm_env(monkeypatch, _prep_row(), _live_target(), unknown)
    result = handler("prep_1")
    assert captured["calls"] == 1
    assert result.structured_content["data"]["outcome"] == "outcome_unknown"
    assert result.structured_content["data"]["operation_id"] == "op-1"


# ---------------------------------------------------------------------------
# (e) each precondition breach REFUSES dispatch (no execute_operation call).
# ---------------------------------------------------------------------------


def _assert_refused(monkeypatch, prep, target, code, *, quota_ok=True):
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    handler, captured, _marked = _confirm_env(monkeypatch, prep, target, op)
    if not quota_ok:
        import core.operations_mcp as omod

        monkeypatch.setattr(
            omod, "_quota_estimate",
            lambda platform, points: {"can_proceed": False, "verdict": "budget_exhausted",
                                      "estimated_points": points, "platform_known": True},
        )
    with pytest.raises(Exception) as exc:
        handler("prep_1")
    assert code in str(exc.value)
    assert captured["calls"] == 0  # NO dispatch on any refusal.


def test_stale_versions_refuse_dispatch(monkeypatch):
    _assert_refused(
        monkeypatch, _prep_row(), _live_target(plan_version_id="p2"), "stale_versions"
    )


def test_changed_policy_refuses_dispatch(monkeypatch):
    prep = _prep_row(target_versions={"plan_version_id": "p1", "mapping_version_id": "m1",
                                      "policy_version": "OLD"})
    _assert_refused(monkeypatch, prep, _live_target(), "policy_changed")


def test_forbidden_interval_refuses_dispatch(monkeypatch):
    prep = _prep_row(kind="refetch", interval={"from": "2026-01-01", "to": "2026-12-31"})
    _assert_refused(monkeypatch, prep, _live_target(), "forbidden_interval")


def test_missing_exposure_refuses_dispatch(monkeypatch):
    _assert_refused(monkeypatch, _prep_row(), _live_target(platform=None), "missing_exposure")


def test_quota_violation_refuses_dispatch(monkeypatch):
    _assert_refused(monkeypatch, _prep_row(), _live_target(), "quota_violation", quota_ok=False)


def test_lock_conflict_refuses_dispatch(monkeypatch):
    _assert_refused(
        monkeypatch, _prep_row(kind="retry"),
        _live_target(active_execution_id="dse_active"), "lock_conflict"
    )


def test_stale_or_consumed_preparation_refuses_dispatch(monkeypatch):
    _assert_refused(monkeypatch, _prep_row(state="confirmed"), _live_target(), "stale_preparation")
    _assert_refused(monkeypatch, _prep_row(expired=True), _live_target(), "stale_preparation")


# ---------------------------------------------------------------------------
# (f) the module NEVER publishes / mutates the live pointer.
# ---------------------------------------------------------------------------


def test_dispatch_never_calls_commit_publication_or_mutates_pointer(monkeypatch):
    """The recovery mutation reuses create_execution/reconcile_execution only.

    It must NEVER touch commit_publication or write current_published_execution_id.
    """
    import core.datastream_publication as pub
    from core import operations_mcp

    commit_spy = MagicMock(side_effect=AssertionError("commit_publication must not be called"))
    monkeypatch.setattr(pub, "commit_publication", commit_spy, raising=False)

    created = {}
    monkeypatch.setattr(
        pub, "create_execution",
        lambda **kw: created.update(kw) or {"id": "dse_new", "state": "created"},
    )

    conn, cur = _fake_conn()
    prep = _prep_row(kind="retry")
    target = _live_target()
    result = operations_mcp._dispatch_recovery(conn, "op-1", prep, target)

    assert result.outcome == "succeeded"
    assert result.result["execution_id"] == "dse_new"
    commit_spy.assert_not_called()
    # A candidate execution was created against the pinned versions; the SQL issued
    # via `conn` never updated the published pointer column.
    sql = " ".join(
        str(c.args[0]) for c in cur.execute.call_args_list if c.args
    )
    assert "current_published_execution_id" not in sql


def test_module_source_contains_no_commit_publication_reference():
    """Static guard: operations_mcp must not import/call commit_publication."""
    from pathlib import Path

    src = Path("server/core/operations_mcp.py").read_text(encoding="utf-8")
    # It may mention the exclusion in a comment, but never call it.
    assert "commit_publication(" not in src


# ---------------------------------------------------------------------------
# migration 070 contract.
# ---------------------------------------------------------------------------


def test_migration_070_contains_operation_preparation_contract():
    from pathlib import Path

    sql = Path("infra/nango/migrations/070_operations_recovery.sql").read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.operation_preparations" in sql
    assert "kind" in sql and "CHECK (kind IN ('retry', 'refetch', 'reconcile'))" in sql
    assert "target_versions" in sql
    assert "idempotency_key_hash" in sql
    assert "operation_id" in sql
    assert "REFERENCES app.operations(id)" in sql
    # Immutability enforced by a trigger (AD-27 immutable proposal).
    assert "proposal is immutable" in sql
    assert "trg_operation_preparation_immutable" in sql
    assert sql.strip().startswith("-- Story 36.13")
    assert "BEGIN;" in sql and "COMMIT;" in sql
