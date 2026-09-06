"""The four Tax-specific MCP tools may not come back (Story 48.4 AC10).

Why a guard and not a note. They were built once, as a transitional surface, and
the 41.6 cutover then removed their module from `main.py` — so they sat for days
as 621 lines that served nobody while the ratified target's fourth MCP verb,
`auto-propose`, was uncovered. Anyone reading that state sees dead code and either
deletes the coverage with it or re-registers the tools to "fix" it. Both are
wrong, and prose in a docstring stops neither.

What the target actually asks, in `capabilities/tax-fees.md`'s Required view
coverage table: MCP must serve "Inspect, auto-propose, prepare and human-confirm
rule changes". Story 48.4's AC10 says WHICH surface serves it — the generic
capability commands plus the generic Rule Set commands — and adds that "the four
planned Tax-specific CRUD tools and custom Tax App are not created". Story 41.8's
canonical correction of 2026-08-01 says the same in one word: not built, and not
kept.

So this file asserts both halves, because either alone is a trap:

  1. no Tax-specific MCP tool is registered anywhere;
  2. the four verbs the target requires ARE served — three by the generic tools,
     and auto-propose by the bounded block on the generic capability read.

Offline. It reads source, not a running server: a test that needed a live MCP
session would skip in exactly the situation it exists to catch.
"""

from __future__ import annotations

import ast
from pathlib import Path

_CORE = Path(__file__).resolve().parents[2] / "core"

#: The exact four. Named rather than pattern-matched, so a rename is a deliberate
#: edit to this list and not a silent escape.
_FORBIDDEN_TOOLS = frozenset(
    {
        "get_fee_tax_rules",
        "auto_populate_tax_rules",
        "add_fee_tax_rule",
        "update_fee_tax_rule",
    }
)


def _registered_tool_names(path: Path) -> set[str]:
    """Every function handed to `register_profiled` in one module.

    Reads the AST rather than the text: `register_profiled(mcp, handler, ...)`
    passes the function by NAME, so the call site is what registers a tool, and a
    function merely defined is not one.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if not (isinstance(target, ast.Name) and target.id == "register_profiled"):
            continue
        for argument in node.args:
            if isinstance(argument, ast.Name):
                names.add(argument.id)
        # `for handler in (a, b): register_profiled(mcp, handler, ...)` hides the
        # names in the loop, so the tuple is read separately below.
    for node in ast.walk(tree):
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            body_registers = any(
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == "register_profiled"
                for inner in ast.walk(node)
            )
            if body_registers:
                names.update(
                    element.id
                    for element in node.iter.elts
                    if isinstance(element, ast.Name)
                )
    return names


def test_no_tax_specific_mcp_tool_is_registered_anywhere():
    offenders: dict[str, set[str]] = {}
    for path in sorted(_CORE.glob("*.py")):
        found = _registered_tool_names(path) & _FORBIDDEN_TOOLS
        if found:
            offenders[path.name] = found
    assert not offenders, (
        f"Tax-specific MCP tools are registered again: {offenders}. "
        "AC10 of Story 48.4 and the canonical correction of Story 41.8 both say the "
        "generic capability and Rule Set commands serve Tax & Fees. If a verb is "
        "genuinely missing, add it to the GENERIC surface -- a second tool family is "
        "a second place to ask one question."
    )


def test_fee_tax_mcp_registers_nothing_at_all():
    """The module that held them keeps `ladder_summary` and nothing tool-shaped."""
    module = _CORE / "fee_tax_mcp.py"
    tree = ast.parse(module.read_text(encoding="utf-8"))
    top_level = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
    assert "register" not in top_level, (
        "fee_tax_mcp.register() is back. This module is a read helper for the "
        "generic surface; it is not an MCP surface."
    )
    assert "ladder_summary" in top_level, (
        "ladder_summary is the one thing this module still owes the tree -- "
        "project_capabilities_mcp calls it. Removing it breaks the generic read."
    )


def test_the_generic_surface_still_serves_all_four_required_verbs():
    """The other half, and the one that makes the removal safe rather than lossy.

    `capabilities/tax-fees.md` requires inspect / auto-propose / prepare /
    human-confirm. Asserting only "the bespoke tools are gone" would pass just as
    well on a tree where the capability had been deleted outright.
    """
    module = _CORE / "project_capabilities_mcp.py"
    source = module.read_text(encoding="utf-8")
    registered = _registered_tool_names(module)

    for verb, tool in (
        ("inspect", "read_project_capability"),
        ("prepare", "prepare_project_capability_change"),
        ("human-confirm", "confirm_project_capability_change"),
    ):
        assert tool in registered, f"the generic surface no longer serves {verb}"

    # auto-propose is not a tool but a bounded block on the read, which is what
    # AC10 asks for ("no new tool family or authority is introduced").
    assert 'capability_key == "tax_fees"' in source, (
        "the generic capability read no longer carries the Tax & Fee proposals, so "
        "the `auto-propose` verb of capabilities/tax-fees.md is uncovered -- the "
        "exact silent gap this whole removal was sequenced to avoid."
    )
    assert "_tax_fee_preset_proposals" in source
