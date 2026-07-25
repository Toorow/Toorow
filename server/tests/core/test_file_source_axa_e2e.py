"""toorow -- AXA media-plan end-to-end acceptance (Story 22.13).

Proves the framework runs a MESSY, multi-sheet, media-plan-shaped workbook end
to end as a `planned` catalog template over core/reshape/ (no AXA-specific code;
the template is fully declarative). The literal AXA binary is described in
reference-mediaplan-axa.md; here the same SHAPE is built in-memory (metadata
block, header not row 1, group/subtotal/total rows, German dotted per-line date
ranges) so the acceptance is deterministic and dependency-free.

Asserts the AC: lands as DAILY canonical rows classified `planned`, placed on the
declared metric/dimension/period, and the file total reconciles to the sum of the
landed rows (F-1 invariant).
"""

from __future__ import annotations

import io
from decimal import Decimal

import pytest
from core.file_source_producer import landed_total, produce, stamp_placement

openpyxl = pytest.importorskip("openpyxl")


def _write(ws, rows, *, at_row=1):
    for r, row in enumerate(rows, start=at_row):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)


def _axa_workbook() -> bytes:
    """A 2-sheet AXA-shaped media plan (one sheet per channel/vendor)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    dv360 = wb.create_sheet("DV360")
    _write(dv360, [
        ["Client:", "AXA"],
        ["Campaign:", "MCC Health"],
        ["Planned Budget:", 1100],
        ["Campaign-Period:", "01.03.2026-31.03.2026"],
        [],
        ["Vermarkter", "Start", "Ende", "Bruttokosten Gesamt"],  # header row 6
        ["Xcross Incremental", None, None, None],                 # group label
        ["Google DV360", "01.03.2026", "01.03.2026", 1000],       # 1-day line
        ["IAS Verification", "01.03.2026", "03.03.2026", 100],    # 3-day line
        ["TOTAL Xcross Incremental", None, None, 1100],           # subtotal
        ["TOTAL", None, None, 1100],                              # grand total
    ])

    social = wb.create_sheet("Social")
    _write(social, [
        ["Client:", "AXA"],
        ["Vermarkter", "Start", "Ende", "Bruttokosten Gesamt"],  # header row 2
        ["Meta", "01.03.2026", "02.03.2026", 500],               # 2-day line
        ["TOTAL", None, None, 500],                              # grand total
    ])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _planned_template(sheet_name: str, header_row: int, metadata_rows):
    """A declarative planned catalog template for one media-plan sheet."""
    return {"contract": {
        "kind": "catalog",
        "class": "planned",
        "grain": "daily",
        "placement": {"metric": "mdm_cost", "period": "mdm_date",
                      "dimension": ["mdm_channel"]},
        "reshape": {
            "fields": {"mdm_channel": "Vermarkter", "start": "Start", "end": "Ende",
                       "mdm_cost": "Bruttokosten Gesamt"},
            "amount_field": "mdm_cost", "start_field": "start", "end_field": "end",
            "date_field": "mdm_date", "label_field": "mdm_channel",
            "sheet_name": sheet_name, "header_row": header_row,
            "metadata_rows": metadata_rows, "date_format": "%d.%m.%Y",
        },
    }}


def test_axa_media_plan_lands_planned_daily_and_reconciles():
    data = _axa_workbook()
    sheets = [
        _planned_template("DV360", 6, [1, 4]),
        _planned_template("Social", 2, None),
    ]

    all_rows = []
    for template in sheets:
        result = stamp_placement(produce(data, template, None), template)
        # F-1 per sheet: landed total == the sheet's line totals (subtotal/TOTAL skipped).
        expected = {"DV360": Decimal("1100.00"), "Social": Decimal("500.00")}[
            template["contract"]["reshape"]["sheet_name"]
        ]
        assert landed_total(result, "mdm_cost") == expected
        all_rows.extend(result.rows)

    # Every landed row is classified `planned` (the plane).
    assert all(r["_placement_class"] == "planned" for r in all_rows)
    # Placed on the declared coordinates: metric (mdm_cost) + dimension (mdm_channel)
    # + period (mdm_date, the DAILY grain).
    assert all({"mdm_cost", "mdm_channel", "mdm_date"} <= set(r) for r in all_rows)
    # Daily grain: DV360 = 1 + 3 days, Social = 2 days => 6 landed rows.
    assert len(all_rows) == 6
    # F-1 overall: the whole file total reconciles to the sum of landed rows.
    total = sum(Decimal(r["mdm_cost"]) for r in all_rows)
    assert total == Decimal("1600.00")


def test_axa_dates_are_daily_and_sum_preserving():
    data = _axa_workbook()
    template = _planned_template("DV360", 6, [1, 4])
    result = stamp_placement(produce(data, template, None), template)
    ias = sorted(
        (r for r in result.rows if r["mdm_channel"] == "IAS Verification"),
        key=lambda r: r["mdm_date"],
    )
    # 100.00 over 3 days -> 33.34 / 33.33 / 33.33, summing to the cent.
    assert [r["mdm_date"] for r in ias] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    assert sum(Decimal(r["mdm_cost"]) for r in ias) == Decimal("100.00")
