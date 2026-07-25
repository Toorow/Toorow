"""toorow -- OTel/Langfuse tracing (Story 5.1).

Every MCP tool call, queue-worker pull, and dbt run can produce an OpenTelemetry
span exported (via OTLP HTTP) to a self-hosted Langfuse (AD-13, NFR7). Tracing is
OFF by default and fully optional:

  * ``TRACING_ENABLED`` (default ``false``) gates everything. When false, every
    public function here is a no-op returning ``None`` -- no exporter is built,
    no OTLP HTTP call is ever attempted (AC10).
  * The OpenTelemetry SDK is an OPTIONAL dependency (pyproject ``[tracing]`` extra).
    All SDK imports are guarded; the server starts normally when the SDK is
    absent from the venv (AC10).
  * No function here ever raises into a tool path -- tracing must never crash a
    tool (T2.2). Exporter/SDK errors are swallowed and logged at debug level.

AD-3 / AC4: ``_sanitize_params`` strips any parameter whose key looks like a
secret (token/key/secret/password/credential/auth) and records only value shapes
(type + length) for strings -- plaintext secret values NEVER reach a span.

AD-2: source-agnostic -- no module-specific strings here.
L-3 / AI-22: no ``http://``/``https://`` URL literals in this file (the Langfuse
endpoint is assembled from ``LANGFUSE_HOST`` at runtime).
"""

from __future__ import annotations

import base64
import contextvars
import logging
import os
import re
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

# Holds the active tool-call span created by TracingMiddleware. Read by
# record_current_span_attributes so the P1 gate data (AC7) lands on the span WE
# control and export, even across the async->sync thread boundary FastMCP uses to
# run sync tools (anyio copies contextvars into the worker thread). This decouples
# us from FastMCP's own built-in span, which lives on the global tracer provider.
_active_tool_span: contextvars.ContextVar = contextvars.ContextVar(
    "connector_active_tool_span", default=None
)

# ---------------------------------------------------------------------------
# Secret-shaped key detection (AD-3 / AC4)
# ---------------------------------------------------------------------------
# Case-insensitive: any param key containing one of these substrings has its
# value redacted before it can become a span attribute.
_SECRET_KEY_RE = re.compile(r"(token|secret|password|credential|api[_-]?key|auth)", re.IGNORECASE)
# A bare "key" match is also redacted (kept separate to avoid matching e.g. "monkey").
_SECRET_KEY_EXACT = re.compile(r"(^|[_-])key($|[_-])", re.IGNORECASE)

_REDACTED = "<redacted>"

# Langfuse OTLP traces path (relative -- host prefix assembled at runtime).
_OTLP_TRACES_PATH = "/api/public/otel/v1/traces"

# Module-level tracer provider handle (built once by init_tracing()).
_provider = None
_tracer = None
_initialized = False


# ---------------------------------------------------------------------------
# Environment guards
# ---------------------------------------------------------------------------


def is_enabled() -> bool:
    """Return True only when TRACING_ENABLED is truthy (default false -- AC10)."""
    return os.environ.get("TRACING_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _sample_rate() -> float:
    """Head-based sample rate (default 1.0). Clamped to [0.0, 1.0] (AC10)."""
    try:
        rate = float(os.environ.get("TRACING_SAMPLE_RATE", "1.0"))
    except (ValueError, TypeError):
        return 1.0
    return max(0.0, min(1.0, rate))


# ---------------------------------------------------------------------------
# Parameter sanitisation (AD-3 / AC4)
# ---------------------------------------------------------------------------


def _is_secret_key(key: str) -> bool:
    """True when *key* looks like it names a secret (token/key/secret/...)."""
    if not isinstance(key, str):
        return False
    return bool(_SECRET_KEY_RE.search(key) or _SECRET_KEY_EXACT.search(key))


def _shape(value: Any) -> Any:
    """Return a value SHAPE (never a raw secret string).

    - str  -> {"type": "str", "length": N}   (no plaintext -- AC4)
    - int/float/bool/None -> passed through (not secret-bearing)
    - dict -> recursively sanitised
    - list/tuple -> element-wise sanitised (capped)
    - other -> its type name
    """
    if value is None or isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str):
        return {"type": "str", "length": len(value)}
    if isinstance(value, dict):
        return _sanitize_params(value)
    if isinstance(value, (list, tuple)):
        return [_shape(v) for v in list(value)[:50]]
    return {"type": type(value).__name__}


def _sanitize_params(params: dict | None) -> dict:
    """Return a sanitised copy of *params* safe to record as span attributes.

    Any key that looks like a secret is replaced with ``"<redacted>"``. All other
    string values are reduced to a ``{type, length}`` shape so plaintext values
    (which may themselves be secrets even under an innocuous key) never land in a
    span attribute (AD-3 / AC4). Non-secret scalars pass through for usefulness.
    """
    if not isinstance(params, dict):
        return {}
    out: dict = {}
    for key, value in params.items():
        if _is_secret_key(str(key)):
            out[str(key)] = _REDACTED
        else:
            out[str(key)] = _shape(value)
    return out


# ---------------------------------------------------------------------------
# Initialisation
# ---------------------------------------------------------------------------


def init_tracing(*, force: bool = False):
    """Initialise the OTel tracer provider + Langfuse OTLP exporter.

    No-op (returns None) when TRACING_ENABLED is false OR the OTel SDK is absent.
    Never raises -- any SDK/exporter error is logged at debug and swallowed so the
    server keeps starting (AC10, T2.2).

    Returns the tracer provider on success, else None. Idempotent unless *force*.
    """
    global _provider, _tracer, _initialized

    if _initialized and not force:
        return _provider

    if not is_enabled():
        _initialized = True
        _provider = None
        _tracer = None
        logger.debug("tracing: disabled (TRACING_ENABLED not truthy) -- no exporter")
        return None

    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
        from opentelemetry.sdk.trace.sampling import (  # noqa: PLC0415
            ParentBased,
            TraceIdRatioBased,
        )
    except Exception as exc:  # SDK not installed -- optional dependency (AC10)
        logger.warning(
            "tracing: OpenTelemetry SDK not available (%s) -- tracing disabled. "
            "Install the [tracing] extra to enable.",
            exc,
        )
        _initialized = True
        _provider = None
        _tracer = None
        return None

    try:
        service_name = os.environ.get("OTEL_SERVICE_NAME", "connector")
        resource = Resource.create({"service.name": service_name})
        sampler = ParentBased(root=TraceIdRatioBased(_sample_rate()))
        provider = TracerProvider(resource=resource, sampler=sampler)

        exporter = _build_langfuse_exporter()
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter))
        else:
            logger.warning(
                "tracing: enabled but LANGFUSE_HOST/keys incomplete -- spans "
                "created but not exported (no OTLP endpoint configured)."
            )

        trace.set_tracer_provider(provider)
        _provider = provider
        _tracer = trace.get_tracer("connector.core")
        _initialized = True
        logger.info("tracing: initialised (sample_rate=%s)", _sample_rate())
        return provider
    except Exception as exc:
        logger.warning("tracing: init failed (%s) -- tracing disabled", exc)
        _initialized = True
        _provider = None
        _tracer = None
        return None


def _build_langfuse_exporter():
    """Build the Langfuse OTLP HTTP span exporter from env, or None if unconfigured.

    Reads LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY, LANGFUSE_SECRET_KEY. The endpoint is
    ``<LANGFUSE_HOST>/api/public/otel/v1/traces`` with HTTP Basic auth built from
    the public/secret key pair (per Langfuse OTel docs). No URL literal is hardcoded
    (AI-22): the host comes entirely from the environment.
    """
    host = os.environ.get("LANGFUSE_HOST", "").strip().rstrip("/")
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not host or not public_key or not secret_key:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (  # noqa: PLC0415
            OTLPSpanExporter,
        )
    except Exception as exc:
        logger.warning("tracing: OTLP HTTP exporter unavailable (%s)", exc)
        return None

    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return OTLPSpanExporter(
        endpoint=f"{host}{_OTLP_TRACES_PATH}",
        headers={"Authorization": f"Basic {token}"},
    )


def get_tracer():
    """Return the initialised tracer, or None when tracing is disabled/unavailable."""
    if not _initialized:
        init_tracing()
    return _tracer


def reset_for_tests() -> None:
    """Reset module state so a test can re-init with a fresh exporter/provider."""
    global _provider, _tracer, _initialized
    _provider = None
    _tracer = None
    _initialized = False
    _active_tool_span.set(None)


# ---------------------------------------------------------------------------
# W3C traceparent extraction (AC3)
# ---------------------------------------------------------------------------

_TRACEPARENT_RE = re.compile(r"^[0-9a-f]{2}-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$")


def _extract_parent_context(meta: dict | None):
    """Return an OTel Context carrying the client's traceparent, or None.

    Validates the W3C ``00-<32hex>-<16hex>-<2hex>`` shape before use; an invalid or
    absent value yields None so the caller starts a fresh root span (AC3).
    """
    if not isinstance(meta, dict):
        return None
    traceparent = meta.get("traceparent")
    if not isinstance(traceparent, str) or not _TRACEPARENT_RE.match(traceparent.lower()):
        return None
    try:
        from opentelemetry.trace.propagation.tracecontext import (  # noqa: PLC0415
            TraceContextTextMapPropagator,
        )

        return TraceContextTextMapPropagator().extract({"traceparent": traceparent})
    except Exception as exc:
        logger.debug("tracing: traceparent extract failed (%s)", exc)
        return None


# ---------------------------------------------------------------------------
# Public span API -- never raises (T2.2)
# ---------------------------------------------------------------------------


@contextmanager
def tool_span(tool_name: str, params: dict | None = None, *, meta: dict | None = None):
    """Context manager producing a root/child span for one MCP tool call.

    - Extracts a W3C ``traceparent`` from *meta* (``ctx._meta``) to CONTINUE the
      client-side trace; starts a fresh root span when absent/invalid (AC3).
    - Records ``tool.name`` and sanitised ``tool.params`` (AD-3 / AC4) up front.
    - Yields a lightweight handle exposing ``set(key, value)`` so the caller can
      attach latency / token / payload attributes after the tool returns (AC2/AC7).

    A no-op (yields an inert handle) when tracing is disabled/unavailable. Never
    raises into the tool path (T2.2).
    """
    handle = _SpanHandle(None)
    tracer = get_tracer()
    if tracer is None:
        yield handle
        return

    parent_ctx = _extract_parent_context(meta)
    try:
        from opentelemetry.trace import SpanKind  # noqa: PLC0415

        span_cm = tracer.start_as_current_span(
            f"tool.{tool_name}", context=parent_ctx, kind=SpanKind.SERVER
        )
    except Exception as exc:
        logger.debug("tracing: failed to start span for %s (%s)", tool_name, exc)
        yield handle
        return

    try:
        with span_cm as span:
            handle = _SpanHandle(span)
            _token = _active_tool_span.set(span)
            try:
                handle.set("tool.name", tool_name)
                sanitized = _sanitize_params(params)
                for k, v in sanitized.items():
                    handle.set(f"tool.params.{k}", v)
                yield handle
            finally:
                try:
                    _active_tool_span.reset(_token)
                except Exception:  # noqa: BLE001
                    pass
    except Exception as exc:  # noqa: BLE001 -- tracing must not crash the tool
        logger.debug("tracing: tool_span error for %s (%s)", tool_name, exc)
        yield handle


@contextmanager
def worker_span(name: str, attributes: dict | None = None, *, parent_meta: dict | None = None):
    """Context manager for a queue-worker / dbt span (AC5, AC6).

    *parent_meta* may carry a ``traceparent`` (reconstructed from a stored trace_id)
    so the worker span joins the originating trace tree. No-op when disabled.
    """
    handle = _SpanHandle(None)
    tracer = get_tracer()
    if tracer is None:
        yield handle
        return
    parent_ctx = _extract_parent_context(parent_meta)
    try:
        span_cm = tracer.start_as_current_span(name, context=parent_ctx)
    except Exception as exc:
        logger.debug("tracing: failed to start worker span %s (%s)", name, exc)
        yield handle
        return
    try:
        with span_cm as span:
            handle = _SpanHandle(span)
            for k, v in (attributes or {}).items():
                handle.set(k, v)
            yield handle
    except Exception as exc:  # noqa: BLE001
        logger.debug("tracing: worker_span error for %s (%s)", name, exc)
        yield handle


def current_trace_id_hex() -> str | None:
    """Return the current span's 32-hex trace_id, or None when no span is active.

    Used at enqueue time to persist ``trace_id`` on the pull_jobs row (AC5) so the
    worker (running in a separate thread) can continue the same trace tree.
    """
    if not is_enabled():
        return None
    try:
        from opentelemetry import trace  # noqa: PLC0415

        span = trace.get_current_span()
        ctx = span.get_span_context() if span else None
        if ctx is None or not ctx.is_valid:
            return None
        return format(ctx.trace_id, "032x")
    except Exception:
        return None


def traceparent_from_trace_id(trace_id_hex: str | None) -> str | None:
    """Build a synthetic W3C traceparent from a stored 32-hex trace_id (AC5).

    The worker stores only the trace_id (not the full span context), so we
    reconstruct a traceparent with a fresh-but-deterministic-enough parent span id.
    Returns None on invalid input.
    """
    if not isinstance(trace_id_hex, str) or not re.match(r"^[0-9a-f]{32}$", trace_id_hex.lower()):
        return None
    # Parent span id is unknown across the thread boundary; use a non-zero placeholder
    # so the traceparent is well-formed and the worker span links to the trace_id.
    return f"00-{trace_id_hex.lower()}-{'0' * 15}1-01"


def record_current_span_attributes(attributes: dict) -> None:
    """Set *attributes* on the active tool-call span, if any (AC7).

    Used by ``metrics.log_tool_metrics`` to surface the P1 gate figures on the root
    tool span created by :class:`TracingMiddleware`, without the metrics layer needing
    a span handle. Prefers the middleware span stored in ``_active_tool_span`` (the
    span WE export) and falls back to the OTel current span. No-op & never raises when
    tracing is disabled or no span is active.
    """
    if not is_enabled():
        return
    try:
        span = _active_tool_span.get()
        if span is None:
            from opentelemetry import trace  # noqa: PLC0415

            span = trace.get_current_span()
        if span is None:
            return
        ctx = span.get_span_context()
        if ctx is None or not ctx.is_valid:
            return
        for key, value in attributes.items():
            try:
                span.set_attribute(key, value)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        logger.debug("tracing: record_current_span_attributes failed (%s)", exc)


def build_middleware():
    """Return a FastMCP ``TracingMiddleware`` instance, or None when unavailable.

    Registered on the FastMCP app so EVERY tool call -- core and module-namespaced
    -- produces a root span (AC2, T3.3). Returns None when the FastMCP middleware
    base is unimportable so app construction never fails.
    """
    try:
        from fastmcp.server.middleware import Middleware  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("tracing: FastMCP middleware base unavailable (%s)", exc)
        return None

    class TracingMiddleware(Middleware):
        """Universal per-tool-call tracing (AC2, AC3, AC4).

        - Extracts a W3C ``traceparent`` from the call's ``_meta`` to CONTINUE the
          client trace; fresh root span otherwise (AC3).
        - Records ``tool.name`` + sanitised ``tool.params`` (AD-3 / AC4) and
          ``tool.latency_ms``. On error, marks the span and re-raises.
        - No-op passthrough when tracing is disabled/unavailable; never swallows the
          tool's own result or exception beyond span bookkeeping.
        """

        async def on_call_tool(self, context, call_next):
            tracer = get_tracer()
            if tracer is None:
                return await call_next(context)

            msg = getattr(context, "message", None)
            tool_name = getattr(msg, "name", None) or "unknown_tool"
            arguments = getattr(msg, "arguments", None) or {}
            meta = getattr(msg, "meta", None)
            parent_ctx = _extract_parent_context(meta if isinstance(meta, dict) else None)

            import time as _time  # noqa: PLC0415

            try:
                from opentelemetry.trace import SpanKind, Status, StatusCode  # noqa: PLC0415
            except Exception:  # noqa: BLE001
                return await call_next(context)

            t0 = _time.perf_counter()
            with tracer.start_as_current_span(
                f"tool.{tool_name}", context=parent_ctx, kind=SpanKind.SERVER
            ) as span:
                # Publish OUR span so log_tool_metrics (AC7) attaches the P1 gate data
                # here, decoupled from FastMCP's own built-in span. Reset on exit.
                _token = _active_tool_span.set(span)
                try:
                    span.set_attribute("tool.name", tool_name)
                    for k, v in _sanitize_params(arguments).items():
                        _set_primitive(span, f"tool.params.{k}", v)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    result = await call_next(context)
                except Exception as exc:
                    try:
                        span.set_status(Status(StatusCode.ERROR, str(exc)))
                        span.set_attribute("tool.error", type(exc).__name__)
                    except Exception:  # noqa: BLE001
                        pass
                    raise
                finally:
                    try:
                        span.set_attribute(
                            "tool.latency_ms", int((_time.perf_counter() - t0) * 1000)
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    try:
                        _active_tool_span.reset(_token)
                    except Exception:  # noqa: BLE001
                        pass
                return result

    return TracingMiddleware()


def _set_primitive(span, key: str, value: Any) -> None:
    """Set a span attribute, coercing dict/list values to OTel-safe primitives."""
    try:
        if isinstance(value, dict):
            span.set_attribute(key, str(value))
        elif isinstance(value, (list, tuple)):
            span.set_attribute(key, [str(v) for v in value])
        else:
            span.set_attribute(key, value)
    except Exception:  # noqa: BLE001
        pass


class _SpanHandle:
    """Thin wrapper so callers set attributes without importing OTel types.

    ``set()`` is a hard no-op when the wrapped span is None (tracing disabled) and
    never raises (T2.2).
    """

    __slots__ = ("_span",)

    def __init__(self, span):
        self._span = span

    @property
    def active(self) -> bool:
        return self._span is not None

    def set(self, key: str, value: Any) -> None:
        span = self._span
        if span is None:
            return
        try:
            if isinstance(value, dict):
                # OTel attributes must be primitives -- flatten dict values to a
                # stable string shape (already secret-free via _sanitize_params).
                span.set_attribute(key, str(value))
            elif isinstance(value, (list, tuple)):
                span.set_attribute(key, [str(v) for v in value])
            else:
                span.set_attribute(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.debug("tracing: set_attribute(%s) failed (%s)", key, exc)
