"""Story 36.16 recovery-bridge tests (diagnosis -> allowed procedure).

Fully offline (no live Postgres): the pure error-class -> procedure map is exercised
directly, and ``propose_recovery`` is driven with a MagicMock connection while the
36.12 diagnosis composition (``_collect_events`` / ``_diagnose``) and the operations
PREPARE seam are monkeypatched. Mirrors test_epic36_operations_mcp.py /
test_epic36_datastream_diagnosis.py.

Coverage (Story 36.16 AC1-AC5 + E36-NFR01/NFR05):
  (a) each canonical error class maps to EXACTLY ONE allowed procedure with a distinct
      owner + safe action (pure-function table test);
  (b) a recovery is NEVER an out-of-set procedure;
  (c) a write-effect recovery returns a prepared-operation REFERENCE and does NOT itself
      execute a write (no execute_operation / commit_publication);
  (d) unknown / contradictory diagnosis STOPS automation and offers a bounded Support
      handoff;
  (e) after recovery completes / fails / outcome_unknown the original operation IDs +
      last-known-good publication remain visible;
  (f) no raw provider evidence in the output.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core import datastream_recovery as dr

# ---------------------------------------------------------------------------
# (a) + (b) pure map: each canonical class -> exactly one allowed, distinct procedure.
# ---------------------------------------------------------------------------


def test_every_table_procedure_is_in_the_fixed_allowed_set():
    # (b) A recovery is NEVER an out-of-set procedure.
    for cls, proc in dr._PROCEDURE_TABLE.items():
        assert proc.procedure in dr._ALLOWED_PROCEDURES, cls
        # A write-effect operations route must carry a concrete operations kind.
        if proc.route == dr._ROUTE_OPERATIONS:
            assert proc.operations_kind in {"retry", "refetch", "reconcile"}


def test_map_error_class_returns_exactly_one_allowed_procedure_per_class():
    # (a) Every canonical class maps to exactly one allowed procedure.
    expected = {
        "auth_expired": "reconnect",
        "auth_revoked": "reconnect",
        "permission_denied": "reconnect",
        "rate_limited": "retry",
        "invalid_request": "reload",
        "provider_transient": "retry",
        "mapping_drift": "replace",
        "dq_failure": "reprocess",
        "partial_data": "refetch",
        "uncertain_publication": "reconcile",
    }
    for cls, procedure in expected.items():
        got = dr.map_error_class(cls)
        assert got is not None, cls
        assert got.procedure == procedure
        assert got.procedure in dr._ALLOWED_PROCEDURES


def test_each_error_class_has_a_distinct_owner_and_safe_action():
    # (a) Distinctness is a property of the seven canonical error classes named in AC2.
    classes = [
        "auth_expired",  # authorization failure
        "rate_limited",  # rate limit
        "invalid_request",  # configuration error
        "mapping_drift",  # mapping drift
        "dq_failure",  # DQ failure
        "partial_data",  # partial/empty data
        "uncertain_publication",  # uncertain publication
    ]
    procedures = [dr.map_error_class(c).procedure for c in classes]
    owners_actions = {
        (dr.map_error_class(c).owner, dr.map_error_class(c).safe_action_fr) for c in classes
    }
    # Seven distinct procedures and seven distinct (owner, action) pairs.
    assert len(set(procedures)) == 7
    assert len(owners_actions) == 7


def test_unknown_or_unclassified_class_maps_to_none():
    for unknown in (None, "", "unclassified", "totally_unknown"):
        assert dr.map_error_class(unknown) is None


# ---------------------------------------------------------------------------
# Test harness for propose_recovery: stub the 36.12 composition + operations prepare.
# ---------------------------------------------------------------------------


def _stub_diagnosis(monkeypatch, *, diagnosis, target):
    """Monkeypatch _collect_events / _diagnose / _load_recovery_target for one call."""
    from core import datastream_diagnosis

    monkeypatch.setattr(datastream_diagnosis, "_collect_events", lambda conn, ds: ([], {}))
    monkeypatch.setattr(datastream_diagnosis, "_diagnose", lambda events: diagnosis)
    monkeypatch.setattr(dr, "_load_recovery_target", lambda conn, ds: target)


def _ok_diagnosis():
    return {
        "verdict": "ok",
        "error_class": None,
        "retryable": None,
        "recommended_action": None,
        "attempts": 0,
        "affected_interval": None,
        "correlation": {},
        "source_kind": None,
    }


def _failure_diagnosis(error_class, **extra):
    base = {
        "verdict": "failure",
        "error_class": error_class,
        "retryable": False,
        "recommended_action": None,
        "attempts": 2,
        "affected_interval": {"from": "2026-07-01", "to": "2026-07-05"},
        "correlation": {"pull_id": "pull_ABC", "execution_id": "dse_XYZ", "trace_id": "f" * 32},
        "source_kind": "pull",
    }
    base.update(extra)
    return base


# ---------------------------------------------------------------------------
# (c) write-effect recovery returns a prepared reference and executes NO write.
# ---------------------------------------------------------------------------


def test_write_effect_recovery_prepares_and_never_executes(monkeypatch):
    from core import operations_mcp

    target = {
        "org_id": "org_1",
        "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD",
    }
    _stub_diagnosis(monkeypatch, diagnosis=_failure_diagnosis("rate_limited"), target=target)

    # Stub ONLY the immutable-proposal insert seam; assert confirm/execute are untouched.
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: {
        "datastream_id": ds, "project_id": "proj_1", "org_id": "org_1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    })
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "cat-v1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate", lambda p, n: {"can_proceed": True})
    inserted = {}

    def fake_insert(conn, proposal, idem):
        inserted["proposal"] = proposal
        inserted["idem"] = idem
        return "prep_NEW"

    monkeypatch.setattr(operations_mcp, "_insert_preparation", fake_insert)

    # execute_operation / commit_publication MUST NOT be called by the bridge.
    boom = MagicMock(side_effect=AssertionError("bridge must not execute a write"))
    monkeypatch.setattr(operations_mcp, "execute_operation", boom, raising=False)

    proposal = dr.propose_recovery(
        MagicMock(), datastream_id="ds_1", project_id=None, actor="user-1"
    )

    assert proposal["status"] == "proposed"
    assert proposal["procedure"] == "retry"
    assert proposal["write_effect"] is True
    prepared = proposal["prepared_operation"]
    assert prepared["preparation_id"] == "prep_NEW"
    assert prepared["executed"] is False
    assert prepared["confirm_via"] == "confirm_datastream_recovery"
    # The immutable proposal records the requesting actor + never publishes.
    assert inserted["proposal"]["actor"] == "user-1"
    assert inserted["proposal"]["impact"]["touches_published_pointer"] is False
    boom.assert_not_called()


def test_refetch_recovery_bounds_the_affected_interval(monkeypatch):
    from core import operations_mcp

    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=_failure_diagnosis(
        "partial_data", verdict="partial", source_kind="verification"), target=target)
    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: {
        "project_id": "proj_1", "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    })
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "cat-v1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate", lambda p, n: {"can_proceed": True})
    captured = {}
    monkeypatch.setattr(operations_mcp, "_insert_preparation",
                        lambda conn, proposal, idem: captured.setdefault("p", proposal) or "prep_R")

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["procedure"] == "refetch"
    assert captured["p"]["interval"] == {"from": "2026-07-01", "to": "2026-07-05"}


def test_mapping_drift_routes_to_governance_not_operations(monkeypatch):
    target = {"org_id": "org_1", "mapping_version_id": "m7",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=_failure_diagnosis(
        "mapping_drift", recovery_class="mapping_drift"), target=target)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["procedure"] == "replace"
    prepared = proposal["prepared_operation"]
    assert prepared["route"] == dr._ROUTE_GOVERNANCE
    assert prepared["propose_via"] == "propose_mapping_correction"  # Story 36.17
    assert prepared["current_mapping_version_id"] == "m7"
    assert prepared["executed"] is False


def test_human_action_recovery_prepares_no_write(monkeypatch):
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=_failure_diagnosis("auth_revoked"), target=target)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["procedure"] == "reconnect"
    assert proposal["write_effect"] is False
    assert proposal["owner"] == dr._OWNER_CREDENTIAL
    assert proposal["prepared_operation"] is None


# ---------------------------------------------------------------------------
# (d) unknown / contradictory diagnosis -> STOP + bounded Support handoff.
# ---------------------------------------------------------------------------


def test_unknown_diagnosis_stops_and_offers_bounded_support_handoff(monkeypatch):
    # A failure verdict with no mappable class = unknown/contradictory.
    diagnosis = _failure_diagnosis("unclassified")
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=diagnosis, target=target)
    # No bound setup task -> the bridge offers (no bearer minted) but still STOPS.
    monkeypatch.setattr(dr, "_resolve_support_task_id", lambda conn, ds: None)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["status"] == "stopped_unknown_diagnosis"
    assert proposal["procedure"] is None
    assert proposal["owner"] == "support"
    handoff = proposal["support_handoff"]
    assert handoff["kind"] == "support_handoff"
    assert handoff["prepared"] is False
    # Bounded: only the sanitized correlation IDs ride along.
    assert handoff["correlation_ids"] == {
        "pull_id": "pull_ABC", "execution_id": "dse_XYZ", "trace_id": "f" * 32
    }


def test_unknown_diagnosis_with_bound_task_mints_purpose_scoped_handoff(monkeypatch):
    from core import setup_responsibilities

    diagnosis = _failure_diagnosis("unclassified")
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=diagnosis, target=target)
    monkeypatch.setattr(dr, "_resolve_support_task_id", lambda conn, ds: "setuptask_1")

    fake_result = type("H", (), {
        "handoff_id": "handoff_1", "state": "created",
        "expires_at": "2026-07-23T00:00:00+00:00", "operation_id": "op_1",
    })()
    captured = {}

    def fake_prepare(conn, **kwargs):
        captured.update(kwargs)
        return fake_result

    monkeypatch.setattr(setup_responsibilities, "prepare_handoff", fake_prepare)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    handoff = proposal["support_handoff"]
    assert handoff["prepared"] is True
    assert handoff["handoff_id"] == "handoff_1"
    # Purpose-scoped + minimal: the handoff is bound to the resolved task, expiry set.
    assert captured["task_id"] == "setuptask_1"
    assert captured["expires_in_hours"] == 24


# ---------------------------------------------------------------------------
# (e) original op IDs + last-known-good publication always visible.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_class", ["rate_limited", "auth_revoked", "unclassified"])
def test_original_ids_and_last_known_good_always_visible(monkeypatch, error_class):
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=_failure_diagnosis(error_class), target=target)
    # Make any write-effect prepare a no-op reference so we only assert the tail.
    monkeypatch.setattr(dr, "_prepare_via_operations",
                        lambda *a, **k: {"preparation_id": "prep_X", "executed": False})
    monkeypatch.setattr(dr, "_resolve_support_task_id", lambda conn, ds: None)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    # AC5: whatever the branch, the original correlation IDs + last-known-good survive.
    assert proposal["original_operation_ids"] == {
        "pull_id": "pull_ABC", "execution_id": "dse_XYZ", "trace_id": "f" * 32
    }
    assert proposal["last_known_good_execution_id"] == "dse_GOOD"


def test_healthy_datastream_needs_no_recovery(monkeypatch):
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=_ok_diagnosis(), target=target)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["status"] == "no_recovery_needed"
    assert proposal["procedure"] is None
    assert proposal["last_known_good_execution_id"] == "dse_GOOD"


# ---------------------------------------------------------------------------
# (f) no raw provider evidence leaks into the output.
# ---------------------------------------------------------------------------


def test_no_raw_provider_evidence_in_output(monkeypatch):
    # Even if a diagnosis (defensively) carried raw-looking keys, propose_recovery only
    # copies the sanitized allowlist fields -- never a payload/error_detail.
    diagnosis = _failure_diagnosis("rate_limited")
    diagnosis["error_detail"] = "PROVIDER SECRET 500 stacktrace ..."  # must NOT appear
    diagnosis["provider_payload"] = {"token": "abc"}  # must NOT appear
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _stub_diagnosis(monkeypatch, diagnosis=diagnosis, target=target)
    monkeypatch.setattr(dr, "_prepare_via_operations",
                        lambda *a, **k: {"preparation_id": "prep_X", "executed": False})

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    blob = repr(proposal)
    assert "PROVIDER SECRET" not in blob
    assert "stacktrace" not in blob
    assert "token" not in blob
    assert "provider_payload" not in blob


# ---------------------------------------------------------------------------
# REACHABILITY (the 36.16 gap fix): drive the REAL 36.12 pipeline end-to-end.
#
# The rest of this file monkeypatches _collect_events / _diagnose with synthetic
# dicts. THESE tests do NOT: they feed fixture DB rows through the REAL
# datastream_diagnosis._collect_events + _diagnose (only the row-fetch seam is
# stubbed), then assert propose_recovery reaches refetch / reconcile / reprocess /
# replace. This proves partial_data / uncertain_publication / dq_failure /
# mapping_drift are now PRODUCEABLE by the pipeline -- not just injectable in tests.
# ---------------------------------------------------------------------------


def _install_real_pipeline(monkeypatch, *, rows_by_table, target):
    """Wire the REAL _collect_events/_diagnose over fixture rows + a recovery target.

    Only ``_resolve_scope`` and ``_fetch_rows`` (the DB seams) are stubbed; the
    sanitizers, ``_collect_events`` composition and ``_diagnose`` aggregation all run
    for real. ``rows_by_table`` maps a table name to the list of dict rows the fetch
    should return for that source.
    """
    from core import datastream_diagnosis as dd

    monkeypatch.setattr(dd, "_resolve_scope", lambda conn, ds: ("proj_1", "conn_1"))

    def fake_fetch(conn, table, sql, params):
        return list(rows_by_table.get(table, []))

    monkeypatch.setattr(dd, "_fetch_rows", fake_fetch)
    # Recovery target read is a separate seam in datastream_recovery.
    monkeypatch.setattr(dr, "_load_recovery_target", lambda conn, ds: target)


def _stub_operations_prepare(monkeypatch, *, captured):
    """Stub ONLY the operations PREPARE insert seam; assert confirm/execute untouched."""
    from core import operations_mcp

    monkeypatch.setattr(operations_mcp, "_load_target", lambda conn, ds: {
        "datastream_id": ds, "project_id": "proj_1", "org_id": "org_1",
        "plan_version_id": "p1", "mapping_version_id": "m1",
        "current_published_execution_id": "dse_GOOD", "platform": "opaque",
        "active_execution_id": None,
    })
    monkeypatch.setattr(operations_mcp, "_current_policy_version", lambda: "cat-v1")
    monkeypatch.setattr(operations_mcp, "_quota_estimate", lambda p, n: {"can_proceed": True})

    def fake_insert(conn, proposal, idem):
        captured["proposal"] = proposal
        return "prep_REAL"

    monkeypatch.setattr(operations_mcp, "_insert_preparation", fake_insert)
    boom = MagicMock(side_effect=AssertionError("bridge must not execute a write"))
    monkeypatch.setattr(operations_mcp, "execute_operation", boom, raising=False)


def test_real_pipeline_partial_verification_reaches_refetch(monkeypatch):
    # (a) A partial/empty verification produced by the REAL pipeline -> refetch.
    rows = {
        "pull_jobs": [{
            "id": "job_1", "pull_id": "pull_1",
            "date_from": "2026-07-01", "date_to": "2026-07-05",
            "state": "done", "error_detail": None, "attempt_count": 1,
            "enqueued_at": "2026-07-05T09:00:00Z", "completed_at": "2026-07-05T10:00:00Z",
        }],
        "pull_verifications": [{
            "pull_id": "pull_1", "expected_rows": 1000, "actual_rows": 30,
            "verdict": "partial", "verified_at": "2026-07-05T10:05:00Z",
            "date_from": "2026-07-01", "date_to": "2026-07-05",
        }],
    }
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _install_real_pipeline(monkeypatch, rows_by_table=rows, target=target)
    captured: dict = {}
    _stub_operations_prepare(monkeypatch, captured=captured)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["status"] == "proposed"
    assert proposal["recovery_class"] == "partial_data"
    assert proposal["procedure"] == "refetch"
    assert proposal["owner"] == dr._OWNER_OPERATOR
    prepared = proposal["prepared_operation"]
    assert prepared["operations_kind"] == "refetch"
    assert prepared["preparation_id"] == "prep_REAL"
    assert prepared["executed"] is False
    # The refetch is bounded to the verification's pull window (from the real pipeline).
    assert captured["proposal"]["interval"] == {"from": "2026-07-01", "to": "2026-07-05"}


def test_real_pipeline_uncertain_publication_reaches_reconcile(monkeypatch):
    # (b) A stuck 'publishing' execution produced by the REAL pipeline -> reconcile.
    rows = {
        "datastream_executions": [{
            "id": "dse_" + "A" * 26, "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_1", "state": "publishing", "row_count": 500,
            "error_code": None, "state_changed_at": "2026-07-06T10:00:00Z",
        }],
    }
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _install_real_pipeline(monkeypatch, rows_by_table=rows, target=target)
    captured: dict = {}
    _stub_operations_prepare(monkeypatch, captured=captured)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["recovery_class"] == "uncertain_publication"
    assert proposal["procedure"] == "reconcile"
    assert proposal["owner"] == dr._OWNER_OPERATOR
    prepared = proposal["prepared_operation"]
    assert prepared["operations_kind"] == "reconcile"
    assert prepared["preparation_id"] == "prep_REAL"
    # reconcile targets the uncertain execution by its correlation id (from the pipeline).
    assert captured["proposal"]["interval"] == {"execution_id": "dse_" + "A" * 26}


def test_real_pipeline_dq_gate_failure_reaches_reprocess(monkeypatch):
    # (c) A failed execution carrying a typed blocking DQ gate code -> reprocess.
    rows = {
        "datastream_executions": [{
            "id": "dse_" + "B" * 26, "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_1", "state": "failed", "row_count": 0,
            "error_code": "dq_gate_failed",
            # error_detail is provider-adjacent free text -- must NEVER surface.
            "error_detail": "gate blew up: SELECT * FROM secret; token=abc",
            "state_changed_at": "2026-07-06T11:00:00Z",
        }],
    }
    target = {"org_id": "org_1", "mapping_version_id": "m1",
              "current_published_execution_id": "dse_GOOD"}
    _install_real_pipeline(monkeypatch, rows_by_table=rows, target=target)
    captured: dict = {}
    _stub_operations_prepare(monkeypatch, captured=captured)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["recovery_class"] == "dq_failure"
    assert proposal["procedure"] == "reprocess"
    assert proposal["owner"] == dr._OWNER_OPERATOR
    # reprocess routes as an operations "retry" candidate (never a live-pointer change).
    prepared = proposal["prepared_operation"]
    assert prepared["operations_kind"] == "retry"
    assert prepared["executed"] is False
    # No raw provider evidence leaks through the real pipeline into the proposal.
    blob = repr(proposal)
    assert "secret" not in blob.lower()
    assert "token=abc" not in blob


def test_real_pipeline_mapping_drift_reaches_governance_replace(monkeypatch):
    # (d) A failed execution carrying a typed schema/mapping-hash gate -> replace.
    rows = {
        "datastream_executions": [{
            "id": "dse_" + "C" * 26, "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_9", "state": "failed", "row_count": 100,
            "error_code": "schema_hash_mismatch", "error_detail": None,
            "state_changed_at": "2026-07-06T12:00:00Z",
        }],
    }
    target = {"org_id": "org_1", "mapping_version_id": "dmap_9",
              "current_published_execution_id": "dse_GOOD"}
    _install_real_pipeline(monkeypatch, rows_by_table=rows, target=target)

    proposal = dr.propose_recovery(MagicMock(), datastream_id="ds_1", project_id=None, actor="u")
    assert proposal["recovery_class"] == "mapping_drift"
    assert proposal["procedure"] == "replace"
    assert proposal["owner"] == dr._OWNER_DATA
    prepared = proposal["prepared_operation"]
    assert prepared["route"] == dr._ROUTE_GOVERNANCE
    assert prepared["propose_via"] == "propose_mapping_correction"
    assert prepared["executed"] is False


# ---------------------------------------------------------------------------
# recovery_mcp registration: exactly one operations/read tool, no contradiction.
# ---------------------------------------------------------------------------


def test_recovery_mcp_registers_one_operations_read_tool():
    from core import mcp_profiles, recovery_mcp

    mcp_profiles.reset_registry_for_tests()
    try:
        recorder = MagicMock()
        recorder.tool = MagicMock()
        recovery_mcp.register(recorder)
        decls = {d.name: d for d in mcp_profiles.registered_declarations()}
        assert set(decls) == {"propose_datastream_recovery"}
        d = decls["propose_datastream_recovery"]
        assert d.profile == "operations"
        assert d.effect == "read"
        assert d.confirmation_mode == "none"
        # Boot validation accepts it (no insights/write or read/confirmation contradiction).
        validated = {x.name for x in mcp_profiles.validate_catalog()}
        assert "propose_datastream_recovery" in validated
    finally:
        mcp_profiles.reset_registry_for_tests()
