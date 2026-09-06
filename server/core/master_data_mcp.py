"""Master Data over MCP -- the second door onto the SAME command runner (48.2 tâche 7).

WHY THIS MODULE EXISTS
Measured 2026-08-04: ``country_workspace_commands.run_country_workspace_command``
had exactly ONE caller -- the REST route at ``governance_surface_api.py:326`` --
and no MCP tool existed for master data at all (``governance_mcp`` covers only
per-Datastream agent changes). Story 48.2 task 7 asks for a REST/MCP hash parity
proof; there was nothing to compare against, so the task read as "a test to
write" when it was a door to build.

WHAT IT DELIBERATELY DOES NOT DO
It re-implements no validation, no versioning and no hashing. Every one of those
lives in ``country_workspace_commands`` and is reached by calling the SAME
function the route calls. That is what makes the parity STRUCTURAL rather than
asserted: two doors that each computed their own hash could agree in a test and
diverge in production the day one of them was edited.

The authorization is mirrored EXACTLY, not approximated:
``set_local_access_context`` + ``resolve_strict_resource_access`` with the same
capability mapping (``manage`` for publish and prepare_publish, ``edit``
otherwise) and the same ``hold_access=True``. A second door with a softer guard
is not a second door, it is a way around the first one.

Deny-by-default like its neighbours: an unauthorized caller gets ``not_found``,
never ``forbidden`` -- the existence of another project's master data is itself
sensitive.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: Same mapping as `governance_surface_api`. Kept as one constant so a future
#: change cannot land on one door and miss the other.
_MANAGE_ACTIONS = frozenset({"prepare_publish", "publish"})
_VALID_ACTIONS = frozenset({"apply_preset", "start_draft", "save_hierarchy", "publish"})


def _tool_error(code: str, message: str):
    import json  # noqa: PLC0415

    from fastmcp.exceptions import ToolError  # noqa: PLC0415

    return ToolError(json.dumps({"code": code, "message": message}))


def _identity() -> str:
    from fastmcp.server.dependencies import get_access_token  # noqa: PLC0415

    token = get_access_token()
    return token.claims.get("sub", token.client_id) if token else "anonymous"


def _minimum_capability(action: str) -> str:
    return "manage" if action in _MANAGE_ACTIONS else "edit"


def _envelope(text: str, structured: dict[str, Any]) -> dict[str, Any]:
    """Split the short model channel from the full canonical envelope (AD-1)."""
    return {"content": [{"type": "text", "text": text}], "structuredContent": structured}


def _run(
    project_id: str,
    action: str,
    payload: Mapping[str, Any],
    idempotency_key: str,
) -> dict[str, Any]:
    """Authorize, then hand over to the one command runner. No second scorer."""
    from core.country_workspace_commands import (  # noqa: PLC0415
        prepare_country_publish_confirmation,
        run_country_workspace_command,
    )
    from core.db import request_connection  # noqa: PLC0415
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415

    actor = _identity()
    if not actor or actor == "anonymous":
        raise _tool_error("not_found", "Resource not found.")
    if action != "prepare_publish" and action not in _VALID_ACTIONS:
        raise _tool_error(
            "invalid_action",
            "action must be apply_preset, start_draft, save_hierarchy, prepare_publish or publish",
        )
    if not str(idempotency_key or "").strip():
        raise _tool_error("missing_idempotency_key", "An idempotency key is required.")

    with request_connection(actor) as conn:
        decision = resolve_strict_resource_access(
            actor,
            conn,
            project_id=project_id,
            minimum_capability=_minimum_capability(action),
            hold_access=True,
        )
        if not decision.allowed or not decision.org_id:
            # Indistinguishable from absence, exactly like the REST door.
            raise _tool_error("not_found", "Resource not found.")
        org_id = str(decision.org_id)

        if action == "prepare_publish":
            result = prepare_country_publish_confirmation(
                conn,
                project_id=project_id,
                org_id=org_id,
                actor=actor,
                idempotency_key=idempotency_key,
                payload=dict(payload or {}),
            )
        else:
            result = run_country_workspace_command(
                conn,
                project_id=project_id,
                org_id=org_id,
                actor=actor,
                idempotency_key=idempotency_key,
                action=action,
                payload=dict(payload or {}),
            )
        conn.commit()
    return result


def refuse_manage_action(action: str) -> None:
    """The edit tool must not accept a publish.

    Module-level and not buried in the closure so it can be tested for what it
    is: without it, the ``manage``/``edit`` capability switch would be bypassable
    by handing ``publish`` to the tool declared for drafts -- an elevation an
    agent could reach on its own.
    """
    if action in _MANAGE_ACTIONS:
        raise _tool_error(
            "wrong_tool",
            "publish and prepare_publish have their own tools: they need a human confirmation.",
        )


def _summary(action: str, project_id: str, result: Any) -> str:
    version = result.get("version") if isinstance(result, dict) else None
    content_hash = version.get("content_hash") if isinstance(version, dict) else None
    suffix = f" -- content_hash {content_hash}" if content_hash else ""
    return f"{action} on {project_id}{suffix}"


def edit_country_master_data(
    project_id: str,
    action: str,
    payload: dict | None = None,
    idempotency_key: str = "",
):
    """Apply a preset, start a draft or save the Country hierarchy. Publishes nothing."""
    refuse_manage_action(action)
    result = _run(project_id, action, payload or {}, idempotency_key)
    return _envelope(_summary(action, project_id, result), result)


def prepare_country_master_data_publish(
    project_id: str,
    payload: dict | None = None,
    idempotency_key: str = "",
):
    """Freeze what a Country publish would do and mint its confirmation. Authorizes nothing."""
    result = _run(project_id, "prepare_publish", payload or {}, idempotency_key)
    return _envelope(_summary("prepare_publish", project_id, result), result)


def publish_country_master_data(
    project_id: str,
    payload: dict | None = None,
    idempotency_key: str = "",
):
    """Publish the Country hierarchy, consuming the confirmation minted above."""
    result = _run(project_id, "publish", payload or {}, idempotency_key)
    return _envelope(_summary("publish", project_id, result), result)


def register(mcp) -> None:
    """Register the master-data tools under the governance profile.

    THREE tools and not one, because the REST door distinguishes three acts and a
    single tool would have to declare one ``confirmation_mode`` for all of them.
    Declaring ``human`` on a draft save would be a lie in the catalogue; declaring
    ``none`` on a publish would be a worse one.
    """
    from core.mcp_profiles import register_profiled  # noqa: PLC0415

    register_profiled(
        mcp,
        edit_country_master_data,
        profile="governance",
        effect="confirmed_write",
        data_class="operational",
        # `host` and not `server`: the catalogue validator refuses a consequential
        # mutation without a trusted confirmation, and it is right to. An agent
        # rewriting a Project's Country hierarchy is exactly the act the AD-27
        # ceremony exists for -- the host asks before it runs. `human` is reserved
        # for the publish below, which additionally consumes a minted confirmation.
        confirmation_mode="host",
    )
    register_profiled(
        mcp,
        prepare_country_master_data_publish,
        profile="governance",
        effect="prepare",
        data_class="operational",
        confirmation_mode="none",
    )
    register_profiled(
        mcp,
        publish_country_master_data,
        profile="governance",
        effect="confirmed_write",
        data_class="sensitive",
        confirmation_mode="human",
    )
