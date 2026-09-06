"""Clause 19 of `mcp-tool-surface.md`, measured against `get_card` rather than assumed.

WHY THIS FILE EXISTS. The amendment of 2026-08-30 names `get_card` as "the
producer that mounts them" for the card feedback bars, "through
`_meta.ui.resourceUri`", and opens clause 19 on it: *a tool mounts a widget whose
feedback bar can append, and mints no handle into its result `_meta`*. The premise
is not true of the code it was written against, and a clause pointed at the wrong
producer sends the next reader to repair a tool that cannot carry the repair. So
the three facts are pinned here, each by a command rather than by a sentence:

  1. exactly ONE tool in the assembled catalog advertises a widget resource, and
     it is `render_analyze_result` -- not `get_card`;
  2. any other tool that tries to declare one is refused AT REGISTRATION, so
     `get_card` could not mount a widget even if this lot added the binding;
  3. `get_card`'s result `_meta` carries `answer` (and `gate`), and no `ui` key.

And the fourth, which is the half of the clause that WAS repairable here: the two
feedback bars now speak the argument names `submit_feedback` declares. They sent
`module`, which the tool's schema does not declare and refuses
(`additionalProperties: false`) -- so every card and widget rating was rejected at
the boundary, before the handle rule was ever reached. The guard below reads the
bars' own call site and intersects it with the LIVE tool schema, so the pair
cannot drift apart again in either direction.

ASCII-only, English copy.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from core.main import mcp
from core.mcp_profiles import (
    RENDER_TOOL_NAME,
    CatalogValidationError,
    assert_data_render_split,
)

REPO_ROOT = Path(__file__).resolve().parents[3]

#: The two bars that can append a rating from a mounted widget.
FEEDBACK_BARS = (
    REPO_ROOT / "ui" / "shell" / "src" / "FeedbackBar.tsx",
    REPO_ROOT / "ui" / "cards" / "shell" / "src" / "CardFeedbackBar.tsx",
)


# ---------------------------------------------------------------------------
# 1-2. `get_card` mounts no widget, and cannot.
# ---------------------------------------------------------------------------


def test_only_the_render_tool_advertises_a_widget_resource():
    """Driven off the LIVE registry, never a hand-written list of names."""
    bound = assert_data_render_split(mcp)
    assert bound == {RENDER_TOOL_NAME: "ui://core/visualization-runtime"}
    assert "get_card" not in bound
    assert "list_card_templates" not in bound


def test_a_card_tool_that_declared_a_widget_would_be_refused_at_registration():
    """The reason clause 19 cannot be closed by editing `cards_mcp.py` alone.

    `_record_app_declaration` raises for any tool but the render tool that carries
    an `AppConfig(resource_uri=...)`. So `get_card` cannot become the producer the
    clause asks for: the data/render split of Story 50.6 forbids it, and that split
    is ratified in `visualization-and-rendering.md`.
    """
    from core.mcp_profiles import register_profiled
    from fastmcp import FastMCP
    from fastmcp.apps import AppConfig

    scratch = FastMCP("clause-19-probe")

    def get_card(project_id: str = "") -> str:
        return ""

    with pytest.raises(CatalogValidationError) as exc_info:
        register_profiled(
            scratch,
            get_card,
            profile="insights",
            effect="read",
            data_class="operational",
            confirmation_mode="none",
            app=AppConfig(visibility=["app"], resource_uri="ui://core/card-kpi"),
        )
    assert "data/render tool split" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 3. The result `_meta` of a real `get_card` call.
# ---------------------------------------------------------------------------


def test_get_card_result_meta_has_no_ui_binding(monkeypatch):
    """Call the tool body and read the `_meta` it actually returns.

    Not a source assertion: a source assertion would go green the day another
    module started decorating this result.
    """
    from core import cards as core_cards
    from core import cards_mcp
    from core import main as core_main

    envelope: dict[str, Any] = {
        "schema_version": 1,
        "meta": {"project_id": "proj_EXAMPLE"},
        "data": {"card_id": "kpi", "composition": [], "date_range": {"end": "2026-08-30"}},
    }

    monkeypatch.setattr(core_main, "get_access_token", lambda: None)
    monkeypatch.setattr(core_main, "_resolve_project", lambda pid, identity: pid)
    monkeypatch.setattr(core_main, "_fetch_context_events", lambda *a, **k: [])
    # `get_card` imports the gate from `core.reporting_mcp` (its defining module)
    # since 2026-09-01, so that is the address the stub must land on.
    from core import reporting_mcp as core_reporting_mcp

    monkeypatch.setattr(
        core_reporting_mcp, "_apply_pre_query_gate", lambda summary, *a, **k: (summary, None)
    )
    monkeypatch.setattr(cards_mcp, "refuse_unless_project_scope", lambda *a, **k: None)
    monkeypatch.setattr(
        core_cards,
        "get_card",
        lambda *a, **k: ("A summary line.", envelope, "ui://core/card-kpi"),
    )

    result = cards_mcp.get_card(project_id="proj_EXAMPLE")
    meta = result.meta or {}
    assert "ui" not in meta, "get_card must not mount a widget (data/render split)"
    assert set(meta) <= {"answer", "gate"}
    # The bundle is NAMED, not mounted -- that distinction is the whole clause.
    assert meta["answer"]["visual"]["widget_uri"] == "ui://core/card-kpi"


# ---------------------------------------------------------------------------
# 4. The bars speak the tool's argument names.
# ---------------------------------------------------------------------------


def _submitted_argument_names(source: str) -> set[str]:
    """Top-level keys of the object literal the bar sends to `submit_feedback`."""
    start = source.index('callServerTool("submit_feedback", {')
    body = source[start + len('callServerTool("submit_feedback", {') :]
    depth = 0
    literal: list[str] = []
    for char in body:
        if char == "}" and depth == 0:
            break
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        literal.append(char)
    return set(re.findall(r"^\s{8}([a-z_]+):", "".join(literal), flags=re.MULTILINE))


@pytest.mark.anyio
async def test_the_feedback_bars_send_only_arguments_the_tool_declares():
    """The guard that would have caught `module`, for BOTH bars at once.

    `submit_feedback` declares `additionalProperties: false`, so a key the schema
    does not know is a refused call, not a spare field. The bars had been sending
    `module` since the parameter was renamed `connector`; nothing measured the
    pair, so the rating path was dead long before the handle rule closed it.
    """
    tools = await mcp._list_tools()
    tool = next(t for t in tools if getattr(t, "name", None) == "submit_feedback")
    schema = tool.parameters or {}
    declared = set((schema.get("properties") or {}).keys())
    assert schema.get("additionalProperties") is False
    assert "handle" in declared, "the server-minted handle is the append's condition"
    assert "module" not in declared

    for bar in FEEDBACK_BARS:
        sent = _submitted_argument_names(bar.read_text(encoding="utf-8"))
        assert sent, f"no submit_feedback call site found in {bar.name}"
        assert sent <= declared, f"{bar.name} sends arguments the tool refuses: {sent - declared}"
        assert "handle" in sent, f"{bar.name} must forward the server-minted handle"
