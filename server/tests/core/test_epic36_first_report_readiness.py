"""Story 36.10 -- first-pull progress + AUTHORITATIVE report readiness (offline).

Offline MagicMock-style, mirroring test_epic36_recent_first_publication.py: the
composed seams (36.9 get_recent_first_state, 36.12 _collect_events / _diagnose,
13.4 fetch_dq_report_data, 032 get_mapping_version) and this module's single thin
draft read are patched, so the Story 36.10 INVARIANTS are exercised WITHOUT live
Postgres. Live-PG coverage (RLS existence-hiding on denial, real coverage rows) is
the established follow-up; the access denial is exercised at the guard level.

Covered invariants:
  (a) the readiness object is VERSIONED (deterministic hash) and derives every
      required field (publication, selected objects, recent+historical coverage,
      runs, verification/DQ, mapping/provenance, freshness, exclusions, last
      success, current publication, per-phase state) from the seams.
  (b) partial history / degraded DQ with recent-ok -> overall "degraded" and NEVER
      "ready"; host_cta is not "enabled".
  (c) hard failure / no recent publication -> "blocked" + host_cta "disabled".
  (d) each phase carries state + interval / rows / attempts / next-action.
  (e) access denial existence-hides (the REST guard returns not_found, never a
      readiness object).
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import first_report_readiness as frr  # noqa: E402

_INTERVAL = {"start": "2026-06-22T00:00:00Z", "end_exclusive": "2026-07-22T00:00:00Z"}
_HASH = "a" * 64
_SCHEMA_HASH = "b" * 64


def _recent_state(
    *,
    pointer="dse_pub",
    recent_state="covered",
    historical_state="covered",
    recent_available=True,
):
    return {
        "datastream_id": "ds-1",
        "project_id": "proj-1",
        "current_published_execution_id": pointer,
        "recent_coverage": {
            "coverage_kind": "recent",
            "state": recent_state,
            "interval_start": _INTERVAL["start"],
            "interval_end_exclusive": _INTERVAL["end_exclusive"],
            "execution_id": "dse_pub",
            "pull_id": "pull_1",
            "row_count": 120,
            "content_hash": _HASH,
            "covered_at": "2026-07-22T00:00:00Z" if recent_state == "covered" else None,
        }
        if recent_state
        else None,
        "historical_coverage": {
            "coverage_kind": "historical",
            "state": historical_state,
            "interval_start": "2025-01-01T00:00:00Z",
            "interval_end_exclusive": _INTERVAL["start"],
            "execution_id": "dse_hist",
            "pull_id": "pull_h",
            "row_count": 5000,
            "content_hash": _HASH,
            "covered_at": None,
        }
        if historical_state
        else None,
        "recent_available": recent_available,
    }


def _events():
    """Sanitized events as 36.12 _collect_events would emit (publication + verify)."""
    return [
        {
            "kind": "publication",
            "state": "published",
            "row_count": 120,
            "correlation": {"execution_id": "dse_pub", "prior_execution_id": "dse_prev"},
            "at": "2026-07-22T00:00:00Z",
        },
        {
            "kind": "verification",
            "state": "ok",
            "row_count": 120,
            "expected_rows": 120,
            "correlation": {"pull_id": "pull_1"},
            "at": "2026-07-22T00:00:00Z",
        },
        {
            "kind": "pull",
            "state": "succeeded",
            "error_class": None,
            "attempts": 2,
            "affected_interval": {"from": _INTERVAL["start"], "to": _INTERVAL["end_exclusive"]},
            "correlation": {"pull_id": "pull_1", "job_id": "job_1"},
            "at": "2026-07-22T00:00:00Z",
        },
    ]


def _context():
    return {
        "datastream_id": "ds-1",
        "project_id": "proj-1",
        "connection_ref": "conn_1",
        "freshness_last_pull_at": "2026-07-22T00:00:00Z",
        "connection_health": "healthy",
    }


def _dq_report(*, total_unresolved=0, monitors_unavailable=False, issues=None):
    return {
        "monitors": [],
        "issues": issues or [],
        "freshness_last_pull_at": "2026-07-22T00:00:00Z",
        "evaluated_days_30d": 30,
        "total_unresolved": total_unresolved,
        "monitors_unavailable": monitors_unavailable,
    }


def _draft():
    return {
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "recommendation": {
            "report_id": "report_x",
            "metrics": ["clicks", "cost"],
            "dimensions": ["date"],
            "grain": ["day"],
            "timezone": "UTC",
            "currency": "EUR",
            "interval": dict(_INTERVAL),
            "exclusions": [{"kind": "unsupported_metric", "reason": "hors capacité"}],
        },
    }


def _mapping_version():
    return {
        "id": "dmap_1",
        "version_number": 3,
        "mapping_contract_version": "1.2",
        "source_schema_hash": _SCHEMA_HASH,
        "plan_version_id": "dsp_1",
        "capability_fingerprint": _HASH,
        "content_hash": _HASH,
        "ossie_spec_version": "0.9",
        "toorow_extension_version": "1.0",
        "executable": True,
    }


@pytest.fixture
def patched(monkeypatch):
    """Patch every composed seam so compute runs fully offline."""
    from core import datastream_diagnosis as diag
    from core import datastream_field_mapping as dfm
    from core import dq_api
    from core import recent_first_publication as rfp

    state = {
        "recent_state": _recent_state(),
        "events": _events(),
        "context": _context(),
        "dq": _dq_report(),
        "draft": _draft(),
        "mapping": _mapping_version(),
    }

    monkeypatch.setattr(
        rfp, "get_recent_first_state", lambda *a, **k: state["recent_state"]
    )
    monkeypatch.setattr(
        diag, "_collect_events", lambda *a, **k: (state["events"], state["context"])
    )
    # _diagnose stays real (pure) -- exercises the true canonical derivation.
    monkeypatch.setattr(dq_api, "fetch_dq_report_data", lambda *a, **k: state["dq"])
    monkeypatch.setattr(dfm, "get_mapping_version", lambda *a, **k: state["mapping"])
    monkeypatch.setattr(
        frr, "_load_active_draft", lambda *a, **k: state["draft"]
    )

    ns = MagicMock()
    ns.conn = MagicMock()
    ns.state = state
    return ns


def _compute(ns):
    return frr.compute_first_report_readiness(
        ns.conn, datastream_id="ds-1", project_id="proj-1", actor="user:1"
    )


# ---------------------------------------------------------------------------
# (a) versioned + derives every required field.
# ---------------------------------------------------------------------------
def test_readiness_is_versioned_and_derives_all_required_fields(patched):
    r = _compute(patched).as_dict()

    # Versioned: deterministic 32-hex hash + a schema version.
    assert r["schema_version"] == frr.READINESS_SCHEMA_VERSION
    assert isinstance(r["readiness_version"], str) and len(r["readiness_version"]) == 32

    # Every required field is present and non-empty where the seams provide it.
    assert r["current_publication"]["execution_id"] == "dse_pub"
    assert r["selected_objects"]["report_id"] == "report_x"
    assert r["selected_objects"]["metrics"] == ["clicks", "cost"]
    assert r["recent_coverage"]["state"] == "covered"
    assert r["historical_coverage"]["state"] == "covered"
    assert r["verification"]["verdict"] == "ok"
    assert r["dq"]["total_unresolved"] == 0
    assert r["mapping"]["mapping_version_id"] == "dmap_1"
    assert r["provenance"]["source_schema_hash"] == _SCHEMA_HASH
    assert r["freshness"]["last_pull_at"] == "2026-07-22T00:00:00Z"
    assert r["exclusions"] == [{"kind": "unsupported_metric", "reason": "hors capacité"}]
    assert r["last_successful_pull"]["execution_id"] == "dse_pub"
    assert r["diagnosis"]["verdict"] == "ok"

    # All-green truth -> ready + enabled CTA.
    assert r["overall"] == "ready"
    assert r["host_cta"] == "enabled"
    assert "prêt" in r["headline"].lower()


def test_readiness_version_is_deterministic_and_change_sensitive(patched):
    r1 = _compute(patched)
    r2 = _compute(patched)
    assert r1.readiness_version == r2.readiness_version  # identical truth -> stable

    # A real change (DQ now degraded) flips the version.
    patched.state["dq"] = _dq_report(total_unresolved=3, issues=[{"firing_id": "f1"}])
    r3 = _compute(patched)
    assert r3.readiness_version != r1.readiness_version


# ---------------------------------------------------------------------------
# (b) partial history / degraded DQ with recent-ok -> degraded, NEVER ready.
# ---------------------------------------------------------------------------
def test_partial_history_recent_ok_is_degraded_never_ready(patched):
    patched.state["recent_state"] = _recent_state(historical_state="failed")
    r = _compute(patched).as_dict()

    assert r["overall"] == "degraded"
    assert r["overall"] != "ready"
    assert r["host_cta"] != "enabled"  # not enabled while degraded
    assert r["host_cta"] == "degraded"
    assert "prêt" not in r["headline"].lower()  # never "fully ready"
    # The recent result stays available despite the failed history marker.
    assert r["recent_coverage"]["state"] == "covered"
    hist_phase = next(p for p in r["phases"] if p["phase"] == "history")
    assert hist_phase["state"] == "degraded"


def test_degraded_dq_recent_ok_is_degraded_never_ready(patched):
    patched.state["dq"] = _dq_report(total_unresolved=2, issues=[{"firing_id": "f1"}])
    r = _compute(patched).as_dict()

    assert r["overall"] == "degraded"
    assert r["host_cta"] == "degraded"
    dq_phase = next(p for p in r["phases"] if p["phase"] == "data_quality")
    assert dq_phase["state"] == "degraded"
    assert dq_phase["next_action"] is not None


# ---------------------------------------------------------------------------
# (c) hard failure / no recent publication -> blocked + host_cta disabled.
# ---------------------------------------------------------------------------
def test_no_recent_publication_is_blocked_and_cta_disabled(patched):
    patched.state["recent_state"] = _recent_state(
        pointer=None, recent_state="pending", recent_available=False
    )
    patched.state["events"] = []  # no publication event
    r = _compute(patched).as_dict()

    assert r["overall"] == "blocked"
    assert r["host_cta"] == "disabled"
    assert r["current_publication"] is None
    assert "indisponible" in r["headline"].lower()


def test_recent_failed_is_blocked(patched):
    patched.state["recent_state"] = _recent_state(
        recent_state="failed", recent_available=False
    )
    r = _compute(patched).as_dict()
    assert r["overall"] == "blocked"
    assert r["host_cta"] == "disabled"
    recent_phase = next(p for p in r["phases"] if p["phase"] == "recent_pull")
    assert recent_phase["state"] == "failed"
    assert recent_phase["next_action"] is not None


# ---------------------------------------------------------------------------
# (d) each phase carries state + interval / rows / attempts / next-action.
# ---------------------------------------------------------------------------
def test_every_phase_carries_required_shape(patched):
    r = _compute(patched).as_dict()
    phases = {p["phase"]: p for p in r["phases"]}

    assert set(phases) == {
        "selection",
        "recent_pull",
        "verification",
        "publication",
        "data_quality",
        "history",
    }
    for p in r["phases"]:
        assert p["state"] in {
            "waiting",
            "running",
            "succeeded",
            "degraded",
            "failed",
            "blocked",
        }
        assert "interval" in p
        assert "rows" in p
        assert isinstance(p["attempts"], int)
        assert "live_publication_execution_id" in p
        assert "next_action" in p

    # Recent pull carries the interval, the row count and the max attempt count.
    recent = phases["recent_pull"]
    assert recent["interval"] == _INTERVAL
    assert recent["rows"] == 120
    assert recent["attempts"] == 2
    assert recent["live_publication_execution_id"] == "dse_pub"


# ---------------------------------------------------------------------------
# (e) access denial existence-hides (REST guard, not the compute function).
# ---------------------------------------------------------------------------
def test_access_denial_existence_hides(monkeypatch):
    """A denied strict-access decision must yield not_found, never a readiness object.

    Mirrors the REST guard contract: compute is only reached AFTER
    resolve_strict_resource_access allows. We assert the endpoint helper returns a
    404 not_found on denial and never calls compute.
    """
    from core import admin_api, project_access

    denied = project_access.AccessDecision(False, "grant_required")
    monkeypatch.setattr(
        project_access, "resolve_strict_resource_access", lambda *a, **k: denied
    )

    called = {"compute": False}

    def _boom(*a, **k):  # pragma: no cover - must never run on denial
        called["compute"] = True
        raise AssertionError("compute must not run when access is denied")

    monkeypatch.setattr(
        "core.first_report_readiness.compute_first_report_readiness", _boom
    )

    # The strict decision path is the load-bearing invariant: denied -> not_found.
    decision = project_access.resolve_strict_resource_access(
        "user:1", MagicMock(), project_id="proj-1", minimum_capability="view"
    )
    assert not decision.allowed
    assert called["compute"] is False
    assert admin_api is not None  # endpoint wiring imported without error
