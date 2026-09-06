"""The convergence has an address, and the address holds the right capability.

Story 49.2, AC1. `core.master_data_convergence` moves an organization's whole
business taxonomy into the Master Data authority. A command that only a test can
call is the defect this repository keeps paying for, so what is asserted here is
the ROUTE -- registration, method, capability and each refusal code -- rather
than the function behind it, which its own pg-gated suite walks.

TWO ROUTES, ONE PATH, and it is the point rather than an accident: seeing what
would move needs `view`, moving it needs `manage`. Registering one route with
both verbs would have made the capability depend on the verb rather than on the
address, which is exactly what `test_the_reads_are_read_only` refuses next door.
"""

from __future__ import annotations

import inspect

from core.governance_surface_api import (
    GOVERNANCE_SURFACE_ROUTES,
    _master_data_convergence,
)

PATH = "/api/projects/{project_id}/governance/master-data/convergence"


def _routes():
    return [r for r in GOVERNANCE_SURFACE_ROUTES if r.path == PATH]


def test_the_plan_and_the_command_are_two_routes_on_one_address() -> None:
    routes = _routes()

    assert len(routes) == 2, "the convergence is unreachable, or it is one route with two verbs"
    methods = sorted(sorted(r.methods) for r in routes)
    assert methods == [["GET", "HEAD"], ["POST"]]


def test_neither_route_is_shadowed_by_the_generic_section_read() -> None:
    """`/governance/{section}` matches `/governance/master-data`; order decides."""
    paths = [r.path for r in GOVERNANCE_SURFACE_ROUTES]
    generic_section = "/api/projects/{project_id}/governance/{section}"

    assert paths.index(PATH) < paths.index(generic_section)


def test_moving_an_organization_master_requires_manage_and_reading_the_plan_view() -> None:
    """AC11: *organization-master writes require organization manage permission*.

    The node command holds `edit` because it retires ONE identity inside a
    Project. This moves the organization's entire taxonomy, so it holds one step
    more -- and the plan, which writes nothing, holds one step less.
    """
    source = inspect.getsource(_master_data_convergence)

    assert 'minimum_capability="manage" if writing else "view"' in source


def test_the_organization_is_rooted_from_the_project_and_never_from_the_body() -> None:
    """AC11 again: *they never trust client owner/scope/impact fields*."""
    source = inspect.getsource(_master_data_convergence)

    assert "decision.org_id" in source
    assert 'body.get("org_id")' not in source
    assert 'body.get("reason")' in source, "the only field the body may carry"


def test_the_write_demands_an_idempotency_key_and_never_mints_one() -> None:
    source = inspect.getsource(_master_data_convergence)

    assert "Idempotency-Key" in source
    assert "idempotency_key_required" in source
    assert "428" in source
    # A key minted server-side makes every retry a new command, which is the
    # opposite of what it is for.
    assert "uuid" not in source.lower()


def test_the_plan_needs_no_key_because_it_writes_nothing() -> None:
    """Demanding one on a read would ask for a value the route does not use."""
    source = inspect.getsource(_master_data_convergence)

    assert "if writing:" in source
    assert source.index("if writing:") < source.index("Idempotency-Key")


def test_every_refusal_the_convergence_can_raise_has_a_distinct_answer() -> None:
    """Collapsing them is how "I could not check" starts reading as "nothing
    to do"."""
    source = inspect.getsource(_master_data_convergence)

    for expected in (
        "ConvergenceRefused",  # 409, carrying the branch that could not be placed
        "master_data_evidence_unavailable",  # 503, fail closed
        "invalid_input",  # 422
        "governance_unavailable",  # 503, without disclosing existence
        "_denied()",  # 403 for a known member below the capability
        "_not_found()",  # 404, existence-hiding
    ):
        assert expected in source, expected
