"""Authoritative Project Overview derivation tests (Story 43.15)."""

from __future__ import annotations

from core.project_overview import (
    _query_daily_insights,
    _query_latest_test,
    _query_project_evidence,
    authorize_overview_project,
    derive_project_trust,
)


def _row(
    datastream_id: str,
    *,
    coverage_end_exclusive: str | None = "2026-07-23T00:00:00+00:00",
    latest_state: str = "done",
    latest_verdict: str | None = "ok",
    connection_status: str | None = "ok",
    published: bool = True,
    active_state: str | None = None,
    enabled: bool = True,
    coverage_state: str | None = "covered",
    coverage_execution_id: str | None = None,
    coverage_verdict: str | None = "ok",
    source_kind: str = "connector_pull",
) -> dict:
    current_execution_id = f"exec-{datastream_id}" if published else None
    return {
        "id": datastream_id,
        "name": f"Datastream {datastream_id}",
        "enabled": enabled,
        "source_kind": source_kind,
        "connection_ref_id": (
            f"conn-{datastream_id}" if source_kind == "connector_pull" else None
        ),
        "connection_status": connection_status,
        "connection_health_current": True,
        "connection_ref_status": "active",
        "connection_enabled": True,
        "account_state": "ready",
        "account_available": True,
        "account_exposed": True,
        "current_published_execution_id": current_execution_id,
        "publication_state": "published" if published else None,
        "published_at": "2026-07-22T00:00:00Z" if published else None,
        "coverage_interval_start": "2026-07-01T00:00:00Z",
        "coverage_end_exclusive": coverage_end_exclusive,
        "verified_at": "2026-07-22T00:00:00Z",
        "latest_pull_state": latest_state,
        "latest_pull_completed_at": "2026-07-23T00:00:00Z",
        "coverage_state": coverage_state,
        "coverage_execution_id": (
            coverage_execution_id
            if coverage_execution_id is not None
            else current_execution_id
        ),
        "coverage_verdict": coverage_verdict,
        "latest_verdict": latest_verdict,
        "active_state": active_state,
    }


def test_trusted_complete_through_uses_oldest_required_verified_interval():
    summary, attention, active_work = derive_project_trust(
        [
            _row("one", coverage_end_exclusive="2026-07-23T00:00:00Z"),
            _row("two", coverage_end_exclusive="2026-07-20T00:00:00Z"),
        ]
    )

    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-19"
    assert summary["verified_datastreams"] == 2
    assert attention == []
    assert active_work == []


def test_failed_candidate_keeps_last_known_good_and_requires_attention():
    summary, attention, _active_work = derive_project_trust(
        [_row("search", latest_state="failed", latest_verdict=None)]
    )

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == [
        {
            "datastream_id": "search",
            "name": "Datastream search",
            "reason": "latest_run_failed",
            "detail": (
                "The latest run failed; the verified current publication remains available."
            ),
            "target": "runs",
        }
    ]


def test_missing_evidence_is_unknown_without_completion_date():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "unknown",
                coverage_end_exclusive=None,
                connection_status=None,
                published=False,
            )
        ]
    )

    assert summary["published_trust"] == "unknown"
    assert summary["complete_through"] is None
    assert summary["verified_datastreams"] == 0
    assert attention[0]["reason"] == "evidence_missing"
    assert "current published execution" in attention[0]["detail"]
    assert "connection-health evidence" in attention[0]["detail"]


def test_empty_project_is_not_trusted():
    summary, attention, active_work = derive_project_trust([])

    assert summary["published_trust"] == "no_data"
    assert summary["active_datastreams"] == 0
    assert summary["complete_through"] is None
    assert attention == []
    assert active_work == []
    assert summary["no_data_reason"] == "empty_project"


def test_only_disabled_datastreams_are_not_described_as_an_empty_project():
    summary, attention, active_work = derive_project_trust(
        [_row("disabled", enabled=False)]
    )

    assert summary["published_trust"] == "no_data"
    assert summary["no_data_reason"] == "no_active_datastreams"
    assert summary["evidence_message"].startswith("No Datastream is active")
    assert attention == []
    assert active_work == []


def test_active_work_is_reported_from_persisted_execution_state():
    summary, _attention, active_work = derive_project_trust(
        [_row("one", active_state="validating")]
    )

    assert summary["active_work_count"] == 1
    assert active_work == [
        {
            "datastream_id": "one",
            "name": "Datastream one",
            "state": "validating",
            "target": "runs",
        }
    ]


def test_old_coverage_cannot_verify_the_current_publication():
    summary, attention, _active_work = derive_project_trust(
        [_row("mismatch", coverage_execution_id="exec-previous")]
    )

    assert summary["published_trust"] == "unknown"
    assert summary["complete_through"] is None
    assert summary["verified_datastreams"] == 0
    assert attention[0]["reason"] == "evidence_missing"
    assert "current publication" in attention[0]["detail"]


def test_degraded_coverage_is_attention_without_a_completion_date():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "degraded",
                coverage_state="degraded",
                coverage_verdict=None,
            )
        ]
    )

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] is None
    assert {item["reason"] for item in attention} == {
        "coverage_incomplete",
        "evidence_missing",
    }


def test_non_connector_uses_source_agnostic_coverage_evidence():
    summary, attention, _active_work = derive_project_trust(
        [_row("warehouse", source_kind="external_bq", coverage_verdict=None)]
    )

    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == []


def test_exclusive_coverage_end_reports_previous_complete_day():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "exclusive", coverage_end_exclusive="2026-07-23T00:00:00+00:00"
            )
        ]
    )
    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == []

def test_failed_coverage_candidate_preserves_current_publication_with_limitation():
    summary, attention, _active_work = derive_project_trust(
        [
            _row(
                "candidate",
                coverage_state="failed",
                coverage_execution_id="exec-candidate",
                coverage_end_exclusive=None,
                coverage_verdict=None,
                latest_state="failed",
                latest_verdict=None,
            )
        ]
    )

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] is None
    failure = next(item for item in attention if item["reason"] == "latest_run_failed")
    assert "current publication remains available" in failure["detail"]
    assert "coverage horizon cannot be verified" in failure["detail"]
    assert {item["target"] for item in attention} == {"runs"}


def test_failed_pull_older_than_current_publication_does_not_downgrade_trust():
    row = _row("ordered", latest_state="failed", latest_verdict=None)
    row["latest_pull_completed_at"] = "2026-07-20T00:00:00Z"
    row["published_at"] = "2026-07-22T00:00:00Z"

    summary, attention, _active_work = derive_project_trust([row])

    assert summary["published_trust"] == "trusted"
    assert summary["complete_through"] == "2026-07-22"
    assert attention == []


def test_stale_health_evidence_cannot_be_trusted():
    row = _row("stale-health")
    row["connection_health_current"] = False

    summary, attention, _active_work = derive_project_trust([row])

    assert summary["published_trust"] == "unknown"
    assert summary["complete_through"] is None
    assert attention[0]["target"] == "overview"
    assert "recent connection-health evidence" in attention[0]["detail"]


def test_revoked_account_keeps_published_horizon_but_requires_attention():
    row = _row("revoked")
    row["account_exposed"] = False

    summary, attention, _active_work = derive_project_trust([row])

    assert summary["published_trust"] == "attention"
    assert summary["complete_through"] == "2026-07-22"
    assert attention[0]["reason"] == "connection_unusable"
    assert attention[0]["target"] == "overview"

class _FakeTransaction:
    def __init__(self):
        self.rolled_back = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        self.rolled_back = exc_type is not None
        return False


class _FakeCursor:
    def __init__(self, *, rows=None, fail=False, capture=None):
        self.rows = rows or []
        self.fail = fail
        self.capture = capture
        self.description = []

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        return False

    def execute(self, sql, params):
        if self.capture is not None:
            self.capture.append((sql, params))
        if self.fail:
            raise RuntimeError("optional relation unavailable")

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class _FakeConnection:
    def __init__(self, *, rows=None, fail=False, capture=None):
        self.rows = rows
        self.fail = fail
        self.capture = capture
        self.transactions = []

    def cursor(self):
        return _FakeCursor(rows=self.rows, fail=self.fail, capture=self.capture)

    def transaction(self):
        tx = _FakeTransaction()
        self.transactions.append(tx)
        return tx


def test_project_evidence_uses_ratified_project_flux_topology():
    captured = []
    _query_project_evidence("project-linked", _FakeConnection(capture=captured))

    sql, params = captured[0]
    assert "FROM app.project_flux pf" in sql
    assert "pf.project_id = %s" in sql
    assert "ds.project_id = %s" not in sql
    assert params[1] == "project-linked"


def test_authenticated_overview_uses_strict_access_even_without_epic36_flag(
    monkeypatch,
):
    from types import SimpleNamespace

    from core import db, project_access

    events = []
    monkeypatch.setenv("TOOROW_AUTH_MODE", "static")
    monkeypatch.setenv("TOOROW_EPIC36_PRODUCTION_ENABLED", "false")
    monkeypatch.setattr(
        db,
        "set_local_access_context",
        lambda *_args, **kwargs: events.append(("context", kwargs)),
    )
    monkeypatch.setattr(
        project_access,
        "resolve_strict_resource_access",
        lambda *_args, **_kwargs: events.append(("strict", {}))
        or SimpleNamespace(allowed=True),
    )
    monkeypatch.setattr(
        project_access,
        "identity_has_project_access",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("legacy access must not run")
        ),
    )
    conn = _FakeConnection(rows=[(1,)])

    assert authorize_overview_project("person-1", "project-1", conn) is True
    assert events[0] == ("context", {"enforce_epic36": False})
    assert events[1][0] == "strict"


def test_optional_query_failure_rolls_back_its_savepoint():
    conn = _FakeConnection(fail=True)

    assert _query_latest_test("project-1", conn) == {
        "status": "unavailable",
        "value": None,
    }
    assert len(conn.transactions) == 1
    assert conn.transactions[0].rolled_back is True


def test_failed_latest_insight_run_does_not_render_stale_published_items():
    conn = _FakeConnection(rows=[("failed", None, None, None, None)])

    assert _query_daily_insights("project-1", conn) == {
        "status": "unavailable",
        "items": [],
    }
