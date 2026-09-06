"""toorow -- the Project access door on MCP: who reaches what, and handing it on.

WHY IT EXISTS. The gap audit of 2026-09-05 measured ten of the eighteen ratified
gestures as dark on the MCP plane, and this was the one with the barest row of
all (`reviews/audit-2026-09-05-gap/00-synthese.md`, 2). Verbatim: *"know who has
access to what, and hand an access on -- **none.** No registered tool names
access, a grant or a handoff. `partial` -- MCP plane missing entirely."* A model
could act inside a Project all day and could not answer the first question anyone
asks about one: who else is in here, and what may they do.

TWO TOOLS, TWO QUESTIONS (`mcp-tool-surface.md`):

  * ``read_project_access``   -- Insights, READ. Who reaches this Project, at
                                 which capability, and WHERE that capability
                                 comes from: the organization role, the project
                                 grant, and the effective capability the two
                                 resolve to.
  * ``grant_project_access``  -- Governance, WRITE, confirmation "human". Change
                                 one person's capability, or hand an access on so
                                 someone can resume where the work stopped.

ONE STATE, TWO DOORS (AD-1). Every call here goes through
`core.project_access_surface` -- `read_project_access`, `prepare_grant_change`,
`issue_grant_confirmation`, `confirm_grant_change`, `prepare_access_handoff` --
the five functions `project_access_api.py` translates HTTP into and the
Project access screens drive. No SQL, no capability table, no expiry and no
idempotency lives in this file.

THE SECRET IS MINTED AND CONSUMED INSIDE ONE TRANSACTION, and never enters model
context. `prepare_grant_change` opens the change, `issue_grant_confirmation`
mints a single-use secret, `confirm_grant_change` spends it. The console
round-trips that secret through a browser because a person has to be the one who
confirms; here the three calls happen inside one server-side transaction, so the
secret is created and spent without ever being written into an answer. That is
the pattern `governance_mcp.publish_semantic_model_change` established, and it is
strictly stronger than the console path rather than a weaker version of it.

WHAT A HANDOFF RETURNS, AND WHAT IT DOES NOT. `handoff_id`, `state`, `expires_at`
and the `resume_ref` -- never a URL and never a token. `mcp-tool-surface.md`
(amendment of 2026-08-17) draws that line for Shares in the same words: the
object is operable by an agent, the secret link is not. A `resume_ref` must be a
same-origin canonical route; the service refuses anything else and this door does
not soften it.

WHAT IS NOT BUILT, AND SAID SO. Inviting someone who is not already a member of
the organization is not here: that is an organization gesture with its own
delivery, its own expiry and its own screens. This door changes what an existing
member reaches inside ONE Project, and hands an access on to someone who can
already sign in.

Conventions mirror `governance_mcp` and `evidence_chain_mcp`: lazy `core.*`
imports inside function bodies (no cycle with `core.main`), ASCII-only source, one
canonical existence-hiding refusal, and registration through `register_profiled`
(AD-42/AD-43).
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

#: How many lines the text channel carries. The payload rides structuredContent.
_SUMMARY_MAX_LINES = 12

#: The two acts this door carries, closed. An unknown action is refused by name
#: rather than falling through to the safest branch.
_ACTIONS = ("set_capability", "hand_off")

#: The capability floor. Deciding who reaches a Project is the rank the console
#: guards it at, and the rank `publish_shared_identity` holds for the same
#: reason: it decides what other people may do afterwards.
_WRITE_CAPABILITY = "manage"


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


def _capabilities() -> tuple[object, ...]:
    """The closed capability set, READ from the service and never copied.

    A copy ages, and the day it aged this door would accept a capability the
    screen refuses. `None` is a member of the set on purpose: it is how a grant
    is REMOVED, and a door that could not express it could raise an access and
    never lower one.
    """
    from core.project_access_surface import _CAPABILITIES  # noqa: PLC0415

    return tuple(sorted(_CAPABILITIES, key=lambda value: (value is not None, value or "")))


def _access_summary(project_id: str, access: dict) -> str:
    """One line per person, and the two sources of a capability kept apart."""
    people = access.get("members") or access.get("people") or []
    if not people:
        return (
            f"No member reaches project {project_id!r} yet. Access comes from an "
            "organization membership, then from a grant on this Project: nothing "
            "here invites anyone into the organization."
        )
    lines = [f"{len(people)} identit(y|ies) reach {project_id!r}:"]
    for person in people[: _SUMMARY_MAX_LINES - 1]:
        effective = person.get("effective_capability") or person.get("effective")
        lines.append(
            f"- {person.get('identity')}: org role {person.get('role')},"
            f" project grant {person.get('capability') or 'none'}"
            f" -> effective {effective or 'none'}"
        )
    hidden = len(people) - (len(lines) - 1)
    if hidden > 0:
        lines.append(f"[+{hidden} more in the detail]")
    return "\n".join(lines)


def read_project_access(project_id: str):
    """Who reaches this Project, at which capability, and where it comes from.

    Every identity with its ORGANIZATION ROLE, its PROJECT GRANT and the
    EFFECTIVE capability the two resolve to -- the three kept apart, because an
    effective capability alone cannot be repaired: raising a grant does nothing
    for someone whose org role already caps them lower, and a reader who sees one
    number cannot tell which of the two to change.

    Read-only. Changing a capability or handing an access on is
    `grant_project_access`.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import (
        caller_identity,  # noqa: PLC0415
        refuse_unless_project_scope,  # noqa: PLC0415
    )

    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    identity = caller_identity()
    refuse_unless_project_scope(checked, identity)

    from core.project_access_surface import (  # noqa: PLC0415
        ProjectAccessUnavailable,
    )
    from core.project_access_surface import (  # noqa: PLC0415
        read_project_access as _read,
    )

    try:
        with request_connection(identity) as conn:
            access = _read(checked, conn, actor=identity)
    except ProjectAccessUnavailable as exc:
        raise _tool_error(
            "project_not_found", "Project not found or archived."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("project_access_mcp: read failed: %s", type(exc).__name__)
        raise _tool_error("seam_unavailable", "Project access is unavailable.") from exc

    return _result(_access_summary(checked, access), {"project_id": checked, **access})


def grant_project_access(project_id: str, intent: dict, idempotency_key: str):
    """Change what one person reaches here, or hand an access on.

    - `intent.action = "set_capability"`: `identity` and `capability` -- one of
      view, edit, manage, or `null` to REMOVE the grant. The change is prepared,
      its single-use confirmation is minted and spent inside ONE transaction, so
      the confirmation secret never enters this answer. An owner of the
      organization cannot have their access removed this way; the service refuses
      it, and this door does not soften the refusal.
    - `intent.action = "hand_off"`: `resume_ref` (a same-origin canonical route
      beginning `/org/`), optional `identity`, optional `expires_in_hours`
      (default 48). The answer carries the handoff's id, state, expiry and
      resume_ref -- never a URL and never a token: the object is operable by an
      agent, the link is not.

    `idempotency_key` is mandatory: a retried grant resumes the same change
    instead of opening a second one.

    Inviting someone who is not yet a member of the organization is NOT here --
    that is an organization gesture with its own delivery and its own screens.
    """
    checked = (project_id or "").strip()
    key = (idempotency_key or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    if not isinstance(intent, dict) or not intent:
        raise _tool_error(
            "missing_param",
            "intent is required: {action: set_capability | hand_off, ...}.",
        )
    action = str(intent.get("action") or "").strip()
    if action not in _ACTIONS:
        raise _tool_error(
            "unknown_action", "intent.action is one of: " + ", ".join(_ACTIONS) + "."
        )
    if not key:
        raise _tool_error(
            "missing_idempotency_key",
            "An idempotency key is required so a retried grant resumes the same "
            "change instead of opening a second one.",
        )

    allowed = _capabilities()
    if action == "set_capability":
        if not str(intent.get("identity") or "").strip():
            raise _tool_error(
                "missing_param", "intent.identity is required for set_capability."
            )
        if "capability" not in intent:
            raise _tool_error(
                "missing_param",
                "intent.capability is required: "
                + ", ".join(str(c) for c in allowed if c)
                + ", or null to remove the grant.",
            )
        if intent.get("capability") not in allowed:
            raise _tool_error(
                "invalid_param",
                "intent.capability must be one of: "
                + ", ".join(str(c) for c in allowed if c)
                + ", or null to remove the grant.",
            )
    elif not str(intent.get("resume_ref") or "").strip():
        raise _tool_error(
            "missing_param",
            "intent.resume_ref is required for hand_off: the same-origin canonical "
            "route the recipient resumes at.",
        )

    from core.db import request_connection  # noqa: PLC0415
    from core.mcp_scope import caller_identity  # noqa: PLC0415

    actor = caller_identity()
    if not actor or actor == "anonymous":
        raise _tool_error("project_not_found", "Project not found or archived.")

    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.project_access_surface import (  # noqa: PLC0415
        ProjectAccessConflict,
        ProjectAccessUnavailable,
        ProjectAccessValidationError,
        confirm_grant_change,
        issue_grant_confirmation,
        prepare_access_handoff,
        prepare_grant_change,
    )

    with request_connection(actor) as conn:
        try:
            decision = resolve_strict_resource_access(
                actor,
                conn,
                project_id=checked,
                minimum_capability=_WRITE_CAPABILITY,
                hold_access=True,
            )
        except Exception as exc:  # noqa: BLE001 -- an outage fails CLOSED
            logger.error("project_access_mcp: access resolution failed: %s", exc)
            raise _tool_error(
                "project_not_found", "Project not found or archived."
            ) from exc
        if not decision.allowed or not decision.org_id:
            raise _tool_error("project_not_found", "Project not found or archived.")

        try:
            if action == "set_capability":
                change = prepare_grant_change(
                    conn,
                    project_id=checked,
                    identity=str(intent["identity"]).strip(),
                    after_capability=intent.get("capability"),
                    actor=actor,
                    idempotency_key=key,
                )
                # The three calls of the ceremony, in ONE transaction. The secret
                # is minted on the line above the one that spends it and is never
                # placed in `landed`: strictly stronger than the console path,
                # where a browser has to carry it.
                issued = issue_grant_confirmation(
                    conn,
                    change_id=str(change.get("change_id") or change.get("id")),
                    actor=actor,
                    project_id=checked,
                )
                landed = confirm_grant_change(
                    conn,
                    change_id=str(change.get("change_id") or change.get("id")),
                    actor=actor,
                    confirmation_id=str(issued["confirmation_id"]),
                    confirmation_secret=str(issued["confirmation_secret"]),
                    project_id=checked,
                )
            else:
                landed = prepare_access_handoff(
                    conn,
                    project_id=checked,
                    identity=(str(intent.get("identity") or "").strip() or None),
                    actor=actor,
                    resume_ref=str(intent["resume_ref"]).strip(),
                    expires_in_hours=int(intent.get("expires_in_hours") or 48),
                    idempotency_key=key,
                )
            conn.commit()
        except ProjectAccessUnavailable as exc:
            raise _tool_error(
                "project_not_found", "Project not found or archived."
            ) from exc
        except ProjectAccessValidationError as exc:
            raise _tool_error("invalid_param", str(exc)) from exc
        except ProjectAccessConflict as exc:
            raise _tool_error("conflict", str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            if exc.__class__.__name__ == "ToolError":
                raise
            logger.error("project_access_mcp: %s failed: %s", action, type(exc).__name__)
            raise _tool_error(
                "seam_unavailable", "The access change could not be recorded."
            ) from exc

    # NOTHING OF THE CEREMONY TRAVELS BACK. Whatever the service returned, the
    # two secret-bearing keys are stripped by name here as well as never being
    # added: a future service that started returning one would not leak through
    # this door on the day it changed.
    safe = {
        key_: value
        for key_, value in (landed or {}).items()
        if key_ not in {"confirmation_secret", "secret", "token", "url"}
    }
    if action == "set_capability":
        summary = (
            f"{intent.get('identity')} now reaches {checked!r} at "
            f"{intent.get('capability') or 'no project grant'}. The confirmation "
            "was minted and spent inside one transaction; no secret is in this "
            "answer."
        )
    else:
        summary = (
            f"Access handoff {safe.get('handoff_id')} is {safe.get('state')}, "
            f"expiring {safe.get('expires_at')}, resuming at "
            f"{safe.get('resume_ref')}. The link itself is not an agent's to "
            "carry and is not here."
        )
    return _result(summary, {"project_id": checked, "action": action, **safe})


def register(mcp) -> None:
    """Register the Project access door on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`.

    REGISTERED ONE BY ONE, AND NOT IN A LOOP, for the reason `evaluation_mcp`
    states: the legacy census derives a tool's identity from the AST of the
    `register_profiled` call.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        read_project_access,
        # A read that mutates nothing, but what it reads is who people are and
        # what they may do: `sensitive`, like every other read of an identity.
        profile="insights",
        effect="read",
        data_class="sensitive",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        grant_project_access,
        # Deciding who reaches a Project decides what other people may do
        # afterwards: the rank of `publish_shared_identity`, for the same reason.
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
