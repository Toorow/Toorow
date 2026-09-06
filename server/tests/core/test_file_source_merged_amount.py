"""toorow -- money is never forward-filled across a merged cell (chantier 67-25).

`cell_resolved` returns (value, range_id); the reshape walk used to discard the
range id and forward-fill the AMOUNT onto every row a merge covered. A merged
budget of 1200.00 spanning three sub-rows landed 3600.00 -- silently, because the
stated reconciliation invariant (`landed_total`) had no caller outside its tests.

Forward-fill is CORRECT for a descriptive cell and is a multiplication for money.
The distinction is taken on the declared amount field. These tests hold both
halves: the amount lands once on its anchor row, the label keeps forward-filling.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from core.file_source_producer import (
    RULE_MERGED_AMOUNT_CARRIED,
    ReshapeProducerError,
    ReshapeSpec,
    landed_total,
    reconcile_amounts,
    reshape_workbook_to_daily,
)

openpyxl = pytest.importorskip("openpyxl")


def _spec(**over) -> ReshapeSpec:
    base = dict(
        fields={"label": "Line", "start": "Start", "end": "End", "amount": "Budget"},
        amount_field="amount",
        start_field="start",
        end_field="end",
        label_field="label",
        sheet_name="Plan",
        header_row=1,
    )
    base.update(over)
    return ReshapeSpec(**base)


def _workbook(rows, *, merges=()) -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append(["Line", "Start", "End", "Budget"])
    for row in rows:
        ws.append(row)
    for rng in merges:
        ws.merge_cells(rng)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def test_merged_budget_lands_once_not_once_per_covered_row():
    """1200.00 merged over three sub-rows lands 1200.00 -- never 3600.00."""
    data = _workbook(
        [
            ["Spot A", "2026-01-01", "2026-01-02", 1200],
            ["Spot B", "2026-01-01", "2026-01-02", None],
            ["Spot C", "2026-01-01", "2026-01-02", None],
        ],
        merges=["D2:D4"],  # ONE budget over three physical sub-rows
    )
    result = reshape_workbook_to_daily(data, _spec())

    assert landed_total(result, "amount") == Decimal("1200.00")
    # The anchor row's two days carry it; the covered rows land nothing.
    assert len(result.rows) == 2

    carried = [r for r in result.rejected if r.rule == RULE_MERGED_AMOUNT_CARRIED]
    assert len(carried) == 2, "each covered non-anchor row is honest evidence"
    assert all("anchor row 2" in r.reason for r in carried)


def test_merged_budget_reconciles_against_the_file_total():
    """The invariant is measured, not decorative: file == landed + rejected."""
    data = _workbook(
        [
            ["Spot A", "2026-01-01", "2026-01-02", 1200],
            ["Spot B", "2026-01-01", "2026-01-02", None],
            ["Spot C", "2026-01-01", "2026-01-02", None],
        ],
        merges=["D2:D4"],
    )
    result = reshape_workbook_to_daily(data, _spec())

    amounts = reconcile_amounts(result, "amount")
    assert amounts == {
        "file": "1200.00",
        "landed": "1200.00",
        "rejected": "0.00",
        "reconciled": True,
    }


def test_a_merged_label_still_forward_fills():
    """Forward-fill stays for DESCRIPTIVE cells -- only money is anchored."""
    data = _workbook(
        [
            ["Brand campaign", "2026-01-01", "2026-01-01", 100],
            [None, "2026-01-02", "2026-01-02", 250],
        ],
        merges=["A2:A3"],  # ONE label covering two lines, each with its own budget
    )
    result = reshape_workbook_to_daily(data, _spec())

    # Both lines land, both carry the merged label, and no money is duplicated.
    assert len(result.rows) == 2
    assert [r["label"] for r in result.rows] == ["Brand campaign", "Brand campaign"]
    assert landed_total(result, "amount") == Decimal("350.00")
    assert reconcile_amounts(result, "amount")["reconciled"] is True


def test_rejected_money_is_counted_so_the_invariant_still_holds():
    """A readable amount on a rejected row is surfaced, never melted."""
    data = _workbook(
        [
            ["Spot A", "2026-01-01", "2026-01-02", 400],
            ["Spot B", None, None, 600],  # readable amount, no date range
            ["TOTAL", None, None, 1000],  # grand total, skipped for landing
        ]
    )
    result = reshape_workbook_to_daily(data, _spec())

    amounts = reconcile_amounts(result, "amount")
    assert amounts["landed"] == "400.00"
    # 600 (dateless line) + 1000 (grand total) stay visible on the rejected side.
    assert amounts["rejected"] == "1600.00"
    assert amounts["file"] == "2000.00"
    assert amounts["reconciled"] is True


def test_an_unplaceable_merged_amount_refuses_and_names_the_cell():
    """Dropping a merged amount would lose money silently -> refuse, naming it."""
    data = _workbook(
        [
            ["Spot A", None, None, 900],  # merged amount, no valid date range
            ["Spot B", None, None, None],
        ],
        merges=["D2:D3"],
    )
    with pytest.raises(ReshapeProducerError) as exc:
        reshape_workbook_to_daily(data, _spec())

    assert exc.value.code == "merged_amount_not_placeable"
    assert "D2:D3" in str(exc.value)
