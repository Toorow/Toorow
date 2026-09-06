"""`confirm_change_set` passed a confirmation mode the operation layer refuses.

`semantic_model.confirm_change_set` built its `OperationSpec` with
`confirmation_mode="server_verified"`. `operations.prepare_operation` accepts
exactly `{none, server, host, human}` and raises `invalid confirmation_mode`
otherwise -- before reaching the mutation. So no semantic change set could ever
be confirmed, and therefore no Semantic View or Concept version could ever be
published through the governed lifecycle.

It survived because the lifecycle had never been executed end to end:
`app.semantic_views`, `app.semantic_view_versions` and `app.semantic_change_sets`
were all empty on 2026-07-30, so the module's tests exercised `create` and
`prepare` and stopped where the defect lived.

These tests are deliberately about the CONTRACT BETWEEN the two modules rather
than about one literal. A unit test asserting `== "server"` would have passed
just as happily against `"server_verified"` if someone had written the
assertion from the same wrong value.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from core.operations import OperationValidationError, prepare_operation

ROOT = Path(__file__).resolve().parents[3]
SEMANTIC_MODEL = ROOT / "server" / "core" / "semantic_model.py"
OPERATIONS = ROOT / "server" / "core" / "operations.py"


def _accepted_modes() -> set[str]:
    """Read the accepted set from `operations.py` rather than restating it."""
    tree = ast.parse(OPERATIONS.read_text("utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or not node.ops:
            continue
        if not isinstance(node.ops[0], ast.NotIn):
            continue
        left = node.left
        if not (isinstance(left, ast.Attribute) and left.attr == "confirmation_mode"):
            continue
        comparator = node.comparators[0]
        if isinstance(comparator, ast.Set):
            return {
                element.value
                for element in comparator.elts
                if isinstance(element, ast.Constant) and isinstance(element.value, str)
            }
    raise AssertionError("could not locate the confirmation_mode guard in operations.py")


def _confirmation_modes_passed_by(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"))
    modes: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "confirmation_mode":
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            modes.add(node.value.value)
    return modes


def test_the_operation_layer_still_declares_a_closed_set():
    assert _accepted_modes() == {"none", "server", "host", "human"}


def test_every_confirmation_mode_semantic_model_passes_is_one_the_layer_accepts():
    """The regression itself, stated as the contract rather than as a literal."""
    accepted = _accepted_modes()
    passed = _confirmation_modes_passed_by(SEMANTIC_MODEL)

    assert passed, "semantic_model.py passes no literal confirmation_mode any more"
    assert passed <= accepted, (
        f"semantic_model.py passes {sorted(passed - accepted)}, which "
        f"prepare_operation refuses. Accepted: {sorted(accepted)}"
    )


def test_the_rejected_spelling_is_really_rejected_by_the_layer():
    """Proves the guard bites, so the test above is not vacuous."""

    class _Spec:
        command_type = "semantic_model.confirm_change_set"
        actor = "pytest"
        idempotency_key = "k"
        effective_org_id = "org_1"
        resource_path = ("project", "p1")
        confirmation_mode = "server_verified"
        trace_id = None
        confirmation_reference = None
        host_context = {}
        versions = {}
        request_payload = {}
        provider_references = {}

    with pytest.raises(OperationValidationError, match="invalid confirmation_mode"):
        prepare_operation(_Spec())


@pytest.mark.parametrize("mode", sorted({"none", "server", "host", "human"}))
def test_each_accepted_mode_passes_the_guard(mode):
    """The counterpart: the guard is not simply refusing everything."""

    class _Spec:
        command_type = "semantic_model.confirm_change_set"
        actor = "pytest"
        idempotency_key = "k"
        effective_org_id = "org_1"
        resource_path = ("project", "p1")
        confirmation_mode = mode
        trace_id = None
        confirmation_reference = None
        host_context = {}
        versions = {}
        request_payload = {}
        provider_references = {}

    prepare_operation(_Spec())
