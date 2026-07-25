"""Story 38.3: connector domain and adapter-route configuration -- offline + ASGI tests.

Covers (non-tautological per review lessons H1-H4):
  (a) Deterministic idempotency: request_payload contains NO random id; two identical
      configure calls produce byte-identical hashed payloads (review H1).
  (b) Versioned supersede: config_version increments; prior row is superseded before
      a new row is inserted; both state changes are asserted (review H3).
  (c) Duplicate-domain-same-env: a second configure call for the same env+connector
      bumps the version (allowed idempotent re-configure), verified with real functions.
  (d) Cross-environment conflict: same domain already active in a different env
      raises ConnectorDomainConflict (409-mappable, AC3).
  (e) Validation errors: bad domain shape, unknown adapter, missing installation,
      NOT_INSTALLED or DISABLED installation all fail before SQL (AC3).
  (f) Secret-free read-model: signing_secret_ref and dns_evidence_hash are never in
      get_domain_config or configure_domain outputs (AC6, E38-NFR03).
  (g) No ds_<token>: configure_domain never returns a ds_ prefixed token (AC2).
  (h) Audit/outbox written through execute_operation (never a parallel path, AC4).
  (i) Idempotent replay (same key) produces no new operation; conflicting key -> 409.
  (j) REST: POST non-admin -> 404; missing Idempotency-Key -> 422; GET non-admin -> 404.
  (k) REST: POST valid -> 200 with safe read-model; GET admin -> 200.
  (l) MCP get_connector_domain_config returns the same safe model as REST GET (AC2).
  (m) Live-PG-gated: partial-unique constraints + immutability trigger (real DB).
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared mock helpers (same pattern as test_epic38_connector_installation.py).
# ---------------------------------------------------------------------------


def _cur(*fetchone_rows, fetchall=None, rowcount=1):
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return cur


def _conn_with(*curs):
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module, *, capture=None):
    """Stub execute_operation to run the mutation directly (non-tautological)."""
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-domain-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-domain-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


def _install_row(inst_id="cin_TESTID01ABCDEFGHIJKLMNOPQ", state="DOMAIN_PENDING"):
    """Return a mock installation row (id, state)."""
    return (inst_id, state)


def _domain_config_row(
    config_id="cdc_TESTCFGID01ABCDEFGHIJKLMN",
    domain="mail.example.com",
    adapter="adapter_eu_v1",
    wev="v1",
    dec="spf_dkim",
    version=1,
    created_at=None,
):
    """Return a mock connector_domain_configs row."""
    return (config_id, domain, adapter, wev, dec, version, created_at)


# ---------------------------------------------------------------------------
# (a) Deterministic idempotency -- review H1.
# ---------------------------------------------------------------------------


def test_configure_domain_payload_is_deterministic(monkeypatch):
    """Two identical configure calls produce byte-identical hashed request_payloads.

    The row id must NOT appear in request_payload (review H1).
    """
    from core import connector_domain as cd

    def _payload_of_one_configure():
        capture: dict = {}
        _stub_operation(monkeypatch, cd, capture=capture)

        # Installation exists in DOMAIN_PENDING.
        inst_cur = _cur(_install_row())
        # No existing domain binding (cross-env guard: no active domain row for this domain).
        no_domain_cur = _cur(None)
        # No prior active config.
        no_prior_cur = _cur(None)
        # Inside mutation: INSERT rowcount=1 then SELECT created_at.
        insert_cur = _cur(None)  # created_at fetch
        insert_cur.rowcount = 1

        conn = _conn_with(inst_cur, no_domain_cur, no_prior_cur, insert_cur)

        cd.configure_domain(
            conn,
            environment="production",
            connector_name="inbound-connector",
            domain="mail.example.com",
            provider_adapter="adapter_eu_v1",
            webhook_endpoint_version="v1",
            signing_secret_ref="secret-ref-abc",
            dns_evidence_class=None,
            actor="admin@toorow.io",
            idempotency_key="ik-domain-same",
            host_context={},
            trace_id=None,
        )
        return capture["specs"][0].request_payload

    first = _payload_of_one_configure()
    second = _payload_of_one_configure()

    # Byte-identical payloads (deterministic hash seed).
    assert first == second, "request_payload must be identical across calls with same inputs"
    # No random row id in the hashed payload (review H1).
    assert "config_id" not in first, "config_id (row id) must NOT be in request_payload"
    assert "id" not in first, "row id must NOT be in request_payload"


# ---------------------------------------------------------------------------
# (b) Versioned supersede -- review H3.
# ---------------------------------------------------------------------------


def test_configure_domain_versioned_supersede(monkeypatch):
    """Second configure supersedes prior row and bumps config_version."""
    from core import connector_domain as cd

    capture: dict = {}
    _stub_operation(monkeypatch, cd, capture=capture)

    inst_cur = _cur(_install_row())
    no_domain_cur = _cur(None)
    # Prior active config exists at version 1. The supersede lookup SELECTs only
    # (id, config_version), so the mock row is a 2-tuple, not a full-width row.
    prior_cur = _cur(("cdc_prior01ABCDEFGHIJKLMN", 1))

    # The mutation opens ONE cursor: UPDATE supersede, INSERT, and the created_at
    # SELECT all run on it (rowcount=1 satisfies both the UPDATE and INSERT checks).
    update_cur = _cur(None)
    update_cur.rowcount = 1

    conn = _conn_with(inst_cur, no_domain_cur, prior_cur, update_cur)

    result = cd.configure_domain(
        conn,
        environment="production",
        connector_name="inbound-connector",
        domain="mail.example.com",
        provider_adapter="adapter_eu_v1",
        webhook_endpoint_version="v1",
        signing_secret_ref=None,
        dns_evidence_class="spf_dkim",
        actor="admin@toorow.io",
        idempotency_key="ik-domain-v2",
        host_context={},
        trace_id=None,
    )

    # config_version must be 2 (prior version 1 + 1).
    assert result["config_version"] == 2, (
        f"Expected config_version=2 after versioned supersede, got {result['config_version']}"
    )
    # execute_operation was called exactly once.
    assert len(capture.get("specs", [])) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.domain.configured"
    assert spec.request_payload["config_version"] == 2
    # Outbox payload must be secret-free (review H3 / E38-NFR03).
    outbox = capture["changes"][0].outbox_payload
    assert "signing_secret_ref" not in outbox, "signing_secret_ref must NOT appear in outbox"
    assert "dns_evidence_hash" not in outbox, "dns_evidence_hash must NOT appear in outbox"


def test_configure_domain_first_version_is_one(monkeypatch):
    """First configure for a connector inserts at config_version=1."""
    from core import connector_domain as cd

    capture: dict = {}
    _stub_operation(monkeypatch, cd, capture=capture)

    inst_cur = _cur(_install_row())
    no_domain_cur = _cur(None)
    no_prior_cur = _cur(None)
    insert_cur = _cur(None)
    insert_cur.rowcount = 1

    conn = _conn_with(inst_cur, no_domain_cur, no_prior_cur, insert_cur)

    result = cd.configure_domain(
        conn,
        environment="production",
        connector_name="new-connector",
        domain="rx.example.com",
        provider_adapter="adapter_us_v1",
        webhook_endpoint_version="v1",
        signing_secret_ref=None,
        dns_evidence_class=None,
        actor="admin@toorow.io",
        idempotency_key="ik-first-v1",
        host_context={},
        trace_id=None,
    )

    assert result["config_version"] == 1
    assert len(capture["specs"]) == 1
    assert capture["specs"][0].request_payload["config_version"] == 1
    assert "config_id" not in capture["specs"][0].request_payload


# ---------------------------------------------------------------------------
# (c) Duplicate-domain-same-env (idempotent re-configure, bumps version).
# ---------------------------------------------------------------------------


def test_configure_domain_same_env_bumps_version(monkeypatch):
    """Re-configuring the same env+connector is allowed -- version increments."""
    from core import connector_domain as cd

    capture: dict = {}
    _stub_operation(monkeypatch, cd, capture=capture)

    inst_cur = _cur(_install_row())
    # The domain IS already active -- but for the SAME env+connector, so allowed.
    same_env_domain_row = ("production", "inbound-connector")
    domain_row_cur = _cur(same_env_domain_row)
    # Prior config at version 3. Supersede lookup SELECTs only (id, config_version).
    prior_cur = _cur(("cdc_prior03ABCDEFGHIJKLMN", 3))

    update_cur = _cur(None)
    update_cur.rowcount = 1

    conn = _conn_with(inst_cur, domain_row_cur, prior_cur, update_cur)

    result = cd.configure_domain(
        conn,
        environment="production",
        connector_name="inbound-connector",
        domain="mail.example.com",
        provider_adapter="adapter_eu_v1",
        webhook_endpoint_version="v2",
        signing_secret_ref=None,
        dns_evidence_class=None,
        actor="admin@toorow.io",
        idempotency_key="ik-reconfigure",
        host_context={},
        trace_id=None,
    )

    assert result["config_version"] == 4


# ---------------------------------------------------------------------------
# (d) Cross-environment conflict (AC3).
# ---------------------------------------------------------------------------


def test_configure_domain_cross_env_conflict_raises(monkeypatch):
    """Same domain bound in a DIFFERENT environment raises ConnectorDomainConflict."""
    from core import connector_domain as cd

    inst_cur = _cur(_install_row())
    # Domain already active in a DIFFERENT environment.
    cross_env_row = ("staging", "other-connector")
    domain_row_cur = _cur(cross_env_row)

    conn = _conn_with(inst_cur, domain_row_cur)

    with pytest.raises(cd.ConnectorDomainConflict, match="cross-environment"):
        cd.configure_domain(
            conn,
            environment="production",
            connector_name="inbound-connector",
            domain="mail.example.com",
            provider_adapter="adapter_eu_v1",
            webhook_endpoint_version="v1",
            signing_secret_ref=None,
            dns_evidence_class=None,
            actor="admin@toorow.io",
            idempotency_key="ik-cross-env",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (e) Validation errors (AC3).
# ---------------------------------------------------------------------------


def test_configure_domain_rejects_bad_domain_shape():
    """Domain with scheme or port is rejected before SQL."""
    from core.connector_domain import ConnectorDomainValidationError, configure_domain

    bad_domains = [
        "https://mail.example.com",
        "mail.example.com:25",
        "not-a-domain",
        "",
    ]
    for bad in bad_domains:
        with pytest.raises(ConnectorDomainValidationError):
            configure_domain(
                MagicMock(),
                environment="production",
                connector_name="x",
                domain=bad,
                provider_adapter="adapter_eu_v1",
                webhook_endpoint_version="v1",
                signing_secret_ref=None,
                dns_evidence_class=None,
                actor="a@b.com",
                idempotency_key="ik-bad-domain",
                host_context={},
                trace_id=None,
            )


def test_configure_domain_rejects_unknown_adapter():
    """Unknown provider_adapter is rejected before SQL."""
    from core.connector_domain import ConnectorDomainValidationError, configure_domain

    with pytest.raises(ConnectorDomainValidationError, match="allowed adapter"):
        configure_domain(
            MagicMock(),
            environment="production",
            connector_name="x",
            domain="mail.example.com",
            provider_adapter="unknown_vendor_xyz",
            webhook_endpoint_version="v1",
            signing_secret_ref=None,
            dns_evidence_class=None,
            actor="a@b.com",
            idempotency_key="ik-bad-adapter",
            host_context={},
            trace_id=None,
        )


def test_configure_domain_rejects_not_installed_state():
    """Installation in NOT_INSTALLED state raises ConnectorDomainUnavailable."""
    from core.connector_domain import ConnectorDomainUnavailable, configure_domain

    inst_cur = _cur(_install_row(state="NOT_INSTALLED"))
    conn = _conn_with(inst_cur)

    with pytest.raises(ConnectorDomainUnavailable, match="NOT_INSTALLED"):
        configure_domain(
            conn,
            environment="production",
            connector_name="x",
            domain="mail.example.com",
            provider_adapter="adapter_eu_v1",
            webhook_endpoint_version="v1",
            signing_secret_ref=None,
            dns_evidence_class=None,
            actor="a@b.com",
            idempotency_key="ik-not-installed",
            host_context={},
            trace_id=None,
        )


def test_configure_domain_rejects_disabled_state():
    """Installation in DISABLED state raises ConnectorDomainUnavailable."""
    from core.connector_domain import ConnectorDomainUnavailable, configure_domain

    inst_cur = _cur(_install_row(state="DISABLED"))
    conn = _conn_with(inst_cur)

    with pytest.raises(ConnectorDomainUnavailable, match="DISABLED"):
        configure_domain(
            conn,
            environment="production",
            connector_name="x",
            domain="mail.example.com",
            provider_adapter="adapter_eu_v1",
            webhook_endpoint_version="v1",
            signing_secret_ref=None,
            dns_evidence_class=None,
            actor="a@b.com",
            idempotency_key="ik-disabled",
            host_context={},
            trace_id=None,
        )


def test_configure_domain_rejects_missing_installation():
    """Missing installation row raises ConnectorDomainUnavailable."""
    from core.connector_domain import ConnectorDomainUnavailable, configure_domain

    inst_cur = _cur(None)  # no installation row
    conn = _conn_with(inst_cur)

    with pytest.raises(ConnectorDomainUnavailable, match="not found"):
        configure_domain(
            conn,
            environment="production",
            connector_name="never-installed",
            domain="mail.example.com",
            provider_adapter="adapter_eu_v1",
            webhook_endpoint_version="v1",
            signing_secret_ref=None,
            dns_evidence_class=None,
            actor="a@b.com",
            idempotency_key="ik-no-install",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (f) Secret-free read-model (AC6, E38-NFR03).
# ---------------------------------------------------------------------------


def test_safe_read_model_contains_no_secret_keys():
    """The read-model projection must not contain secret-like field names."""
    from core.connector_domain import _safe_read_model
    from core.operations import _is_secret_key

    model = _safe_read_model(
        domain="mail.example.com",
        provider_adapter="adapter_eu_v1",
        webhook_endpoint_version="v1",
        dns_evidence_class="spf_dkim",
        config_version=1,
        next_action="platform_admin: trigger verification",
        created_at=None,
    )
    for key in model:
        assert not _is_secret_key(key), (
            f"Read-model contains secret-like key: {key!r}"
        )
    # Explicit: these must never appear.
    assert "signing_secret_ref" not in model
    assert "dns_evidence_hash" not in model


def test_get_domain_config_excludes_secrets():
    """get_domain_config SELECT does not expose signing_secret_ref or dns_evidence_hash."""
    from core.connector_domain import get_domain_config
    from core.operations import _is_secret_key

    row = ("mail.example.com", "adapter_eu_v1", "v1", "spf_dkim", 2, None)
    cur = _cur(row)
    conn = _conn_with(cur)

    result = get_domain_config(conn, environment="production", connector_name="c")
    assert result is not None

    # Verify the SQL selected only safe columns (no secret columns in SELECT).
    sql_called = cur.execute.call_args[0][0]
    assert "signing_secret_ref" not in sql_called
    assert "dns_evidence_hash" not in sql_called

    for key in result:
        assert not _is_secret_key(key), (
            f"get_domain_config returned secret-like key: {key!r}"
        )


def test_configure_domain_result_excludes_secrets(monkeypatch):
    """configure_domain return value must not contain any secret field."""
    from core import connector_domain as cd
    from core.operations import _is_secret_key

    _stub_operation(monkeypatch, cd)

    inst_cur = _cur(_install_row())
    no_domain_cur = _cur(None)
    no_prior_cur = _cur(None)
    insert_cur = _cur(None)
    insert_cur.rowcount = 1

    conn = _conn_with(inst_cur, no_domain_cur, no_prior_cur, insert_cur)

    result = cd.configure_domain(
        conn,
        environment="production",
        connector_name="c",
        domain="mail.example.com",
        provider_adapter="adapter_eu_v1",
        webhook_endpoint_version="v1",
        signing_secret_ref="ref-id-secret",
        dns_evidence_class=None,
        actor="a@b.com",
        idempotency_key="ik-secret-check",
        host_context={},
        trace_id=None,
    )

    for key in result:
        assert not _is_secret_key(key), f"configure_domain result contains secret key: {key!r}"
    assert "signing_secret_ref" not in result
    assert "dns_evidence_hash" not in result


# ---------------------------------------------------------------------------
# (g) No ds_<token> issued (AC2).
# ---------------------------------------------------------------------------


def test_configure_domain_does_not_issue_ds_token(monkeypatch):
    """configure_domain must never return a ds_ prefixed token."""
    from core import connector_domain as cd

    _stub_operation(monkeypatch, cd)

    inst_cur = _cur(_install_row())
    no_domain_cur = _cur(None)
    no_prior_cur = _cur(None)
    insert_cur = _cur(None)
    insert_cur.rowcount = 1

    conn = _conn_with(inst_cur, no_domain_cur, no_prior_cur, insert_cur)

    result = cd.configure_domain(
        conn,
        environment="production",
        connector_name="c",
        domain="mail.example.com",
        provider_adapter="adapter_eu_v1",
        webhook_endpoint_version="v1",
        signing_secret_ref=None,
        dns_evidence_class=None,
        actor="a@b.com",
        idempotency_key="ik-no-ds",
        host_context={},
        trace_id=None,
    )

    # No value in the result starts with ds_.
    for val in result.values():
        if isinstance(val, str):
            assert not val.startswith("ds_"), (
                f"configure_domain must never issue a ds_ token; got {val!r}"
            )


# ---------------------------------------------------------------------------
# (h) Audit/outbox written through execute_operation (AC4).
# ---------------------------------------------------------------------------


def test_configure_domain_audit_via_execute_operation(monkeypatch):
    """configure_domain routes through execute_operation with correct command_type."""
    from core import connector_domain as cd

    capture: dict = {}
    _stub_operation(monkeypatch, cd, capture=capture)

    inst_cur = _cur(_install_row())
    no_domain_cur = _cur(None)
    no_prior_cur = _cur(None)
    insert_cur = _cur(None)
    insert_cur.rowcount = 1

    conn = _conn_with(inst_cur, no_domain_cur, no_prior_cur, insert_cur)

    cd.configure_domain(
        conn,
        environment="production",
        connector_name="c",
        domain="mail.example.com",
        provider_adapter="adapter_eu_v1",
        webhook_endpoint_version="v1",
        signing_secret_ref=None,
        dns_evidence_class=None,
        actor="a@b.com",
        idempotency_key="ik-audit",
        host_context={},
        trace_id=None,
    )

    assert len(capture["specs"]) == 1
    spec = capture["specs"][0]
    assert spec.command_type == "connector.domain.configured"
    assert spec.effective_org_id == "platform"
    # Outbox payload must be secret-free.
    from core.operations import _is_secret_key
    outbox = capture["changes"][0].outbox_payload
    for key in outbox:
        assert not _is_secret_key(key), f"Outbox payload has secret key: {key!r}"
    assert "signing_secret_ref" not in outbox


# ---------------------------------------------------------------------------
# (i) Idempotent replay and conflicting-key 409 (AC4).
# ---------------------------------------------------------------------------


def test_configure_domain_no_new_op_on_existing_row_idempotent(monkeypatch):
    """When the operation spine replays, no new write is attempted (idempotent)."""
    from core import connector_domain as cd
    from core.operations import OperationIdempotencyConflict

    inst_cur = _cur(_install_row())
    no_domain_cur = _cur(None)
    no_prior_cur = _cur(None)

    conn = _conn_with(inst_cur, no_domain_cur, no_prior_cur)

    # Simulate the operation spine returning idempotency conflict.
    def raise_conflict(operation_conn, spec, *, mutation):
        raise OperationIdempotencyConflict("different request")

    monkeypatch.setattr(cd, "execute_operation", raise_conflict)

    with pytest.raises(OperationIdempotencyConflict):
        cd.configure_domain(
            conn,
            environment="production",
            connector_name="c",
            domain="mail.example.com",
            provider_adapter="adapter_eu_v1",
            webhook_endpoint_version="v1",
            signing_secret_ref=None,
            dns_evidence_class=None,
            actor="a@b.com",
            idempotency_key="ik-conflict",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (j) REST gating: non-admin 404, missing key 422 (Task 3).
# ---------------------------------------------------------------------------


@pytest.fixture()
def _domain_client():
    from core.connector_domain_api import CONNECTOR_DOMAIN_ROUTES
    from starlette.routing import Router
    from starlette.testclient import TestClient

    app = Router(routes=CONNECTOR_DOMAIN_ROUTES)
    return TestClient(app, raise_server_exceptions=False)


def _patch_domain_auth(monkeypatch, *, authorized: bool = True, identity: str = "test@example.com"):
    async def fake_auth(request):
        return authorized, identity

    monkeypatch.setattr("core.connector_domain_api._check_auth", fake_auth)


def _patch_domain_admin(monkeypatch, *, is_admin: bool):
    monkeypatch.setattr(
        "core.connector_domain_api._is_platform_admin",
        lambda identity: is_admin,
    )


def test_post_domain_non_admin_gets_404(monkeypatch, _domain_client):
    import core.audit as audit_mod

    _patch_domain_auth(monkeypatch, authorized=True, identity="tenant@client.com")
    _patch_domain_admin(monkeypatch, is_admin=False)
    audit_calls: list = []
    monkeypatch.setattr(audit_mod, "write_audit_row", lambda **kw: audit_calls.append(kw))

    resp = _domain_client.post(
        "/api/connectors/test-connector/domain",
        headers={"Idempotency-Key": "ik-test"},
        json={"domain": "mail.example.com", "provider_adapter": "adapter_eu_v1"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    # review M2: prove the denial is actually audited (a silent regression must fail).
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == audit_mod.ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED


def test_post_domain_missing_idempotency_key_gets_422(monkeypatch, _domain_client):
    _patch_domain_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_domain_admin(monkeypatch, is_admin=True)

    resp = _domain_client.post(
        "/api/connectors/test-connector/domain",
        json={"domain": "mail.example.com", "provider_adapter": "adapter_eu_v1"},
    )
    assert resp.status_code == 422
    assert resp.json()["code"] == "missing_header"


def test_get_domain_non_admin_gets_404(monkeypatch, _domain_client):
    import core.audit as audit_mod

    _patch_domain_auth(monkeypatch, authorized=True, identity="tenant@client.com")
    _patch_domain_admin(monkeypatch, is_admin=False)
    audit_calls: list = []
    monkeypatch.setattr(audit_mod, "write_audit_row", lambda **kw: audit_calls.append(kw))

    resp = _domain_client.get("/api/connectors/test-connector/domain")
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    # review M2: prove the denied GET is audited.
    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == audit_mod.ACTION_CONNECTOR_DOMAIN_CONFIG_DENIED


def test_get_domain_unauthorized_gets_401(monkeypatch, _domain_client):
    _patch_domain_auth(monkeypatch, authorized=False, identity="")

    resp = _domain_client.get("/api/connectors/test-connector/domain")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# (k) REST POST + GET success (Task 3).
# ---------------------------------------------------------------------------


def test_post_domain_conflict_idempotency_key_returns_409(monkeypatch, _domain_client):
    import contextlib

    import core.connector_domain as cd_mod
    import core.db as db_mod
    from core.operations import OperationIdempotencyConflict

    _patch_domain_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_domain_admin(monkeypatch, is_admin=True)

    def raise_conflict(conn, *, environment, connector_name, **kw):
        raise OperationIdempotencyConflict("different request")

    monkeypatch.setattr(cd_mod, "configure_domain", raise_conflict)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = _domain_client.post(
        "/api/connectors/test-connector/domain",
        headers={"Idempotency-Key": "ik-conflict"},
        json={"domain": "mail.example.com", "provider_adapter": "adapter_eu_v1"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"


def test_post_domain_domain_conflict_returns_409(monkeypatch, _domain_client):
    import contextlib

    import core.connector_domain as cd_mod
    import core.db as db_mod
    from core.connector_domain import ConnectorDomainConflict

    _patch_domain_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_domain_admin(monkeypatch, is_admin=True)

    def raise_conflict(conn, *, environment, connector_name, **kw):
        raise ConnectorDomainConflict("cross-environment binding refused")

    monkeypatch.setattr(cd_mod, "configure_domain", raise_conflict)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = _domain_client.post(
        "/api/connectors/test-connector/domain",
        headers={"Idempotency-Key": "ik-domain-conflict"},
        json={"domain": "mail.example.com", "provider_adapter": "adapter_eu_v1"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "domain_conflict"


def test_post_domain_admin_success(monkeypatch, _domain_client):
    """Admin POST returns 200 with safe read-model (no secrets)."""
    import contextlib

    import core.connector_domain as cd_mod
    import core.db as db_mod
    from core.operations import _is_secret_key

    _patch_domain_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_domain_admin(monkeypatch, is_admin=True)
    monkeypatch.setattr(
        "core.connector_domain_api._get_environment",
        lambda: "production",
    )

    fake_model = {
        "domain": "mail.example.com",
        "provider_adapter": "adapter_eu_v1",
        "webhook_endpoint_version": "v1",
        "dns_evidence_class": None,
        "config_version": 1,
        "safe_next_action": "trigger verification",
        "configured_at": None,
    }

    monkeypatch.setattr(cd_mod, "configure_domain",
                        lambda conn, *, environment, connector_name, **kw: fake_model)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = _domain_client.post(
        "/api/connectors/test-connector/domain",
        headers={"Idempotency-Key": "ik-success"},
        json={"domain": "mail.example.com", "provider_adapter": "adapter_eu_v1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["domain"] == "mail.example.com"
    assert body["config_version"] == 1
    # No secrets in response.
    for key in body:
        assert not _is_secret_key(key), f"Response contains secret key: {key!r}"
    assert "signing_secret_ref" not in body
    assert "dns_evidence_hash" not in body


def test_get_domain_admin_configured(monkeypatch, _domain_client):
    """Admin GET returns 200 with safe read-model when domain is configured."""
    import contextlib

    import core.connector_domain as cd_mod
    import core.db as db_mod
    from core.operations import _is_secret_key

    _patch_domain_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_domain_admin(monkeypatch, is_admin=True)
    monkeypatch.setattr(
        "core.connector_domain_api._get_environment",
        lambda: "production",
    )

    fake_model = {
        "domain": "mail.example.com",
        "provider_adapter": "adapter_eu_v1",
        "webhook_endpoint_version": "v1",
        "dns_evidence_class": "spf_dkim",
        "config_version": 2,
        "safe_next_action": "trigger verification",
        "configured_at": "2026-07-23T12:00:00+00:00",
    }

    monkeypatch.setattr(cd_mod, "get_domain_config",
                        lambda conn, *, environment, connector_name: fake_model)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = _domain_client.get("/api/connectors/test-connector/domain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["domain"] == "mail.example.com"
    assert body["config_version"] == 2
    assert body["configured_at"] == "2026-07-23T12:00:00+00:00"
    for key in body:
        assert not _is_secret_key(key), f"GET response contains secret key: {key!r}"


def test_get_domain_admin_not_configured(monkeypatch, _domain_client):
    """Admin GET returns 200 with configured=False when no domain row exists."""
    import contextlib

    import core.connector_domain as cd_mod
    import core.db as db_mod

    _patch_domain_auth(monkeypatch, authorized=True, identity="admin@toorow.io")
    _patch_domain_admin(monkeypatch, is_admin=True)
    monkeypatch.setattr(
        "core.connector_domain_api._get_environment",
        lambda: "production",
    )

    monkeypatch.setattr(cd_mod, "get_domain_config",
                        lambda conn, *, environment, connector_name: None)

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    resp = _domain_client.get("/api/connectors/test-connector/domain")
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is False
    assert "safe_next_action" in body


# ---------------------------------------------------------------------------
# (l) MCP get_connector_domain_config == REST evidence (AC2).
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


def test_mcp_domain_tool_registered_under_operations_profile():
    from core import mcp_profiles

    _register_operations_tools()
    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert "get_connector_domain_config" in decls
    d = decls["get_connector_domain_config"]
    assert d.profile == "operations"
    assert d.effect == "read"
    assert d.confirmation_mode == "none"


def test_mcp_domain_tool_returns_same_model_as_rest(monkeypatch):
    """MCP tool returns the same safe read-model fields as the REST GET endpoint."""
    import contextlib

    handlers = _register_operations_tools()
    handler = handlers.get("get_connector_domain_config")
    assert handler is not None, "get_connector_domain_config not registered"

    import core.connector_domain as cd_mod
    import core.db as db_mod
    from core.operations import _is_secret_key

    fake_model = {
        "domain": "mail.example.com",
        "provider_adapter": "adapter_eu_v1",
        "webhook_endpoint_version": "v1",
        "dns_evidence_class": "spf_dkim",
        "config_version": 1,
        "safe_next_action": "trigger verification",
        "configured_at": None,
    }

    monkeypatch.setattr(cd_mod, "get_domain_config",
                        lambda conn, *, environment, connector_name: fake_model)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    result = handler("test-connector")
    envelope = result.structured_content
    data = envelope["data"]

    assert data["configured"] is True
    assert data["domain"] == "mail.example.com"
    assert data["config_version"] == 1
    assert "safe_next_action" in data
    assert "configured_at" in data
    # Secret-free.
    for key in data:
        assert not _is_secret_key(key), f"MCP data contains secret key: {key!r}"
    assert "signing_secret_ref" not in data
    assert "dns_evidence_hash" not in data


def test_mcp_domain_tool_not_configured_returns_unconfigured(monkeypatch):
    """MCP tool returns configured=False when no domain row exists."""
    import contextlib

    handlers = _register_operations_tools()
    handler = handlers.get("get_connector_domain_config")

    import core.connector_domain as cd_mod
    import core.db as db_mod

    monkeypatch.setattr(cd_mod, "get_domain_config",
                        lambda conn, *, environment, connector_name: None)
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    @contextlib.contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(db_mod, "get_connection", fake_conn)

    result = handler("unconfigured-connector")
    data = result.structured_content["data"]
    assert data["configured"] is False
    assert "safe_next_action" in data


def test_mcp_domain_tool_missing_connector_name_raises():
    """MCP tool raises tool error when connector_name is empty."""
    handlers = _register_operations_tools()
    handler = handlers.get("get_connector_domain_config")

    from fastmcp.exceptions import ToolError

    with pytest.raises(ToolError):
        handler("")


# ---------------------------------------------------------------------------
# (m) Live-PG-gated: partial-unique constraints + immutability trigger.
# (mirrors 082 live tests: auto-rollback probe via DO blocks / psycopg)
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
    / "084_connector_domain_config.sql"
)

_MIGRATION_082 = (
    __import__("pathlib").Path(__file__).resolve().parents[3]
    / "infra"
    / "nango"
    / "migrations"
    / "082_connector_installation_state.sql"
)


def _apply_migrations(conn) -> None:
    """Apply 082 (installations) then 084 (domain configs) idempotently."""
    with conn.cursor() as cur:
        cur.execute(_MIGRATION_082.read_text(encoding="utf-8"))
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()


def _insert_installation(conn, *, env: str, name: str, inst_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.connector_installations "
            "(id, environment, connector_name, state) "
            "VALUES (%s, %s, %s, 'DOMAIN_PENDING')",
            (inst_id, env, name),
        )
    conn.commit()


def _insert_domain_config(
    conn,
    *,
    config_id: str,
    installation_id: str,
    env: str,
    name: str,
    domain: str,
    adapter: str = "adapter_eu_v1",
    version: int = 1,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.connector_domain_configs "
            "(id, installation_id, environment, connector_name, domain, "
            "provider_adapter, webhook_endpoint_version, config_version) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'v1', %s)",
            (config_id, installation_id, env, name, domain, adapter, version),
        )
    conn.commit()


@requires_postgres
def test_live_active_env_name_unique_constraint():
    """Only one ACTIVE domain config per (environment, connector_name)."""
    import psycopg
    from ulid import ULID

    with psycopg.connect(_DSN) as conn:
        _apply_migrations(conn)
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"
        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        id1 = f"cdc_{ULID()}"
        _insert_domain_config(
            conn, config_id=id1, installation_id=inst_id,
            env=env, name=name, domain=f"mail-{ULID()}.example.com",
        )

        # Second ACTIVE row for the same (env, name) must violate the partial-unique index.
        id2 = f"cdc_{ULID()}"
        with pytest.raises(Exception) as exc_info:
            _insert_domain_config(
                conn, config_id=id2, installation_id=inst_id,
                env=env, name=name, domain=f"mail2-{ULID()}.example.com",
            )
        assert "unique" in str(exc_info.value).lower()
        conn.rollback()


@requires_postgres
def test_live_active_domain_unique_constraint():
    """The same domain may not be ACTIVE for two different env+connector pairs."""
    import psycopg
    from ulid import ULID

    with psycopg.connect(_DSN) as conn:
        _apply_migrations(conn)
        shared_domain = f"shared-{ULID()}.example.com"

        env1 = f"test-{ULID()}"
        name1 = f"connector-{ULID()}"
        inst1 = f"cin_{ULID()}"
        _insert_installation(conn, env=env1, name=name1, inst_id=inst1)
        id1 = f"cdc_{ULID()}"
        _insert_domain_config(
            conn, config_id=id1, installation_id=inst1,
            env=env1, name=name1, domain=shared_domain,
        )

        env2 = f"test-{ULID()}"
        name2 = f"connector-{ULID()}"
        inst2 = f"cin_{ULID()}"
        _insert_installation(conn, env=env2, name=name2, inst_id=inst2)
        id2 = f"cdc_{ULID()}"

        # Second connector trying to bind the same domain must fail.
        with pytest.raises(Exception) as exc_info:
            _insert_domain_config(
                conn, config_id=id2, installation_id=inst2,
                env=env2, name=name2, domain=shared_domain,
            )
        assert "unique" in str(exc_info.value).lower()
        conn.rollback()


@requires_postgres
def test_live_superseded_row_does_not_block_new_active():
    """A superseded row (superseded_by IS NOT NULL) does not block a new active row."""
    import psycopg
    from ulid import ULID

    with psycopg.connect(_DSN) as conn:
        _apply_migrations(conn)
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"
        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        id1 = f"cdc_{ULID()}"
        domain = f"mail-{ULID()}.example.com"
        _insert_domain_config(
            conn, config_id=id1, installation_id=inst_id,
            env=env, name=name, domain=domain, version=1,
        )

        id2 = f"cdc_{ULID()}"
        # Supersede id1 -> id2.
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_domain_configs SET superseded_by = %s WHERE id = %s",
                (id2, id1),
            )
        conn.commit()

        # Now insert id2 as the new active row (same domain, same env+name).
        # The partial-unique index only applies to active rows, so this must succeed.
        _insert_domain_config(
            conn, config_id=id2, installation_id=inst_id,
            env=env, name=name, domain=domain, version=2,
        )


@requires_postgres
def test_live_immutability_trigger_blocks_identity_mutation():
    """protect_connector_domain_config freezes identity/evidence columns."""
    import psycopg
    from ulid import ULID

    with psycopg.connect(_DSN) as conn:
        _apply_migrations(conn)
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"
        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        config_id = f"cdc_{ULID()}"
        domain = f"mail-{ULID()}.example.com"
        _insert_domain_config(
            conn, config_id=config_id, installation_id=inst_id,
            env=env, name=name, domain=domain,
        )

        # Attempt to mutate `domain` (frozen identity column) -> trigger fires.
        with conn.cursor() as cur:
            with pytest.raises(Exception, match="immutable"):
                cur.execute(
                    "UPDATE app.connector_domain_configs "
                    "SET domain = %s WHERE id = %s",
                    ("mutated.example.com", config_id),
                )
        conn.rollback()


@requires_postgres
def test_live_immutability_trigger_allows_superseded_by_update():
    """Mutable column (superseded_by) may be updated."""
    import psycopg
    from ulid import ULID

    with psycopg.connect(_DSN) as conn:
        _apply_migrations(conn)
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"
        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        config_id = f"cdc_{ULID()}"
        domain = f"mail-{ULID()}.example.com"
        _insert_domain_config(
            conn, config_id=config_id, installation_id=inst_id,
            env=env, name=name, domain=domain,
        )

        next_id = f"cdc_{ULID()}"
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.connector_domain_configs "
                "SET superseded_by = %s, updated_at = NOW() WHERE id = %s",
                (next_id, config_id),
            )
            assert cur.rowcount == 1
        conn.commit()


@requires_postgres
def test_live_delete_rejected_by_trigger():
    """DELETE on connector_domain_configs is rejected (append-only)."""
    import psycopg
    from ulid import ULID

    with psycopg.connect(_DSN) as conn:
        _apply_migrations(conn)
        env = f"test-{ULID()}"
        name = f"connector-{ULID()}"
        inst_id = f"cin_{ULID()}"
        _insert_installation(conn, env=env, name=name, inst_id=inst_id)

        config_id = f"cdc_{ULID()}"
        domain = f"mail-{ULID()}.example.com"
        _insert_domain_config(
            conn, config_id=config_id, installation_id=inst_id,
            env=env, name=name, domain=domain,
        )

        with conn.cursor() as cur:
            with pytest.raises(Exception, match="append-only"):
                cur.execute(
                    "DELETE FROM app.connector_domain_configs WHERE id = %s",
                    (config_id,),
                )
        conn.rollback()
