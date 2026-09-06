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

import datetime as dt
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
    conn = _conn_with(lock_cur, _cur(None))

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
    conn = _conn_with(lock_cur, _cur(None))

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
        last_verified_at=dt.datetime(2026, 8, 1, tzinfo=dt.UTC),
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
    assert spec.request_payload["last_verified_at"] == "2026-08-01T00:00:00+00:00"



def test_transition_retry_replays_after_state_already_reached(monkeypatch):
    from types import SimpleNamespace

    from core import connector_installation as ci

    timestamp = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    replayed = {
        "state": "READY",
        "responsible_actor": "automated",
        "blocking_cause": None,
        "last_verified_at": timestamp.isoformat(),
    }
    conn = _conn_with(
        _cur(("cin_TESTID01ABCDEFGHIJKLMNOPQ", "READY", "automated", None, timestamp)),
        _cur((replayed, "request-hash")),
    )
    monkeypatch.setattr(
        ci,
        "prepare_operation",
        lambda _spec: SimpleNamespace(
            idempotency_key_hash="key-hash",
            request_hash="request-hash",
        ),
    )
    monkeypatch.setattr(
        ci,
        "execute_operation",
        lambda *_args, **_kwargs: pytest.fail("replay must not insert an operation"),
    )

    result = ci.transition_state(
        conn,
        environment="production",
        connector_name="test-connector",
        target_state="READY",
        responsible_actor="automated",
        blocking_cause=None,
        last_verified_at=timestamp,
        actor="platform-admin@test",
        idempotency_key="ik-retry",
        host_context={},
        trace_id=None,
    )

    assert result == {
        "state": "READY",
        "safe_next_action": ci._SAFE_NEXT_ACTIONS["READY"],
        "responsible_actor": "automated",
        "blocking_cause": None,
        "last_verified_at": timestamp.isoformat(),
    }


def test_transition_retry_rejects_conflicting_payload(monkeypatch):
    from types import SimpleNamespace

    from core import connector_installation as ci
    from core.operations import OperationIdempotencyConflict

    timestamp = dt.datetime(2026, 8, 1, tzinfo=dt.UTC)
    conn = _conn_with(
        _cur(("cin_TESTID01ABCDEFGHIJKLMNOPQ", "READY", "automated", None, timestamp)),
        _cur(({"state": "READY"}, "original-request-hash")),
    )
    monkeypatch.setattr(
        ci,
        "prepare_operation",
        lambda _spec: SimpleNamespace(
            idempotency_key_hash="key-hash",
            request_hash="changed-request-hash",
        ),
    )

    with pytest.raises(OperationIdempotencyConflict):
        ci.transition_state(
            conn,
            environment="production",
            connector_name="test-connector",
            target_state="READY",
            responsible_actor="platform_support",
            blocking_cause=None,
            last_verified_at=timestamp,
            actor="platform-admin@test",
            idempotency_key="ik-retry",
            host_context={},
            trace_id=None,
        )

# ---------------------------------------------------------------------------
# (b) apply_installation idempotency + conflict 409.
# ---------------------------------------------------------------------------


def test_apply_installation_returns_existing_when_row_present(monkeypatch):
    """When a row exists, apply_installation returns the existing read-model."""
    from core import connector_installation as ci

    capture: dict = {}
    _stub_operation(monkeypatch, ci, capture=capture)
    existing_row = (
        "cin_TESTID01ABCDEFGHIJKLMNOPQ",
        "DOMAIN_PENDING",
        "platform_admin",
        "domain_configuration_pending",
        None,
    )
    existing_cur = _cur(existing_row)
    existing_cur.rowcount = 0
    conn = _conn_with(existing_cur)

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
    # The operation seam sees the key before row-level reconciliation.
    assert len(capture.get("specs", [])) == 1


def test_apply_installation_inserts_new_row(monkeypatch):
    """When no row exists, apply_installation inserts via execute_operation."""
    from core import connector_installation as ci

    capture: dict = {}
    _stub_operation(monkeypatch, ci, capture=capture)

    insert_cur = _cur(None)
    conn = _conn_with(insert_cur)

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
    assert spec.effective_org_id is None
    # AC5: initial apply binds DOMAIN_PENDING (never READY); no random id hashed (H1).
    assert spec.request_payload["target_state"] == "DOMAIN_PENDING"
    assert "installation_id" not in spec.request_payload
    assert spec.request_payload["blocking_cause"] == "domain_configuration_pending"
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


@pytest.mark.parametrize(
    ("payload", "status"),
    [
        ([], 400),
        ("not-an-object", 400),
        ({"responsible_actor": []}, 422),
        ({"blocking_cause": {}}, 422),
    ],
)
def test_post_installation_rejects_invalid_json_shapes(
    monkeypatch, _client, payload, status
):
    _patch_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_admin(monkeypatch, is_admin=True)

    response = _client.post(
        "/api/connectors/test-connector/installation",
        headers={"Idempotency-Key": "ik-valid"},
        json=payload,
    )
    assert response.status_code == status
    assert response.json()["code"] == "invalid_body"


@pytest.mark.parametrize("key", ["   ", "x" * 256])
def test_post_installation_rejects_invalid_idempotency_key(
    monkeypatch, _client, key
):
    _patch_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_admin(monkeypatch, is_admin=True)

    response = _client.post(
        "/api/connectors/test-connector/installation",
        headers={"Idempotency-Key": key},
        json={},
    )
    assert response.status_code == 422

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


def test_refuse_activation_raises_when_not_ready():
    from core.connector_installation_api import (
        ConnectorNotReady,
        refuse_activation_unless_ready,
    )

    for state in ("NOT_INSTALLED", "DOMAIN_PENDING", "VERIFYING", "DEGRADED", "DISABLED"):
        with pytest.raises(ConnectorNotReady):
            refuse_activation_unless_ready(
                _conn_with(_cur((state,))),
                connector_name="test-connector",
                environment="production",
            )


def test_refuse_activation_holds_share_lock_when_ready():
    from core.connector_installation_api import refuse_activation_unless_ready

    ready_cur = _cur(("READY",))
    refuse_activation_unless_ready(
        _conn_with(ready_cur),
        connector_name="test-connector",
        environment="production",
    )

    sql = " ".join(str(ready_cur.execute.call_args.args[0]).split())
    assert sql.endswith("FOR SHARE")


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
        blocking_cause="domain_route_misconfigured",
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
    assert model["blocking_cause"] == "domain_route_misconfigured"



def test_safe_read_model_redacts_legacy_free_text():
    from core.connector_installation import _safe_read_model

    model = _safe_read_model(
        state="DEGRADED",
        responsible_actor="person@example.com",
        blocking_cause="token=super-secret",
        last_verified_at=None,
    )

    assert model["responsible_actor"] == "platform_support"
    assert model["blocking_cause"] == "dependency_unavailable"
    assert "secret" not in repr(model)


def test_installation_metadata_accepts_only_closed_classifications():
    from core.connector_installation import (
        ConnectorInstallationValidationError,
        normalize_responsible_actor,
        validate_blocking_cause_code,
    )

    with pytest.raises(ConnectorInstallationValidationError):
        normalize_responsible_actor(
            "person@example.com", default="platform_support"
        )
    with pytest.raises(ConnectorInstallationValidationError):
        validate_blocking_cause_code("api_key=secret")

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
        blocking_cause="verification_failed",
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
    monkeypatch.setattr("core.operations_mcp._identity", lambda: "admin@toorow.io")
    monkeypatch.setattr("core.super_admin.is_super_admin", lambda _identity: True)

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
    monkeypatch.setattr("core.operations_mcp._identity", lambda: "admin@toorow.io")
    monkeypatch.setattr("core.super_admin.is_super_admin", lambda _identity: True)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    result = handler("never-installed-connector")
    envelope = result.structured_content
    data = envelope["data"]
    assert data["state"] == "NOT_INSTALLED"
    assert data["catalog_availability"] == "unavailable"


def test_mcp_tool_denies_operations_profile_without_platform_admin(monkeypatch):
    from fastmcp.exceptions import ToolError

    handler = _register_operations_tools()["get_connector_installation_status"]
    monkeypatch.setattr("core.operations_mcp._identity", lambda: "tenant@example.com")
    monkeypatch.setattr("core.super_admin.is_super_admin", lambda _identity: False)

    with pytest.raises(ToolError, match="not_found"):
        handler("test-connector")

# ---------------------------------------------------------------------------
# Live-PG-gated: UNIQUE constraint + immutability trigger (Task 6).
# ---------------------------------------------------------------------------

import os as _os  # noqa: E402

_DSN = _os.environ.get("TEST_POSTGRES_DSN")

requires_postgres = pytest.mark.skipif(
    not _DSN,
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

_MIGRATION_178 = (
    __import__("pathlib").Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "178_connector_installation_transition_guard.sql"
)


def test_corrective_migration_guards_operation_backed_transitions():
    sql = _MIGRATION_178.read_text(encoding="utf-8")
    assert "BEFORE INSERT OR UPDATE OR DELETE" in sql
    assert "fresh operation" in sql
    assert "illegal connector installation state transition" in sql
    assert "effective_org_id" in sql

    assert "NEW.responsible_actor IS NULL" in sql
    assert "NEW.last_verified_at IS NOT NULL" in sql

def _insert_operation(conn, *, command_type: str) -> str:
    import hashlib

    from ulid import ULID

    operation_id = f"op_{ULID()}"
    digest = hashlib.sha256(operation_id.encode()).hexdigest()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, "
            "host_context, versions, request_hash, provider_references, "
            "confirmation_mode, idempotency_key_hash, state) "
            "VALUES (%s, NULL, %s, 'test', '[\"platform:test\"]'::jsonb, "
            "'{}'::jsonb, '{}'::jsonb, %s, '{}'::jsonb, 'server', %s, 'pending')",
            (operation_id, command_type, digest, digest),
        )
    return operation_id


def _insert_installation(conn, *, env: str, name: str, inst_id: str) -> None:
    operation_id = _insert_operation(conn, command_type="connector.install.applied")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.connector_installations "
            "(id, environment, connector_name, state, blocking_cause, "
            "responsible_actor, operation_id) "
            "VALUES (%s, %s, %s, 'DOMAIN_PENDING', "
            "'domain_configuration_pending', 'platform_admin', %s)",
            (inst_id, env, name, operation_id),
        )


@requires_postgres
def test_live_unique_constraint_rejects_duplicate():
    import psycopg
    from ulid import ULID

    conn = psycopg.connect(_DSN)
    try:
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        _insert_installation(conn, env=env, name=name, inst_id=f"cin_{ULID()}")

        with pytest.raises(Exception) as exc_info:
            _insert_installation(conn, env=env, name=name, inst_id=f"cin_{ULID()}")
        assert "unique" in str(exc_info.value).lower()
    finally:
        conn.rollback()
        conn.close()


@requires_postgres
def test_live_immutability_trigger_blocks_identity_mutation():
    import psycopg
    from ulid import ULID

    conn = psycopg.connect(_DSN)
    try:
        inst_id = f"cin_{ULID()}"
        _insert_installation(
            conn,
            env=f"test-{ULID()}",
            name=f"connector-{ULID()}",
            inst_id=inst_id,
        )

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="immutable"):
                cur.execute(
                    "UPDATE app.connector_installations "
                    "SET connector_name = %s WHERE id = %s",
                    ("mutated-name", inst_id),
                )
    finally:
        conn.rollback()
        conn.close()


@requires_postgres
def test_live_state_update_requires_fresh_operation():
    import psycopg
    from ulid import ULID

    conn = psycopg.connect(_DSN)
    try:
        inst_id = f"cin_{ULID()}"
        _insert_installation(
            conn,
            env=f"test-{ULID()}",
            name=f"connector-{ULID()}",
            inst_id=inst_id,
        )

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="fresh operation"):
                cur.execute(
                    "UPDATE app.connector_installations "
                    "SET state = 'VERIFYING', updated_at = NOW() WHERE id = %s",
                    (inst_id,),
                )
    finally:
        conn.rollback()
        conn.close()


@requires_postgres
def test_live_legal_state_update_accepts_new_platform_operation():
    import psycopg
    from ulid import ULID

    conn = psycopg.connect(_DSN)
    try:
        inst_id = f"cin_{ULID()}"
        _insert_installation(
            conn,
            env=f"test-{ULID()}",
            name=f"connector-{ULID()}",
            inst_id=inst_id,
        )
        transition_op = _insert_operation(
            conn, command_type="connector.state.changed"
        )

        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_installations "
                "SET state = 'VERIFYING', blocking_cause = NULL, "
                "operation_id = %s, updated_at = NOW() WHERE id = %s",
                (transition_op, inst_id),
            )
            assert cur.rowcount == 1
    finally:
        conn.rollback()
        conn.close()


@requires_postgres
def test_live_illegal_state_jump_is_rejected():
    import psycopg
    from ulid import ULID

    conn = psycopg.connect(_DSN)
    try:
        inst_id = f"cin_{ULID()}"
        _insert_installation(
            conn,
            env=f"test-{ULID()}",
            name=f"connector-{ULID()}",
            inst_id=inst_id,
        )
        transition_op = _insert_operation(
            conn, command_type="connector.state.changed"
        )

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="illegal"):
                cur.execute(
                    "UPDATE app.connector_installations "
                    "SET state = 'READY', blocking_cause = NULL, "
                    "last_verified_at = NOW(), responsible_actor = 'automated', "
                    "operation_id = %s, updated_at = NOW() WHERE id = %s",
                    (transition_op, inst_id),
                )
    finally:
        conn.rollback()
        conn.close()


@requires_postgres
def test_live_delete_rejected_by_trigger():
    import psycopg
    from ulid import ULID

    conn = psycopg.connect(_DSN)
    try:
        inst_id = f"cin_{ULID()}"
        _insert_installation(
            conn,
            env=f"test-{ULID()}",
            name=f"connector-{ULID()}",
            inst_id=inst_id,
        )

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="append-only"):
                cur.execute(
                    "DELETE FROM app.connector_installations WHERE id = %s",
                    (inst_id,),
                )
    finally:
        conn.rollback()
        conn.close()
