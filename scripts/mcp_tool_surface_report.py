#!/usr/bin/env python
"""Measure the MCP wire catalog and model Skill projection independently.

WHY THIS SCRIPT EXISTS. The catalog was measured once, by hand, against a live
`tools/list` on Cloud Run -- 92 tools, ~11 300 tokens of names and descriptions,
39 of them carrying a provider's name. A number obtained that way cannot be
re-obtained next week by someone else, so the claim "the surface is bounded"
had no instrument behind it and the surface grew by one tool per connector.

WHAT IT MEASURES, AND WHY NOT `mcp._list_tools()` ALONE. `_list_tools()` returns
the ASSEMBLED catalog. The MCP wire applies the profile/presence filter plus a
small named host-routing allowlist. Generated Skills apply a second projection
that excludes every app-only tool and bounds declarations. These are different
surfaces and this script reports both rather than labelling one as the other.

The default persona is the one every host starts as: authenticated or not, with
no endpoint/workspace evidence -- `insights` only, no interactive presence. That
is the population whose context window the catalog spends.

TOKENS. There is no tokenizer in this environment and adding one to read a
docstring would be a dependency for an estimate. The estimate is bytes/4, stated
as an estimate, and the exact byte count is printed beside it so the reproducible
number is the one that cannot drift with a tokenizer version.

AND THE SPLIT INSIDE `insights` (2026-08-30). Clause 8 of `mcp-tool-surface.md`
says a tool that mutates must not be reachable under `insights`. Counting tools
never showed that: two tools declared `effect="read"` appended a row for months,
and the number 43 was the same either way. So the report now also derives, from
the source of `server/core/*.py`, WHICH `insights` tools append and what stops a
caller who merely knows the name -- a server-minted handle, another named
governor, or nothing. `tests/core/test_mcp_tool_surface.py` imports the same
function rather than deriving a second copy of the rule.

Usage:
    python scripts/mcp_tool_surface_report.py            # the three numbers
    python scripts/mcp_tool_surface_report.py --json     # machine-readable
    python scripts/mcp_tool_surface_report.py --verbose  # + every tool, per line
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import functools
import json
import re
import sys
from collections import Counter
from pathlib import Path

_SERVER = Path(__file__).resolve().parent.parent / "server"
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

#: Bytes per token. A stated estimator, not a measurement -- see the module
#: docstring. Every token figure this script prints is `bytes // _BYTES_PER_TOKEN`.
_BYTES_PER_TOKEN = 4

#: THE GROWTH BOUND (audit 2026-08-20, C14). AD-42 bounds the CONNECTOR catalog
#: (zero provider-named tools); nothing bounded the CORE catalog, and it grew
#: from 98 assembled / 43 wire to 113 / 48 in eight days without anyone having
#: to say why. These budgets are the ratchet: `--gate` fails when one is
#: exceeded, and raising one means editing this constant WITH the reason in the
#: commit message -- a deliberate budget, never a silent drift.
BUDGETS = {
    # 130 -> 131 on 2026-09-02: `compose_dossier` (epic 74, story 74-1), by Jean's
    # catalogue decision written in `docs/product-architecture/mcp-tool-surface.md`
    # (note of 2026-09-02) -- a write cannot ride a parameter of a read.
    # 2026-09-05, three decisions in a row, each written in `mcp-tool-surface.md`
    # before the code: 134 -> 137 the Test DEFINITION door (list_golden_questions
    # insights, publish_golden_question_version + set_golden_question_lifecycle
    # governance); 137 -> 139 epic 75 (propose_calculated_field, get_exemplars);
    # 139 -> 142 the Regression Run VERBS (open_evaluation_run and
    # advance_evaluation_run operations, decide_evaluation_run governance);
    # 142 -> 144 the Test REVIEW door (read_feedback insights, review_feedback
    # operations) -- with it the three Test rows the gap audit measured as
    # console-only are closed, and the MCP plane of `user-bridge.md` 5 goes
    # from 8 green on 18 to 18 green on 26; 144 -> 145 the Evidence door
    # (walk_evidence_chain, insights) -- << a model cannot re-walk the chain it
    # is asked to trust >>, the sharpest verdict of the ten ; 145 -> 147 la porte
    # d ACCES (read_project_access insights, grant_project_access governance) --
    # << none. No registered tool names access, a grant or a handoff >>, la ligne
    # la plus nue des dix.
    "assembled_tools": 147,
    # 137 -> 139 on 2026-09-05 (epic 75): `propose_calculated_field` (operations, confirmed_write --
    # the agent proposes in the SAME review rail as a human, 75-1) and `get_exemplars`
    # (insights, read -- approved exemplars pinned and budgeted, 75-3) -- mcp-tool-surface.md,
    # note of 2026-09-05 "the semantic layer learns by use".
    "wire_tools": 60,
    "wire_catalog_bytes": 45_000,
    "model_skill_catalog_bytes": 26_000,
}


# ---------------------------------------------------------------------------
# THE SPLIT INSIDE `insights` -- which of those tools APPEND, and what stops a
# caller who merely knows the name (`mcp-tool-surface.md`, "Incomplete if" n. 8).
#
# Derived from the source, never from a list: the two tools this clause was
# opened for were found by reading, and a hand-written list would have aged the
# way the plan of epic 53 aged (four tools named, nine measured five days later).
# ---------------------------------------------------------------------------

_CORE = _SERVER / "core"

#: The bound on how far a call chain is followed. THREE, because that is the
#: longest chain measured to a real append (`search_context` ->
#: `context_search.search_context` -> `search_context_walk` -> the INSERT). A
#: complete closure makes every tool an appender: at depth 4 a pure read reached
#: `app.*` writes through `list_entity_types`, and the census said so. The
#: resolver repair of the second pass (2026-08-30) did not move it: every one of
#: the appenders it newly surfaced is reached within the same three hops.
_DEPTH = 3

#: `effect` classifies what a tool does to DOMAIN state; AD-28 separately REQUIRES
#: every read to leave an append-only audit event. Counting that audit row as an
#: append would make AD-28 unimplementable for every read in the catalog, so the
#: audit writer is not a write here. It is the ONLY such exclusion.
_AUDIT_WRITERS = frozenset({"write_audit_row", "declare_action"})

#: What consuming a server-minted grant looks like in a call chain. `touch_handle`
#: is the one function allowed to move a grant row (`core.result_app_grants`), and
#: `consume_observation_handle` is the observation writers' seam onto it.
_HANDLE_CONSUMERS = frozenset({"touch_handle", "consume_observation_handle"})

_WRITE_SQL = re.compile(
    r"(?:insert\s+into|update|delete\s+from)\s+(app\.[a-z_]+)", re.IGNORECASE
)


def _core_trees() -> dict:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for path in sorted(_CORE.glob("*.py"))
    }


def _functions(tree):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _tables_written(node) -> set:
    tables: set = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            flat = " ".join(child.value.split())
            tables |= {t.lower() for t in _WRITE_SQL.findall(flat)}
    return tables


def _bindings(module: str, tree, trees: dict) -> tuple:
    """What every NAME in this module's body actually points at, inside `core/`.

    TWO maps, because Python binds two different things and the first census read
    only one of them:

      * `symbols` -- a `def` in this module, or `from core.x import fn`: the local
        name is a FUNCTION, and it is the one written in `x.py`, not whichever
        file happens to spell it the same way.
      * `modules` -- `from core import x`, `import core.x as y`: the local name is
        a MODULE, so `x.fn()` is `fn` OF `x.py`.

    **The second map is the repair (2026-08-30, second pass).** Before it an
    attribute call was looked up as a bare spelling in a flat vocabulary of
    imported NAMES; a module binding puts no name there, so the edge was silently
    dropped. `core/evidence_inspection_mcp.py:104` does `from core import
    evidence_inspections` and `:144` calls
    `evidence_inspections.insert_inspection(...)`, whose `INSERT INTO
    app.evidence_inspections` the census never reached -- it credited
    `app_record_evidence_inspection` with the grant table alone, and would have
    missed the tool entirely had that second write not existed.
    `core/reporting_mcp.py:143` hides the same shape
    (`_adherence.record_data_query` -> `app.query_adherence`). Eleven appending
    `insights` tools were invisible for this one reason.

    Resolution stays EXACT in both maps: a name that resolves to no `core/*.py`
    function is DROPPED, never matched against a same-spelled function elsewhere.
    That bound is what keeps the census a measurement -- with global matching,
    `.get(...)` and `cur.execute(...)` land on whatever `core` function shares the
    spelling and the answer becomes "most of the catalog appends".

    Imports are read wherever they sit, function-local ones included, because
    `core/` imports inside bodies to break cycles. A binding is therefore
    module-wide here even where Python scopes it to one function: the same
    approximation the vocabulary made, and it only ever widens the closure of the
    module that wrote the import.
    """
    symbols: dict = {}
    modules: dict = {}

    for node in _functions(tree):
        symbols.setdefault(node.name, set()).add((module, node.name))

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `source is None` means the imported names are MODULES of `core`.
            if node.level == 0 and node.module == "core":
                source = None
            elif node.level == 0 and (node.module or "").startswith("core."):
                source = node.module[len("core.") :] + ".py"
            elif node.level == 1 and not node.module:
                source = None
            elif node.level == 1 and node.module:
                source = node.module + ".py"
            else:
                continue
            for alias in node.names:
                local = alias.asname or alias.name
                if source is None:
                    if alias.name + ".py" in trees:
                        modules[local] = alias.name + ".py"
                elif source in trees:
                    symbols.setdefault(local, set()).add((source, alias.name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if not alias.name.startswith("core."):
                    continue
                target = alias.name[len("core.") :] + ".py"
                if alias.asname and target in trees:
                    modules[alias.asname] = target
    return symbols, modules


def _called(node, module: str, index: dict) -> set:
    """The `core/` functions REALLY called in a body, as qualified names.

    A qualified name is the file plus the function --
    `("evidence_inspections.py", "insert_inspection")` -- because a bare spelling
    is exactly what let a callee resolve to a stranger that shared it.
    """
    symbols, modules = index["binds"][module]
    out: set = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if isinstance(func, ast.Name):
            out |= symbols.get(func.id, set())
            continue
        if not isinstance(func, ast.Attribute):
            continue
        owner, source = func.value, None
        if isinstance(owner, ast.Name):
            source = modules.get(owner.id)
        elif (
            isinstance(owner, ast.Attribute)
            and isinstance(owner.value, ast.Name)
            and owner.value.id == "core"
        ):
            # `import core.x` at the top, then `core.x.fn()` in the body.
            source = owner.attr + ".py"
        if source is not None:
            out.add((source, func.attr))
            continue
        # Not a module: `self.write(...)`, `conn.execute(...)`, `decision.allow()`.
        # The attribute may still be a function this module imported BY VALUE and
        # reached through an object; that is the vocabulary bound, unchanged.
        out |= symbols.get(func.attr, set())
    return {key for key in out if key in index["defs"]}


@functools.cache
def _core_index() -> dict:
    """`core/` read once: its functions, its bindings, its resolved call graph.

    Resolved ONCE rather than inside the closure loop: 524 modules walked three
    times per closure, twice per census, is the difference between an instrument
    someone runs and one they stop running.
    """
    trees = _core_trees()
    defs: dict = {}
    for module, tree in trees.items():
        for node in _functions(tree):
            defs.setdefault((module, node.name), []).append(node)
    index = {
        "trees": trees,
        "defs": defs,
        "binds": {
            module: _bindings(module, tree, trees) for module, tree in trees.items()
        },
    }
    index["calls"] = {
        key: set().union(*(_called(node, key[0], index) for node in nodes))
        for key, nodes in defs.items()
    }
    index["tables"] = {
        key: set().union(*(_tables_written(node) for node in nodes))
        for key, nodes in defs.items()
    }
    return index


def _registered_tool_names(tree) -> set:
    """The three registration forms, wherever they sit in the module.

    The same reading `tests/conformance/test_mcp_tools_resolve_project_scope.py`
    settled on, loop registrations included -- eight real tools were invisible to
    that guard until the `for handler in (...)` form was read unconditionally.
    """
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                target = decorator.func if isinstance(decorator, ast.Call) else decorator
                if isinstance(target, ast.Attribute) and target.attr == "tool":
                    names.add(node.name)
        if isinstance(node, ast.Call):
            func, arg = node.func, None
            if isinstance(func, ast.Attribute) and func.attr == "tool" and node.args:
                arg = node.args[0]
            elif (
                isinstance(func, ast.Name)
                and func.id == "register_profiled"
                and len(node.args) >= 2
            ):
                arg = node.args[1]
            if isinstance(arg, ast.Name):
                names.add(arg.id)
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            registers = any(
                isinstance(inner, ast.Call)
                and (
                    (isinstance(inner.func, ast.Name) and inner.func.id == "register_profiled")
                    or (isinstance(inner.func, ast.Attribute) and inner.func.attr == "tool")
                )
                for inner in ast.walk(node)
            )
            if registers:
                names.update(e.id for e in node.iter.elts if isinstance(e, ast.Name))
    return names


def _closure(index: dict, seeds: dict) -> dict:
    """Grow *seeds* along resolved calls, `_DEPTH` times, over the functions of `core/`."""
    reached = dict(seeds)
    calls = index["calls"]
    for _ in range(_DEPTH):
        grown = dict(reached)
        for key, callees in calls.items():
            if key[1] in _AUDIT_WRITERS:
                continue
            acquired: set = set()
            for callee in callees & reached.keys():
                acquired |= reached[callee]
            if acquired:
                grown[key] = grown.get(key, set()) | acquired
        if grown.keys() == reached.keys():
            return grown
        reached = grown
    return reached


def _reachable(index: dict, seeds: set) -> set:
    """Functions reachable from *seeds* within `_DEPTH` calls, over `core/`.

    Answers "does this tool reach that governor at all", which a direct-callee
    list cannot: every app-only tool of `analyze_render_mcp` delegates to a
    private adapter, so the guard always sits one hop below the registered name.
    """
    reached = set(seeds)
    frontier = set(seeds)
    calls = index["calls"]
    for _ in range(_DEPTH):
        nxt: set = set()
        for key in frontier:
            nxt |= calls.get(key, set())
        nxt -= reached
        if not nxt:
            break
        reached |= nxt
        frontier = nxt
    return reached


def insights_append_census(declared: dict) -> list:
    """Every `insights` tool that appends, with its tables and its handle verdict.

    *declared* maps a tool name to its `ToolDeclaration` -- the in-process
    registry, so a tool that stops being declared `insights` leaves this census
    on its own rather than being removed from a list by hand.
    """
    index = _core_index()
    defs, calls = index["defs"], index["calls"]

    write_seeds = {
        key: tables
        for key, tables in index["tables"].items()
        if tables and key[1] not in _AUDIT_WRITERS
    }
    writers = _closure(index, write_seeds)
    consumers = _closure(
        index, {key: {key} for key in defs if key[1] in _HANDLE_CONSUMERS}
    )

    census: list = []
    for module, tree in sorted(index["trees"].items()):
        for tool in sorted(_registered_tool_names(tree)):
            decl = declared.get(tool)
            if decl is None or decl.profile != "insights":
                continue
            key = (module, tool)
            if key not in defs:
                continue
            callees = calls[key]
            tables = set(index["tables"][key])
            for callee in callees & writers.keys():
                tables |= writers[callee]
            if not tables:
                continue
            signature = {
                arg.arg
                for node in defs[key]
                for arg in node.args.args + node.args.kwonlyargs
            }
            census.append(
                {
                    "tool": tool,
                    "module": module,
                    "effect": decl.effect,
                    "appends_to": sorted(tables),
                    "takes_a_handle": "handle" in signature,
                    "consumes_a_handle": bool(callees & consumers.keys()),
                    "calls": sorted({name for _file, name in callees}),
                    "reaches": sorted(
                        {name for _file, name in _reachable(index, callees)}
                    ),
                }
            )
    return census


#: THE PER-CALL TOKEN CEREMONY, played whole by one function.
#: `datastream_activation.execute_confirmed_operation` CONSUMES an
#: `app.entry_confirmations` row (its reference and its secret) and runs the
#: mutation, the audit row and the outbox inside that same outer transaction.
#:
#: IT IS NOT THE DEFINITION OF `confirmation_mode="human"`, and reading it as one
#: is the mistake this census exists to make measurable: the AD-27 proof of
#: interactive presence is a COLUMN of the capability context (migration 283),
#: refused centrally at `mcp_profiles.py#on_call_tool` for every `confirmed_write`
#: before any body runs. This is the SECOND ceremony, owed only where a surface
#: document demands a two-step act.
_TWO_STEP_ENTRY = frozenset({"execute_confirmed_operation"})

#: EVERY WAY A BODY CONSUMES A SINGLE-USE CONFIRMATION, not only the wrapper
#: above -- because "39 tools consume nothing" is exactly the reassuring, smaller
#: world an under-seeded instrument reports. Each name was read at its own
#: definition before it was written here; all four say so in their first line.
_CONFIRMATION_CONSUMERS = {
    "execute_confirmed_operation": (
        "consumes an `app.entry_confirmations` row and runs the mutation, the "
        "audit row and the outbox in the same outer transaction"
    ),
    "consume_entry_confirmation": (
        "`entry_confirmations.py`: 'Atomically consume a confirmation or replay "
        "its one linked operation' -- the primitive the wrapper above calls, and "
        "reached directly by the country publication door"
    ),
    "consume_presence_bound_confirmation": (
        "`entry_confirmations.py`: the same consumption for a confirmation whose "
        "trusted presence binding was already resolved"
    ),
    "confirm_change_set": (
        "`semantic_model.py`: 'Consume the confirmation once and commit, or "
        "refuse with the exact reason' -- the Semantic Model token that "
        "`mcp-tool-surface.md` (amendment of 2026-08-25) says is minted and "
        "consumed inside one server-side call"
    ),
}


def confirmed_write_ceremony_census(declared: dict) -> list:
    """Every registered `confirmed_write` of `core/`, and whether it plays the two-step.

    *declared* maps a tool name to its `ToolDeclaration` -- the in-process registry,
    so a tool that stops declaring `confirmed_write` leaves this census on its own.

    Same resolver as `insights_append_census`, deliberately: exact module-and-symbol
    resolution over `core/*.py`, `_DEPTH` hops from the tool's direct callees. The
    first census reported a smaller world when its resolver dropped module bindings
    (2026-08-30), and a smaller world is always the reassuring one -- so this one
    reuses the repaired resolver rather than growing a second.

    A tool registered outside `server/core/*.py` cannot be seen here; the count of
    what the census covers is returned with it, so a shrinking census is loud.
    """
    index = _core_index()
    defs, calls = index["defs"], index["calls"]

    census: list = []
    for module, tree in sorted(index["trees"].items()):
        for tool in sorted(_registered_tool_names(tree)):
            decl = declared.get(tool)
            if decl is None or decl.effect != "confirmed_write":
                continue
            key = (module, tool)
            if key not in defs:
                continue
            reaches = {name for _file, name in _reachable(index, calls[key])}
            consumes = sorted(reaches & _CONFIRMATION_CONSUMERS.keys())
            census.append(
                {
                    "tool": tool,
                    "module": module,
                    "profile": decl.profile,
                    "confirmation_mode": decl.confirmation_mode,
                    "plays_the_two_step": bool(reaches & _TWO_STEP_ENTRY),
                    "consumes_a_confirmation": bool(consumes),
                    "consumes": consumes,
                    "reaches": sorted(reaches),
                }
            )
    return census


def _collect() -> dict:
    """Boot the real app and measure assembled, wire, and model projections."""
    from core import main as core_main  # noqa: PLC0415
    from core import mcp_profiles  # noqa: PLC0415
    from core.skill_tool_catalog import list_skill_tool_catalog  # noqa: PLC0415

    tools = asyncio.run(core_main.mcp._list_tools())
    declared = {d.name: d for d in mcp_profiles.registered_declarations()}
    connectors = sorted(loaded.name for loaded in core_main._loaded_modules)

    # The default persona: no token, no evidence -> insights only, not interactive.
    allowed = mcp_profiles.visible_profiles(None, None, None)
    # The SAME function the middleware calls -- profile and interactive presence.
    # App-only visibility is host-routing metadata and intentionally remains on
    # the wire; the generated Skill projection filters it separately.
    wire = mcp_profiles.model_visible_tools(
        tools, allowed=allowed, interactive=False
    )
    skill = asyncio.run(list_skill_tool_catalog())["tools"]
    app_only = sorted(
        getattr(t, "name", "") or "" for t in tools if mcp_profiles.is_app_only_tool(t)
    )

    def _name(tool) -> str:
        return getattr(tool, "name", "") or ""

    def _weight(tool) -> int:
        return len(_name(tool)) + len(getattr(tool, "description", None) or "")

    def _carries_connector_name(name: str) -> bool:
        # A namespaced mount produces "<connector>_<tool>"; a connector that named
        # itself inside a core tool produces the name anywhere in the string.
        return any(
            name.startswith(f"{connector}_") or f"_{connector}_" in name
            for connector in connectors
        )

    wire_catalog_bytes = sum(_weight(tool) for tool in wire)
    skill_catalog_bytes = sum(
        len(item["name"]) + len(item["description"]) for item in skill
    )
    provider_named = sorted(_name(t) for t in tools if _carries_connector_name(_name(t)))
    undeclared_visible = sorted(
        _name(t) for t in wire if _name(t) not in declared
    )
    wire_names = [_name(t) for t in wire]
    skill_names = [item["name"] for item in skill]
    host_routed = [name for name in wire_names if name in app_only]
    expected_host_routed = sorted(mcp_profiles.HOST_ROUTED_APP_ONLY_TOOLS)
    if Counter(host_routed) != Counter(expected_host_routed):
        raise RuntimeError(
            "app-only wire projection drifted: "
            f"expected exactly {expected_host_routed}, got {host_routed}"
        )
    leaked_to_skill = [name for name in skill_names if name in app_only]
    if leaked_to_skill:
        raise RuntimeError(f"app-only tools leaked into model Skill: {leaked_to_skill}")

    by_profile: dict[str, int] = {}
    for decl in declared.values():
        by_profile[decl.profile] = by_profile.get(decl.profile, 0) + 1

    # The split inside `insights`: a count of tools never showed clause 8.
    appending = insights_append_census(declared)
    ungoverned = sorted(
        entry['tool'] for entry in appending if not entry['consumes_a_handle']
    )
    # Clause 9: WHO plays the second ceremony. The first is central and per call
    # (`on_call_tool` refuses a `confirmed_write` without verified interactive
    # presence); this names the bodies that also consume a per-call confirmation.
    ceremonies = confirmed_write_ceremony_census(declared)
    two_step = sorted(e['tool'] for e in ceremonies if e['plays_the_two_step'])
    consuming = sorted(e['tool'] for e in ceremonies if e['consumes_a_confirmation'])

    return {
        "assembled_tools": len(tools),
        "wire_tools": len(wire),
        "wire_catalog_bytes": wire_catalog_bytes,
        "wire_catalog_tokens_estimate": wire_catalog_bytes // _BYTES_PER_TOKEN,
        "model_skill_tools": len(skill),
        "model_skill_catalog_bytes": skill_catalog_bytes,
        "model_skill_catalog_tokens_estimate": skill_catalog_bytes // _BYTES_PER_TOKEN,
        "declarations": len(declared),
        "declarations_by_profile": dict(sorted(by_profile.items())),
        "undeclared_reaching_discovery": undeclared_visible,
        "app_only_tools": app_only,
        "host_routed_app_only_tools": host_routed,
        "callable_by_name_app_only_tools": sorted(set(app_only) - set(host_routed)),
        "model_skill_app_only_tools": leaked_to_skill,
        "provider_named_tools": provider_named,
        "loaded_connectors": len(connectors),
        "wire_tool_names": sorted(wire_names),
        "wire_tool_weights": {
            _name(t): _weight(t) for t in sorted(wire, key=_name)
        },
        "model_skill_tool_names": sorted(skill_names),
        "insights_appending_tools": appending,
        "insights_appending_without_a_handle": ungoverned,
        "confirmed_write_tools": ceremonies,
        "confirmed_write_playing_the_two_step": two_step,
        "confirmed_write_consuming_a_confirmation": consuming,
    }


def _print_human(report: dict, verbose: bool) -> None:
    print("MCP tool surface -- what a default host sees before it does anything")
    print("=" * 72)
    print(f"  assembled tools (every provider)      : {report['assembled_tools']}")
    print(f"  tools/list for the default persona    : {report['wire_tools']}")
    print(
        f"  wire cost (names + descriptions)      : {report['wire_catalog_bytes']} bytes"
        f"  ~= {report['wire_catalog_tokens_estimate']} tokens (bytes/{_BYTES_PER_TOKEN})"
    )
    print(f"  model Skill projection                : {report['model_skill_tools']}")
    print(
        f"  Skill cost (names + descriptions)     : {report['model_skill_catalog_bytes']} bytes"
        f"  ~= {report['model_skill_catalog_tokens_estimate']} tokens "
        f"(bytes/{_BYTES_PER_TOKEN})"
    )
    print(f"  loaded connectors                     : {report['loaded_connectors']}")
    print(
        f"  profiled declarations                 : {report['declarations']} "
        f"{report['declarations_by_profile']}"
    )
    print()
    undeclared = report["undeclared_reaching_discovery"]
    print(f"REACH DISCOVERY WITHOUT A DECLARED PROFILE: {len(undeclared)}")
    for name in undeclared:
        print(f"    {name}")
    print()
    host_routed = report["host_routed_app_only_tools"]
    print(f"APP-ONLY, PRESENT ON THE WIRE FOR HOST ROUTING: {len(host_routed)}")
    for name in host_routed:
        print(f"    {name}")
    callable_only = report["callable_by_name_app_only_tools"]
    print(f"APP-ONLY, CALLABLE BY NAME BUT NOT ON THE WIRE: {len(callable_only)}")
    for name in callable_only:
        print(f"    {name}")
    print()
    named = report["provider_named_tools"]
    print(f"TOOL NAMES CARRYING A CONNECTOR NAME: {len(named)}")
    for name in named:
        print(f"    {name}")
    print()
    appending = report["insights_appending_tools"]
    print(f"INSIGHTS TOOLS THAT APPEND: {len(appending)}")
    for entry in appending:
        gate = "handle" if entry["consumes_a_handle"] else "NO HANDLE"
        print(
            f"    [{gate:>9}] {entry['tool']} ({entry['module']}) "
            f"-> {', '.join(entry['appends_to'])}"
        )
    print()
    ceremonies = report["confirmed_write_tools"]
    two_step = report["confirmed_write_playing_the_two_step"]
    print(
        f"CONFIRMED_WRITE TOOLS SEEN IN core/: {len(ceremonies)} "
        f"-- every one refused at on_call_tool without verified interactive presence"
    )
    consuming = report["confirmed_write_consuming_a_confirmation"]
    print(f"    of which consuming a single-use confirmation: {len(consuming)}")
    for name in consuming:
        mark = " (execute_confirmed_operation)" if name in two_step else ""
        print(f"        {name}{mark}")
    if verbose:
        print()
        print("EVERY WIRE TOOL, BY CATALOG WEIGHT")
        weights = report["wire_tool_weights"]
        for name in sorted(weights, key=lambda n: -weights[n]):
            print(f"    {weights[name]:6d}  {name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--verbose", action="store_true", help="also list every visible tool"
    )
    parser.add_argument(
        "--gate",
        action="store_true",
        help="exit non-zero when a BUDGETS ceiling is exceeded",
    )
    args = parser.parse_args(argv)

    report = _collect()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_human(report, args.verbose)
    if args.gate:
        exceeded = [
            f"{key}={report[key]} > budget {ceiling}"
            for key, ceiling in BUDGETS.items()
            if report[key] > ceiling
        ]
        if exceeded:
            print()
            print("CATALOG GROWTH BOUND EXCEEDED:")
            for line in exceeded:
                print(f"  {line}")
            print(
                "  Adding a core tool is a catalog decision: raise the budget in "
                "scripts/mcp_tool_surface_report.py with the reason, or slim the surface."
            )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
