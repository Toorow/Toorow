"""toorow -- the declared total authority, read from MCP: `get_metric_grain_authority`.

WHY IT EXISTS. `query_execution` refuses a request several Datastreams could
answer, and its refusal NAMES a gesture: "Declare which Datastream is
authoritative for the total of this measure." That refusal is reachable from the
MCP door -- `execute_analyze_query_spec` meets it -- and the audit of 2026-08-17
(`reviews/audit-2026-08-17/03-analytics.md`, P1-4) measured what happened next:
`grep metric_grain server/core/*mcp*.py` returned zero. An agent was sent toward
a gesture it could not make, and could not even read whether the gesture had
already been made.

WHAT THIS DOOR CARRIES, AND WHAT IT DOES NOT. It carries the READING: who carries
this measure, at which grain, which one was declared authoritative for the total,
what each breakdown was declared to sum to, and the one sentence naming what is
owed next. It does NOT carry the declaration itself. Declaring the total is a
governed console gesture -- `PUT /api/projects/{id}/metric-grain/{concept_id}/total`,
`member` rank, audited by `metric_grain._write_audit` -- and duplicating that
write here would give the product two authorities over the same statement about
where its figures come from. The refusal an agent meets therefore stays true: the
gesture is a person's, and this tool tells the agent whether it has been made and
by which Datastream, so the agent can name it instead of guessing.

`grain_coverage` already described itself as "the shape both the console and the
MCP door read". The console read it; the MCP door did not exist. This is that
door, and it composes through the SAME function -- a second derivation would let
the screen and the agent disagree about which Datastream is authoritative, which
is the one disagreement this whole chantier exists to prevent.

Conventions mirror `project_posture_mcp`: lazy `core.*` imports inside function
bodies, ASCII-only source, the canonical existence-hiding refusal, and
registration through `register_profiled` (AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: How many carrier lines ride the text channel. The full reading rides
#: structuredContent (AD-1).
_SUMMARY_MAX_LINES = 10


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": "live", "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _result(summary: str, data: dict):
    """Build the dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def _summary(coverage: dict) -> str:
    """The reading, in the order it is repaired: carriers, total, breakdowns."""
    carriers = coverage.get("carriers") or []
    total_id = coverage.get("total_datastream_id")
    lines = [
        f"{coverage.get('concept_name')}: {len(carriers)} carrier(s); "
        + (
            f"total declared on {total_id}."
            if total_id
            else "NO total authority declared."
        )
    ]
    for carrier in carriers[: _SUMMARY_MAX_LINES - 2]:
        role = carrier.get("role")
        sums_to = carrier.get("sums_to")
        tail = "" if role == "total" else f", sums_to={sums_to or 'undeclared'}"
        lines.append(
            f"- {carrier.get('datastream_name')} ({carrier.get('datastream_id')})"
            f" [{role}{tail}] grain={'/'.join(carrier.get('grain') or []) or 'unstated'}"
        )
    hidden = len(carriers) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    gesture = coverage.get("next_gesture")
    if gesture:
        lines.append(f"Next: {gesture}")
    return "\n".join(lines)


def get_metric_grain_authority(
    project_id: str,
    concept_id: str | None = None,
    concept_name: str | None = None,
    semantic_view_version_id: str | None = None,
):
    """Which Datastream is authoritative for the total of one measure.

    Answers what a refused query raises: when several Datastreams can answer
    a request, which one was DECLARED to carry the total, and what were the
    others declared to sum to? Name the measure by `concept_id` or by
    `concept_name` (the machine name a mapping binds as canonical target).

    Returns the `carriers` with their grain and role (`total` / `breakdown`),
    `total_datastream_id` -- null when nothing was declared, which is why the
    query is refused rather than silently answered by one of them -- the
    `declaration` itself, how many carriers a given
    `semantic_view_version_id` binds, and `next_gesture`: one sentence naming
    what is owed next, or null.

    Read-only. Declaring the total, or what a breakdown sums to, is a
    governed console gesture; this door reports whether it was made.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    wanted_id = (concept_id or "").strip() or None
    wanted_name = (concept_name or "").strip() or None
    if not wanted_id and not wanted_name:
        raise _tool_error(
            "missing_param", "Name the measure by concept_id or by concept_name."
        )

    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core.metric_grain import grain_coverage  # noqa: PLC0415
    from core.metric_grain_api import _concept  # noqa: PLC0415
    from core.semantic_model import concept_names  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            if wanted_id is None:
                # The name -> id direction has no reader of its own; it is the
                # inversion of the one that exists. Writing a second SELECT
                # here is how two readers of one table start disagreeing about
                # which concept a name means.
                matches = [
                    cid
                    for cid, name in concept_names(conn, checked).items()
                    if name == wanted_name
                ]
                if len(matches) != 1:
                    raise _tool_error(
                        "concept_not_found",
                        "No single Semantic Concept of this Project carries "
                        "this name.",
                    )
                wanted_id = matches[0]
            found = _concept(conn, project_id=checked, concept_id=wanted_id)
            if found is None:
                # The same refusal the console route gives, for the same
                # measure. A platform-scoped concept is refused here exactly
                # as it is there -- one rule, not two doors.
                raise _tool_error(
                    "concept_not_found",
                    "No Semantic Concept with this id in this Project.",
                )
            coverage = grain_coverage(
                conn,
                project_id=checked,
                concept_id=found[0],
                concept_name=found[1],
                view_version_id=(semantic_view_version_id or "").strip() or None,
            )
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("metric_grain_mcp: read failed: %s", type(exc).__name__)
        raise _tool_error(
            "seam_unavailable", "The metric grain reading is unavailable."
        ) from exc

    return _result(_summary(coverage), coverage)


def register(mcp) -> None:
    """Register `get_metric_grain_authority` on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`.
    `profile="insights"` / `effect="read"` / `confirmation_mode="none"`: the tool
    reads a declaration, it never writes one, and a read authorizes nothing.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_metric_grain_authority,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
