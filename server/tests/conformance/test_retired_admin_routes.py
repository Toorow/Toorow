"""A retired door stays shut, and says which one answers instead -- AD-43.

WHY THIS FILE EXISTS. Four handlers in `admin_api.py` carry a route address in
their docstring and are mounted by nothing: 243 lines of door that opens onto
nothing. Deleting them would leave the retirement without a trace, and the next
reader remounts the route -- which has happened here once already
(`_create_datastream_mapping_version`, arbitrated in `e6e33d1`, whose six tests
are held as `xfail(strict=True)` for exactly that reason).

So the orphans are kept and the ABSENCE is made loud. Each names its successor in
its own docstring; these cases refuse a silent remount, and they refuse the other
half too -- a successor that disappears while the orphan is still described as
retired would leave the console with no door at all.

Measured 2026-08-12 on `admin_api.router.routes`: each retired address has a live
replacement, which is what turned "retired or forgotten?" from a judgement call
into a reading.
"""

from __future__ import annotations

import pytest
from core import admin_api

#: orphan handler -> (its retired address, the address that answers today)
RETIRED = {
    "_publish_datastream_first_publication": (
        "/api/projects/{project_id}/datastreams/{ds_id}/publish",
        "/api/projects/{project_id}/datastreams/{datastream_id}/executions"
        "/{execution_id}/publish-activate",
    ),
    "_rollback_dataset": (
        "/api/datastreams/{id}/rollback",
        "/api/projects/{project_id}/datastreams/{datastream_id}/workbench/outputs"
        "/rollback-preparations",
    ),
    "_preview_dataset_rollback": (
        "/api/datastreams/{id}/rollback/preview",
        "/api/projects/{project_id}/datastreams/{datastream_id}/workbench/outputs"
        "/rollback-preparations/{preparation_id}/confirm",
    ),
    # Arbitrated separately in `e6e33d1`; listed here so the four read together
    # rather than one of them living only in a session note.
    "_create_datastream_mapping_version": (
        None,
        "the governed change path -- `datastream_change.confirm_change`, which "
        "runs inside `execute_operation`",
    ),
    # Found by the AD-43 sixth step, 2026-08-13: once every subject had left
    # `admin_api.py`, these three were what remained with no route, no caller in
    # `core`, and no test. 139 lines that nothing could reach. Their successors
    # are all PROJECT-SCOPED, which is the whole reason the org-wide originals
    # stopped being mounted -- remounting one would reopen a door that answers
    # without a project.
    "_prepare_setup_handoff": (
        "/api/setup/handoffs",
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/handoffs",
    ),
    "_reassign_setup_task": (
        "/api/setup/tasks/{task_id}/owner",
        "/api/projects/{project_id}/getting-started/tasks/{task_id}/owner",
    ),
    "_overlay_rejection_counts": (
        None,
        "the daily breakdown itself -- `datastreams_api` reads the rejection "
        "count where the sample days are built, instead of annotating them "
        "afterwards",
    ),
}


def _mounted() -> dict[str, str]:
    """Every mounted path, and the handler behind it."""
    return {
        route.path: getattr(route.endpoint, "__name__", "")
        for route in admin_api.router.routes
        if getattr(route, "path", None)
    }


@pytest.mark.parametrize("handler", sorted(RETIRED))
def test_a_retired_handler_is_mounted_by_nothing(handler: str) -> None:
    """The remount this file exists to catch.

    A route re-added on top of a retired handler is not a restoration: the
    handler kept the shape it had BEFORE the successor existed. The
    `_create_datastream_mapping_version` case is the measured one -- it would
    activate the mapping version it appends, which both governed callers refuse
    explicitly.
    """
    assert hasattr(admin_api, handler), (
        f"{handler} is gone. Deleting a retired handler leaves the retirement "
        "without a trace -- if it was deliberate, this case goes with it."
    )
    assert handler not in _mounted().values(), (
        f"{handler} has been remounted. It was retired because another address "
        f"answers: {RETIRED[handler][1]}"
    )


@pytest.mark.parametrize("handler", sorted(RETIRED))
def test_the_successor_of_a_retired_door_is_still_mounted(handler: str) -> None:
    """The half that is easy to forget.

    An orphan described as *retired because X answers instead* becomes a lie the
    day X is itself removed -- and the console is then left with no door at all,
    while a docstring keeps pointing at one.
    """
    _retired, successor = RETIRED[handler]
    if not successor.startswith("/"):
        pytest.skip("arbitrated retirement whose successor is a call path, not a route")

    assert successor in _mounted(), (
        f"{handler} says it was retired because {successor} answers instead, "
        "and that address is no longer mounted."
    )


def test_each_retired_handler_names_its_successor_where_a_reader_will_look():
    """In the docstring, not only in a document nobody opens beside the code."""
    for handler in RETIRED:
        doc = (getattr(admin_api, handler).__doc__ or "").upper()
        assert "RETIRE" in doc or "26695DC" in doc, (
            f"{handler} does not say it is retired where it is read"
        )
