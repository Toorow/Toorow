"""A write route that never commits answers 201 -- and loses everything.

WHAT THIS FILE GUARDS. `core.db.get_connection` NEVER commits: it closes, and
psycopg rolls back the open transaction. Its docstring has said so since it was
corrected ("Every write must call `conn.commit()` itself"). The four Analyze REST
modules share the same armed connection (`analyze_connection`), and three of them
committed everywhere; `visualization_specs_api` committed nowhere. Its two write
routes therefore answered `201` with an id, a version number and a content hash,
for a row that never existed.

Nothing could see it: the response is complete and coherent, and the only call
that disproves it is a RE-READ, in ANOTHER request. G13-T08 found it on
2026-08-12 -- creation `201`, re-read `404`, empty table.

WHY AN EXPLICIT EXEMPTION LIST. Not every mutating method writes: `/validate` is a
POST that only reads. Each exemption is therefore NAMED one by one, with its
reason. A new write route is red by default, which is the whole point of the
guard; a new read-only POST costs one line and its justification.
"""

from __future__ import annotations

import inspect

import pytest
from core.analyze_artifacts_api import ROUTES as ANALYZE_ARTIFACT_ROUTES
from core.analyze_workbench_api import ANALYZE_WORKBENCH_ROUTES
from core.query_specs_api import query_spec_routes
from core.visualization_specs_api import visualization_spec_routes

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

#: Routes that mutate by METHOD but not by EFFECT, each with the reason that
#: exempts it. Nothing else is exempt.
READ_ONLY_BY_DESIGN = {
    "_validate_visualization_spec_version": (
        "a POST that only revalidates an existing spec against its Result: it "
        "touches no table"
    ),
}

ROUTE_FAMILIES = (
    ("query_specs_api", query_spec_routes),
    ("analyze_workbench_api", ANALYZE_WORKBENCH_ROUTES),
    ("analyze_artifacts_api", ANALYZE_ARTIFACT_ROUTES),
    ("visualization_specs_api", visualization_spec_routes),
)


def _writing_endpoints() -> list[tuple[str, str, object]]:
    found = []
    for module_name, routes in ROUTE_FAMILIES:
        for route in routes:
            methods = {m for m in (route.methods or set()) if m in MUTATING}
            if not methods:
                continue
            endpoint = route.endpoint
            if endpoint.__name__ in READ_ONLY_BY_DESIGN:
                continue
            found.append((module_name, endpoint.__name__, endpoint))
    return found


def test_the_families_declare_at_least_one_write_each():
    """Without it, the next test would pass on an empty list."""
    modules = {module for module, _name, _fn in _writing_endpoints()}
    assert "visualization_specs_api" in modules, "the family that carried the defect"
    assert len(modules) >= 3


@pytest.mark.parametrize(
    ("module_name", "name", "endpoint"),
    _writing_endpoints(),
    ids=[f"{m}.{n}" for m, n, _ in _writing_endpoints()],
)
def test_every_write_route_commits(module_name, name, endpoint):
    source = inspect.getsource(endpoint)
    assert "conn.commit()" in source, (
        f"{module_name}.{name} writes without committing: the response will be a "
        "success and the row will not exist"
    )


def test_the_exemptions_still_point_at_real_endpoints():
    """An exemption that points at nothing is a forgotten open door."""
    known = {
        endpoint.__name__
        for _module, routes in ROUTE_FAMILIES
        for route in routes
        for endpoint in [route.endpoint]
    }
    assert set(READ_ONLY_BY_DESIGN) <= known
