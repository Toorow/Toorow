"""toorow -- measured pre-query gate adherence (Story 11.6, AD-18).

The playbook's pre-query gate says an analysis agent should consult the governed
context layer (``CONTEXT_TOOLS`` below: ``search_context``, ``get_procedure``,
``resolve_business_path``, ``get_knowledge``) BEFORE it asks a data question
(``DATA_TOOLS`` below: the report and card tools, the Analyze Result tools and
the Datastream report -- every registered ``read`` tool that hands the model
figures; `analyze-and-test.md`, amendment of 2026-09-01). We do NOT own the host's
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
not hand us a stable chat id), and nothing persisted here carries a run id or a
question id: ``app.query_adherence`` stores ``session_key``, ``session_kind``,
``data_tool``, ``adherent``, ``context_tool`` and ``trace_id``, and the MCP
surface offers no correlation field at all beyond the W3C ``traceparent`` the
host may send. So the pair is keyed on the only two things the data permits:

  1. ``trace``       -- the current OTel trace_id (32-hex) when tracing is ON.
     All tool calls the host makes inside one client-side trace share this id
     (``core.tracing`` continues the client's W3C traceparent), so a context call
     and a later data call in the same turn collapse to the same key. This is the
     strongest signal and is used whenever it is available.
  2. ``time_window`` -- when tracing is OFF (no trace_id), the pair is keyed on
     THE CONSULT THAT OPENED IT, not on the clock. ``mark_context_call`` opens an
     exchange for ``tw:{identity}:{project}`` and stamps it with its own instant;
     a data query for that same caller and project joins that exchange while the
     consult is still fresh (``ADHERENCE_SESSION_WINDOW_SECONDS``, default 300s
     of ELAPSED time). Folding the project in prevents a cross-tenant
     false-adherent (the ``_context_seen`` map is process-global); folding the
     IDENTITY in prevents the same failure between two operators of one project
     -- without it, one person running ``search_context`` made everyone else's
     data query in that window read as adherent, which is the case
     `context-hub.md` names: "context adherence is recorded for a caller that did
     not itself consult context".

     THIS USED TO BE A WALL-CLOCK BUCKET (``tw:{identity}:{project}:{bucket}``
     with ``bucket = now // 300``), and a bucket makes the measure depend on
     WHERE the boundary falls rather than on how much time separated the two
     calls. Two calls a millisecond apart across a boundary read as
     not-adherent; two calls 299 seconds apart read as adherent. Every run
     longer than one window crossed exactly one boundary, so one question of a
     replay flipped: measured 2026-08-24 on one corpus against one database,
     PASS=21 FAIL=1 then PASS=22 FAIL=0 (AI-316). Elapsed freshness is what the
     basis always CLAIMED to mean -- "the same person made them in the same
     project within a few minutes" -- and the bucket was the one thing that made
     that sentence false.

     The recorded ``session_key`` is then the exchange -- ``tw:{who}:{scope}@{ms
     of the consult}`` -- so two questions replayed back to back are two keys,
     and a query with no fresh consult gets its own unpaired exchange rather
     than borrowing a neighbour's bucket.

State lives in-process (a small dict keyed by the pairing key). At P3-dev the
server is single-replica; a Phase-B multi-replica deployment would move this
signal to Postgres/Redis (same TODO as the feedback rate-limiter in main.py).
That does not affect correctness of the MEASURE: a missed cross-replica context
call simply records adherent=false, never blocks.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# The CORE-owned context tools that satisfy the gate (AD-2: core-owned, no
# module prefix). A call to any of them marks the session as "context consulted".
#
# `get_knowledge` joined on 2026-09-01 (`analyze-and-test.md`, amendment of that
# date): it is the second call of every observed Analyze session and the gate
# did not count it, so a caller who did exactly what the Skill said read as
# non-adherent. The rule is the one the Skills actually direct a caller through,
# and `tests/conformance/test_every_data_question_is_measured.py` holds this
# tuple equal to the registered tools that call `mark_context_call`.
CONTEXT_TOOLS = ("search_context", "get_procedure", "resolve_business_path", "get_knowledge")

# The CORE-owned data tools the gate measures adherence FOR: every registered
# `read` tool that hands the model figures from the Project's measured data.
#
# THIS TUPLE IS HELD AGAINST THE INVENTORY, NOT WRITTEN BY HAND. It said three
# names for four months while the Analyze path -- the door every current Skill
# sends a caller through -- produced Results nothing measured; measured
# 2026-09-01, three real sessions and "No adherence observation". The
# conformance test named above derives the `read` tools whose call closure
# reaches a figure composer and requires this tuple to equal them, both ways.
DATA_TOOLS = (
    "get_daily_report",
    "get_report",
    "get_card",
    "execute_analyze_query_spec",
    "explore_analyze_query",
    "analyze_result",
    "render_analyze_result",
    "get_datastream_report",
)

# Trace-attribute keys written on the data-query span (Langfuse via OTel). Kept
# under the ``gate.*`` namespace so they are easy to slice in Langfuse / Epic 14.
_ATTR_ADHERENT = "gate.adherent"
_ATTR_CONTEXT_TOOL = "gate.context_tool"
_ATTR_SESSION_KEY = "gate.session_key"
_ATTR_SESSION_KIND = "gate.session_kind"
_ATTR_DATA_TOOL = "gate.data_tool"


def _window_seconds() -> int:
    """How long a context consult stays fresh, in ELAPSED seconds (default 300s).

    Not a bucket size. It bounds the gap between the consult and the data query,
    which is the only thing the ``time_window`` basis ever claimed to measure.
    """
    try:
        return max(1, int(os.environ.get("ADHERENCE_SESSION_WINDOW_SECONDS", "300")))
    except (ValueError, TypeError):
        return 300


# ---------------------------------------------------------------------------
# In-process session state: pairing_key ->
#   {"context_tool": str, "seen_at": float, "exchange": str}.
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


def current_exchange_trace_id() -> str | None:
    """The trace id BOTH halves of an exchange must read (2026-09-01).

    The consult mark and the data verdict used to take the trace from the active
    OTel span alone, while the AI Path recorder keyed the observed path on the
    client's W3C ``traceparent`` read from the call's ``_meta``. With tracing off
    the two halves fell to the wall-clock inference although the client had
    named the exchange on every call -- and `observed_path_for`, which looks the
    path up by THAT id, found nothing to put beside the boolean.

    One function, so the two halves cannot drift again: the client's traceparent
    as the middleware read it for this call, else the active span. Never raises.
    """
    try:
        from core import ai_path_recorder  # noqa: PLC0415

        traced = ai_path_recorder.current_call_trace_id()
        if traced:
            return traced
    except Exception as exc:  # noqa: BLE001 -- observation must never break a tool
        logger.debug("adherence: call trace id unreadable: %s", exc)
    try:
        from core import tracing  # noqa: PLC0415

        return tracing.current_trace_id_hex() or None
    except Exception as exc:  # noqa: BLE001
        logger.debug("adherence: span trace id unreadable: %s", exc)
        return None


def session_key(
    *,
    trace_id: str | None,
    project_id: str | None = None,
    identity: str | None = None,
) -> tuple[str, str]:
    """Return ``(pairing_key, session_kind)`` -- WHERE a consult and a query meet.

    This is the slot the two calls have to share, not the identity of the
    exchange they end up in: the exchange is named by the consult that opened it
    (see ``_exchange_id``), so that two questions replayed one after the other
    are two exchanges instead of two neighbours of one clock bucket.

    ``trace_id`` (32-hex OTel id) yields a ``trace`` key when present -- the host
    already told us the two calls are one exchange, and nothing needs inferring.
    Otherwise the slot is ``tw:{identity}:{project}``, and NO CLOCK ENTERS IT.

    The slot folds in ``project_id`` AND ``identity``. The project scope came
    first, so that project A's context call could not mark project B's data query
    as adherent -- the ``_context_seen`` map is process-global. It stopped there,
    and the missing dimension was the caller: two DIFFERENT identities in the
    SAME project still shared a slot, so one operator running ``search_context``
    made another operator's data query count as adherent although that operator
    never consulted anything.

    `context-hub.md` names exactly that: "context adherence is recorded for a
    caller that did not itself consult context". Trace mode already isolates by
    the client's trace id and is unaffected.

    ``identity`` is optional so a caller that genuinely has none degrades to the
    previous, wider slot instead of silently keying on the string ``"None"``.
    """
    if trace_id:
        return f"trace:{trace_id}", "trace"
    scope = project_id if project_id else "-"
    who = identity if identity else "-"
    return f"tw:{who}:{scope}", "time_window"


def _exchange_id(pairing_key: str, kind: str, opened_at: float) -> str:
    """Name the exchange a consult opens, or the one a lone query stands in.

    A trace IS the exchange, so its key is left exactly as the host sent it
    (``trace:<32-hex>``; ``test_no_token_in_spans`` allows that one long span
    attribute by that exact shape). Without a trace the exchange is named by the
    instant the consult happened, which is the closest thing to a run or a
    question identifier the persisted data permits.
    """
    if kind == "trace":
        return pairing_key
    return f"{pairing_key}@{int(opened_at * 1000)}"


def _freshness_seconds(kind: str) -> float:
    """How long a consult can precede a query and still be the same exchange.

    ``time_window`` is bounded by the configured window itself -- nothing else
    bounds it now that the bucket is gone. ``trace`` keeps the wider few-window
    horizon it always had: the trace already proves the two calls belong
    together, and the bound only stops a stale process entry from leaking a false
    "adherent" into an unrelated later reuse of the same id.
    """
    window = _window_seconds()
    return float(window) if kind == "time_window" else float(window * 4)


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
    identity: str | None = None,
    now: float | None = None,
) -> None:
    """Record that a context tool (``search_context``/``get_procedure``) ran.

    OPENS AN EXCHANGE. Called from the context tools AFTER they resolve
    identity/trace so the mark lands in the SAME slot a subsequent data query will
    compute, and the exchange it opens is named by THIS instant -- so a replay
    that asks twenty questions in a row produces twenty exchanges, not one bucket
    that happens to hold them. No-op & never raises (best-effort observation).
    ``project_id`` scopes the slot so a mark from one project cannot make another
    project's data query adherent when tracing is off, and ``identity`` scopes it
    so one operator's context call cannot make ANOTHER operator's query adherent
    (see ``session_key``).
    """
    try:
        ts = _now() if now is None else now
        key, kind = session_key(
            trace_id=trace_id, project_id=project_id, identity=identity
        )
        with _lock:
            _prune(ts)
            _context_seen[key] = {
                "context_tool": tool_name,
                "seen_at": ts,
                "exchange": _exchange_id(key, kind, ts),
            }
    except Exception as exc:  # noqa: BLE001 -- observation must never break a tool
        logger.debug("adherence: mark_context_call skipped: %s", exc)


def _open_exchange(key: str, kind: str, *, now: float) -> dict[str, Any] | None:
    """The exchange still open in *key*, or None when nothing fresh sits there.

    Freshness is ELAPSED time since the consult -- ``_freshness_seconds`` -- and
    never a boundary in absolute time. That is the whole of AI-316: a boundary
    made the verdict depend on when the run started rather than on how long the
    caller waited.
    """
    entry = _context_seen.get(key)
    if not entry:
        return None
    if now - float(entry.get("seen_at", 0.0)) > _freshness_seconds(kind):
        return None
    return entry



def observed_path_for(project_id: str | None, trace_id: str | None) -> dict[str, Any] | None:
    """WHAT the caller actually consulted under this trace, or None (AI-84).

    ADHERENCE KNOWS *THAT*, THE PATH KNOWS *WHAT*. `record_data_query` answers
    « did a context call precede this one in the same session », and in
    `time_window` mode "the same session" is a consult less than 300 seconds old
    from the same caller in the same project -- an inference, and the module
    docstring says so plainly.

    `ai_path_recorder` already writes, under the SAME trace id the client sends,
    which nodes were consulted. So when a trace exists there is no need to infer
    at all: the steps ARE the evidence. This reads them and hands them to the
    caller beside the boolean.

    IT NEVER REPLACES THE BOOLEAN, and that is deliberate. A path with zero steps
    and a path that could not be read are different things, and a verdict that
    silently swapped its own basis would make a measured adherence and an
    inferred one indistinguishable -- which is the defect this whole module
    exists to avoid measuring badly.

    None means "no trace, or nothing recorded under it". Best-effort like every
    sink here: a database that will not answer must never make a data query fail
    (AD-18, measured and never enforced).
    """
    if not trace_id or not project_id:
        return None
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.step_kind, s.owner_object_type, s.owner_object_id, s.tool_name
                  FROM app.ai_path_steps s
                  JOIN app.ai_paths p ON p.id = s.path_id
                 WHERE p.project_id = %s AND p.w3c_trace_id = %s
                 ORDER BY s.ordinal
                """,
                (project_id, trace_id),
            )
            rows = cur.fetchall()
    except Exception as exc:  # noqa: BLE001 -- never blocks a data query
        logger.debug("adherence: observed path unreadable: %s", exc)
        return None
    if not rows:
        return None
    return {
        "step_count": len(rows),
        "steps": [
            {
                "kind": row[0],
                "object_type": row[1],
                "object_id": row[2],
                "tool": row[3],
            }
            for row in rows
        ],
    }


def record_data_query(
    data_tool: str,
    *,
    project_id: str | None,
    trace_id: str | None,
    identity: str | None = None,
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
    key, kind = session_key(trace_id=trace_id, project_id=project_id, identity=identity)
    with _lock:
        entry = _open_exchange(key, kind, now=ts)
    context_tool = entry.get("context_tool") if entry else None
    # The exchange the consult opened, or -- when nothing preceded this query --
    # one of its own. A query with no consult never borrows a neighbour's key.
    exchange = entry.get("exchange") if entry else _exchange_id(key, kind, ts)
    verdict = {
        "adherent": context_tool is not None,
        "context_tool": context_tool,
        "session_key": exchange,
        "session_kind": kind,
        "data_tool": data_tool,
    }

    # AI-84 -- LE CHEMIN OBSERVE, A COTE DU BOOLEEN.
    #
    # `adherent` repond « quelque chose a-t-il ete consulte avant », et en mode
    # `time_window` cette reponse vient d une consultation vieille de moins de 300
    # secondes : c est une INFERENCE. Le chemin d IA, lui, sait QUOI a ete
    # consulte, enregistre sous le
    # meme trace id -- donc quand un trace existe, il n y a plus rien a inferer.
    #
    # Il ne REMPLACE pas le booleen. Un lecteur doit pouvoir distinguer une
    # adherence mesuree d une adherence inferee, et une substitution silencieuse
    # les rendrait identiques -- exactement le defaut que ce module existe pour
    # ne pas commettre.
    verdict["observed_path"] = observed_path_for(project_id, trace_id)
    # Sur quelle BASE le booleen repose. Dit ici plutot que deduit de
    # `session_kind` par chaque lecteur : trois lecteurs, trois deductions, et la
    # premiere qui se trompe le fait en silence.
    verdict["adherence_basis"] = (
        "observed_ai_path"
        if verdict["observed_path"]
        else ("trace_session" if kind == "trace" else "time_window_inference")
    )

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
# Reading what was measured. A measure with no reader is a measure nobody keeps.
# ---------------------------------------------------------------------------

#: The window a reader gets when it names none. Long enough to survive a quiet
#: week, short enough that "recently" still means something.
ADHERENCE_DEFAULT_WINDOW_DAYS = 30
ADHERENCE_MAX_WINDOW_DAYS = 365

#: What a row's `session_kind` is worth as evidence. The column stores HOW the
#: session was identified; this says what that identification proves, once,
#: rather than leaving three readers to deduce it three times -- and the first
#: one to deduce it wrongly would do so in silence.
BASIS_OBSERVED_SESSION = "observed_session"
BASIS_INFERRED_WINDOW = "inferred_window"
_BASIS_BY_SESSION_KIND = {"trace": BASIS_OBSERVED_SESSION, "time_window": BASIS_INFERRED_WINDOW}

_BASIS_MEANING = {
    BASIS_OBSERVED_SESSION: (
        "the context call and the data query carried the same trace from the host, "
        "so they are known to be one exchange"
    ),
    BASIS_INFERRED_WINDOW: (
        "no trace was sent, so the two calls were treated as one exchange because "
        "the same person made them in the same project within a few minutes"
    ),
}


def _window_bounds(conn, days: int) -> tuple[int, Any, Any]:
    bounded = max(1, min(int(days), ADHERENCE_MAX_WINDOW_DAYS))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT now() - make_interval(days => %s), now()",
            (bounded,),
        )
        row = cur.fetchone()
    return bounded, row[0], row[1]


def adherence_overview(
    conn, *, project_id: str, days: int = ADHERENCE_DEFAULT_WINDOW_DAYS
) -> dict[str, Any]:
    """What the pre-query gate measured in this Project over a window.

    THE READER THIS MEASURE NEVER HAD. `app.query_adherence` has been written
    since migration 034 and read by nothing: no API, no screen, no tool. A
    measurement with no reader cannot be acted on, cannot be checked, and cannot
    even be noticed when it stops arriving -- which is the state it was in.

    COUNTS, WITH THEIR DENOMINATOR. Every bucket carries `observations`,
    `adherent` and `not_adherent`, and a `share` only where the denominator is
    beside it. `adherence_basis` is not stored on the row -- it is decided at
    write time from the trace -- so this reports the basis the column CAN
    support (`session_kind`) and says plainly what each one proves. It never
    reports a row as observed-path evidence, because the row does not know.

    NO FIGURE HERE MERGES THE TWO BASES. `analyze-and-test.md:1330` says a
    trace-identified session and a wall-clock inference "are reported apart and
    never merged", and this payload used to carry two merges: a pooled top-level
    `adherent`/`share_adherent` summed across every `session_kind`, and a
    `by_data_tool` bucket filled from the same rows without splitting the basis.
    The console rendered the first as the "All questions" row at the top of the
    table, so the headline figure a reader met FIRST was the one inference merged
    into the measures. Both are gone: `by_data_tool` is now one bucket per data
    tool AND basis, and the only totals are `by_basis`.

    `observations` survives at the top level because it is a count of
    measurements taken and carries no verdict beside it -- there is no
    `adherent`, so no share can be built from it and no inference can pass for a
    measure. It is what `empty_state` is decided on.

    A read, and only a read. Nothing here writes, prunes or repairs.
    """
    bounded, window_from, window_to = _window_bounds(conn, days)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT data_tool, session_kind, adherent, COUNT(*),
                   MIN(created_at), MAX(created_at)
              FROM app.query_adherence
             WHERE project_id = %s AND created_at >= %s
             GROUP BY data_tool, session_kind, adherent
            """,
            (project_id, window_from),
        )
        rows = cur.fetchall()
        cur.execute(
            """
            SELECT context_tool, COUNT(*)
              FROM app.query_adherence
             WHERE project_id = %s AND created_at >= %s
               AND adherent AND context_tool IS NOT NULL
             GROUP BY context_tool
             ORDER BY COUNT(*) DESC, context_tool
            """,
            (project_id, window_from),
        )
        context_rows = cur.fetchall()
        # WHAT THE WINDOW DID NOT SEE (2026-09-01). The AI Path recorder writes a
        # step for every tool call it observes, data tools included, whether or
        # not the gate measured them. Counting those steps is what lets an empty
        # window say "no data question was asked" or "data questions were asked
        # and none was measured" instead of one "0" for both -- the two readings
        # three real Analyze sessions could not be told apart by.
        cur.execute(
            """
            SELECT COUNT(*)
              FROM app.ai_path_steps
             WHERE project_id = %s AND observed_at >= %s
               AND tool_name = ANY(%s)
            """,
            (project_id, window_from, list(DATA_TOOLS)),
        )
        observed_row = cur.fetchone()
    data_tool_calls_observed = int((observed_row or (0,))[0] or 0)

    # Keyed by (data_tool, basis) and by basis. NEVER by data_tool alone: a
    # bucket keyed on the tool is a bucket that has already merged the two bases
    # by the time anyone reads it.
    by_tool: dict[tuple[str, str], dict[str, int]] = {}
    by_basis: dict[str, dict[str, int]] = {}
    measurements = 0
    first_seen = last_seen = None
    for data_tool, session_kind, adherent, count, oldest, newest in rows:
        basis = _BASIS_BY_SESSION_KIND.get(str(session_kind), BASIS_INFERRED_WINDOW)
        for bucket, key in (
            (by_tool, (str(data_tool), basis)),
            (by_basis, basis),
        ):
            entry = bucket.setdefault(
                key, {"observations": 0, "adherent": 0, "not_adherent": 0}
            )
            entry["observations"] += count
            entry["adherent" if adherent else "not_adherent"] += count
        measurements += count
        first_seen = oldest if first_seen is None or oldest < first_seen else first_seen
        last_seen = newest if last_seen is None or newest > last_seen else last_seen

    def _with_share(entry: dict[str, int], **named: str) -> dict[str, Any]:
        return {
            **named,
            **entry,
            # The denominator travels in the same object, always. A share alone
            # over four observations reads exactly like a share over four hundred.
            "share_adherent": (
                round(entry["adherent"] / entry["observations"], 4)
                if entry["observations"]
                else None
            ),
        }

    payload: dict[str, Any] = {
        "project_id": project_id,
        "window": {
            "days": bounded,
            "from": window_from.isoformat() if hasattr(window_from, "isoformat") else window_from,
            "to": window_to.isoformat() if hasattr(window_to, "isoformat") else window_to,
        },
        # A count of measurements taken, with no verdict beside it. There is no
        # top-level `adherent` and no top-level `share_adherent`: either would be
        # an inference summed into a measure, which is the one thing the ratified
        # amendment forbids.
        "observations": measurements,
        "first_observation": first_seen.isoformat() if first_seen else None,
        "last_observation": last_seen.isoformat() if last_seen else None,
        "by_data_tool": [
            {
                **_with_share(by_tool[(tool, basis)], data_tool=tool, basis=basis),
                "means": _BASIS_MEANING[basis],
            }
            for tool, basis in sorted(by_tool)
        ],
        "by_basis": [
            {**_with_share(by_basis[basis], basis=basis), "means": _BASIS_MEANING[basis]}
            for basis in sorted(by_basis)
        ],
        "context_tools": [
            {"context_tool": row[0], "observations": row[1]} for row in context_rows
        ],
        "measured_data_tools": list(DATA_TOOLS),
        "measured_context_tools": list(CONTEXT_TOOLS),
        # A count of STEPS the AI Path recorder observed for a measured data
        # tool, not of questions: one exploration leaves two (its own call and
        # the execution it delegates to). It is never compared to
        # `observations` as an equality; it only says whether the window held
        # any data question at all.
        "data_tool_calls_observed": {
            "count": data_tool_calls_observed,
            "means": (
                "tool calls of a measured data tool that the AI Path recorder saw in "
                "this window, whether or not an adherence verdict was recorded for "
                "them; a count of recorded steps, not of questions"
            ),
        },
        "empty_state": None,
    }
    if not measurements and data_tool_calls_observed:
        # Data questions WERE asked here, and none reached the measure. Said
        # apart from the quiet window: the two call for opposite gestures.
        payload["empty_state"] = {
            "headline": "Data questions were asked here, and none was measured.",
            "detail": (
                f"{data_tool_calls_observed} call(s) of a data tool were observed in "
                "this window and no adherence verdict was recorded for them. The "
                "measured tools are listed below."
            ),
            "next_step": (
                "Ask one question again from the assistant and come back here; if "
                "this state persists, the tool that answered is not among the "
                "measured tools listed below."
            ),
        }
    elif not measurements:
        # An empty list says WHY it is empty and names the gesture that fills it.
        # Never a deployment state, never a table name.
        payload["empty_state"] = {
            "headline": "Nothing recorded yet.",
            "detail": (
                "Adherence is measured when someone asks this project a data question "
                "from an assistant -- a daily report, a report, a card, a Datastream "
                "report or an Analyze Result -- and it says whether the governed "
                "context was consulted first."
            ),
            "next_step": (
                "Connect this project to an assistant and ask it one question, then "
                "come back here."
            ),
        }
    return payload


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
        f"Context tip: governed definitions exist for {shown}{more} - "
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
