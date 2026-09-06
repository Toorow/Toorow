"""Story 5.5 -- `submit_feedback`, the core-owned write-ack tool (AC3, AD-2, AD-10).

Records a thumbs-up / thumbs-down on a report in `app.feedback`, writes an audit
row, and sends a Langfuse score when tracing is enabled and a trace_id is
supplied (AD-13). It returns a one-line acknowledgement and nothing else: the
write-ack pattern of AD-1, so nothing beyond that line reaches the LLM context
(FR10).

The per-project rate limit is in memory, single-replica, and moves to Postgres
with the rest of the sliding-window counters -- the same open TODO as
`connection_revocation._refresh_health_last`.

The `from core.main import ...` line inside the body is the seam every extracted
surface uses: `core.main` imports this module, so a module-level import back
would be a cycle, and the helpers are patched at the `core.main` address.
"""

from __future__ import annotations

import json
import logging
import os
import time

from fastmcp.apps import AppConfig
from fastmcp.server.auth import AccessToken
from fastmcp.tools.tool import ToolResult
from mcp.types import TextContent

from core import tracing
from core.audit import declare_action

# --- LES ACTIONS QUE CE MODULE ECRIT ------------------------------------
#
# AD-42 (2026-08-12) : declarees ICI, a cote du code qui les ecrit, et non
# dans `core/audit.py`. Ce fichier etait un carrefour -- 43 editions de 29
# sujets depuis juin, dont 34 n'ajoutaient qu'une constante -- et 45 % des
# actions reellement ecrites en production n'y etaient meme pas declarees,
# parce que la liste etait trop loin pour valoir le detour. `write_audit_row`
# refuse desormais une action que personne n'a declaree.
ACTION_FEEDBACK_SUBMITTED = declare_action("feedback.submitted")


logger = logging.getLogger(__name__)




# AI-36 (Story 6.1, AC13): per-project in-memory rate limit on submit_feedback.
# Reuses the sliding-window dict pattern from connection_revocation._refresh_health_last:
# maps project_id -> list of monotonic timestamps of recent submissions, pruned to
# the last hour on each call. In-memory only (single-replica at P3-dev); Phase-B
# moves this to Postgres for multi-replica safety (same TODO as _refresh_health_last).
_feedback_calls: dict[str, list[float]] = {}


def _feedback_rate_limited(project_id: str, *, clock=time.monotonic) -> bool:
    """Return True if *project_id* has exceeded FEEDBACK_RATE_LIMIT_PER_HOUR (default 60).

    Records the current call when it is allowed. Sliding 1-hour window.
    """
    limit = int(os.environ.get("FEEDBACK_RATE_LIMIT_PER_HOUR", "60"))
    now = clock()
    cutoff = now - 3600.0
    calls = [t for t in _feedback_calls.get(project_id, []) if t >= cutoff]
    if len(calls) >= limit:
        _feedback_calls[project_id] = calls
        return True
    calls.append(now)
    _feedback_calls[project_id] = calls
    return False


# Story 9.10 (AC2): pure UI affordance -- submit_feedback is called only from the
# FeedbackBar / CardFeedbackBar widgets (callServerTool), never useful to the model
# directly. visibility=["app"] rides the wire meta (meta.ui.visibility, MCP Apps
# spec 2026-01-26) so conforming hosts keep it out of the model's tool list while
# widget iframes can still call it.
def submit_feedback(
    project_id: str,
    rating: int,
    trace_id: str | None = None,
    comment: str = "",
    report_ref: str = "",
    connector: str = "",
    handle: str = "",
) -> ToolResult:
    """Submit 👍/👎 feedback for a report (Story 5.5, AC3).

    Persists a user rating (1=thumbs-up, -1=thumbs-down) and optional comment
    to app.feedback, writes an audit row, and sends a Langfuse score when
    tracing is enabled and a trace_id is available (AD-13).

    AD-2: source-agnostic — no Connector-specific code here.
    AD-10: widget calls this via callServerTool; no direct DB writes from widget.
    AD-1: write-ack tool — returns a minimal one-line acknowledgement, no
    structuredContent. The comment used to say « French text only »; the text has
    been English for a while, and it is right that it is: an ack and a refusal are
    OPERATOR MESSAGES, which `analyze-and-test.md:1974-1978` keeps in English on
    purpose. Only a récit follows the reader's language.
    FR10: nothing pollutes the chat thread beyond this one-line ack.

    Parameters:
        project_id:  Project identifier.
        rating:      1 (👍 thumbs-up) or -1 (👎 thumbs-down). Integer only.
        trace_id:    OTel trace_id echoed from meta.trace_id (may be null).
        comment:     Optional qualitative text.
        report_ref:  The Result this rating is about. Empty means "the Result
                     the handle was issued over", which is the stronger
                     direction; a non-empty value that disagrees with the
                     handle is refused.
        connector:   Connector name, e.g. "google-analytics". This is the
                     installed source adapter the report came from -- never a
                     Datastream id and never an MCP tool name. See
                     docs/product-architecture/glossary.md.
        handle:      The server-minted `result_handle` the render path put in
                     result `_meta`. REQUIRED: a rating is an append, and an
                     append the caller can make on its own word is a write --
                     see `core.app_observation_handle` and the amendment of
                     2026-08-30 in docs/product-architecture/mcp-tool-surface.md.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    from core import db as core_db  # noqa: PLC0415
    from core.app_observation_handle import (  # noqa: PLC0415
        ObservationHandleRefused,
        consume_observation_handle,
        require_observation_handle,
        resolve_observation_handle,
    )
    from core.audit import write_audit_row  # noqa: PLC0415
    from core.main import (  # noqa: PLC0415
        _refuse_unless_project_scope,
        _resolve_project,
        get_access_token,
    )

    # Validate rating — must be exactly 1 or -1.
    if rating not in (1, -1):
        raise ToolError(
            json.dumps({"code": "invalid_input", "message": "rating must be 1 or -1"})
        )

    # Story 53.1. The identity was resolved BELOW the rate limiter, so the scope
    # check has to come with it -- and the order matters in the other direction
    # too: answering `rate_limited` before deciding access tells a stranger that
    # the project exists and is busy. Access first, then the limiter.
    #
    # 2026-08-25: it is resolved before the PROJECT too. The resolver decides
    # existence for a named caller now, so a token read afterwards would leave
    # the resolution anonymous while the guard below is not.
    _feedback_token: AccessToken | None = get_access_token()
    _feedback_identity = (
        (_feedback_token.claims.get("sub") or _feedback_token.client_id)
        if _feedback_token
        else "anonymous"
    ) or "anonymous"

    # Story 7.1 (AC5): resolve + validate project via the shared resolver.
    project_id = _resolve_project(project_id, _feedback_identity)

    _refuse_unless_project_scope(
        project_id,
        _feedback_identity,
        minimum_capability="edit",
    )

    # THE HANDLE IS THE GRANT (2026-08-30, clause 8 of `mcp-tool-surface.md`).
    #
    # A rating is an append. Until this line the tool accepted its arguments on
    # the caller's word, so the ONLY thing between a model that knew the name and
    # an appended row was the app-only discovery filter -- and the same document
    # says in as many words that "visibility metadata is not authorization".
    #
    # AFTER the scope refusal, and that order is the ratified one, not a taste:
    # `project_not_found` is the single envelope every refusal of this tool owes a
    # stranger (amendment of 2026-08-25), so a payload complaint must never be
    # able to answer first. It costs one probe on the path of a widget that forgot
    # its handle, which is the cheap side of the trade.
    try:
        require_observation_handle(handle)
    except ObservationHandleRefused as exc:
        raise ToolError(json.dumps(exc.as_dict())) from exc

    # AI-36 (AC13): per-project rate limit (default 60/hour). The refusal is an
    # operator message and is therefore in English, in the code
    # (`analyze-and-test.md:1974-1978`) -- the comment said « French message »
    # and the string had not been French for a while.
    if _feedback_rate_limited(project_id):
        raise ToolError(
            json.dumps(
                {
                    "code": "rate_limited",
                    "message": "Feedback rate limit exceeded. Try again in a few minutes.",
                }
            )
        )

    # Resolve identity from access token (AD-14).
    token: AccessToken | None = get_access_token()
    identity = (
        (token.claims.get("sub") or token.client_id) if token else "anonymous"
    )
    created_by = identity or "anonymous"

    # Mint fb_ ULID (ARCHITECTURE-SPINE §IDs: fb_ is listed explicitly).
    from ulid import ULID  # noqa: PLC0415

    fb_id = f"fb_{ULID()}"

    # Write app.feedback row, against the grant and never beside it.
    #
    # The grant is resolved on the SAME armed connection that writes, and it is
    # consumed before the commit, so the observation and the record of its
    # handle land together or not at all. `report_ref` is bound to the Result the
    # server issued the handle over: an empty ref is filled from the grant (the
    # stronger direction), a disagreeing one is refused.
    recorded_ref = report_ref
    try:
        with core_db.request_connection(identity) as conn:
            grant = resolve_observation_handle(
                conn,
                handle=handle,
                identity=identity,
                project_id=project_id,
                result_ref=report_ref,
            )
            recorded_ref = (report_ref or "").strip() or grant["result_id"]
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.feedback
                        (id, project_id, trace_id, report_ref, module,
                         rating, comment, created_by, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NOW())
                    """,
                    (
                        fb_id,
                        project_id,
                        trace_id or None,
                        recorded_ref or None,
                        connector or None,
                        rating,
                        comment or None,
                        created_by,
                    ),
                )
            consume_observation_handle(conn, handle=handle)
            conn.commit()
    except ObservationHandleRefused as exc:
        # Named BEFORE the catch-all below, which would otherwise re-label a
        # refused grant as `db_error` and hide the gesture that repairs it.
        raise ToolError(json.dumps(exc.as_dict())) from exc
    except Exception as exc:
        logger.warning("submit_feedback: db_write_failed: %s", exc)
        raise ToolError(
            json.dumps({"code": "db_error", "message": f"Database write failed: {exc}"})
        )

    # Write audit row (never raises — AC6 in audit.py).
    write_audit_row(
        identity=created_by,
        action=ACTION_FEEDBACK_SUBMITTED,
        provider_account="",
        connection_ref="",
        metadata={
            "feedback_id": fb_id,
            "rating": rating,
            "module": connector or None,
            "trace_id": trace_id,
            # The Result the grant named, not the ref the caller typed: the
            # audit must be able to say WHICH Result was rated.
            "report_ref": recorded_ref or None,
        },
    )

    # Langfuse score (AD-13): only when tracing is enabled AND trace_id is present.
    # Wrapped in try/except — Langfuse unreachable must NEVER fail the tool call (AC4).
    # Import is guarded inside the if-block: tool works when [tracing] extra absent.
    #
    # Langfuse SDK v4 note (Dev Agent Record):
    #   In Langfuse v3+ with OTel-native ingestion, the OTel trace_id (32-hex) is
    #   mapped 1:1 to the Langfuse trace_id. lf.score(trace_id=otel_trace_id_hex)
    #   therefore works directly to attach a score to the same trace. If OTel is used
    #   via OTLP (as this project does), Langfuse stores the span under the OTel
    #   trace_id, so the score links correctly. No lf.get_trace_by_otel_id() needed.
    #   Source: Langfuse OTel integration docs + SDK source review (langfuse 4.14+).
    #
    # Per-call instantiation: Langfuse client is instantiated per call (not singleton)
    # to avoid threading issues. This is slightly inefficient (creates an httpx client
    # per feedback submit). TODO: cache a module-level Langfuse client instance with
    # a lock for thread safety when call volume warrants it.
    if tracing.is_enabled() and trace_id:
        try:
            from langfuse import Langfuse  # noqa: PLC0415

            lf = Langfuse()
            lf.score(
                trace_id=trace_id,
                name="user_feedback",
                value=float(rating),
                comment=comment or None,
            )
            lf.flush()
        except Exception as lf_exc:
            # Langfuse unreachable — log but do not fail the tool call.
            logger.warning("submit_feedback: langfuse_score_failed: %s", lf_exc)

    # Write-ack only (AD-1): minimal English confirmation text, no structuredContent.
    # FR10: nothing beyond this one-line ack reaches the LLM context.
    return ToolResult(
        content=[TextContent(type="text", text="Thanks for your feedback.")],
    )


def register(mcp) -> None:  # noqa: ANN001 -- a FastMCP instance
    """Bind the feedback tool, app-visible only, on the given FastMCP app.

    `visibility=["app"]` is the declaration the tool carried inline: the widget
    calls it, the model never sees it.

    AD-43 closes the hole `mcp_profiles._record_app_declaration` names in its own
    docstring -- "`submit_feedback` is registered on plain `mcp.tool`, so it carries
    NO declaration at all and this function never sees it. That is a hole in the
    catalog." It goes through `register_profiled` now, which both records the
    declaration and enforces the app-only rule (an app-only tool must be
    `effect=read` / `confirmation_mode=none`, so a hidden mutating tool cannot be a
    side channel with a ceremony nobody can perform).

    `effect="read"` is the settled reading, not a convenience: `effect` classifies
    what a tool does to DOMAIN state, and a rating is an append-only OBSERVATION --
    "evidence that a surface was used, never a second copy of what it displayed".
    The same exemption already ships `app_read_result_manifest` and
    `app_record_evidence_inspection`.

    AND IT IS CONDITIONAL, since 2026-08-30. The exemption held only because the
    append is not something a caller can make on its own word: `submit_feedback`
    refuses without a server-minted `result_handle` (`core.app_observation_handle`),
    which the render path puts in result `_meta` and the model never reads. Until
    that line existed the discovery filter was the whole guard, and
    `mcp-tool-surface.md` says itself that "visibility metadata is not
    authorization". The document carries the condition now (amendment of
    2026-08-30): an observation is a read BEHIND A HANDLE, and a declaration that
    kept `insights`/`read` without one would be false.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        submit_feedback,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
        app=AppConfig(visibility=["app"]),
    )
