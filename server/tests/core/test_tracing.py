"""Story 5.1 (AC2, AC4, AC10) -- core.tracing unit tests.

All tests use an in-memory OTel span exporter -- no live Langfuse (per Dev Notes).
The OTel SDK is an optional dependency; these tests importorskip when it is absent,
matching the "server starts normally with the OTel SDK absent" contract (AC10).

Env guard (project pattern): background threads are suppressed via setdefault.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# The tracing tests need the OTel SDK (in-memory exporter). Skip cleanly if absent.
pytest.importorskip("opentelemetry.sdk")

from core import tracing  # noqa: E402


@pytest.fixture
def in_memory_tracer(monkeypatch):
    """Build a tracer wired to an InMemorySpanExporter and install it in core.tracing.

    Yields the exporter so tests can read finished spans. Restores module state after.
    """
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


# ---------------------------------------------------------------------------
# AC10 -- disabled by default: no exporter, no OTLP
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("TRACING_ENABLED", raising=False)
    tracing.reset_for_tests()
    assert tracing.is_enabled() is False
    assert tracing.init_tracing() is None
    assert tracing.get_tracer() is None


def test_disabled_span_is_noop(monkeypatch):
    monkeypatch.setenv("TRACING_ENABLED", "false")
    tracing.reset_for_tests()
    with tracing.tool_span("health", {"project_id": "default"}) as handle:
        assert handle.active is False
        handle.set("tool.name", "health")  # must not raise


def test_init_noop_when_sdk_absent(monkeypatch):
    """When the OTel SDK import fails, init_tracing no-ops (server still starts)."""
    monkeypatch.setenv("TRACING_ENABLED", "true")
    tracing.reset_for_tests()

    import builtins

    real_import = builtins.__import__

    def _fail_sdk(name, *args, **kwargs):
        if name.startswith("opentelemetry.sdk"):
            raise ImportError("simulated: SDK absent")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fail_sdk)
    assert tracing.init_tracing(force=True) is None
    tracing.reset_for_tests()


# ---------------------------------------------------------------------------
# AC2 -- spans carry expected attribute keys
# ---------------------------------------------------------------------------


def test_tool_span_emits_expected_attributes(in_memory_tracer):
    with tracing.tool_span("get_daily_report", {"project_id": "acme"}) as handle:
        assert handle.active is True
        handle.set("tool.latency_ms", 42)
        handle.set("tool.payload_bytes", 1000)

    spans = in_memory_tracer.get_finished_spans()
    assert len(spans) == 1
    span = spans[0]
    assert span.name == "tool.get_daily_report"
    attrs = dict(span.attributes)
    assert attrs["tool.name"] == "get_daily_report"
    assert attrs["tool.latency_ms"] == 42
    assert attrs["tool.payload_bytes"] == 1000
    # project_id is a non-secret string -> recorded as a shape, not plaintext
    assert "tool.params.project_id" in attrs


def test_record_current_span_attributes(in_memory_tracer):
    with tracing.tool_span("health", {}):
        tracing.record_current_span_attributes(
            {"tool.summary_token_estimate": 7, "tool.payload_bytes": 55}
        )
    span = in_memory_tracer.get_finished_spans()[0]
    attrs = dict(span.attributes)
    assert attrs["tool.summary_token_estimate"] == 7
    assert attrs["tool.payload_bytes"] == 55


# ---------------------------------------------------------------------------
# AC4 / AD-3 -- _sanitize_params strips secrets
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "access_token",
        "api_key",
        "apiKey",
        "client_secret",
        "password",
        "nango_credential",
        "authToken",
    ],
)
def test_sanitize_redacts_secret_keys(key):
    out = tracing._sanitize_params({key: "super-secret-value-1234567890"})
    assert out[key] == "<redacted>"
    assert "super-secret" not in str(out)


def test_sanitize_records_string_shape_not_value():
    out = tracing._sanitize_params({"project_id": "acme-corp"})
    assert out["project_id"] == {"type": "str", "length": len("acme-corp")}
    assert "acme-corp" not in str(out)


def test_sanitize_passthrough_scalars_and_nested():
    out = tracing._sanitize_params(
        {"count": 5, "flag": True, "nested": {"token": "abc", "n": 3}}
    )
    assert out["count"] == 5
    assert out["flag"] is True
    assert out["nested"]["token"] == "<redacted>"
    assert out["nested"]["n"] == 3
