"""Story 75-1 -- a model offers a calculation to the model: `propose_calculated_field`.

WHY IT EXISTS. Epic 75's thesis is that a semantic layer must ABSORB what is
discovered during analysis and give it back, governed, to every consumer. A
model that executes a governed query, sees that two measures should be divided,
and can only say so in prose has discovered nothing the next session will know.
The console half of this rail is 75-2; this is the half the model uses, and it
is deliberately THE SAME RAIL -- one table, one queue, one vocabulary. A second
door with a second word for `open` would be a second product.

WHAT IT DEPOSITS. One proposal: a name, a typed expression tree, and the
provenance of the exploration it came from. It publishes nothing, changes no
Concept and confirms no change-set. A person accepts it -- and even then, all
the acceptance does is open a PREPARED change-set that the same person confirms
on the governance surface.

`confirmed_write`, LIKE `add_context_remark` AND `compose_dossier`. It appends a
row to a work queue a person will have to triage, so it leaves the default
Insights catalogue (an `insights` tool may not declare a mutating effect) and
the middleware requires proven interactive presence before it is listed or
called.

`edit`, NOT `view`, AND THAT IS A DEPARTURE FROM `add_context_remark`. That tool
is guarded at `view` because the console guards the same gesture at `view`, and
its reason holds for what it does: a remark carries no typed payload. This one
does, and accepting the payload opens a change-set on the Project's semantic
model. A `view` holder who could fill this queue could make the neighbour's
governance queue say whatever they wanted. The rank is the one the REST door of
this same rail asks for -- read from that door, not invented here.

MARKED AS MACHINE, AND NAMED. `origin="agent"`, and `requested_by` is the
CALLING identity rather than a shared constant, for the reason
`context_remark_mcp` records: a proposal from a machine confused with a
person's is worth less than nothing, and the queue should record WHICH agent
said it.

Conventions mirror `context_remark_mcp`: lazy `core.*` imports inside the
function body, ASCII-only source, the canonical existence-hiding refusal, and
registration through `register_profiled` (AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"


def _envelope(data: dict) -> dict:
    """The canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": "live", "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str, refusals: list | None = None):
    """A ToolError carrying the canonical ``{code, message}`` JSON.

    The named refusals of the expression contract ride along when the formula is
    what was refused: a model that sent one bad operation and one unpinned
    reference learns both in one turn instead of two.
    """
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    body: dict = {"code": code, "message": message}
    if refusals:
        body["refusals"] = refusals
    return ToolError(json.dumps(body))


def _result(summary: str, data: dict):
    """The dual-channel ToolResult (lean text summary + AD-1 envelope)."""
    from fastmcp.tools.tool import ToolResult  # noqa: PLC0415
    from mcp.types import TextContent  # noqa: PLC0415

    return ToolResult(
        content=[TextContent(type="text", text=summary)],
        structured_content=_envelope(data),
    )


def propose_calculated_field(
    project_id: str,
    name: str,
    expression: dict,
    result_id: str = "",
    query_spec_version_id: str = "",
    description: str = "",
):
    """Offer a calculation you found while exploring to the governed model.

    Files ONE proposal in this Project's promotion queue, marked as coming from
    a machine and attributed to you. Use it when a Result you executed shows a
    measure the model does not carry -- a ratio, a difference, a share -- and
    the next reader would need it too. Writes to PostgreSQL.

    This publishes NOTHING. A person accepts or declines it; accepting opens a
    prepared semantic change-set that the same person confirms in the console.
    Until then the model serves exactly as it did.

    Parameters:
        project_id:  Project identifier.
        name:        The name the field would be known by (1 to 120 chars).
        expression:  The typed expression TREE, never SQL. Each node carries an
                     allowlisted `op`; a reference to another Concept is
                     `{"op": "concept_ref", "concept_id": ..., "version_id":
                     ...}` and BOTH ids are required -- a reference without a
                     version follows `latest` and is refused. Example:
                     `{"op": "ratio", "numerator": {...}, "denominator": {...},
                     "zero_denominator": "null"}`.
        result_id:   The Result this calculation was found on. Give this, or
                     `query_spec_version_id`, or both: a promotion that cannot
                     name the exploration behind it is refused.
        query_spec_version_id: The executed plan version, when there is no
                     Result to name. Derived from the Result when absent.
        description: Optional. What the field means, for the person reviewing.

    The type and the unit are INFERRED from the tree and from the exact Concept
    versions it pins; you do not declare them, and a formula that adds a count
    to an amount of money is refused by name rather than summed.

    A Project you may not reach answers `project_not_found`, and a Result that
    Project does not hold answers `unknown_provenance` -- so comparing two
    refusals teaches nothing.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (  # noqa: PLC0415
        caller_identity,
        refuse_unless_project_scope,
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    clean_name = (name or "").strip()
    if not clean_name:
        raise _tool_error(
            "missing_param", "name is required: a field nobody can name cannot be reviewed."
        )
    if not isinstance(expression, dict) or not expression:
        raise _tool_error(
            "invalid_param",
            "expression is a typed expression tree object. Executable SQL is not "
            "accepted here.",
        )

    identity = caller_identity()
    # A WRITE whose acceptance opens a change-set, so `edit` -- through the seam
    # that fails CLOSED (AI-269), before anything is read.
    refuse_unless_project_scope(checked, identity, minimum_capability="edit")

    from core import calculated_field_proposals as proposals  # noqa: PLC0415

    provenance = {
        "result_id": (result_id or "").strip(),
        "query_spec_version_id": (query_spec_version_id or "").strip(),
    }

    try:
        with request_connection(identity) as conn:
            # The SAME reader the REST door uses. Two doors that each wrote
            # their own `SELECT org_id` would be two answers to one question.
            org_id = proposals.project_org_id(conn, checked)
            if org_id is None:
                raise _tool_error(
                    "project_not_found", "No such Project, or it is archived."
                )
            filed = proposals.propose(
                conn,
                org_id=org_id,
                project_id=checked,
                name=clean_name,
                expression=expression,
                provenance=provenance,
                origin="agent",
                requested_by=identity,
                description=(description or "").strip() or None,
            )
            conn.commit()
    except proposals.CalculatedFieldProposalError as exc:
        raise _tool_error(exc.code, exc.message, exc.refusals) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error(
            "calculated_field_proposals_mcp: deposit failed: %s", type(exc).__name__
        )
        raise _tool_error(
            "seam_unavailable", "The promotion queue is unavailable."
        ) from exc

    dependencies = filed.get("dependencies") or []
    summary = (
        f"Calculated field '{filed.get('name')}' proposed "
        f"(id: {filed.get('id')}, status: {filed.get('status')}, "
        f"inferred type: {filed.get('value_type')}"
        f"{', unit ' + str(filed['unit']) if filed.get('unit') else ''}). "
        f"It pins {len(dependencies)} exact Concept version"
        f"{'' if len(dependencies) == 1 else 's'} and publishes nothing: a "
        f"person in the console accepts or declines it, and accepting only "
        f"prepares a change-set they still have to confirm."
    )
    return _result(
        summary,
        {
            "id": filed.get("id"),
            "status": filed.get("status"),
            "origin": filed.get("origin"),
            "name": filed.get("name"),
            "value_type": filed.get("value_type"),
            "unit": filed.get("unit"),
            "currency": filed.get("currency"),
            "dependencies": dependencies,
            "provenance": filed.get("provenance"),
            "requested_by": filed.get("requested_by"),
        },
    )


def register(mcp) -> None:
    """Register `propose_calculated_field` on *mcp*.

    Declared exactly as `add_context_remark` and `compose_dossier` are:
    `profile="operations"`, `effect="confirmed_write"`,
    `data_class="operational"`, `confirmation_mode="human"` -- the queue entry
    is triaged by a person, so the middleware requires proven interactive
    presence before this tool is listed or called.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        propose_calculated_field,
        profile="operations",
        effect="confirmed_write",
        data_class="operational",
        confirmation_mode="human",
    )
