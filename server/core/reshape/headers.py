"""toorow -- Shared reshape primitive: header-row column resolution (Story 22.9).

Extracted verbatim (no behaviour change) from ``core.mediaplan_import`` so that
ONE header-detection / column-resolution primitive serves both the media-plan
import (Story 22.1) and the file-source ingestion producer (Epic 22 Phase B,
AD-5).

Resolves a contract column key (a header cell text, or an Excel column letter as
a fallback) to a 1-based column index on a declared header row.
"""

from __future__ import annotations

from typing import Any


def resolve_column_index(ws: Any, header_row: int, key: str) -> int | None:
    """Resolve a contract column key to a 1-based column index.

    ``key`` is either a header cell text (matched case-insensitively, trimmed,
    against the header row) or an Excel column letter (e.g. "C"). Header text is
    tried FIRST so a header that happens to read like a letter (e.g. "TV") is not
    mis-parsed as a column reference; a pure-letter key is used as a fallback only
    when no header matches. Returns None when unresolved.
    """
    from openpyxl.utils import column_index_from_string  # noqa: PLC0415

    stripped = key.strip()
    target = stripped.casefold()
    for col in range(1, ws.max_column + 1):
        val = ws.cell(row=header_row, column=col).value
        if val is not None and str(val).strip().casefold() == target:
            return col
    # Fallback: interpret the key as an Excel column letter (A, AB, ...).
    if stripped.isalpha() and stripped.isupper():
        try:
            return column_index_from_string(stripped)
        except ValueError:
            return None
    return None
