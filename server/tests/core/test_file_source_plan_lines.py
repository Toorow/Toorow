"""toorow -- the LINE grain of the one file-source engine (chantier 67-25b).

`file-source-ingestion.md` ratified ONE ingestion engine and named the six
capabilities a media plan needs that the engine could not express. Two of them
(merged-amount collapse, the wired total invariant) landed in d2b94e16. These
tests hold the other four, which are what let a plan spreadsheet run on this
engine at all:

  * ``grain: "line"``   -- the line and its SPAN survive; nothing is spread;
  * ``sheets: [...]``   -- one import sees the whole workbook;
  * ``line_key``        -- rows have an identity, and a collision refuses;
  * ``merged_amount.explode`` -- the explicit split, exact-sum-or-422, bound to
    the merge's PHYSICAL A1 range so a shifted merge stops applying.

The money invariant is the daily grain's and is asserted here too: a grain that
counted a merge twice would be exactly the defect the amendment measured.
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from core.file_source_plan_lines import (
    DuplicateLineKey,
    reshape_workbook_to_lines,
    slugify,
)
from core.file_source_producer import (
    GRAIN_LINE,
    ReshapeProducerError,
    ReshapeSpec,
    landed_total,
    reconcile_amounts,
)

#: The REAL primitive, kept under a name the sabotage below cannot shadow. The
#: discriminant test patches `core.file_source_plan_lines.cell_resolved`; it must
#: still be able to call the honest one to build its wrong answer from.
from core.reshape import cell_resolved as _reshape_cell_resolved  # noqa: E402

openpyxl = pytest.importorskip("openpyxl")


HEADERS = ["Line", "Start", "End", "Budget"]


def _spec(**over) -> ReshapeSpec:
    base = dict(
        fields={
            "label": "Line",
            "start_date": "Start",
            "end_date": "End",
            "budget": "Budget",
        },
        amount_field="budget",
        start_field="start_date",
        end_field="end_date",
        label_field="label",
        sheet_name="Plan",
        header_row=1,
        grain=GRAIN_LINE,
        total_markers=(),
    )
    base.update(over)
    return ReshapeSpec(**base)


def _workbook(sheets: dict[str, list], *, merges: dict[str, list] | None = None) -> bytes:
    """Build an xlsx: {sheet_name: [row, ...]} plus optional per-sheet merges."""
    wb = openpyxl.Workbook()
    first = True
    for name, rows in sheets.items():
        ws = wb.active if first else wb.create_sheet()
        ws.title = name
        first = False
        ws.append(HEADERS)
        for row in rows:
            ws.append(row)
        for rng in (merges or {}).get(name, []):
            ws.merge_cells(rng)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# grain: "line"
# ---------------------------------------------------------------------------


def test_the_line_grain_keeps_the_span_and_never_spreads_to_daily():
    data = _workbook(
        {"Plan": [["Search", "01/01/2026", "05/01/2026", 500], ]}
    )
    result = reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))

    # Five days of coverage, ONE row: the plan store spreads at publish.
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["start_date"] == "2026-01-01"
    assert row["end_date"] == "2026-01-05"
    assert row["budget"] == "500.00"
    assert landed_total(result, "budget") == Decimal("500.00")


def test_the_line_grain_composes_a_line_key_from_sheet_and_label():
    data = _workbook({"Plan": [["Search Brand", "01/01/2026", "05/01/2026", 500]]})
    result = reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))
    assert result.rows[0]["line_key"] == "plan/search-brand"


def test_a_named_column_supplies_the_line_key_instead():
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Plan"
    ws.append([*HEADERS, "Key"])
    ws.append(["Search", "01/01/2026", "05/01/2026", 500, "IO-4471"])
    buf = io.BytesIO()
    wb.save(buf)

    result = reshape_workbook_to_lines(
        buf.getvalue(), _spec(date_format="%d/%m/%Y", line_key="Key")
    )
    assert result.rows[0]["line_key"] == "IO-4471"


# ---------------------------------------------------------------------------
# merged_amount: collapse (the default) and the explicit explode
# ---------------------------------------------------------------------------


def test_a_merged_amount_collapses_into_one_line_spanning_the_whole_group():
    data = _workbook(
        {
            "Plan": [
                ["Display FR", "01/01/2026", "10/01/2026", 1200],
                ["Display DE", "05/01/2026", "20/01/2026", None],
                ["Display IT", "03/01/2026", "15/01/2026", None],
            ]
        },
        merges={"Plan": ["D2:D4"]},
    )
    result = reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))

    # ONE line, the money ONCE, spanning min(start)..max(end) of the group.
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row["budget"] == "1200.00"
    assert row["start_date"] == "2026-01-01"
    assert row["end_date"] == "2026-01-20"
    assert landed_total(result, "budget") == Decimal("1200.00")
    assert reconcile_amounts(result, "budget")["reconciled"] is True


def test_an_explicit_explode_splits_the_merged_amount_into_exact_parts():
    data = _workbook(
        {
            "Plan": [
                ["Display FR", "01/01/2026", "10/01/2026", 1200],
                ["Display DE", "05/01/2026", "20/01/2026", None],
                ["Display IT", "03/01/2026", "15/01/2026", None],
            ]
        },
        merges={"Plan": ["D2:D4"]},
    )
    result = reshape_workbook_to_lines(
        data,
        _spec(
            date_format="%d/%m/%Y",
            merged_amount_explode={"D2:D4": ["500.00", "400.00", "300.00"]},
        ),
    )

    assert len(result.rows) == 3
    assert [r["budget"] for r in result.rows] == ["500.00", "400.00", "300.00"]
    assert [r["label"] for r in result.rows] == ["Display FR", "Display DE", "Display IT"]
    # Each exploded line keeps its OWN span, not the group's.
    assert result.rows[1]["start_date"] == "2026-01-05"
    assert result.rows[1]["end_date"] == "2026-01-20"
    # And the split is exact: the file total still reconciles.
    assert landed_total(result, "budget") == Decimal("1200.00")
    assert reconcile_amounts(result, "budget")["reconciled"] is True


def test_an_explode_that_does_not_sum_exactly_refuses():
    data = _workbook(
        {
            "Plan": [
                ["Display FR", "01/01/2026", "10/01/2026", 1200],
                ["Display DE", "05/01/2026", "20/01/2026", None],
            ]
        },
        merges={"Plan": ["D2:D3"]},
    )
    with pytest.raises(ReshapeProducerError) as exc:
        reshape_workbook_to_lines(
            data,
            _spec(
                date_format="%d/%m/%Y",
                merged_amount_explode={"D2:D3": ["500.00", "400.00"]},
            ),
        )
    assert exc.value.code == "explode_sum_mismatch"
    # The refusal names both numbers: 900 supplied against 1200 merged.
    assert "1200.00" in str(exc.value)
    assert "900.00" in str(exc.value)


def test_an_explode_keyed_on_a_shifted_merge_no_longer_applies():
    """The explode binds to PHYSICAL coordinates, never to a stale key.

    Next month's file puts the same merge one row lower. The stale key must not
    split it silently: the group collapses to one line, which is the safe
    default, and the money still lands exactly once.
    """
    data = _workbook(
        {
            "Plan": [
                ["Header band", None, None, None],
                ["Display FR", "01/01/2026", "10/01/2026", 1200],
                ["Display DE", "05/01/2026", "20/01/2026", None],
            ]
        },
        merges={"Plan": ["D3:D4"]},
    )
    result = reshape_workbook_to_lines(
        data,
        # The contract still names last month's range.
        _spec(
            date_format="%d/%m/%Y",
            merged_amount_explode={"D2:D3": ["600.00", "600.00"]},
        ),
    )
    assert len(result.rows) == 1
    assert result.rows[0]["budget"] == "1200.00"
    assert reconcile_amounts(result, "budget")["reconciled"] is True


# ---------------------------------------------------------------------------
# sheets: [...] -- ONE import sees the whole workbook
# ---------------------------------------------------------------------------


def test_several_sheets_land_in_one_import_with_one_file_total():
    data = _workbook(
        {
            "France": [["Search", "01/01/2026", "05/01/2026", 500]],
            "Germany": [["Search", "01/01/2026", "05/01/2026", 300]],
        }
    )
    result = reshape_workbook_to_lines(
        data, _spec(sheets=("France", "Germany"), date_format="%d/%m/%Y")
    )

    assert len(result.rows) == 2
    # The identity is sheet-scoped, so the same label on two sheets is two lines.
    assert [r["line_key"] for r in result.rows] == ["france/search", "germany/search"]
    assert landed_total(result, "budget") == Decimal("800.00")
    # ONE invariant over the WHOLE workbook, not one per sheet summed in a test.
    assert reconcile_amounts(result, "budget") == {
        "file": "800.00",
        "landed": "800.00",
        "rejected": "0.00",
        "reconciled": True,
    }


def test_a_duplicate_line_key_across_two_sheets_refuses_the_import():
    """The registry is SHARED across sheets, which is the whole point.

    Two sheets naming the same identity would silently keep one line and lose the
    other in a store keyed by line_key. It refuses instead, naming both sheets.
    """
    wb = openpyxl.Workbook()
    for idx, name in enumerate(("France", "Germany")):
        ws = wb.active if idx == 0 else wb.create_sheet()
        ws.title = name
        ws.append([*HEADERS, "Key"])
        ws.append(["Search", "01/01/2026", "05/01/2026", 500, "IO-4471"])
    buf = io.BytesIO()
    wb.save(buf)

    with pytest.raises(DuplicateLineKey) as exc:
        reshape_workbook_to_lines(
            buf.getvalue(),
            _spec(sheets=("France", "Germany"), date_format="%d/%m/%Y", line_key="Key"),
        )
    assert exc.value.code == "duplicate_line_key"
    assert "IO-4471" in str(exc.value)
    assert "France" in str(exc.value) and "Germany" in str(exc.value)


# ---------------------------------------------------------------------------
# The money invariant, at the line grain
# ---------------------------------------------------------------------------


def test_rejected_money_is_counted_so_the_invariant_still_holds():
    data = _workbook(
        {
            "Plan": [
                ["Search", "01/01/2026", "05/01/2026", 500],
                ["Dateless line", None, None, 250],
            ]
        }
    )
    result = reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))

    assert len(result.rows) == 1
    assert reconcile_amounts(result, "budget") == {
        "file": "750.00",
        "landed": "500.00",
        "rejected": "250.00",
        "reconciled": True,
    }


def test_a_merged_group_with_no_usable_date_is_rejected_once_not_per_row():
    data = _workbook(
        {
            "Plan": [
                ["Search", "01/01/2026", "05/01/2026", 500],
                ["Block A", None, None, 900],
                ["Block B", None, None, None],
                ["Block C", None, None, None],
            ]
        },
        merges={"Plan": ["D3:D5"]},
    )
    result = reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))

    amounts = reconcile_amounts(result, "budget")
    # 900 counted ONCE against three covered rows -- a triple count here is
    # exactly the defect the amendment measured on the daily grain.
    assert amounts == {
        "file": "1400.00",
        "landed": "500.00",
        "rejected": "900.00",
        "reconciled": True,
    }


def test_slugify_folds_accents_so_a_key_is_stable_across_locales():
    assert slugify("Télévision Régionale") == "television-regionale"


# ---------------------------------------------------------------------------
# MOVED HERE 2026-08-24, with the retirement of the second xlsx engine.
#
# `file-source-ingestion.md` said these properties must MOVE to the engine, not
# be deleted with `mediaplan_import`. They were the retired suite's discriminant
# half -- the part that could tell a working guard from a decorative one -- and
# the engine had no equivalent for any of them.
# ---------------------------------------------------------------------------


def test_an_fr_formatted_amount_string_parses_to_the_cent():
    """"1 234,56" is what an agency workbook actually contains.

    Thin and non-breaking spaces as thousands separators, a comma as the decimal
    point, and the cell typed as TEXT. The retired walk had its own coercion for
    this; the engine's `_parse_amount` is strictly wider, and nothing proved it
    on this shape.
    """
    # Written as escapes, not as literal exotic spaces: a NBSP pasted into source
    # is invisible in a diff and the next editor deletes it without knowing.
    nbsp = " "
    narrow = " "  # U+202F, the one Excel FR writes
    data = _workbook(
        {
            "Plan": [
                ["Search", "01/01/2026", "05/01/2026", f"1{nbsp}234,56"],
                ["Display", "01/01/2026", "05/01/2026", f"2{narrow}000,44"],
            ]
        }
    )
    result = reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))

    assert [r["budget"] for r in result.rows] == ["1234.56", "2000.44"]
    assert landed_total(result, "budget") == Decimal("3235.00")
    assert reconcile_amounts(result, "budget")["reconciled"] is True


def test_an_explode_weight_that_is_zero_or_negative_refuses_at_parse_time():
    """The template gate checks the weights; so does the walk, and on purpose.

    A contract can reach the walk without passing through
    `validate_template_contract` -- a hand-built ReshapeSpec, a replay of an
    older artifact -- and a negative weight there would silently move money
    between plan lines while still summing to the merged amount.
    """
    data = _workbook(
        {
            "Plan": [
                ["Display FR", "01/01/2026", "10/01/2026", 1200],
                ["Display DE", "05/01/2026", "20/01/2026", None],
            ]
        },
        merges={"Plan": ["D2:D3"]},
    )
    with pytest.raises(ReshapeProducerError) as exc:
        reshape_workbook_to_lines(
            data,
            _spec(
                date_format="%d/%m/%Y",
                merged_amount_explode={"D2:D3": ["1500.00", "-300.00"]},
            ),
        )
    assert exc.value.code == "explode_invalid_weight"
    assert "negative or zero weight" in str(exc.value)


def test_an_absurd_sheet_is_refused_before_any_per_cell_loop(monkeypatch):
    """The DoS bound fires on the DIMENSIONS, before the walk reads a cell.

    Lowering the bound is what makes this provable without committing a
    100k-row workbook: if the guard ran after the loop, a bound of 1 would still
    let a two-row sheet be walked in full first.
    """
    from core import file_source_plan_lines as fspl

    data = _workbook({"Plan": [["Search", "01/01/2026", "05/01/2026", 500]]})
    monkeypatch.setattr(fspl, "MAX_SHEET_ROWS", 1)

    with pytest.raises(ReshapeProducerError) as exc:
        reshape_workbook_to_lines(data, _spec(date_format="%d/%m/%Y"))
    assert exc.value.code == "sheet_too_large"


# ---------------------------------------------------------------------------
# The invariant is DISCRIMINANT -- it can fail, so its passing means something.
# ---------------------------------------------------------------------------


def _sabotage_the_walk(monkeypatch):
    """Make the walk forget that a merged cell IS a merge -- the 2026-08-17 defect.

    `cell_resolved` returns `(value, range_id)`. Dropping the range id is exactly
    what `file_source_producer.py:403` used to do: every row a merge covered got
    the top-left AMOUNT as if it were its own, and 1200 landed three times.

    This patches ONLY `core.file_source_plan_lines.cell_resolved`. The file-side
    total is computed by `file_source_producer._source_amount_total`, which
    resolves its own binding -- so the sabotaged walk cannot also move the number
    it is compared against. That separation is the entire reason the invariant is
    worth running; if one patch moved both, every assertion here would be x == x.
    """
    real = _reshape_cell_resolved

    def forgets_the_merge(ws, merges, row, col):
        value, _range_id = real(ws, merges, row, col)
        return value, None

    monkeypatch.setattr(
        "core.file_source_plan_lines.cell_resolved", forgets_the_merge
    )


def _merged_workbook() -> bytes:
    return _workbook(
        {
            "Plan": [
                ["Display FR", "01/01/2026", "10/01/2026", 1200],
                ["Display DE", "05/01/2026", "20/01/2026", None],
                ["Display IT", "03/01/2026", "15/01/2026", None],
            ]
        },
        merges={"Plan": ["D2:D4"]},
    )


def test_a_walk_that_double_counts_a_merge_breaks_the_invariant(monkeypatch):
    """The negative case the engine never had.

    Every other assertion in this file reads `reconciled is True`. On its own
    that cannot distinguish a sound invariant from one comparing a number to
    itself -- and this repository has met that exact defect before. Here the walk
    is deliberately made wrong, and the invariant must SAY SO.
    """
    _sabotage_the_walk(monkeypatch)
    result = reshape_workbook_to_lines(_merged_workbook(), _spec(date_format="%d/%m/%Y"))

    # The defect, reproduced: three lines at the full merged amount.
    assert len(result.rows) == 3
    assert landed_total(result, "budget") == Decimal("3600.00")

    amounts = reconcile_amounts(result, "budget")
    assert amounts["file"] == "1200.00", "the independent pass stays honest"
    assert amounts["landed"] == "3600.00"
    assert amounts["reconciled"] is False


def test_the_producer_BLOCKS_a_result_whose_money_does_not_reconcile(monkeypatch):
    """And the other half: a false invariant must stop the landing, not be logged.

    `landed_total` computed half of this check for weeks and had no caller, which
    is precisely why the triple-count was silent. The wiring
    (`file_source_resolution.prepare_for_landing`) had no test of its refusing
    branch at all -- only of the branch where everything reconciles.
    """
    from core.file_source_resolution import FileSourceProducer

    _sabotage_the_walk(monkeypatch)
    result = reshape_workbook_to_lines(_merged_workbook(), _spec(date_format="%d/%m/%Y"))

    producer = FileSourceProducer(
        {"contract": {"reshape": {"amount_field": "budget"}}, "content_hash": "x" * 64},
        mapping={},
        template_id="fst_test",
        catalog_governed=True,
    )
    _unchanged, blocked = producer.prepare_for_landing(result, filename="plan.xlsx")

    assert blocked is not None, "a workbook whose money does not add up must not land"
    assert blocked["reason"] == "amount_reconciliation_failed"
    assert blocked["outcome"] == "amount_reconciliation_blocked"
    # The refusal carries the three numbers, so a person can see WHICH side moved
    # rather than being told only that something did.
    assert blocked["amounts"]["file"] == "1200.00"
    assert blocked["amounts"]["landed"] == "3600.00"
