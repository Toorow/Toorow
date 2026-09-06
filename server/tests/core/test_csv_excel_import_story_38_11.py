"""Golden offline conformance matrix for Story 38.11."""

from __future__ import annotations

import io
import time
import zipfile
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from core.csv_excel_import import (
    CsvExcelImportError,
    DuplicateColumns,
    FormulaInCells,
    ParserReviewRequired,
    build_preview,
    parse_csv,
    parse_excel,
    run_import,
)
from openpyxl import Workbook
from openpyxl.worksheet.table import Table

#: Repository root, anchored on this file rather than on the working directory.
#: Resolving the migration against the cwd made this suite return two different
#: verdicts for the same tree, depending on where pytest was launched (AI-136).
ROOT = Path(__file__).resolve().parents[3]


def _xlsx(*sheets: tuple[str, list[list[object]]]) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    for title, rows in sheets:
        worksheet = workbook.create_sheet(title)
        for row in rows:
            worksheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def _rewrite_xlsx(data: bytes, replacements: dict[str, bytes], additions=None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as source, zipfile.ZipFile(output, "w") as target:
        for item in source.infolist():
            target.writestr(item, replacements.get(item.filename, source.read(item)))
        for name, payload in (additions or {}).items():
            target.writestr(name, payload)
    return output.getvalue()


def _code(exc: pytest.ExceptionInfo[CsvExcelImportError]) -> str:
    return exc.value.code


def test_header_row_is_bounded_before_any_file_scan():
    with pytest.raises(CsvExcelImportError) as csv_header:
        parse_csv(b"\n" * 1000, delimiter=",", header_row=1000, max_rows=10)
    assert _code(csv_header) == "invalid_import_contract"
    with pytest.raises(CsvExcelImportError) as excel_header:
        parse_excel(
            _xlsx(("Data", [["a"], [1]])), header_row=1000, max_rows=10
        )
    assert _code(excel_header) == "invalid_import_contract"
    with pytest.raises(CsvExcelImportError) as contract_header:
        build_preview(
            b"a\n1\n",
            filename="bounded.csv",
            contract={
                "format": "csv",
                "delimiter": ",",
                "header_row": 12,
                "max_rows": 10,
            },
        )
    assert _code(contract_header) == "invalid_import_contract"


def test_csv_overflow_and_field_bounds_are_typed_and_never_truncated():
    with pytest.raises(CsvExcelImportError) as rows:
        parse_csv(b"a\n1\n2\n", delimiter=",", max_rows=1)
    assert _code(rows) == "row_limit_exceeded"
    with pytest.raises(CsvExcelImportError) as field:
        parse_csv(b"a\n12345\n", delimiter=",", max_field_chars=4)
    assert _code(field) == "field_limit_exceeded"


def test_declared_encoding_is_applied_and_auto_detection_is_conservative():
    utf16 = "name\nAlice\n".encode("utf-16")
    assert parse_csv(utf16, delimiter=",", encoding="utf-16").encoding == "utf-16-le"
    cp1252 = b"name\ncaf\xe9\n"
    assert parse_csv(cp1252, delimiter=",").encoding == "cp1252"
    assert parse_csv(cp1252, delimiter=",", encoding="latin-1").encoding == "iso-8859-1"
    with pytest.raises(CsvExcelImportError) as undecidable:
        parse_csv(b"name\n\x81\n", delimiter=",")
    assert _code(undecidable) == "encoding_error"


def test_delimiter_candidates_ignore_separators_inside_quoted_fields():
    result = parse_csv(b'"a|x";b\n"v|z";q\n')
    assert result.delimiter == ";"
    assert "delimiter_ambiguous" not in {issue.code for issue in result.issues}
    assert result.rows == [{"a|x": "v|z", "b": "q"}]


def test_delimiter_date_and_locale_ambiguity_are_review_issues():
    delimiter = parse_csv(b"single\nvalue\n")
    assert [issue.code for issue in delimiter.issues] == ["delimiter_undetermined"]
    dates = parse_csv(b"day\n01/02/2026\n", delimiter=",")
    assert "date_locale_ambiguous" in {issue.code for issue in dates.issues}
    numeric = parse_csv(b"amount;label\n1,234;x\n", delimiter=";")
    assert "numeric_locale_ambiguous" in {issue.code for issue in numeric.issues}


def test_declared_locale_controls_numeric_and_date_interpretation():
    fr = parse_csv(b"amount;day\n1,234;01/02/2026\n", delimiter=";", locale="fr")
    en = parse_csv(b"amount;day\n1,234;01/02/2026\n", delimiter=";", locale="en")
    assert fr.rows[0]["amount"] == pytest.approx(1.234)
    assert en.rows[0]["amount"] == 1234
    assert fr.rows[0]["day"] == date(2026, 2, 1)
    assert en.rows[0]["day"] == date(2026, 1, 2)
    assert fr.issues == en.issues == []


def test_csv_grammar_and_formula_detection_share_one_quote_aware_parse():
    with pytest.raises(CsvExcelImportError) as malformed:
        parse_csv(b'a,b\n"unterminated,1\n', delimiter=",")
    assert _code(malformed) == "malformed_csv"
    with pytest.raises(FormulaInCells):
        parse_csv(b'a,b\nx,"=SUM(A1)"\n', delimiter=",")


@pytest.mark.parametrize("payload", [b"a,,c\n1,2,3\n", b"a,A\n1,2\n"])
def test_csv_blank_and_duplicate_headers_fail_closed(payload: bytes):
    expected = "blank_header" if b",," in payload else "duplicate_columns"
    with pytest.raises(CsvExcelImportError) as exc:
        parse_csv(payload, delimiter=",")
    assert _code(exc) == expected


def test_xlsx_requires_unique_visible_sheet_or_explicit_selection_and_range():
    data = _xlsx(("One", [["a"], [1]]), ("Two", [["b"], [2]]))
    with pytest.raises(CsvExcelImportError) as ambiguous:
        parse_excel(data)
    assert _code(ambiguous) == "worksheet_selection_required"
    result = parse_excel(data, sheet_name="Two", cell_range="A1:A2")
    assert result.sheet_name == "Two"
    assert result.metadata["cell_range"] == "A1:A2"


def test_xlsx_rejects_formula_macro_external_and_dishonest_dimensions():
    formula = _xlsx(("Data", [["a"], ["=SUM(1,2)"]]))
    with pytest.raises(FormulaInCells):
        parse_excel(formula)
    clean = _xlsx(("Data", [["a"], [1]]))
    macro = _rewrite_xlsx(clean, {}, {"xl/vbaProject.bin": b"x"})
    with pytest.raises(CsvExcelImportError) as macro_exc:
        parse_excel(macro)
    assert _code(macro_exc) == "xlsx_active_content"
    external = _rewrite_xlsx(clean, {}, {"xl/externalLinks/externalLink1.xml": b"<x/>"})
    with pytest.raises(CsvExcelImportError) as external_exc:
        parse_excel(external)
    assert _code(external_exc) == "xlsx_active_content"
    with zipfile.ZipFile(io.BytesIO(clean)) as package:
        sheet = package.read("xl/worksheets/sheet1.xml")
    dishonest = _rewrite_xlsx(
        clean,
        {"xl/worksheets/sheet1.xml": sheet.replace(b'ref="A1:A2"', b'ref="A1:XFD1048576"')},
    )
    with pytest.raises(CsvExcelImportError) as dimensions:
        parse_excel(dishonest, max_rows=100, max_columns=10)
    assert _code(dimensions) == "xlsx_dimension_limit"


def test_xlsx_native_values_and_schema_are_row_order_independent():
    first = parse_excel(
        _xlsx(("Data", [["n", "d"], [1, date(2026, 1, 1)], [2.5, date(2026, 1, 2)]]))
    )
    second = parse_excel(
        _xlsx(("Data", [["n", "d"], [2.5, date(2026, 1, 2)], [1, date(2026, 1, 1)]]))
    )
    assert isinstance(first.rows[0]["n"], int)
    assert first.columns[0].detected_type == "decimal"
    assert first.schema_fingerprint == second.schema_fingerprint


def test_common_envelope_has_versioned_counts_columns_and_fingerprints():
    result = parse_csv(b"a,b\n1,x\n", delimiter=",", locale="en")
    assert result.envelope_version == "tabular-parse-v1"
    assert result.accepted_count == 1 and result.rejected_count == 0
    assert result.detected_row_count == 1
    assert len(result.content_hash) == len(result.schema_fingerprint) == 64
    assert result.columns[0].source_name == "a"
    assert result.columns[0].source_index == 0


def test_preview_records_actual_tsv_format_and_validates_empty_contract():
    preview = build_preview(
        b"a\tb\n1\t2\n", filename="data.tsv", contract={"format": "csv", "delimiter": "\t"}
    )
    assert preview.format == "csv"
    assert preview.producer_format == "tsv"
    with pytest.raises(CsvExcelImportError) as exc:
        build_preview(b"a,b\n1,2\n", filename="data.csv", contract={})
    assert exc.value.code == "invalid_import_contract"


def test_preview_surfaces_issues_while_confirmed_upload_refuses_them():
    preview = build_preview(b"only\nvalue\n", filename="one.csv")
    assert preview.row_count == 1
    assert preview.issues[0].code == "delimiter_undetermined"
    with pytest.raises(ParserReviewRequired):
        run_import(
            b"only\nvalue\n",
            datastream_id="ds_1",
            project_id="proj_1",
            plan_version_id="plan_1",
            mapping_version_id="map_1",
            projection_plan={"executable": True},
            actor="person_1",
            idempotency_key="review-1",
            source_metadata={"filename": "one.csv"},
            contract={"format": "csv"},
            conn=MagicMock(),
        )


def test_parser_evidence_is_persisted_on_existing_ledger_execution_only():
    opened = {
        "ledger": {"id": "mfl_1"},
        "execution": {"id": "dse_1"},
        "no_op": True,
        "replay": False,
    }
    with (
        patch("core.import_runner.version_contract", return_value="cic_1"),
        patch("core.managed_feed_ledger.open_import", return_value=opened) as open_import,
    ):
        result = run_import(
            b"a,b\n1,x\n",
            datastream_id="ds_1",
            project_id="proj_1",
            plan_version_id="plan_1",
            mapping_version_id="map_1",
            projection_plan={"executable": True},
            actor="person_1",
            idempotency_key="evidence-1",
            source_metadata={"filename": "data.csv"},
            contract={"format": "csv", "delimiter": ",", "locale": "en"},
            conn=MagicMock(),
        )
    evidence = open_import.call_args.kwargs["source_metadata"]["parser_evidence"]
    assert result["outcome"] == "noop"
    assert evidence["envelope_version"] == "tabular-parse-v1"
    assert evidence["content_fingerprint"]
    assert evidence["schema_fingerprint"]
    assert evidence["accepted_row_count"] == 1


def test_duplicate_excel_header_matches_csv_contract():
    with pytest.raises(DuplicateColumns):
        parse_excel(_xlsx(("Data", [["a", "A"], [1, 2]])))



def test_utf8_controls_and_late_locale_ambiguity_fail_closed():
    with pytest.raises(CsvExcelImportError) as control:
        parse_csv(b"name\nali\x00ce\n", delimiter=",")
    assert _code(control) == "encoding_error"
    values = [f"label-{index}" for index in range(100)] + ["1.234"]
    result = parse_csv(("value\n" + "\n".join(values) + "\n").encode(), delimiter=",")
    assert "numeric_locale_ambiguous" in {issue.code for issue in result.issues}


def test_representative_schema_is_stable_beyond_sample_cap():
    values = [str(index) for index in range(100)] + ["not-a-number"]
    first = parse_csv(("v\n" + "\n".join(values) + "\n").encode(), delimiter=",")
    second = parse_csv(("v\n" + "\n".join(reversed(values)) + "\n").encode(), delimiter=",")
    assert first.columns[0].detected_type == second.columns[0].detected_type == "mixed"
    assert first.schema_fingerprint == second.schema_fingerprint
    assert first.accepted_count == second.accepted_count == 101


def test_locale_override_does_not_turn_text_with_comma_into_decimal():
    result = parse_csv('name\n"Smith, John"\n'.encode(), delimiter=",", locale="fr")
    assert result.columns[0].detected_type == "text"
    assert result.rows[0]["name"] == "Smith, John"


def test_mixed_date_formats_are_reviewable_and_locale_governs_rejections():
    payload = b"day\n31/01/2026\n12/31/2026\n"
    for locale in (None, "fr", "en"):
        result = parse_csv(payload, delimiter=",", locale=locale)
        assert "date_format_mixed" in {issue.code for issue in result.issues}
        if locale is not None:
            assert result.accepted_count == 1 and result.rejected_count == 1


@pytest.mark.parametrize(
    ("value", "declared_type"),
    [(1.5, "integer"), (42, "date"), (2, "boolean"), (True, "integer")],
)
def test_native_xlsx_values_obey_declared_types(value, declared_type):
    result = parse_excel(
        _xlsx(("Data", [["v"], [value]])), column_types={"v": declared_type}
    )
    assert result.accepted_count == 0
    assert result.rejected_count == 1


def test_native_xlsx_text_contract_normalises_values_to_text():
    result = parse_excel(_xlsx(("Data", [["v"], [123]])), column_types={"v": "text"})
    assert result.rows == [{"v": "123"}]


def test_structural_formula_detection_has_no_dde_substring_false_positive():
    clean = _xlsx(("Data", [["v"], ["sudden hidden"]]))
    assert parse_excel(clean).rows == [{"v": "sudden hidden"}]
    formula = _xlsx(("Data", [["v"], ["=SUM(1,2)"]]))
    with zipfile.ZipFile(io.BytesIO(formula)) as package:
        sheet = package.read("xl/worksheets/sheet1.xml")
    namespaced = sheet.replace(
        b"<f>", b'<x:f xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
    ).replace(b"</f>", b"</x:f>")
    assert namespaced != sheet
    with pytest.raises(FormulaInCells):
        parse_excel(_rewrite_xlsx(formula, {"xl/worksheets/sheet1.xml": namespaced}))


def test_xlsx_unique_table_range_is_used_and_ambiguous_ranges_are_refused():
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Data"
    for row in [["a", "b"], [1, 2], [None, None], ["note", "outside"]]:
        sheet.append(row)
    sheet.add_table(Table(displayName="Primary", ref="A1:B2"))
    output = io.BytesIO()
    workbook.save(output)
    parsed = parse_excel(output.getvalue())
    assert parsed.rows == [{"a": 1, "b": 2}]
    assert parsed.metadata["cell_range"] == "A1:B2"

    workbook = Workbook()
    sheet = workbook.active
    for row in [["a", "b"], [1, 2], [None, None], ["c", "d"], [3, 4]]:
        sheet.append(row)
    sheet.add_table(Table(displayName="First", ref="A1:B2"))
    sheet.add_table(Table(displayName="Second", ref="A4:B5"))
    output = io.BytesIO()
    workbook.save(output)
    with pytest.raises(CsvExcelImportError) as ambiguous:
        parse_excel(output.getvalue())
    assert _code(ambiguous) == "xlsx_range_selection_required"

    disconnected = _xlsx(("Data", [["a"], [1], [None], ["other"], [2]]))
    with pytest.raises(CsvExcelImportError) as disconnected_exc:
        parse_excel(disconnected)
    assert _code(disconnected_exc) == "xlsx_range_selection_required"


def test_xlsx_zip_compression_budget_rejects_bomb_before_xml_read():
    clean = _xlsx(("Data", [["a"], [1]]))
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(clean)) as source, zipfile.ZipFile(
        output, "w", compression=zipfile.ZIP_DEFLATED
    ) as target:
        for item in source.infolist():
            target.writestr(item, source.read(item))
        target.writestr("xl/media/bomb.bin", b"0" * (2 * 1024 * 1024))
    with pytest.raises(CsvExcelImportError) as bomb:
        parse_excel(output.getvalue())
    assert _code(bomb) == "xlsx_compression_ratio"


@pytest.mark.parametrize(
    "row",
    ["plain,text,value\n", "99/99/9999,01/02/2026,31/01/2026\n"],
)
def test_large_csv_ambiguity_scan_is_cpu_bounded(row):
    payload = ("a,b,c\n" + row * 10_000).encode()
    started = time.perf_counter()
    result = parse_csv(payload, delimiter=",")
    assert result.accepted_count == 10_000
    assert time.perf_counter() - started < 2.0


def test_parser_evidence_includes_resolved_column_date_format():
    from core.csv_excel_import import _parse_evidence

    result = parse_csv(b"day\n2026-01-01\n", delimiter=",")
    assert _parse_evidence(result)["columns"][0]["date_format"] == "%Y-%m-%d"



@pytest.mark.parametrize(
    ("filename", "payload_factory", "contract", "producer_format"),
    [
        (
            "matrix.csv",
            lambda: b"day,amount\n2026-01-01,10\n",
            {"format": "csv", "delimiter": ",", "locale": "en"},
            "csv",
        ),
        (
            "matrix.tsv",
            lambda: b"day\tamount\n2026-01-01\t10\n",
            {"format": "csv", "delimiter": "\t", "locale": "en"},
            "tsv",
        ),
        (
            "matrix.xlsx",
            lambda: _xlsx(("Data", [["day", "amount"], [date(2026, 1, 1), 10]])),
            {"format": "excel", "sheet_name": "Data", "cell_range": "A1:B2"},
            "xlsx",
        ),
    ],
)
def test_common_golden_matrix_has_preview_upload_and_real_inbound_parity(
    monkeypatch, filename, payload_factory, contract, producer_format
):
    from core import inbound_ingest
    from core.csv_excel_import import resolve_file_source_producer

    del resolve_file_source_producer
    payload = payload_factory()
    preview = build_preview(payload, filename=filename, contract=contract)
    opened = {
        "ledger": {"id": "mfl_matrix"},
        "execution": {"id": "dse_matrix"},
        "no_op": True,
        "replay": False,
    }

    # ONE governed bundle, handed to BOTH paths. This is the whole point of the
    # test: upload and inbound must reach the same result from the same file and
    # the same mapping. Giving the mapping only to the inbound side compared a
    # governed run against an ungoverned one, and the difference it reported
    # ('tabular-parse-v1' vs 'universal-datastream-candidate-v1', source names vs
    # canonical names) was manufactured by the test, not found in the product.
    matrix_mapping = {
        "fields": [
            {
                "field_id": "day",
                "physical_type": "date",
                "binding": {"status": "confirmed", "canonical_target": "canonical_day"},
            },
            {
                "field_id": "amount",
                "physical_type": "decimal",
                "binding": {
                    "status": "confirmed",
                    "canonical_target": "canonical_amount",
                },
            },
        ]
    }
    matrix_projection = {
        "executable": True,
        "plan_version_id": "plan_matrix",
        "mapping_version_id": "map_matrix",
        "projection_contract_version": "1",
        "full_grain_relation": {
            "grain_columns": [{"field_id": "day", "type_class": "date"}],
            "grain_key": "grain_key",
            "source_fields": ["amount"],
            "provenance": {},
            "scope_columns": ["connector", "project_id"],
        },
        "additive_measures": [{"field_id": "amount"}],
        "governed_dimension_projection": None,
    }

    with (
        patch("core.import_runner.version_contract", return_value="cic_matrix"),
        patch("core.managed_feed_ledger.open_import", return_value=opened) as upload_open,
    ):
        upload = run_import(
            payload,
            datastream_id="ds_matrix",
            project_id="proj_matrix",
            plan_version_id="plan_matrix",
            mapping_version_id="map_matrix",
            projection_plan=matrix_projection,
            actor="person_matrix",
            idempotency_key=f"upload:{producer_format}",
            source_metadata={"filename": filename},
            contract=contract,
            conn=MagicMock(),
            mapping_payload=matrix_mapping,
        )
    upload_evidence = upload_open.call_args.kwargs["source_metadata"]["parser_evidence"]

    monkeypatch.setattr(
        inbound_ingest,
        "_load_ingestable_datastream",
        lambda *_args, **_kwargs: {
            "source_kind": "managed_feed",
            "enabled": True,
            "config": {"channels": ["managed"]},
            "current_plan_version_id": "plan_matrix",
            "current_mapping_version_id": "map_matrix",
        },
    )
    # The projection must agree with the mapping fields below, or the dispatch
    # refuses with dispatch_projection_field_mismatch. `{"executable": True}`
    # alone never reached that check because the run died earlier.
    monkeypatch.setattr(
        inbound_ingest,
        "_fetch_projection_plan",
        lambda *_args, **_kwargs: matrix_projection,
    )
    # `include_evidence=True` is what the dispatch bundle asks for, and it returns
    # the CONTRACT ENVELOPE, not the bare contract. Returning the bare contract
    # made `_producer_from_frozen_bundle` see `parser["contract"]` missing and
    # raise InboundContractReviewRequired, again short of the parity comparison.
    monkeypatch.setattr(
        inbound_ingest,
        "_fetch_active_parsing_contract",
        lambda *_args, **kwargs: (
            {
                "contract": contract,
                "contract_id": "cic_matrix",
                "contract_fingerprint": "contract_fingerprint_matrix",
                "confirmed_by": "person_matrix",
                "confirmed_at": "2026-08-02T00:00:00+00:00",
            }
            if kwargs.get("include_evidence")
            else contract
        ),
    )
    monkeypatch.setattr(inbound_ingest, "_read_allow_empty_publication", lambda *_args: False)
    # The pinned bundle is read with SQL. Left unstubbed it ran against the
    # MagicMock connection, so `row[0]` was a MagicMock, `json.loads` raised
    # TypeError and the call died in DatastreamNotIngestable BEFORE reaching the
    # parity comparison this test exists for -- it proved nothing (AI-138).
    # Stubbed like its three siblings above, with a real immutable document.
    monkeypatch.setattr(
        inbound_ingest,
        "_fetch_mapping_bundle",
        lambda *_args, **_kwargs: {
            "mapping_payload": matrix_mapping,
            "mapping_fingerprint": "map_fingerprint_matrix",
            "source_schema_hash": "source_schema_hash_matrix",
            "capability_fingerprint": "capability_fingerprint_matrix",
            "plan_fingerprint": "plan_fingerprint_matrix",
            "plan_capability_fingerprint": "plan_capability_matrix",
            "plan_contract_version": "1",
        },
    )
    monkeypatch.setattr(
        "core.csv_excel_import.resolve_file_source_producer",
        lambda *_args, **_kwargs: None,
    )
    with (
        patch("core.import_runner.version_contract", return_value="cic_matrix"),
        patch("core.managed_feed_ledger.open_import", return_value=opened) as inbound_open,
    ):
        inbound = inbound_ingest.ingest_inbound_file(
            MagicMock(),
            datastream_id="ds_matrix",
            project_id="proj_matrix",
            file_bytes=payload,
            filename=filename,
            channel="managed",
            message_id=f"message:{producer_format}",
            actor="worker_matrix",
        )
    inbound_evidence = inbound_open.call_args.kwargs["source_metadata"]["parser_evidence"]

    assert upload["outcome"] == inbound["outcome"] == "noop"
    assert preview.row_count == upload_evidence["accepted_row_count"] == 1
    assert preview.producer_format == producer_format
    assert inbound_evidence == upload_evidence
    assert [col.source_name for col in preview.columns] == [
        col["source_name"]
        for col in upload_evidence["columns"]
        if col["source_name"] != "grain_key"
    ]


class _ContractCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, *_args):
        return None

    def fetchone(self):
        return self.row


class _ContractConnection:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _ContractCursor(self.row)


def test_unattended_contract_must_be_unique_and_confirmed():
    from datetime import UTC, datetime

    from core.inbound_ingest import (
        InboundContractReviewRequired,
        _fetch_active_parsing_contract,
    )

    contract = {"format": "csv", "delimiter": ","}
    row = (contract, "cic_1", "a" * 64, "person_1", datetime.now(UTC), 1)
    assert (
        _fetch_active_parsing_contract(
            _ContractConnection(row), datastream_id="ds_1", project_id="proj_1"
        )
        == contract
    )
    for invalid in (
        (contract, "cic_1", "a" * 64, None, None, 1),
        (contract, "cic_1", "a" * 64, "person_1", datetime.now(UTC), 2),
        None,
    ):
        with pytest.raises(InboundContractReviewRequired):
            _fetch_active_parsing_contract(
                _ContractConnection(invalid),
                datastream_id="ds_1",
                project_id="proj_1",
            )


def test_confirmation_migration_is_additive_immutable_and_scope_bound():
    sql = ROOT.joinpath(
        "infra/nango/migrations/192_csv_excel_contract_confirmation.sql"
    ).read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.csv_excel_import_contract_confirmations" in sql
    assert "append-only" in sql
    assert "fingerprint" in sql
    assert "fk_csv_excel_confirmation_scope" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql
    assert "csv_excel_import_contracts_strict" in sql
    assert "csv_excel_contract_confirmations_strict" in sql
    assert sql.count("FORCE ROW LEVEL SECURITY") == 2
    assert "epic36_has_resource_access" in sql
