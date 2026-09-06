"""The comparison door of the mapping ledger — 2026-08-18.

Two things are pinned here and they are both about ORDER and INTENT, not about
the diff itself (`test_mapping_diff.py` holds that):

  * `.../mapping/versions/compare` must be declared BEFORE `.../{ver}`, or
    Starlette hands `compare` to the single-version handler and a comparison
    answers "version introuvable" forever;
  * it must stay a GET at `viewer`. Reading what changed between two recorded
    versions is not proposing a change, and the append stays behind
    `workbench/mapping/changes` -> `confirm`.
"""

from __future__ import annotations

import inspect

from core import datastream_mapping_api as mapping_api

_COMPARE = "/api/datastreams/{id}/mapping/versions/compare"
_ONE = "/api/datastreams/{id}/mapping/versions/{ver}"


def _paths() -> list[str]:
    return [route.path for route in mapping_api.DATASTREAM_MAPPING_ROUTES]


def test_compare_is_declared_before_the_single_version_route() -> None:
    paths = _paths()
    assert _COMPARE in paths, "the comparison door is not mounted"
    assert paths.index(_COMPARE) < paths.index(_ONE), (
        "`compare` is declared after `{ver}`; Starlette resolves in declaration "
        "order, so the path parameter swallows it and every comparison 404s"
    )


def test_compare_reads_and_never_writes() -> None:
    route = next(
        route for route in mapping_api.DATASTREAM_MAPPING_ROUTES if route.path == _COMPARE
    )
    assert set(route.methods) <= {"GET", "HEAD"}

    source = inspect.getsource(mapping_api._compare_datastream_mapping_versions)
    assert '"viewer"' in source, "history is read at viewer, like every other read here"
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "save_field_mapping"):
        assert forbidden not in source
