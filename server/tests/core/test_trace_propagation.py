"""Story 5.1 (AC3) -- W3C traceparent propagation via _meta.

A valid ``_meta.traceparent`` continues the client trace (same trace_id); an
invalid/absent value starts a fresh root span. In-memory exporter only.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

pytest.importorskip("opentelemetry.sdk")

from core import tracing  # noqa: E402


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


_CLIENT_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
_VALID_TRACEPARENT = f"00-{_CLIENT_TRACE_ID}-00f067aa0ba902b7-01"


def test_valid_traceparent_continues_trace(in_memory_tracer):
    with tracing.tool_span("get_daily_report", {}, meta={"traceparent": _VALID_TRACEPARENT}):
        pass
    span = in_memory_tracer.get_finished_spans()[0]
    emitted_trace_id = format(span.context.trace_id, "032x")
    assert emitted_trace_id == _CLIENT_TRACE_ID
    # It is a child of the injected parent, not a new root.
    assert span.parent is not None
    assert format(span.parent.span_id, "016x") == "00f067aa0ba902b7"


def test_absent_traceparent_starts_new_root(in_memory_tracer):
    with tracing.tool_span("get_daily_report", {}, meta=None):
        pass
    span = in_memory_tracer.get_finished_spans()[0]
    assert format(span.context.trace_id, "032x") != _CLIENT_TRACE_ID
    assert span.parent is None  # fresh root


def test_invalid_traceparent_starts_new_root(in_memory_tracer):
    with tracing.tool_span("get_daily_report", {}, meta={"traceparent": "not-a-traceparent"}):
        pass
    span = in_memory_tracer.get_finished_spans()[0]
    assert span.parent is None


def test_extract_parent_context_validates_shape():
    assert tracing._extract_parent_context({"traceparent": "garbage"}) is None
    assert tracing._extract_parent_context({}) is None
    assert tracing._extract_parent_context(None) is None
    # well-formed value yields a non-None context
    assert tracing._extract_parent_context({"traceparent": _VALID_TRACEPARENT}) is not None


def test_traceparent_from_trace_id_roundtrip():
    tp = tracing.traceparent_from_trace_id(_CLIENT_TRACE_ID)
    assert tp is not None
    assert tp.startswith(f"00-{_CLIENT_TRACE_ID}-")
    assert tracing.traceparent_from_trace_id("too-short") is None
