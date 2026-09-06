"""toorow -- CSV/Excel governed import parser + ledger orchestration (Story 12.9).

Turns a raw CSV or Excel upload into a validated, versioned candidate dataset that
flows through the 12.8 immutable import ledger and the 12.5 atomic publication gates.

Architecture boundaries enforced HARD across this chain:
  AD-2  : source-agnostic. The feed format (csv/excel) is resolved once at the
           function boundary and never branched on inside the domain logic. No
           provider name, no connector slug, no import from server/modules/*.
  AD-8  : dbt is the ONLY writer of analytical marts. This chain:
             (a) writes NOTHING to a mart; the raw landing is project-scoped
                 ``managed_feed_<datastream_id>`` opened via the 12.8 ledger seam;
             (b) delegates publication to ``core.datastream_publication`` via the
                 12.8 ``open_import`` / ``record_rows`` / ``evaluate_rejection_gate``
                 / ``mark_outcome`` contract -- it never re-implements ledger semantics.
  AD-9  : null is NEVER coerced to 0; rejected rows are honest per-row evidence; a
           zero-row parse is an explicit outcome, not a silent empty list.
  NFR14 : the ledger row + isolated 12.5 candidate exist BEFORE any row is written;
           a parse failure leaves no published mutation.
  NFR15 : replace is the safe default; append is blocked until a stable-key contract
           exists (12.9 scope: Replace only).

The chain is structured in two clean layers:
  1. PARSE layer (pure Python, no I/O, unit-testable on in-memory bytes):
       ``detect_format``, ``validate_upload_meta``, ``parse_csv``, ``parse_excel``,
       ``build_preview``, ``validate_import_contract``.
  2. ORCHESTRATE layer (consumes 12.8 public contract, pg-gated):
       ``run_import`` -- drives parse -> open_import -> record_rows -> gate.
       ``version_contract`` -- idempotent insert/dedup into
       ``app.csv_excel_import_contracts`` (AC2) so import configs are reusable.

Per-row rejection (AC3, C2): the parse layer emits ``RejectedRow`` evidence for
rows that fail the contract (date-format mismatch, non-coercible values in a
confirmed-typed column, short/over-long rows). ``run_import`` forwards that evidence
to ``record_rows`` and the UNMOCKED 12.8 rejection gate blocks above the governed
threshold.

The parsing contract is VERSIONED (``csv_excel_import_contracts``) via migration 078
so import configurations are reusable and reviewable (AC2). ``version_contract``
computes a stable ``fingerprint`` and threads ``import_contract_id`` onto the ledger
row so an import is traceable to the exact config version used.

Empty-file / zero-row policy (AC4):
  ``run_import`` returns ``{"blocked": True, "reason": "empty_import"}`` by default.
  An intentional empty publish requires BOTH:
    * ``force_empty_publish=True`` in the call, AND
    * the project preference ``allow_empty_publication=True``.
  Absent both, the ledger is NOT opened and the prior published state is untouched.
  The ``build_preview`` function always reports ``row_count=0`` honestly.

Append unavailability (AC5):
  Calling ``run_import`` with ``write_mode='append'`` raises ``AppendUnavailable``.
  The error carries a ``repair`` explanation that Replace is the safe supported mode.
  No append path exists in this story (deferred to a keyed-dedup contract).

Publication honesty:
  A passing rejection gate leaves the ledger at ``written``; ``run_import`` returns
  ``{"outcome": "written_pending_publication", "published": False, ...}``. This
  module NEVER fabricates a publish.

  The physical row write IS real now, through ``raw_landing.land_raw_rows`` --
  the same seam the connectors use, so it reaches BigQuery as well as DuckDB and
  honours an active ``candidate_execution`` isolation. This paragraph used to
  list that write among the "HONEST Phase B" items, which described a path that
  counted its accepted rows, recorded its per-row rejections, passed its gates
  and landed NOTHING. The counts were true and the data absent; that reads as a
  success, which is why it survived. `landed_row_count` and `landing` in the
  result now say what actually reached the warehouse, separately from what the
  parse accepted.

  Still genuinely deferred: the full 12.5 DQ gates and the pointer-swap
  ``commit_publication``.

WHERE THE CODE IS. This module was 4 357 lines; it is now the stable SEAM
and nothing else. The code lives in the modules it re-exports below, in
dependency order: ``tabular_types`` (vocabulary, errors, shapes),
``tabular_parsing`` and ``xlsx_parsing`` (the pure parse layer),
``file_source_resolution`` (which Template and mapping an arrival replays),
``import_landing`` (governed mapping + the physical write),
``import_contract`` (validate, version, parse-under-contract),
``import_preview`` (both previews) and ``import_runner`` (``run_import``).
Importing from here and importing from the owning module reach the same
objects; a test that PATCHES one of them must target the owning module.
"""

from __future__ import annotations

from core.file_source_resolution import (  # noqa: F401
    _CONFIRMED_BINDING_STATES,
    _FILE_SOURCE_TEMPLATE_REF,
    CONFIRMATION_CONFIRMED,
    CONFIRMATION_MAPPING_MOVED,
    CONFIRMATION_NEVER,
    CONFIRMATION_TEMPLATE_MOVED,
    FileSourceProducer,
    _as_dict,
    _catalog_template_producer,
    _confirmed_mapping,
    resolve_file_source_producer,
)
from core.import_contract import (  # noqa: F401
    _mint_contract_id,
    contract_fingerprint,
    parse_for_contract,
    validate_import_contract,
    version_contract,
)
from core.import_landing import (  # noqa: F401
    _MAPPING_TYPE_ALIASES,
    _WAREHOUSE_TYPES,
    _apply_governed_mapping,
    _candidate_content_fingerprint,
    _canonical_fingerprint,
    _land_managed_rows,
    _parse_evidence,
)
from core.import_preview import (  # noqa: F401
    _classification_report,
    _coercion_report,
    _row_validation_report,
    _unit_report,
    _vocabulary_report,
    build_file_source_preview,
    build_preview,
)
from core.import_runner import (  # noqa: F401
    OUTCOME_WRITTEN_PENDING_PUBLICATION,
    run_import,
)
from core.tabular_parsing import (  # noqa: F401
    _CSV_EXTENSIONS,
    _DATE_SHAPE,
    _EXCEL_EXTENSIONS,
    _SAV_EXTENSIONS,
    _THOUSANDS_GROUPED,
    _XLS_MAGIC,
    _XLSX_MAGIC,
    _candidate_ambiguities,
    _classify_value,
    _coerce_decimal,
    _coerce_integer,
    _coerce_typed,
    _content_hash,
    _detect_delimiter,
    _detect_encoding,
    _finalize_parse_result,
    _infer_column_type,
    _is_coercible,
    _locale_numeric_sample,
    _looks_like_thousands_grouped,
    _matching_date_formats,
    _normalise_encoding_name,
    _parse_date_value,
    _representative_samples,
    _resolve_column_type,
    _resolved_date_format,
    _schema_fingerprint,
    _strip_bom,
    _strip_ws,
    _truncate_value,
    _validate_decoded_text,
    _validate_rows,
    detect_format,
    parse_csv,
    validate_upload_meta,
)
from core.tabular_types import (  # noqa: F401
    _DELIMITERS,
    _ENCODINGS,
    FORMAT_CSV,
    FORMAT_EXCEL,
    FORMAT_SAV,
    MAX_COLUMNS,
    MAX_FIELD_CHARS,
    MAX_FILE_BYTES,
    MAX_ROWS,
    MAX_SHEET_COLUMNS,
    MAX_SHEET_ROWS,
    MAX_XLSX_COMPRESSION_RATIO,
    MAX_XLSX_UNCOMPRESSED_BYTES,
    MAX_XLSX_XML_BYTES,
    PREVIEW_ROW_LIMIT,
    RULE_DATE_FORMAT,
    RULE_LONG_ROW,
    RULE_SHORT_ROW,
    RULE_TYPE_MISMATCH,
    SAMPLE_CAP,
    SUPPORTED_COLUMN_TYPES,
    SUPPORTED_DATE_FORMATS,
    SUPPORTED_FORMATS,
    SUPPORTED_LOCALES,
    WRITE_MODE_APPEND,
    WRITE_MODE_REPLACE,
    AppendUnavailable,
    ColumnSpec,
    CsvExcelImportError,
    DuplicateColumns,
    EmptyFile,
    EmptyImportBlocked,
    EncodingError,
    FileTooLarge,
    FormulaInCells,
    ImportPreview,
    InvalidImportContract,
    NoHeaderRow,
    ParseResult,
    ParserIssue,
    ParserReviewRequired,
    RejectedRow,
    SheetNotFound,
    UnsupportedFileType,
)
from core.xlsx_parsing import (  # noqa: F401
    _inspect_xlsx_package,
    _load_workbook_safe,
    _xlsx_bounds,
    parse_excel,
)
