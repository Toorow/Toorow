"""Conformance — the product nouns stay canonical (glossary.md).

`Module`, `Connector` and `Tool` all named the same object at once: the Data
lens was "Modules", the object was "Connector", its workbench was the "Module
Workbench". A model reading that cannot tell whether it is one object or three.

docs/product-architecture/glossary.md retires `Module` and `Extension` as
product nouns and reserves `Tool` for MCP tools. This file is what keeps the
retirement true, because a synonym reappears one screen at a time and no single
reintroduction ever looks wrong on its own.

WHAT IS CHECKED, AND WHAT IS NOT
Only the five surfaces the glossary declares normative: HTTP route segments,
MCP tool and parameter names, product copy in the console, console route ids,
and the Data lens keys -- plus, since 2026-08-31, the DOCSTRING of every
registered MCP tool.

WHY THE DOCSTRING JOINED. Until then this file read a tool's NAME and its
PARAMETER names and stopped there. A name is a handful of words; the docstring is
the paragraph the model actually reads before it decides what the object is, and
it was the one MCP surface nothing held. Measured the day it was added: of 129
registration sites, six tool docstrings still handed a model a retired noun --
`get_card` offering `report_ref` as `"{module}/{report}"`, `flows_get` promising
"the module base pack", `add_context_event` and `get_events` claiming
"no module-specific strings", `health` counting "modules with quota" and
`get_report` rendering "a module-shipped report definition". A model told the
object is a Module by the paragraph cannot be un-told by the tool name above it.

RESOLVED, NOT GREPPED. A tool is registered in one file (`register_profiled(mcp,
get_card, ...)`) and defined in another, so the docstring is followed through the
`from core.x import y` that carries it -- a name-only match would have read
`cards.get_card`, a business function nobody registers, and reported a defect in
a file the MCP surface never shows.

Internal Python plumbing bound to the `server/modules/` path is explicitly out of
scope -- the glossary keeps `module` as the on-disk name. So is `module_name` as
a wire field: it is a recorded migration (42 emission sites, some matched with
LIKE against stored audit evidence), not a rename this test may force.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]
_UI_SRC = _REPO_ROOT / "ui" / "admin" / "src"
_GLOSSARY = _REPO_ROOT / "docs" / "product-architecture" / "glossary.md"

#: Retired as product nouns. `Tool` is not here: it is a legitimate word for an
#: MCP tool, so it is checked contextually below rather than by spelling.
_RETIRED = re.compile(r"\b[Mm]odules?\b|\b[Ee]xtensions?\b")

#: Occurrences that are the on-disk path or a mockup filename, not the product
#: noun. Each entry is a substring of the offending line.
_ALLOWED_SUBSTRINGS = (
    "server/modules",          # the on-disk path the glossary keeps
    "node_modules",
    "key-modules.html",        # the validated mockup's own filename
    ".module-card",            # ditto, a class in that mockup
    ".module-icon",            # ditto
    "module-breadcrumb",       # legacy CSS class, not copy (tracked separately)
    "module_name",             # recorded wire-field migration, out of scope here
    "module_kind",             # ditto
    "moduleName",              # ditto
    "project_modules",         # DB table, recorded migration
    "toolModule",              # wizard draft field bound to the wire key above
    "module scope",            # python import machinery, not the product noun
    "this module",             # a source file referring to itself
    "module-level",            # a JS/Python module, not a Connector
    "chart modules",           # bundled JS chart modules
)


#: A line reading the not-yet-migrated wire field may say so explicitly. The
#: marker is deliberately verbose: it must be cheaper to rename the noun than to
#: type the exemption, or the exemption becomes the new default.
_WIRE_FIELD_MARKER = "vocabulary: wire-field"


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    hits: list[tuple[int, str]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for number, line in enumerate(lines, 1):
        if not _RETIRED.search(line):
            continue
        if any(allowed in line for allowed in _ALLOWED_SUBSTRINGS):
            continue
        previous = lines[number - 2] if number >= 2 else ""
        if _WIRE_FIELD_MARKER in line or _WIRE_FIELD_MARKER in previous:
            continue
        hits.append((number, line.strip()))
    return hits


def test_glossary_exists_and_retires_the_synonyms() -> None:
    """The contract this whole file enforces must be readable, not implied."""
    assert _GLOSSARY.exists(), (
        f"[vocabulary] {_GLOSSARY} is missing. Every check below points at it; "
        "without it the canon is folklore again."
    )
    text = _GLOSSARY.read_text(encoding="utf-8")
    for word in ("Connector", "Datastream", "Project Capability", "Source Authorization"):
        assert word in text, f"[vocabulary] glossary.md defines no entry for {word!r}"
    assert "Retired synonyms" in text or "retired" in text.lower()


#: The two surfaces this file reads, on 2026-08-31. FLOORS, not equalities.
#: Criterion 13 of `docs/product-architecture/module-boundaries.md`: the route
#: test ends on `assert not offenders` and the screen test is PARAMETRIZED over
#: a glob -- an empty glob collects zero cases, which pytest reports as success.
_CORE_MODULES_AT_2026_08_31 = 524
_CONSOLE_SCREENS_AT_2026_08_31 = 29


def test_both_vocabulary_scans_still_reach_their_surface() -> None:
    """Rename either tree and this test is red, instead of the file going mute."""
    routes = sorted((_REPO_ROOT / "server" / "core").glob("*.py"))
    screens = sorted((_UI_SRC / "shell" / "pages").glob("*.tsx"))
    assert len(routes) >= _CORE_MODULES_AT_2026_08_31, (
        f"{len(routes)} modules globbed under server/core, "
        f"{_CORE_MODULES_AT_2026_08_31} on 2026-08-31 -- the route vocabulary "
        "verdict below is about nothing."
    )
    assert len(screens) >= _CONSOLE_SCREENS_AT_2026_08_31, (
        f"{len(screens)} screens globbed under {_UI_SRC / 'shell' / 'pages'}, "
        f"{_CONSOLE_SCREENS_AT_2026_08_31} on 2026-08-31 -- the per-screen test "
        "is parametrized over this list and collects nothing when it is empty."
    )


def test_no_api_route_segment_names_a_module() -> None:
    """`/api/...` paths are read by people and by agents; they carry the canon.

    A route named /api/modules/... states that the object is a Module, whatever
    the screen above it says.
    """
    offenders: list[str] = []
    for path in (_REPO_ROOT / "server" / "core").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for match in re.finditer(r'"(/api/[^"]*)"', source):
            route = match.group(1)
            if re.search(r"\bmodules?\b|\bextensions?\b", route):
                offenders.append(f"{path.name}: {route}")
    assert not offenders, (
        "[vocabulary] API routes still name a retired object:\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n  The object is a Connector (docs/product-architecture/glossary.md)."
    )


def test_no_mcp_tool_or_parameter_names_a_module() -> None:
    """What the model reads first is the tool name and its parameter names."""
    main = _REPO_ROOT / "server" / "core" / "main.py"
    tree = ast.parse(main.read_text(encoding="utf-8"))

    registered: set[str] = set()
    for node in ast.walk(tree):
        # `mcp.tool(list_connectors)` — registration by reference.
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "tool"
            and node.args
            and isinstance(node.args[0], ast.Name)
        ):
            registered.add(node.args[0].id)

    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        decorated = any(
            (isinstance(d, ast.Call) and getattr(d.func, "attr", None) == "tool")
            or getattr(d, "attr", None) == "tool"
            for d in node.decorator_list
        )
        if not decorated and node.name not in registered:
            continue
        if re.search(r"\bmodules?\b|\bextensions?\b", node.name):
            offenders.append(f"tool name: {node.name}")
        for argument in node.args.args + node.args.kwonlyargs:
            if re.fullmatch(r"modules?|extensions?", argument.arg):
                offenders.append(f"{node.name}(): parameter {argument.arg}")

    assert not offenders, (
        "[vocabulary] MCP surface still names a retired object:\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n  A model cannot tell a Module from a Connector; there is only a Connector."
    )


def test_no_console_route_id_or_data_lens_names_a_module() -> None:
    """Console route ids and Data lens keys are the shared vocabulary seam.

    They appear in the URL, in the navigation config, in the router and in the
    envelope's `schema_version` — five places that must agree or the screen 404s.
    """
    watched = [
        _UI_SRC / "shell" / "ContentRouter.tsx",
        # AD-42 (2026-08-12) : le routage est en trois fichiers. Un garde de
        # vocabulaire qui ne lit que l assembleur cesse de voir les mots des
        # branches -- et il cesse SANS RIEN DIRE, ce qui est la seule facon dont
        # un garde peut mentir.
        _UI_SRC / "shell" / "objectSurfaces.tsx",
        _UI_SRC / "shell" / "collectionSurfaces.tsx",
        _UI_SRC / "shell" / "navigation.ts",
        _UI_SRC / "data" / "dataSurface.ts",
    ]
    offenders: list[str] = []
    for path in watched:
        if not path.exists():
            continue
        for number, line in _offending_lines(path):
            offenders.append(f"{path.relative_to(_REPO_ROOT)}:{number}: {line}")
    assert not offenders, (
        "[vocabulary] console routing still names a retired object:\n"
        + "\n".join(f"  - {o}" for o in offenders)
        + "\n  Data > Connectors, route data/connectors, lens 'connectors'."
    )


#: Registration sites resolved on 2026-08-31. A FLOOR, like the two above: the
#: resolver walks imports, and a rename that broke the walk would leave this test
#: reading zero docstrings and reporting success.
_MCP_REGISTRATIONS_AT_2026_08_31 = 129


def _core_trees() -> dict[str, ast.Module]:
    return {
        path.name: ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted((_REPO_ROOT / "server" / "core").glob("*.py"))
    }


def _registration_sites(trees: dict[str, ast.Module]) -> set[tuple[str, str]]:
    """Every ``(module, handler name)`` this repository registers as an MCP tool.

    Three shapes, because the repository uses three: `register_profiled(mcp,
    handler, ...)` (the governed door, 135 call sites), a bare `mcp.tool(handler)`
    and the `@mcp.tool` decorator.
    """
    sites: set[tuple[str, str]] = set()
    for module, tree in trees.items():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if (
                    isinstance(func, ast.Name)
                    and func.id == "register_profiled"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Name)
                ):
                    sites.add((module, node.args[1].id))
                elif (
                    isinstance(func, ast.Attribute)
                    and func.attr == "tool"
                    and node.args
                    and isinstance(node.args[0], ast.Name)
                ):
                    sites.add((module, node.args[0].id))
            if isinstance(node, ast.FunctionDef):
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and getattr(decorator.func, "attr", None) == "tool"
                    ) or getattr(decorator, "attr", None) == "tool":
                        sites.add((module, node.name))
    return sites


def _resolve_handler(
    trees: dict[str, ast.Module],
    module: str,
    name: str,
    seen: tuple[str, ...] = (),
) -> tuple[str, ast.FunctionDef] | None:
    """Follow *name* from the file that registers it to the file that defines it."""
    if module in seen or module not in trees:
        return None
    tree = trees[module]
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return module, node
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("core."):
            for alias in node.names:
                if (alias.asname or alias.name) == name:
                    return _resolve_handler(
                        trees,
                        node.module.split(".")[-1] + ".py",
                        alias.name,
                        seen + (module,),
                    )
    return None


def test_no_registered_mcp_tool_docstring_names_a_module() -> None:
    """The paragraph a model reads before it decides what the object is.

    The tool NAME and the PARAMETER names are held above. This holds the sentence
    between them, which is longer than both and was the only part of the MCP
    surface nothing read.
    """
    trees = _core_trees()
    sites = _registration_sites(trees)
    assert len(sites) >= _MCP_REGISTRATIONS_AT_2026_08_31, (
        f"{len(sites)} MCP registration sites resolved, "
        f"{_MCP_REGISTRATIONS_AT_2026_08_31} on 2026-08-31 -- the docstring "
        "verdict below is about nothing."
    )

    offenders: list[str] = []
    read = 0
    for module, name in sorted(sites):
        resolved = _resolve_handler(trees, module, name)
        if resolved is None:
            # `mcp.tool(handler)` inside `register_profiled` itself: a parameter,
            # not a tool. Skipped rather than failed -- and the floor above is
            # what keeps the skip from becoming the whole answer.
            continue
        where, node = resolved
        docstring = ast.get_docstring(node, clean=False)
        if not docstring:
            continue
        read += 1
        lines = docstring.splitlines()
        for number, line in enumerate(lines, 1):
            if not _RETIRED.search(line):
                continue
            if any(allowed in line for allowed in _ALLOWED_SUBSTRINGS):
                continue
            previous = lines[number - 2] if number >= 2 else ""
            if _WIRE_FIELD_MARKER in line or _WIRE_FIELD_MARKER in previous:
                continue
            offenders.append(f"{where}#{name} docstring line {number}: {line.strip()}")

    assert read >= 100, (
        f"only {read} tool docstrings were read; the resolver stopped following "
        "imports and this verdict is vacuous."
    )
    assert not offenders, (
        "[vocabulary] a registered MCP tool tells a model the object is a "
        "retired noun:\n"
        + "\n".join(f"  - {o}" for o in sorted(set(offenders)))
        + "\n  The object is a Connector (docs/product-architecture/glossary.md). "
        "A wire key that cannot be renamed yet may say so with "
        f"`{_WIRE_FIELD_MARKER}`."
    )


@pytest.mark.parametrize(
    "screen",
    sorted(
        path.relative_to(_UI_SRC).as_posix()
        for path in (_UI_SRC / "shell" / "pages").glob("*.tsx")
    ),
)
def test_no_screen_copy_names_a_module(screen: str) -> None:
    """Screen titles, empty states and error copy are what a person reads.

    Parametrized per screen so a reintroduction names the file it landed in
    rather than one opaque list failure.
    """
    path = _UI_SRC / screen
    offenders = _offending_lines(path)
    assert not offenders, (
        f"[vocabulary] {screen} still shows a retired product noun:\n"
        + "\n".join(f"  - line {number}: {line}" for number, line in offenders)
        + "\n  See docs/product-architecture/glossary.md."
    )
