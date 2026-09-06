"""toorow -- an agent deposits a remark on a Hub node: `add_context_remark`.

WHY IT EXISTS. The audit of 2026-08-17
(`reviews/audit-2026-08-17/08-context-hub.md`) measured the asymmetry in one
line: for the review queue, "l'agent LIT (`get_procedure` -> `open_remarks`) ;
**aucun outil pour deposer**". `context-hub.md` states the target the other way
round -- "if the model finds a Skill wrong or stale it can flag it" -- and the
model could not. The queue existed (`app.context_review_requests`, migration
215), the reader existed, the console writer existed, and the one caller who
meets a wrong Skill most often had no way to say so.

WHAT IT DEPOSITS. One remark, on one node -- a Knowledge topic or a Skill -- AT A
PRECISE VERSION. The version is not decoration: "this constraint no longer
exists" means nothing when nobody knows which version is being talked about, and
a remark that floats can be neither verified nor closed. The version is read in
the same transaction as the write, from the node itself, never taken from the
caller.

ATTRIBUTED, AND MARKED AS MACHINE. `origin="agent"` and `requested_by` is the
CALLING identity, not a shared constant. `context_review` has distinguished
`human` from `agent` since it was written -- "une remarque de machine confondue
avec une remarque humaine vaut moins que rien" -- and the existing machine
producer (`propose_missing_link`) deposits under the generic `agent:toorow`
because no identity is available where it runs. Here one is, so the queue records
WHICH agent said it.

`view`, NOT `edit`, AND IT IS THE CONSOLE'S RULE. `context_api._request_review`
guards this exact gesture at `view` with the reason written at its line: "any
consumer of the knowledge can flag a node, not only those who write it." A door
that demanded `edit` for the same gesture would make two answers to one question
about who may flag -- and it would be stricter than the agent write path the
product already has. The rank is the console's, read from the console, not
invented here. What the rank buys is bounded: the remark is a QUEUE ENTRY on a
node the caller may already read; it changes no governed content, and only a
`member` can accept or decline it.

WHAT IT DOES NOT DO: notify, and apply. The queue's own docstring already refuses
to promise notification. And `proposed_change` -- the typed payload a remark may
carry so that accepting it APPLIES something -- is deliberately not exposed here:
its kinds are governed and applying one writes a business link. A remark that
only says remains a remark, and that is the common case. Naming the payload as
not carried is cheaper than a half-built write.

Conventions mirror `project_posture_mcp`: lazy `core.*` imports inside function
bodies, ASCII-only source, the canonical existence-hiding refusal, and
registration through `register_profiled` (AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: The two node kinds the Hub review queue accepts. Named explicitly rather than
#: derived from the id prefix, so an unknown shape never resolves in silence to
#: the wrong reader -- the same choice the console route makes.
_NODE_TYPES = ("topic", "procedure")


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


def add_context_remark(
    project_id: str,
    node_type: str,
    node_id: str,
    note: str,
):
    """Flag a Knowledge topic or a Skill that is wrong, stale or incomplete.

    Deposits ONE remark in the Context Hub review queue, against the node's
    CURRENT version, attributed to the calling agent and marked as machine
    origin so a human reviewer can tell it from a person's remark.

    Use it when a governed Skill or Knowledge entry contradicts what was
    observed -- a constraint that no longer holds, a step that no longer
    exists, a missing case. Say what is wrong and what you observed; the note
    is what a reviewer reads, and a remark that says nothing cannot be
    reviewed.

    Parameters:
        project_id: the Project whose Hub is being read.
        node_type:  `topic` (Knowledge) or `procedure` (Skill).
        node_id:    the node's id, as returned by `get_knowledge`,
                    `get_skills` or `get_procedure`.
        note:       what is wrong, in the reader's words (1 to 4000 chars).

    The remark keeps the NODE's scope, not the caller's: a remark on a
    platform Skill concerns the whole organization, exactly as the queue
    reads it.

    An identical remark, on the same version, by the same author, is a
    DUPLICATE and not a second signal: the existing one is returned instead
    of a second row being created.

    This does not notify anyone, does not change the node, and does not
    propose a typed change to apply. It opens a queue entry that a person
    will accept or decline; until then the Skill keeps serving as it is.

    A Project the caller may not view answers `project_not_found`, and a node
    that does not exist in it answers `node_not_found` -- the same answer, so
    comparing two refusals teaches nothing.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    kind = (node_type or "").strip()
    if kind not in _NODE_TYPES:
        raise _tool_error(
            "invalid_param", "node_type must be one of: " + ", ".join(_NODE_TYPES) + "."
        )
    target = (node_id or "").strip()
    if not target:
        raise _tool_error("missing_param", "node_id is required.")
    text = (note or "").strip()
    if not text:
        raise _tool_error(
            "invalid_param",
            "note is required: a remark that says nothing cannot be reviewed.",
        )

    # `view`, and the reason is written in the module docstring: the console
    # guards this same gesture at `view`, and two ranks for one gesture would
    # be two products.
    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core import context_review  # noqa: PLC0415
    from core.context_api import _project_org_id  # noqa: PLC0415
    from core.context_store import get_procedure, get_topic  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            node = (
                get_topic(conn, topic_id=target, caller_project_id=checked)
                if kind == "topic"
                else get_procedure(
                    conn, procedure_id=target, caller_project_id=checked
                )
            )
            if not node:
                raise _tool_error(
                    "node_not_found", "No such node in this Project's Context Hub."
                )
            try:
                queued = context_review.request_review(
                    conn,
                    org_id=_project_org_id(conn, checked),
                    # The remark keeps the NODE's scope, not the caller's --
                    # the console route holds the same rule, and `list_open`
                    # reads a NULL project_id exactly this way.
                    project_id=node["project_id"],
                    node_type=kind,
                    node_id=target,
                    # Read in THIS transaction, from the node itself. A
                    # version supplied by the caller would let a remark be
                    # filed against a version nobody served.
                    node_version=int(node.get("version_number") or 1),
                    note=text,
                    requested_by=identity,
                    origin="agent",
                )
            except context_review.ReviewRequestError as exc:
                raise _tool_error("invalid_param", str(exc)) from exc
            conn.commit()
    except ValueError as exc:
        # `_project_org_id` raises when the Project carries no active
        # organization -- indistinguishable, from outside, from a node that
        # is not there.
        raise _tool_error(
            "node_not_found", "No such node in this Project's Context Hub."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("context_remark_mcp: deposit failed: %s", type(exc).__name__)
        raise _tool_error(
            "seam_unavailable", "The review queue is unavailable."
        ) from exc

    summary = (
        f"Remark queued on {kind} {target} v{queued.get('node_version')} "
        f"(id: {queued.get('id')}, status: {queued.get('status')})."
    )
    return _result(
        summary,
        {
            "id": queued.get("id"),
            "node_type": queued.get("node_type"),
            "node_id": queued.get("node_id"),
            "node_version": queued.get("node_version"),
            "status": queued.get("status"),
            "origin": queued.get("origin"),
            "requested_by": queued.get("requested_by"),
        },
    )


def register(mcp) -> None:
    """Register `add_context_remark` on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`.
    `profile="operations"` / `effect="confirmed_write"` /
    `confirmation_mode="human"`: it appends a row to a work queue a person will
    have to triage, so it leaves the default Insights catalogue (an `insights`
    tool may not declare a mutating effect) and the middleware requires proven
    interactive presence before it is listed or called.

    The AD-27 ceremony INSIDE the body remains the open debt `Incomplete if` n.9
    of `mcp-tool-surface.md` names for every write that left `insights` -- this
    tool inherits it rather than pretending otherwise. What is enforced today is
    the gate: no attested interactive context, no tool.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415


    register_profiled(
        mcp,
        add_context_remark,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
