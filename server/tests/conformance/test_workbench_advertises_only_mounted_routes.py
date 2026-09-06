"""A read model must never advertise a door the router does not open — and the
console must call the door that is actually open.

Written after two real failures, both on 2026-08-03, a few hours apart.

FIRST. `read_tab("data")` was changed to report `sample_state: "reachable"` and a
console component was wired to `GET /api/datastreams/{id}/sample`. That endpoint
was NOT mounted: the handler existed in `admin_api.py`, no `Route` registered it,
and `test_datastream_rollback_workbench.py:21` asserted its absence deliberately
— story 47.5 retired it because it "cannot serve one processed table under
several stage labels or return an unbound sample" and "reports no exact version
binding and aliases several requested stages". Every demonstration ran against
the sandbox's mocked `fetch`; against the real router it was a 404.

SECOND, and it walked straight past the first version of this file. A parallel
session mounted a NEW project-scoped sample route — the right call — while the
console still addressed the retired one. The guard asserted only that SOME route
ending in `/sample` existed, so it stayed green while the tab advertised a sample
it could not fetch.

The lesson is in the second failure: **two things that must agree cannot be
checked one at a time.** What is asserted here is the PAIR.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
WORKBENCH = ROOT / "server" / "core" / "datastream_workbench.py"
CONSOLE_SAMPLE = (
    ROOT
    / "ui"
    / "admin"
    / "src"
    / "datastreams"
    / "workbench"
    / "DatastreamSample.tsx"
)

RETIRED_SAMPLE_ROUTE = "/api/datastreams/{id}/sample"


def _mounted_paths() -> set[str]:
    from core.admin_api import router

    return {getattr(route, "path", "") for route in router.routes}


def _shapes(paths: set[str]) -> set[str]:
    """Route paths with their parameter names removed, so they can be compared."""
    return {re.sub(r"\{[^}]*\}", "{}", path) for path in paths}


def test_the_data_tab_never_claims_a_sample_endpoint_that_is_not_mounted() -> None:
    source = WORKBENCH.read_text(encoding="utf-8")
    claims_reachable = re.search(r'"sample_state":\s*"reachable"', source) is not None
    sample_mounted = any(path.endswith("/sample") for path in _mounted_paths())

    assert claims_reachable is sample_mounted, (
        "read_tab('data') advertises `sample_state: reachable` while no route "
        "ends in /sample, or the reverse. The console calls what this field "
        "promises; the two must move together."
    )


def test_the_console_calls_a_sample_route_that_is_actually_mounted() -> None:
    source = CONSOLE_SAMPLE.read_text(encoding="utf-8")
    # Comments FIRST. This component's own header explains why the retired route
    # must not be called again, and a matcher that reads prose as a call reports
    # the explanation as the defect.
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    source = re.sub(r"//[^\n]*", "", source)
    # The URL is built from concatenated template literals; flatten those.
    flat = re.sub(r"`\s*\+\s*`", "", source)

    called = {
        re.sub(r"\$\{[^}]*\}", "{}", raw)
        for raw in re.findall(r"`(/api/[^`]*?/sample)(?:\?|`)", flat)
    }
    assert called, "no /sample call found in the console component"

    mounted = _shapes(_mounted_paths())
    for shape in called:
        assert shape in mounted, (
            f"the console calls {shape!r}, which no Route registers. Mounted: "
            f"{sorted(p for p in mounted if p.endswith('/sample'))}"
        )


def test_the_retired_consolidated_sample_route_stays_retired() -> None:
    """47.5 removed it because it LIED about stage and version binding.

    Re-mounting it would put a known-dishonest surface back, and it would do so
    while looking like a repair. Its replacement must bind a sample to its exact
    run/plan/mapping versions and refuse to alias a stage — which is what the
    project-scoped route now mounted is for."""
    assert RETIRED_SAMPLE_ROUTE not in _mounted_paths()
