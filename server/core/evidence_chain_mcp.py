"""toorow -- the Evidence door on MCP: re-walk the chain a figure came from.

WHY IT EXISTS. The gap audit of 2026-09-05 measured ten of the eighteen ratified
gestures as green on component, API and UI and dark on MCP
(`reviews/audit-2026-09-05-gap/00-synthese.md`, 2). This is the one whose verdict
was the sharpest, verbatim: *"walk the chain back -- lineage, provenance,
versions, approvals, audited activity. `app_record_evidence_inspection` is
app-only and not on the wire; no tool walks lineage, versions or the audit log.
`partial` -- MCP plane missing: **a model cannot re-walk the chain it is asked to
trust.**"*

That sentence is the whole reason for this module. The product's claim is that a
model composes a governed figure and can say where it came from; the model could
compose it and could not check it. It could record that it had inspected evidence
-- `app_record_evidence_inspection`, app-only -- and could not read any.

ONE TOOL, ONE QUESTION. `walk_evidence_chain` answers *where does this come from,
and who decided it*. It is one tool and not three because the three lenses of the
Evidence section are three answers to that one question, exactly as
`get_data_surface` holds the six lenses of the Data screen behind one tool:

  * `lineage-provenance`   -- what feeds what, and from which source;
  * `versions-approvals`   -- which version was in force, and who approved it;
  * `audit-activity`       -- what was actually done, and by whom.

Naming an object opens ONE of them by exact identity -- an `evidence-trace`
(overview, lineage, provenance), an `object-version` (overview, diff, approvals,
used-by) or an `audit-event` (overview). The collection bound is a DISPLAY bound:
the 201st Evidence Record still opens by id, because a chain that stops being
walkable at two hundred is not a chain.

ONE STATE, TWO DOORS (AD-1). Every read here calls
`governance_read_model.compose_governance_collection` and
`compose_governance_object` -- the same two functions
`governance_surface_api.py` calls for the console's Evidence screen, with the
same `section="evidence"`. No SQL, no lens table, no decoration and no bound
lives in this file. A second derivation would let a model and a person disagree
about the provenance of the same figure, which is the exact defect this surface
exists to remove.

READ-ONLY, AND ENTIRELY. Nothing here approves, re-approves, retires or annotates
anything. Walking a chain must not be able to change it: the Evidence surface is
where a decision is *re-read*, and the acts that make decisions live on the
surfaces that own them (`publish_semantic_model_change`, `publish_shared_identity`,
`decide_evaluation_run`). `app_record_evidence_inspection` stays app-only and is
not re-exported here: recording that an inspection happened is the app's job at
the moment a person looks, not a model's to assert.

WHY `insights`. It is a read of state that mutates nothing, the rank its
neighbours `get_data_surface` and `get_evaluation_runs` hold. The alternative
considered was `governance`, on the ground that the subject is governed; the rank
follows the EFFECT and not the subject, or every read of a governed object would
climb, and the profile that exists for safe reads would end up holding none of
the ones that matter.

Conventions mirror `evaluation_mcp` and `feedback_review_mcp`: lazy `core.*`
imports inside function bodies (no cycle with `core.main`), ASCII-only source, one
canonical existence-hiding refusal, and registration through `register_profiled`
(AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: The section this door reads, and the only one. A parameter here would make it
#: a second `get_data_surface` for Governance at large -- a different tool, with a
#: different question, and `mcp-tool-surface.md` says a tool without a question of
#: its own has no place.
_SECTION = "evidence"

#: How many lines the text channel carries. The payload rides structuredContent.
_SUMMARY_MAX_LINES = 12


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


def _org_of(conn, project_id: str) -> str:
    """The organization this Project belongs to -- the single reader, borrowed."""
    from core.context_api import _project_org_id  # noqa: PLC0415

    return _project_org_id(conn, project_id)


def _contract():
    """The Evidence section's own contract: its lenses and its object types.

    READ from `governance_read_model`, never copied. The lens names are a
    vocabulary the console and the server already share; a third copy here would
    accept a lens the composer refuses, or refuse one it accepts.
    """
    from core.governance_read_model import resolve_section  # noqa: PLC0415

    return resolve_section(_SECTION)


def _collection_summary(project_id: str, lens: str, envelope: dict) -> str:
    """One line per item, and an empty lens that says why in its own words."""
    items = envelope.get("items") or []
    if not items:
        reasons = envelope.get("unavailable_reasons") or []
        said = " ".join(
            str(r.get("message") or "") for r in reasons if isinstance(r, dict)
        ).strip()
        return (
            f"Nothing on the `{lens}` lens of the Evidence chain in {project_id!r}. "
            + (
                said
                or "A chain appears when a governed object is published, approved "
                "or acted on: nothing has been yet."
            )
        )
    lines = [f"{len(items)} entr(y|ies) on `{lens}` in {project_id!r}:"]
    for item in items[: _SUMMARY_MAX_LINES - 1]:
        lines.append(
            f"- {item.get('object_type') or item.get('kind') or 'entry'}"
            f" {item.get('id') or item.get('object_id')}"
            f" {item.get('label') or item.get('title') or ''}".rstrip()
        )
    hidden = len(items) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    return "\n".join(lines)


def walk_evidence_chain(
    project_id: str,
    lens: str | None = None,
    object_type: str | None = None,
    object_id: str | None = None,
    cursor: str | None = None,
    filters: dict | None = None,
):
    """Where does this come from, and who decided it -- the chain, re-walked.

    Three lenses, three answers to that one question:
      * `lineage-provenance` (default) -- what feeds what, and from which source;
      * `versions-approvals` -- which version was in force, and who approved it;
      * `audit-activity` -- what was actually done here, and by whom.

    Naming `object_type` and `object_id` opens ONE entry by exact identity
    instead of the lens: an `evidence-trace` (overview, lineage, provenance), an
    `object-version` (overview, diff, approvals, used-by) or an `audit-event`.
    The collection bound is a DISPLAY bound -- the 201st Evidence Record still
    opens by id, because a chain that stops being walkable at two hundred is not
    a chain.

    `cursor` and `filters` page and narrow a lens that paginates; a filter aimed
    at a lens that does not is REFUSED rather than ignored, because an ignored
    filter renders a page that does not match the address that produced it.

    Read-only, entirely. Walking a chain cannot change it: nothing here approves,
    re-approves, retires or annotates. The acts that make decisions live on the
    surfaces that own them.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")

    contract = _contract()
    chosen = (lens or "").strip() or contract.default_lens
    if chosen not in contract.lenses:
        raise _tool_error(
            "unknown_lens", "lens is one of: " + ", ".join(contract.lenses) + "."
        )
    wanted_type = (object_type or "").strip() or None
    wanted_id = (object_id or "").strip() or None
    if bool(wanted_type) != bool(wanted_id):
        raise _tool_error(
            "missing_param",
            "opening one entry needs BOTH object_type and object_id; omit both to "
            "read the lens.",
        )
    if wanted_type:
        declared = tuple(o.object_type for o in contract.objects)
        if wanted_type not in declared:
            raise _tool_error(
                "unknown_object_type",
                "object_type is one of: " + ", ".join(declared) + ".",
            )
    if filters is not None and not isinstance(filters, dict):
        raise _tool_error("invalid_param", "filters is an object.")

    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core.governance_read_model import (  # noqa: PLC0415
        GovernanceUnknownRoute,
        compose_governance_collection,
        compose_governance_object,
    )

    query: dict = dict(filters or {})
    if (cursor or "").strip():
        query["cursor"] = cursor.strip()

    try:
        with request_connection(identity) as conn:
            org_id = _org_of(conn, checked)
            if wanted_type:
                envelope = compose_governance_object(
                    checked,
                    _SECTION,
                    wanted_type,
                    wanted_id,
                    conn,
                    org_id=org_id,
                )
                summary = (
                    f"{wanted_type} {wanted_id} of the Evidence chain in "
                    f"{checked!r}. Its tabs answer the chain in order; nothing "
                    "read here can change what it records."
                )
                return _result(
                    summary,
                    {
                        "project_id": checked,
                        "object_type": wanted_type,
                        "object_id": wanted_id,
                        **(envelope if isinstance(envelope, dict) else {}),
                    },
                )
            envelope = compose_governance_collection(
                checked,
                _SECTION,
                conn,
                lens=chosen,
                org_id=org_id,
                query=query or None,
            )
    except GovernanceUnknownRoute as exc:
        # The composer's own refusal for an unknown lens or object type. It is a
        # shape refusal about the CALL, so it is not hidden behind not_found.
        raise _tool_error("unknown_lens", str(exc)) from exc
    except LookupError as exc:
        raise _tool_error(
            "evidence_not_found", "No such entry in this Project's Evidence chain."
        ) from exc
    except ValueError as exc:
        raise _tool_error("invalid_param", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("evidence_chain_mcp: read failed: %s", type(exc).__name__)
        raise _tool_error(
            "seam_unavailable", "The Evidence chain is unavailable."
        ) from exc

    data = {"project_id": checked, "lens": chosen}
    if isinstance(envelope, dict):
        data.update(envelope)
    return _result(_collection_summary(checked, chosen, data), data)


def register(mcp) -> None:
    """Register the Evidence door on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`. One tool,
    `profile="insights"` / `effect="read"` / `confirmation_mode="none"`: the rank
    follows the EFFECT and not the subject. A read that mutates nothing is the
    safe default even when what it reads is governed -- the alternative would
    climb every read of a governed object into `governance` and leave the profile
    that exists for safe reads holding none of the ones that matter.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        walk_evidence_chain,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
