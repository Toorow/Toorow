"""toorow -- tests for the shared reshape engine (Story 22.9, AD-5).

Proves the extraction is REAL and self-contained: the mechanical primitives
(merged-cell resolution, header-row column resolution, cent-exact daily spread)
live in ONE ``core.reshape`` library, work standalone, and are the SAME engine the
media-plan domain now consumes -- no second Excel engine, no reinvented spread.

OFFLINE / pure: workbooks are built in-memory with openpyxl (no committed
binaries), no DB.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from core.reshape import (
    CENT,
    ReshapeError,
    ReshapeValidationError,
    cell_resolved,
    compute_spread,
    resolve_column_index,
    resolve_merges,
)

# openpyxl backs the workbook helpers below; skip the whole module if absent.
openpyxl = pytest.importorskip("openpyxl")


# ---------------------------------------------------------------------------
# In-memory workbook helper
# ---------------------------------------------------------------------------


def _sheet(rows: list[list], merges: list[str] | None = None):
    wb = openpyxl.Workbook()
    ws = wb.active
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)
    for rng in merges or []:
        ws.merge_cells(rng)
    # Round-trip through bytes so merged_cells behave exactly as on a loaded file.
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return openpyxl.load_workbook(buf, read_only=False, data_only=True).active


# ---------------------------------------------------------------------------
# compute_spread -- the cent-exact core, self-contained error type
# ---------------------------------------------------------------------------


def _sum(spread):
    return sum((amt for _d, amt in spread), Decimal("0"))


def test_spread_sum_is_cent_exact_and_deterministic():
    a = compute_spread(Decimal("100.00"), date(2026, 1, 1), date(2026, 1, 3))
    assert [amt for _d, amt in a] == [Decimal("33.34"), Decimal("33.33"), Decimal("33.33")]
    assert _sum(a) == Decimal("100.00")
    # Deterministic: identical inputs -> byte-identical rendering.
    b = compute_spread(Decimal("100.00"), date(2026, 1, 1), date(2026, 1, 3))
    assert [(d.isoformat(), format(amt, "f")) for d, amt in a] == [
        (d.isoformat(), format(amt, "f")) for d, amt in b
    ]


def test_spread_raises_reshape_validation_error_standalone():
    # The library carries its OWN typed error (no dependency on the media-plan
    # domain): a float budget, inverted dates, and a non-finite budget all raise
    # ReshapeValidationError (a ReshapeError, a ValueError).
    with pytest.raises(ReshapeValidationError):
        compute_spread(100.0, date(2026, 1, 1), date(2026, 1, 3))  # type: ignore[arg-type]
    with pytest.raises(ReshapeValidationError):
        compute_spread(Decimal("100.00"), date(2026, 1, 3), date(2026, 1, 1))
    with pytest.raises(ReshapeValidationError):
        compute_spread(Decimal("NaN"), date(2026, 1, 1), date(2026, 1, 3))
    assert issubclass(ReshapeValidationError, ReshapeError)
    assert issubclass(ReshapeError, ValueError)
    assert CENT == Decimal("0.01")


def test_media_plan_shares_the_same_spread_engine():
    # AD-5: the media-plan domain consumes the SAME engine (not a copy). Same
    # result, and its public wrapper re-raises reshape's error as the media-plan
    # typed error so the API still maps a bad budget to 422 (never a 500).
    from core.mediaplan_store import MediaPlanValidationError
    from core.mediaplan_store import compute_spread as mp_spread

    args = (Decimal("9999.99"), date(2026, 3, 1), date(2026, 5, 31))
    assert mp_spread(*args) == compute_spread(*args)
    with pytest.raises(MediaPlanValidationError):
        mp_spread(100.0, date(2026, 1, 1), date(2026, 1, 3))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Merged-cell resolution
# ---------------------------------------------------------------------------


def test_resolve_merges_maps_covered_cells_to_top_left_and_range_id():
    ws = _sheet(
        [
            ["Label", "Budget"],
            ["A", 9000],
            ["B", None],
            ["C", None],
        ],
        merges=["B2:B4"],
    )
    merges = resolve_merges(ws)
    # Every covered cell resolves to the top-left value + the A1 range id.
    for r in (2, 3, 4):
        assert merges[(r, 2)] == (9000, "B2:B4")
    # An unmerged cell is not in the map.
    assert (2, 1) not in merges


def test_cell_resolved_reads_top_left_for_covered_else_own_value():
    ws = _sheet(
        [["Label", "Budget"], ["A", 9000], ["B", None]],
        merges=["B2:B3"],
    )
    merges = resolve_merges(ws)
    # Covered sub-row reads the merge's top-left value + range id.
    assert cell_resolved(ws, merges, 3, 2) == (9000, "B2:B3")
    # Unmerged cell reads its own value + None.
    assert cell_resolved(ws, merges, 2, 1) == ("A", None)


# ---------------------------------------------------------------------------
# Header-row column resolution
# ---------------------------------------------------------------------------


def test_resolve_column_index_by_header_text_case_insensitive():
    ws = _sheet([["Libellé", "Budget", "Canal"], ["A", 1, "TV"]])
    assert resolve_column_index(ws, 1, "budget") == 2  # trimmed + casefolded
    assert resolve_column_index(ws, 1, "Canal") == 3


def test_resolve_column_index_prefers_header_text_over_letter_lookalike():
    # A header that reads like a column letter ("TV") must match as HEADER TEXT,
    # not be mis-parsed as an Excel column reference.
    ws = _sheet([["TV", "Budget"], [1, 2]])
    assert resolve_column_index(ws, 1, "TV") == 1


def test_resolve_column_index_letter_fallback_and_unresolved():
    ws = _sheet([["Libellé", "Budget"], ["A", 1]])
    # No header "C" -> fall back to the Excel column letter.
    assert resolve_column_index(ws, 1, "C") == 3
    # Neither a header nor a valid pure-letter key -> None.
    assert resolve_column_index(ws, 1, "Inexistant") is None
