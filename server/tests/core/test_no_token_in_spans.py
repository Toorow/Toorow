"""Story 5.1 (AC4 / AD-3) -- token values NEVER appear in span attributes.

Exercises the universal TracingMiddleware end-to-end via a FastMCP in-process client,
then greps every exported span attribute for secret-shaped values. In-memory exporter
only -- no live Langfuse.
"""

from __future__ import annotations

import asyncio
import os
import re

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

pytest.importorskip("opentelemetry.sdk")

from core import tracing  # noqa: E402
from core.main import mcp  # noqa: E402
from fastmcp import Client  # noqa: E402

# A recognisable secret value: a long base64-ish token that MUST never be exported.
_SECRET_VALUE = "ya29A0ARrdaMSUPERSECRETTOKENvalue1234567890abcdefGHIJKLMNOP"
# Broad secret-pattern grep (the AC4 test contract): long token-like strings.
_SECRET_LIKE = re.compile(r"[A-Za-z0-9+/_-]{32,}")
# Attribute keys allowed to legitimately hold long strings.
# gate.session_key (Story 11.6) is "trace:<32-hex OTel trace id>" -- the trace id
# is by definition already carried by every span (it IS the span's trace id), so
# exporting it under gate.* leaks nothing; the broad regex just pattern-matches
# any 32-hex run. Allowlisted with this justification (epic-22 close-out sweep).
_ALLOWED_LONG_ATTRS: set[str] = {"gate.session_key"}


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


def _all_attr_values(exporter) -> list[tuple[str, object]]:
    pairs: list[tuple[str, object]] = []
    for span in exporter.get_finished_spans():
        for key, value in dict(span.attributes).items():
            pairs.append((key, value))
    return pairs


def test_secret_token_never_in_span_attributes(in_memory_tracer):
    async def _call():
        async with Client(mcp) as c:
            # A secret-shaped VALUE smuggled into a legitimate string param.
            # Story 7.1 (AC5): project_id is now validated against app.projects,
            # so the secret is smuggled into `connectors` (a list-of-strings param
            # that also flows through the middleware) while project_id stays the
            # seeded 'default'. The middleware records only the value SHAPE
            # ({type,length}) for strings, so the plaintext must never be exported.
            await c.call_tool(
                "get_daily_report",
                {
                    "project_id": "default",
                    "date_range": {"start": "2026-01-01", "end": "2026-01-02"},
                    "connectors": [_SECRET_VALUE],
                },
            )

    asyncio.run(_call())

    pairs = _all_attr_values(in_memory_tracer)
    assert pairs, "expected at least one span with attributes"

    # 1. The literal secret value never appears anywhere.
    for key, value in pairs:
        assert _SECRET_VALUE not in str(value), f"secret leaked in attribute {key!r}"

    # 2. No attribute value matches the broad secret-like pattern (AC4 grep contract),
    #    except explicitly allowed attributes.
    for key, value in pairs:
        if key in _ALLOWED_LONG_ATTRS:
            continue
        assert not _SECRET_LIKE.search(str(value)), (
            f"attribute {key!r}={value!r} matches secret pattern -- AD-3 violation"
        )

    # 3. The string param is recorded as a shape (type+length), not plaintext.
    keys = {k for k, _ in pairs}
    assert "tool.params.connectors" in keys

    # 4. AC7: the middleware span carries the P1 gate data (token-burn split).
    mw = [s for s in in_memory_tracer.get_finished_spans() if s.name == "tool.get_daily_report"]
    assert mw, "expected the middleware tool.get_daily_report span"
    mw_attrs = dict(mw[0].attributes)
    assert mw_attrs["tool.name"] == "get_daily_report"
    assert "tool.latency_ms" in mw_attrs
    assert "tool.summary_token_estimate" in mw_attrs
    assert "tool.payload_bytes" in mw_attrs


def test_middleware_span_created_for_health(in_memory_tracer):
    async def _call():
        async with Client(mcp) as c:
            await c.call_tool("health", {"project_id": "default"})

    asyncio.run(_call())
    names = {s.name for s in in_memory_tracer.get_finished_spans()}
    assert "tool.health" in names
