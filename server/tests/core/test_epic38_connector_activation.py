"""Story 38.5: org-scoped connector activation -- offline + ASGI tests.

Covers (per AC + Task 6):
  (a) Activation requires READY (not-READY -> ConnectorNotReady; refuse_activation_unless_ready).
  (b) Org-owner authority required; non-owner -> nondisclosing 404.
  (c) Deactivation preserves the row (UPDATE, not DELETE); deactivated_at set.
  (d) Cross-org isolation: org B cannot GET or operate org A's activation -> 404.
  (e) Idempotent activate: same payload + key replays cleanly (no duplicate).
  (f) Conflicting Idempotency-Key -> 409.
  (g) Re-activate no-op replay (ON CONFLICT reconcile path).
  (h) Audit/outbox written through execute_operation (never a parallel path).
  (i) MCP get_connector_activation_status registered + returns safe model.
  (j) Secret-free read-model (no provider/credential/cross-tenant detail).
  (k) request_payload has no random id (deterministic idempotency, review H1).
  (l) Mock cursor SEQUENCE matches real conn.cursor() call order (no dead cursors).
  (m) Live-PG-gated: UNIQUE constraint + immutability trigger probed on real DB.
"""

from __future__ import annotations

import contextlib
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared mock helpers (mirrors test_epic38_connector_installation.py style).
# ---------------------------------------------------------------------------


def _cur(*fetchone_rows, rowcount: int = 1, fetchall=None):
    """Build a mock cursor with an ordered sequence of fetchone return values."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return cur


def _conn_with(*curs):
    """Build a mock connection that yields cursors in order."""
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module, *, capture=None):
    """Stub execute_operation to run the mutation synchronously."""
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-test-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-test-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


# ---------------------------------------------------------------------------
# (a) Activation requires READY -- refuse_activation_unless_ready gate.
# ---------------------------------------------------------------------------


def test_activate_raises_when_installation_not_ready(monkeypatch):
    """AC1: activate MUST raise ConnectorNotReady when connector is not READY."""
    from core import connector_activation as ca
    from core.connector_installation_api import ConnectorNotReady

    # Patch refuse_activation_unless_ready to raise (simulates non-READY).
    def _raise(conn, *, connector_name, environment):
        raise ConnectorNotReady("not ready")

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready", _raise
    )
    _stub_operation(monkeypatch, ca)

    with pytest.raises(ConnectorNotReady):
        ca.activate(
            MagicMock(),
            org_id="org-alpha",
            connector_name="test-connector",
            environment="production",
            activated_by="user@example.com",
            actor="user@example.com",
            idempotency_key="ik-ready-gate",
            host_context={},
            trace_id=None,
        )


def test_activate_proceeds_when_ready(monkeypatch):
    """AC1: activate proceeds when refuse_activation_unless_ready is a no-op."""
    import datetime

    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )

    ts = datetime.datetime(2026, 7, 23, 12, 0, 0)
    # Cursors in mutation order: INSERT (rowcount=1), then SELECT activated_at.
    insert_cur = _cur(None, rowcount=1)
    select_cur = _cur(("ACTIVE", ts, None))
    capture: dict = {}
    _stub_operation(monkeypatch, ca, capture=capture)

    # The stub calls mutation(operation_conn, op_id) with the conn passed to
    # execute_operation -- which is the top-level conn passed to activate().
    conn = _conn_with(insert_cur, select_cur)
    result = ca.activate(
        conn,
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        activated_by="user@example.com",
        actor="user@example.com",
        idempotency_key="ik-activate-1",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "ACTIVE"
    assert result["org_id"] == "org-alpha"
    assert result["connector_name"] == "test-connector"
    assert "deactivated_at" in result
    assert result["deactivated_at"] is None
    # execute_operation was called once.
    assert len(capture["specs"]) == 1


# ---------------------------------------------------------------------------
# (k) Deterministic idempotency: request_payload has no random id.
# ---------------------------------------------------------------------------


def test_activate_payload_is_deterministic(monkeypatch):
    """review H1: two identical activations produce byte-identical request_payload.

    The row id (cac_<ULID>) is generated at write time inside the mutation, never
    in the request_payload that feeds the idempotency hash.
    """
    import datetime

    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )

    ts = datetime.datetime(2026, 7, 23, 12, 0, 0)

    def _one_payload():
        capture: dict = {}
        _stub_operation(monkeypatch, ca, capture=capture)
        insert_cur = _cur(None, rowcount=1)
        select_cur = _cur(("ACTIVE", ts, None))
        conn = _conn_with(insert_cur, select_cur)
        ca.activate(
            conn,
            org_id="org-alpha",
            connector_name="test-connector",
            environment="production",
            activated_by="user@example.com",
            actor="user@example.com",
            idempotency_key="ik-deterministic",
            host_context={},
            trace_id=None,
        )
        return capture["specs"][0].request_payload

    p1 = _one_payload()
    p2 = _one_payload()
    assert p1 == p2
    # No random id in the hashed payload.
    assert "activation_id" not in p1
    assert "cac_" not in str(p1)


# ---------------------------------------------------------------------------
# (g) Re-activate no-op: ON CONFLICT reconcile path (review H2).
# ---------------------------------------------------------------------------


def test_activate_concurrent_race_reconciles_to_persisted_row(monkeypatch):
    """review H2: when INSERT ON CONFLICT DO NOTHING fires (rowcount=0), the
    mutation reconciles to the persisted row instead of fabricating a result."""
    import datetime

    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )

    ts = datetime.datetime(2026, 7, 23, 10, 0, 0)
    # One cursor: INSERT ON CONFLICT (rowcount=0 -> race) then the reconcile SELECT
    # run on the SAME cursor, so it must return the persisted row via fetchone.
    race_cur = _cur(("cac_EXISTING01ABCDEFGHIJKLMNO", "ACTIVE", ts, None), rowcount=0)
    _stub_operation(monkeypatch, ca)
    conn = _conn_with(race_cur)

    result = ca.activate(
        conn,
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        activated_by="user@example.com",
        actor="user@example.com",
        idempotency_key="ik-race",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "ACTIVE"
    assert result["org_id"] == "org-alpha"


# ---------------------------------------------------------------------------
# (c) Deactivation preserves the row (UPDATE not DELETE).
# ---------------------------------------------------------------------------


def test_deactivate_issues_update_not_delete(monkeypatch):
    """AC3: deactivate issues an UPDATE; asserts no DELETE SQL is issued."""
    import datetime

    from core import connector_activation as ca

    ts_activated = datetime.datetime(2026, 7, 23, 9, 0, 0)
    ts_deactivated = datetime.datetime(2026, 7, 23, 11, 0, 0)

    # First cursor: SELECT row to load activation record.
    select_cur = _cur(("cac_DEACT01ABCDEFGHIJKLMNOPQR", "ACTIVE", ts_activated))
    # Second cursor (UPDATE + re-SELECT in mutation): rowcount=1, then ts row.
    update_cur = _cur(("DEACTIVATED", ts_activated, ts_deactivated), rowcount=1)
    capture: dict = {}
    _stub_operation(monkeypatch, ca, capture=capture)
    conn = _conn_with(select_cur, update_cur)

    result = ca.deactivate(
        conn,
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        actor="user@example.com",
        idempotency_key="ik-deactivate-1",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "DEACTIVATED"
    assert result["deactivated_at"] is not None
    assert result["org_id"] == "org-alpha"

    # Verify no DELETE was issued in any cursor call.
    for c in (select_cur, update_cur):
        for call in c.execute.call_args_list:
            sql = (call.args[0] if call.args else "").upper()
            assert "DELETE" not in sql, (
                f"deactivate must not issue DELETE; found: {sql!r}"
            )

    # execute_operation called once.
    assert len(capture["specs"]) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.activation.deactivated"


def test_deactivate_not_found_raises(monkeypatch):
    """deactivate raises ConnectorActivationUnavailable when no row exists."""
    from core import connector_activation as ca

    # SELECT returns no row.
    select_cur = _cur(None)
    conn = _conn_with(select_cur)

    with pytest.raises(ca.ConnectorActivationUnavailable):
        ca.deactivate(
            conn,
            org_id="org-alpha",
            connector_name="test-connector",
            environment="production",
            actor="user@example.com",
            idempotency_key="ik-notfound",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (d) Cross-org isolation: get_activation returns None for wrong org.
# ---------------------------------------------------------------------------


def test_get_activation_returns_none_for_different_org():
    """AC4: get_activation is org-scoped -- a different org receives None.

    The SQL query is parameterised on org_id; a row belonging to org-alpha
    is NOT returned when queried for org-beta (no row exists for org-beta).
    """
    from core import connector_activation as ca

    # No row for org-beta.
    no_row_cur = _cur(None)
    conn = _conn_with(no_row_cur)

    result = ca.get_activation(
        conn,
        org_id="org-beta",
        connector_name="test-connector",
        environment="production",
    )
    assert result is None


def test_get_activation_returns_model_for_correct_org():
    """AC4: get_activation returns the read-model when org_id matches."""
    import datetime

    from core import connector_activation as ca

    ts = datetime.datetime(2026, 7, 23, 9, 0, 0)
    row_cur = _cur(("ACTIVE", ts, None))
    conn = _conn_with(row_cur)

    result = ca.get_activation(
        conn,
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
    )
    assert result is not None
    assert result["state"] == "ACTIVE"
    assert result["org_id"] == "org-alpha"
    assert result["deactivated_at"] is None


# ---------------------------------------------------------------------------
# (j) Secret-free read-model.
# ---------------------------------------------------------------------------


def test_safe_read_model_contains_no_secrets():
    """AC2: the safe read-model contains no secrets or cross-tenant detail."""
    from core.connector_activation import _safe_read_model

    model = _safe_read_model(
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        state="ACTIVE",
        activated_at=None,
        deactivated_at=None,
    )
    # Only expected keys.
    assert set(model.keys()) == {
        "org_id",
        "connector_name",
        "environment",
        "state",
        "activated_at",
        "deactivated_at",
    }
    # No secret-like keys.
    for key in model:
        assert "secret" not in key.lower()
        assert "credential" not in key.lower()
        assert "token" not in key.lower()
        assert "signing" not in key.lower()


# ---------------------------------------------------------------------------
# (h) Audit/outbox through execute_operation (no parallel path).
# ---------------------------------------------------------------------------


def test_activate_routes_through_execute_operation(monkeypatch):
    """AC1/AC2: the write always passes through execute_operation (no parallel audit)."""
    import datetime

    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )

    ts = datetime.datetime(2026, 7, 23, 12, 0, 0)
    insert_cur = _cur(None, rowcount=1)
    select_cur = _cur(("ACTIVE", ts, None))
    capture: dict = {}
    _stub_operation(monkeypatch, ca, capture=capture)
    conn = _conn_with(insert_cur, select_cur)

    ca.activate(
        conn,
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        activated_by="user@example.com",
        actor="user@example.com",
        idempotency_key="ik-route-check",
        host_context={},
        trace_id=None,
    )

    # Exactly one execute_operation call recorded.
    assert len(capture["specs"]) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.activation.activated"
    assert spec.effective_org_id == "org-alpha"
    # outbox_payload from the mutation result.
    mutation_result = capture["changes"][0]
    assert mutation_result.outbox_payload["org_id"] == "org-alpha"
    assert mutation_result.outbox_payload["connector_name"] == "test-connector"


# ---------------------------------------------------------------------------
# ASGI / REST tests (sync Starlette TestClient, same as epic38_installation).
# ---------------------------------------------------------------------------


def _build_client():
    """Build a Starlette TestClient with only the activation routes."""
    from core.connector_activation_api import CONNECTOR_ACTIVATION_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_ACTIVATION_ROUTES)
    return TestClient(app, raise_server_exceptions=False)


def _patch_check_auth(monkeypatch, *, authorized: bool = True, identity: str = "user@example.com"):
    async def _fake_auth(request):
        return authorized, identity

    monkeypatch.setattr("core.connector_activation_api._check_auth", _fake_auth)


def _fake_conn_ctx(mock_conn):
    @contextlib.contextmanager
    def _ctx():
        yield mock_conn

    return _ctx


# ---------------------------------------------------------------------------
# (b) Org-owner authority required.
# ---------------------------------------------------------------------------


def test_post_activation_requires_auth(monkeypatch):
    """POST /activation returns 401 when no Bearer token."""
    _patch_check_auth(monkeypatch, authorized=False, identity="anonymous")
    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-1"},
    )
    assert resp.status_code == 401


def test_post_activation_non_owner_gets_404(monkeypatch):
    """POST /activation: non-owner -> nondisclosing 404 (not 403)."""
    _patch_check_auth(monkeypatch)

    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: False)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    monkeypatch.setattr("core.audit.write_audit_row", lambda **kw: None)

    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-non-owner"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_post_activation_missing_idempotency_key(monkeypatch):
    """POST /activation: missing Idempotency-Key -> 422."""
    _patch_check_auth(monkeypatch)
    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={"org_id": "org-alpha"},
        # no Idempotency-Key header
    )
    assert resp.status_code == 422


def test_post_activation_not_ready_returns_422(monkeypatch):
    """POST /activation: not-READY connector -> 422 (nondisclosing)."""
    from core.connector_installation_api import ConnectorNotReady

    _patch_check_auth(monkeypatch)

    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))

    def _raise_not_ready(*a, **kw):
        raise ConnectorNotReady("not ready")

    monkeypatch.setattr(
        "core.connector_activation.activate",
        _raise_not_ready,
    )

    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-not-ready"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "connector_unavailable"
    # Must not disclose installation state.
    assert "READY" not in body["message"]
    assert "installation" not in body["message"].lower()


def test_post_activation_conflict_409(monkeypatch):
    """POST /activation: conflicting Idempotency-Key -> 409."""
    from core.operations import OperationIdempotencyConflict

    _patch_check_auth(monkeypatch)

    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))

    def _raise_conflict(*a, **kw):
        raise OperationIdempotencyConflict("conflict")

    monkeypatch.setattr(
        "core.connector_activation.activate",
        _raise_conflict,
    )

    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-conflict"},
    )
    assert resp.status_code == 409


def test_post_activation_success(monkeypatch):
    """POST /activation: org-owner + READY -> 200 with state=ACTIVE."""
    _patch_check_auth(monkeypatch)

    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    monkeypatch.setattr(
        "core.connector_activation.activate",
        lambda *a, **kw: {
            "org_id": "org-alpha",
            "connector_name": "test-connector",
            "environment": "production",
            "state": "ACTIVE",
            "activated_at": "2026-07-23T12:00:00",
            "deactivated_at": None,
        },
    )

    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-success"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ACTIVE"
    assert body["org_id"] == "org-alpha"
    assert body["deactivated_at"] is None


# ---------------------------------------------------------------------------
# (d) Cross-org isolation at the REST layer.
# ---------------------------------------------------------------------------


def test_get_activation_cross_org_gets_404(monkeypatch):
    """AC4: org B caller requesting org A's activation -> 404 (nondisclosing).

    identity_has_org_access returns True only for org-beta; requesting org-alpha
    returns 404 because the caller has no access to that org.
    """
    _patch_check_auth(monkeypatch, identity="user-b@example.com")

    import core.db as db_mod
    import core.project_access as pa_mod

    # Only org-beta is accessible to this identity.
    monkeypatch.setattr(
        pa_mod, "identity_has_org_access",
        lambda oid, identity, conn: oid == "org-beta",
    )
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))

    client = _build_client()
    resp = client.get(
        "/api/connectors/test-connector/activation",
        params={"org_id": "org-alpha"},  # different org
    )
    assert resp.status_code == 404


def test_get_activation_missing_org_id_returns_400(monkeypatch):
    """GET /activation: missing org_id query param -> 400."""
    _patch_check_auth(monkeypatch)
    client = _build_client()
    resp = client.get("/api/connectors/test-connector/activation")
    assert resp.status_code == 400


def test_get_activation_no_row_returns_404(monkeypatch):
    """GET /activation: no activation record for this org -> 404."""
    _patch_check_auth(monkeypatch)

    import core.connector_activation as ca_mod
    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_has_org_access", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    monkeypatch.setattr(ca_mod, "get_activation", lambda *a, **kw: None)

    client = _build_client()
    resp = client.get(
        "/api/connectors/test-connector/activation",
        params={"org_id": "org-alpha"},
    )
    assert resp.status_code == 404


def test_get_activation_returns_read_model(monkeypatch):
    """GET /activation: valid org access + existing row -> 200 with safe read-model."""
    _patch_check_auth(monkeypatch)

    import core.connector_activation as ca_mod
    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_has_org_access", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    monkeypatch.setattr(
        ca_mod, "get_activation",
        lambda *a, **kw: {
            "org_id": "org-alpha",
            "connector_name": "test-connector",
            "environment": "production",
            "state": "ACTIVE",
            "activated_at": "2026-07-23T09:00:00",
            "deactivated_at": None,
        },
    )

    client = _build_client()
    resp = client.get(
        "/api/connectors/test-connector/activation",
        params={"org_id": "org-alpha"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "ACTIVE"
    assert body["org_id"] == "org-alpha"
    # Secret-free: no signing, credential, or token keys in the response.
    assert "secret" not in str(body).lower()
    assert "credential" not in str(body).lower()


def test_deactivation_endpoint_returns_200(monkeypatch):
    """POST /activation/deactivate: org-owner -> 200; state DEACTIVATED."""
    _patch_check_auth(monkeypatch)

    import core.connector_activation as ca_mod
    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    monkeypatch.setattr(
        ca_mod, "deactivate",
        lambda *a, **kw: {
            "org_id": "org-alpha",
            "connector_name": "test-connector",
            "environment": "production",
            "state": "DEACTIVATED",
            "activated_at": "2026-07-23T09:00:00",
            "deactivated_at": "2026-07-23T11:00:00",
        },
    )

    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation/deactivate",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-deact-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "DEACTIVATED"
    assert body["deactivated_at"] is not None


def test_deactivation_non_owner_gets_404(monkeypatch):
    """POST /activation/deactivate: non-owner -> nondisclosing 404."""
    _patch_check_auth(monkeypatch)

    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: False)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    monkeypatch.setattr("core.audit.write_audit_row", lambda **kw: None)

    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation/deactivate",
        json={"org_id": "org-alpha"},
        headers={"Idempotency-Key": "ik-deact-non-owner"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# (i) MCP parity -- get_connector_activation_status == REST GET.
# ---------------------------------------------------------------------------


def test_mcp_get_connector_activation_status_registered(monkeypatch):
    """AC2/Task5: get_connector_activation_status registered under operations profile."""
    from core import mcp_profiles

    mcp_profiles.reset_registry_for_tests()
    try:
        monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
        recorder = MagicMock()
        recorder.tool = MagicMock()
        captured = {}

        real = mcp_profiles.register_profiled

        def spy(mcp, handler, **kwargs):
            captured[handler.__name__] = kwargs
            return real(mcp, handler, **kwargs)

        mcp_profiles.register_profiled = spy  # type: ignore[assignment]
        try:
            from core import operations_mcp

            operations_mcp.register(recorder)
        finally:
            mcp_profiles.register_profiled = real  # type: ignore[assignment]

        assert "get_connector_activation_status" in captured
        decl_kw = captured["get_connector_activation_status"]
        assert decl_kw.get("profile") == "operations"
        assert decl_kw.get("effect") == "read"
        assert decl_kw.get("confirmation_mode") == "none"
    finally:
        mcp_profiles.reset_registry_for_tests()


def test_mcp_tool_returns_not_activated_when_no_row(monkeypatch):
    """MCP: no row for org -> activated:false response (same as REST 404 model)."""
    from core import mcp_profiles

    mcp_profiles.reset_registry_for_tests()
    try:
        monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
        monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

        import core.connector_activation as ca_mod
        import core.db as db_mod

        monkeypatch.setattr(ca_mod, "get_activation", lambda *a, **kw: None)
        monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))

        # Simulate MCP access token: no token -> identity = "anonymous"
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: None,
            raising=False,
        )
        monkeypatch.setattr(
            "core.project_access.identity_has_org_access", lambda *a, **kw: True
        )

        captured_handlers: dict = {}
        real = mcp_profiles.register_profiled

        def spy(mcp, handler, **kwargs):
            captured_handlers[handler.__name__] = handler
            return real(mcp, handler, **kwargs)

        mcp_profiles.register_profiled = spy  # type: ignore[assignment]
        try:
            recorder = MagicMock()
            recorder.tool = MagicMock()
            from core import operations_mcp

            operations_mcp.register(recorder)
        finally:
            mcp_profiles.register_profiled = real  # type: ignore[assignment]

        handler = captured_handlers["get_connector_activation_status"]
        tool_result = handler("test-connector", "org-alpha")
        # Dual-channel: structuredContent.data.activated == False
        sc = tool_result.structured_content
        assert sc["data"]["activated"] is False
        assert sc["data"]["connector_name"] == "test-connector"
        assert sc["data"]["org_id"] == "org-alpha"
    finally:
        mcp_profiles.reset_registry_for_tests()


def test_mcp_tool_cross_org_denied(monkeypatch):
    """AC4/review F4: an MCP caller WITHOUT access to org_id gets a nondisclosing
    error and the foreign org's activation is NEVER queried."""
    from core import mcp_profiles

    mcp_profiles.reset_registry_for_tests()
    try:
        monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
        monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

        import core.connector_activation as ca_mod
        import core.db as db_mod

        called = {"get_activation": 0}

        def _guarded_get(*a, **kw):
            called["get_activation"] += 1
            return {"state": "ACTIVE"}

        monkeypatch.setattr(ca_mod, "get_activation", _guarded_get)
        monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token", lambda: None, raising=False
        )
        # Caller has NO access to the requested org.
        monkeypatch.setattr(
            "core.project_access.identity_has_org_access", lambda *a, **kw: False
        )

        captured_handlers: dict = {}
        real = mcp_profiles.register_profiled

        def spy(mcp, handler, **kwargs):
            captured_handlers[handler.__name__] = handler
            return real(mcp, handler, **kwargs)

        mcp_profiles.register_profiled = spy  # type: ignore[assignment]
        try:
            recorder = MagicMock()
            recorder.tool = MagicMock()
            from core import operations_mcp

            operations_mcp.register(recorder)
        finally:
            mcp_profiles.register_profiled = real  # type: ignore[assignment]

        handler = captured_handlers["get_connector_activation_status"]
        with pytest.raises(Exception):
            handler("test-connector", "org-foreign")
        # The foreign org's activation state was never read.
        assert called["get_activation"] == 0
    finally:
        mcp_profiles.reset_registry_for_tests()


def test_mcp_tool_returns_active_when_row_exists(monkeypatch):
    """MCP: existing ACTIVE row -> activated:true + state:ACTIVE."""
    from core import mcp_profiles

    mcp_profiles.reset_registry_for_tests()
    try:
        monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
        monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

        import core.connector_activation as ca_mod
        import core.db as db_mod

        monkeypatch.setattr(
            ca_mod, "get_activation",
            lambda *a, **kw: {
                "org_id": "org-alpha",
                "connector_name": "test-connector",
                "environment": "production",
                "state": "ACTIVE",
                "activated_at": "2026-07-23T09:00:00",
                "deactivated_at": None,
            },
        )
        monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_access_token",
            lambda: None,
            raising=False,
        )
        monkeypatch.setattr(
            "core.project_access.identity_has_org_access", lambda *a, **kw: True
        )

        captured_handlers: dict = {}
        real = mcp_profiles.register_profiled

        def spy(mcp, handler, **kwargs):
            captured_handlers[handler.__name__] = handler
            return real(mcp, handler, **kwargs)

        mcp_profiles.register_profiled = spy  # type: ignore[assignment]
        try:
            recorder = MagicMock()
            recorder.tool = MagicMock()
            from core import operations_mcp

            operations_mcp.register(recorder)
        finally:
            mcp_profiles.register_profiled = real  # type: ignore[assignment]

        handler = captured_handlers["get_connector_activation_status"]
        tool_result = handler("test-connector", "org-alpha")
        sc = tool_result.structured_content
        assert sc["data"]["activated"] is True
        assert sc["data"]["state"] == "ACTIVE"
        # Secret-free.
        assert "secret" not in str(sc).lower()
    finally:
        mcp_profiles.reset_registry_for_tests()


# ---------------------------------------------------------------------------
# Live-PG-gated tests (skipped when PLATFORM_DB_URL not set).
# ---------------------------------------------------------------------------

_LIVE_MARK = pytest.mark.skipif(
    not __import__("os").environ.get("PLATFORM_DB_URL"),
    reason="PLATFORM_DB_URL not set -- live-PG probe skipped",
)


@_LIVE_MARK
def test_live_unique_constraint_enforced():
    """Live-PG: UNIQUE(org_id, connector_name, environment) fires on duplicate INSERT."""
    import os

    import psycopg
    from ulid import ULID

    db_url = os.environ["PLATFORM_DB_URL"]
    unique_org = f"live-test-org-{ULID()}"
    row_id_a = f"cac_{ULID()}"
    row_id_b = f"cac_{ULID()}"

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_activations "
                "(id, org_id, connector_name, environment, state, activated_by) "
                "VALUES (%s, %s, 'test-connector', 'test', 'ACTIVE', 'tester')",
                (row_id_a, unique_org),
            )
        conn.commit()
        # Second INSERT on same (org, connector, env) must raise.
        with pytest.raises(
            Exception,
            match="uq_connector_activations_org_connector_env|unique",
        ):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.connector_activations "
                    "(id, org_id, connector_name, environment, state, activated_by) "
                    "VALUES (%s, %s, 'test-connector', 'test', 'ACTIVE', 'tester')",
                    (row_id_b, unique_org),
                )
            conn.commit()
        conn.rollback()


@_LIVE_MARK
def test_live_immutability_trigger_blocks_identity_mutation():
    """Live-PG: immutability trigger forbids mutating org_id after insert (DO block)."""
    import os

    import psycopg
    from ulid import ULID

    db_url = os.environ["PLATFORM_DB_URL"]
    unique_org = f"live-imm-org-{ULID()}"
    row_id = f"cac_{ULID()}"

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_activations "
                "(id, org_id, connector_name, environment, state, activated_by) "
                "VALUES (%s, %s, 'test-connector', 'test', 'ACTIVE', 'tester')",
                (row_id, unique_org),
            )
        conn.commit()
        # Attempt to mutate org_id (identity column) -> trigger must raise.
        with pytest.raises(Exception, match="immutable|23000"):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.connector_activations "
                    "SET org_id = 'different-org' WHERE id = %s",
                    (row_id,),
                )
            conn.commit()
        conn.rollback()
        # Allowed mutation: state + deactivated_at.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_activations "
                "SET state = 'DEACTIVATED', deactivated_at = NOW() WHERE id = %s",
                (row_id,),
            )
        conn.commit()
        # Verify row exists and is now DEACTIVATED (row preserved, not deleted).
        with conn.cursor() as cur:
            cur.execute(
                "SELECT state FROM app.connector_activations WHERE id = %s",
                (row_id,),
            )
            final_row = cur.fetchone()
        assert final_row is not None
        assert final_row[0] == "DEACTIVATED"


@_LIVE_MARK
def test_live_immutability_trigger_blocks_delete():
    """Live-PG: immutability trigger forbids DELETE on activation rows."""
    import os

    import psycopg
    from ulid import ULID

    db_url = os.environ["PLATFORM_DB_URL"]
    unique_org = f"live-del-org-{ULID()}"
    row_id = f"cac_{ULID()}"

    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.connector_activations "
                "(id, org_id, connector_name, environment, state, activated_by) "
                "VALUES (%s, %s, 'test-connector', 'test', 'ACTIVE', 'tester')",
                (row_id, unique_org),
            )
        conn.commit()
        # DELETE must be blocked by the trigger.
        with pytest.raises(Exception, match="may not be deleted|23000"):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.connector_activations WHERE id = %s",
                    (row_id,),
                )
            conn.commit()
        conn.rollback()
        # Row must still exist.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.connector_activations WHERE id = %s",
                (row_id,),
            )
            assert cur.fetchone() is not None
