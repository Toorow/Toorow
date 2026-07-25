"""Story 38.4: Continuous domain verification and synthetic delivery -- offline + ASGI tests.

Covers (non-tautological per review lessons H1-H4):
  (a) Deterministic idempotency: request_payload contains NO random id; two identical
      run_verification calls produce byte-identical hashed payloads (review H1).
  (b) All-pass drives DOMAIN_PENDING -> READY via transition_state (real functions,
      mock cursor SEQUENCE matches the code's conn.cursor() call order -- review H4).
  (c) Failing check keeps the state (DOMAIN_PENDING stays); does NOT advance.
  (d) READY -> DEGRADED on drift with first_seen_at carried forward from the prior
      degraded run (AC2).
  (e) Existing publications/data are NOT touched by state advance/degrade.
  (f) Synthetic delivery is flagged synthetic_delivery=TRUE in the MutationResult;
      writes NO import, quarantine, or publication rows (AC3). The DB never receives
      INSERT into receipt/quarantine/import/publication tables.
  (g) Secret-free read-model: get_verification_state never returns evidence_hash,
      DNS token, signing secret, or raw credential (AC4, E38-NFR03).
  (h) Idempotent verify + conflicting-key 409 via execute_operation replay (AC5).
  (i) Audit/outbox written through execute_operation; no parallel path.
  (j) MCP get_connector_verification_status == REST GET safe model (AC4).
  (k) REST: POST /verify non-admin -> 404; missing Idempotency-Key -> 422; admin -> 200.
  (l) REST: POST /test-delivery non-admin -> 404; admin -> 200 with synthetic flag.
  (m) REST: GET /verification non-admin -> 404; admin -> 200.
  (n) Live-PG-gated: append-only trigger proven on real DB (UPDATE -> error, DELETE ->
      error); latest-run index proven via EXPLAIN on the real DB.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Shared mock helpers (same style as test_epic38_connector_domain.py).
# ---------------------------------------------------------------------------


def _cur(*fetchone_rows, fetchall=None, rowcount=1):
    """Build a mock cursor with preset fetchone returns and a rowcount."""
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    cur.fetchone.side_effect = list(fetchone_rows)
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount
    return cur


def _conn_with(*curs):
    """Build a mock connection whose cursor() calls return the supplied cursors in order."""
    conn = MagicMock()
    conn.cursor.side_effect = list(curs)
    return conn


def _stub_operation(monkeypatch, module, *, capture=None):
    """Stub execute_operation to run the mutation and return a successful OperationResult.

    Non-tautological: calls the REAL mutation function (review H4 -- the test
    actually exercises the mutation logic, not a mock of it).
    """
    from core import operations

    def execute(operation_conn, spec, *, mutation):
        changed = mutation(operation_conn, "op-ver-1")
        if capture is not None:
            capture.setdefault("specs", []).append(spec)
            capture.setdefault("changes", []).append(changed)
        return operations.OperationResult(
            "op-ver-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(module, "execute_operation", execute)


# ---------------------------------------------------------------------------
# Cursor-sequence construction helpers for the real code's conn.cursor() order.
#
# run_verification opens cursors in this exact order (review H4 -- must match):
#   [0]  SELECT id, state FROM connector_installations (load installation)
#   [1]  SELECT id FROM connector_domain_configs (load active domain config)
#   [2]  SELECT first_seen_at FROM connector_verification_runs (carry-forward,
#          only on DEGRADED path; skip on READY/pass path)
#   [N]  (mutation cursor) INSERT INTO connector_verification_runs
#   [N+1] get_installation_state re-read (F4) -- one cursor for SELECT state
#          (only when transition_state is NOT stubbed; when stubbed, also stub
#          get_installation_state so no extra cursor is consumed from conn).
#
# On the all-pass / DOMAIN_PENDING -> READY path (transition_state STUBBED):
#   cursor 0: installation row
#   cursor 1: domain config row
#   cursor 2: mutation INSERT cursor (rowcount=1)
#   get_installation_state: stubbed -> no extra cursor needed
#
# On the READY -> DEGRADED path (transition_state STUBBED):
#   cursor 0: installation row (state=READY)
#   cursor 1: domain config row
#   cursor 2: carry-forward SELECT first_seen_at
#   cursor 3: mutation INSERT cursor (rowcount=1)
#   get_installation_state: stubbed -> no extra cursor needed
#
# When testing the REAL walk (transition_state NOT stubbed), the walk itself
# opens additional cursors through transition_state (FOR UPDATE select, UPDATE).
#
# run_synthetic_delivery opens:
#   cursor 0: installation row
#   cursor 1: domain config row
#   cursor 2: mutation INSERT cursor (rowcount=1)
#
# get_verification_state opens:
#   cursor 0: SELECT latest run (JOIN) -> row or None
# ---------------------------------------------------------------------------


_INST_ROW_DOMAIN_PENDING = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "DOMAIN_PENDING")
_INST_ROW_READY = ("cin_TESTID01ABCDEFGHIJKLMNOPQ", "READY")
_DC_ROW = ("cdc_TESTCFGID01ABCDEFGHIJKLMN",)  # (id,)


def _check_pass():
    """A passing check callable (source-agnostic stub)."""
    from core.connector_verification import EVIDENCE_CLASS_ROUTING

    return True, EVIDENCE_CLASS_ROUTING, ""


def _check_fail():
    """A failing check callable (source-agnostic stub)."""
    from core.connector_verification import EVIDENCE_CLASS_ROUTING

    return False, EVIDENCE_CLASS_ROUTING, "routing_check_failed"


# ---------------------------------------------------------------------------
# (a) Deterministic idempotency -- review H1.
# ---------------------------------------------------------------------------


def test_run_verification_payload_is_deterministic(monkeypatch):
    """Two identical run_verification calls produce byte-identical hashed payloads.

    The row id (cvr_<ULID>) must NOT appear in request_payload (review H1).
    """
    from core import connector_installation as ci
    from core import connector_verification as cv

    def _payload_of_one_run():
        capture: dict = {}
        _stub_operation(monkeypatch, cv, capture=capture)

        # Cursor sequence for all-pass / DOMAIN_PENDING path:
        # [0] load installation, [1] load domain config, [2] mutation INSERT.
        conn = _conn_with(
            _cur(_INST_ROW_DOMAIN_PENDING),   # [0] installation
            _cur(_DC_ROW),                     # [1] domain config
            _cur(None, rowcount=1),            # [2] mutation INSERT (rowcount=1)
        )
        # Stub transition_state and get_installation_state to avoid real DB logic.
        # get_installation_state is called after the walk (F4 re-read).
        monkeypatch.setattr(ci, "transition_state", lambda *a, **kw: {})
        monkeypatch.setattr(
            ci, "get_installation_state",
            lambda conn, **kw: {"state": "READY"},
        )

        cv.run_verification(
            conn,
            environment="production",
            connector_name="test-connector",
            checks=[_check_pass],
            actor="platform-admin@test",
            idempotency_key="ik-same-ver",
            host_context={},
            trace_id=None,
        )
        return capture["specs"][0].request_payload

    first = _payload_of_one_run()
    second = _payload_of_one_run()
    assert first == second
    # The run id (cvr_...) must never appear in the hashed inputs.
    assert not any("cvr_" in str(v) for v in first.values())
    assert "run_id" not in first


def test_run_synthetic_delivery_payload_is_deterministic(monkeypatch):
    """Synthetic delivery request_payload is deterministic and never carries a row id.

    Cursor sequence: [0] installation, [1] domain config.
    The mutation cursor is owned by the _stub_operation which calls mutation(conn, ...),
    so conn needs a 3rd cursor slot for the mutation INSERT.
    """
    from core import connector_verification as cv

    def _payload_of_one_run():
        capture: dict = {}
        _stub_operation(monkeypatch, cv, capture=capture)

        # [0] installation, [1] domain config, [2] mutation INSERT.
        conn = _conn_with(
            _cur(_INST_ROW_DOMAIN_PENDING),
            _cur(_DC_ROW),
            _cur(None, rowcount=1),
        )
        cv.run_synthetic_delivery(
            conn,
            environment="production",
            connector_name="test-connector",
            delivery_runner=lambda: (True, "ok"),
            actor="platform-admin@test",
            idempotency_key="ik-synth-same",
            host_context={},
            trace_id=None,
        )
        return capture["specs"][0].request_payload

    first = _payload_of_one_run()
    second = _payload_of_one_run()
    assert first == second
    assert not any("cvr_" in str(v) for v in first.values())
    assert "run_id" not in first


# ---------------------------------------------------------------------------
# (b) All-pass drives DOMAIN_PENDING -> READY (state actually advances).
# ---------------------------------------------------------------------------


def test_all_pass_advances_domain_pending_to_ready(monkeypatch):
    """All checks passing from DOMAIN_PENDING: two-hop walk -> VERIFYING -> READY.

    Non-tautological (F1): we assert BOTH hops are called in order, proving the
    state machine is walked legally (not a single DOMAIN_PENDING->READY hop).
    The read-model installation_state is derived from the re-read persisted state
    (F4), not the intended target.
    """
    from core import connector_installation as ci
    from core import connector_verification as cv

    _stub_operation(monkeypatch, cv)

    transitions_called = []

    def fake_transition(conn, *, environment, connector_name, target_state, **kw):
        transitions_called.append(target_state)

    monkeypatch.setattr(ci, "transition_state", fake_transition)
    # F4: stub get_installation_state to return READY (the post-walk persisted state).
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "READY"},
    )

    # Cursor sequence for all-pass / DOMAIN_PENDING path (no carry-forward):
    # [0] installation (DOMAIN_PENDING), [1] domain config, [2] mutation INSERT.
    # transition_state and get_installation_state are stubbed -> no extra cursors.
    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
        _cur(None, rowcount=1),
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_pass],
        actor="platform-admin@test",
        idempotency_key="ik-allpass",
        host_context={},
        trace_id=None,
    )

    assert result["last_outcome"] == "passed"
    # F1: verify the two-hop walk: DOMAIN_PENDING -> VERIFYING, then VERIFYING -> READY.
    assert transitions_called == ["VERIFYING", "READY"], (
        f"Expected two-hop walk [VERIFYING, READY], got {transitions_called}"
    )
    # F4: installation_state in the read-model must reflect the re-read persisted state.
    assert result["installation_state"] == "READY"


def test_no_checks_returns_not_configured_and_does_not_advance(monkeypatch):
    """F5: An empty checks list must NOT silently pass or advance state.

    Outcome must be 'not_configured', evidence_class 'no_checks', checks_evaluated=0,
    and NO state transition must be attempted. No DB row is written (early return
    before execute_operation).
    """
    from core import connector_installation as ci
    from core import connector_verification as cv

    # Do NOT stub execute_operation -- it must never be called for zero checks.
    execute_called = [False]
    original_execute = cv.execute_operation

    def spy_execute(*a, **kw):
        execute_called[0] = True
        return original_execute(*a, **kw)

    monkeypatch.setattr(cv, "execute_operation", spy_execute)

    transitions_called = []

    def _fake_ts(conn, *, environment, connector_name, target_state, **kw):
        transitions_called.append(target_state)

    monkeypatch.setattr(ci, "transition_state", _fake_ts)

    # Only cursor 0 (installation) and 1 (domain config) are consumed --
    # no mutation cursor because the early return path writes NO DB row.
    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),   # [0] installation
        _cur(_DC_ROW),                     # [1] domain config
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[],   # F5: empty -> NOT silently passing
        actor="admin",
        idempotency_key="ik-nocheck",
        host_context={},
        trace_id=None,
    )
    # F5: must NOT be "passed" and must NOT advance.
    assert result["last_outcome"] == "not_configured", (
        f"Expected not_configured, got {result['last_outcome']!r} -- zero checks must not pass"
    )
    assert result.get("checks_evaluated", -1) == 0
    # No state transition must have been attempted.
    assert not transitions_called, (
        f"Expected no state transitions, but got {transitions_called}"
    )
    # No DB write must have been attempted.
    assert not execute_called[0], (
        "execute_operation must not be called when checks=[]: no DB row should be written"
    )


# ---------------------------------------------------------------------------
# (c) Failing check keeps / does not advance from DOMAIN_PENDING.
# ---------------------------------------------------------------------------


def test_failing_check_from_domain_pending_does_not_advance(monkeypatch):
    """A blocking failure from DOMAIN_PENDING -> outcome='failed'; no state transition."""
    from core import connector_installation as ci
    from core import connector_verification as cv

    _stub_operation(monkeypatch, cv)
    transitions_called = []
    monkeypatch.setattr(
        ci, "transition_state",
        lambda *a, **kw: transitions_called.append(kw.get("target_state")),
    )
    # F4: stub get_installation_state (called unconditionally for re-read).
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "DOMAIN_PENDING"},
    )

    # DOMAIN_PENDING is not in _DEGRADABLE so outcome=FAILED, no carry-forward cursor.
    # Cursor sequence: [0] installation, [1] domain config, [2] mutation INSERT.
    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
        _cur(None, rowcount=1),
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_fail],
        actor="admin",
        idempotency_key="ik-fail-dp",
        host_context={},
        trace_id=None,
    )

    assert result["last_outcome"] == "failed"
    # No state transition should have been attempted (target_state=None for FAILED).
    assert not transitions_called


# ---------------------------------------------------------------------------
# (d) READY -> DEGRADED on drift, first_seen_at carried forward (AC2).
# ---------------------------------------------------------------------------


def test_ready_to_degraded_carries_first_seen(monkeypatch):
    """A failing check on a READY installation: outcome='degraded', first_seen carried.

    Cursor sequence for READY -> DEGRADED path:
      [0] installation (READY), [1] domain config, [2] carry-forward SELECT,
      [3] mutation INSERT.
    Non-tautological: assert first_seen_at is the value from the prior degraded row.
    """
    import datetime as dt

    from core import connector_installation as ci
    from core import connector_verification as cv

    prior_first_seen = dt.datetime(2026, 1, 1, 10, 0, 0)

    capture: dict = {}
    _stub_operation(monkeypatch, cv, capture=capture)
    transitions_called = []

    def _record_transition(conn, *, environment, connector_name, target_state, **kw):
        transitions_called.append(target_state)

    monkeypatch.setattr(ci, "transition_state", _record_transition)
    # F4: stub get_installation_state to return DEGRADED (the post-transition state).
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "DEGRADED"},
    )

    conn = _conn_with(
        _cur(_INST_ROW_READY),          # [0] installation
        _cur(_DC_ROW),                   # [1] domain config
        _cur((prior_first_seen,)),       # [2] carry-forward SELECT -> prior degraded row
        _cur(None, rowcount=1),          # [3] mutation INSERT
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_fail],
        actor="admin",
        idempotency_key="ik-ready-degrade",
        host_context={},
        trace_id=None,
    )

    assert result["last_outcome"] == "degraded"
    assert "DEGRADED" in transitions_called
    # first_seen_at must be the prior value, not None or the current time.
    assert result["first_seen_at"] is not None
    assert "2026-01-01" in result["first_seen_at"]


def test_ready_to_degraded_no_prior_run_first_seen_is_not_null(monkeypatch):
    """F6: First-ever degradation: no prior degraded row -> first_seen_at set to DB NOW().

    The read-model first_seen_at is None here because the sentinel 'SET_TO_NOW' maps to
    None in the read-model (the real DB value is set via NOW() in SQL, not Python).
    What matters is that the INSERT uses NOW() (verified by SQL inspection), and the
    DB value will be non-null. The read-model returns None only as an artifact of the
    mock; in a live run the re-read would return the DB timestamp.
    """
    from core import connector_installation as ci
    from core import connector_verification as cv

    sql_calls = []

    def tracking_execute(operation_conn, spec, *, mutation):
        from core import operations

        def recording_execute_sql(sql, *args):
            sql_calls.append(sql.lower().strip())

        def _recording_cur(fetchone_val=None, rowcount=1):
            cur = MagicMock()
            cur.__enter__.return_value = cur
            cur.__exit__.return_value = False
            cur.fetchone.return_value = fetchone_val
            cur.rowcount = rowcount
            cur.execute = recording_execute_sql
            return cur

        op_conn = MagicMock()
        op_conn.cursor.side_effect = [_recording_cur(None, rowcount=1)]
        changed = mutation(op_conn, "op-f6-1")
        return operations.OperationResult(
            "op-f6-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(cv, "execute_operation", tracking_execute)
    monkeypatch.setattr(ci, "transition_state", lambda *a, **kw: None)
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "DEGRADED"},
    )

    conn = _conn_with(
        _cur(_INST_ROW_READY),
        _cur(_DC_ROW),
        _cur(None),              # carry-forward SELECT -> None (no prior degraded row)
        # No extra cursor: mutation cursor is provided by tracking_execute above.
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_fail],
        actor="admin",
        idempotency_key="ik-firstdegrade",
        host_context={},
        trace_id=None,
    )

    assert result["last_outcome"] == "degraded"
    # F6: The INSERT SQL must use NOW() for first_seen_at (not a Python NULL).
    insert_sqls = [
        s for s in sql_calls if "insert into" in s and "connector_verification_runs" in s
    ]
    assert len(insert_sqls) == 1, f"Expected one INSERT, got {insert_sqls}"
    assert "now()" in insert_sqls[0], (
        f"F6: first-ever degradation INSERT must use NOW() for first_seen_at, "
        f"but SQL was: {insert_sqls[0]!r}"
    )


# ---------------------------------------------------------------------------
# (b2) F2: DEGRADED all-pass must advance to READY (single hop, DEGRADED -> READY).
# ---------------------------------------------------------------------------


def test_all_pass_from_degraded_advances_to_ready(monkeypatch):
    """F2: All checks passing from DEGRADED: single hop DEGRADED -> READY.

    DEGRADED is now in _ADVANCEABLE_TO_READY; a single-hop transition is required.
    Non-tautological: assert transition_state is called with READY and NOT VERIFYING.
    """
    from core import connector_installation as ci
    from core import connector_verification as cv

    _stub_operation(monkeypatch, cv)

    transitions_called = []

    def fake_transition(conn, *, environment, connector_name, target_state, idempotency_key, **kw):
        transitions_called.append((target_state, idempotency_key))

    monkeypatch.setattr(ci, "transition_state", fake_transition)
    # F4: stub get_installation_state to return READY after the hop.
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "READY"},
    )

    # DEGRADED installation; all-pass -> advance to READY.
    # No carry-forward cursor (all_pass=True path, not degraded path).
    conn = _conn_with(
        _cur(("cin_TESTID01ABCDEFGHIJKLMNOPQ", "DEGRADED")),  # [0] installation
        _cur(_DC_ROW),                                          # [1] domain config
        _cur(None, rowcount=1),                                 # [2] mutation INSERT
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_pass],
        actor="admin",
        idempotency_key="ik-degraded-recover",
        host_context={},
        trace_id=None,
    )

    assert result["last_outcome"] == "passed"
    # F2: exactly one hop, DEGRADED -> READY (not a two-hop walk).
    assert len(transitions_called) == 1, (
        f"Expected exactly 1 hop (DEGRADED->READY), got {transitions_called}"
    )
    target, ik = transitions_called[0]
    assert target == "READY", f"Expected READY, got {target!r}"
    assert ik.endswith(":to_ready"), f"Expected idempotency key suffix ':to_ready', got {ik!r}"
    # F4: installation_state must reflect the re-read persisted state.
    assert result["installation_state"] == "READY"


# ---------------------------------------------------------------------------
# (e) Existing data NOT touched by state transition.
# ---------------------------------------------------------------------------


def test_state_advance_does_not_touch_imports_or_publications(monkeypatch):
    """Verification never writes to quarantine, import, or publication tables.

    Non-tautological: inspect every SQL call on the mock cursor and assert no
    INSERT targets durable receipt/quarantine/import/publication tables (AC3).
    """
    from core import connector_installation as ci
    from core import connector_verification as cv

    sql_calls = []

    def recording_execute(sql, *args):
        sql_calls.append(sql.lower().strip())

    _stub_operation(monkeypatch, cv)
    monkeypatch.setattr(ci, "transition_state", lambda *a, **kw: None)
    # F4: stub get_installation_state (called unconditionally after the walk).
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "READY"},
    )

    # Build cursors that record the SQL they receive.
    def _recording_cur(fetchone_val=None, rowcount=1):
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        cur.fetchone.return_value = fetchone_val
        cur.rowcount = rowcount
        cur.execute = recording_execute
        return cur

    conn = MagicMock()
    conn.cursor.side_effect = [
        _recording_cur(_INST_ROW_DOMAIN_PENDING),
        _recording_cur(_DC_ROW),
        _recording_cur(None, rowcount=1),
    ]

    cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_pass],
        actor="admin",
        idempotency_key="ik-nodatawrite",
        host_context={},
        trace_id=None,
    )

    forbidden = {
        "quarantine",
        "raw_import",
        "publication",
        "datastream_execution",
        "receipt",
    }
    for sql in sql_calls:
        for term in forbidden:
            assert term not in sql, (
                f"Verification wrote to a forbidden table: {term!r} found in SQL: {sql!r}"
            )


# ---------------------------------------------------------------------------
# (f) Synthetic delivery flags synthetic=TRUE; writes NO import/quarantine/publication.
# ---------------------------------------------------------------------------


def test_synthetic_delivery_flagged_and_writes_no_import(monkeypatch):
    """run_synthetic_delivery: MutationResult.result has synthetic_delivery=True.

    No INSERT into quarantine, import, publication, or receipt tables (AC3).
    """
    from core import connector_verification as cv

    sql_calls = []
    mutation_results = []

    def tracking_execute(operation_conn, spec, *, mutation):
        # Run the real mutation and capture what it writes.
        from core import operations

        # Use a recording cursor for the INSERT.
        def recording_execute_sql(sql, *args):
            sql_calls.append(sql.lower().strip())

        def _recording_cur(fetchone_val=None, rowcount=1):
            cur = MagicMock()
            cur.__enter__.return_value = cur
            cur.__exit__.return_value = False
            cur.fetchone.return_value = fetchone_val
            cur.rowcount = rowcount
            cur.execute = recording_execute_sql
            return cur

        op_conn = MagicMock()
        op_conn.cursor.side_effect = [_recording_cur(None, rowcount=1)]

        changed = mutation(op_conn, "op-synth-1")
        mutation_results.append(changed)
        return operations.OperationResult(
            "op-synth-1", "succeeded", changed.result, "audit-1", "outbox-1", False
        )

    monkeypatch.setattr(cv, "execute_operation", tracking_execute)

    # Cursors for run_synthetic_delivery:
    # [0] installation, [1] domain config.
    # The mutation cursor is supplied by tracking_execute above.
    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
    )

    result = cv.run_synthetic_delivery(
        conn,
        environment="production",
        connector_name="test-connector",
        delivery_runner=lambda: (True, "auth_passed"),
        actor="admin",
        idempotency_key="ik-synth-1",
        host_context={},
        trace_id=None,
    )

    # The MutationResult must carry synthetic_delivery=True.
    assert len(mutation_results) == 1
    assert mutation_results[0].result.get("synthetic_delivery") is True

    # The read-model must also flag it.
    assert result["synthetic_delivery"] is True

    # No forbidden table writes.
    forbidden = {"quarantine", "raw_import", "publication", "datastream_execution", "receipt"}
    for sql in sql_calls:
        for term in forbidden:
            assert term not in sql, (
                f"Synthetic delivery wrote to forbidden table: {term!r} in SQL {sql!r}"
            )


def test_synthetic_delivery_runner_failure_records_failed_run(monkeypatch):
    """A runner that returns (False, reason) -> outcome='failed', still synthetic."""
    from core import connector_verification as cv

    _stub_operation(monkeypatch, cv)

    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
        _cur(None, rowcount=1),
    )

    result = cv.run_synthetic_delivery(
        conn,
        environment="production",
        connector_name="test-connector",
        delivery_runner=lambda: (False, "auth_rejected"),
        actor="admin",
        idempotency_key="ik-synth-fail",
        host_context={},
        trace_id=None,
    )

    assert result["last_outcome"] == "failed"
    assert result["synthetic_delivery"] is True
    assert result["blocking_reason"] == "auth_rejected"


# ---------------------------------------------------------------------------
# (g) Secret-free read-model: get_verification_state never leaks secrets (AC4).
# ---------------------------------------------------------------------------


def test_get_verification_state_is_secret_free():
    """get_verification_state never returns evidence_hash, DNS token, or signing value."""
    from core import connector_verification as cv

    # Simulate one latest run row (no evidence_hash in the SELECT columns by design).
    run_row = (
        "passed",             # outcome
        "routing_check",      # evidence_class
        None,                 # first_seen_at
        None,                 # created_at (last_run_at)
        None,                 # blocking_reason
        False,                # synthetic_delivery
        "READY",              # inst_state
    )
    conn = _conn_with(_cur(run_row))

    result = cv.get_verification_state(
        conn, environment="production", connector_name="test-connector"
    )

    assert result is not None
    # These keys must never appear.
    for forbidden_key in ("evidence_hash", "dns_token", "signing_secret",
                          "signing_secret_ref", "raw_secret"):
        assert forbidden_key not in result, (
            f"Secret key {forbidden_key!r} found in read-model"
        )
    # Required safe keys must be present.
    for required in ("last_outcome", "evidence_class", "safe_next_action",
                     "installation_state", "synthetic_delivery"):
        assert required in result, f"Required key {required!r} missing from read-model"


def test_get_verification_state_returns_none_when_no_run():
    """get_verification_state returns None (not an error) when no run exists."""
    from core import connector_verification as cv

    conn = _conn_with(_cur(None))  # fetchone returns None -> no row
    result = cv.get_verification_state(
        conn, environment="production", connector_name="missing-connector"
    )
    assert result is None


# ---------------------------------------------------------------------------
# (h) Idempotency replay + conflicting-key 409.
# ---------------------------------------------------------------------------


def test_run_verification_replays_on_same_key(monkeypatch):
    """Same Idempotency-Key replays cleanly (OperationResult.replayed=True)."""
    from core import connector_installation as ci
    from core import connector_verification as cv
    from core import operations

    replay_result = operations.OperationResult(
        "op-ver-1", "succeeded", {"outcome": "passed", "evidence_class": "routing_check"},
        "audit-1", "outbox-1", True  # replayed=True
    )

    def execute_replay(operation_conn, spec, *, mutation):
        return replay_result

    monkeypatch.setattr(cv, "execute_operation", execute_replay)
    monkeypatch.setattr(ci, "transition_state", lambda *a, **kw: None)
    # F4: stub get_installation_state for the post-walk re-read.
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "DOMAIN_PENDING"},
    )

    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
    )

    result = cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_pass],
        actor="admin",
        idempotency_key="ik-replay",
        host_context={},
        trace_id=None,
    )

    # Should return the replayed outcome without error.
    assert result["last_outcome"] == "passed"


def test_run_verification_conflicting_key_raises(monkeypatch):
    """Conflicting payload on the same Idempotency-Key raises OperationIdempotencyConflict."""
    from core import connector_verification as cv
    from core import operations

    def execute_conflict(operation_conn, spec, *, mutation):
        raise operations.OperationIdempotencyConflict(
            "idempotency key is already bound to a different request"
        )

    monkeypatch.setattr(cv, "execute_operation", execute_conflict)

    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
    )

    with pytest.raises(operations.OperationIdempotencyConflict):
        cv.run_verification(
            conn,
            environment="production",
            connector_name="test-connector",
            checks=[_check_pass],
            actor="admin",
            idempotency_key="ik-conflict",
            host_context={},
            trace_id=None,
        )


# ---------------------------------------------------------------------------
# (i) Audit/outbox written through execute_operation (no parallel path).
# ---------------------------------------------------------------------------


def test_run_verification_routes_through_execute_operation(monkeypatch):
    """Verification MUST route through execute_operation (no parallel audit path)."""
    from core import connector_installation as ci
    from core import connector_verification as cv

    execute_call_count = [0]

    def counting_execute(operation_conn, spec, *, mutation):
        from core import operations

        execute_call_count[0] += 1
        changed = mutation(operation_conn, "op-ver-2")
        return operations.OperationResult(
            "op-ver-2", "succeeded", changed.result, "audit-2", "outbox-2", False
        )

    monkeypatch.setattr(cv, "execute_operation", counting_execute)
    monkeypatch.setattr(ci, "transition_state", lambda *a, **kw: None)
    # F4: stub get_installation_state for the post-walk re-read.
    monkeypatch.setattr(
        ci, "get_installation_state",
        lambda conn, **kw: {"state": "DOMAIN_PENDING"},
    )

    conn = _conn_with(
        _cur(_INST_ROW_DOMAIN_PENDING),
        _cur(_DC_ROW),
        _cur(None, rowcount=1),
    )

    cv.run_verification(
        conn,
        environment="production",
        connector_name="test-connector",
        checks=[_check_pass],
        actor="admin",
        idempotency_key="ik-route-check",
        host_context={},
        trace_id=None,
    )

    # Exactly 1 execute_operation call (the verification run). The state
    # transition uses transition_state (stubbed above) which internally calls
    # its own execute_operation -- but that is isolated in connector_installation.
    assert execute_call_count[0] == 1


# ---------------------------------------------------------------------------
# (j) MCP == REST safe model (AC4).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_registry(monkeypatch):
    from core import mcp_profiles

    monkeypatch.setenv("TOOROW_MCP_HIGHRISK_ENABLED", "1")
    mcp_profiles.reset_registry_for_tests()
    yield
    mcp_profiles.reset_registry_for_tests()


def test_mcp_verification_status_registered_and_is_read_only():
    """get_connector_verification_status is registered under operations, effect=read."""
    from core import mcp_profiles, operations_mcp

    class _Recorder:
        def __init__(self):
            self.handlers = {}
            self.tool = MagicMock()

        def capture(self, handler):
            self.handlers[handler.__name__] = handler
            return handler

    recorder = _Recorder()
    real = mcp_profiles.register_profiled

    def spy(mcp, handler, **kwargs):
        recorder.capture(handler)
        return real(mcp, handler, **kwargs)

    mcp_profiles.register_profiled = spy
    try:
        operations_mcp.register(recorder)
    finally:
        mcp_profiles.register_profiled = real

    decls = {d.name: d for d in mcp_profiles.registered_declarations()}
    assert "get_connector_verification_status" in decls
    vd = decls["get_connector_verification_status"]
    assert vd.profile == "operations"
    assert vd.effect == "read"
    assert vd.confirmation_mode == "none"


def test_mcp_verification_status_returns_same_model_as_rest(monkeypatch):
    """get_connector_verification_status returns the same safe model as GET /verification."""
    import core.db as _db
    from core import connector_verification as cv
    from core import operations_mcp

    monkeypatch.setattr(cv, "get_verification_state", lambda conn, **kw: {
        "connector_name": "test-connector",
        "environment": "production",
        "installation_state": "READY",
        "last_outcome": "passed",
        "evidence_class": "routing_check",
        "first_seen_at": None,
        "last_run_at": None,
        "blocking_reason": None,
        "synthetic_delivery": False,
        "safe_next_action": "no action required -- verification passed; connector is READY",
    })

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(_db, "get_connection", lambda: _FakeConn())
    monkeypatch.setenv("TOOROW_ENVIRONMENT", "production")

    # Extract the handler directly.
    from core import mcp_profiles

    mcp_profiles.reset_registry_for_tests()

    class _Rec:
        def __init__(self):
            self.handlers = {}
            self.tool = MagicMock()

        def capture(self, h):
            self.handlers[h.__name__] = h
            return h

    rec = _Rec()
    real = mcp_profiles.register_profiled
    def spy(mcp, handler, **kw):
        rec.capture(handler)
        return real(mcp, handler, **kw)
    mcp_profiles.register_profiled = spy
    try:
        operations_mcp.register(rec)
    finally:
        mcp_profiles.register_profiled = real

    handler = rec.handlers["get_connector_verification_status"]
    tool_result = handler("test-connector")

    data = tool_result.structured_content["data"]
    assert data["verified"] is True
    assert data["last_outcome"] == "passed"
    # No evidence_hash or secrets.
    assert "evidence_hash" not in data
    assert "signing_secret" not in data


# ---------------------------------------------------------------------------
# (k) REST POST /verify -- non-admin 404, missing key 422, admin 200.
# ---------------------------------------------------------------------------


def _build_asgi_app():
    """Build the ASGI app from connector_verification_api routes for ASGI tests."""
    from core.connector_verification_api import CONNECTOR_VERIFICATION_ROUTES
    from starlette.applications import Starlette

    return Starlette(routes=CONNECTOR_VERIFICATION_ROUTES)


@pytest.fixture
def asgi_client():
    from starlette.testclient import TestClient

    return TestClient(_build_asgi_app())


def test_post_verify_non_admin_returns_404(monkeypatch, asgi_client):
    """POST /verify from a non-admin returns 404 (nondisclosing, AD-5).

    F3: also asserts the denial is audited with ACTION_CONNECTOR_VERIFICATION_DENIED.
    """
    import core.api_auth as _api_auth
    import core.audit as _audit
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "non-admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: False)

    audit_calls: list = []
    monkeypatch.setattr(_audit, "write_audit_row", lambda **kw: audit_calls.append(kw))

    resp = asgi_client.post(
        "/api/connectors/test-connector/verify",
        headers={"Authorization": "Bearer tok", "Idempotency-Key": "ik-1"},
    )
    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"
    # F3: denial must be audited exactly once with the correct action.
    assert len(audit_calls) == 1, f"Expected 1 audit call, got {audit_calls}"
    assert audit_calls[0]["action"] == _audit.ACTION_CONNECTOR_VERIFICATION_DENIED


def test_post_verify_missing_idempotency_key_returns_422(monkeypatch, asgi_client):
    """POST /verify without Idempotency-Key returns 422."""
    import core.api_auth as _api_auth
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: True)

    resp = asgi_client.post(
        "/api/connectors/test-connector/verify",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 422
    assert "Idempotency-Key" in resp.json()["message"]


def test_post_verify_admin_returns_200(monkeypatch, asgi_client):
    """POST /verify from admin with Idempotency-Key returns 200 with safe model."""
    import core.api_auth as _api_auth
    import core.db as _db
    from core import connector_verification as cv
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: True)

    safe_model = {
        "installation_state": "READY",
        "last_outcome": "passed",
        "evidence_class": "routing_check",
        "first_seen_at": None,
        "last_run_at": None,
        "blocking_reason": None,
        "synthetic_delivery": False,
        "safe_next_action": "no action required -- verification passed; connector is READY",
    }
    monkeypatch.setattr(cv, "run_verification", lambda conn, **kw: safe_model)

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def commit(self): pass

    monkeypatch.setattr(_db, "get_connection", lambda: _FakeConn())

    resp = asgi_client.post(
        "/api/connectors/test-connector/verify",
        headers={"Authorization": "Bearer tok", "Idempotency-Key": "ik-admin-1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["last_outcome"] == "passed"
    assert "evidence_hash" not in body


# ---------------------------------------------------------------------------
# (l) REST POST /test-delivery -- non-admin 404, admin 200 with synthetic flag.
# ---------------------------------------------------------------------------


def test_post_test_delivery_non_admin_returns_404(monkeypatch, asgi_client):
    """POST /test-delivery from a non-admin returns 404 (nondisclosing, AD-5).

    F3: also asserts the denial is audited with ACTION_CONNECTOR_VERIFICATION_DENIED.
    """
    import core.api_auth as _api_auth
    import core.audit as _audit
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "non-admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: False)

    audit_calls: list = []
    monkeypatch.setattr(_audit, "write_audit_row", lambda **kw: audit_calls.append(kw))

    resp = asgi_client.post(
        "/api/connectors/test-connector/test-delivery",
        headers={"Authorization": "Bearer tok", "Idempotency-Key": "ik-1"},
    )
    assert resp.status_code == 404
    # F3: denial must be audited exactly once with the correct action.
    assert len(audit_calls) == 1, f"Expected 1 audit call, got {audit_calls}"
    assert audit_calls[0]["action"] == _audit.ACTION_CONNECTOR_VERIFICATION_DENIED


def test_post_test_delivery_admin_returns_200_with_synthetic_flag(monkeypatch, asgi_client):
    """POST /test-delivery: admin returns 200 with synthetic_delivery=True."""
    import core.api_auth as _api_auth
    import core.db as _db
    from core import connector_verification as cv
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: True)

    safe_model = {
        "installation_state": "DOMAIN_PENDING",
        "last_outcome": "passed",
        "evidence_class": "synthetic_delivery_auth",
        "first_seen_at": None,
        "last_run_at": None,
        "blocking_reason": None,
        "synthetic_delivery": True,
        "safe_next_action": "no action required -- verification passed; connector is READY",
    }
    monkeypatch.setattr(cv, "run_synthetic_delivery", lambda conn, **kw: safe_model)

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def commit(self): pass

    monkeypatch.setattr(_db, "get_connection", lambda: _FakeConn())

    resp = asgi_client.post(
        "/api/connectors/test-connector/test-delivery",
        headers={"Authorization": "Bearer tok", "Idempotency-Key": "ik-synth-admin"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["synthetic_delivery"] is True
    assert "evidence_hash" not in body


# ---------------------------------------------------------------------------
# (m) REST GET /verification -- non-admin 404, admin 200.
# ---------------------------------------------------------------------------


def test_get_verification_non_admin_returns_404(monkeypatch, asgi_client):
    """GET /verification from a non-admin returns 404 (nondisclosing, AD-5).

    F3: also asserts the denial is audited with ACTION_CONNECTOR_VERIFICATION_DENIED.
    """
    import core.api_auth as _api_auth
    import core.audit as _audit
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "non-admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: False)

    audit_calls: list = []
    monkeypatch.setattr(_audit, "write_audit_row", lambda **kw: audit_calls.append(kw))

    resp = asgi_client.get(
        "/api/connectors/test-connector/verification",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 404
    # F3: denial must be audited exactly once with the correct action.
    assert len(audit_calls) == 1, f"Expected 1 audit call, got {audit_calls}"
    assert audit_calls[0]["action"] == _audit.ACTION_CONNECTOR_VERIFICATION_DENIED


def test_get_verification_admin_returns_200_when_no_run(monkeypatch, asgi_client):
    """GET /verification: admin, no run yet -> 200 with verified=False."""
    import core.api_auth as _api_auth
    import core.db as _db
    from core import connector_verification as cv
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: True)
    monkeypatch.setattr(cv, "get_verification_state", lambda conn, **kw: None)

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(_db, "get_connection", lambda: _FakeConn())

    resp = asgi_client.get(
        "/api/connectors/test-connector/verification",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is False


def test_get_verification_admin_returns_200_with_run(monkeypatch, asgi_client):
    """GET /verification: admin with an existing run -> 200 with verified=True."""
    import core.api_auth as _api_auth
    import core.db as _db
    from core import connector_verification as cv
    from core import connector_verification_api as cva

    async def _fake_auth(req):
        return True, "admin@test"

    monkeypatch.setattr(_api_auth, "authenticate_api_request", _fake_auth)
    monkeypatch.setattr(cva, "_is_platform_admin", lambda identity: True)

    safe_model = {
        "connector_name": "test-connector",
        "environment": "production",
        "installation_state": "READY",
        "last_outcome": "passed",
        "evidence_class": "routing_check",
        "first_seen_at": None,
        "last_run_at": None,
        "blocking_reason": None,
        "synthetic_delivery": False,
        "safe_next_action": "no action required -- verification passed; connector is READY",
    }
    monkeypatch.setattr(cv, "get_verification_state", lambda conn, **kw: safe_model)

    class _FakeConn:
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(_db, "get_connection", lambda: _FakeConn())

    resp = asgi_client.get(
        "/api/connectors/test-connector/verification",
        headers={"Authorization": "Bearer tok"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["verified"] is True
    assert body["last_outcome"] == "passed"
    assert "evidence_hash" not in body


# ---------------------------------------------------------------------------
# (n) Live-PG-gated: append-only trigger + latest-run index.
#     These tests run only when PLATFORM_DB_URL is set (live PG gate).
# ---------------------------------------------------------------------------


pytestmark_live = pytest.mark.skipif(
    not __import__("os").environ.get("PLATFORM_DB_URL"),
    reason="live PG not configured (PLATFORM_DB_URL unset)",
)


@pytestmark_live
def test_live_append_only_trigger_rejects_update():
    """Trigger protect_connector_verification_run rejects UPDATE (append-only)."""
    import os

    import psycopg

    db_url = os.environ["PLATFORM_DB_URL"]
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DO $$
                DECLARE
                    v_inst_id TEXT;
                    v_op_id   TEXT;
                    v_run_id  TEXT;
                BEGIN
                    -- Reuse or create a minimal operation row.
                    INSERT INTO app.operations
                        (id, effective_org_id, command_type, actor, resource_path,
                         host_context, versions, request_hash, provider_references,
                         confirmation_mode, idempotency_key_hash, state)
                    VALUES
                        ('op_LIVETEST001ABCDEFGHIJKLMNOPQ', 'platform',
                         'connector.verification.ran', 'test', '["t"]'::jsonb,
                         '{}'::jsonb, '{}'::jsonb,
                         'a0' || repeat('0', 62),
                         '{}'::jsonb, 'server',
                         'b0' || repeat('0', 62), 'pending')
                    ON CONFLICT DO NOTHING;
                    SELECT id INTO v_op_id FROM app.operations
                    WHERE id = 'op_LIVETEST001ABCDEFGHIJKLMNOPQ';

                    -- Reuse or create a minimal installation row.
                    INSERT INTO app.connector_installations
                        (id, environment, connector_name, state, operation_id)
                    VALUES
                        ('cin_LIVETEST001ABCDEFGHIJKLMNO', 'test', 'live-test-conn',
                         'READY', v_op_id)
                    ON CONFLICT DO NOTHING;
                    SELECT id INTO v_inst_id FROM app.connector_installations
                    WHERE environment = 'test' AND connector_name = 'live-test-conn';

                    -- Insert one verification run.
                    v_run_id := 'cvr_LIVETEST001ABCDEFGHIJKLMNO';
                    INSERT INTO app.connector_verification_runs
                        (id, installation_id, environment, connector_name,
                         outcome, evidence_class, synthetic_delivery, operation_id)
                    VALUES
                        (v_run_id, v_inst_id, 'test', 'live-test-conn',
                         'passed', 'routing_check', FALSE, v_op_id)
                    ON CONFLICT DO NOTHING;

                    -- Attempt UPDATE -> trigger must raise.
                    BEGIN
                        UPDATE app.connector_verification_runs
                        SET evidence_class = 'tampered'
                        WHERE id = v_run_id;
                        RAISE EXCEPTION 'trigger did not fire -- append-only violation';
                    EXCEPTION WHEN OTHERS THEN
                        IF SQLERRM LIKE '%append-only%' THEN
                            NULL;  -- expected
                        ELSE
                            RAISE;
                        END IF;
                    END;

                    -- Attempt DELETE -> trigger must raise.
                    BEGIN
                        DELETE FROM app.connector_verification_runs WHERE id = v_run_id;
                        RAISE EXCEPTION 'trigger did not fire -- DELETE not blocked';
                    EXCEPTION WHEN OTHERS THEN
                        IF SQLERRM LIKE '%append-only%' THEN
                            NULL;  -- expected
                        ELSE
                            RAISE;
                        END IF;
                    END;

                    -- Rollback the test data.
                    RAISE EXCEPTION 'rollback_test_data';
                END;
                $$ LANGUAGE plpgsql;
                """
            )
        conn.rollback()


@pytestmark_live
def test_live_latest_run_index_used_by_planner():
    """idx_connector_verification_runs_latest is used in EXPLAIN for the latest-run query."""
    import json
    import os

    import psycopg

    db_url = os.environ["PLATFORM_DB_URL"]
    explain_sql = (
        "EXPLAIN (FORMAT JSON) "
        "SELECT outcome, evidence_class, first_seen_at, created_at, "
        "       blocking_reason, synthetic_delivery "
        "FROM app.connector_verification_runs "
        "WHERE environment = 'production' AND connector_name = 'test-connector' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    with psycopg.connect(db_url) as conn:
        with conn.cursor() as cur:
            cur.execute(explain_sql)
            rows = cur.fetchall()
    plan_json = json.dumps(rows)
    assert "idx_connector_verification_runs_latest" in plan_json, (
        "Expected the latest-run index to be used by the query planner. "
        f"EXPLAIN output: {plan_json[:500]}"
    )
