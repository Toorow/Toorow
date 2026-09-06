"""The second xlsx engine is gone, and the door onto it stays shut.

WHY THIS FILE EXISTS. `file-source-ingestion.md` ratified ONE ingestion engine on
2026-08-17 (Jean, audit arbitration, question 5). `core/mediaplan_import.py` was
the second one -- 915 lines of its own Excel walk -- reachable through three
Story 22.2 addresses:

    PUT  /api/mediaplans/{plan_id}/import-contracts/{sheet_name:path}
    GET  /api/mediaplans/{plan_id}/import-contracts
    POST /api/mediaplans/{plan_id}/import

All three were removed on 2026-08-24 with the module they were the only door to.
The amendment « the second xlsx engine is retired » carries the measurement that
authorised it (no console caller, no server caller but the routes, zero plans
ever imported in production) and the two capabilities the one engine does not yet
express.

WHAT THIS FILE IS FOR, precisely. A deletion with no guard is how a route comes
back: it has happened in this repository once already
(`_create_datastream_mapping_version`, arbitrated in `e6e33d1`, whose tests are
held as `xfail(strict=True)` for exactly that reason). Remounting one of these
three addresses would not be a restoration -- there is no handler left to mount,
so it could only be a NEW second walk, which is the thing the ratified doctrine
forbids.

The assertions are on the COMPOSED router, never on a copy of the route list:
a guard that reads its own fixture proves nothing about what the server serves.
"""

from __future__ import annotations

import importlib
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[3]
_CORE = _REPO / "server" / "core"

#: The three addresses, and what answers instead. Normalised the way
#: `test_route_inventory_is_current` normalises: parameters are holes.
RETIRED_ADDRESSES = (
    "/api/mediaplans/{plan_id}/import",
    "/api/mediaplans/{plan_id}/import-contracts",
    "/api/mediaplans/{plan_id}/import-contracts/{sheet_name:path}",
)


def _mounted_paths() -> set[str]:
    """Every path the composed server actually serves."""
    from core.admin_api import router

    return {getattr(route, "path", "") for route in router.routes}


# ---------------------------------------------------------------------------
# The door
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("address", RETIRED_ADDRESSES)
def test_a_retired_import_address_is_mounted_by_nothing(address: str) -> None:
    """Nothing answers here, and nothing may start answering here again.

    The replacement is not another address on this surface: a plan is ingested
    like every other file source, through the carrier Datastream's import chain.
    """
    assert address not in _mounted_paths(), (
        f"{address} has been remounted. It was retired with the second xlsx "
        "engine on 2026-08-24; a plan now reaches app.media_plan_lines through "
        "import_runner.run_import() with a Template declaring "
        'landing_target: "plan_store". See file-source-ingestion.md, amendment '
        "« the second xlsx engine is retired »."
    )


def test_the_guard_is_not_vacuous() -> None:
    """The router really carries paths -- an empty set would pass anything.

    And the sibling addresses of the same surface are still mounted, which is
    what tells "these three were retired" from "the whole module failed to
    import and every assertion above passed for the wrong reason".
    """
    mounted = _mounted_paths()
    assert len(mounted) > 300, len(mounted)
    for surviving in (
        "/api/mediaplans/{plan_id}",
        "/api/mediaplans/{plan_id}/pacing",
        "/api/mediaplans/versions/{version_id}/publish",
    ):
        assert surviving in mounted, (
            f"{surviving} is gone too. The retirement was of the IMPORT surface "
            "only; a plan must still be readable, publishable and diffable."
        )


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def test_the_second_xlsx_engine_module_is_gone() -> None:
    """`core.mediaplan_import` must not exist, under that name or any other.

    Keeping the module unmounted-but-importable is what this repository calls an
    orphan door: it survives, someone reads it, and it comes back. Here there is
    nothing to preserve -- every parser property it held is either expressed by
    the one engine or written down as an unbuilt capability.
    """
    assert not (_CORE / "mediaplan_import.py").exists()
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("core.mediaplan_import")


def test_no_second_excel_walk_survives_in_the_mediaplan_surface() -> None:
    """The retirement is of a WALK, not of three URLs.

    `mediaplan_api` may describe the removal in its docstring -- that trace is
    the point -- but it must not open a workbook. The check is on executable
    code, so the record of the retirement cannot make the guard fail, and a
    plain text search cannot be loosened until it proves nothing.
    """
    import ast
    import io
    import tokenize

    source = (_CORE / "mediaplan_api.py").read_text(encoding="utf-8")
    ast.parse(source)
    code = " ".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT, tokenize.STRING)
    )
    for forbidden in ("load_workbook", "openpyxl", "mediaplan_import", "merged_cells"):
        assert forbidden not in code, (
            f"`{forbidden}` is back in executable code in mediaplan_api.py. "
            "There is ONE ingestion engine for files; a plan reaches it through "
            "run_import(), never through a walk of its own."
        )


# ---------------------------------------------------------------------------
# The successor, and the seam the doc forbids deleting
# ---------------------------------------------------------------------------


def test_the_successor_path_is_still_there() -> None:
    """The other half, and the one that is easy to forget.

    A retirement that says "X answers instead" becomes a lie the day X is itself
    removed, and the product is then left with no path at all while three
    docstrings keep pointing at one.
    """
    from core.file_source_plan_lines import reshape_workbook_to_lines
    from core.file_source_template import GRAIN_LINE, PLAN_LINE_FIELDS
    from core.import_landing import land_plan_store_rows

    assert callable(reshape_workbook_to_lines)
    assert callable(land_plan_store_rows)
    assert GRAIN_LINE == "line"
    # The vocabulary a plan-store Template is validated against. Losing a field
    # here would silently stop landing it: `land_plan_store_rows` iterates
    # exactly this tuple.
    assert set(PLAN_LINE_FIELDS) == {
        "line_key",
        "label",
        "channel",
        "start_date",
        "end_date",
        "budget",
        "buy_mode",
        "is_plan_only",
    }


def test_the_per_sheet_seam_is_not_deleted_as_dead_code() -> None:
    """`walk_sheet_lines` has no caller, and that is written down, not fixed here.

    The amendment names it: the one engine cannot yet give each sheet its OWN
    column mapping, which the retired path could, and `walk_sheet_lines` is the
    seam that capability will be built on. Deleting it as unreachable code is an
    `Incomplete if` of this surface -- so the absence of a caller must not be
    read as permission to remove it.
    """
    from core.file_source_plan_lines import LineKeyRegistry, walk_sheet_lines

    assert callable(walk_sheet_lines)
    # It takes a CALLER-OWNED registry: that is the whole reason it exists, and
    # a signature change that dropped it would quietly lose cross-sheet identity.
    import inspect

    assert "registry" in inspect.signature(walk_sheet_lines).parameters
    assert callable(LineKeyRegistry)
