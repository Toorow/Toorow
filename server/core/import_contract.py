"""toorow -- the versioned parsing contract: validation, identity, parse.

The import contract is the reusable, auditable JSON document that governs how a
recurring CSV/Excel upload is parsed. Its three moments:

  * ``validate_import_contract`` -- normalise, and reject a malformed contract;
  * ``contract_fingerprint`` / ``version_contract`` -- stable identity and the
    idempotent insert into ``app.csv_excel_import_contracts`` (migration 078);
  * ``parse_for_contract`` -- bytes into a ``ParseResult`` under the contract
    that governs them, whether that is a CSV/Excel contract or an Epic 22
    file-source Template carried by a producer.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Callable

from ulid import ULID

from core.file_source_resolution import (
    FileSourceProducer,
)
from core.tabular_parsing import (
    _normalise_encoding_name,
    detect_format,
    parse_csv,
)
from core.tabular_types import (
    _DELIMITERS,
    FORMAT_CSV,
    FORMAT_EXCEL,
    FORMAT_SAV,
    MAX_COLUMNS,
    MAX_FIELD_CHARS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    SUPPORTED_COLUMN_TYPES,
    SUPPORTED_DATE_FORMATS,
    SUPPORTED_FORMATS,
    SUPPORTED_LOCALES,
    WRITE_MODE_APPEND,
    WRITE_MODE_REPLACE,
    AppendUnavailable,
    CsvExcelImportError,
    InvalidImportContract,
    ParseResult,
)
from core.xlsx_parsing import (
    parse_excel,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Import contract validation (pure).
# ---------------------------------------------------------------------------

# The import contract is a versioned JSON document that governs how a recurring
# CSV/Excel upload is parsed.  It is STORED in ``app.csv_excel_import_contracts``
# (migration 078) so the same configuration is reusable and auditable across runs.
#
# Minimal contract shape:
#   {
#     "format": "csv" | "excel",
#     "delimiter": "," | ";" | "\t" | "|" | null,  -- CSV only; null = auto-detect
#     "encoding": "utf-8" | ...,                    -- CSV only; null = auto-detect
#     "sheet_name": "Sheet1" | null,                -- Excel only
#     "header_row": 1,                              -- 1-based (default 1)
#     "date_format": "%Y-%m-%d" | null,             -- null = auto-detect
#     "locale": "en" | "fr" | null,
#     "formulas_as_values": true,                   -- Excel only (default true)
#     "write_mode": "replace",                      -- only "replace" in 12.9
#     "column_types": {                             -- optional; declared type per column
#         "id": "text", "spend": "decimal", "day": "date"   -- overrides inference
#     }
#   }


def validate_import_contract(contract: dict[str, Any]) -> dict[str, Any]:
    """Validate and normalise a parsing/import contract dict.

    Returns a normalised copy on success.
    Raises ``InvalidImportContract`` (-> 422) on structural / unsupported-value errors.
    """
    if not isinstance(contract, dict):
        raise InvalidImportContract("Contract must be a JSON object.")

    # format.
    fmt = contract.get("format")
    if fmt not in SUPPORTED_FORMATS:
        raise InvalidImportContract(
            f"'format' must be one of {sorted(SUPPORTED_FORMATS)}; got {fmt!r}."
        )

    # write_mode (only replace in 12.9).
    wm = contract.get("write_mode", WRITE_MODE_REPLACE)
    if wm == WRITE_MODE_APPEND:
        raise AppendUnavailable()
    if wm != WRITE_MODE_REPLACE:
        raise InvalidImportContract(
            f"'write_mode' must be 'replace'; got {wm!r}. "
            "Append mode is not available (no stable key contract)."
        )

    # header_row.
    hr = contract.get("header_row", 1)
    if not isinstance(hr, int) or isinstance(hr, bool) or hr < 1:
        raise InvalidImportContract("'header_row' must be a positive integer >= 1.")

    # delimiter (CSV only; None = auto).
    delim = contract.get("delimiter", None)
    if delim is not None and delim not in _DELIMITERS:
        raise InvalidImportContract(
            f"'delimiter' must be one of {_DELIMITERS} or null; got {delim!r}."
        )

    # date_format (optional; None = auto).
    df = contract.get("date_format", None)
    if df is not None and df not in SUPPORTED_DATE_FORMATS:
        raise InvalidImportContract(
            f"'date_format' {df!r} is not in the supported set: {sorted(SUPPORTED_DATE_FORMATS)}."
        )

    # locale (optional).
    loc = contract.get("locale", None)
    if loc is not None and loc not in SUPPORTED_LOCALES:
        raise InvalidImportContract(
            f"'locale' must be one of {sorted(SUPPORTED_LOCALES)} or null; got {loc!r}."
        )

    # sheet_name (Excel only; string or None).
    sn = contract.get("sheet_name", None)
    if sn is not None and not isinstance(sn, str):
        raise InvalidImportContract("'sheet_name' must be a string or null.")

    # formulas_as_values (Excel only; bool, default true).
    fav = contract.get("formulas_as_values", True)
    if not isinstance(fav, bool):
        raise InvalidImportContract("'formulas_as_values' must be a boolean.")

    # column_types (optional): a DECLARED per-column type map {name: type}. A declared
    # type is authoritative over the leading-value inference (kills the ID/YYYYMMDD
    # false-rejects). Sorted into a plain dict so the contract fingerprint is stable.
    ct = contract.get("column_types", None)
    column_types: dict[str, str] = {}
    if ct is not None:
        if not isinstance(ct, dict):
            raise InvalidImportContract(
                "'column_types' must be an object mapping column name -> type, or null."
            )
        for cname, ctype in ct.items():
            if not isinstance(cname, str) or not cname.strip():
                raise InvalidImportContract("'column_types' keys must be non-empty column names.")
            if ctype not in SUPPORTED_COLUMN_TYPES:
                raise InvalidImportContract(
                    f"'column_types[{cname}]' must be one of "
                    f"{sorted(SUPPORTED_COLUMN_TYPES)}; got {ctype!r}."
                )
            column_types[cname] = ctype

    encoding = contract.get("encoding")
    if encoding is not None:
        if not isinstance(encoding, str):
            raise InvalidImportContract("'encoding' must be a supported encoding name or null.")
        encoding = _normalise_encoding_name(encoding)
        if encoding not in {
            "utf-8",
            "utf-8-sig",
            "utf-16",
            "utf-16-le",
            "utf-16-be",
            "cp1252",
            "iso-8859-1",
        }:
            raise InvalidImportContract(f"Unsupported encoding {encoding!r}.")
    cell_range = contract.get("cell_range")
    if cell_range is not None and (not isinstance(cell_range, str) or not cell_range.strip()):
        raise InvalidImportContract("'cell_range' must be a non-empty A1 range or null.")
    bounds: dict[str, int] = {}
    for key, default in (
        ("max_rows", MAX_ROWS),
        ("max_columns", MAX_COLUMNS),
        ("max_field_chars", MAX_FIELD_CHARS),
    ):
        value = contract.get(key, default)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1 or value > default:
            raise InvalidImportContract(
                f"'{key}' must be a positive integer no greater than {default}."
            )
        bounds[key] = value
    if hr > bounds["max_rows"] + 1:
        raise InvalidImportContract("'header_row' must not exceed 'max_rows' + 1.")

    sav_options: dict[str, Any] = {}
    if fmt == FORMAT_SAV:
        from core.inbound_sav import (
            DEFAULT_MAX_BYTES as SAV_MAX_BYTES,
        )
        from core.inbound_sav import (
            DEFAULT_MAX_CELLS as SAV_MAX_CELLS,
        )
        from core.inbound_sav import (
            DEFAULT_MAX_DECOMPRESSED_BYTES as SAV_MAX_DECOMPRESSED,
        )
        from core.inbound_sav import (
            DEFAULT_MAX_MEMORY_BYTES as SAV_MAX_MEMORY,
        )
        from core.inbound_sav import (
            DEFAULT_MAX_SECONDS as SAV_MAX_SECONDS,
        )
        from core.inbound_sav import (
            DEFAULT_MAX_STRING_CHARS as SAV_MAX_STRING_CHARS,
        )

        if delim is not None or encoding is not None or sn is not None or cell_range is not None:
            raise InvalidImportContract(
                "SAV contracts cannot declare CSV encoding/delimiter or Excel sheet/range settings."
            )
        apply_user_missing = contract.get("apply_user_missing", True)
        if not isinstance(apply_user_missing, bool):
            raise InvalidImportContract("'apply_user_missing' must be a boolean.")
        sav_limits: dict[str, Any] = {}
        for key, ceiling in (
            ("max_bytes", SAV_MAX_BYTES),
            ("max_cells", SAV_MAX_CELLS),
            ("max_string_chars", SAV_MAX_STRING_CHARS),
            ("max_decompressed_bytes", SAV_MAX_DECOMPRESSED),
            ("max_memory_bytes", SAV_MAX_MEMORY),
        ):
            value = contract.get(key, ceiling)
            if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= ceiling:
                raise InvalidImportContract(
                    f"'{key}' must be a positive integer no greater than {ceiling}."
                )
            sav_limits[key] = value
        max_seconds = contract.get("max_seconds", SAV_MAX_SECONDS)
        if (
            not isinstance(max_seconds, (int, float))
            or isinstance(max_seconds, bool)
            or not 0 < max_seconds <= SAV_MAX_SECONDS
        ):
            raise InvalidImportContract("'max_seconds' is outside platform bounds.")
        sav_options = {
            "apply_user_missing": apply_user_missing,
            "max_seconds": float(max_seconds),
            **sav_limits,
        }

    return {
        "contract_version": "csv-excel-parse-v1",
        "format": fmt,
        "write_mode": WRITE_MODE_REPLACE,
        "header_row": hr,
        "delimiter": delim,
        "encoding": encoding,
        "date_format": df,
        "locale": loc,
        "sheet_name": sn,
        "cell_range": cell_range,
        "formulas_as_values": fav,
        "column_types": column_types,
        **bounds,
        **sav_options,
    }


def contract_fingerprint(normalised_contract: dict[str, Any]) -> str:
    """Stable SHA-256 fingerprint over the NORMALISED parsing contract (AC2).

    Keys are sorted so the fingerprint is invariant to dict ordering. The same config
    -> the same fingerprint -> de-duplication against the DB UNIQUE index on
    ``(datastream_id, project_id, fingerprint)``.
    """
    canonical = json.dumps(normalised_contract, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Contract versioning (AC2) -- idempotent insert/dedup into the 078 table.
# ---------------------------------------------------------------------------


def _mint_contract_id() -> str:
    return f"cic_{ULID()}"


def version_contract(
    contract: dict[str, Any],
    *,
    datastream_id: str,
    project_id: str,
    actor: str,
    conn,
    label: str | None = None,
) -> str:
    """Persist a versioned parsing contract; return its ``cic_<ULID>`` id (AC2).

    Computes a stable ``fingerprint`` over the NORMALISED contract and performs an
    idempotent insert:
      * a NEW config (unseen fingerprint for this datastream+project) -> a new
        ``cic_<ULID>`` row;
      * an IDENTICAL config (same fingerprint) -> the EXISTING row id is returned
        (``ON CONFLICT (datastream_id, project_id, fingerprint) DO NOTHING`` + re-read),
        so an unchanged contract is never re-versioned.

    The caller owns the transaction (does NOT commit here) so the contract row and the
    ledger row that references it commit together. Immutability of the identity fields
    is enforced by the 078 trigger.
    """
    norm = validate_import_contract(contract)
    fingerprint = contract_fingerprint(norm)
    fmt = norm["format"]
    write_mode = norm["write_mode"]

    with conn.cursor() as cur:
        # Fast path: an identical contract already exists -> return it (dedup).
        cur.execute(
            """
            SELECT id FROM app.csv_excel_import_contracts
            WHERE datastream_id = %s AND project_id = %s AND fingerprint = %s
            """,
            (datastream_id, project_id, fingerprint),
        )
        row = cur.fetchone()
        if row is not None:
            cur.execute(
                """
                INSERT INTO app.csv_excel_import_contract_confirmations
                    (contract_id, datastream_id, project_id, fingerprint, confirmed_by)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (contract_id) DO NOTHING
                """,
                (row[0], datastream_id, project_id, fingerprint, actor),
            )
            return row[0]

        contract_id = _mint_contract_id()
        cur.execute(
            """
            INSERT INTO app.csv_excel_import_contracts
                (id, datastream_id, project_id, fingerprint, format, write_mode,
                 contract, created_by, label)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            ON CONFLICT (datastream_id, project_id, fingerprint) DO NOTHING
            """,
            (
                contract_id,
                datastream_id,
                project_id,
                fingerprint,
                fmt,
                write_mode,
                json.dumps(norm),
                actor,
                label,
            ),
        )
        # Re-read: our insert may have been a no-op if a concurrent writer won the
        # fingerprint race -- return whichever id is now persisted (dedup guarantee).
        cur.execute(
            """
            SELECT id FROM app.csv_excel_import_contracts
            WHERE datastream_id = %s AND project_id = %s AND fingerprint = %s
            """,
            (datastream_id, project_id, fingerprint),
        )
        row = cur.fetchone()
        if row is None:  # pragma: no cover - the insert above just ran.
            raise CsvExcelImportError(
                "contract_version_failed",
                "contract row not found after insert (unexpected)",
            )
        cur.execute(
            """
            INSERT INTO app.csv_excel_import_contract_confirmations
                (contract_id, datastream_id, project_id, fingerprint, confirmed_by)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (contract_id) DO NOTHING
            """,
            (row[0], datastream_id, project_id, fingerprint, actor),
        )
        return row[0]


# ---------------------------------------------------------------------------
# Bytes under contract -> ParseResult.
# ---------------------------------------------------------------------------


def parse_for_contract(
    data: bytes,
    *,
    contract: dict[str, Any],
    source_metadata: dict[str, Any],
    mapping_version_id: str,
    producer: Callable[[bytes], ParseResult] | None = None,
) -> tuple[ParseResult, str]:
    """Parse ``data`` under the contract that governs it; return (result, format).

    Two contracts, one seam. Without a ``producer`` the CSV/Excel contract is
    normalised, re-checked against the ACTUAL bytes (a contract labelled csv over
    xlsx bytes is an error, never a parse on binary) and dispatched to the
    matching parser. With a ``FileSourceProducer`` the Epic 22 Template IS the
    contract: its human confirmation and its source drift are checked first --
    ``mapping_version_id`` is the pinned version that confirmation must match --
    then the producer owns parsing and the source->canonical mapping (AD-1).

    Raises ``InvalidImportContract`` on a contract/bytes mismatch, and
    ``CsvExcelImportError`` on missing confirmation or detected drift.
    """
    if producer is not None:
        if isinstance(producer, FileSourceProducer) and not producer.confirmed_for(
            mapping_version_id
        ):
            raise CsvExcelImportError(
                "file_source_confirmation_required",
                "the bound Template and mapping version do not carry matching "
                "human confirmation evidence",
                repair={
                    "preview_and_confirm": True,
                    "template_id": producer.template_id,
                },
            )
        if isinstance(producer, FileSourceProducer):
            contract_payload = producer.template.get("contract") or {}
            if contract_payload.get("kind") != "adaptation":
                from core.file_source_producer import detect_source_drift  # noqa: PLC0415
                from core.import_preview import build_preview  # noqa: PLC0415

                raw_preview = build_preview(data, filename=source_metadata.get("filename"))
                drift = detect_source_drift(
                    producer.template,
                    producer.mapping or None,
                    [column.name for column in raw_preview.columns],
                )
                if drift.get("status") == "needs_revalidation":
                    # THE REFUSAL NAMES THE COLUMNS (AI-321, 2026-08-29): the
                    # sentence said "required source columns disappeared" and
                    # not which -- `file-source-ingestion.md` wants the
                    # disappeared column named, and it was in `drift` all along.
                    gone = [str(c) for c in (drift.get("missing_required_sources") or [])]
                    named = (": " + ", ".join(gone)) if gone else ""
                    raise CsvExcelImportError(
                        "file_source_drift",
                        "required source columns disappeared" + named + "; preview and reconfirm",
                        repair={"preview_and_confirm": True, "drift": drift},
                    )
        # File-source producer path (AD-1): the producer owns parsing + the
        # source->canonical mapping. feed_format stays a valid ledger enum
        # (csv/excel); the file-source template is the versioned contract.
        result = producer(data)
        actual_producer_format = result.source_format or source_metadata.get("format")
        fmt = (
            FORMAT_CSV
            if actual_producer_format in {"csv", "tsv"}
            else FORMAT_SAV
            if actual_producer_format == "sav"
            else FORMAT_EXCEL
        )
    else:
        # Normalise and validate the contract.
        norm = validate_import_contract(contract)
        fmt = norm["format"]

        # H2: re-detect the actual format from the bytes and assert it matches the
        # contract (mirrors build_preview). A contract labelled csv over xlsx bytes
        # must NOT run parse_csv on binary -- fail with the invalid-contract error.
        detected = detect_format(source_metadata.get("filename"), data)
        if detected != fmt:
            raise InvalidImportContract(
                f"Contract format '{fmt}' does not match the actual file format "
                f"'{detected}' (detected from content/extension)."
            )

        # Parse (re-parse after the preview confirmation -- bytes are cheap).
        if fmt == FORMAT_CSV:
            result = parse_csv(
                data,
                delimiter=norm.get("delimiter"),
                encoding=norm.get("encoding"),
                date_format=norm.get("date_format"),
                locale=norm.get("locale"),
                header_row=norm.get("header_row", 1),
                max_rows=norm.get("max_rows", MAX_ROWS),
                max_columns=norm.get("max_columns", MAX_COLUMNS),
                max_field_chars=norm.get("max_field_chars", MAX_FIELD_CHARS),
                column_types=norm.get("column_types"),
            )
        elif fmt == FORMAT_EXCEL:
            result = parse_excel(
                data,
                sheet_name=norm.get("sheet_name"),
                cell_range=norm.get("cell_range"),
                header_row=norm.get("header_row", 1),
                date_format=norm.get("date_format"),
                locale=norm.get("locale"),
                formulas_as_values=norm.get("formulas_as_values", True),
                max_rows=norm.get("max_rows", MAX_SHEET_ROWS),
                max_columns=norm.get("max_columns", MAX_SHEET_COLUMNS),
                max_field_chars=norm.get("max_field_chars", MAX_FIELD_CHARS),
                column_types=norm.get("column_types"),
            )
        else:
            from core.inbound_sav import parse_sav

            result = parse_sav(
                data,
                max_bytes=norm.get("max_bytes", MAX_FILE_BYTES),
                max_rows=norm.get("max_rows", MAX_ROWS),
                max_columns=norm.get("max_columns", MAX_COLUMNS),
                max_cells=norm.get("max_cells", 5_000_000),
                max_string_chars=norm.get("max_string_chars", MAX_FIELD_CHARS),
                max_decompressed_bytes=norm.get("max_decompressed_bytes", 256 * 1024 * 1024),
                max_memory_bytes=norm.get("max_memory_bytes", 256 * 1024 * 1024),
                max_seconds=norm.get("max_seconds", 10.0),
                apply_user_missing=norm.get("apply_user_missing", True),
                column_types=norm.get("column_types"),
            )
    return result, fmt
