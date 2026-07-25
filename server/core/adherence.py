"""toorow -- measured pre-query gate adherence (Story 11.6, AD-18).

The playbook's pre-query gate says an analysis agent should consult the governed
context layer (``search_context`` / ``get_procedure``) BEFORE it queries data
(``get_daily_report`` / ``get_report`` / ``get_card``). We do NOT own the host's
system prompt or the LLM's behaviour (NFR9: no Claude-only hack), so the gate can
only live in surfaces WE control -- tool DESCRIPTIONS and tool RESPONSES -- and its
truth can only be OBSERVED server-side.

This module is that observation seam. It is:

  * NON-BLOCKING (AD-18). A non-adherent data query still returns a normal,
    successful envelope. A hard gate would break hosts that do not iterate. We
    MEASURE the rate; Epic 14 judges it.
  * BEST-EFFORT / DEGRADE CLEAN (AI-34 partial, AI-13 N/A -- no external API).
    The primary sink is a Langfuse trace attribute (via ``core.tracing``); when
    Langfuse is not up we still write the trace attribute best-effort AND fall
    back to a Postgres row (``app.query_adherence``, migration 034). Any failure
    in either sink is swallowed -- recording adherence must NEVER fail a tool.

The "same session" key (the crux)
----------------------------------
There is NO reliable conversation identity across MCP tool calls (the host does
not hand us a stable chat id). So we define the session key as:

    trace parent + time window

concretely, in priority order:

  1. ``trace``       -- the current OTel trace_id (32-hex) when tracing is ON.
     All tool calls the host makes inside one client-side trace share this id
     (``core.tracing`` continues the client's W3C traceparent), so a context call
     and a later data call in the same turn collapse to the same key. This is the
     strongest signal and is used whenever it is available.
  2. ``time_window`` -- when tracing is OFF (no trace_id), we bucket by a coarse
     wall-clock window (``ADHERENCE_SESSION_WINDOW_SECONDS``, default 300s) SCOPED
     by ``project_id`` (``tw:{project}:{bucket}``). A context call and a data call
     within the same window AND the same project are treated as the same session.
     Folding the project into the key prevents a cross-tenant false-adherent: the
     ``_context_seen`` map is process-global, so without the project scope project
     A's search would mark the bucket and project B's data query in the same window
     would read as adherent. This is deliberately coarse: without conversation
     identity we can only approximate "recently consulted context". Known limit.

State lives in-process (a small dict keyed by session_key). At P3-dev the server
is single-replica; a Phase-B multi-replica deployment would move this signal to
Postgres/Redis (same TODO as the feedback rate-limiter in main.py). That does not
affect correctness of the MEASURE: a missed cross-replica context call simply
records adherent=false, never blocks.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# The two CORE-owned context tools that satisfy the gate (AD-2: core-owned, no
# module prefix). A call to either marks the session as "context consulted".
CONTEXT_TOOLS = ("search_context", "get_procedure")

# The three CORE-owned data tools the gate measures adherence FOR.
DATA_TOOLS = ("get_daily_report", "get_report", "get_card")

# Trace-attribute keys written on the data-query span (Langfuse via OTel). Kept
# under the ``gate.*`` namespace so they are easy to slice in Langfuse / Epic 14.
_ATTR_ADHERENT = "gate.adherent"
_ATTR_CONTEXT_TOOL = "gate.context_tool"
_ATTR_SESSION_KEY = "gate.session_key"
_ATTR_SESSION_KIND = "gate.session_kind"
_ATTR_DATA_TOOL = "gate.data_tool"


def _window_seconds() -> int:
    """Time-window bucket size for the ``time_window`` session key (default 300s)."""
    try:
        return max(1, int(os.environ.get("ADHERENCE_SESSION_WINDOW_SECONDS", "300")))
    except (ValueError, TypeError):
        return 300


# ---------------------------------------------------------------------------
# In-process session state: session_key -> {"context_tool": str, "seen_at": float}.
# Guarded by a lock (FastMCP runs sync tools on a worker-thread pool).
# ---------------------------------------------------------------------------
_lock = threading.Lock()
_context_seen: dict[str, dict[str, Any]] = {}


def reset_for_tests() -> None:
    """Clear the in-process context-seen map (test isolation)."""
    with _lock:
        _context_seen.clear()


def _now() -> float:
    return time.time()


def session_key(
    *, trace_id: str | None, project_id: str | None = None, now: float | None = None
) -> tuple[str, str]:
    """Return ``(session_key, session_kind)`` for the current call.

    ``trace_id`` (32-hex OTel id) yields a ``trace`` key when present; otherwise a
    coarse ``time_window`` bucket keyed on wall-clock / the configured window size.
    See the module docstring for why this is the best available "same session"
    proxy (no reliable conversation identity across MCP calls).

    The time-window key folds in ``project_id`` (``tw:{project}:{bucket}``) so two
    DIFFERENT projects that search/query in the same wall-clock window do NOT share
    a session bucket -- otherwise project A's context call would falsely mark project
    B's data query as adherent (the ``_context_seen`` map is process-global). Trace
    mode already isolates by the client's trace id and is unaffected.
    """
    if trace_id:
        return f"trace:{trace_id}", "trace"
    ts = _now() if now is None else now
    bucket = int(ts // _window_seconds())
    scope = project_id if project_id else "-"
    return f"tw:{scope}:{bucket}", "time_window"


def _prune(now: float) -> None:
    """Drop context-seen entries older than a few windows (bounded memory)."""
    horizon = now - (_window_seconds() * 4)
    stale = [k for k, v in _context_seen.items() if v.get("seen_at", 0.0) < horizon]
    for k in stale:
        _context_seen.pop(k, None)


def mark_context_call(
    tool_name: str,
    *,
    trace_id: str | None,
    project_id: str | None = None,
    now: float | None = None,
) -> None:
    """Record that a context tool (``search_context``/``get_procedure``) ran.

    Called from the context tools AFTER they resolve identity/trace so the mark
    lands under the SAME session key a subsequent data query will compute. No-op &
    never raises (best-effort observation). ``project_id`` scopes the time-window
    key so a mark from one project cannot make another project's data query
    adherent when tracing is off (see ``session_key``).
    """
    try:
        ts = _now() if now is None else now
        key, _kind = session_key(trace_id=trace_id, project_id=project_id, now=ts)
        with _lock:
            _prune(ts)
            _context_seen[key] = {"context_tool": tool_name, "seen_at": ts}
    except Exception as exc:  # noqa: BLE001 -- observation must never break a tool
        logger.debug("adherence: mark_context_call skipped: %s", exc)


def _lookup_context(key: str, *, now: float) -> str | None:
    """Return the context tool seen for *key* within the freshness horizon, else None."""
    entry = _context_seen.get(key)
    if not entry:
        return None
    # For the time_window key the bucket already bounds freshness; for the trace
    # key we still bound by a few windows so a stale process entry cannot leak a
    # false "adherent" across an unrelated later trace reuse.
    if entry.get("seen_at", 0.0) < now - (_window_seconds() * 4):
        return None
    return entry.get("context_tool")


def record_data_query(
    data_tool: str,
    *,
    project_id: str | None,
    trace_id: str | None,
    now: float | None = None,
) -> dict[str, Any]:
    """Measure + record adherence for one data query. NEVER blocks, NEVER raises.

    Computes the session key, checks whether a context call preceded this data
    query in the SAME session, and writes the verdict to BOTH sinks best-effort:
      1. the current tool span as ``gate.*`` attributes (Langfuse via core.tracing);
      2. an ``app.query_adherence`` row (Postgres fallback, migration 034).

    Returns the verdict dict (``{adherent, context_tool, session_key,
    session_kind, data_tool}``) so the caller can surface it in ``meta`` and tests
    can assert on it by value. A non-adherent query is recorded and the caller
    proceeds normally (AD-18: measured, never enforced).
    """
    ts = _now() if now is None else now
    key, kind = session_key(trace_id=trace_id, project_id=project_id, now=ts)
    with _lock:
        context_tool = _lookup_context(key, now=ts)
    verdict = {
        "adherent": context_tool is not None,
        "context_tool": context_tool,
        "session_key": key,
        "session_kind": kind,
        "data_tool": data_tool,
    }

    # Both sinks are best-effort; guard here too so record_data_query itself never
    # raises even if a patched/overridden sink misbehaves (AD-18: never blocks).
    try:
        _record_trace_attributes(verdict, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("adherence: trace sink raised: %s", exc)
    try:
        _record_postgres_fallback(verdict, project_id=project_id, trace_id=trace_id)
    except Exception as exc:  # noqa: BLE001
        logger.debug("adherence: postgres sink raised: %s", exc)
    return verdict


def _record_trace_attributes(verdict: dict[str, Any], *, trace_id: str | None) -> None:
    """Write the gate verdict onto the active tool span (best-effort, AI-34 partial).

    No-op & never raises when tracing is disabled or no span is active -- degrade
    cleanly without Langfuse (spec). ``core.tracing.record_current_span_attributes``
    already guards all of this.
    """
    try:
        from core import tracing  # noqa: PLC0415

        tracing.record_current_span_attributes(
            {
                _ATTR_ADHERENT: bool(verdict["adherent"]),
                _ATTR_CONTEXT_TOOL: verdict["context_tool"] or "",
                _ATTR_SESSION_KEY: verdict["session_key"],
                _ATTR_SESSION_KIND: verdict["session_kind"],
                _ATTR_DATA_TOOL: verdict["data_tool"],
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("adherence: trace-attribute record skipped: %s", exc)


def _record_postgres_fallback(
    verdict: dict[str, Any], *, project_id: str | None, trace_id: str | None
) -> None:
    """Insert an ``app.query_adherence`` row (Postgres fallback). Never raises.

    This is the degrade-clean sink for when Langfuse is not up (AI-34 partial): the
    adherence rate stays queryable from Postgres alone. A DB failure is swallowed so
    a data query NEVER fails because adherence could not be persisted (AD-18).

    RETENTION (deferred): ``app.query_adherence`` is append-only with no prune here,
    so it grows unbounded. A nightly prune of rows older than ~90 days is DEFERRED
    (single-tenant / negligible volume now) and documented in migration 034 so this
    is a tracked follow-up, not a silent unbounded-growth hole.
    """
    try:
        from ulid import ULID  # noqa: PLC0415

        from core import db as _core_db  # noqa: PLC0415

        row_id = f"adh_{ULID()}"
        with _core_db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.query_adherence
                        (id, project_id, session_key, session_kind, data_tool,
                         adherent, context_tool, trace_id, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, now())
                    """,
                    (
                        row_id,
                        project_id,
                        verdict["session_key"],
                        verdict["session_kind"],
                        verdict["data_tool"],
                        bool(verdict["adherent"]),
                        verdict["context_tool"],
                        trace_id or None,
                    ),
                )
            conn.commit()
    except Exception as exc:  # noqa: BLE001 -- fallback write must never break a query
        logger.debug("adherence: postgres fallback skipped: %s", exc)


# ---------------------------------------------------------------------------
# One-line pointer in data-tool summaries (AD-1: ~1 line, NEVER a dump).
# ---------------------------------------------------------------------------

# Hard ceiling shared with the data tools' summaries (AD-1 / NFR1).
_MAX_LINES = 30


def build_context_pointer(metric_definitions: dict | None, *, adherent: bool) -> str | None:
    """Return a ~1-line pointer to ``search_context`` (AD-1), or None.

    The gate's response-side nudge: when the queried report carries metric
    DEFINITIONS (served on the envelope via AI-50) AND the agent did NOT consult
    context first (``adherent`` is False), we emit ONE line naming a couple of the
    defined terms and pointing at ``search_context`` -- so the agent can go read
    the governed definition. It is a POINTER, never a dump of the definitions
    (AD-1: ~1 line). Returns None when there is nothing to point at or the agent
    already consulted context (no nag once adherent).
    """
    if adherent:
        return None
    if not isinstance(metric_definitions, dict) or not metric_definitions:
        return None
    terms = [str(k) for k in metric_definitions.keys() if k]
    if not terms:
        return None
    shown = ", ".join(terms[:3])
    more = " ..." if len(terms) > 3 else ""
    # ONE line. French-first (UX copy). ASCII-safe punctuation kept minimal.
    return (
        f"Astuce contexte : des definitions gouvernees existent pour {shown}{more} - "
        "appelez search_context avant d'interpreter."
    )


def append_pointer_within_cap(
    summary: str, pointer: str | None, *, max_lines: int = _MAX_LINES
) -> str:
    """Append *pointer* to *summary* while preserving the <= max_lines cap (AD-1).

    When adding the pointer would exceed the cap, the summary TAIL is trimmed (not
    the pointer) so the one-line nudge always survives and the total stays <=
    max_lines. Proven by test (the <=30-line limit holds with the pointer added).
    No-op when *pointer* is falsy.
    """
    if not pointer:
        return summary
    body = summary.rstrip("\n")
    lines = body.split("\n") if body else []
    # Reserve exactly one line for the pointer.
    if len(lines) + 1 > max_lines:
        lines = lines[: max_lines - 1]
    lines.append(pointer)
    return "\n".join(lines[:max_lines])
