"""Story 36.9 -- execute-and-publish the recent-first candidate SAFELY (offline).

Offline MagicMock-style, mirroring test_epic36_first_report_draft.py: the shared
Epic 12 publication seams (create_execution / advance_state / run_dq_gates /
commit_publication) and this module's own thin SQL helpers are patched, so the
Story 36.9 INVARIANTS are exercised WITHOUT live Postgres. Live-PG coverage
(pointer non-advance, coverage UNIQUE constraint, immutability triggers, RLS) is
the established follow-up.

Covered invariants:
  (a) creates EXACTLY ONE idempotent candidate from the saved plan/mapping versions.
  (b) empty/partial/invalid/failed/DQ-blocked candidate NEVER calls
      commit_publication / mutates current_published_execution_id; last-known-good
      stays visible.
  (c) a ready valid candidate commits atomically (pointer+audit+outbox) exactly once.
  (d) a duplicate execute returns the ORIGINAL candidate (idempotent, no duplicate).
  (e) recent and historical coverage are tracked separately; a simulated later
      history failure does NOT flip the recent result to unavailable.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import recent_first_publication as rfp  # noqa: E402
from core.datastream_publication import (  # noqa: E402
    IdempotencyConflict,
    PublicationError,
)

_HASH_A = "a" * 64
_HASH_B = "b" * 64

_INTERVAL = {"start": "2026-06-22T00:00:00Z", "end_exclusive": "2026-07-22T00:00:00Z"}


def _draft(**overrides):
    base = {
        "draft_id": "frdraft_1",
        "datastream_id": "ds-1",
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "projection_plan": {"executable": True},
        "interval": dict(_INTERVAL),
    }
    base.update(overrides)
    return base


def _execution(state="created", execution_id="dse_1", content_hash=None, row_count=None):
    return {
        "id": execution_id,
        "datastream_id": "ds-1",
        "project_id": "proj-1",
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "state": state,
        "content_hash": content_hash,
        "row_count": row_count,
    }


@pytest.fixture
def patched(monkeypatch):
    """Patch the module's SQL helpers + a recorder for coverage upserts.

    Returns a namespace of the MagicMocks + a coverage-state recorder keyed by
    coverage_kind so the recent/historical separation is directly assertable.
    """
    conn = MagicMock()

    # Record every coverage upsert as {coverage_kind: last_state}. Proves the
    # recent and historical markers are updated as SEPARATE facts.
    coverage_state: dict[str, str] = {}

    def _fake_upsert(_conn, **kwargs):
        coverage_state[kwargs["coverage_kind"]] = kwargs["state"]

    def _fake_read(_conn, **kwargs):
        kind = kwargs["coverage_kind"]
        if kind not in coverage_state:
            return None
        return rfp.CoverageMarker(
            coverage_kind=kind,
            state=coverage_state[kind],
            interval_start=_INTERVAL["start"],
            interval_end_exclusive=_INTERVAL["end_exclusive"],
            execution_id="dse_1",
            pull_id="pull_1",
            row_count=100,
            content_hash=_HASH_A,
            covered_at="2026-07-22T00:00:00Z" if coverage_state[kind] == "covered" else None,
        )

    pointer = {"value": "dse_prev"}  # last-known-good pointer

    def _fake_pointer(_conn, **kwargs):
        return pointer["value"]

    monkeypatch.setattr(rfp, "_upsert_coverage", _fake_upsert)
    monkeypatch.setattr(rfp, "_read_coverage", _fake_read)
    monkeypatch.setattr(rfp, "_current_pointer", _fake_pointer)
    monkeypatch.setattr(
        rfp, "_load_saved_draft", MagicMock(return_value=_draft())
    )

    create = MagicMock(return_value=_execution("created"))
    advance = MagicMock(side_effect=lambda *a, **k: _execution(a[2]))
    gates = MagicMock(return_value=[])
    commit = MagicMock(
        return_value={
            "execution": _execution("published", content_hash=_HASH_A, row_count=100),
            "publication_log_id": "dplog_1",
            "prior_execution_id": "dse_prev",
        }
    )
    monkeypatch.setattr(rfp, "create_execution", create)
    monkeypatch.setattr(rfp, "advance_state", advance)
    monkeypatch.setattr(rfp, "run_dq_gates", gates)
    monkeypatch.setattr(rfp, "commit_publication", commit)

    ns = MagicMock()
    ns.conn = conn
    ns.coverage_state = coverage_state
    ns.pointer = pointer
    ns.create = create
    ns.advance = advance
    ns.gates = gates
    ns.commit = commit
    return ns


# ---------------------------------------------------------------------------
# (a) ONE idempotent candidate from the exact approved plan/mapping versions.
# ---------------------------------------------------------------------------
def test_creates_exactly_one_candidate_from_saved_versions(patched):
    rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        row_count=100,
        content_hash=_HASH_A,
        verification_verdict="ok",
        pull_id="pull_1",
    )
    # Exactly ONE create_execution, with the EXACT saved plan/mapping versions.
    assert patched.create.call_count == 1
    args = patched.create.call_args.args
    # create_execution(datastream_id, project_id, plan_version_id, mapping_version_id, ...)
    assert args[0] == "ds-1"
    assert args[2] == "dsp_1"
    assert args[3] == "dmap_1"
    # The idempotency key is threaded verbatim.
    assert "idem-1" in args


# ---------------------------------------------------------------------------
# (c) ready valid candidate publishes atomically ONCE (pointer+audit+outbox).
# ---------------------------------------------------------------------------
def test_ready_valid_candidate_commits_atomically_once(patched):
    result = rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        row_count=100,
        content_hash=_HASH_A,
        verification_verdict="ok",
        pull_id="pull_1",
    )
    assert result.published is True
    assert result.disposition == "published"
    # Atomic publish delegated to commit_publication EXACTLY once (it writes the
    # pointer + publication_log + outbox + audit in ONE transaction).
    assert patched.commit.call_count == 1
    assert result.current_published_execution_id == "dse_1"
    assert result.publication_log_id == "dplog_1"
    # Recent coverage marked covered; historical untouched (separate row).
    assert patched.coverage_state.get("recent") == "covered"
    assert "historical" not in patched.coverage_state


# ---------------------------------------------------------------------------
# (b) empty/partial/invalid/failed candidate NEVER publishes; pointer preserved.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kwargs,reason_prefix",
    [
        ({"row_count": 0, "verification_verdict": "empty"}, "verification_empty"),
        ({"row_count": 40, "verification_verdict": "partial"}, "verification_partial"),
        ({"row_count": None, "verification_verdict": "ok"}, "verification_"),
        ({"row_count": 100, "verification_verdict": "invalid"}, "verification_invalid"),
    ],
)
def test_bad_candidate_never_publishes_and_preserves_last_known_good(
    patched, kwargs, reason_prefix
):
    result = rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        content_hash=_HASH_A,
        pull_id="pull_1",
        **kwargs,
    )
    # commit_publication is NEVER called -> pointer never mutated.
    assert patched.commit.call_count == 0
    assert result.published is False
    assert result.disposition == "blocked"
    # Last-known-good pointer preserved verbatim.
    assert result.current_published_execution_id == "dse_prev"
    assert result.reason.startswith(reason_prefix)
    # Candidate advanced to failed (terminal) -- one advance_state to 'failed'.
    failed_calls = [c for c in patched.advance.call_args_list if c.args[2] == "failed"]
    assert failed_calls
    # Recent coverage recorded failed; historical never touched.
    assert patched.coverage_state.get("recent") == "failed"
    assert "historical" not in patched.coverage_state


def test_dq_gate_failure_never_publishes(patched):
    patched.gates.return_value = [
        {"code": "row_count_delta_exceeded", "detail": "too big", "repair": {}}
    ]
    result = rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        row_count=100,
        content_hash=_HASH_A,
        verification_verdict="ok",
        pull_id="pull_1",
    )
    assert patched.commit.call_count == 0
    assert result.published is False
    assert result.disposition == "blocked"
    assert result.reason == "dq_gate_failed"
    assert result.current_published_execution_id == "dse_prev"
    assert result.dq_issues and result.dq_issues[0]["code"] == "row_count_delta_exceeded"


def test_publication_failure_preserves_pointer(patched):
    patched.commit.side_effect = PublicationError("candidate_not_validated", "x")
    result = rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        row_count=100,
        content_hash=_HASH_A,
        verification_verdict="ok",
        pull_id="pull_1",
    )
    # commit_publication was attempted but raised -> pointer stays at prior value.
    assert patched.commit.call_count == 1
    assert result.published is False
    assert result.disposition == "failed"
    assert result.current_published_execution_id == "dse_prev"
    assert result.reason == "candidate_not_validated"


# ---------------------------------------------------------------------------
# (d) duplicate execute returns the ORIGINAL candidate (idempotent, no duplicate).
# ---------------------------------------------------------------------------
def test_duplicate_execute_returns_original_candidate(patched):
    # create_execution is idempotent: it returns an ALREADY-published execution for
    # the reused key. The module must NOT re-drive or re-publish it.
    patched.create.return_value = _execution(
        "published", content_hash=_HASH_A, row_count=100
    )
    patched.pointer["value"] = "dse_1"  # pointer already at that candidate
    patched.coverage_state["recent"] = "covered"

    result = rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        row_count=100,
        content_hash=_HASH_A,
        verification_verdict="ok",
        pull_id="pull_1",
    )
    assert result.replayed is True
    assert result.execution_id == "dse_1"
    assert result.published is True
    # A replayed terminal candidate is NOT advanced, gated, or re-committed.
    assert patched.advance.call_count == 0
    assert patched.gates.call_count == 0
    assert patched.commit.call_count == 0
    # And exactly ONE candidate was ever created (no duplicate).
    assert patched.create.call_count == 1


def test_idempotency_conflict_maps_to_recent_first_conflict(patched):
    patched.create.side_effect = IdempotencyConflict()
    with pytest.raises(rfp.RecentFirstConflict):
        rfp.execute_recent_first(
            patched.conn,
            project_id="proj-1",
            actor="camille@example.com",
            idempotency_key="idem-1",
            draft_id="frdraft_1",
            row_count=100,
            content_hash=_HASH_A,
            verification_verdict="ok",
        )
    assert patched.commit.call_count == 0


# ---------------------------------------------------------------------------
# (e) recent vs historical coverage separate; later history failure does NOT
#     flip the recent result to unavailable.
# ---------------------------------------------------------------------------
def test_history_failure_does_not_flip_recent_result(patched):
    # First publish the recent-first candidate -> recent = covered.
    rfp.execute_recent_first(
        patched.conn,
        project_id="proj-1",
        actor="camille@example.com",
        idempotency_key="idem-1",
        draft_id="frdraft_1",
        row_count=100,
        content_hash=_HASH_A,
        verification_verdict="ok",
        pull_id="pull_1",
    )
    assert patched.coverage_state["recent"] == "covered"

    # Now a LATER background-history attempt fails -> historical = failed. This
    # touches ONLY the historical marker (separate row).
    rfp.note_history_progress(
        patched.conn,
        project_id="proj-1",
        datastream_id="ds-1",
        actor="system",
        state="failed",
        interval={"start": "2020-01-01T00:00:00Z", "end_exclusive": "2026-06-22T00:00:00Z"},
    )
    assert patched.coverage_state["historical"] == "failed"
    # The recent marker is UNTOUCHED by the history failure.
    assert patched.coverage_state["recent"] == "covered"

    # The readiness read proves the recent result stays AVAILABLE despite the
    # historical failure.
    state = rfp.get_recent_first_state(
        patched.conn, project_id="proj-1", datastream_id="ds-1"
    )
    assert state["recent_available"] is True
    assert state["recent_coverage"]["state"] == "covered"
    assert state["historical_coverage"]["state"] == "failed"


def test_get_recent_first_state_shape(patched):
    patched.coverage_state["recent"] = "degraded"
    patched.pointer["value"] = "dse_1"
    state = rfp.get_recent_first_state(
        patched.conn, project_id="proj-1", datastream_id="ds-1"
    )
    assert state["current_published_execution_id"] == "dse_1"
    assert state["recent_available"] is True  # degraded is still available
    assert state["historical_coverage"] is None
