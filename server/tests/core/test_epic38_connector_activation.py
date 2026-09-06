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

from tests.conftest import REPO_ROOT

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
    select_cur = _cur(("cac_EXISTING01ABCDEFGHIJKLMNO", "ACTIVE", ts, None))
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
        select_cur = _cur(("cac_EXISTING01ABCDEFGHIJKLMNO", "ACTIVE", ts, None))
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


def test_activate_reactivates_deactivated_row(monkeypatch):
    """A DEACTIVATED row is updated back to ACTIVE without a duplicate row."""

    import datetime

    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )
    ts = datetime.datetime(2026, 7, 23, 10, 0, 0)
    write_cur = _cur(None, rowcount=1)
    read_cur = _cur(("cac_EXISTING01ABCDEFGHIJKLMNO", "ACTIVE", ts, None))
    _stub_operation(monkeypatch, ca)

    result = ca.activate(
        _conn_with(write_cur, read_cur),
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        activated_by="forged@example.com",
        actor="owner@example.com",
        idempotency_key="ik-reactivate",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "ACTIVE"
    sql = write_cur.execute.call_args.args[0]
    assert "DO UPDATE" in sql
    assert "deactivated_at = NULL" in sql


# ---------------------------------------------------------------------------
# (c) Deactivation preserves the row (UPDATE not DELETE).
# ---------------------------------------------------------------------------


def test_deactivate_issues_update_not_delete(monkeypatch):
    """Deactivation locks then updates the row and preserves its first timestamp."""

    import datetime

    from core import connector_activation as ca

    ts_activated = datetime.datetime(2026, 7, 23, 9, 0, 0)
    ts_deactivated = datetime.datetime(2026, 7, 23, 11, 0, 0)
    locked_cur = _cur(
        ("cac_DEACT01ABCDEFGHIJKLMNOPQR", "ACTIVE", ts_activated, None)
    )
    update_cur = _cur(None, rowcount=1)
    final_cur = _cur(("DEACTIVATED", ts_activated, ts_deactivated))
    capture = {}
    _stub_operation(monkeypatch, ca, capture=capture)

    result = ca.deactivate(
        _conn_with(locked_cur, update_cur, final_cur),
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        actor="user@example.com",
        idempotency_key="ik-deactivate-1",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "DEACTIVATED"
    assert result["deactivated_at"] == ts_deactivated.isoformat()
    assert "FOR UPDATE" in locked_cur.execute.call_args.args[0]
    for cursor in (locked_cur, update_cur, final_cur):
        for call in cursor.execute.call_args_list:
            assert "DELETE" not in call.args[0].upper()
    spec = capture["specs"][0]
    assert spec.request_payload == {
        "org_id": "org-alpha",
        "connector_name": "test-connector",
        "environment": "production",
        "target_state": "DEACTIVATED",
    }


def test_repeated_deactivation_preserves_original_timestamp(monkeypatch):
    import datetime

    from core import connector_activation as ca

    activated_at = datetime.datetime(2026, 7, 23, 9, 0, 0)
    first_deactivated_at = datetime.datetime(2026, 7, 23, 11, 0, 0)
    locked_cur = _cur(
        (
            "cac_DEACT01ABCDEFGHIJKLMNOPQR",
            "DEACTIVATED",
            activated_at,
            first_deactivated_at,
        )
    )
    final_cur = _cur(("DEACTIVATED", activated_at, first_deactivated_at))
    _stub_operation(monkeypatch, ca)
    result = ca.deactivate(
        _conn_with(locked_cur, final_cur),
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        actor="user@example.com",
        idempotency_key="ik-deactivate-again",
        host_context={},
        trace_id=None,
    )
    assert result["deactivated_at"] == first_deactivated_at.isoformat()
    all_sql = [
        call.args[0].upper()
        for cursor in (locked_cur, final_cur)
        for call in cursor.execute.call_args_list
    ]
    assert not any(sql.startswith("UPDATE") for sql in all_sql)


def test_activation_rejects_impossible_rowcount(monkeypatch):
    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )
    _stub_operation(monkeypatch, ca)
    with pytest.raises(ca.ConnectorActivationConflict, match="exactly one row"):
        ca.activate(
            _conn_with(_cur(None, rowcount=0)),
            org_id="org-alpha",
            connector_name="test-connector",
            environment="production",
            activated_by="owner@example.com",
            actor="owner@example.com",
            idempotency_key="ik-zero-row",
            host_context={},
            trace_id=None,
        )


def test_deactivate_not_found_raises(monkeypatch):
    """deactivate raises ConnectorActivationUnavailable when no row exists."""
    from core import connector_activation as ca

    _stub_operation(monkeypatch, ca)
    conn = _conn_with(_cur(None))

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


def test_activation_replay_skips_later_ready_gate(monkeypatch):
    from core import connector_activation as ca
    from core import operations

    ready_calls = []
    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: ready_calls.append(True),
    )
    monkeypatch.setattr(
        ca,
        "execute_operation",
        lambda conn, spec, *, mutation: operations.OperationResult(
            "op-replay",
            "succeeded",
            {
                "state": "ACTIVE",
                "activated_at": "2026-07-23T12:00:00",
                "deactivated_at": None,
            },
            "audit",
            "outbox",
            True,
        ),
    )
    result = ca.activate(
        MagicMock(),
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        activated_by="forged@example.com",
        actor="owner@example.com",
        idempotency_key="ik-replay",
        host_context={},
        trace_id=None,
    )
    assert result["state"] == "ACTIVE"
    assert ready_calls == []


def test_activation_provenance_uses_actor_and_keys_are_bounded(monkeypatch):
    import datetime

    from core import connector_activation as ca

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda *a, **kw: None,
    )
    ts = datetime.datetime(2026, 7, 23, 12, 0, 0)
    write_cur = _cur(None, rowcount=1)
    read_cur = _cur(("cac_EXISTING01ABCDEFGHIJKLMNO", "ACTIVE", ts, None))
    capture = {}
    _stub_operation(monkeypatch, ca, capture=capture)
    ca.activate(
        _conn_with(write_cur, read_cur),
        org_id="org-alpha",
        connector_name="test-connector",
        environment="production",
        activated_by="forged@example.com",
        actor="owner@example.com",
        idempotency_key="  ik-trimmed  ",
        host_context={},
        trace_id=None,
    )
    assert write_cur.execute.call_args.args[1][4] == "owner@example.com"
    assert capture["specs"][0].idempotency_key == "ik-trimmed"
    assert "activated_by" not in capture["specs"][0].request_payload

    with pytest.raises(ca.ConnectorActivationValidationError, match="too long"):
        ca.deactivate(
            MagicMock(),
            org_id="org-alpha",
            connector_name="test-connector",
            environment="production",
            actor="owner@example.com",
            idempotency_key="x" * 256,
            host_context={},
            trace_id=None,
        )


def test_corrective_activation_migration_and_health_layers_are_explicit():
    from pathlib import Path

    migration = Path(
        REPO_ROOT / "infra/nango/migrations/181_connector_activation_guard.sql"
    ).read_text(encoding="utf-8")
    for fragment in (
        "fk_connector_activations_org",
        "connector.activation.activated",
        "connector.activation.deactivated",
        "operation_org IS DISTINCT FROM NEW.org_id",
        "NEW.activated_by IS DISTINCT FROM operation_actor",
        "NEW.deactivated_at IS NULL",
        "BEFORE INSERT OR UPDATE OR DELETE",
    ):
        assert fragment in migration

    surface = Path(REPO_ROOT / "server/core/data_surface.py").read_text(encoding="utf-8")
    assert "i.state AS installation_state" in surface
    assert "a.state AS activation_state" in surface
    assert '"installation": row.get("installation_state")' in surface
    assert '"activation": row.get("activation_state")' in surface


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
    select_cur = _cur(("cac_EXISTING01ABCDEFGHIJKLMNO", "ACTIVE", ts, None))
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
    monkeypatch.setattr("core.audit.write_audit_row", lambda **kw: None)

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
    """The authenticated owner, not body input, owns activation provenance."""

    _patch_check_auth(monkeypatch, identity="owner@example.com")
    import core.db as db_mod
    import core.project_access as pa_mod

    monkeypatch.setattr(pa_mod, "identity_can_manage_org", lambda oid, identity, conn: True)
    monkeypatch.setattr(db_mod, "get_connection", _fake_conn_ctx(MagicMock()))
    captured = {}

    def fake_activate(*args, **kwargs):
        captured.update(kwargs)
        return {
            "org_id": "org-alpha",
            "connector_name": "test-connector",
            "environment": "production",
            "state": "ACTIVE",
            "activated_at": "2026-07-23T12:00:00",
            "deactivated_at": None,
        }

    monkeypatch.setattr("core.connector_activation.activate", fake_activate)
    client = _build_client()
    resp = client.post(
        "/api/connectors/test-connector/activation",
        json={
            "org_id": "org-alpha",
            "activated_by": "attacker-controlled@example.com",
        },
        headers={"Idempotency-Key": "  ik-success  "},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "ACTIVE"
    assert captured["actor"] == "owner@example.com"
    assert captured["activated_by"] == "owner@example.com"
    assert captured["idempotency_key"] == "ik-success"


def test_activation_rejects_non_object_json(monkeypatch):
    _patch_check_auth(monkeypatch)
    client = _build_client()
    response = client.post(
        "/api/connectors/test-connector/activation",
        content='["org-alpha"]',
        headers={
            "Content-Type": "application/json",
            "Idempotency-Key": "ik-list-body",
        },
    )
    assert response.status_code == 400
    assert response.json() == {
        "code": "invalid_body",
        "message": "Request body must be a JSON object",
    }


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
# Live-PG-gated corrective guard probe. Never uses a deployment DSN and the
# deliberate final exception rolls every fixture back.
# ---------------------------------------------------------------------------

_LIVE_MARK = pytest.mark.skipif(
    not __import__("os").environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live-PG probe skipped",
)


@_LIVE_MARK
def test_live_activation_constraints_and_lifecycle_guard():
    import os

    import psycopg

    probe = """
    DO $$
    DECLARE
        org_id TEXT := 'org_live_activation_guard';
        row_id TEXT := 'cac_01JZAAABBBCCCDDDEEEFFF0010';
        duplicate_id TEXT := 'cac_01JZAAABBBCCCDDDEEEFFF0011';
        op_activate TEXT := 'op_01JZAAABBBCCCDDDEEEFFF0012';
        op_reactivate TEXT := 'op_01JZAAABBBCCCDDDEEEFFF0013';
        op_deactivate TEXT := 'op_01JZAAABBBCCCDDDEEEFFF0014';
    BEGIN
        INSERT INTO app.organizations (id, name, slug, status, created_by)
        VALUES (
            org_id, 'Activation guard probe', 'activation-guard-probe',
            'active', 'test'
        );

        INSERT INTO app.operations
            (id, effective_org_id, command_type, actor, resource_path,
             host_context, versions, request_hash, provider_references,
             confirmation_mode, idempotency_key_hash, state)
        VALUES
            (op_activate, org_id, 'connector.activation.activated', 'owner@test',
             '["activation"]'::jsonb, '{}'::jsonb, '{}'::jsonb,
             repeat('a', 64), '{}'::jsonb, 'server', repeat('1', 64), 'pending'),
            (op_reactivate, org_id, 'connector.activation.activated', 'owner@test',
             '["reactivation"]'::jsonb, '{}'::jsonb, '{}'::jsonb,
             repeat('b', 64), '{}'::jsonb, 'server', repeat('2', 64), 'pending'),
            (op_deactivate, org_id, 'connector.activation.deactivated', 'owner@test',
             '["deactivation"]'::jsonb, '{}'::jsonb, '{}'::jsonb,
             repeat('c', 64), '{}'::jsonb, 'server', repeat('3', 64), 'pending');

        INSERT INTO app.connector_activations
            (id, org_id, connector_name, environment, state, activated_by,
             operation_id)
        VALUES
            (row_id, org_id, 'test-connector', 'test', 'ACTIVE', 'owner@test',
             op_activate);

        BEGIN
            INSERT INTO app.connector_activations
                (id, org_id, connector_name, environment, state, activated_by,
                 operation_id)
            VALUES
                (duplicate_id, org_id, 'test-connector', 'test', 'ACTIVE',
                 'owner@test', op_reactivate);
            RAISE EXCEPTION 'unique guard did not fire';
        EXCEPTION
            WHEN unique_violation THEN NULL;
        END;

        BEGIN
            UPDATE app.connector_activations
               SET org_id = 'org_other'
             WHERE id = row_id;
            RAISE EXCEPTION 'identity guard did not fire';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLERRM = 'identity guard did not fire' THEN
                    RAISE;
                END IF;
        END;

        UPDATE app.connector_activations
           SET state = 'DEACTIVATED', deactivated_at = NOW(),
               operation_id = op_deactivate, updated_at = NOW()
         WHERE id = row_id;

        IF NOT EXISTS (
            SELECT 1 FROM app.connector_activations
             WHERE id = row_id AND state = 'DEACTIVATED'
               AND deactivated_at IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'valid deactivation was not preserved';
        END IF;

        UPDATE app.connector_activations
           SET state = 'ACTIVE', deactivated_at = NULL,
               operation_id = op_reactivate, updated_at = NOW()
         WHERE id = row_id;

        BEGIN
            DELETE FROM app.connector_activations WHERE id = row_id;
            RAISE EXCEPTION 'delete guard did not fire';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLERRM = 'delete guard did not fire' THEN
                    RAISE;
                END IF;
        END;

        BEGIN
            INSERT INTO app.connector_activations
                (id, org_id, connector_name, environment, state, activated_by)
            VALUES
                ('cac_01JZAAABBBCCCDDDEEEFFF0015', org_id, 'other-connector',
                 'test', 'ACTIVE', 'owner@test');
            RAISE EXCEPTION 'operation provenance guard did not fire';
        EXCEPTION
            WHEN OTHERS THEN
                IF SQLERRM = 'operation provenance guard did not fire' THEN
                    RAISE;
                END IF;
        END;

        RAISE EXCEPTION 'rollback_test_data';
    END;
    $$ LANGUAGE plpgsql;
    """
    with psycopg.connect(os.environ["TEST_POSTGRES_DSN"]) as conn:
        with conn.cursor() as cur:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match="rollback_test_data",
            ):
                cur.execute(probe)
        conn.rollback()
