"""Every DQ alert names a repair address the console can actually open.

`unresolved-values.md`, "Incomplete if": *an alert names no repair address, or
names a screen that cannot repair its reason*.

WHAT WAS MEASURED. `geographic_conformance.build_dq_firing_payload` wrote
``metadata["repair"]["surface"] = "dimension_conformance"``. That string has no
consumer: `grep -rn "dimension_conformance" ui/admin/src` returns nothing, and
`ownerResolution.ts` -- the ONE thing in the console that turns a
server-composed address into a destination -- knows two surfaces, `project` and
`global`. So the single alert this surface writes carried a repair address that
no code path anywhere could resolve. It read like an address, which is worse
than carrying none.

WHY THIS IS A CLASS GUARD AND NOT A REPAIRED INSTANCE. Asserting the one
payload's new shape would go green for the next writer that invents its own
vocabulary. So this test does two things instead:

  * it COMPUTES the inventory -- every module under `server/core` that writes a
    `dq_*` firing to `app.alert_firings` -- by walking the AST, never from a
    list typed here that would stop growing;
  * it resolves each repair address those writers emit through the CONSOLE'S
    OWN TABLE, parsed from `shell/navigation/*.ts` and
    `shell/ownerResolution.ts` (`tests.support.console_addresses`), never
    through a copy of that table restated in Python. An instrument that
    measures its own copy of the target measures nothing.

The guard fails loudly when the inventory is empty, when a writer's repair block
cannot be followed, and when a followed address does not resolve.
"""

from __future__ import annotations

import ast
import importlib
import pathlib

import pytest

from tests.support.console_addresses import (
    read_global_sections,
    read_workspaces,
    resolve_console_address,
)

CORE = pathlib.Path(__file__).resolve().parents[2] / "core"

#: The prefix `dq_api` filters `app.alert_firings` on -- a firing whose type
#: starts with it is a DQ issue, and a DQ issue is what a person opens looking
#: for the gesture that repairs it.
DQ_TYPE_PREFIX = "dq_"


def _module_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so `alert_type=` can be read."""
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        constants[target.id] = node.value.value
    return constants


def _alert_type(call: ast.Call, constants: dict[str, str]) -> str | None:
    for keyword in call.keywords:
        if keyword.arg != "alert_type":
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
        if isinstance(keyword.value, ast.Name):
            return constants.get(keyword.value.id)
    return None


def _writes_a_dq_firing(tree: ast.Module) -> bool:
    constants = _module_constants(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name != "write_infra_firing":
            continue
        declared = _alert_type(node, constants)
        if declared and declared.startswith(DQ_TYPE_PREFIX):
            return True
    return False


def _repair_expressions(tree: ast.Module) -> list[ast.expr]:
    """Every value bound to a ``"repair"`` key anywhere in the module."""
    found: list[ast.expr] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "repair":
                found.append(value)
    return found


def _dq_firing_writers() -> dict[str, ast.Module]:
    writers: dict[str, ast.Module] = {}
    for path in sorted(CORE.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:  # pragma: no cover -- a broken-module fixture
            continue
        if _writes_a_dq_firing(tree):
            writers[path.stem] = tree
    return writers


def test_the_console_address_table_is_readable_at_all():
    """The instrument before the measurement -- a blind parser reads as green."""
    workspaces = read_workspaces()
    assert workspaces, "the navigation registry parsed to zero workspaces"
    assert read_global_sections(), "GLOBAL_OWNER_SECTIONS parsed to nothing"
    # The two addresses `unresolved-values.md` names, S1 and S2, exist as the
    # console declares them -- so a failure below is the payload's, not the
    # table's.
    assert "mapping" in workspaces["data"]["datastreams"].objects["datastream"].tabs
    assert "value-tables" in workspaces["governance"]["semantic-model"].lenses


def test_the_inventory_of_dq_firing_writers_is_computed_and_not_empty():
    """Never a list typed here: a hand-written inventory stops growing."""
    writers = _dq_firing_writers()
    assert writers, (
        "no module under server/core was found writing a `dq_*` firing -- this "
        "guard has gone blind. Check that `write_infra_firing(alert_type=...)` "
        "is still how a DQ issue reaches `app.alert_firings`."
    )
    assert "geographic_conformance" in writers, (
        "the geography monitor is the writer this criterion was measured on; if "
        "it stopped writing a firing, say so in unresolved-values.md rather than "
        "letting the guard lose its subject"
    )


def _binding(tree: ast.Module, name: str) -> ast.expr | None:
    """What a local named *name* is assigned in this module, or `None`.

    `"repair": repair` is the shape a payload takes once the address is composed
    into a named value rather than inline. A guard that stopped at the name would
    stop at the first writer that reads well.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
    return None


def _follow(
    module_name: str, expression: ast.expr, tree: ast.Module | None = None
) -> list[tuple[str, object]]:
    """Turn one `"repair": <expr>` into the address(es) it actually emits.

    A dict literal is read as written. A local name is followed to what it was
    assigned. A call to a module-level builder is CALLED -- with no arguments,
    and then with a sample object id -- because that is the only way a guard
    follows a writer that composes its address instead of spelling it.
    """
    if isinstance(expression, ast.Name) and tree is not None:
        bound = _binding(tree, expression.id)
        if bound is not None:
            return _follow(module_name, bound)
    if isinstance(expression, ast.Dict):
        try:
            return [("literal", ast.literal_eval(expression))]
        except ValueError:
            return [("unreadable", None)]
    if isinstance(expression, ast.Call) and isinstance(expression.func, ast.Name):
        module = importlib.import_module(f"core.{module_name}")
        builder = getattr(module, expression.func.id, None)
        if builder is None:
            return [("unreadable", None)]
        emitted: list[tuple[str, object]] = [(f"{expression.func.id}()", builder())]
        try:
            emitted.append((f"{expression.func.id}('ds-1')", builder("ds-1")))
        except TypeError:
            pass
        return emitted
    return [("unreadable", None)]


def test_every_repair_address_a_dq_firing_writes_resolves_in_the_console():
    """THE CLASS. Each writer's repair block, through the console's own table."""
    checked = 0
    failures: list[str] = []
    for module_name, tree in _dq_firing_writers().items():
        for expression in _repair_expressions(tree):
            for label, address in _follow(module_name, expression, tree):
                if label == "unreadable":
                    failures.append(
                        f"core/{module_name}.py line {expression.lineno}: this "
                        "guard cannot follow the `repair` value to an address. "
                        "Compose it in a module-level function that takes no "
                        "required argument, so the address a person is sent to "
                        "can be read here."
                    )
                    continue
                checked += 1
                refusal = resolve_console_address(address)
                if refusal:
                    failures.append(
                        f"core/{module_name}.py line {expression.lineno} "
                        f"[{label}]: {refusal}\n    address: {address!r}"
                    )
    assert not failures, (
        "a DQ alert names a repair address the console cannot open:\n  "
        + "\n  ".join(failures)
    )
    assert checked, "no repair address was checked at all -- the guard is blind"


def test_the_geography_firing_sends_a_reader_to_the_panel_that_repairs_it():
    """The instance, named: S1 with a Datastream, S2 without one."""
    from core import geographic_conformance as gc

    items = gc.aggregate_unmapped_evidence(
        [{"raw_value": "Frnce", "connector": "acme", "occurrences": 3}]
    )
    on_datastream = gc.build_dq_firing_payload(items, datastream_id="ds-1")
    assert on_datastream is not None
    repair = on_datastream["metadata"]["repair"]
    assert resolve_console_address(repair) is None, repair
    assert repair["tab"] == "mapping" and repair["object_id"] == "ds-1"

    project_wide = gc.build_dq_firing_payload(items)
    assert project_wide is not None
    wide = project_wide["metadata"]["repair"]
    assert resolve_console_address(wide) is None, wide
    assert wide["lens"] == "value-tables"


@pytest.mark.parametrize(
    "address",
    [
        {"surface": "dimension_conformance", "scope_level": "PROJECT"},
        {"surface": "project", "workspace": "data", "section": "no-such-section"},
        {
            "surface": "project",
            "workspace": "data",
            "section": "datastreams",
            "object_type": "datastream",
            "object_id": "ds-1",
            "tab": "deliveries",
        },
        {"surface": "global", "global_surface": "billing", "global_section": "plan"},
    ],
)
def test_the_guard_refuses_an_address_the_console_cannot_open(address):
    """The instrument proves it can go red -- one refusal per branch it guards."""
    assert resolve_console_address(address) is not None, address
