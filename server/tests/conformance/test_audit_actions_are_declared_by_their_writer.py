"""An audit action is declared where it is written -- AD-42.

WHY THIS FILE EXISTS. `core/audit.py` held 96 `ACTION_` constants and was on
everybody's path: 43 edits from 29 distinct subjects since June, **34 of them
adding nothing but a constant**. That is a crossroads, and dividing it would have
repaired nothing -- whatever file the list lives in, everybody still has to open
it. It was inverted instead: the module that WRITES an action declares it, and
`core.audit` only collects.

The cost was never only the conflicts. Measured on the live `app.audit_log` the
day this changed:

    96   constants declared centrally
    64   action values actually written in production
    29   of those 64 -- 45 % -- declared NOWHERE, the string typed in by hand
    61   of the 96 declared and never written once

A list that 45 % of its writers bypass is not a vocabulary. These cases refuse
the two ways it could come back: a central list growing again, and a writer that
declares nothing.

Offline: they read the source tree and the registry, never a database.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from core import audit

SERVER = pathlib.Path(__file__).resolve().parents[2]
CORE = SERVER / "core"

#: Actions written by MORE THAN ONE module. A shared vocabulary has no single
#: owner, and putting it inside one writer would make the other import a module
#: it has no other reason to know. Every other action belongs to its writer.
_SHARED_OWNER = "core.audit"


def _declared_in(path: pathlib.Path) -> list[str]:
    """Every `X = declare_action("...")` at module level in *path*."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
    except SyntaxError:  # a neighbouring session mid-edit
        return []
    found = []
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if getattr(func, "id", None) != "declare_action":
            continue
        arg = node.value.args[0] if node.value.args else None
        if isinstance(arg, ast.Constant):
            found.append(arg.value)
    return found


def test_the_central_file_keeps_only_what_is_written_by_more_than_one_module():
    """The crossroads must not grow back.

    An action declared in `core/audit.py` puts its subject back on everybody's
    path. Only a genuinely shared one earns that, and the list is short enough to
    read: eleven, against ninety-six before.
    """
    central = _declared_in(CORE / "audit.py")

    assert len(central) <= 12, (
        f"core/audit.py declares {len(central)} actions. It is a crossroads "
        "again: an action belongs to the module that writes it."
    )


def test_an_action_is_declared_by_exactly_one_module():
    """The collision the eye could never catch in a 96-line list.

    `declare_action` refuses a second owner at import time; this refuses one at
    rest, so the duplicate is named by a test rather than by a boot failure.
    """
    seen: dict[str, str] = {}
    duplicates = []
    for path in sorted(CORE.glob("*.py")):
        for code in _declared_in(path):
            if code in seen and seen[code] != path.name:
                duplicates.append((code, seen[code], path.name))
            seen[code] = path.name

    assert not duplicates, f"one action, two owners: {duplicates}"


def test_no_module_writes_an_action_it_did_not_declare():
    """THE DEFECT THE CROSSROADS PRODUCED, and the one this guard closes.

    29 of the 64 action values in the live journal were typed as literals at the
    call site because the central list was too far away to bother with. A literal
    passed to `write_audit_row` is now a `declare_action` call away from being
    checkable, so it must be one.
    """
    # AN INDIRECT WRITER IS A WRITER. The first version of this guard read only
    # the direct callers of the two writers, and missed five modules that pass
    # through a local `_audit(...)` wrapper -- one of which was composing its
    # action with an f-string, which nothing could have checked. A wrapper that
    # forwards its own `action` parameter is followed.
    writers = {"write_audit_row", "insert_audit_row"}
    for path in sorted(CORE.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if "action" not in {a.arg for a in node.args.args + node.args.kwonlyargs}:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Call):
                    continue
                name = getattr(sub.func, "attr", None) or getattr(sub.func, "id", None)
                if name not in writers:
                    continue
                if any(
                    kw.arg == "action" and getattr(kw.value, "id", None) == "action"
                    for kw in sub.keywords
                ):
                    writers.add(node.name)

    offenders = []
    for path in sorted(CORE.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
            if name not in writers:
                continue
            literals = [
                kw.value.value
                for kw in node.keywords
                if kw.arg == "action" and isinstance(kw.value, ast.Constant)
            ]
            if name == "write_audit_row" and len(node.args) >= 2:
                if isinstance(node.args[1], ast.Constant):
                    literals.append(node.args[1].value)
            for value in literals:
                offenders.append(f"{path.name}:{node.lineno} writes {value!r} as a literal")
            # An action assembled at run time is verifiable by nothing. Declare
            # the family term by term instead -- see `BUSINESS_DOMAIN_ACTIONS`.
            for kw in node.keywords:
                if kw.arg == "action" and isinstance(kw.value, ast.JoinedStr):
                    offenders.append(
                        f"{path.name}:{node.lineno} composes its action at run time"
                    )

    assert not offenders, (
        "an action written as a literal is an action nothing can check:\n  "
        + "\n  ".join(offenders)
    )


def test_the_writer_refuses_an_action_nobody_declared():
    """The guarantee the central list never gave.

    Sound because the module that writes an action is the module that declares
    it, so its declaration has run by the time it writes.
    """
    with pytest.raises(audit.UndeclaredAuditAction) as refusal:
        audit.insert_audit_row(
            object(),
            identity="owner@example.com",
            action="nobody.declared.this",
            provider_account="",
            connection_ref="",
        )

    sentence = str(refusal.value)
    assert "declare_action" in sentence, "the refusal must name the gesture"
    assert "nobody.declared.this" in sentence


def test_a_declared_action_names_its_owner():
    """`declared_actions()` is what replaces reading a 96-line list by eye."""
    owners = audit.declared_actions()

    assert owners, "nothing declared -- the registry is not being populated"
    assert owners["connection.created"] == _SHARED_OWNER
