"""Every live path that appends a mapping version leaves an audit trail.

Written after a real incident, and it turned out NOT to be a code defect --
which is exactly why it deserves a test rather than a note.

On 2026-07-30 an operator script called `datastream_field_mapping.save_field_mapping`
directly to create the repository's first mapping version. The row landed and was
correct, but `app.operations` gained nothing: no audit, no outbox. A parallel
session spotted the gap and asked whether a route was writing unaudited.

None was. Every live caller already runs inside `operations.execute_operation`:

  * `datastream_change.confirm_change` -- the governed append the six-tab
    Workbench routes to;
  * `datastream_activation.materialize_draft_mutation` -- the wizard's
    activation, driven by `execute_confirmed_operation`.

The fourth definition, `admin_api._create_datastream_mapping_version`, is an
orphan that `26695dc` deliberately unmounted; a session remounted its route and
reverted that in `e6e33d1`.

So the property held by accident of who happened to call what. This test makes it
hold on purpose: a new writer that skips the operation seam fails here rather
than being discovered later by an audit row that does not exist.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CORE = ROOT / "server" / "core"

#: Modules allowed to call `save_field_mapping`, and why each is governed.
_GOVERNED_CALLERS = {
    # Both reach it only from a mutation running inside execute_operation.
    "datastream_change.py": "confirm_change, inside execute_operation",
    "datastream_activation.py": "materialize_draft_mutation, via execute_confirmed_operation",
    # The Story 12.3 owner itself.
    "datastream_field_mapping.py": "the definition",
    # Orphaned by 26695dc: defined, mounted nowhere. Kept as the inventory of a
    # retirement (see e6e33d1); it is not a live path.
    "admin_api.py": "orphaned handler, no route mounts it",
    # First-report draft path, which composes its own governed publication.
    "first_report_draft.py": "draft composition",
    # Human file-source confirmation mints the pending version inside its operation.
    "file_source_gate.py": "confirm_mapping_version mutation, inside execute_operation",
    # Country fan-out (epic 37). apply_country_plan_fan_out never opens its own
    # transaction: project_settings.py hands it the `inner_conn` of the change-set
    # confirmation and it writes with commit=False / advance_pointer=False, so the
    # append lives or dies with the execute_operation at project_settings.py:1243.
    "country_activation.py": "apply_country_plan_fan_out, inside execute_operation",
}


def _modules_calling(symbol: str) -> set[str]:
    found: set[str] = set()
    for path in CORE.glob("*.py"):
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = getattr(func, "id", None) or getattr(func, "attr", None)
                if name == symbol:
                    found.add(path.name)
    return found


def test_no_new_module_appends_a_mapping_version_outside_the_governed_seam():
    """A new caller must justify itself here, in the same change that adds it.

    Adding a module to `_GOVERNED_CALLERS` is cheap; the point is that it cannot
    happen silently. An unaudited mapping append is invisible precisely because
    what it fails to produce is a row nobody is looking for.
    """
    callers = _modules_calling("save_field_mapping")

    unexpected = sorted(callers - set(_GOVERNED_CALLERS))
    assert unexpected == [], (
        f"{unexpected} call save_field_mapping without being declared governed. "
        "Route the write through operations.execute_operation, or record here why "
        "it is not a live path."
    )


def test_the_orphaned_admin_handler_still_has_no_route():
    """The retirement `26695dc` made, re-asserted where a reader will see it.

    If this fails, someone remounted `POST .../mapping/versions`. That handler
    calls `save_field_mapping` with `advance_pointer` at its `True` default and
    outside any operation -- it would ACTIVATE the version it appends, unaudited,
    which both governed callers refuse explicitly.
    """
    source = (CORE / "admin_api.py").read_text("utf-8")

    assert "endpoint=_create_datastream_mapping_version" not in source, (
        "the orphaned mapping-append handler has been mounted again; see e6e33d1"
    )
