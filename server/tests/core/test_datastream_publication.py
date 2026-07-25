"""Offline unit tests for the atomic publication orchestrator (Story 12.5).

These run WITHOUT Postgres. They cover the PURE logic (state-machine transitions,
DQ-gate decisions, fail-closed, hash checks) and the atomic-commit rollback
behaviour via a scripted fake connection/cursor that injects a mid-transaction
DB error. The live constraints (append-only trigger, real FK/pointer swap) are
proven in the pg-gated tests in
server/tests/integration/test_datastream_publication_constraints.py.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.datastream_publication import (  # noqa: E402
    ACTIVE_STATES,
    DEFAULT_MAX_ROW_COUNT_DELTA_PCT,
    GATE_CONTENT_HASH_MISMATCH,
    GATE_EMPTY_CANDIDATE,
    GATE_MAPPING_DRIFT,
    GATE_ROW_COUNT_DELTA_EXCEEDED,
    GATE_SCHEMA_HASH_MISMATCH,
    STATE_CANCELLED,
    STATE_CREATED,
    STATE_FAILED,
    STATE_LOADING,
    STATE_PUBLISHED,
    STATE_PUBLISHING,
    STATE_READY,
    STATE_VALIDATING,
    TERMINAL_STATES,
    evaluate_dq_gates,
    is_valid_transition,
    resolve_dq_thresholds,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


# ---------------------------------------------------------------------------
# State machine (pure).
# ---------------------------------------------------------------------------

_ALL_STATES = [
    STATE_CREATED,
    STATE_LOADING,
    STATE_VALIDATING,
    STATE_READY,
    STATE_PUBLISHING,
    STATE_PUBLISHED,
    STATE_FAILED,
    STATE_CANCELLED,
]

_VALID_FORWARD = {
    (STATE_CREATED, STATE_LOADING),
    (STATE_LOADING, STATE_VALIDATING),
    (STATE_VALIDATING, STATE_READY),
    (STATE_READY, STATE_PUBLISHING),
    (STATE_PUBLISHING, STATE_PUBLISHED),
}

_CANCELLABLE = {STATE_CREATED, STATE_LOADING, STATE_VALIDATING, STATE_READY}


def test_all_valid_forward_transitions_accepted():
    for current, new in _VALID_FORWARD:
        assert is_valid_transition(current, new), f"{current}->{new} should be valid"


def test_failed_reachable_from_every_non_terminal_state():
    for current in _ALL_STATES:
        expected = current not in TERMINAL_STATES
        assert is_valid_transition(current, STATE_FAILED) is expected


def test_cancelled_only_from_cancellable_states():
    for current in _ALL_STATES:
        expected = current in _CANCELLABLE
        assert is_valid_transition(current, STATE_CANCELLED) is expected
    # Explicitly: publishing may NOT cancel (no safe cancel mid-commit).
    assert is_valid_transition(STATE_PUBLISHING, STATE_CANCELLED) is False


def test_no_transition_out_of_any_terminal_state():
    for terminal in TERMINAL_STATES:
        for target in _ALL_STATES:
            assert is_valid_transition(terminal, target) is False


def test_representative_invalid_transitions_rejected():
    # Backward and skip transitions are all invalid.
    for current, new in [
        (STATE_VALIDATING, STATE_LOADING),  # backward
        (STATE_CREATED, STATE_READY),       # skip
        (STATE_LOADING, STATE_PUBLISHING),  # skip
        (STATE_READY, STATE_PUBLISHED),     # skip publishing
        (STATE_CREATED, STATE_PUBLISHED),   # skip everything
    ]:
        assert is_valid_transition(current, new) is False, f"{current}->{new}"


def test_active_states_are_the_non_terminal_ones():
    assert ACTIVE_STATES == frozenset(
        {STATE_CREATED, STATE_LOADING, STATE_VALIDATING, STATE_READY, STATE_PUBLISHING}
    )


# ---------------------------------------------------------------------------
# DQ threshold resolution (project-preference governed, never a silent hardcode).
# ---------------------------------------------------------------------------


def test_resolve_thresholds_documented_default_when_unset():
    delta, allow_empty, source = resolve_dq_thresholds(None)
    assert delta == DEFAULT_MAX_ROW_COUNT_DELTA_PCT
    assert allow_empty is False
    assert source == "documented_default"


def test_resolve_thresholds_uses_project_preference_when_set():
    delta, allow_empty, source = resolve_dq_thresholds(
        {"max_row_count_delta_pct": 10, "allow_empty_publication": True}
    )
    assert delta == 10.0
    assert allow_empty is True
    assert source == "project_preference"


def test_resolve_thresholds_negative_delta_falls_back_to_default():
    delta, _allow, source = resolve_dq_thresholds({"max_row_count_delta_pct": -5})
    assert delta == DEFAULT_MAX_ROW_COUNT_DELTA_PCT
    assert source == "documented_default"


# ---------------------------------------------------------------------------
# DQ gate decisions (pure) -- fail closed, hash checks.
# ---------------------------------------------------------------------------


def _base_gate_kwargs(**overrides):
    kwargs = dict(
        row_count=100,
        content_hash=_HASH_A,
        validated_content_hash=_HASH_A,
        prior_row_count=100,
        plan_source_schema_hash=None,
        current_capability_fingerprint=None,
        landing_schema_hash=None,
        plan_declared_schema_hash=None,
        preferences=None,
        approved=False,
        force_empty_publish=False,
    )
    kwargs.update(overrides)
    return kwargs


def test_all_gates_pass_returns_empty_list():
    assert evaluate_dq_gates(**_base_gate_kwargs()) == []


def test_empty_candidate_blocked_by_default():
    issues = evaluate_dq_gates(**_base_gate_kwargs(row_count=0, prior_row_count=100))
    codes = {i["code"] for i in issues}
    assert GATE_EMPTY_CANDIDATE in codes


def test_empty_candidate_publishable_with_force_and_preference():
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(
            row_count=0,
            prior_row_count=100,
            force_empty_publish=True,
            preferences={"allow_empty_publication": True},
        )
    )
    assert GATE_EMPTY_CANDIDATE not in {i["code"] for i in issues}


def test_empty_candidate_force_without_preference_still_blocked():
    # force_empty_publish alone is NOT enough: the project preference must allow it.
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(row_count=0, prior_row_count=100, force_empty_publish=True)
    )
    assert GATE_EMPTY_CANDIDATE in {i["code"] for i in issues}


def test_row_count_delta_exceeded_blocked_unless_approved():
    # 100 -> 200 = 100% delta, over the default 50% threshold.
    issues = evaluate_dq_gates(**_base_gate_kwargs(row_count=200, prior_row_count=100))
    assert GATE_ROW_COUNT_DELTA_EXCEEDED in {i["code"] for i in issues}
    # Owner approval clears it.
    issues_ok = evaluate_dq_gates(
        **_base_gate_kwargs(row_count=200, prior_row_count=100, approved=True)
    )
    assert GATE_ROW_COUNT_DELTA_EXCEEDED not in {i["code"] for i in issues_ok}


def test_row_count_delta_uses_project_threshold():
    # A tighter 10% threshold blocks a 20% delta that the default 50% would allow.
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(
            row_count=120,
            prior_row_count=100,
            preferences={"max_row_count_delta_pct": 10},
        )
    )
    assert GATE_ROW_COUNT_DELTA_EXCEEDED in {i["code"] for i in issues}
    # The same delta passes under the documented default (50%).
    issues_default = evaluate_dq_gates(
        **_base_gate_kwargs(row_count=120, prior_row_count=100)
    )
    assert GATE_ROW_COUNT_DELTA_EXCEEDED not in {i["code"] for i in issues_default}


def test_content_hash_mismatch_blocks_when_independent_hash_diverges():
    # FIX 1: a DIVERGENT independent validated hash fires content_hash_mismatch and
    # is not Owner-overridable.
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(content_hash=_HASH_A, validated_content_hash=_HASH_B, approved=True)
    )
    hits = [i for i in issues if i["code"] == GATE_CONTENT_HASH_MISMATCH]
    assert hits, "divergent independent hash must fire content_hash_mismatch"
    assert hits[0].get("content_hash_verified") is True


def test_content_hash_absent_is_skipped_and_marked_unverified_not_a_false_pass():
    # FIX 1: v1 default path -- NO independent validated_content_hash supplied. The
    # gate must be SKIPPED (byte-identity not independently verified) and recorded
    # as an explicit content_hash_verified=False advisory marker -- NEVER a false
    # pass and NEVER a content_hash_mismatch block.
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(content_hash=_HASH_A, validated_content_hash=None)
    )
    codes = {i["code"] for i in issues}
    assert GATE_CONTENT_HASH_MISMATCH not in codes
    markers = [i for i in issues if i.get("code") == "content_hash_check"]
    assert markers, "absent independent hash must record an honest marker"
    marker = markers[0]
    assert marker.get("content_hash_verified") is False
    assert marker.get("advisory") is True


def test_content_hash_absent_marker_is_non_blocking():
    # FIX 1: the advisory marker must NOT block the publish path. split_gate_issues
    # separates it from the blocking issues so run_dq_gates returns [] here.
    from core.datastream_publication import split_gate_issues

    issues = evaluate_dq_gates(
        **_base_gate_kwargs(content_hash=_HASH_A, validated_content_hash=None)
    )
    blocking, advisory = split_gate_issues(issues)
    assert blocking == []
    assert len(advisory) == 1
    assert advisory[0]["content_hash_verified"] is False


def test_content_hash_null_stored_with_independent_hash_fails_closed():
    # A NULL stored hash cannot prove byte-identity against a supplied independent
    # hash -> fail closed (AD-9).
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(content_hash=None, validated_content_hash=_HASH_A)
    )
    assert GATE_CONTENT_HASH_MISMATCH in {i["code"] for i in issues}


def test_mapping_drift_blocks_when_hashes_differ():
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(
            plan_source_schema_hash=_HASH_A,
            current_capability_fingerprint=_HASH_B,
        )
    )
    assert GATE_MAPPING_DRIFT in {i["code"] for i in issues}


def test_no_mapping_drift_when_fingerprints_match():
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(
            plan_source_schema_hash=_HASH_A,
            current_capability_fingerprint=_HASH_A,
        )
    )
    assert GATE_MAPPING_DRIFT not in {i["code"] for i in issues}


def test_schema_hash_mismatch_blocks():
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(
            landing_schema_hash=_HASH_A,
            plan_declared_schema_hash=_HASH_B,
        )
    )
    assert GATE_SCHEMA_HASH_MISMATCH in {i["code"] for i in issues}


def test_every_gate_issue_carries_code_detail_repair():
    issues = evaluate_dq_gates(
        **_base_gate_kwargs(
            row_count=0,
            content_hash=_HASH_A,
            validated_content_hash=_HASH_B,
        )
    )
    assert issues
    for issue in issues:
        assert set(issue.keys()) >= {"code", "detail", "repair"}


# ---------------------------------------------------------------------------
# Atomic commit rollback with a scripted fake connection (no Postgres).
#
# The fake cursor answers the SELECTs commit_publication needs, then raises on the
# 3rd write (the pointer swap) to simulate a mid-transaction DB error. We assert
# the whole group rolled back and the execution was failed OUT OF BAND (separate
# connection).
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._last_result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        # Answer the two FOR UPDATE selects commit_publication issues.
        if "FROM app.datastreams" in s and "FOR UPDATE" in s:
            self._last_result = (self._conn.prior_pointer,)
        elif "FROM app.datastream_executions" in s and "FOR UPDATE" in s:
            # datastream_id, plan_version_id, mapping_version_id, state, content_hash, row_count
            self._last_result = (
                self._conn.datastream_id,
                "dsp_x",
                "dmap_x",
                self._conn.state,
                _HASH_A,
                100,
            )
        elif s.startswith("UPDATE app.datastreams SET current_published_execution_id"):
            # The pointer swap is the injected failure point.
            self._conn.pointer_swap_attempts += 1
            if self._conn.fail_on_pointer_swap:
                raise RuntimeError("injected mid-transaction DB error")
            self.rowcount = 1
        else:
            self._last_result = None
            self.rowcount = 1

    def fetchone(self):
        return self._last_result


class _FakeConn:
    def __init__(self, *, fail_on_pointer_swap):
        self.executed = []
        self.committed = False
        self.rolled_back = False
        self.prior_pointer = "dse_prior"
        self.datastream_id = "ds_1"
        self.state = STATE_READY
        self.fail_on_pointer_swap = fail_on_pointer_swap
        self.pointer_swap_attempts = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


class _RecordingFactory:
    """A connection_factory that records the out-of-band failure connection."""

    def __init__(self):
        self.opened = []

    def __call__(self):
        conn = _OutOfBandConn()
        self.opened.append(conn)
        return conn


class _OutOfBandConn:
    def __init__(self):
        self.executed = []
        self.committed = False
        self.state = STATE_PUBLISHING
        self._enter = self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _OutOfBandCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


class _OutOfBandCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = [("state",), ("project_id",)]
        self._result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if "SELECT state, project_id FROM app.datastream_executions" in s:
            self._result = (self._conn.state, "proj_a")
        elif "SELECT state, datastream_id, project_id" in s and "FOR UPDATE" in s:
            self.description = [("state",), ("datastream_id",), ("project_id",)]
            self._result = (self._conn.state, "ds_1", "proj_a")
        elif s.startswith("UPDATE app.datastream_executions"):
            self._conn.state = STATE_FAILED
            self.rowcount = 1
        elif "INSERT INTO app.audit_log" in s:
            self.rowcount = 1
        elif "SELECT id, datastream_id, project_id, plan_version_id" in s:
            # advance_state's final _fetch_execution: return a full row.
            self.description = [
                ("id",), ("datastream_id",), ("project_id",), ("plan_version_id",),
                ("mapping_version_id",), ("projection_plan_ref",), ("state",),
                ("state_changed_at",), ("content_hash",), ("row_count",),
                ("error_code",), ("error_detail",), ("idempotency_key_hash",),
                ("created_by",), ("created_at",), ("updated_at",),
            ]
            self._result = (
                "dse_current", "ds_1", "proj_a", "dsp_x", "dmap_x", {},
                self._conn.state, None, None, None, "publication_error",
                "rolled back", None, "test", None, None,
            )
        else:
            self._result = None

    def fetchone(self):
        return self._result


def test_commit_publication_rolls_back_and_fails_out_of_band(monkeypatch):
    from core import datastream_publication as pub

    # Silence the in-transaction audit insert (it runs on the fake conn only on
    # the happy path; on the failure path we never reach it).
    conn = _FakeConn(fail_on_pointer_swap=True)
    factory = _RecordingFactory()

    with pytest.raises(RuntimeError, match="injected mid-transaction DB error"):
        pub.commit_publication(
            "dse_current", "proj_a", "user-1", conn, connection_factory=factory
        )

    # The transaction rolled back; nothing committed on the primary connection.
    assert conn.rolled_back is True
    assert conn.committed is False
    # The pointer swap was ATTEMPTED (proving we got past log insert) then failed.
    assert conn.pointer_swap_attempts == 1
    # The failure was written OUT OF BAND on a SEPARATE connection.
    assert len(factory.opened) == 1
    oob = factory.opened[0]
    assert oob.committed is True
    assert oob.state == STATE_FAILED


def test_commit_publication_rejects_non_ready_execution():
    from core import datastream_publication as pub
    from core.datastream_publication import InvalidStateTransition

    conn = _FakeConn(fail_on_pointer_swap=False)
    conn.state = STATE_VALIDATING  # not ready
    factory = _RecordingFactory()

    with pytest.raises(InvalidStateTransition):
        pub.commit_publication(
            "dse_current", "proj_a", "user-1", conn, connection_factory=factory
        )
    assert conn.committed is False
    assert conn.rolled_back is True
