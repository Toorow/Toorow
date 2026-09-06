"""Bounded SPSS SAV parser used by the governed tabular registry."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import math
import pickle
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

from core.csv_excel_import import (
    ColumnSpec,
    CsvExcelImportError,
    ParseResult,
    _finalize_parse_result,
)

SAV_MAGIC = (b"$FL2", b"$FL3")
SAV_UNAVAILABLE = "sav_reader_unavailable"
SAV_CORRUPT = "sav_unreadable"
SAV_EMPTY = "sav_no_rows"
SAV_TOO_LARGE = "sav_byte_limit_exceeded"
SAV_TOO_MANY_ROWS = "sav_row_limit_exceeded"
SAV_TOO_MANY_COLUMNS = "sav_column_limit_exceeded"
SAV_TOO_MANY_CELLS = "sav_cell_limit_exceeded"
SAV_STRING_LIMIT = "sav_string_limit_exceeded"
SAV_MEMORY_LIMIT = "sav_memory_limit_exceeded"
SAV_DECOMPRESSION_LIMIT = "sav_decompression_limit_exceeded"
SAV_TIMEOUT = "sav_time_limit_exceeded"
SAV_NONFINITE = "sav_nonfinite_number"
SAV_METADATA_LIMIT = "sav_metadata_limit_exceeded"
SAV_TYPE_MISMATCH = "sav_declared_type_mismatch"
SAV_WORKER_ERROR = "sav_worker_unavailable"
SAV_UNSUPPORTED_VALUE = "sav_unsupported_value"
SAV_SCRATCH_ERROR = "sav_scratch_error"

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MAX_ROWS = 500_000
DEFAULT_MAX_COLUMNS = 500
DEFAULT_MAX_CELLS = 5_000_000
DEFAULT_MAX_STRING_CHARS = 1_000_000
DEFAULT_MAX_DECOMPRESSED_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_MEMORY_BYTES = 256 * 1024 * 1024
DEFAULT_MAX_SECONDS = 10.0


class SavAdapterError(CsvExcelImportError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(code, message)


def looks_like_sav(data: bytes) -> bool:
    return any(data.startswith(prefix) for prefix in SAV_MAGIC)


def _deadline(deadline: float) -> None:
    if time.monotonic() > deadline:
        raise SavAdapterError(SAV_TIMEOUT, "The SAV parser exceeded its configured deadline.")


def _bound(value: Any, name: str, ceiling: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= ceiling:
        raise SavAdapterError(
            "invalid_import_contract",
            f"'{name}' must be a positive integer no greater than {ceiling}.",
        )
    return value


def _system_missing(value: Any) -> bool:
    return value is None or isinstance(value, float) and math.isnan(value)


def _user_missing(value: Any, ranges: list[dict[str, Any]]) -> bool:
    if _system_missing(value):
        return False
    for span in ranges:
        lo, hi = span.get("lo"), span.get("hi")
        hi = lo if hi is None else hi
        try:
            if isinstance(value, str) or isinstance(lo, str) or isinstance(hi, str):
                if str(value) == str(lo) == str(hi):
                    return True
            elif lo is not None and float(lo) <= float(value) <= float(hi):
                return True
        except (TypeError, ValueError, OverflowError):
            pass
    return False


def _safe(value: Any, max_chars: int) -> Any:
    if _system_missing(value):
        return None
    if isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SavAdapterError(SAV_NONFINITE, "SAV contains a non-finite number.")
        return value
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, (dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    if isinstance(value, str):
        if len(value) > max_chars:
            raise SavAdapterError(SAV_STRING_LIMIT, "A SAV string exceeds the configured bound.")
        return value
    raise SavAdapterError(
        SAV_UNSUPPORTED_VALUE,
        f"SAV contains unsupported decoded type '{type(value).__name__}'.",
    )


def _coerce_declared(value: Any, declared_type: str) -> Any:
    """Apply the confirmed type contract instead of merely relabelling a column."""
    if declared_type == "text":
        return value if isinstance(value, str) else str(value)
    if declared_type == "integer":
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("+-").isdigit():
            return int(value.strip())
        raise ValueError
    if declared_type == "decimal":
        if isinstance(value, bool):
            raise ValueError
        number = float(value) if isinstance(value, str) else value
        if not isinstance(number, (int, float)) or not math.isfinite(float(number)):
            raise ValueError
        return number
    if declared_type == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in {0, 1}:
            return bool(value)
        if isinstance(value, str) and value.strip().lower() in {"true", "false", "0", "1"}:
            return value.strip().lower() in {"true", "1"}
        raise ValueError
    if declared_type == "date":
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return value
        if isinstance(value, str):
            try:
                return dt.datetime.fromisoformat(value)
            except ValueError:
                try:
                    return dt.date.fromisoformat(value)
                except ValueError as exc:
                    raise ValueError from exc
        raise ValueError
    raise ValueError


def _bounded_metadata_text(value: Any, max_chars: int, budget: list[int]) -> str:
    text = str(value)
    if len(text) > max_chars:
        raise SavAdapterError(SAV_STRING_LIMIT, "SAV metadata string exceeds its bound.")
    budget[0] += len(text.encode("utf-8", "surrogatepass"))
    if budget[0] > budget[1]:
        raise SavAdapterError(SAV_METADATA_LIMIT, "SAV metadata exceeds its byte budget.")
    return text


def _bounded_labels(meta: Any, max_chars: int, max_metadata_bytes: int):
    budget = [0, max_metadata_bytes]
    entries = 0
    variable_labels: dict[str, str] = {}
    for key, value in (getattr(meta, "column_names_to_labels", {}) or {}).items():
        if value is None:
            continue
        entries += 1
        if entries > 100_000:
            raise SavAdapterError(SAV_METADATA_LIMIT, "SAV metadata has too many entries.")
        variable_labels[_bounded_metadata_text(key, max_chars, budget)] = _bounded_metadata_text(
            value, max_chars, budget
        )
    value_labels: dict[str, dict[str, str]] = {}
    for variable, labels in (getattr(meta, "variable_value_labels", {}) or {}).items():
        safe_labels: dict[str, str] = {}
        for code, label in labels.items():
            entries += 1
            if entries > 100_000:
                raise SavAdapterError(SAV_METADATA_LIMIT, "SAV metadata has too many entries.")
            safe_labels[_bounded_metadata_text(code, max_chars, budget)] = _bounded_metadata_text(
                label, max_chars, budget
            )
        value_labels[_bounded_metadata_text(variable, max_chars, budget)] = safe_labels
    return variable_labels, value_labels, budget


def _metadata_type(
    name: str, meta: Any, values: list[Any], declared: dict[str, str]
) -> tuple[str, str]:
    source = str((getattr(meta, "original_variable_types", {}) or {}).get(name, ""))
    storage = str((getattr(meta, "readstat_variable_types", {}) or {}).get(name, ""))
    if name in declared:
        return declared[name], source or storage
    upper = source.upper()
    if storage == "string" or upper.startswith("A"):
        return "text", source or "string"
    if any(token in upper for token in ("DATE", "TIME")):
        return "date", source or "date"
    if storage not in {"double", "float", "int8", "int16", "int32"}:
        raise SavAdapterError(SAV_UNSUPPORTED_VALUE, f"Variable '{name}' has an unsupported type.")
    observed = [value for value in values if not _system_missing(value)]
    if observed and all(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value).is_integer()
        for value in observed
    ):
        return "integer", source or storage
    return "decimal", source or storage


def _scratch(data: bytes) -> tuple[None, io.BytesIO]:
    """Keep quarantined bytes in memory; pyreadstat supports a file-like object."""
    return None, io.BytesIO(data)


class _ResponseLimitExceeded(RuntimeError):
    pass


class _BoundedReader:
    def __init__(self, stream, limit: int) -> None:
        self.stream = stream
        self.limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        remaining = self.limit - self.total
        request = remaining + 1 if size < 0 else min(size, remaining + 1)
        chunk = self.stream.read(request)
        self.total += len(chunk)
        if self.total > self.limit:
            raise _ResponseLimitExceeded
        return chunk

    def readline(self, size: int = -1) -> bytes:
        remaining = self.limit - self.total
        request = remaining + 1 if size < 0 else min(size, remaining + 1)
        chunk = self.stream.readline(request)
        self.total += len(chunk)
        if self.total > self.limit:
            raise _ResponseLimitExceeded
        return chunk

    def readinto(self, buffer) -> int:
        chunk = self.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)


def _worker_exit_error(returncode: int | None) -> str:
    memory_codes = {-9, 137, -1073741801, -1073741670, 0xC0000017, 0xC000009A}
    return SAV_MEMORY_LIMIT if returncode in memory_codes else SAV_WORKER_ERROR


def _reap_worker(worker) -> None:
    try:
        worker.kill()
        worker.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SavAdapterError(
            SAV_WORKER_ERROR, "The isolated SAV worker could not be reaped."
        ) from exc


def _parse_sav_in_process(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_cells: int = DEFAULT_MAX_CELLS,
    max_string_chars: int = DEFAULT_MAX_STRING_CHARS,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    apply_user_missing: bool = True,
    column_types: dict[str, str] | None = None,
) -> ParseResult:
    """Read metadata first, prove every resource bound, then materialise once."""
    max_bytes = _bound(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)
    max_rows = _bound(max_rows, "max_rows", DEFAULT_MAX_ROWS)
    max_columns = _bound(max_columns, "max_columns", DEFAULT_MAX_COLUMNS)
    max_cells = _bound(max_cells, "max_cells", DEFAULT_MAX_CELLS)
    max_string_chars = _bound(max_string_chars, "max_string_chars", DEFAULT_MAX_STRING_CHARS)
    max_decompressed_bytes = _bound(
        max_decompressed_bytes, "max_decompressed_bytes", DEFAULT_MAX_DECOMPRESSED_BYTES
    )
    max_memory_bytes = _bound(max_memory_bytes, "max_memory_bytes", DEFAULT_MAX_MEMORY_BYTES)
    if (
        not isinstance(max_seconds, (int, float))
        or isinstance(max_seconds, bool)
        or not 0 < max_seconds <= DEFAULT_MAX_SECONDS
    ):
        raise SavAdapterError(
            "invalid_import_contract", "'max_seconds' is outside platform bounds."
        )
    if not isinstance(apply_user_missing, bool):
        raise SavAdapterError("invalid_import_contract", "'apply_user_missing' must be boolean.")
    if len(data) > max_bytes:
        raise SavAdapterError(SAV_TOO_LARGE, "The SAV payload exceeds the byte bound.")
    if column_types is not None:
        if not isinstance(column_types, dict) or any(
            not isinstance(name, str)
            or not name
            or declared not in {"integer", "decimal", "date", "boolean", "text"}
            for name, declared in column_types.items()
        ):
            raise SavAdapterError(
                "invalid_import_contract", "'column_types' carries an unsupported declaration."
            )
    if not looks_like_sav(data):
        raise SavAdapterError(SAV_CORRUPT, "The payload has no SAV signature.")
    try:
        import pyreadstat
    except ImportError as exc:
        raise SavAdapterError(SAV_UNAVAILABLE, "The SPSS reader is unavailable.") from exc

    stop = time.monotonic() + float(max_seconds)
    try:
        from core.inbound_scan import ScanRetryableError, _sav_dimensions

        header_rows, header_columns = _sav_dimensions(data, deadline=stop)
    except ScanRetryableError as exc:
        raise SavAdapterError(SAV_TIMEOUT, "The SAV header scan exceeded its deadline.") from exc
    if header_columns < 1:
        raise SavAdapterError(SAV_CORRUPT, "The SAV dictionary is structurally invalid.")
    if header_rows > max_rows:
        raise SavAdapterError(SAV_TOO_MANY_ROWS, "The SAV row bound is exceeded.")
    if header_columns > max_columns:
        raise SavAdapterError(SAV_TOO_MANY_COLUMNS, "The SAV column bound is exceeded.")
    if header_rows > 0 and header_rows * header_columns > max_cells:
        raise SavAdapterError(SAV_TOO_MANY_CELLS, "The SAV cell bound is exceeded.")

    directory, path = _scratch(data)
    primary: BaseException | None = None
    try:
        _deadline(stop)
        path.seek(0)
        try:
            _, meta = pyreadstat.read_sav(
                path,
                metadataonly=True,
                apply_value_formats=False,
                user_missing=True,
                output_format="dict",
            )
        except Exception as exc:
            raise SavAdapterError(
                SAV_CORRUPT, "The SAV metadata is corrupt or unsupported."
            ) from exc
        _deadline(stop)
        names = [
            _safe(str(name), max_string_chars)
            for name in (getattr(meta, "column_names", None) or [])
        ]
        if len(set(names)) != len(names):
            raise SavAdapterError(SAV_CORRUPT, "The SAV variable names are not unique.")
        rows_declared = int(getattr(meta, "number_rows", 0) or 0)
        columns_declared = int(getattr(meta, "number_columns", 0) or len(names))
        unknown_declared = set(column_types or {}) - set(names)
        if unknown_declared:
            raise SavAdapterError(
                "invalid_import_contract", "'column_types' names an absent SAV variable."
            )
        if not names or columns_declared != len(names):
            raise SavAdapterError(SAV_CORRUPT, "The SAV variable dictionary is inconsistent.")
        if rows_declared <= 0:
            raise SavAdapterError(SAV_EMPTY, "The SAV file carries no rows.")
        if rows_declared > max_rows:
            raise SavAdapterError(SAV_TOO_MANY_ROWS, "The SAV row bound is exceeded.")
        if columns_declared > max_columns:
            raise SavAdapterError(SAV_TOO_MANY_COLUMNS, "The SAV column bound is exceeded.")
        if rows_declared * columns_declared > max_cells:
            raise SavAdapterError(SAV_TOO_MANY_CELLS, "The SAV cell bound is exceeded.")
        widths = getattr(meta, "variable_storage_width", {}) or {}
        logical_bytes = rows_declared * sum(max(8, int(widths.get(name, 8) or 8)) for name in names)
        if logical_bytes > max_decompressed_bytes:
            raise SavAdapterError(
                SAV_DECOMPRESSION_LIMIT, "The SAV logical size exceeds the decompression bound."
            )
        cells = rows_declared * columns_declared
        estimated_memory = (
            len(data) * 3
            + logical_bytes * 3
            + cells * 96
            + rows_declared * 256
            + columns_declared * 4096
        )
        if estimated_memory > max_memory_bytes:
            raise SavAdapterError(SAV_MEMORY_LIMIT, "The SAV memory budget is exceeded.")
        max_metadata_bytes = max(
            1, min(16 * 1024 * 1024, max_decompressed_bytes, max_memory_bytes // 4)
        )
        variable_labels, value_labels, metadata_budget = _bounded_labels(
            meta, max_string_chars, max_metadata_bytes
        )
        missing_ranges: dict[str, list[dict[str, Any]]] = {}
        missing_entries = 0
        for name, ranges in (getattr(meta, "missing_ranges", {}) or {}).items():
            if not isinstance(ranges, (list, tuple)):
                raise SavAdapterError(SAV_METADATA_LIMIT, "SAV missing metadata is invalid.")
            safe_name = _bounded_metadata_text(name, max_string_chars, metadata_budget)
            safe_ranges: list[dict[str, Any]] = []
            for span in ranges:
                missing_entries += 1
                if missing_entries > 100_000 or not isinstance(span, dict):
                    raise SavAdapterError(
                        SAV_METADATA_LIMIT, "SAV missing metadata has too many entries."
                    )
                safe_span = {
                    "lo": _safe(span.get("lo"), max_string_chars),
                    "hi": _safe(span.get("hi"), max_string_chars),
                }
                metadata_budget[0] += len(
                    json.dumps(safe_span, default=str, separators=(",", ":")).encode("utf-8")
                )
                if metadata_budget[0] > max_metadata_bytes:
                    raise SavAdapterError(
                        SAV_METADATA_LIMIT, "SAV metadata exceeds its byte budget."
                    )
                safe_ranges.append(safe_span)
            missing_ranges[safe_name] = safe_ranges
        _deadline(stop)
        path.seek(0)
        try:
            raw, meta = pyreadstat.read_sav(
                path,
                apply_value_formats=False,
                user_missing=True,
                row_limit=max_rows + 1,
                output_format="dict",
            )
        except Exception as exc:
            raise SavAdapterError(SAV_CORRUPT, "The SAV data section is unreadable.") from exc
        _deadline(stop)
        observed_rows = len(next(iter(raw.values()), []))
        if observed_rows != rows_declared or observed_rows > max_rows:
            raise SavAdapterError(SAV_CORRUPT, "The SAV data and metadata row counts differ.")

        max_chars = max_string_chars
        declared_types = column_types or {}
        missing_evidence = {name: {"system": 0, "user": 0} for name in names}
        columns: dict[str, list[Any]] = {name: [] for name in names}
        rows: list[dict[str, Any]] = []
        for row_index in range(observed_rows):
            _deadline(stop)
            row: dict[str, Any] = {}
            for name in names:
                value = raw[name][row_index]
                if _system_missing(value):
                    missing_evidence[name]["system"] += 1
                    safe = None
                elif _user_missing(value, missing_ranges.get(name, [])):
                    missing_evidence[name]["user"] += 1
                    if apply_user_missing:
                        safe = None
                    else:
                        try:
                            coerced = (
                                _coerce_declared(value, declared_types[name])
                                if name in declared_types
                                else value
                            )
                        except (TypeError, ValueError, OverflowError) as exc:
                            raise SavAdapterError(
                                SAV_TYPE_MISMATCH, f"Variable '{name}' violates its declared type."
                            ) from exc
                        safe = _safe(coerced, max_chars)
                else:
                    try:
                        coerced = (
                            _coerce_declared(value, declared_types[name])
                            if name in declared_types
                            else value
                        )
                    except (TypeError, ValueError, OverflowError) as exc:
                        raise SavAdapterError(
                            SAV_TYPE_MISMATCH, f"Variable '{name}' violates its declared type."
                        ) from exc
                    safe = _safe(coerced, max_chars)
                row[name] = safe
                columns[name].append(safe)
            rows.append(row)

        specs = []
        for index, name in enumerate(names):
            detected, source = _metadata_type(name, meta, raw[name], declared_types)
            specs.append(
                ColumnSpec(
                    name=name,
                    index=index,
                    detected_type=detected,
                    null_count=sum(value is None for value in columns[name]),
                    sample_values=[value for value in columns[name] if value is not None][:3],
                    source_name=name,
                    source_index=index,
                    source_type=source,
                )
            )
        metadata = {
            "format": "sav",
            "file_encoding": _bounded_metadata_text(
                getattr(meta, "file_encoding", "") or "", max_chars, metadata_budget
            ),
            "file_format": _bounded_metadata_text(
                getattr(meta, "file_format", "") or "", max_chars, metadata_budget
            ),
            "variable_labels": variable_labels,
            "value_labels": value_labels,
            "missing_ranges": missing_ranges,
            "missing_evidence": missing_evidence,
            "user_missing_applied": apply_user_missing,
            "row_count_declared": rows_declared,
            "column_count_declared": columns_declared,
            "applied_decisions": {
                "value_labels_applied": False,
                "variable_labels_are_evidence": True,
                "user_missing_applied": apply_user_missing,
            },
            "budgets": {
                "max_bytes": max_bytes,
                "max_rows": max_rows,
                "max_columns": max_columns,
                "max_cells": max_cells,
                "max_string_chars": max_string_chars,
                "max_decompressed_bytes": max_decompressed_bytes,
                "max_memory_bytes": max_memory_bytes,
                "max_seconds": float(max_seconds),
            },
        }
        return _finalize_parse_result(
            ParseResult(
                rows=rows,
                rejected=[],
                columns=specs,
                encoding="sav",
                delimiter=None,
                sheet_name=None,
                detected_row_count=observed_rows,
                content_hash=hashlib.sha256(data).hexdigest(),
                metadata=metadata,
                source_format="sav",
            )
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        path.close()
        if directory is not None:
            try:
                shutil.rmtree(directory)
            except OSError as exc:
                if primary is None:
                    raise SavAdapterError(
                        SAV_SCRATCH_ERROR, "Secure SAV scratch cleanup failed."
                    ) from exc


def parse_sav(
    data: bytes,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_rows: int = DEFAULT_MAX_ROWS,
    max_columns: int = DEFAULT_MAX_COLUMNS,
    max_cells: int = DEFAULT_MAX_CELLS,
    max_string_chars: int = DEFAULT_MAX_STRING_CHARS,
    max_decompressed_bytes: int = DEFAULT_MAX_DECOMPRESSED_BYTES,
    max_memory_bytes: int = DEFAULT_MAX_MEMORY_BYTES,
    max_seconds: float = DEFAULT_MAX_SECONDS,
    apply_user_missing: bool = True,
    column_types: dict[str, str] | None = None,
) -> ParseResult:
    """Run native ReadStat code in a process that can be terminated at deadline."""
    if (
        not isinstance(max_seconds, (int, float))
        or isinstance(max_seconds, bool)
        or not 0 < max_seconds <= DEFAULT_MAX_SECONDS
    ):
        raise SavAdapterError(
            "invalid_import_contract", "'max_seconds' is outside platform bounds."
        )
    try:
        import pyreadstat  # noqa: F401
    except ImportError as exc:
        raise SavAdapterError(SAV_UNAVAILABLE, "The SPSS reader is unavailable.") from exc
    max_bytes = _bound(max_bytes, "max_bytes", DEFAULT_MAX_BYTES)
    max_rows = _bound(max_rows, "max_rows", DEFAULT_MAX_ROWS)
    max_columns = _bound(max_columns, "max_columns", DEFAULT_MAX_COLUMNS)
    max_cells = _bound(max_cells, "max_cells", DEFAULT_MAX_CELLS)
    max_string_chars = _bound(max_string_chars, "max_string_chars", DEFAULT_MAX_STRING_CHARS)
    max_decompressed_bytes = _bound(
        max_decompressed_bytes, "max_decompressed_bytes", DEFAULT_MAX_DECOMPRESSED_BYTES
    )
    max_memory_bytes = _bound(max_memory_bytes, "max_memory_bytes", DEFAULT_MAX_MEMORY_BYTES)
    if len(data) * 3 + 64 * 1024 > max_memory_bytes:
        raise SavAdapterError(SAV_MEMORY_LIMIT, "The SAV memory budget is exceeded.")
    if len(data) > max_bytes:
        raise SavAdapterError(SAV_TOO_LARGE, "The SAV payload exceeds the byte bound.")
    if not looks_like_sav(data):
        raise SavAdapterError(SAV_CORRUPT, "The payload has no SAV signature.")
    if not isinstance(apply_user_missing, bool):
        raise SavAdapterError("invalid_import_contract", "'apply_user_missing' must be boolean.")

    options = {
        "max_bytes": max_bytes,
        "max_rows": max_rows,
        "max_columns": max_columns,
        "max_cells": max_cells,
        "max_string_chars": max_string_chars,
        "max_decompressed_bytes": max_decompressed_bytes,
        "max_memory_bytes": max_memory_bytes,
        "max_seconds": max_seconds,
        "apply_user_missing": apply_user_missing,
        "column_types": column_types,
    }
    try:
        worker = subprocess.Popen(
            [sys.executable, "-m", "core.inbound_sav_worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise SavAdapterError(SAV_WORKER_ERROR, "The isolated SAV worker could not start.") from exc
    exchange: dict[str, Any] = {}

    def _exchange() -> None:
        try:
            if worker.stdin is None or worker.stdout is None:
                raise OSError("SAV worker pipes are unavailable.")
            pickle.dump((data, options), worker.stdin, protocol=pickle.HIGHEST_PROTOCOL)
            worker.stdin.close()
            reader = _BoundedReader(worker.stdout, max_memory_bytes)
            exchange["payload"] = pickle.Unpickler(reader).load()
        except BaseException as exc:
            exchange["error"] = exc
        finally:
            if worker.stdout is not None:
                worker.stdout.close()

    exchange_thread = threading.Thread(target=_exchange, daemon=True)
    exchange_thread.start()
    exchange_thread.join(timeout=float(max_seconds))
    if exchange_thread.is_alive():
        _reap_worker(worker)
        exchange_thread.join(timeout=1.0)
        raise SavAdapterError(SAV_TIMEOUT, "The isolated SAV parser exceeded its deadline.")
    try:
        returncode = worker.wait(timeout=1.0)
    except subprocess.TimeoutExpired as exc:
        _reap_worker(worker)
        raise SavAdapterError(
            SAV_WORKER_ERROR, "The SAV worker did not terminate after returning evidence."
        ) from exc
    if returncode != 0:
        raise SavAdapterError(
            _worker_exit_error(returncode),
            "The isolated SAV worker exited after returning incomplete evidence.",
        )
    failure = exchange.get("error")
    if isinstance(failure, _ResponseLimitExceeded):
        raise SavAdapterError(SAV_MEMORY_LIMIT, "The SAV worker response exceeds its bound.")
    if failure is not None:
        code = _worker_exit_error(returncode) if returncode else SAV_WORKER_ERROR
        raise SavAdapterError(code, "The isolated SAV worker failed.") from failure
    payload = exchange.get("payload")
    if not isinstance(payload, tuple) or not payload:
        raise SavAdapterError(SAV_WORKER_ERROR, "The SAV worker returned invalid evidence.")
    if payload[0] == "error":
        if len(payload) != 3 or not all(isinstance(item, str) for item in payload[1:]):
            raise SavAdapterError(SAV_WORKER_ERROR, "The SAV worker error is malformed.")
        raise SavAdapterError(payload[1], payload[2])
    if payload[0] != "ok" or len(payload) != 2 or not isinstance(payload[1], ParseResult):
        raise SavAdapterError(SAV_WORKER_ERROR, "The SAV worker result is malformed.")
    return payload[1]


def sav_producer(**options: Any):
    return lambda data: parse_sav(data, **options)
