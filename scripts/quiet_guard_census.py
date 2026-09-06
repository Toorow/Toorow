"""Which guards read the repository BY PATH, and which of them can go quiet.

A guard that opens a source file -- `Path(...).read_text()`, `open(...)`,
`inspect.getsource(...)`, a `rglob` over a directory of modules -- and then
asserts `not offenders` measures nothing the day its target moves. The read
returns an empty string, the glob returns an empty list, the offender list is
empty because nothing was scanned, and the suite is green. That is criterion 13
of `docs/product-architecture/module-boundaries.md`: *a guard reads a source
file BY PATH and the code it checks has moved -- it does not fail, it goes
quietly true.* `navigation_backward_audit` is the named case: 31 declared
object types became 0 while the guard kept reporting "0 rendered by nothing".

The repair is never "check the path exists somewhere in a comment". It is the
model of `server/tests/support/minted_identifiers.py`: an instrument states its
own coverage and fails when the coverage collapses. In a test that means ONE
extra assertion -- a floor -- that is false on an empty scan:

    assert len(modules) >= 24, "the route modules moved; this guard scanned none"

This census finds those guards with the AST and sorts them into two columns.

WHAT COUNTS AS A SOURCE-READING GUARD (both must hold):

  a. the test, or a helper it calls, reads a REPO path -- an expression rooted
     at `__file__` (`Path(__file__).parents[3] / "server" / "core"`) or at a
     repo-relative literal (`Path("server/core/admin_api.py")`) -- or calls
     `inspect.getsource`. A read of `tmp_path` is not a repo read: nothing in
     the product can move out from under it.
  b. the test asserts.

FLOORED vs QUIET, stated as one question: *if the read returned nothing, would
an assertion be false?* An assertion is a FLOOR when it fails on an empty scan
-- `assert found`, `assert len(x) >= 3`, `assert x == ["a", "b"]`,
`assert "declare" in source`, `assert path.exists()`, `assert match is not
None`, `if not found: pytest.fail(...)`. It is not a floor when it is vacuously true there --
`assert not offenders`, `assert offenders == []`, `assert all(...)`,
`assert len(x) == 0`. Assertions that sit inside a `for` over the scan, or
inside an `if`, are not floors either: on an empty scan they never run. A test
whose every assertion is of that shape is QUIET -- it passes on nothing.

KNOWN BLIND SPOTS, written down because a guard that hides them is worse than
none: a floor living in an imported helper two modules away is not followed
(same-module helpers are, two levels deep); `assert some_predicate(x)` is read
as a floor because a bare call is usually `.exists()` or `.is_file()`, so a
predicate that happens to be vacuously true is filed FLOORED by mistake; and a
guard parameterized by `@pytest.mark.parametrize` over a hand-written list is
counted once, not per case.

Usage:
    python scripts/quiet_guard_census.py             # per-file verdicts + totals
    python scripts/quiet_guard_census.py --quiet-only # only the guards that can go mute
    python scripts/quiet_guard_census.py --json
    python scripts/quiet_guard_census.py --gate       # ratchet: refuse a NEW quiet guard
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TESTS = REPO / "server" / "tests"

FLOORED = "floored"
QUIET = "quiet"

#: Attribute calls that pull bytes or names out of a path.
_READ_ATTRS = {"read_text", "read_bytes", "read", "open"}

#: Attribute calls that enumerate a directory of sources. A guard over one of
#: these is the high-blast-radius kind: the whole directory can move.
_WALK_ATTRS = {"glob", "rglob", "iterdir", "walk"}

#: `inspect.getsource(...)` reads the file the object was defined in. No path
#: is written down, but the guard is pinned to a source just the same.
_SOURCE_FUNCS = {"getsource", "getsourcefile", "getsourcelines", "findsource"}

#: A string literal starting with one of these is a repo-relative source path.
_REPO_PREFIXES = (
    "server/",
    "ui/",
    "scripts/",
    "docs/",
    "infra/",
    "e2e/",
    "dbt/",
    "reviews/",
    "screens/",
)

#: Vacuously true on an empty collection, so never a floor.
_VACUOUS_CALLS = {"all"}


@dataclass
class Guard:
    """One test function that reads the repository and asserts about it."""

    path: str
    test: str
    line: int
    verdict: str
    reads: str  # "directory" when a glob/walk is involved, else "file"
    targets: list[str]
    floors: list[str]

    @property
    def identity(self) -> str:
        return f"{self.path}::{self.test}"


# --------------------------------------------------------------------------- #
# Is this expression a path into the repository?
# --------------------------------------------------------------------------- #


def _literal_repo_path(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        text = node.value.replace("\\", "/")
        if text.startswith(_REPO_PREFIXES):
            return text
    return None


def _is_anchored(node: ast.AST, anchored: set[str]) -> bool:
    """Does this expression resolve to a path inside the repository?

    Anchored means rooted at `__file__` or at a repo-relative literal, directly
    or through a name this module bound to one. `tmp_path / "x"` is not
    anchored and must not be: a fixture directory cannot go stale.
    """

    if isinstance(node, ast.Name):
        return node.id in anchored or node.id == "__file__"
    if _literal_repo_path(node) is not None:
        return True
    if isinstance(node, ast.Attribute):
        return _is_anchored(node.value, anchored)
    if isinstance(node, ast.Subscript):
        return _is_anchored(node.value, anchored)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _is_anchored(node.left, anchored)
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name) and node.func.id in {"Path", "str", "PurePath"}:
            return bool(node.args) and _is_anchored(node.args[0], anchored)
        if isinstance(node.func, ast.Attribute):
            return _is_anchored(node.func.value, anchored)
    return False


def _target_label(node: ast.AST) -> str:
    """A short human name for what a read is aimed at, for the report."""

    literal = _literal_repo_path(node)
    if literal:
        return literal
    parts: list[str] = []
    cursor: ast.AST | None = node
    while cursor is not None:
        if isinstance(cursor, ast.BinOp) and isinstance(cursor.op, ast.Div):
            right = cursor.right
            if isinstance(right, ast.Constant):
                parts.append(str(right.value))
            cursor = cursor.left
            continue
        if isinstance(cursor, ast.Call) and isinstance(cursor.func, ast.Attribute):
            cursor = cursor.func.value
            continue
        if isinstance(cursor, ast.Call) and cursor.args:
            cursor = cursor.args[0]
            continue
        if isinstance(cursor, ast.Attribute):
            cursor = cursor.value
            continue
        if isinstance(cursor, ast.Subscript):
            cursor = cursor.value
            continue
        if isinstance(cursor, ast.Name):
            parts.append(cursor.id)
        break
    return "/".join(reversed(parts)) or "<expr>"


def _module_anchors(tree: ast.Module) -> set[str]:
    """Module-level names bound to a repo path. Two passes: anchors of anchors."""

    anchored: set[str] = set()
    for _ in range(3):
        grew = False
        for node in tree.body:
            targets: list[ast.expr] = []
            value: ast.expr | None = None
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if value is None or not _is_anchored(value, anchored):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in anchored:
                    anchored.add(target.id)
                    grew = True
        if not grew:
            break
    return anchored


# --------------------------------------------------------------------------- #
# Does this function read the repository?
# --------------------------------------------------------------------------- #


@dataclass
class _Reads:
    targets: list[str]
    directory: bool


def _local_anchors(func: ast.AST, anchored: set[str]) -> set[str]:
    """Module anchors plus the names this function binds to a repo path."""

    names = set(anchored)
    for _ in range(3):
        grew = False
        for node in ast.walk(func):
            value: ast.expr | None = None
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets, value = list(node.targets), node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            elif isinstance(node, ast.NamedExpr):
                targets, value = [node.target], node.value
            if value is None or not _is_anchored(value, names):
                continue
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    grew = True
        if not grew:
            break
    return names


def _direct_reads(func: ast.AST, anchored: set[str]) -> _Reads:
    """The repo reads written inside this function body, ignoring what it calls."""

    names = _local_anchors(func, anchored)
    targets: list[str] = []
    directory = False
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute):
            if f.attr in _SOURCE_FUNCS:
                inner = _target_label(node.args[0]) if node.args else ""
                targets.append(f"inspect.{f.attr}({inner})")
                continue
            if f.attr == "walk" and node.args and _is_anchored(node.args[0], names):
                targets.append(_target_label(node.args[0]))
                directory = True
                continue
            if not _is_anchored(f.value, names):
                continue
            if f.attr in _WALK_ATTRS:
                targets.append(_target_label(f.value))
                directory = True
            elif f.attr in _READ_ATTRS:
                targets.append(_target_label(f.value))
        elif isinstance(f, ast.Name):
            if f.id in _SOURCE_FUNCS and node.args:
                targets.append(f"{f.id}({_target_label(node.args[0])})")
            elif f.id == "open" and node.args and _is_anchored(node.args[0], names):
                targets.append(_target_label(node.args[0]))
    return _Reads(sorted(set(targets)), directory)


def _called_names(func: ast.AST) -> set[str]:
    """The plain-NAME calls inside a function -- the helper hops to follow.

    Attribute calls (`obj.method()`) are deliberately not followed: the receiver
    is a runtime object this walk cannot resolve, and matching on the bare
    attribute name would attribute one module's reads to any function that
    happened to call a method of the same name.
    """

    out: set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            out.add(node.func.id)
    return out


def _referenced_names(func: ast.AST) -> set[str]:
    """Every name this function reads -- used to attribute a module-level read."""

    return {n.id for n in ast.walk(func) if isinstance(n, ast.Name)}


# --------------------------------------------------------------------------- #
# Does an assertion put a floor under the read?
# --------------------------------------------------------------------------- #


def _nonempty_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        value = node.value
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, (str, bytes)):
            return len(value) > 0
        return False
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return len(node.elts) > 0
    if isinstance(node, ast.Dict):
        return len(node.keys) > 0
    return False


def _numeric(node: ast.AST, consts: dict[str, float] | None = None) -> float | None:
    """The number this expression is, following module constants by name.

    `assert len(found) >= _KNOWN_STAGING_MODELS` is the canonical repaired
    shape in this repository, and the bound is a NAME. Reading only literals
    files 37 well-floored guards as quiet -- measured 2026-08-31 by disabling
    `_module_constants` and re-running the census (233 quiet instead of 196).
    """

    consts = consts or {}
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        if isinstance(node.value, bool):
            return None
        return float(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _numeric(node.operand, consts)
        return None if inner is None else -inner
    if isinstance(node, ast.Name):
        return consts.get(node.id)
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        if node.args and isinstance(node.args[0], ast.Name):
            return consts.get(f"len({node.args[0].id})")
        if node.args and _nonempty_literal(node.args[0]):
            return 1.0
    return None


def _module_constants(tree: ast.Module) -> dict[str, float]:
    """Module-level names bound to a number, and `len(NAME)` for sized literals."""

    out: dict[str, float] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        if value is None:
            continue
        for target in targets:
            if not isinstance(target, ast.Name):
                continue
            number = _numeric(value)
            if number is not None:
                out[target.id] = number
            sized = value
            if isinstance(sized, ast.Call) and sized.args:
                sized = sized.args[0]  # frozenset({...}), tuple([...])
            if isinstance(sized, (ast.List, ast.Tuple, ast.Set)):
                out[f"len({target.id})"] = float(len(sized.elts))
            elif isinstance(sized, ast.Dict):
                out[f"len({target.id})"] = float(len(sized.keys))
    return out


def _is_floor(node: ast.AST, consts: dict[str, float] | None = None) -> bool:
    """Would this expression be FALSE if the scan had found nothing?

    That single question is the whole classification. `assert found` is false on
    an empty list; `assert not offenders` is true on one.
    """

    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return False
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return any(_is_floor(v, consts) for v in node.values)
        return all(_is_floor(v, consts) for v in node.values)
    if isinstance(node, ast.Constant):
        return False
    if isinstance(node, (ast.Name, ast.Attribute, ast.Subscript, ast.ListComp, ast.GeneratorExp)):
        return True
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        return name not in _VACUOUS_CALLS
    if isinstance(node, ast.Compare):
        if len(node.ops) != 1:
            return False
        op, right = node.ops[0], node.comparators[0]
        left = node.left
        if isinstance(op, (ast.Gt, ast.GtE)):
            bound = _numeric(right, consts)
            if bound is not None:
                return bound >= 1 if isinstance(op, ast.GtE) else bound >= 0
            return False
        if isinstance(op, (ast.Lt, ast.LtE)):
            bound = _numeric(left, consts)
            if bound is not None:
                return bound >= 1 if isinstance(op, ast.LtE) else bound >= 0
            return False
        if isinstance(op, ast.Eq):
            return _nonempty_literal(right) or _nonempty_literal(left)
        if isinstance(op, ast.NotEq):
            # `x != []` is false on an empty scan; `x != 3` is not a floor.
            return not _nonempty_literal(right) and isinstance(
                right, (ast.List, ast.Tuple, ast.Set, ast.Dict)
            )
        if isinstance(op, ast.In):
            return True  # "literal" in <read content>: empty content fails it
        if isinstance(op, ast.IsNot):
            # `assert match is not None, "the CHECK moved"` -- the shape a guard
            # takes when what it read is a single object rather than a list. On
            # an empty read `re.search` returns None and the assertion is false,
            # so it is a floor. Added 2026-08-31 after the census filed two such
            # guards QUIET while both said, in their own message, what a missing
            # read means. `is None` stays a non-floor: it is TRUE on nothing.
            return isinstance(right, ast.Constant) and right.value is None
        return False
    return False


def _floor_source(lines: list[str], node: ast.AST) -> str:
    line = lines[node.lineno - 1].strip() if 0 < node.lineno <= len(lines) else ""
    return line[:110]


def _fail_on_empty(node: ast.If, consts: dict[str, float] | None = None) -> bool:
    """`if not found: pytest.fail(...)` -- a floor written as a branch."""

    test = node.test
    negated = isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not)
    zero = (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
        and _numeric(test.comparators[0], consts) == 0
    )
    if not (negated or zero):
        return False
    return any(isinstance(inner, (ast.Raise,)) or _is_fail_call(inner) for inner in node.body)


def _is_fail_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "fail"
    )


def _unconditional_floors(
    body: list[ast.stmt], lines: list[str], consts: dict[str, float] | None = None
) -> list[str]:
    """Floors that RUN on an empty scan.

    An assert inside `for item in scanned:` never executes when `scanned` is
    empty, so it is not a floor no matter what it says. Same for an `if` body
    and an except handler. That exclusion is the whole point: it is where the
    quiet guards hide.
    """

    found: list[str] = []
    for node in body:
        if isinstance(node, ast.Assert):
            if _is_floor(node.test, consts):
                found.append(_floor_source(lines, node))
        elif isinstance(node, ast.If):
            if _fail_on_empty(node, consts):
                found.append(_floor_source(lines, node))
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            found += _unconditional_floors(node.body, lines, consts)
        elif isinstance(node, ast.Try):
            found += _unconditional_floors(node.body, lines, consts)
        elif isinstance(node, ast.For) and _iterates_a_nonempty_literal(node.iter):
            # A `for` over a LITERAL, non-empty tuple/list runs its body at
            # least once, so a floor inside it is unconditional in fact. The
            # general loop exclusion stands -- a loop over a scanned collection
            # is exactly where quiet guards hide -- but a two-name literal
            # (test_no_revision_write_can_revive_a_discarded_draft, 2026-08-31)
            # is the guard's own fixed scope, not a scan result.
            found += _unconditional_floors(node.body, lines, consts)
    return found


def _iterates_a_nonempty_literal(node: ast.AST) -> bool:
    """TRUE only for `for x in ("a", "b")` shapes: a literal with >= 1 element."""
    return isinstance(node, (ast.Tuple, ast.List)) and len(node.elts) >= 1


def _has_assertion(func: ast.AST) -> bool:
    for node in ast.walk(func):
        if isinstance(node, ast.Assert) or _is_fail_call(node):
            return True
        if isinstance(node, ast.Raise):
            return True
    return False


# --------------------------------------------------------------------------- #
# The walk
# --------------------------------------------------------------------------- #


def _iter_python(root: Path):
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


@dataclass
class _Module:
    path: str
    tree: ast.Module
    lines: list[str]
    anchors: set[str]
    consts: dict[str, float]
    functions: dict[str, ast.AST]
    reads: dict[str, _Reads]
    imported: dict[str, str]  # local name -> "module::name"
    module_read: dict[str, _Reads]  # module constant -> the read that filled it


def _load(path: Path) -> _Module | None:
    text = path.read_text(encoding="utf-8", errors="replace")
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    rel = str(path.relative_to(REPO)).replace("\\", "/")
    anchors = _module_anchors(tree)
    consts = _module_constants(tree)
    functions: dict[str, ast.AST] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for inner in node.body:
                if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    functions[f"{node.name}.{inner.name}"] = inner
    reads = {name: _direct_reads(fn, anchors) for name, fn in functions.items()}

    imported: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                imported[alias.asname or alias.name] = f"{node.module}::{alias.name}"

    # A module-level read: `SOURCE = (REPO / "x").read_text()` at import time.
    # Attributed only to the tests that actually USE the bound name -- a
    # module constant nobody reads is not this test's coverage.
    module_read: dict[str, _Reads] = {}
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            got = _direct_reads(node.value, anchors)
            if not got.targets:
                continue
            bound = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in bound:
                if isinstance(target, ast.Name):
                    module_read[target.id] = got
    return _Module(
        path=rel,
        tree=tree,
        lines=text.splitlines(),
        anchors=anchors,
        consts=consts,
        functions=functions,
        reads=reads,
        imported=imported,
        module_read=module_read,
    )


def _reader_closure(modules: dict[str, _Module]) -> dict[str, _Reads]:
    """`{"path::func": what it reads}`, following helper calls to a fixed point.

    A test that calls `_route_modules()` reads whatever that helper reads. Two
    hops are resolved: same-module names, and `from ... import name` where the
    target module is also under `server/tests/`.
    """

    by_suffix: dict[str, list[str]] = {}
    for mod in modules.values():
        stem = mod.path[: -len(".py")].replace("/", ".")
        by_suffix.setdefault(stem, []).append(mod.path)

    def resolve_import(mod: _Module, name: str) -> tuple[str, str] | None:
        ref = mod.imported.get(name)
        if not ref:
            return None
        module_name, orig = ref.split("::", 1)
        tail = module_name.replace(".", "/")
        for candidate in modules:
            if candidate.endswith(f"{tail}.py"):
                return candidate, orig
        return None

    closure: dict[str, _Reads] = {}
    for mod in modules.values():
        for name, got in mod.reads.items():
            closure[f"{mod.path}::{name}"] = _Reads(list(got.targets), got.directory)

    for _ in range(4):
        grew = False
        for mod in modules.values():
            for name, fn in mod.functions.items():
                key = f"{mod.path}::{name}"
                current = closure[key]
                for called in _called_names(fn):
                    hops: list[str] = []
                    if called in mod.functions:
                        hops.append(f"{mod.path}::{called}")
                    resolved = resolve_import(mod, called)
                    if resolved:
                        hops.append(f"{resolved[0]}::{resolved[1]}")
                    for hop in hops:
                        other = closure.get(hop)
                        if not other or not other.targets:
                            continue
                        new = [t for t in other.targets if t not in current.targets]
                        if new or (other.directory and not current.directory):
                            current.targets = sorted(set(current.targets) | set(other.targets))
                            current.directory = current.directory or other.directory
                            grew = True
        if not grew:
            break
    return closure


@lru_cache(maxsize=4)
def census(root: Path = TESTS) -> list[Guard]:
    """Every source-reading guard under `root`, classified FLOORED or QUIET.

    Cached: the walk parses ~1 250 files, and the gate asks for it four times.
    Callers must not mutate the result -- it is the same list every time.
    """

    modules: dict[str, _Module] = {}
    for path in _iter_python(root):
        mod = _load(path)
        if mod is not None:
            modules[mod.path] = mod

    closure = _reader_closure(modules)
    guards: list[Guard] = []

    for mod in modules.values():
        name = Path(mod.path).name
        if not (name.startswith("test_") or name.endswith("_test.py")):
            continue
        for func_name, fn in sorted(mod.functions.items(), key=lambda kv: kv[1].lineno):
            short = func_name.split(".")[-1]
            if not short.startswith("test_"):
                continue
            reads = closure[f"{mod.path}::{func_name}"]
            targets = list(reads.targets)
            directory = reads.directory
            used = _referenced_names(fn)
            for constant, got in mod.module_read.items():
                if constant in used:
                    targets = sorted(set(targets) | set(got.targets))
                    directory = directory or got.directory
            if not targets:
                continue
            if not _has_assertion(fn):
                continue

            floors = _unconditional_floors(fn.body, mod.lines, mod.consts)
            # A floor may live in a same-module helper the test calls at the top
            # level -- `_assert_every_route_declared(found)`.
            for called in _called_names(fn):
                helper = mod.functions.get(called)
                if helper is not None and called != func_name:
                    floors += _unconditional_floors(helper.body, mod.lines, mod.consts)

            guards.append(
                Guard(
                    path=mod.path,
                    test=func_name,
                    line=fn.lineno,
                    verdict=FLOORED if floors else QUIET,
                    reads="directory" if directory else "file",
                    targets=targets,
                    floors=sorted(set(floors))[:2],
                )
            )
    return _credit_siblings(guards)


def _credit_siblings(guards: list[Guard]) -> list[Guard]:
    """A dedicated population test in the SAME module floors its neighbours.

    `test_every_staging_supersedes_on_pull_id.py` carries
    `test_the_population_is_whole` -- `assert len(found) >= 54` -- next to the
    offender test that says `assert not offenders`. That file cannot go quiet:
    a glob returning nothing reddens the population test and the reader is told
    the verdict below means nothing. Crediting it is not charity, it is the
    repair shape this lot recommends; refusing to see it would file the model
    answer as the defect.

    The credit is scoped: the floored sibling must read at least one of the same
    targets, so a floor over a JSON fixture does not vouch for a glob over
    `server/modules/`.
    """

    by_file: dict[str, list[Guard]] = {}
    for guard in guards:
        by_file.setdefault(guard.path, []).append(guard)

    for file_guards in by_file.values():
        floored = [g for g in file_guards if g.verdict == FLOORED]
        if not floored:
            continue
        for guard in file_guards:
            if guard.verdict == FLOORED:
                continue
            vouchers = [g for g in floored if set(g.targets) & set(guard.targets)]
            if vouchers:
                guard.verdict = FLOORED
                voucher = vouchers[0]
                guard.floors = [
                    f"sibling {voucher.test}:{voucher.line} floors the same read"
                ]
    for guard in guards:
        guard.targets = guard.targets[:4]
    return guards


# --------------------------------------------------------------------------- #
# The ratchet
# --------------------------------------------------------------------------- #

#: The QUIET guards measured on 2026-08-31, the day criterion 13's instrument
#: was written. The list turns ONE WAY: a guard that gains a floor must be
#: deleted from it, and a NEW quiet guard is refused at the moment it is written
#: -- the only moment adding one line to it is free.
#:
#: These are not 196 bugs. They are 196 tests that would survive their target
#: moving, ranked by nothing. The ten repaired in the same lot were chosen by
#: blast radius: a guard over a DIRECTORY of modules loses everything at once,
#: a guard over one fixture loses one file.
#:
#:     196  2026-08-31 02:24  mesure d'origine (b00a715e)
#:     195  2026-08-31        `test_ai_path_visual_family::test_the_migration_
#:                            check_mirrors_the_spec_selectable_families_exactly`
#:                            RETIRE : il portait `assert match is not None,
#:                            "the family CHECK moved"` depuis toujours -- le
#:                            classifieur, pas le garde, etait en tort. La
#:                            reparation du classifieur le rend FLOORED, donc sa
#:                            ligne part le meme jour, comme le cliquet l'exige.
QUIET_GUARDS_AT_2026_08_31: frozenset[str] = frozenset(
    {
        "server/tests/conformance/test_bundle.py::test_bundle_artifact",
        "server/tests/conformance/test_comparison_preconditions_have_one_owner.py::test_the_console_phrases_no_comparison_refusal_of_its_own",
        "server/tests/conformance/test_comparison_preconditions_have_one_owner.py::test_the_refusing_function_carries_no_sentence_of_its_own",
        "server/tests/conformance/test_comparison_preconditions_have_one_owner.py::test_the_screen_tests_exactly_the_conditions_the_validator_declares",
        "server/tests/conformance/test_datastream_progress_estimate_surface.py::test_no_screen_of_the_console_computes_a_remaining_duration",
        "server/tests/conformance/test_datastream_readers_carry_project_scope.py::test_the_mcp_guard_vocabulary_knows_every_access_decision_helper",
                "server/tests/conformance/test_dq_monitor_registry.py::test_every_check_function_has_a_registry_entry_and_the_reverse",
        "server/tests/conformance/test_dq_monitor_registry.py::test_every_dispatched_check_answers_a_verdict_not_a_boolean",
        "server/tests/conformance/test_dq_monitor_registry.py::test_no_new_test_pins_a_french_word_from_a_refusal",
        "server/tests/conformance/test_dq_monitor_registry.py::test_the_datastream_dossier_names_every_monitor_and_no_absent_table",
        "server/tests/conformance/test_dq_monitor_registry.py::test_the_glossary_names_every_monitor",
        "server/tests/conformance/test_epic41_additive_only.py::test_epic41_models_are_views",
        "server/tests/conformance/test_epic41_additive_only.py::test_epic41_models_depend_only_on_the_allowed_set",
        "server/tests/conformance/test_epic41_additive_only.py::test_existing_yaml_untouched_by_epic41",
        "server/tests/conformance/test_epic41_additive_only.py::test_no_pre_existing_node_depends_on_epic41",
        "server/tests/conformance/test_epic41_revenue_additive_only.py::test_ad4_no_ratio_is_ever_written_as_a_fact",
        "server/tests/conformance/test_epic41_revenue_additive_only.py::test_epic41_revenue_models_are_views",
        "server/tests/conformance/test_epic41_revenue_additive_only.py::test_epic41_revenue_models_depend_only_on_the_allowed_set",
        "server/tests/conformance/test_epic41_revenue_additive_only.py::test_no_pre_existing_node_depends_on_epic41_revenue",
        "server/tests/conformance/test_epic41_revenue_additive_only.py::test_shared_yaml_carries_no_41_5_entry",
        "server/tests/conformance/test_epic41_verification_overlay_additive.py::test_41_3_seeder_carries_no_41_4_fixture",
        "server/tests/conformance/test_epic41_verification_overlay_additive.py::test_forbidden_files_are_unchanged_from_head",
            "server/tests/conformance/test_epic41_verification_overlay_additive.py::test_shared_config_files_do_not_mention_the_overlay",
        "server/tests/conformance/test_execution_state_registry.py::test_every_state_has_a_phase_in_both_languages",
        "server/tests/conformance/test_execution_state_registry.py::test_no_python_reader_keeps_its_own_copy_of_the_state_list",
        "server/tests/conformance/test_execution_state_registry.py::test_the_active_index_predicate_is_exactly_the_active_states",
        "server/tests/conformance/test_execution_state_registry.py::test_the_console_mirror_matches_the_registry_entry_for_entry",
        "server/tests/conformance/test_field_compatibility_schema.py::test_declared_blocks_load_through_the_core_loader_hook",
        "server/tests/conformance/test_field_compatibility_schema.py::test_declaring_modules_smoke",
        "server/tests/conformance/test_field_compatibility_schema.py::test_every_declared_block_validates",
        "server/tests/conformance/test_landing_vocabulary_has_one_owner.py::test_every_declared_landing_is_in_the_vocabulary",
        "server/tests/conformance/test_landing_vocabulary_has_one_owner.py::test_the_function_reads_the_schema_rather_than_repeating_it",
        "server/tests/conformance/test_language_binding_catalog.py::test_the_cited_evidence_is_the_provider_text_verbatim",
        "server/tests/conformance/test_mcp_tools_resolve_project_scope.py::test_the_guard_vocabulary_knows_every_access_decision_helper",
        "server/tests/conformance/test_metric_name_map_is_current.py::test_the_seed_matches_what_the_manifests_declare",
        "server/tests/conformance/test_migration_existence_guards.py::test_no_migration_guards_on_a_table_that_a_later_migration_creates",
        "server/tests/conformance/test_migration_existence_guards.py::test_the_allowlist_names_only_real_closed_defects",
        "server/tests/conformance/test_migration_existence_guards.py::test_the_scanner_can_name_every_table_the_migrations_create",
        "server/tests/conformance/test_no_alignment_specific_mcp_tools.py::test_no_alignment_specific_mcp_tool_is_registered_anywhere",
        "server/tests/conformance/test_no_geographic_hardcode.py::test_no_default_market_selection_ships_as_a_platform_constant",
        "server/tests/conformance/test_no_geographic_hardcode.py::test_no_iso_code_tuple_masquerades_as_a_tracked_set",
        "server/tests/conformance/test_no_geographic_hardcode.py::test_no_migration_seeds_a_market_or_a_client_alias",
        "server/tests/conformance/test_no_geographic_hardcode.py::test_no_module_level_market_composition_ships_in_code",
        "server/tests/conformance/test_no_geographic_hardcode.py::test_the_shared_seed_carries_only_the_legal_iso_set",
        "server/tests/conformance/test_no_placeholder_in_sql_comments.py::test_no_placeholder_hides_in_a_sql_comment",
        "server/tests/conformance/test_no_tax_specific_mcp_tools.py::test_no_tax_specific_mcp_tool_is_registered_anywhere",
        "server/tests/conformance/test_one_rule_authority_per_family.py::test_the_retired_store_table_names_relations_a_migration_actually_retired",
        "server/tests/conformance/test_product_vocabulary.py::test_no_mcp_tool_or_parameter_names_a_module",
        "server/tests/conformance/test_published_docs.py::test_every_command_a_published_page_gives_can_actually_be_run",
        "server/tests/conformance/test_published_docs.py::test_no_published_page_cites_a_module_directory_that_does_not_exist",
        "server/tests/conformance/test_published_docs.py::test_no_published_page_cites_an_internal_decision_code",
        "server/tests/conformance/test_published_docs.py::test_the_mintlify_navigation_and_the_files_agree",
        "server/tests/conformance/test_published_docs.py::test_the_retired_product_noun_stays_retired_in_published_copy",
        "server/tests/conformance/test_pull_job_state_registry.py::test_the_active_index_predicate_is_exactly_the_active_states",
        "server/tests/conformance/test_refusals_name_a_gesture.py::test_no_refusal_of_a_covered_module_hands_back_a_table_name",
        "server/tests/conformance/test_reports.py::test_get_report_returns_valid_envelope",
        "server/tests/conformance/test_reports.py::test_reports_validate_against_schema",
        "server/tests/conformance/test_superseded_concepts.py::test_a_superseded_concept_never_appears_without_its_replacement",
        "server/tests/conformance/test_workbench_advertises_only_mounted_routes.py::test_the_data_tab_never_claims_a_sample_endpoint_that_is_not_mounted",
        "server/tests/core/test_ai_path_emission.py::test_no_migration_carries_the_streaming_vocabulary",
        "server/tests/core/test_ai_path_visual_family.py::test_the_declaration_added_no_migration_of_its_own",
        "server/tests/core/test_analyze_artifacts_seam.py::test_no_route_can_create_a_share_or_return_a_bearer",
        "server/tests/core/test_asof_query.py::TestWarehouseGetDailyReportAsof.test_no_python_max_or_sorted_in_asof_code",
        "server/tests/core/test_async_reports.py::TestNoProviderVocabulary.test_core_file_has_no_provider_names",
        "server/tests/core/test_bounded_recovery.py::test_module_source_never_calls_commit_publication",
        "server/tests/core/test_business_graph_projection_targets.py::test_the_browser_can_draw_every_node_type_the_projection_emits",
        "server/tests/core/test_business_link_target_types.py::test_no_screen_reinvents_the_business_target_list",
        "server/tests/core/test_business_link_target_types.py::test_the_browser_can_name_every_target_type_the_server_accepts",
        "server/tests/core/test_candidate_fate.py::test_fate_emitters_do_not_retype_a_fate_literal",
        "server/tests/core/test_candidate_fate.py::test_no_second_site_retypes_a_reason_literal",
        "server/tests/core/test_card_blocks.py::test_no_platform_cpa_objective_is_assigned_in_the_module",
        "server/tests/core/test_conflict_resolutions.py::TestAD6Invariant.test_no_fx_conversion_in_conflict_resolutions_api",
        "server/tests/core/test_conflict_resolutions.py::TestUpsertFxResolution.test_no_fx_conversion_in_upsert",
        "server/tests/core/test_context_event_route.py::test_the_schema_enum_and_the_module_agree",
        "server/tests/core/test_context_hub_completeness.py::test_0_business_domains_lead_the_render_order",
        "server/tests/core/test_criteria_parser.py::test_every_corpus_document_has_a_hand_written_answer",
        "server/tests/core/test_criteria_parser.py::test_the_walk_returns_what_a_reader_reads",
        "server/tests/core/test_currency_refusal.py::test_ad2_no_provider_names_in_engine",
            "server/tests/core/test_datastream_dispatch.py::test_the_dispatchers_project_every_column_the_gates_read",
            "server/tests/core/test_datastream_progress.py::test_no_execution_state_is_typed_in_this_module",
            "server/tests/core/test_db_context_manager_usage.py::test_no_discarded_connection_context_manager",
        "server/tests/core/test_entity_context.py::test_no_object_kind_literal_appears_in_the_doors",
        "server/tests/core/test_entry_confirmations.py::test_every_command_the_code_issues_is_in_the_database_vocabulary",
        "server/tests/core/test_epic36_admin_seam.py::test_every_caller_of_the_scoped_resolver_acquires_an_armed_connection",
        "server/tests/core/test_epic38_import_templates.py::test_template_immutability_offline_no_update_or_delete",
        "server/tests/core/test_epic39_validation.py::test_ad2_no_new_core_module_and_no_provider_vocabulary_here",
        "server/tests/core/test_evidence_inspections.py::test_the_summary_reads_and_never_writes",
        "server/tests/core/test_execution_stop.py::test_it_does_not_commit_and_says_so",
        "server/tests/core/test_fee_tax_auto_population.py::test_47_no_python_rate_map_ships_in_the_loader_module",
        "server/tests/core/test_fee_tax_auto_population.py::test_64_every_write_goes_through_the_audited_store",
        "server/tests/core/test_fee_tax_mcp.py::test_no_tool_here_declares_the_obsolete_write_effect",
        "server/tests/core/test_fee_tax_mcp.py::test_the_legacy_tax_commands_are_not_mounted",
        "server/tests/core/test_fee_tax_mcp.py::test_the_matcher_refusal_codes_are_not_reimplemented_here",
        "server/tests/core/test_fee_tax_mcp.py::test_the_module_never_imports_the_superseded_auto_population",
        "server/tests/core/test_fee_tax_rules.py::test_20_every_mirror_table_is_declared_as_a_dbt_source",
        "server/tests/core/test_fee_tax_rules.py::test_20e_the_mirror_never_opts_into_rls_enforcement",
        "server/tests/core/test_fee_tax_rules.py::test_23b_nothing_gates_on_the_retired_activation_flag_any_more",
        "server/tests/core/test_feedback_review.py::test_a_single_writer_owns_the_annotation_row",
        "server/tests/core/test_field_compat.py::TestNoProviderVocabulary.test_core_file_has_no_provider_names",
                "server/tests/core/test_fx_frankfurter.py::test_fx_helper_stays_frankfurter_free",
        "server/tests/core/test_fx_helper.py::test_ad2_no_provider_vocabulary_in_source",
        "server/tests/core/test_inbound_raw_imports.py::test_migration_185_enforces_cross_tenant_provenance_and_lifecycle",
        "server/tests/core/test_inbound_reprocess_scope.py::test_the_scope_execution_never_writes_to_the_retained_evidence",
        "server/tests/core/test_inbound_routing_test.py::test_it_writes_nothing",
        "server/tests/core/test_issued_confirmations_are_committed.py::test_every_issued_confirmation_is_committed_before_its_secret_is_returned",
        "server/tests/core/test_linkedin_ads_connector.py::test_golden_pull_through_parse_and_transform",
        "server/tests/core/test_mapping_writes_are_governed.py::test_no_new_module_appends_a_mapping_version_outside_the_governed_seam",
        "server/tests/core/test_mapping_writes_are_governed.py::test_the_orphaned_admin_handler_still_has_no_route",
        "server/tests/core/test_merge_bound_says_only_what_it_read.py::test_the_unreadable_sentence_carries_no_canonical_identifier",
        "server/tests/core/test_metric_procedures_scope.py::test_no_reader_of_the_retired_store_came_back",
        "server/tests/core/test_metric_semantics_monetary.py::test_ad2_no_provider_name_in_classifier_constants",
        "server/tests/core/test_metric_semantics_monetary.py::test_classifier_partition_over_real_seed",
        "server/tests/core/test_metric_semantics_monetary.py::test_invariant3_real_seed_stripped",
                "server/tests/core/test_nightly_step_ledger.py::test_every_call_site_passes_the_ledger",
        "server/tests/core/test_nightly_step_ledger.py::test_the_declared_sequence_is_the_one_that_is_called",
        "server/tests/core/test_nightly_step_ledger.py::test_the_hourly_steps_do_not_write_the_nightly_ledger",
        "server/tests/core/test_notebooks.py::TestSaveNotebook.test_the_legacy_table_is_never_named_by_this_module_again",
        "server/tests/core/test_onboarding_currency_provenance.py::test_every_project_creation_door_reads_the_suggestion_the_same_way",
        "server/tests/core/test_one_state_machine_for_a_run.py::test_the_audit_metadata_may_not_restate_what_the_row_did",
        "server/tests/core/test_pg_gated_fixtures_declare_their_role.py::test_the_repaired_ddl_fixtures_keep_declaring_pg_owner",
        "server/tests/core/test_platform_clock_declarations.py::test_the_registry_and_the_provisioner_declare_the_same_clocks",
        "server/tests/core/test_platform_clocks.py::test_no_deployment_identifier_is_hardcoded_in_the_module",
        "server/tests/core/test_platform_clocks.py::test_the_job_prefix_environment_variable_is_gone_from_the_source",
        "server/tests/core/test_platform_clocks.py::test_the_module_does_not_touch_the_datastream_cadence",
        "server/tests/core/test_project_capabilities_mcp.py::test_the_obsolete_generic_write_effect_is_gone_from_the_whole_catalog",
        "server/tests/core/test_project_resolver_gate.py::test_no_inline_default_autobind_in_tool_paths",
        "server/tests/core/test_project_resolver_gate.py::test_project_scoped_tools_call_resolver",
        "server/tests/core/test_publication_writes_no_consumer.py::test_delivery_is_an_output_kind_and_never_a_consumer_this_product_serves",
        "server/tests/core/test_pull_errors.py::TestNoProviderVocabulary.test_core_file_has_no_provider_names",
        "server/tests/core/test_queue_invariant.py::test_no_url_literals_in_core",
            "server/tests/core/test_queue_records_execution_progress.py::test_the_worker_records_then_closes_in_that_order",
        "server/tests/core/test_refetch.py::TestNoProviderVocabulary.test_core_file_has_no_provider_names",
        "server/tests/core/test_render_app_payload.py::test_fastmcp_golden_traverses_the_real_share_composer_without_semantic_drift",
            "server/tests/core/test_render_shares_api.py::test_the_console_listing_never_returns_a_delivery_url",
        "server/tests/core/test_row_json_no_naked_row_response.py::test_no_database_row_is_rendered_by_JSONResponse",
        "server/tests/core/test_schema_context_gen.py::test_ad17_no_rest_route_writes_schema_context",
        "server/tests/core/test_self_hosted_instance_claim.py::test_hosted_never_counts_instance_organizations",
        "server/tests/core/test_semantic_model_mcp_door.py::test_the_door_holds_no_private_name_and_no_semantic_sql",
        "server/tests/core/test_sprint_status_shape.py::test_the_real_tracker_is_clean",
        "server/tests/core/test_timezone_signal.py::test_ad2_no_provider_names_in_engine",
        "server/tests/core/test_trace_observation.py::test_no_code_path_in_this_story_writes_a_golden_question_table",
        "server/tests/core/test_trace_observation.py::test_this_story_writes_none_of_the_tables_it_reads",
        "server/tests/core/test_tracked_entity_isolation.py::test_no_production_module_imports_the_replaced_authorities",
        "server/tests/core/test_value_mapping_precedence.py::test_only_their_owners_name_the_two_stores",
        "server/tests/core/test_verification.py::TestComputeExpectedRows.test_ga4_real_manifest_verification_rows_per_day",
        "server/tests/core/test_visualization_compatibility.py::test_no_source_file_derives_a_role_from_a_member_name",
        "server/tests/core/test_visualization_options_labels.py::test_every_mint_in_the_product_was_followed_to_a_literal_prefix",
        "server/tests/evals/test_corpus_determinism.py::test_two_run_determinism",
        "server/tests/evals/test_reference_sql_green.py::test_reference_sql_green_on_seeds",
        "server/tests/integration/test_ai_path_family_widget.py::test_the_family_source_never_names_a_second_tool_or_resource",
        "server/tests/integration/test_ai_paths_api_seam.py::test_ac8_the_ai_path_surface_owns_no_feedback_write",
        "server/tests/integration/test_evidence_index_isolation.py::test_no_evidence_mutation_reaches_mcp_or_the_console",
        "server/tests/integration/test_global_scope_api_seams.py::test_production_code_has_no_default_open_project_authority_or_obsolete_route",
        "server/tests/integration/test_module_loading.py::test_core_is_source_agnostic",
        "server/tests/integration/test_platform_clocks_api_seam.py::test_no_read_handler_can_reach_a_write_seam",
        "server/tests/integration/test_project_settings_constraints.py::test_legacy_settings_surface_and_routes_are_removed",
        "server/tests/integration/test_project_settings_constraints.py::test_project_crud_no_longer_reads_or_writes_legacy_default_columns",
        "server/tests/isolation/test_warehouse_org_isolation.py::test_ad8_dbt_naming_macros_never_touch_mirror",
        "server/tests/isolation/test_warehouse_org_isolation.py::test_ad8_warehouse_write_never_touches_mirror",
        "server/tests/modules/google_analytics/test_catalog_daily_ga4.py::test_catalog_daily_dispatch_resolves_pull_catalog_daily",
        "server/tests/modules/gsc/test_country_vocabulary.py::test_gsc_alpha3_country_resolves_on_the_sql_path",
        "server/tests/modules/hubspot/test_connector.py::test_api_catalog_excluded_fields_have_reason",
        "server/tests/modules/hubspot/test_connector.py::test_api_catalog_excluded_sections_all_excluded",
        "server/tests/modules/hubspot/test_connector.py::test_api_catalog_exposed_sections_not_excluded",
        "server/tests/modules/hubspot/test_connector.py::test_api_catalog_zero_planned_fields",
        "server/tests/modules/hubspot/test_connector.py::test_golden_snapshot_contacts",
        "server/tests/modules/hubspot/test_connector.py::test_golden_snapshot_deals",
        "server/tests/modules/hubspot/test_connector.py::test_golden_snapshot_expected_facts",
        "server/tests/modules/hubspot/test_hardening_hubspot.py::test_default_pull_does_not_log_selected_contact_or_deal_properties",
        "server/tests/modules/linkedin_ads/test_catalog_daily_linkedin.py::test_catalog_daily_dispatch_resolves_pull_catalog_daily",
        "server/tests/modules/meta_ads/test_catalog_daily_meta.py::test_catalog_daily_dispatch_resolves_pull_catalog_daily",
        "server/tests/modules/microsoft_ads/test_pull_microsoft_ads.py::test_transform_matches_golden_fixture",
        "server/tests/modules/shopify/test_pull_shopify.py::test_transform_renames_source_fields_to_canonical",
        "server/tests/modules/square/test_pull_square.py::test_transform_golden_matches_expected",
        "server/tests/modules/stripe/test_pull_stripe.py::test_golden_pull_through_parse_and_transform",
        "server/tests/modules/taboola/test_connector.py::test_no_environment_fallback_for_the_account",
        "server/tests/modules/thetradedesk/test_dispatch_thetradedesk.py::test_every_selectable_report_callable_binds_the_queue_contract",
        "server/tests/modules/thetradedesk/test_dispatch_thetradedesk.py::test_profile_specs_map_to_managed_templates",
            "server/tests/modules/thetradedesk/test_transform_thetradedesk.py::test_transform_maps_golden_pull_to_expected_facts",
        "server/tests/modules/tiktok_ads/test_pull_tiktok.py::test_golden_pull_through_parse_and_transform",
        "server/tests/modules/woocommerce/test_pull_woocommerce.py::test_transform_matches_expected_facts",
        "server/tests/tooling/test_catalog_gen.py::TestMergeSchemaValidity.test_schema_valid_full_fixture",
    }
)


def _gate(guards: list[Guard]) -> int:
    quiet = {g.identity for g in guards if g.verdict == QUIET}
    baseline = QUIET_GUARDS_AT_2026_08_31
    new = sorted(quiet - baseline)
    gone = sorted(baseline - quiet)

    print(f"source-reading guards: {len(guards)}")
    print(f"  floored: {sum(1 for g in guards if g.verdict == FLOORED)}")
    print(f"  quiet:   {len(quiet)}   (baselined on 2026-08-31: {len(baseline)})")

    if new:
        print(f"\nREFUSED: {len(new)} NEW guard(s) that pass on an empty read.")
        print("Add the floor that names the guard's own coverage, e.g.")
        print('  assert len(scanned) >= N, "the sources moved; this guard scanned none"')
        for identity in new:
            print(f"  {identity}")
    if gone:
        print(f"\nREFUSED: {len(gone)} baselined quiet guard(s) no longer exist.")
        print("The ratchet turns one way: delete these from")
        print("QUIET_GUARDS_AT_2026_08_31 in scripts/quiet_guard_census.py.")
        for identity in gone:
            print(f"  {identity}")
    if new or gone:
        return 1
    print("\nOK: no new quiet guard, no stale line in the baseline.")
    return 0


def _report(guards: list[Guard], quiet_only: bool) -> None:
    shown = [g for g in guards if g.verdict == QUIET] if quiet_only else guards
    current = ""
    for guard in shown:
        if guard.path != current:
            current = guard.path
            print(f"\n{current}")
        mark = "QUIET  " if guard.verdict == QUIET else "floored"
        targets = ", ".join(guard.targets) or "-"
        print(f"  {mark} {guard.test}:{guard.line}  [{guard.reads}] {targets}")
        for floor in guard.floors:
            print(f"           floor: {floor}")

    total = len(guards)
    quiet = sum(1 for g in guards if g.verdict == QUIET)
    directories = sum(1 for g in guards if g.verdict == QUIET and g.reads == "directory")
    print("\n" + "-" * 68)
    print(f"source-reading guards : {total}")
    print(f"  floored             : {total - quiet}")
    print(f"  QUIET               : {quiet}   ({directories} of them read a DIRECTORY)")
    print("A QUIET guard is green when its target moves. See criterion 13 of")
    print("docs/product-architecture/module-boundaries.md.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable")
    parser.add_argument("--quiet-only", action="store_true", help="only the mute guards")
    parser.add_argument("--gate", action="store_true", help="refuse a NEW quiet guard")
    parser.add_argument("--baseline", action="store_true", help="print today's quiet set")
    args = parser.parse_args()

    guards = census()
    if args.gate:
        return _gate(guards)
    if args.baseline:
        for identity in sorted(g.identity for g in guards if g.verdict == QUIET):
            print(f'        "{identity}",')
        return 0
    if args.json:
        print(json.dumps([asdict(g) for g in guards], indent=2))
        return 0
    _report(guards, args.quiet_only)
    return 0


if __name__ == "__main__":
    sys.exit(main())
