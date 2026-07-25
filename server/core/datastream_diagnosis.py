"""toorow -- Sanitized pull history and diagnosis MCP surface (Story 36.12, Epic 36).

The LLM/agent-facing SAFE evidence surface for ONE Datastream or pull. Two
project-scoped read tools -- ``datastream_pull_history`` and
``datastream_diagnose`` -- COMPOSE the existing Epic 12/13 read models (pull_jobs,
pull_verifications, extraction ledger, datastream executions / publication log,
DQ report, audit_log, connection_health, freshness) into a BOUNDED, PAGINATED,
allowlist-serialized timeline. They NEVER open a warehouse cursor and NEVER
mutate a publication pointer -- they are pure read/authorize surfaces.

INVARIANTS (contract of the orchestrator -- never bypassed):

  E36-NFR01 (allowlist serializer): every event that leaves this module passes
    through ``_sanitize_*`` which emits EXACTLY canonical error class, retryability,
    recommended user action, attempts, affected interval and copyable correlation
    IDs (pull_id / execution_id / trace_id). Provider strings, exception text, SQL,
    payloads, tokens, headers and emails are treated as UNTRUSTED DATA: they are
    structurally separated, size-limited (pull_errors._truncate_payload) and
    REDACTED before output. app.pull_jobs.error_detail (which can carry a
    provider_payload) is NEVER surfaced raw -- it is mapped through the
    pull_errors taxonomy to a canonical class + user_action only. error_detail is
    NOT an MCP contract (Additional Architecture Requirement, epic line 110).

  E36-NFR06 (bounded + paginated): the timeline caps at ``_MAX_PAGE_SIZE`` events
    per call with an offset cursor; the LLM summary stays compact; structured
    detail rides the canonical envelope. Raw logs/rows/samples are NEVER returned;
    a raw observability lookup stays a human-controlled action keyed by a
    trace/correlation ID. Even with NO UI host the compact response still carries
    mandatory bounded evidence -- never a deep-link-only answer.

  E36-NFR05 (wrap existing seams): this module opens NO parallel diagnostic
    engine and adds NO source-specific vocabulary to core. It reuses
    ``extract_ledger.get_extract_ledger`` / ``_parse_error_class``, the
    ``pull_errors`` taxonomy, ``dq_api.fetch_dq_report_data`` and the
    ``operations`` Support-disclosure foundation.

  E36-NFR02 (fail-closed scope): every read resolves the Datastream/project scope
    through ``project_access.resolve_strict_resource_access``; on denial the tool
    EXISTENCE-HIDES (not_found), mirroring metric_semantics_mcp. Support disclosure
    is separately scoped and audited: the durable audit commits FIRST and the
    response carries ``audit_event_id`` (operations.prepare_support_disclosure /
    confirm_support_audit_committed / build_support_disclosure_response).

AD-2 (ZERO provider vocabulary): this module contains NO connector/provider names.
Every provider-specific string arrives from DB rows and is redacted, never emitted.

Design mirrors metric_semantics_mcp.py: ``from __future__ import annotations``,
module logger, ``core.*`` imports LAZY inside function bodies (no import cycle),
pure serializers kept separate from I/O so the redaction invariants are testable
without Postgres. French ToolError / summary messages. ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Envelope schema version (mirrors core.main.SCHEMA_VERSION -- kept local so this
# module never imports a private of core.main; single canonical value "1").
_SCHEMA_VERSION = "1"

# E36-NFR06: hard cap on timeline events returned per call (bounded + paginated).
_MAX_PAGE_SIZE = 50

# Default history window (days) when the caller does not pass an explicit interval.
_DEFAULT_WINDOW_DAYS = 30

# The canonical error-class strings we are allowed to surface. Anything not in this
# frozenset is coerced to the taxonomy's "unclassified" -- we NEVER surface a raw
# provider error code as if it were a canonical class.
_ALLOWED_ERROR_CLASSES = frozenset(
    (
        "auth_expired",
        "auth_revoked",
        "permission_denied",
        "invalid_request",
        "provider_transient",
        "rate_limited",
        "unclassified",
    )
)

# The canonical set of recommended user actions we may emit (allowlist). Any other
# string is dropped to None -- we never echo a free-form provider instruction.
_ALLOWED_USER_ACTIONS = frozenset(("reconnect",))

# The canonical RECOVERY-class vocabulary (Story 36.16 reachability fix). These are
# the recovery-relevant classes the diagnosis expresses through TYPED, ENUM evidence
# already present in the read models -- NOT through a raw provider error class:
#
#   * "partial_data"          -- a verification verdict of partial/empty rows
#                                (app.pull_verifications.verdict, a closed enum).
#   * "uncertain_publication" -- an execution whose publish outcome is unresolved
#                                (a stuck 'publishing' execution, or the typed
#                                'reconciliation_inconclusive' error_code).
#   * "dq_failure"            -- a blocking DQ gate on a failed execution (the closed
#                                gate-code set from datastream_publication).
#   * "mapping_drift"         -- a schema/mapping-hash mismatch gate on a failed
#                                execution (typed gate code, never raw text).
#
# It is a CLOSED SET. Like _ALLOWED_ERROR_CLASSES, any value not in this frozenset is
# dropped -- we NEVER surface a free-text recovery hint. datastream_recovery's
# _PROCEDURE_TABLE maps exactly these four keys to refetch / reconcile / reprocess /
# replace, so making them reachable here is what closes the 36.16 gap.
_ALLOWED_RECOVERY_CLASSES = frozenset(
    (
        "partial_data",
        "uncertain_publication",
        "dq_failure",
        "mapping_drift",
    )
)

# Maps a TYPED execution ``error_code`` (a bounded, closed enum written by
# datastream_publication -- NEVER free provider text) to a canonical recovery class.
# error_code is the typed gate/failure code (mapping_drift, schema_hash_mismatch,
# dq_gate_failed, empty_candidate, row_count_delta_exceeded, content_hash_mismatch,
# reconciliation_inconclusive). error_DETAIL (the adjacent free text) is NEVER read.
# A code absent from this map contributes NO recovery class (fail-closed to nothing).
_EXECUTION_ERROR_CODE_TO_RECOVERY = {
    # Schema / mapping hash drift -> a governed mapping replacement is required.
    "mapping_drift": "mapping_drift",
    "schema_hash_mismatch": "mapping_drift",
    # Blocking DQ gates on the candidate -> reprocess the candidate.
    "dq_gate_failed": "dq_failure",
    "empty_candidate": "dq_failure",
    "row_count_delta_exceeded": "dq_failure",
    "content_hash_mismatch": "dq_failure",
    # A publish whose cross-store outcome could not be resolved -> reconcile.
    "reconciliation_inconclusive": "uncertain_publication",
}

# The typed verification verdicts (app.pull_verifications.verdict, closed enum) that
# mean the pull landed short of the expected rows -> a bounded refetch is the fix.
_PARTIAL_VERIFICATION_VERDICTS = frozenset(("partial", "empty"))

# The typed non-terminal execution states whose publish outcome is UNRESOLVED. An
# execution stuck in 'publishing' (the atomic commit began but no terminal state was
# recorded) is an uncertain publication -> reconcile the execution state.
_UNCERTAIN_EXECUTION_STATES = frozenset(("publishing",))


# ---------------------------------------------------------------------------
# Envelope + error helpers (local -- never import a private of core.main).
# ---------------------------------------------------------------------------


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope (same shape as core.main)."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON (FR message)."""
    import json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token (same pattern as get_card).

    Absent token => "anonymous", which the strict access seam then rejects
    (existence-hiding, E36-NFR02).
    """
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


# ---------------------------------------------------------------------------
# Guard helper -- MIRRORS the strict AD-5 seam (never a bypass). Existence-hiding.
# ---------------------------------------------------------------------------


def _guard_datastream_read(datastream_id: str, identity: str) -> str:
    """Return the effective org_id, or raise not_found (existence-hiding).

    Wraps ``project_access.resolve_strict_resource_access`` on the Datastream scope
    with minimum ``view`` capability. Denial for ANY reason (not a member, no grant,
    foreign resource, guard-evaluation failure/DB down) surfaces a single
    ``not_found`` -- we NEVER disclose whether the Datastream exists (E36-NFR02).
    The read must NOT proceed unguarded on a guard failure (fail-closed).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with get_connection() as conn:
            decision = resolve_strict_resource_access(
                identity, conn, datastream_id=datastream_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed (never unguarded).
        logger.error(
            "datastream_diagnosis: access guard failed ds=%s: %s", datastream_id, exc
        )
        raise _tool_error("not_found", "Flux introuvable.") from exc
    if not decision.allowed or not decision.org_id:
        raise _tool_error("not_found", "Flux introuvable.")
    return str(decision.org_id)


# ---------------------------------------------------------------------------
# Correlation-ID / interval / attempts sanitizers (PURE -- offline-testable).
# ---------------------------------------------------------------------------

# Correlation IDs are opaque internal handles (ULID with a known prefix). We echo
# them ONLY when they match a strict handle shape -- never a free-form provider
# string smuggled through an id column.
_ID_PREFIXES = ("pull_", "job_", "dse_", "ver_", "dplog_", "op_", "audit_")
_TRACE_RE_STR = r"^[0-9a-f]{32}$"


def _safe_id(value, prefixes=_ID_PREFIXES) -> str | None:
    """Return *value* only if it is a bounded, prefixed internal handle, else None.

    A correlation ID is copyable evidence, not provider data. We accept it only
    when it is a short string starting with a known internal prefix -- a
    defence-in-depth guard so a provider string in an id-typed column is dropped.
    """
    if not isinstance(value, str) or not value or len(value) > 64:
        return None
    return value if value.startswith(prefixes) else None


def _safe_trace_id(value) -> str | None:
    """Return *value* only if it is a 32-hex trace id (the observability key)."""
    import re  # noqa: PLC0415

    if not isinstance(value, str):
        return None
    return value if re.fullmatch(_TRACE_RE_STR, value) else None


def _safe_interval(date_from, date_to) -> dict | None:
    """Return a bounded ``{from, to}`` affected-interval, or None.

    Accepts only ISO date/timestamp-looking strings, size-limited. Anything else
    (a provider blob, a huge string) is dropped -- an interval is structural
    evidence, never free-form text.
    """

    def _one(v):
        if v is None:
            return None
        s = v if isinstance(v, str) else str(v)
        return s[:32] if s else None

    f = _one(date_from)
    t = _one(date_to)
    if f is None and t is None:
        return None
    return {"from": f, "to": t}


def _safe_attempts(value) -> int:
    """Return a bounded non-negative attempt count (never a provider string)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    if n < 0:
        return 0
    return min(n, 10_000)


def _canonical_error(error_detail):
    """Map a pull's ``error_detail`` to (error_class, user_action) -- REDACTED.

    THE core redaction contract: ``error_detail`` can carry a provider_payload,
    stack trace, SQL or email. It is NEVER surfaced raw. We route it through the
    pull_errors taxonomy:

      1. If it is the Story 25.2 structured JSON envelope, read its ``error_class``
         / ``user_action`` (extract_ledger._parse_error_class).
      2. Otherwise (legacy plain text, malformed, non-dict) -> ("unclassified",
         None). We do NOT echo the text.

    The returned class is clamped to ``_ALLOWED_ERROR_CLASSES`` and the action to
    ``_ALLOWED_USER_ACTIONS`` -- a provider code that slipped into the envelope is
    coerced to "unclassified" / None, never emitted as-is.
    """
    from core.extract_ledger import _parse_error_class  # noqa: PLC0415

    error_class, user_action = _parse_error_class(error_detail)
    if error_class not in _ALLOWED_ERROR_CLASSES:
        # No structured class (legacy text) or an unknown/provider string -> unclassified.
        error_class = "unclassified" if error_detail else None
    if user_action not in _ALLOWED_USER_ACTIONS:
        user_action = None
    return error_class, user_action


def _safe_recovery_class(value) -> str | None:
    """Return *value* only if it is an allowlisted recovery class, else None.

    Defence in depth (mirrors _canonical_error's clamp to _ALLOWED_ERROR_CLASSES):
    a recovery class is a CLOSED enum, never free text. Anything not in
    ``_ALLOWED_RECOVERY_CLASSES`` -- including a hostile string smuggled through a
    typed column -- is dropped to None and never surfaced.
    """
    return value if value in _ALLOWED_RECOVERY_CLASSES else None


def _retryable_for_class(error_class) -> bool | None:
    """Return the canonical retryability for a canonical error class (pull_errors)."""
    if error_class is None:
        return None
    from core import pull_errors  # noqa: PLC0415

    cls = pull_errors._CLASS_BY_NAME.get(error_class)
    if cls is not None:
        return cls.retryable
    # rate_limited lives in quota.py and is retryable by contract.
    if error_class == "rate_limited":
        return True
    return None


# ---------------------------------------------------------------------------
# Timeline event sanitizers (PURE -- the allowlist serializer, E36-NFR01).
# ---------------------------------------------------------------------------


def _sanitize_pull_event(row: dict) -> dict:
    """Serialize ONE pull_jobs row into an allowlisted timeline event.

    EXACTLY: kind, state, canonical error class + retryability + recommended
    action, attempts, affected interval, copyable correlation IDs (pull_id,
    execution_id absent here, trace_id absent here) and safe timestamps. The raw
    ``error_detail`` NEVER appears -- only its canonical mapping.
    """
    error_class, user_action = _canonical_error(row.get("error_detail"))
    return {
        "kind": "pull",
        "state": _safe_enum(row.get("state")),
        "error_class": error_class,
        "retryable": _retryable_for_class(error_class),
        "recommended_action": user_action,
        "attempts": _safe_attempts(row.get("attempt_count")),
        "affected_interval": _safe_interval(row.get("date_from"), row.get("date_to")),
        "correlation": {
            "pull_id": _safe_id(row.get("pull_id")),
            "job_id": _safe_id(row.get("id")),
        },
        "at": _safe_ts(row.get("completed_at") or row.get("enqueued_at")),
    }


def _sanitize_ledger_event(row: dict) -> dict:
    """Serialize ONE extract-ledger day into an allowlisted event.

    The ledger already carries a canonical error_class/user_action on failed days
    (extract_ledger surfaces the 25.2 pair). We pass them through the same clamp
    so nothing but the taxonomy leaks.
    """
    error_class = row.get("error_class")
    user_action = row.get("user_action")
    if error_class not in _ALLOWED_ERROR_CLASSES:
        error_class = None
    if user_action not in _ALLOWED_USER_ACTIONS:
        user_action = None
    return {
        "kind": "ledger",
        "state": _safe_enum(row.get("status")),
        "error_class": error_class,
        "retryable": _retryable_for_class(error_class),
        "recommended_action": user_action,
        "row_count": _safe_count(row.get("row_count")),
        "affected_interval": _safe_interval(row.get("date"), row.get("date")),
        "correlation": {"pull_id": _safe_id(row.get("pull_id"))},
        "at": _safe_ts(row.get("loaded_at")),
    }


def _sanitize_verification_event(row: dict) -> dict:
    """Serialize ONE pull_verifications row (verdict + counts, no raw data).

    Story 36.16 reachability: when the TYPED ``verdict`` enum is partial/empty (the
    row-count-short outcome produced by verification.py) we surface a canonical
    ``recovery_class="partial_data"`` so datastream_recovery can reach the bounded
    ``refetch`` procedure. The verdict is a closed enum, never provider text; the
    recovery class is clamped to ``_ALLOWED_RECOVERY_CLASSES``.
    """
    verdict = _safe_enum(row.get("verdict"))
    recovery_class = (
        "partial_data" if verdict in _PARTIAL_VERIFICATION_VERDICTS else None
    )
    return {
        "kind": "verification",
        "state": verdict,
        "recovery_class": _safe_recovery_class(recovery_class),
        "row_count": _safe_count(row.get("actual_rows")),
        "expected_rows": _safe_count(row.get("expected_rows")),
        # The pull window is the interval to bound a partial-data refetch on. Present
        # only when the joined pull carries a date window (structural, size-limited).
        "affected_interval": _safe_interval(row.get("date_from"), row.get("date_to")),
        "correlation": {"pull_id": _safe_id(row.get("pull_id"))},
        "at": _safe_ts(row.get("verified_at")),
    }


def _recovery_class_for_execution(state, error_code) -> str | None:
    """Derive a canonical recovery class from an execution's TYPED fields (PURE).

    Reads ONLY the closed-enum ``state`` and the closed-enum ``error_code`` -- never
    ``error_detail`` (the adjacent free text). Precedence:

      1. A stuck non-terminal ``publishing`` state -> "uncertain_publication"
         (the atomic commit began but no terminal state was recorded).
      2. A typed failure ``error_code`` in ``_EXECUTION_ERROR_CODE_TO_RECOVERY``
         (mapping/schema hash drift -> "mapping_drift"; a blocking DQ gate ->
         "dq_failure"; an inconclusive reconcile -> "uncertain_publication").

    Any unrecognised code contributes NOTHING (fail-closed). The result is clamped to
    ``_ALLOWED_RECOVERY_CLASSES`` by the caller.
    """
    if state in _UNCERTAIN_EXECUTION_STATES:
        return "uncertain_publication"
    if isinstance(error_code, str):
        return _EXECUTION_ERROR_CODE_TO_RECOVERY.get(error_code)
    return None


def _sanitize_execution_event(row: dict) -> dict:
    """Serialize ONE datastream_executions row.

    Its ``error_detail`` is provider-adjacent free text -> it is NEVER echoed. The
    ``error_code`` is a TYPED, closed enum (gate/failure code) -- we do NOT echo it
    either, but we route it through ``_recovery_class_for_execution`` to surface a
    canonical, allowlisted ``recovery_class`` (Story 36.16 reachability: this is what
    makes ``dq_failure`` / ``mapping_drift`` / ``uncertain_publication`` reachable by
    the recovery bridge). We otherwise surface only the typed state-machine state +
    row_count + correlation IDs (execution_id, plan/mapping version ids as handles).
    """
    state = _safe_enum(row.get("state"))
    recovery_class = _recovery_class_for_execution(state, row.get("error_code"))
    return {
        "kind": "execution",
        "state": state,
        "recovery_class": _safe_recovery_class(recovery_class),
        "row_count": _safe_count(row.get("row_count")),
        "correlation": {
            "execution_id": _safe_id(row.get("id")),
            "plan_version_id": _safe_id(row.get("plan_version_id")),
            "mapping_version_id": _safe_id(row.get("mapping_version_id")),
        },
        "at": _safe_ts(row.get("state_changed_at")),
    }


def _sanitize_publication_event(row: dict) -> dict:
    """Serialize ONE datastream_publication_log row (append-only pointer swap)."""
    return {
        "kind": "publication",
        "state": "published",
        "row_count": _safe_count(row.get("row_count")),
        "correlation": {
            "execution_id": _safe_id(row.get("execution_id")),
            "prior_execution_id": _safe_id(row.get("prior_execution_id")),
        },
        "at": _safe_ts(row.get("published_at")),
    }


def _sanitize_audit_event(row: dict) -> dict:
    """Serialize ONE audit_log row (action + outcome + correlation, no metadata).

    The audit ``metadata`` JSONB and ``identity`` are NOT echoed -- only the
    allowlisted action code, outcome and correlation handles (operation_id,
    trace_id). Actions are clamped to a short bounded enum shape.
    """
    return {
        "kind": "audit",
        "action": _safe_enum(row.get("action"), max_len=64),
        "outcome": _safe_enum(row.get("outcome")),
        "correlation": {
            "operation_id": _safe_id(row.get("operation_id")),
            "trace_id": _safe_trace_id(row.get("trace_id")),
        },
        "at": _safe_ts(row.get("created_at")),
    }


def _sanitize_health_event(row: dict) -> dict:
    """Serialize the connection_health snapshot (status + safe timestamps)."""
    return {
        "kind": "connection_health",
        "state": _safe_enum(row.get("status")),
        "at": _safe_ts(row.get("last_checked_at")),
        "last_fetched_at": _safe_ts(row.get("last_fetched_at")),
    }


# ---------------------------------------------------------------------------
# Small value guards used by the sanitizers.
# ---------------------------------------------------------------------------


def _safe_enum(value, max_len: int = 32) -> str | None:
    """Return a short lowercase-ish enum token, or None. Drops long/blob strings.

    Timeline ``state``/``verdict``/``action`` fields are enums, not free text. A
    value longer than *max_len* is treated as untrusted (a provider blob smuggled
    into an enum column) and dropped.
    """
    if not isinstance(value, str) or not value:
        return None
    return value if len(value) <= max_len else None


def _safe_count(value) -> int | None:
    """Return a bounded non-negative row count, or None (never a provider string)."""
    if value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 0 else None


def _safe_ts(value) -> str | None:
    """Return a bounded ISO timestamp string, or None."""
    if value is None:
        return None
    s = value if isinstance(value, str) else str(value)
    return s[:40] if s else None


# ---------------------------------------------------------------------------
# Timeline assembly (bounded + paginated, E36-NFR06). Pure over fetched rows.
# ---------------------------------------------------------------------------


def _bound_page_size(page_size) -> int:
    """Clamp a requested page size to 1..``_MAX_PAGE_SIZE`` (default cap)."""
    try:
        n = int(page_size)
    except (TypeError, ValueError):
        return _MAX_PAGE_SIZE
    return max(1, min(n, _MAX_PAGE_SIZE))


def _bound_offset(cursor) -> int:
    """Clamp a requested offset cursor to a non-negative int."""
    try:
        n = int(cursor)
    except (TypeError, ValueError):
        return 0
    return max(0, n)


def _assemble_timeline(events: list[dict], *, cursor: int, page_size: int) -> dict:
    """Sort the sanitized events (newest first) and slice one bounded page.

    Returns ``{events, page, total, next_cursor}`` where ``events`` is at most
    ``page_size`` (<=``_MAX_PAGE_SIZE``) items and ``next_cursor`` is the offset
    of the next page or None when the timeline is exhausted (E36-NFR06).
    """
    ordered = sorted(events, key=lambda e: (e.get("at") or ""), reverse=True)
    total = len(ordered)
    start = min(cursor, total)
    end = min(start + page_size, total)
    page = ordered[start:end]
    next_cursor = end if end < total else None
    return {
        "events": page,
        "page": {"cursor": start, "size": len(page), "page_size": page_size},
        "total": total,
        "next_cursor": next_cursor,
    }


# ---------------------------------------------------------------------------
# I/O: fetch the raw rows for one datastream (bounded queries). Fail-soft.
# ---------------------------------------------------------------------------


def _resolve_scope(conn, datastream_id: str) -> tuple[str | None, str | None]:
    """Return (project_id, connection_ref_id) for *datastream_id*, or (None, None)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT project_id, connection_ref_id FROM app.datastreams WHERE id = %s",
            (datastream_id,),
        )
        row = cur.fetchone()
    if not row:
        return None, None
    return row[0], row[1]


def _fetch_rows(conn, table: str, sql: str, params) -> list[dict]:
    """Run one bounded SELECT and return rows as dicts. Fail-soft to []."""
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception as exc:  # noqa: BLE001 -- one degraded stream never fails diagnosis.
        logger.warning("datastream_diagnosis: %s query failed: %s", table, exc)
        return []


def _collect_events(conn, datastream_id: str) -> tuple[list[dict], dict]:
    """Compose the sanitized events for one datastream + a small context block.

    Reuses the existing read models (E36-NFR05). Each source query is bounded and
    fail-soft: a degraded source contributes no events rather than failing the
    whole diagnosis. Returns (events, context) where context is safe metadata
    (freshness, health status, connection ref handle).
    """
    project_id, connection_ref_id = _resolve_scope(conn, datastream_id)
    events: list[dict] = []

    # 1. pull_jobs (bounded, newest first) -- the run history.
    pull_rows = _fetch_rows(
        conn,
        "pull_jobs",
        """
        SELECT id, pull_id, date_from, date_to, state, error_detail,
               attempt_count, enqueued_at, completed_at
        FROM app.pull_jobs
        WHERE datastream_id = %s
        ORDER BY enqueued_at DESC
        LIMIT %s
        """,
        (datastream_id, _MAX_PAGE_SIZE),
    )
    events.extend(_sanitize_pull_event(r) for r in pull_rows)

    # 2. pull_verifications for those pulls (verdict + counts).
    pull_ids = [r.get("pull_id") for r in pull_rows if r.get("pull_id")]
    if pull_ids:
        ver_rows = _fetch_rows(
            conn,
            "pull_verifications",
            """
            SELECT v.pull_id, v.expected_rows, v.actual_rows, v.verdict,
                   v.verified_at, j.date_from, j.date_to
            FROM app.pull_verifications v
            JOIN app.pull_jobs j ON j.pull_id = v.pull_id
            WHERE v.pull_id = ANY(%s)
            ORDER BY v.verified_at DESC
            LIMIT %s
            """,
            (pull_ids, _MAX_PAGE_SIZE),
        )
        events.extend(_sanitize_verification_event(r) for r in ver_rows)

    # 3. datastream_executions -- the candidate registry state machine.
    exec_rows = _fetch_rows(
        conn,
        "datastream_executions",
        """
        SELECT id, plan_version_id, mapping_version_id, state, row_count,
               error_code, state_changed_at
        FROM app.datastream_executions
        WHERE datastream_id = %s
        ORDER BY state_changed_at DESC
        LIMIT %s
        """,
        (datastream_id, _MAX_PAGE_SIZE),
    )
    events.extend(_sanitize_execution_event(r) for r in exec_rows)

    # 4. datastream_publication_log -- append-only pointer swaps.
    pub_rows = _fetch_rows(
        conn,
        "datastream_publication_log",
        """
        SELECT execution_id, prior_execution_id, row_count, published_at
        FROM app.datastream_publication_log
        WHERE datastream_id = %s
        ORDER BY published_at DESC
        LIMIT %s
        """,
        (datastream_id, _MAX_PAGE_SIZE),
    )
    events.extend(_sanitize_publication_event(r) for r in pub_rows)

    # 5. audit_log for the pulls (action + outcome + correlation, no metadata).
    if connection_ref_id:
        audit_rows = _fetch_rows(
            conn,
            "audit_log",
            """
            SELECT action, outcome, operation_id, trace_id, created_at
            FROM app.audit_log
            WHERE connection_ref = %s
            ORDER BY created_at DESC
            LIMIT %s
            """,
            (connection_ref_id, _MAX_PAGE_SIZE),
        )
        events.extend(_sanitize_audit_event(r) for r in audit_rows)

    # 6. connection_health snapshot (status + freshness).
    context: dict = {
        "datastream_id": _safe_id(datastream_id, prefixes=("ds_",)) or datastream_id[:64],
        "project_id": _safe_id(project_id, prefixes=("proj_",)) if project_id else None,
        "connection_ref": _safe_id(connection_ref_id, prefixes=("conn_",))
        if connection_ref_id
        else None,
        "freshness_last_pull_at": None,
        "connection_health": None,
    }
    if connection_ref_id:
        health_rows = _fetch_rows(
            conn,
            "connection_health",
            """
            SELECT status, last_checked_at, last_fetched_at
            FROM app.connection_health
            WHERE connection_ref_id = %s
            """,
            (connection_ref_id,),
        )
        if health_rows:
            health_event = _sanitize_health_event(health_rows[0])
            events.append(health_event)
            context["connection_health"] = health_event["state"]
            context["freshness_last_pull_at"] = health_rows[0].get("last_fetched_at") and _safe_ts(
                health_rows[0].get("last_fetched_at")
            )

    return events, context


def _diagnose(events: list[dict]) -> dict:
    """Derive the canonical single diagnosis from the sanitized events (PURE).

    Two kinds of failure evidence, in PRECEDENCE order:

      1. A canonical ERROR class (auth_expired, rate_limited, provider_transient,
         ...): the most recent event carrying one wins -- the E36-FR08 allowlist
         result (class + retryability + recommended action + attempts + interval +
         correlation IDs). A hard provider/auth failure is the dominant signal.

      2. Otherwise a canonical RECOVERY class (Story 36.16 reachability):
         partial_data / uncertain_publication / dq_failure / mapping_drift, surfaced
         by the verification and execution sanitizers from TYPED enum evidence. The
         most recent event carrying one sets ``recovery_class`` on the diagnosis and
         a "failure" verdict -- WITHOUT an error_class -- so datastream_recovery's
         ``_derived_class`` (which reads ``recovery_class`` first, then falls back to
         ``source_kind`` == verification/publication) can reach refetch / reconcile /
         reprocess / replace. Both channels stay closed enums; NEVER raw text.

    When no failure evidence of either kind exists the diagnosis is a healthy verdict.
    """
    ordered = sorted(events, key=lambda e: (e.get("at") or ""), reverse=True)

    # (1) Hard error class dominates (auth / rate limit / provider transient / ...).
    for ev in ordered:
        if ev.get("error_class"):
            return {
                "verdict": "failure",
                "error_class": ev.get("error_class"),
                "recovery_class": None,
                "retryable": ev.get("retryable"),
                "recommended_action": ev.get("recommended_action"),
                "attempts": ev.get("attempts", 0),
                "affected_interval": ev.get("affected_interval"),
                "correlation": ev.get("correlation") or {},
                "source_kind": ev.get("kind"),
            }

    # (2) Canonical recovery class from typed verification / execution evidence.
    for ev in ordered:
        recovery_class = _safe_recovery_class(ev.get("recovery_class"))
        if recovery_class:
            return {
                "verdict": "failure",
                "error_class": None,
                "recovery_class": recovery_class,
                "retryable": None,
                "recommended_action": None,
                "attempts": ev.get("attempts", 0),
                "affected_interval": ev.get("affected_interval"),
                "correlation": ev.get("correlation") or {},
                "source_kind": ev.get("kind"),
            }

    return {
        "verdict": "ok",
        "error_class": None,
        "recovery_class": None,
        "retryable": None,
        "recommended_action": None,
        "attempts": 0,
        "affected_interval": None,
        "correlation": {},
        "source_kind": None,
    }


def _diagnose_summary(diagnosis: dict, context: dict) -> str:
    """Build a compact (<=30 line) LLM summary carrying MANDATORY bounded evidence.

    E36-NFR06: even with no UI host this is never a deep-link-only answer -- it
    always enumerates the canonical class, retryability, recommended action and the
    copyable correlation IDs. NEVER a raw log line.
    """
    ds = context.get("datastream_id")
    lines = [f"Diagnostic du flux {ds!r}."]
    if diagnosis.get("verdict") == "ok":
        lines.append("Aucune defaillance canonique detectee sur la fenetre.")
        health = context.get("connection_health")
        if health:
            lines.append(f"Sante de la connexion : {health}.")
        return "\n".join(lines[:30])

    error_class = diagnosis.get("error_class")
    if error_class:
        lines.append(f"Classe d'erreur canonique : {error_class}.")
    recovery_class = diagnosis.get("recovery_class")
    if recovery_class:
        lines.append(f"Classe de recuperation canonique : {recovery_class}.")
    retry = diagnosis.get("retryable")
    if retry is True:
        lines.append("Rejouable : oui (backoff automatique).")
    elif retry is False:
        lines.append("Rejouable : non (action requise).")
    action = diagnosis.get("recommended_action")
    if action:
        lines.append(f"Action recommandee : {action}.")
    lines.append(f"Tentatives : {diagnosis.get('attempts', 0)}.")
    interval = diagnosis.get("affected_interval")
    if interval:
        lines.append(
            f"Intervalle affecte : {interval.get('from')} -> {interval.get('to')}."
        )
    corr = diagnosis.get("correlation") or {}
    ids = [f"{k}={v}" for k, v in corr.items() if v]
    if ids:
        lines.append("Identifiants de correlation (copiables) : " + ", ".join(ids))
    lines.append(
        "Preuve brute (logs) : action humaine controlee via l'ID de correlation "
        "(non retournee ici)."
    )
    return "\n".join(lines[:30])


# ---------------------------------------------------------------------------
# Support disclosure (bounded redacted evidence AFTER durable audit).
# ---------------------------------------------------------------------------


def _support_disclose(
    *,
    identity: str,
    org_id: str,
    datastream_id: str,
    diagnosis: dict,
) -> dict:
    """Return bounded redacted Support evidence, audit-committed FIRST.

    Restricted human Support flow (E36-NFR02 / Story 36.12 AC4): the durable audit
    event commits BEFORE the evidence is returned, and the response carries
    ``audit_event_id``. Reuses operations.prepare_support_disclosure /
    confirm_support_audit_committed / build_support_disclosure_response. The
    evidence is the SAME sanitized canonical diagnosis -- never raw logs/rows.
    """
    from core import operations  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    evidence = {
        "class": diagnosis.get("error_class") or "unclassified",
        "correlation_ids": {k: v for k, v in (diagnosis.get("correlation") or {}).items() if v},
    }
    evidence_hash = operations._canonical_hash(evidence)
    redaction_hash = operations._canonical_hash(
        {"redacted": ["error_detail", "provider_payload", "sql", "stack_trace", "emails"]}
    )
    resource_path = (f"organization:{org_id}", f"flux:{datastream_id}")
    idempotency_key = f"support.diagnosis:{org_id}:{datastream_id}:{evidence_hash}"

    with get_connection() as conn:
        op_result = operations.prepare_support_disclosure(
            conn,
            actor=identity,
            effective_org_id=org_id,
            resource_path=resource_path,
            idempotency_key=idempotency_key,
            host_context={},
            versions={"tool": "datastream_diagnose"},
            disclosure_class=evidence["class"],
            evidence_hash=evidence_hash,
            redaction_hash=redaction_hash,
            trace_id=None,
        )
        conn.commit()  # durable audit committed FIRST (AC: audit-committed-first).

    # Re-verify the audit from a fresh post-commit transaction before responding.
    with get_connection() as conn:
        reference = operations.confirm_support_audit_committed(
            conn, op_result.audit_event_id
        )
    return operations.build_support_disclosure_response(
        audit_reference=reference,
        evidence={
            "class": evidence["class"],
            "evidence_hash": evidence_hash,
            "correlation_ids": evidence["correlation_ids"],
        },
    )


# ---------------------------------------------------------------------------
# register(mcp): define each handler locally and register it functionally.
# ---------------------------------------------------------------------------


def register(mcp) -> None:
    """Register the sanitized pull-history / diagnosis MCP tools on *mcp*.

    Called once from core.main (the ONLY hook 36.12 adds to main.py). Defines each
    handler as a local function and calls ``mcp.tool(handler)`` -- the functional
    registration form (like metric_semantics_mcp), so this module never imports the
    mcp instance at module level (no cycle).

    Story 36.11 note: once ``mcp_profiles`` lands these tools must be re-registered
    via ``mcp_profiles.register_profiled(profile="insights", effect="read",
    data_class="operational", confirmation_mode="none")``. For now they register
    with plain ``mcp.tool`` (Insights-default, read-only).
    """

    # ---- Read: sanitized bounded/paginated pull history -------------------
    def datastream_pull_history(
        datastream_id: str,
        cursor: int = 0,
        page_size: int = _MAX_PAGE_SIZE,
    ):
        """Chronologie SANITISEE et PAGINEE des pulls d'UN flux (Story 36.12).

        Compose les modeles de lecture existants (pull_jobs, pull_verifications,
        executions/publication, audit, sante de connexion) en une chronologie
        BORNEE : au plus 50 evenements par page + curseur d'offset (E36-NFR06).
        Chaque evenement passe par le serialiseur allow-list (E36-NFR01) : classe
        d'erreur canonique, rejouabilite, action recommandee, tentatives,
        intervalle affecte, identifiants de correlation copiables (pull_id /
        execution_id / trace_id). Le ``error_detail`` brut (qui peut contenir un
        provider_payload) n'apparait JAMAIS -- seulement sa classe canonique. Guard
        d'acces strict AD-5 ; flux etranger -> not_found (existence cachee). Ne mute
        aucun pointeur de publication (surface de lecture pure).
        """
        datastream_id = (datastream_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        identity = _identity()
        _guard_datastream_read(datastream_id, identity)

        page_size_b = _bound_page_size(page_size)
        cursor_b = _bound_offset(cursor)
        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                events, context = _collect_events(conn, datastream_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "datastream_diagnosis: history failed ds=%s: %s", datastream_id, exc
            )
            raise _tool_error("server_error", "Erreur serveur.") from exc

        timeline = _assemble_timeline(events, cursor=cursor_b, page_size=page_size_b)
        data = {"context": context, "timeline": timeline}
        shown = timeline["page"]["size"]
        summary = (
            f"Historique du flux {context.get('datastream_id')!r} : "
            f"{shown} evenement(s) affiche(s) sur {timeline['total']} "
            f"(curseur {timeline['page']['cursor']})."
        )
        return _result(summary, data)

    # ---- Read: canonical single diagnosis ---------------------------------
    def datastream_diagnose(datastream_id: str, disclose_support: bool = False):
        """Diagnostic CANONIQUE d'UN flux : classe d'erreur + rejouabilite + action.

        Compose la meme chronologie sanitisee puis derive le diagnostic canonique
        (E36-FR08) : classe d'erreur canonique, rejouabilite, action recommandee,
        tentatives, intervalle affecte, identifiants de correlation copiables --
        via le schema allow-list (E36-NFR01). Le resume compact porte TOUJOURS la
        preuve bornee obligatoire, meme sans hote UI (jamais une reponse
        deep-link-only, E36-NFR06). Aucune donnee brute (logs/lignes/echantillons)
        n'est retournee ; la recherche d'observabilite brute reste une action
        humaine controlee via l'ID de correlation. ``disclose_support=True`` (flux
        Support restreint) renvoie une preuve bornee redigee APRES un audit durable
        committe d'abord ; la reponse porte alors ``audit_event_id``. Guard strict
        AD-5 ; flux etranger -> not_found. Ne mute aucun pointeur de publication.
        """
        datastream_id = (datastream_id or "").strip() or None
        if datastream_id is None:
            raise _tool_error("missing_param", "datastream_id est requis.")
        identity = _identity()
        org_id = _guard_datastream_read(datastream_id, identity)

        try:
            from core.db import get_connection  # noqa: PLC0415

            with get_connection() as conn:
                events, context = _collect_events(conn, datastream_id)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "datastream_diagnosis: diagnose failed ds=%s: %s", datastream_id, exc
            )
            raise _tool_error("server_error", "Erreur serveur.") from exc

        diagnosis = _diagnose(events)
        # Bounded, paginated first page of the supporting evidence timeline.
        timeline = _assemble_timeline(events, cursor=0, page_size=_MAX_PAGE_SIZE)
        data: dict = {
            "context": context,
            "diagnosis": diagnosis,
            "evidence": timeline,
        }

        if disclose_support:
            try:
                data["support_disclosure"] = _support_disclose(
                    identity=identity,
                    org_id=org_id,
                    datastream_id=datastream_id,
                    diagnosis=diagnosis,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "datastream_diagnosis: support disclosure failed ds=%s: %s",
                    datastream_id,
                    exc,
                )
                raise _tool_error(
                    "server_error", "Divulgation Support indisponible."
                ) from exc

        return _result(_diagnose_summary(diagnosis, context), data)

    mcp.tool(datastream_pull_history)
    mcp.tool(datastream_diagnose)
