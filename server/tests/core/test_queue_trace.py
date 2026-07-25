"""Story 5.1 (AC5) -- queue worker spans join the same trace tree.

Verifies:
  * enqueue_pull captures the current trace_id and stores it in the pull_jobs INSERT.
  * _execute_job emits a worker span carrying job.id / pull.pull_id / job.connector /
    job.latency_ms, joined to the originating trace via the stored trace_id.

All DB calls are mocked; the in-memory OTel exporter replaces live Langfuse.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

pytest.importorskip("opentelemetry.sdk")

from core import queue, tracing  # noqa: E402


@pytest.fixture
def in_memory_tracer(monkeypatch):
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("TRACING_ENABLED", "true")
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracing.reset_for_tests()
    tracing._provider = provider
    tracing._tracer = provider.get_tracer("test")
    tracing._initialized = True
    try:
        yield exporter
    finally:
        tracing.reset_for_tests()


def _fake_conn(fetchone_return=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=fetchone_return)
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)

    @contextmanager
    def _get():
        yield conn

    return _get, conn, cur


# ---------------------------------------------------------------------------
# AC5 -- trace_id captured + stored at enqueue time
# ---------------------------------------------------------------------------


def test_enqueue_stores_trace_id_in_insert(in_memory_tracer):
    fake_get, _conn, cur = _fake_conn(fetchone_return=None)

    with patch("core.db.get_connection", fake_get), patch("core.audit.write_audit_row"):
        # Open a span so a real trace_id is active during enqueue.
        with tracing.tool_span("get_daily_report", {}):
            queue.enqueue_pull(
                "conn_123", "2026-01-01", "2026-01-07", requested_by="tester"
            )

    # Find the INSERT ... pull_jobs call and assert trace_id is a 32-hex id.
    # Story 8.2: INSERT now has 9 params (added datastream_id as last).
    # Column order: id, pull_id, connection_ref_id, date_from, date_to,
    #               state, requested_by, trace_id, datastream_id
    # trace_id is params[-2] (second to last).
    insert_calls = [
        c for c in cur.execute.call_args_list if "INSERT INTO app.pull_jobs" in c.args[0]
    ]
    assert insert_calls, "expected an INSERT into app.pull_jobs"
    sql, params = insert_calls[0].args
    assert "trace_id" in sql
    trace_id = params[-2]
    assert isinstance(trace_id, str) and len(trace_id) == 32


def test_enqueue_trace_id_null_when_disabled(monkeypatch):
    monkeypatch.setenv("TRACING_ENABLED", "false")
    tracing.reset_for_tests()
    fake_get, _conn, cur = _fake_conn(fetchone_return=None)

    with patch("core.db.get_connection", fake_get), patch("core.audit.write_audit_row"):
        queue.enqueue_pull("conn_123", "2026-01-01", "2026-01-07", requested_by="tester")

    insert_calls = [
        c for c in cur.execute.call_args_list if "INSERT INTO app.pull_jobs" in c.args[0]
    ]
    assert insert_calls
    _sql, params = insert_calls[0].args
    # Story 8.2: datastream_id is last; trace_id is second-to-last (params[-2])
    assert params[-2] is None  # trace_id NULL when tracing disabled


# ---------------------------------------------------------------------------
# AC5 -- worker span emitted with job attributes, joined to stored trace
# ---------------------------------------------------------------------------


def test_execute_job_emits_worker_span(in_memory_tracer):
    parent_trace_id = "4bf92f3577b34da6a3ce929d0e0e4736"
    job = {
        "id": "job_ABC",
        "pull_id": "pull_XYZ",
        "connection_ref_id": "conn_123",
        "date_from": "2026-01-01",
        "date_to": "2026-01-07",
        "requested_by": "tester",
        "attempt_count": 0,
        "trace_id": parent_trace_id,
    }

    fake_get, _conn, _cur = _fake_conn()
    ref = {
        "id": "conn_123",
        "nango_connection_id": "nango_1",
        "provider": "google-analytics",
        "project_id": "default",
    }

    def _fake_pull(**kwargs):
        return {"row_count": 10}

    with (
        patch("core.db.get_connection", fake_get),
        patch("core.queue._resolve_connection_ref", return_value=ref),
        patch("core.main.get_module_pull_fn", return_value=_fake_pull),
        patch("core.queue._get_manifest_for_provider", return_value={"module_kind": "kpi"}),
        patch("core.audit.write_audit_row"),
        patch("core.quota.get_read_cost", return_value=0),
        patch("core.quota.pre_check", return_value=(True, None)),
        patch("core.verification.run_post_pull_verification"),
    ):
        queue._execute_job(job)

    spans = [s for s in in_memory_tracer.get_finished_spans() if s.name == "queue.pull"]
    assert spans, "expected a queue.pull worker span"
    span = spans[0]
    attrs = dict(span.attributes)
    assert attrs["job.id"] == "job_ABC"
    assert attrs["pull.pull_id"] == "pull_XYZ"
    assert attrs["job.connector"] == "google-analytics"
    assert "job.latency_ms" in attrs
    # Joined to the originating trace (same trace_id as the stored parent).
    assert format(span.context.trace_id, "032x") == parent_trace_id
