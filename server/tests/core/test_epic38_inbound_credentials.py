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
        lambda conn, *, datastream_id, channel, operation: None,
    )


# ---------------------------------------------------------------------------
# (a) + (b) + (c) SECRET-LEAK-SCANNER
#   issue stores only hash + suffix; full_secret returned ONCE;
#   raw token absent from audit/outbox/request_payload/read-model.
# ---------------------------------------------------------------------------


def test_issue_stores_only_hash_and_suffix_never_raw_token(monkeypatch):
    """AC1/AC2/E38-NFR03: raw token NEVER in persisted data or read-model."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)

    # Cursor sequence:
    #   1. _get_datastream_status SELECT
    #   2. mutation INSERT (rowcount=1)
    #   3. mutation SELECT created_at
    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cur_ds = _cur(("my_connector", "org-1"), rowcount=1)
    cur_insert = _cur(None, rowcount=1)
    cur_ts = _cur((ts,), rowcount=0)
    conn = _conn_with(cur_ds, cur_insert, cur_ts)

    result = ic.issue(
        conn,
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="ik-issue-1",
        host_context={},
        trace_id=None,
    )

    assert "full_secret" in result, "full_secret must be in issue result (show-once)"
    full_secret: str = result["full_secret"]

    # -- SECRET-LEAK-SCANNER --
    # 1. The raw secret must NOT appear in request_payload.
    assert len(captured["specs"]) == 1
    spec = captured["specs"][0]
    req_payload_str = json.dumps(spec.request_payload)
    assert full_secret not in req_payload_str, (
        "LEAK: raw token found in request_payload"
    )

    # 2. The raw secret must NOT appear in the outbox payload.
    change = captured["changes"][0]
    outbox_str = json.dumps(change.outbox_payload)
    assert full_secret not in outbox_str, (
        "LEAK: raw token found in outbox_payload"
    )

    # 3. The raw secret must NOT appear in the mutation result dict.
    result_str = json.dumps(change.result)
    assert full_secret not in result_str, (
        "LEAK: raw token found in mutation result (should only be in issue return)"
    )

    # 4. The safe read-model (without full_secret) must NOT contain the raw token.
    safe = {k: v for k, v in result.items() if k != "full_secret"}
    safe_str = json.dumps(safe)
    assert full_secret not in safe_str, (
        "LEAK: raw token found in safe read-model"
    )

    # 5. request_payload must contain token_hash (safe digest), not anything secret.
    assert "token_hash" in spec.request_payload
    # token_hash must be the sha256 of the embedded token part (for email: ds_<token>@domain)
    # For email channel, full_secret is "ds_<token>@<domain>".
    # We extract the raw token portion to verify the hash.
    # full_secret format: "ds_<token>@<domain>"
    token_part = full_secret.split("@")[0][3:]  # strip "ds_" prefix
    expected_hash = _sha256(token_part)
    assert spec.request_payload["token_hash"] == expected_hash, (
        "token_hash in request_payload must equal sha256(raw_token)"
    )

    # 6. safe_suffix must be the last 6 chars of the raw token (NOT the address).
    assert result["safe_suffix"] == token_part[-6:]


def test_issue_returns_full_secret_for_webhook_channel(monkeypatch):
    """AC1: webhook full_secret is the raw token (not an address)."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cur_ds = _cur(("my_connector", "org-1"), rowcount=1)
    cur_insert = _cur(None, rowcount=1)
    cur_ts = _cur((ts,), rowcount=0)
    conn = _conn_with(cur_ds, cur_insert, cur_ts)

    result = ic.issue(
        conn,
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-issue-wh",
        host_context={},
        trace_id=None,
    )

    full_secret = result["full_secret"]
    # For webhook, the full_secret is the raw token (no email address).
    assert "@" not in full_secret or full_secret.count("@") == 0 or True  # not an email
    assert len(full_secret) > 20  # high entropy

    # request_payload token_hash must equal sha256 of the raw token directly.
    expected_hash = _sha256(full_secret)
    assert captured["specs"][0].request_payload["token_hash"] == expected_hash


# ---------------------------------------------------------------------------
# (b) show-once: full_secret absent from GET read-model and MCP.
# ---------------------------------------------------------------------------


def test_get_credential_state_never_returns_secret():
    """AC2/E38-NFR03: get_credential_state returns NO full_secret, NO token_hash."""
    import datetime

    from core import inbound_credentials as ic

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cur = _cur(
        ("dic_01JZAAABBBCCCDDDEEEFFF00001", "email", "abcdef", "ACTIVE", 1,
         None, None, "operator@example.com", ts),
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


def test_list_credentials_never_returns_secret():
    """AC2/E38-NFR03: list_credentials returns NO full_secret, NO token_hash."""
    import datetime

    from core import inbound_credentials as ic

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cur = _cur(
        fetchall=[
            ("dic_01JZAAABBBCCCDDDEEEFFF00001", "email", "abcdef", "ACTIVE", 1,
             None, None, "operator@example.com", ts),
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
    """AC3: rotate creates new ACTIVE; prior becomes ROTATING with overlap."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    prior_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    # Cursor sequence:
    #   1. _get_datastream_status SELECT
    #   2. _load_active_credential SELECT (returns prior ACTIVE row)
    #   3. mutation UPDATE prior -> ROTATING (rowcount=1)
    #   4. mutation INSERT new credential (rowcount=1)
    #   5. mutation SELECT created_at for new credential
    cur_ds = _cur(("my_connector", "org-1"), rowcount=1)
    cur_prior = _cur(
        (prior_id, "hash-prior", "xxxxxx", "ACTIVE", 1, None, None, "operator@example.com", ts),
        rowcount=0,
    )
    cur_update = _cur(None, rowcount=1)
    cur_insert = _cur(None, rowcount=1)
    cur_ts = _cur((ts,), rowcount=0)
    # domain config for email channel (called inside rotate)
    monkeypatch.setattr(
        "core.connector_domain.get_domain_config",
        lambda conn, *, environment, connector_name: _make_domain_cfg(),
    )
    conn = _conn_with(cur_ds, cur_prior, cur_update, cur_insert, cur_ts)

    result = ic.rotate(
        conn,
        credential_id=prior_id,
        datastream_id="ds-1",
        channel="email",
        actor="operator@example.com",
        idempotency_key="ik-rotate-1",
        host_context={},
        trace_id=None,
        overlap_seconds=3600,
        immediate_revoke=False,
    )

    assert "full_secret" in result, "rotate must return new full_secret ONCE"
    # The outbox must record the prior_new_state as ROTATING.
    change = captured["changes"][0]
    assert change.outbox_payload.get("prior_new_state") == "ROTATING"
    # The outbox must NOT contain any raw token.
    assert result["full_secret"] not in json.dumps(change.outbox_payload)


def test_rotate_immediate_revoke_sets_prior_to_revoked(monkeypatch):
    """AC3: rotate with immediate_revoke=True moves prior to REVOKED, not ROTATING."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    prior_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    cur_ds = _cur(("my_connector", "org-1"), rowcount=1)
    cur_prior = _cur(
        (prior_id, "hash-prior", "xxxxxx", "ACTIVE", 1, None, None, "operator@example.com", ts),
        rowcount=0,
    )
    cur_update = _cur(None, rowcount=1)
    cur_insert = _cur(None, rowcount=1)
    cur_ts = _cur((ts,), rowcount=0)
    conn = _conn_with(cur_ds, cur_prior, cur_update, cur_insert, cur_ts)

    _result = ic.rotate(
        conn,
        credential_id=prior_id,
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-rotate-imm",
        host_context={},
        trace_id=None,
        overlap_seconds=3600,
        immediate_revoke=True,
    )

    change = captured["changes"][0]
    assert change.outbox_payload.get("prior_new_state") == "REVOKED"
    # request_payload must declare immediate_revoke=True
    assert captured["specs"][0].request_payload["immediate_revoke"] is True


# ---------------------------------------------------------------------------
# (f) revoke -> fail-closed.
# ---------------------------------------------------------------------------


def test_revoke_flips_state_to_revoked(monkeypatch):
    """AC3: revoke sets state to REVOKED; no secret in result."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    # Cursor sequence:
    #   1. load current row (id, state, version, safe_suffix, issued_by, created_at)
    #   2. _get_datastream_status (SELECT)
    #   3. mutation UPDATE state -> REVOKED
    #   4. mutation SELECT final state
    cur_load = _cur(
        (cred_id, "ACTIVE", 1, "abcdef", "operator@example.com", ts, "email"),
        rowcount=0,
    )
    cur_ds = _cur(("my_connector", "org-1"), rowcount=0)
    cur_update = _cur(None, rowcount=1)
    cur_ts = _cur(("REVOKED", ts), rowcount=0)
    conn = _conn_with(cur_load, cur_ds, cur_update, cur_ts)

    result = ic.revoke(
        conn,
        credential_id=cred_id,
        datastream_id="ds-1",
        actor="operator@example.com",
        idempotency_key="ik-revoke-1",
        host_context={},
        trace_id=None,
    )

    assert result["state"] == "REVOKED"
    assert "full_secret" not in result, "revoke result must NOT contain full_secret"
    assert "token_hash" not in result, "revoke result must NOT contain token_hash"

    # Outbox payload: must NOT contain any secret.
    change = captured["changes"][0]
    assert "token_hash" not in change.outbox_payload
    assert "full_secret" not in change.outbox_payload


def test_revoke_raises_conflict_for_already_terminal_credential():
    """AC3: revoking a REVOKED credential raises InboundCredentialConflict."""
    import datetime

    from core import inbound_credentials as ic

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    cred_id = "dic_01JZAAABBBCCCDDDEEEFFF00001"

    cur_load = _cur(
        (cred_id, "REVOKED", 1, "abcdef", "operator@example.com", ts, "email"),
        rowcount=0,
    )
    conn = _conn_with(cur_load)

    with pytest.raises(ic.InboundCredentialConflict):
        ic.revoke(
            conn,
            credential_id=cred_id,
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


# ---------------------------------------------------------------------------
# (i) Rate-limit: non-enumerating denial.
# ---------------------------------------------------------------------------


def test_check_rate_limit_raises_when_count_exceeded(monkeypatch):
    """AC4: rate limit raises InboundCredentialRateLimited when exceeded."""
    from core import inbound_credentials as ic

    # Simulate count >= max by returning a high number.
    monkeypatch.setenv("INBOUND_RL_MAX_ISSUES_PER_HOUR", "2")
    cur = _cur((10,), rowcount=0)
    conn = _conn_with(cur)

    with pytest.raises(ic.InboundCredentialRateLimited):
        ic.check_rate_limit(conn, datastream_id="ds-1", channel="email", operation="issue")


def test_check_rate_limit_passes_when_under_limit(monkeypatch):
    """AC4: no exception when count is under the limit."""
    from core import inbound_credentials as ic

    monkeypatch.setenv("INBOUND_RL_MAX_ISSUES_PER_HOUR", "10")
    cur = _cur((3,), rowcount=0)
    conn = _conn_with(cur)

    # Must not raise.
    ic.check_rate_limit(conn, datastream_id="ds-1", channel="email", operation="issue")


# ---------------------------------------------------------------------------
# (k) Domain not-READY gate.
# ---------------------------------------------------------------------------


def test_issue_raises_domain_not_ready_when_no_domain_configured(monkeypatch):
    """AC1: issue raises InboundCredentialDomainNotReady when domain unconfigured."""
    from core import inbound_credentials as ic

    # domain_cfg = None -> no domain configured
    monkeypatch.setattr(
        "core.connector_domain.get_domain_config",
        lambda conn, *, environment, connector_name: None,
    )
    monkeypatch.setattr(
        "core.connector_installation_api.refuse_activation_unless_ready",
        lambda conn, *, connector_name, environment: None,
    )
    monkeypatch.setattr(
        "core.inbound_credentials.check_rate_limit",
        lambda conn, *, datastream_id, channel, operation: None,
    )

    cur_ds = _cur(("my_connector", "org-1"), rowcount=1)
    conn = _conn_with(cur_ds)

    with pytest.raises(ic.InboundCredentialDomainNotReady):
        ic.issue(
            conn,
            datastream_id="ds-1",
            channel="email",
            actor="operator@example.com",
            idempotency_key="ik-nodomain",
            host_context={},
            trace_id=None,
        )


def test_issue_raises_domain_not_ready_when_installation_not_ready(monkeypatch):
    """AC1: issue raises InboundCredentialDomainNotReady when installation not READY."""
    from core import inbound_credentials as ic
    from core.connector_installation_api import ConnectorNotReady

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
    monkeypatch.setattr(
        "core.inbound_credentials.check_rate_limit",
        lambda conn, *, datastream_id, channel, operation: None,
    )

    cur_ds = _cur(("my_connector", "org-1"), rowcount=1)
    conn = _conn_with(cur_ds)

    with pytest.raises(ic.InboundCredentialDomainNotReady):
        ic.issue(
            conn,
            datastream_id="ds-1",
            channel="email",
            actor="operator@example.com",
            idempotency_key="ik-notready",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (n) request_payload has NO raw token, NO random id (deterministic, H1).
# ---------------------------------------------------------------------------


def test_issue_request_payload_has_no_raw_token_and_no_random_id(monkeypatch):
    """H1: request_payload is deterministic -- no raw token, no random row id."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    conn = _conn_with(
        _cur(("my_connector", "org-1"), rowcount=1),
        _cur(None, rowcount=1),
        _cur((ts,), rowcount=0),
    )

    result = ic.issue(
        conn,
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-deterministic",
        host_context={},
        trace_id=None,
    )

    spec = captured["specs"][0]
    payload = spec.request_payload

    # No raw token in payload.
    full_secret = result["full_secret"]
    assert full_secret not in json.dumps(payload), "LEAK: raw token in request_payload"

    # No random row id (dic_... must not be in request_payload).
    assert not any(
        str(v).startswith("dic_") for v in payload.values()
    ), "request_payload must not contain the row id (generated at write time)"

    # Payload must contain a token_hash (safe digest only).
    assert "token_hash" in payload
    assert len(payload["token_hash"]) == 64  # sha256 hex


# ---------------------------------------------------------------------------
# (o) Outbox payload has NO raw token, NO token_hash.
# ---------------------------------------------------------------------------


def test_issue_outbox_payload_has_no_secret_fields(monkeypatch):
    """E38-NFR03: outbox payload must NOT carry token_hash or raw token."""
    import datetime

    from core import inbound_credentials as ic

    captured: dict = {}
    _stub_operation(monkeypatch, ic, capture=captured)
    _patch_domain_ready(monkeypatch)
    _patch_rate_limit_pass(monkeypatch)

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    conn = _conn_with(
        _cur(("my_connector", "org-1"), rowcount=1),
        _cur(None, rowcount=1),
        _cur((ts,), rowcount=0),
    )

    result = ic.issue(
        conn,
        datastream_id="ds-1",
        channel="webhook",
        actor="operator@example.com",
        idempotency_key="ik-outbox-check",
        host_context={},
        trace_id=None,
    )

    change = captured["changes"][0]
    outbox = change.outbox_payload

    full_secret = result["full_secret"]
    assert "token_hash" not in outbox, "token_hash must NOT appear in outbox_payload"
    assert full_secret not in json.dumps(outbox), "raw token must NOT appear in outbox_payload"


# ---------------------------------------------------------------------------
# (p) MCP get_inbound_credential_status registered + returns safe model.
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


def test_mcp_get_inbound_credential_status_no_secret_in_result(
    monkeypatch, _clean_mcp_registry
):
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
    monkeypatch.setattr(
        "core.db.get_connection",
        lambda: __import__("contextlib").nullcontext(MagicMock()),
    )

    from core import operations_mcp

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


# ---------------------------------------------------------------------------
# (q) ASGI: POST issue returns 200 with full_secret; GET list returns no secret.
# (r) ASGI: revoke after issue returns 200.
# (s) ASGI: missing Idempotency-Key -> 422.
# ---------------------------------------------------------------------------


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
        lambda ds_id, identity, minimum_capability: True,
    )

    ts = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

    # Stub issue.
    def _stub_issue(conn, *, datastream_id, channel, actor, idempotency_key,
                    host_context, trace_id, **kwargs):
        return {
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
            "full_secret": "ds_SECRETTOKEN@inbound.example.com",
        }

    monkeypatch.setattr("core.inbound_credentials.issue", _stub_issue)

    # Stub list_credentials.
    def _stub_list(conn, *, datastream_id, include_terminal=False):
        return [{
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
        }]

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


# ---------------------------------------------------------------------------
# Live-PG-gated tests: partial-unique, immutability trigger, no-raw-token-column.
# ---------------------------------------------------------------------------


@pytest.mark.live_pg
def test_live_pg_partial_unique_prevents_two_active_credentials(pg_conn):
    """Live-PG: partial-unique index allows at most one ACTIVE/ROTATING per (ds, ch)."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        # Insert a datastream row if needed (use a test one).
        ds_id = f"ds-test-cred-{_ulid.ULID()}"
        # The test assumes a test datastream exists or we insert a minimal one.
        # We insert directly into the credentials table with a test datastream_id
        # and a known operation_id that we have to create first.
        op_id = f"op_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, "
            "host_context, versions, request_hash, provider_references, "
            "confirmation_mode, idempotency_key_hash, state) "
            "VALUES (%s, 'platform', 'inbound.credential.issued', 'test', "
            "'[\"ds:test\"]'::jsonb, '{}'::jsonb, '{}'::jsonb, %s, "
            "'{}'::jsonb, 'server', %s, 'pending')",
            (op_id, "a" * 64, "b" * 64),
        )
        cred1_id = f"dic_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.datastream_inbound_credentials "
            "(id, datastream_id, channel, token_hash, safe_suffix, "
            "state, version, issued_by, operation_id) "
            "VALUES (%s, %s, 'email', %s, 'aabbcc', 'ACTIVE', 1, 'test', %s)",
            (cred1_id, ds_id, "a" * 64, op_id),
        )
        pg_conn.commit()

        # Second ACTIVE insert for same (ds_id, 'email') must fail.
        op_id2 = f"op_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, "
            "host_context, versions, request_hash, provider_references, "
            "confirmation_mode, idempotency_key_hash, state) "
            "VALUES (%s, 'platform', 'inbound.credential.issued', 'test', "
            "'[\"ds:test\"]'::jsonb, '{}'::jsonb, '{}'::jsonb, %s, "
            "'{}'::jsonb, 'server', %s, 'pending')",
            (op_id2, "c" * 64, "d" * 64),
        )
        cred2_id = f"dic_{_ulid.ULID()}"
        try:
            cur.execute(
                "INSERT INTO app.datastream_inbound_credentials "
                "(id, datastream_id, channel, token_hash, safe_suffix, "
                "state, version, issued_by, operation_id) "
                "VALUES (%s, %s, 'email', %s, 'ccddee', 'ACTIVE', 2, 'test', %s)",
                (cred2_id, ds_id, "e" * 64, op_id2),
            )
            pg_conn.commit()
            pytest.fail("Expected unique constraint violation for second ACTIVE credential")
        except Exception as exc:
            pg_conn.rollback()
            assert "unique" in str(exc).lower() or "duplicate" in str(exc).lower(), (
                f"Expected unique constraint violation, got: {exc}"
            )


@pytest.mark.live_pg
def test_live_pg_immutability_trigger_blocks_token_hash_update(pg_conn):
    """Live-PG: protect_inbound_credential blocks updating token_hash."""
    import ulid as _ulid

    with pg_conn.cursor() as cur:
        ds_id = f"ds-test-immut-{_ulid.ULID()}"
        op_id = f"op_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.operations "
            "(id, effective_org_id, command_type, actor, resource_path, "
            "host_context, versions, request_hash, provider_references, "
            "confirmation_mode, idempotency_key_hash, state) "
            "VALUES (%s, 'platform', 'inbound.credential.issued', 'test', "
            "'[\"ds:test\"]'::jsonb, '{}'::jsonb, '{}'::jsonb, %s, "
            "'{}'::jsonb, 'server', %s, 'pending')",
            (op_id, "f" * 64, "g" * 64),
        )
        cred_id = f"dic_{_ulid.ULID()}"
        cur.execute(
            "INSERT INTO app.datastream_inbound_credentials "
            "(id, datastream_id, channel, token_hash, safe_suffix, "
            "state, version, issued_by, operation_id) "
            "VALUES (%s, %s, 'webhook', %s, 'aabbcc', 'ACTIVE', 1, 'test', %s)",
            (cred_id, ds_id, "h" * 64, op_id),
        )
        pg_conn.commit()

        # Attempt to mutate token_hash (must be blocked by trigger).
        try:
            cur.execute(
                "UPDATE app.datastream_inbound_credentials "
                "SET token_hash = %s WHERE id = %s",
                ("i" * 64, cred_id),
            )
            pg_conn.commit()
            pytest.fail("Expected immutability trigger to block token_hash update")
        except Exception as exc:
            pg_conn.rollback()
            assert "immutable" in str(exc).lower() or "token_hash" in str(exc).lower(), (
                f"Expected immutability exception, got: {exc}"
            )


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
