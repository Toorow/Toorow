"""Story 36.14 host preflight, install handoff and capability-context binding tests.

Offline MagicMock-style, mirroring test_epic36_source_delegation.py: the shared
operation seam, the handoff carrier, the capability-context binding and the setup
task reconciliation are stubbed so the preflight state machine and its invariants
are exercised without live Postgres.

Covers:
  (a) preflight records dated capabilities/plan/role/UI-support AND ordering is
      capability-driven, NOT brand-first (two hosts differing only by name get
      identical treatment);
  (b) operator without host-admin authority -> a minimal purpose-scoped handoff
      (no Toorow data in it);
  (c) a successful bind writes the mcp_capability_contexts binding with
      endpoint/org/policy/catalog version through execute_operation;
  (d) unsupported app UI -> standard-tool fallback with bounded evidence + a
      deep-link that is additional-only;
  (e) a policy/capability change invalidates a stale preflight and high-risk
      profiles stay hidden (bind refuses on a stale preflight);
  (f) the callback reconciles SERVER state (not a client callback).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


def _cur(*fetchone_rows, fetchall=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = 1
    return cur


def _conn_with(*curs):
    """Return a conn whose successive .cursor() calls yield the given cursors."""
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module, *, capture=None):
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


# ---------------------------------------------------------------------------
# (a) preflight records dated capabilities/plan/role/UI-support; capability-driven,
#     NOT brand-first (two hosts differing only by name -> identical treatment).
# ---------------------------------------------------------------------------
def test_preflight_records_dated_capabilities_and_ui_support(monkeypatch):
    from core import host_preflight as hp

    # task row: id, journey_id, org_id, state, step_key, return_condition
    task_row = ("task-h", "journey-1", "org-1", "waiting", "host_connection",
                {"kind": "host_connected", "resource_id": "proj-1"})
    lookup = _cur(task_row)
    insert = _cur()
    conn = _conn_with(lookup, insert)
    capture: dict = {}
    _stub_operation(monkeypatch, hp, capture=capture)

    result = hp.preflight_host(
        conn,
        host_key="host_app_ui_v1",
        task_id="task-h",
        org_id="org-1",
        project_id="proj-1",
        actor="op@example.com",
        idempotency_key="pf-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result["state"] == "prepared"
    assert result["dated_at"]  # dated entry (AC1)
    assert result["capabilities"]  # capability facts recorded
    assert result["plan_constraints"]  # plan constraints recorded
    assert result["required_role"] == "host_workspace_admin"  # required role recorded
    assert result["ui_support"]["mode"] == "app_ui"  # UI-support decision recorded
    assert result["workspace_proof_required"] is True  # workspace-proof requirement

    spec = capture["specs"][0]
    assert spec.command_type == "host.preflight.prepare"
    assert spec.effective_org_id == "org-1"
    # The persisted INSERT carries dated capability facts, not brand behaviour.
    insert_sql = " ".join(call.args[0] for call in insert.execute.call_args_list)
    assert "app.host_preflights" in insert_sql
    assert "dated_at" in insert_sql
    assert "required_role" in insert_sql


def test_ordering_is_capability_driven_not_brand_first(monkeypatch):
    """Two hosts differing ONLY by name receive identical treatment (E36-NFR03)."""
    from core import host_preflight as hp

    base = hp._HOST_CATALOG["host_app_ui_v1"]
    twin = hp.HostCapabilityEntry(
        host_key=base.host_key,
        display_name="A Totally Different Brand Name",  # only the name differs
        dated_at=base.dated_at,
        capabilities=base.capabilities,
        plan_constraints=base.plan_constraints,
        required_role=base.required_role,
        app_ui_supported=base.app_ui_supported,
        catalog_refresh=base.catalog_refresh,
        workspace_proof_required=base.workspace_proof_required,
    )

    # The UI-support decision reads capability flags only -- identical for both.
    assert hp.resolve_ui_support(base) == hp.resolve_ui_support(twin)

    # The catalog is an unordered MAPPING -- there is no "first host" list to bias.
    catalog = hp.host_catalog()
    assert isinstance(catalog, dict)
    # display_name never gates: strip names and the capability records are identical.
    a = {k: v for k, v in base.as_record().items() if k != "display_name"}
    b = {k: v for k, v in twin.as_record().items() if k != "display_name"}
    assert a == b


# ---------------------------------------------------------------------------
# (b) operator without host-admin authority -> minimal purpose-scoped handoff,
#     NO Toorow data in it.
# ---------------------------------------------------------------------------
def test_prepare_install_handoff_is_minimal_purpose_scoped(monkeypatch):
    from core import host_preflight as hp

    # preflight row: id, org_id, task_id, required_role, state, expires_at
    pf_row = ("hostpf-1", "org-1", "task-h", "host_workspace_admin", "prepared", None)
    lookup = _cur(pf_row)
    # _transition does its own SELECT ... FOR UPDATE then UPDATE.
    trans_lookup = _cur(("prepared", "org-1", "host_app_ui_v1"))
    trans_update = _cur()
    conn = _conn_with(lookup, trans_lookup, trans_update)
    _stub_operation(monkeypatch, hp)

    # The handoff carrier is the seam that guarantees no Toorow data leaks: stub it
    # and assert this module passes ONLY the task id + expiry + idempotency to it.
    handoff = MagicMock()
    handoff.handoff_id = "handoff-1"
    handoff.state = "created"
    handoff.expires_at = "2026-07-25T00:00:00+00:00"
    handoff.delivery_url = "https://console.toorow.test/handoff#handoff=abc"
    handoff.operation_id = "op-h"
    handoff.audit_event_id = "audit-h"
    handoff.replayed = False
    prepare_handoff = MagicMock(return_value=handoff)
    monkeypatch.setattr(
        "core.setup_responsibilities.prepare_handoff", prepare_handoff
    )

    result = hp.prepare_host_install_handoff(
        conn,
        preflight_id="hostpf-1",
        actor="op@example.com",
        idempotency_key="hk-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result["state"] == "handed_off"
    # The administrator action is NAMED with role + return condition, no data.
    assert result["admin_action"]["action"] == "install_mcp_host"
    assert result["admin_action"]["required_role"] == "host_workspace_admin"
    assert result["admin_action"]["return_condition"]["kind"] == "host_connected"

    # The handoff carrier received ONLY the bound task id + expiry + idempotency:
    # no report, no org data, no profiles, no token.
    _, kwargs = prepare_handoff.call_args
    assert kwargs["task_id"] == "task-h"
    assert set(kwargs) <= {
        "task_id", "actor", "expires_in_hours", "idempotency_key",
        "host_context", "trace_id",
    }
    # The result surface carries no Toorow data / secret-looking keys.
    forbidden = {"report", "rows", "sample", "token", "secret", "credential", "data"}
    assert not (set(result) & forbidden)


# ---------------------------------------------------------------------------
# (c) successful bind writes the mcp_capability_contexts binding with
#     endpoint/org/policy/catalog version through execute_operation.
# ---------------------------------------------------------------------------
def test_bind_writes_capability_context_with_versions(monkeypatch):
    from core import host_preflight as hp

    # preflight row: id, host_key, org_id, project_id, task_id, proof_required, state
    pf_row = ("hostpf-1", "host_app_ui_v1", "org-1", "proj-1", "task-h", True, "handed_off")
    lookup = _cur(pf_row)
    # mutation: INSERT context, UPDATE preflight (rowcount 1), then _reconcile_host_task
    mut = _cur()
    # _reconcile_host_task: SELECT state,return_condition then UPDATE
    recon_lookup = _cur(("waiting", {"kind": "host_connected", "resource_id": "task-h"}))
    recon_update = _cur()
    conn = _conn_with(lookup, mut, recon_lookup, recon_update)
    capture: dict = {}
    _stub_operation(monkeypatch, hp, capture=capture)
    monkeypatch.setattr("core.mcp_profiles.catalog_version", lambda cc: f"catver-{cc}")

    evidence = "a" * 64  # valid 64-hex workspace evidence hash
    result = hp.bind_host_connection(
        conn,
        preflight_id="hostpf-1",
        endpoint_binding="mcp://admin-endpoint",
        enabled_profiles=["insights", "operations"],
        workspace_evidence_hash=evidence,
        host="opaque-host",
        workspace_id="ws-1",
        workspace_type="team",
        client_id="cli-1",
        policy_version="policy-v7",
        actor="op@example.com",
        idempotency_key="bind-1",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert result["state"] == "bound"
    assert result["capability_context_id"].startswith("mcpctx_")
    assert result["endpoint_binding"] == "mcp://admin-endpoint"
    assert result["org_id"] == "org-1"
    assert result["policy_version"] == "policy-v7"
    # High-risk requested + proof present + host requires proof -> operations bound.
    assert "operations" in result["enabled_profiles"]
    assert result["connection_class"] == "admin"
    assert result["catalog_version"] == "catver-admin"  # from mcp_profiles.catalog_version
    assert result["workspace_proof_bound"] is True

    spec = capture["specs"][0]
    assert spec.command_type == "host.preflight.bind"
    assert spec.versions["policy"] == "policy-v7"
    assert spec.versions["catalog"] == "catver-admin"
    # The INSERT targets the Story 36.11 context table with the binding columns.
    insert_sql = " ".join(call.args[0] for call in mut.execute.call_args_list)
    assert "app.mcp_capability_contexts" in insert_sql
    assert "endpoint_binding" in insert_sql
    assert "workspace_evidence_hash" in insert_sql


def test_bind_fails_closed_without_workspace_proof(monkeypatch):
    """No verifiable proof -> high-risk profiles are dropped; only insights binds."""
    from core import host_preflight as hp

    pf_row = ("hostpf-1", "host_app_ui_v1", "org-1", "proj-1", "task-h", True, "handed_off")
    lookup = _cur(pf_row)
    mut = _cur()
    recon_lookup = _cur(("waiting", {"kind": "host_connected", "resource_id": "task-h"}))
    recon_update = _cur()
    conn = _conn_with(lookup, mut, recon_lookup, recon_update)
    _stub_operation(monkeypatch, hp)
    monkeypatch.setattr("core.mcp_profiles.catalog_version", lambda cc: f"catver-{cc}")

    result = hp.bind_host_connection(
        conn,
        preflight_id="hostpf-1",
        endpoint_binding="mcp://admin-endpoint",
        enabled_profiles=["insights", "operations", "governance"],
        workspace_evidence_hash=None,  # NO proof
        host="opaque-host",
        workspace_id="ws-1",
        workspace_type="team",
        client_id="cli-1",
        policy_version="policy-v7",
        actor="op@example.com",
        idempotency_key="bind-2",
        host_context={"host": "rest"},
        trace_id=None,
    )

    # Fail closed: high-risk profiles are NOT bound; only insights survives.
    assert result["enabled_profiles"] == ["insights"]
    assert result["connection_class"] == "read"
    assert result["catalog_version"] == "catver-read"
    assert result["workspace_proof_bound"] is False


# ---------------------------------------------------------------------------
# (d) unsupported app UI -> standard-tool fallback with bounded evidence +
#     deep-link that is additional-only.
# ---------------------------------------------------------------------------
def test_unsupported_app_ui_selects_standard_tool_fallback(monkeypatch):
    from core import host_preflight as hp

    task_row = ("task-h", "journey-1", "org-1", "waiting", "host_connection",
                {"kind": "host_connected", "resource_id": "proj-1"})
    lookup = _cur(task_row)
    insert = _cur()
    conn = _conn_with(lookup, insert)
    _stub_operation(monkeypatch, hp)

    result = hp.preflight_host(
        conn,
        host_key="host_standard_tools_v1",  # app_ui_supported = False
        task_id="task-h",
        org_id="org-1",
        project_id="proj-1",
        actor="op@example.com",
        idempotency_key="pf-2",
        host_context={"host": "rest"},
        trace_id=None,
    )

    ui = result["ui_support"]
    assert ui["mode"] == "standard_tool_fallback"
    # Bounded evidence is MANDATORY -- never a deep-link-only answer (AC4).
    assert ui["bounded_evidence_required"] is True
    assert ui["console_deep_link"] is not None
    assert ui["console_deep_link"]["additional_only"] is True


# ---------------------------------------------------------------------------
# (e) policy/capability change invalidates a stale preflight and high-risk
#     profiles stay hidden (bind refuses on a stale preflight).
# ---------------------------------------------------------------------------
def test_invalidate_stale_preflight_and_bind_refuses_when_stale(monkeypatch):
    from core import host_preflight as hp

    # invalidate: _transition SELECT then UPDATE. A pending (non-terminal) preflight
    # is invalidated to 'stale' on a policy/capability change; a bound context is
    # immutable and re-approval mints a NEW one instead.
    trans_lookup = _cur(("handed_off", "org-1", "host_app_ui_v1"))
    trans_update = _cur()
    conn = _conn_with(trans_lookup, trans_update)
    _stub_operation(monkeypatch, hp)

    result = hp.invalidate_stale_preflight(
        conn,
        preflight_id="hostpf-1",
        reason="policy_version_changed",
        actor="admin@example.com",
        idempotency_key="stale-1",
        host_context={"host": "rest"},
        trace_id=None,
    )
    assert result["state"] == "stale"

    # A stale preflight cannot bind -- high-risk profiles stay hidden until a fresh,
    # re-approved preflight mints a NEW context (36.11 keeps them hidden meanwhile).
    stale_row = ("hostpf-1", "host_app_ui_v1", "org-1", "proj-1", "task-h", True, "stale")
    conn2 = _conn_with(_cur(stale_row))
    _stub_operation(monkeypatch, hp)
    with pytest.raises(hp.HostPreflightConflict):
        hp.bind_host_connection(
            conn2,
            preflight_id="hostpf-1",
            endpoint_binding="mcp://admin-endpoint",
            enabled_profiles=["insights", "operations"],
            workspace_evidence_hash="a" * 64,
            host="opaque-host",
            workspace_id="ws-1",
            workspace_type="team",
            client_id="cli-1",
            policy_version="policy-v8",
            actor="op@example.com",
            idempotency_key="bind-3",
            host_context={"host": "rest"},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (f) the callback reconciles SERVER state (not a client callback).
# ---------------------------------------------------------------------------
def test_bind_reconciles_server_state_not_client_callback(monkeypatch):
    from core import host_preflight as hp

    pf_row = ("hostpf-1", "host_app_ui_v1", "org-1", "proj-1", "task-h", True, "handed_off")
    lookup = _cur(pf_row)
    mut = _cur()
    # The reconcile reads the task and advances it from SERVER evidence.
    recon_lookup = _cur(("waiting", {"kind": "host_connected", "resource_id": "task-h"}))
    recon_update = _cur()
    conn = _conn_with(lookup, mut, recon_lookup, recon_update)
    _stub_operation(monkeypatch, hp)
    monkeypatch.setattr("core.mcp_profiles.catalog_version", lambda cc: f"catver-{cc}")

    # Spy on reconcile_task_state: it must be called with SERVER evidence for the
    # bound resource, and NO client_payload is threaded through.
    calls: list = []
    real = None
    from core import setup_responsibilities as sr
    real = sr.reconcile_task_state

    def spy(**kwargs):
        calls.append(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(hp, "_reconcile_host_task", hp._reconcile_host_task)
    monkeypatch.setattr(sr, "reconcile_task_state", spy)

    hp.bind_host_connection(
        conn,
        preflight_id="hostpf-1",
        endpoint_binding="mcp://admin-endpoint",
        enabled_profiles=["insights"],
        workspace_evidence_hash=None,
        host="opaque-host",
        workspace_id="ws-1",
        workspace_type="team",
        client_id="cli-1",
        policy_version="policy-v7",
        actor="op@example.com",
        idempotency_key="bind-4",
        host_context={"host": "rest"},
        trace_id=None,
    )

    assert calls, "reconcile_task_state must be invoked"
    kwargs = calls[0]
    # Server evidence proves the host_connected condition; no client payload trusted.
    assert kwargs["server_evidence"] == {"host_connected": {"task-h": True}}
    assert "client_payload" not in kwargs or kwargs.get("client_payload") is None
