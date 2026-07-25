"""Story 12.11 bounded synchronize / reload / reprocess tests (offline).

Fully offline (no live Postgres): the pure scope/interval/retention helpers are
exercised directly, the DB is a MagicMock cursor, and the AD-27 substrate seams
(``operations_mcp._insert_preparation`` / ``_load_preparation`` /
``_mark_preparation_confirmed`` / ``_load_target``, ``operations.execute_operation``,
``datastream_publication.create_execution``) are monkeypatched. Mirrors
test_epic36_operations_mcp.py / test_epic36_datastream_recovery.py.

Coverage (Story 12.11 AC1-AC4):
  (AC1) each verb states interval/partition + versions + expected impact +
        confirmation; actor/reason/idempotency/trace/source-version/target-candidate
        are recorded on the durable operation.
  (AC2) a reload half-open [from, to) range supersedes ONLY its scope: adjacent
        intervals + unrelated dimensions + the prior published version outside the
        scope are untouched (proven via window tiling + candidate projection plan).
  (AC3) reprocess reapplies the chosen mapping WITHOUT a provider call (asserted: no
        provider/queue call); missing retention / incompatible schema block with an
        actionable reason.
  (AC4) an invalid candidate stays UNPUBLISHED: the dispatch NEVER calls
        commit_publication / mutates current_published_execution_id.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest
from core import bounded_recovery as br

# ---------------------------------------------------------------------------
# Fake connection helper (MagicMock cursor context manager).
# ---------------------------------------------------------------------------


def _fake_conn(fetch_map=None):
    """Return a MagicMock connection whose cursor returns queued fetchone rows.

    ``fetch_map`` is unused here; individual tests monkeypatch the module I/O
    helpers directly, so the connection is a passthrough MagicMock.
    """
    conn = MagicMock()
    cur = MagicMock()
    cur.__enter__.return_value = cur
    cur.__exit__.return_value = False
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# (Pure) reload half-open scope normalization + windowing.
# ---------------------------------------------------------------------------


def test_reload_scope_is_half_open_and_tiles_into_inclusive_windows():
    # [2026-07-01, 2026-07-06) -> inclusive last day 07-05, one window 01..05.
    scope = br.normalize_reload_scope("2026-07-01", "2026-07-06")
    assert scope.date_from == "2026-07-01"
    assert scope.date_to_exclusive == "2026-07-06"
    assert scope.windows == ({"date_from": "2026-07-01", "date_to": "2026-07-05"},)


def test_reload_scope_splits_wide_range_into_31_day_windows():
    # A 40-day inclusive span (01-01 .. 02-09) splits into 31 + 9.
    scope = br.normalize_reload_scope("2026-01-01", "2026-02-10")  # exclusive 02-10
    assert scope.windows[0] == {"date_from": "2026-01-01", "date_to": "2026-01-31"}
    assert scope.windows[1] == {"date_from": "2026-02-01", "date_to": "2026-02-09"}
    # The windows cover EXACTLY [from, to_exclusive) and nothing beyond.
    assert scope.windows[-1]["date_to"] == "2026-02-09"  # 02-10 is NOT reloaded.


def test_reload_scope_rejects_empty_and_inverted_and_overwide_ranges():
    for bad in (
        ("2026-07-05", "2026-07-05"),  # empty (from == to_exclusive)
        ("2026-07-06", "2026-07-01"),  # inverted
        ("2026-01-01", "2027-01-01"),  # over-wide (> 31 days bounded)
        ("nope", "2026-07-06"),        # malformed
    ):
        with pytest.raises(br.BoundedRecoveryError) as exc:
            br.normalize_reload_scope(*bad)
        assert exc.value.code == "forbidden_interval"


def test_adjacent_reloads_tile_without_overlap():
    # AC2 isolation: [01, 06) and [06, 11) share NO day -- 06 belongs only to the
    # second reload (half-open), so an earlier reload never supersedes a later day.
    a = br.normalize_reload_scope("2026-07-01", "2026-07-06")
    b = br.normalize_reload_scope("2026-07-06", "2026-07-11")
    a_days = {w["date_to"] for w in a.windows} | {w["date_from"] for w in a.windows}
    b_days = {w["date_from"] for w in b.windows}
    assert "2026-07-06" in b_days
    assert "2026-07-06" not in a_days  # the boundary day is NOT in the first scope.


# ---------------------------------------------------------------------------
# (Pure) synchronize interval.
# ---------------------------------------------------------------------------


def test_synchronize_interval_ends_yesterday_and_uses_refetch_depth():
    today = date(2026, 7, 23)
    iv = br.synchronize_interval(today, refetch_days=3)
    assert iv == {"from": "2026-07-20", "to": "2026-07-22"}  # 3 days ending yesterday.


def test_synchronize_interval_falls_back_to_default_depth():
    today = date(2026, 7, 23)
    iv = br.synchronize_interval(today, refetch_days=None)
    # DEFAULT_SYNCHRONIZE_DAYS days ending yesterday.
    assert iv["to"] == "2026-07-22"
    span = (date.fromisoformat(iv["to"]) - date.fromisoformat(iv["from"])).days + 1
    assert span == br.DEFAULT_SYNCHRONIZE_DAYS


# ---------------------------------------------------------------------------
# (Pure) reprocess retention + schema compatibility.
# ---------------------------------------------------------------------------


def test_reprocess_retention_missing_blocks_with_actionable_reason():
    check = br.evaluate_reprocess_retention(
        retained_execution_id=None,
        retained_source_schema_hash=None,
        chosen_mapping_source_schema_hash="a" * 64,
    )
    assert check.available is False
    assert check.code == "retention_unavailable"
    assert "synchron" in check.reason.lower() or "recharg" in check.reason.lower()


def test_reprocess_incompatible_schema_blocks_with_actionable_reason():
    check = br.evaluate_reprocess_retention(
        retained_execution_id="dse_OLD",
        retained_source_schema_hash="a" * 64,
        chosen_mapping_source_schema_hash="b" * 64,  # differs -> incompatible.
    )
    assert check.available is False
    assert check.code == "incompatible_schema"
    assert check.retained_execution_id == "dse_OLD"


def test_reprocess_available_when_retained_and_schema_match():
    check = br.evaluate_reprocess_retention(
        retained_execution_id="dse_OLD",
        retained_source_schema_hash="c" * 64,
        chosen_mapping_source_schema_hash="c" * 64,
    )
    assert check.available is True
    assert check.code is None


def test_reprocess_unknown_retained_schema_blocks():
    check = br.evaluate_reprocess_retention(
        retained_execution_id="dse_OLD",
        retained_source_schema_hash=None,  # unknown shape -> cannot prove safe.
        chosen_mapping_source_schema_hash="c" * 64,
    )
    assert check.available is False
    assert check.code == "incompatible_schema"


# ---------------------------------------------------------------------------
# (Pure) impact statement per verb (AC1).
# ---------------------------------------------------------------------------


def test_impact_never_touches_published_pointer_and_is_candidate_only():
    for kind in br.BOUNDED_KINDS:
        impact = br.build_impact(kind, interval={"from": "a", "to": "b"},
                                 reload_scope=None, reprocess=None)
        assert impact["touches_published_pointer"] is False
        assert impact["candidate_only"] is True


def test_reprocess_impact_declares_no_provider_call():
    check = br.RetentionCheck(available=True, retained_execution_id="dse_OLD")
    impact = br.build_impact(br.KIND_REPROCESS, interval=None, reload_scope=None,
                             reprocess=check)
    assert impact["calls_provider"] is False
    assert impact["retained_execution_id"] == "dse_OLD"


# ---------------------------------------------------------------------------
# assemble_proposal -- composes the AD-27 substrate per verb.
# ---------------------------------------------------------------------------


def _patch_target(monkeypatch, **over):
    from core import operations_mcp

    base = {
        "datastream_id": "ds-1", "project_id": "proj-1", "org_id": "org-1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    }
    base.update(over)
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: base)
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate",
                        lambda p, n: {"can_proceed": True, "verdict": "ok",
                                      "estimated_points": n, "platform_known": True})
    return base


def test_assemble_synchronize_states_interval_and_versions(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_refetch_days", lambda conn, ds: 3)
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_SYNCHRONIZE,
        actor="user-1", reason="late data", date_from=None, date_to_exclusive=None,
        partition=None, chosen_mapping_version_id=None, estimated_points=5,
        today=date(2026, 7, 23),
    )
    assert proposal.kind == "synchronize"
    assert proposal.interval == {"from": "2026-07-20", "to": "2026-07-22"}
    assert proposal.target_versions == {"plan_version_id": "p1",
                                        "mapping_version_id": "m1",
                                        "policy_version": "pol-1"}
    assert proposal.impact["touches_published_pointer"] is False
    assert proposal.rollback_ref == "dse_GOOD"
    assert proposal.reason == "late data"


def test_assemble_reload_carries_half_open_windows(monkeypatch):
    _patch_target(monkeypatch)
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_RELOAD,
        actor="u", reason=None, date_from="2026-07-01", date_to_exclusive="2026-07-06",
        partition=None, chosen_mapping_version_id=None, estimated_points=0,
    )
    assert proposal.kind == "reload"
    assert proposal.interval["from"] == "2026-07-01"
    assert proposal.interval["to"] == "2026-07-05"  # inclusive last day.
    assert proposal.interval["to_exclusive"] == "2026-07-06"
    assert proposal.interval["windows"] == [{"date_from": "2026-07-01",
                                             "date_to": "2026-07-05"}]
    assert proposal.impact["supersedes_only_scope"] is True


def test_assemble_reload_rejects_overwide_range(monkeypatch):
    _patch_target(monkeypatch)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.assemble_proposal(
            conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_RELOAD,
            actor="u", reason=None, date_from="2026-01-01",
            date_to_exclusive="2027-01-01", partition=None,
            chosen_mapping_version_id=None, estimated_points=0,
        )
    assert exc.value.code == "forbidden_interval"


def test_assemble_reprocess_blocks_when_retention_missing(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_retained_source", lambda conn, ds: None)
    monkeypatch.setattr(br, "_load_mapping_source_schema_hash", lambda conn, mv: "a" * 64)
    conn, _ = _fake_conn()

    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.assemble_proposal(
            conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
            actor="u", reason=None, date_from=None, date_to_exclusive=None,
            partition=None, chosen_mapping_version_id="m2", estimated_points=0,
        )
    assert exc.value.code == "retention_unavailable"


def test_assemble_reprocess_blocks_on_incompatible_schema(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_retained_source", lambda conn, ds: {
        "execution_id": "dse_OLD", "mapping_version_id": "m1",
        "row_count": 100, "state": "published", "source_schema_hash": "a" * 64,
    })
    monkeypatch.setattr(br, "_load_mapping_source_schema_hash", lambda conn, mv: "b" * 64)
    conn, _ = _fake_conn()

    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.assemble_proposal(
            conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
            actor="u", reason=None, date_from=None, date_to_exclusive=None,
            partition=None, chosen_mapping_version_id="m2", estimated_points=0,
        )
    assert exc.value.code == "incompatible_schema"


def test_assemble_reprocess_pins_chosen_mapping_when_compatible(monkeypatch):
    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_retained_source", lambda conn, ds: {
        "execution_id": "dse_OLD", "mapping_version_id": "m1",
        "row_count": 100, "state": "published", "source_schema_hash": "c" * 64,
    })
    monkeypatch.setattr(br, "_load_mapping_source_schema_hash", lambda conn, mv: "c" * 64)
    conn, _ = _fake_conn()

    proposal = br.assemble_proposal(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_REPROCESS,
        actor="u", reason=None, date_from=None, date_to_exclusive=None,
        partition=None, chosen_mapping_version_id="m2", estimated_points=0,
    )
    # The proposal pins the CHOSEN mapping (m2), not the live one (m1).
    assert proposal.target_versions["mapping_version_id"] == "m2"
    assert proposal.interval["calls_provider"] is False
    assert proposal.interval["retained_execution_id"] == "dse_OLD"
    assert proposal.impact["calls_provider"] is False


# ---------------------------------------------------------------------------
# prepare_bounded_recovery -- persists into the AD-27 store, no durable op.
# ---------------------------------------------------------------------------


def test_prepare_persists_immutable_proposal_and_returns_stated_fields(monkeypatch):
    from core import operations_mcp

    _patch_target(monkeypatch)
    monkeypatch.setattr(br, "_load_refetch_days", lambda conn, ds: 3)
    inserted = {}

    def fake_insert(conn, payload, idem):
        inserted["payload"] = payload
        inserted["idem"] = idem
        return "prep_NEW"

    monkeypatch.setattr(operations_mcp, "_insert_preparation", fake_insert)
    conn, _ = _fake_conn()

    out = br.prepare_bounded_recovery(
        conn, org_id="org-1", datastream_id="ds-1", kind=br.KIND_SYNCHRONIZE,
        actor="user-1", reason="late data", today=date(2026, 7, 23),
    )
    assert out["preparation_id"] == "prep_NEW"
    for field in ("kind", "target", "target_versions", "interval", "impact",
                  "quota", "lock_ref", "rollback_ref", "reason", "expires_in_seconds"):
        assert field in out, field
    # The immutable proposal payload records the requesting actor + never publishes.
    assert inserted["payload"]["actor"] == "user-1"
    assert inserted["payload"]["impact"]["touches_published_pointer"] is False
    # The idempotency key is deterministic over the immutable proposal fingerprint.
    assert inserted["idem"].startswith("operations.recovery:org-1:ds-1:synchronize:")


# ---------------------------------------------------------------------------
# confirm_bounded_recovery -- routes EXACTLY ONE durable operation (AC1) and
# records actor/reason/idempotency/trace/source-version/target-candidate.
# ---------------------------------------------------------------------------


def _prep_row(**over):
    base = {
        "id": "prep_1", "org_id": "org-1", "project_id": "proj-1",
        "datastream_id": "ds-1", "kind": "synchronize", "actor": "user-1",
        "target_versions": {"plan_version_id": "p1", "mapping_version_id": "m1",
                            "policy_version": "pol-1"},
        "interval": {"from": "2026-07-20", "to": "2026-07-22"},
        "impact": {}, "quota": {"estimated_points": 5},
        "lock_ref": None, "rollback_ref": "dse_GOOD",
        "idempotency_key_hash": None, "state": "prepared", "operation_id": None,
        "expired": False,
    }
    base.update(over)
    return base


def _wire_confirm(monkeypatch, prep, target, exec_return):
    """Wire the confirm seams; pin the idempotency hash so the tamper guard passes."""
    from core import operations, operations_mcp
    from core.operations import _sha256

    monkeypatch.setattr(operations_mcp, "_load_preparation", lambda conn, pid: prep)
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: target)
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "pol-1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate",
                        lambda p, n: {"can_proceed": True, "verdict": "ok",
                                      "estimated_points": n, "platform_known": True})
    marked = {}
    monkeypatch.setattr(operations_mcp, "_mark_preparation_confirmed",
                        lambda conn, pid, op_id: marked.setdefault("op_id", op_id))

    fp = operations_mcp._proposal_fingerprint(
        {"datastream_id": prep["datastream_id"], "kind": prep["kind"],
         "interval": prep["interval"], "target_versions": prep["target_versions"]}
    )
    key = operations_mcp._recovery_idempotency_key(
        prep["org_id"], prep["datastream_id"], prep["kind"], fp
    )
    prep["idempotency_key_hash"] = _sha256(key)

    captured = {"calls": 0}

    def fake_execute(conn, spec, *, mutation):
        captured["calls"] += 1
        captured["spec"] = spec
        captured["mutation"] = mutation
        return exec_return

    monkeypatch.setattr(operations, "execute_operation", fake_execute)
    return captured, marked


def _live_target(**over):
    base = {
        "datastream_id": "ds-1", "project_id": "proj-1", "org_id": "org-1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    }
    base.update(over)
    return base


def test_confirm_routes_exactly_one_operation_and_records_audit_fields(monkeypatch):
    from core import operations

    op = operations.OperationResult("op-1", "succeeded", {"kind": "synchronize"},
                                    "audit-1", "opout-1", False)
    captured, marked = _wire_confirm(monkeypatch, _prep_row(), _live_target(), op)
    conn, _ = _fake_conn()

    out = br.confirm_bounded_recovery(
        conn, preparation_id="prep_1", actor="user-1", reason="late data",
        trace_id="f" * 32,
    )
    assert captured["calls"] == 1
    spec = captured["spec"]
    # AC1: actor / reason / idempotency / trace / source-version / target-candidate.
    assert spec.command_type == "datastream.bounded.synchronize"
    assert spec.actor == "user-1"
    assert spec.trace_id == "f" * 32
    assert spec.request_payload["reason"] == "late data"
    assert spec.request_payload["plan_version_id"] == "p1"
    assert spec.request_payload["mapping_version_id"] == "m1"
    assert spec.confirmation_mode == "human"
    assert out["operation_id"] == "op-1"
    assert marked["op_id"] == "op-1"


def test_duplicate_confirm_replays_original_operation(monkeypatch):
    from core import operations

    replay = operations.OperationResult("op-1", "succeeded", {"kind": "reload"},
                                        "audit-1", "opout-1", True)
    prep = _prep_row(kind="reload",
                     interval={"from": "2026-07-01", "to": "2026-07-05",
                               "to_exclusive": "2026-07-06",
                               "windows": [{"date_from": "2026-07-01",
                                            "date_to": "2026-07-05"}],
                               "partition": None})
    captured, _ = _wire_confirm(monkeypatch, prep, _live_target(), replay)
    conn, _ = _fake_conn()

    out = br.confirm_bounded_recovery(conn, preparation_id="prep_1", actor="u")
    assert captured["calls"] == 1  # execute_operation itself dedups.
    assert out["replayed"] is True
    assert out["operation_id"] == "op-1"


def test_confirm_refuses_stale_versions_for_repull_verbs(monkeypatch):
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    captured, _ = _wire_confirm(monkeypatch, _prep_row(),
                                _live_target(plan_version_id="p2"), op)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.confirm_bounded_recovery(conn, preparation_id="prep_1", actor="u")
    assert exc.value.code == "stale_versions"
    assert captured["calls"] == 0  # NO dispatch on refusal.


def test_confirm_refuses_lock_conflict(monkeypatch):
    from core import operations

    op = operations.OperationResult("op-x", "succeeded", {}, "a", "b", False)
    captured, _ = _wire_confirm(monkeypatch, _prep_row(),
                                _live_target(active_execution_id="dse_active"), op)
    conn, _ = _fake_conn()
    with pytest.raises(br.BoundedRecoveryError) as exc:
        br.confirm_bounded_recovery(conn, preparation_id="prep_1", actor="u")
    assert exc.value.code == "lock_conflict"
    assert captured["calls"] == 0


def test_confirm_reprocess_skips_quota_and_exposure_gates(monkeypatch):
    from core import operations

    # Reprocess is provider-free: even with NO platform exposure it must proceed
    # (retention was proven at prepare); it never calls the provider or quota.
    op = operations.OperationResult("op-1", "succeeded", {"kind": "reprocess"},
                                    "a", "b", False)
    prep = _prep_row(kind="reprocess",
                     target_versions={"plan_version_id": "p1",
                                      "mapping_version_id": "m2",
                                      "policy_version": "pol-1"},
                     interval={"retained_execution_id": "dse_OLD",
                               "chosen_mapping_version_id": "m2",
                               "calls_provider": False})
    captured, _ = _wire_confirm(monkeypatch, prep, _live_target(platform=None), op)
    # Quota must NOT be consulted for reprocess.
    from core import operations_mcp
    monkeypatch.setattr(operations_mcp, "_quota_estimate",
                        MagicMock(side_effect=AssertionError("reprocess must not check quota")))
    conn, _ = _fake_conn()

    out = br.confirm_bounded_recovery(conn, preparation_id="prep_1", actor="u")
    assert captured["calls"] == 1
    assert out["operation_id"] == "op-1"


# ---------------------------------------------------------------------------
# (AC3) reprocess mutation NEVER calls the provider / queue.
# (AC4) the dispatch NEVER calls commit_publication / mutates the pointer.
# ---------------------------------------------------------------------------


def test_reprocess_dispatch_creates_candidate_without_provider_or_queue(monkeypatch):
    import core.datastream_publication as pub

    created = {}
    monkeypatch.setattr(
        pub, "create_execution",
        lambda **kw: created.update(kw) or {"id": "dse_new", "state": "created"},
    )
    # A provider dispatch / queue enqueue MUST NOT be called by reprocess.
    import sys
    import types

    fake_queue = types.ModuleType("core.queue")
    fake_queue.enqueue_pull = MagicMock(
        side_effect=AssertionError("reprocess must not enqueue a provider pull")
    )
    monkeypatch.setitem(sys.modules, "core.queue", fake_queue)

    commit_spy = MagicMock(side_effect=AssertionError("commit_publication must not be called"))
    monkeypatch.setattr(pub, "commit_publication", commit_spy, raising=False)

    conn, cur = _fake_conn()
    prep = _prep_row(kind="reprocess",
                     target_versions={"plan_version_id": "p1",
                                      "mapping_version_id": "m2",
                                      "policy_version": "pol-1"},
                     interval={"retained_execution_id": "dse_OLD",
                               "chosen_mapping_version_id": "m2",
                               "calls_provider": False})
    result = br._dispatch_bounded_recovery(conn, "op-1", prep, _live_target())

    assert result.outcome == "succeeded"
    assert result.result["execution_id"] == "dse_new"
    assert result.result["calls_provider"] is False
    # The candidate is created against the CHOSEN mapping (m2), no provider call.
    assert created["mapping_version_id"] == "m2"
    assert created["projection_plan"]["recovery_kind"] == "reprocess"
    assert created["projection_plan"]["calls_provider"] is False
    fake_queue.enqueue_pull.assert_not_called()
    commit_spy.assert_not_called()


def test_reload_dispatch_carries_half_open_windows_and_supersedes_only_scope(monkeypatch):
    import core.datastream_publication as pub

    created = {}
    monkeypatch.setattr(
        pub, "create_execution",
        lambda **kw: created.update(kw) or {"id": "dse_new", "state": "created"},
    )
    commit_spy = MagicMock(side_effect=AssertionError("commit_publication must not be called"))
    monkeypatch.setattr(pub, "commit_publication", commit_spy, raising=False)

    conn, cur = _fake_conn()
    prep = _prep_row(kind="reload",
                     interval={"from": "2026-07-01", "to": "2026-07-05",
                               "to_exclusive": "2026-07-06",
                               "windows": [{"date_from": "2026-07-01",
                                            "date_to": "2026-07-05"}],
                               "partition": None})
    result = br._dispatch_bounded_recovery(conn, "op-1", prep, _live_target())

    assert result.outcome == "succeeded"
    plan = created["projection_plan"]
    # AC2: the candidate carries the half-open windows so it supersedes ONLY that scope.
    assert plan["recovery_kind"] == "reload"
    assert plan["half_open_range"] == {"from": "2026-07-01", "to_exclusive": "2026-07-06"}
    assert plan["windows"] == [{"date_from": "2026-07-01", "date_to": "2026-07-05"}]
    # AC4: no SQL issued via conn touched the published pointer.
    sql = " ".join(str(c.args[0]) for c in cur.execute.call_args_list if c.args)
    assert "current_published_execution_id" not in sql
    commit_spy.assert_not_called()


def test_dispatch_candidate_rejection_stays_unpublished(monkeypatch):
    # AC4: an invalid candidate (create_execution rejects) yields a failed outcome
    # and NEVER publishes -- the current version stays authoritative.
    import core.datastream_publication as pub

    def boom(**kw):
        raise pub.PublicationError("projection_not_executable", "bad plan")

    monkeypatch.setattr(pub, "create_execution", boom)
    commit_spy = MagicMock(side_effect=AssertionError("commit_publication must not be called"))
    monkeypatch.setattr(pub, "commit_publication", commit_spy, raising=False)

    conn, _ = _fake_conn()
    result = br._dispatch_bounded_recovery(conn, "op-1", _prep_row(), _live_target())
    assert result.outcome == "failed"
    assert result.result["reason"] == "candidate_rejected"
    commit_spy.assert_not_called()


# ---------------------------------------------------------------------------
# Static guard: the module never calls commit_publication (AC4) and never
# imports a connector module (AD-2).
# ---------------------------------------------------------------------------


def test_module_source_never_calls_commit_publication():
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "core" / "bounded_recovery.py"
    ).read_text(encoding="utf-8")
    assert "commit_publication(" not in src
    assert "current_published_execution_id =" not in src  # never writes the pointer.


def test_migration_080_widens_kind_check_additively():
    from pathlib import Path

    sql = (
        Path(__file__).resolve().parents[3]
        / "infra" / "nango" / "migrations" / "080_bounded_recovery.sql"
    ).read_text(encoding="utf-8")
    assert sql.strip().startswith("-- Story 12.11")
    assert "operation_preparations_kind_check" in sql
    for verb in ("synchronize", "reload", "reprocess"):
        assert f"'{verb}'" in sql
    # The 36.13 verbs remain admissible (superset, additive).
    for verb in ("retry", "refetch", "reconcile"):
        assert f"'{verb}'" in sql
    assert "BEGIN;" in sql and "COMMIT;" in sql
