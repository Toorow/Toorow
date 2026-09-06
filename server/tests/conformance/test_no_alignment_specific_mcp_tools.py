"""Analytics Alignment gets NO tool family of its own (story 71.4).

Why a guard and not a note. `capabilities/analytics-alignment.md` asks MCP for
three verbs -- *"read the alignment"*, *"prepare and human-confirm an
arbitration, never write one silently"*, and *"read a split and the volume it
rode on"* -- and every one of them reads or decides ONE Project capability. The
header of `core/fee_tax_mcp.py` records, at length, what happened the last time a
capability answered such a list with a module of its own: four bespoke tools, a
cutover that unregistered them, and one required verb silently uncovered for days
inside 621 lines that looked like dead weight.

So 71.4 landed the three verbs on the GENERIC surface -- a bounded block on
`read_project_capability`, and a `*_row_decision` prepare/confirm pair that takes
a `capability_key` and dispatches -- and this file asserts BOTH halves, because
either alone is a trap:

  1. no alignment-specific MCP tool is registered anywhere;
  2. the three verbs the target requires ARE served, and by the generic tools.

Offline. It reads source, not a running server: a test that needed a live MCP
session would skip in exactly the situation it exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

# The one AST reader, imported rather than copied: a second parser of "what does
# this module register" is a second answer to the question, and the two would come
# to disagree the day `register_profiled` grows a call shape.
from tests.conformance.test_no_tax_specific_mcp_tools import _registered_tool_names

_CORE = Path(__file__).resolve().parents[2] / "core"

#: Named rather than pattern-matched, so re-introducing one is a deliberate edit
#: to this list and not a silent escape. Every name below is a verb the generic
#: surface already serves.
_FORBIDDEN_TOOLS = frozenset(
    {
        "read_analytics_alignment",
        "read_alignment_pairs",
        "arbitrate_alignment_row",
        "accept_alignment_row",
        "prepare_alignment_arbitration",
        "confirm_alignment_arbitration",
        "ventilate_alignment",
        "read_alignment_ventilation",
        "read_measurement_grain",
        "list_measurement_grains",
        "declare_measurement_grain",
    }
)

#: The two tool names story 71.4 DID introduce. Both are generic in shape -- they
#: take a `capability_key` -- and neither carries a capability's name.
_INTRODUCED_TOOLS = (
    "prepare_project_capability_row_decision",
    "confirm_project_capability_row_decision",
)


def test_no_alignment_specific_mcp_tool_is_registered_anywhere():
    offenders: dict[str, set[str]] = {}
    for path in sorted(_CORE.glob("*.py")):
        found = _registered_tool_names(path) & _FORBIDDEN_TOOLS
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"Analytics-Alignment-specific MCP tools are registered: {offenders}. "
        "The generic capability surface serves all three verbs the ratified card asks "
        "of MCP. If one is genuinely missing, add it THERE -- a second tool family is "
        "a second place to ask one question, and `core/fee_tax_mcp.py`'s header is the "
        "record of what that costs."
    )


def test_the_two_new_tools_are_generic_in_shape_not_capability_named():
    """A tool whose NAME carries a capability is a tool family by another route."""
    module = _CORE / "project_capabilities_mcp.py"
    registered = _registered_tool_names(module)
    for tool in _INTRODUCED_TOOLS:
        assert tool in registered, f"{tool} is no longer registered"
    for tool in registered:
        assert "alignment" not in tool, (
            f"{tool} names a capability on the generic surface. The dispatch is a "
            "`capability_key` argument, never a tool name."
        )


def test_the_row_decision_pair_dispatches_on_a_capability_key():
    source = (_CORE / "project_capabilities_mcp.py").read_text(encoding="utf-8")
    assert "_ROW_DECISION_CAPABILITIES" in source, (
        "the row-decision pair no longer dispatches on a declared set of capability "
        "keys, so it is capability-specific in everything but its name"
    )


def test_analytics_alignment_read_registers_nothing_at_all():
    """It is a read helper for the generic surface, exactly as `fee_tax_mcp` is."""
    module = _CORE / "analytics_alignment_read.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    top_level = {node.name for node in tree.body if isinstance(node, ast.FunctionDef)}
    assert "register" not in top_level, (
        "analytics_alignment_read.register() exists. This module composes the cascade, "
        "the ventilation and the grain for the generic read; it is not an MCP surface."
    )
    assert {"read_alignment", "sanction_breakdown", "prepare_row_decision"} <= top_level


def test_the_generic_surface_still_serves_the_three_required_verbs():
    """The other half, and the one that makes the absence of a tool family safe.

    Asserting only "no bespoke tool exists" would pass just as well on a tree where
    the capability had never been wired to MCP at all -- which is precisely the state
    `capabilities/analytics-alignment.md` recorded as *Not delivered* before 71.4.
    """
    module = _CORE / "project_capabilities_mcp.py"
    source = module.read_text(encoding="utf-8")
    registered = _registered_tool_names(module)

    assert "read_project_capability" in registered
    for tool in _INTRODUCED_TOOLS:
        assert tool in registered

    # "Read the alignment" and "read a split and the volume it rode on" are one
    # bounded block on the generic read, not two tools.
    assert 'capability_key == "analytics_alignment"' in source, (
        "the generic capability read no longer carries the Analytics Alignment block, "
        "so the two READ verbs of capabilities/analytics-alignment.md are uncovered"
    )
    assert "read_alignment" in source


def test_the_grain_sanction_is_reachable_from_the_read():
    """Epic 71's first caller: the read must go through the grain, not around it."""
    source = (_CORE / "analytics_alignment_read.py").read_text(encoding="utf-8")
    assert "list_measurement_grains" in source
    assert "derived_coverage" in source
    assert "breakdown_dimension_not_in_grain" in source, (
        "the refusal `governance.md` § Amendment 2026-08-27 names is gone, so a "
        "breakdown the MDM never sanctioned can be served again"
    )
