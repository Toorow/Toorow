"""Every identifier prefix the product mints, FOLLOWED TO ITS LITERAL.

WHY THIS IS NOT A REGEX ANY MORE. The 2026-08-21 derivation was one line --
an f-string pattern anchored on ``f"<prefix>_{ULID()}"`` -- and it read exactly
ONE of the two shapes this repository mints with. The other shape is a helper::

    def _mint(prefix: str) -> str:          # master_data.py:156
        return f"{prefix}_{ULID()}"

    node_id = _mint("mdnode")               # master_data.py:701

The prefix of ``mdnode_01KZ...`` is a literal; it is simply a literal at the
CALL SITE rather than inside the f-string. A pattern anchored on the f-string
cannot see it, so whole families of the product were absent from a set whose
docstring said it was derived from the mints. That is the defect the derivation
was written to close, reappearing one level down: the enumeration was no longer
a typed list, it was a typed SHAPE.

So the walk is an AST walk, and it follows the prefix through the hops that
actually exist here:

  1. the literal leads the f-string           -- ``f"pull_{ULID()}"``
  2. the literal is a module-level constant   -- ``f"{HANDLE_PREFIX}{ULID()}"``
  3. the literal is the call-site argument of a minting helper
                                              -- ``_mint("mdnode")``
  4. the literal reaches that argument through a module-level table or a
     declared field -- ``_mint_id(_ID_PREFIXES["metric_grain_breakdowns"])``,
     ``_mint_id(ledger.id_prefix)`` where a ledger declares ``id_prefix="crlv_"``

AND THE FIFTH HOP DOES NOT EXIST: anything this walk cannot follow is returned
as an UNRESOLVED site, with its file and its line, so a mint written in a shape
nobody anticipated FAILS the exhaustiveness test instead of quietly shrinking
the set. An instrument that cannot prove its own coverage is an enumeration in
disguise; `unresolved_mint_sites` is how this one proves it.

SCOPE: the product's own code -- `server/`, minus `server/tests/`. Test
fixtures mint throwaway prefixes (``a_``, ``k_``, ``t206-real_``) that name no
family of the product, and admitting them widens every rule built on this set.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

#: `server/`, from `server/tests/support/`.
SERVER = Path(__file__).resolve().parents[2]

#: Directories whose mints name no family of the product. `tests/` mints
#: fixtures; the eval and fixture generators live under it.
_EXCLUDED = (SERVER / "tests",)


@dataclass(frozen=True)
class MintSite:
    """One expression that builds a prefixed identifier, and what it resolved to."""

    path: Path
    lineno: int
    form: str
    source: str
    prefixes: frozenset[str]

    @property
    def where(self) -> str:
        return f"{self.path.relative_to(SERVER).as_posix()}:{self.lineno}"


def _source(path: Path) -> str:
    # Two suites in this tree carry a UTF-8 BOM; `utf-8` keeps it and
    # `ast.parse` refuses the character. Neither is a reason to skip a file.
    return path.read_text(encoding="utf-8-sig", errors="ignore")


def _product_modules() -> list[Path]:
    return [
        path
        for path in sorted(SERVER.rglob("*.py"))
        if not any(excluded in path.parents for excluded in _EXCLUDED)
    ]


def _mentions_ulid(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name) and sub.id == "ULID":
            return True
        if isinstance(sub, ast.Attribute) and sub.attr == "ULID":
            return True
    return False


def _leading_prefix(text: str) -> str | None:
    """``"mgd_"`` and ``"pull_"`` name a family; ``"_"`` and ``""`` name none."""
    head = text.split("_", 1)[0]
    return head or None


def _string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` -- hop 2."""
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        found[target.id] = node.value.value
    return found


def _string_tables(tree: ast.Module) -> dict[str, dict[str, str]]:
    """Module-level ``NAME = {"key": "literal"}`` -- hop 4, the table form."""
    found: dict[str, dict[str, str]] = {}
    for node in tree.body:
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict)):
            continue
        table = {
            key.value: value.value
            for key, value in zip(node.value.keys, node.value.values, strict=True)
            if isinstance(key, ast.Constant)
            and isinstance(key.value, str)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, str)
        }
        for target in node.targets:
            if isinstance(target, ast.Name):
                found[target.id] = table
    return found


def _field_literals(tree: ast.Module, field: str) -> set[str]:
    """Every string literal this module writes into ``field=`` -- hop 4, the field form.

    ``rule_versions.py`` declares its ledgers as dataclasses carrying
    ``id_prefix="crlv_"``, and mints with ``_mint_id(ledger.id_prefix)``. The
    literal is right there, one declaration away; refusing to walk to it would
    lose two families to a shape the repository uses on purpose.
    """
    literals: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == field:
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                literals.add(node.value.value)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == field and isinstance(node.value, ast.Constant):
                if isinstance(node.value.value, str):
                    literals.add(node.value.value)
    return literals


def _parents(tree: ast.Module) -> dict[ast.AST, ast.AST]:
    links: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            links[child] = node
    return links


def _enclosing_function(node: ast.AST, links: dict[ast.AST, ast.AST]):
    current = links.get(node)
    while current is not None and not isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef)):
        current = links.get(current)
    return current


def _module_path(module: str) -> Path | None:
    """``core.data_identities`` -> ``server/core/data_identities.py``, when it exists."""
    candidate = SERVER.joinpath(*module.split("."))
    if candidate.with_suffix(".py").exists():
        return candidate.with_suffix(".py")
    if (candidate / "__init__.py").exists():
        return candidate / "__init__.py"
    return None


def _imported_names(tree: ast.Module) -> dict[str, Path]:
    """``from core.x import mint_data_id`` -> ``{"mint_data_id": server/core/x.py}``.

    Scoping the helper to the module that DEFINES it is what keeps a
    same-named ``_mint()`` in a neighbouring file from being read as a mint.
    """
    bound: dict[str, Path] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            origin = _module_path(node.module)
            if origin is None:
                continue
            for alias in node.names:
                bound[alias.asname or alias.name] = origin
    return bound


def _resolve(
    argument: ast.expr | None,
    constants: dict[str, str],
    tables: dict[str, dict[str, str]],
    tree: ast.Module,
) -> set[str]:
    """Walk one call-site argument down to the literals it can carry."""
    if argument is None:
        return set()
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        prefix = _leading_prefix(argument.value)
        return {prefix} if prefix else set()
    if isinstance(argument, ast.Name) and argument.id in constants:
        prefix = _leading_prefix(constants[argument.id])
        return {prefix} if prefix else set()
    if isinstance(argument, ast.Subscript) and isinstance(argument.value, ast.Name):
        table = tables.get(argument.value.id)
        if table is not None:
            key = argument.slice
            if isinstance(key, ast.Constant) and isinstance(key.value, str):
                prefix = _leading_prefix(table.get(key.value, ""))
                return {prefix} if prefix else set()
            return {p for p in (_leading_prefix(v) for v in table.values()) if p}
    if isinstance(argument, ast.Attribute):
        return {p for p in (_leading_prefix(v) for v in _field_literals(tree, argument.attr)) if p}
    return set()


@lru_cache(maxsize=1)
def _analysis() -> tuple[tuple[MintSite, ...], frozenset[str]]:
    trees: dict[Path, ast.Module] = {}
    for path in _product_modules():
        try:
            trees[path] = ast.parse(_source(path))
        except SyntaxError:  # pragma: no cover -- a module that does not parse mints nothing
            continue

    sites: list[MintSite] = []
    #: (defining module, function name) -> the parameter carrying the prefix.
    helpers: dict[tuple[Path, str], str] = {}
    parametric: list[tuple[Path, ast.JoinedStr, str]] = []

    for path, tree in trees.items():
        constants = _string_constants(tree)
        links = _parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.JoinedStr) and _mentions_ulid(node)):
                continue
            if not node.values:  # pragma: no cover -- an empty f-string mints nothing
                continue
            head = node.values[0]
            source = ast.unparse(node)
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                prefix = _leading_prefix(head.value)
                if prefix:
                    sites.append(
                        MintSite(path, node.lineno, "literal", source, frozenset({prefix}))
                    )
                    continue
            if isinstance(head, ast.FormattedValue) and isinstance(head.value, ast.Name):
                name = head.value.id
                if name in constants:  # hop 2
                    prefix = _leading_prefix(constants[name])
                    if prefix:
                        sites.append(
                            MintSite(path, node.lineno, "constant", source, frozenset({prefix}))
                        )
                        continue
                function = _enclosing_function(node, links)
                parameters = (
                    {
                        argument.arg
                        for argument in [
                            *function.args.posonlyargs,
                            *function.args.args,
                            *function.args.kwonlyargs,
                        ]
                    }
                    if function is not None
                    else set()
                )
                if name in parameters:
                    helpers[(path, function.name)] = name
                    parametric.append((path, node, function.name))
                    continue
            sites.append(MintSite(path, node.lineno, "unresolved", source, frozenset()))

    helper_names = {name for _path, name in helpers}
    by_helper: dict[tuple[Path, str], set[str]] = {key: set() for key in helpers}
    unresolved_calls: list[MintSite] = []

    for path, tree in trees.items():
        constants = _string_constants(tree)
        tables = _string_tables(tree)
        imported = _imported_names(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name):
                name = node.func.id
            elif isinstance(node.func, ast.Attribute):
                name = node.func.attr
            else:
                continue
            if name not in helper_names:
                continue
            key = (imported.get(name, path), name)
            if key not in helpers:
                continue  # a same-named function that mints nothing
            parameter = helpers[key]
            argument: ast.expr | None = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg == parameter:
                    argument = keyword.value
            literals = _resolve(argument, constants, tables, tree)
            if literals:
                by_helper[key].update(literals)
            else:
                unresolved_calls.append(
                    MintSite(path, node.lineno, "unresolved-call", ast.unparse(node), frozenset())
                )

    for path, node, function_name in parametric:
        resolved = by_helper.get((path, function_name), set())
        sites.append(
            MintSite(
                path,
                node.lineno,
                "helper" if resolved else "unresolved",
                ast.unparse(node),
                frozenset(resolved),
            )
        )
    sites.extend(unresolved_calls)

    prefixes = frozenset(prefix for site in sites for prefix in site.prefixes)
    return tuple(sorted(sites, key=lambda site: (str(site.path), site.lineno))), prefixes


def mint_sites() -> tuple[MintSite, ...]:
    """Every expression in the product that builds a prefixed identifier."""
    return _analysis()[0]


def unaccounted_ulid_uses() -> tuple[MintSite, ...]:
    """Every ULID the walk did NOT account for -- the proof that it SWEEPS.

    `mint_sites` reads one shape: an f-string. That is a shape somebody chose,
    and choosing a shape is how the first derivation came to miss the helper
    form. So the coverage is not asserted on f-strings, it is asserted on the
    MINT ITSELF: every reference to `ULID` in the product's code must fall into
    one of three accounted classes --

      * an import of the name;
      * a reference inside an f-string this walk resolved to a literal prefix;
      * a bare `ULID()` that no f-string wraps -- an identifier with no family,
        such as the sync run key of `google_sheets_sync.py`.

    A mint written any other way -- ``"%s_%s" % (prefix, ULID())``,
    ``prefix + "_" + str(ULID())``, an f-string whose prefix is computed -- is
    in none of them, and comes back here with its file and its line. That is
    what a hand-typed list can never do: it can only name what somebody already
    met.
    """
    resolved: set[tuple[Path, int]] = {
        (site.path, site.lineno) for site in mint_sites() if site.prefixes
    }
    unaccounted: list[MintSite] = []
    for path in _product_modules():
        try:
            tree = ast.parse(_source(path))
        except SyntaxError:  # pragma: no cover -- a module that does not parse mints nothing
            continue
        links = _parents(tree)
        for node in ast.walk(tree):
            named_ulid = (isinstance(node, ast.Name) and node.id == "ULID") or (
                isinstance(node, ast.Attribute) and node.attr == "ULID"
            )
            if not named_ulid:
                continue
            carrier = _carrier(node, links, resolved, path)
            if carrier is not None:
                unaccounted.append(
                    MintSite(path, carrier.lineno, "unaccounted", ast.unparse(carrier), frozenset())
                )
    return tuple(unaccounted)


def _carrier(
    node: ast.AST,
    links: dict[ast.AST, ast.AST],
    resolved: set[tuple[Path, int]],
    path: Path,
) -> ast.AST | None:
    """The expression that glues a prefix onto this ULID, when it is not accounted.

    Climbing from the `ULID` reference, exactly two ways up are accounted for:
    plain calls (``ULID()``, ``str(ULID())``) reaching a statement -- a bare
    identifier, no family -- and an f-string this walk already resolved. A
    method call (``"{}_{}".format(...)``, ``"-".join(...)``) or an operator
    (``prefix + str(ULID())``, ``"%s_%s" % (...)``) GLUES something to the
    identifier, and what it glues is a prefix this walk never read. Those come
    back named.
    """
    current: ast.AST | None = links.get(node)
    while current is not None:
        if isinstance(current, ast.JoinedStr):
            return None if (path, current.lineno) in resolved else current
        if isinstance(current, ast.Call):
            if not isinstance(current.func, ast.Name):
                return current
        elif isinstance(current, (ast.BinOp, ast.JoinedStr)):
            return current
        elif not isinstance(current, (ast.expr, ast.keyword)):
            return None  # a statement: the ULID reached it unglued
        current = links.get(current)
    return None


def unresolved_mint_sites() -> tuple[MintSite, ...]:
    """The mints this walk could NOT follow to a literal. Must stay empty."""
    return tuple(site for site in mint_sites() if not site.prefixes)


def minted_identifier_prefixes() -> frozenset[str]:
    """The prefix of every identifier family the product mints."""
    return _analysis()[1]


@lru_cache(maxsize=1)
def identifier_shape() -> re.Pattern[str]:
    """THE SHAPE OF A PRODUCT IDENTIFIER, over every family the walk found.

    A minted family prefix, an underscore, then the opaque part. The opaque part
    is upper-case letters and digits -- a Crockford ULID, or the `EXAMPLE`
    stand-in the fixtures use -- never a word a person wrote, which is what keeps
    `Return on ad spend` out of it. The prefix is looked for ANYWHERE in the
    text, not only at its start, because `Ad spend (mdm_01KZ...)` prints an
    identifier just as surely as `mdm_01KZ...` does. Case matters: text
    beginning `Person_A` is a word, `person_01K` is an identifier.
    """
    return re.compile(
        r"(?:^|[^0-9a-z])(?:" + "|".join(sorted(minted_identifier_prefixes())) + r")_[0-9A-Z]"
    )


def identifier_rendered_in(text: str) -> str | None:
    """The identifier this sentence puts in front of a person, or None.

    ONE ASSERTION FOR EVERY FAMILY, and the reason the suites stopped writing
    `assert "mdm_" not in message`. That check names one family out of the
    hundreds the product mints: a refusal that printed `ds_01KZ...`,
    `qsv_01KZ...` or `scv_01KZ...` walked straight past it, on ten separate
    sites. This one is derived from the mints, so it covers the family added
    tomorrow with no edit in any test.
    """
    found = identifier_shape().search(text or "")
    return found.group().strip() if found else None
