"""toorow -- the Project posture on the MCP surface: `get_project_posture`.

WHY IT EXISTS. The amendment of 2026-08-17 to `first-figure-path.md` ratifies
where a new Project's first-publication gesture lands: "THE WEB SHARE AND THE
MCP APP: publishing produces a shared render reachable by web link, and the same
result is served to the MCP app surface. (...) the project-posture MCP tool is
built so an agent can read the same first-publication state the Overview shows."

Before this module, no tool answered "what is the posture of this Project, and
has it published a first figure yet". An agent had to compose `list_datastreams`
+ `get_datastream_readiness` + `get_daily_report` and STILL could not obtain the
three posture dimensions, the deduplicated attention queue, or the readiness
component that says whether a first value exists. The audit of 2026-08-17
measured it: of the `register_profiled` declarations then assembled, not one name
carried overview, posture, getting-started or journey.

ONE COMPOSER, NOT A SECOND MODEL. This tool calls
`project_overview.compose_project_overview` -- the same function the console's
`GET /api/projects/{id}/overview` calls -- and projects a BOUNDED subset of its
envelope. A second derivation would be a second truth: the console and the agent
would eventually disagree about whether a Project has published anything, which
is precisely the divergence the audit already found between Overview and Getting
Started for the readiness evidence.

WHAT IT DOES NOT DO. It does not publish, it does not repair, it does not create
a journey. `effect="read"`, and the composer it calls only reads.

BOUNDED, BECAUSE A CATALOGUE HAS A COST. The attention queue can be long; the
tool returns the first `_ATTENTION_LIMIT` items and states `total` and
`has_more`, so a count is never silently truncated into a smaller truth. Coverage
rows, outcomes and changes are NOT returned: they answer a different question and
`get_daily_report` already carries the business signal.

Conventions mirror `first_report_render_mcp` and `platform_clocks_mcp`:
`from __future__ import annotations`, module logger, `core.*` imports LAZY inside
function bodies (no import cycle with `core.main`), ASCII-only source, and every
registration through `register_profiled` (AD-42/AD-43) -- a bare `mcp.tool`
escapes the capability middleware entirely, and since AD-43 an undeclared tool
reaches nobody rather than defaulting to Insights.

No production identifier appears here: the project is a caller-supplied opaque
identifier and every label comes from the composer.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = "1"

# How many attention items ride the envelope. The TRUE total always rides with
# them: a bounded list that hid its own denominator would be a smaller Project
# than the one that exists.
_ATTENTION_LIMIT = 5


def _envelope(data: dict) -> dict:
    """Build the canonical AD-1 structured_content envelope."""
    return {
        "schema_version": _SCHEMA_VERSION,
        "meta": {"freshness": None, "provenance": None, "alerts": []},
        "data": data,
    }


def _tool_error(code: str, message: str):
    """Return a ToolError carrying the canonical ``{code, message}`` JSON."""
    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    """Resolve the caller identity from the MCP token."""
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


def _project_not_found():
    """The CANONICAL refusal for a project the caller may not see.

    Reused, never re-spelled: `project_resolver` owns the code and the sentence,
    and a tool that invented its own envelope would be an enumeration oracle
    wearing a security word -- comparing two refusals would tell a stranger which
    project exists. `tests/isolation/test_mcp_tool_scope_refusal.py` is what
    proves every scoped tool answers with this one.
    """
    from core.project_resolver import (  # noqa: PLC0415
        PROJECT_NOT_FOUND_CODE,
        PROJECT_NOT_FOUND_MESSAGE,
    )

    return _tool_error(PROJECT_NOT_FOUND_CODE, PROJECT_NOT_FOUND_MESSAGE)


def _guard_project_view(project_id: str, identity: str) -> None:
    """Refuse unless the caller may VIEW this Project.

    Existence-hiding: a Project the caller cannot see, a Project that does not
    exist, and a guard that could not run are ONE answer. A caller outside the
    Project learns nothing -- not that it exists, not that it is denied.

    Fail-closed. An authorization seam that cannot run has not granted anything.
    """
    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity, conn, project_id=project_id, minimum_capability="view"
            )
    except Exception as exc:  # noqa: BLE001 -- fail-closed, never unguarded.
        logger.error("project_posture_mcp: access guard failed: %s", type(exc).__name__)
        raise _project_not_found() from exc
    if not decision.allowed or not decision.org_id:
        raise _project_not_found()


def _first_publication(envelope: dict[str, Any]) -> dict[str, Any]:
    """The first-publication state, named as its own fact.

    It is the `first_value` component of the readiness the Overview composes --
    `state`, the publication it is evidenced by, and the OWNER the console
    itself would open. Since the amendment of 2026-08-17 that owner is the
    Renders collection, where a published render and its web share live.

    `published` is derived, never stored: a first value exists exactly when the
    readiness names the publication that proves it.
    """
    readiness = envelope.get("readiness") or {}
    component = readiness.get("first_value") or {}
    return {
        "state": component.get("state", "unknown"),
        "published": bool(component.get("evidence_ref")),
        "evidence_ref": component.get("evidence_ref"),
        "owner": component.get("owner"),
    }


def _readiness_components(envelope: dict[str, Any]) -> dict[str, Any]:
    """The four readiness components, with the version that identifies the set."""
    readiness = envelope.get("readiness") or {}
    keys = ("project_foundation", "source", "datastream", "first_value")
    return {
        "version": readiness.get("version"),
        "schema_version": readiness.get("schema_version"),
        "components": {
            key: {
                "state": (readiness.get(key) or {}).get("state", "unknown"),
                "evidence_ref": (readiness.get(key) or {}).get("evidence_ref"),
                "owner": (readiness.get(key) or {}).get("owner"),
            }
            for key in keys
            if key in readiness
        },
    }


def _posture(envelope: dict[str, Any]) -> dict[str, Any]:
    """The three dimensions, separate. NEVER a composite score.

    `overview.md` fixes three dimensions and forbids collapsing them into one
    number; an agent that received a single figure could not name which of the
    three is limiting, which is the only actionable half of the reading.
    """
    posture = envelope.get("posture") or {}
    keys = ("operational_health", "trust_readiness", "business_signals")
    out: dict[str, Any] = {
        key: {
            "state": (posture.get(key) or {}).get("state", "unknown"),
            "explanation": (posture.get(key) or {}).get("explanation"),
            "evidence_horizon": (posture.get(key) or {}).get("evidence_horizon"),
        }
        for key in keys
        if key in posture
    }
    out["limiting_dimension"] = posture.get("limiting_dimension")
    return out


def _attention(envelope: dict[str, Any]) -> dict[str, Any]:
    """The bounded attention queue, in the composer's deterministic order."""
    attention = envelope.get("attention") or {}
    items = attention.get("items") or []
    shown = [
        {
            "id": item.get("id"),
            "cause": item.get("cause"),
            "impact": item.get("impact"),
            "scope": item.get("scope"),
            "status": item.get("status"),
            "owner": item.get("owner"),
        }
        for item in items[:_ATTENTION_LIMIT]
    ]
    total = attention.get("total", len(items))
    return {
        "items": shown,
        "total": total,
        "has_more": bool(attention.get("has_more")) or len(shown) < len(items),
    }


def _summary(project: dict[str, Any], first_publication: dict[str, Any], posture: dict[str, Any],
             attention: dict[str, Any]) -> str:
    """One line an agent can act on, without reading the envelope."""
    name = project.get("name") or project.get("id")
    limiting = posture.get("limiting_dimension")
    published = (
        "first publication done"
        if first_publication["published"]
        else f"NO first publication yet ({first_publication['state']})"
    )
    limiting_text = f"limiting dimension {limiting}" if limiting else "no limiting dimension"
    return (
        f"Project {name!r}: {published}; {limiting_text}; "
        f"{attention['total']} attention item(s)."
    )


def get_project_posture(project_id: str):
    """Read the posture of ONE project, and whether it has published a first figure.

    Returns exactly what the Project Overview screen shows, from the same
    composer -- so an agent and a person never disagree about the same
    Project:

      * `first_publication` -- has this project published a first result?
        `state`, whether it is `published`, the publication that evidences
        it, and the console destination that owns the gesture (the Renders
        collection, where the shared render and its web link live).
      * `readiness` -- the four setup components (project foundation,
        source, datastream, first value), each with its state, its evidence
        and its owner, plus the version identifying the set.
      * `posture` -- the three dimensions kept SEPARATE (operational health,
        trust and readiness, business signals) and which one is limiting.
        There is no composite score, on purpose.
      * `next_action` -- the single next gesture the project surface names,
        with whether the caller is permitted to make it.
      * `attention` -- the deduplicated queue, first 5 items, with the TRUE
        total and `has_more`.

    Missing, failed or unauthorized evidence reads as unknown, unavailable or
    permission-limited. It NEVER reads as healthy.

    Pure read: nothing is created, published or repaired. A project the
    caller may not view is `not_found` -- the same answer as a project that
    does not exist.

    Coverage rows, business outcomes and the change feed are deliberately not
    returned: `get_daily_report` answers the business question, and a
    catalogue entry that returns everything is a page, not a tool.
    """
    checked = (project_id or "").strip()
    if not checked:
        raise _tool_error("missing_param", "project_id is required.")
    identity = _identity()
    _guard_project_view(checked, identity)

    from core.db import request_connection  # noqa: PLC0415
    from core.project_overview import compose_project_overview  # noqa: PLC0415

    try:
        with request_connection(identity) as conn:
            envelope = compose_project_overview(
                checked,
                conn,
                actor=identity,
                can_edit=False,
            )
    except Exception as exc:  # noqa: BLE001
        if exc.__class__.__name__ == "ToolError":
            raise
        logger.error("project_posture_mcp: compose failed: %s", type(exc).__name__)
        raise _tool_error("seam_unavailable", "Project posture is unavailable.") from exc

    project = envelope.get("project") or {}
    first_publication = _first_publication(envelope)
    posture = _posture(envelope)
    attention = _attention(envelope)
    data = {
        "project": {
            "id": project.get("id"),
            "name": project.get("name"),
            "as_of": project.get("as_of"),
        },
        "first_publication": first_publication,
        "readiness": _readiness_components(envelope),
        "posture": posture,
        "next_action": envelope.get("next_action"),
        "attention": attention,
    }
    return _result(_summary(project, first_publication, posture, attention), data)


def register(mcp) -> None:
    """Register `get_project_posture` on *mcp*.

    Called once from `core.main` BEFORE `validate_catalog()`, so the boot-time
    validator sees the declaration. `profile="insights"` / `effect="read"` /
    `confirmation_mode="none"`: Insights is the always-discoverable, non-risky
    profile, and this tool composes a read and returns it. It writes nothing, so
    it does not belong to Operations or Governance.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        get_project_posture,
        profile="insights",
        effect="read",
        data_class="operational",
        confirmation_mode="none",
    )
