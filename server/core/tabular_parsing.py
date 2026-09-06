"""toorow -- format detection, decoding, typing and CSV parsing (pure, no I/O).

The PARSE layer: raw bytes into a ``ParseResult``, without touching a database,
a warehouse or a ledger. Every function here is unit-testable on in-memory
bytes.

Excel lives next door in ``xlsx_parsing`` because inspecting a workbook safely
is a body of rules of its own; the coercion and column-typing helpers are shared
from here so a CSV column and an Excel column are typed by the same code.
"""

from __future__ import annotations

import csv as _csv
import hashlib
import io
import json
import logging
import math
import re as _re
from datetime import date, datetime
from typing import Any

from core.inbound_scan import FORMULA_LEADERS
from core.tabular_types import (
    _DELIMITERS,
    _ENCODINGS,
    FORMAT_CSV,
    FORMAT_EXCEL,
    FORMAT_SAV,
    MAX_COLUMNS,
    MAX_FIELD_CHARS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    RULE_DATE_FORMAT,
    RULE_LONG_ROW,
    RULE_TYPE_MISMATCH,
    SAMPLE_CAP,
    SUPPORTED_LOCALES,
    ColumnSpec,
    CsvExcelImportError,
    DuplicateColumns,
    EmptyFile,
    EncodingError,
    FileTooLarge,
    FormulaInCells,
    InvalidImportContract,
    NoHeaderRow,
    ParseResult,
    ParserIssue,
    RejectedRow,
    UnsupportedFileType,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure helper: content hash.
# ---------------------------------------------------------------------------


def _content_hash(data: bytes) -> str:
    """SHA-256 hex of the raw upload bytes (64 hex chars)."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Format detection (pure, no I/O).
# ---------------------------------------------------------------------------

_CSV_EXTENSIONS = frozenset({".csv", ".tsv", ".txt"})
_EXCEL_EXTENSIONS = frozenset({".xlsx", ".xls"})
_SAV_EXTENSIONS = frozenset({".sav"})

# Excel magic bytes (first 4 bytes of the file).
_XLSX_MAGIC = b"PK\x03\x04"  # ZIP-based (xlsx, xlsm, ...)
_XLS_MAGIC = b"\xd0\xcf\x11\xe0"  # OLE2 compound document (legacy xls)


def detect_format(
    filename: str | None,
    data: bytes,
) -> str:
    """Return ``FORMAT_CSV`` or ``FORMAT_EXCEL``; raise ``UnsupportedFileType`` otherwise.

    Detection order:
      1. Magic bytes (content wins over filename when unambiguous).
      2. File extension (lower-cased).
    """
    if data[:4] == _XLSX_MAGIC or data[:4] == _XLS_MAGIC:
        return FORMAT_EXCEL
    if data[:4] in {b"$FL2", b"$FL3"}:
        return FORMAT_SAV
    ext = ""
    if filename:
        name = filename.strip().lower()
        for suffix in _EXCEL_EXTENSIONS:
            if name.endswith(suffix):
                return FORMAT_EXCEL
        for suffix in _SAV_EXTENSIONS:
            if name.endswith(suffix):
                return FORMAT_SAV
        for suffix in _CSV_EXTENSIONS:
            if name.endswith(suffix):
                return FORMAT_CSV
        dot = name.rfind(".")
        if dot >= 0:
            ext = name[dot:]
    raise UnsupportedFileType(f"Could not determine file type from content or extension '{ext}'")


# ---------------------------------------------------------------------------
# Upload meta validation (pure).
# ---------------------------------------------------------------------------


def validate_upload_meta(
    data: bytes,
    *,
    max_bytes: int = MAX_FILE_BYTES,
) -> None:
    """Validate raw upload bytes before parsing.

    Raises:
      EmptyFile   -- zero bytes or all whitespace.
      FileTooLarge -- size exceeds ``max_bytes``.
    """
    if not data or not data.strip():
        raise EmptyFile()
    if len(data) > max_bytes:
        raise FileTooLarge(len(data), max_bytes)


# ---------------------------------------------------------------------------
# CSV parsing (pure).
# ---------------------------------------------------------------------------


def _normalise_encoding_name(value: str) -> str:
    key = value.strip().lower().replace("_", "-")
    aliases = {
        "utf8": "utf-8",
        "utf-8-sig": "utf-8-sig",
        "utf16": "utf-16",
        "utf-16le": "utf-16-le",
        "utf-16be": "utf-16-be",
        "windows-1252": "cp1252",
        "latin1": "iso-8859-1",
        "latin-1": "iso-8859-1",
    }
    return aliases.get(key, key)


def _detect_encoding(data: bytes, declared: str | None = None) -> tuple[str, str]:
    """Decode only a declared encoding or an encoding demonstrated by bytes.

    UTF-16 is accepted only with a matching BOM.  Undeclared single-byte input is
    labelled CP1252 (never Latin-1) because the C1 byte range differentiates it;
    opaque control-heavy bytes fail closed instead of being treated as text.
    """
    if declared:
        enc = _normalise_encoding_name(declared)
        if enc == "utf-16":
            if data.startswith(b"\xff\xfe"):
                enc = "utf-16-le"
            elif data.startswith(b"\xfe\xff"):
                enc = "utf-16-be"
            else:
                raise CsvExcelImportError("encoding_bom_required", "UTF-16 requires a BOM.")
        if enc in {"utf-16-le", "utf-16-be"}:
            expected = b"\xff\xfe" if enc.endswith("le") else b"\xfe\xff"
            if not data.startswith(expected):
                raise CsvExcelImportError(
                    "encoding_bom_mismatch", "Declared UTF-16 byte order does not match the BOM."
                )
        if enc not in {"utf-8", "utf-8-sig", "utf-16-le", "utf-16-be", "cp1252", "iso-8859-1"}:
            raise InvalidImportContract(f"Unsupported CSV encoding {declared!r}.")
        try:
            text = data.decode(enc)
        except UnicodeDecodeError as exc:
            raise EncodingError([enc]) from exc
        return _validate_decoded_text(text.lstrip("\ufeff")), enc

    if data.startswith(b"\xff\xfe"):
        return _validate_decoded_text(data.decode("utf-16-le").lstrip("\ufeff")), "utf-16-le"
    if data.startswith(b"\xfe\xff"):
        return _validate_decoded_text(data.decode("utf-16-be").lstrip("\ufeff")), "utf-16-be"
    if data.startswith(b"\xef\xbb\xbf"):
        return _validate_decoded_text(data.decode("utf-8-sig")), "utf-8-sig"
    try:
        return _validate_decoded_text(data.decode("utf-8")), "utf-8"
    except UnicodeDecodeError:
        pass
    # NUL and non-whitespace C0 controls are not tabular text. CP1252's five
    # undefined bytes are rejected as undecidable arbitrary data.
    forbidden = {0x00, 0x81, 0x8D, 0x8F, 0x90, 0x9D}
    if any(byte in forbidden or (byte < 0x20 and byte not in {0x09, 0x0A, 0x0D}) for byte in data):
        raise EncodingError(_ENCODINGS)
    try:
        return _validate_decoded_text(data.decode("cp1252")), "cp1252"
    except UnicodeDecodeError as exc:
        raise EncodingError(_ENCODINGS) from exc


def _validate_decoded_text(text: str) -> str:
    """Reject non-tabular C0 controls regardless of the selected codec."""
    if any(ord(char) < 0x20 and char not in {"\t", "\n", "\r"} for char in text):
        raise EncodingError(_ENCODINGS)
    return text


def _detect_delimiter(sample: str) -> tuple[str, ParserIssue | None]:
    """Return a demonstrable delimiter and a review issue when none is unique."""
    head = sample[:8192]
    try:
        dialect = _csv.Sniffer().sniff(head, delimiters="".join(_DELIMITERS))
    except _csv.Error:
        return ",", ParserIssue("delimiter_undetermined")
    candidates: list[str] = []
    for candidate in _DELIMITERS:
        try:
            reader = _csv.reader(io.StringIO(head, newline=""), delimiter=candidate, strict=True)
            widths = [len(row) for row in reader if any(cell.strip() for cell in row)][:20]
        except _csv.Error:
            continue
        if widths and min(widths) > 1 and len(set(widths)) == 1:
            candidates.append(candidate)
    issue = ParserIssue("delimiter_ambiguous") if len(candidates) > 1 else None
    return dialect.delimiter, issue


# A comma-grouped integer looks like 1,234 or 12,345,678: groups of exactly 3
# digits after the first 1-3 digits. A stray "1,5" is NOT thousands-grouped (the
# group after the comma is not 3 digits) so it stays a decimal candidate (LOW fix:
# integer inference must not strip a decimal comma into a fake integer).
_THOUSANDS_GROUPED = _re.compile(r"^-?\d{1,3}(,\d{3})+$")


def _strip_ws(s: str) -> str:
    """Strip surrounding whitespace + interior thousands spaces (regular + NBSP + thin)."""
    return s.strip().replace(" ", "").replace(" ", "").replace(" ", "")


def _looks_like_thousands_grouped(s: str) -> bool:
    return bool(_THOUSANDS_GROUPED.match(s))


def _coerce_integer(raw: str) -> int:
    """Coerce a stripped string to int (thousands-grouping tolerated). Raises ValueError."""
    s = _strip_ws(raw)
    if _looks_like_thousands_grouped(s):
        s = s.replace(",", "")
    return int(s)


def _coerce_decimal(raw: str, locale: str | None = None) -> float:
    """Coerce a stripped string to float (FR/EN decimal conventions). Raises ValueError."""
    s = _strip_ws(raw)
    if locale == "fr":
        if "," in s and "." in s:
            s = s.replace(".", "").replace(",", ".")
        elif "," in s:
            s = s.replace(",", ".")
        return float(s)
    if locale == "en":
        if "," in s:
            if not _looks_like_thousands_grouped(s) and "." not in s:
                raise ValueError("ambiguous English decimal separator")
            s = s.replace(",", "")
        return float(s)
    if "," in s and "." in s:
        # Both present: the LAST separator is the decimal point.
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")  # FR: 1.234,56
        else:
            s = s.replace(",", "")  # EN: 1,234.56
    elif "," in s:
        if _looks_like_thousands_grouped(s):
            s = s.replace(",", "")  # 1,234 (thousands)
        else:
            s = s.replace(",", ".")  # 1,5 (FR decimal)
    return float(s)


_DATE_SHAPE = _re.compile(
    r"^(?:[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}|[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{4}|[0-9]{8})$"
)


def _parse_date_value(text: str, fmt: str) -> date:
    """Parse the five governed shapes without general-purpose strptime backtracking."""
    if fmt == "%Y-%m-%d":
        year, month, day = (int(part) for part in text.split("-"))
    elif fmt == "%Y%m%d":
        year, month, day = int(text[:4]), int(text[4:6]), int(text[6:8])
    elif fmt == "%d-%m-%Y":
        day, month, year = (int(part) for part in text.split("-"))
    elif fmt == "%d/%m/%Y":
        day, month, year = (int(part) for part in text.split("/"))
    elif fmt == "%m/%d/%Y":
        month, day, year = (int(part) for part in text.split("/"))
    else:  # guarded by contract validation; defensive for internal callers.
        raise ValueError(fmt)
    return date(year, month, day)


def _matching_date_formats(text: str) -> set[str]:
    if not _DATE_SHAPE.fullmatch(text):
        return set()
    if "/" in text:
        candidates = ("%d/%m/%Y", "%m/%d/%Y")
    elif "-" not in text:
        candidates = ("%Y%m%d",)
    elif len(text.split("-", 1)[0]) == 4:
        candidates = ("%Y-%m-%d",)
    else:
        candidates = ("%d-%m-%Y",)
    matches: set[str] = set()
    for fmt in candidates:
        try:
            _parse_date_value(text, fmt)
            matches.add(fmt)
        except (TypeError, ValueError):
            pass
    return matches


def _is_coercible(raw: str, detected_type: str, date_format: str | None) -> bool:
    """Return True if ``raw`` is coercible to the column's confirmed type (C2 evidence).

    ``text`` / ``mixed`` never reject (any string is valid text). An empty value is
    honest null, never a rejection (AD-9). ``boolean`` / ``integer`` / ``decimal`` /
    ``date`` reject non-coercible values.
    """
    s = raw.strip()
    if s == "":
        return True
    if detected_type in ("text", "mixed"):
        return True
    if detected_type == "boolean":
        return s.lower() in {"true", "false", "yes", "no", "1", "0", "oui", "non"}
    if detected_type == "integer":
        try:
            _coerce_integer(s)
            return True
        except ValueError:
            return False
    if detected_type == "decimal":
        try:
            _coerce_decimal(s)
            return True
        except ValueError:
            return False
    if detected_type == "date":
        if not _DATE_SHAPE.fullmatch(s):
            return False
        fmts = [date_format] if date_format else sorted(_matching_date_formats(s))
        for fmt in fmts:
            if not fmt:
                continue
            try:
                _parse_date_value(s, fmt)
                return True
            except ValueError:
                continue
        return False
    return True


def _resolve_column_type(
    col_name: str,
    values: list[Any],
    column_types: dict[str, str] | None,
) -> tuple[str, str | None]:
    """Resolve a column's type. A DECLARED contract type (``column_types[col_name]``)
    is authoritative and short-circuits the leading-value inference -- this is the fix
    for the heuristic's false-rejects on ID/SKU/YYYYMMDD-looking columns. Returns
    ``(type, date_format_or_None)``; a declared column carries no inferred date_format,
    so the caller's global contract ``date_format`` (or the supported-format set)
    governs a declared 'date' column. Undeclared columns fall back to inference.
    """
    declared = (column_types or {}).get(col_name)
    if declared:
        return declared, None
    return _infer_column_type(values)


def _classify_value(value: Any) -> tuple[str, str | None]:
    if isinstance(value, datetime):
        return "date", "%Y-%m-%d"
    if isinstance(value, date):
        return "date", "%Y-%m-%d"
    if isinstance(value, bool):
        return "boolean", None
    if isinstance(value, int):
        return "integer", None
    if isinstance(value, float):
        return "decimal", None
    text = str(value).strip()
    date_formats = _matching_date_formats(text)
    if date_formats:
        return "date", next(iter(date_formats)) if len(date_formats) == 1 else None
    try:
        _coerce_integer(text)
        return "integer", None
    except ValueError:
        pass
    try:
        _coerce_decimal(text)
        return "decimal", None
    except ValueError:
        pass
    if text.lower() in {"true", "false", "yes", "no", "oui", "non"}:
        return "boolean", None
    return "text", None


def _infer_column_type(values: list[Any]) -> tuple[str, str | None]:
    """Infer from the bounded representative sample, independent of row order."""
    observed = [_classify_value(v) for v in values if v is not None and str(v).strip()]
    if not observed:
        return "text", None
    kinds = {kind for kind, _ in observed}
    if kinds <= {"integer", "decimal"}:
        return ("decimal" if "decimal" in kinds else "integer"), None
    if len(kinds) != 1:
        return "mixed", None
    kind = next(iter(kinds))
    formats = {fmt for observed_kind, fmt in observed if observed_kind == "date" and fmt}
    return kind, (next(iter(formats)) if len(formats) == 1 else None)


def _representative_samples(
    rows: list[tuple[int, list[Any]]], column_count: int
) -> dict[int, list[Any]]:
    """Return stable bounded samples selected from the complete bounded input."""
    buckets: dict[int, dict[str, Any]] = {index: {} for index in range(column_count)}
    representatives: dict[int, dict[tuple[str, str | None], tuple[str, Any]]] = {
        index: {} for index in range(column_count)
    }
    for _, cells in rows:
        for index, value in enumerate(cells[:column_count]):
            if value is None or (isinstance(value, str) and not value.strip()):
                continue
            canonical = f"{type(value).__name__}\0{value}".encode("utf-8", "surrogatepass")
            key = hashlib.sha256(canonical).hexdigest()
            bucket = buckets[index]
            bucket.setdefault(key, value)
            if len(bucket) > SAMPLE_CAP:
                del bucket[max(bucket)]
            category = _classify_value(value)
            current = representatives[index].get(category)
            if current is None or key < current[0]:
                representatives[index][category] = (key, value)
    samples: dict[int, list[Any]] = {}
    for index, bucket in buckets.items():
        selected = {key: value for key, value in representatives[index].values()}
        for key in sorted(bucket):
            selected.setdefault(key, bucket[key])
            if len(selected) >= SAMPLE_CAP:
                break
        samples[index] = [selected[key] for key in sorted(selected)[:SAMPLE_CAP]]
    return samples


def _locale_numeric_sample(values: list[Any], locale: str | None) -> bool:
    if locale != "fr" or not values or not any("," in str(value) for value in values):
        return False
    return all(
        _re.fullmatch(r"-?[0-9 \u00a0\u202f]+(?:[.,][0-9]+)*", str(value).strip())
        for value in values
    )


def _resolved_date_format(
    detected_type: str,
    detected_format: str | None,
    values: list[Any],
    explicit_format: str | None,
    locale: str | None,
) -> str | None:
    if explicit_format or detected_type != "date":
        return explicit_format or detected_format
    has_slash_date = any(
        isinstance(value, str)
        and "/" in value
        and any(_is_coercible(value, "date", candidate) for candidate in ("%d/%m/%Y", "%m/%d/%Y"))
        for value in values
    )
    if has_slash_date and locale in SUPPORTED_LOCALES:
        return "%d/%m/%Y" if locale == "fr" else "%m/%d/%Y"
    return detected_format


def _coerce_typed(value: Any, spec: ColumnSpec) -> Any:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        if spec.detected_type in {"text", "mixed"}:
            return str(value)
        if spec.detected_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(value)
            return value
        if spec.detected_type == "decimal":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError(value)
            if not math.isfinite(float(value)):
                raise ValueError(value)
            return value
        if spec.detected_type == "boolean":
            if not isinstance(value, bool):
                raise TypeError(value)
            return value
        if spec.detected_type == "date":
            if isinstance(value, datetime):
                return value.date()
            if isinstance(value, date):
                return value
            raise TypeError(value)
        raise TypeError(value)
    text = value.strip()
    if spec.detected_type in {"text", "mixed"}:
        return text
    if spec.detected_type == "integer":
        return _coerce_integer(text)
    if spec.detected_type == "decimal":
        number = _coerce_decimal(text, spec.locale)
        if not math.isfinite(float(number)):
            raise ValueError(value)
        return number
    if spec.detected_type == "boolean":
        lowered = text.lower()
        if lowered in {"true", "yes", "1", "oui"}:
            return True
        if lowered in {"false", "no", "0", "non"}:
            return False
        raise ValueError(value)
    if spec.detected_type == "date":
        formats = [spec.date_format] if spec.date_format else sorted(_matching_date_formats(text))
        for fmt in formats:
            if not fmt:
                continue
            try:
                return _parse_date_value(text, fmt)
            except ValueError:
                pass
        raise ValueError(text)
    return text


def _schema_fingerprint(columns: list[ColumnSpec]) -> str:
    payload = [
        {
            "index": col.index,
            "name": col.name,
            "type": col.detected_type,
            "date_format": col.date_format,
            "source_type": col.source_type,
            "locale": col.locale,
        }
        for col in columns
    ]
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _finalize_parse_result(result: ParseResult) -> ParseResult:
    for col in result.columns:
        if col.source_name is None:
            col.source_name = col.name
        if col.source_index is None:
            col.source_index = col.index
    result.schema_fingerprint = _schema_fingerprint(result.columns)
    return result


def _candidate_ambiguities(
    rows: list[tuple[int, list[Any]]],
    names: list[str],
    *,
    date_format: str | None,
    locale: str | None,
) -> list[ParserIssue]:
    issues: list[ParserIssue] = []
    seen: set[tuple[str, str | None]] = set()
    observed_date_formats: dict[str, set[str]] = {name: set() for name in names}
    for _, cells in rows:
        for index, value in enumerate(cells[: len(names)]):
            text = str(value).strip() if value is not None else ""
            field = names[index]
            if text and date_format is None:
                matches = _matching_date_formats(text)
                slash_matches = matches & {"%d/%m/%Y", "%m/%d/%Y"}
                if len(slash_matches) > 1:
                    if locale in SUPPORTED_LOCALES:
                        observed_date_formats[field].add(
                            "%d/%m/%Y" if locale == "fr" else "%m/%d/%Y"
                        )
                    elif ("date_locale_ambiguous", field) not in seen:
                        issues.append(ParserIssue("date_locale_ambiguous", field))
                        seen.add(("date_locale_ambiguous", field))
                else:
                    observed_date_formats[field].update(matches)
            if locale is None and _re.fullmatch(r"-?\d{1,3}[,.]\d{3}", text):
                if ("numeric_locale_ambiguous", field) not in seen:
                    issues.append(ParserIssue("numeric_locale_ambiguous", field))
                    seen.add(("numeric_locale_ambiguous", field))
    if date_format is None:
        for field, formats in observed_date_formats.items():
            if len(formats) > 1 and ("date_format_mixed", field) not in seen:
                issues.append(ParserIssue("date_format_mixed", field))
                seen.add(("date_format_mixed", field))
    return issues


def _strip_bom(value: str) -> str:
    """Drop a leading UTF-8/UTF-16 BOM char from a header/cell label.

    Defends against DOUBLE-BOM exports (a literal ``﻿`` in the source that then
    gets ``utf-8-sig`` re-encoded): the ``utf-8-sig`` decode strips only the FIRST BOM,
    so the first header would otherwise read ``﻿col_a``. This keeps column names
    honest without changing the detected encoding.
    """
    return value.lstrip("﻿")


def _truncate_value(v: Any, max_len: int = 500) -> str:
    """Bounded/redacted string for rejected-row storage (AD-9)."""
    s = str(v) if v is not None else ""
    if len(s) > max_len:
        return s[:max_len] + "…"
    return s


def _validate_rows(
    accepted_rows: list[tuple[int, list[Any]]],
    col_specs: list[ColumnSpec],
    col_names: list[str],
) -> tuple[list[dict[str, Any]], list[RejectedRow]]:
    """Validate bounded rows and emit typed values under the resolved schema."""
    accepted: list[dict[str, Any]] = []
    rejected: list[RejectedRow] = []
    n_cols = len(col_names)
    for line_no, cells in accepted_rows:
        if len(cells) > n_cols:
            rejected.append(
                RejectedRow(
                    line_no,
                    "",
                    RULE_LONG_ROW,
                    f"row has {len(cells)} fields but the header declares {n_cols} columns",
                    _truncate_value(cells),
                )
            )
            continue
        row_dict: dict[str, Any] = {}
        for index, col_name in enumerate(col_names):
            raw = cells[index] if index < len(cells) else None
            spec = col_specs[index]
            try:
                row_dict[col_name] = _coerce_typed(raw, spec)
            except (TypeError, ValueError):
                rule = RULE_DATE_FORMAT if spec.detected_type == "date" else RULE_TYPE_MISMATCH
                rejected.append(
                    RejectedRow(
                        row_number=line_no,
                        field_name=col_name,
                        rule=rule,
                        reason=f"value is not a valid {spec.detected_type} for column '{col_name}'",
                        rejected_value=_truncate_value(raw),
                    )
                )
                break
        else:
            accepted.append(row_dict)
    return accepted, rejected


def parse_csv(
    data: bytes,
    *,
    delimiter: str | None = None,
    encoding: str | None = None,
    date_format: str | None = None,
    locale: str | None = None,
    header_row: int = 1,
    max_rows: int = MAX_ROWS,
    max_columns: int = MAX_COLUMNS,
    max_field_chars: int = MAX_FIELD_CHARS,
    check_formulas: bool = True,
    column_types: dict[str, str] | None = None,
) -> ParseResult:
    """Strict, quote-aware and bounded CSV/TSV parser."""
    if (
        max_rows < 0
        or max_rows > MAX_ROWS
        or not isinstance(header_row, int)
        or isinstance(header_row, bool)
        or header_row < 1
        or header_row > max_rows + 1
        or max_columns < 1
        or max_columns > MAX_COLUMNS
        or max_field_chars < 1
        or max_field_chars > MAX_FIELD_CHARS
    ):
        raise InvalidImportContract("CSV bounds must be positive and within platform limits.")
    text, enc = _detect_encoding(data, encoding)
    used_delim, delimiter_issue = (delimiter, None) if delimiter else _detect_delimiter(text)
    if used_delim not in _DELIMITERS:
        raise InvalidImportContract(f"Unsupported delimiter {used_delim!r}.")

    previous_limit = _csv.field_size_limit()
    _csv.field_size_limit(max_field_chars)
    try:
        reader = _csv.reader(io.StringIO(text, newline=""), delimiter=used_delim, strict=True)
        header: list[str] | None = None
        non_blank: list[tuple[int, list[str]]] = []
        formula_count = 0
        try:
            for logical_index, raw_row in enumerate(reader, start=1):
                if any(len(cell) > max_field_chars for cell in raw_row):
                    raise CsvExcelImportError(
                        "field_limit_exceeded", "A field exceeds the configured character bound."
                    )
                if check_formulas:
                    # Same leaders the inbound scan neutralises (FORMULA_LEADERS):
                    # = + - @ TAB CR. A cell led by any of them is executable the
                    # moment a spreadsheet opens the export, so the parser refuses
                    # it on the SAME definition -- a third copy would diverge. The
                    # raw startswith catches TAB/CR-led cells; the lstripped one
                    # keeps catching space-padded "=".
                    formula_count += sum(
                        1
                        for cell in raw_row
                        if cell.startswith(FORMULA_LEADERS)
                        or cell.lstrip().startswith(FORMULA_LEADERS)
                    )
                if logical_index < header_row:
                    continue
                if logical_index == header_row:
                    header = raw_row
                    continue
                if all(not cell.strip() for cell in raw_row):
                    continue
                if len(non_blank) >= max_rows:
                    raise CsvExcelImportError(
                        "row_limit_exceeded",
                        f"File contains more than the configured {max_rows} data rows.",
                        repair={"split_file": True, "max_rows": max_rows},
                    )
                non_blank.append((reader.line_num, list(raw_row)))
        except _csv.Error as exc:
            code = "field_limit_exceeded" if "field larger" in str(exc).lower() else "malformed_csv"
            raise CsvExcelImportError(
                code, "CSV grammar is invalid or exceeds a configured bound."
            ) from exc
    finally:
        _csv.field_size_limit(previous_limit)

    if formula_count:
        raise FormulaInCells(formula_count)
    if header is None or not header:
        raise NoHeaderRow()
    col_names = [_strip_bom(value.strip()) for value in header]
    if all(not name for name in col_names):
        raise NoHeaderRow()
    if any(not name for name in col_names):
        raise CsvExcelImportError(
            "blank_header", "Every source column must have a non-blank header."
        )
    seen: dict[str, int] = {}
    for name in col_names:
        key = name.casefold()
        seen[key] = seen.get(key, 0) + 1
    duplicates = [name for name, count in seen.items() if count > 1]
    if duplicates:
        raise DuplicateColumns(duplicates)
    if len(col_names) > max_columns:
        raise CsvExcelImportError(
            "too_many_columns", f"File has {len(col_names)} columns; maximum is {max_columns}."
        )

    for _, cells in non_blank:
        while len(cells) > len(col_names) and not cells[-1].strip():
            cells.pop()
    samples = _representative_samples(non_blank, len(col_names))
    columns: list[ColumnSpec] = []
    for index, name in enumerate(col_names):
        detected_type, detected_format = _resolve_column_type(name, samples[index], column_types)
        if not (column_types or {}).get(name) and _locale_numeric_sample(samples[index], locale):
            detected_type, detected_format = "decimal", None
        resolved_date_format = _resolved_date_format(
            detected_type, detected_format, samples[index], date_format, locale
        )
        columns.append(
            ColumnSpec(
                name=name,
                index=index,
                detected_type=detected_type,
                date_format=resolved_date_format,
                sample_values=samples[index][:10],
                source_name=name,
                source_index=index,
                source_type="text",
                locale=locale,
            )
        )
    accepted, rejected = _validate_rows(non_blank, columns, col_names)
    for spec in columns:
        spec.null_count = sum(row.get(spec.name) is None for row in accepted)
    issues = _candidate_ambiguities(non_blank, col_names, date_format=date_format, locale=locale)
    if delimiter_issue:
        issues.insert(0, delimiter_issue)
    return _finalize_parse_result(
        ParseResult(
            rows=accepted,
            rejected=rejected,
            columns=columns,
            encoding=enc,
            delimiter=used_delim,
            sheet_name=None,
            detected_row_count=len(non_blank),
            content_hash=_content_hash(data),
            source_format=("tsv" if used_delim == "\t" else "csv"),
            issues=issues,
            metadata={
                "parser_policy": {
                    "header_row": header_row,
                    "date_format": date_format,
                    "locale": locale,
                    "max_rows": max_rows,
                    "max_columns": max_columns,
                    "max_field_chars": max_field_chars,
                    "formula_policy": "reject-v1" if check_formulas else "allow-explicit-v1",
                }
            },
        )
    )
