"""Every governed handler is reachable — a structural guard, not a status probe.

The finding this exists for: `85b1deb` deleted three `Route(...)` lines and left
their handlers in place. `PATCH /api/projects/{project_id}` answered 405 for a day,
so no project could be updated at all — not its name, not its geographic posture,
not its verification preferences, not its currency — and the governed geographic
preview/confirm pair was unreachable.

Nothing caught it. `test_epic43_module_project_security.py` invokes `_patch_project`
**directly, as a function**, so its four security invariants stayed green while the
route was gone; the only UI caller was itself unmounted. A handler tested by calling
it and reached by nobody looks exactly like a working one.

Status probes are the wrong instrument: a 404 means both "refused" and "no such
route", and with no Postgres listening the handler answers 500 before either. So
this reads the router table. It is cheap, deterministic, and it fails on the exact
mistake — a handler that exists with no door.

Adding a route here is deliberate. Removing one is too: if a path is genuinely
superseded, delete its line WITH its handler and say what replaced it, the way
Story 46.4 retired `_get_setup_journey` and `/api/overview/summary` in favour of
`getting_started_routes` and the Overview read model.
"""

from __future__ import annotations

import pytest

# (path, method) pairs whose absence broke something real, or would.
GOVERNED_ROUTES = [
    # Project lifecycle. The PATCH is the one that was missing.
    ("/api/projects", "GET"),
    ("/api/projects", "POST"),
    ("/api/projects/{project_id}", "GET"),
    ("/api/projects/{project_id}", "PATCH"),
    ("/api/projects/{project_id}", "DELETE"),
    # The governed geographic preview/confirm pair used to be listed here. Story
    # 48.2 Task 4 RETIRED it on 2026-08-04 — the way the docstring above demands:
    # the two Route lines went WITH their handlers, and what replaced them is
    # `POST /api/projects/{project_id}/governance/master-data/country`
    # (`governance_surface_api`, prepare then publish), the one owner AC1 gives
    # Country. `test_geographic_change_api.py` now guards both directions: no
    # private pair comes back, and the governed route stays registered.
    # Organization lifecycle and its invitation pair.
    ("/api/organizations", "GET"),
    ("/api/organizations", "POST"),
    ("/api/organizations/{org_id}", "DELETE"),
    ("/api/organizations/{org_id}/invitations", "GET"),
    ("/api/organizations/{org_id}/invitations/{invitation_id}/revoke", "POST"),
    ("/api/organizations/{org_id}/invitations/{invitation_id}/resend", "POST"),
    # Connector enablement — the family Story 47.1 renamed from /api/modules.
    ("/api/connectors/available", "GET"),
    ("/api/connectors/{project_id}/{connector_name}", "PATCH"),
]


def _registered() -> set[tuple[str, str]]:
    from core.admin_api import router

    return {
        (route.path, method)
        for route in router.routes
        for method in (getattr(route, "methods", None) or set())
    }


@pytest.mark.parametrize(("path", "method"), GOVERNED_ROUTES)
def test_governed_route_has_a_door(path: str, method: str):
    assert (path, method) in _registered(), (
        f"{method} {path} has no Route(...) line. Its handler may still exist and its "
        "unit tests may still pass by calling it directly -- that is exactly how "
        "PATCH /api/projects/{project_id} answered 405 for a day."
    )


def test_no_governed_route_is_declared_twice_for_one_method():
    """Two Route lines for one (path, method) means the second is dead.

    Starlette matches the first, so a later registration silently never runs -- the
    mirror image of the missing-route defect, and just as invisible.
    """
    from core.admin_api import router

    seen: dict[tuple[str, str], int] = {}
    for route in router.routes:
        for method in getattr(route, "methods", None) or set():
            if method in ("HEAD", "OPTIONS"):
                continue
            seen[(route.path, method)] = seen.get((route.path, method), 0) + 1
    duplicated = {key: count for key, count in seen.items() if count > 1}
    assert not duplicated, f"shadowed route registrations: {duplicated}"
