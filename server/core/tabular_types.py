"""toorow -- shared vocabulary, errors and shapes of the tabular import chain.

Closed vocabularies (formats, write modes, budgets, rejection rules), the
``CsvExcelImportError`` family, and the five dataclasses every other module of
the chain speaks in: ``ColumnSpec``, ``ParserIssue``, ``RejectedRow``,
``ParseResult``, ``ImportPreview``.

This module depends on NO other module of the chain. Everything else imports
from here, which is what keeps the chain acyclic.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from core.inbound_scan import DEFAULT_MAX_BYTES

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Closed vocabularies (mirrors 12.8 managed_feed_ledger).
# ---------------------------------------------------------------------------

FORMAT_CSV = "csv"
FORMAT_EXCEL = "excel"
FORMAT_SAV = "sav"
SUPPORTED_FORMATS = frozenset({FORMAT_CSV, FORMAT_EXCEL, FORMAT_SAV})

WRITE_MODE_REPLACE = "replace"
WRITE_MODE_APPEND = "append"

# ---------------------------------------------------------------------------
# Size + structural guards (DoS / sanity limits).
# These are documented defaults — overridable per project preference in Phase B.
# ---------------------------------------------------------------------------

# The upload size bound has ONE source of truth: the inbound scan's
# DEFAULT_MAX_BYTES (25 MiB), the largest object a parser may ever see. The
# intake body bound (INBOUND_MAX_BODY_BYTES) and the quarantine default are
# already calibrated on it; a route that admitted more would accept a file the
# worker then rejects AFTER landing. `inbound_scan` is not a module of this
# chain (it sits BEFORE it), so importing the constant keeps the chain acyclic.
MAX_FILE_BYTES = DEFAULT_MAX_BYTES
MAX_ROWS = 500_000  # bounded full-file scan
MAX_COLUMNS = 500  # wide-enough for real datasets
MAX_FIELD_CHARS = 1_000_000
MAX_XLSX_XML_BYTES = 16 * 1024 * 1024
MAX_XLSX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 100

# Excel-specific guards (same spirit as mediaplan_import.py limits).
MAX_SHEET_ROWS = 500_000
MAX_SHEET_COLUMNS = 500

# Bounded preview returned WITHOUT publishing.
PREVIEW_ROW_LIMIT = 100

# Samples collected per column for type inference (cap; only [:10] are stored).
SAMPLE_CAP = 100

# Supported text encodings tried in order (CSV only).
_ENCODINGS = ["utf-8-sig", "utf-8", "utf-16-le", "utf-16-be", "cp1252"]

# Allowed delimiters for auto-detection (CSV).
_DELIMITERS = [",", ";", "\t", "|"]

# Date format tokens we recognise in contract declarations.
SUPPORTED_DATE_FORMATS = frozenset({"%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%Y%m%d"})

# Locale codes we understand for numeric formatting.
SUPPORTED_LOCALES = frozenset({"en", "fr"})

# Per-column types an operator may DECLARE in the contract (`column_types`). A
# declared type is AUTHORITATIVE and overrides the leading-value inference -- it kills
# the false-rejects the heuristic causes on columns whose head merely LOOKS typed (an
# ID/SKU column starting "00123" -> integer; an 8-digit code starting "20260101" ->
# date). 'text' declared on such a column makes it accept everything.
SUPPORTED_COLUMN_TYPES = frozenset({"integer", "decimal", "date", "boolean", "text"})

# ---------------------------------------------------------------------------
# Per-row rejection rule codes (stable; stored on managed_feed_rejected_rows).
# ---------------------------------------------------------------------------

RULE_DATE_FORMAT = "date_format"  # value does not parse under contract date_format
RULE_TYPE_MISMATCH = "type_mismatch"  # non-coercible value in a confirmed-typed column
RULE_SHORT_ROW = "short_row"  # row has fewer fields than the header
RULE_LONG_ROW = "long_row"  # row has more fields than the header (extra data)

# ---------------------------------------------------------------------------
# Typed error hierarchy (stable codes -> callers map to HTTP status).
# ---------------------------------------------------------------------------


class CsvExcelImportError(Exception):
    """Base for all import-layer errors. Carries a stable ``code``."""

    def __init__(self, code: str, detail: str = "", *, repair: dict | None = None) -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail
        self.repair = repair or {}


class UnsupportedFileType(CsvExcelImportError):
    """The upload is neither CSV nor Excel (-> 422)."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(
            "unsupported_file_type",
            detail or "File must be .csv, .tsv, .txt (CSV) or .xlsx, .xls (Excel)",
        )


class FileTooLarge(CsvExcelImportError):
    """The upload exceeds the size guard (-> 422)."""

    def __init__(self, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            "file_too_large",
            f"File size {size_bytes} bytes exceeds maximum {max_bytes} bytes",
            repair={"compress_or_split": True},
        )


class EncodingError(CsvExcelImportError):
    """The file could not be decoded with any supported encoding (-> 422)."""

    def __init__(self, tried: list[str]) -> None:
        super().__init__(
            "encoding_error",
            f"File could not be decoded. Tried encodings: {tried}. Re-save as UTF-8.",
            repair={"re_encode_as_utf8": True},
        )


class EmptyFile(CsvExcelImportError):
    """The file has no content at all (zero bytes or all whitespace) (-> 422)."""

    def __init__(self) -> None:
        super().__init__(
            "empty_file",
            "The uploaded file is empty (zero bytes or blank).",
            repair={"upload_non_empty_file": True},
        )


class DuplicateColumns(CsvExcelImportError):
    """The header row contains duplicate column names (-> 422)."""

    def __init__(self, duplicates: list[str]) -> None:
        super().__init__(
            "duplicate_columns",
            f"Duplicate column names found: {', '.join(duplicates)}",
            repair={"rename_duplicate_columns": True},
        )


class NoHeaderRow(CsvExcelImportError):
    """The file has content but no valid header row (-> 422)."""

    def __init__(self) -> None:
        super().__init__(
            "no_header_row",
            "Could not detect a header row. Ensure the first row contains column names.",
            repair={"add_header_row": True},
        )


class SheetNotFound(CsvExcelImportError):
    """The requested sheet name does not exist in the workbook (-> 422)."""

    def __init__(self, sheet: str, available: list[str]) -> None:
        super().__init__(
            "sheet_not_found",
            f"Sheet '{sheet}' not found. Available: {available}",
            repair={"pick_available_sheet": available},
        )


class FormulaInCells(CsvExcelImportError):
    """Cells contain formula strings instead of computed values (-> 422).

    The import contract must specify ``formulas_as_values=True`` (data_only mode)
    or the file must be re-exported with values only.
    """

    def __init__(self, count: int) -> None:
        super().__init__(
            "formula_in_cells",
            f"{count} cell(s) contain formula strings (=...). "
            "Re-export the workbook as values-only, or set formulas_as_values=true "
            "in the import contract.",
            repair={"export_values_only": True, "or_set_formulas_as_values": True},
        )


class AppendUnavailable(CsvExcelImportError):
    """Append write mode is requested but no stable key + dedup contract exists.

    Replace is the safe, supported mode for 12.9.
    """

    def __init__(self) -> None:
        super().__init__(
            "append_unavailable",
            "Append mode is unavailable: no stable key and deduplication contract "
            "exists for this datastream. Use Replace (write_mode='replace'), which "
            "validates the full candidate before the active pointer changes.",
            repair={"use_replace_mode": True},
        )


class EmptyImportBlocked(CsvExcelImportError):
    """Zero rows were parsed and publication is blocked by default (-> 422)."""

    def __init__(self) -> None:
        super().__init__(
            "empty_import_blocked",
            "The file produced zero rows after parsing. Publication is blocked by "
            "default. To intentionally publish an empty dataset, call again with "
            "force_empty_publish=True and ensure the project preference "
            "'allow_empty_publication' is enabled.",
            repair={
                "add_data_rows": True,
                "or_set_force_empty_publish_with_project_preference": True,
            },
        )


class InvalidImportContract(CsvExcelImportError):
    """The import contract is malformed or contains unsupported settings (-> 422)."""

    def __init__(self, detail: str) -> None:
        super().__init__("invalid_import_contract", detail)


class ParserReviewRequired(CsvExcelImportError):
    """Parsing cannot proceed unattended until a human resolves an ambiguity."""

    def __init__(self, issues: list["ParserIssue"]) -> None:
        super().__init__(
            "parser_review_required",
            "The parsing contract is ambiguous and requires operator review.",
            repair={"issues": [issue.as_dict() for issue in issues]},
        )


# ---------------------------------------------------------------------------
# Data structures (pure Python, no I/O).
# ---------------------------------------------------------------------------


@dataclass
class ColumnSpec:
    """One column resolved from parsing."""

    name: str  # header label as-found in the file
    index: int  # 0-based column index
    detected_type: str  # "text" | "integer" | "decimal" | "date" | "boolean" | "mixed"
    date_format: str | None = None  # detected or contract-declared date format
    null_count: int = 0
    sample_values: list[Any] = field(default_factory=list)
    source_name: str | None = None
    source_index: int | None = None
    source_type: str | None = None
    locale: str | None = None


@dataclass(frozen=True)
class ParserIssue:
    """Bounded, machine-readable candidate issue; never contains raw file data."""

    code: str
    field: str | None = None
    blocking: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "field": self.field, "blocking": self.blocking}


@dataclass
class RejectedRow:
    """A row that failed a validation rule."""

    row_number: int  # 1-based, matching the file line number (header = 1)
    field_name: str  # column name or "" for row-level issues
    rule: str  # stable rule code (e.g. "date_format", "type_mismatch")
    reason: str  # human-readable explanation
    rejected_value: str  # bounded/redacted original value (max 500 chars)
    # The MONEY this rejected row carried, when the amount was readable
    # (Decimal-as-string, JSON-safe). ``None`` when the row carried no readable
    # amount, which is NOT the same as zero.
    #
    # A rejected row must let a person tell "not in the file" from "dropped on
    # the way", and for a rejected row that had a perfectly readable amount the
    # amount IS the thing they need to see. Without it the reconciliation could
    # only say a total was missing somewhere; with it, the report names the row.
    # Additive with a default, so every existing constructor is unaffected.
    rejected_amount: str | None = None


@dataclass
class ParseResult:
    """Output of parse_csv / parse_excel: accepted rows + evidence."""

    rows: list[dict[str, Any]]  # accepted rows as column->value dicts
    rejected: list[RejectedRow]  # per-row rejection detail
    columns: list[ColumnSpec]  # resolved column specs
    encoding: str  # encoding used (CSV) or "xlsx"/"xls" (Excel)
    delimiter: str | None  # delimiter used (CSV) or None (Excel)
    sheet_name: str | None  # sheet parsed (Excel) or None (CSV)
    detected_row_count: int  # total data rows seen (accepted + rejected)
    content_hash: str  # SHA-256 of the raw input bytes
    # Story 22.10 (AD-4): an OPTIONAL parse-level metadata block a reshape producer
    # may surface (e.g. the media-plan top ``label: value`` block). Additive with a
    # default so the CSV/Excel parsers (Story 12.9) are unaffected.
    metadata: dict[str, Any] = field(default_factory=dict)
    envelope_version: str = "tabular-parse-v1"
    source_format: str | None = None
    schema_fingerprint: str = ""
    warnings: list[str] = field(default_factory=list)
    issues: list[ParserIssue] = field(default_factory=list)
    # The money the SOURCE file carries in the amount column, measured by a pass
    # INDEPENDENT of the one that produced ``rows`` (each merged range counted
    # once), and the part of it carried by rejected rows. Decimal-as-string so the
    # envelope stays JSON-safe. ``None`` when no producer measured them -- the
    # reconciliation is then skipped rather than guessed. Additive with a default,
    # so the CSV/Excel parsers (Story 12.9) are unaffected.
    source_amount_total: str | None = None
    rejected_amount_total: str | None = None

    @property
    def accepted_count(self) -> int:
        return len(self.rows)

    @property
    def rejected_count(self) -> int:
        return len(self.rejected)


@dataclass
class ImportPreview:
    """Bounded preview returned by build_preview WITHOUT any publish."""

    format: str  # "csv" or "excel"
    encoding: str
    delimiter: str | None
    sheet_name: str | None
    columns: list[ColumnSpec]
    row_count: int  # total accepted rows
    rejected_count: int
    preview_rows: list[dict[str, Any]]  # first PREVIEW_ROW_LIMIT accepted rows
    content_hash: str
    contract_version_id: str | None  # if a contract was applied
    schema_fingerprint: str = ""
    envelope_version: str = "tabular-parse-v1"
    warnings: list[str] = field(default_factory=list)
    issues: list[ParserIssue] = field(default_factory=list)
    producer_format: str | None = None
