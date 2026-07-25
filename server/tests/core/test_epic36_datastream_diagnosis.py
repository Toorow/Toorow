"""Story 36.12 sanitized pull history and diagnosis tests (offline).

Proves the E36-NFR01 allowlist serializer, E36-NFR06 bounded/paginated timeline,
existence-hiding scope denial, canonical diagnosis output and the audit-committed-
first Support disclosure -- all with MagicMock stand-ins (no live Postgres), the
same pattern as test_epic36_operations.py.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from core import datastream_diagnosis as dd

# A hostile error_detail: a fake provider payload carrying a token, SQL, an email
# and a stack trace. NONE of these raw strings may ever appear in the output.
_TOKEN = "ya29.SECRETACCESSTOKEN_should_never_leak"
_SQL = "SELECT * FROM app.raw WHERE api_key = 'shh'"
_EMAIL = "victim@example.com"
_STACK = 'Traceback (most recent call last): File "x.py", line 1'
_HOSTILE_ERROR_DETAIL = json.dumps(
    {
        "error_class": "auth_expired",
        "user_action": "reconnect",
        "provider_payload": {
            "access_token": _TOKEN,
            "sql": _SQL,
            "email": _EMAIL,
            "stack": _STACK,
            "message": "invalid_grant: token expired",
        },
    }
)


def _tool_error_code(exc) -> str:
    """Extract the {code} carried by a ToolError raised by the module."""
    return json.loads(str(exc.args[0]))["code"]


# ---------------------------------------------------------------------------
# (a) Redaction proof: a hostile error_detail never surfaces raw.
# ---------------------------------------------------------------------------


def test_hostile_error_detail_is_redacted_to_canonical_class_only():
    event = dd._sanitize_pull_event(
        {
            "id": "job_ABC",
            "pull_id": "pull_XYZ",
            "date_from": "2026-07-01",
            "date_to": "2026-07-02",
            "state": "failed",
            "error_detail": _HOSTILE_ERROR_DETAIL,
            "attempt_count": 3,
            "completed_at": "2026-07-02T10:00:00Z",
            "enqueued_at": "2026-07-02T09:00:00Z",
        }
    )
    blob = json.dumps(event)
    # None of the raw secret material leaks.
    for raw in (_TOKEN, _SQL, _EMAIL, _STACK, "invalid_grant", "provider_payload"):
        assert raw not in blob, raw
    # Only the canonical class / retryability / action appear.
    assert event["error_class"] == "auth_expired"
    assert event["retryable"] is False
    assert event["recommended_action"] == "reconnect"
    assert event["attempts"] == 3
    assert event["affected_interval"] == {"from": "2026-07-01", "to": "2026-07-02"}
    assert event["correlation"]["pull_id"] == "pull_XYZ"


def test_provider_code_smuggled_as_error_class_is_coerced_to_unclassified():
    """A non-canonical class string (e.g. a provider code) never surfaces as-is."""
    detail = json.dumps({"error_class": "PROVIDER_CODE_190", "user_action": "call_support"})
    ec, ua = dd._canonical_error(detail)
    assert ec == "unclassified"  # coerced, never the raw provider code
    assert ua is None  # non-allowlisted action dropped


def test_legacy_plaintext_error_detail_is_not_echoed():
    ec, ua = dd._canonical_error("upstream boom: " + _TOKEN)
    assert ec == "unclassified"
    assert ua is None


def test_execution_and_audit_events_drop_untrusted_fields():
    exec_ev = dd._sanitize_execution_event(
        {
            "id": "dse_" + "A" * 26,
            "plan_version_id": "dsp_1",
            "mapping_version_id": "dmap_1",
            "state": "failed",
            "row_count": 5,
            "error_detail": _SQL,
            "error_code": _TOKEN,
            "state_changed_at": "2026-07-02T10:00:00Z",
        }
    )
    blob = json.dumps(exec_ev)
    assert _SQL not in blob and _TOKEN not in blob
    assert "error_detail" not in exec_ev and "error_code" not in exec_ev
    assert exec_ev["correlation"]["execution_id"] == "dse_" + "A" * 26

    audit_ev = dd._sanitize_audit_event(
        {
            "action": "pull.triggered",
            "outcome": "failed",
            "operation_id": "op_1",
            "trace_id": "a" * 32,
            "created_at": "2026-07-02T10:00:00Z",
            "identity": _EMAIL,
            "metadata": {"secret": _TOKEN},
        }
    )
    ablob = json.dumps(audit_ev)
    assert _EMAIL not in ablob and _TOKEN not in ablob
    assert audit_ev["correlation"]["trace_id"] == "a" * 32


# ---------------------------------------------------------------------------
# (b) Scope denial existence-hides (not_found).
# ---------------------------------------------------------------------------


def _install_guard(monkeypatch, *, allowed: bool, org_id: str | None = "org-1", raises=False):
    """Patch the strict access seam + get_connection used by the guard."""
    from core import project_access

    @contextmanager
    def fake_conn():
        yield MagicMock()

    monkeypatch.setattr(dd, "_identity", lambda: "user-1")

    import core.db as core_db

    monkeypatch.setattr(core_db, "get_connection", fake_conn)

    def fake_resolve(identity, conn, **kwargs):
        if raises:
            raise RuntimeError("db down")
        return project_access.AccessDecision(
            allowed, "explicit_grant" if allowed else "not_found", "view", org_id
        )

    monkeypatch.setattr(project_access, "resolve_strict_resource_access", fake_resolve)


def test_scope_denial_existence_hides_not_found(monkeypatch):
    _install_guard(monkeypatch, allowed=False)
    with pytest.raises(Exception) as exc:
        # Call the guard directly (register wires it into both tools).
        dd._guard_datastream_read("ds_forbidden", "user-1")
    assert _tool_error_code(exc.value) == "not_found"


def test_guard_failure_fails_closed_to_not_found(monkeypatch):
    _install_guard(monkeypatch, allowed=True, raises=True)
    with pytest.raises(Exception) as exc:
        dd._guard_datastream_read("ds_1", "user-1")
    assert _tool_error_code(exc.value) == "not_found"


def test_guard_allows_and_returns_org(monkeypatch):
    _install_guard(monkeypatch, allowed=True, org_id="org-42")
    assert dd._guard_datastream_read("ds_1", "user-1") == "org-42"


# ---------------------------------------------------------------------------
# (c) Timeline is bounded/paginated (respects the page cap).
# ---------------------------------------------------------------------------


def test_timeline_is_bounded_to_max_page_size():
    events = [
        {"kind": "pull", "at": f"2026-07-{i:02d}T00:00:00Z"} for i in range(1, 60)
    ]
    # Request way over the cap -> clamped to _MAX_PAGE_SIZE.
    page_size = dd._bound_page_size(500)
    assert page_size == dd._MAX_PAGE_SIZE
    timeline = dd._assemble_timeline(events, cursor=0, page_size=page_size)
    assert len(timeline["events"]) == dd._MAX_PAGE_SIZE
    assert timeline["total"] == 59
    assert timeline["next_cursor"] == dd._MAX_PAGE_SIZE
    # Newest first.
    assert timeline["events"][0]["at"] > timeline["events"][-1]["at"]


def test_timeline_pagination_walks_the_cursor():
    events = [{"kind": "pull", "at": f"2026-07-{i:02d}T00:00:00Z"} for i in range(1, 12)]
    first = dd._assemble_timeline(events, cursor=0, page_size=5)
    assert len(first["events"]) == 5 and first["next_cursor"] == 5
    second = dd._assemble_timeline(events, cursor=first["next_cursor"], page_size=5)
    assert len(second["events"]) == 5 and second["next_cursor"] == 10
    last = dd._assemble_timeline(events, cursor=second["next_cursor"], page_size=5)
    assert len(last["events"]) == 1 and last["next_cursor"] is None


def test_bound_helpers_clamp_bad_input():
    assert dd._bound_page_size(0) == 1
    assert dd._bound_page_size(-3) == 1
    assert dd._bound_page_size("nan") == dd._MAX_PAGE_SIZE
    assert dd._bound_offset(-5) == 0
    assert dd._bound_offset("x") == 0


# ---------------------------------------------------------------------------
# (d) Diagnose output carries the canonical allowlist result.
# ---------------------------------------------------------------------------


def test_diagnose_surfaces_canonical_class_retryability_action_and_ids():
    events = [
        {
            "kind": "verification",
            "at": "2026-07-01T00:00:00Z",
            "state": "ok",
        },
        {
            "kind": "pull",
            "at": "2026-07-02T00:00:00Z",
            "error_class": "auth_expired",
            "retryable": False,
            "recommended_action": "reconnect",
            "attempts": 2,
            "affected_interval": {"from": "2026-07-01", "to": "2026-07-02"},
            "correlation": {"pull_id": "pull_1", "job_id": "job_1"},
        },
    ]
    diag = dd._diagnose(events)
    assert diag["verdict"] == "failure"
    assert diag["error_class"] == "auth_expired"
    assert diag["retryable"] is False
    assert diag["recommended_action"] == "reconnect"
    assert diag["attempts"] == 2
    assert diag["affected_interval"] == {"from": "2026-07-01", "to": "2026-07-02"}
    assert diag["correlation"]["pull_id"] == "pull_1"

    # Compact summary carries mandatory bounded evidence, not a deep-link-only answer.
    summary = dd._diagnose_summary(diag, {"datastream_id": "ds_1"})
    assert "auth_expired" in summary
    assert "reconnect" in summary
    assert "pull_id=pull_1" in summary
    assert len(summary.splitlines()) <= 30
    for raw in (_TOKEN, _SQL, _EMAIL):
        assert raw not in summary


def test_diagnose_healthy_when_no_failure_evidence():
    diag = dd._diagnose([{"kind": "verification", "at": "2026-07-01T00:00:00Z", "state": "ok"}])
    assert diag["verdict"] == "ok"
    assert diag["error_class"] is None
    assert diag["recovery_class"] is None


# ---------------------------------------------------------------------------
# (d') Recovery-class reachability: typed verification / execution evidence now
# surfaces a canonical, allowlisted recovery_class (Story 36.16 gap fix). These are
# the classes datastream_recovery maps to refetch / reconcile / reprocess / replace.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("verdict", ["partial", "empty"])
def test_partial_verification_surfaces_partial_data_recovery_class(verdict):
    ev = dd._sanitize_verification_event(
        {
            "pull_id": "pull_1",
            "expected_rows": 1000,
            "actual_rows": 0 if verdict == "empty" else 30,
            "verdict": verdict,
            "verified_at": "2026-07-05T10:05:00Z",
            "date_from": "2026-07-01",
            "date_to": "2026-07-05",
        }
    )
    assert ev["recovery_class"] == "partial_data"
    # The pull window rides along as the bounded refetch interval.
    assert ev["affected_interval"] == {"from": "2026-07-01", "to": "2026-07-05"}


def test_ok_verification_carries_no_recovery_class():
    ev = dd._sanitize_verification_event(
        {"pull_id": "pull_1", "expected_rows": 10, "actual_rows": 10,
         "verdict": "ok", "verified_at": "2026-07-05T10:05:00Z"}
    )
    assert ev["recovery_class"] is None


def test_stuck_publishing_execution_surfaces_uncertain_publication():
    ev = dd._sanitize_execution_event(
        {"id": "dse_" + "A" * 26, "plan_version_id": "dsp_1", "mapping_version_id": "dmap_1",
         "state": "publishing", "row_count": 500, "error_code": None,
         "state_changed_at": "2026-07-06T10:00:00Z"}
    )
    assert ev["recovery_class"] == "uncertain_publication"


@pytest.mark.parametrize(
    "error_code,expected",
    [
        ("mapping_drift", "mapping_drift"),
        ("schema_hash_mismatch", "mapping_drift"),
        ("dq_gate_failed", "dq_failure"),
        ("empty_candidate", "dq_failure"),
        ("row_count_delta_exceeded", "dq_failure"),
        ("content_hash_mismatch", "dq_failure"),
        ("reconciliation_inconclusive", "uncertain_publication"),
    ],
)
def test_typed_execution_error_code_maps_to_recovery_class(error_code, expected):
    ev = dd._sanitize_execution_event(
        {"id": "dse_" + "B" * 26, "plan_version_id": "dsp_1", "mapping_version_id": "dmap_1",
         "state": "failed", "row_count": 0, "error_code": error_code,
         # error_detail (free provider text) must NEVER surface.
         "error_detail": _SQL + " " + _TOKEN,
         "state_changed_at": "2026-07-06T11:00:00Z"}
    )
    assert ev["recovery_class"] == expected
    blob = json.dumps(ev)
    assert _SQL not in blob and _TOKEN not in blob
    assert "error_detail" not in ev and "error_code" not in ev


def test_hostile_string_in_typed_columns_never_becomes_a_recovery_class():
    """recovery_class is a CLOSED enum -- a hostile provider string is dropped."""
    # A hostile string smuggled into the verdict enum column: dropped, no class.
    ver = dd._sanitize_verification_event(
        {"pull_id": "pull_1", "expected_rows": 1, "actual_rows": 0,
         "verdict": "DROP TABLE; " + _TOKEN, "verified_at": "2026-07-05T10:05:00Z"}
    )
    assert ver["recovery_class"] is None
    assert _TOKEN not in json.dumps(ver)
    # A hostile string smuggled into the error_code column: not in the map -> dropped.
    ex = dd._sanitize_execution_event(
        {"id": "dse_" + "D" * 26, "plan_version_id": "dsp_1", "mapping_version_id": "dmap_1",
         "state": "failed", "row_count": 0, "error_code": "PROVIDER_500 " + _SQL,
         "state_changed_at": "2026-07-06T11:00:00Z"}
    )
    assert ex["recovery_class"] is None
    assert _SQL not in json.dumps(ex)
    # Direct guard: only the four canonical classes survive.
    assert dd._safe_recovery_class("partial_data") == "partial_data"
    assert dd._safe_recovery_class("uncertain_publication") == "uncertain_publication"
    assert dd._safe_recovery_class("dq_failure") == "dq_failure"
    assert dd._safe_recovery_class("mapping_drift") == "mapping_drift"
    for bad in (None, "", "ok", "failure", "auth_expired", _TOKEN):
        assert dd._safe_recovery_class(bad) is None


def test_diagnose_aggregates_recovery_class_without_error_class():
    """A recovery-class event yields a 'failure' verdict carrying recovery_class."""
    events = [
        {"kind": "verification", "at": "2026-07-05T10:05:00Z", "state": "partial",
         "recovery_class": "partial_data",
         "affected_interval": {"from": "2026-07-01", "to": "2026-07-05"},
         "correlation": {"pull_id": "pull_1"}},
    ]
    diag = dd._diagnose(events)
    assert diag["verdict"] == "failure"
    assert diag["error_class"] is None
    assert diag["recovery_class"] == "partial_data"
    assert diag["source_kind"] == "verification"
    assert diag["affected_interval"] == {"from": "2026-07-01", "to": "2026-07-05"}
    # The compact summary names the recovery class (bounded, redacted).
    summary = dd._diagnose_summary(diag, {"datastream_id": "ds_1"})
    assert "partial_data" in summary
    assert len(summary.splitlines()) <= 30


def test_diagnose_hard_error_class_dominates_a_recovery_class():
    """A hard auth/provider error class takes precedence over a soft recovery class."""
    events = [
        {"kind": "execution", "at": "2026-07-05T09:00:00Z", "state": "failed",
         "recovery_class": "dq_failure", "correlation": {"execution_id": "dse_x"}},
        {"kind": "pull", "at": "2026-07-05T10:00:00Z", "error_class": "auth_expired",
         "retryable": False, "recommended_action": "reconnect",
         "correlation": {"pull_id": "pull_1"}},
    ]
    diag = dd._diagnose(events)
    assert diag["error_class"] == "auth_expired"
    assert diag["recovery_class"] is None  # hard class wins; no recovery hint set


# ---------------------------------------------------------------------------
# (e) Support disclosure requires audit committed first + returns audit_event_id.
# ---------------------------------------------------------------------------


def test_support_disclosure_commits_audit_first_and_returns_audit_event_id(monkeypatch):
    from core import operations

    order: list[str] = []

    committed = MagicMock()
    committed.commit.side_effect = lambda: order.append("commit")

    @contextmanager
    def fake_conn():
        yield committed

    import core.db as core_db

    monkeypatch.setattr(core_db, "get_connection", fake_conn)

    def fake_prepare(conn, **kwargs):
        order.append("prepare")
        # audit-committed-first invariant is exercised by asserting order below.
        return operations.OperationResult(
            "op-support", "succeeded", {}, "audit-support-1", "outbox-1", False
        )

    def fake_confirm(conn, audit_event_id):
        order.append("confirm")
        assert "commit" in order, "audit must commit before it is confirmed"
        return operations.CommittedAuditReference(audit_event_id)

    monkeypatch.setattr(operations, "prepare_support_disclosure", fake_prepare)
    monkeypatch.setattr(operations, "confirm_support_audit_committed", fake_confirm)

    diagnosis = {
        "error_class": "auth_expired",
        "correlation": {"pull_id": "pull_1", "trace_id": "a" * 32},
    }
    response = dd._support_disclose(
        identity="support-1",
        org_id="org-1",
        datastream_id="ds_1",
        diagnosis=diagnosis,
    )
    assert response["audit_event_id"] == "audit-support-1"
    # Order proves: prepare -> commit -> confirm (audit committed before disclosure).
    assert order == ["prepare", "commit", "confirm"]
    # Bounded redacted evidence only -- no raw material.
    blob = json.dumps(response)
    for raw in (_TOKEN, _SQL, _EMAIL, _STACK):
        assert raw not in blob
    assert response["evidence"]["class"] == "auth_expired"


# ---------------------------------------------------------------------------
# Composition smoke: _collect_events assembles from the existing read models.
# ---------------------------------------------------------------------------


def test_collect_events_composes_and_sanitizes_from_read_models(monkeypatch):
    calls = {"n": 0}

    def fake_resolve_scope(conn, ds):
        return "proj_1", "conn_1"

    def fake_fetch(conn, table, sql, params):
        calls["n"] += 1
        if table == "pull_jobs":
            return [
                {
                    "id": "job_1",
                    "pull_id": "pull_1",
                    "date_from": "2026-07-01",
                    "date_to": "2026-07-02",
                    "state": "failed",
                    "error_detail": _HOSTILE_ERROR_DETAIL,
                    "attempt_count": 2,
                    "enqueued_at": "2026-07-02T09:00:00Z",
                    "completed_at": "2026-07-02T10:00:00Z",
                }
            ]
        if table == "connection_health":
            return [
                {
                    "status": "ok",
                    "last_checked_at": "2026-07-02T11:00:00Z",
                    "last_fetched_at": "2026-07-02T10:30:00Z",
                }
            ]
        return []

    monkeypatch.setattr(dd, "_resolve_scope", fake_resolve_scope)
    monkeypatch.setattr(dd, "_fetch_rows", fake_fetch)

    events, context = dd._collect_events(MagicMock(), "ds_1")
    blob = json.dumps({"events": events, "context": context})
    for raw in (_TOKEN, _SQL, _EMAIL, "provider_payload"):
        assert raw not in blob
    assert context["connection_health"] == "ok"
    assert context["freshness_last_pull_at"] == "2026-07-02T10:30:00Z"
    # The hostile pull became a canonical auth_expired event.
    pull_events = [e for e in events if e["kind"] == "pull"]
    assert pull_events and pull_events[0]["error_class"] == "auth_expired"


def test_wiring_snippet_matches_module_register():
    """The exact main.py wiring imports register from this module."""
    assert hasattr(dd, "register")
