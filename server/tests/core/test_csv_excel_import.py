"""Offline unit tests for csv_excel_import.py (Story 12.9).

These tests run WITHOUT Postgres and WITHOUT any file I/O. Every workbook fixture
is constructed in-memory with openpyxl (no committed binaries -- AI-54 spirit).

Coverage map:
  VALIDATION MATRIX
    test_detect_format_*           -- format detection by magic bytes + extension.
    test_validate_upload_meta_*    -- size + empty guards.
    test_parse_csv_*               -- encoding, delimiter, duplicate columns,
                                      NoHeaderRow, column type inference.
    test_parse_excel_*             -- sheet resolution, merged-cell passthrough,
                                      FormulaInCells, DuplicateColumns.
    test_validate_import_contract  -- all contract fields + AppendUnavailable.
    test_build_preview_*           -- preview: no publish, bounded rows, hash.

  EMPTY-BLOCKED-BY-DEFAULT (AC4)
    test_run_import_empty_blocked_by_default
    test_run_import_empty_force_ok_with_preference
    test_run_import_empty_force_missing_preference_still_blocked

  APPEND-UNAVAILABLE (AC5)
    test_append_unavailable_via_run_import
    test_append_unavailable_via_contract

  DELEGATES-TO-LEDGER (no second writer)
    test_run_import_delegates_to_ledger
    test_run_import_replay_is_forwarded
    test_run_import_no_op_is_forwarded

The live DB invariants (contract uniqueness, ledger FK, immutability trigger,
rejection gate against real Postgres, end-to-end write + publication) are proven
in server/tests/integration/test_csv_excel_import_pg.py (pg-gated).
"""

from __future__ import annotations

import io
import os
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core.csv_excel_import import (  # noqa: E402
    _XLSX_MAGIC,
    FORMAT_CSV,
    FORMAT_EXCEL,
    PREVIEW_ROW_LIMIT,
    RULE_DATE_FORMAT,
    RULE_LONG_ROW,
    RULE_TYPE_MISMATCH,
    WRITE_MODE_APPEND,
    WRITE_MODE_REPLACE,
    AppendUnavailable,
    DuplicateColumns,
    EmptyFile,
    FileTooLarge,
    FormulaInCells,
    InvalidImportContract,
    NoHeaderRow,
    SheetNotFound,
    UnsupportedFileType,
    _content_hash,
    build_preview,
    contract_fingerprint,
    detect_format,
    parse_csv,
    parse_excel,
    run_import,
    validate_import_contract,
    validate_upload_meta,
)

# ---------------------------------------------------------------------------
# In-memory Excel builder helpers (no committed binaries).
# ---------------------------------------------------------------------------

openpyxl = pytest.importorskip("openpyxl")


def _xlsx_bytes(build_fn) -> bytes:
    """Build an xlsx workbook in-memory and return its bytes."""
    wb = openpyxl.Workbook()
    default = wb.active
    wb.remove(default)
    build_fn(wb)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _write_rows(ws, rows: list[list]) -> None:
    for r, row in enumerate(rows, start=1):
        for c, val in enumerate(row, start=1):
            ws.cell(row=r, column=c, value=val)


def _simple_csv(rows: list[list[str]], *, delimiter: str = ",") -> bytes:
    """Build a CSV bytes object from a list-of-lists."""
    lines = [delimiter.join(str(v) for v in row) for row in rows]
    return "\n".join(lines).encode("utf-8")


# ---------------------------------------------------------------------------
# Format detection.
# ---------------------------------------------------------------------------


class TestDetectFormat:
    def test_xlsx_magic_bytes_wins(self):
        # Even a .txt filename: magic bytes detect xlsx.
        data = _XLSX_MAGIC + b"\x00" * 100
        assert detect_format("report.txt", data) == FORMAT_EXCEL

    def test_csv_extension_no_magic(self):
        data = b"col_a,col_b\n1,2\n"
        assert detect_format("budget.csv", data) == FORMAT_CSV

    def test_tsv_extension(self):
        data = b"col_a\tcol_b\n1\t2\n"
        assert detect_format("data.tsv", data) == FORMAT_CSV

    def test_xlsx_extension_no_magic(self):
        # Extension alone (no magic match on short bytes).
        data = b"\x00\x00\x00\x00"
        assert detect_format("data.xlsx", data) == FORMAT_EXCEL

    def test_xls_extension(self):
        data = b"\xd0\xcf\x11\xe0"  # actual XLS magic
        assert detect_format("data.xls", data) == FORMAT_EXCEL

    def test_unknown_extension_raises(self):
        data = b"some data here"
        with pytest.raises(UnsupportedFileType):
            detect_format("data.pdf", data)

    def test_no_filename_no_magic_raises(self):
        data = b"some text without markers"
        with pytest.raises(UnsupportedFileType):
            detect_format(None, data)


# ---------------------------------------------------------------------------
# Upload meta validation.
# ---------------------------------------------------------------------------


class TestValidateUploadMeta:
    def test_empty_bytes_raises(self):
        with pytest.raises(EmptyFile):
            validate_upload_meta(b"")

    def test_all_whitespace_raises(self):
        with pytest.raises(EmptyFile):
            validate_upload_meta(b"   \n\t  ")

    def test_too_large_raises(self):
        big = b"x" * (1024 * 1024 + 1)
        with pytest.raises(FileTooLarge):
            validate_upload_meta(big, max_bytes=1024 * 1024)

    def test_normal_file_passes(self):
        data = b"col_a,col_b\n1,2\n"
        validate_upload_meta(data)  # no exception


# ---------------------------------------------------------------------------
# CSV parsing: basic flow.
# ---------------------------------------------------------------------------


class TestParseCsv:
    def test_simple_three_rows(self):
        data = _simple_csv(
            [
                ["name", "age", "score"],
                ["Alice", "30", "95.5"],
                ["Bob", "25", "80.0"],
                ["Carol", "28", "88.3"],
            ]
        )
        result = parse_csv(data)
        assert len(result.rows) == 3
        assert result.rows[0]["name"] == "Alice"
        assert result.rejected == []

    def test_content_hash_is_64_hex(self):
        data = b"a,b\n1,2\n"
        result = parse_csv(data)
        assert len(result.content_hash) == 64
        assert all(c in "0123456789abcdef" for c in result.content_hash)

    def test_content_hash_stable(self):
        data = b"a,b\n1,2\n"
        h1 = parse_csv(data).content_hash
        h2 = parse_csv(data).content_hash
        assert h1 == h2

    def test_different_bytes_different_hash(self):
        h1 = parse_csv(b"a,b\n1,2\n").content_hash
        h2 = parse_csv(b"a,b\n1,3\n").content_hash
        assert h1 != h2

    def test_utf8_sig_bom_decoded(self):
        # UTF-8 BOM (Excel-CSV export).
        data = "﻿col_a,col_b\n1,2\n".encode("utf-8-sig")
        result = parse_csv(data)
        assert "col_a" in [c.name for c in result.columns]

    def test_latin1_encoding_decoded(self):
        data = "prénom,âge\nAlice,30\n".encode("latin-1")
        result = parse_csv(data)
        assert len(result.rows) == 1

    def test_semicolon_delimiter_detected(self):
        data = "col_a;col_b\n1;2\n3;4\n".encode("utf-8")
        result = parse_csv(data)
        assert result.delimiter == ";"
        assert len(result.rows) == 2

    def test_tab_delimiter_detected(self):
        data = "col_a\tcol_b\n1\t2\n".encode("utf-8")
        result = parse_csv(data)
        assert result.delimiter == "\t"

    def test_explicit_delimiter_overrides_sniff(self):
        # Content looks like comma-separated but we force semicolon.
        data = "a,b\n1,2\n".encode("utf-8")
        result = parse_csv(data, delimiter=";")
        assert result.delimiter == ";"
        # With semicolon: header is one column "a,b".
        assert result.columns[0].name == "a,b"

    def test_duplicate_columns_raises(self):
        data = _simple_csv([["col", "col", "other"], ["1", "2", "3"]])
        with pytest.raises(DuplicateColumns) as exc_info:
            parse_csv(data)
        assert "col" in exc_info.value.detail

    def test_no_header_row_raises_when_empty_header(self):
        data = b",,,\n1,2,3,4\n"  # all-blank header
        with pytest.raises(NoHeaderRow):
            parse_csv(data)

    def test_blank_data_rows_skipped(self):
        data = "col_a,col_b\n1,2\n\n   ,   \n3,4\n".encode("utf-8")
        result = parse_csv(data)
        assert len(result.rows) == 2

    def test_formula_in_csv_blocked(self):
        # A tab-separated export from a spreadsheet that contains formulas.
        data = "a\tb\n=SUM(A1:A2)\t5\n".encode("utf-8")
        with pytest.raises(FormulaInCells):
            parse_csv(data, check_formulas=True)

    def test_formula_check_disabled(self):
        data = "a\tb\n=SUM(A1:A2)\t5\n".encode("utf-8")
        result = parse_csv(data, check_formulas=False)
        assert len(result.rows) == 1

    def test_column_type_integer_detected(self):
        data = "id,name\n1,Alice\n2,Bob\n".encode("utf-8")
        result = parse_csv(data)
        id_col = next(c for c in result.columns if c.name == "id")
        assert id_col.detected_type == "integer"

    def test_column_type_date_detected(self):
        data = "date,val\n2026-01-01,10\n2026-01-02,20\n".encode("utf-8")
        result = parse_csv(data)
        date_col = next(c for c in result.columns if c.name == "date")
        assert date_col.detected_type == "date"

    def test_column_type_decimal_detected(self):
        data = "price,item\n9.99,Widget\n19.50,Gadget\n".encode("utf-8")
        result = parse_csv(data)
        price_col = next(c for c in result.columns if c.name == "price")
        assert price_col.detected_type == "decimal"

    def test_too_many_columns_raises(self):
        header = ",".join(f"col_{i}" for i in range(10))
        data = (header + "\n" + ",".join("1" for _ in range(10)) + "\n").encode()
        with pytest.raises(Exception):
            parse_csv(data, max_columns=5)

    def test_empty_csv_returns_zero_rows(self):
        # A valid CSV with header but no data rows.
        data = b"col_a,col_b\n"
        result = parse_csv(data)
        assert len(result.rows) == 0
        assert result.columns  # columns still resolved from header


# ---------------------------------------------------------------------------
# C2: per-row rejection is POPULATED (not dead code) + the real gate blocks.
# ---------------------------------------------------------------------------


class TestPerRowRejection:
    def test_bad_dates_rejected_with_rule_and_row_number(self):
        # A 'date' column (2 good samples) then N garbage rows -> N rejections.
        rows = [
            ["date", "spend"],
            ["2026-01-01", "10"],
            ["2026-01-02", "20"],
            ["31/13/2026", "30"],  # impossible date
            ["not-a-date", "40"],  # garbage
            ["2026-99-99", "50"],  # out-of-range
        ]
        data = _simple_csv(rows)
        result = parse_csv(data)
        date_col = next(c for c in result.columns if c.name == "date")
        assert date_col.detected_type == "date"
        # Exactly the 3 bad rows are rejected; the 2 good rows are accepted.
        assert len(result.rejected) == 3
        assert len(result.rows) == 2
        for rr in result.rejected:
            assert rr.rule == RULE_DATE_FORMAT
            assert rr.field_name == "date"
        # Row numbers are the 1-based file line numbers (header=1, first data row=2).
        assert sorted(rr.row_number for rr in result.rejected) == [4, 5, 6]

    def test_n_bad_rows_reject_count_equals_n(self):
        # 2 good integer samples establish the 'integer' type; then N non-coercible.
        good = [["id"], ["1"], ["2"]]
        bad = [["abc"], ["x1"], ["12.3.4"], ["--"]]  # N = 4 non-integers
        data = _simple_csv(good + bad)
        result = parse_csv(data)
        assert next(c for c in result.columns if c.name == "id").detected_type == "integer"
        assert len(result.rejected) == len(bad)
        assert all(rr.rule == RULE_TYPE_MISMATCH for rr in result.rejected)

    def test_over_long_row_rejected(self):
        # A row with MORE fields than the header is a structural error.
        data = _simple_csv([["a", "b"], ["1", "2"], ["1", "2", "3", "4"]])
        result = parse_csv(data)
        assert len(result.rows) == 1
        assert len(result.rejected) == 1
        assert result.rejected[0].rule == RULE_LONG_ROW

    def test_short_row_pads_null_not_rejected(self):
        # A short row pads missing trailing fields with null (AD-9), not a rejection.
        data = _simple_csv([["a", "b", "c"], ["1", "2", "3"], ["9"]])
        result = parse_csv(data)
        assert len(result.rejected) == 0
        assert len(result.rows) == 2
        assert result.rows[1]["b"] is None and result.rows[1]["c"] is None

    def test_text_column_never_rejects(self):
        # A free-text column accepts anything (no false rejections).
        data = _simple_csv([["note"], ["hello"], ["=DANGER"], ["123abc"]])
        result = parse_csv(data, check_formulas=False)
        # note is text/mixed -> zero rejections.
        assert len(result.rejected) == 0

    def test_contract_date_format_mismatch_rejects(self):
        # Contract declares %Y-%m-%d but the column is d/m/Y -> every row rejected.
        rows = [["date"], ["01/02/2026"], ["03/04/2026"]]
        data = _simple_csv(rows)
        result = parse_csv(data, date_format="%Y-%m-%d")
        # date_format contract override forces the strict format; slashes don't match.
        assert len(result.rejected) == 2
        assert all(rr.rule == RULE_DATE_FORMAT for rr in result.rejected)

    def test_real_gate_blocks_above_threshold_unmocked(self):
        """The rejected rows drive a REAL evaluate_rejection_gate call (unmocked)."""
        from core.managed_feed_ledger import (
            GATE_REJECTION_THRESHOLD_EXCEEDED,
            evaluate_rejection_gate,
        )

        # 2 accepted, 4 rejected = 4/6 = 66.7% > default 25% -> blocked.
        good = [["id"], ["1"], ["2"]]
        bad = [["abc"], ["def"], ["ghi"], ["jkl"]]
        result = parse_csv(_simple_csv(good + bad))
        issue = evaluate_rejection_gate(
            accepted_row_count=len(result.rows),
            rejected_row_count=len(result.rejected),
            preferences=None,  # documented default 25%
        )
        assert issue is not None
        assert issue["code"] == GATE_REJECTION_THRESHOLD_EXCEEDED

    def test_real_gate_allows_below_threshold_unmocked(self):
        from core.managed_feed_ledger import evaluate_rejection_gate

        # 9 accepted, 1 rejected = 10% < 25% -> allowed (None).
        good = [["id"]] + [[str(i)] for i in range(9)]
        bad = [["bad"]]
        result = parse_csv(_simple_csv(good + bad))
        issue = evaluate_rejection_gate(
            accepted_row_count=len(result.rows),
            rejected_row_count=len(result.rejected),
            preferences=None,
        )
        assert issue is None


# ---------------------------------------------------------------------------
# Contract fingerprint (AC2 dedup oracle -- pure).
# ---------------------------------------------------------------------------


class TestContractFingerprint:
    def test_identical_contracts_same_fingerprint(self):
        a = validate_import_contract({"format": "csv", "header_row": 1, "delimiter": ";"})
        b = validate_import_contract({"delimiter": ";", "header_row": 1, "format": "csv"})
        assert contract_fingerprint(a) == contract_fingerprint(b)

    def test_different_config_different_fingerprint(self):
        a = validate_import_contract({"format": "csv", "header_row": 1})
        b = validate_import_contract({"format": "csv", "header_row": 2})
        assert contract_fingerprint(a) != contract_fingerprint(b)

    def test_fingerprint_is_64_hex(self):
        fp = contract_fingerprint(validate_import_contract({"format": "csv", "header_row": 1}))
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# H1: formula detection is delimiter-aware (comma/semicolon/pipe CSVs).
# ---------------------------------------------------------------------------


class TestFormulaDetectionDelimiterAware:
    def test_comma_csv_formula_detected(self):
        # H1 regression: `name,=SUM(B1)` was previously MISSED (tab-only split).
        data = "name,val\nAlice,=SUM(B1)\n".encode("utf-8")
        with pytest.raises(FormulaInCells):
            parse_csv(data)

    def test_semicolon_csv_formula_detected(self):
        data = "name;val\nBob;=A1*2\n".encode("utf-8")
        with pytest.raises(FormulaInCells):
            parse_csv(data)

    def test_pipe_csv_formula_detected(self):
        data = "name|val\nCarol|=NOW()\n".encode("utf-8")
        with pytest.raises(FormulaInCells):
            parse_csv(data)

    def test_leading_space_before_equals_still_detected(self):
        data = "a,b\nx, =SUM(A1)\n".encode("utf-8")
        with pytest.raises(FormulaInCells):
            parse_csv(data)


# ---------------------------------------------------------------------------
# H2: run_import re-detects format and rejects a mismatched contract.
# ---------------------------------------------------------------------------


class TestRunImportFormatRedetection:
    def test_csv_contract_over_xlsx_bytes_rejected(self):
        # A contract labelled csv over real xlsx bytes must NOT run parse_csv on binary.
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(ws, [["a", "b"], [1, 2]])

        xlsx = _xlsx_bytes(build)
        contract = {"format": "csv", "write_mode": "replace", "header_row": 1}
        with pytest.raises(InvalidImportContract):
            run_import(
                xlsx,
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="mismatch-1",
                source_metadata={"filename": "data.xlsx"},
                contract=contract,
                conn=MagicMock(),
            )


# ---------------------------------------------------------------------------
# Excel parsing.
# ---------------------------------------------------------------------------


class TestParseExcel:
    def _simple_sheet(self) -> bytes:
        def build(wb):
            ws = wb.create_sheet("Data")
            _write_rows(
                ws,
                [
                    ["product", "qty", "price"],
                    ["Widget", 10, 9.99],
                    ["Gadget", 5, 19.50],
                ],
            )

        return _xlsx_bytes(build)

    def test_simple_sheet_two_rows(self):
        data = self._simple_sheet()
        result = parse_excel(data)
        assert len(result.rows) == 2
        assert result.rows[0]["product"] == "Widget"
        assert result.sheet_name == "Data"
        assert result.delimiter is None  # always None for Excel

    def test_content_hash_64_hex(self):
        data = self._simple_sheet()
        result = parse_excel(data)
        assert len(result.content_hash) == 64
        assert all(c in "0123456789abcdef" for c in result.content_hash)

    def test_sheet_name_selection(self):
        def build(wb):
            ws1 = wb.create_sheet("Sheet1")
            _write_rows(ws1, [["a"], [1]])
            ws2 = wb.create_sheet("Budget")
            _write_rows(ws2, [["x", "y"], [10, 20]])

        data = _xlsx_bytes(build)
        result = parse_excel(data, sheet_name="Budget")
        assert result.sheet_name == "Budget"
        assert result.columns[0].name == "x"

    def test_unknown_sheet_raises(self):
        data = self._simple_sheet()
        with pytest.raises(SheetNotFound) as exc_info:
            parse_excel(data, sheet_name="NoSuchSheet")
        assert "NoSuchSheet" in exc_info.value.detail
        assert "Data" in exc_info.value.detail

    def test_duplicate_columns_raises(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(ws, [["col", "col", "other"], [1, 2, 3]])

        data = _xlsx_bytes(build)
        with pytest.raises(DuplicateColumns):
            parse_excel(data)

    def test_blank_rows_skipped(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(
                ws,
                [
                    ["a", "b"],
                    [1, 2],
                    [None, None],  # blank row -> skipped
                    [3, 4],
                ],
            )

        data = _xlsx_bytes(build)
        result = parse_excel(data)
        assert len(result.rows) == 2

    def test_zero_data_rows(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(ws, [["col_a", "col_b"]])  # header only

        data = _xlsx_bytes(build)
        result = parse_excel(data)
        assert len(result.rows) == 0
        assert len(result.columns) == 2

    def test_custom_header_row(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(
                ws,
                [
                    ["Report 2026"],  # metadata row
                    ["name", "score"],  # header on row 2
                    ["Alice", 95],
                ],
            )

        data = _xlsx_bytes(build)
        result = parse_excel(data, header_row=2)
        assert len(result.rows) == 1
        assert result.rows[0]["name"] == "Alice"

    def test_column_type_integer_detected(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(ws, [["id", "name"], [1, "A"], [2, "B"]])

        data = _xlsx_bytes(build)
        result = parse_excel(data)
        id_col = next(c for c in result.columns if c.name == "id")
        assert id_col.detected_type == "integer"

    def test_too_many_columns_raises(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            for i in range(1, 12):
                ws.cell(row=1, column=i, value=f"col_{i}")
            for i in range(1, 12):
                ws.cell(row=2, column=i, value=i)

        data = _xlsx_bytes(build)
        with pytest.raises(Exception):
            parse_excel(data, max_columns=5)


# ---------------------------------------------------------------------------
# Import contract validation.
# ---------------------------------------------------------------------------


class TestValidateImportContract:
    def _base(self, **overrides) -> dict:
        base = {
            "format": "csv",
            "write_mode": "replace",
            "header_row": 1,
        }
        base.update(overrides)
        return base

    def test_valid_csv_contract(self):
        c = validate_import_contract(self._base())
        assert c["format"] == "csv"
        assert c["write_mode"] == WRITE_MODE_REPLACE

    def test_valid_excel_contract(self):
        c = validate_import_contract(self._base(format="excel", sheet_name="Budget"))
        assert c["format"] == "excel"
        assert c["sheet_name"] == "Budget"

    def test_missing_format_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract({"write_mode": "replace", "header_row": 1})

    def test_unknown_format_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(format="parquet"))

    def test_append_write_mode_raises_append_unavailable(self):
        with pytest.raises(AppendUnavailable) as exc_info:
            validate_import_contract(self._base(write_mode="append"))
        assert exc_info.value.code == "append_unavailable"
        assert "replace" in exc_info.value.detail.lower()

    def test_unsupported_write_mode_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(write_mode="upsert"))

    def test_invalid_header_row_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(header_row=0))
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(header_row=-1))
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(header_row="1"))

    def test_invalid_delimiter_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(delimiter="!"))

    def test_valid_delimiter_passes(self):
        for delim in [",", ";", "\t", "|"]:
            c = validate_import_contract(self._base(delimiter=delim))
            assert c["delimiter"] == delim

    def test_unsupported_date_format_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(date_format="%A %B %Y"))

    def test_supported_date_format_passes(self):
        c = validate_import_contract(self._base(date_format="%Y-%m-%d"))
        assert c["date_format"] == "%Y-%m-%d"

    def test_unsupported_locale_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(locale="de"))

    def test_supported_locale_passes(self):
        for loc in ["en", "fr"]:
            c = validate_import_contract(self._base(locale=loc))
            assert c["locale"] == loc

    def test_not_a_dict_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract("format=csv")  # type: ignore[arg-type]

    def test_formulas_as_values_non_bool_raises(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract(self._base(format="excel", formulas_as_values="yes"))

    def test_default_formulas_as_values_is_true(self):
        c = validate_import_contract(self._base(format="excel"))
        assert c["formulas_as_values"] is True


# ---------------------------------------------------------------------------
# build_preview: no publish, bounded rows, hash.
# ---------------------------------------------------------------------------


class TestBuildPreview:
    def test_csv_preview_no_publish(self):
        rows = [["id", "name", "amount"]] + [
            [str(i), f"Name{i}", f"{i * 9.99:.2f}"] for i in range(1, 6)
        ]
        data = _simple_csv(rows)
        preview = build_preview(data, filename="test.csv")
        assert preview.format == FORMAT_CSV
        assert preview.row_count == 5
        assert preview.rejected_count == 0
        assert len(preview.preview_rows) == 5
        assert preview.content_hash  # non-empty

    def test_excel_preview_no_publish(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(ws, [["col_a", "col_b"]] + [[i, i * 2] for i in range(1, 11)])

        data = _xlsx_bytes(build)
        preview = build_preview(data, filename="data.xlsx")
        assert preview.format == FORMAT_EXCEL
        assert preview.row_count == 10
        assert preview.sheet_name == "Sheet1"

    def test_preview_bounded_to_limit(self):
        rows = [["id"]] + [[str(i)] for i in range(PREVIEW_ROW_LIMIT + 50)]
        data = _simple_csv(rows)
        preview = build_preview(data, filename="big.csv")
        # preview_rows is bounded but row_count is the full parse.
        assert len(preview.preview_rows) == PREVIEW_ROW_LIMIT
        assert preview.row_count == PREVIEW_ROW_LIMIT + 50

    def test_zero_row_preview_honest(self):
        data = b"col_a,col_b\n"
        preview = build_preview(data, filename="empty.csv")
        assert preview.row_count == 0
        assert preview.preview_rows == []

    def test_content_hash_is_deterministic(self):
        data = b"a,b\n1,2\n"
        h1 = build_preview(data, filename="f.csv").content_hash
        h2 = build_preview(data, filename="f.csv").content_hash
        assert h1 == h2

    def test_contract_format_mismatch_raises(self):
        data = _simple_csv([["a"], [1]])
        with pytest.raises(InvalidImportContract):
            build_preview(data, filename="data.csv", contract={"format": "excel", "header_row": 1})

    def test_empty_file_raises(self):
        with pytest.raises(EmptyFile):
            build_preview(b"", filename="empty.csv")

    def test_format_hint_overrides_extension(self):
        # Extension is .xlsx but hint says csv (contrived; exercises the code path).
        data = b"col_a,col_b\n1,2\n"
        preview = build_preview(data, filename="weird.xlsx", format_hint="csv")
        assert preview.format == FORMAT_CSV


# ---------------------------------------------------------------------------
# AC4: Empty-blocked-by-default.
# ---------------------------------------------------------------------------


class TestEmptyBlockedByDefault:
    """run_import with zero parsed rows blocks unless force_empty_publish + preference."""

    def _make_conn(self) -> MagicMock:
        return MagicMock()  # ledger not called when blocked early

    def test_empty_csv_blocked_by_default(self):
        data = b"col_a,col_b\n"  # header only, no data rows
        contract = {"format": "csv", "write_mode": "replace", "header_row": 1}
        result = run_import(
            data,
            datastream_id="ds_01",
            project_id="proj_1",
            plan_version_id="dsp_01",
            mapping_version_id="dmap_01",
            projection_plan={"executable": True},
            actor="user@example.com",
            idempotency_key="test-key-empty-1",
            source_metadata={"filename": "empty.csv"},
            contract=contract,
            conn=self._make_conn(),
            preferences=None,  # no preference set -> block
        )
        assert result["blocked"] is True
        assert result["reason"] == "empty_import"
        assert result["row_count"] == 0
        assert result["ledger"] is None  # ledger NOT opened for a trivially blocked run

    def test_empty_csv_blocked_with_force_but_no_preference(self):
        data = b"col_a,col_b\n"
        contract = {"format": "csv", "write_mode": "replace", "header_row": 1}
        result = run_import(
            data,
            datastream_id="ds_01",
            project_id="proj_1",
            plan_version_id="dsp_01",
            mapping_version_id="dmap_01",
            projection_plan={"executable": True},
            actor="user@example.com",
            idempotency_key="test-key-empty-2",
            source_metadata={"filename": "empty.csv"},
            contract=contract,
            conn=self._make_conn(),
            force_empty_publish=True,
            preferences={"allow_empty_publication": False},  # preference says no
        )
        assert result["blocked"] is True
        assert result["reason"] == "empty_import"

    def test_empty_csv_passes_with_force_and_preference(self):
        """force_empty_publish=True + allow_empty_publication=True -> ledger is opened."""
        data = b"col_a,col_b\n"
        contract = {"format": "csv", "write_mode": "replace", "header_row": 1}

        # Stub out the ledger calls at the managed_feed_ledger module level
        # (run_import uses lazy imports from core.managed_feed_ledger).
        fake_ledger = {
            "id": "mfl_test_01",
            "outcome": "written",
            "execution_id": "dse_test_01",
        }
        fake_result = {
            "ledger": fake_ledger,
            "execution": {"id": "dse_test_01"},
            "no_op": False,
            "replay": False,
        }

        with (
            patch("core.csv_excel_import.version_contract", return_value="cic_test_01"),
            patch("core.managed_feed_ledger.open_import", return_value=fake_result) as mock_open,
            patch("core.managed_feed_ledger.record_rows", return_value=fake_ledger) as mock_record,
            patch("core.managed_feed_ledger.evaluate_rejection_gate_for_ledger", return_value=None),
            patch(
                "core.managed_feed_ledger.allocate_landing_relation",
                return_value="main.managed_feed_ds_01",
            ),
            patch("core.managed_feed_ledger.mark_outcome"),
        ):
            result = run_import(
                data,
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="test-key-empty-3",
                source_metadata={"filename": "empty.csv"},
                contract=contract,
                conn=MagicMock(),
                force_empty_publish=True,
                preferences={"allow_empty_publication": True},
            )

        assert result["blocked"] is False
        assert result["published"] is False  # honest: written, not published (Phase B)
        assert result["outcome"] == "written_pending_publication"
        # AC2: the versioned contract id is threaded onto the result + into open_import.
        assert result["import_contract_id"] == "cic_test_01"
        assert mock_open.call_args.kwargs["import_contract_id"] == "cic_test_01"
        assert mock_open.called  # ledger was opened
        assert mock_record.called

    def test_excel_empty_blocked(self):
        def build(wb):
            ws = wb.create_sheet("Sheet1")
            _write_rows(ws, [["col_a", "col_b"]])  # header only

        data = _xlsx_bytes(build)
        contract = {"format": "excel", "write_mode": "replace", "header_row": 1}
        result = run_import(
            data,
            datastream_id="ds_02",
            project_id="proj_1",
            plan_version_id="dsp_01",
            mapping_version_id="dmap_01",
            projection_plan={"executable": True},
            actor="user@example.com",
            idempotency_key="test-key-empty-excel-1",
            source_metadata={"filename": "empty.xlsx"},
            contract=contract,
            conn=MagicMock(),
        )
        assert result["blocked"] is True
        assert result["reason"] == "empty_import"


# ---------------------------------------------------------------------------
# AC5: Append unavailable.
# ---------------------------------------------------------------------------


class TestAppendUnavailable:
    def test_append_via_run_import_raises(self):
        data = b"col_a,col_b\n1,2\n"
        contract = {"format": "csv", "write_mode": "replace", "header_row": 1}
        with pytest.raises(AppendUnavailable) as exc_info:
            run_import(
                data,
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="append-test-1",
                source_metadata={},
                contract=contract,
                conn=MagicMock(),
                write_mode=WRITE_MODE_APPEND,  # passed directly to run_import
            )
        assert exc_info.value.code == "append_unavailable"
        assert "replace" in exc_info.value.detail.lower()
        assert exc_info.value.repair.get("use_replace_mode")

    def test_append_via_contract_raises(self):
        """AppendUnavailable when the contract write_mode is 'append'."""
        with pytest.raises(AppendUnavailable):
            validate_import_contract(
                {
                    "format": "csv",
                    "write_mode": WRITE_MODE_APPEND,
                    "header_row": 1,
                }
            )


# ---------------------------------------------------------------------------
# Delegates-to-ledger: run_import calls open_import / record_rows.
# ---------------------------------------------------------------------------


class TestDelegatesToLedger:
    """Prove that run_import delegates to the 12.8 ledger and does NOT
    reimplement ledger semantics (no second writer, no duplicate candidate)."""

    def _csv_data(self):
        return _simple_csv(
            [
                ["date", "channel", "spend"],
                ["2026-01-01", "search", "1000.00"],
                ["2026-01-02", "social", "500.00"],
            ]
        )

    def _contract(self):
        return {"format": "csv", "write_mode": "replace", "header_row": 1}

    def _fake_ledger(self):
        return {
            "id": "mfl_test_001",
            "outcome": "written",
            "execution_id": "dse_test_001",
        }

    def _fake_open_result(self):
        return {
            "ledger": self._fake_ledger(),
            "execution": {"id": "dse_test_001", "state": "created"},
            "no_op": False,
            "replay": False,
        }

    def test_delegates_open_import_and_record_rows(self):
        with (
            patch("core.csv_excel_import.version_contract", return_value="cic_del_01"),
            patch(
                "core.managed_feed_ledger.open_import", return_value=self._fake_open_result()
            ) as mock_open,
            patch(
                "core.managed_feed_ledger.record_rows", return_value=self._fake_ledger()
            ) as mock_record,
            patch("core.managed_feed_ledger.evaluate_rejection_gate_for_ledger", return_value=None),
            patch(
                "core.managed_feed_ledger.allocate_landing_relation",
                return_value="main.managed_feed_ds_01",
            ),
            patch("core.managed_feed_ledger.mark_outcome"),
        ):
            result = run_import(
                self._csv_data(),
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="import-test-1",
                source_metadata={"filename": "spend.csv"},
                contract=self._contract(),
                conn=MagicMock(),
            )

        # open_import called ONCE (not twice -- no second writer).
        assert mock_open.call_count == 1
        # record_rows called ONCE (not reimplemented).
        assert mock_record.call_count == 1
        assert result["blocked"] is False
        assert result["row_count"] == 2
        assert result["ledger"]["id"] == "mfl_test_001"
        # Honest publication signal: written, NOT published (Phase B).
        assert result["published"] is False
        assert result["outcome"] == "written_pending_publication"

    def test_replay_forwarded_from_ledger(self):
        """A replay result (same key + same payload) is forwarded transparently."""
        replay_result = {
            "ledger": self._fake_ledger(),
            "execution": {"id": "dse_test_001"},
            "no_op": False,
            "replay": True,
        }
        with (
            patch("core.csv_excel_import.version_contract", return_value="cic_replay_01"),
            patch("core.managed_feed_ledger.open_import", return_value=replay_result),
            patch("core.managed_feed_ledger.record_rows", return_value=self._fake_ledger()),
            patch("core.managed_feed_ledger.evaluate_rejection_gate_for_ledger", return_value=None),
            patch(
                "core.managed_feed_ledger.allocate_landing_relation",
                return_value="main.managed_feed_ds_01",
            ),
            patch("core.managed_feed_ledger.mark_outcome"),
        ):
            result = run_import(
                self._csv_data(),
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="import-test-replay",
                source_metadata={"filename": "spend.csv"},
                contract=self._contract(),
                conn=MagicMock(),
            )
        assert result["replay"] is True

    def test_no_op_forwarded_from_ledger(self):
        """An unchanged-snapshot no-op from the ledger is forwarded without re-processing."""
        noop_result = {
            "ledger": {**self._fake_ledger(), "outcome": "noop"},
            "execution": None,
            "no_op": True,
            "replay": False,
        }
        with (
            patch("core.csv_excel_import.version_contract", return_value="cic_noop_01"),
            patch("core.managed_feed_ledger.open_import", return_value=noop_result),
            patch("core.managed_feed_ledger.record_rows") as mock_record,
        ):
            result = run_import(
                self._csv_data(),
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="import-test-noop",
                source_metadata={"filename": "spend.csv"},
                contract=self._contract(),
                conn=MagicMock(),
            )

        assert result["no_op"] is True
        assert mock_record.call_count == 0  # no rows written on a no-op

    def test_rejection_gate_blocks_and_marks_failed(self):
        """When the rejection gate fires, the outcome is marked 'failed'."""
        gate_issue = {
            "code": "rejection_threshold_exceeded",
            "detail": "50/100 rows rejected (50.00%) exceeds threshold 25.00%",
            "repair": {},
        }
        with (
            patch("core.csv_excel_import.version_contract", return_value="cic_gate_01"),
            patch("core.managed_feed_ledger.open_import", return_value=self._fake_open_result()),
            patch("core.managed_feed_ledger.record_rows", return_value=self._fake_ledger()),
            patch(
                "core.managed_feed_ledger.evaluate_rejection_gate_for_ledger",
                return_value=gate_issue,
            ),
            patch(
                "core.managed_feed_ledger.allocate_landing_relation",
                return_value="main.managed_feed_ds_01",
            ),
            patch("core.managed_feed_ledger.mark_outcome") as mock_mark,
        ):
            result = run_import(
                self._csv_data(),
                datastream_id="ds_01",
                project_id="proj_1",
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                actor="user@example.com",
                idempotency_key="import-test-gate",
                source_metadata={"filename": "spend.csv"},
                contract=self._contract(),
                conn=MagicMock(),
            )

        assert result["blocked"] is True
        assert result["reason"] == "rejection_threshold_exceeded"
        # mark_outcome must be called with 'failed'.
        mock_mark.assert_called_once()
        call_kwargs = mock_mark.call_args.kwargs
        assert call_kwargs["outcome"] == "failed"


# ---------------------------------------------------------------------------
# Content hash (pure helper).
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_hash_is_64_hex(self):
        h = _content_hash(b"hello world")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_bytes_different_hash(self):
        assert _content_hash(b"a") != _content_hash(b"b")

    def test_hash_stable_across_calls(self):
        data = b"a,b\n1,2\n"
        assert _content_hash(data) == _content_hash(data)


# ---------------------------------------------------------------------------
# 12.9 hardening: per-column DECLARED type (contract column_types) is
# authoritative over the leading-value inference -- kills its false-rejects.
# ---------------------------------------------------------------------------


class TestColumnTypesContract:
    def test_validate_accepts_and_normalises(self):
        norm = validate_import_contract(
            {"format": "csv", "column_types": {"id": "text", "spend": "decimal"}}
        )
        assert norm["column_types"] == {"id": "text", "spend": "decimal"}

    def test_validate_defaults_to_empty(self):
        assert validate_import_contract({"format": "csv"})["column_types"] == {}

    def test_validate_rejects_unknown_type(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract({"format": "csv", "column_types": {"id": "guid"}})

    def test_validate_rejects_non_dict(self):
        with pytest.raises(InvalidImportContract):
            validate_import_contract({"format": "csv", "column_types": ["id"]})

    def test_declared_text_kills_leading_value_false_reject(self):
        # "id" head "00123" -> inference types integer -> "AB-45"/"XYZ-9" false-reject.
        data = _simple_csv([["id"], ["00123"], ["AB-45"], ["XYZ-9"]])
        assert len(parse_csv(data).rejected) == 2  # inference alone
        typed = parse_csv(data, column_types={"id": "text"})
        assert typed.rejected == []  # declared text accepts everything
        assert next(c for c in typed.columns if c.name == "id").detected_type == "text"

    def test_declared_integer_enforces_type(self):
        data = _simple_csv([["n"], ["1"], ["2"], ["oops"]])
        typed = parse_csv(data, column_types={"n": "integer"})
        assert len(typed.rejected) == 1
        assert typed.rejected[0].rule == "type_mismatch"

    def test_declared_date_uses_supported_formats(self):
        data = _simple_csv([["day"], ["2026-01-01"], ["nope"]])
        typed = parse_csv(data, column_types={"day": "date"})
        assert next(c for c in typed.columns if c.name == "day").detected_type == "date"
        assert len(typed.rejected) == 1

    def test_column_types_change_the_fingerprint(self):
        a = contract_fingerprint(validate_import_contract({"format": "csv"}))
        b = contract_fingerprint(
            validate_import_contract({"format": "csv", "column_types": {"id": "text"}})
        )
        assert a != b

    def test_build_preview_threads_column_types(self):
        data = _simple_csv([["id"], ["00123"], ["AB-45"]])
        prev = build_preview(
            data, filename="x.csv",
            contract={"format": "csv", "column_types": {"id": "text"}},
        )
        assert prev.rejected_count == 0
