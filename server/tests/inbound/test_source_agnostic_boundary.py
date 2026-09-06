"""AD-2 source-agnostic boundary scanner (Epic 38, Story 38.1, AC5).

Asserts that ``server/core`` carries no inbound-provider vocabulary: provider
names (Mailgun, Cloudflare, ...) and the inbound package itself must live only
behind the ``ReceiptAdapter`` seam under ``server/inbound``. Follows the
scanner style of the conformance vocabulary tests (no module-specific strings
baked into core).

This is a static source scan (ast/text), not an import — it fails loudly if a
later change smuggles a provider name into the core boundary.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).parents[3]  # tests/inbound -> tests -> server -> repo root
_CORE_DIR = _REPO_ROOT / "server" / "core"

# THE ONE core file allowed to name the inbound package, and the ONLY module it
# may name. Every other core file asks it for a capability BY NAME and never
# learns which module -- let alone which provider -- answers.
#
# Why the exception is named rather than absent: the scanner below resolves
# `importlib` targets, and something has to perform the first import. Confining
# that to a single provider-neutral module is the difference between one
# declared boundary crossing and six undeclared ones.
_ALLOWED_INBOUND_IMPORTS: dict[str, frozenset[str]] = {
    "inbound_seam.py": frozenset({"inbound.capabilities"}),
}

# Provider / inbound-package vocabulary that must NEVER appear in server/core.
# The guard's purpose (per the module docstring) is provider-NEUTRALITY: no
# provider names and no coupling to the internet-facing ``server/inbound`` package
# or its adapter seam. It is NOT meant to ban the provider-agnostic domain noun
# "inbound receipt": Story 38.8 legitimately introduces the durable receipt ledger
# (``app.inbound_receipts`` / core.inbound_receipts) in core -- it MUST live here
# because it needs the DB + operations seam, and it carries no provider vocabulary.
# The bare "inbound-receipt"/"inbound_receipt" tokens (added in 38.1, when core had
# nothing durable) are therefore intentionally omitted; provider-neutrality stays
# fully enforced by the provider + adapter + wire tokens below and by
# ``test_core_never_imports_inbound_package``.
_FORBIDDEN_TOKENS = [
    "mailgun",
    "cloudflare_worker",
    "cloudflareworker",
    "inbound.adapters",
    "receiptadapter",
    "inbounddelivery",
    "x-inbound-worker-secret",
]


def _core_py_files() -> list[Path]:
    return [p for p in _CORE_DIR.rglob("*.py") if "__pycache__" not in p.parts]


#: `server/core/**/*.py` on 2026-08-31. A FLOOR, not an equality: criterion 13
#: of `docs/product-architecture/module-boundaries.md`. `is_dir()` is not a
#: floor -- an EMPTY `core/` is still a directory, and both tests below say
#: "no offender found", which is what a scan of nothing says.
_CORE_PY_FILES_AT_2026_08_31 = 528


def test_core_dir_exists():
    assert _CORE_DIR.is_dir(), f"expected core dir at {_CORE_DIR}"
    scanned = _core_py_files()
    assert len(scanned) >= _CORE_PY_FILES_AT_2026_08_31, (
        f"{len(scanned)} python files read under {_CORE_DIR}, "
        f"{_CORE_PY_FILES_AT_2026_08_31} on 2026-08-31 -- the boundary verdicts "
        "below are about a tree that shrank out from under them."
    )


@pytest.mark.parametrize("token", _FORBIDDEN_TOKENS)
def test_core_contains_no_inbound_provider_vocabulary(token: str):
    offenders: list[str] = []
    for path in _core_py_files():
        text = path.read_text(encoding="utf-8", errors="ignore").lower()
        if token in text:
            offenders.append(str(path.relative_to(_REPO_ROOT)))
    assert not offenders, (
        f"AD-2 violation: inbound-provider token {token!r} found in server/core "
        f"({offenders}). Provider vocabulary must stay behind the ReceiptAdapter "
        f"seam in server/inbound."
    )


def _static_strings(node: ast.AST, names: dict[str, list[str]]) -> list[str] | None:
    """Every value ``node`` can statically take, or None if it is not knowable.

    Returns a LIST because one expression can stand for several modules: a name
    bound to a tuple of module paths (``for name in _PROFILE_MODULES``) is a
    perfectly checkable import, and reporting it as unknowable would make this
    guard cry wolf on the legitimate registry loaders in core.

    Handles the form that carried the evasion: an f-string whose parts are ALL
    constants. ``f"inbound.{'adapters'}.x"`` is a compile-time constant that
    merely *looks* dynamic.
    """
    if isinstance(node, ast.Constant):
        return [node.value] if isinstance(node.value, str) else None
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        collected: list[str] = []
        for element in node.elts:
            values = _static_strings(element, names)
            if values is None:
                return None
            collected.extend(values)
        return collected
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            inner_node = value.value if isinstance(value, ast.FormattedValue) else value
            inner = _static_strings(inner_node, names)
            if inner is None or len(inner) != 1:
                return None
            parts.append(inner[0])
        return ["".join(parts)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _static_strings(node.left, names)
        right = _static_strings(node.right, names)
        if left is None or right is None or len(left) != 1 or len(right) != 1:
            return None
        return [left[0] + right[0]]
    return None


def _static_bindings(tree: ast.AST) -> dict[str, list[str]]:
    """Names bound to statically-known module paths, anywhere in the file.

    Two bindings matter: a constant assignment (``_INDEX = "inbound.capabilities"``)
    and a loop variable drawing from a constant sequence (``for name in
    _PROFILE_MODULES``). Both are indirection a reader can follow, so the scanner
    follows them too rather than declaring them opaque.
    """
    bindings: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            values = _static_strings(node.value, bindings)
            if values is not None:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bindings[target.id] = values
        elif isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            values = _static_strings(node.iter, bindings)
            if values is not None:
                bindings[node.target.id] = values
    return bindings


def _imported_modules(tree: ast.AST) -> list[tuple[str, int]]:
    """Every module this file imports, by whatever syntax -- including importlib.

    The line-prefix scan this replaces saw only ``import x`` / ``from x import``.
    Six core call sites reached the inbound package through
    ``importlib.import_module(...)`` instead, so the boundary read green while
    core depended on three provider modules by name.
    """
    bindings = _static_bindings(tree)
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module and not node.level:
                found.append((node.module, node.lineno))
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name in ("import_module", "__import__") and node.args:
                targets = _static_strings(node.args[0], bindings)
                # A target this scanner CANNOT resolve is reported too: an
                # unreadable module name is exactly how the next evasion would be
                # written, and silence there is what let the last one through.
                found.extend(
                    (target, node.lineno) for target in (targets or ["<unresolved>"])
                )
    return found


def test_core_never_imports_inbound_package():
    """Core imports no inbound module -- by any syntax, and importlib is a syntax.

    Regression guard for the 2026-08-04 rewrite (bad4cb16) that turned
    ``from inbound.adapters.datastream_setup import ...`` into
    ``importlib.import_module(f"inbound.{'adapters'}.datastream_setup")``. The
    split literal is byte-for-byte identical at runtime; it changed nothing but
    what this test could see.
    """
    offenders: list[str] = []
    for path in _core_py_files():
        allowed = _ALLOWED_INBOUND_IMPORTS.get(path.name, frozenset())
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"), filename=str(path))
        for module, lineno in _imported_modules(tree):
            if module == "<unresolved>":
                offenders.append(
                    f"{path.relative_to(_REPO_ROOT)}:{lineno}: import target cannot be "
                    f"read statically, so this boundary cannot be checked here"
                )
            elif (module == "inbound" or module.startswith("inbound.")) and module not in allowed:
                offenders.append(f"{path.relative_to(_REPO_ROOT)}:{lineno}: {module}")
    assert not offenders, (
        f"AD-2 violation: server/core imports the inbound package ({offenders}). "
        f"Core must remain provider-agnostic: ask core.inbound_seam.resolve_inbound "
        f"for a capability by name instead."
    )


def test_the_seam_exception_is_a_single_provider_neutral_file():
    """The allowlist is one file and one module, and both really exist.

    An allowlist that drifts ahead of the code is how a boundary becomes a
    formality: it would keep passing while naming files that no longer hold the
    seam.
    """
    assert list(_ALLOWED_INBOUND_IMPORTS) == ["inbound_seam.py"]
    seam = _CORE_DIR / "inbound_seam.py"
    assert seam.is_file(), f"the allowlisted seam is missing at {seam}"
    for token in _FORBIDDEN_TOKENS:
        assert token not in seam.read_text(encoding="utf-8").lower(), (
            f"the seam itself names {token!r}; it must stay provider-neutral"
        )
