"""toorow -- tests for the reshape-to-daily canonical-rows producer (Story 22.10).

OFFLINE / pure: every fixture workbook is built in-memory with openpyxl (no
committed binaries). Proves the AC on an AXA-shaped messy media plan: a top
metadata block, a header NOT on row 1, merged cells, grouped lines with
subtotal/total rows, and per-line Start/Ende date ranges exploded to DAILY rows
whose amounts sum back to the line total (to the cent), output as a ParseResult
of canonical rows.
"""

from __future__ import annotations

import io
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from core.csv_excel_import import ParseResult, run_import
from core.file_source_producer import (
    RULE_GROUP_OR_NOISE,
    RULE_INVERTED_DATES,
    RULE_MISSING_DATES,
    RULE_SUBTOTAL_OR_TOTAL,
    ReshapeProducerError,
    ReshapeSpec,
    canonical_rows_signature,
    detect_source_drift,
    landed_total,
    produce,
    read_source_columns,
    replay_locked_template,
    reshape_workbook_to_daily,
    stamp_placement,
)

openpyxl = pytest.importorskip("openpyxl")


# ---------------------------------------------------------------------------
# In-memory workbook builder
# ---------------------------------------------------------------------------


def _wb_bytes(build) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    build(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write(ws, rows, *, at_row=1):
    for r, row in enumerate(rows, start=at_row):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)


# A realistic AXA-shaped sheet: a top metadata block (rows 1-4), a blank spacer,
# a header on row 6 (NOT row 1), then grouped lines with a subtotal + a grand
# total, plus per-line German dotted date ranges.
def _axa_like(wb):
    ws = wb.create_sheet("DV360")
    _write(
        ws,
        [
            ["Client:", "AXA"],
            ["Campaign:", "MCC Health"],
            ["Planned Budget:", 3000],
            ["Campaign-Period:", "11.07.2022-31.10.2022"],
            [],  # blank spacer
            ["Vermarkter", "Start", "Ende", "Bruttokosten Gesamt"],  # header (row 6)
            ["Xcross Incremental", None, None, None],  # group-label row -> noise
            ["Google DV360", "01.03.2026", "01.03.2026", 1000],  # 1-day line
            ["IAS Verification", "01.03.2026", "03.03.2026", 100],  # 3-day line
            ["TOTAL Xcross Incremental", None, None, 1100],  # subtotal -> skip
            ["TOTAL", None, None, 1100],  # grand total -> skip
        ],
    )


_SPEC = ReshapeSpec(
    fields={
        "vendor": "Vermarkter",
        "start": "Start",
        "end": "Ende",
        "cost": "Bruttokosten Gesamt",
    },
    amount_field="cost",
    start_field="start",
    end_field="end",
    date_field="date",
    sheet_name="DV360",
    header_row=6,
    metadata_rows=(1, 4),
    date_format="%d.%m.%Y",
)


# ---------------------------------------------------------------------------
# Happy path: metadata + header detect + skips + daily explode
# ---------------------------------------------------------------------------


def test_reshape_produces_daily_rows_sum_preserving():
    result = reshape_workbook_to_daily(_wb_bytes(_axa_like), _SPEC)
    assert isinstance(result, ParseResult)

    # 1-day line (1000) -> 1 row; 3-day line (100) -> 3 rows (33.34/33.33/33.33).
    assert len(result.rows) == 4

    one_day = [r for r in result.rows if r["vendor"] == "Google DV360"]
    assert one_day == [{"vendor": "Google DV360", "date": "2026-03-01", "cost": "1000.00"}]

    three_day = [r for r in result.rows if r["vendor"] == "IAS Verification"]
    assert [r["date"] for r in three_day] == ["2026-03-01", "2026-03-02", "2026-03-03"]
    assert [r["cost"] for r in three_day] == ["33.34", "33.33", "33.33"]
    # Sum-preserving to the cent.
    assert sum(Decimal(r["cost"]) for r in three_day) == Decimal("100.00")


def test_reshape_extracts_metadata_block():
    result = reshape_workbook_to_daily(_wb_bytes(_axa_like), _SPEC)
    assert result.metadata["Client"] == "AXA"
    assert result.metadata["Campaign"] == "MCC Health"
    assert result.metadata["Planned Budget"] == "3000"
    assert result.metadata["Campaign-Period"] == "11.07.2022-31.10.2022"


def test_reshape_skips_subtotal_total_and_group_rows():
    result = reshape_workbook_to_daily(_wb_bytes(_axa_like), _SPEC)
    rules = {rr.rule for rr in result.rejected}
    assert RULE_SUBTOTAL_OR_TOTAL in rules  # TOTAL Xcross + grand TOTAL
    assert RULE_GROUP_OR_NOISE in rules  # "Xcross Incremental" group-label row
    # The two subtotal/total rows are BOTH rejected, never landed.
    subtotal = [rr for rr in result.rejected if rr.rule == RULE_SUBTOTAL_OR_TOTAL]
    assert len(subtotal) == 2
    # No total amount leaked into the daily rows (would double-count).
    assert all(r["vendor"] not in ("TOTAL", "TOTAL Xcross Incremental") for r in result.rows)


def test_reshape_conserves_total_landed_equals_sum_of_line_totals():
    # F-1 spirit: the landed daily total == the sum of the (skipped-subtotal-free)
    # line totals, 1000 + 100 == 1100, and the grand TOTAL row is NOT added.
    result = reshape_workbook_to_daily(_wb_bytes(_axa_like), _SPEC)
    landed = sum(Decimal(r["cost"]) for r in result.rows)
    assert landed == Decimal("1100.00")


# ---------------------------------------------------------------------------
# Merged cells (rule a) resolve via the shared reshape engine
# ---------------------------------------------------------------------------


def test_reshape_resolves_merged_descriptive_cell():
    def build(wb):
        ws = wb.create_sheet("S")
        _write(
            ws,
            [
                ["Vermarkter", "Start", "Ende", "Cost"],
                ["Merged Vendor", "01.03.2026", "02.03.2026", 200],
                [None, "03.03.2026", "04.03.2026", 200],  # vendor merged from above
            ],
        )
        ws.merge_cells("A2:A3")  # vendor spans both lines

    spec = ReshapeSpec(
        fields={"vendor": "Vermarkter", "start": "Start", "end": "Ende", "cost": "Cost"},
        amount_field="cost", start_field="start", end_field="end",
        sheet_name="S", header_row=1, date_format="%d.%m.%Y",
    )
    result = reshape_workbook_to_daily(_wb_bytes(build), spec)
    # Both lines (4 daily rows) carry the merged top-left vendor value.
    assert len(result.rows) == 4
    assert all(r["vendor"] == "Merged Vendor" for r in result.rows)


# ---------------------------------------------------------------------------
# Header auto-detection (header not on row 1, no explicit header_row)
# ---------------------------------------------------------------------------


def test_reshape_auto_detects_header_row():
    spec = ReshapeSpec(
        fields={"vendor": "Vermarkter", "start": "Start", "end": "Ende",
                "cost": "Bruttokosten Gesamt"},
        amount_field="cost", start_field="start", end_field="end",
        sheet_name="DV360", header_row=None,  # auto-detect
        metadata_rows=(1, 4), date_format="%d.%m.%Y",
    )
    result = reshape_workbook_to_daily(_wb_bytes(_axa_like), spec)
    assert len(result.rows) == 4  # same as the explicit-header run


# ---------------------------------------------------------------------------
# Honest rejection: amount without dates / inverted dates / no numeric amount
# ---------------------------------------------------------------------------


def test_reshape_rejects_amount_without_dates():
    def build(wb):
        ws = wb.create_sheet("S")
        _write(ws, [
            ["Name", "Start", "Ende", "Cost"],
            ["No dates", None, None, 500],  # amount but no date range
        ])
    spec = ReshapeSpec(
        fields={"name": "Name", "start": "Start", "end": "Ende", "cost": "Cost"},
        amount_field="cost", start_field="start", end_field="end",
        sheet_name="S", header_row=1,
    )
    result = reshape_workbook_to_daily(_wb_bytes(build), spec)
    assert result.rows == []
    assert [rr.rule for rr in result.rejected] == [RULE_MISSING_DATES]


def test_reshape_rejects_inverted_dates():
    def build(wb):
        ws = wb.create_sheet("S")
        _write(ws, [
            ["Name", "Start", "Ende", "Cost"],
            ["Backwards", "05.03.2026", "01.03.2026", 500],
        ])
    spec = ReshapeSpec(
        fields={"name": "Name", "start": "Start", "end": "Ende", "cost": "Cost"},
        amount_field="cost", start_field="start", end_field="end",
        sheet_name="S", header_row=1, date_format="%d.%m.%Y",
    )
    result = reshape_workbook_to_daily(_wb_bytes(build), spec)
    assert result.rows == []
    assert [rr.rule for rr in result.rejected] == [RULE_INVERTED_DATES]


# ---------------------------------------------------------------------------
# Structural failures raise (never a silent empty result)
# ---------------------------------------------------------------------------


def test_reshape_raises_on_unreadable_bytes():
    with pytest.raises(ReshapeProducerError) as exc:
        reshape_workbook_to_daily(b"not an xlsx", _SPEC)
    assert exc.value.code == "unreadable_workbook"


def test_reshape_raises_on_missing_column():
    def build(wb):
        ws = wb.create_sheet("S")
        _write(ws, [["Name", "Start", "Ende"], ["x", "01.03.2026", "02.03.2026"]])
    spec = ReshapeSpec(
        fields={"name": "Name", "start": "Start", "end": "Ende", "cost": "Cost"},
        amount_field="cost", start_field="start", end_field="end",
        sheet_name="S", header_row=1,
    )
    with pytest.raises(ReshapeProducerError) as exc:
        reshape_workbook_to_daily(_wb_bytes(build), spec)
    assert exc.value.code == "column_not_found"


def test_reshape_sample_only_bounds_source_lines():
    def build(wb):
        ws = wb.create_sheet("S")
        header = [["Name", "Start", "Ende", "Cost"]]
        lines = [[f"L{i}", "01.03.2026", "01.03.2026", 10] for i in range(20)]
        _write(ws, header + lines)
    spec = ReshapeSpec(
        fields={"name": "Name", "start": "Start", "end": "Ende", "cost": "Cost"},
        amount_field="cost", start_field="start", end_field="end",
        sheet_name="S", header_row=1, date_format="%d.%m.%Y",
    )
    result = reshape_workbook_to_daily(
        _wb_bytes(build), spec, sample_only=True, sample_line_limit=5
    )
    # Only the first 5 source lines are processed (each is a 1-day line -> 5 rows).
    assert len(result.rows) == 5
    assert result.detected_row_count == 5


# ---------------------------------------------------------------------------
# Story 22.12: unified produce() interface + run_import producer-path wiring
# ---------------------------------------------------------------------------


def _csv(rows: list[list[str]]) -> bytes:
    return "\n".join(",".join(r) for r in rows).encode("utf-8")


def test_produce_tabular_remaps_columns_to_canonical_ids():
    data = _csv([
        ["Vendor", "Spend", "Day"],
        ["Google", "100", "2026-03-01"],
        ["Meta", "200", "2026-03-02"],
    ])
    template = {"contract": {"kind": "catalog", "required_fields": ["mdm_cost"]}}
    mapping = {"Vendor": "mdm_channel", "Spend": "mdm_cost", "Day": "mdm_date"}
    result = produce(data, template, mapping)
    assert isinstance(result, ParseResult)
    assert result.rows == [
        {"mdm_channel": "Google", "mdm_cost": "100", "mdm_date": "2026-03-01"},
        {"mdm_channel": "Meta", "mdm_cost": "200", "mdm_date": "2026-03-02"},
    ]
    # Columns are keyed by canonical ids.
    assert {c.name for c in result.columns} == {"mdm_channel", "mdm_cost", "mdm_date"}


def test_produce_tabular_requires_mapping():
    data = _csv([["A", "B"], ["1", "2"]])
    with pytest.raises(ReshapeProducerError) as exc:
        produce(data, {"contract": {"kind": "catalog"}}, None)
    assert exc.value.code == "mapping_required"


def test_produce_dispatches_reshape_when_contract_has_reshape():
    # A media-plan catalog template carries a reshape sub-document -> daily explode.
    contract = {
        "kind": "catalog",
        "reshape": {
            "fields": {"vendor": "Vermarkter", "start": "Start", "end": "Ende",
                       "cost": "Bruttokosten Gesamt"},
            "amount_field": "cost", "start_field": "start", "end_field": "end",
            "sheet_name": "DV360", "header_row": 6, "metadata_rows": [1, 4],
            "date_format": "%d.%m.%Y",
        },
    }
    result = produce(_wb_bytes(_axa_like), {"contract": contract}, None)
    assert len(result.rows) == 4  # same daily explode as Story 22.10
    assert result.rows[0]["cost"] == "1000.00"


def test_produce_invalid_reshape_spec_raises():
    contract = {"kind": "catalog", "reshape": {"fields": {"a": "A"}}}  # missing amount/start/end
    with pytest.raises(ReshapeProducerError) as exc:
        produce(b"x", {"contract": contract}, None)
    assert exc.value.code == "invalid_reshape_spec"


def test_run_import_producer_path_lands_via_existing_chain():
    # AD-1: the producer plugs at run_import's parse step; the produced rows land
    # through the SAME open_import/record_rows chain, and version_contract is NOT
    # called (the file-source template is the versioned contract, Story 22.11).
    data = _csv([["Vendor", "Spend"], ["Google", "100"], ["Meta", "200"]])
    template = {"contract": {"kind": "catalog", "required_fields": ["mdm_cost"]}}
    mapping = {"Vendor": "mdm_channel", "Spend": "mdm_cost"}

    fake_ledger = {"id": "mfl_1", "outcome": "written", "execution_id": "dse_1"}
    fake_open = {"ledger": fake_ledger, "execution": {"id": "dse_1"},
                 "no_op": False, "replay": False}

    with (
        patch("core.csv_excel_import.version_contract") as mock_version,
        patch("core.managed_feed_ledger.open_import", return_value=fake_open) as mock_open,
        patch("core.managed_feed_ledger.record_rows", return_value=fake_ledger) as mock_record,
        patch("core.managed_feed_ledger.evaluate_rejection_gate_for_ledger", return_value=None),
        patch("core.managed_feed_ledger.allocate_landing_relation",
              return_value="main.managed_feed_ds_1"),
        patch("core.managed_feed_ledger.mark_outcome"),
    ):
        result = run_import(
            data,
            datastream_id="ds_1",
            project_id="proj_1",
            plan_version_id="dsp_1",
            mapping_version_id="dmap_1",
            projection_plan={"executable": True},
            actor="user@example.com",
            idempotency_key="fs-key-1",
            source_metadata={"filename": "plan.csv", "format": "csv",
                             "import_contract_id": "fst_TEMPLATE_1"},
            contract={},  # unused on the producer path
            conn=MagicMock(),
            producer=lambda d: produce(d, template, mapping),
        )

    # Landed via the existing chain, no second path.
    assert mock_open.called and mock_record.called
    assert result["row_count"] == 2  # the two produced canonical rows
    assert result["outcome"] == "written_pending_publication"
    assert result["published"] is False  # honest Phase B
    # version_contract is NOT called; the file-source template id is threaded.
    assert not mock_version.called
    assert result["import_contract_id"] == "fst_TEMPLATE_1"
    assert mock_open.call_args.kwargs["import_contract_id"] == "fst_TEMPLATE_1"
    # open_import received the produced format, and open_import called ONCE.
    assert mock_open.call_count == 1


# ---------------------------------------------------------------------------
# Story 22.14: matrix placement -- stamp class + declared discriminator
# ---------------------------------------------------------------------------


def test_stamp_placement_stamps_class_on_every_row():
    template = {"contract": {"kind": "catalog", "class": "planned"}}
    result = produce(
        _csv([["Vendor", "Spend"], ["Google", "100"], ["Meta", "200"]]),
        {"contract": {"kind": "catalog"}},
        {"Vendor": "mdm_channel", "Spend": "mdm_cost"},
    )
    stamped = stamp_placement(result, template)
    assert all(r["_placement_class"] == "planned" for r in stamped.rows)


def test_stamp_placement_refuses_no_class_ad6():
    result = produce(
        _csv([["A"], ["1"]]), {"contract": {"kind": "catalog"}}, {"A": "mdm_a"}
    )
    with pytest.raises(ReshapeProducerError) as exc:
        stamp_placement(result, {"contract": {"kind": "catalog"}})  # no class
    assert exc.value.code == "no_placement_class"


def test_stamp_placement_filename_discriminator_to_dimension():
    template = {"contract": {
        "kind": "catalog", "class": "actual",
        "discriminator": {"dimension": "mdm_market", "source": "filename",
                          "pattern": r"_(?P<value>[A-Z]{2})_"},
    }}
    result = produce(
        _csv([["Vendor", "Spend"], ["Google", "100"]]),
        {"contract": {"kind": "catalog"}},
        {"Vendor": "mdm_channel", "Spend": "mdm_cost"},
    )
    stamped = stamp_placement(result, template, filename="plan_DE_2026.csv")
    assert stamped.rows[0]["mdm_market"] == "DE"
    assert stamped.rows[0]["_placement_class"] == "actual"


def test_stamp_placement_cell_discriminator_from_metadata():
    # A reshape ParseResult carries a metadata block; a 'cell' discriminator reads it.
    contract = {
        "kind": "catalog", "class": "planned",
        "discriminator": {"dimension": "mdm_market", "source": "cell", "cell": "Client"},
        "reshape": {
            "fields": {"vendor": "Vermarkter", "start": "Start", "end": "Ende",
                       "cost": "Bruttokosten Gesamt"},
            "amount_field": "cost", "start_field": "start", "end_field": "end",
            "sheet_name": "DV360", "header_row": 6, "metadata_rows": [1, 4],
            "date_format": "%d.%m.%Y",
        },
    }
    result = produce(_wb_bytes(_axa_like), {"contract": contract}, None)
    stamped = stamp_placement(result, {"contract": contract})
    assert all(r["mdm_market"] == "AXA" for r in stamped.rows)  # metadata Client -> dimension


def test_landed_total_sums_amount_field():
    result = produce(_wb_bytes(_axa_like), {"contract": {
        "kind": "catalog",
        "reshape": {
            "fields": {"vendor": "Vermarkter", "start": "Start", "end": "Ende",
                       "cost": "Bruttokosten Gesamt"},
            "amount_field": "cost", "start_field": "start", "end_field": "end",
            "sheet_name": "DV360", "header_row": 6, "metadata_rows": [1, 4],
            "date_format": "%d.%m.%Y",
        }}}, None)
    assert landed_total(result, "cost") == Decimal("1100.00")


# ---------------------------------------------------------------------------
# Story 22.16: lock, replay (upload=email parity), drift re-validation
# ---------------------------------------------------------------------------


def _locked_tabular_template():
    return {
        "content_hash": "a" * 64,  # locked (Story 22.11)
        "contract": {
            "kind": "catalog", "class": "planned", "format": "csv",
            "required_fields": ["mdm_cost", "mdm_date"],
        },
    }


_TAB_MAPPING = {"Vendor": "mdm_channel", "Spend": "mdm_cost", "Day": "mdm_date"}


def test_replay_is_byte_identical_upload_equals_email():
    data = _csv([["Vendor", "Spend", "Day"], ["Google", "100", "2026-03-01"],
                 ["Meta", "200", "2026-03-02"]])
    template = _locked_tabular_template()
    # "upload" and "email" are only transports of the SAME bytes.
    upload = replay_locked_template(data, template, _TAB_MAPPING, filename="up.csv")
    email = replay_locked_template(data, template, _TAB_MAPPING, filename="mail.csv")
    assert canonical_rows_signature(upload) == canonical_rows_signature(email)
    assert upload.rows == email.rows


def test_replay_refuses_unlocked_template():
    data = _csv([["Vendor", "Spend"], ["Google", "100"]])
    unlocked = {"contract": {"kind": "catalog", "class": "planned",
                             "required_fields": ["mdm_cost"]}}  # no content_hash
    with pytest.raises(ReshapeProducerError) as exc:
        replay_locked_template(data, unlocked, {"Vendor": "mdm_channel", "Spend": "mdm_cost"})
    assert exc.value.code == "template_not_locked"


def test_drift_added_and_reordered_columns_still_land_required():
    template = _locked_tabular_template()
    # A drifted file: an ADDED column ("Note") and REORDERED columns.
    drifted = _csv([["Day", "Note", "Spend", "Vendor"],
                    ["2026-03-01", "hi", "100", "Google"]])
    cols = read_source_columns(drifted, template)
    drift = detect_source_drift(template, _TAB_MAPPING, cols)
    assert drift["status"] == "ok"  # required sources still present
    assert "Note" in drift["added_columns"]
    # Required fields still land (mapping is by NAME, order-insensitive).
    result = replay_locked_template(drifted, template, _TAB_MAPPING)
    assert result.rows[0]["mdm_cost"] == "100"
    assert result.rows[0]["mdm_date"] == "2026-03-01"


def test_drift_disappearing_required_field_needs_revalidation():
    template = _locked_tabular_template()
    # "Spend" (feeding required mdm_cost) has disappeared.
    drifted = _csv([["Vendor", "Day"], ["Google", "2026-03-01"]])
    cols = read_source_columns(drifted, template)
    drift = detect_source_drift(template, _TAB_MAPPING, cols)
    assert drift["status"] == "needs_revalidation"  # re-enters the AD-7 gate
    assert drift["missing_required_sources"] == ["Spend"]
