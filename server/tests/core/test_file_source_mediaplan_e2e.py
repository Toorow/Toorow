"""toorow -- agency media-plan end-to-end acceptance (Story 22.13).

Proves the framework runs a MESSY, multi-sheet, media-plan-shaped workbook end
to end as a `planned` catalog template over core/reshape/ (no client-specific code;
the template is fully declarative). The literal agency binary is described in
reference-mediaplan-example.md; here the same SHAPE is built in-memory (metadata
block, header not row 1, group/subtotal/total rows, German dotted per-line date
ranges) so the acceptance is deterministic and dependency-free.

Asserts the AC: lands as DAILY canonical rows classified `planned`, placed on the
declared metric/dimension/period, and the file total reconciles to the sum of the
landed rows (F-1 invariant).
"""

from __future__ import annotations

import io
import json
from decimal import Decimal

import pytest
from core.file_source_producer import (
    detect_source_drift,
    landed_total,
    produce,
    read_source_columns,
    replay_locked_template,
    stamp_placement,
)
from core.file_source_template import compute_content_hash, validate_template_contract

openpyxl = pytest.importorskip("openpyxl")


def _write(ws, rows, *, at_row=1):
    for r, row in enumerate(rows, start=at_row):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)


def _example_workbook() -> bytes:
    """A 2-sheet agency-shaped media plan (one sheet per channel/vendor)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    dv360 = wb.create_sheet("DV360")
    _write(dv360, [
        ["Client:", "EXAMPLE"],
        ["Campaign:", "Example Campaign"],
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
        ["Client:", "EXAMPLE"],
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


def test_example_media_plan_lands_planned_daily_and_reconciles():
    data = _example_workbook()
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


def test_example_dates_are_daily_and_sum_preserving():
    data = _example_workbook()
    template = _planned_template("DV360", 6, [1, 4])
    result = stamp_placement(produce(data, template, None), template)
    ias = sorted(
        (r for r in result.rows if r["mdm_channel"] == "IAS Verification"),
        key=lambda r: r["mdm_date"],
    )
    # 100.00 over 3 days -> 33.34 / 33.33 / 33.33, summing to the cent.
    assert [r["mdm_date"] for r in ias] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    assert sum(Decimal(r["mdm_cost"]) for r in ias) == Decimal("100.00")


# ---------------------------------------------------------------------------
# The same media plan through the REAL write path.
#
# Every template above is a hand-built dict, which is how three defects shipped
# green: the contracts never met ``validate_template_contract`` (which persists
# ``json.dumps(normalised)``) and never carried a real seal. This acceptance
# builds the template the way ``create_file_source_template`` stores it.
# ---------------------------------------------------------------------------


def _dv360_sheet(headers) -> bytes:
    """The DV360 media-plan sheet with a substitutable header row (row 6)."""
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet("DV360")
    _write(ws, [
        ["Client:", "EXAMPLE"],
        ["Campaign:", "Example Campaign"],
        ["Planned Budget:", 1100],
        ["Campaign-Period:", "01.03.2026-31.03.2026"],
        [],
        list(headers),                                       # header row 6
        ["Google DV360", "01.03.2026", "01.03.2026", 1000],
        ["IAS Verification", "01.03.2026", "03.03.2026", 100],
        ["TOTAL", None, None, 1100],
    ])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


_PLAN_HEADERS = ["Vermarkter", "Start", "Ende", "Bruttokosten Gesamt"]

_PLAN_CONTRACT = {
    "kind": "catalog", "class": "planned", "grain": "daily",
    "required_fields": ["mdm_cost", "mdm_channel"],
    "placement": {"metric": "mdm_cost", "period": "mdm_date",
                  "dimension": ["mdm_channel"]},
    "discriminator": {"dimension": "mdm_market", "source": "filename",
                      "pattern": r"_(?P<value>[A-Z]{2})_"},
    "reshape": {
        "fields": {"mdm_channel": "Vermarkter", "start": "Start", "end": "Ende",
                   "mdm_cost": "Bruttokosten Gesamt"},
        "amount_field": "mdm_cost", "start_field": "start", "end_field": "end",
        "date_field": "mdm_date", "label_field": "mdm_channel",
        "sheet_name": "DV360", "header_row": 6, "metadata_rows": [1, 4],
        "date_format": "%d.%m.%Y",
    },
}


def _persisted_plan_template():
    normalised = validate_template_contract(_PLAN_CONTRACT)
    return {
        "content_hash": compute_content_hash(normalised),
        "contract": json.loads(json.dumps(normalised)),  # the JSONB round-trip
    }


def test_persisted_plan_template_replays_with_its_market_and_reconciles():
    template = _persisted_plan_template()
    result = replay_locked_template(
        _dv360_sheet(_PLAN_HEADERS), template, None, filename="plan_DE_2026.xlsx"
    )
    # The declared discriminator survived persistence -> a real market, not None.
    assert all(r["mdm_market"] == "DE" for r in result.rows)
    assert all(r["_placement_class"] == "planned" for r in result.rows)
    # F-1 still holds through the locked-replay path.
    assert landed_total(result, "mdm_cost") == Decimal("1100.00")


def test_persisted_plan_template_sees_a_renamed_source_column():
    template = _persisted_plan_template()
    # The agency re-exports the cost column under a new name.
    drifted = _dv360_sheet(["Vermarkter", "Start", "Ende", "Cout brut"])
    drift = detect_source_drift(
        template, None, read_source_columns(drifted, template)
    )
    assert drift["status"] == "needs_revalidation"
    assert drift["missing_required_sources"] == ["Bruttokosten Gesamt"]
