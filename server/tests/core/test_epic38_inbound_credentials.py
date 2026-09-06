"""Story 38.7: Inbound delivery credential lifecycle -- offline unit + ASGI + live-PG.

Covers (per AC + Tasks 1-5):
  (a) issue: stores only hash + suffix; full_secret in result; raw token NOT in
      any persisted column, audit payload, outbox payload, or read-model.
  (b) show-once: full_secret is in the issue result but ABSENT from GET /credentials
      list and MCP get_inbound_credential_status (AC2, E38-NFR03).
  (c) SECRET-LEAK-SCANNER: asserts raw token string does not appear in audit payload,
      outbox payload, persisted request_payload, or the safe read-model (AC2).
  (d) rotation: prior moves to ROTATING with overlap_until; new is ACTIVE.
  (e) rotation + immediate_revoke=True: prior is immediately REVOKED.
  (f) revoke -> fail-closed: resolve_for_delivery returns denial after revoke.
  (g) resolve_for_delivery: ACTIVE token -> scope; revoked/expired/unknown -> denial.
  (h) resolve_for_delivery: constant-shape denial (same shape for all failures, AC4).
  (i) rate-limit: non-enumerating denial (InboundCredentialRateLimited).
  (j) partial-unique: only one ACTIVE/ROTATING per (datastream_id, channel).
  (k) domain-not-READY gate: issue raises InboundCredentialDomainNotReady.
  (l) idempotent issue: same key replays cleanly (no duplicate).
  (m) conflicting Idempotency-Key -> OperationIdempotencyConflict.
  (n) request_payload has NO raw token and NO random id (deterministic idempotency).
  (o) outbox payload has NO raw token, NO token_hash.
  (p) MCP get_inbound_credential_status registered + returns safe model (no secret).
  (q) ASGI: POST issue returns 200 with full_secret; GET list returns no secret.
  (r) ASGI: revoke after issue returns 200 with REVOKED state.
  (s) ASGI: missing Idempotency-Key -> 422.
  (t) live-PG-gated: partial-unique + immutability trigger + no-raw-token-column.

INVARIANTS CHECKED:
  * Source-agnostic: the test carries no mailgun/cloudflare/receiptadapter vocabulary
    (test_source_agnostic_boundary.py scans the core directory at file level; this
    test itself must also comply -- its docstrings and helpers are provider-free).
  * Non-tautological: mock cursor SEQUENCE matches the real SQL call order (no
    dead cursors); assertions are specific enough to catch actual implementation bugs.
"""

from __future__ import annotations

import hashlib
import json
from unittest.mock import MagicMock

import pytest

from tests.conftest import REPO_ROOT

# ---------------------------------------------------------------------------
# Shared mock helpers (mirrors test_epic38_connector_activation.py style).
# ---------------------------------------------------------------------------


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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
    conn._locked_datastream_info = {
        "connector_name": "my_connector",
        "org_id": "org-1",
        "enabled": False,
        "lifecycle_state": "draft",
        "channels": {"email", "webhook"},
    }
    return conn


@pytest.fixture(autouse=True)
def _isolate_time_and_resolution_side_effects(monkeypatch):
    """Existing units own one SQL seam; dedicated tests exercise new side effects."""
    from core import inbound_credentials as ic

    real_materialize = ic._materialize_due_expirations
    monkeypatch.setattr(ic, "_materialize_due_expirations", lambda conn, **kwargs: None)
    monkeypatch.setattr(ic, "_enforce_resolution_rate_limit", lambda conn, **kwargs: None)
    monkeypatch.setattr(ic, "_record_resolution_rate_event", lambda conn, **kwargs: None)
    return real_materialize


def _stub_operation(monkeypatch, module, *, capture=None):
    """Stub execute_operation to run the mutation synchronously."""
    from core import operations

    real_status = module._get_datastream_status

    def locked_status(conn, *, datastream_id, hold_lifecycle=False):
        if hold_lifecycle:
            return conn._locked_datastream_info
        return real_status(conn, datastream_id=datastream_id)

    monkeypatch.setattr(module, "_get_datastream_status", locked_status)

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-test-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-test-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


def _make_domain_cfg(domain: str = "inbound.example.com") -> dict:
    return {
        "domain": domain,
        "provider_adapter": "adapter_eu_v1",
        "webhook_endpoint_version": "v1",
        "dns_evidence_class": None,
        "config_version": 1,
        "safe_next_action": "platform_admin: ...",
        "configured_at": "2026-01-01T00:00:00+00:00",
    }


# ---------------------------------------------------------------------------
# Helpers: patch the domain/installation READY gate for issue tests.
# ---------------------------------------------------------------------------


def _patch_domain_ready(monkeypatch, domain: str = "inbound.example.com"):
    """Patch the domain and installation-READY gates to pass."""
    monkeypatch.setattr(
        "core.connector_domain.get_domain_config",
        lambda conn, *, environment, connector_name: _make_domain_cfg(domain),
    )
    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda conn, *, connector_name, environment: None,
    )


def _patch_rate_limit_pass(monkeypatch):
    """Patch check_rate_limit to always pass."""
    monkeypatch.setattr(
        "core.inbound_credentials.check_rate_limit",
        lambda conn, **kwargs: None,
    )


# ---------------------------------------------------------------------------
# (a) + (b) + (c) SECRET-LEAK-SCANNER
#   issue stores only hash + suffix; full_secret returned ONCE;
#   raw token absent from audit/outbox/request_payload/read-model.
# ---------------------------------------------------------------------------


def _datastream_row(*, state: str = "draft"):
    return ("my_connector", "org-1", state == "active", state, ["email", "webhook"])


def test_issue_stores_only_hash_and_suffix_never_raw_token(monkeypatch):
    """AC1/AC2: only the hash/suffix persist; secret stays ephemeral."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    cur_ds = _cur(_datastream_row())
    cur_existing = _cur(None)
    cur_insert = _cur((ts, None))
    cur_event = _cur(None)
    result = ic.issue(
        _conn_with(cur_ds, cur_existing, cur_insert, cur_event),
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="ik-issue-1",
        host_context={},
        trace_id=None,
    )
    secret = result["full_secret"]
    token = secret.split("@")[0][3:]
    assert result["secret_available"] is True
    assert result["safe_suffix"] == token[-6:]
    assert "token_hash" not in captured["specs"][0].request_payload
    assert secret not in json.dumps(captured["specs"][0].request_payload)
    assert secret not in json.dumps(captured["changes"][0].result)
    assert secret not in json.dumps(captured["changes"][0].outbox_payload)
    insert_params = cur_insert.execute.call_args.args[1]
    assert insert_params[3] == _sha256(token)
    assert insert_params[4] == token[-6:]


def test_issue_returns_full_secret_for_webhook_channel(monkeypatch):
    """Webhook issuance returns a bearer token, never an email address."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    cur_insert = _cur((ts, None))
    result = ic.issue(
        _conn_with(_cur(_datastream_row()), _cur(None), cur_insert, _cur(None)),
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-issue-wh",
        host_context={},
        trace_id=None,
    )
    secret = result["full_secret"]
    assert "@" not in secret
    assert len(secret) > 20
    assert cur_insert.execute.call_args.args[1][3] == _sha256(secret)
    assert "token_hash" not in captured["specs"][0].request_payload


def test_concurrent_pending_replay_returns_conflict_not_fabricated_success(monkeypatch):
    from core import inbound_credentials as ic
    from core.operations import OperationResult

    monkeypatch.setattr(
        ic,
        "execute_operation",
        lambda conn, spec, *, mutation: OperationResult(
            "op-pending", "pending", {}, None, None, True
        ),
    )
    with pytest.raises(ic.InboundCredentialConflict, match="still in progress"):
        ic.issue(
            _conn_with(_cur(_datastream_row())),
            datastream_id="ds-1",
            channel="email",
            actor="operator@example.com",
            idempotency_key="concurrent-key",
            host_context={},
            trace_id=None,
        )


def test_issue_replay_returns_safe_model_without_second_secret(monkeypatch):
    """Same operation replay is successful but cannot reveal the secret twice."""
    from core import inbound_credentials as ic
    from core.operations import OperationResult

    safe = {
        "credential_id": "dic_01JZAAABBBCCCDDDEEEFFF00001",
        "datastream_id": "ds-1",
        "channel": "email",
        "safe_suffix": "abcdef",
        "state": "ACTIVE",
        "version": 1,
        "expires_at": None,
        "overlap_until": None,
        "issued_by": "operator@example.com",
        "created_at": "2030-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        ic,
        "execute_operation",
        lambda conn, spec, *, mutation: OperationResult(
            "op-1", "succeeded", safe, "audit-1", "outbox-1", True
        ),
    )
    result = ic.issue(
        _conn_with(_cur(_datastream_row())),
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="same-key",
        host_context={},
        trace_id=None,
    )
    assert result["secret_available"] is False
    assert "full_secret" not in result


# ---------------------------------------------------------------------------
# (b) show-once: full_secret absent from GET read-model and MCP.
# ---------------------------------------------------------------------------


def test_get_credential_state_never_returns_secret():
    """AC2/E38-NFR03: get_credential_state returns NO full_secret, NO token_hash."""
    import datetime

    from core import inbound_credentials as ic

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cur = _cur(
        (
            "dic_01JZAAABBBCCCDDDEEEFFF00001",
            "email",
            "abcdef",
            "ACTIVE",
            1,
            None,
            None,
            "operator@example.com",
            ts,
        ),
        rowcount=1,
    )
    conn = _conn_with(cur)

    model = ic.get_credential_state(
        conn,
        credential_id="dic_01JZAAABBBCCCDDDEEEFFF00001",
        datastream_id="ds-1",
    )

    assert model is not None
    assert "full_secret" not in model, "get_credential_state must NOT return full_secret"
    assert "token_hash" not in model, "get_credential_state must NOT return token_hash"


def test_safe_reads_project_expiry_without_writes(monkeypatch):
    """Read-effect helpers project expiry but never materialize or execute an operation."""
    import datetime

    from core import inbound_credentials as ic

    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    row = (
        "dic_01JZAAABBBCCCDDDEEEFFF00001",
        "email",
        "abcdef",
        "ACTIVE",
        1,
        past,
        None,
        "operator@example.com",
        past,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("safe credential reads must not write")

    monkeypatch.setattr(ic, "_materialize_due_expirations", forbidden)
    monkeypatch.setattr(ic, "execute_operation", forbidden)

    model = ic.get_credential_state(
        _conn_with(_cur(row)),
        credential_id=row[0],
        datastream_id="ds-1",
    )
    assert model is not None
    assert model["state"] == "EXPIRED"

    assert (
        ic.list_credentials(
            _conn_with(_cur(fetchall=[row])),
            datastream_id="ds-1",
            include_terminal=False,
        )
        == []
    )
    terminal = ic.list_credentials(
        _conn_with(_cur(fetchall=[row])),
        datastream_id="ds-1",
        include_terminal=True,
    )
    assert terminal[0]["state"] == "EXPIRED"


def test_list_credentials_never_returns_secret():
    """AC2/E38-NFR03: list_credentials returns NO full_secret, NO token_hash."""
    import datetime

    from core import inbound_credentials as ic

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cur = _cur(
        fetchall=[
            (
                "dic_01JZAAABBBCCCDDDEEEFFF00001",
                "email",
                "abcdef",
                "ACTIVE",
                1,
                None,
                None,
                "operator@example.com",
                ts,
            ),
        ],
        rowcount=0,
    )
    conn = _conn_with(cur)

    models = ic.list_credentials(conn, datastream_id="ds-1")
    assert isinstance(models, list)
    for m in models:
        assert "full_secret" not in m, "list_credentials must NOT return full_secret"
        assert "token_hash" not in m, "list_credentials must NOT return token_hash"


# ---------------------------------------------------------------------------
# (d) Rotation: prior -> ROTATING with overlap_until; new -> ACTIVE.
# ---------------------------------------------------------------------------


def test_rotate_moves_prior_to_rotating_and_issues_new_active(monkeypatch):
    """Rotation locks ACTIVE, gives old row an overlap and inserts one new ACTIVE."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    prior_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    cur_prior = _cur((prior_id, "ACTIVE", 1, "email"))
    cur_update = _cur((prior_id,))
    cur_insert = _cur((ts,))
    result = ic.rotate(
        _conn_with(_cur(_datastream_row()), cur_prior, cur_update, cur_insert, _cur(None)),
        credential_id=prior_id,
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="ik-rotate-1",
        host_context={},
        trace_id=None,
        overlap_seconds=3600,
    )
    assert result["secret_available"] is True
    assert "@inbound.example.com" in result["full_secret"]
    change = captured["changes"][0]
    assert change.outbox_payload["prior_new_state"] == "ROTATING"
    sql_calls = [call.args[0] for call in cur_update.execute.call_args_list]
    assert any("state = 'ROTATING'" in sql for sql in sql_calls)
    assert result["full_secret"] not in json.dumps(change.outbox_payload)


def test_rotate_immediate_revoke_sets_prior_to_revoked(monkeypatch):
    """Immediate policy revokes the old row instead of creating an overlap."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    prior_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    result = ic.rotate(
        _conn_with(
            _cur(_datastream_row()),
            _cur((prior_id, "ACTIVE", 1, "webhook")),
            _cur((prior_id,)),
            _cur((ts,)),
            _cur(None),
        ),
        credential_id=prior_id,
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-rotate-imm",
        host_context={},
        trace_id=None,
        immediate_revoke=True,
    )
    assert result["secret_available"] is True
    assert captured["changes"][0].outbox_payload["prior_new_state"] == "REVOKED"
    assert captured["specs"][0].request_payload["immediate_revoke"] is True


def test_rotate_replay_does_not_generate_or_return_a_secret(monkeypatch):
    from core import inbound_credentials as ic
    from core.operations import OperationResult

    safe = {
        "credential_id": "dic_01JZAAABBBCCCDDDEEEFFF00002",
        "datastream_id": "ds-1",
        "channel": "email",
        "safe_suffix": "ghijkl",
        "state": "ACTIVE",
        "version": 2,
        "expires_at": None,
        "overlap_until": None,
        "issued_by": "operator@example.com",
        "created_at": "2030-01-01T00:00:00+00:00",
    }
    monkeypatch.setattr(
        ic,
        "execute_operation",
        lambda conn, spec, *, mutation: OperationResult(
            "op-2", "succeeded", safe, "audit-2", "outbox-2", True
        ),
    )
    result = ic.rotate(
        _conn_with(_cur(_datastream_row())),
        credential_id="dic_01JZAAABBBCCCDDDEEEFFF00001",
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="same-rotate",
        host_context={},
        trace_id=None,
    )
    assert result["secret_available"] is False
    assert "full_secret" not in result


def test_rotate_refuses_when_revoke_won_the_lock(monkeypatch):
    """A row observed as REVOKED under lock cannot be resurrected by rotation."""
    from core import inbound_credentials as ic

    _stub_operation(monkeypatch, ic)
    _patch_rate_limit_pass(monkeypatch)
    prior_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    with pytest.raises(ic.InboundCredentialConflict):
        ic.rotate(
            _conn_with(_cur(_datastream_row()), _cur((prior_id, "REVOKED", 1, "email"))),
            credential_id=prior_id,
            datastream_id="ds-1",
            channel="email",
            actor="operator@example.com",
            idempotency_key="rotate-after-revoke",
            host_context={},
            trace_id=None,
        )


def test_revoke_flips_state_to_revoked(monkeypatch):
    """Revoke changes state under lock and returns only the safe model."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    mutation_cur = _cur(
        ("ACTIVE", 1, "abcdef", "operator@example.com", ts, "email", None),
        ("REVOKED",),
    )
    result = ic.revoke(
        _conn_with(_cur(_datastream_row(state="active")), mutation_cur),
        credential_id=cred_id,
        datastream_id="ds-1",
        actor="operator@example.com",
        idempotency_key="ik-revoke-1",
        host_context={},
        trace_id=None,
    )
    assert result["state"] == "REVOKED"
    assert "full_secret" not in result and "token_hash" not in result
    assert "token_hash" not in captured["changes"][0].outbox_payload
    assert "FOR UPDATE" in mutation_cur.execute.call_args_list[0].args[0]


def test_revoke_raises_conflict_for_already_terminal_credential(monkeypatch):
    from core import inbound_credentials as ic

    _stub_operation(monkeypatch, ic)
    terminal = _cur(("REVOKED", 1, "abcdef", "operator@example.com", None, "email", None))
    with pytest.raises(ic.InboundCredentialConflict):
        ic.revoke(
            _conn_with(_cur(_datastream_row()), terminal),
            credential_id="dic_01JZAAABBBCCCDDDEEEFFF00001",
            datastream_id="ds-1",
            actor="operator@example.com",
            idempotency_key="ik-revoke-2",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (g) resolve_for_delivery: ACTIVE -> scope; revoked/expired/unknown -> denial.
# ---------------------------------------------------------------------------


def test_resolve_for_delivery_active_token_returns_scope():
    """AC3: resolve_for_delivery returns scope for a valid ACTIVE token."""
    import datetime

    from core import inbound_credentials as ic

    raw = "test-token-value-12345678901234"
    token_hash = _sha256(raw)
    _ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    # Cursor sequence:
    #   1. lookup by token_hash (returns ACTIVE row)
    #   2. verify datastream exists
    cur_lookup = _cur(
        (cred_id, "ds-1", "email", token_hash, "ACTIVE", 1, None, None),
        rowcount=0,
    )
    cur_ds = _cur(("ds-1",), rowcount=0)
    conn = _conn_with(cur_lookup, cur_ds)

    result = ic.resolve_for_delivery(conn, raw_token=raw)

    assert result["allowed"] is True
    assert result["scope"]["datastream_id"] == "ds-1"
    assert result["scope"]["channel"] == "email"
    assert result["scope"]["credential_id"] == cred_id
    gate_sql = cur_lookup.execute.call_args.args[0]
    assert "app.connector_activations" in gate_sql
    assert "a.state" in gate_sql
    assert "a.connector_name = COALESCE(d.config->>'connector_name', d.module_name)" in gate_sql
    assert "d.lifecycle_state" in gate_sql


def test_resolve_for_delivery_denies_inactive_org_connector():
    """Deactivation propagates through token resolution before receipt creation."""

    from core import inbound_credentials as ic

    raw = "inactive-token-value-123456789"
    token_hash = _sha256(raw)
    credential = _cur(
        (
            "dic_01JZAAABBBCCCDDDEEEFFF00001",
            "ds-1",
            "email",
            token_hash,
            "ACTIVE",
            1,
            None,
            None,
            "my_connector",
            "draft",
            ["email"],
            "INACTIVE",
        ),
        rowcount=0,
    )
    result = ic.resolve_for_delivery(
        _conn_with(credential),
        raw_token=raw,
    )
    assert result == {"allowed": False, "scope": None, "reason": "denied"}


def test_resolve_for_delivery_unknown_token_returns_constant_denial():
    """AC4: unknown token returns the constant-shape denial (non-enumerating)."""
    from core import inbound_credentials as ic

    # lookup returns no row
    cur_lookup = _cur(None, rowcount=0)
    conn = _conn_with(cur_lookup)

    result = ic.resolve_for_delivery(conn, raw_token="unknown-token-xyz")

    assert result == {"allowed": False, "scope": None, "reason": "denied"}


def test_resolve_for_delivery_rotating_token_within_overlap_returns_scope():
    """AC3: ROTATING token within overlap_until is still valid."""
    import datetime

    from core import inbound_credentials as ic

    raw = "rotating-token-12345678901234"
    token_hash = _sha256(raw)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    # overlap_until in the FUTURE
    overlap = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)

    cur_lookup = _cur(
        (cred_id, "ds-1", "email", token_hash, "ROTATING", 1, overlap, None),
        rowcount=0,
    )
    cur_ds = _cur(("ds-1",), rowcount=0)
    conn = _conn_with(cur_lookup, cur_ds)

    result = ic.resolve_for_delivery(conn, raw_token=raw)
    assert result["allowed"] is True


def test_resolve_for_delivery_rotating_token_past_overlap_returns_denial():
    """AC3: ROTATING token past overlap_until fails closed."""
    import datetime

    from core import inbound_credentials as ic

    raw = "old-rotating-token-12345678"
    token_hash = _sha256(raw)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    # overlap_until in the PAST
    overlap = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

    cur_lookup = _cur(
        (cred_id, "ds-1", "email", token_hash, "ROTATING", 1, overlap, None),
        rowcount=0,
    )
    conn = _conn_with(cur_lookup)

    result = ic.resolve_for_delivery(conn, raw_token=raw)
    assert result == {"allowed": False, "scope": None, "reason": "denied"}


# ---------------------------------------------------------------------------
# (g') resolve_by_token_hash: sibling that takes the ALREADY-hashed token.
#      Behaviour must be byte-for-byte identical to resolve_for_delivery once
#      the hash is known (same lookup, overlap, expiry, ds-exists, denial shape).
# ---------------------------------------------------------------------------


def test_resolve_by_token_hash_active_returns_scope():
    """resolve_by_token_hash: valid ACTIVE hash -> scope (same as raw path)."""
    from core import inbound_credentials as ic

    token_hash = _sha256("some-raw-token-abcdef123456")
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    cur_lookup = _cur(
        (cred_id, "ds-1", "email", token_hash, "ACTIVE", 1, None, None),
        rowcount=0,
    )
    cur_ds = _cur(("ds-1",), rowcount=0)
    conn = _conn_with(cur_lookup, cur_ds)

    result = ic.resolve_by_token_hash(conn, token_hash=token_hash)

    assert result["allowed"] is True
    assert result["scope"]["datastream_id"] == "ds-1"
    assert result["scope"]["channel"] == "email"
    assert result["scope"]["credential_id"] == cred_id


def test_resolve_by_token_hash_unknown_returns_constant_denial():
    """resolve_by_token_hash: unknown hash -> constant-shape denial (AC4)."""
    from core import inbound_credentials as ic

    cur_lookup = _cur(None, rowcount=0)
    conn = _conn_with(cur_lookup)

    result = ic.resolve_by_token_hash(conn, token_hash=_sha256("nope"))
    assert result == {"allowed": False, "scope": None, "reason": "denied"}


def test_resolve_by_token_hash_empty_returns_denial():
    """resolve_by_token_hash: empty hash -> denial without any DB call."""
    from core import inbound_credentials as ic

    result = ic.resolve_by_token_hash(MagicMock(), token_hash="")
    assert result == {"allowed": False, "scope": None, "reason": "denied"}


def test_resolve_by_token_hash_rotating_past_overlap_returns_denial():
    """resolve_by_token_hash: ROTATING past overlap_until fails closed."""
    import datetime

    from core import inbound_credentials as ic

    token_hash = _sha256("old-rotating-hash")
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"
    overlap = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)

    cur_lookup = _cur(
        (cred_id, "ds-1", "email", token_hash, "ROTATING", 1, overlap, None),
        rowcount=0,
    )
    conn = _conn_with(cur_lookup)

    result = ic.resolve_by_token_hash(conn, token_hash=token_hash)
    assert result == {"allowed": False, "scope": None, "reason": "denied"}


def test_resolve_by_token_hash_matches_raw_path_for_same_token():
    """resolve_by_token_hash(sha256(raw)) equals resolve_for_delivery(raw)."""
    from core import inbound_credentials as ic

    raw = "parity-token-value-0123456789"
    token_hash = _sha256(raw)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    # Raw path cursors.
    raw_conn = _conn_with(
        _cur((cred_id, "ds-1", "email", token_hash, "ACTIVE", 1, None, None), rowcount=0),
        _cur(("ds-1",), rowcount=0),
    )
    # Hash path cursors (identical SQL sequence).
    hash_conn = _conn_with(
        _cur((cred_id, "ds-1", "email", token_hash, "ACTIVE", 1, None, None), rowcount=0),
        _cur(("ds-1",), rowcount=0),
    )

    raw_result = ic.resolve_for_delivery(raw_conn, raw_token=raw)
    hash_result = ic.resolve_by_token_hash(hash_conn, token_hash=token_hash)

    assert raw_result == hash_result


# ---------------------------------------------------------------------------
# (h) denial shape is identical for all failures (non-enumerating, AC4).
# ---------------------------------------------------------------------------


def test_resolve_for_delivery_denial_shape_is_constant():
    """AC4: all denial cases return the same constant shape (non-enumerating)."""
    from core import inbound_credentials as ic

    # Case 1: unknown token
    cur1 = _cur(None, rowcount=0)
    r1 = ic.resolve_for_delivery(_conn_with(cur1), raw_token="unknown-abc")

    # Case 2: empty token
    r2 = ic.resolve_for_delivery(MagicMock(), raw_token="")

    # Case 3: DB error
    bad_conn = MagicMock()
    bad_conn.cursor.side_effect = RuntimeError("DB down")
    r3 = ic.resolve_for_delivery(bad_conn, raw_token="some-token")

    denial = {"allowed": False, "scope": None, "reason": "denied"}
    assert r1 == denial, f"unknown token denial shape mismatch: {r1}"
    assert r2 == denial, f"empty token denial shape mismatch: {r2}"
    assert r3 == denial, f"DB error denial shape mismatch: {r3}"


def test_datastream_status_uses_canonical_connector_binding():
    from core import inbound_credentials as ic

    cur = _cur(("template_connector", "org-1", False, "draft"))
    info = ic._get_datastream_status(_conn_with(cur), datastream_id="ds-1")
    assert info == {
        "connector_name": "template_connector",
        "org_id": "org-1",
        "enabled": False,
        "lifecycle_state": "draft",
        "channels": {"email", "webhook"},
    }
    sql = cur.execute.call_args.args[0]
    assert "COALESCE(config->>'connector_name', module_name)" in sql
    assert "source_kind" not in sql


# ---------------------------------------------------------------------------
# (i) Rate-limit: non-enumerating denial.
# ---------------------------------------------------------------------------


def test_check_rate_limit_raises_for_each_scope(monkeypatch):
    """Any environment/connector/capability exhaustion returns one denial."""
    from core import inbound_credentials as ic

    monkeypatch.setenv("INBOUND_RL_CAPABILITY_MAX_ISSUES_PER_HOUR", "2")
    cur = _cur((0, 0, 2))
    with pytest.raises(ic.InboundCredentialRateLimited):
        ic.check_rate_limit(
            _conn_with(cur),
            environment="test",
            connector_name="my_connector",
            datastream_id="ds-1",
            channel="email",
            operation="issue",
        )
    calls = cur.execute.call_args_list
    assert len(calls) == 4
    assert all("pg_advisory_xact_lock" in call.args[0] for call in calls[:3])
    assert "count_inbound_credential_rate_events" in calls[3].args[0]
    assert "datastream_inbound_credential_rate_events" not in calls[3].args[0]


def test_record_rate_limit_event_uses_bounded_definer_function():
    from core import inbound_credentials as ic

    cur = _cur(None)
    ic._record_rate_limit_event(
        _conn_with(cur),
        operation_id="op-1",
        environment="test",
        connector_name="my_connector",
        datastream_id="ds-1",
        channel="email",
        operation="issue",
    )
    sql, params = cur.execute.call_args.args
    assert "record_inbound_credential_rate_event" in sql
    assert "INSERT INTO app.datastream_inbound_credential_rate_events" not in sql
    assert params[1:] == ("op-1", "test", "my_connector", "ds-1", "email", "issue")


def test_check_rate_limit_passes_when_all_scopes_are_under_limit():
    from core import inbound_credentials as ic

    ic.check_rate_limit(
        _conn_with(_cur((1, 1, 1))),
        environment="test",
        connector_name="my_connector",
        datastream_id="ds-1",
        channel="email",
        operation="issue",
    )


def test_check_rate_limit_rejects_unknown_operation():
    from core import inbound_credentials as ic

    with pytest.raises(ic.InboundCredentialValidationError):
        ic.check_rate_limit(
            MagicMock(),
            environment="test",
            connector_name="my_connector",
            datastream_id="ds-1",
            channel="email",
            operation="delete",
        )


def test_issue_raises_domain_not_ready_when_no_domain_configured(monkeypatch):
    from core import inbound_credentials as ic

    _stub_operation(monkeypatch, ic)
    _patch_rate_limit_pass(monkeypatch)
    monkeypatch.setattr(
        "core.connector_domain.get_domain_config",
        lambda conn, *, environment, connector_name: None,
    )
    with pytest.raises(ic.InboundCredentialDomainNotReady):
        ic.issue(
            _conn_with(_cur(_datastream_row()), _cur(None)),
            datastream_id="ds-1",
            channel="email",
            actor="operator@example.com",
            idempotency_key="ik-nodomain",
            host_context={},
            trace_id=None,
        )


def test_issue_raises_domain_not_ready_when_installation_not_ready(monkeypatch):
    from core import inbound_credentials as ic
    from core.connector_installation_api import ConnectorNotReady

    _stub_operation(monkeypatch, ic)
    _patch_rate_limit_pass(monkeypatch)
    monkeypatch.setattr(
        "core.connector_domain.get_domain_config",
        lambda conn, *, environment, connector_name: _make_domain_cfg(),
    )

    def _raise_not_ready(conn, *, connector_name, environment):
        raise ConnectorNotReady("not ready")

    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        _raise_not_ready,
    )
    with pytest.raises(ic.InboundCredentialDomainNotReady):
        ic.issue(
            _conn_with(_cur(_datastream_row()), _cur(None)),
            datastream_id="ds-1",
            channel="email",
            actor="operator@example.com",
            idempotency_key="ik-notready",
            host_context={},
            trace_id=None,
        )


def test_issue_request_payload_is_deterministic_and_secret_free(monkeypatch):
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    result = ic.issue(
        _conn_with(_cur(_datastream_row()), _cur(None), _cur((ts, None)), _cur(None)),
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-deterministic",
        host_context={},
        trace_id=None,
    )
    payload = captured["specs"][0].request_payload
    assert result["full_secret"] not in json.dumps(payload)
    assert "token_hash" not in payload
    assert not any(str(value).startswith("dic_") for value in payload.values())
    assert payload == {
        "datastream_id": "ds-1",
        "channel": "webhook",
        "issued_by": "operator@example.com",
        "expires_seconds": None,
    }


def test_issue_outbox_payload_has_no_secret_fields(monkeypatch):
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    ts = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    result = ic.issue(
        _conn_with(_cur(_datastream_row()), _cur(None), _cur((ts, None)), _cur(None)),
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-outbox",
        host_context={},
        trace_id=None,
    )
    outbox = captured["changes"][0].outbox_payload
    assert "token_hash" not in outbox
    assert result["full_secret"] not in json.dumps(outbox)


def test_issue_persists_requested_expiration(monkeypatch):
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    created = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    expires = created + datetime.timedelta(hours=1)
    cur_insert = _cur((created, expires))
    result = ic.issue(
        _conn_with(_cur(_datastream_row()), _cur(None), cur_insert, _cur(None)),
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="ik-expiry",
        host_context={},
        trace_id=None,
        expires_seconds=3600,
    )
    params = cur_insert.execute.call_args.args[1]
    assert params[6:8] == (3600, 3600)
    assert result["expires_at"] == expires.isoformat()
    assert result["state"] == "ACTIVE"
    assert captured["specs"][0].request_payload["expires_seconds"] == 3600


def test_safe_read_model_reflects_expired_boundary():
    import datetime

    from core import inbound_credentials as ic

    past = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=1)
    model = ic._safe_read_model(
        credential_id="dic_01JZAAABBBCCCDDDEEEFFF00001",
        datastream_id="ds-1",
        channel="email",
        safe_suffix="abcdef",
        state="ACTIVE",
        version=1,
        expires_at=past,
        overlap_until=None,
        issued_by="operator",
        created_at=past,
    )
    assert model["state"] == "EXPIRED"


def test_issue_rejects_paused_or_archived_datastream_inside_fresh_mutation(monkeypatch):
    from core import inbound_credentials as ic

    _stub_operation(monkeypatch, ic)
    for lifecycle_state in ("paused", "archived"):
        conn = _conn_with(_cur(_datastream_row(state=lifecycle_state)))
        conn._locked_datastream_info = {
            **conn._locked_datastream_info,
            "lifecycle_state": lifecycle_state,
        }
        with pytest.raises(ic.InboundCredentialUnavailable):
            ic.issue(
                conn,
                datastream_id="ds-1",
                channel="email",
                actor="operator@example.com",
                idempotency_key=f"ik-{lifecycle_state}",
                host_context={},
                trace_id=None,
            )


# ---------------------------------------------------------------------------
# MCP credential parity.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=False)
def _clean_mcp_registry(monkeypatch):
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


def test_mcp_get_inbound_credential_status_registered(_clean_mcp_registry):
    """Task 4: get_inbound_credential_status must be registered under operations."""
    from core import mcp_profiles, operations_mcp

    class _Recorder:
        def __init__(self):
            self.handlers = {}
            self.tool = MagicMock()

        def capture(self, h):
            self.handlers[h.__name__] = h
            return h

    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.capture(handler)
        return real(mcp, handler, **kwargs)

    recorder = _Recorder()
    mcp_profiles.register_profiled = spy  # type: ignore[assignment]
    try:
        operations_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real  # type: ignore[assignment]

    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert "get_inbound_credential_status" in decls, (
        "get_inbound_credential_status must be registered under operations profile"
    )
    d = decls["get_inbound_credential_status"]
    assert d.profile == "operations"
    assert d.effect == "read"
    assert d.confirmation_mode == "none"


def test_mcp_get_inbound_credential_status_no_secret_in_result(monkeypatch, _clean_mcp_registry):
    """Task 4/AC2: MCP tool must never return full_secret or token_hash."""
    import datetime

    # Patch get_credential_state to return a safe model.
    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    safe_model = {
        "credential_id": "dic_01JZAAABBBCCCDDDEEEFFF00001",
        "datastream_id": "ds-1",
        "channel": "email",
        "safe_suffix": "abcdef",
        "state": "ACTIVE",
        "version": 1,
        "expires_at": None,
        "overlap_until": None,
        "issued_by": "operator@example.com",
        "created_at": ts.isoformat(),
    }

    monkeypatch.setattr(
        "core.inbound_credentials.get_credential_state",
        lambda conn, *, credential_id, datastream_id: safe_model,
    )
    action_conn = MagicMock()
    monkeypatch.setattr(
        "core.db.get_connection",
        lambda: __import__("contextlib").nullcontext(action_conn),
    )

    from core import operations_mcp

    monkeypatch.setenv("TOOROW_AUTH_MODE", "strict")
    monkeypatch.setattr(operations_mcp, "_identity", lambda: "operator@example.com")
    access_context_calls = []
    # Story 21.6: the context is installed by the ACQUISITION seam, so that is
    # what this test observes. The property is unchanged -- the connection the
    # tool reads on is armed for the caller before the guard runs.
    monkeypatch.setattr(
        "core.db.install_access_context",
        lambda conn, identity: access_context_calls.append((conn, identity)),
    )
    guard_calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        operations_mcp,
        "_guard_datastream",
        lambda datastream_id, identity, *, minimum_capability, **kwargs: guard_calls.append(
            (datastream_id, identity, minimum_capability, kwargs.get("conn"))
        ),
    )

    # Call the MCP tool directly by extracting its handler from the registry.
    class _Recorder:
        def __init__(self):
            self.handlers = {}
            self.tool = MagicMock()

        def capture(self, h):
            self.handlers[h.__name__] = h
            return h

    from core import mcp_profiles

    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.capture(handler)
        return real(mcp, handler, **kwargs)

    recorder = _Recorder()
    mcp_profiles.register_profiled = spy  # type: ignore[assignment]
    try:
        operations_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real  # type: ignore[assignment]

    handler = recorder.handlers["get_inbound_credential_status"]
    tool_result = handler("ds-1", "dic_01JZAAABBBCCCDDDEEEFFF00001")

    # Serialize the result and verify no secret appears.
    result_json = json.dumps(tool_result, default=str)
    assert "full_secret" not in result_json, "MCP result must NOT contain full_secret"
    assert "token_hash" not in result_json, "MCP result must NOT contain token_hash"
    assert access_context_calls == [(action_conn, "operator@example.com")]
    assert len(guard_calls) == 1
    assert guard_calls[0][:3] == ("ds-1", "operator@example.com", "view")
    assert guard_calls[0][3] is action_conn


# ---------------------------------------------------------------------------
# (q) ASGI: POST issue returns 200 with full_secret; GET list returns no secret.
# (r) ASGI: revoke after issue returns 200.
# (s) ASGI: missing Idempotency-Key -> 422.
# ---------------------------------------------------------------------------


def test_rest_access_context_is_installed_for_authenticated_connections():
    """Story 21.6: this module no longer carries its own copy of the rule.

    It used to define a private `_set_local_access_context` -- one of three
    byte-identical copies across `inbound_credentials_api`,
    `datastream_first_candidate_mcp` and `operations_mcp`, each with its own
    auth-disabled carve-out. Three copies of an access rule is three places for
    it to drift, and the codebase already disagreed with itself: the fourth
    copy, in `query_specs_api`, carved out only `anonymous` rather than every
    auth-disabled caller. The rule now lives once, in `core.db`, and this module
    reaches it by opening its connections through the seam.

    Asserted structurally rather than by patching, because the defect being
    prevented is a *reintroduced private copy* -- something a behavioural test
    on the seam cannot see.
    """
    import inspect

    from core import inbound_credentials_api as api

    assert not hasattr(api, "_set_local_access_context"), (
        "a private access-context helper came back; the rule belongs in core.db"
    )
    source = inspect.getsource(api)
    assert "set_local_access_context" not in source, (
        "this module arms the floor by hand again instead of acquiring an armed "
        "connection through core.db.request_connection"
    )
    assert "request_connection" in source


def test_api_access_guard_reuses_action_connection(monkeypatch):
    from types import SimpleNamespace

    from core import inbound_credentials_api as api

    action_conn = MagicMock()
    seen: list[object] = []
    monkeypatch.setenv("TOOROW_AUTH_MODE", "strict")
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda identity, conn, **kwargs: (seen.append(conn) or SimpleNamespace(allowed=True)),
    )
    monkeypatch.setattr(
        "core.db.get_connection",
        lambda: (_ for _ in ()).throw(AssertionError("opened a second connection")),
    )
    assert (
        api._check_datastream_access(
            "ds-1", "operator@example.com", minimum_capability="edit", conn=action_conn
        )
        is True
    )
    assert seen == [action_conn]


@pytest.fixture
def asgi_client(monkeypatch):
    """Return an ASGI test client for the credential routes."""
    import datetime

    from core.inbound_credentials_api import INBOUND_CREDENTIAL_ROUTES
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    # Disable auth for tests.
    monkeypatch.setenv("TOOROW_AUTH_MODE", "disabled")

    # Stub authentication.
    async def _auth(req):
        return True, "anonymous"

    monkeypatch.setattr("core.inbound_credentials_api._check_auth", _auth)
    # Always grant access.
    monkeypatch.setattr(
        "core.inbound_credentials_api._check_datastream_access",
        lambda ds_id, identity, **kwargs: True,
    )

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    # Stub issue with the domain layer's show-once replay shape.
    seen_issue_keys: set[str] = set()

    def _stub_issue(
        conn, *, datastream_id, channel, actor, idempotency_key, host_context, trace_id, **kwargs
    ):
        result = {
            "credential_id": "dic_01JZAAABBBCCCDDDEEEFFF00001",
            "datastream_id": datastream_id,
            "channel": channel,
            "safe_suffix": "abcdef",
            "state": "ACTIVE",
            "version": 1,
            "expires_at": None,
            "overlap_until": None,
            "issued_by": actor,
            "created_at": ts.isoformat(),
            "secret_available": idempotency_key not in seen_issue_keys,
        }
        if idempotency_key not in seen_issue_keys:
            seen_issue_keys.add(idempotency_key)
            result["full_secret"] = "ds_SECRETTOKEN@inbound.example.com"
        return result

    monkeypatch.setattr("core.inbound_credentials.issue", _stub_issue)
    monkeypatch.setattr(
        "core.inbound_credentials.datastream_matches_connector",
        lambda conn, *, datastream_id, connector_name: True,
    )

    # Stub list_credentials.
    def _stub_list(conn, *, datastream_id, include_terminal=False):
        return [
            {
                "credential_id": "dic_01JZAAABBBCCCDDDEEEFFF00001",
                "datastream_id": datastream_id,
                "channel": "email",
                "safe_suffix": "abcdef",
                "state": "ACTIVE",
                "version": 1,
                "expires_at": None,
                "overlap_until": None,
                "issued_by": "anonymous",
                "created_at": ts.isoformat(),
            }
        ]

    monkeypatch.setattr("core.inbound_credentials.list_credentials", _stub_list)

    # Stub revoke.
    def _stub_revoke(conn, **kwargs):
        return {
            "credential_id": kwargs.get("credential_id", "dic_x"),
            "datastream_id": kwargs.get("datastream_id", "ds-1"),
            "channel": "email",
            "safe_suffix": "abcdef",
            "state": "REVOKED",
            "version": 1,
            "expires_at": None,
            "overlap_until": None,
            "issued_by": "anonymous",
            "created_at": ts.isoformat(),
        }

    monkeypatch.setattr("core.inbound_credentials.revoke", _stub_revoke)

    # Stub DB connection.
    import contextlib

    conn_ctx = contextlib.nullcontext(MagicMock())
    monkeypatch.setattr("core.db.get_connection", lambda: conn_ctx)

    app = Starlette(routes=INBOUND_CREDENTIAL_ROUTES)
    return TestClient(app, raise_server_exceptions=False)


def test_asgi_strict_auth_installs_rls_context_before_guard_and_write(asgi_client, monkeypatch):
    from core import inbound_credentials as ic
    from core import inbound_credentials_api as api

    events = []
    original_scope = ic.datastream_matches_connector
    original_issue = ic.issue
    monkeypatch.setenv("TOOROW_AUTH_MODE", "strict")
    monkeypatch.setattr(
        "core.db.install_access_context",
        lambda conn, identity: events.append(("context", conn, identity)),
    )
    monkeypatch.setattr(
        api,
        "_check_datastream_access",
        lambda datastream_id, identity, **kwargs: (
            events.append(("guard", kwargs["conn"], identity)) or True
        ),
    )
    monkeypatch.setattr(
        ic,
        "datastream_matches_connector",
        lambda conn, **kwargs: (
            events.append(("scope", conn, kwargs["datastream_id"]))
            or original_scope(conn, **kwargs)
        ),
    )
    monkeypatch.setattr(
        ic,
        "issue",
        lambda conn, **kwargs: (
            events.append(("write", conn, kwargs["datastream_id"]))
            or original_issue(conn, **kwargs)
        ),
    )

    response = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        json={"channel": "email"},
        headers={"Idempotency-Key": "strict-context-order"},
    )
    assert response.status_code == 200
    assert [event[0] for event in events] == ["context", "guard", "scope", "write"]
    action_conn = events[0][1]
    assert all(event[1] is action_conn for event in events)
    assert events[0][2] == "anonymous"


def test_asgi_post_issue_returns_full_secret_once(asgi_client):
    """AC2/ASGI: POST /credentials returns full_secret in response (show-once)."""
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        json={"channel": "email"},
        headers={"Idempotency-Key": "ik-asgi-issue-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "full_secret" in body, "POST /credentials must return full_secret ONCE"
    assert body["state"] == "ACTIVE"
    replay = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        json={"channel": "email"},
        headers={"Idempotency-Key": "ik-asgi-issue-1"},
    )
    assert replay.status_code == 200
    assert replay.json()["secret_available"] is False
    assert "full_secret" not in replay.json()


def test_asgi_get_list_returns_no_secret(asgi_client):
    """AC2/ASGI: GET /credentials never returns full_secret or token_hash."""
    resp = asgi_client.get(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
    )
    assert resp.status_code == 200
    body = resp.json()
    body_str = json.dumps(body)
    assert "full_secret" not in body_str, "GET /credentials must NOT contain full_secret"
    assert "token_hash" not in body_str, "GET /credentials must NOT contain token_hash"


def test_asgi_post_revoke_returns_revoked_state(asgi_client):
    """AC3/ASGI (r): POST /credentials/{id}/revoke -> 200 REVOKED, no secret in body."""
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials/"
        "dic_01JZAAABBBCCCDDDEEEFFF00001/revoke",
        headers={"Idempotency-Key": "ik-revoke-asgi"},
        json={"project_id": "proj-1"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "REVOKED"
    assert "full_secret" not in body
    assert "token_hash" not in body


def test_asgi_post_issue_missing_idempotency_key_returns_422(asgi_client):
    """ASGI: missing Idempotency-Key header -> 422."""
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        json={"channel": "email"},
        # No Idempotency-Key header.
    )
    assert resp.status_code == 422


def test_asgi_post_issue_missing_channel_returns_400(asgi_client):
    """ASGI: missing channel in body -> 400."""
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        json={},
        headers={"Idempotency-Key": "ik-nochannel"},
    )
    assert resp.status_code == 400


def test_asgi_conflicting_idempotency_key_returns_409(asgi_client, monkeypatch):
    from core.operations import OperationIdempotencyConflict

    def _conflict(*args, **kwargs):
        raise OperationIdempotencyConflict("bound to another request")

    monkeypatch.setattr("core.inbound_credentials.issue", _conflict)
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        json={"channel": "email", "expires_seconds": 3600},
        headers={"Idempotency-Key": "bound-key"},
    )
    assert resp.status_code == 409
    assert resp.json()["code"] == "conflict"


def test_asgi_rejects_non_object_json(asgi_client):
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials",
        content='["email"]',
        headers={"Content-Type": "application/json", "Idempotency-Key": "ik-array"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_body"


def test_asgi_rotate_rejects_string_boolean(asgi_client):
    resp = asgi_client.post(
        "/api/connectors/my_connector/datastreams/ds-1/credentials/cred-1/rotate",
        json={"immediate_revoke": "false"},
        headers={"Idempotency-Key": "ik-bool"},
    )
    assert resp.status_code == 400
    assert resp.json()["code"] == "invalid_body"


def test_asgi_connector_scope_mismatch_is_nondisclosing(asgi_client, monkeypatch):
    monkeypatch.setattr(
        "core.inbound_credentials.datastream_matches_connector",
        lambda conn, *, datastream_id, connector_name: False,
    )
    monkeypatch.setattr("core.audit.write_audit_row", lambda **kwargs: None)
    resp = asgi_client.get("/api/connectors/wrong_connector/datastreams/ds-1/credentials")
    assert resp.status_code == 404
    assert resp.json() == {"code": "not_found", "message": "Resource not found"}


def test_corrective_migration_pins_lifecycle_and_rate_limit_invariants():
    """Offline contract proof for the additive DB guard; no live DB required."""
    from pathlib import Path

    sql = Path(
        REPO_ROOT / "infra/nango/migrations/183_inbound_credential_lifecycle_guard.sql"
    ).read_text(
        encoding="utf-8"
    )
    required = (
        "datastream_inbound_credential_rate_events",
        "fk_dic_datastream",
        "ck_dic_token_hash",
        "ck_dic_operation_required",
        "ck_dic_lifecycle_shape",
        "uq_dic_token_hash",
        "uq_dic_version_per_datastream_channel",
        "uq_dic_rotating_per_datastream_channel",
        "illegal inbound credential state transition provenance",
        "terminal inbound credential rows are immutable",
        "credential operation provenance is invalid",
        "credential version must be the next historical version",
        "COALESCE(MAX(c.version), 0) + 1",
        "count_inbound_credential_rate_events",
        "record_inbound_credential_rate_event",
        "SECURITY DEFINER",
        "SET row_security = off",
        "REVOKE ALL ON TABLE app.datastream_inbound_credential_rate_events",
        "GRANT EXECUTE ON FUNCTION app.count_inbound_credential_rate_events",
    )
    for fragment in required:
        assert fragment in sql


def test_issue_reuses_monotonic_version_after_terminal_history(monkeypatch):
    import datetime

    from core import inbound_credentials as ic

    captured = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)
    created = datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc)
    history = _cur(fetchall=[("old", "REVOKED", 7)])
    result = ic.issue(
        _conn_with(_cur(_datastream_row()), history, _cur((created, None)), _cur(None)),
        datastream_id="ds-1",
        channel="email",
        actor="operator",
        idempotency_key="reissue-after-revoke",
        host_context={},
        trace_id=None,
    )
    assert result["version"] == 8
    assert captured["changes"][0].outbox_payload["version"] == 8


def test_fresh_issue_revalidates_configured_channel_under_lock(monkeypatch):
    from core import inbound_credentials as ic

    _stub_operation(monkeypatch, ic)
    conn = _conn_with(_cur(_datastream_row()))
    conn._locked_datastream_info = {
        **conn._locked_datastream_info,
        "channels": {"webhook"},
    }
    with pytest.raises(ic.InboundCredentialUnavailable, match="channel"):
        ic.issue(
            conn,
            datastream_id="ds-1",
            channel="email",
            actor="operator",
            idempotency_key="disabled-channel",
            host_context={},
            trace_id=None,
        )


def test_unknown_resolution_uses_three_scope_throttle_without_hash_evidence(monkeypatch):
    from core import inbound_credentials as ic

    enforced = []
    recorded = []
    monkeypatch.setattr(
        ic, "_enforce_resolution_rate_limit", lambda conn, **kwargs: enforced.append(kwargs)
    )
    monkeypatch.setattr(
        ic, "_record_resolution_rate_event", lambda conn, **kwargs: recorded.append(kwargs)
    )
    denial = ic.resolve_for_delivery(_conn_with(_cur(None)), raw_token="unknown-secret")
    assert denial == {"allowed": False, "scope": None, "reason": "denied"}
    assert enforced == [
        {
            "environment": "production",
            "connector_name": "__unknown__",
            "datastream_id": None,
            "channel": "__unknown__",
        }
    ]
    assert recorded[0]["datastream_id"] is None
    assert "unknown-secret" not in json.dumps(recorded)


def test_due_expiration_is_an_audited_operation(
    monkeypatch, _isolate_time_and_resolution_side_effects
):
    import datetime

    from core import inbound_credentials as ic
    from core import operations

    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    due = (
        "dic_01JZAAABBBCCCDDDEEEFFF00001",
        "ds-1",
        "email",
        "ACTIVE",
        3,
        "abcdef",
        "operator",
        past,
        past,
        None,
        "org-1",
    )
    seen = {}

    def execute(conn, spec, *, mutation):
        seen["spec"] = spec
        changed = mutation(conn, "op-expire")
        seen["change"] = changed
        return operations.OperationResult(
            "op-expire", "succeeded", changed.result, "audit", "outbox", False
        )

    monkeypatch.setattr(ic, "execute_operation", execute)
    conn = _conn_with(_cur(fetchall=[due]), _cur((past,)))
    _isolate_time_and_resolution_side_effects(conn, credential_id=due[0])
    assert seen["spec"].command_type == "inbound.credential.expired"
    assert seen["change"].outbox_payload["state"] == "EXPIRED"


def test_due_expiration_fails_closed_when_locked_update_changes_nothing(
    monkeypatch, _isolate_time_and_resolution_side_effects
):
    import datetime

    from core import inbound_credentials as ic

    past = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)
    due = (
        "dic_01JZAAABBBCCCDDDEEEFFF00001",
        "ds-1",
        "email",
        "ACTIVE",
        3,
        "abcdef",
        "operator",
        past,
        past,
        None,
        "org-1",
    )

    def execute(conn, spec, *, mutation):
        return mutation(conn, "op-expire")

    monkeypatch.setattr(ic, "execute_operation", execute)
    conn = _conn_with(_cur(fetchall=[due]), _cur(None))
    with pytest.raises(ic.InboundCredentialConflict, match="lost its locked"):
        _isolate_time_and_resolution_side_effects(conn, credential_id=due[0])


def test_rest_access_guard_requests_hold_access(monkeypatch):
    from types import SimpleNamespace

    from core import inbound_credentials_api as api

    calls = []
    monkeypatch.setenv("TOOROW_AUTH_MODE", "strict")
    monkeypatch.setattr(
        "core.project_access.resolve_strict_resource_access",
        lambda identity, conn, **kwargs: (calls.append(kwargs) or SimpleNamespace(allowed=True)),
    )
    assert api._check_datastream_access("ds-1", "operator", conn=MagicMock()) is True
    assert calls == [
        {
            "datastream_id": "ds-1",
            "minimum_capability": "edit",
            "hold_access": True,
        }
    ]


def test_corrective_migration_forces_rls_and_exact_provenance():
    from pathlib import Path

    sql = Path(
        REPO_ROOT / "infra/nango/migrations/183_inbound_credential_lifecycle_guard.sql"
    ).read_text(
        encoding="utf-8"
    )
    for fragment in (
        "FORCE ROW LEVEL SECURITY",
        "inbound_credentials_strict",
        "inbound_credential_rate_events_read",
        "system:migration-183",
        "INSERT INTO app.audit_log",
        "INSERT INTO app.operation_outbox",
        "lifecycle boundary mutation requires a new operation",
        "illegal inbound credential state transition provenance",
        "operation_resource @> jsonb_build_array",
        "row_number() OVER",
        "credential version must be the next historical version",
        "REVOKE ALL ON TABLE app.datastream_inbound_credential_rate_events",
        "SET row_security = off",
    ):
        assert fragment in sql


# ---------------------------------------------------------------------------
# Live-PG-gated tests: partial-unique, immutability trigger, no-raw-token-column.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_partial_unique_prevents_two_active_credentials(
    pg_conn, inbound_pg_scope, insert_operation
):
    """Live-PG: partial-unique index allows at most one ACTIVE/ROTATING per (ds, ch).

    Les empreintes de jeton sont DERIVEES de l'identifiant du credential, jamais
    ecrites en dur. Ce test committe, `uq_dic_token_hash` est global, et la table
    est append-only (le trigger de la 183 refuse DELETE) : un `"a" * 64` fige ne
    passe donc **qu'une seule fois par base**, puis rend `UniqueViolation` a tout
    jamais. Mesure 2026-08-04, sur le premier lancement ou ces trois preuves
    `live_pg` se sont reellement executees -- elles etaient rouges depuis
    toujours sur un `jsonb_build_array(%s)` non type, donc personne n'avait
    encore pu voir la seconde marche.
    """
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        ds_id = inbound_pg_scope["datastream_id"]
        op_id = insert_operation("inbound.credential.issued")
        cur.execute(
            "UPDATE app.operations SET resource_path = jsonb_build_array(%s::text) WHERE id = %s",
            (f"datastream:{ds_id}", op_id),
        )
        cred1_id = f"dic_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.datastream_inbound_credentials "
            "(id, datastream_id, channel, token_hash, safe_suffix, "
            "state, version, issued_by, operation_id) "
            "VALUES (%s, %s, 'email', %s, 'aabbcc', 'ACTIVE', 1, 'test', %s)",
            (cred1_id, ds_id, _sha256(cred1_id), op_id),
        )
        pg_conn.commit()

        # Second ACTIVE insert for same (ds_id, 'email') must fail.
        op_id2 = insert_operation("inbound.credential.issued")
        cur.execute(
            "UPDATE app.operations SET resource_path = jsonb_build_array(%s::text) WHERE id = %s",
            (f"datastream:{ds_id}", op_id2),
        )
        cred2_id = f"dic_{_ulid.ULID()}"
        try:
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, "
                "state, version, issued_by, operation_id) "
                "VALUES (%s, %s, 'email', %s, 'ccddee', 'ACTIVE', 2, 'test', %s)",
                (cred2_id, ds_id, _sha256(cred2_id), op_id2),
            )
            pg_conn.commit()
            pytest.fail("Expected unique constraint violation for second ACTIVE credential")
        except Exception as exc:
            pg_conn.rollback()
            assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower(), (
                f"Expected unique constraint violation, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_immutability_trigger_blocks_token_hash_update(
    pg_conn, inbound_pg_scope, insert_operation
):
    """Live-PG: protect_inbound_credential blocks updating token_hash."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        ds_id = inbound_pg_scope["datastream_id"]
        op_id = insert_operation("inbound.credential.issued")
        cur.execute(
            "UPDATE app.operations SET resource_path = jsonb_build_array(%s::text) WHERE id = %s",
            (f"datastream:{ds_id}", op_id),
        )
        cred_id = f"dic_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.datastream_inbound_credentials "
            "(id, datastream_id, channel, token_hash, safe_suffix, "
            "state, version, issued_by, operation_id) "
            "VALUES (%s, %s, 'webhook', %s, 'aabbcc', 'ACTIVE', 1, 'test', %s)",
            (cred_id, ds_id, _sha256(cred_id), op_id),
        )
        pg_conn.commit()

        # Attempt to mutate token_hash (must be blocked by trigger).
        try:
            cur.execute(
                "UPDATE app.datastream_inbound_credentials SET token_hash = %s WHERE id = %s",
                (_sha256(f"{cred_id}:rotated"), cred_id),
            )
            pg_conn.commit()
            pytest.fail("Expected immutability trigger to block token_hash update")
        except Exception as exc:
            pg_conn.rollback()
            assert "immutable" in str(exc).lower() or "token_hash" in str(exc).lower(), (
                f"Expected immutability exception, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_force_rls_hides_cross_tenant_credential(
    pg_conn, inbound_pg_scope, insert_operation
):
    """Live-PG probe written for the final module-complete wave; not run here."""
    import ulid as _ulid

    ds_id = inbound_pg_scope["datastream_id"]
    op_id = insert_operation("inbound.credential.issued")
    cred_id = f"dic_{_ulid.ULID()}"
    try:
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.operations SET resource_path = jsonb_build_array(%s::text) "
                "WHERE id = %s",
                (f"datastream:{ds_id}", op_id),
            )
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, "
                "state, version, issued_by, operation_id) "
                "VALUES (%s, %s, 'email', %s, 'aabbcc', 'ACTIVE', 1, 'test', %s)",
                (cred_id, ds_id, _sha256(cred_id), op_id),
            )
            cur.execute("SELECT set_config('toorow.identity', 'foreign-user', true)")
            cur.execute("SELECT set_config('toorow.enforce_epic36', 'on', true)")
            cur.execute(
                "SELECT count(*) FROM app.datastream_inbound_credentials WHERE id = %s",
                (cred_id,),
            )
            assert cur.fetchone()[0] == 0
    finally:
        pg_conn.rollback()


@pytest.mark.live_pg
def test_live_pg_no_raw_token_column_in_schema(pg_conn):
    """Live-PG: the table must have NO column that could hold a raw token."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = 'app' "
            "AND table_name = 'datastream_inbound_credentials' "
            "ORDER BY ordinal_position",
        )
        columns = [row[0] for row in cur.fetchall()]

    # The raw token must not have a column; only the hash and suffix are allowed.
    forbidden_col_fragments = ["raw_token", "token_value", "secret_value", "plaintext"]
    for col in columns:
        for frag in forbidden_col_fragments:
            assert frag not in col.lower(), (
                f"SCHEMA VIOLATION: column {col!r} in datastream_inbound_credentials "
                f"looks like a raw-token column (fragment {frag!r}). "
                f"Only token_hash and safe_suffix are allowed."
            )
    # Confirm expected columns exist.
    assert "token_hash" in columns
    assert "safe_suffix" in columns
    # Confirm no column named exactly 'token' or 'raw_token'.
    assert "token" not in columns
    assert "raw_token" not in columns
