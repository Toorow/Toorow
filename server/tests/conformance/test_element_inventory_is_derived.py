"""The hand-written delivery table against the derived element inventory.

WHY THIS FILE EXISTS. `element-control-loop.md` §5 says the element inventory is
the keystone and that it does not exist, and it says why a hand-written one is
not a substitute: *"a hand-written inventory drifts from the target exactly like
the code did"*. `scripts/object_coverage_audit.DELIVERY` is that hand-written
inventory — 88 lines mapping each ratified object to the address that opens it —
and it cannot simply be derived, for the reason that script's own header gives:
`Product` is delivered as a lens over a generic type and `Run` as a tab of the
Datastream, and those are design decisions no parser produces.

So the halves are split. `scripts/element_inventory.py` derives what the code
DELIVERS from the four sources this repository already owns; the table keeps the
decision of which ratified object each address answers; and this guard is the
seam between them — the shape of `test_route_inventory_is_current.py`, one
directory over: it compares, it never regenerates.

THE SECOND TEST IS NOT CEREMONY. A derivation that scans nothing reports a clean
sheet, and the comparison above passes on two empty sets. That is criterion 13
of `module-boundaries.md` and the whole subject of `quiet_guard_census.py`, so
each plane carries a floor that is false on an empty scan.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import element_inventory  # noqa: E402


@pytest.fixture(scope="module")
def inventory():
    """The three fast planes. The MCP plane boots the real app (~13 s) and is
    measured by `scripts/mcp_tool_surface_report.py` and its own test; booting it
    a third time here would buy nothing this file asserts."""
    return element_inventory.build(with_tools=False)


def test_every_written_delivery_point_is_one_the_code_delivers(inventory) -> None:
    missing = element_inventory.delivery_points_not_derived(inventory.screens)
    assert not missing, (
        "`object_coverage_audit.DELIVERY` names delivery points the navigation "
        "registry does not declare:\n  " + "\n  ".join(missing) + "\n\n"
        "Run `python scripts/element_inventory.py` to see the derived inventory. "
        "Repair the mapping or the registry — never the derivation, which only "
        "reads what the eight registry files say."
    )


def test_no_plane_of_the_derivation_has_collapsed(inventory) -> None:
    collapsed = inventory.collapsed()
    assert not collapsed, "\n".join(collapsed)


def test_the_derivation_really_scanned_the_repository(inventory) -> None:
    """A floor per plane: false on an empty scan, which is the only bar that
    matters. The figures on 2026-08-31 were 35 / 133 / 23 / 518 / 41."""
    counts = inventory.counts()
    assert counts["screen.type"] >= element_inventory.FLOORS["screen.type"], counts
    assert counts["screen.tab"] >= element_inventory.FLOORS["screen.tab"], counts
    assert counts["screen.lens"] >= element_inventory.FLOORS["screen.lens"], counts
    assert counts["route"] >= element_inventory.FLOORS["route"], counts
    assert counts["object"] >= element_inventory.FLOORS["object"], counts
    # And the planes are distinct populations, not one list counted four times.
    planes = {element.plane for element in inventory.all}
    assert planes == {"screen", "route", "object"}, planes


def test_the_backward_direction_reaches_the_route_grain(inventory) -> None:
    """Routes are reconciled with the ratified vocabulary, not merely counted.

    Held as a shape for the same reason as the tab test below: the two counts
    move whenever a module gains an address. What is pinned is that the question
    is ASKED of a route at all — until 2026-09-02 the plane was enumerated per
    declaring module and mapped to nothing, which is `element-control-loop.md`
    §5's own line about it.
    """
    routes = inventory.routes
    assert routes, "no route reached the inventory; the router booted empty"

    claimed = [element for element in routes if element.claimed_by]
    unclaimed = [element for element in routes if not element.claimed_by]
    assert claimed, (
        "not one mounted address is claimed by a ratified object. The delivered "
        "vocabulary collapsed — run `python scripts/element_inventory.py` and read "
        "the COLLAPSED PLANE line, rather than reading this as drift."
    )
    assert unclaimed, (
        "every mounted address is claimed by a ratified object. If that is real, "
        "record the day in docs/product-architecture/element-control-loop.md and "
        "delete this assertion rather than weakening it."
    )
    # A claim is never made without its evidence: the elements the address names.
    assert all(element.answers for element in claimed)


def test_a_route_the_router_stops_mounting_leaves_the_inventory() -> None:
    """The mutation proof: the inventory follows the router, never a cached list.

    `screens/routes.json` holds the same set as a CACHE, and an inventory that
    read it would inherit its drift — the whole subject of
    `test_route_inventory_is_current.py`. So the router is mutated in place here
    and the derivation re-run: a guard that cannot lose an address is a guard
    that cannot fail.
    """
    from core.admin_api import router

    before = element_inventory.route_elements()
    victim = next(
        route
        for route in router.routes
        if getattr(route, "path", "").startswith("/api/")
    )
    at = router.routes.index(victim)
    address = element_inventory._HOLE.sub("{}", victim.path)
    try:
        router.routes.pop(at)
        after = element_inventory.route_elements()
    finally:
        router.routes.insert(at, victim)

    assert address in {element.address for element in before}
    assert address not in {element.address for element in after}, (
        f"{address!r} survived being unmounted: the route plane is reading "
        "something other than the composed router."
    )
    assert element_inventory.route_elements() == before


def test_a_tool_the_registry_stops_declaring_leaves_the_inventory() -> None:
    """The same proof on the tool plane, over the report that owns the measure.

    `mcp_tool_surface_report._collect()` boots the real app (~13 s) and has its
    own tests; what is proven here is the only thing this script adds — that a
    name absent from that report is absent from the inventory, and that the
    reconciliation follows the name rather than a list kept beside it.
    """
    report = {
        "wire_tool_names": ["list_datastreams", "get_report"],
        "app_only_tools": ["app_read_result_slice"],
        "assembled_tools": 3,
    }
    tools = element_inventory.tool_elements(report)
    assert [element.address for element in tools] == [
        "get_report",
        "list_datastreams",
        "app_read_result_slice",
    ]

    report["wire_tool_names"] = ["get_report"]
    survivors = {element.address for element in element_inventory.tool_elements(report)}
    assert "list_datastreams" not in survivors, (
        "a tool the registry no longer declares survived in the inventory."
    )


def test_a_route_and_a_tool_are_mapped_to_the_element_they_name() -> None:
    """The mapping itself, on addresses this repository actually mounts.

    Three shapes, and each one was a real failure of a cruder reading: the tab
    join (`workbench/mapping` -> `tab:datastream#mapping`), the two-segment type
    join (`context/procedures` -> `type:context-procedure`, which reads as drift
    without it) and the lens that needs its section (`templates` alone is a
    connector's delivery template, not the Analyze lens).
    """
    inventory_screens = element_inventory.screen_elements()
    element_inventory.claim_screens(inventory_screens)
    vocab = element_inventory.vocabulary(inventory_screens)

    def named(address: str) -> set[str]:
        return set(
            element_inventory.elements_named(
                element_inventory._route_words(address), vocab
            )
        )

    assert "type:datastream" in named("/api/projects/{}/datastreams/{}/workbench/runs")
    assert "tab:datastream#mapping" in named(
        "/api/projects/{}/datastreams/{}/workbench/mapping"
    )
    assert "type:context-procedure" in named("/api/context/procedures/{}/versions")
    assert named("/api/connectors/{}/templates") == {"type:connector"}
    assert not named("/api/health")

    tool = set(
        element_inventory.elements_named("get_datastream_report".split("_"), vocab)
    )
    assert {"type:datastream", "type:report"} <= tool


def test_the_backward_direction_reaches_the_tab_grain(inventory) -> None:
    """The finding this inventory exists to produce, held as a shape.

    Not a number: the count moves whenever a workbench gains a tab, and pinning
    it here would make an ordinary addition look like a regression. What is
    pinned is that the question is ASKED at the tab grain at all — a tab claimed
    by no ratified object must be visible, because `object_coverage_audit.py`
    runs this same direction one grain coarser and cannot see one.
    """
    tabs = [element for element in inventory.screens if element.kind == "tab"]
    assert tabs, "no tab reached the inventory; the registry parse returned nothing"
    unclaimed = [element for element in tabs if not element.claimed_by]
    claimed = [element for element in tabs if element.claimed_by]
    assert claimed, (
        "not one tab is claimed by a ratified object; `DELIVERY` no longer maps a "
        "`tab:` point, so the backward direction at this grain is vacuous."
    )
    assert unclaimed, (
        "every tab is now claimed by a ratified object. If that is real, it is the "
        "day this repository closed the element-grain backward direction — record "
        "it in docs/product-architecture/element-control-loop.md and delete this "
        "assertion rather than weakening it."
    )
