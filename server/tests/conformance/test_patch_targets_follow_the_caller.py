"""A `patch()` names the module the CALLER resolves through -- AD-43.

WHY THIS FILE EXISTS. Six extractions in two days moved handlers and helpers out
of `admin_api.py`. Every one of them broke test doubles the same way, and the
mistake is always the same shape:

    patch("core.org_lifecycle._erase_org_transactional")   # the SOURCE
    patch("core.admin_api._erase_org_transactional")       # what the caller reads

`_delete_me` still lives in `admin_api`, so it holds its own binding for a name
whose definition moved. Patching the source is not an error Python reports -- it
is a **green test that intercepts nothing**, which is the only kind of failure a
guard has to catch by itself.

A blanket sweep cannot decide this: the answer depends on which module the caller
is written in, one call site at a time. One sweep on 2026-08-12 rewrote a line
that had been repaired an hour earlier, for exactly that reason.

WHAT THIS CHECKS, and what it deliberately does not. For every
`patch("core.X.name")` where `name` IS defined in some other `core` module,
`core.X` must genuinely hold it -- define it or import it. That is exactly the
extraction hazard: the definition moved, and the patch followed the definition
instead of the reader.

It says nothing about a name that exists in NO core module. Those are retired
paths -- `core.snapshot_shares.create_share` is one, held in `xfail(strict=True)`
since story 50.7 -- and a guard that flagged them would be reporting a retirement
as a defect.
"""

from __future__ import annotations

import ast
import pathlib
import re

SERVER = pathlib.Path(__file__).resolve().parents[2]
CORE = SERVER / "core"
TESTS = SERVER / "tests"

#: `core.X.name`, and `core.X.name.anything.else` too. The tail is deliberately
#: not anchored to the closing quote: `patch("core.admin_api.nango_client.
#: _list_connections_async")` is the SAME defect one level deeper, and an
#: anchored pattern read straight past six of them for a day.
_TARGET = re.compile(r'["\'](core\.[a-z_0-9]+)\.(_?[A-Za-z_][A-Za-z_0-9]*)(?:["\'.])')


def _module_names(path: pathlib.Path) -> set[str]:
    """Every name `path` defines OR imports -- i.e. everything it can resolve."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:  # a neighbouring session mid-edit
        return set()
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _targets(text: str) -> list[tuple[str, str, int]]:
    """(module, name, line) for every `core.X.name` written as a patch target."""
    found = []
    for index, line in enumerate(text.split(chr(10)), start=1):
        for module, name in _TARGET.findall(line):
            found.append((module, name, index))
    return found


def _inside_xfail(text: str, lineno: int) -> bool:
    """Is *lineno* inside a test whose decorators declare it expected to fail?"""
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not (node.lineno <= lineno <= (node.end_lineno or node.lineno)):
            continue
        if any("xfail" in ast.unparse(dec) for dec in node.decorator_list):
            return True
    return "pytest.mark.xfail" in text and "pytestmark" in text


def test_no_patch_names_a_module_that_cannot_resolve_it() -> None:
    """The green-and-empty patch, refused at rest.

    Only `core.*` targets are read: a patch on a third-party module is a
    different subject, and a name a module neither defines nor imports is the one
    case that cannot possibly be intercepting anything.
    """
    #: name -> the core modules that DEFINE it. A name defined nowhere is a
    #: retired path, not this guard's subject.
    defined_in: dict[str, set[str]] = {}
    for path in sorted(CORE.glob("*.py")):
        # A core MODULE is a name too. `patch("core.admin_api.nango_client.
        # _list_connections_async")` reaches through a module attribute, and that
        # attribute disappears from `admin_api` the day its readers leave -- eight
        # of those sat green for a day because only `def`s were counted here.
        defined_in.setdefault(path.stem, set()).add("core")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined_in.setdefault(node.name, set()).add(f"core.{path.stem}")

    resolvable: dict[str, set[str]] = {}
    offenders: list[str] = []

    for path in sorted(TESTS.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for module, name, lineno in _targets(text):
            if _inside_xfail(text, lineno):
                # A retired path declares itself broken; its patch naming a module
                # that can no longer resolve the name is PART of the retirement,
                # not a defect. `strict=True` already makes a remount fail loudly.
                continue
            target = CORE / f"{module.split('.')[-1]}.py"
            if not target.exists():
                continue
            if module not in resolvable:
                resolvable[module] = _module_names(target)
            if not resolvable[module]:
                continue
            if name in resolvable[module]:
                continue
            owners = defined_in.get(name, set())
            if not owners:
                continue  # retired path -- see the module docstring
            offenders.append(
                f"{path.relative_to(SERVER)} patches {module}.{name}, but {module} "
                f"neither defines nor imports it -- it is defined in "
                f"{sorted(owners)}. Patch the module the CALLER resolves through."
            )

    assert not offenders, (
        "a patch on a name its module cannot resolve intercepts nothing and stays "
        "green:\n  " + "\n  ".join(sorted(set(offenders)))
    )
