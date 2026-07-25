"""Story 5.1 (AC6) -- dbt-run spans join the trace tree.

scheduler.run_dbt wraps ``subprocess.run(["dbt", ...])`` in a span carrying
dbt.exit_code / dbt.models_run / dbt.latency_ms, linked to the triggering job's
trace_id when available. The subprocess is mocked; in-memory exporter only.
"""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

pytest.importorskip("opentelemetry.sdk")

from core import scheduler, tracing  # noqa: E402


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


def test_run_dbt_emits_span_with_attributes(in_memory_tracer):
    def _fake_runner(cmd, **kwargs):
        assert cmd[:2] == ["dbt", "run"]
        return SimpleNamespace(
            returncode=0,
            stdout="1 of 3 OK created model foo\n2 of 3 OK created model bar\n",
            stderr="",
        )

    result = scheduler.run_dbt(_runner=_fake_runner)
    assert result["exit_code"] == 0
    assert result["models_run"] == 2

    spans = [s for s in in_memory_tracer.get_finished_spans() if s.name == "dbt.run"]
    assert spans, "expected a dbt.run span"
    attrs = dict(spans[0].attributes)
    assert attrs["dbt.exit_code"] == 0
    assert attrs["dbt.models_run"] == 2
    assert "dbt.latency_ms" in attrs


def test_run_dbt_links_to_trace_id(in_memory_tracer):
    parent = "4bf92f3577b34da6a3ce929d0e0e4736"

    def _fake_runner(cmd, **kwargs):
        return SimpleNamespace(returncode=1, stdout="", stderr="boom")

    scheduler.run_dbt(["run", "--select", "marts"], trace_id=parent, _runner=_fake_runner)
    span = [s for s in in_memory_tracer.get_finished_spans() if s.name == "dbt.run"][0]
    assert format(span.context.trace_id, "032x") == parent
    assert dict(span.attributes)["dbt.exit_code"] == 1


def test_run_dbt_noop_span_when_disabled(monkeypatch):
    monkeypatch.setenv("TRACING_ENABLED", "false")
    tracing.reset_for_tests()

    def _fake_runner(cmd, **kwargs):
        return SimpleNamespace(returncode=0, stdout="1 of 1 OK model x\n", stderr="")

    # Still runs the subprocess and returns a result; just no span exported.
    result = scheduler.run_dbt(_runner=_fake_runner)
    assert result["exit_code"] == 0
    assert result["models_run"] == 1
