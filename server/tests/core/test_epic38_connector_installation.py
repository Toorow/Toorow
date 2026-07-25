"""Story 38.2: connector installation readiness state -- offline + ASGI tests.

Covers:
  (a) State-machine: legal transitions succeed; illegal transitions raise
      ConnectorInstallationConflict.
  (b) apply_installation is idempotent: same (env, connector_name) returns the
      existing row without a new operation; conflicting Idempotency-Key raises 409.
  (c) GET /installation: platform-admin sees full evidence; non-admin sees
      restricted nondisclosing projection.
  (d) POST /installation: non-super-admin -> 404 nondisclosing; missing
      Idempotency-Key -> 422.
  (e) Catalog availability: READY connector is `selectable`; non-READY is
      `unavailable`.
  (f) refuse_activation_unless_ready raises ConnectorNotReady when not READY.
  (g) Safe read-model is secret-free (no provider/credential/cross-tenant detail).
  (h) Audit/outbox written through execute_operation (never a parallel path).
  (i) MCP get_connector_installation_status returns the same safe model as REST.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _cur(*fetchone_rows, fetchall=None):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = 1
    return cur


def _conn_with(*curs):
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
# (a) State-machine transitions -- legal and illegal.
# ---------------------------------------------------------------------------


def test_apply_idempotency_payload_is_deterministic(monkeypatch):
    """review H1: two identical applies produce byte-identical hashed request
    payloads, so the same Idempotency-Key replays cleanly instead of colliding on
    a fresh random ULID. The row id is generated at write time, never hashed."""
    from core import connector_installation as ci

    def _payload_of_one_apply():
        capture: dict = {}
        _stub_operation(monkeypatch, ci, capture=capture)
        insert_cur = _cur(None)  # rowcount defaults to 1 -> INSERT "succeeds"
        ci.apply_installation(
            _conn_with(_cur(None), insert_cur),
            environment="production",
            connector_name="test-connector",
            responsible_actor=None,
            blocking_cause=None,
            actor="platform-admin@test",
            idempotency_key="ik-same",
            host_context={},
            trace_id=None,
        )
        return capture["specs"][0].request_payload

    first = _payload_of_one_apply()
    second = _payload_of_one_apply()
    assert first == second
    assert "installation_id" not in first  # no random id in the hashed inputs


def test_transition_map_allows_expected_paths():
    from core.connector_installation import _ALLOWED_TRANSITIONS, INSTALLATION_STATES

    # Forward happy path exists.
    assert "DOMAIN_PENDING" in _ALLOWED_TRANSITIONS["NOT_INSTALLED"]
    assert "VERIFYING" in _ALLOWED_TRANSITIONS["DOMAIN_PENDING"]
    assert "READY" in _ALLOWED_TRANSITIONS["VERIFYING"]
    # READY <-> DEGRADED.
    assert "DEGRADED" in _ALLOWED_TRANSITIONS["READY"]
    assert "READY" in _ALLOWED_TRANSITIONS["DEGRADED"]
    # Any -> DISABLED.
    for state in INSTALLATION_STATES:
        if state != "DISABLED":
            assert "DISABLED" in _ALLOWED_TRANSITIONS[state], (
                f"Expected {state} -> DISABLED to be allowed"
            )
    # DISABLED is terminal: no outgoing transitions.
    assert len(_ALLOWED_TRANSITIONS["DISABLED"]) == 0


def test_transition_state_rejects_illegal_transition(monkeypatch):
    """NOT_INSTALLED -> READY is illegal and raises ConnectorInstallationConflict."""
    from core import connector_installation as ci

    # Return a row with state NOT_INSTALLED.
    row = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "NOT_INSTALLED", None, None, None)
    lock_cur = _cur(row)
    conn = _conn_with(lock_cur)

    with pytest.raises(ci.ConnectorInstallationConflict, match="not allowed"):
        ci.transition_state(
            conn,
            environment="production",
            connector_name="test-connector",
            target_state="READY",
            responsible_actor=None,
            blocking_cause=None,
            last_verified_at=None,
            actor="platform-admin@test",
            idempotency_key="ik-test-1",
            host_context={},
            trace_id=None,
        )


def test_transition_state_rejects_disabled_as_source(monkeypatch):
    """DISABLED -> READY is illegal (terminal state)."""
    from core import connector_installation as ci

    row = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "DISABLED", None, None, None)
    lock_cur = _cur(row)
    conn = _conn_with(lock_cur)

    with pytest.raises(ci.ConnectorInstallationConflict, match="not allowed"):
        ci.transition_state(
            conn,
            environment="production",
            connector_name="test-connector",
            target_state="READY",
            responsible_actor=None,
            blocking_cause=None,
            last_verified_at=None,
            actor="platform-admin@test",
            idempotency_key="ik-test-2",
            host_context={},
            trace_id=None,
        )


def test_transition_state_legal_routes_through_execute_operation(monkeypatch):
    """VERIFYING -> READY is legal and routes through execute_operation."""
    from core import connector_installation as ci

    capture: dict = {}
    _stub_operation(monkeypatch, ci, capture=capture)

    row = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "VERIFYING", "platform_admin", None, None)
    lock_cur = _cur(row)
    update_cur = _cur(None)
    update_cur.rowcount = 1
    conn = _conn_with(lock_cur, update_cur)

    result = ci.transition_state(
        conn,
        environment="production",
        connector_name="test-connector",
        target_state="READY",
        responsible_actor="automated",
        blocking_cause=None,
        last_verified_at=None,
        actor="platform-admin@test",
        idempotency_key="ik-test-3",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "READY"
    assert result["safe_next_action"] == ci._SAFE_NEXT_ACTIONS["READY"]
    # execute_operation was called exactly once.
    assert len(capture.get("specs", [])) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.state.changed"


# ---------------------------------------------------------------------------
# (b) apply_installation idempotency + conflict 409.
# ---------------------------------------------------------------------------


def test_apply_installation_returns_existing_when_row_present(monkeypatch):
    """When a row exists, apply_installation returns the existing read-model."""
    from core import connector_installation as ci

    capture: dict = {}
    _stub_operation(monkeypatch, ci, capture=capture)
    existing_row = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "DOMAIN_PENDING", "platform_admin", None, None)
    conn = _conn_with(_cur(existing_row))

    result = ci.apply_installation(
        conn,
        environment="production",
        connector_name="test-connector",
        responsible_actor="platform_admin",
        blocking_cause=None,
        actor="platform-admin@test",
        idempotency_key="ik-idempotent-1",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "DOMAIN_PENDING"
    # review M2: existing-row path writes NO new operation (assert, don't just comment).
    assert capture.get("specs", []) == []


def test_apply_installation_inserts_new_row(monkeypatch):
    """When no row exists, apply_installation inserts via execute_operation."""
    from core import connector_installation as ci

    capture: dict = {}
    _stub_operation(monkeypatch, ci, capture=capture)

    no_row_cur = _cur(None)  # SELECT check returns None.
    insert_cur = _cur(None)
    conn = _conn_with(no_row_cur, insert_cur)

    result = ci.apply_installation(
        conn,
        environment="production",
        connector_name="new-connector",
        responsible_actor="platform_admin",
        blocking_cause=None,
        actor="platform-admin@test",
        idempotency_key="ik-new-1",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "DOMAIN_PENDING"
    # execute_operation was called once.
    assert len(capture.get("specs", [])) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.install.applied"
    assert spec.effective_org_id == "platform"
    # AC5: initial apply binds DOMAIN_PENDING (never READY); no random id hashed (H1).
    assert spec.request_payload["target_state"] == "DOMAIN_PENDING"
    assert "installation_id" not in spec.request_payload
    assert capture["changes"][0].result["state"] == "DOMAIN_PENDING"


def test_apply_installation_conflict_via_execute_operation(monkeypatch):
    """Conflicting Idempotency-Key propagates OperationIdempotencyConflict."""
    from core import connector_installation as ci
    from core.operations import OperationIdempotencyConflict

    no_row_cur = _cur(None)
    conn = _conn_with(no_row_cur)

    def raise_conflict(conn_, spec, *, mutation):
        raise OperationIdempotencyConflict("different request")

    monkeypatch.setattr(ci, "execute_operation", raise_conflict)

    with pytest.raises(OperationIdempotencyConflict):
        ci.apply_installation(
            conn,
            environment="production",
            connector_name="new-connector",
            responsible_actor=None,
            blocking_cause=None,
            actor="platform-admin@test",
            idempotency_key="ik-conflict-1",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (c) GET /installation: admin vs non-admin projection.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _client(monkeypatch):
    """Return a Starlette TestClient with the connector installation routes."""
    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_INSTALLATION_ROUTES)
    return TestClient(app, raise_server_exceptions=False)


def _patch_auth(monkeypatch, *, authorized: bool = True, identity: str = "test@example.com"):
    async def fake_auth(request):
        return authorized, identity

    monkeypatch.setattr(
        "core.connector_installation_api._check_auth", fake_auth
    )


def _patch_admin(monkeypatch, *, is_admin: bool):
    monkeypatch.setattr(
        "core.connector_installation_api._is_platform_admin",
        lambda identity: is_admin,
    )


def _patch_get_state(monkeypatch, state_dict):
    monkeypatch.setattr(
        "core.connector_installation_api.get_connection",
        None,  # not used when we patch get_installation_state directly
    )

    monkeypatch.setattr(
        "core.connector_installation.get_installation_state",
        lambda conn, *, environment, connector_name: state_dict,
    )
    # Also patch via the import path used inside the API handler.
    monkeypatch.setattr(
        "core.connector_installation_api._get_environment",
        lambda: "production",
    )


def test_get_installation_admin_sees_full_evidence(monkeypatch):
    """Platform-admin gets the full read-model including state, cause, actor."""
    import contextlib

    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_INSTALLATION_ROUTES)
    client = TestClient(app, raise_server_exceptions=False)

    _patch_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_admin(monkeypatch, is_admin=True)

    fake_model = {
        "state": "READY",
        "safe_next_action": "no action required",
        "responsible_actor": "automated",
        "blocking_cause": None,
        "last_verified_at": "2026-07-23T00:00:00+00:00",
    }

    import core.connector_installation as ci_mod
    import core.db as db_mod

    monkeypatch.setattr(ci_mod, "get_installation_state",
                        lambda conn, *, environment, connector_name: fake_model)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = client.get("/api/connectors/test-connector/installation")
    assert resp.status_code == 200
    body = resp.json()
    assert body["state"] == "READY"
    assert body["catalog_availability"] == "selectable"
    assert "blocking_cause" in body
    assert "last_verified_at" in body


def test_get_installation_non_admin_sees_restricted_projection(monkeypatch):
    """Non-admin sees catalog_availability + safe_next_action only."""
    import contextlib

    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_INSTALLATION_ROUTES)
    client = TestClient(app, raise_server_exceptions=False)

    _patch_auth(monkeypatch, authorized=True, identity="tenant@client.com")
    _patch_admin(monkeypatch, is_admin=False)

    fake_model = {
        "state": "DOMAIN_PENDING",
        "safe_next_action": "platform_admin: configure domain",
        "responsible_actor": "platform_admin",
        "blocking_cause": "route not configured",
        "last_verified_at": None,
    }

    import core.connector_installation as ci_mod
    import core.db as db_mod

    monkeypatch.setattr(ci_mod, "get_installation_state",
                        lambda conn, *, environment, connector_name: fake_model)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = client.get("/api/connectors/test-connector/installation")
    assert resp.status_code == 200
    body = resp.json()
    # Non-admin: no state, no blocking_cause, no responsible_actor disclosed.
    assert "state" not in body
    assert "blocking_cause" not in body
    assert "responsible_actor" not in body
    # But catalog_availability and safe_next_action are present.
    assert body["catalog_availability"] == "unavailable"
    assert "safe_next_action" in body


# ---------------------------------------------------------------------------
# (d) POST /installation gating: non-admin 404, missing key 422.
# ---------------------------------------------------------------------------


def test_post_installation_non_admin_gets_404(monkeypatch):
    import core.audit as audit_mod
    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_INSTALLATION_ROUTES)
    client = TestClient(app, raise_server_exceptions=False)

    _patch_auth(monkeypatch, authorized=True, identity="tenant@client.com")
    _patch_admin(monkeypatch, is_admin=False)

    # Patch write_audit_row to avoid real DB calls.
    audit_calls: list = []
    monkeypatch.setattr(audit_mod, "write_audit_row", lambda **kw: audit_calls.append(kw))

    resp = client.post(
        "/api/connectors/test-connector/installation",
        headers={"Idempotency-Key": "ik-test"},
        json={},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["code"] == "not_found"


def test_post_installation_missing_idempotency_key_gets_422(monkeypatch):
    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_INSTALLATION_ROUTES)
    client = TestClient(app, raise_server_exceptions=False)

    _patch_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_admin(monkeypatch, is_admin=True)

    resp = client.post(
        "/api/connectors/test-connector/installation",
        # No Idempotency-Key header.
        json={},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["code"] == "missing_header"


def test_post_installation_conflict_returns_409(monkeypatch):
    """Conflicting Idempotency-Key returns 409."""
    import contextlib

    from core.connector_installation_api import CONNECTOR_INSTALLATION_ROUTES
    from core.operations import OperationIdempotencyConflict
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_INSTALLATION_ROUTES)
    client = TestClient(app, raise_server_exceptions=False)

    _patch_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_admin(monkeypatch, is_admin=True)

    import core.connector_installation as ci_mod
    import core.db as db_mod

    def raise_conflict(conn, *, environment, connector_name, **kw):
        raise OperationIdempotencyConflict("different request")

    monkeypatch.setattr(ci_mod, "apply_installation", raise_conflict)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = client.post(
        "/api/connectors/test-connector/installation",
        headers={"Idempotency-Key": "ik-conflict"},
        json={},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["code"] == "conflict"


# ---------------------------------------------------------------------------
# (e) Catalog availability gate (AC3).
# ---------------------------------------------------------------------------


def test_get_catalog_availability_ready_is_selectable(monkeypatch):
    import core.connector_installation as ci_mod
    from core.connector_installation_api import get_catalog_availability

    ready_model = {
        "state": "READY",
        "safe_next_action": "no action required",
        "responsible_actor": "automated",
        "blocking_cause": None,
        "last_verified_at": None,
    }
    monkeypatch.setattr(ci_mod, "get_installation_state", lambda conn, **kw: ready_model)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    result = get_catalog_availability(MagicMock(), connector_name="test-connector")
    assert result["catalog_availability"] == "selectable"
    assert result["catalog_status"] == "ready"


def test_get_catalog_availability_not_ready_is_unavailable(monkeypatch):
    import core.connector_installation as ci_mod
    from core.connector_installation_api import get_catalog_availability

    for state in ("NOT_INSTALLED", "DOMAIN_PENDING", "VERIFYING", "DEGRADED", "DISABLED"):
        model = {
            "state": state,
            "safe_next_action": "",
            "responsible_actor": None,
            "blocking_cause": None,
            "last_verified_at": None,
        }
        monkeypatch.setattr(ci_mod, "get_installation_state", lambda conn, _m=model, **kw: _m)
        monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

        result = get_catalog_availability(MagicMock(), connector_name="test-connector")
        assert result["catalog_availability"] == "unavailable", (
            f"Expected unavailable for state {state}"
        )


def test_get_catalog_availability_missing_row_is_unavailable(monkeypatch):
    import core.connector_installation as ci_mod
    from core.connector_installation_api import get_catalog_availability

    monkeypatch.setattr(ci_mod, "get_installation_state", lambda conn, **kw: None)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    result = get_catalog_availability(MagicMock(), connector_name="never-installed")
    assert result["catalog_availability"] == "unavailable"


# ---------------------------------------------------------------------------
# (f) refuse_activation_unless_ready (AC4).
# ---------------------------------------------------------------------------


def test_refuse_activation_raises_when_not_ready(monkeypatch):
    import core.connector_installation as ci_mod
    from core.connector_installation_api import (
        ConnectorNotReady,
        refuse_activation_unless_ready,
    )

    for state in ("NOT_INSTALLED", "DOMAIN_PENDING", "VERIFYING", "DEGRADED", "DISABLED"):
        model = {
            "state": state,
            "safe_next_action": "",
            "responsible_actor": None,
            "blocking_cause": None,
            "last_verified_at": None,
        }
        monkeypatch.setattr(ci_mod, "get_installation_state", lambda conn, _m=model, **kw: _m)
        monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

        with pytest.raises(ConnectorNotReady):
            refuse_activation_unless_ready(MagicMock(), connector_name="test-connector")


def test_refuse_activation_does_not_raise_when_ready(monkeypatch):
    import core.connector_installation as ci_mod
    from core.connector_installation_api import refuse_activation_unless_ready

    ready_model = {
        "state": "READY",
        "safe_next_action": "no action required",
        "responsible_actor": "automated",
        "blocking_cause": None,
        "last_verified_at": None,
    }
    monkeypatch.setattr(ci_mod, "get_installation_state", lambda conn, **kw: ready_model)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    # Should not raise.
    refuse_activation_unless_ready(MagicMock(), connector_name="test-connector")


# ---------------------------------------------------------------------------
# (g) Secret-free read-model (AC2, AC6).
# ---------------------------------------------------------------------------


def test_safe_read_model_contains_no_secret_keys():
    """The read-model projection must not contain secret-like field names."""
    from core.connector_installation import _safe_read_model
    from core.operations import _is_secret_key

    model = _safe_read_model(
        state="READY",
        responsible_actor="automated",
        blocking_cause=None,
        last_verified_at=None,
    )
    for key in model:
        assert not _is_secret_key(key), (
            f"Read-model contains secret-like key: {key!r}"
        )


def test_safe_read_model_shape():
    """Read-model has exactly the expected fields."""
    from core.connector_installation import _safe_read_model

    model = _safe_read_model(
        state="DEGRADED",
        responsible_actor="platform_admin",
        blocking_cause="route misconfigured",
        last_verified_at=None,
    )
    assert set(model.keys()) == {
        "state",
        "safe_next_action",
        "responsible_actor",
        "blocking_cause",
        "last_verified_at",
    }
    assert model["state"] == "DEGRADED"
    assert model["blocking_cause"] == "route misconfigured"


# ---------------------------------------------------------------------------
# (h) Audit / outbox written through execute_operation (AC1).
# ---------------------------------------------------------------------------


def test_transition_state_audit_recorded_via_execute_operation(monkeypatch):
    """transition_state routes through execute_operation; spec has correct command_type."""
    from core import connector_installation as ci

    capture: dict = {}
    _stub_operation(monkeypatch, ci, capture=capture)

    row = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "READY", "automated", None, None)
    lock_cur = _cur(row)
    update_cur = _cur(None)
    update_cur.rowcount = 1
    conn = _conn_with(lock_cur, update_cur)

    ci.transition_state(
        conn,
        environment="production",
        connector_name="test-connector",
        target_state="DEGRADED",
        responsible_actor="platform_admin",
        blocking_cause="upstream error",
        last_verified_at=None,
        actor="platform-admin@test",
        idempotency_key="ik-audit-test",
        host_context={},
        trace_id=None,
    )

    assert len(capture["specs"]) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.state.changed"
    # outbox_payload must not contain secrets.
    from core.operations import _is_secret_key
    payload = capture["changes"][0].outbox_payload
    for key in payload:
        assert not _is_secret_key(key), f"Outbox payload contains secret key: {key!r}"


# ---------------------------------------------------------------------------
# (i) MCP get_connector_installation_status == REST evidence (AC2).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_mcp_registry(monkeypatch):
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


class _Recorder:
    def __init__(self):
        self.handlers: dict = {}
        self.tool = MagicMock()

    def capture(self, handler):
        self.handlers[handler.__name__] = handler
        return handler


def _register_operations_tools():
    from core import mcp_profiles, operations_mcp

    recorder = _Recorder()
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


def test_mcp_tool_registered_under_operations_profile():
    from core import mcp_profiles

    _register_operations_tools()

    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert "get_connector_installation_status" in decls
    d = decls["get_connector_installation_status"]
    assert d.profile == "operations"
    assert d.effect == "read"
    assert d.confirmation_mode == "none"


def test_mcp_tool_returns_same_model_as_rest(monkeypatch):
    """MCP tool returns the same safe read-model fields as the REST GET endpoint."""
    import contextlib

    handlers = _register_operations_tools()
    handler = handlers.get("get_connector_installation_status")
    assert handler is not None, "get_connector_installation_status not registered"

    import core.connector_installation as ci_mod
    import core.db as db_mod

    ready_model = {
        "state": "READY",
        "safe_next_action": "no action required",
        "responsible_actor": "automated",
        "blocking_cause": None,
        "last_verified_at": None,
    }

    monkeypatch.setattr(ci_mod, "get_installation_state",
                        lambda conn, *, environment, connector_name: ready_model)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    result = handler("test-connector")
    # _result() returns a ToolResult with structured_content.
    envelope = result.structured_content
    data = envelope["data"]
    assert data["state"] == "READY"
    assert data["catalog_availability"] == "selectable"
    assert "safe_next_action" in data
    assert "blocking_cause" in data
    assert "last_verified_at" in data
    # Secret-free: no secret-like keys in data.
    from core.operations import _is_secret_key
    for key in data:
        assert not _is_secret_key(key), f"MCP data contains secret key: {key!r}"


def test_mcp_tool_not_installed_returns_unavailable(monkeypatch):
    """MCP tool returns unavailable + NOT_INSTALLED when no row exists."""
    import contextlib

    handlers = _register_operations_tools()
    handler = handlers.get("get_connector_installation_status")

    import core.connector_installation as ci_mod
    import core.db as db_mod

    monkeypatch.setattr(ci_mod, "get_installation_state",
                        lambda conn, *, environment, connector_name: None)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    result = handler("never-installed-connector")
    envelope = result.structured_content
    data = envelope["data"]
    assert data["state"] == "NOT_INSTALLED"
    assert data["catalog_availability"] == "unavailable"


# ---------------------------------------------------------------------------
# Live-PG-gated: UNIQUE constraint + immutability trigger (Task 6).
# ---------------------------------------------------------------------------

import os as _os  # noqa: E402

_DSN = _os.environ.get("TEST_POSTGRES_DSN") or _os.environ.get("PLATFORM_DB_URL")

requires_postgres = pytest.mark.skipif(
    not _DSN,
    reason="TEST_POSTGRES_DSN/PLATFORM_DB_URL not set -- live Postgres constraint test skipped",
)

_MIGRATION = (
    __import__("pathlib").Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "082_connector_installation_state.sql"
)


def _apply_migration(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def _insert_installation(conn, *, env: str, name: str, inst_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.connector_installations "
            "(id, environment, connector_name, state) "
            "VALUES (%s, %s, %s, 'NOT_INSTALLED')",
            (inst_id, env, name),
        )
    conn.commit()


@requires_postgres
def test_live_unique_constraint_rejects_duplicate():
    """UNIQUE (environment, connector_name) is enforced by the DB."""
    import psycopg

    with psycopg.connect(_DSN) as conn:
        _apply_migration(conn)
        from ulid import ULID
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        id1 = f"cin_{ULID()}"
        id2 = f"cin_{ULID()}"

        # First insert succeeds.
        _insert_installation(conn, env=env, name=name, inst_id=id1)

        # Second insert on the same (env, name) must raise unique violation.
        with pytest.raises(Exception) as exc_info:
            _insert_installation(conn, env=env, name=name, inst_id=id2)
        assert "uq_connector_installations_env_name" in str(exc_info.value).lower() or (
            "unique" in str(exc_info.value).lower()
        )


@requires_postgres
def test_live_immutability_trigger_blocks_identity_mutation():
    """protect_connector_installation freezes id/environment/connector_name."""
    import psycopg

    with psycopg.connect(_DSN) as conn:
        _apply_migration(conn)
        from ulid import ULID
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"

        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        # Attempt to mutate `connector_name` (frozen identity column) -> trigger fires.
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="immutable"):
                cur.execute(
                    "UPDATE app.connector_installations "
                    "SET connector_name = %s WHERE id = %s",
                    ("mutated-name", inst_id),
                )
        conn.rollback()


@requires_postgres
def test_live_immutability_trigger_allows_state_update():
    """Mutable columns (state, blocking_cause, etc.) may be updated."""
    import psycopg

    with psycopg.connect(_DSN) as conn:
        _apply_migration(conn)
        from ulid import ULID
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"

        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        # Updating `state` (mutable) must succeed.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_installations "
                "SET state = 'DOMAIN_PENDING', updated_at = NOW() "
                "WHERE id = %s",
                (inst_id,),
            )
            assert cur.rowcount == 1
        conn.commit()


@requires_postgres
def test_live_delete_rejected_by_trigger():
    """DELETE on connector_installations is rejected (append-only)."""
    import psycopg

    with psycopg.connect(_DSN) as conn:
        _apply_migration(conn)
        from ulid import ULID
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"

        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="append-only"):
                cur.execute(
                    "DELETE FROM app.connector_installations WHERE id = %s",
                    (inst_id,),
                )
        conn.rollback()
