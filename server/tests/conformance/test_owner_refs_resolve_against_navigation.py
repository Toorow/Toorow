"""An owner reference the server composes is an address the console can open.

WHY THIS FILE EXISTS. The audit of 2026-08-17 measured the defect and named the
class: *"une reference owner emise par le serveur que la navigation ne sait pas
resoudre meurt en silence"*. The instance was `action: "first-publication"`,
composed on the `datastream` object by `project_overview.py` and
`project_readiness.py`, against a `datastream` navigation contract that declares
NO actions. `ContentRouter.openOwner` refused it and returned; the single most
important item of a brand-new Project clicked and did nothing.

Repointing that one reference fixes one click. This file is the other half of
the P0: it derives, on every run, EVERY workspace / section / object type / tab /
action the server composes into an owner reference, and validates each one
against the console's navigation registry. The next invented address breaks a
test instead of a click.

HOW IT DERIVES, AND WHAT IT CANNOT SEE.

Two layers, both over `server/core/*.py`, both by `ast` -- never by grep, because
a textual match credits an import as a call (the mistake
`test_mcp_tools_resolve_project_scope.py` had to repair on 2026-08-17).

  1. CALLS to an owner-reference CONSTRUCTOR. The constructors are DERIVED, not
     listed: a function of `server/core` whose body returns a dict literal
     carrying exactly the eleven owner fields, and whose first two positional
     parameters are named `workspace` and `section`. Matching on the NAME instead
     was tried and measured wrong -- it credited `_owner_link`, `_owner_href`,
     `_datastream_owner` and `owner_command`, four unrelated helpers with four
     different signatures, and produced nineteen false failures. The same name
     also means two different things in two modules (`_owner` in
     `project_readiness` and in `controls_attention`), so a call is resolved
     through the calling module's own imports and definitions.
     Workspace and section are read from the first two positional arguments or
     from the matching keywords; `object_type`, `tab` and `action` from their
     keywords. EVERY string constant inside a keyword expression is collected, so
     a conditional (`action=("x" if cond else None)`) is checked on both branches
     -- which is exactly the shape the measured defect had.
  2. DICT LITERALS carrying the eleven owner fields with `surface: "project"` --
     the hand-built shape, which no call-site scan would see.

WHAT IT CANNOT SEE, STATED RATHER THAN HIDDEN: a reference whose workspace or
section is a VARIABLE. `controls_attention._owner` passes `section` through, and
`getting_started._owner_reference` assigns the pair to locals before building the
dict. Those call sites are counted and printed, and the count is pinned by
`test_the_underivable_share_does_not_grow` -- so the blind spot cannot widen
without somebody deciding to widen it.

The registry is read through `tests/support/navigation_source` (AD-42: a guard
pinned to one PATH went quietly true the day the registry was split).
"""

from __future__ import annotations

import ast
import pathlib
import re

import pytest

from tests.support.navigation_source import navigation_source

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CORE = REPO_ROOT / "server" / "core"
SHELL_NAV = REPO_ROOT / "ui" / "admin" / "src" / "shell" / "navigation"

# The eleven keys `_OWNER_FIELDS` fixes in `project_overview.py`.
OWNER_FIELDS = {
    "surface", "workspace", "section", "global_surface", "global_section",
    "object_type", "object_id", "tab", "action", "version_id", "evidence_id",
}

# Call sites whose workspace or section is not a literal, today. Measured, not
# chosen: raise it only with a reason.
UNDERIVABLE_BUDGET = 11

# ONE reference of this class is still unrepaired, and it is named rather than
# hidden. `query_execution.py` was being edited by another session on the day
# this guard landed (2026-08-17), and repointing an owner inside a file somebody
# else holds open is how two correct repairs become one broken merge.
#
# What it names: `governance/capabilities`, a section the console does not
# declare. Project capabilities live on the GLOBAL Project Settings surface
# (`project-settings` / `capabilities`), which `_global_owner_reference` builds.
# Since the repair of `ContentRouter.openOwner` it no longer dies in silence --
# the refusal now names a gesture -- but it still does not open its owner.
# VIDE depuis le 2026-08-22 (story 67.11), et il le reste par egalite stricte :
# une entree qui ne correspond plus a un site echoue aussi fort qu'un site non
# declare. `query_execution` composait `governance/capabilities` ; il compose
# desormais la surface GLOBALE que la console declare vraiment.
KNOWN_UNRESOLVED: set[tuple[str, str, str]] = set()


# ---------------------------------------------------------------------------
# The console registry, parsed into contracts
# ---------------------------------------------------------------------------


def _balanced(text: str, start: int) -> str:
    """Return the argument text of the `section(` call opening at *start*."""
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[start + 1 : index]
    raise AssertionError("unbalanced section() call in the navigation registry")


def _strings(text: str) -> list[str]:
    return re.findall(r'"([^"\\]*)"', text)


def navigation_contracts() -> dict[str, dict[str, dict]]:
    """`{workspace: {section: {"objects": {type: {"tabs", "actions"}}, "actions": [...]}}}`.

    Derived from the workspace files, never restated here: a table copied into a
    test is a second registry, and the two disagree the first time a section
    moves.
    """
    contracts: dict[str, dict[str, dict]] = {}
    for path in sorted(SHELL_NAV.glob("*.ts")):
        source = path.read_text(encoding="utf-8")
        # COMMENTS ARE STRIPPED FIRST, and it is not tidiness. The `result`
        # contract of `analyze/explore` carries `// ... /tab/{lens}/evidence/
        # {datum_key}`: those braces closed the contract block early, the object
        # vanished from this parse, and the test reported a valid owner
        # reference as naming an object the console does not hold.
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
        source = re.sub(r"//[^\n]*", "", source)
        key = re.search(r'\bkey:\s*"([\w-]+)"', source)
        if not key:
            continue  # vocabulary.ts declares the types, not a workspace.
        sections: dict[str, dict] = {}
        for match in re.finditer(r"\bsection\(", source):
            args = _balanced(source, match.end() - 1)
            slug = _strings(args)[0]
            # An object contract is the only brace block inside a section call
            # that carries a `type:`; lens entries carry `slug:`.
            objects: dict[str, dict] = {}
            for block in re.findall(r"\{[^{}]*\}", args):
                object_type = re.search(r'\btype:\s*"([\w-]+)"', block)
                if not object_type:
                    continue
                tabs = re.search(r"\btabs:\s*\[([^\]]*)\]", block)
                actions = re.search(r"\bactions:\s*\[([^\]]*)\]", block)
                objects[object_type.group(1)] = {
                    "tabs": _strings(tabs.group(1)) if tabs else [],
                    "actions": _strings(actions.group(1)) if actions else [],
                }
            # Collection-owned actions are the bare string array left once the
            # object contracts and the lens entries are removed.
            stripped = re.sub(r"\{[^{}]*\}", "", args)
            collection_actions: list[str] = []
            for array in re.findall(r"\[([^\[\]]*)\]", stripped):
                collection_actions.extend(_strings(array))
            sections[slug] = {"objects": objects, "actions": collection_actions}
        contracts[key.group(1)] = sections
    return contracts


CONTRACTS = navigation_contracts()


# ---------------------------------------------------------------------------
# The owner references the server composes
# ---------------------------------------------------------------------------


class Reference:
    """One derived owner reference: where it is written, and what it names."""

    def __init__(self, where: str, workspace, section, object_types, tabs, actions):
        self.where = where
        self.workspace = workspace
        self.section = section
        self.object_types = object_types
        self.tabs = tabs
        self.actions = actions

    @property
    def derivable(self) -> bool:
        return isinstance(self.workspace, str) and isinstance(self.section, str)

    def __repr__(self) -> str:  # pragma: no cover -- assertion messages only
        return (
            f"{self.where}: {self.workspace}/{self.section} "
            f"{self.object_types} {self.tabs} {self.actions}"
        )


def _constants(node: ast.AST) -> list[str]:
    """Every string constant this expression can EVALUATE to (`IfExp` included).

    A subscript KEY is not one of them. `tab=item["target"] if ... else None`
    can evaluate to whatever `item["target"]` holds -- never to the string
    `"target"`, which merely names the slot. Collecting it reported a tab called
    `target` that nobody had ever composed.
    """
    values: list[str] = []
    skip = {id(child.slice) for child in ast.walk(node) if isinstance(child, ast.Subscript)}

    def visit(current: ast.AST) -> None:
        if id(current) in skip:
            return
        if isinstance(current, ast.Constant) and isinstance(current.value, str):
            values.append(current.value)
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return values


def _one(node: ast.AST | None):
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _returns_owner_dict(node: ast.FunctionDef) -> bool:
    """Does this function return a dict literal carrying the eleven owner fields?"""
    for child in ast.walk(node):
        if not isinstance(child, ast.Return) or not isinstance(child.value, ast.Dict):
            continue
        keys = {
            key.value
            for key in child.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        if keys == OWNER_FIELDS:
            return True
    return False


def owner_constructors() -> set[tuple[str, str]]:
    """The functions that BUILD a project owner reference, derived from their bodies.

    The signature check is the discriminator: `_global_owner_reference` takes
    `(surface, section)` and addresses the settings shell, not a workspace, so it
    is not one of these -- and neither is any of the four `*_owner_*` helpers
    whose first parameter is a connection or a record.
    """
    names: set[tuple[str, str]] = set()
    for path in sorted(CORE.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or not _returns_owner_dict(node):
                continue
            params = [arg.arg for arg in node.args.args][:2]
            if params == ["workspace", "section"]:
                names.add((path.stem, node.name))
    return names


CONSTRUCTORS = owner_constructors()


def _callable_names(module: str, tree: ast.Module) -> set[str]:
    """The constructor names this module can actually call: its own, and its imports.

    KEYED BY MODULE, and that is the whole point. `_owner` names a constructor in
    `project_readiness` and something else entirely in `analyze_workbench` and
    `controls_attention`; a set of bare names credited all three and produced
    seven false failures. A local definition SHADOWS an import of the same name,
    so it decides on its own merits.

    An import is not a call (the mistake `test_mcp_tools_resolve_project_scope.py`
    repaired on 2026-08-17) -- but it IS what says which constructor a bare name
    in this module refers to. Aliases resolve to what they import.
    """
    local = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    names = {name for (owner, name) in CONSTRUCTORS if owner == module and name in local}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or not node.module:
            continue
        source = node.module.rsplit(".", 1)[-1]
        for alias in node.names:
            if (source, alias.name) in CONSTRUCTORS:
                imported = alias.asname or alias.name
                if imported not in local:
                    names.add(imported)
    return names


def _from_call(path: pathlib.Path, node: ast.Call, callable_names: set[str]) -> Reference | None:
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    if name not in callable_names:
        return None
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    positional = list(node.args)
    workspace = _one(positional[0]) if positional else _one(keywords.get("workspace"))
    section = _one(positional[1]) if len(positional) > 1 else _one(keywords.get("section"))
    return Reference(
        where=f"{path.name}:{node.lineno} {name}()",
        workspace=workspace,
        section=section,
        object_types=_constants(keywords["object_type"]) if "object_type" in keywords else [],
        tabs=_constants(keywords["tab"]) if "tab" in keywords else [],
        actions=_constants(keywords["action"]) if "action" in keywords else [],
    )


# ---------------------------------------------------------------------------
# Call sites that unpack a ROUTE TABLE, and why they are not a blind spot
# ---------------------------------------------------------------------------
#
# `master_data_consumers._href` composes an owner reference from
# `workspace, section, object_type, tab = route`, where `route` is a row of one
# of two module-level tables. Read as a call site alone it is underivable -- two
# variables -- and on 2026-09-01 (`ce647c59`, story 49.2) it became the twelfth
# such site against a budget of eleven.
#
# RAISING THE BUDGET WOULD HAVE BEEN THE WRONG ANSWER, because the destinations
# are not unknown here: they are nineteen LITERAL four-tuples written in the
# module, and every one of them is exactly the kind of address this file exists
# to resolve. A budget is for what cannot be checked; this can. So the scan
# follows the unpack and reports one reference per row of the table -- the blind
# spot shrinks instead of the ratchet loosening, and a route added to either
# table is checked against the console the day it is written.


def _route_tuple(node: ast.AST) -> tuple | None:
    """`(workspace, section, object_type, tab)` if this is that literal, else None."""
    if not isinstance(node, ast.Tuple) or len(node.elts) != 4:
        return None
    values = []
    for element in node.elts:
        if not isinstance(element, ast.Constant):
            return None
        if not (isinstance(element.value, str) or element.value is None):
            return None
        values.append(element.value)
    return tuple(values)


def _route_tables(tree: ast.Module) -> list[tuple[str, tuple]]:
    """`[(key, route), ...]` for the route tables a module declares at top level.

    MODULE LEVEL ONLY, and the restriction is the point: a four-tuple built
    inside a function is not a declared destination, and counting it would let
    the scan invent addresses nobody composes. The indexed form is read too --
    `for key in KEYS: TABLE[key] = (...)` is how `master_data_consumers` says
    that every Rule Set profile routes to one workbench.
    """
    routes: list[tuple[str, tuple]] = []

    def _from_statement(statement: ast.AST) -> None:
        if isinstance(statement, ast.Assign | ast.AnnAssign):
            value = statement.value
            if isinstance(value, ast.Dict):
                for key, entry in zip(value.keys, value.values):
                    route = _route_tuple(entry)
                    if route is not None and isinstance(key, ast.Constant):
                        routes.append((str(key.value), route))
                return
            target = statement.targets[0] if isinstance(statement, ast.Assign) else statement.target
            if isinstance(target, ast.Subscript):
                route = _route_tuple(value)
                if route is not None:
                    routes.append((ast.unparse(target), route))

    for statement in tree.body:
        _from_statement(statement)
        if isinstance(statement, ast.For):
            for inner in statement.body:
                _from_statement(inner)
    return routes


def _unpack_bindings(func: ast.FunctionDef) -> dict[str, int]:
    """`{local name -> its index in the route tuple}` for `a, b, c, d = <param>`."""
    params = {arg.arg for arg in func.args.args} | {arg.arg for arg in func.args.kwonlyargs}
    bindings: dict[str, int] = {}
    for node in ast.walk(func):
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Tuple) or not isinstance(node.value, ast.Name):
            continue
        if node.value.id not in params:
            continue
        for index, element in enumerate(target.elts):
            if isinstance(element, ast.Name):
                bindings[element.id] = index
    return bindings


def _slot(node: ast.AST | None, bindings: dict[str, int]) -> int | None:
    return bindings.get(node.id) if isinstance(node, ast.Name) else None


def _expand_route_call(
    path: pathlib.Path,
    node: ast.Call,
    name: str,
    bindings: dict[str, int],
    tables: list[tuple[str, tuple]],
) -> list[Reference]:
    """One Reference per declared route, or `[]` when this is not that shape."""
    keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
    positional = list(node.args)

    def _argument(index: int, keyword: str):
        if len(positional) > index:
            return positional[index]
        return keywords.get(keyword)

    workspace_slot = _slot(_argument(0, "workspace"), bindings)
    section_slot = _slot(_argument(1, "section"), bindings)
    if workspace_slot is None or section_slot is None or not tables:
        return []
    object_slot = _slot(keywords.get("object_type"), bindings)
    tab_slot = _slot(keywords.get("tab"), bindings)

    references: list[Reference] = []
    for key, route in tables:
        workspace, section = route[workspace_slot], route[section_slot]
        if not isinstance(workspace, str) or not isinstance(section, str):
            # The table says "workspace, and no addressable screen"; `_href`
            # returns None for it, so no owner reference is composed at all.
            # Emitting one would report an address the product refuses to build.
            continue
        object_type = route[object_slot] if object_slot is not None else None
        tab = route[tab_slot] if tab_slot is not None else None
        references.append(
            Reference(
                where=f"{path.name}:{node.lineno} {name}() route {key!r}",
                workspace=workspace,
                section=section,
                object_types=[object_type] if isinstance(object_type, str) else [],
                tabs=[tab] if isinstance(tab, str) else [],
                actions=[],
            )
        )
    return references


def _from_dict(path: pathlib.Path, node: ast.Dict) -> Reference | None:
    keys = {k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)}
    if keys != OWNER_FIELDS:
        return None
    pairs = {
        key.value: value
        for key, value in zip(node.keys, node.values)
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }
    if _one(pairs.get("surface")) != "project":
        return None
    return Reference(
        where=f"{path.name}:{node.lineno} literal",
        workspace=_one(pairs.get("workspace")),
        section=_one(pairs.get("section")),
        object_types=_constants(pairs["object_type"]) if "object_type" in pairs else [],
        tabs=_constants(pairs["tab"]) if "tab" in pairs else [],
        actions=_constants(pairs["action"]) if "action" in pairs else [],
    )


def composed_references() -> list[Reference]:
    found: list[Reference] = []
    for path in sorted(CORE.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover -- a broken module fails elsewhere
            continue
        callable_names = _callable_names(path.stem, tree)
        tables = _route_tables(tree)
        #: `{id(call node) -> the function that holds it}`, so a call whose
        #: workspace comes from a name can be asked WHERE that name was bound.
        enclosing: dict[int, ast.FunctionDef] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        enclosing.setdefault(id(child), node)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                reference = _from_call(path, node, callable_names)
                if reference is not None and not reference.derivable:
                    func = enclosing.get(id(node))
                    expanded = (
                        _expand_route_call(
                            path,
                            node,
                            reference.where.rsplit(" ", 1)[-1].removesuffix("()"),
                            _unpack_bindings(func),
                            tables,
                        )
                        if func is not None
                        else []
                    )
                    if expanded:
                        found.extend(expanded)
                        continue
            else:
                reference = _from_dict(path, node) if isinstance(node, ast.Dict) else None
            if reference is not None:
                found.append(reference)
    return found


REFERENCES = composed_references()


# ---------------------------------------------------------------------------
# The instrument answers before it judges
# ---------------------------------------------------------------------------


def test_the_registry_parsed_into_real_contracts():
    """A parser that silently found nothing would make every check below pass."""
    assert set(CONTRACTS) >= {"overview", "analyze", "test", "data", "governance", "context-hub"}
    assert CONTRACTS["analyze"]["renders"]["objects"]["render"]["tabs"] == [
        "result", "evidence", "sharing",
    ]
    assert CONTRACTS["data"]["datastreams"]["actions"] == ["create"]
    assert CONTRACTS["data"]["datastreams"]["objects"]["datastream"]["actions"] == []
    # And the registry text really is the assembled one, not one file.
    assert 'key: "governance"' in navigation_source()


def test_the_scan_found_owner_references_to_check():
    derivable = [reference for reference in REFERENCES if reference.derivable]
    assert len(derivable) >= 20, (
        f"only {len(derivable)} derivable owner references found -- the scan is broken, "
        "not the code"
    )


# ---------------------------------------------------------------------------
# The class
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [reference for reference in REFERENCES if reference.derivable],
    ids=lambda reference: reference.where,
)
def test_every_composed_owner_reference_is_an_address_the_console_can_open(reference):
    residue = (reference.where.split(":")[0], reference.workspace, reference.section)
    if residue in KNOWN_UNRESOLVED:
        pytest.xfail(f"named residue, owned by another session today: {reference.where}")
    sections = CONTRACTS.get(reference.workspace)
    assert sections is not None, (
        f"{reference.where} names workspace {reference.workspace!r}, which the console "
        "does not declare"
    )
    contract = sections.get(reference.section)
    assert contract is not None, (
        f"{reference.where} names section {reference.workspace}/{reference.section}, which "
        "the console does not declare"
    )

    for object_type in reference.object_types:
        assert object_type in contract["objects"], (
            f"{reference.where} names object {object_type!r}, which "
            f"{reference.workspace}/{reference.section} does not hold"
        )

    declared_tabs = {tab for obj in contract["objects"].values() for tab in obj["tabs"]}
    for tab in reference.tabs:
        assert tab in declared_tabs, (
            f"{reference.where} names tab {tab!r}, which no object of "
            f"{reference.workspace}/{reference.section} declares"
        )

    declared_actions = set(contract["actions"])
    for obj in contract["objects"].values():
        declared_actions.update(obj["actions"])
    for action in reference.actions:
        assert action in declared_actions, (
            f"{reference.where} names action {action!r}, which neither "
            f"{reference.workspace}/{reference.section} nor its objects declare. "
            "This is the class the audit of 2026-08-17 named: the console refuses it, "
            "and before the repair it refused it in SILENCE."
        )


def test_the_first_publication_gesture_points_at_the_share_and_render_destination():
    """The ratified destination, asserted where it is composed.

    Amendment of 2026-08-17 to `first-figure-path.md`: the first-publication
    gesture lands on the web share and the MCP app, and "the owner refs (...) are
    repointed at the share/render destination". `analyze/renders` is that
    destination -- the collection whose object carries the `sharing` tab.
    """
    composing = [
        reference
        for reference in REFERENCES
        if reference.where.split(":")[0] in {"project_readiness.py", "project_overview.py"}
    ]
    assert composing, "the scan lost the two modules that compose the readiness owners"

    orphan_actions = [
        reference for reference in composing if "first-publication" in reference.actions
    ]
    assert not orphan_actions, (
        "an owner reference still composes `first-publication` as a navigation action: "
        f"{orphan_actions}"
    )

    renders = [
        reference
        for reference in composing
        if (reference.workspace, reference.section) == ("analyze", "renders")
    ]
    assert len(renders) >= 2, (
        "both the readiness component and the attention item must name the ratified "
        f"share/render destination; found {renders}"
    )


def test_the_underivable_share_does_not_grow():
    """A blind spot that is counted cannot widen without a decision."""
    underivable = [reference for reference in REFERENCES if not reference.derivable]
    assert len(underivable) <= UNDERIVABLE_BUDGET, (
        f"{len(underivable)} owner references build their workspace or section from a "
        f"variable (budget {UNDERIVABLE_BUDGET}); this scan cannot check them:\n"
        + "\n".join(reference.where for reference in underivable)
    )
