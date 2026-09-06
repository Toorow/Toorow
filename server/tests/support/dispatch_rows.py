"""ONE dispatchable Datastream row, for every test that needs a run to start.

WHY THIS EXISTS. `core.datastream_dispatch.gate_refusal` answers `row_incomplete`
for a column the caller did not project, deliberately: a guard that silently
passes what it was not shown covers half a fleet and reads as if it covered all
of it. That strictness reaches the test doubles too -- a hand-rolled dict
missing `lifecycle_state` is not "a simpler row", it is a row the real SELECT
never produces.

So the doubles are built HERE, from one base that passes every gate, and each
case names only what it is about:

    dispatch_row(refetch_days=7)                     # a window case
    dispatch_row(cr_status="revoked")                # a gate case
    dispatch_row(source_kind="external_bq")          # story 12.7

Adding a gate means adding its column here once, and every double keeps working
or fails for a reason that is the gate's -- never for a reason that is the
fixture's.
"""

from __future__ import annotations

from typing import Any

__all__ = ["dispatch_row", "as_cursor_rows"]

#: A Datastream that passes every gate. Deliberately boring: a case that wants a
#: refusal says so by overriding exactly one key.
_BASE: dict[str, Any] = {
    "ds_id": "ds_001",
    "project_id": "proj_a",
    "module_name": "google-analytics",
    "refetch_days": 3,
    "date_window_days": None,
    "window_offset_days": None,
    "schedule_mode": "nightly",
    "source_kind": "connector_pull",
    "enabled": True,
    "lifecycle_state": "active",
    #: The column the soft archive really writes (2026-08-18). `None` is "not
    #: archived"; a case that wants the archive gate says `archived_at="..."`.
    "archived_at": None,
    "current_plan_version_id": "dsp_001",
    "current_mapping_version_id": "dsm_001",
    "connection_ref_id": "conn_x",
    "cr_status": "active",
    "cr_enabled": True,
    "module_enabled": True,
    "project_status": "active",
}


def dispatch_row(**overrides: Any) -> dict[str, Any]:
    """A row the dispatchers' SELECT would really return, plus *overrides*."""
    row = dict(_BASE)
    row.update(overrides)
    return row


def as_cursor_rows(rows: list[dict[str, Any]]) -> tuple[list[tuple], list[tuple]]:
    """`(description, fetchall)` for *rows* -- the shape psycopg hands back."""
    cols = list(rows[0].keys())
    return [(c,) for c in cols], [tuple(row[c] for c in cols) for row in rows]
