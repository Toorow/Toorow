"""Country prepares and confirms through ONE route, and it is the governed one.

Story 48.2, Task 4. This file used to exercise
``POST /api/projects/{project_id}/geography/preview`` and its
``/previews/{preview_id}/confirm`` companion on ``admin_api``. That pair was the
last standalone geographic prepare/confirm surface: a second authority for the
same decision, next to the governed Master Data registry that AC1 gives Country
as its one owner.

The pair is removed. These assertions replace it rather than disappearing with
it, because "the route is gone" is only half the requirement -- the other half
is that the governed replacement is registered and reachable. If someone
re-adds a private geographic prepare/confirm, the first test fails; if someone
removes the governed one, the second does.

``core/geographic_change.py`` itself was retired on 2026-08-17, and this file
records the removal so it cannot come back by accident. When Story 48.2 removed
the routes, the module was kept "behind market governance and the audit trail" --
but it was not: measured on 2026-08-17, its preview -> confirm cycle
(`create_geographic_change_preview`, `confirm_geographic_change`) had ZERO
production callers, and its one live export was a string constant that now lives
at its reader, `rule_versions.NO_BACKFILL_FACT`. What it read
(`fetch_project_geographic_posture`) is the authority `country_registry.py`
declares replaced outright.
"""

from __future__ import annotations

import pytest
from core import admin_api


def _paths(router) -> list[str]:
    return [route.path for route in router.routes]


def test_no_standalone_geographic_prepare_or_confirm_route_remains() -> None:
    paths = _paths(admin_api.router)
    assert "/api/projects/{project_id}/geography/preview" not in paths
    assert "/api/projects/{project_id}/geography/previews/{preview_id}/confirm" not in paths
    # And not under any other spelling: no admin_api route may carry `geography`
    # as its own prepare/confirm verb.
    offenders = [path for path in paths if "/geography" in path]
    assert offenders == []


def test_no_orphan_handler_is_left_behind() -> None:
    # `85b1deb` deleted Route lines and left their handlers compiling, which is
    # how a 405 went unnoticed for a day (Epic 46 review, C-8). Removing a route
    # means removing what it pointed at.
    assert not hasattr(admin_api, "_preview_geographic_change")
    assert not hasattr(admin_api, "_confirm_geographic_change")


def test_the_governed_country_route_is_the_one_that_remains() -> None:
    from core import governance_surface_api

    routes = governance_surface_api.GOVERNANCE_SURFACE_ROUTES
    country = [
        route
        for route in routes
        if route.path == "/api/projects/{project_id}/governance/master-data/country"
    ]
    # Both halves: read, and the prepare/publish command that replaced the pair
    # deleted above.
    methods = {method for route in country for method in (route.methods or set())}
    assert {"GET", "POST"} <= methods


def test_the_retired_module_is_gone_and_names_its_successor() -> None:
    """The noisy absence (AD-43 precedent).

    A deleted module leaves no trace to read, so the trace is here: what it did,
    where that is done now, and a failure the day it reappears. `app` tables
    `geographic_change_previews` are NOT dropped -- confirmed previews are audit
    evidence, and destroying evidence to tidy a module is the wrong trade.
    """
    import importlib
    import pathlib

    module = (
        pathlib.Path(__file__).resolve().parents[2] / "core" / "geographic_change.py"
    )
    assert not module.exists(), (
        "core/geographic_change.py is back. Its preview -> confirm cycle for the "
        "project geographic posture was retired: Country is prepared and published "
        "through core.country_workspace_commands and core.governance_surface_api "
        "(route /api/projects/{project_id}/governance/master-data/country), and the "
        "capability itself moves through the generic project capability change set. "
        "If a geographic impact preview is needed again, it belongs there."
    )

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.geographic_change")


def test_the_sentence_the_retired_module_owned_still_has_a_home() -> None:
    """Removing a module must not silently remove what other screens render."""
    from core.rule_versions import NO_BACKFILL_FACT

    assert NO_BACKFILL_FACT == (
        "No raw rewrite, no provider pull and no backfill are required."
    )
