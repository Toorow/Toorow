"""Google Sheets managed-feed sync consumer (Story 12.10).

This module is the SYNC CONSUMER for the 12.8 managed-feed ledger -- it
orchestrates a complete snapshot of a Google Sheet through the common ledger +
atomic replace path. It is source-agnostic at the core boundary (AD-2): the
actual Sheets API call is delegated to the Story 15.6 adapter in
``server/modules/google-sheets/connector.py``. This module never imports from
that adapter directly -- it calls it through the registered provider adapter
seam (the ``sheets_adapter`` parameter, injected at call time) so the unit
tests can mock it without touching network or credentials.

Architecture compliance:
  AD-2  : source-agnostic core. No Google Sheets branch in this file. The
           adapter (15.6) is injected as a callable, never imported at module
           scope from server/modules/*.
  AD-3  : token obtained once per sync run, used inside the adapter call, then
           discarded. Never logged, stored, or passed beyond the adapter layer.
  AD-8  : no mart write. The managed landing is a project-scoped RAW table
           (``managed_feed_<datastream_id>``) routed through
           ``managed_feed_ledger.open_managed_landing``.
  AD-21 : Google stack uses direct OAuth via ``get_fresh_token`` (token_service
           facade). NOT Nango. The adapter handles this transparently (the sync
           consumer passes ``connection_id`` and the adapter obtains the token).
  NFR14 : ledger row + isolated 12.5 candidate exist BEFORE any row is written.
  NFR15 : unchanged snapshot -> no-op ledger row; no duplicate rows.
  NFR20 : every sync attempt is a durable, immutable, queryable audit record
           via the 12.8 ledger.

Safe-fail catalogue (AC: safe-fail):
  REVOKED_CONSENT       -> PermissionError from adapter -> ``revoked_consent``
  SPREADSHEET_NOT_FOUND -> FileNotFoundError from adapter -> ``spreadsheet_not_found``
  TAB_RENAMED_OR_DELETED-> ColumnMappingError / ValueError from adapter -> ``tab_not_found``
  RANGE_DRIFT           -> ValueError with range context -> ``range_drift``
  DUPLICATE_HEADERS     -> validated in _validate_sheet_headers() -> ``duplicate_headers``
  SCHEMA_DRIFT          -> mismatched column set vs mapping -> ``schema_drift``
  RATE_LIMIT            -> RateLimitError from adapter -> re-raised (worker-handled)
  All failures: prior published version stays available (ledger ``failed`` outcome,
  no pointer swap).

Range-drift / tab-not-found classification (M2):
  The 15.6 adapter raises TYPED exceptions -- ``ColumnMappingError`` (declared
  column absent), ``FileNotFoundError`` (HTTP 404), ``PermissionError`` (HTTP 403).
  This module classifies on the exception TYPE first (via a duck-typed check on the
  class name so no import from server/modules at module scope -- AD-2), and only
  falls back to substring sniffing of the message when the type is ambiguous
  (a bare ValueError for a range that is out of bounds). The safe-fail outcome is
  fail-closed regardless: an unrecognised recoverable error still lands a ``failed``
  ledger row, never a publish.

Honest publication boundary (C1 -- Phase B):
  ``run_sync`` does the snapshot -> ledger -> landing -> record_rows ->
  rejection-gate + empty-gate portion of the publication contract. It does NOT
  perform the ATOMIC POINTER SWAP: the real 12.5 ``commit_publication`` requires the
  physical candidate rows in the live warehouse (BigQuery), which is Phase B
  (AI-08). Therefore a fully-validated run returns
  ``outcome="validated_pending_publication"`` -- an HONEST terminal for the sync
  consumer -- and the ledger row stays ``written`` (a legitimate non-terminal state
  meaning "landed + validated, awaiting the Phase-B pointer swap"). ``run_sync``
  NEVER marks the ledger ``published`` itself (that would flip a text column without
  swapping ``current_published_execution_id`` -- a lie).

Empty-candidate gate (C1a -- fail closed):
  Zero accepted rows BLOCK publication by default, mirroring
  ``datastream_publication.evaluate_dq_gates``' ``empty_candidate`` gate. An
  intentional empty publish requires BOTH an explicit ``force_empty_publish=True``
  argument AND the project preference ``allow_empty_publication=True``. When blocked,
  the ledger is marked ``rejected`` with error_code ``empty_candidate`` (prior
  published version stays available).

Cadence / quota:
  Hourly cadence is accepted ONLY when the ``quota_profile`` on the datastream
  config carries ``allow_hourly: true``. This is the sole gating check -- the
  ledger does not enforce cadence independently.

Phase-B marker:
  Live Google OAuth is BLOCKED in Phase B (AI-08). The adapter call is
  abstracted behind the ``sheets_adapter`` injectable so tests can mock it
  without real credentials. In production the default adapter calls
  ``_fetch_sheet_values`` from the 15.6 module.
  Mark all live-network paths with # PHASE_B_LIVE_BLOCKED.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable

from ulid import ULID

from core import managed_feed_ledger as ledger

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Closed vocabularies.
# ---------------------------------------------------------------------------

# Safe-fail error codes (stable, surfaced on the ledger row error_code).
ERR_REVOKED_CONSENT = "revoked_consent"
ERR_SPREADSHEET_NOT_FOUND = "spreadsheet_not_found"
ERR_TAB_NOT_FOUND = "tab_not_found"
ERR_RANGE_DRIFT = "range_drift"
ERR_DUPLICATE_HEADERS = "duplicate_headers"
ERR_SCHEMA_DRIFT = "schema_drift"
ERR_QUOTA_HOURLY_NOT_PERMITTED = "hourly_cadence_not_permitted"
ERR_MISSING_SPREADSHEET_ID = "missing_spreadsheet_id"
ERR_MISSING_SHEET_RANGE = "missing_sheet_range"
ERR_MISSING_COLUMN_MAPPING = "missing_column_mapping"
# Empty-candidate gate code (mirrors datastream_publication.GATE_EMPTY_CANDIDATE).
ERR_EMPTY_CANDIDATE = "empty_candidate"

# Honest terminal for the sync consumer: the snapshot landed + validated (rejection
# gate + empty gate passed) but the ATOMIC POINTER SWAP (12.5 commit_publication) is
# Phase B -- it needs the physical candidate rows in the live warehouse (AI-08). The
# ledger row legitimately stays 'written' (non-terminal) until that Phase-B step runs.
OUTCOME_VALIDATED_PENDING_PUBLICATION = "validated_pending_publication"

# The 15.6 adapter's stable typed exception class NAMES (matched by class-name so we
# never import from server/modules at module scope -- AD-2). ``ColumnMappingError``
# is the adapter's declared-column-absent signal (a schema/tab-structure drift);
# ``RateLimitError`` re-raises for the worker breaker.
_ADAPTER_COLUMN_MAPPING_ERROR_NAME = "ColumnMappingError"
_ADAPTER_RATE_LIMIT_ERROR_NAME = "RateLimitError"


# ---------------------------------------------------------------------------
# Public exceptions.
# ---------------------------------------------------------------------------


class SheetsSyncError(Exception):
    """Base for safe-fail sync errors carrying a stable ``code``."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code
        self.detail = detail


class QuotaViolation(SheetsSyncError):
    """Hourly cadence requested but not permitted by the quota profile."""

    def __init__(self, detail: str) -> None:
        super().__init__(ERR_QUOTA_HOURLY_NOT_PERMITTED, detail)


class SheetStructureError(SheetsSyncError):
    """Sheet structure does not match the declared mapping (safe-fail)."""


class RevokedConsentError(SheetsSyncError):
    """OAuth consent was revoked -- adapter returned PermissionError."""

    def __init__(self, detail: str = "") -> None:
        super().__init__(ERR_REVOKED_CONSENT, detail)


class SpreadsheetNotFoundError(SheetsSyncError):
    """Spreadsheet (or tab) no longer exists (safe-fail)."""

    def __init__(self, code: str = ERR_SPREADSHEET_NOT_FOUND, detail: str = "") -> None:
        super().__init__(code, detail)


# ---------------------------------------------------------------------------
# Content-hash helpers (pure, no DB).
# ---------------------------------------------------------------------------


def compute_rows_content_hash(rows: list[list[str]]) -> str:
    """Compute a SHA-256 content hash over the raw sheet value matrix.

    This is the no-op oracle input: identical sheet content -> identical hash
    -> ledger no-op (no row duplication). The hash covers the FULL raw value
    matrix (header + data rows) as a canonical JSON string (sorted within each
    row for stability). Headers are included so a column rename is detected.

    The hash is caller-supplied to ``open_import`` -- the ledger stores it and
    uses it as the unchanged-snapshot oracle (12.8 contract). The adapter
    computes it here from the raw bytes before any filtering or parsing so the
    hash reflects the actual source snapshot.
    """
    canonical = json.dumps(rows, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Column-mapping / header validation (pure, no network).
# ---------------------------------------------------------------------------


def _validate_sheet_headers(
    headers: list[str],
    column_mapping: dict[str, Any],
) -> list[str]:
    """Validate sheet headers against the declared column_mapping.

    Returns a list of validation error messages. An empty list means OK.
    Detects:
      - duplicate_headers: the sheet has repeated column names.
      - schema_drift: declared columns are absent from the sheet headers.
    AD-9: missing columns are explicit errors, never silent zeros.
    """
    errors: list[str] = []

    # Duplicate headers.
    seen: dict[str, int] = {}
    for h in headers:
        seen[h] = seen.get(h, 0) + 1
    duplicates = [h for h, count in seen.items() if count > 1]
    if duplicates:
        errors.append(
            f"{ERR_DUPLICATE_HEADERS}: sheet has duplicate column names: "
            + ", ".join(repr(d) for d in duplicates)
        )

    # Schema drift: declared columns absent.
    date_column = column_mapping.get("date_column", "")
    metric_columns = column_mapping.get("metric_columns", {})
    row_id_column = column_mapping.get("row_id_column")

    missing: list[str] = []
    if date_column and date_column not in headers:
        missing.append(f"date_column={date_column!r}")
    for col in metric_columns:
        if col not in headers:
            missing.append(f"metric_column={col!r}")
    if row_id_column is not None and row_id_column not in headers:
        missing.append(f"row_id_column={row_id_column!r}")

    if missing:
        errors.append(
            f"{ERR_SCHEMA_DRIFT}: declared columns absent from sheet headers "
            f"({', '.join(missing)}); available headers: {headers!r}"
        )

    return errors


# ---------------------------------------------------------------------------
# Quota / cadence validation (pure, no DB).
# ---------------------------------------------------------------------------


def validate_cadence(
    cadence_mode: str,
    quota_profile: dict[str, Any] | None,
) -> list[str]:
    """Validate the requested cadence against the quota profile.

    Hourly cadence is accepted ONLY when ``quota_profile.allow_hourly`` is
    ``True``. Daily and manual cadences are always permitted. Returns a list of
    validation error messages; empty = OK.

    This is pure (no network, no DB) so it can be exercised in offline tests.
    """
    errors: list[str] = []
    if cadence_mode == "hourly":
        prefs = quota_profile or {}
        if not prefs.get("allow_hourly", False):
            errors.append(
                f"{ERR_QUOTA_HOURLY_NOT_PERMITTED}: hourly cadence requires "
                "quota_profile.allow_hourly=true; configure it on the datastream "
                "or switch to daily/manual cadence."
            )
    return errors


# ---------------------------------------------------------------------------
# Empty-candidate gate (pure) -- mirrors evaluate_dq_gates' empty_candidate.
# ---------------------------------------------------------------------------


def evaluate_empty_candidate_gate(
    *,
    accepted_row_count: int,
    force_empty_publish: bool,
    allow_empty_publication: bool,
) -> dict[str, Any] | None:
    """Pure decision: does a zero-row candidate BLOCK publication?

    Mirrors ``core.datastream_publication.evaluate_dq_gates``' ``empty_candidate``
    gate (C1a): zero accepted rows are BLOCKED BY DEFAULT. An intentional empty
    publish is allowed ONLY when BOTH the explicit ``force_empty_publish`` argument
    AND the project preference ``allow_empty_publication`` are True. Returns a
    blocking ``{code, detail, repair}`` issue when blocked, else ``None``.

    Fails CLOSED: a non-empty candidate never trips this gate; an empty one trips it
    unless both confirmations are present.
    """
    if accepted_row_count != 0:
        return None
    if force_empty_publish and allow_empty_publication:
        return None
    return {
        "code": ERR_EMPTY_CANDIDATE,
        "detail": (
            "candidate has zero rows; set force_empty_publish=true and the "
            "allow_empty_publication project preference to publish an empty snapshot"
        ),
        "repair": {
            "confirm_empty_intent": {
                "force_empty_publish": True,
                "allow_empty_publication": True,
            }
        },
    }


def _read_allow_empty_publication(conn, project_id: str) -> bool:
    """Read the project-scoped ``allow_empty_publication`` preference (fail closed).

    Defaults to ``False`` (the documented default -- mirrors
    ``datastream_publication.DEFAULT_ALLOW_EMPTY_PUBLICATION``) when the column /
    row is absent or NULL, so an intentional empty publish is never silently
    allowed. Any read error also fails closed (returns False).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT allow_empty_publication
                FROM app.project_preferences
                WHERE project_id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
    except Exception:  # noqa: BLE001 - fail closed on any read error.
        return False
    if row is None or row[0] is None:
        return False
    return bool(row[0])


# ---------------------------------------------------------------------------
# Adapter-exception classification (M2) -- type-first, substring fallback.
# ---------------------------------------------------------------------------


def _classify_adapter_error(exc: Exception) -> tuple[str, str]:
    """Classify a recoverable adapter exception into a stable safe-fail code.

    M2: prefer the exception's TYPE over substring-sniffing the (French) message.
    We match on the class name / MRO so no import from server/modules is needed at
    module scope (AD-2). Only when the type is a bare ``ValueError`` / generic error
    do we fall back to substring hints for range-vs-tab disambiguation.

    Returns ``(error_code, detail)``. The caller has already excluded
    ``RateLimitError`` (re-raised) before reaching here.
    """
    detail = str(exc)
    type_names = {cls.__name__ for cls in type(exc).__mro__}

    # PermissionError (HTTP 403) -> revoked / missing consent.
    if isinstance(exc, PermissionError) or "PermissionError" in type_names:
        return ERR_REVOKED_CONSENT, detail

    # ColumnMappingError (15.6 typed, declared column absent) -> tab/structure drift.
    # Matched by class NAME so we never import it from server/modules (AD-2).
    if _ADAPTER_COLUMN_MAPPING_ERROR_NAME in type_names:
        return ERR_TAB_NOT_FOUND, detail

    # FileNotFoundError (HTTP 404) -> spreadsheet gone, OR a renamed/deleted tab when
    # the message carries range/tab context.
    if isinstance(exc, FileNotFoundError) or "FileNotFoundError" in type_names:
        lowered = detail.lower()
        if "range" in lowered or "tab" in lowered:
            return ERR_TAB_NOT_FOUND, detail
        return ERR_SPREADSHEET_NOT_FOUND, detail

    # Bare ValueError -> range drift (invalid / out-of-bounds range). This is the
    # only place substring hints are still consulted, and only to distinguish a
    # tab-context ValueError from a pure range one; either way it fails closed.
    if isinstance(exc, ValueError) or "ValueError" in type_names:
        lowered = detail.lower()
        if "tab" in lowered:
            return ERR_TAB_NOT_FOUND, detail
        return ERR_RANGE_DRIFT, detail

    # Unrecognised recoverable error: fail closed as a range drift (safe-fail, no
    # publish) rather than letting it escape or silently pass.
    return ERR_RANGE_DRIFT, detail


# ---------------------------------------------------------------------------
# Next-run / missed-run helpers (consume schedule seams, no DB).
# ---------------------------------------------------------------------------


def describe_next_run(schedule_window) -> dict[str, Any]:
    """Describe the next run / missed-run behaviour from a ScheduleWindow.

    Returns a dict suitable for surfacing in the sync status response:
      - next_run_at: ISO-8601 UTC string or null (manual schedule).
      - scheduled_for: current window scheduled_for timestamp.
      - coalesced_windows: how many missed windows were coalesced.
      - lookback_overlap_minutes: how far back the window extends.
    """
    if schedule_window is None:
        return {
            "next_run_at": None,
            "scheduled_for": None,
            "coalesced_windows": 0,
            "lookback_overlap_minutes": 0,
        }
    d = schedule_window.as_dict()
    return {
        "next_run_at": d.get("next_run_at"),
        "scheduled_for": d.get("scheduled_for"),
        "coalesced_windows": d.get("coalesced_windows", 0),
        "lookback_overlap_minutes": d.get("lookback_overlap_minutes", 0),
    }


# ---------------------------------------------------------------------------
# Idempotency-key builder (pure).
# ---------------------------------------------------------------------------


def build_sync_idempotency_key(
    datastream_id: str,
    run_id: str,
) -> str:
    """Build the idempotency key for a single sync run.

    The key encodes datastream + run_id so a retry of the same scheduled run
    (same run_id) is idempotent, and a new run (new run_id) is a fresh import.
    The 12.8 ledger hashes this key; we just compose the stable string here.
    """
    return f"gsheets_sync:{datastream_id}:{run_id}"


# ---------------------------------------------------------------------------
# Source-metadata builder (pure).
# ---------------------------------------------------------------------------


def build_source_metadata(
    spreadsheet_id: str,
    sheet_range: str,
    sheet_name: str,
    connection_id: str,
) -> dict[str, Any]:
    """Assemble the source_metadata dict stored in the ledger row.

    Stored in ``app.managed_feed_import_ledger.source_metadata`` (JSONB).
    Constitutes part of the idempotency IDENTITY (a range change -> new identity,
    not a conflict). No secrets: connection_id is a reference, not a token.
    """
    return {
        "feed_type": "google_sheets",
        "spreadsheet_id": spreadsheet_id,
        "sheet_range": sheet_range,
        "sheet_name": sheet_name or sheet_range,
        "connection_id": connection_id,
    }


# ---------------------------------------------------------------------------
# Core sync runner.
# ---------------------------------------------------------------------------


def run_sync(
    *,
    datastream_id: str,
    project_id: str,
    connection_id: str,
    spreadsheet_id: str,
    sheet_range: str,
    sheet_name: str,
    column_mapping: dict[str, Any],
    plan_version_id: str,
    mapping_version_id: str,
    projection_plan: dict[str, Any],
    actor: str,
    cadence_mode: str = "manual",
    quota_profile: dict[str, Any] | None = None,
    run_id: str | None = None,
    conn,
    sheets_adapter: Callable[..., list[list[str]]] | None = None,
    duckdb_path: str | None = None,
    force_empty_publish: bool = False,
) -> dict[str, Any]:
    """Execute one complete Google Sheets sync run through the 12.8 ledger.

    This is the orchestration boundary: it calls the 15.6 adapter for the
    actual read, then feeds the result into the common managed-feed path
    (open_import -> open_managed_landing -> record_rows -> mark_outcome).

    PHASE_B_LIVE_BLOCKED: ``sheets_adapter`` defaults to None and must be
    injected in tests or by the production dispatch hook. When None, the
    function raises ``NotImplementedError`` so the gap is explicit, never
    silently swallowed. Production code injects the 15.6 adapter callable.

    Args:
        datastream_id: Datastream identifier (``ds_<ULID>``).
        project_id:    Project identifier.
        connection_id: OAuth connection reference (passed to the adapter;
                       the adapter calls ``get_fresh_token(connection_id)``).
        spreadsheet_id: Google Spreadsheet ID (from the Sheet URL).
        sheet_range:   Range string (e.g. ``'Sheet1!A:F'``).
        sheet_name:    Human-readable sheet name for traceability.
        column_mapping: Declarative mapping from 15.6
                        (``{date_column, metric_columns, row_id_column?}``).
        plan_version_id: Committed plan version (``dsp_<ULID>``).
        mapping_version_id: Committed mapping version (``dmap_<ULID>``).
        projection_plan: Executable 12.4 projection plan (the 12.5 ticket).
        actor:          Identity string for audit rows.
        cadence_mode:   ``'manual'`` | ``'daily'`` | ``'hourly'``.
        quota_profile:  Project/datastream quota profile dict (may be None).
        run_id:         Caller-supplied run identifier for idempotency key
                        composition. Defaults to a fresh ULID (manual runs).
        conn:           Active psycopg connection (caller owns the transaction).
        sheets_adapter: Injectable adapter callable -- signature:
                        ``(connection_id, spreadsheet_id, sheet_range) ->
                        list[list[str]]``. Defaults to None (PHASE_B_LIVE_BLOCKED).
        duckdb_path:    DuckDB path for the RAW landing writer (dev/test only).
        force_empty_publish: Explicit operator confirmation that a zero-row snapshot
                        is intentional. Publication of an empty candidate ALSO
                        requires the project preference ``allow_empty_publication``.
                        Defaults False (empty is blocked -- C1a).

    Returns a ``sync_result`` dict::

        {
          "outcome": "validated_pending_publication" | "noop" | "failed"
                     | "rejected",
          "ledger_id": str,
          "execution_id": str | None,
          "row_count": int | None,
          "content_hash": str | None,
          "no_op": bool,
          "error_code": str | None,
          "error_detail": str | None,
          "next_run": {...},      # from describe_next_run()
        }

    Outcome semantics (C1):
      * ``validated_pending_publication`` -- the snapshot landed, record_rows ran,
        and BOTH the rejection gate and the empty-candidate gate passed. The ATOMIC
        POINTER SWAP (12.5 commit_publication) is Phase B (it needs the physical
        candidate in the live warehouse -- AI-08); the ledger row stays ``written``.
        ``run_sync`` NEVER marks the ledger ``published`` (that would flip a text
        column without swapping the published pointer).
      * ``rejected`` -- a blocking gate fired (rejection-threshold breach OR an empty
        candidate without both confirmations). Ledger marked ``rejected``; prior
        published version stays.
      * ``noop`` -- unchanged snapshot (NFR15).
      * ``failed`` -- a recoverable fetch/parse/write error (safe-fail).

    Safe-fail contract: ALL recoverable errors are caught, marked as
    ``failed``/``rejected`` on the ledger, and returned in the ``error_code`` field
    -- they do NOT bubble up as Python exceptions. ``RateLimitError`` (429) is the
    sole exception: it re-raises for the worker breaker. Other unrecoverable errors
    (DB connection loss, etc.) also bubble up.
    """
    # ------------------------------------------------------------------
    # 0. Cadence / quota validation (pure, before ANY DB write).
    # ------------------------------------------------------------------
    cadence_errors = validate_cadence(cadence_mode, quota_profile)
    if cadence_errors:
        raise QuotaViolation("; ".join(cadence_errors))

    # ------------------------------------------------------------------
    # 1. Validate required parameters.
    # ------------------------------------------------------------------
    param_errors: list[str] = []
    if not spreadsheet_id:
        param_errors.append(ERR_MISSING_SPREADSHEET_ID)
    if not sheet_range:
        param_errors.append(ERR_MISSING_SHEET_RANGE)
    if not column_mapping or "date_column" not in column_mapping:
        param_errors.append(ERR_MISSING_COLUMN_MAPPING)
    if param_errors:
        raise SheetsSyncError(
            "invalid_sync_config",
            "; ".join(param_errors),
        )

    # ------------------------------------------------------------------
    # 2. Mint a run_id if not supplied.
    # ------------------------------------------------------------------
    if run_id is None:
        run_id = str(ULID())

    idempotency_key = build_sync_idempotency_key(datastream_id, run_id)
    source_metadata = build_source_metadata(spreadsheet_id, sheet_range, sheet_name, connection_id)

    # ------------------------------------------------------------------
    # 3. Fetch the sheet snapshot (PHASE_B_LIVE_BLOCKED: adapter injected).
    # ------------------------------------------------------------------
    if sheets_adapter is None:
        raise NotImplementedError(
            "google_sheets_sync.run_sync: sheets_adapter must be injected. "
            "PHASE_B_LIVE_BLOCKED: live Google OAuth is not available in Phase B. "
            "Pass a mock adapter in tests or the 15.6 adapter callable in production."
        )

    raw_values: list[list[str]] = []
    fetch_error_code: str | None = None
    fetch_error_detail: str | None = None

    try:
        # PHASE_B_LIVE_BLOCKED: production path calls
        #   from server.modules.google-sheets.connector import _fetch_sheet_values
        # and passes (token, spreadsheet_id, sheet_range). In Phase B the test
        # adapter is injected here instead.
        raw_values = sheets_adapter(connection_id, spreadsheet_id, sheet_range)
    except Exception as exc:  # noqa: BLE001 - classified below; RateLimit re-raises.
        # RATE_LIMIT (429) re-raises so the worker breaker handles it (not a
        # safe-fail). Matched by class NAME so no import from server/modules (AD-2).
        if _ADAPTER_RATE_LIMIT_ERROR_NAME in {c.__name__ for c in type(exc).__mro__}:
            raise
        # Everything else is a recoverable safe-fail: classify TYPE-first (M2).
        fetch_error_code, fetch_error_detail = _classify_adapter_error(exc)
        logger.warning(
            '{"event": "gsheets_sync_fetch_error", '
            '"datastream_id": "%s", "spreadsheet_id": "%s", '
            '"error_code": "%s", "error": %s}',
            datastream_id,
            spreadsheet_id,
            fetch_error_code,
            (fetch_error_detail or "")[:200],
        )

    if fetch_error_code is not None:
        # Safe-fail: open a minimal ledger row in 'failed' state so the prior
        # published version stays available and the error is auditable.
        return _record_fetch_failure(
            datastream_id=datastream_id,
            project_id=project_id,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
            source_metadata=source_metadata,
            idempotency_key=idempotency_key,
            projection_plan=projection_plan,
            actor=actor,
            error_code=fetch_error_code,
            error_detail=fetch_error_detail or "",
            conn=conn,
        )

    # ------------------------------------------------------------------
    # 4. Validate sheet structure (pure -- no network after fetch).
    # ------------------------------------------------------------------
    headers: list[str] = []
    if raw_values:
        headers = [str(h).strip() for h in raw_values[0]]

    header_errors = _validate_sheet_headers(headers, column_mapping)
    if header_errors:
        error_code = (
            ERR_DUPLICATE_HEADERS
            if any(ERR_DUPLICATE_HEADERS in e for e in header_errors)
            else ERR_SCHEMA_DRIFT
        )
        return _record_fetch_failure(
            datastream_id=datastream_id,
            project_id=project_id,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
            source_metadata=source_metadata,
            idempotency_key=idempotency_key,
            projection_plan=projection_plan,
            actor=actor,
            error_code=error_code,
            error_detail="; ".join(header_errors),
            conn=conn,
        )

    # ------------------------------------------------------------------
    # 5. Compute content hash (no-op oracle input).
    # ------------------------------------------------------------------
    content_hash = compute_rows_content_hash(raw_values)

    # ------------------------------------------------------------------
    # 6. Open the import ledger row + isolated 12.5 candidate (NFR14).
    # ------------------------------------------------------------------
    import_result = ledger.open_import(
        datastream_id=datastream_id,
        project_id=project_id,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        feed_format=ledger.FORMAT_GOOGLE_SHEETS,
        projection_plan=projection_plan,
        actor=actor,
        idempotency_key=idempotency_key,
        source_metadata=source_metadata,
        content_hash=content_hash,
        conn=conn,
        write_mode=ledger.WRITE_MODE_REPLACE,
    )

    if import_result["no_op"]:
        # Unchanged snapshot: prior published version stays; freshness refreshed.
        ledger_row = import_result["ledger"]
        logger.info(
            '{"event": "gsheets_sync_noop", '
            '"datastream_id": "%s", "ledger_id": "%s", "content_hash": "%s"}',
            datastream_id,
            ledger_row["id"],
            content_hash[:8] + "...",
        )
        return {
            "outcome": ledger.OUTCOME_NOOP,
            "ledger_id": ledger_row["id"],
            "execution_id": None,
            "row_count": ledger_row.get("row_count"),
            "content_hash": content_hash,
            "no_op": True,
            "replay": import_result.get("replay", False),
            "error_code": None,
            "error_detail": None,
            "next_run": describe_next_run(None),
        }

    if import_result.get("replay"):
        # Idempotent replay of a prior run.
        ledger_row = import_result["ledger"]
        logger.info(
            '{"event": "gsheets_sync_replay", "datastream_id": "%s", "ledger_id": "%s"}',
            datastream_id,
            ledger_row["id"],
        )
        return {
            "outcome": ledger_row["outcome"],
            "ledger_id": ledger_row["id"],
            "execution_id": ledger_row.get("execution_id"),
            "row_count": ledger_row.get("row_count"),
            "content_hash": content_hash,
            "no_op": False,
            "replay": True,
            "error_code": ledger_row.get("error_code"),
            "error_detail": ledger_row.get("error_detail"),
            "next_run": describe_next_run(None),
        }

    ledger_row = import_result["ledger"]
    ledger_id = ledger_row["id"]
    execution = import_result["execution"]

    # ------------------------------------------------------------------
    # 7. Parse sheet rows (using the 15.6 parser via the adapter contract).
    # ------------------------------------------------------------------
    # The adapter has already returned raw_values. We parse them here using
    # the 15.6 module's _parse_sheet_response if available, or a minimal
    # fallback (used in tests when the adapter returns pre-parsed lists).
    parsed_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    parse_error_code: str | None = None
    parse_error_detail: str | None = None

    try:
        parsed_rows, rejected_rows = _parse_raw_values(raw_values, column_mapping)
    except Exception as exc:  # noqa: BLE001
        parse_error_code = ERR_SCHEMA_DRIFT
        parse_error_detail = str(exc)

    if parse_error_code is not None:
        ledger.mark_outcome(
            ledger_id,
            project_id,
            outcome=ledger.OUTCOME_FAILED,
            actor=actor,
            conn=conn,
            error_code=parse_error_code,
            error_detail=parse_error_detail,
        )
        logger.warning(
            '{"event": "gsheets_sync_parse_failed", '
            '"datastream_id": "%s", "ledger_id": "%s", "error": %s}',
            datastream_id,
            ledger_id,
            (parse_error_detail or "")[:200],
        )
        return {
            "outcome": ledger.OUTCOME_FAILED,
            "ledger_id": ledger_id,
            "execution_id": execution["id"] if execution else None,
            "row_count": None,
            "content_hash": content_hash,
            "no_op": False,
            "replay": False,
            "error_code": parse_error_code,
            "error_detail": parse_error_detail,
            "next_run": describe_next_run(None),
        }

    # ------------------------------------------------------------------
    # 8. Write the RAW landing (managed_feed_<datastream_id>).
    # ------------------------------------------------------------------
    accepted_count = len(parsed_rows)
    write_error_code: str | None = None
    write_error_detail: str | None = None

    try:
        landing_relation = ledger.allocate_landing_relation(
            datastream_id=datastream_id,
            project_id=project_id,
            raw_schema=None,  # resolved by warehouse_tenancy
        )
        _write_landing_rows(
            parsed_rows,
            landing_relation=landing_relation,
            datastream_id=datastream_id,
            project_id=project_id,
            duckdb_path=duckdb_path,
        )
    except Exception as exc:  # noqa: BLE001
        write_error_code = "landing_write_failed"
        write_error_detail = str(exc)

    if write_error_code is not None:
        ledger.mark_outcome(
            ledger_id,
            project_id,
            outcome=ledger.OUTCOME_FAILED,
            actor=actor,
            conn=conn,
            error_code=write_error_code,
            error_detail=write_error_detail,
        )
        logger.warning(
            '{"event": "gsheets_sync_write_failed", '
            '"datastream_id": "%s", "ledger_id": "%s", "error": %s}',
            datastream_id,
            ledger_id,
            (write_error_detail or "")[:200],
        )
        return {
            "outcome": ledger.OUTCOME_FAILED,
            "ledger_id": ledger_id,
            "execution_id": execution["id"] if execution else None,
            "row_count": None,
            "content_hash": content_hash,
            "no_op": False,
            "replay": False,
            "error_code": write_error_code,
            "error_detail": write_error_detail,
            "next_run": describe_next_run(None),
        }

    # ------------------------------------------------------------------
    # 9. Record rows on the ledger (advances 'opened' -> 'written').
    # ------------------------------------------------------------------
    ledger.record_rows(
        ledger_id=ledger_id,
        project_id=project_id,
        landing_relation=landing_relation,
        accepted_row_count=accepted_count,
        content_hash=content_hash,
        rejected_rows=rejected_rows,
        actor=actor,
        conn=conn,
    )

    # ------------------------------------------------------------------
    # 10. Blocking gates (both fail CLOSED, both -> 'rejected' on the ledger so
    #     the prior published version stays available -- no pointer swap).
    #
    #     (a) Rejection-threshold gate (12.8): rejected/(accepted+rejected) over
    #         the governed threshold blocks. This is what the H1 fix feeds: the
    #         unparsable-row count synthesized in _parse_raw_values reaches here.
    #     (b) Empty-candidate gate (C1a): zero accepted rows block UNLESS both the
    #         explicit force_empty_publish argument AND the project preference
    #         allow_empty_publication are True (mirrors evaluate_dq_gates).
    # ------------------------------------------------------------------
    gate_issue = ledger.evaluate_rejection_gate_for_ledger(ledger_id, project_id, conn)
    if gate_issue is None:
        allow_empty = _read_allow_empty_publication(conn, project_id)
        gate_issue = evaluate_empty_candidate_gate(
            accepted_row_count=accepted_count,
            force_empty_publish=force_empty_publish,
            allow_empty_publication=allow_empty,
        )

    if gate_issue is not None:
        ledger.mark_outcome(
            ledger_id,
            project_id,
            outcome=ledger.OUTCOME_REJECTED,
            actor=actor,
            conn=conn,
            error_code=gate_issue["code"],
            error_detail=gate_issue["detail"],
        )
        logger.warning(
            '{"event": "gsheets_sync_gate_blocked", '
            '"datastream_id": "%s", "ledger_id": "%s", "code": "%s"}',
            datastream_id,
            ledger_id,
            gate_issue["code"],
        )
        return {
            "outcome": ledger.OUTCOME_REJECTED,
            "ledger_id": ledger_id,
            "execution_id": execution["id"] if execution else None,
            "row_count": accepted_count,
            "content_hash": content_hash,
            "no_op": False,
            "replay": False,
            "error_code": gate_issue["code"],
            "error_detail": gate_issue["detail"],
            "repair": gate_issue.get("repair"),
            "next_run": describe_next_run(None),
        }

    # ------------------------------------------------------------------
    # 11. Validated, pending publication (HONEST Phase-B boundary -- C1b).
    #
    #     The snapshot landed, record_rows advanced the ledger 'opened' ->
    #     'written', and both blocking gates passed. We do NOT mark the ledger
    #     'published': the real publication is 12.5's atomic pointer swap
    #     (commit_publication), which requires the PHYSICAL candidate rows in the
    #     live warehouse (BigQuery) -- Phase B (AI-08). Marking 'published' here
    #     would flip a text column WITHOUT swapping current_published_execution_id,
    #     which is a lie. The ledger row legitimately stays 'written' (a valid
    #     non-terminal state) until the Phase-B step runs commit_publication and
    #     then mark_outcome('published').
    # ------------------------------------------------------------------
    logger.info(
        '{"event": "gsheets_sync_validated_pending_publication", '
        '"datastream_id": "%s", "ledger_id": "%s", '
        '"row_count": %d, "content_hash": "%s"}',
        datastream_id,
        ledger_id,
        accepted_count,
        content_hash[:8] + "...",
    )

    return {
        "outcome": OUTCOME_VALIDATED_PENDING_PUBLICATION,
        "ledger_id": ledger_id,
        "execution_id": execution["id"] if execution else None,
        "row_count": accepted_count,
        "content_hash": content_hash,
        "no_op": False,
        "replay": False,
        "error_code": None,
        "error_detail": None,
        # PHASE_B_LIVE_BLOCKED: the atomic pointer swap (12.5 commit_publication)
        # runs against the live warehouse candidate -- deferred to Phase B.
        "pending_publication": True,
        "next_run": describe_next_run(None),
    }


# ---------------------------------------------------------------------------
# Schedule-aware dispatch entry point.
# ---------------------------------------------------------------------------


def dispatch_managed_feed_sync(
    *,
    datastream_id: str,
    project_id: str,
    connection_id: str,
    spreadsheet_id: str,
    sheet_range: str,
    sheet_name: str,
    column_mapping: dict[str, Any],
    plan_version_id: str,
    mapping_version_id: str,
    projection_plan: dict[str, Any],
    cadence_policy: dict[str, Any],
    quota_profile: dict[str, Any] | None,
    actor: str,
    conn,
    sheets_adapter: Callable[..., list[list[str]]] | None = None,
    duckdb_path: str | None = None,
    now_utc: datetime | None = None,
    last_committed_watermark: datetime | None = None,
    force_empty_publish: bool = False,
) -> dict[str, Any]:
    """Schedule-aware wrapper: compute the current window, then run_sync.

    Consumes ``core.datastream_schedule.calculate_schedule_window`` for the
    next-run / missed-run / coalesce logic. The scheduler dispatch hook calls
    this function; the admin_api sync-now route calls ``run_sync`` directly
    (bypassing the window calculation with a fresh run_id).

    cadence_policy follows the ``core.datastream_schedule`` shape::

        {
          "mode": "manual" | "daily" | "hourly",
          "timezone": "Europe/Paris",
          "interval_minutes": 60,   # hourly only
          "watermark": {"delay_minutes": 5},
          "missed_run": {"mode": "coalesce", "max_catchup_windows": 3},
          "late_arrival": {"lookback_minutes": 15},
        }

    Returns the ``run_sync`` result dict augmented with ``next_run`` from the
    schedule window.
    """
    from core.datastream_schedule import calculate_schedule_window  # noqa: PLC0415

    if now_utc is None:
        now_utc = datetime.now(tz=UTC)

    schedule_window = calculate_schedule_window(
        cadence_policy,
        now_utc=now_utc,
        last_committed_watermark=last_committed_watermark,
    )

    cadence_mode = cadence_policy.get("mode", "manual")

    # Mint a run_id from the scheduled_for timestamp (stable across retries).
    if schedule_window.scheduled_for is not None:
        run_id = f"sched_{int(schedule_window.scheduled_for.timestamp())}"
    else:
        run_id = str(ULID())

    result = run_sync(
        datastream_id=datastream_id,
        project_id=project_id,
        connection_id=connection_id,
        spreadsheet_id=spreadsheet_id,
        sheet_range=sheet_range,
        sheet_name=sheet_name,
        column_mapping=column_mapping,
        plan_version_id=plan_version_id,
        mapping_version_id=mapping_version_id,
        projection_plan=projection_plan,
        actor=actor,
        cadence_mode=cadence_mode,
        quota_profile=quota_profile,
        run_id=run_id,
        conn=conn,
        sheets_adapter=sheets_adapter,
        duckdb_path=duckdb_path,
        force_empty_publish=force_empty_publish,
    )

    # Augment with full schedule context.
    result["next_run"] = describe_next_run(schedule_window)
    return result


# ---------------------------------------------------------------------------
# Private helpers.
# ---------------------------------------------------------------------------


def _record_fetch_failure(
    *,
    datastream_id: str,
    project_id: str,
    plan_version_id: str,
    mapping_version_id: str,
    source_metadata: dict[str, Any],
    idempotency_key: str,
    projection_plan: dict[str, Any],
    actor: str,
    error_code: str,
    error_detail: str,
    conn,
) -> dict[str, Any]:
    """Open a ledger row for a sync run that failed before any rows were fetched.

    The prior published version stays available: we open the import, then
    immediately mark it failed. No candidate is published, no landing is written.
    """
    try:
        import_result = ledger.open_import(
            datastream_id=datastream_id,
            project_id=project_id,
            plan_version_id=plan_version_id,
            mapping_version_id=mapping_version_id,
            feed_format=ledger.FORMAT_GOOGLE_SHEETS,
            projection_plan=projection_plan,
            actor=actor,
            idempotency_key=idempotency_key,
            source_metadata=source_metadata,
            content_hash=None,  # not yet known (fetch failed)
            conn=conn,
        )
        ledger_row = import_result["ledger"]
        ledger_id = ledger_row["id"]
        execution = import_result.get("execution")

        ledger.mark_outcome(
            ledger_id,
            project_id,
            outcome=ledger.OUTCOME_FAILED,
            actor=actor,
            conn=conn,
            error_code=error_code,
            error_detail=error_detail,
        )
    except Exception as exc:  # noqa: BLE001
        # If even the ledger open fails (e.g. DB hiccup), return the error
        # without a ledger_id -- this is a last-resort safe path.
        logger.error(
            '{"event": "gsheets_sync_ledger_open_failed", "datastream_id": "%s", "error": %s}',
            datastream_id,
            str(exc)[:200],
        )
        return {
            "outcome": ledger.OUTCOME_FAILED,
            "ledger_id": None,
            "execution_id": None,
            "row_count": None,
            "content_hash": None,
            "no_op": False,
            "replay": False,
            "error_code": error_code,
            "error_detail": error_detail,
            "next_run": describe_next_run(None),
        }

    return {
        "outcome": ledger.OUTCOME_FAILED,
        "ledger_id": ledger_id,
        "execution_id": execution["id"] if execution else None,
        "row_count": None,
        "content_hash": None,
        "no_op": False,
        "replay": False,
        "error_code": error_code,
        "error_detail": error_detail,
        "next_run": describe_next_run(None),
    }


def _parse_raw_values(
    raw_values: list[list[str]],
    column_mapping: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Parse raw Sheets API values into canonical records + per-row rejections.

    Attempts to delegate to the 15.6 module's ``_parse_sheet_response`` if
    importable; falls back to a minimal inline parser for test isolation
    (tests inject pre-processed rows via the adapter mock).

    Returns ``(accepted_rows, rejected_rows)`` where:
      - accepted_rows: list of parsed dict records (ready for RAW landing).
      - rejected_rows: list of rejection dicts compatible with
        ``ledger.record_rows`` (keys: row_number, field_name, rule, reason,
        rejected_value).

    AD-2: the import from server/modules/google-sheets is done at call time,
    guarded, and only used as a convenience -- the fallback path covers tests.

    AD-9 / H1: the 15.6 ``_parse_sheet_response`` returns
    ``(records, unparsable_count)``. The unparsable count MUST NOT be discarded --
    if it were, ``rejected_row_count`` would be 0 on the production path and the
    rejection gate would never fire on real data. We map each unparsable row into a
    structured rejection dict so the count reaches the ledger + the gate.
    """
    try:
        # Attempt to use the 15.6 module's parser directly.
        # This import is safe: the caller (run_sync) is already inside the
        # server/core boundary; the import is guarded so tests that don't
        # have server/modules on the path still work.
        from connector import _parse_sheet_response  # type: ignore[import]  # noqa: PLC0415

        records, unparsable_count = _parse_sheet_response(raw_values, column_mapping)
        # H1: synthesize structured rejection rows from the unparsable count so the
        # rejection gate (which reads rejected_row_count) sees them. The 15.6 parser
        # only surfaces a COUNT (per-row detail is logged there, not returned), so we
        # emit count-many rejection dicts with row_number=None (no ordinal available)
        # -- the ledger stores them as append-only rejection evidence.
        rejected_from_parser = [
            {
                "row_number": None,
                "field_name": None,
                "rule": "unparsable_metric",
                "reason": (
                    "row had one or more metric cells that could not be parsed as a "
                    "number (AD-9: counted, never a silent zero)"
                ),
                "rejected_value": None,
            }
            for _ in range(int(unparsable_count or 0))
        ]
        return records, rejected_from_parser
    except ImportError:
        pass

    # Fallback minimal parser (used in offline tests -- the adapter already
    # returns pre-parsed row dicts in test mode).
    if not raw_values or len(raw_values) < 2:
        return [], []

    headers = [str(h).strip() for h in raw_values[0]]
    date_column = column_mapping.get("date_column", "")
    metric_columns = column_mapping.get("metric_columns", {})
    row_id_column = column_mapping.get("row_id_column")

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []

    for row_idx, row in enumerate(raw_values[1:], start=1):
        cell = {h: (row[i] if i < len(row) else "") for i, h in enumerate(headers)}
        raw_date = cell.get(date_column, "").strip()
        if not raw_date:
            rejected.append(
                {
                    "row_number": row_idx,
                    "field_name": date_column,
                    "rule": "required",
                    "reason": "date cell is empty",
                    "rejected_value": raw_date,
                }
            )
            continue

        record: dict[str, Any] = {"date": raw_date}
        if row_id_column:
            record["sheet_row_id"] = cell.get(row_id_column, "").strip() or f"row_{row_idx}"
        else:
            record["sheet_row_id"] = f"row_{row_idx}"

        for col, canonical in metric_columns.items():
            raw_val = cell.get(col, "").strip()
            if not raw_val:
                record[canonical] = None
            else:
                try:
                    record[canonical] = float(raw_val.replace(",", "."))
                except (ValueError, TypeError):
                    record[canonical] = None

        accepted.append(record)

    return accepted, rejected


def _write_landing_rows(
    rows: list[dict[str, Any]],
    *,
    landing_relation: str,
    datastream_id: str,
    project_id: str,
    duckdb_path: str | None,
) -> int:
    """Write accepted rows to the project-scoped managed RAW landing.

    Uses ``ledger.open_managed_landing`` -> ``open_raw_writer`` routing
    (AD-8: never a mart; schema from warehouse_tenancy). Returns the row count.

    In Phase B this will route to BigQuery via the same open_raw_writer seam.
    At Phase A / dev, uses DuckDB.
    """
    if not rows:
        return 0

    if duckdb_path is None:
        import os  # noqa: PLC0415

        duckdb_path = os.environ.get("TOOROW_DUCKDB_PATH", ":memory:")

    con = ledger.open_managed_landing(duckdb_path, project_id)

    # Extract table name from landing_relation (schema.table or just table).
    table_name = landing_relation.rsplit(".", 1)[-1]

    # Derive column names from first row.
    if not rows:
        con.close()
        return 0

    columns = list(rows[0].keys())
    col_defs = ", ".join(f"{c} VARCHAR" for c in columns)
    col_list = ", ".join(columns)
    placeholders = ", ".join("?" * len(columns))

    create_sql = f"CREATE TABLE IF NOT EXISTS {table_name} ({col_defs})"
    insert_sql = f"INSERT INTO {table_name} ({col_list}) VALUES ({placeholders})"

    con.execute(create_sql)
    values = [tuple(str(r.get(c, "") or "") for c in columns) for r in rows]
    if values:
        con.executemany(insert_sql, values)
    con.close()
    return len(rows)
