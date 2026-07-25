"""Unit tests for the measured pre-query gate (Story 11.6, AD-18).

Covers core.adherence directly (session-key definition, both call orders, the
one-line pointer + <=30-line cap, and the non-blocking / never-raises contract).
The MCP-layer seam is exercised separately in
server/tests/integration/test_pre_query_gate_seams.py (AI-56).
"""

from __future__ import annotations

import pytest
from core import adherence


@pytest.fixture(autouse=True)
def _reset_adherence():
    adherence.reset_for_tests()
    yield
    adherence.reset_for_tests()


# ---------------------------------------------------------------------------
# The "same session" key: trace parent + time window (documented choice).
# ---------------------------------------------------------------------------


def test_session_key_prefers_trace_id_when_present():
    key, kind = adherence.session_key(trace_id="a" * 32)
    assert kind == "trace"
    assert key == "trace:" + "a" * 32


def test_session_key_falls_back_to_time_window_without_trace():
    key, kind = adherence.session_key(trace_id=None, now=1000.0)
    assert kind == "time_window"
    assert key.startswith("tw:")


def test_time_window_bucket_is_stable_within_window(monkeypatch):
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")
    # Bucket 3 = [900, 1200): 1000 and 1100 share it; 1300 is bucket 4.
    k1, _ = adherence.session_key(trace_id=None, now=1000.0)
    k2, _ = adherence.session_key(trace_id=None, now=1100.0)  # same 300s bucket
    k3, _ = adherence.session_key(trace_id=None, now=1300.0)  # next bucket
    assert k1 == k2
    assert k1 != k3


# ---------------------------------------------------------------------------
# Adherence recorded in BOTH call orders (verdict by value). We patch the two
# sinks so no Langfuse / Postgres is required (degrade-clean, AI-13 N/A).
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_sinks(monkeypatch):
    """Silence the trace + Postgres sinks so we assert the pure verdict."""
    monkeypatch.setattr(adherence, "_record_trace_attributes", lambda *a, **k: None)
    monkeypatch.setattr(adherence, "_record_postgres_fallback", lambda *a, **k: None)


def test_context_then_data_is_adherent(_no_sinks):
    trace = "b" * 32
    adherence.mark_context_call("search_context", trace_id=trace)
    verdict = adherence.record_data_query(
        "get_report", project_id="projA", trace_id=trace
    )
    assert verdict["adherent"] is True
    assert verdict["context_tool"] == "search_context"
    assert verdict["data_tool"] == "get_report"
    assert verdict["session_kind"] == "trace"


def test_data_only_is_not_adherent(_no_sinks):
    trace = "c" * 32
    verdict = adherence.record_data_query(
        "get_card", project_id="projA", trace_id=trace
    )
    assert verdict["adherent"] is False
    assert verdict["context_tool"] is None


def test_data_then_context_is_not_adherent_for_that_query(_no_sinks):
    """A context call AFTER the data query does not retroactively make it adherent."""
    trace = "d" * 32
    v1 = adherence.record_data_query("get_daily_report", project_id="p", trace_id=trace)
    adherence.mark_context_call("get_procedure", trace_id=trace)
    assert v1["adherent"] is False  # the recorded verdict for the earlier query


def test_different_sessions_do_not_leak_adherence(_no_sinks):
    adherence.mark_context_call("search_context", trace_id="e" * 32)
    verdict = adherence.record_data_query(
        "get_report", project_id="p", trace_id="f" * 32  # different trace/session
    )
    assert verdict["adherent"] is False


def test_time_window_session_adherence(_no_sinks, monkeypatch):
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")
    # No trace id -> time-window session. Same window + same project -> adherent.
    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", now=1000.0
    )
    verdict = adherence.record_data_query(
        "get_card", project_id="p", trace_id=None, now=1100.0
    )
    assert verdict["adherent"] is True


def test_time_window_key_is_scoped_by_project():
    """The time-window key folds in project_id so two projects don't collide."""
    ka, _ = adherence.session_key(trace_id=None, project_id="projA", now=1000.0)
    kb, _ = adherence.session_key(trace_id=None, project_id="projB", now=1000.0)
    assert ka != kb
    assert ka.startswith("tw:projA:")
    assert kb.startswith("tw:projB:")


def test_cross_project_time_window_does_not_leak_adherence(_no_sinks, monkeypatch):
    """[tw session key] Tracing OFF, same window: project A's context call must NOT
    make project B's data query adherent (the _context_seen map is process-global,
    so the time-window key MUST include project_id to isolate tenants)."""
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")
    # Project A consults context (no trace -> time-window session).
    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="projA", now=1000.0
    )
    # Project B queries data in the SAME wall-clock window, no context of its own.
    verdict = adherence.record_data_query(
        "get_card", project_id="projB", trace_id=None, now=1100.0
    )
    assert verdict["adherent"] is False  # no cross-tenant leak
    assert verdict["context_tool"] is None
    # Project A's own data query in the same window is still adherent.
    verdict_a = adherence.record_data_query(
        "get_report", project_id="projA", trace_id=None, now=1150.0
    )
    assert verdict_a["adherent"] is True


# ---------------------------------------------------------------------------
# The one-line pointer (AD-1: ~1 line, never a dump) + <=30-line cap preserved.
# ---------------------------------------------------------------------------


def test_pointer_none_when_adherent():
    defs = {"clicks": {"definition": "Clics", "direction": "up_good"}}
    assert adherence.build_context_pointer(defs, adherent=True) is None


def test_pointer_none_when_no_definitions():
    assert adherence.build_context_pointer(None, adherent=False) is None
    assert adherence.build_context_pointer({}, adherent=False) is None


def test_pointer_is_single_line_and_mentions_search_context():
    defs = {
        "clicks": {"definition": "Clics"},
        "impressions": {"definition": "Impr."},
        "conversions": {"definition": "Conv."},
        "cost": {"definition": "Cout"},
    }
    pointer = adherence.build_context_pointer(defs, adherent=False)
    assert pointer is not None
    assert "\n" not in pointer  # EXACTLY one line (AD-1)
    assert "search_context" in pointer
    # It POINTS, it does not DUMP: at most 3 terms named + an ellipsis, never the
    # definition texts themselves.
    assert "Clics" not in pointer
    assert pointer.count(",") <= 2  # <=3 terms listed


def test_append_pointer_preserves_30_line_cap():
    # A summary already AT the 30-line cap.
    summary = "\n".join(f"line {i}" for i in range(30))
    pointer = "Astuce contexte : appelez search_context."
    out = adherence.append_pointer_within_cap(summary, pointer)
    lines = out.split("\n")
    assert len(lines) <= 30, f"cap broken: {len(lines)} lines"
    # The pointer survives (tail trimmed, not the pointer).
    assert lines[-1] == pointer


def test_append_pointer_noop_when_pointer_falsy():
    summary = "one\ntwo"
    assert adherence.append_pointer_within_cap(summary, None) == summary
    assert adherence.append_pointer_within_cap(summary, "") == summary


def test_append_pointer_under_cap_keeps_all_lines():
    summary = "a\nb\nc"
    pointer = "ptr"
    out = adherence.append_pointer_within_cap(summary, pointer)
    assert out.split("\n") == ["a", "b", "c", "ptr"]


# ---------------------------------------------------------------------------
# Non-blocking / never raises: sink failures must not surface (AD-18).
# ---------------------------------------------------------------------------


def test_record_data_query_never_raises_on_sink_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(adherence, "_record_trace_attributes", _boom)
    monkeypatch.setattr(adherence, "_record_postgres_fallback", _boom)
    # record_data_query catches sink errors internally; assert it returns a verdict.
    verdict = adherence.record_data_query("get_report", project_id="p", trace_id="1" * 32)
    assert verdict["data_tool"] == "get_report"
    assert verdict["adherent"] is False


def test_mark_context_call_never_raises(monkeypatch):
    # Force session_key to blow up; mark must swallow it.
    monkeypatch.setattr(
        adherence, "session_key", lambda **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    adherence.mark_context_call("search_context", trace_id=None)  # no raise
