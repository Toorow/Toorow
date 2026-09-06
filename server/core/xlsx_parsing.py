"""toorow -- Excel (.xlsx/.xls) package inspection and parsing (pure, no I/O).

Split from ``tabular_parsing`` because reading a workbook safely is a body of
rules of its own: decompression budgets, entry-count budgets, formula detection,
declared sheet bounds. Value coercion and column typing are NOT duplicated here
-- they come from ``tabular_parsing``.
"""

from __future__ import annotations

import io
import logging
import posixpath
import zipfile
from typing import Any
from xml.etree import ElementTree

from core.tabular_parsing import (
    _XLSX_MAGIC,
    _candidate_ambiguities,
    _content_hash,
    _finalize_parse_result,
    _locale_numeric_sample,
    _representative_samples,
    _resolve_column_type,
    _resolved_date_format,
    _strip_bom,
    _validate_rows,
)
from core.tabular_types import (
    MAX_FIELD_CHARS,
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    MAX_XLSX_COMPRESSION_RATIO,
    MAX_XLSX_UNCOMPRESSED_BYTES,
    MAX_XLSX_XML_BYTES,
    ColumnSpec,
    CsvExcelImportError,
    DuplicateColumns,
    FormulaInCells,
    InvalidImportContract,
    NoHeaderRow,
    ParseResult,
    SheetNotFound,
    UnsupportedFileType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Excel parsing (pure, uses openpyxl).
# ---------------------------------------------------------------------------


def _xlsx_bounds(reference: str) -> tuple[int, int, int, int]:
    try:
        from openpyxl.utils.cell import range_boundaries  # noqa: PLC0415

        return range_boundaries(reference)
    except Exception as exc:
        raise CsvExcelImportError(
            "xlsx_invalid_range", "Worksheet range metadata is invalid."
        ) from exc


def _inspect_xlsx_package(
    data: bytes,
    *,
    max_rows: int,
    max_columns: int,
    header_row: int,
    formulas_as_values: bool = False,
) -> dict[str, list[str]]:
    """Fail-closed OOXML inspection before openpyxl sees workbook content."""
    worksheet_table_ranges: dict[str, list[str]] = {}
    try:
        package = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as exc:
        raise UnsupportedFileType("Only structurally valid XLSX workbooks are supported.") from exc
    with package:
        members = package.infolist()
        if not members or len(members) > 2048:
            raise CsvExcelImportError(
                "xlsx_package_limit", "Workbook package entry count is invalid or excessive."
            )
        total_uncompressed = sum(item.file_size for item in members)
        if total_uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
            raise CsvExcelImportError(
                "xlsx_package_limit", "Workbook decompressed size exceeds the safety budget."
            )
        if any(
            item.file_size > 1024 * 1024
            and item.file_size > max(item.compress_size, 1) * MAX_XLSX_COMPRESSION_RATIO
            for item in members
        ):
            raise CsvExcelImportError(
                "xlsx_compression_ratio", "Workbook contains a suspiciously compressed entry."
            )
        names = {item.filename for item in members}
        required = {"[Content_Types].xml", "xl/workbook.xml"}
        if not required <= names:
            raise UnsupportedFileType("The ZIP container is not an XLSX workbook.")
        for item in members:
            normal = item.filename.replace("\\", "/")
            lower = normal.lower()
            if item.flag_bits & 0x1:
                raise CsvExcelImportError(
                    "xlsx_encrypted", "Encrypted workbook entries are refused."
                )
            if normal.startswith("/") or "../" in f"/{normal}":
                raise CsvExcelImportError(
                    "xlsx_unsafe_path", "Workbook contains an unsafe package path."
                )
            if item.file_size > MAX_XLSX_XML_BYTES and lower.endswith((".xml", ".rels")):
                raise CsvExcelImportError(
                    "xlsx_xml_limit", "Workbook XML exceeds the inspection bound."
                )
            if any(
                marker in lower
                for marker in (
                    "vbaproject",
                    "externalLinks".lower(),
                    "embeddings/",
                    "oleobject",
                    "activex",
                    "connections.xml",
                )
            ):
                raise CsvExcelImportError(
                    "xlsx_active_content",
                    "Macros, external links and embedded active content are refused.",
                )
            if lower.startswith("xl/worksheets/") and lower.endswith(".xml"):
                raw = package.read(item)
                try:
                    root = ElementTree.fromstring(raw)
                except ElementTree.ParseError as exc:
                    raise CsvExcelImportError(
                        "xlsx_invalid_xml", "Worksheet XML is not structurally valid."
                    ) from exc
                table_relation_ids: list[str] = []
                for element in root.iter():
                    local_name = element.tag.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower()
                    if local_name == "tablepart":
                        relation_id = next(
                            (
                                value
                                for key, value in element.attrib.items()
                                if key.rsplit("}", 1)[-1].rsplit(":", 1)[-1].lower() == "id"
                            ),
                            None,
                        )
                        if relation_id:
                            table_relation_ids.append(relation_id)
                    # A FORMULA IS NOT ACTIVE CONTENT WHEN ONLY ITS CACHED VALUE
                    # IS READ. `_load_workbook_safe` already opens the package
                    # with `data_only=True`: openpyxl returns the value Excel
                    # last stored and never evaluates anything. So the refusal
                    # belongs to the CONTRACT, not to the package -- and the
                    # contract's own default is `formulas_as_values=true`, which
                    # `parse_excel` used to delete with the comment "values-only
                    # is invariant". The error it raised named a repair
                    # ("set formulas_as_values=true") that no code path could
                    # honour: measured 2026-08-11 on a real workbook whose
                    # lookup sheet is entirely `=VLOOKUP(...)`.
                    #
                    # The genuinely active constructs below are still refused,
                    # flag or no flag: a DDE link, an OLE object and an external
                    # link reach outside the file, which a cached value cannot.
                    if local_name == "f" and not formulas_as_values:
                        raise FormulaInCells(1)
                    if local_name in {"ddelink", "oleobject", "externallink"}:
                        raise CsvExcelImportError(
                            "xlsx_active_content", "Workbook active links are refused."
                        )
                    if local_name == "c" and element.get("r"):
                        coordinate = element.get("r")
                        _, _, cell_col, cell_row = _xlsx_bounds(f"{coordinate}:{coordinate}")
                        if cell_col > max_columns or cell_row > max_rows + header_row:
                            raise CsvExcelImportError(
                                "xlsx_dimension_limit",
                                "Worksheet cell coordinates exceed configured bounds.",
                            )
                    if local_name == "dimension" and element.get("ref"):
                        _, _, max_col, max_row = _xlsx_bounds(element.get("ref", ""))
                        if max_col > max_columns or max_row > max_rows + header_row:
                            raise CsvExcelImportError(
                                "xlsx_dimension_limit",
                                "Worksheet advertised dimensions exceed configured bounds.",
                                repair={"max_rows": max_rows, "max_columns": max_columns},
                            )
                table_ranges: list[str] = []
                if table_relation_ids:
                    rels_name = posixpath.join(
                        posixpath.dirname(normal), "_rels", posixpath.basename(normal) + ".rels"
                    )
                    if rels_name not in names:
                        raise CsvExcelImportError(
                            "xlsx_invalid_relationships",
                            "Worksheet table relationships are missing.",
                        )
                    try:
                        rels_root = ElementTree.fromstring(package.read(rels_name))
                    except ElementTree.ParseError as exc:
                        raise CsvExcelImportError(
                            "xlsx_invalid_xml", "Worksheet relationships are invalid."
                        ) from exc
                    relations = {
                        element.get("Id"): element.get("Target")
                        for element in rels_root.iter()
                        if element.tag.rsplit("}", 1)[-1].lower() == "relationship"
                        and str(element.get("Type", "")).lower().endswith("/table")
                    }
                    for relation_id in table_relation_ids:
                        target = relations.get(relation_id)
                        if not target:
                            raise CsvExcelImportError(
                                "xlsx_invalid_relationships", "Worksheet table target is missing."
                            )
                        table_name = (
                            posixpath.normpath(target.lstrip("/"))
                            if target.startswith("/")
                            else posixpath.normpath(
                                posixpath.join(posixpath.dirname(normal), target)
                            )
                        )
                        if table_name not in names:
                            raise CsvExcelImportError(
                                "xlsx_invalid_relationships", "Worksheet table target is invalid."
                            )
                        try:
                            table_root = ElementTree.fromstring(package.read(table_name))
                        except ElementTree.ParseError as exc:
                            raise CsvExcelImportError(
                                "xlsx_invalid_xml", "Worksheet table XML is invalid."
                            ) from exc
                        table_ref = table_root.get("ref")
                        if not table_ref:
                            raise CsvExcelImportError(
                                "xlsx_invalid_range", "Worksheet table has no declared range."
                            )
                        _xlsx_bounds(table_ref)
                        table_ranges.append(table_ref)
                worksheet_table_ranges[lower] = table_ranges
        content_types = package.read("[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vba" in content_types:
            raise CsvExcelImportError("xlsx_macro_content", "Macro-enabled workbooks are refused.")
    return worksheet_table_ranges


def _load_workbook_safe(data: bytes) -> Any:
    try:
        import openpyxl  # noqa: PLC0415

        return openpyxl.load_workbook(
            io.BytesIO(data), data_only=True, read_only=True, keep_links=False
        )
    except CsvExcelImportError:
        raise
    except Exception as exc:
        raise UnsupportedFileType("Could not open as a safe XLSX workbook.") from exc


def parse_excel(
    data: bytes,
    *,
    sheet_name: str | None = None,
    cell_range: str | None = None,
    header_row: int = 1,
    date_format: str | None = None,
    locale: str | None = None,
    formulas_as_values: bool = False,
    max_rows: int = MAX_SHEET_ROWS,
    max_columns: int = MAX_SHEET_COLUMNS,
    max_field_chars: int = MAX_FIELD_CHARS,
    column_types: dict[str, str] | None = None,
) -> ParseResult:
    """Parse one explicit or uniquely demonstrable safe XLSX sheet/range."""
    if (
        max_rows < 1
        or max_rows > MAX_SHEET_ROWS
        or not isinstance(header_row, int)
        or isinstance(header_row, bool)
        or header_row < 1
        or header_row > max_rows + 1
        or max_columns < 1
        or max_columns > MAX_SHEET_COLUMNS
        or max_field_chars < 1
        or max_field_chars > MAX_FIELD_CHARS
    ):
        raise InvalidImportContract("XLSX bounds must be positive and within platform limits.")
    if data[:4] != _XLSX_MAGIC:
        raise UnsupportedFileType("Only XLSX is supported by the safe Excel adapter.")
    worksheet_table_ranges = _inspect_xlsx_package(
        data,
        max_rows=max_rows,
        max_columns=max_columns,
        header_row=header_row,
        formulas_as_values=formulas_as_values,
    )
    workbook = _load_workbook_safe(data)
    visible = [ws.title for ws in workbook.worksheets if ws.sheet_state == "visible"]
    if sheet_name is None:
        if len(visible) != 1:
            raise CsvExcelImportError(
                "worksheet_selection_required",
                "Select one visible worksheet explicitly; the workbook is not unambiguous.",
                repair={"visible_sheets": visible},
            )
        sheet_name = visible[0]
    if sheet_name not in workbook.sheetnames:
        raise SheetNotFound(sheet_name, workbook.sheetnames)
    worksheet = workbook[sheet_name]
    if worksheet.sheet_state != "visible":
        raise CsvExcelImportError("worksheet_not_visible", "Hidden worksheets cannot be imported.")
    worksheet_path = str(getattr(worksheet, "_worksheet_path", "")).lstrip("/").lower()
    declared_table_ranges = worksheet_table_ranges.get(worksheet_path, [])
    if cell_range is None and len(declared_table_ranges) > 1:
        raise CsvExcelImportError(
            "xlsx_range_selection_required",
            "The worksheet contains multiple table ranges; select one cell_range explicitly.",
        )
    range_was_declared = cell_range is not None or len(declared_table_ranges) == 1
    if cell_range is None and declared_table_ranges:
        cell_range = declared_table_ranges[0]

    if cell_range:
        min_col, min_row, max_col, max_row = _xlsx_bounds(cell_range)
        if max_col - min_col + 1 > max_columns or max_row - min_row + 1 > max_rows + 1:
            raise CsvExcelImportError(
                "xlsx_range_limit", "Configured worksheet range exceeds bounds."
            )
    else:
        dimension = worksheet.calculate_dimension(force=True)
        min_col, min_row, max_col, max_row = _xlsx_bounds(dimension)
    if header_row < min_row or header_row > max_row:
        raise NoHeaderRow()

    rows_iter = worksheet.iter_rows(
        min_row=min_row,
        max_row=max_row,
        min_col=min_col,
        max_col=max_col,
        values_only=True,
    )
    header_cells: list[Any] | None = None
    non_blank: list[tuple[int, list[Any]]] = []
    blank_after_data = False
    for absolute_row, row in enumerate(rows_iter, start=min_row):
        values = list(row)
        if any(isinstance(value, str) and len(value) > max_field_chars for value in values):
            raise CsvExcelImportError("field_limit_exceeded", "A cell exceeds the character bound.")
        if absolute_row < header_row:
            continue
        if absolute_row == header_row:
            header_cells = values
            continue
        if all(value is None or (isinstance(value, str) and not value.strip()) for value in values):
            if non_blank:
                blank_after_data = True
            continue
        if blank_after_data and not range_was_declared:
            raise CsvExcelImportError(
                "xlsx_range_selection_required",
                "The worksheet contains disconnected regions; select one cell_range explicitly.",
            )
        if len(non_blank) >= max_rows:
            raise CsvExcelImportError(
                "row_limit_exceeded", f"Sheet contains more than {max_rows} data rows."
            )
        non_blank.append((absolute_row, values))
    if header_cells is None:
        raise NoHeaderRow()
    names = [_strip_bom(str(value).strip()) if value is not None else "" for value in header_cells]
    while names and not names[-1]:
        names.pop()
    if not names:
        raise NoHeaderRow()
    if any(not name for name in names):
        raise CsvExcelImportError(
            "blank_header", "Every source column must have a non-blank header."
        )
    seen: dict[str, int] = {}
    for name in names:
        key = name.casefold()
        seen[key] = seen.get(key, 0) + 1
    duplicates = [name for name, count in seen.items() if count > 1]
    if duplicates:
        raise DuplicateColumns(duplicates)
    if len(names) > max_columns:
        raise CsvExcelImportError(
            "too_many_columns", f"Sheet has {len(names)} columns; maximum is {max_columns}."
        )

    for _, values in non_blank:
        while len(values) > len(names) and values[-1] is None:
            values.pop()
    samples = _representative_samples(non_blank, len(names))
    columns: list[ColumnSpec] = []
    for index, name in enumerate(names):
        detected_type, detected_format = _resolve_column_type(name, samples[index], column_types)
        if not (column_types or {}).get(name) and _locale_numeric_sample(samples[index], locale):
            detected_type, detected_format = "decimal", None
        resolved_date_format = _resolved_date_format(
            detected_type, detected_format, samples[index], date_format, locale
        )
        native_types = sorted({type(value).__name__ for value in samples[index]})
        columns.append(
            ColumnSpec(
                name=name,
                index=index,
                detected_type=detected_type,
                date_format=resolved_date_format,
                sample_values=samples[index][:10],
                source_name=name,
                source_index=index,
                source_type=(native_types[0] if len(native_types) == 1 else "mixed"),
                locale=locale,
            )
        )
    accepted, rejected = _validate_rows(non_blank, columns, names)
    for spec in columns:
        spec.null_count = sum(row.get(spec.name) is None for row in accepted)
    issues = _candidate_ambiguities(non_blank, names, date_format=date_format, locale=locale)
    return _finalize_parse_result(
        ParseResult(
            rows=accepted,
            rejected=rejected,
            columns=columns,
            encoding="xlsx",
            delimiter=None,
            sheet_name=sheet_name,
            detected_row_count=len(non_blank),
            content_hash=_content_hash(data),
            source_format="xlsx",
            issues=issues,
            metadata={
                "cell_range": cell_range or worksheet.calculate_dimension(force=True),
                "parser_policy": {
                    "header_row": header_row,
                    "date_format": date_format,
                    "locale": locale,
                    "max_rows": max_rows,
                    "max_columns": max_columns,
                    "max_field_chars": max_field_chars,
                    "formula_policy": "reject-v1",
                    "external_links": "reject-v1",
                    "macros": "reject-v1",
                },
            },
        )
    )
