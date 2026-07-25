"""toorow -- Shared reshape primitive: merged-cell resolution (Story 22.9).

Extracted verbatim (no behaviour change) from ``core.mediaplan_import`` so that
ONE merged-cell resolver serves both the media-plan import (Story 22.1) and the
file-source ingestion producer (Epic 22 Phase B, AD-5).

Merged-cell policy (rule a of the import contract): a covered cell reads the
top-left value of its merge and the A1 range id; an unmerged cell reads its own
value and ``None``. The range id (e.g. ``"C4:C6"``) is stable and human-readable
-- used as an explode key and in rejection reasons by the callers.

openpyxl worksheets are ``Any``-typed here to keep the primitive free of an
import-time openpyxl dependency (callers already load the workbook).
"""

from __future__ import annotations

from typing import Any


def resolve_merges(ws: Any) -> dict[tuple[int, int], tuple[Any, str]]:
    """Build {(row, col) -> (top_left_value, range_id)} for every merged cell.

    row/col are 1-based (openpyxl convention). range_id is the A1 range string
    (e.g. "C4:C6"), stable and human-readable -- used as the explode key and in
    rejection reasons.
    """
    covered: dict[tuple[int, int], tuple[Any, str]] = {}
    for rng in ws.merged_cells.ranges:
        range_id = str(rng)
        top_left = ws.cell(row=rng.min_row, column=rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                covered[(r, c)] = (top_left, range_id)
    return covered


def cell_resolved(
    ws: Any, merges: dict[tuple[int, int], tuple[Any, str]], row: int, col: int
) -> tuple[Any, str | None]:
    """Return (value, range_id|None) for a cell, resolving merges (rule a).

    A covered cell reads the top-left value and its range id; an unmerged cell
    reads its own value and None.
    """
    if (row, col) in merges:
        return merges[(row, col)]
    return ws.cell(row=row, column=col).value, None
