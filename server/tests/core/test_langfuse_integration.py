"""Story 5.1 (T10.5) -- Langfuse integration test, skips when unreachable.

Same skip-if-unreachable pattern as @airbyte_available in test_airbyte_client.py.
CI never starts Langfuse, so this test skips there; it runs only when a developer
has brought up infra/langfuse/docker-compose.yml and set the env vars.
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# AI-34 LIVE VERIFICATION CHECKLIST (run when Langfuse is brought up per infra/langfuse/SETUP.md):
# 1. Call get_daily_report with TRACING_ENABLED=true.
# 2. Capture meta.trace_id from response.
# 3. Call submit_feedback with that trace_id.
# 4. Verify the score appears on the correct trace in Langfuse UI at http://localhost:3004.
# 5. If OTel trace_id != Langfuse trace_id: implement lf.get_trace_by_otel_id() and document.

LANGFUSE_HOST = os.environ.get("LANGFUSE_HOST", "http://localhost:3004")


def _langfuse_reachable(host: str) -> bool:
    """Return True if the Langfuse health endpoint is reachable."""
    try:
        import httpx  # noqa: PLC0415

        httpx.get(host.rstrip("/") + "/api/public/health", timeout=3).raise_for_status()
        return True
    except Exception:
        return False


langfuse_available = pytest.mark.skipif(
    not _langfuse_reachable(LANGFUSE_HOST),
    reason="Langfuse not running -- bring up infra/langfuse/docker-compose.yml",
)


@langfuse_available
def test_langfuse_export_roundtrip(monkeypatch):
    """With a real Langfuse + keys, a span export must not raise (best-effort check)."""
    pytest.importorskip("opentelemetry.sdk")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")

    if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
        pytest.skip("LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set")

    from core import tracing

    monkeypatch.setenv("TRACING_ENABLED", "true")
    tracing.reset_for_tests()
    provider = tracing.init_tracing(force=True)
    assert provider is not None

    with tracing.tool_span("get_daily_report", {"project_id": "default"}) as span:
        assert span.active is True
        span.set("tool.latency_ms", 5)

    # Force flush so the exporter actually ships the span to Langfuse.
    provider.force_flush()
    tracing.reset_for_tests()
