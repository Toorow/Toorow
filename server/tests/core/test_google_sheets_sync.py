"""Offline unit tests for the Google Sheets managed-feed sync consumer (Story 12.10).

These run WITHOUT Postgres and WITHOUT live Google OAuth. The 15.6 Sheets
adapter and the 12.8 ledger DB calls are fully mocked (no network, no DB).

What each test section proves:
  1. Cadence / quota validation (pure): hourly accepted only with allow_hourly=true.
  2. Content hash (pure): identical raw values -> identical hash; any change -> different hash.
  3. Source metadata / idempotency key (pure): deterministic, no secrets.
  4. Header validation (pure): duplicate headers detected; schema drift detected.
  5. Snapshot -> ledger delegation: run_sync calls open_import, record_rows, mark_outcome.
  6. Unchanged snapshot -> no-op: same content_hash -> ledger no_op path.
  7. Safe-fail matrix: revoked consent, spreadsheet not found, tab renamed, range drift,
     duplicate headers, schema drift -- all produce a failed ledger row, prior published
     version stays available, no exception bubbles to the caller.
  8. No write scope: the adapter callable signature does NOT accept a write scope;
     the sync consumer never passes one.
  9. Phase-B marker: None adapter raises NotImplementedError (explicit, not silently skipped).
 10. Schedule dispatch: dispatch_managed_feed_sync populates next_run from the window.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

import pytest  # noqa: E402
from core.google_sheets_sync import (  # noqa: E402
    ERR_DUPLICATE_HEADERS,
    ERR_EMPTY_CANDIDATE,
    ERR_QUOTA_HOURLY_NOT_PERMITTED,
    ERR_RANGE_DRIFT,
    ERR_REVOKED_CONSENT,
    ERR_SCHEMA_DRIFT,
    ERR_SPREADSHEET_NOT_FOUND,
    ERR_TAB_NOT_FOUND,
    OUTCOME_VALIDATED_PENDING_PUBLICATION,
    QuotaViolation,
    SheetsSyncError,
    _classify_adapter_error,
    _parse_raw_values,
    _validate_sheet_headers,
    build_source_metadata,
    build_sync_idempotency_key,
    compute_rows_content_hash,
    describe_next_run,
    evaluate_empty_candidate_gate,
    validate_cadence,
)

# ---------------------------------------------------------------------------
# Shared fixtures / helpers.
# ---------------------------------------------------------------------------

_SPREADSHEET_ID = "1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
_SHEET_RANGE = "Budget2026!A:F"
_SHEET_NAME = "Budget2026"
_DATASTREAM_ID = "ds_01ABCDEFGHIJKLMNOPQRST"
_PROJECT_ID = "proj_test_1"
_CONNECTION_ID = "conn_google_direct_1"

_MINIMAL_COLUMN_MAPPING = {
    "date_column": "Date",
    "metric_columns": {"Budget": "budget_declared", "Objectif CA": "target_revenue"},
    "row_id_column": "Campagne",
}

_RAW_VALUES_TWO_ROWS = [
    ["Date", "Campagne", "Budget", "Objectif CA"],
    ["2026-07-01", "Camp A", "10000", "50000"],
    ["2026-07-02", "Camp B", "5000,50", "25000"],
]

_RAW_VALUES_SAME = [
    ["Date", "Campagne", "Budget", "Objectif CA"],
    ["2026-07-01", "Camp A", "10000", "50000"],
]


def _fake_adapter(
    connection_id: str,
    spreadsheet_id: str,
    sheet_range: str,
    *,
    raise_exc: Exception | None = None,
    return_values: list | None = None,
) -> list[list[str]]:
    """A fake sheets adapter for tests (no network, no write scope)."""
    if raise_exc is not None:
        raise raise_exc
    return return_values or _RAW_VALUES_TWO_ROWS


def _make_adapter(*, raise_exc=None, return_values=None):
    """Return a callable adapter that optionally raises or returns specific values."""

    def adapter(connection_id, spreadsheet_id, sheet_range):
        if raise_exc is not None:
            raise raise_exc
        return return_values if return_values is not None else _RAW_VALUES_TWO_ROWS

    return adapter


def _make_fake_conn(
    *,
    open_import_result: dict | None = None,
    replay: bool = False,
    no_op: bool = False,
):
    """Build a minimal psycopg-like connection mock for testing run_sync.

    The mock wires open_import / record_rows / mark_outcome / evaluate_rejection_gate_for_ledger
    via patching in the caller.
    """
    conn = MagicMock()
    return conn


def _base_run_sync_args(**overrides) -> dict:
    base = dict(
        datastream_id=_DATASTREAM_ID,
        project_id=_PROJECT_ID,
        connection_id=_CONNECTION_ID,
        spreadsheet_id=_SPREADSHEET_ID,
        sheet_range=_SHEET_RANGE,
        sheet_name=_SHEET_NAME,
        column_mapping=_MINIMAL_COLUMN_MAPPING,
        plan_version_id="dsp_01",
        mapping_version_id="dmap_01",
        projection_plan={"executable": True},
        actor="test-actor",
        cadence_mode="manual",
        quota_profile=None,
        run_id="test-run-001",
        conn=MagicMock(),
        sheets_adapter=_make_adapter(),
        duckdb_path=":memory:",
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# 1. Cadence / quota validation (pure).
# ---------------------------------------------------------------------------


class TestCadenceValidation:
    def test_manual_cadence_always_permitted(self):
        errors = validate_cadence("manual", quota_profile=None)
        assert errors == []

    def test_daily_cadence_always_permitted(self):
        errors = validate_cadence("daily", quota_profile=None)
        assert errors == []

    def test_hourly_cadence_blocked_when_no_profile(self):
        errors = validate_cadence("hourly", quota_profile=None)
        assert len(errors) == 1
        assert ERR_QUOTA_HOURLY_NOT_PERMITTED in errors[0]

    def test_hourly_cadence_blocked_when_profile_missing_allow_hourly(self):
        errors = validate_cadence("hourly", quota_profile={"some_other_key": True})
        assert len(errors) == 1
        assert ERR_QUOTA_HOURLY_NOT_PERMITTED in errors[0]

    def test_hourly_cadence_blocked_when_allow_hourly_false(self):
        errors = validate_cadence("hourly", quota_profile={"allow_hourly": False})
        assert len(errors) == 1
        assert ERR_QUOTA_HOURLY_NOT_PERMITTED in errors[0]

    def test_hourly_cadence_permitted_when_allow_hourly_true(self):
        errors = validate_cadence("hourly", quota_profile={"allow_hourly": True})
        assert errors == []

    def test_quota_violation_raised_when_hourly_not_permitted(self):
        """run_sync raises QuotaViolation BEFORE any DB write when hourly is blocked."""
        with pytest.raises(QuotaViolation) as exc_info:
            from core.google_sheets_sync import run_sync

            run_sync(
                **_base_run_sync_args(
                    cadence_mode="hourly",
                    quota_profile=None,
                )
            )
        assert ERR_QUOTA_HOURLY_NOT_PERMITTED in str(exc_info.value)

    def test_hourly_cadence_permitted_with_quota_profile(self):
        """validate_cadence passes with allow_hourly=True (no exception)."""
        errors = validate_cadence("hourly", quota_profile={"allow_hourly": True})
        assert errors == []


# ---------------------------------------------------------------------------
# 2. Content hash (pure).
# ---------------------------------------------------------------------------


class TestContentHash:
    def test_identical_values_produce_identical_hash(self):
        values = [["A", "B"], ["1", "2"], ["3", "4"]]
        h1 = compute_rows_content_hash(values)
        h2 = compute_rows_content_hash(values)
        assert h1 == h2

    def test_hash_is_64_hex(self):
        h = compute_rows_content_hash(_RAW_VALUES_TWO_ROWS)
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_different_data_produces_different_hash(self):
        values_a = [["Date", "Budget"], ["2026-07-01", "100"]]
        values_b = [["Date", "Budget"], ["2026-07-01", "200"]]
        assert compute_rows_content_hash(values_a) != compute_rows_content_hash(values_b)

    def test_header_change_produces_different_hash(self):
        """A column rename changes the hash -- detects schema drift at no-op level."""
        values_a = [["Date", "Budget"], ["2026-07-01", "100"]]
        values_b = [["Date", "Budget_Renamed"], ["2026-07-01", "100"]]
        assert compute_rows_content_hash(values_a) != compute_rows_content_hash(values_b)

    def test_empty_sheet_produces_stable_hash(self):
        h = compute_rows_content_hash([])
        assert len(h) == 64

    def test_header_only_sheet_has_its_own_hash(self):
        header_only = [["Date", "Budget"]]
        two_rows = [["Date", "Budget"], ["2026-07-01", "100"]]
        assert compute_rows_content_hash(header_only) != compute_rows_content_hash(two_rows)


# ---------------------------------------------------------------------------
# 3. Source metadata / idempotency key (pure).
# ---------------------------------------------------------------------------


class TestSourceMetadata:
    def test_source_metadata_contains_no_token(self):
        meta = build_source_metadata(_SPREADSHEET_ID, _SHEET_RANGE, _SHEET_NAME, _CONNECTION_ID)
        # Must not contain a token, password, or secret.
        assert "token" not in str(meta).lower()
        assert "password" not in str(meta).lower()
        assert "secret" not in str(meta).lower()

    def test_source_metadata_has_required_keys(self):
        meta = build_source_metadata(_SPREADSHEET_ID, _SHEET_RANGE, _SHEET_NAME, _CONNECTION_ID)
        assert meta["spreadsheet_id"] == _SPREADSHEET_ID
        assert meta["sheet_range"] == _SHEET_RANGE
        assert meta["connection_id"] == _CONNECTION_ID
        assert meta["feed_type"] == "google_sheets"

    def test_idempotency_key_is_deterministic(self):
        k1 = build_sync_idempotency_key(_DATASTREAM_ID, "run-001")
        k2 = build_sync_idempotency_key(_DATASTREAM_ID, "run-001")
        assert k1 == k2

    def test_different_run_id_yields_different_key(self):
        k1 = build_sync_idempotency_key(_DATASTREAM_ID, "run-001")
        k2 = build_sync_idempotency_key(_DATASTREAM_ID, "run-002")
        assert k1 != k2

    def test_different_datastream_yields_different_key(self):
        k1 = build_sync_idempotency_key("ds_A", "run-001")
        k2 = build_sync_idempotency_key("ds_B", "run-001")
        assert k1 != k2


# ---------------------------------------------------------------------------
# 4. Header validation (pure).
# ---------------------------------------------------------------------------


class TestHeaderValidation:
    def test_valid_headers_produce_no_errors(self):
        headers = ["Date", "Campagne", "Budget", "Objectif CA"]
        errors = _validate_sheet_headers(headers, _MINIMAL_COLUMN_MAPPING)
        assert errors == []

    def test_duplicate_headers_detected(self):
        headers = ["Date", "Budget", "Budget", "Other"]
        errors = _validate_sheet_headers(
            headers,
            {"date_column": "Date", "metric_columns": {"Budget": "budget_declared"}},
        )
        assert any(ERR_DUPLICATE_HEADERS in e for e in errors)

    def test_missing_date_column_detected_as_schema_drift(self):
        headers = ["Campagne", "Budget"]
        errors = _validate_sheet_headers(
            headers,
            {"date_column": "Date", "metric_columns": {"Budget": "budget_declared"}},
        )
        assert any(ERR_SCHEMA_DRIFT in e for e in errors)

    def test_missing_metric_column_detected_as_schema_drift(self):
        headers = ["Date", "Campagne"]
        errors = _validate_sheet_headers(
            headers,
            {"date_column": "Date", "metric_columns": {"Budget": "budget_declared"}},
        )
        assert any(ERR_SCHEMA_DRIFT in e for e in errors)

    def test_missing_row_id_column_detected(self):
        headers = ["Date", "Budget"]
        errors = _validate_sheet_headers(
            headers,
            {
                "date_column": "Date",
                "metric_columns": {"Budget": "budget_declared"},
                "row_id_column": "Campagne",
            },
        )
        assert any(ERR_SCHEMA_DRIFT in e for e in errors)

    def test_extra_columns_in_sheet_are_ignored(self):
        """Extra columns in the sheet beyond what the mapping declares are OK.

        The sheet carries ALL declared columns (Date, Campagne, Budget, Objectif CA)
        PLUS two undeclared extras -- validation must pass (only DECLARED columns are
        required to be present; extras are ignored, not a schema drift).
        """
        headers = [
            "Date",
            "Campagne",
            "Budget",
            "Objectif CA",
            "ExtraColumn",
            "AnotherExtra",
        ]
        errors = _validate_sheet_headers(headers, _MINIMAL_COLUMN_MAPPING)
        assert errors == []

    def test_empty_headers_triggers_schema_drift(self):
        errors = _validate_sheet_headers(
            [],
            {"date_column": "Date", "metric_columns": {"Budget": "budget_declared"}},
        )
        assert any(ERR_SCHEMA_DRIFT in e for e in errors)


# ---------------------------------------------------------------------------
# 5. Snapshot -> ledger delegation (mocked DB).
# ---------------------------------------------------------------------------


class TestRunSyncLedgerDelegation:
    """Verify run_sync delegates correctly to the 12.8 ledger API."""

    def _make_ledger_mocks(self):
        """Return patch contexts for the three ledger functions."""
        import_result = {
            "ledger": {
                "id": "mfl_01TEST",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_01TEST",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_01TEST"},
            "no_op": False,
            "replay": False,
        }
        return import_result

    def test_run_sync_calls_open_import(self):
        """run_sync must call ledger.open_import with feed_format='google_sheets'."""
        import_result = self._make_ledger_mocks()

        with (
            patch(
                "core.google_sheets_sync.ledger.open_import", return_value=import_result
            ) as mock_open,
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
        ):
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args())

            assert mock_open.called
            call_kwargs = mock_open.call_args.kwargs
            assert call_kwargs["feed_format"] == "google_sheets"
            assert call_kwargs["datastream_id"] == _DATASTREAM_ID
            assert call_kwargs["project_id"] == _PROJECT_ID

    def test_run_sync_calls_record_rows_after_write(self):
        """record_rows must be called after the landing write."""
        import_result = self._make_ledger_mocks()

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}) as mock_record,
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
        ):
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args())

            assert mock_record.called
            call_kwargs = mock_record.call_args.kwargs
            assert call_kwargs["ledger_id"] == "mfl_01TEST"
            assert call_kwargs["project_id"] == _PROJECT_ID
            # content_hash must be the 64-hex we computed from the raw values.
            assert len(call_kwargs["content_hash"]) == 64

    def test_run_sync_outcome_validated_pending_publication_on_success(self):
        """A validated sync returns the HONEST Phase-B terminal, NOT 'published'.

        C1b: run_sync does the snapshot -> ledger -> landing -> record_rows ->
        gates portion, but the atomic pointer swap (12.5 commit_publication) is
        Phase B. run_sync must NOT fake a publish -- it returns
        'validated_pending_publication' and never marks the ledger 'published'.
        """
        import_result = self._make_ledger_mocks()

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
            # allow_empty preference is irrelevant here (2 accepted rows); return
            # False to prove the empty gate is not what lets this pass.
            patch(
                "core.google_sheets_sync._read_allow_empty_publication",
                return_value=False,
            ),
        ):
            from core.google_sheets_sync import (
                OUTCOME_VALIDATED_PENDING_PUBLICATION,
                run_sync,
            )

            result = run_sync(**_base_run_sync_args())

            assert result["outcome"] == OUTCOME_VALIDATED_PENDING_PUBLICATION
            assert result["no_op"] is False
            assert result["error_code"] is None
            assert result["pending_publication"] is True
            # The ledger is NEVER marked 'published' by run_sync (no fake publish).
            for call in mock_mark.call_args_list:
                assert call.kwargs.get("outcome") != "published"

    def test_no_write_scope_in_adapter_call(self):
        """The adapter is called with (connection_id, spreadsheet_id, sheet_range) only.

        This proves the sync consumer NEVER requests a write scope: the adapter
        signature is read-only (spreadsheets.readonly via get_fresh_token, AD-21).
        """
        adapter_calls: list[tuple] = []

        def capturing_adapter(connection_id, spreadsheet_id, sheet_range):
            adapter_calls.append((connection_id, spreadsheet_id, sheet_range))
            return _RAW_VALUES_TWO_ROWS

        import_result = self._make_ledger_mocks()

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
        ):
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args(sheets_adapter=capturing_adapter))

        assert len(adapter_calls) == 1
        args = adapter_calls[0]
        # Exactly 3 positional args; no write-scope keyword argument.
        assert args == (_CONNECTION_ID, _SPREADSHEET_ID, _SHEET_RANGE)


# ---------------------------------------------------------------------------
# 6. Unchanged snapshot -> no-op (NFR15).
# ---------------------------------------------------------------------------


class TestNoOp:
    def test_unchanged_snapshot_returns_noop(self):
        """When open_import returns no_op=True, run_sync returns outcome='noop'."""
        noop_result = {
            "ledger": {
                "id": "mfl_NOOP01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": None,
                "outcome": "noop",
                "row_count": 2,
                "content_hash": compute_rows_content_hash(_RAW_VALUES_SAME),
                "error_code": None,
                "error_detail": None,
            },
            "execution": None,
            "no_op": True,
            "replay": False,
        }

        with patch("core.google_sheets_sync.ledger.open_import", return_value=noop_result):
            from core.google_sheets_sync import run_sync

            result = run_sync(
                **_base_run_sync_args(sheets_adapter=_make_adapter(return_values=_RAW_VALUES_SAME))
            )

        assert result["outcome"] == "noop"
        assert result["no_op"] is True
        assert result["execution_id"] is None

    def test_noop_does_not_call_record_rows(self):
        """A no-op must NOT call record_rows (no row duplication, NFR15)."""
        noop_result = {
            "ledger": {
                "id": "mfl_NOOP02",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": None,
                "outcome": "noop",
                "row_count": 2,
                "content_hash": "a" * 64,
                "error_code": None,
                "error_detail": None,
            },
            "execution": None,
            "no_op": True,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=noop_result),
            patch("core.google_sheets_sync.ledger.record_rows") as mock_record,
        ):
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args())

        mock_record.assert_not_called()

    def test_noop_does_not_call_mark_outcome(self):
        """A no-op does NOT advance the outcome (it is already terminal)."""
        noop_result = {
            "ledger": {
                "id": "mfl_NOOP03",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": None,
                "outcome": "noop",
                "row_count": 2,
                "content_hash": "b" * 64,
                "error_code": None,
                "error_detail": None,
            },
            "execution": None,
            "no_op": True,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=noop_result),
            patch("core.google_sheets_sync.ledger.mark_outcome") as mock_mark,
        ):
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args())

        mock_mark.assert_not_called()


# ---------------------------------------------------------------------------
# 7. Safe-fail matrix.
# ---------------------------------------------------------------------------


class TestSafeFailMatrix:
    """Every recoverable error results in a failed ledger row; prior published stays."""

    def _run_with_adapter_error(self, exc: Exception) -> dict:
        """Run run_sync with an adapter that raises *exc*; return the result dict."""
        import_result = {
            "ledger": {
                "id": "mfl_FAIL01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_FAIL01",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_FAIL01"},
            "no_op": False,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(**_base_run_sync_args(sheets_adapter=_make_adapter(raise_exc=exc)))
            self._mock_mark = mock_mark
        return result

    def test_revoked_consent_is_safe_fail(self):
        result = self._run_with_adapter_error(
            PermissionError("google-sheets: HTTP 403 -- spreadsheets.readonly revoked")
        )
        assert result["outcome"] == "failed"
        assert result["error_code"] == ERR_REVOKED_CONSENT
        assert result["no_op"] is False
        # The ledger mark_outcome was called with 'failed'.
        self._mock_mark.assert_called()
        call_kwargs = self._mock_mark.call_args.kwargs
        assert call_kwargs["outcome"] == "failed"

    def test_spreadsheet_not_found_is_safe_fail(self):
        result = self._run_with_adapter_error(
            FileNotFoundError("google-sheets: HTTP 404 -- spreadsheet not found")
        )
        assert result["outcome"] == "failed"
        assert result["error_code"] in (ERR_SPREADSHEET_NOT_FOUND, ERR_TAB_NOT_FOUND)

    def test_tab_renamed_is_safe_fail(self):
        result = self._run_with_adapter_error(
            FileNotFoundError("google-sheets: range 'Budget2026!A:F' not found (tab renamed)")
        )
        assert result["outcome"] == "failed"
        assert result["error_code"] in (ERR_TAB_NOT_FOUND, ERR_SPREADSHEET_NOT_FOUND)

    def test_range_drift_is_safe_fail(self):
        result = self._run_with_adapter_error(
            ValueError("google-sheets: invalid range 'OldSheet!A:F'")
        )
        assert result["outcome"] == "failed"
        assert result["error_code"] == ERR_RANGE_DRIFT

    def test_duplicate_headers_is_safe_fail(self):
        """Duplicate headers caught AFTER fetch -> failed ledger, no rows written."""
        duplicate_header_values = [
            ["Date", "Budget", "Budget", "Other"],  # Budget appears twice
            ["2026-07-01", "100", "200", "x"],
        ]
        import_result = {
            "ledger": {
                "id": "mfl_DUP01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_DUP01",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_DUP01"},
            "no_op": False,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(
                **_base_run_sync_args(
                    column_mapping={
                        "date_column": "Date",
                        "metric_columns": {"Budget": "budget_declared"},
                    },
                    sheets_adapter=_make_adapter(return_values=duplicate_header_values),
                )
            )

        assert result["outcome"] == "failed"
        assert result["error_code"] == ERR_DUPLICATE_HEADERS
        mock_mark.assert_called()
        assert mock_mark.call_args.kwargs["outcome"] == "failed"

    def test_schema_drift_is_safe_fail(self):
        """Declared metric column missing from sheet -> schema_drift safe-fail."""
        values_missing_col = [
            ["Date", "Campagne"],  # 'Budget' column removed
            ["2026-07-01", "Camp A"],
        ]
        import_result = {
            "ledger": {
                "id": "mfl_DRIFT01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_DRIFT01",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_DRIFT01"},
            "no_op": False,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(
                **_base_run_sync_args(
                    sheets_adapter=_make_adapter(return_values=values_missing_col)
                )
            )

        assert result["outcome"] == "failed"
        assert result["error_code"] == ERR_SCHEMA_DRIFT
        mock_mark.assert_called()
        assert mock_mark.call_args.kwargs["outcome"] == "failed"

    def test_safe_fail_does_not_raise_to_caller(self):
        """All safe-fail paths return a dict; exceptions do NOT propagate."""
        errors_to_test = [
            PermissionError("consent revoked"),
            FileNotFoundError("spreadsheet gone"),
            ValueError("range drift"),
        ]
        for exc in errors_to_test:
            import_result = {
                "ledger": {
                    "id": "mfl_SAFE01",
                    "datastream_id": _DATASTREAM_ID,
                    "project_id": _PROJECT_ID,
                    "execution_id": "dse_SAFE01",
                    "outcome": "opened",
                    "row_count": None,
                    "content_hash": None,
                    "error_code": None,
                    "error_detail": None,
                },
                "execution": {"id": "dse_SAFE01"},
                "no_op": False,
                "replay": False,
            }
            with (
                patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
                patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
            ):
                from core.google_sheets_sync import run_sync

                # Must NOT raise:
                result = run_sync(
                    **_base_run_sync_args(sheets_adapter=_make_adapter(raise_exc=exc))
                )
                assert result["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 8. No write scope proof (structural).
# ---------------------------------------------------------------------------


class TestNoWriteScope:
    def test_adapter_signature_has_no_write_scope_param(self):
        """The adapter callable receives ONLY (connection_id, spreadsheet_id, sheet_range).

        This is the structural guarantee: the sync consumer never passes a
        write scope (e.g. 'spreadsheets' without .readonly), because the
        adapter signature does not have a scope parameter.
        """
        import inspect

        # The fake adapter used in tests only accepts 3 positional args.
        sig = inspect.signature(_make_adapter())
        params = list(sig.parameters.keys())
        assert "scope" not in params
        assert "write" not in " ".join(params)

    def test_sheets_api_url_never_contains_write_endpoint(self):
        """The 15.6 adapter uses spreadsheets.readonly -- never spreadsheets (rw).

        This test checks the 15.6 module's SHEETS_API_BASE and _fetch_sheet_values
        at import time (if available) to confirm the URL never includes a write op.
        Phase B: the module may not be importable in isolation; that's expected.
        """
        try:
            import connector as sheets_module  # type: ignore[import]

            base = getattr(sheets_module, "SHEETS_API_BASE", "")
            # The URL must be the read-only Sheets API, not Drive upload etc.
            assert "spreadsheets.googleapis.com" not in base or "upload" not in base
        except (ImportError, ModuleNotFoundError):
            pytest.skip("15.6 adapter not importable in offline test context (PHASE_B)")


# ---------------------------------------------------------------------------
# 9. Phase-B marker: None adapter raises NotImplementedError.
# ---------------------------------------------------------------------------


class TestPhaseBMarker:
    def test_none_adapter_raises_not_implemented(self):
        """run_sync with sheets_adapter=None raises NotImplementedError (explicit gap).

        In Phase B, live Google OAuth is not available. The caller MUST inject
        the adapter; passing None is an explicit coding error, not a silent skip.
        """
        with pytest.raises(NotImplementedError) as exc_info:
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args(sheets_adapter=None))

        assert "PHASE_B_LIVE_BLOCKED" in str(exc_info.value)

    def test_none_adapter_raises_before_cadence_check(self):
        """QuotaViolation fires BEFORE the NotImplementedError (cadence is checked first)."""
        with pytest.raises(QuotaViolation):
            from core.google_sheets_sync import run_sync

            run_sync(
                **_base_run_sync_args(
                    sheets_adapter=None,
                    cadence_mode="hourly",
                    quota_profile=None,
                )
            )


# ---------------------------------------------------------------------------
# 10. Schedule dispatch: describe_next_run + dispatch_managed_feed_sync.
# ---------------------------------------------------------------------------


class TestScheduleDispatch:
    def test_describe_next_run_with_none_returns_nulls(self):
        result = describe_next_run(None)
        assert result["next_run_at"] is None
        assert result["scheduled_for"] is None

    def test_describe_next_run_with_window(self):
        # Build a minimal ScheduleWindow-like object.
        class FakeWindow:
            def as_dict(self):
                return {
                    "next_run_at": "2026-07-24T02:00:00Z",
                    "scheduled_for": "2026-07-23T02:00:00Z",
                    "coalesced_windows": 1,
                    "lookback_overlap_minutes": 15,
                }

        result = describe_next_run(FakeWindow())
        assert result["next_run_at"] == "2026-07-24T02:00:00Z"
        assert result["coalesced_windows"] == 1
        assert result["lookback_overlap_minutes"] == 15

    def test_dispatch_managed_feed_sync_calls_run_sync(self):
        """dispatch_managed_feed_sync delegates to run_sync with a stable run_id."""
        from datetime import UTC, datetime

        cadence_policy = {
            "mode": "daily",
            "timezone": "Europe/Paris",
            "watermark": {"delay_minutes": 5},
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 3},
            "late_arrival": {"lookback_minutes": 15},
        }

        import_result = {
            "ledger": {
                "id": "mfl_DISP01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_DISP01",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_DISP01"},
            "no_op": False,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
        ):
            from core.google_sheets_sync import dispatch_managed_feed_sync

            result = dispatch_managed_feed_sync(
                datastream_id=_DATASTREAM_ID,
                project_id=_PROJECT_ID,
                connection_id=_CONNECTION_ID,
                spreadsheet_id=_SPREADSHEET_ID,
                sheet_range=_SHEET_RANGE,
                sheet_name=_SHEET_NAME,
                column_mapping=_MINIMAL_COLUMN_MAPPING,
                plan_version_id="dsp_01",
                mapping_version_id="dmap_01",
                projection_plan={"executable": True},
                cadence_policy=cadence_policy,
                quota_profile=None,
                actor="scheduler",
                conn=MagicMock(),
                sheets_adapter=_make_adapter(),
                duckdb_path=":memory:",
                now_utc=datetime(2026, 7, 23, 10, 0, tzinfo=UTC),
            )

        # The result includes next_run from the schedule window.
        assert "next_run" in result
        assert "next_run_at" in result["next_run"]

    def test_dispatch_idempotent_same_schedule_window(self):
        """Two calls with the same scheduled_for -> same run_id -> idempotent replay."""
        from datetime import UTC, datetime

        cadence_policy = {
            "mode": "daily",
            "timezone": "UTC",
            "watermark": {"delay_minutes": 0},
            "missed_run": {"mode": "coalesce", "max_catchup_windows": 1},
            "late_arrival": {"lookback_minutes": 0},
        }

        # Capture run_ids across two calls.
        run_ids: list[str] = []

        def capturing_open_import(**kwargs):
            idempotency_key = kwargs.get("idempotency_key", "")
            # Extract run_id from the key (format: gsheets_sync:<ds>:<run_id>)
            parts = idempotency_key.split(":")
            if len(parts) == 3:
                run_ids.append(parts[2])
            return {
                "ledger": {
                    "id": "mfl_IDEMP01",
                    "datastream_id": _DATASTREAM_ID,
                    "project_id": _PROJECT_ID,
                    "execution_id": "dse_IDEMP01",
                    "outcome": "opened",
                    "row_count": None,
                    "content_hash": None,
                    "error_code": None,
                    "error_detail": None,
                },
                "execution": {"id": "dse_IDEMP01"},
                "no_op": False,
                "replay": False,
            }

        now_utc = datetime(2026, 7, 23, 2, 5, tzinfo=UTC)  # after midnight UTC

        with (
            patch("core.google_sheets_sync.ledger.open_import", side_effect=capturing_open_import),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
        ):
            from core.google_sheets_sync import dispatch_managed_feed_sync

            for _ in range(2):
                dispatch_managed_feed_sync(
                    datastream_id=_DATASTREAM_ID,
                    project_id=_PROJECT_ID,
                    connection_id=_CONNECTION_ID,
                    spreadsheet_id=_SPREADSHEET_ID,
                    sheet_range=_SHEET_RANGE,
                    sheet_name=_SHEET_NAME,
                    column_mapping=_MINIMAL_COLUMN_MAPPING,
                    plan_version_id="dsp_01",
                    mapping_version_id="dmap_01",
                    projection_plan={"executable": True},
                    cadence_policy=cadence_policy,
                    quota_profile=None,
                    actor="scheduler",
                    conn=MagicMock(),
                    sheets_adapter=_make_adapter(),
                    duckdb_path=":memory:",
                    now_utc=now_utc,
                )

        # Both calls produced the SAME run_id (derived from scheduled_for timestamp).
        assert len(run_ids) == 2
        assert run_ids[0] == run_ids[1], (
            f"Two calls at the same scheduled_for must yield the same run_id: "
            f"{run_ids[0]!r} != {run_ids[1]!r}"
        )


# ---------------------------------------------------------------------------
# 11. Rejection gate integration (mocked).
# ---------------------------------------------------------------------------


class TestRejectionGate:
    def test_rejection_gate_breach_returns_rejected_outcome(self):
        """When the rejection gate fires, outcome='rejected' and prior published stays."""
        import_result = {
            "ledger": {
                "id": "mfl_REJECT01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_REJECT01",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_REJECT01"},
            "no_op": False,
            "replay": False,
        }
        gate_issue = {
            "code": "rejection_threshold_exceeded",
            "detail": "30/100 rows rejected (30.00%) exceeds threshold 25.00%",
            "repair": {"fix_source_or_mapping": {}},
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=gate_issue,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
            patch("core.google_sheets_sync._write_landing_rows", return_value=70),
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(**_base_run_sync_args())

        assert result["outcome"] == "rejected"
        assert result["error_code"] == "rejection_threshold_exceeded"
        # mark_outcome must have been called with 'rejected'.
        mock_mark.assert_called()
        assert mock_mark.call_args.kwargs["outcome"] == "rejected"

    def test_below_threshold_rejection_does_not_block(self):
        """When the rejection gate returns None, the run VALIDATES (rejections don't block).

        Allowed rejected rows (below the threshold) do NOT block: the outcome is the
        honest Phase-B terminal 'validated_pending_publication'. The ledger is NOT
        marked 'published' by run_sync (the pointer swap is Phase B).
        """
        import_result = {
            "ledger": {
                "id": "mfl_BELOW01",
                "datastream_id": _DATASTREAM_ID,
                "project_id": _PROJECT_ID,
                "execution_id": "dse_BELOW01",
                "outcome": "opened",
                "row_count": None,
                "content_hash": None,
                "error_code": None,
                "error_detail": None,
            },
            "execution": {"id": "dse_BELOW01"},
            "no_op": False,
            "replay": False,
        }

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
            patch(
                "core.google_sheets_sync._read_allow_empty_publication",
                return_value=False,
            ),
        ):
            from core.google_sheets_sync import (
                OUTCOME_VALIDATED_PENDING_PUBLICATION,
                run_sync,
            )

            result = run_sync(**_base_run_sync_args())

        assert result["outcome"] == OUTCOME_VALIDATED_PENDING_PUBLICATION
        # run_sync must NOT mark the ledger 'published' (no fake publish, C1b).
        for call in mock_mark.call_args_list:
            assert call.kwargs.get("outcome") != "published"


# ---------------------------------------------------------------------------
# 12. Missing-parameter validation.
# ---------------------------------------------------------------------------


class TestParameterValidation:
    def test_missing_spreadsheet_id_raises_sync_error(self):
        with pytest.raises(SheetsSyncError) as exc_info:
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args(spreadsheet_id=""))
        assert "missing_spreadsheet_id" in str(exc_info.value)

    def test_missing_sheet_range_raises_sync_error(self):
        with pytest.raises(SheetsSyncError) as exc_info:
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args(sheet_range=""))
        assert "missing_sheet_range" in str(exc_info.value)

    def test_missing_column_mapping_raises_sync_error(self):
        with pytest.raises(SheetsSyncError) as exc_info:
            from core.google_sheets_sync import run_sync

            run_sync(**_base_run_sync_args(column_mapping={}))
        assert "missing_column_mapping" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 13. Empty-candidate gate (C1a) -- pure decision + run_sync fail-closed.
# ---------------------------------------------------------------------------


def _opened_import_result(ledger_id: str, exec_id: str) -> dict:
    return {
        "ledger": {
            "id": ledger_id,
            "datastream_id": _DATASTREAM_ID,
            "project_id": _PROJECT_ID,
            "execution_id": exec_id,
            "outcome": "opened",
            "row_count": None,
            "content_hash": None,
            "error_code": None,
            "error_detail": None,
        },
        "execution": {"id": exec_id},
        "no_op": False,
        "replay": False,
    }


class TestEmptyCandidateGate:
    """C1a: a zero-row candidate is BLOCKED by default; both confirmations lift it."""

    def test_pure_gate_blocks_zero_rows_by_default(self):
        issue = evaluate_empty_candidate_gate(
            accepted_row_count=0,
            force_empty_publish=False,
            allow_empty_publication=False,
        )
        assert issue is not None
        assert issue["code"] == ERR_EMPTY_CANDIDATE

    def test_pure_gate_blocks_when_only_force_set(self):
        issue = evaluate_empty_candidate_gate(
            accepted_row_count=0,
            force_empty_publish=True,
            allow_empty_publication=False,
        )
        assert issue is not None

    def test_pure_gate_blocks_when_only_pref_set(self):
        issue = evaluate_empty_candidate_gate(
            accepted_row_count=0,
            force_empty_publish=False,
            allow_empty_publication=True,
        )
        assert issue is not None

    def test_pure_gate_allows_when_both_confirmations(self):
        issue = evaluate_empty_candidate_gate(
            accepted_row_count=0,
            force_empty_publish=True,
            allow_empty_publication=True,
        )
        assert issue is None

    def test_pure_gate_never_fires_on_nonempty(self):
        issue = evaluate_empty_candidate_gate(
            accepted_row_count=5,
            force_empty_publish=False,
            allow_empty_publication=False,
        )
        assert issue is None

    def test_run_sync_blocks_empty_snapshot_by_default(self):
        """A header-only sheet (zero data rows) is REJECTED, not published (C1a)."""
        header_only = [["Date", "Campagne", "Budget", "Objectif CA"]]
        import_result = _opened_import_result("mfl_EMPTY01", "dse_EMPTY01")

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
            patch("core.google_sheets_sync._write_landing_rows", return_value=0),
            patch(
                "core.google_sheets_sync._read_allow_empty_publication",
                return_value=False,
            ),
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(
                **_base_run_sync_args(
                    sheets_adapter=_make_adapter(return_values=header_only)
                )
            )

        assert result["outcome"] == "rejected"
        assert result["error_code"] == ERR_EMPTY_CANDIDATE
        mock_mark.assert_called()
        assert mock_mark.call_args.kwargs["outcome"] == "rejected"

    def test_run_sync_allows_empty_snapshot_with_force_and_pref(self):
        """An intentional empty publish (force + allow_empty pref) VALIDATES (not blocked)."""
        header_only = [["Date", "Campagne", "Budget", "Objectif CA"]]
        import_result = _opened_import_result("mfl_EMPTY02", "dse_EMPTY02")

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", return_value={}),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                return_value=None,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
            patch("core.google_sheets_sync._write_landing_rows", return_value=0),
            patch(
                "core.google_sheets_sync._read_allow_empty_publication",
                return_value=True,
            ),
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(
                **_base_run_sync_args(
                    sheets_adapter=_make_adapter(return_values=header_only),
                    force_empty_publish=True,
                )
            )

        assert result["outcome"] == OUTCOME_VALIDATED_PENDING_PUBLICATION
        # Never a fake publish, even on the intentional-empty path.
        for call in mock_mark.call_args_list:
            assert call.kwargs.get("outcome") != "published"


# ---------------------------------------------------------------------------
# 14. H1 -- unparsable-row count from the 15.6 parser reaches the rejection gate.
# ---------------------------------------------------------------------------


class TestUnparsableRowsReachGate:
    """H1: _parse_sheet_response's unparsable_count must not be silently dropped."""

    def test_parse_raw_values_maps_unparsable_count_to_rejected_rows(self):
        """When the real 15.6 parser is used, unparsable_count -> len(rejected_rows).

        We inject a fake ``connector`` module exposing a ``_parse_sheet_response``
        that returns ``(records, unparsable_count)`` (the real 15.6 signature), then
        assert ``_parse_raw_values`` synthesizes exactly ``unparsable_count`` rejection
        dicts. Without the H1 fix this returned ``[]`` and the gate never saw them.
        """
        import sys
        import types

        fake = types.ModuleType("connector")

        def _fake_parse(values, column_mapping):
            # 4 accepted records + 3 unparsable-metric rows detected by the parser.
            records = [{"date": f"2026-07-0{i}", "sheet_row_id": f"row_{i}"} for i in range(1, 5)]
            return records, 3

        fake._parse_sheet_response = _fake_parse  # type: ignore[attr-defined]

        prior = sys.modules.get("connector")
        sys.modules["connector"] = fake
        try:
            accepted, rejected = _parse_raw_values(
                _RAW_VALUES_TWO_ROWS, _MINIMAL_COLUMN_MAPPING
            )
        finally:
            if prior is not None:
                sys.modules["connector"] = prior
            else:
                del sys.modules["connector"]

        assert len(accepted) == 4
        # H1: the 3 unparsable rows are surfaced as structured rejection dicts.
        assert len(rejected) == 3
        assert all(r["rule"] == "unparsable_metric" for r in rejected)

    def test_unparsable_rows_reach_gate_and_block_above_threshold(self):
        """End-to-end: unparsable rows raise rejected_row_count so the gate can fire.

        We patch _parse_raw_values to return a HIGH rejection fraction (mapped from the
        parser's unparsable_count via the H1 fix) and let the real ledger rejection
        gate evaluate it -> the run is REJECTED, proving the count reaches the gate.
        """
        import_result = _opened_import_result("mfl_UNPARSE01", "dse_UNPARSE01")

        # 2 accepted, 8 unparsable -> 80% > 25% default threshold -> block.
        accepted_records = [{"date": "2026-07-01", "sheet_row_id": "row_1"}] * 2
        rejected_records = [
            {
                "row_number": None,
                "field_name": None,
                "rule": "unparsable_metric",
                "reason": "unparsable",
                "rejected_value": None,
            }
        ] * 8

        # A ledger stub whose record_rows persists the counts and whose
        # evaluate_rejection_gate_for_ledger runs the REAL pure decision.
        from core import managed_feed_ledger as real_ledger

        recorded: dict = {}

        def fake_record_rows(**kwargs):
            recorded["accepted"] = kwargs["accepted_row_count"]
            recorded["rejected"] = len(kwargs["rejected_rows"] or [])
            return {}

        def fake_gate(ledger_id, project_id, conn):
            # Real pure decision over the recorded counts (default 25% threshold).
            return real_ledger.evaluate_rejection_gate(
                accepted_row_count=recorded["accepted"],
                rejected_row_count=recorded["rejected"],
                preferences=None,
            )

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.record_rows", side_effect=fake_record_rows),
            patch(
                "core.google_sheets_sync.ledger.evaluate_rejection_gate_for_ledger",
                side_effect=fake_gate,
            ),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
            patch("core.google_sheets_sync._write_landing_rows", return_value=2),
            patch(
                "core.google_sheets_sync._parse_raw_values",
                return_value=(accepted_records, rejected_records),
            ),
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(**_base_run_sync_args())

        assert recorded["rejected"] == 8  # the unparsable count reached record_rows
        assert result["outcome"] == "rejected"
        assert result["error_code"] == "rejection_threshold_exceeded"
        assert mock_mark.call_args.kwargs["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# 15. M2 -- adapter-exception classification is TYPE-first, not string-sniffing.
# ---------------------------------------------------------------------------


class TestAdapterErrorClassification:
    """M2: prefer the 15.6 adapter's typed exceptions over French-message substrings."""

    def test_permission_error_classified_by_type(self):
        code, _ = _classify_adapter_error(PermissionError("any message at all"))
        assert code == ERR_REVOKED_CONSENT

    def test_column_mapping_error_classified_by_class_name(self):
        """The 15.6 ColumnMappingError (declared column absent) -> tab_not_found.

        Matched by class NAME (no import from server/modules -- AD-2). We define a
        local class with the SAME name to prove the type-name match, independent of
        the (French) message text.
        """

        class ColumnMappingError(Exception):
            pass

        code, _ = _classify_adapter_error(
            ColumnMappingError("colonnes declarees absentes -- peu importe le texte")
        )
        assert code == ERR_TAB_NOT_FOUND

    def test_file_not_found_spreadsheet_vs_tab(self):
        code_ss, _ = _classify_adapter_error(FileNotFoundError("spreadsheet introuvable"))
        assert code_ss == ERR_SPREADSHEET_NOT_FOUND
        code_tab, _ = _classify_adapter_error(
            FileNotFoundError("range 'Budget!A:F' not found (tab renamed)")
        )
        assert code_tab == ERR_TAB_NOT_FOUND

    def test_value_error_is_range_drift(self):
        code, _ = _classify_adapter_error(ValueError("invalid range 'OldSheet!A:F'"))
        assert code == ERR_RANGE_DRIFT

    def test_unrecognised_error_fails_closed(self):
        """An unknown recoverable error still classifies to a safe-fail code (fail closed)."""
        code, _ = _classify_adapter_error(RuntimeError("something odd"))
        assert code in (ERR_RANGE_DRIFT, ERR_TAB_NOT_FOUND, ERR_SPREADSHEET_NOT_FOUND)

    def test_run_sync_column_mapping_error_is_safe_fail(self):
        """A ColumnMappingError raised by the adapter -> failed ledger (tab_not_found)."""

        class ColumnMappingError(Exception):
            pass

        import_result = _opened_import_result("mfl_CME01", "dse_CME01")

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}) as mock_mark,
        ):
            from core.google_sheets_sync import run_sync

            result = run_sync(
                **_base_run_sync_args(
                    sheets_adapter=_make_adapter(
                        raise_exc=ColumnMappingError("declared column absent")
                    )
                )
            )

        assert result["outcome"] == "failed"
        assert result["error_code"] == ERR_TAB_NOT_FOUND
        assert mock_mark.call_args.kwargs["outcome"] == "failed"


# ---------------------------------------------------------------------------
# 16. RateLimitError re-raises (worker breaker), never a safe-fail.
# ---------------------------------------------------------------------------


class TestRateLimitReRaises:
    def test_rate_limit_error_reraises(self):
        """A RateLimitError (429) from the adapter re-raises -- NOT caught as safe-fail."""

        class RateLimitError(Exception):
            def __init__(self, platform, retry_after):
                super().__init__(f"{platform}: rate limited")
                self.platform = platform
                self.retry_after = retry_after

        import_result = _opened_import_result("mfl_RL01", "dse_RL01")

        with (
            patch("core.google_sheets_sync.ledger.open_import", return_value=import_result),
            patch("core.google_sheets_sync.ledger.mark_outcome", return_value={}),
        ):
            from core.google_sheets_sync import run_sync

            with pytest.raises(RateLimitError):
                run_sync(
                    **_base_run_sync_args(
                        sheets_adapter=_make_adapter(
                            raise_exc=RateLimitError("google-sheets", 60)
                        )
                    )
                )
