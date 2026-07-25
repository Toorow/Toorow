"""Offline unit tests for the safe replace/append/rollback module (Story 12.12).

These run WITHOUT Postgres. They cover the PURE decision logic (rollback window,
append availability, empty-replace gate, owner-floor classification) and the
rollback / preflight orchestration via a scripted fake connection/cursor. The live
constraints (append-only trigger on the rollback log row, real pointer swap-back,
concurrent-execution structural guard, the DISTINCTNESS from the 36.18 mapping
rollback) are proven in the pg-gated tests in
server/tests/integration/test_dataset_recovery_constraints.py.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.dataset_recovery import (  # noqa: E402
    ACTION_REPLACE,
    ACTION_ROLLBACK,
    DEFAULT_ROLLBACK_WINDOW_HOURS,
    OWNER_FLOOR_OPERATIONS,
    AppendUnavailable,
    ConcurrentMutationActive,
    EmptyReplacementBlocked,
    OwnerFloorRequired,
    RollbackGateFailed,
    RollbackTargetInvalid,
    RollbackWindowExpired,
    append_availability,
    empty_replacement_allowed,
    preflight_replace,
    requires_owner_floor,
    resolve_rollback_window_hours,
    rollback_dataset,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64


# ---------------------------------------------------------------------------
# Rollback window resolution (project-preference governed, documented default).
# ---------------------------------------------------------------------------


def test_rollback_window_defaults_to_documented_when_unset():
    hours, source = resolve_rollback_window_hours(None)
    assert hours == DEFAULT_ROLLBACK_WINDOW_HOURS
    assert source == "documented_default"
    hours, source = resolve_rollback_window_hours({})
    assert source == "documented_default"


def test_rollback_window_uses_project_preference_when_set():
    hours, source = resolve_rollback_window_hours({"rollback_window_hours": 48})
    assert hours == 48
    assert source == "project_preference"


def test_rollback_window_zero_is_a_valid_preference_no_window():
    hours, source = resolve_rollback_window_hours({"rollback_window_hours": 0})
    assert hours == 0
    assert source == "project_preference"


def test_rollback_window_negative_falls_back_to_default():
    hours, source = resolve_rollback_window_hours({"rollback_window_hours": -5})
    assert hours == DEFAULT_ROLLBACK_WINDOW_HOURS
    assert source == "documented_default"


# ---------------------------------------------------------------------------
# Append availability (safe-by-default => unavailable => Replace fallback).
# ---------------------------------------------------------------------------


def test_append_unavailable_without_stable_key_contract():
    res = append_availability(
        append_stable_key=None,
        target_schema_hash=_HASH_A,
        candidate_schema_hash=_HASH_A,
    )
    assert res["available"] is False
    assert res["fallback_action"] == ACTION_REPLACE
    assert res["reason"] == "no_stable_key_contract"


def test_append_unavailable_with_empty_stable_key():
    res = append_availability(
        append_stable_key=[],
        target_schema_hash=_HASH_A,
        candidate_schema_hash=_HASH_A,
    )
    assert res["available"] is False
    assert res["reason"] == "no_stable_key_contract"


def test_append_unavailable_with_incompatible_schema():
    res = append_availability(
        append_stable_key=["date", "campaign_id"],
        target_schema_hash=_HASH_A,
        candidate_schema_hash=_HASH_B,
    )
    assert res["available"] is False
    assert res["fallback_action"] == ACTION_REPLACE
    assert res["reason"] == "incompatible_schema"


def test_append_unavailable_when_schema_unknown():
    res = append_availability(
        append_stable_key=["date"],
        target_schema_hash=None,
        candidate_schema_hash=_HASH_A,
    )
    assert res["available"] is False
    assert res["reason"] == "schema_unknown"


def test_append_available_with_stable_key_and_compatible_schema():
    res = append_availability(
        append_stable_key=["date", "campaign_id"],
        target_schema_hash=_HASH_A,
        candidate_schema_hash=_HASH_A,
    )
    assert res["available"] is True
    assert res["fallback_action"] is None
    assert res["stable_key"] == ["date", "campaign_id"]


# ---------------------------------------------------------------------------
# Empty-replace gate (blocked unless 2nd confirm + preference).
# ---------------------------------------------------------------------------


def test_non_empty_replacement_always_allowed_by_empty_gate():
    assert empty_replacement_allowed(
        row_count=100, force_empty_publish=False, allow_empty_publication=False
    )


def test_empty_replacement_blocked_by_default():
    assert not empty_replacement_allowed(
        row_count=0, force_empty_publish=False, allow_empty_publication=False
    )


def test_empty_replacement_needs_both_confirm_and_preference():
    # 2nd confirm alone is not enough.
    assert not empty_replacement_allowed(
        row_count=0, force_empty_publish=True, allow_empty_publication=False
    )
    # preference alone is not enough.
    assert not empty_replacement_allowed(
        row_count=0, force_empty_publish=False, allow_empty_publication=True
    )
    # both -> allowed.
    assert empty_replacement_allowed(
        row_count=0, force_empty_publish=True, allow_empty_publication=True
    )


def test_empty_replacement_unknown_rowcount_fails_closed():
    assert not empty_replacement_allowed(
        row_count=None, force_empty_publish=True, allow_empty_publication=True
    )


# ---------------------------------------------------------------------------
# Owner-floor classification (which operations require the Owner role).
# ---------------------------------------------------------------------------


def test_destination_policy_operations_require_owner_floor():
    for op in ("change_ownership", "change_access", "change_retention", "irreversible_deletion"):
        assert requires_owner_floor(op) is True
        assert op in OWNER_FLOOR_OPERATIONS


def test_recoverable_data_actions_do_not_require_owner_floor():
    for op in ("dataset.replace", "dataset.append", "dataset.rollback", "anything_else"):
        assert requires_owner_floor(op) is False


# ---------------------------------------------------------------------------
# Owner-floor enforcement (RBAC) via a fake conn + patched role resolver.
# ---------------------------------------------------------------------------


class _RoleConn:
    """A conn whose only job is to satisfy identity_has_project_role via monkeypatch."""

    def cursor(self):  # pragma: no cover - not reached (resolver is patched)
        raise AssertionError("role resolver should be patched, not the cursor")


def test_owner_floor_allows_owner(monkeypatch):
    import core.project_access as pa
    from core import dataset_recovery

    monkeypatch.setattr(pa, "epic36_production_access_enabled", lambda **_: False)
    monkeypatch.setattr(pa, "identity_has_project_role", lambda *a, **k: True)
    # No raise == allowed.
    dataset_recovery.enforce_owner_floor(
        _RoleConn(),
        operation="change_retention",
        identity="owner-1",
        project_id="proj_a",
    )


def test_owner_floor_rejects_non_owner(monkeypatch):
    import core.project_access as pa
    from core import dataset_recovery

    monkeypatch.setattr(pa, "epic36_production_access_enabled", lambda **_: False)
    monkeypatch.setattr(pa, "identity_has_project_role", lambda *a, **k: False)
    with pytest.raises(OwnerFloorRequired) as exc:
        dataset_recovery.enforce_owner_floor(
            _RoleConn(),
            operation="irreversible_deletion",
            identity="member-1",
            project_id="proj_a",
        )
    assert exc.value.operation == "irreversible_deletion"


# ---------------------------------------------------------------------------
# Scripted fake connection for the rollback / preflight orchestration.
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, conn):
        self._conn = conn
        self.description = None
        self._result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        s = " ".join(sql.split())
        if "SELECT current_published_execution_id" in s and "FOR UPDATE" in s:
            self._result = (self._conn.current_pointer,)
        elif "current_published_execution_id, append_stable_key" in s:
            self._result = (self._conn.current_pointer, self._conn.append_stable_key)
        elif "state = ANY(%s)" in s:  # active-execution concurrency probe
            self._result = (self._conn.active_execution,) if self._conn.active_execution else None
        elif "max_row_count_delta_pct, allow_empty_publication, rollback_window_hours" in s:
            self._result = (
                self._conn.max_delta,
                self._conn.allow_empty,
                self._conn.rollback_window_hours,
            )
        elif "FROM app.datastream_publication_log" in s and "effective_deadline" in s and (
            "execution_id = %s" in s
        ):
            # _load_rollback_target -- 8 columns:
            #   execution_id, retained, rollback_deadline(stored), published_at,
            #   effective_deadline, expired, content_hash, row_count
            t = self._conn.target
            self._result = (
                t["execution_id"],
                t["retained"],
                t.get("stored_deadline"),
                t.get("published_at"),
                t["deadline"],
                t["expired"],
                t["content_hash"],
                t["row_count"],
            ) if t else None
        elif (
            "SELECT published_at FROM app.datastream_publication_log" in s
            and "execution_id = %s" in s
        ):
            # _current_pointer_published_at (the monotonic anchor for the default
            # resolver). Return a fixed instant; the fake's latest_prior is authoritative.
            self._result = (self._conn.current_pointer_published_at,)
        elif "SELECT execution_id FROM app.datastream_publication_log" in s:
            # _latest_retained_prior (default target resolution, monotonic)
            self._result = (
                (self._conn.latest_prior,) if self._conn.latest_prior else None
            )
        elif "SELECT row_count FROM app.datastream_executions" in s:
            self._result = (self._conn.prior_row_count,)
        elif "SELECT plan_version_id, mapping_version_id, content_hash, row_count" in s:
            self._result = (
                "dsp_x", "dmap_x",
                self._conn.target["content_hash"] if self._conn.target else None,
                self._conn.target["row_count"] if self._conn.target else None,
                self._conn.target_state,
            )
        elif s.startswith("UPDATE app.datastreams SET current_published_execution_id"):
            self._conn.pointer_swaps += 1
            self.rowcount = 1
        elif s.startswith("INSERT INTO app.datastream_publication_log"):
            self._conn.log_inserts += 1
            self.rowcount = 1
        elif s.startswith("INSERT INTO app.datastream_outbox"):
            self._conn.outbox_inserts += 1
            self.rowcount = 1
        elif "INSERT INTO app.audit_log" in s:
            self.rowcount = 1
        else:
            self._result = None
            self.rowcount = 1

    def fetchone(self):
        return self._result


class _FakeConn:
    def __init__(self):
        self.executed = []
        self.committed = False
        self.rolled_back = False
        self.current_pointer = "dse_current"
        self.append_stable_key = None
        self.active_execution = None
        self.max_delta = None
        self.allow_empty = None
        self.rollback_window_hours = None
        self.latest_prior = "dse_prior"
        self.current_pointer_published_at = "2026-07-21T00:00:00+00:00"
        self.prior_row_count = 100
        self.pointer_swaps = 0
        self.log_inserts = 0
        self.outbox_inserts = 0
        # A valid, PUBLISHED target.
        self.target_state = "published"
        self.target = {
            "execution_id": "dse_prior",
            "retained": True,
            # stored_deadline is None on a normal forward publish (C1): the runtime
            # resolves the effective deadline from published_at + the window. The fake
            # returns the DB-computed `expired` flag directly.
            "stored_deadline": None,
            "published_at": "2026-07-20T00:00:00+00:00",
            "deadline": "2026-08-19T00:00:00+00:00",  # effective_deadline
            "expired": False,
            "content_hash": _HASH_B,
            "row_count": 90,
        }

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True


def _patch_audit(monkeypatch):
    import core.audit as audit

    monkeypatch.setattr(audit, "insert_audit_row", lambda *a, **k: None)


def test_rollback_swaps_pointer_back_and_writes_new_log_row(monkeypatch):
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    result = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1"
    )
    # The DATASET pointer was swapped BACK to the retained prior execution.
    assert conn.pointer_swaps == 1
    assert result["rolled_back_from"] == "dse_current"
    assert result["rolled_back_to"] == "dse_prior"
    # A rollback is a NEW append-only publication-log row (not a mutation).
    assert conn.log_inserts == 1
    assert result["publication_log_id"].startswith("dplog_")
    assert conn.committed is True


def test_rollback_only_after_gates_pass_empty_target_blocks(monkeypatch):
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    # A zero-row target trips the non-overridable empty gate: rollback refused.
    conn.target["row_count"] = 0
    with pytest.raises(RollbackGateFailed):
        rollback_dataset(conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1")
    assert conn.pointer_swaps == 0
    assert conn.rolled_back is True


def test_rollback_disabled_when_deadline_expired(monkeypatch):
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    conn.target["expired"] = True
    conn.target["deadline"] = "2020-01-01T00:00:00+00:00"
    with pytest.raises(RollbackWindowExpired) as exc:
        rollback_dataset(
            conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
            target_execution_id="dse_prior",
        )
    # C1: with a NULL stored deadline the source names the RESOLVED window that
    # actually governed the decision, not a phantom stored deadline.
    assert exc.value.deadline_source == "resolved_window:documented_default"
    assert conn.pointer_swaps == 0


def test_rollback_window_enforced_on_null_stored_deadline_source(monkeypatch):
    """C1: deadline_source on a normal (NULL-deadline) rollback is the resolved window.

    Every 12.5 forward publish leaves rollback_deadline NULL, so the reported source
    must be the resolved window, and it must reflect the project preference when set.
    """
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    conn.rollback_window_hours = 48  # project preference set
    result = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
        target_execution_id="dse_prior",
    )
    assert result["deadline_source"] == "resolved_window:project_preference"
    assert conn.pointer_swaps == 1


def test_rollback_idempotent_when_already_at_target(monkeypatch):
    """H1: rolling back to a target that IS the current pointer is a stable no-op.

    This is the retry short-circuit: no pointer swap, no new log row, no oscillation.
    """
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    # The resolved/explicit target equals the current pointer.
    result = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
        target_execution_id="dse_current",
    )
    assert result["already_at_target"] is True
    assert result["rolled_back_to"] == "dse_current"
    assert result["publication_log_id"] is None
    assert conn.pointer_swaps == 0
    assert conn.log_inserts == 0


def test_default_rollback_twice_lands_once_and_stays(monkeypatch):
    """H1: a retried DEFAULT (target-less) rollback does NOT oscillate the pointer.

    First call: current=dse_current, newest retained prior=dse_prior -> swap to prior.
    Second call (retry): the pointer is now dse_prior; the newest retained prior that
    is DIFFERENT from the current pointer would be dse_current, BUT the default
    resolution excludes the current pointer AND -- critically -- if it resolves back to
    the current pointer it short-circuits. Here we simulate the realistic retry where
    the latest retained prior == current pointer (nothing older) -> no-op.
    """
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    # First rollback: default resolves to dse_prior, swaps.
    result1 = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1"
    )
    assert result1["rolled_back_to"] == "dse_prior"
    assert conn.pointer_swaps == 1

    # Simulate the post-swap world: the live pointer is now dse_prior, and the
    # MONOTONIC resolver finds NOTHING strictly older than prior's own publication
    # (prior was the oldest retained version). The default resolver returns None ->
    # the target-less request is an idempotent NO-OP, NOT a re-swap back to dse_current.
    conn.current_pointer = "dse_prior"
    conn.latest_prior = None
    result2 = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1"
    )
    # The retry is a stable no-op -- the pointer STAYS at dse_prior (no re-publish of
    # the version just rolled away, no second swap).
    assert result2["already_at_target"] is True
    assert conn.pointer_swaps == 1  # still exactly one swap total
    assert conn.log_inserts == 1  # only the first rollback wrote a log row


def test_rollback_rejected_when_a_mutation_is_active(monkeypatch):
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    conn.active_execution = "dse_inflight"
    with pytest.raises(ConcurrentMutationActive) as exc:
        rollback_dataset(conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1")
    assert exc.value.blocking_execution_id == "dse_inflight"
    assert exc.value.lock_reason == "execution_in_flight"
    assert conn.pointer_swaps == 0


def test_rollback_explicit_missing_target_raises_not_found(monkeypatch):
    """An EXPLICIT target with no publication-log evidence is a real error."""
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    conn.target = None  # no log evidence for the explicitly-requested target
    with pytest.raises(RollbackTargetInvalid):
        rollback_dataset(
            conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
            target_execution_id="dse_missing",
        )
    assert conn.pointer_swaps == 0


def test_default_rollback_with_nothing_older_is_noop_not_error(monkeypatch):
    """H1: a DEFAULT (target-less) rollback with no strictly-older retained version is
    an idempotent NO-OP (not RollbackTargetNotFound) -- we are already at the oldest.
    """
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    conn.latest_prior = None  # monotonic resolver finds nothing older
    result = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1"
    )
    assert result["already_at_target"] is True
    assert conn.pointer_swaps == 0
    assert conn.log_inserts == 0


def test_rollback_refused_when_target_not_published(monkeypatch):
    """L4: the target's live state is re-verified as 'published' at swap time."""
    _patch_audit(monkeypatch)
    conn = _FakeConn()
    conn.target_state = "failed"  # superseded/retired since the log row was written
    with pytest.raises(RollbackTargetInvalid) as exc:
        rollback_dataset(
            conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
            target_execution_id="dse_prior",
        )
    assert "published" in str(exc.value)
    assert conn.pointer_swaps == 0


def test_rollback_only_empty_gate_blocks_drift_and_schema_are_skipped(monkeypatch):
    """M1 (honest): on a rollback ONLY the empty gate blocks.

    The mapping-drift and schema-hash gates are intentionally NOT fed on a pointer-swap
    rollback (no live source probe -> no honest 'current' fingerprint; feeding the
    stored hash to both sides would be theatre). So a non-empty, retained, in-window
    target passes even though those gates never fire -- and an empty target still
    blocks via empty_candidate. This asserts the empty gate is the sole blocker here.
    """
    _patch_audit(monkeypatch)
    # Non-empty target -> passes (drift/schema skipped, not blocking).
    conn = _FakeConn()
    result = rollback_dataset(
        conn, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
        target_execution_id="dse_prior",
    )
    assert result["rolled_back_to"] == "dse_prior"
    # Empty target -> blocks via the (sole) empty gate.
    conn2 = _FakeConn()
    conn2.target["row_count"] = 0
    with pytest.raises(RollbackGateFailed):
        rollback_dataset(
            conn2, datastream_id="ds_1", project_id="proj_a", actor="owner-1",
            target_execution_id="dse_prior",
        )


# ---------------------------------------------------------------------------
# Replace preflight (concurrency + empty gate BEFORE an execution is minted).
# ---------------------------------------------------------------------------


def test_preflight_replace_rejects_when_mutation_active():
    conn = _FakeConn()
    conn.active_execution = "dse_inflight"
    with pytest.raises(ConcurrentMutationActive) as exc:
        preflight_replace(
            conn, datastream_id="ds_1", project_id="proj_a", candidate_row_count=10
        )
    assert exc.value.blocking_execution_id == "dse_inflight"


def test_preflight_replace_blocks_empty_by_default():
    conn = _FakeConn()
    with pytest.raises(EmptyReplacementBlocked):
        preflight_replace(
            conn, datastream_id="ds_1", project_id="proj_a", candidate_row_count=0
        )


def test_preflight_replace_allows_empty_with_confirm_and_pref():
    conn = _FakeConn()
    conn.allow_empty = True
    res = preflight_replace(
        conn,
        datastream_id="ds_1",
        project_id="proj_a",
        candidate_row_count=0,
        force_empty_publish=True,
    )
    assert res["ok"] is True
    assert res["action"] == ACTION_REPLACE


def test_preflight_replace_allows_non_empty():
    conn = _FakeConn()
    res = preflight_replace(
        conn, datastream_id="ds_1", project_id="proj_a", candidate_row_count=42
    )
    assert res["ok"] is True


# ---------------------------------------------------------------------------
# Import-surface sanity (AppendUnavailable is exported for the API error map).
# ---------------------------------------------------------------------------


def test_append_unavailable_error_is_exported():
    err = AppendUnavailable("no_stable_key_contract")
    assert err.code == "append_unavailable"
    assert err.reason == "no_stable_key_contract"
    assert ACTION_ROLLBACK == "dataset.rollback"
