"""Every producer of a cross-source total consults the reconciliation gate.

Story 53.2 / CAV-02. The register recorded the gate as "wired in `cards.py`" and it
was true, but it was wired at ONE of the six places that call `rollup.compute_rollup`.
The proof of that wiring was also worthless: deleting
`route_resolver=_route_resolver_for(project_id)` from `cards.py` left
`pytest tests/core/test_cards_api.py tests/core/test_rollup.py` at 56 passed, because
the two CAV-02 tests exercised the helper `_route_resolver_for` and the parameter
`compute_rollup(route_resolver=...)` separately and nothing joined them.

This guard is COMPUTED: it parses the repository, finds every call to
`compute_rollup`, and requires each one to pass `route_resolver`. No list of blessed
call sites is written down, so a seventh producer landing tomorrow is caught the day
it lands rather than the day someone re-reads the register. Deleting the keyword at
any single site turns this red and names the file and line.

It also prints the count, because "five of six were unwired" is the kind of number
that must come out of a command rather than out of a paragraph.
"""

from __future__ import annotations

import ast
import io
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
_ROLLUP_FUNCTION = "compute_rollup"


def _python_sources() -> list[Path]:
    """Every production module of the server (tests excluded -- they may inject)."""
    return sorted(
        p
        for p in _SERVER.rglob("*.py")
        if "tests" not in p.parts and "__pycache__" not in p.parts
    )


def _compute_rollup_calls() -> list[tuple[Path, int, bool]]:
    """(path, line, passes_route_resolver) for every ``compute_rollup(...)`` call."""
    found: list[tuple[Path, int, bool]] = []
    for path in _python_sources():
        try:
            tree = ast.parse(io.open(path, encoding="utf-8").read())
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover - not our files
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else func.id
                if isinstance(func, ast.Name)
                else None
            )
            if name != _ROLLUP_FUNCTION:
                continue
            wired = any(kw.arg == "route_resolver" for kw in node.keywords)
            found.append((path, node.lineno, wired))
    return found


def test_every_cross_source_total_asks_whether_it_may_be_combined(capsys):
    calls = _compute_rollup_calls()
    assert calls, "no compute_rollup call found -- the guard is measuring nothing"

    unwired = [(p, line) for p, line, wired in calls if not wired]
    with capsys.disabled():
        print(
            f"\ncompute_rollup call sites: {len(calls)} declared / "
            f"{len(calls) - len(unwired)} consult the gate / {len(unwired)} do not"
        )

    assert not unwired, "these publish a cross-source total without asking:\n" + "\n".join(
        f"  {p.relative_to(_SERVER)}:{line}" for p, line in unwired
    )


def test_the_gate_factory_lives_in_one_place():
    """One binding, so a new call site cannot invent a second, laxer one.

    `_route_resolver_for` used to CONTAIN the binding inside `cards.py`, which is
    why the other five producers had none: reaching it meant importing the card
    module. It now delegates to `metric_reconciliation.route_status_resolver`.
    """
    from core import cards, metric_reconciliation

    assert callable(metric_reconciliation.route_status_resolver)
    assert metric_reconciliation.route_status_resolver("") is None
    assert cards._route_resolver_for("") is None

    marker = object()

    def _fake(project_id):
        assert project_id == "proj_EXAMPLE"
        return marker

    original = metric_reconciliation.route_status_resolver
    metric_reconciliation.route_status_resolver = _fake
    try:
        assert cards._route_resolver_for("proj_EXAMPLE") is marker
    finally:
        metric_reconciliation.route_status_resolver = original


def test_no_second_rollup_authority_computes_its_own_aggregation():
    """`reports._rollup` computes nothing of its own -- it projects `compute_rollup`.

    It used to hold `sums[metric] / counts[metric]`, the unweighted mean CAV-03 was
    closed for, and it is what filled the report envelope. A future edit that puts an
    arithmetic aggregation back in that function is what this asserts against: the
    function body must contain a call to the one authority and no division of a sum
    by a count.
    """
    from core import reports

    tree = ast.parse(io.open(reports.__file__, encoding="utf-8").read())
    body = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_rollup"
    )

    calls = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")
        for n in ast.walk(body)
        if isinstance(n, ast.Call)
    }
    assert _ROLLUP_FUNCTION in calls, "reports._rollup no longer calls the one authority"

    divisions = [
        n for n in ast.walk(body) if isinstance(n, ast.BinOp) and isinstance(n.op, ast.Div)
    ]
    assert not divisions, (
        "reports._rollup divides again -- that is how the mean of ratios came back "
        f"({len(divisions)} division(s))"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q", "-s", "-p", "no:randomly"]))
