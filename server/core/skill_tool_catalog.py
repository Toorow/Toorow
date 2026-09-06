"""Read-only projection of the live MCP tool catalog for governed skill authoring.

The Context API must not import ``core.main`` (that would create an import cycle),
so the assembled FastMCP app injects its ``list_tools`` callable after every tool
has been registered. The projection contains declarations only; it never executes
a tool and never exposes credentials or provider data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

ToolProvider = Callable[[], Awaitable[Sequence[Any]]]

_provider: ToolProvider | None = None


def configure_skill_tool_provider(provider: ToolProvider) -> None:
    """Configure the live FastMCP catalog provider during application assembly."""
    global _provider
    _provider = provider


def reset_skill_tool_provider_for_tests() -> None:
    """Clear the process-local provider between isolated tests."""
    global _provider
    _provider = None


def _declared_value(tool: Any, key: str, default: str) -> str:
    meta = getattr(tool, "meta", None)
    if isinstance(meta, dict):
        value = meta.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    prefix = f"{key}:"
    for tag in getattr(tool, "tags", None) or ():
        if isinstance(tag, str) and tag.startswith(prefix):
            value = tag.split(":", 1)[1].strip()
            if value:
                return value
    return default


def _is_app_only(tool: Any) -> bool:
    """True for a tool that exists for a mounted widget, not for a model.

    Story 50.6: an app-only read tool returns bounded slices of an immutable
    Result. It is NOT an analytical capability, and a governed Skill must not be
    authorable against it -- a Skill calling a slice reader as if it were an
    analytical tool is precisely the model-callable app tool
    `visualization-and-rendering.md` ("Tool split") forbids.

    67.7: this module owned a private copy of the predicate while discovery owned
    none, so the Skill catalog excluded the five app-only tools and `tools/list`
    still offered them. One predicate now, in `core.mcp_profiles`, read by every
    surface that asks the question.
    """
    from core.mcp_profiles import is_app_only_tool  # noqa: PLC0415

    return is_app_only_tool(tool)


def _project(tool: Any) -> dict[str, str] | None:
    name = getattr(tool, "name", None)
    if not isinstance(name, str) or not name.strip():
        return None
    if _is_app_only(tool):
        return None
    description = getattr(tool, "description", "")
    if not isinstance(description, str):
        description = ""
    return {
        "name": name.strip(),
        "description": description.strip()[:500],
        # Epic 36 deliberately treats undeclared legacy tools as safe Insights
        # reads. Mirror that catalog contract rather than inventing a new class.
        "profile": _declared_value(tool, "profile", "insights"),
        "effect": _declared_value(tool, "effect", "read"),
        "data_class": _declared_value(tool, "data_class", "operational"),
        "confirmation_mode": _declared_value(tool, "confirmation_mode", "none"),
    }


async def list_skill_tool_catalog() -> dict[str, Any]:
    """Return a deterministic, bounded projection of the assembled tool catalog."""
    if _provider is None:
        raise RuntimeError("The MCP tool catalog is not available.")
    tools = []
    for raw_tool in await _provider():
        projected = _project(raw_tool)
        if projected is not None:
            tools.append(projected)
    tools.sort(key=lambda item: item["name"])
    encoded = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return {
        "catalog_version": hashlib.sha256(encoded).hexdigest(),
        "tools": tools,
    }
